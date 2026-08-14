"""Runtime-state reset — `python -m app.context.reset_runtime`.

Deletes everything encounter-derived (encounters, transcript segments,
encounter facts, suggestions, care plans/actions/decisions, tool executions,
trigger logs) while KEEPING the seeded chart (patients, chart facts, timeline,
evidence, feedback analytics). Then replays the Neo4j projection so the graph
carries no stale nodes. This is the fast between-rehearsals reset (spec §30:
"a fresh encounter always behaves like the first"); `make reset-demo` remains
the full golden-snapshot path (volume recreation + re-seed).
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import delete, select

from app.context import rebuild_graph
from app.db import models as m
from app.db.session import get_session_factory


async def reset_runtime() -> dict[str, int]:
    session_factory = get_session_factory()
    counts: dict[str, int] = {}
    async with session_factory() as session:
        # FK-safe order: every table referencing encounters BEFORE encounters.
        stmts = [
            ("tool_executions", delete(m.ToolExecution)),
            ("decisions", delete(m.Decision)),
            ("care_plan_actions", delete(m.CarePlanAction)),
            ("care_plans", delete(m.CarePlan)),
            ("suggestions", delete(m.Suggestion)),
            ("transcript_segments", delete(m.TranscriptSegment)),
            ("encounter_facts", delete(m.Fact).where(m.Fact.encounter_id.is_not(None))),
            ("eval_results", delete(m.EvalResult)),
        ]
        if hasattr(m, "TriggerLog"):  # exists once the AI-core models landed
            stmts.append(("trigger_log", delete(m.TriggerLog)))
        stmts.append(("encounters", delete(m.Encounter)))
        for label, stmt in stmts:
            result = await session.execute(stmt)
            counts[label] = result.rowcount or 0
        # Seeded chart facts may have been marked disputed by a prior run —
        # restore their pristine state (they are EHR-confirmed in the seed).
        chart_facts = (
            await session.execute(select(m.Fact).where(m.Fact.encounter_id.is_(None)))
        ).scalars().all()
        restored = 0
        for fact in chart_facts:
            if fact.verification_status != "confirmed":
                fact.verification_status = "confirmed"
                restored += 1
        counts["chart_facts_restored"] = restored
        await session.commit()
    return counts


async def main() -> int:
    counts = await reset_runtime()
    print("runtime reset — deleted:", ", ".join(f"{k}={v}" for k, v in counts.items()))
    # Replay the projection so Neo4j drops stale encounter-derived nodes.
    code = await rebuild_graph.rebuild()
    if code != 0:
        print("graph rebuild FAILED — run make rebuild-graph after fixing", file=sys.stderr)
        return 2
    print("graph projection rebuilt")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
