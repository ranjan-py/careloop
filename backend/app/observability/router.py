"""AI Operations trace summary — GET /api/ops/trace-summary/{encounter_id}.

Computed from the app's OWN Postgres records, never live Langfuse queries
(v4 ingestion is async — spec §19/§21A). Every pipeline step gets a row with
PASS/DEGRADED/BLOCKED/DENIED status, real counts in the detail, and latency
where the persisted timestamps honestly support one. Permission denials
(blocked tool executions) surface as their own DENIED rows (spec §15/§21A).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.session import require_clinician
from app.db.session import get_session
from app.evals.evaluators import EncounterSnapshot, latency_metrics, load_snapshot, percentile
from app.schemas.core import TraceSummaryRow

router = APIRouter(prefix="/api/ops", tags=["observability"])


def _ms(seconds: float | None) -> float | None:
    return round(seconds * 1000.0, 1) if seconds is not None else None


def build_trace_summary(snapshot: EncounterSnapshot) -> list[TraceSummaryRow]:
    """Pure row builder over the evaluation snapshot (unit-testable)."""
    enc = snapshot.encounter
    metrics = latency_metrics(snapshot)
    rows: list[TraceSummaryRow] = []

    # 1) Context load — the seeded chart facts available to the encounter.
    n_chart = len(snapshot.chart_facts)
    rows.append(
        TraceSummaryRow(
            step="Context load",
            status="PASS" if n_chart > 0 else "DEGRADED",
            detail=(
                f"{n_chart} chart facts loaded for patient {enc.patient_id} "
                f"(trace {enc.trace_id or 'unset'})"
                if n_chart
                else f"No chart facts for patient {enc.patient_id} — seed missing?"
            ),
        )
    )

    # 2) Live transcription.
    n_seg = len(snapshot.segments)
    rows.append(
        TraceSummaryRow(
            step="Live transcription",
            status="PASS" if n_seg > 0 else "DEGRADED",
            detail=(
                f"{n_seg} finalized segments persisted (mode={enc.mode or 'unset'})"
                + (
                    "; first segment includes session setup time"
                    if metrics["first_transcript_s"] is not None
                    else ""
                )
                if n_seg
                else "No transcript segments persisted (no session run yet)"
            ),
            latency_ms=_ms(metrics["first_transcript_s"]),
        )
    )

    # 3) Fact extraction — encounter facts + §9 trigger stats.
    n_facts = len(snapshot.facts)
    fired = sum(
        1 for t in snapshot.trigger_logs
        if t.stage == "extraction" and t.decision == "fired"
    )
    skipped = sum(
        1 for t in snapshot.trigger_logs
        if t.stage == "extraction" and t.decision == "skipped"
    )
    state_lat = metrics["state_update_s"]
    rows.append(
        TraceSummaryRow(
            step="Fact extraction",
            status="PASS" if n_facts > 0 else "DEGRADED",
            detail=(
                f"{n_facts} encounter facts persisted; triggers: {fired} fired / "
                f"{skipped} skipped"
                if n_facts
                else f"No encounter facts (triggers: {fired} fired / {skipped} skipped)"
            ),
            latency_ms=_ms(percentile(state_lat, 0.5) if state_lat else None),
        )
    )

    # 4) Live suggestions (next best question / action).
    n_sugg = len(snapshot.suggestions)
    by_status: dict[str, int] = {}
    for s in snapshot.suggestions:
        by_status[s.status] = by_status.get(s.status, 0) + 1
    nba_p95 = metrics["nba_p95_s"]
    rows.append(
        TraceSummaryRow(
            step="Live suggestions",
            status="PASS" if n_sugg > 0 else "DEGRADED",
            detail=(
                f"{n_sugg} suggestions ("
                + ", ".join(f"{v} {k}" for k, v in sorted(by_status.items()))
                + ")"
                if n_sugg
                else "No suggestions generated"
            ),
            latency_ms=_ms(nba_p95),
        )
    )

    # 5) Encounter summary (spec §12 step 4).
    has_summary = bool(getattr(enc, "summary", None))
    rows.append(
        TraceSummaryRow(
            step="Encounter summary",
            status="PASS" if has_summary else "DEGRADED",
            detail=(
                f"Pre-decision summary stored ({len(enc.summary)} chars)"
                if has_summary
                else "No encounter summary stored yet"
            ),
        )
    )

    # 6) Care-plan generation.
    n_actions = len(snapshot.actions)
    rows.append(
        TraceSummaryRow(
            step="Care-plan generation",
            status="PASS" if snapshot.care_plan is not None and n_actions > 0 else "DEGRADED",
            detail=(
                f"Care plan {snapshot.care_plan.id} with {n_actions} actions "
                f"(status {snapshot.care_plan.status})"
                if snapshot.care_plan is not None and n_actions
                else "No generated care-plan actions yet"
            ),
            latency_ms=_ms(metrics["care_plan_s"]),
        )
    )

    # 7) Clinician feedback — decisions incl. per-status breakdown.
    decision_rows = [d for ds in snapshot.decisions.values() for d in ds]
    per_type: dict[str, int] = {}
    for d in decision_rows:
        per_type[d.decision_type] = per_type.get(d.decision_type, 0) + 1
    rows.append(
        TraceSummaryRow(
            step="Clinician feedback captured",
            status="PASS" if decision_rows else "DEGRADED",
            detail=(
                f"{len(decision_rows)} decisions ("
                + ", ".join(f"{v} {k}" for k, v in sorted(per_type.items()))
                + ")"
                if decision_rows
                else "No clinician decisions recorded yet"
            ),
        )
    )

    # 8) Tool execution + explicit permission-denial rows.
    executed = [e for e in snapshot.executions if e.status == "executed"]
    blocked = [e for e in snapshot.executions if e.status == "blocked"]
    failed = [e for e in snapshot.executions if e.status == "failed"]
    if snapshot.executions:
        rows.append(
            TraceSummaryRow(
                step="Tool execution",
                status="PASS" if executed and not failed else ("FAIL" if failed else "DEGRADED"),
                detail=(
                    f"{len(executed)} executed, {len(blocked)} blocked, "
                    f"{len(failed)} failed (mocked demo tools)"
                ),
            )
        )
    else:
        rows.append(
            TraceSummaryRow(
                step="Tool execution", status="DEGRADED", detail="No tool executions yet"
            )
        )
    for e in blocked:
        rows.append(
            TraceSummaryRow(
                step=f"Permission gate — {e.tool_name}",
                status="DENIED",
                detail=e.error or f"Invocation above permission tier {e.permission_tier}",
            )
        )

    # 9) Reports.
    cp = snapshot.care_plan
    has_clin = bool(cp and cp.clinician_summary)
    has_pat = bool(cp and cp.patient_instructions)
    rows.append(
        TraceSummaryRow(
            step="Reports",
            status="PASS" if has_clin and has_pat else "DEGRADED",
            detail=(
                f"clinician_summary {len(cp.clinician_summary)} chars, "
                f"patient_instructions {len(cp.patient_instructions)} chars "
                "(leakage-checked at generation)"
                if has_clin and has_pat
                else "Reports not generated yet (care plan not finalized)"
            ),
        )
    )

    return rows


@router.get("/trace-summary/{encounter_id}")
async def trace_summary(
    encounter_id: str,
    session: AsyncSession = Depends(get_session),
    _=Depends(require_clinician),
) -> dict:
    try:
        snapshot = await load_snapshot(session, encounter_id)
    except ValueError:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Encounter not found")

    rows = build_trace_summary(snapshot)

    # 10) Online evals (from eval_results — written by app.evals).
    from sqlalchemy import select

    from app.db import models as m

    eval_rows = (
        await session.execute(
            select(m.EvalResult).where(m.EvalResult.encounter_id == encounter_id)
        )
    ).scalars().all()
    n_passed = sum(1 for r in eval_rows if r.passed)
    if eval_rows:
        # Step health keys off the DETERMINISTIC gates; a model judge's honest
        # sub-1.0 verdict is a displayed judgment, not a pipeline failure.
        from app.evals.evaluators import gates_passed

        gates_ok, failing = gates_passed(eval_rows)
        rows.append(
            TraceSummaryRow(
                step="Online evals",
                status="PASS" if gates_ok else "DEGRADED",
                detail=(
                    f"{len(eval_rows)} evaluator results stored ({n_passed} passed"
                    + (f"; gate failures: {failing}" if failing else "")
                    + ") — prototype evaluators, not clinical validation"
                ),
            )
        )
    else:
        rows.append(
            TraceSummaryRow(
                step="Online evals", status="DEGRADED", detail="No evaluator results yet"
            )
        )

    return {"rows": [r.model_dump(mode="json") for r in rows]}
