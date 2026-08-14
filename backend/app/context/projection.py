"""Idempotent Neo4j projection of committed Postgres rows (spec §17).

Postgres is authoritative; every write here is a MERGE keyed on a stable id,
so replays and re-seeds converge instead of duplicating. All projected nodes
carry the marker label :Careloop — the wipe (rebuild_graph, seed) touches
ONLY careloop-labeled nodes.

The helpers take committed ORM rows, so the encounter-time AI loop reuses
them verbatim: after committing new Fact rows, call project_facts(rows) —
patient links, OCCURRED_IN encounter links, and CONFLICTS_WITH edges (when
Fact.conflicts_with is set) all come from the same code path as the seed.

Datetimes are projected as ISO-8601 strings (never neo4j temporal types) so
the graph API can serialize node properties straight to JSON.

On any Neo4j failure a ProjectionError is raised — callers surface the
degraded state honestly (spec §2.4); Postgres remains committed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.context.neo4j_client import get_driver
from app.db import models as m

logger = logging.getLogger(__name__)

MARKER_LABEL = "Careloop"

# fact_type → (node label, patient relationship). medication_status resolves
# its relationship from source_type: the EHR says the patient TAKES it; the
# patient REPORTED everything patient-sourced (spec §17 suggested vocab).
FACT_NODE_LABELS: dict[str, tuple[str, str]] = {
    "condition": ("Condition", "HAS_CONDITION"),
    "medication_status": ("Medication", "TAKES"),  # rel overridden per source_type
    "lab": ("LabResult", "HAS_LAB"),
    "observation": ("Observation", "HAS_OBSERVATION"),
    "symptom": ("Symptom", "REPORTED"),
    "care_gap": ("CareGap", "HAS_CARE_GAP"),
}


class ProjectionError(RuntimeError):
    """Neo4j projection failed. Postgres is still authoritative and committed —
    fix connectivity and replay with `make rebuild-graph`."""


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


async def _run_batches(batches: Sequence[tuple[str, dict]]) -> None:
    """Run (cypher, params) batches in one Neo4j session; wrap failures."""
    if not batches:
        return
    driver = get_driver()
    try:
        async with driver.session() as session:
            for cypher, params in batches:
                result = await session.run(cypher, params)
                await result.consume()
    except Exception as exc:  # noqa: BLE001 — single, clear failure surface
        raise ProjectionError(f"Neo4j projection failed: {type(exc).__name__}: {exc}") from exc


# ---------------------------------------------------------------------------
# Query builders (pure — one (cypher, params) tuple per batch)
# ---------------------------------------------------------------------------


def _patient_batch(patients: Iterable[m.Patient]) -> tuple[str, dict]:
    rows = [
        {
            "id": p.id,
            "name": p.name,
            "age": p.age,
            "sex": p.sex,
            "display_only": bool(getattr(p, "display_only", False)),
            "source_class": p.source_class,
        }
        for p in patients
    ]
    cypher = f"""
    UNWIND $rows AS r
    MERGE (p:{MARKER_LABEL}:Patient {{id: r.id}})
    SET p.name = r.name, p.age = r.age, p.sex = r.sex,
        p.display_only = r.display_only, p.source_class = r.source_class
    """
    return cypher, {"rows": rows}


def _clinician_batch(clinicians: Iterable[m.Clinician]) -> tuple[str, dict]:
    rows = [{"id": c.id, "name": c.name, "email": c.email, "role": c.role} for c in clinicians]
    cypher = f"""
    UNWIND $rows AS r
    MERGE (c:{MARKER_LABEL}:Clinician {{id: r.id}})
    SET c.name = r.name, c.email = r.email, c.role = r.role
    """
    return cypher, {"rows": rows}


def _fact_params(fact: m.Fact) -> dict:
    return {
        "id": fact.id,
        "patient_id": fact.patient_id,
        "encounter_id": fact.encounter_id,
        "fact_type": fact.fact_type,
        "subject": fact.subject,
        "value": fact.value,
        "source_type": fact.source_type,
        "source_class": fact.source_class,
        "method": fact.method,
        "reported_at": _iso(fact.reported_at),
        "confidence": fact.confidence,
        "verification_status": fact.verification_status,
        "conflicts_with": fact.conflicts_with,
    }


def fact_batches(facts: Sequence[m.Fact]) -> list[tuple[str, dict]]:
    """MERGE batches for Fact rows: typed node + patient link, OCCURRED_IN
    encounter links, and CONFLICTS_WITH edges. Used by seed AND the AI loop."""
    groups: dict[tuple[str, str], list[dict]] = {}
    for fact in facts:
        label, rel = FACT_NODE_LABELS.get(fact.fact_type, ("Observation", "HAS_OBSERVATION"))
        if fact.fact_type == "medication_status" and fact.source_type != "ehr":
            rel = "REPORTED"
        groups.setdefault((label, rel), []).append(_fact_params(fact))

    batches: list[tuple[str, dict]] = []
    for (label, rel), rows in groups.items():
        cypher = f"""
        UNWIND $rows AS r
        MERGE (p:{MARKER_LABEL}:Patient {{id: r.patient_id}})
        MERGE (n:{MARKER_LABEL}:{label} {{id: r.id}})
        SET n.name = r.subject, n.value = r.value, n.fact_type = r.fact_type,
            n.source_type = r.source_type, n.source_class = r.source_class,
            n.method = r.method, n.reported_at = r.reported_at,
            n.confidence = r.confidence, n.verification_status = r.verification_status
        MERGE (p)-[:{rel}]->(n)
        """
        batches.append((cypher, {"rows": rows}))

    encounter_rows = [_fact_params(f) for f in facts if f.encounter_id]
    if encounter_rows:
        batches.append(
            (
                f"""
                UNWIND $rows AS r
                MERGE (e:{MARKER_LABEL}:Encounter {{id: r.encounter_id}})
                WITH r, e
                MATCH (n:{MARKER_LABEL} {{id: r.id}})
                MERGE (n)-[:OCCURRED_IN]->(e)
                """,
                {"rows": encounter_rows},
            )
        )

    conflict_rows = [_fact_params(f) for f in facts if f.conflicts_with]
    if conflict_rows:
        batches.append(
            (
                f"""
                UNWIND $rows AS r
                MATCH (a:{MARKER_LABEL} {{id: r.id}})
                MATCH (b:{MARKER_LABEL} {{id: r.conflicts_with}})
                MERGE (a)-[:CONFLICTS_WITH]->(b)
                """,
                {"rows": conflict_rows},
            )
        )
    return batches


def _timeline_encounter_batch(events: Iterable[m.TimelineEvent]) -> tuple[str, dict]:
    rows = [
        {
            "id": e.id,
            "patient_id": e.patient_id,
            "label": e.label,
            "occurred_at": _iso(e.occurred_at),
            "detail": e.detail,
        }
        for e in events
    ]
    cypher = f"""
    UNWIND $rows AS r
    MERGE (p:{MARKER_LABEL}:Patient {{id: r.patient_id}})
    MERGE (e:{MARKER_LABEL}:Encounter {{id: r.id}})
    SET e.label = r.label, e.occurred_at = r.occurred_at, e.detail = r.detail
    MERGE (p)-[:HAS_ENCOUNTER]->(e)
    """
    return cypher, {"rows": rows}


def _encounter_batch(encounters: Iterable[m.Encounter]) -> tuple[str, dict]:
    rows = [
        {
            "id": e.id,
            "patient_id": e.patient_id,
            "status": e.status,
            "mode": e.mode,
            "started_at": _iso(e.started_at),
            "ended_at": _iso(e.ended_at),
        }
        for e in encounters
    ]
    cypher = f"""
    UNWIND $rows AS r
    MERGE (p:{MARKER_LABEL}:Patient {{id: r.patient_id}})
    MERGE (e:{MARKER_LABEL}:Encounter {{id: r.id}})
    SET e.status = r.status, e.mode = r.mode,
        e.started_at = r.started_at, e.ended_at = r.ended_at
    MERGE (p)-[:HAS_ENCOUNTER]->(e)
    """
    return cypher, {"rows": rows}


def _care_gap_batch(patient_id: str, care_gaps: Sequence[dict]) -> tuple[str, dict]:
    rows = [
        {"id": g.get("id"), "label": g.get("label"), "detail": g.get("detail")}
        for g in care_gaps
        if g.get("id")
    ]
    cypher = f"""
    UNWIND $rows AS r
    MERGE (p:{MARKER_LABEL}:Patient {{id: $patient_id}})
    MERGE (g:{MARKER_LABEL}:CareGap {{id: r.id}})
    SET g.label = r.label, g.detail = r.detail
    MERGE (p)-[:HAS_CARE_GAP]->(g)
    """
    return cypher, {"rows": rows, "patient_id": patient_id}


# ---------------------------------------------------------------------------
# Public projection API
# ---------------------------------------------------------------------------


async def project_patients(patients: Sequence[m.Patient]) -> None:
    if patients:
        await _run_batches([_patient_batch(patients)])


async def project_clinicians(clinicians: Sequence[m.Clinician]) -> None:
    if clinicians:
        await _run_batches([_clinician_batch(clinicians)])


async def project_facts(facts: Sequence[m.Fact]) -> None:
    """Project committed Fact rows — shared by seed and the AI loop."""
    await _run_batches(fact_batches(facts))


async def project_timeline_encounters(events: Sequence[m.TimelineEvent]) -> None:
    encounter_events = [e for e in events if e.event_type == "encounter"]
    if encounter_events:
        await _run_batches([_timeline_encounter_batch(encounter_events)])


async def project_encounters(encounters: Sequence[m.Encounter]) -> None:
    if encounters:
        await _run_batches([_encounter_batch(encounters)])


async def project_care_gaps(patient_id: str, care_gaps: Sequence[dict]) -> None:
    if care_gaps:
        await _run_batches([_care_gap_batch(patient_id, care_gaps)])


async def wipe_projection() -> None:
    """Delete ONLY careloop-labeled nodes (and their relationships)."""
    await _run_batches([(f"MATCH (n:{MARKER_LABEL}) DETACH DELETE n", {})])


async def projection_counts() -> dict[str, int]:
    driver = get_driver()
    try:
        async with driver.session() as session:
            node_result = await session.run(f"MATCH (n:{MARKER_LABEL}) RETURN count(n) AS c")
            node_record = await node_result.single()
            rel_result = await session.run(
                f"MATCH (:{MARKER_LABEL})-[r]->(:{MARKER_LABEL}) RETURN count(r) AS c"
            )
            rel_record = await rel_result.single()
    except Exception as exc:  # noqa: BLE001
        raise ProjectionError(f"Neo4j count query failed: {type(exc).__name__}: {exc}") from exc
    return {
        "nodes": node_record["c"] if node_record else 0,
        "relationships": rel_record["c"] if rel_record else 0,
    }


async def replay_from_postgres(session: AsyncSession, *, wipe: bool = True) -> dict[str, int]:
    """Rebuild the whole projection from authoritative Postgres rows.

    Reads committed rows, optionally wipes the careloop projection, then
    MERGEs everything back. Returns projected node/relationship counts.
    """
    patients = (await session.execute(select(m.Patient))).scalars().all()
    clinicians = (await session.execute(select(m.Clinician))).scalars().all()
    facts = (await session.execute(select(m.Fact))).scalars().all()
    timeline_events = (
        await session.execute(select(m.TimelineEvent).where(m.TimelineEvent.event_type == "encounter"))
    ).scalars().all()
    encounters = (await session.execute(select(m.Encounter))).scalars().all()

    if wipe:
        await wipe_projection()

    await project_patients(patients)
    await project_clinicians(clinicians)
    await project_timeline_encounters(timeline_events)
    await project_encounters(encounters)
    await project_facts(facts)
    for patient in patients:
        await project_care_gaps(patient.id, patient.care_gaps or [])

    counts = await projection_counts()
    logger.info(
        "Neo4j projection replayed: %d nodes, %d relationships",
        counts["nodes"],
        counts["relationships"],
    )
    return counts
