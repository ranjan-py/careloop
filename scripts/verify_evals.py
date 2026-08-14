#!/usr/bin/env python3
"""REAL verification of the evaluation + observability layer (spec §2.3).

Flow (against the LIVE stack, real OpenAI + Langfuse + Deepgram replay):
 1. `docker compose up -d --build backend` — the eval code must be IN the
    image (verified fresh build), then wait for health.
 2. `docker compose exec -T backend python -m app.context.reset_runtime`.
 3. Drive a REAL mini flow: scripts/verify_full_loop.py (replay -> extraction
    -> suggestions; prints the encounter id) then scripts/verify_hitl.py
    (end -> care plan -> approve/modify/reject -> finalize -> reports).
 4. Run the ONLINE evaluators in-container:
    `python -m app.evals.online <encounter_id>` (real OPENAI_EVAL_MODEL judges).
 5. Assert via the API: GET /api/evals/{id} (>=6 evaluators, deterministic
    gates green, launch criteria >=4 green), GET /api/ops/trace-summary/{id},
    GET /api/analytics/feedback (current-session decisions merged).
 6. Offline suite on 2 cases with the REAL model in-container:
    `python -m app.evals.run --cases 2` — metric table + Langfuse experiment
    logging evidenced.

Prints PASS/FAIL per assertion; exit 0 iff all PASS. Secrets never echoed.
Usage (from the repo root or scripts/):  backend/.venv/bin/python scripts/verify_evals.py
Flags: --skip-build (image already fresh), --encounter <id> (reuse a finished
encounter; skips steps 1-3).
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import subprocess
import sys
import time
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
BASE = "http://localhost:8002"
LANGFUSE_HOST = "http://localhost:3101"
EMAIL = "maya.patel@careloop.demo"
PASSWORD = "demo-only-password"

DETERMINISTIC_GATES = (
    "schema_validity",
    "rejected_action_leakage",
    "modified_action_fidelity",
    "tool_selection_validity",
    "permission_behavior",
    "latency",
)

results: list[tuple[str, bool, str]] = []


def check(name: str, passed: bool, detail: str) -> None:
    results.append((name, passed, detail))
    print(f"  [{'PASS' if passed else 'FAIL'}] {name}: {detail}")


def run(cmd: list[str], *, timeout: int, label: str) -> subprocess.CompletedProcess:
    print(f"\n$ {' '.join(cmd)}  ({label})")
    proc = subprocess.run(
        cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout
    )
    tail = "\n".join((proc.stdout + "\n" + proc.stderr).strip().splitlines()[-14:])
    print("\n".join(f"    | {line}" for line in tail.splitlines()))
    return proc


def env_langfuse_keys() -> tuple[str, str] | None:
    """LANGFUSE keys from ../.env for host-side span polling (never echoed)."""
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return None
    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    pk, sk = values.get("LANGFUSE_PUBLIC_KEY"), values.get("LANGFUSE_SECRET_KEY")
    return (pk, sk) if pk and sk else None


def main(args: argparse.Namespace) -> int:
    enc_id: str | None = args.encounter

    # ------------------------------------------------------------------ 1-3
    if enc_id is None:
        if not args.skip_build:
            proc = run(
                ["docker", "compose", "up", "-d", "--build", "--wait", "backend"],
                timeout=600, label="rebuild backend with the eval layer baked in",
            )
            check("backend_rebuilt", proc.returncode == 0,
                  "image rebuilt + healthy" if proc.returncode == 0
                  else f"exit {proc.returncode}")
            if proc.returncode != 0:
                return 1

        # The live suggestion engine may legitimately return zero suggestions
        # on an unlucky run ("return an empty list when nothing is worth
        # asking") — each attempt is a fresh REAL run, retried once.
        for attempt in (1, 2):
            proc = run(
                ["docker", "compose", "exec", "-T", "backend",
                 "python", "-m", "app.context.reset_runtime"],
                timeout=180, label="fresh runtime state",
            )
            if proc.returncode != 0:
                check("runtime_reset", False, f"exit {proc.returncode}")
                return 1

            proc = run(
                [sys.executable, str(REPO_ROOT / "scripts" / "verify_full_loop.py")],
                timeout=600,
                label=f"REAL replay -> extraction -> suggestions (attempt {attempt})",
            )
            match = re.search(r"encounter (enc_\w+) created", proc.stdout)
            enc_id = match.group(1) if match else None
            if proc.returncode == 0 and enc_id:
                break
            print(f"    (full-loop attempt {attempt} failed — "
                  f"{'retrying once' if attempt == 1 else 'giving up'})")
        check("runtime_reset", True, "encounter-derived state wiped")
        check("full_loop_pass", proc.returncode == 0 and enc_id is not None,
              f"encounter {enc_id}" if enc_id else "no encounter id in output")
        if proc.returncode != 0 or enc_id is None:
            return 1

        proc = run(
            [sys.executable, str(REPO_ROOT / "scripts" / "verify_hitl.py"), enc_id],
            timeout=600, label="REAL end -> decisions -> finalize -> reports",
        )
        check("hitl_pass", proc.returncode == 0, f"exit {proc.returncode}")
        if proc.returncode != 0:
            return 1
    else:
        print(f"(reusing encounter {enc_id}; steps 1-3 skipped)")

    # ------------------------------------------------------------------ 4
    proc = run(
        ["docker", "compose", "exec", "-T", "backend",
         "python", "-m", "app.evals.online", enc_id],
        timeout=600, label="ONLINE evaluators in-container (real judge calls)",
    )
    check("online_evals_run", proc.returncode == 0,
          "all deterministic gates green in-container" if proc.returncode == 0
          else f"exit {proc.returncode} (gate failure or error)")

    # ------------------------------------------------------------------ 5
    with httpx.Client(base_url=BASE, timeout=60) as client:
        r = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        r.raise_for_status()
        cookies = r.cookies

        r = client.get(f"/api/evals/{enc_id}", cookies=cookies)
        check("evals_api_200", r.status_code == 200, f"status={r.status_code}")
        payload = r.json() if r.status_code == 200 else {}
        rows = payload.get("results", [])
        names = {row["evaluator"] for row in rows}
        kinds = {row["kind"] for row in rows}
        check("evals_rows_present", len(names) >= 6,
              f"{len(rows)} rows, {len(names)} evaluators: {sorted(names)}")
        check("evals_both_kinds", kinds == {"deterministic", "model"},
              f"kinds={sorted(kinds)}")
        by_name = {row["evaluator"]: row for row in rows}
        failing_gates = [g for g in DETERMINISTIC_GATES
                         if g not in by_name or not by_name[g]["passed"]]
        check("deterministic_gates_green", not failing_gates,
              "all 6 deterministic gates pass on the clean encounter"
              if not failing_gates else f"failing: {failing_gates}")
        labeled = all("not clinical validation" in row["detail"] for row in rows)
        check("scores_labeled_prototype", labeled and bool(rows),
              "every detail carries the prototype-evaluator label")

        criteria = payload.get("launch_criteria", [])
        green = [c for c in criteria if c["passed"]]
        check("launch_criteria_renders", len(criteria) == 6,
              f"{len(criteria)} criteria rows: "
              + "; ".join(f"{c['name']}={c['actual']}" for c in criteria))
        check("launch_criteria_green", len(green) >= 4,
              f"{len(green)}/6 green: {[c['name'] for c in green]}")

        r = client.get(f"/api/ops/trace-summary/{enc_id}", cookies=cookies)
        check("trace_summary_200", r.status_code == 200, f"status={r.status_code}")
        trows = r.json().get("rows", []) if r.status_code == 200 else []
        steps = {row["step"] for row in trows}
        statuses = {row["status"] for row in trows}
        valid_statuses = statuses <= {"PASS", "FAIL", "DEGRADED", "BLOCKED", "DENIED"}
        core_steps = {"Context load", "Live transcription", "Fact extraction",
                      "Live suggestions", "Care-plan generation",
                      "Clinician feedback captured", "Tool execution", "Reports",
                      "Online evals"}
        check("trace_summary_rows", core_steps <= steps and valid_statuses,
              f"{len(trows)} rows; statuses={sorted(statuses)}; "
              f"missing={sorted(core_steps - steps)}")
        core_pass = [s for s in ("Fact extraction", "Care-plan generation",
                                 "Clinician feedback captured", "Reports",
                                 "Online evals")
                     if any(row["step"] == s and row["status"] == "PASS" for row in trows)]
        check("trace_summary_core_pass", len(core_pass) == 5,
              f"PASS steps: {core_pass}")
        with_latency = [row["step"] for row in trows if row.get("latency_ms")]
        check("trace_summary_latencies", len(with_latency) >= 2,
              f"latency_ms present on: {with_latency}")

        r = client.get("/api/analytics/feedback", cookies=cookies)
        check("analytics_200", r.status_code == 200, f"status={r.status_code}")
        analytics = r.json() if r.status_code == 200 else {}
        mix = analytics.get("decision_mix", {})
        cur = analytics.get("current_session", {})
        check("analytics_mix_keys",
              all(k in mix for k in ("accepted", "modified", "rejected", "total_events")),
              f"decision_mix keys={sorted(mix.keys())}")
        check("analytics_current_session",
              cur.get("modified", 0) >= 1 and cur.get("rejected", 0) >= 1,
              f"current_session={cur} (hitl modified+rejected decisions merged)")
        reasons = analytics.get("reasons", [])
        live_reject = [x for x in reasons if x.get("current_session", 0) > 0]
        check("analytics_live_reason_merged",
              any(x["category"] == "patient_limitation" for x in live_reject),
              f"live rejection reasons: {[(x['category'], x['current_session']) for x in live_reject]}")

    # Langfuse: eval spans landed on the encounter trace (best effort, real).
    keys = env_langfuse_keys()
    if keys:
        with httpx.Client(base_url=BASE, timeout=30) as client:
            r = client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
            trace_id = None
            if r.status_code == 200:
                enc = client.get(f"/api/encounters/{enc_id}", cookies=r.cookies)
                if enc.status_code == 200:
                    trace_id = enc.json()["encounter"].get("trace_id")
        found: set[str] = set()
        if trace_id:
            auth = base64.b64encode(f"{keys[0]}:{keys[1]}".encode()).decode()
            deadline = time.monotonic() + 90
            while time.monotonic() < deadline:
                try:
                    resp = httpx.get(
                        f"{LANGFUSE_HOST}/api/public/v2/observations",
                        params={"traceId": trace_id, "limit": 100, "page": 1},
                        headers={"Authorization": f"Basic {auth}"}, timeout=15,
                    )
                    obs_names = {o.get("name") for o in resp.json().get("data", [])}
                    found = {n for n in obs_names if n and n.startswith("eval_")} | (
                        {"run_online_evaluators"} & obs_names
                    )
                    if "run_online_evaluators" in found and len(found) >= 3:
                        break
                except Exception as exc:  # noqa: BLE001
                    print(f"    (langfuse poll error: {type(exc).__name__})")
                time.sleep(5)
        check("langfuse_eval_spans", "run_online_evaluators" in found,
              f"spans on encounter trace: {sorted(found)}")
    else:
        print("  (skipping Langfuse span check — no keys in ../.env)")

    # ------------------------------------------------------------------ 6
    proc = run(
        ["docker", "compose", "exec", "-T", "backend",
         "python", "-m", "app.evals.run", "--cases", "2"],
        timeout=600, label="OFFLINE suite, 2 cases, REAL model",
    )
    out = proc.stdout + proc.stderr
    check("offline_metric_table", "Metric table" in out and "Fact recall" in out,
          "fixed metric table printed")
    check("offline_langfuse_experiment",
          "Logging as Langfuse experiment" in out and "DEVIATION" not in out,
          "experiment run logged over dataset careloop_eval_cases"
          if "Logging as Langfuse experiment" in out
          else "experiment logging NOT evidenced")
    check("offline_gates", proc.returncode == 0,
          "deterministic gates ALL PASS" if proc.returncode == 0
          else f"exit {proc.returncode} — see GATE FAIL lines above")

    failures = [name for name, passed, _ in results if not passed]
    print("\n" + "=" * 70)
    print(f"verify_evals: {'ALL CHECKS PASSED' if not failures else 'FAILED: ' + ', '.join(failures)} "
          f"({sum(1 for _, p, _ in results if p)}/{len(results)})")
    return 0 if not failures else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-build", action="store_true")
    parser.add_argument("--encounter", default=None,
                        help="reuse a finished encounter id (skips replay + hitl)")
    sys.exit(main(parser.parse_args()))
