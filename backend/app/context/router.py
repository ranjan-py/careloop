"""Context-graph routes — live Neo4j reads (spec §18).

Queries run against the real Neo4j instance. On connection failure the routes
return 503 with a "graph sync degraded"-style detail (spec §2.4) — no canned
graph data, ever. Empty graph → empty arrays (honest pre-seed state).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.auth.session import require_clinician
from app.context.neo4j_client import get_driver

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/context", tags=["context"])

_DEGRADED_DETAIL = "Context graph degraded — Neo4j unreachable. Care workflow continues; graph view unavailable."


@router.get("/{patient_id}/graph")
async def get_patient_graph(patient_id: str, _=Depends(require_clinician)) -> dict:
    """Nodes + relationships in the patient's neighborhood (up to 2 hops)."""
    cypher = """
    MATCH (p:Patient {id: $patient_id})
    OPTIONAL MATCH (p)-[r*1..2]-(n)
    WITH p, collect(DISTINCT n) AS ns, collect(DISTINCT r) AS rels
    RETURN p, ns, rels
    """
    try:
        driver = get_driver()
        async with driver.session() as session:
            result = await session.run(cypher, {"patient_id": patient_id})
            record = await result.single()
    except Exception as exc:  # noqa: BLE001 — degraded, not fake (spec §2.4)
        logger.warning("Neo4j graph read failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DEGRADED_DETAIL)

    nodes: dict[str, dict] = {}
    relationships: list[dict] = []
    if record is not None and record["p"] is not None:
        raw_nodes = [record["p"], *(record["ns"] or [])]
        for node in raw_nodes:
            if node is None:
                continue
            nodes[node.element_id] = {
                "id": node.get("id", node.element_id),
                "labels": sorted(node.labels),
                "properties": dict(node),
            }
        for path_rels in record["rels"] or []:
            for rel in path_rels:
                relationships.append(
                    {
                        "id": rel.element_id,
                        "type": rel.type,
                        "start_id": rel.start_node.get("id", rel.start_node.element_id),
                        "end_id": rel.end_node.get("id", rel.end_node.element_id),
                        "properties": dict(rel),
                    }
                )
    return {"nodes": list(nodes.values()), "relationships": relationships}


@router.get("/{patient_id}/provenance/{item_id}")
async def get_provenance(patient_id: str, item_id: str, _=Depends(require_clinician)) -> dict:
    """Multi-hop provenance (spec §18): trace an instruction/action back through
    the recommendation to the transcript utterance/fact that produced it."""
    cypher = """
    MATCH (p:Patient {id: $patient_id})
    MATCH (item {id: $item_id})
    MATCH path = (item)-[:BASED_ON|GENERATED|RECOMMENDS|EXECUTED_AS|SUPPORTS|HAS_EVIDENCE|REPORTED|OCCURRED_IN*1..6]-(origin)
    WHERE (origin)-[:REPORTED|HAS_OBSERVATION|HAS_LAB|OCCURRED_IN]-(p) OR origin = p
    WITH path ORDER BY length(path) DESC LIMIT 1
    RETURN [rel IN relationships(path) | {
        relationship: type(rel),
        source: {id: coalesce(startNode(rel).id, elementId(startNode(rel))), labels: labels(startNode(rel)), properties: properties(startNode(rel))},
        target: {id: coalesce(endNode(rel).id, elementId(endNode(rel))), labels: labels(endNode(rel)), properties: properties(endNode(rel))}
    }] AS hops
    """
    try:
        driver = get_driver()
        async with driver.session() as session:
            result = await session.run(cypher, {"patient_id": patient_id, "item_id": item_id})
            record = await result.single()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Neo4j provenance read failed: %s", exc)
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, detail=_DEGRADED_DETAIL)

    hops = record["hops"] if record is not None else []
    return {"path": hops or []}
