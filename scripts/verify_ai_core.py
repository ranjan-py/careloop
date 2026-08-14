#!/usr/bin/env python
"""REAL verification of the CareLoop AI loop core (spec §2.3 — nothing is
successful until tested against the real integrations).

Runs the §9 pipeline (per-condition extraction subagents -> deterministic
conflict detection -> next-best-question engine) against the scripted John
Miller encounter (docs/encounter_script.md) with:

- REAL OpenAI calls through app.ai.client.call_model (key from env),
- REAL Langfuse v4 prompt management (fetch-by-label; publish-on-miss) and
  REAL encounter-rooted tracing (trace_context on a pre-minted trace id),
  verified by reading spans back via GET /api/public/v2/observations,
- an IN-MEMORY persistence layer (--no-db): app-postgres exposes no host
  port, so DB writes cannot run from the host; persistence is covered by
  unit tests + the dockerized stack instead.

The in-memory store is seeded with the chart facts parsed from
data/scenario_overlay/john_miller_bundle.json via app.context.fhir_ingest —
the same helpers the real seed uses.

Usage (from careloop/backend, env must carry OPENAI_API_KEY + LANGFUSE_*):
    ./.venv/bin/python ../scripts/verify_ai_core.py --no-db

Prints PASS/FAIL per assertion + latency + token/cost. Exit 0 iff all PASS.
Secrets are never echoed.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import re
import statistics
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
from app.ai.extraction import FactView  # noqa: E402
from app.ai.pipeline import EncounterPipeline, InMemoryStore, PipelineConfig  # noqa: E402
from app.ai.prompts import PROMPT_DEFAULTS, PromptRegistry  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.context import fhir_ingest  # noqa: E402
from app.observability.tracing import EncounterTracer  # noqa: E402
from app.schemas.core import TranscriptSegment  # noqa: E402

TURN_RE = re.compile(r"^\*\*(Doctor|Patient):\*\*\s*(.+)$")
ENCOUNTER_ID = "enc_verify_ai_core"

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str) -> None:
    results.append((name, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")


def parse_script_turns(path: Path) -> list[TranscriptSegment]:
    turns: list[TranscriptSegment] = []
    ts = 0.0
    for line in path.read_text(encoding="utf-8").splitlines():
        match = TURN_RE.match(line.strip())
        if match:
            speaker = match.group(1).lower()
            text = match.group(2).strip()
            turns.append(TranscriptSegment(id=f"seg_{len(turns):03d}", speaker=speaker, text=text, ts=ts))
            ts += 5.0
    return turns


def seed_store_from_bundle(store: InMemoryStore) -> tuple[str, int]:
    bundle_path = REPO_ROOT / "data" / "scenario_overlay" / "john_miller_bundle.json"
    chart = fhir_ingest.parse_bundle(fhir_ingest.load_bundle(bundle_path))
    fact_dicts = fhir_ingest.build_chart_facts(chart, ingested_at=datetime.now(timezone.utc))
    for f in fact_dicts:
        store.seed_fact(
            f["patient_id"],
            FactView(
                id=f["id"],
                fact_type=f["fact_type"],
                subject=f["subject"],
                value=f["value"],
                source_type=f["source_type"],
                source_class=f["source_class"],
                verification_status=f["verification_status"],
                method=f["method"],
                encounter_id=f["encounter_id"],
                conflicts_with=f["conflicts_with"],
            ),
        )
    return chart.patient.id, len(fact_dicts)


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
        self.calls.append({
            "task": task,
            "latency_s": time.monotonic() - started,
            "prompt_version": prompt_version,
            "usage": dict(sink),
        })
        return parsed


async def poll_observations(settings, trace_id: str, timeout_s: float = 90.0) -> list[dict]:
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
                names = {r.get("name") for r in rows}
                if "extract_patient_facts" in names and "extract_hypertension" in names:
                    return rows
            except Exception as exc:  # noqa: BLE001
                print(f"    (langfuse poll error: {type(exc).__name__}: {exc})")
            await asyncio.sleep(3)
    return rows


async def main(args: argparse.Namespace) -> int:
    settings = get_settings()
    missing = [
        name for name, value in (
            ("OPENAI_API_KEY", settings.openai_api_key),
            ("LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key),
            ("LANGFUSE_SECRET_KEY", settings.langfuse_secret_key),
        ) if not value
    ]
    if missing:
        print(f"BLOCKED: {', '.join(missing)} not set — this verifier requires REAL keys.")
        return 3
    if not args.no_db:
        print("NOTE: DB mode unavailable from the host (app-postgres exposes no host "
              "port) — running --no-db (in-memory persistence) mode.")

    print(f"model={settings.openai_model} langfuse={settings.langfuse_host} (keys present, not echoed)")

    # -- Prompt management: real Langfuse round-trip ------------------------
    registry = PromptRegistry()
    prompt_versions = registry.sync_all()
    langfuse_backed = [v for v in prompt_versions.values() if "@v" in v]
    check(
        "prompt_management_langfuse",
        len(langfuse_backed) == len(PROMPT_DEFAULTS),
        f"{len(langfuse_backed)}/{len(PROMPT_DEFAULTS)} prompts served from Langfuse: "
        + ", ".join(sorted(prompt_versions.values())),
    )

    # -- Seed store + pipeline ---------------------------------------------
    store = InMemoryStore()
    patient_id, n_chart_facts = seed_store_from_bundle(store)
    check("chart_seed", n_chart_facts == 11,
          f"{n_chart_facts} chart facts from john_miller_bundle.json (expect 11), patient={patient_id}")

    turns = parse_script_turns(REPO_ROOT / "docs" / "encounter_script.md")
    check("script_parsed", len(turns) >= 15, f"{len(turns)} scripted turns parsed")

    trace_id = uuid.uuid4().hex
    tracer = EncounterTracer(trace_id)
    meter = CallMeter()
    ai_client.call_model = meter  # real calls, metered

    fact_events: list[tuple[str, str]] = []
    sug_events: list[str] = []

    async def on_fact(fact, change):
        fact_events.append((fact.subject, change))
        print(f"    state.fact {change}: {fact.fact_type} {fact.subject} = {fact.value!r}"
              f" (method={fact.method}, conflicts_with={fact.conflicts_with})")

    async def on_suggestion(s):
        sug_events.append(s.text)
        print(f"    suggestion.active [{s.kind}] {s.text}")

    async def on_remove(sid):
        print(f"    suggestion.remove {sid}")

    pipeline = EncounterPipeline(
        encounter_id=ENCOUNTER_ID,
        patient_id=patient_id,
        store=store,
        tracer=tracer,
        registry=registry,
        # Real §9 thresholds; debounce shortened so the scripted feed (which
        # arrives without real-time pacing) completes in demo time.
        config=PipelineConfig(debounce_seconds=0.3),
        on_fact=on_fact,
        on_suggestion=on_suggestion,
        on_suggestion_remove=on_remove,
    )

    print(f"\nFeeding {len(turns)} finalized turns through the §9 trigger pipeline "
          f"(trace_id={trace_id}) ...")
    run_started = time.monotonic()
    for turn in turns:
        print(f"  >> {turn.speaker}: {turn.text[:76]}{'...' if len(turn.text) > 76 else ''}")
        await pipeline.feed_final_segment(turn)
        await pipeline.wait_idle()
    await pipeline.flush()
    await pipeline.close()
    run_seconds = time.monotonic() - run_started
    ai_client.call_model = meter.original

    # -- Assertions ---------------------------------------------------------
    print("\nResults:")
    if pipeline.last_error:
        print(f"  (last pipeline error surfaced: {pipeline.last_error})")

    patient_facts = [v for v in store.facts.values() if v.source_type == "patient_report"]

    bp = [f for f in patient_facts
          if "pressure" in f.subject.lower() or re.search(r"\b1[45]\d\s*/\s*9", f.value)]
    bp_kiosk = [f for f in bp if f.method and "kiosk" in f.method.lower()]
    check("kiosk_bp_fact", bool(bp_kiosk),
          (f"{bp_kiosk[0].subject}={bp_kiosk[0].value!r} method={bp_kiosk[0].method!r}"
           if bp_kiosk else f"no patient-reported BP fact with kiosk method (bp facts: "
           f"{[(f.subject, f.value, f.method) for f in bp]})"))

    lis = [f for f in patient_facts
           if f.fact_type == "medication_status" and "lisinopril" in f.subject.lower()
           and "stop" in f.value.lower()]
    lis_ok = bool(lis) and lis[0].conflicts_with == "med-lisinopril" \
        and lis[0].source_class == "patient_report" and lis[0].verification_status == "unverified"
    check("lisinopril_conflict_fact", lis_ok,
          (f"value={lis[0].value!r} conflicts_with={lis[0].conflicts_with} "
           f"source_class={lis[0].source_class} status={lis[0].verification_status}"
           if lis else "no lisinopril-stopped patient_report fact created"))

    ehr_lis = store.facts.get("med-lisinopril")
    check("ehr_fact_disputed_not_overwritten",
          ehr_lis is not None and ehr_lis.verification_status == "disputed"
          and ehr_lis.value == "active",
          f"med-lisinopril value={ehr_lis.value!r} status={ehr_lis.verification_status!r}"
          if ehr_lis else "EHR lisinopril fact missing")

    sug_rows = list(store.suggestions.values())
    topic_re = re.compile(r"dizz|potassium|blood.?pressure|\bbp\b|monitor|cuff|medicat|antihypertensive",
                          re.IGNORECASE)
    on_topic = [s for s in sug_rows if topic_re.search(s.text + " " + s.rationale)]
    check("suggestion_generated", bool(on_topic),
          (f"{len(sug_rows)} suggestion(s); on-topic e.g. "
           f"[{on_topic[0].kind}] {on_topic[0].text!r} (dedup_key={on_topic[0].dedup_key})"
           if on_topic else f"suggestions: {[(s.kind, s.text) for s in sug_rows]}"))

    check("all_model_outputs_schema_valid",
          len(meter.calls) > 0 and not meter.failures,
          f"{len(meter.calls)} real model calls, {len(meter.failures)} failures"
          + (f" — {meter.failures[:3]}" if meter.failures else ""))

    fired = [e for e in store.trigger_log if e.decision == "fired" and e.stage == "extraction"]
    skipped = [e for e in store.trigger_log if e.decision == "skipped"]
    check("trigger_decisions_logged", bool(fired) and bool(skipped),
          f"{len(store.trigger_log)} decisions logged ({len(fired)} extraction fired, "
          f"{len(skipped)} skipped, reasons e.g. {sorted({e.reason.split(' ')[0] for e in skipped})})")

    # -- Langfuse trace round-trip ------------------------------------------
    print(f"\nPolling Langfuse for trace {trace_id} ...")
    tracer.flush()
    obs = await poll_observations(settings, trace_id)
    names = sorted({o.get("name") for o in obs})
    have_extract = "extract_patient_facts" in names
    have_conditions = all(
        f"extract_{k}" in names for k in ("hypertension", "type2_diabetes", "ckd_risk")
    )
    child_ok = any(
        o.get("name") == "extract_hypertension" and o.get("parentObservationId") for o in obs
    )
    check("langfuse_trace_extract_spans",
          have_extract and have_conditions and child_ok,
          f"{len(obs)} observations on trace; names={names}; "
          f"per-condition spans nested={child_ok}")
    if "generate_next_best_action" in names:
        print("    (generate_next_best_action span present)")
    if "context_assembly" in names:
        print("    (context_assembly span present)")

    # -- Latency / token / cost ---------------------------------------------
    latencies = [c["latency_s"] for c in meter.calls]
    by_task: dict[str, list[float]] = {}
    for c in meter.calls:
        by_task.setdefault(c["task"], []).append(c["latency_s"])
    tokens_in = sum(c["usage"].get("input_tokens") or 0 for c in meter.calls)
    tokens_out = sum(c["usage"].get("output_tokens") or 0 for c in meter.calls)
    cost = sum(float(o.get("calculatedTotalCost") or 0) for o in obs)

    print(f"\nLatency: total run {run_seconds:.1f}s, {len(meter.calls)} model calls, "
          f"median {statistics.median(latencies):.2f}s, max {max(latencies):.2f}s" if latencies
          else "\nLatency: no model calls recorded")
    for task, ls in sorted(by_task.items()):
        print(f"  {task}: n={len(ls)} median={statistics.median(ls):.2f}s max={max(ls):.2f}s")
    print(f"Tokens: input={tokens_in} output={tokens_out} total={tokens_in + tokens_out}")
    print(f"Cost (from Langfuse calculatedTotalCost over this trace): "
          f"${cost:.4f}" if cost else "Cost: not reported by Langfuse for this model")

    failures = [name for name, passed, _ in results if not passed]
    print(f"\n{'ALL CHECKS PASSED' if not failures else 'FAILED: ' + ', '.join(failures)} "
          f"({sum(1 for _, p, _ in results if p)}/{len(results)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--no-db", action="store_true",
                        help="in-memory persistence (the only mode runnable from the host)")
    sys.exit(asyncio.run(main(parser.parse_args())))
