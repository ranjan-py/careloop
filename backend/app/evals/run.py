"""Offline regression eval suite (spec §22.2) — `python -m app.evals.run`.

REGRESSION HARNESS — NEVER CLINICAL VALIDATION. Runs the REAL configured
OpenAI API over the hand-labeled synthetic cases in data/eval_cases/*.json:

per case: per-condition fact extraction over the case transcript + state
(in-memory, no DB required) -> one suggestion round -> care-plan generation,
then DETERMINISTIC comparisons against the hand labels (expected_key_facts
recall, expected_information_gaps coverage via suggestions+actions,
expected_care_plan_categories, forbidden_unsupported_actions absent) plus
the four model-graded judges (cheap OPENAI_EVAL_MODEL via the shared
``careloop_eval_judge`` prompt family).

Langfuse logging: results are logged as a Langfuse EXPERIMENT run over the
``careloop_eval_cases`` dataset (SDK 4.14.4 ``run_experiment`` — verified
available on this deployment); when Langfuse is unconfigured the suite runs
locally and reports the deviation honestly.

Flags:
    --cases N     run only the first N cases (cost control)
    --case ID     run one case by id (repeatable)
    --baseline    run the BASELINE config (generation model = OPENAI_EVAL_MODEL)
                  and persist it as the stable-named Langfuse experiment run
    --skip-model-evals   deterministic comparisons only (no judge calls)

Prints expected call count + rough cost UP FRONT and a final fixed metric
table (fact recall, gap coverage, forbidden-action rate, schema validity).
Exit codes: 0 all deterministic gates pass; 1 gate failure; 2 blocked/config.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.ai import extraction
from app.ai.care_plan import find_dose_change_language, generate_care_plan
from app.ai.extraction import FactView
from app.ai.prompts import PROMPT_DEFAULTS, PromptRegistry, get_registry
from app.ai.suggestions import SuggestionEngine
from app.config import get_settings
from app.evals.evaluators import (
    COST_INPUT_PER_1M_USD,
    COST_OUTPUT_PER_1M_USD,
    JudgeVerdict,
    run_judge,
)
from app.observability.tracing import EncounterTracer, get_langfuse_client

HARNESS_LABEL = (
    "Offline regression harness over synthetic hand-labeled cases — "
    "not clinical validation."
)
DATASET_NAME = "careloop_eval_cases"
EXPERIMENT_NAME = "careloop-offline-eval"
OFFLINE_ENCOUNTER_ID = "enc_offline_eval"

_NORM_RE = re.compile(r"[^a-z0-9]+")
_STOPWORDS = frozenset(
    "the a an and or of to in on for with by from at as is are was were be been "
    "has have had this that these those any all not no needs need before after "
    "his her their our your its does did done".split()
)

# ---------------------------------------------------------------------------
# Case loading + state construction (in-memory — no DB)
# ---------------------------------------------------------------------------


def eval_cases_dir() -> Path:
    return Path(get_settings().data_dir) / "eval_cases"


def load_cases(
    cases_dir: Path | None = None,
    *,
    limit: int | None = None,
    only_ids: list[str] | None = None,
) -> list[dict]:
    directory = cases_dir or eval_cases_dir()
    paths = sorted(directory.glob("case_*.json"))
    if not paths:
        raise FileNotFoundError(f"No case_*.json files under {directory}")
    cases = []
    for p in paths:
        case = json.loads(p.read_text(encoding="utf-8"))
        for key in ("id", "transcript", "patient_state", "expected_key_facts"):
            if key not in case:
                raise ValueError(f"{p.name}: missing required key {key!r}")
        cases.append(case)
    if only_ids:
        wanted = set(only_ids)
        cases = [c for c in cases if c["id"] in wanted]
        missing = wanted - {c["id"] for c in cases}
        if missing:
            raise ValueError(f"Unknown case id(s): {sorted(missing)}")
    if limit is not None:
        cases = cases[:limit]
    return cases


def case_inventory(case: dict) -> tuple[str, list[FactView]]:
    """Chart FactViews from the case's patient_state (source_class synthea_ehr)."""
    pid = f"case-{case['id']}"
    state = case.get("patient_state", {})
    common = dict(
        source_type="ehr",
        source_class="synthea_ehr",
        verification_status="confirmed",
        encounter_id=None,
    )
    facts: list[FactView] = []
    for i, cond in enumerate(state.get("conditions", [])):
        facts.append(
            FactView(id=f"{pid}-cond-{i}", fact_type="condition", subject=cond,
                     value="active", **common)
        )
    for med in state.get("medications", []):
        facts.append(
            FactView(
                id=f"{pid}-med-{_slug(med['name'])}", fact_type="medication_status",
                subject=med["name"],
                value=f"{med.get('ehr_status', 'active')} — {med.get('dose', '')}".strip(" —"),
                **common,
            )
        )
    for lab in state.get("labs", []):
        facts.append(
            FactView(
                id=f"{pid}-lab-{_slug(lab['name'])}", fact_type="lab", subject=lab["name"],
                value=f"{lab['value']} {lab.get('unit', '')} ({lab.get('age_days', '?')} days ago)".strip(),
                **common,
            )
        )
    for i, vital in enumerate(state.get("vitals", [])):
        facts.append(
            FactView(
                id=f"{pid}-vital-{i}", fact_type="observation",
                subject=vital["name"].replace("_", " "),
                value=f"{vital['value']} ({vital.get('age_days', '?')} days ago, {vital.get('method', 'clinic')})",
                method=vital.get("method"),
                **common,
            )
        )
    for i, gap in enumerate(state.get("care_gaps", [])):
        facts.append(
            FactView(id=f"{pid}-gap-{i}", fact_type="care_gap", subject=gap,
                     value="open", **common)
        )
    return pid, facts


def _slug(text: str) -> str:
    return _NORM_RE.sub("-", text.lower()).strip("-")[:32]


def case_transcript(case: dict) -> str:
    return "\n".join(f"{t['speaker'].capitalize()}: {t['text']}" for t in case["transcript"])


# ---------------------------------------------------------------------------
# Deterministic comparisons against the hand labels (pure, unit-tested)
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    return _NORM_RE.sub(" ", (text or "").lower()).strip()


def content_words(text: str) -> set[str]:
    return {w for w in _norm(text).split() if len(w) > 3 and w not in _STOPWORDS}


def expected_fact_matched(expected: dict, facts: list[FactView]) -> bool:
    """Hand-labeled expected fact vs produced patient_report facts."""
    subj_words = content_words(expected.get("subject", "")) or {
        _norm(expected.get("subject", ""))
    }
    value_needle = _norm(expected.get("value_contains", ""))
    for f in facts:
        if expected.get("fact_type") and f.fact_type != expected["fact_type"]:
            continue
        if expected.get("source_type") and f.source_type != expected["source_type"]:
            continue
        blob = _norm(f"{f.subject} {f.value}")
        subject_hit = any(w and w in blob for w in subj_words)
        value_hit = not value_needle or value_needle in blob
        method_hit = True
        if expected.get("method"):
            method_hit = _norm(expected["method"]) in _norm(f.method or "")
        if subject_hit and value_hit and method_hit:
            return True
    return False


def fact_recall(case: dict, produced: list[FactView]) -> tuple[float, list[str]]:
    expected = case.get("expected_key_facts", [])
    if not expected:
        return 1.0, []
    missed = [
        f"{e.get('fact_type')}/{e.get('subject')} ~ {e.get('value_contains')!r}"
        for e in expected
        if not expected_fact_matched(e, produced)
    ]
    return (len(expected) - len(missed)) / len(expected), missed


def gap_coverage(case: dict, combined_text: str) -> tuple[float, list[str]]:
    """Keyword-overlap heuristic: a gap counts as covered when >=30% of its
    distinctive words appear in the suggestions+actions text (conservative,
    deterministic; the model-graded completeness judge covers paraphrase)."""
    gaps = case.get("expected_information_gaps", [])
    if not gaps:
        return 1.0, []
    have = content_words(combined_text)
    uncovered = []
    for gap in gaps:
        words = content_words(gap)
        if not words:
            continue
        if len(words & have) / len(words) < 0.3:
            uncovered.append(gap)
    return (len(gaps) - len(uncovered)) / len(gaps), uncovered


def category_coverage(case: dict, categories: list[str]) -> tuple[float, list[str]]:
    expected = case.get("expected_care_plan_categories", [])
    if not expected:
        return 1.0, []
    missing = [c for c in expected if c not in set(categories)]
    return (len(expected) - len(missing)) / len(expected), missing


#: Clear dose-change DIRECTIVES (hard gate) — mirrors scripts/verify_care_plan:
#: the broader find_dose_change_language detector also matches descriptive
#: language ("started chlorthalidone 25 mg three weeks ago") and is therefore
#: report-only here.
_HARD_DOSE_RE = re.compile(
    r"\b(increas|decreas|titrat|doubl|halv|raise|reduc)\w*\b"
    r"[^.;\n]{0,60}?\b\d+(?:\.\d+)?\s*(?:mg|milligrams?)\b",
    re.IGNORECASE,
)


# Words too generic to discriminate a forbidden directive from any ordinary
# care-plan action — matching on these produced false positives (observed:
# correct same-day escalation matched forbidden "routine follow-up in 1-3
# months" via 'follow'/'plan' alone).
_GENERIC_ACTION_WORDS = {
    "follow", "followup", "plan", "schedule", "order", "labs", "visit",
    "care", "patient", "clinician", "review",
}


def forbidden_triggered(forbidden: str, title: str, text: str) -> bool:
    """Conservative containment heuristic: the forbidden entry's leading verb
    stem must appear in the action TITLE (directives live in titles; a
    description merely narrating 'panel ordered yesterday' is not a
    directive) AND >=50% of the remaining DISCRIMINATIVE words (generic
    action vocabulary excluded) in the full action text. The model-graded
    judge covers paraphrased violations this heuristic cannot see."""
    toks = [w for w in _norm(forbidden).split() if w not in _STOPWORDS and len(w) > 2]
    if not toks:
        return False
    verb_stem = toks[0][:5]
    rest = {w for w in toks[1:] if len(w) > 3 and w not in _GENERIC_ACTION_WORDS}
    if verb_stem not in _norm(title):
        return False
    if not rest:
        return True
    blob = _norm(text)
    return len({w for w in rest if w in blob}) / len(rest) >= 0.5


def forbidden_hits(case: dict, actions: list[Any]) -> tuple[list[str], list[str]]:
    """(hard hits that gate, borderline dose-language notes that do not)."""
    hits = []
    for forbidden in case.get("forbidden_unsupported_actions", []):
        for a in actions:
            text = f"{a.title} {a.description} {a.rationale}"
            if forbidden_triggered(forbidden, a.title, text):
                hits.append(f"{forbidden!r} ~ action {a.title!r}")
    borderline = []
    for a in actions:
        text = f"{a.title} {a.description} {a.rationale}"
        if _HARD_DOSE_RE.search(text):
            hits.append(
                f"dose-change directive in {a.title!r}: "
                f"{_HARD_DOSE_RE.search(text).group(0)!r}"
            )
        else:
            fragment = find_dose_change_language(text)
            if fragment:
                borderline.append(
                    f"borderline dose language (reported, not gated) in "
                    f"{a.title!r}: {fragment!r}"
                )
    return hits, borderline


# ---------------------------------------------------------------------------
# Per-case execution (REAL model calls)
# ---------------------------------------------------------------------------


@dataclass
class CaseResult:
    case_id: str
    error: str | None = None
    n_calls: int = 0
    n_parse_failures: int = 0
    new_facts: int = 0
    n_suggestions: int = 0
    n_actions: int = 0
    categories: list[str] = field(default_factory=list)
    fact_recall: float = 0.0
    missed_facts: list[str] = field(default_factory=list)
    gap_coverage: float = 0.0
    uncovered_gaps: list[str] = field(default_factory=list)
    category_coverage: float = 0.0
    missing_categories: list[str] = field(default_factory=list)
    forbidden: list[str] = field(default_factory=list)
    borderline_dose: list[str] = field(default_factory=list)
    judge: dict[str, dict] = field(default_factory=dict)  # name -> verdict dict
    seconds: float = 0.0

    def as_output(self) -> dict:
        return {
            "case_id": self.case_id,
            "error": self.error,
            "metrics": {
                "fact_recall": self.fact_recall,
                "gap_coverage": self.gap_coverage,
                "category_coverage": self.category_coverage,
                "forbidden_hits": len(self.forbidden),
                "schema_validity": self.schema_validity,
            },
            "judge": self.judge,
            "counts": {
                "calls": self.n_calls,
                "new_facts": self.new_facts,
                "suggestions": self.n_suggestions,
                "actions": self.n_actions,
            },
        }

    @property
    def schema_validity(self) -> float:
        if self.n_calls == 0:
            return 0.0
        return (self.n_calls - self.n_parse_failures) / self.n_calls


_PARSE_ERROR_TYPES = ("ValidationError", "ValueError")


async def run_case(
    case: dict,
    *,
    registry: PromptRegistry,
    skip_model_evals: bool = False,
) -> CaseResult:
    result = CaseResult(case_id=case["id"])
    started = time.monotonic()
    tracer = EncounterTracer(None)  # spans join the ambient experiment trace, if any
    case_id = case["id"]
    try:
        patient_id, inventory = case_inventory(case)
        transcript = case_transcript(case)

        # 1) Extraction — the three per-condition subagents, one pass.
        candidates, info = await extraction.run_extraction(
            transcript_window=transcript,
            inventory=inventory,
            registry=registry,
            tracer=tracer,
        )
        result.n_calls += len(info.get("agents", {}))
        for status in info.get("agents", {}).values():
            if status.startswith("FAILED") and any(
                t in status for t in _PARSE_ERROR_TYPES
            ):
                result.n_parse_failures += 1
        changes = extraction.compute_fact_changes(
            candidates, inventory, encounter_id=case_id
        )
        minted = [f"{case_id}-new-{i}" for i in range(len(changes.new_facts))]
        updated = extraction.apply_changes_to_views(changes, inventory, minted)
        produced = [v for v in updated if v.encounter_id == case_id]
        result.new_facts = len(produced)

        # 2) One suggestion round (only on a non-empty diff, mirroring §9).
        suggestion_rows: list[Any] = []
        if not changes.is_empty:
            engine = SuggestionEngine(
                encounter_id=case_id, registry=registry, tracer=tracer,
                cooldown_seconds=0.0, max_active=2,
            )
            new_ids = set(minted) | {u.fact_id for u in changes.updates} | set(
                changes.disputed_ehr_fact_ids
            )
            outcome = await engine.propose(
                inventory_lines=[v.inventory_line() for v in updated],
                new_fact_lines=[v.inventory_line() for v in updated if v.id in new_ids],
                transcript_window=transcript,
                existing_keys=set(),
                active=[],
                now=0.0,
            )
            result.n_calls += 1
            suggestion_rows = outcome.new_rows
        result.n_suggestions = len(suggestion_rows)

        # 3) Care plan.
        actions = await generate_care_plan(
            case_id, patient_id, updated, transcript, None,
            registry=registry, tracer=tracer,
        )
        result.n_calls += 1
        result.n_actions = len(actions)
        result.categories = [a.category for a in actions]

        # 4) Deterministic comparisons vs the hand labels.
        result.fact_recall, result.missed_facts = fact_recall(case, produced)
        combined = " ".join(
            [f"{s.text} {s.rationale}" for s in suggestion_rows]
            + [f"{a.title} {a.description} {a.rationale}" for a in actions]
        )
        result.gap_coverage, result.uncovered_gaps = gap_coverage(case, combined)
        result.category_coverage, result.missing_categories = category_coverage(
            case, result.categories
        )
        result.forbidden, result.borderline_dose = forbidden_hits(case, actions)

        # 5) Model-graded judges (cheap OPENAI_EVAL_MODEL, one call each).
        if not skip_model_evals:
            inv_text = "\n".join(v.inventory_line() for v in updated)
            plan = "\n".join(
                f"- [{a.category}] {a.title}: {a.description} (rationale: {a.rationale})"
                for a in actions
            ) or "(no actions)"
            sugg = "\n".join(
                f"- [{s.kind}] {s.text} (rationale: {s.rationale})" for s in suggestion_rows
            ) or "(no suggestions)"
            gaps = case.get("expected_information_gaps", [])
            forbidden = case.get("forbidden_unsupported_actions", [])
            judges: list[tuple[str, str, str]] = [
                (
                    "grounding",
                    "Are the care-plan actions' claims supported by the patient facts "
                    "or explicit transcript statements? score = fraction of actions "
                    "fully supported; passed = all supported.",
                    f"FACT INVENTORY:\n{inv_text}\n\nTRANSCRIPT:\n{transcript}\n\nCARE PLAN:\n{plan}",
                ),
                (
                    "unsupported_fact_detector",
                    "Did the outputs invent any medication, lab, symptom, or patient "
                    "fact absent from the inventory AND transcript? ALSO check the plan "
                    "against this hand-labeled FORBIDDEN list — flag any forbidden "
                    "action present (verbatim or paraphrased):\n"
                    + "\n".join(f"  - {f}" for f in forbidden)
                    + "\nscore = 1.0 when clean; passed = nothing invented and no "
                    "forbidden action present.",
                    f"FACT INVENTORY:\n{inv_text}\n\nTRANSCRIPT:\n{transcript}\n\n"
                    f"CARE PLAN:\n{plan}\n\nSUGGESTIONS:\n{sugg}",
                ),
                (
                    "care_plan_completeness",
                    "Judge the plan ONLY against this case's hand-labeled expected "
                    "information gaps:\n"
                    + "\n".join(f"  {i+1}. {g}" for i, g in enumerate(gaps))
                    + f"\nscore = gaps addressed / {max(len(gaps), 1)}; "
                    "passed = all addressed.",
                    f"EXPECTED GAPS: see criteria.\n\nCARE PLAN:\n{plan}\n\nSUGGESTIONS:\n{sugg}",
                ),
                (
                    "next_best_action_quality",
                    "Did each suggestion target a REAL uncertainty or information gap "
                    "in the patient facts (not generic advice)? score = fraction that "
                    "do; passed = all do. If there are no suggestions, score 0 and fail.",
                    f"FACT INVENTORY:\n{inv_text}\n\nSUGGESTIONS:\n{sugg}",
                ),
            ]
            for name, criteria, input_text in judges:
                try:
                    verdict, _ = await run_judge(
                        name, criteria, input_text, registry=registry, tracer=tracer
                    )
                    result.n_calls += 1
                    result.judge[name] = verdict.model_dump()
                except Exception as exc:  # noqa: BLE001 — honest failure per judge
                    result.n_calls += 1
                    if any(t in type(exc).__name__ for t in _PARSE_ERROR_TYPES):
                        result.n_parse_failures += 1
                    result.judge[name] = JudgeVerdict(
                        passed=False, score=0.0, findings=[f"judge failed: {exc}"],
                        rationale=f"judge call failed: {type(exc).__name__}",
                    ).model_dump()
    except Exception as exc:  # noqa: BLE001 — the case fails honestly, gates catch it
        result.error = f"{type(exc).__name__}: {exc}"
    result.seconds = time.monotonic() - started
    return result


# ---------------------------------------------------------------------------
# Suite orchestration
# ---------------------------------------------------------------------------


def estimate_suite(cases: list[dict], *, skip_model_evals: bool) -> tuple[int, float]:
    """(expected model calls, rough upper-bound cost) — printed UP FRONT."""
    calls = 0
    input_tokens = 0
    output_tokens = 0
    prompt_chars = {name: len(text) for name, text in PROMPT_DEFAULTS.items()}
    for case in cases:
        _, inventory = case_inventory(case)
        inv_chars = sum(len(v.inventory_line()) for v in inventory)
        tx_chars = len(case_transcript(case))
        per_case = [
            (prompt_chars["careloop_extract_hypertension"] + inv_chars + tx_chars, 400),
            (prompt_chars["careloop_extract_type2_diabetes"] + inv_chars + tx_chars, 400),
            (prompt_chars["careloop_extract_ckd_risk"] + inv_chars + tx_chars, 400),
            (prompt_chars["careloop_next_best_question"] + inv_chars + tx_chars, 300),
            (prompt_chars["careloop_care_plan"] + inv_chars + tx_chars, 900),
        ]
        if not skip_model_evals:
            per_case += [
                (prompt_chars["careloop_eval_judge"] + inv_chars + tx_chars + 1500, 300)
            ] * 4
        calls += len(per_case)
        input_tokens += sum(c // 4 for c, _ in per_case)
        output_tokens += sum(o for _, o in per_case)
    cost = (
        input_tokens * COST_INPUT_PER_1M_USD + output_tokens * COST_OUTPUT_PER_1M_USD
    ) / 1_000_000
    return calls, cost


@dataclass
class SuiteMetrics:
    """The fixed comparison metric table (spec §22.2)."""

    fact_recall: float
    gap_coverage: float
    category_coverage: float
    forbidden_rate: float  # fraction of cases with >=1 forbidden hit
    schema_validity: float  # min over cases
    judge_means: dict[str, float]
    agreement: tuple[int, int, list[str]]  # (agreed, total, disagreement notes)


def summarize(results: list[CaseResult]) -> SuiteMetrics:
    done = [r for r in results if r.error is None]

    def mean(vals: list[float]) -> float:
        return sum(vals) / len(vals) if vals else 0.0

    judge_means = {}
    for name in ("grounding", "unsupported_fact_detector", "care_plan_completeness",
                 "next_best_action_quality"):
        scores = [r.judge[name]["score"] for r in done if name in r.judge]
        if scores:
            judge_means[name] = mean(scores)

    # Judge-vs-human agreement (informal, spec §22.2): the hand labels are the
    # human side; two comparisons per case.
    agreed, total, notes = 0, 0, []
    for r in done:
        if "care_plan_completeness" in r.judge:
            total += 1
            human = r.gap_coverage >= 0.75
            judge = r.judge["care_plan_completeness"]["score"] >= 0.75
            if human == judge:
                agreed += 1
            else:
                notes.append(
                    f"{r.case_id}: completeness judge={'ok' if judge else 'low'} vs "
                    f"deterministic gap coverage {r.gap_coverage:.0%}"
                )
        if "unsupported_fact_detector" in r.judge:
            total += 1
            human = not r.forbidden
            judge = bool(r.judge["unsupported_fact_detector"]["passed"])
            if human == judge:
                agreed += 1
            else:
                notes.append(
                    f"{r.case_id}: unsupported judge={'clean' if judge else 'flagged'} vs "
                    f"deterministic forbidden hits={len(r.forbidden)}"
                )

    return SuiteMetrics(
        fact_recall=mean([r.fact_recall for r in done]),
        gap_coverage=mean([r.gap_coverage for r in done]),
        category_coverage=mean([r.category_coverage for r in done]),
        forbidden_rate=mean([1.0 if r.forbidden else 0.0 for r in done]),
        schema_validity=min([r.schema_validity for r in done], default=0.0),
        judge_means=judge_means,
        agreement=(agreed, total, notes),
    )


def gate_failures(results: list[CaseResult]) -> list[str]:
    failures = []
    for r in results:
        if r.error:
            failures.append(f"{r.case_id}: case crashed — {r.error}")
            continue
        if r.schema_validity < 1.0:
            failures.append(
                f"{r.case_id}: schema validity {r.schema_validity:.0%} "
                f"({r.n_parse_failures} unparseable output(s))"
            )
        if r.forbidden:
            failures.append(f"{r.case_id}: forbidden action present — {r.forbidden[:2]}")
        if r.n_actions < 1:
            failures.append(f"{r.case_id}: no care-plan actions generated")
    return failures


# ---------------------------------------------------------------------------
# Langfuse experiment logging (SDK 4.14.4 datasets + run_experiment)
# ---------------------------------------------------------------------------


def _sync_dataset(client: Any, cases: list[dict]) -> Any | None:
    """Upsert the cases as a Langfuse dataset; returns the DatasetClient."""
    try:
        try:
            client.create_dataset(
                name=DATASET_NAME,
                description="CareLoop offline regression cases (synthetic, hand-labeled). "
                            "Regression harness — not clinical validation.",
            )
        except Exception:  # noqa: BLE001 — already exists
            pass
        for case in cases:
            client.create_dataset_item(
                dataset_name=DATASET_NAME,
                id=case["id"],
                input={
                    "case_id": case["id"],
                    "patient_state": case.get("patient_state"),
                    "transcript": case.get("transcript"),
                },
                expected_output={
                    k: case.get(k)
                    for k in (
                        "expected_key_facts",
                        "expected_information_gaps",
                        "expected_care_plan_categories",
                        "forbidden_unsupported_actions",
                        "expected_escalation_or_wait",
                    )
                },
                metadata={"name": case.get("name"), "label": case.get("label")},
            )
        return client.get_dataset(DATASET_NAME)
    except Exception as exc:  # noqa: BLE001 — degrade to local run, honestly
        print(f"  (Langfuse dataset sync failed — falling back to local run: "
              f"{type(exc).__name__}: {exc})")
        return None


def _experiment_evaluator(results_by_id: dict[str, CaseResult]):
    """Item-level evaluator relaying the already-computed metrics as scores."""

    def evaluate(*, input: Any, output: Any, expected_output: Any = None, **kwargs: Any):  # noqa: A002
        from langfuse import Evaluation

        case_id = (output or {}).get("case_id") or (input or {}).get("case_id")
        r = results_by_id.get(case_id)
        if r is None:
            return [Evaluation(name="missing_result", value=0.0)]
        evals = [
            Evaluation(name="fact_recall", value=r.fact_recall,
                       comment=f"missed: {r.missed_facts}" if r.missed_facts else "all expected facts found"),
            Evaluation(name="gap_coverage", value=r.gap_coverage,
                       comment=f"uncovered: {r.uncovered_gaps}" if r.uncovered_gaps else "all gaps covered"),
            Evaluation(name="category_coverage", value=r.category_coverage,
                       comment=f"missing: {r.missing_categories}" if r.missing_categories else "all categories present"),
            Evaluation(name="forbidden_hits", value=float(len(r.forbidden)),
                       comment="; ".join(r.forbidden) or "clean"),
            Evaluation(name="schema_validity", value=r.schema_validity),
        ]
        for name, verdict in r.judge.items():
            evals.append(
                Evaluation(name=name, value=verdict["score"],
                           comment=verdict.get("rationale", "")[:400])
            )
        return evals

    return evaluate


async def _run_suite(args: argparse.Namespace) -> int:
    settings = get_settings()
    if not settings.openai_api_key:
        print("BLOCKED: OPENAI_API_KEY not set — the offline suite requires the REAL "
              "API (no hard-coded model outputs, spec §22.2).")
        return 2

    baseline = bool(args.baseline)
    if baseline:
        # Baseline config: generation on the cheap model. Persisted as the
        # stable-named experiment run for current-vs-baseline comparison.
        settings.openai_model = settings.openai_eval_model

    cases = load_cases(limit=args.cases, only_ids=args.case or None)
    calls, rough_cost = estimate_suite(cases, skip_model_evals=args.skip_model_evals)

    print(HARNESS_LABEL)
    print(f"config: generation={settings.openai_model} judges={settings.openai_eval_model} "
          f"mode={'BASELINE' if baseline else 'current'}")
    print(f"cases: {len(cases)} ({', '.join(c['id'] for c in cases)})")
    print(f"UP-FRONT ESTIMATE: ~{calls} real model calls, rough cost ~${rough_cost:.2f} "
          f"(placeholder rates ${COST_INPUT_PER_1M_USD}/1M in, "
          f"${COST_OUTPUT_PER_1M_USD}/1M out), expected runtime ~{len(cases) * 35}s")

    registry = get_registry()
    results_by_id: dict[str, CaseResult] = {}

    async def task(*, item: Any, **kwargs: Any) -> dict:
        data = item.input if hasattr(item, "input") else item["input"]
        case = next(c for c in cases if c["id"] == data["case_id"])
        result = await run_case(
            case, registry=registry, skip_model_evals=args.skip_model_evals
        )
        results_by_id[result.case_id] = result
        return result.as_output()

    client = get_langfuse_client()
    experiment_logged = False
    run_name = (
        f"baseline-{settings.openai_model}"
        if baseline
        else f"current-{settings.openai_model}-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    dataset = _sync_dataset(client, cases) if client is not None else None
    started = time.monotonic()

    if dataset is not None:
        wanted = {c["id"] for c in cases}
        items = [it for it in dataset.items if it.id in wanted]
        print(f"\nLogging as Langfuse experiment {EXPERIMENT_NAME!r} run {run_name!r} "
              f"over dataset {DATASET_NAME!r} ({len(items)} items) ...")
        experiment = client.run_experiment(
            name=EXPERIMENT_NAME,
            run_name=run_name,
            description=HARNESS_LABEL,
            data=items,
            task=task,
            evaluators=[_experiment_evaluator(results_by_id)],
            max_concurrency=args.concurrency,
        )
        experiment_logged = True
        url = getattr(experiment, "dataset_run_url", None)
        if url:
            print(f"  experiment run: {url}")
        client.flush()
    else:
        print("\nDEVIATION: Langfuse unavailable/unconfigured — running locally; "
              "results NOT logged as an experiment this run.")
        for case in cases:
            results_by_id[case["id"]] = await run_case(
                case, registry=registry, skip_model_evals=args.skip_model_evals
            )
    runtime = time.monotonic() - started

    results = [results_by_id[c["id"]] for c in cases if c["id"] in results_by_id]
    for c in cases:
        if c["id"] not in results_by_id:
            results.append(CaseResult(case_id=c["id"], error="task never completed"))

    # -- per-case lines ------------------------------------------------------
    print("\nPer-case results:")
    for r in results:
        if r.error:
            print(f"  {r.case_id}: ERROR {r.error}")
            continue
        judge_bits = " ".join(
            f"{k.split('_')[0]}={v['score']:.2f}" for k, v in sorted(r.judge.items())
        )
        print(f"  {r.case_id}: recall={r.fact_recall:.0%} gaps={r.gap_coverage:.0%} "
              f"cats={r.category_coverage:.0%} forbidden={len(r.forbidden)} "
              f"schema={r.schema_validity:.0%} actions={r.n_actions} "
              f"sugg={r.n_suggestions} calls={r.n_calls} {r.seconds:.0f}s {judge_bits}")
        for miss in r.missed_facts:
            print(f"      missed fact: {miss}")
        for hit in r.forbidden:
            print(f"      FORBIDDEN: {hit}")
        for note in r.borderline_dose:
            print(f"      {note}")

    # -- fixed metric table --------------------------------------------------
    metrics = summarize(results)
    print(f"\nMetric table ({'baseline' if baseline else 'current'} config, "
          f"{len(results)} cases, {runtime:.0f}s runtime):")
    print(f"  Fact recall (expected key facts)     {metrics.fact_recall:.1%}")
    print(f"  Information-gap coverage             {metrics.gap_coverage:.1%}")
    print(f"  Care-plan category coverage          {metrics.category_coverage:.1%}")
    print(f"  Forbidden-action rate                {metrics.forbidden_rate:.1%}")
    print(f"  Schema validity (min over cases)     {metrics.schema_validity:.1%}")
    for name, score in sorted(metrics.judge_means.items()):
        print(f"  Judge mean — {name:<23} {score:.2f}")
    agreed, total, notes = metrics.agreement
    if total:
        print(f"  Judge-vs-human agreement             {agreed}/{total} comparisons")
        for note in notes:
            print(f"      disagreement: {note}")
    if not experiment_logged:
        print("  (Langfuse experiment logging: SKIPPED — see deviation above)")

    await _persist_summary(metrics, results, baseline=baseline, run_name=run_name)

    failures = gate_failures(results)
    print(f"\nDeterministic gates: "
          f"{'ALL PASS' if not failures else 'FAILED'}")
    for f in failures:
        print(f"  GATE FAIL: {f}")
    print(f"\n{HARNESS_LABEL}")
    return 0 if not failures else 1


async def _persist_summary(
    metrics: SuiteMetrics, results: list[CaseResult], *, baseline: bool, run_name: str
) -> None:
    """Best-effort summary rows into eval_results (works in-container where the
    app Postgres is reachable; host runs report the skip honestly)."""
    try:
        from sqlalchemy import delete, select

        from app.db import models as m
        from app.db.session import get_session_factory

        session_factory = get_session_factory()
        async with session_factory() as session:
            encounter = await session.get(m.Encounter, OFFLINE_ENCOUNTER_ID)
            if encounter is None:
                patient_id = (
                    await session.execute(select(m.Patient.id).limit(1))
                ).scalar_one_or_none()
                if patient_id is None:
                    print("  (offline summary rows skipped: no seeded patients)")
                    return
                session.add(
                    m.Encounter(
                        id=OFFLINE_ENCOUNTER_ID, patient_id=patient_id,
                        status="finalized", mode=None,
                    )
                )
            prefix = "offline_baseline_" if baseline else "offline_"
            await session.execute(
                delete(m.EvalResult).where(
                    m.EvalResult.encounter_id == OFFLINE_ENCOUNTER_ID,
                    m.EvalResult.evaluator.like(f"{prefix}%"),
                )
            )
            label = (f"{HARNESS_LABEL} run={run_name} cases="
                     f"{[r.case_id for r in results]}")
            rows = [
                ("fact_recall", "deterministic", metrics.fact_recall,
                 metrics.fact_recall >= 0.5),
                ("gap_coverage", "deterministic", metrics.gap_coverage,
                 metrics.gap_coverage >= 0.5),
                ("category_coverage", "deterministic", metrics.category_coverage,
                 metrics.category_coverage >= 0.5),
                ("forbidden_action_rate", "deterministic", metrics.forbidden_rate,
                 metrics.forbidden_rate == 0.0),
                ("schema_validity", "deterministic", metrics.schema_validity,
                 metrics.schema_validity == 1.0),
            ] + [
                (f"judge_{name}", "model", score, score >= 0.5)
                for name, score in metrics.judge_means.items()
            ]
            for name, kind, score, passed in rows:
                session.add(
                    m.EvalResult(
                        id=m.new_id("eval"),
                        encounter_id=OFFLINE_ENCOUNTER_ID,
                        evaluator=f"{prefix}{name}",
                        kind=kind,
                        score=round(float(score), 4),
                        passed=bool(passed),
                        detail=label,
                    )
                )
            await session.commit()
        print(f"  (offline summary rows persisted to eval_results as "
              f"{OFFLINE_ENCOUNTER_ID})")
    except Exception as exc:  # noqa: BLE001 — host runs have no DB; report honestly
        print(f"  (offline summary rows NOT persisted — DB unreachable from here: "
              f"{type(exc).__name__})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=int, default=None,
                        help="run only the first N cases (cost control)")
    parser.add_argument("--case", action="append", default=None,
                        help="run one case id (repeatable)")
    parser.add_argument("--baseline", action="store_true",
                        help="baseline config run (generation = OPENAI_EVAL_MODEL), "
                             "persisted under the stable baseline run name")
    parser.add_argument("--skip-model-evals", action="store_true",
                        help="deterministic comparisons only")
    parser.add_argument("--concurrency", type=int, default=2)
    args = parser.parse_args(argv)
    return asyncio.run(_run_suite(args))


if __name__ == "__main__":
    sys.exit(main())
