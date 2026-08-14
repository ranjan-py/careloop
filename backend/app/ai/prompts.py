"""Versioned prompt registry — Langfuse prompt management with in-code fallback.

Decision (verified for REAL against the local Langfuse v4 server + SDK
4.14.4): ``Langfuse.create_prompt`` / ``get_prompt`` round-trip works —
create a version on the server, fetch it back by the ``production`` label,
compile ``{{var}}`` templates. So Langfuse IS the prompt store (spec §10/§19):

- ``PROMPT_DEFAULTS`` below is the in-code source text; ``ensure_prompt``
  publishes it to Langfuse the first time a name is missing there.
- Runtime fetches by label with a short timeout; the fetched server version
  number becomes the recorded ``prompt_version`` (``name@vN``).
- If Langfuse is unconfigured/unreachable, the registry serves the in-code
  text with version ``name@fallback`` — degraded but honest, never a crash.

Every model call records the returned ``prompt_version`` (traces + rows).
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")

SAFETY_PREAMBLE = (
    "You are part of CareLoop, a SYNTHETIC clinical AI prototype — not for "
    "patient care. All data is synthetic. Never invent facts that are not "
    "supported by the transcript or the fact inventory."
)

_EXTRACTION_RULES = """
You receive (1) the patient's CURRENT FACT INVENTORY — each line is
`<fact_id> | <fact_type> | <subject>: <value> (<source>, <status>)` — and
(2) a RECENT FINALIZED TRANSCRIPT window of a primary-care visit.

Extract ONLY new clinical facts relevant to your condition focus that are
explicitly stated in the transcript. For each candidate fact return:
- fact_type: one of medication_status | observation | symptom | lab | condition | care_gap
- subject: short canonical subject (e.g. "lisinopril", "Blood pressure", "dizziness")
- value: concise value (e.g. "stopped", "150/95 mmHg", "resolved after stopping lisinopril")
- method: HOW the value was measured when the transcript says so, as a short
  snake_case token (e.g. a pharmacy kiosk / store machine reading -> "pharmacy_kiosk",
  home cuff -> "home_monitor"); null when no measurement method applies.
- confidence: 0.0-1.0 — how clearly the transcript states this.
- source_quote: the shortest transcript quote that supports the fact, verbatim.
- contradicts_fact_id: if the fact directly contradicts one INVENTORY line
  (e.g. patient stopped a medication the record lists as active), give THAT
  line's fact_id exactly as written; otherwise null.

Rules:
- Do NOT restate inventory facts the patient merely confirms unchanged.
- Do NOT extract the clinician's plans or orders as facts.
- Do NOT invent numbers, dates, or medications not present in the transcript.
- Prefer few high-quality facts over many marginal ones. Return an empty list
  when the window adds nothing for your condition focus.
"""

PROMPT_DEFAULTS: dict[str, str] = {
    "careloop_extract_hypertension": (
        SAFETY_PREAMBLE
        + "\nCondition focus: HYPERTENSION. You care about blood-pressure readings"
        " (with measurement method and source), antihypertensive medication status"
        " and adherence (started/stopped/dose changes), medication side effects"
        " (e.g. dizziness) and their timing, and home-monitoring capability"
        " (e.g. whether the patient owns a blood-pressure cuff -> care_gap).\n"
        + _EXTRACTION_RULES
    ),
    "careloop_extract_type2_diabetes": (
        SAFETY_PREAMBLE
        + "\nCondition focus: TYPE 2 DIABETES. You care about glucose/A1C values,"
        " diabetes medication status and adherence (e.g. metformin), hypoglycemia"
        " or medication side effects, and diet/monitoring behaviours stated in the"
        " transcript.\n"
        + _EXTRACTION_RULES
    ),
    "careloop_extract_ckd_risk": (
        SAFETY_PREAMBLE
        + "\nCondition focus: CHRONIC KIDNEY DISEASE RISK. You care about renal"
        " labs (creatinine, eGFR, potassium) and their recency, nephrotoxic or"
        " renally-dosed medication changes (ACE inhibitors like lisinopril),"
        " urinary symptoms, and hydration/volume symptoms such as dizziness or"
        " lightheadedness.\n"
        + _EXTRACTION_RULES
    ),
    "careloop_next_best_question": (
        SAFETY_PREAMBLE
        + """
You generate the LIVE "next best question / action" for the clinician DURING
the visit. You receive the patient's current fact inventory (including facts
extracted moments ago, marked NEW), the recent finalized transcript window,
and the currently active suggestions.

Suggest at most {{max_suggestions}} SMALL information-gathering or workflow
prompts — never aggressive treatment instructions. Good examples: confirm
symptom timing relative to a medication dose; flag a stale lab that needs a
fresh result; confirm whether any other medication of a class is being taken.

For each suggestion return:
- kind: "question" (something to ask now) | "info_gap" (missing/stale data)
  | "action" (small workflow step)
- text: one sentence, imperative, clinician-facing.
- rationale: one short sentence citing the specific facts that motivate it.
- dedup_key: a stable snake_case key for the underlying intent (e.g.
  "dizziness_timing_lisinopril", "potassium_recency") so the same intent is
  never shown twice this encounter.

Rules:
- Target REAL uncertainty or information gaps in the CURRENT facts — priority
  on the NEW facts just extracted.
- Never repeat an active suggestion or a dedup intent listed as already used.
- Return an empty list when nothing genuinely new is worth asking.
"""
    ),
    "careloop_care_plan": (
        SAFETY_PREAMBLE
        + """
You generate the STRUCTURED CARE PLAN at the end of a primary-care visit
(spec: about four demo actions). You receive the patient's FULL FACT
INVENTORY — each line is
`<fact_id> | <fact_type> | <subject>: <value> (<source>, <status>)` —
and the FINALIZED ENCOUNTER TRANSCRIPT.

Propose 3-5 actions. For each action return:
- category: one of lab | medication | monitoring | follow_up | referral | other
- title: short imperative clinician-facing title.
- description: 1-3 sentences describing the concrete workflow step.
- rationale: 1-2 sentences citing the SPECIFIC patient facts that motivate it.
- patient_facts_used: the fact_ids (exactly as written in the inventory) that
  ground this action. Use ONLY ids present in the inventory.
- risk_level: low | medium | high — the clinical risk of performing this
  process step (information gathering is usually low; anything touching
  medication decisions is medium or high).

HARD RULES — information-gathering and process-oriented actions ONLY:
- NEVER instruct a dose change, a new prescription, a specific drug to start
  or stop, or any milligram amount. A medication concern becomes a
  "medication review" action (category medication) that gathers information
  and tees up the clinician's decision — the clinician decides, not you.
- Every action must be executable as a small workflow step (order labs,
  schedule follow-up, set up monitoring, save a review task, generate
  patient instructions, create a referral, queue outreach).
- Ground every action in facts actually present in the inventory or
  transcript; never invent readings, dates, or medications.
- Prefer covering distinct needs (e.g. medication review, updated labs,
  BP monitoring, follow-up) over redundant variants of one need.
- When patient instructions/education would help, include ONE low-risk
  category "other" action for generating patient-friendly instructions.
"""
    ),
    "careloop_report": (
        SAFETY_PREAMBLE
        + """
You write the post-visit report for ONE audience: {{audience}}.

audience "clinician" -> a concise clinician summary: longitudinal context,
what happened this visit (new patient-reported information, conflicts with
the chart), and the FINAL decided plan. Professional register; may use
clinical terminology; short paragraphs or tight bullet points.

audience "patient" -> patient-friendly instructions: plain language at
roughly an 8th-grade reading level, no jargon (explain any necessary term),
warm and direct ("you/your"), and ONLY what the patient needs to do next.

You receive:
- PATIENT FACTS: chart facts and this visit's facts (with provenance).
- FINAL PLAN: the clinician's decided actions in their FINAL wording. Where
  an action was modified by the clinician, ONLY the final wording appears —
  render the plan EXACTLY from these final versions.
- EXCLUDED ACTIONS: actions the clinician REJECTED. Do NOT mention, imply,
  reference, or resurrect them in any form — not as done, planned, declined,
  or recommended. They are simply absent from your report.

Rules:
- The FINAL PLAN list is the complete plan. Never add, upgrade, or re-derive
  actions beyond it, and never reintroduce an excluded action.
- Never instruct dose changes or name doses the plan does not contain.
- Do not fabricate readings, dates, or results not present in the facts.
- Output: a short title and the report body text.

Output discipline (STRICT): the schema string fields contain ONLY the
finished document text. Never emit braces, brackets, JSON syntax, formatting
commentary, apologies, corrections, or any reference to JSON or to your own
output. Stop cleanly at the final sentence of the document — nothing after it.
"""
    ),
    "careloop_encounter_summary": (
        SAFETY_PREAMBLE
        + """
You write the SHORT PRE-DECISION ENCOUNTER SUMMARY generated the moment the
visit ends (spec: End Encounter step 4) — BEFORE the clinician has reviewed
or decided on any care-plan action.

You receive:
- PATIENT FACTS: chart facts plus this visit's extracted facts, each line
  `<fact_id> | <fact_type> | <subject>: <value> (<source>, <status>)`.
- FINALIZED ENCOUNTER TRANSCRIPT.

Write 3-6 tight sentences for the clinician covering:
- what the patient reported this visit (with measurement source when stated,
  e.g. a pharmacy kiosk reading),
- any conflict between patient-reported information and the chart (state both
  sides; the chart is disputed, not overwritten),
- what remains uncertain or unmeasured.

Rules:
- NO plan, NO recommendations, NO orders — decisions have not been made yet.
- Never invent readings, dates, medications, or facts not present in the
  inputs.
- Professional clinical register; plain prose, no headings or bullets.
- Output: a short title and the summary body.
"""
    ),
    "careloop_eval_judge": (
        SAFETY_PREAMBLE
        + """
You are an EVALUATION JUDGE for a prototype evaluator named: {{evaluator}}.
This is a PROTOTYPE EVALUATOR — not clinical validation.

Evaluation criteria for this evaluator:
{{criteria}}

You receive the evaluation inputs below (fact inventories, transcripts,
generated outputs). Judge ONLY against what is actually present in the
inputs — never assume unstated facts, and never reward or penalize style.

Return:
- passed: overall boolean verdict per the criteria.
- score: 0.0-1.0 — the fraction of evaluated items that satisfy the
  criteria (1.0 = fully clean/covered/supported).
- findings: one short string per concrete problem found (empty when clean),
  each quoting or naming the offending item.
- rationale: 1-3 sentences explaining the verdict, citing specifics.
"""
    ),
    "careloop_rejection_classifier": (
        SAFETY_PREAMBLE
        + """
You classify a clinician's free-text reason for REJECTING an AI-suggested
care-plan action into exactly one category:

- missing_patient_information: needed data about the patient is absent/stale.
- clinical_disagreement: the clinician disagrees with the clinical substance.
- patient_limitation: the patient cannot execute it (no equipment, cost,
  transport, cognition, language, adherence capacity).
- operational_workflow_limitation: the clinic/workflow cannot support it
  (scheduling, staffing, tooling).
- organization_protocol_constraint: local policy/protocol/formulary forbids
  or supersedes it.
- requires_supervision_escalation: needs specialist/supervisor sign-off or
  escalation first.
- other: none of the above fits.

Return the single best category. This is a SUGGESTION only — the clinician's
own selection remains authoritative.
"""
    ),
}


@dataclass(frozen=True)
class RegisteredPrompt:
    """A resolved prompt: text + the version identifier recorded everywhere."""

    name: str
    text: str
    version: str  # e.g. "careloop_extract_hypertension@v2" or "...@fallback"
    source: str  # "langfuse" | "fallback"

    def compile(self, **variables: Any) -> str:
        def _sub(match: re.Match[str]) -> str:
            key = match.group(1)
            return str(variables[key]) if key in variables else match.group(0)

        return _VAR_RE.sub(_sub, self.text)


class PromptRegistry:
    """Fetch-by-label registry over Langfuse with an in-code fallback cache."""

    def __init__(
        self,
        *,
        client: Any = None,
        defaults: dict[str, str] | None = None,
        label: str = "production",
        cache_ttl_seconds: float = 300.0,
        fetch_timeout_seconds: float = 5.0,
    ) -> None:
        self._explicit_client = client
        self._defaults = defaults if defaults is not None else PROMPT_DEFAULTS
        self._label = label
        self._cache_ttl = cache_ttl_seconds
        self._fetch_timeout = fetch_timeout_seconds
        self._cache: dict[str, tuple[float, RegisteredPrompt]] = {}
        self._lock = threading.Lock()

    # -- client resolution -------------------------------------------------

    def _client(self) -> Any:
        if self._explicit_client is not None:
            return self._explicit_client
        from app.observability.tracing import get_langfuse_client

        return get_langfuse_client()

    # -- core API ----------------------------------------------------------

    def _fallback(self, name: str) -> RegisteredPrompt:
        if name not in self._defaults:
            raise KeyError(f"Unknown prompt name: {name!r}")
        return RegisteredPrompt(
            name=name, text=self._defaults[name], version=f"{name}@fallback", source="fallback"
        )

    def get(self, name: str) -> RegisteredPrompt:
        """Resolve a prompt (blocking; short-timeout HTTP on cache miss)."""
        if name not in self._defaults:
            raise KeyError(f"Unknown prompt name: {name!r}")
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(name)
            if cached and cached[0] > now:
                return cached[1]

        client = self._client()
        if client is None:
            return self._fallback(name)

        resolved: RegisteredPrompt
        try:
            fetched = self._fetch_or_create(client, name)
            resolved = RegisteredPrompt(
                name=name,
                text=fetched.prompt,
                version=f"{name}@v{fetched.version}",
                source="langfuse",
            )
        except Exception as exc:  # noqa: BLE001 — degraded, never crash
            logger.warning(
                "Langfuse prompt %s unavailable (%s: %s) — using in-code fallback.",
                name, type(exc).__name__, exc,
            )
            resolved = self._fallback(name)

        with self._lock:
            self._cache[name] = (now + self._cache_ttl, resolved)
        return resolved

    async def aget(self, name: str) -> RegisteredPrompt:
        """Async wrapper — the blocking fetch runs off the event loop."""
        return await asyncio.to_thread(self.get, name)

    def _fetch_or_create(self, client: Any, name: str) -> Any:
        try:
            return client.get_prompt(
                name,
                label=self._label,
                type="text",
                max_retries=1,
                fetch_timeout_seconds=int(self._fetch_timeout),
            )
        except Exception:  # noqa: BLE001 — not found (or transient): try to publish
            logger.info("Prompt %s not fetchable from Langfuse — publishing in-code default.", name)
            client.create_prompt(
                name=name, prompt=self._defaults[name], labels=[self._label], type="text"
            )
            return client.get_prompt(
                name,
                label=self._label,
                type="text",
                max_retries=1,
                fetch_timeout_seconds=int(self._fetch_timeout),
            )

    def sync_all(self) -> dict[str, str]:
        """Ensure every default exists in Langfuse; returns name -> version."""
        return {name: self.get(name).version for name in self._defaults}

    def publish(self, name: str) -> str:
        """Force-publish the in-code default as a NEW Langfuse version.

        get_prompt serves by label, so edits to PROMPT_DEFAULTS are invisible
        until published — run `python -m app.ai.prompts publish <name>` after
        changing a default."""
        if name not in self._defaults:
            raise KeyError(f"Unknown prompt name: {name!r}")
        client = self._client()
        if client is None:
            raise RuntimeError("Langfuse client unavailable — cannot publish")
        client.create_prompt(
            name=name, prompt=self._defaults[name], labels=[self._label], type="text"
        )
        self.clear_cache()
        return self.get(name).version

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()


_default_registry: PromptRegistry | None = None


def get_registry() -> PromptRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = PromptRegistry()
    return _default_registry


if __name__ == "__main__":  # `python -m app.ai.prompts publish <name> [...]`
    import sys

    if len(sys.argv) >= 3 and sys.argv[1] == "publish":
        registry = get_registry()
        for prompt_name in sys.argv[2:]:
            print(f"{prompt_name} -> {registry.publish(prompt_name)}")
    else:
        print("usage: python -m app.ai.prompts publish <name> [...]", file=sys.stderr)
        sys.exit(2)
