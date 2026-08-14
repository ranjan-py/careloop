"""Idempotent demo seed — `python -m app.context.seed` (spec §30).

Pipeline: scenario-overlay FHIR bundle (already runtime-relative, spec §6.1)
→ fhir_ingest → authoritative Postgres rows → Neo4j projection (spec §17).

Idempotency: every seeded row has a DETERMINISTIC id (bundle ids, file ids,
or fixed fb-/tl-/gap- prefixes). Leaf rows are delete+reinserted scoped to
exactly the seeded entities; FK-referenced identity rows (patients,
clinician) are upserted in place so runtime rows that reference them
(encounters, care plans) survive a re-seed.

Exit codes: 0 = seeded + projected; 1 = data/Postgres failure (nothing
committed); 2 = Postgres committed but the Neo4j projection FAILED — run
`make rebuild-graph` after fixing connectivity.

All data is synthetic — "Synthetic clinical AI prototype — not for patient care."
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import delete, text

from app.auth.session import DEMO_CLINICIAN_ID, DEMO_CLINICIAN_NAME, DEMO_CLINICIAN_ROLE
from app.config import get_settings
from app.context import fhir_ingest
from app.context.neo4j_client import close_driver
from app.context.projection import ProjectionError, replay_from_postgres
from app.db import models as m
from app.db.session import dispose_engine, get_engine, get_session_factory, init_db
from app.schemas.core import CareGap, PriorityItem, RejectionCategory

BUNDLE_RELPATH = Path("scenario_overlay/john_miller_bundle.json")
SECONDARY_RELPATH = Path("scenario_overlay/secondary_patients.json")
EVIDENCE_RELPATH = Path("evidence/evidence_corpus.json")
FEEDBACK_RELPATH = Path("historical_feedback.json")

FEEDBACK_ID_PREFIX = "fb-"
EVIDENCE_ID_PREFIX = "ev-"


# ---------------------------------------------------------------------------
# Historical feedback: deterministic expansion of the precomputed aggregates
# ---------------------------------------------------------------------------
#
# data/historical_feedback.json ships AGGREGATES only (no raw rows), while the
# analytics router (app/analytics/router.py) aggregates FeedbackAnalyticsEvent
# rows: decision ∈ {approved, modified, rejected} for the mix, rejection_category
# (shared enum wire values, rejected rows only — modified rows carry free-text
# reasons, so the column stays NULL) for the reasons chart, and
# recommendation_title for the high-friction table.
#
# The file's aggregates are MUTUALLY INCONSISTENT: its four high-friction
# titles alone imply 1,200 approved events (1,490 recommended − 290
# modify/reject), but its decision mix allows only 1,184 approved — and its
# remaining 240 events would need 256 modify/rejects (>100%). No set of raw
# rows can reproduce every number, so this expansion preserves, in priority
# order: (1) the spec-§23-pinned high-friction display — exact titles, counts
# and 38/17/12/4 modify/reject percentages, in order, with each pinned title's
# top rejection reason; (2) the reasons ranking (counts scaled to the smaller
# rejected total); (3) total_events = 1,730 exactly. The decision mix lands at
# 82.9 / 10.4 / 6.7 instead of the file's 68.4 / 19.1 / 12.4 — flagged here
# and in the seed report; _verify_feedback_source() fails loudly if the data
# file ever changes.

_REASON_ORDER = [
    RejectionCategory.PATIENT_LIMITATION,
    RejectionCategory.MISSING_PATIENT_INFORMATION,
    RejectionCategory.CLINICAL_DISAGREEMENT,
    RejectionCategory.OPERATIONAL_WORKFLOW_LIMITATION,
    RejectionCategory.ORGANIZATION_PROTOCOL_CONSTRAINT,
    RejectionCategory.REQUIRES_SUPERVISION_ESCALATION,
    RejectionCategory.OTHER,
]

# (title, slug, total, modified, rejected-per-category in _REASON_ORDER, orgs_observed)
# Tail titles are low-volume/low-friction so the four pinned titles stay at the
# top of the router's pct-sorted high-friction table (2.2–2.9% < 4%).
_FEEDBACK_EXPANSION: list[tuple[str, str, int, int, tuple[int, ...], int]] = [
    ("Home BP twice daily", "home-bp", 400, 92, (26, 8, 8, 7, 4, 4, 3), 4),
    ("Medication titration", "med-titration", 540, 56, (4, 14, 7, 4, 3, 2, 2), 4),
    ("Specialist referral", "referral", 300, 22, (1, 2, 3, 2, 4, 1, 1), 3),
    ("Updated lab", "updated-lab", 250, 6, (0, 1, 0, 3, 0, 0, 0), 4),
    ("Follow-up scheduling", "follow-up-scheduling", 90, 1, (0, 0, 0, 0, 0, 1, 0), 4),
    ("Patient education materials", "patient-education", 80, 1, (0, 0, 0, 0, 0, 0, 1), 4),
    ("Outreach phone check-in", "outreach-check-in", 70, 2, (0, 0, 0, 0, 0, 0, 0), 4),
]


def _verify_feedback_source(src: dict) -> None:
    """Fail loudly if historical_feedback.json diverges from this expansion."""
    total = sum(row[2] for row in _FEEDBACK_EXPANSION)
    if total != src.get("total_events"):
        raise ValueError(
            f"Feedback expansion covers {total} events but the file declares "
            f"{src.get('total_events')} — update _FEEDBACK_EXPANSION."
        )
    by_title = {row[0]: row for row in _FEEDBACK_EXPANSION}
    for pinned in src.get("high_friction", []):
        row = by_title.get(pinned["action_title"])
        if row is None:
            raise ValueError(f"High-friction title {pinned['action_title']!r} missing from expansion.")
        _, _, row_total, modified, rejected_by_cat, orgs_observed = row
        modify_reject = modified + sum(rejected_by_cat)
        if row_total != pinned["recommended_count"] or modify_reject != pinned["modify_reject_count"]:
            raise ValueError(
                f"{pinned['action_title']!r}: expansion has {row_total}/{modify_reject} "
                f"but the file pins {pinned['recommended_count']}/{pinned['modify_reject_count']}."
            )
        top_idx = max(range(len(rejected_by_cat)), key=rejected_by_cat.__getitem__)
        if _REASON_ORDER[top_idx].value != pinned["top_reason_category"]:
            raise ValueError(
                f"{pinned['action_title']!r}: top rejection reason "
                f"{_REASON_ORDER[top_idx].value!r} != pinned {pinned['top_reason_category']!r}."
            )
        if orgs_observed != pinned.get("organizations_observed", orgs_observed):
            raise ValueError(f"{pinned['action_title']!r}: organizations_observed mismatch.")
    # Reasons ranking must match the file's ordering (counts are scaled down
    # proportionally to the expansion's rejected total — see module comment).
    file_order = [r["category"] for r in src.get("reasons", [])]
    totals = {
        cat.value: sum(row[4][i] for row in _FEEDBACK_EXPANSION)
        for i, cat in enumerate(_REASON_ORDER)
    }
    expansion_order = sorted(totals, key=lambda c: totals[c], reverse=True)
    if file_order != expansion_order:
        raise ValueError(
            f"Reasons ranking drifted: file {file_order} vs expansion {expansion_order}."
        )


def expand_feedback_events(src: dict, *, now: datetime) -> list[dict]:
    """Deterministic FeedbackAnalyticsEvent rows (ids fb-<slug>-<n>)."""
    _verify_feedback_source(src)
    orgs = src.get("organizations") or ["Synthetic demo organization"]
    window_days = int(src.get("window_days", 180))
    rows: list[dict] = []
    for t_idx, (title, slug, total, modified, rejected_by_cat, orgs_observed) in enumerate(
        _FEEDBACK_EXPANSION
    ):
        decisions: list[tuple[str, str | None]] = []
        for cat, count in zip(_REASON_ORDER, rejected_by_cat):
            decisions.extend(("rejected", cat.value) for _ in range(count))
        decisions.extend(("modified", None) for _ in range(modified))
        decisions.extend(("approved", None) for _ in range(total - len(decisions)))
        for i, (decision, category) in enumerate(decisions):
            rows.append(
                {
                    "id": f"{FEEDBACK_ID_PREFIX}{slug}-{i:04d}",
                    "recommendation_title": title,
                    "decision": decision,
                    "rejection_category": category,
                    "organization": orgs[(i + t_idx) % min(orgs_observed, len(orgs))],
                    "recorded_at": now
                    - timedelta(days=(i * 37 + t_idx * 11) % window_days, minutes=(i * 733) % 1440),
                }
            )
    return rows


# ---------------------------------------------------------------------------
# John Miller scenario copy — priorities / care gaps (canonical gap list, §7/§22.1)
# ---------------------------------------------------------------------------


def _days_old(now: datetime, then: datetime | None) -> int:
    return (now - then).days if then else 0


def build_priorities_and_gaps(
    chart: fhir_ingest.ChartIngest, *, now: datetime
) -> tuple[list[dict], list[dict]]:
    """The canonical 'What matters today?' list (spec §7) — also the expected-gap
    list for the §22.1 completeness evaluator. Ages derive from bundle timestamps."""
    bps = sorted(chart.bp_readings, key=lambda b: b.observed_at)
    bp_detail = (
        "Clinic readings "
        + " and ".join(f"{b.systolic:g}/{b.diastolic:g} ({_days_old(now, b.observed_at)}d ago)" for b in bps)
        + " — recurring around 150/95"
        if bps
        else "Recurring elevated clinic readings around 150/95"
    )
    med = next((med for med in chart.medications if med.name == "lisinopril"), None)
    med_detail = (
        f"EHR lists {med.name} {med.dose or ''} as {med.ehr_status} — confirm current use with the patient".replace("  ", " ")
        if med
        else "EHR medication list needs confirmation with the patient"
    )
    potassium = next((lab for lab in chart.labs if lab.name == "Potassium"), None)
    egfr = next((lab for lab in chart.labs if lab.name == "eGFR"), None)
    renal_detail = (
        f"Potassium {_days_old(now, potassium.observed_at) if potassium else '?'}d old; "
        f"eGFR {egfr.value_display if egfr else 'stale'} is "
        f"{_days_old(now, egfr.observed_at) if egfr else '?'}d old — no renal panel since"
    )
    missed = chart.missed_followups[0] if chart.missed_followups else None
    followup_detail = (
        f"Hypertension follow-up missed {_days_old(now, missed.start)}d ago; nothing rescheduled"
        if missed
        else "Follow-up overdue; nothing rescheduled"
    )

    priorities = [
        PriorityItem(id="prio-bp-uncontrolled", label="Blood pressure uncontrolled", detail=bp_detail, severity="high"),
        PriorityItem(id="prio-med-status", label="Medication status needs confirmation", detail=med_detail, severity="high"),
        PriorityItem(id="prio-renal-labs", label="Renal labs stale", detail=renal_detail, severity="medium"),
        PriorityItem(id="prio-followup-overdue", label="Follow-up overdue", detail=followup_detail, severity="medium"),
    ]
    care_gaps = [
        CareGap(id="gap-bp-uncontrolled", label="Blood pressure uncontrolled", detail=bp_detail),
        CareGap(id="gap-med-status", label="Medication status needs confirmation", detail=med_detail),
        CareGap(id="gap-renal-labs", label="Renal labs stale", detail=renal_detail),
        CareGap(id="gap-followup-overdue", label="Follow-up overdue", detail=followup_detail),
    ]
    return (
        [p.model_dump(mode="json") for p in priorities],
        [g.model_dump(mode="json") for g in care_gaps],
    )


# ---------------------------------------------------------------------------
# Seed orchestration
# ---------------------------------------------------------------------------


async def _ensure_display_only_column() -> None:
    """create_all never ALTERs an existing table; patch older databases so the
    Patient.display_only column (contract: PatientListItem.display_only) exists."""
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        return
    async with engine.begin() as conn:
        await conn.execute(
            text("ALTER TABLE patients ADD COLUMN IF NOT EXISTS display_only BOOLEAN NOT NULL DEFAULT FALSE")
        )


async def seed() -> int:
    settings = get_settings()
    data_dir = Path(settings.data_dir)
    now = datetime.now(timezone.utc)

    # ---- Load and parse every input BEFORE touching the database ----------
    try:
        bundle = fhir_ingest.load_bundle(data_dir / BUNDLE_RELPATH)
        chart = fhir_ingest.parse_bundle(bundle)
        with open(data_dir / SECONDARY_RELPATH, encoding="utf-8") as fh:
            secondary = json.load(fh)["patients"]
        with open(data_dir / EVIDENCE_RELPATH, encoding="utf-8") as fh:
            evidence = json.load(fh)["snippets"]
        with open(data_dir / FEEDBACK_RELPATH, encoding="utf-8") as fh:
            feedback_src = json.load(fh)
        feedback_rows = expand_feedback_events(feedback_src, now=now)
    except (OSError, ValueError, KeyError) as exc:
        print(f"seed FAILED reading inputs from {data_dir}: {exc}", file=sys.stderr)
        return 1

    patient_row = fhir_ingest.build_patient_row(chart, now=now)
    priorities, care_gaps = build_priorities_and_gaps(chart, now=now)
    patient_row.update(
        priorities=priorities,
        care_gaps=care_gaps,
        display_only=False,
        fhir_bundle_ref=str(BUNDLE_RELPATH),
    )
    fact_rows = fhir_ingest.build_chart_facts(chart, ingested_at=now)
    timeline_rows = fhir_ingest.build_timeline_events(chart)

    secondary_rows = [
        {
            "id": sp["id"],
            "name": sp["name"],
            "age": sp["age"],
            "sex": sp["sex"],
            "conditions": sp.get("conditions", []),
            "medications": [],
            "priorities": [],
            "care_gaps": [],
            "appointment_time": datetime.fromisoformat(sp["appointment_time"].replace("Z", "+00:00"))
            if sp.get("appointment_time")
            else None,
            "source_class": "synthea_ehr",
            "display_only": True,
            "fhir_bundle_ref": str(SECONDARY_RELPATH),
        }
        for sp in secondary
    ]
    patient_ids = [patient_row["id"]] + [row["id"] for row in secondary_rows]

    if chart.background_active_medications:
        leftovers = ", ".join(
            f"{med.name} ({med.ehr_status})" for med in chart.background_active_medications
        )
        print(
            f"note: skipped background Synthea MedicationRequests still marked active: {leftovers} "
            "— spec §6.2 pins the medication state to the overlay (lisinopril + metformin).",
            file=sys.stderr,
        )

    # ---- Postgres: one transaction, delete+reinsert scoped to seeded ids --
    if not await init_db():
        print("seed FAILED: app Postgres unreachable — nothing was written.", file=sys.stderr)
        return 1
    try:
        await _ensure_display_only_column()
        async with get_session_factory()() as session:
            # Identity rows referenced by runtime FKs (encounters, care plans)
            # are UPSERTED in place — same deterministic ids, all fields
            # rewritten — so a re-seed never breaks referential integrity.
            await session.merge(
                m.Clinician(
                    id=DEMO_CLINICIAN_ID,
                    email=settings.demo_user_email,
                    name=DEMO_CLINICIAN_NAME,
                    role=DEMO_CLINICIAN_ROLE,
                )
            )
            for row in [patient_row, *secondary_rows]:
                await session.merge(m.Patient(**row))

            # Leaf rows: delete+reinsert, scoped to exactly what the seed owns.
            await session.execute(
                delete(m.TimelineEvent).where(
                    m.TimelineEvent.patient_id.in_(patient_ids),
                    m.TimelineEvent.id.like(f"{fhir_ingest.TIMELINE_ID_PREFIX}%"),
                )
            )
            # Chart facts are exactly: seeded patients + no encounter linkage +
            # synthea_ehr provenance. Encounter-time facts (patient_report /
            # encounter-linked) are never touched.
            await session.execute(
                delete(m.Fact).where(
                    m.Fact.patient_id.in_(patient_ids),
                    m.Fact.encounter_id.is_(None),
                    m.Fact.source_class == "synthea_ehr",
                )
            )
            await session.execute(
                delete(m.EvidenceSnippet).where(m.EvidenceSnippet.id.like(f"{EVIDENCE_ID_PREFIX}%"))
            )
            await session.execute(
                delete(m.FeedbackAnalyticsEvent).where(
                    m.FeedbackAnalyticsEvent.id.like(f"{FEEDBACK_ID_PREFIX}%")
                )
            )

            session.add_all(m.TimelineEvent(**row) for row in timeline_rows)
            session.add_all(m.Fact(**row) for row in fact_rows)
            session.add_all(
                m.EvidenceSnippet(
                    id=snippet["id"],
                    title=snippet["title"],
                    body=snippet["body"],
                    topic_tags=snippet.get("topic_tags", []),
                )
                for snippet in evidence
            )
            session.add_all(m.FeedbackAnalyticsEvent(**row) for row in feedback_rows)
            await session.commit()
    except Exception as exc:  # noqa: BLE001 — one honest failure surface
        print(f"seed FAILED writing to Postgres: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    decision_counts = {"approved": 0, "modified": 0, "rejected": 0}
    for row in feedback_rows:
        decision_counts[row["decision"]] += 1

    # ---- Neo4j projection (module 3) — after the Postgres commit ----------
    graph_summary = ""
    graph_failed = False
    try:
        async with get_session_factory()() as session:
            counts = await replay_from_postgres(session, wipe=True)
        graph_summary = f"nodes={counts['nodes']} relationships={counts['relationships']}"
    except ProjectionError as exc:
        graph_failed = True
        graph_summary = "FAILED"
        print(
            f"graph projection FAILED — run make rebuild-graph after fixing: {exc}",
            file=sys.stderr,
        )

    rows = [
        ("clinicians", "1", ""),
        ("patients", str(1 + len(secondary_rows)), f"{patient_row['id']} + {len(secondary_rows)} display-only"),
        ("timeline_events", str(len(timeline_rows)), "labs, vitals, missed follow-up, encounters"),
        ("chart_facts", str(len(fact_rows)), "ehr / synthea_ehr / confirmed"),
        ("evidence_snippets", str(len(evidence)), ""),
        (
            "feedback_events",
            str(len(feedback_rows)),
            f"approved {decision_counts['approved']} / modified {decision_counts['modified']}"
            f" / rejected {decision_counts['rejected']}",
        ),
        ("neo4j_projection", graph_summary, "" if not graph_failed else "Postgres committed; graph degraded"),
    ]
    width = max(len(r[0]) for r in rows)
    print("\nCareLoop seed summary — synthetic demo data (not for patient care)")
    print("-" * 72)
    for name, count, note in rows:
        line = f"  {name:<{width}}  {count:>6}"
        print(f"{line}  {note}" if note else line)
    print("-" * 72)
    return 2 if graph_failed else 0


async def _main() -> int:
    try:
        return await seed()
    finally:
        await close_driver()
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
