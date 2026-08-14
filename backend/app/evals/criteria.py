"""Launch-criteria table (spec §22.1) — "what would have to be true to ship".

Pure math over persisted eval_results rows (plus the token-derived cost
estimate row, see app.evals.evaluators.estimate_encounter_cost). Targets are
the contracts-v2 stated bounds: p95 next-best-action latency < 6 s, cost per
encounter < $0.50. Rows without measurements report "not yet measured" —
honest red, never a fabricated green.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from app.evals.evaluators import NBA_P95_BOUND_SECONDS, percentile
from app.schemas.core import CriterionRow

COST_BOUND_USD = 0.50

#: (criterion display name, target, source evaluator name)
CRITERIA_SPEC: tuple[tuple[str, str, str], ...] = (
    ("Schema validity", "100%", "schema_validity"),
    ("Unsupported-fact rate", "0%", "unsupported_fact_detector"),
    ("Rejected-action leakage", "0", "rejected_action_leakage"),
    ("Modified-action fidelity", "100%", "modified_action_fidelity"),
    ("p95 next-best-action latency", f"< {NBA_P95_BOUND_SECONDS:g} s", "latency"),
    ("Cost per encounter", f"< ${COST_BOUND_USD:.2f}", "cost_per_encounter"),
)


@dataclass(frozen=True)
class _Row:
    score: float
    passed: bool


def _index(results: Iterable[Any]) -> dict[str, _Row]:
    """Last row per evaluator name wins (rows are replaced per run anyway)."""
    out: dict[str, _Row] = {}
    for r in results:
        name = getattr(r, "evaluator", None) or r["evaluator"]  # ORM/pydantic or dict
        score = getattr(r, "score", None) if not isinstance(r, dict) else r["score"]
        passed = getattr(r, "passed", None) if not isinstance(r, dict) else r["passed"]
        out[name] = _Row(score=float(score), passed=bool(passed))
    return out


def _actual(criterion: str, row: _Row) -> str:
    if criterion == "Schema validity":
        return f"{row.score * 100:g}%"
    if criterion == "Unsupported-fact rate":
        # Detector score = fraction clean (1.0 = nothing invented).
        return f"{(1.0 - row.score) * 100:g}%"
    if criterion == "Rejected-action leakage":
        return f"{int(row.score)}"
    if criterion == "Modified-action fidelity":
        return f"{row.score * 100:g}%"
    if criterion == "p95 next-best-action latency":
        return f"{row.score:.1f} s" if row.score >= 0 else "not measurable"
    if criterion == "Cost per encounter":
        return f"~${row.score:.2f} (token-derived estimate)"
    return f"{row.score:g}"


def build_launch_criteria(results: Iterable[Any]) -> list[CriterionRow]:
    """CriterionRow list for ONE encounter's eval_results rows."""
    by_name = _index(results)
    rows: list[CriterionRow] = []
    for name, target, evaluator in CRITERIA_SPEC:
        row = by_name.get(evaluator)
        if row is None:
            rows.append(
                CriterionRow(name=name, target=target, actual="not yet measured", passed=False)
            )
        else:
            rows.append(
                CriterionRow(name=name, target=target, actual=_actual(name, row), passed=row.passed)
            )
    return rows


def aggregate_launch_criteria(
    results_by_encounter: dict[str, list[Any]],
) -> list[CriterionRow]:
    """Launch criteria over the latest N encounters (regression view).

    Aggregation is worst-case-honest: percentage criteria take the MINIMUM
    encounter score, leakage counts SUM, p95 latency is the p95 over the
    per-encounter p95 values, cost is the MEAN estimate. An encounter with a
    missing measurement makes the criterion unmeasured (red), never skipped.
    """
    indexed = {enc: _index(rows) for enc, rows in results_by_encounter.items()}
    n = len(indexed)
    out: list[CriterionRow] = []
    for name, target, evaluator in CRITERIA_SPEC:
        rows = [ix.get(evaluator) for ix in indexed.values()]
        if n == 0 or any(r is None for r in rows):
            missing = sum(1 for r in rows if r is None)
            out.append(
                CriterionRow(
                    name=name,
                    target=target,
                    actual=(
                        "not yet measured"
                        if n == 0
                        else f"unmeasured in {missing}/{n} encounter(s)"
                    ),
                    passed=False,
                )
            )
            continue
        assert all(r is not None for r in rows)
        all_passed = all(r.passed for r in rows)  # type: ignore[union-attr]
        scores = [r.score for r in rows]  # type: ignore[union-attr]
        if name == "Rejected-action leakage":
            agg = _Row(score=sum(scores), passed=all_passed)
        elif name == "p95 next-best-action latency":
            measurable = [s for s in scores if s >= 0]
            p95 = percentile(measurable, 0.95) if measurable else None
            agg = _Row(score=p95 if p95 is not None else -1.0, passed=all_passed)
        elif name == "Cost per encounter":
            agg = _Row(score=sum(scores) / n, passed=all_passed)
        elif name == "Unsupported-fact rate":
            agg = _Row(score=min(scores), passed=all_passed)
        else:  # percentage criteria: worst encounter
            agg = _Row(score=min(scores), passed=all_passed)
        actual = _actual(name, agg) + (f" (over {n} encounters)" if n > 1 else "")
        out.append(CriterionRow(name=name, target=target, actual=actual, passed=agg.passed))
    return out


async def latest_criteria(session: Any, n: int = 5) -> list[CriterionRow]:
    """Aggregate launch criteria over the latest N encounters that have rows."""
    from sqlalchemy import select

    from app.db import models as m

    enc_ids = (
        (
            await session.execute(
                select(m.EvalResult.encounter_id)
                .group_by(m.EvalResult.encounter_id)
                .order_by(m.EvalResult.encounter_id.desc())
                .limit(n)
            )
        )
        .scalars()
        .all()
    )
    results_by_encounter: dict[str, list[Any]] = {}
    for enc_id in enc_ids:
        rows = (
            (
                await session.execute(
                    select(m.EvalResult).where(m.EvalResult.encounter_id == enc_id)
                )
            )
            .scalars()
            .all()
        )
        results_by_encounter[enc_id] = rows
    return aggregate_launch_criteria(results_by_encounter)
