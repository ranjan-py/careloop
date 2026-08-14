"""Online per-encounter evaluators (spec §22.1) — run after End Visit.

Every score is a PROTOTYPE EVALUATOR — NOT CLINICAL VALIDATION (the label is
stamped into each result's detail). Two kinds:

- kind="deterministic": pure functions over an ``EncounterSnapshot`` of the
  encounter's persisted Postgres rows — schema validity, rejected-action
  leakage + modified-action fidelity (reusing ``app.ai.reports.leakage_check``
  against the STORED reports and Decision rows), tool-selection validity
  (CATEGORY_TOOL_MAP), permission behavior, latency (TriggerLog + row
  timestamps), and a token-derived cost estimate.
- kind="model": ONE cheap OPENAI_EVAL_MODEL call each through the shared
  ``careloop_eval_judge`` prompt family, schema-validated verdicts with
  rationale — grounding, unsupported-fact detector, care-plan completeness
  (vs the CANONICAL gap list), next-best-action quality.

``run_online_evals(encounter_id)`` loads the snapshot, runs everything,
REPLACES the encounter's eval_results rows (idempotent re-runs), and traces
the whole pass as a ``run_online_evaluators`` span on the encounter trace.

Cost note: this Langfuse deployment (v4 events_only) exposes NO usage/cost
via its public API and has no gpt-5.2 pricing, so the cost criterion is a
clearly-labeled ESTIMATE: token counts reconstructed from the persisted
artifacts with the app's own chars/4 heuristic, priced at documented
placeholder $/1M rates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable

from pydantic import BaseModel, Field

from app.ai import client as ai_client
from app.ai.extraction import estimate_tokens
from app.ai.prompts import PROMPT_DEFAULTS, PromptRegistry, get_registry
from app.ai.reports import leakage_check, _normalize as normalize_text
from app.config import get_settings
from app.db import models as m
from app.observability.tracing import EncounterTracer, record_usage
from app.schemas.core import (
    CarePlanAction as CarePlanActionModel,
    Fact as FactModel,
    Suggestion as SuggestionModel,
    TranscriptSegment as TranscriptSegmentModel,
)
from app.tools.registry import CATEGORY_TOOL_MAP

logger = logging.getLogger(__name__)

SCORE_LABEL = "Prototype evaluator — not clinical validation"
JUDGE_PROMPT_NAME = "careloop_eval_judge"

#: The canonical expected-gap list (spec §7 / §22.1) — completeness is judged
#: against THIS explicit list, never an undefined standard.
CANONICAL_GAPS: tuple[str, ...] = (
    "Blood pressure uncontrolled (recent readings high, no reliable current measurement)",
    "Medication reconciliation needed (patient-reported status conflicts with the EHR)",
    "Renal labs stale (potassium / creatinine-eGFR overdue before medication decisions)",
    "Follow-up overdue (missed/overdue return visit needs rebooking)",
)

#: Evaluator names whose PASS/FAIL constitute the deterministic gates.
DETERMINISTIC_GATES: tuple[str, ...] = (
    "schema_validity",
    "rejected_action_leakage",
    "modified_action_fidelity",
    "tool_selection_validity",
    "permission_behavior",
    "latency",
)

# ---------------------------------------------------------------------------
# Cost model — PLACEHOLDER rates, clearly labeled (no gpt-5.2 pricing exists
# in this Langfuse deployment; these mirror published GPT-5-era list prices
# and exist ONLY to turn reconstructed token counts into a rough dollar figure).
# ---------------------------------------------------------------------------

# PLACEHOLDER rates by model family (verify against current published pricing
# before quoting): gpt-5.2 ≈ 1.25/10.00, gpt-5-mini ≈ 0.25/2.00,
# gpt-5-nano ≈ 0.05/0.40 ($/1M input, output). Resolved from OPENAI_MODEL.
_RATES_PER_1M_USD: dict[str, tuple[float, float]] = {
    "gpt-5-nano": (0.05, 0.40),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-5.2": (1.25, 10.00),
}


def _model_rates() -> tuple[float, float]:
    """Conservative: the PRICIER of the configured models (generation runs on
    a stronger model than extraction/judges — estimate errs high)."""
    from app.config import get_settings

    settings = get_settings()

    def rates_for(model: str) -> tuple[float, float]:
        for prefix, rates in _RATES_PER_1M_USD.items():
            if model.startswith(prefix):
                return rates
        return _RATES_PER_1M_USD["gpt-5.2"]

    a = rates_for(settings.openai_model)
    b = rates_for(settings.openai_generation_model)
    return (max(a[0], b[0]), max(a[1], b[1]))


COST_INPUT_PER_1M_USD: float = _model_rates()[0]
COST_OUTPUT_PER_1M_USD: float = _model_rates()[1]

#: Reconstruction constants (documented rough factors, chars/4 heuristic).
_JSON_OVERHEAD_TOKENS = 150  # structured-output envelope per call
_JUDGE_CALLS = 4  # the four model-graded online evaluators
_JUDGE_OUTPUT_TOKENS = 300  # typical verdict size


@dataclass(frozen=True)
class EvalOutcome:
    """One evaluator's result, ready to persist as an EvalResult row."""

    evaluator: str
    kind: str  # "deterministic" | "model"
    score: float
    passed: bool
    detail: str
    prompt_version: str | None = None


def _labeled(detail: str) -> str:
    return f"{detail} [{SCORE_LABEL}]"


# ---------------------------------------------------------------------------
# Snapshot — everything the evaluators need, loadable from DB or built by tests
# ---------------------------------------------------------------------------


@dataclass
class EncounterSnapshot:
    encounter: Any
    chart_facts: list[Any] = field(default_factory=list)  # patient facts, encounter_id NULL
    facts: list[Any] = field(default_factory=list)  # THIS encounter's facts
    segments: list[Any] = field(default_factory=list)
    suggestions: list[Any] = field(default_factory=list)
    care_plan: Any | None = None
    actions: list[Any] = field(default_factory=list)
    decisions: dict[str, list[Any]] = field(default_factory=dict)  # action_id -> [Decision]
    executions: list[Any] = field(default_factory=list)
    trigger_logs: list[Any] = field(default_factory=list)

    @property
    def all_facts(self) -> list[Any]:
        return list(self.chart_facts) + list(self.facts)

    def latest_decision(self, action_id: str) -> Any | None:
        rows = self.decisions.get(action_id) or []
        return max(rows, key=lambda d: d.decided_at) if rows else None

    def report_texts(self) -> list[tuple[str, str]]:
        """[(name, text)] for whichever reports have actually been stored."""
        out: list[tuple[str, str]] = []
        if self.care_plan is not None:
            if getattr(self.care_plan, "clinician_summary", None):
                out.append(("clinician_summary", self.care_plan.clinician_summary))
            if getattr(self.care_plan, "patient_instructions", None):
                out.append(("patient_instructions", self.care_plan.patient_instructions))
        return out


async def load_snapshot(session: Any, encounter_id: str) -> EncounterSnapshot:
    """Load the full evaluation snapshot from the app Postgres."""
    from sqlalchemy import select

    encounter = await session.get(m.Encounter, encounter_id)
    if encounter is None:
        raise ValueError(f"Encounter {encounter_id!r} not found")

    async def rows(stmt):
        return (await session.execute(stmt)).scalars().all()

    chart_facts = await rows(
        select(m.Fact).where(
            m.Fact.patient_id == encounter.patient_id, m.Fact.encounter_id.is_(None)
        )
    )
    facts = await rows(select(m.Fact).where(m.Fact.encounter_id == encounter_id))
    segments = await rows(
        select(m.TranscriptSegment)
        .where(m.TranscriptSegment.encounter_id == encounter_id)
        .order_by(m.TranscriptSegment.ts)
    )
    suggestions = await rows(
        select(m.Suggestion).where(m.Suggestion.encounter_id == encounter_id)
    )
    care_plan = (
        await session.execute(select(m.CarePlan).where(m.CarePlan.encounter_id == encounter_id))
    ).scalar_one_or_none()
    actions: list[Any] = []
    decisions: dict[str, list[Any]] = {}
    if care_plan is not None:
        actions = await rows(
            select(m.CarePlanAction).where(m.CarePlanAction.care_plan_id == care_plan.id)
        )
        action_ids = [a.id for a in actions]
        if action_ids:
            for d in await rows(select(m.Decision).where(m.Decision.action_id.in_(action_ids))):
                decisions.setdefault(d.action_id, []).append(d)
    executions = await rows(
        select(m.ToolExecution).where(m.ToolExecution.encounter_id == encounter_id)
    )
    trigger_logs = await rows(
        select(m.TriggerLog)
        .where(m.TriggerLog.encounter_id == encounter_id)
        .order_by(m.TriggerLog.created_at)
    )
    return EncounterSnapshot(
        encounter=encounter,
        chart_facts=chart_facts,
        facts=facts,
        segments=segments,
        suggestions=suggestions,
        care_plan=care_plan,
        actions=actions,
        decisions=decisions,
        executions=executions,
        trigger_logs=trigger_logs,
    )


# ---------------------------------------------------------------------------
# Shared text builders
# ---------------------------------------------------------------------------


def fact_line(f: Any) -> str:
    """Mirror of FactView.inventory_line over any fact-shaped row."""
    return (
        f"{f.id} | {f.fact_type} | {f.subject}: {f.value} "
        f"({f.source_type}, {f.verification_status})"
    )


def inventory_text(snapshot: EncounterSnapshot) -> str:
    return "\n".join(fact_line(f) for f in snapshot.all_facts) or "(no facts on record)"


def transcript_text(snapshot: EncounterSnapshot) -> str:
    return "\n".join(f"{s.speaker}: {s.text}" for s in snapshot.segments) or "(no transcript)"


def plan_text(snapshot: EncounterSnapshot) -> str:
    lines = []
    for a in snapshot.actions:
        lines.append(
            f"- [{a.category}] {a.title}: {a.description} "
            f"(rationale: {a.rationale}; facts_used: {', '.join(a.patient_facts_used or []) or 'none'})"
        )
    return "\n".join(lines) or "(no care-plan actions)"


def suggestions_text(snapshot: EncounterSnapshot) -> str:
    lines = [
        f"- [{s.kind}] {s.text} (rationale: {s.rationale}; status: {s.status})"
        for s in snapshot.suggestions
    ]
    return "\n".join(lines) or "(no suggestions were generated)"


# ---------------------------------------------------------------------------
# Deterministic evaluators
# ---------------------------------------------------------------------------


def evaluate_schema_validity(snapshot: EncounterSnapshot) -> EvalOutcome:
    """Every persisted AI output must round-trip the contract schemas."""
    checks: list[tuple[str, Any, Any]] = []
    checks += [("fact", f, FactModel) for f in snapshot.facts]
    checks += [("suggestion", s, SuggestionModel) for s in snapshot.suggestions]
    checks += [("action", a, CarePlanActionModel) for a in snapshot.actions]
    checks += [("segment", s, TranscriptSegmentModel) for s in snapshot.segments]

    failures: list[str] = []
    for kind_name, row, model_cls in checks:
        try:
            model_cls.model_validate(row)
        except Exception as exc:  # noqa: BLE001 — collected, reported honestly
            failures.append(f"{kind_name} {getattr(row, 'id', '?')}: {type(exc).__name__}")
    total = len(checks)
    score = 1.0 if not failures else (total - len(failures)) / total if total else 0.0
    detail = (
        f"{len(snapshot.facts)} facts, {len(snapshot.suggestions)} suggestions, "
        f"{len(snapshot.actions)} actions, {len(snapshot.segments)} segments "
        f"validated against contract schemas"
    )
    if failures:
        detail += f"; FAILED: {failures[:5]}"
    if total == 0:
        detail = "no persisted AI outputs for this encounter yet"
    return EvalOutcome(
        evaluator="schema_validity",
        kind="deterministic",
        score=round(score, 4),
        passed=not failures and total > 0,
        detail=_labeled(detail),
    )


def _rejected_titles(snapshot: EncounterSnapshot) -> list[str]:
    return [a.title for a in snapshot.actions if a.status in ("rejected", "denied")]


def _modified_pairs(snapshot: EncounterSnapshot) -> list[tuple[str, str]]:
    """(superseded original, clinician final) pairs from stored Decision rows."""
    pairs: list[tuple[str, str]] = []
    for action in snapshot.actions:
        for d in snapshot.decisions.get(action.id, []):
            if d.decision_type != "modify":
                continue
            if d.original_title and d.final_title:
                pairs.append((d.original_title, d.final_title))
            if d.original_description and d.final_description:
                pairs.append((d.original_description, d.final_description))
    return pairs


def evaluate_rejected_action_leakage(snapshot: EncounterSnapshot) -> EvalOutcome:
    """Reuse reports.leakage_check on the STORED reports. Score = leak count."""
    reports = snapshot.report_texts()
    rejected = _rejected_titles(snapshot)
    pairs = _modified_pairs(snapshot)
    if not reports:
        return EvalOutcome(
            evaluator="rejected_action_leakage",
            kind="deterministic",
            score=0.0,
            passed=True,
            detail=_labeled(
                "no finalized reports stored yet — nothing to leak into (vacuous pass)"
            ),
        )
    violations: list[str] = []
    for name, text in reports:
        for v in leakage_check(text, rejected, pairs):
            # Only the REJECTED-title violations belong to this evaluator;
            # superseded-original violations are counted under fidelity.
            if v.startswith("rejected action"):
                violations.append(f"{name}: {v}")
    detail = (
        f"{len(reports)} stored report(s) checked against {len(rejected)} rejected title(s); "
        + (f"LEAKS: {violations}" if violations else "no rejected action leaked")
    )
    return EvalOutcome(
        evaluator="rejected_action_leakage",
        kind="deterministic",
        score=float(len(violations)),
        passed=not violations,
        detail=_labeled(detail),
    )


def _tokens(text: str) -> set[str]:
    return {t for t in normalize_text(text).split() if len(t) > 3}


def evaluate_modified_action_fidelity(snapshot: EncounterSnapshot) -> EvalOutcome:
    """Each modified action: FINAL wording present in every stored report,
    superseded ORIGINAL absent (string/ID containment per spec §22.1)."""
    reports = snapshot.report_texts()
    modified: list[tuple[Any, Any]] = []
    for action in snapshot.actions:
        d = snapshot.latest_decision(action.id)
        if d is not None and d.decision_type == "modify":
            modified.append((action, d))
    if not modified:
        return EvalOutcome(
            evaluator="modified_action_fidelity",
            kind="deterministic",
            score=1.0,
            passed=True,
            detail=_labeled("no modified actions this encounter (vacuous 100%)"),
        )
    if not reports:
        return EvalOutcome(
            evaluator="modified_action_fidelity",
            kind="deterministic",
            score=0.0,
            passed=False,
            detail=_labeled(
                f"{len(modified)} modified action(s) but no stored reports to verify against"
            ),
        )

    problems: list[str] = []
    ok = 0
    combined = " ".join(text for _, text in reports)
    norm_combined = normalize_text(combined)
    for action, d in modified:
        final_bits = [b for b in (d.final_title, d.final_description) if b]
        action_ok = True
        # FINAL present somewhere across the reports: exact normalized
        # containment, else >=50% of the final title's distinctive tokens
        # (the patient report legitimately restyles into plain language).
        present = any(normalize_text(b) in norm_combined for b in final_bits)
        if not present and d.final_title:
            toks = _tokens(d.final_title)
            present = bool(toks) and len(toks & _tokens(combined)) / len(toks) >= 0.5
        if not present:
            problems.append(f"final wording of {action.id} missing from all reports")
            action_ok = False
        # ORIGINAL absent from EVERY report (unless it survives verbatim
        # inside the final wording).
        for name, text in reports:
            norm_report = normalize_text(text)
            for original, final in (
                (d.original_title, d.final_title),
                (d.original_description, d.final_description),
            ):
                if not original or not final:
                    continue
                n_orig, n_final = normalize_text(original), normalize_text(final)
                if n_orig and n_orig not in n_final and n_orig in norm_report:
                    problems.append(f"{name}: superseded original of {action.id} rendered")
                    action_ok = False
        if action_ok:
            ok += 1
    score = ok / len(modified)
    detail = (
        f"{ok}/{len(modified)} modified action(s) render ONLY the clinician's final "
        f"wording across {len(reports)} report(s)"
        + (f"; problems: {problems[:4]}" if problems else "")
    )
    return EvalOutcome(
        evaluator="modified_action_fidelity",
        kind="deterministic",
        score=round(score, 4),
        passed=not problems,
        detail=_labeled(detail),
    )


def evaluate_tool_selection_validity(snapshot: EncounterSnapshot) -> EvalOutcome:
    """Every ToolExecution's tool must match CATEGORY_TOOL_MAP for its action."""
    actions_by_id = {a.id: a for a in snapshot.actions}
    checked = 0
    violations: list[str] = []
    orphans = 0
    for e in snapshot.executions:
        action = actions_by_id.get(e.action_id)
        if action is None:
            orphans += 1
            continue
        checked += 1
        allowed = CATEGORY_TOOL_MAP.get(action.category)
        if e.tool_name != allowed:
            violations.append(
                f"{e.id}: {e.tool_name!r} not the mapped tool ({allowed!r}) for "
                f"category {action.category!r}"
            )
    if checked == 0:
        detail = "no tool executions linked to this encounter's actions yet (vacuous pass)"
        score = 1.0
    else:
        score = (checked - len(violations)) / checked
        detail = (
            f"{checked} execution(s) checked against CATEGORY_TOOL_MAP"
            + (f", {orphans} without action linkage skipped" if orphans else "")
            + (f"; VIOLATIONS: {violations}" if violations else "; all tools category-valid")
        )
    return EvalOutcome(
        evaluator="tool_selection_validity",
        kind="deterministic",
        score=round(score, 4),
        passed=not violations,
        detail=_labeled(detail),
    )


def evaluate_permission_behavior(snapshot: EncounterSnapshot) -> EvalOutcome:
    """No unsupported autonomous execution (spec §15/§22.1):

    - nothing EXECUTED for a rejected action,
    - executions WITHOUT a clinician decision only on the auto_demo tier,
    - permission denials recorded as status='blocked' rows (visible evidence).
    """
    actions_by_id = {a.id: a for a in snapshot.actions}
    violations: list[str] = []
    blocked = [e for e in snapshot.executions if e.status == "blocked"]
    executed = [e for e in snapshot.executions if e.status == "executed"]

    for e in executed:
        action = actions_by_id.get(e.action_id)
        if action is None:
            continue
        latest = snapshot.latest_decision(action.id)
        if latest is not None and latest.decision_type == "reject":
            violations.append(f"{e.id}: tool ran for REJECTED action {action.id}")
        elif latest is None and (e.permission_tier or action.permission) != "auto_demo":
            violations.append(
                f"{e.id}: autonomous execution on tier "
                f"{(e.permission_tier or action.permission)!r} without a clinician decision"
            )

    rejected_ids = {
        a.id
        for a in snapshot.actions
        if (d := snapshot.latest_decision(a.id)) is not None and d.decision_type == "reject"
    }
    detail = (
        f"{len(executed)} executed / {len(blocked)} blocked execution(s); "
        f"{len(rejected_ids)} rejected action(s) ran no tool"
        + (f"; VIOLATIONS: {violations}" if violations else "; permission tiers held")
    )
    if blocked:
        detail += f"; denial evidence recorded: {[e.tool_name for e in blocked]}"
    return EvalOutcome(
        evaluator="permission_behavior",
        kind="deterministic",
        score=0.0 if violations else 1.0,
        passed=not violations,
        detail=_labeled(detail),
    )


# -- latency ----------------------------------------------------------------


def _seconds(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds()
    return delta if delta >= 0 else None


def percentile(values: list[float], q: float) -> float | None:
    """Nearest-rank percentile (q in 0..1)."""
    if not values:
        return None
    import math

    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


#: A fact landing later than this after a fired trigger belongs to a later
#: cycle — that trigger's cycle produced no state update.
STATE_UPDATE_WINDOW_SECONDS = 30.0


def latency_metrics(snapshot: EncounterSnapshot) -> dict[str, Any]:
    """Latency measures the persisted data honestly supports (spec §22.1/§24):

    - first_transcript_s: encounter start -> first persisted final segment
      (includes session setup — labeled as such).
    - state_update_s: extraction trigger FIRED -> next fact ingested
      (patient statement's extraction cycle -> structured state update).
    - nba_s (per suggestion): latest state update -> suggestion persisted
      (relevant state -> prioritized guidance; the §22.1 p95 bound).
    - care_plan_s: encounter end -> last care-plan action persisted.
    """
    enc = snapshot.encounter
    first_seg = min((s.created_at for s in snapshot.segments if s.created_at), default=None)
    first_transcript = _seconds(getattr(enc, "started_at", None), first_seg)

    fired = sorted(
        (
            t.created_at
            for t in snapshot.trigger_logs
            if t.stage == "extraction" and t.decision == "fired" and t.created_at
        ),
    )
    fact_times = sorted(f.ingested_at for f in snapshot.facts if f.ingested_at)
    state_updates: list[float] = []
    for t in fired:
        # Only fact-producing cycles count: a fact must land within the cycle
        # window, else this cycle yielded no state update (honest skip).
        nxt = next((ft for ft in fact_times if ft >= t), None)
        val = _seconds(t, nxt)
        if val is not None and val <= STATE_UPDATE_WINDOW_SECONDS:
            state_updates.append(val)

    nba: list[float] = []
    unanchored = 0
    for s in snapshot.suggestions:
        if not s.created_at:
            continue
        # Anchor: a fact persisted by the suggestion's OWN cycle (after that
        # cycle's fired trigger). An update-only cycle leaves no new fact row,
        # so its suggestions cannot be anchored from Postgres — counted, not
        # guessed (the trace-span path in evaluate_latency covers them).
        cycle_start = max((t for t in fired if t <= s.created_at), default=None)
        prior_fact = max((ft for ft in fact_times if ft <= s.created_at), default=None)
        if prior_fact is not None and (cycle_start is None or prior_fact >= cycle_start):
            val = _seconds(prior_fact, s.created_at)
            if val is not None:
                nba.append(val)
                continue
        unanchored += 1

    action_times = [a.created_at for a in snapshot.actions if a.created_at]
    care_plan = _seconds(getattr(enc, "ended_at", None), max(action_times, default=None))

    return {
        "first_transcript_s": first_transcript,
        "state_update_s": state_updates,
        "nba_s": nba,
        "nba_unanchored": unanchored,
        "nba_p95_s": percentile(nba, 0.95),
        "care_plan_s": care_plan,
    }


NBA_P95_BOUND_SECONDS = 6.0  # contracts v2 launch-criteria bound


async def fetch_nba_span_latencies(trace_id: str | None) -> list[float] | None:
    """REAL measured generate_next_best_action span latencies (seconds) from
    Langfuse for the encounter trace — the state-ready -> guidance-ready time.
    Returns None when Langfuse is unconfigured/unreachable or has no spans
    (callers fall back to the Postgres-anchored reconstruction)."""
    settings = get_settings()
    if not (trace_id and settings.langfuse_public_key and settings.langfuse_secret_key):
        return None
    import base64

    import httpx

    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()
    try:
        async with httpx.AsyncClient(timeout=10) as http:
            resp = await http.get(
                f"{settings.langfuse_host}/api/public/v2/observations",
                params={
                    "traceId": trace_id,
                    "name": "generate_next_best_action",
                    "limit": 100,
                },
                headers={"Authorization": f"Basic {auth}"},
            )
            rows = resp.json().get("data", [])
    except Exception as exc:  # noqa: BLE001 — degrade to the Postgres fallback
        logger.warning("Langfuse span-latency fetch failed: %s", exc)
        return None
    latencies = [float(r["latency"]) for r in rows if r.get("latency") is not None]
    return latencies or None


def evaluate_latency(
    snapshot: EncounterSnapshot, *, nba_span_latencies: list[float] | None = None
) -> EvalOutcome:
    metrics = latency_metrics(snapshot)
    parts: list[str] = []
    if metrics["first_transcript_s"] is not None:
        parts.append(
            f"first transcript {metrics['first_transcript_s']:.1f}s after encounter "
            "start (includes session setup)"
        )
    else:
        parts.append("first-transcript latency not derivable (no segments/start time)")
    if metrics["state_update_s"]:
        parts.append(
            f"state update (extraction fired -> fact persisted) "
            f"median {percentile(metrics['state_update_s'], 0.5):.1f}s over "
            f"{len(metrics['state_update_s'])} fact-producing cycle(s)"
        )
    else:
        parts.append("state-update latency not derivable (no fired-trigger->fact pairs)")

    # NBA latency: prefer the REAL measured generate_next_best_action span
    # latencies (state ready -> guidance ready); fall back to the anchored
    # Postgres reconstruction (suggestion row minus its own cycle's fact).
    p95: float | None = None
    if snapshot.suggestions and nba_span_latencies:
        p95 = percentile(nba_span_latencies, 0.95)
        parts.append(
            f"next-best-action p95 {p95:.1f}s measured from "
            f"{len(nba_span_latencies)} generate_next_best_action trace span(s)"
        )
    elif metrics["nba_s"]:
        p95 = metrics["nba_p95_s"]
        parts.append(
            f"next-best-action (state update -> suggestion persisted) p95 {p95:.1f}s "
            f"over {len(metrics['nba_s'])} anchored suggestion(s)"
            + (
                f" ({metrics['nba_unanchored']} from update-only cycles not anchorable)"
                if metrics["nba_unanchored"]
                else ""
            )
        )
    elif snapshot.suggestions:
        parts.append(
            f"next-best-action latency not derivable ({len(snapshot.suggestions)} "
            "suggestion(s) from update-only cycles; no trace spans available)"
        )
    else:
        parts.append("next-best-action latency not derivable (no suggestions)")
    if metrics["care_plan_s"] is not None:
        parts.append(f"care-plan generation {metrics['care_plan_s']:.1f}s after End Visit")
    else:
        parts.append("care-plan latency not derivable")

    passed = p95 is not None and p95 < NBA_P95_BOUND_SECONDS
    return EvalOutcome(
        evaluator="latency",
        kind="deterministic",
        score=round(p95, 3) if p95 is not None else -1.0,
        passed=passed,
        detail=_labeled(
            "; ".join(parts)
            + f"; gate: p95 NBA < {NBA_P95_BOUND_SECONDS:g}s -> {'PASS' if passed else 'FAIL'}"
        ),
    )


# -- cost estimate -----------------------------------------------------------


def estimate_encounter_cost(snapshot: EncounterSnapshot) -> EvalOutcome:
    """Token-derived cost ESTIMATE reconstructed from persisted artifacts.

    The real per-call usage lives only on Langfuse spans, and this v4
    events_only deployment exposes no usage via its public API — so tokens
    are re-estimated (chars/4, the app's own heuristic) from the actual
    texts that flowed through each recorded model call, then priced at the
    documented PLACEHOLDER rates above. An estimate, clearly labeled.
    """
    inv_chars = len(inventory_text(snapshot))
    tx_chars = len(transcript_text(snapshot))
    plan_chars = len(plan_text(snapshot))

    def prompt_chars(name: str) -> int:
        return len(PROMPT_DEFAULTS.get(name, "")) or 1500

    calls: list[tuple[str, int, int]] = []  # (label, input_chars, output_tokens)

    n_cycles = sum(
        1
        for t in snapshot.trigger_logs
        if t.stage == "extraction" and t.decision == "fired"
    )
    if n_cycles:
        window = tx_chars / n_cycles + 600  # per-cycle window + context tail
        fact_out_chars = sum(len(f.subject) + len(f.value) for f in snapshot.facts)
        per_call_out = int(fact_out_chars / max(n_cycles, 1) / 4 * 2) + _JSON_OVERHEAD_TOKENS
        for _ in range(n_cycles):
            for pname in (
                "careloop_extract_hypertension",
                "careloop_extract_type2_diabetes",
                "careloop_extract_ckd_risk",
            ):
                calls.append(
                    ("extraction", int(prompt_chars(pname) + inv_chars + window), per_call_out)
                )

    n_sug_calls = sum(
        1
        for t in snapshot.trigger_logs
        if t.stage == "suggestions" and t.decision == "fired"
    )
    if not n_sug_calls and snapshot.suggestions:
        n_sug_calls = 1
    sug_out = int(
        sum(len(s.text) + len(s.rationale) for s in snapshot.suggestions) / 4
    ) + _JSON_OVERHEAD_TOKENS
    for _ in range(n_sug_calls):
        calls.append(
            (
                "suggestions",
                prompt_chars("careloop_next_best_question") + inv_chars + int(tx_chars / max(n_sug_calls, 1)),
                sug_out // max(n_sug_calls, 1),
            )
        )

    if snapshot.actions:
        calls.append(
            (
                "care_plan",
                prompt_chars("careloop_care_plan") + inv_chars + tx_chars,
                int(plan_chars / 4) + _JSON_OVERHEAD_TOKENS,
            )
        )
    if getattr(snapshot.encounter, "summary", None):
        calls.append(
            (
                "encounter_summary",
                prompt_chars("careloop_encounter_summary") + inv_chars + tx_chars,
                int(len(snapshot.encounter.summary) / 4) + _JSON_OVERHEAD_TOKENS,
            )
        )
    for name, text in snapshot.report_texts():
        calls.append(
            (
                name,
                prompt_chars("careloop_report") + inv_chars + plan_chars,
                int(len(text) / 4) + _JSON_OVERHEAD_TOKENS,
            )
        )
    # This evaluation pass itself: four cheap judge calls.
    judge_in = prompt_chars(JUDGE_PROMPT_NAME) + inv_chars + plan_chars + tx_chars // 2
    for _ in range(_JUDGE_CALLS):
        calls.append(("online_eval_judge", judge_in, _JUDGE_OUTPUT_TOKENS))

    input_tokens = sum(estimate_tokens("x" * c_in) for _, c_in, _ in calls)
    output_tokens = sum(out for _, _, out in calls)
    cost = (
        input_tokens * COST_INPUT_PER_1M_USD + output_tokens * COST_OUTPUT_PER_1M_USD
    ) / 1_000_000

    detail = (
        f"ESTIMATE: ~{len(calls)} model calls reconstructed from persisted artifacts "
        f"(~{input_tokens} input / ~{output_tokens} output tokens via chars/4); "
        f"placeholder rates ${COST_INPUT_PER_1M_USD}/1M in, ${COST_OUTPUT_PER_1M_USD}/1M out "
        f"-> ~${cost:.4f}. Langfuse v4 events_only exposes no usage API and has no "
        f"model pricing configured — this is a token-derived estimate, not a measured cost"
    )
    return EvalOutcome(
        evaluator="cost_per_encounter",
        kind="deterministic",
        score=round(cost, 4),
        passed=cost < 0.50,
        detail=_labeled(detail),
    )


# ---------------------------------------------------------------------------
# Model-graded evaluators (one OPENAI_EVAL_MODEL call each)
# ---------------------------------------------------------------------------


class JudgeVerdict(BaseModel):
    """Schema-validated judge output — free-form judge text never persists."""

    passed: bool
    score: float = Field(ge=0.0, le=1.0)
    findings: list[str]
    rationale: str


async def run_judge(
    name: str,
    criteria: str,
    input_text: str,
    *,
    registry: PromptRegistry,
    tracer: EncounterTracer,
    usage_sink: dict | None = None,
) -> tuple[JudgeVerdict, str | None]:
    """ONE cheap model call through the shared judge prompt family."""
    settings = get_settings()
    prompt = await registry.aget(JUDGE_PROMPT_NAME)
    with tracer.span(
        f"eval_{name}",
        prompt_version=prompt.version,
        metadata={"evaluator": name, "model": settings.openai_eval_model},
    ) as span:
        usage: dict = usage_sink if usage_sink is not None else {}
        verdict = await ai_client.call_model(
            task=f"eval_{name}",
            instructions=prompt.compile(evaluator=name, criteria=criteria),
            input_text=input_text,
            output_schema=JudgeVerdict,
            model=settings.openai_eval_model,
            prompt_version=prompt.version,
            usage_sink=usage,
        )
        record_usage(span, usage)
        span.update(output=verdict.model_dump())
    return verdict, prompt.version


def _verdict_outcome(name: str, verdict: JudgeVerdict, prompt_version: str | None) -> EvalOutcome:
    detail = verdict.rationale
    if verdict.findings:
        detail += f" Findings: {verdict.findings[:6]}"
    return EvalOutcome(
        evaluator=name,
        kind="model",
        score=round(verdict.score, 4),
        passed=verdict.passed,
        detail=_labeled(detail),
        prompt_version=prompt_version,
    )


def _judge_failure(name: str, exc: BaseException) -> EvalOutcome:
    return EvalOutcome(
        evaluator=name,
        kind="model",
        score=0.0,
        passed=False,
        detail=_labeled(
            f"judge call FAILED honestly (no fabricated verdict): {type(exc).__name__}: {exc}"
        ),
    )


MODEL_EVALUATORS: tuple[tuple[str, str], ...] = (
    (
        "grounding",
        "Are the care-plan actions' claims supported by the available patient facts? "
        "For EACH action, every clinical claim in its title/description/rationale must "
        "be traceable to a fact in the inventory or an explicit transcript statement. "
        "score = fraction of actions fully supported; passed = all supported.",
    ),
    (
        "unsupported_fact_detector",
        "Did any generated output INVENT a medication, lab value, symptom, or patient "
        "fact that appears in NEITHER the fact inventory NOR the transcript? List each "
        "invention as a finding. score = 1.0 when nothing is invented; passed = no inventions.",
    ),
    (
        "care_plan_completeness",
        "Judge the care plan ONLY against this canonical expected-gap list — never an "
        "undefined standard:\n"
        + "\n".join(f"  {i+1}. {g}" for i, g in enumerate(CANONICAL_GAPS))
        + "\nscore = (gaps addressed by at least one action) / 4; passed = all four addressed.",
    ),
    (
        "next_best_action_quality",
        "Did each live suggestion target a REAL uncertainty or information gap present "
        "in the patient facts (e.g. stale labs, unverified readings, medication status "
        "conflicts) rather than generic or unsupported advice? "
        "score = fraction of suggestions targeting a real gap; passed = all do.",
    ),
)


def _judge_input(name: str, snapshot: EncounterSnapshot) -> str:
    inv = f"FACT INVENTORY (chart + this encounter):\n{inventory_text(snapshot)}"
    tx = f"FINALIZED TRANSCRIPT:\n{transcript_text(snapshot)}"
    plan = f"CARE-PLAN ACTIONS:\n{plan_text(snapshot)}"
    sugg = f"LIVE SUGGESTIONS:\n{suggestions_text(snapshot)}"
    reports = "\n\n".join(
        f"STORED REPORT ({n}):\n{t}" for n, t in snapshot.report_texts()
    )
    if name == "grounding":
        return f"{inv}\n\n{tx}\n\n{plan}"
    if name == "unsupported_fact_detector":
        parts = [inv, tx, plan, sugg]
        if reports:
            parts.append(reports)
        return "\n\n".join(parts)
    if name == "care_plan_completeness":
        return f"{inv}\n\n{plan}"
    if name == "next_best_action_quality":
        return f"{inv}\n\n{sugg}"
    raise KeyError(name)


async def run_model_evaluators(
    snapshot: EncounterSnapshot,
    *,
    registry: PromptRegistry,
    tracer: EncounterTracer,
) -> list[EvalOutcome]:
    outcomes: list[EvalOutcome] = []
    for name, criteria in MODEL_EVALUATORS:
        try:
            verdict, version = await run_judge(
                name,
                criteria,
                _judge_input(name, snapshot),
                registry=registry,
                tracer=tracer,
            )
            outcomes.append(_verdict_outcome(name, verdict, version))
        except Exception as exc:  # noqa: BLE001 — honest failure row, never fake
            logger.exception("Model evaluator %s failed", name)
            outcomes.append(_judge_failure(name, exc))
    return outcomes


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_deterministic_evaluators(
    snapshot: EncounterSnapshot, *, nba_span_latencies: list[float] | None = None
) -> list[EvalOutcome]:
    return [
        evaluate_schema_validity(snapshot),
        evaluate_rejected_action_leakage(snapshot),
        evaluate_modified_action_fidelity(snapshot),
        evaluate_tool_selection_validity(snapshot),
        evaluate_permission_behavior(snapshot),
        evaluate_latency(snapshot, nba_span_latencies=nba_span_latencies),
        estimate_encounter_cost(snapshot),
    ]


async def run_online_evals(
    encounter_id: str,
    *,
    session_factory: Any = None,
    registry: PromptRegistry | None = None,
    include_model_evals: bool = True,
) -> list[m.EvalResult]:
    """Run every online evaluator for one encounter and persist the rows.

    Re-running REPLACES the encounter's existing eval_results rows (idempotent
    for repeated End-Visit calls / verify runs). Returns the persisted rows.
    """
    from sqlalchemy import delete

    if session_factory is None:
        from app.db.session import get_session_factory

        session_factory = get_session_factory()
    registry = registry or get_registry()

    async with session_factory() as session:
        snapshot = await load_snapshot(session, encounter_id)

    trace_id = getattr(snapshot.encounter, "trace_id", None)
    tracer = EncounterTracer(trace_id)
    nba_span_latencies = await fetch_nba_span_latencies(trace_id)
    outcomes: list[EvalOutcome] = []
    with tracer.span(
        "run_online_evaluators", metadata={"encounter_id": encounter_id}
    ) as span:
        outcomes.extend(
            run_deterministic_evaluators(snapshot, nba_span_latencies=nba_span_latencies)
        )
        if include_model_evals:
            outcomes.extend(
                await run_model_evaluators(snapshot, registry=registry, tracer=tracer)
            )
        span.update(
            output={
                o.evaluator: {"score": o.score, "passed": o.passed} for o in outcomes
            }
        )

    rows = [
        m.EvalResult(
            id=m.new_id("eval"),
            encounter_id=encounter_id,
            evaluator=o.evaluator,
            kind=o.kind,
            score=o.score,
            passed=o.passed,
            detail=o.detail,
            prompt_version=o.prompt_version,
        )
        for o in outcomes
    ]
    async with session_factory() as session:
        await session.execute(
            delete(m.EvalResult).where(m.EvalResult.encounter_id == encounter_id)
        )
        session.add_all(rows)
        await session.commit()
    tracer.flush()
    logger.info(
        "Online evals persisted for %s: %s",
        encounter_id,
        {r.evaluator: (r.score, r.passed) for r in rows},
    )
    return rows


def gates_passed(outcomes: Iterable[Any]) -> tuple[bool, list[str]]:
    """(all deterministic gates green, failing gate names)."""
    by_name = {getattr(o, "evaluator", None): o for o in outcomes}
    failing = [
        name
        for name in DETERMINISTIC_GATES
        if name not in by_name or not by_name[name].passed
    ]
    return (not failing, failing)
