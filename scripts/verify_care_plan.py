#!/usr/bin/env python
"""REAL verification of the care-plan generation layer (spec §2.3 — nothing
is successful until tested against the real integrations).

Runs spec §11 retrieval + §12/§13 care-plan generation + §14 rejection
classification + §16 reports with:

- REAL OpenAI calls through app.ai.client.call_model (key from env),
- REAL Langfuse v4 prompt management + encounter-rooted tracing, verified by
  reading spans back via GET /api/public/v2/observations,
- IN-MEMORY data (no DB needed from the host): chart facts parsed from
  data/scenario_overlay/john_miller_bundle.json via app.context.fhir_ingest,
  plus the encounter facts the full loop produces (kiosk BP, lisinopril-
  stopped conflict, dizziness timing, no-home-cuff care gap) constructed in
  code; transcript from docs/encounter_script.md.

Flow: generate_care_plan -> structural/tier/evidence assertions -> simulated
clinician decisions (approve labs, MODIFY follow-up, REJECT home BP
monitoring) -> generate_reports -> leakage assertions -> classifier check ->
Langfuse span round-trip. Prints PASS/FAIL per assertion + latency + tokens.
Exit 0 iff all PASS. Secrets are never echoed.

Usage (from careloop/backend, env must carry OPENAI_API_KEY + LANGFUSE_*):
    ./.venv/bin/python ../scripts/verify_care_plan.py
"""

from __future__ import annotations

import asyncio
import base64
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import httpx  # noqa: E402

from app.ai import client as ai_client  # noqa: E402
from app.ai import retrieval  # noqa: E402
from app.ai.care_plan import (  # noqa: E402
    CandidateAction,
    find_dose_change_language,
    generate_care_plan,
)
from app.ai.classifier import suggest_rejection_category  # noqa: E402
from app.ai.extraction import FactView  # noqa: E402
from app.ai.prompts import PromptRegistry  # noqa: E402
from app.ai.reports import (  # noqa: E402
    DecidedAction,
    RejectedAction,
    generate_reports,
    leakage_check,
)
from app.config import get_settings  # noqa: E402
from app.context import fhir_ingest  # noqa: E402
from app.observability.tracing import EncounterTracer  # noqa: E402
from app.schemas.core import RejectionCategory  # noqa: E402

TURN_RE = re.compile(r"^\*\*(Doctor|Patient):\*\*\s*(.+)$")
ENCOUNTER_ID = "enc_verify_care_plan"

NEW_PROMPTS = ("careloop_care_plan", "careloop_report", "careloop_rejection_classifier")

#: Clear dose-change directives (hard FAIL); the broader detector in
#: app.ai.care_plan flags borderline prescribe/start/switch language (report only).
HARD_DOSE_RE = re.compile(
    r"\b(increas|decreas|titrat|doubl|halv|raise|reduc)\w*\b"
    r"[^.;\n]{0,60}?\b\d+(?:\.\d+)?\s*(?:mg|milligrams?)\b",
    re.IGNORECASE,
)

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str) -> None:
    results.append((name, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")


def note(text: str) -> None:
    print(f"    (note) {text}")


def parse_transcript(path: Path) -> str:
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        match = TURN_RE.match(line.strip())
        if match:
            lines.append(f"{match.group(1)}: {match.group(2).strip()}")
    return "\n".join(lines)


def chart_facts_from_bundle() -> tuple[str, list[FactView]]:
    bundle_path = REPO_ROOT / "data" / "scenario_overlay" / "john_miller_bundle.json"
    chart = fhir_ingest.parse_bundle(fhir_ingest.load_bundle(bundle_path))
    fact_dicts = fhir_ingest.build_chart_facts(chart, ingested_at=datetime.now(timezone.utc))
    views = [
        FactView(
            id=f["id"], fact_type=f["fact_type"], subject=f["subject"], value=f["value"],
            source_type=f["source_type"], source_class=f["source_class"],
            verification_status=f["verification_status"], method=f["method"],
            encounter_id=f["encounter_id"], conflicts_with=f["conflicts_with"],
        )
        for f in fact_dicts
    ]
    return chart.patient.id, views


def encounter_facts() -> list[FactView]:
    """The facts the §9 live loop produces for this encounter (mirrors the
    scripted beats in docs/encounter_script.md — constructed, not extracted,
    because this verifier isolates the §12-§16 layer)."""
    common = dict(source_type="patient_report", source_class="patient_report",
                  verification_status="unverified", encounter_id=ENCOUNTER_ID)
    return [
        FactView(id="fact_enc_bp_kiosk", fact_type="observation", subject="Blood pressure",
                 value="150/95 mmHg", method="pharmacy_kiosk", **common),
        FactView(id="fact_enc_lisinopril_stopped", fact_type="medication_status",
                 subject="lisinopril", value="stopped about two weeks ago due to dizziness",
                 conflicts_with="med-lisinopril", **common),
        FactView(id="fact_enc_dizziness", fact_type="symptom", subject="dizziness",
                 value="occurred within two hours of lisinopril dose; resolved after stopping",
                 **common),
        FactView(id="fact_enc_no_cuff", fact_type="care_gap", subject="Home BP monitor",
                 value="patient does not own a home blood pressure cuff; checks at a "
                       "pharmacy kiosk about monthly", **common),
    ]


class CallMeter:
    """Wraps the REAL call_model — counts calls, latency, usage, failures."""

    def __init__(self) -> None:
        self.original = ai_client.call_model
        self.calls: list[dict] = []
        self.failures: list[str] = []

    async def __call__(self, *, task, instructions, input_text, output_schema,
                       model=None, prompt_version=None, usage_sink=None):
        sink = usage_sink if usage_sink is not None else {}
        started = time.monotonic()
        try:
            parsed = await self.original(
                task=task, instructions=instructions, input_text=input_text,
                output_schema=output_schema, model=model,
                prompt_version=prompt_version, usage_sink=sink,
            )
        except Exception as exc:
            self.failures.append(f"{task}: {type(exc).__name__}: {exc}")
            raise
        self.calls.append({"task": task, "latency_s": time.monotonic() - started,
                           "usage": dict(sink)})
        return parsed


async def poll_observations(settings, trace_id: str, required: set[str],
                            timeout_s: float = 90.0) -> list[dict]:
    auth = base64.b64encode(
        f"{settings.langfuse_public_key}:{settings.langfuse_secret_key}".encode()
    ).decode()
    deadline = time.monotonic() + timeout_s
    rows: list[dict] = []
    async with httpx.AsyncClient(timeout=10) as http:
        while time.monotonic() < deadline:
            try:
                resp = await http.get(
                    f"{settings.langfuse_host}/api/public/v2/observations",
                    params={"traceId": trace_id, "limit": 100},
                    headers={"Authorization": f"Basic {auth}"},
                )
                rows = resp.json().get("data", [])
                if required <= {r.get("name") for r in rows}:
                    return rows
            except Exception as exc:  # noqa: BLE001
                print(f"    (langfuse poll error: {type(exc).__name__}: {exc})")
            await asyncio.sleep(3)
    return rows


def norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


async def main() -> int:
    settings = get_settings()
    missing = [name for name, value in (
        ("OPENAI_API_KEY", settings.openai_api_key),
        ("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key),
        ("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key),
    ) if not value]
    if missing:
        print(f"BLOCKED: {', '.join(missing)} not set — this verifier requires REAL keys.")
        return 3
    print(f"model={settings.openai_model} eval_model={settings.openai_eval_model} "
          f"langfuse={settings.langfuse_host} (keys present, not echoed)")

    # -- Prompt management: the three new prompts round-trip through Langfuse --
    registry = PromptRegistry()
    versions = {name: registry.get(name).version for name in NEW_PROMPTS}
    check("prompts_langfuse_backed",
          all("@v" in v for v in versions.values()),
          ", ".join(sorted(versions.values())))

    # -- Retrieval sanity (pure, real corpus) -------------------------------
    hits = retrieval.retrieve("patient checks blood pressure at a pharmacy kiosk, no home monitor", k=3)
    check("retrieval_kiosk_snippet", bool(hits) and hits[0].id == "ev-022",
          f"top hits: {[(h.id, round(h.score, 2)) for h in hits]}")

    # -- Assemble in-memory encounter state ---------------------------------
    patient_id, facts = chart_facts_from_bundle()
    for f in facts:
        if f.id == "med-lisinopril":
            f.verification_status = "disputed"  # the §8B conflict outcome
    facts += encounter_facts()
    known_ids = {f.id for f in facts}
    check("state_assembled", len(facts) == 15 and "fact_enc_bp_kiosk" in known_ids,
          f"{len(facts)} facts ({len(known_ids)} ids), patient={patient_id}")

    transcript = parse_transcript(REPO_ROOT / "docs" / "encounter_script.md")
    check("transcript_parsed", transcript.count("\n") >= 14, f"{transcript.count(chr(10)) + 1} turns")

    trace_id = uuid.uuid4().hex
    tracer = EncounterTracer(trace_id)
    meter = CallMeter()
    ai_client.call_model = meter

    # -- §12/§13: generate the care plan (ONE real model call) --------------
    print(f"\nGenerating care plan (trace_id={trace_id}) ...")
    started = time.monotonic()
    actions = await generate_care_plan(
        ENCOUNTER_ID, patient_id, facts, transcript, trace_id,
        registry=registry, tracer=tracer,
    )
    plan_seconds = time.monotonic() - started
    for a in actions:
        print(f"    [{a.category:<10}] ({a.risk_level}/{a.permission}) {a.title}")
        print(f"        facts={a.patient_facts_used} evidence={a.evidence_refs}")

    check("action_count_3_to_5", 3 <= len(actions) <= 5, f"{len(actions)} actions")

    cats = {a.category for a in actions}
    core = {"medication", "lab", "monitoring", "follow_up"}
    missing_core = sorted(core - cats)
    check("categories_core_coverage", len(core & cats) >= 3,
          f"categories={sorted(cats)}"
          + (f" — missing core: {missing_core} (honest report)" if missing_core else " — full core coverage"))

    bad_ids = {fid for a in actions for fid in a.patient_facts_used} - known_ids
    check("fact_ids_all_known", not bad_ids,
          "every patient_facts_used id is in the inventory" if not bad_ids else f"unknown ids: {bad_ids}")
    grounded = [a for a in actions if a.patient_facts_used]
    check("actions_grounded_in_facts", len(grounded) == len(actions),
          f"{len(grounded)}/{len(actions)} actions cite at least one fact id")

    corpus_ids = {s.id for s in retrieval.get_index().snippets}
    with_ev = [a for a in actions if a.evidence_refs]
    refs_valid = all(set(a.evidence_refs) <= corpus_ids and len(a.evidence_refs) <= 2 for a in actions)
    check("evidence_refs_retrieval_produced", bool(with_ev) and refs_valid,
          f"{len(with_ev)}/{len(actions)} actions carry evidence; all refs real corpus ids={refs_valid}")

    auto = [a for a in actions if a.permission == "auto_demo"]
    check("exactly_one_auto_demo", len(auto) == 1,
          f"auto_demo: {[(a.category, a.title) for a in auto]}")
    meds = [a for a in actions if a.category == "medication"]
    check("medication_requires_decision",
          all(a.permission == "required_clinician_decision" for a in meds),
          f"{len(meds)} medication action(s) all required_clinician_decision"
          if meds else "no medication action generated (vacuous — see category coverage)")

    hard_dose = [(a.title, HARD_DOSE_RE.search(f"{a.title} {a.description} {a.rationale}").group(0))
                 for a in actions
                 if HARD_DOSE_RE.search(f"{a.title} {a.description} {a.rationale}")]
    check("no_dose_change_instructions", not hard_dose,
          "no increase/decrease-with-mg language in any action" if not hard_dose
          else f"dose-change language: {hard_dose}")
    for a in actions:
        borderline = find_dose_change_language(f"{a.title} {a.description} {a.rationale}")
        if borderline and not any(t == a.title for t, _ in hard_dose):
            note(f"borderline dose language (reported, not failed) in {a.title!r}: {borderline!r}")

    # -- §14: simulate clinician decisions ----------------------------------
    def pick(category: str, keywords: str = "") -> CandidateAction | None:
        by_cat = [a for a in actions if a.category == category]
        if by_cat:
            return by_cat[0]
        kw = re.compile(keywords, re.IGNORECASE) if keywords else None
        return next((a for a in actions if kw and kw.search(a.title)), None)

    lab_action = pick("lab", "lab|potassium|kidney")
    follow_action = pick("follow_up", "follow")
    monitor_action = pick("monitoring", "monitor|home bp|blood pressure")
    print("\nSimulated decisions: "
          f"approve={getattr(lab_action, 'title', None)!r}, "
          f"modify={getattr(follow_action, 'title', None)!r}, "
          f"reject={getattr(monitor_action, 'title', None)!r}")
    check("decision_targets_found",
          all(x is not None for x in (lab_action, follow_action, monitor_action))
          and len({id(x) for x in (lab_action, follow_action, monitor_action)}) == 3,
          "found distinct lab/follow-up/monitoring actions to decide on")
    if not all(x is not None for x in (lab_action, follow_action, monitor_action)):
        print("\nCannot run the report-fidelity flow without distinct decision targets — aborting.")
        ai_client.call_model = meter.original
        return 1

    MOD_FINAL_TITLE = "Schedule follow-up visit in ten days"
    MOD_FINAL_DESC = ("Book a return visit in ten days to review the updated lab results "
                      "and the new blood pressure readings before any medication decision.")
    decided: list[DecidedAction] = []
    for a in actions:
        if a is monitor_action:
            continue  # rejected below
        if a is follow_action:
            decided.append(DecidedAction(
                category=a.category, title=MOD_FINAL_TITLE, description=MOD_FINAL_DESC,
                status="modified", original_title=a.title, original_description=a.description))
        else:
            decided.append(DecidedAction(
                category=a.category, title=a.title, description=a.description, status="approved"))
    rejected = [RejectedAction(title=monitor_action.title, category=monitor_action.category)]

    # -- §16: reports (consolidated audience-parameterized family) ----------
    print("\nGenerating reports ...")
    started = time.monotonic()
    bundle = await generate_reports(
        ENCOUNTER_ID, patient_id, facts, decided, rejected, trace_id,
        registry=registry, tracer=tracer,
    )
    report_seconds = time.monotonic() - started
    print(f"    clinician_summary: {bundle.clinician_summary[:160]!r}...")
    print(f"    patient_instructions: {bundle.patient_instructions[:160]!r}...")
    if bundle.clinician.retried or bundle.patient.retried:
        note(f"leakage retry used (clinician={bundle.clinician.retried}, patient={bundle.patient.retried})")

    mod_pairs = [(follow_action.title, MOD_FINAL_TITLE),
                 (follow_action.description, MOD_FINAL_DESC)]
    for label, text in (("clinician_summary", bundle.clinician_summary),
                        ("patient_instructions", bundle.patient_instructions)):
        violations = leakage_check(text, [monitor_action.title], mod_pairs)
        reject_absent = norm(monitor_action.title) not in norm(text)
        check(f"{label}_no_rejected_action", not violations and reject_absent,
              "clean (rejected title + superseded originals absent)" if not violations and reject_absent
              else f"violations={violations}")
    check("modified_final_text_rendered",
          "ten days" in norm(bundle.clinician_summary)
          and "ten days" in norm(bundle.patient_instructions),
          "clinician-edited FINAL wording ('ten days') present in both reports"
          if "ten days" in norm(bundle.clinician_summary) else
          f"final wording missing — clinician={'ten days' in norm(bundle.clinician_summary)}, "
          f"patient={'ten days' in norm(bundle.patient_instructions)}")
    orig_in_reports = norm(follow_action.title) in norm(bundle.clinician_summary + bundle.patient_instructions)
    check("modified_original_absent", not orig_in_reports or norm(follow_action.title) in norm(MOD_FINAL_TITLE),
          f"superseded original follow-up title absent (original={follow_action.title!r})")

    # -- §14: rejection-category classifier (real OPENAI_EVAL_MODEL call) ---
    suggestion = await suggest_rejection_category(
        "Patient has no home BP monitor, checks at pharmacy kiosk occasionally — "
        "cannot do a twice-daily home protocol.",
        action_title=monitor_action.title, trace_id=trace_id,
        registry=registry, tracer=tracer,
    )
    check("classifier_patient_limitation",
          suggestion is RejectionCategory.PATIENT_LIMITATION,
          f"suggested category: {suggestion.value} (clinician's choice stays authoritative)")

    ai_client.call_model = meter.original

    # -- Langfuse span round-trip -------------------------------------------
    print(f"\nPolling Langfuse for trace {trace_id} ...")
    tracer.flush()
    required = {"generate_care_plan", "context_assembly", "generate_clinician_summary",
                "generate_patient_instructions", "classify_override"}
    obs = await poll_observations(settings, trace_id, required)
    names = sorted({o.get("name") for o in obs})
    nested = any(o.get("name") == "context_assembly" and o.get("parentObservationId") for o in obs)
    check("langfuse_spans_on_trace", required <= set(names) and nested,
          f"{len(obs)} observations; names={names}; context_assembly nested={nested}")

    # -- Latency / tokens ----------------------------------------------------
    tokens_in = sum(c["usage"].get("input_tokens") or 0 for c in meter.calls)
    tokens_out = sum(c["usage"].get("output_tokens") or 0 for c in meter.calls)
    cost = sum(float(o.get("calculatedTotalCost") or 0) for o in obs)
    print(f"\nLatency: care_plan={plan_seconds:.1f}s reports={report_seconds:.1f}s "
          f"({len(meter.calls)} model calls, {len(meter.failures)} failures)")
    for c in meter.calls:
        print(f"  {c['task']}: {c['latency_s']:.2f}s "
              f"in={c['usage'].get('input_tokens')} out={c['usage'].get('output_tokens')}")
    print(f"Tokens: input={tokens_in} output={tokens_out} total={tokens_in + tokens_out}")
    print(f"Cost (Langfuse calculatedTotalCost over this trace): ${cost:.4f}"
          if cost else "Cost: not reported by Langfuse for this model")

    failures = [name for name, passed, _ in results if not passed]
    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILED: ' + ', '.join(failures)} "
          f"({sum(1 for _, p, _ in results if p)}/{len(results)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
