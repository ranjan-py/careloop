"""Post-visit reports (spec §16) — clinician summary + patient instructions.

ONE consolidated prompt family (``careloop_report``) with an AUDIENCE
parameter (spec §10: fewer prompts, fewer versions to track) generates both
reports from:

- longitudinal + encounter facts,
- the FINAL decided plan: approved/modified actions only, with modified
  actions in their clinician-edited FINAL wording (the superseded originals
  are NEVER shown to the model),
- an explicit EXCLUSION list of rejected actions the prompt must not mention.

Defense in depth (spec §14/§22.1): the deterministic ``leakage_check`` runs
on every generated report — if a rejected action or a modified action's
superseded original leaks into the text, the generation is retried ONCE with
the violations called out; a second failure raises ``ReportLeakageError``
(honest failure, never silently shipped).

Traced as ``generate_clinician_summary`` / ``generate_patient_instructions``
spans (each with a ``context_assembly`` child) on the encounter trace id.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass

from pydantic import BaseModel

from app.ai import client as ai_client
from app.ai.extraction import FactView, estimate_tokens
from app.ai.prompts import PromptRegistry, get_registry
from app.config import get_settings
from app.observability.tracing import EncounterTracer, record_usage

logger = logging.getLogger(__name__)

PROMPT_NAME = "careloop_report"

AUDIENCES: tuple[str, ...] = ("clinician", "patient")
_SPAN_NAMES = {
    "clinician": "generate_clinician_summary",
    "patient": "generate_patient_instructions",
}

_NORM_RE = re.compile(r"[^a-z0-9]+")


class ReportLeakageError(RuntimeError):
    """A report reintroduced rejected/superseded content after the retry."""

    def __init__(self, audience: str, violations: list[str]) -> None:
        self.audience = audience
        self.violations = violations
        super().__init__(
            f"{audience} report failed leakage_check after retry: {'; '.join(violations)}"
        )


# ---------------------------------------------------------------------------
# Inputs (built by the caller from persisted actions + decisions)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecidedAction:
    """One approved/modified action in its FINAL form (spec §14: reports render
    the clinician's FINAL version, never the superseded original)."""

    category: str
    title: str  # FINAL title (clinician-edited for modified actions)
    description: str  # FINAL description
    status: str  # "approved" | "modified"
    original_title: str | None = None  # modified only — for leakage_check, NOT the prompt
    original_description: str | None = None


@dataclass(frozen=True)
class RejectedAction:
    """Explicit exclusion-list entry — the prompt must not mention it."""

    title: str
    category: str | None = None


class ReportOutput(BaseModel):
    """Schema-validated model output for one audience."""

    title: str
    body: str


@dataclass(frozen=True)
class GeneratedReport:
    audience: str
    title: str
    body: str
    prompt_version: str | None
    model_version: str | None
    retried: bool

    @property
    def text(self) -> str:
        return f"{self.title}\n\n{self.body}" if self.title.strip() else self.body


@dataclass(frozen=True)
class ReportBundle:
    clinician: GeneratedReport
    patient: GeneratedReport

    @property
    def clinician_summary(self) -> str:
        return self.clinician.text

    @property
    def patient_instructions(self) -> str:
        return self.patient.text


# ---------------------------------------------------------------------------
# Deterministic leakage check (also used by the §22.1 evaluators)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Output-degeneration guard. Observed on gpt-5-nano (user-reported): the model
# finished the document, then emitted hundreds of trailing braces plus
# meta-commentary ("extraneous trailing braces are not intended… Here's the
# corrected output") INSIDE the schema's string field — schema validation
# cannot catch content inside a valid string. Deterministic sanitation +
# one retry; the backend never renders raw degeneration.
# ---------------------------------------------------------------------------

_BRACE_RUN_RE = re.compile(r"(?:[{}\[\]]\s*){5,}")
_META_MARKERS: tuple[str, ...] = (
    "extraneous trailing braces",
    "output is garbled",
    "corrected output",
    "clean json",
    "valid json",
    "final answer is",
    "let's finalize",
    "sorry for confusion",
    "continuous retry prevented",
    "provide just the two fields",
)

#: Sanitized body shorter than this = the document itself was destroyed.
_MIN_BODY_CHARS = 120


class ReportDegenerationError(RuntimeError):
    """Model output remained degenerate (brace loops / meta-commentary) after retry."""

    def __init__(self, audience: str, issues: list[str]) -> None:
        self.audience = audience
        self.issues = issues
        super().__init__(
            f"{audience} report output degenerate after retry: {'; '.join(issues)}"
        )


def sanitize_generated_text(text: str) -> tuple[str, list[str]]:
    """Strip trailing degeneration deterministically; report what was found.

    Returns (cleaned_text, issues) — empty issues means the text was clean."""
    issues: list[str] = []
    cleaned = text
    match = _BRACE_RUN_RE.search(cleaned)
    if match:
        issues.append(f"brace-run degeneration at char {match.start()}")
        cleaned = cleaned[: match.start()]
    lowered = cleaned.lower()
    cut = len(cleaned)
    for marker in _META_MARKERS:
        idx = lowered.find(marker)
        if idx != -1 and idx < cut:
            cut = idx
            issues.append(f"meta-commentary marker {marker!r}")
    if cut < len(cleaned):
        # Cut at the start of the line carrying the first marker.
        line_start = cleaned.rfind("\n", 0, cut)
        cleaned = cleaned[: line_start if line_start != -1 else cut]
    cleaned = cleaned.rstrip(" \t\n{}[]\"'")
    return cleaned.strip(), issues


def _normalize(text: str) -> str:
    return _NORM_RE.sub(" ", text.lower()).strip()


def leakage_check(
    report_text: str,
    rejected_titles: list[str],
    modified_pairs: list[tuple[str, str]],
) -> list[str]:
    """Deterministic post-check: returns violation strings (empty == clean).

    - a REJECTED action's title appearing (normalized substring) is a leak;
    - a MODIFIED action's superseded ORIGINAL wording appearing is a leak,
      unless the original text survives verbatim inside the final wording
      (then its presence is expected, not a leak).
    """
    violations: list[str] = []
    norm_report = _normalize(report_text)
    for title in rejected_titles:
        norm_title = _normalize(title)
        if norm_title and norm_title in norm_report:
            violations.append(f"rejected action mentioned: {title!r}")
    for original, final in modified_pairs:
        norm_original = _normalize(original)
        norm_final = _normalize(final)
        if not norm_original or norm_original == norm_final or norm_original in norm_final:
            continue
        if norm_original in norm_report:
            violations.append(f"superseded original of a modified action rendered: {original!r}")
    return violations


def _modified_pairs(decided_actions: list[DecidedAction]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for action in decided_actions:
        if action.status != "modified":
            continue
        if action.original_title:
            pairs.append((action.original_title, action.title))
        if action.original_description:
            pairs.append((action.original_description, action.description))
    return pairs


# ---------------------------------------------------------------------------
# Prompt input assembly
# ---------------------------------------------------------------------------


def build_report_input(
    facts: list[FactView],
    decided_actions: list[DecidedAction],
    rejected_actions: list[RejectedAction],
) -> str:
    """Shared (audience-independent) input. Modified actions appear ONLY in
    final form; rejected actions appear ONLY on the exclusion list."""
    fact_lines = "\n".join(f.inventory_line() for f in facts) or "(no facts on record)"
    # No category tags here — the report renders prose, and models copy
    # bracketed tags verbatim into patient-facing text.
    plan_lines = "\n".join(
        f"- {a.title}: {a.description}" for a in decided_actions
    ) or "(no actions were approved)"
    excluded_lines = "\n".join(
        f"- {r.title}" + (f" [{r.category}]" if r.category else "") for r in rejected_actions
    ) or "(none)"
    return (
        "PATIENT FACTS (chart + this visit, with provenance):\n"
        f"{fact_lines}\n\n"
        "FINAL PLAN (the clinician's decided actions, FINAL wording — render exactly these):\n"
        f"{plan_lines}\n\n"
        "EXCLUDED ACTIONS (rejected by the clinician — never mention these in any form):\n"
        f"{excluded_lines}"
    )


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------


async def _generate_one(
    *,
    audience: str,
    input_text: str,
    rejected_titles: list[str],
    modified_pairs: list[tuple[str, str]],
    fact_ids: list[str],
    registry: PromptRegistry,
    tracer: EncounterTracer,
    usage_totals: list[dict],
) -> GeneratedReport:
    prompt = await registry.aget(PROMPT_NAME)
    settings = get_settings()
    span_name = _SPAN_NAMES.get(audience, f"generate_report_{audience}")

    with tracer.span(
        span_name, prompt_version=prompt.version, metadata={"audience": audience}
    ) as span:
        with tracer.context_assembly(
            task=span_name, fact_ids=fact_ids, token_estimate=estimate_tokens(input_text)
        ):
            pass

        attempt_input = input_text
        violations: list[str] = []
        for attempt in range(2):
            usage: dict = {}
            parsed = await ai_client.call_model(
                task=span_name,
                instructions=prompt.compile(audience=audience),
                input_text=attempt_input,
                output_schema=ReportOutput,
                prompt_version=prompt.version,
                usage_sink=usage,
                # Reports carry the fidelity/leakage guarantees — stronger
                # model (measured: nano paraphrased clinician-edited wording).
                model=settings.openai_generation_model,
            )
            record_usage(span, usage)
            usage_totals.append(usage)

            # Degeneration guard BEFORE leakage: sanitize, retry if destroyed.
            title, title_issues = sanitize_generated_text(parsed.title)
            body, body_issues = sanitize_generated_text(parsed.body)
            degen_issues = title_issues + body_issues
            if degen_issues:
                logger.warning(
                    "%s report output degeneration (attempt %d, sanitized): %s",
                    audience, attempt + 1, degen_issues,
                )
            # Short is only fatal when degeneration was actually detected —
            # a clean short document is the model's legitimate choice.
            if degen_issues and len(body) < _MIN_BODY_CHARS:
                degen_issues.append(f"body only {len(body)} chars after sanitation")
                attempt_input = (
                    input_text
                    + "\n\nYOUR PREVIOUS ATTEMPT contained formatting garbage (stray "
                    "braces or commentary about JSON/output). The schema string "
                    "fields must contain ONLY the finished document text — stop "
                    "cleanly at its final sentence."
                )
                violations = degen_issues
                continue

            violations = leakage_check(
                f"{title}\n{body}", rejected_titles, modified_pairs
            )
            if not violations:
                span.update(
                    output={
                        "title": title,
                        "body": body,
                        "retried": attempt > 0,
                        "sanitized": bool(degen_issues),
                    }
                )
                return GeneratedReport(
                    audience=audience,
                    title=title,
                    body=body,
                    prompt_version=prompt.version,
                    model_version=usage.get("model") or settings.openai_model,
                    retried=attempt > 0,
                )
            logger.warning(
                "%s report failed leakage_check (attempt %d): %s",
                audience, attempt + 1, violations,
            )
            attempt_input = (
                input_text
                + "\n\nYOUR PREVIOUS ATTEMPT WAS REJECTED for mentioning excluded or "
                "superseded content:\n"
                + "\n".join(f"- {v}" for v in violations)
                + "\nRegenerate the report WITHOUT any reference to that content."
            )

        span.update(output={"error": "generation failed after retry", "violations": violations})
        if violations and any("chars after sanitation" in v or "degeneration" in v for v in violations):
            raise ReportDegenerationError(audience, violations)
        raise ReportLeakageError(audience, violations)


async def generate_reports(
    encounter_id: str,
    patient_id: str,
    facts: list[FactView],
    decided_actions: list[DecidedAction],
    rejected_actions: list[RejectedAction],
    trace_id: str | None,
    usage_sink: dict | None = None,
    *,
    registry: PromptRegistry | None = None,
    tracer: EncounterTracer | None = None,
) -> ReportBundle:
    """Generate both reports via the audience-parameterized prompt family.

    ``decided_actions`` must contain ONLY approved/modified actions, modified
    ones already in their clinician-edited FINAL form. ``rejected_actions`` is
    the explicit exclusion list. Raises ReportLeakageError when a report still
    leaks after one retry — the caller surfaces the failure, never fakes text.
    """
    registry = registry or get_registry()
    tracer = tracer or EncounterTracer(trace_id)

    input_text = build_report_input(facts, decided_actions, rejected_actions)
    rejected_titles = [r.title for r in rejected_actions]
    modified_pairs = _modified_pairs(decided_actions)
    fact_ids = [f.id for f in facts]
    usage_totals: list[dict] = []

    results = await asyncio.gather(
        *(
            _generate_one(
                audience=audience,
                input_text=input_text,
                rejected_titles=rejected_titles,
                modified_pairs=modified_pairs,
                fact_ids=fact_ids,
                registry=registry,
                tracer=tracer,
                usage_totals=usage_totals,
            )
            for audience in AUDIENCES
        ),
        return_exceptions=True,
    )
    errors = [r for r in results if isinstance(r, BaseException)]
    if errors:
        for extra in errors[1:]:
            logger.error("Additional report-generation failure: %s", extra)
        raise errors[0]
    clinician, patient = results  # type: ignore[misc]  # AUDIENCES order

    if usage_sink is not None:
        usage_sink["calls"] = usage_totals
        usage_sink["input_tokens"] = sum(u.get("input_tokens") or 0 for u in usage_totals)
        usage_sink["output_tokens"] = sum(u.get("output_tokens") or 0 for u in usage_totals)
        usage_sink["total_tokens"] = sum(u.get("total_tokens") or 0 for u in usage_totals)

    return ReportBundle(clinician=clinician, patient=patient)
