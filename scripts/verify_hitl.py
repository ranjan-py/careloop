#!/usr/bin/env python3
"""HITL E2E (spec §34 steps 12–23): end → care plan → decisions → finalize.

Usage: verify_hitl.py <encounter_id>   (an encounter whose replay just ended,
e.g. from verify_full_loop.py). Real OpenAI generation + real tools + real
reports against the live stack. Exit 0 = PASS.
"""

from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8002"
EMAIL = "maya.patel@careloop.demo"
PASSWORD = "demo-only-password"

failures = 0


def check(name: str, cond: bool, detail: str) -> None:
    global failures
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}: {detail}")
    if not cond:
        failures += 1


async def main() -> int:
    if len(sys.argv) < 2:
        print("usage: verify_hitl.py <encounter_id>")
        return 2
    enc_id = sys.argv[1]

    async with httpx.AsyncClient(base_url=BASE, timeout=180) as client:
        r = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        r.raise_for_status()
        cookies = r.cookies

        # --- End Visit → REAL care-plan generation (spec §12/§13)
        r = await client.post(f"/api/encounters/{enc_id}/end", cookies=cookies)
        r.raise_for_status()
        plan = r.json()["care_plan"]
        actions = plan["actions"]
        cats = sorted({a["category"] for a in actions})
        check("actions_generated", 3 <= len(actions) <= 6, f"{len(actions)} actions, categories={cats}")
        core = {"medication", "lab", "monitoring", "follow_up"}
        check("core_categories", len(core & set(cats)) >= 3, f"covered={sorted(core & set(cats))}")
        with_ev = [a for a in actions if a["evidence_refs"]]
        check("evidence_refs_present", len(with_ev) >= 1, f"{len(with_ev)}/{len(actions)} actions have retrieval evidence")
        med = [a for a in actions if a["category"] == "medication"]
        check(
            "med_requires_decision",
            all(a["permission"] == "required_clinician_decision" for a in med),
            f"{len(med)} medication actions",
        )
        auto = [a for a in actions if a["permission"] == "auto_demo"]
        check("one_auto_demo", len(auto) <= 1, f"{len(auto)} auto_demo actions")

        # Resolve evidence refs → snippets must be real
        if with_ev:
            ids = ",".join(with_ev[0]["evidence_refs"])
            r = await client.get(f"/api/evidence?ids={ids}", cookies=cookies)
            snippets = r.json().get("snippets", [])
            check("evidence_resolves", len(snippets) >= 1, f"{len(snippets)} snippets for {ids}")

        # --- Decisions: approve a lab/updated-labs action; modify follow-up; reject monitoring
        def pick(cat: str):
            for a in actions:
                if a["category"] == cat:
                    return a
            return None

        lab, follow, monitor = pick("lab"), pick("follow_up"), pick("monitoring")
        others = [a for a in actions if a not in (lab, follow, monitor) and a["permission"] != "auto_demo"]

        if lab:
            r = await client.post(f"/api/care-plan/actions/{lab['id']}/approve", json={}, cookies=cookies)
            check("approve_lab", r.status_code == 200, f"{lab['title'][:40]!r}")
        if follow:
            r = await client.post(
                f"/api/care-plan/actions/{follow['id']}/modify",
                json={
                    "final_title": follow["title"],
                    "final_description": "Schedule follow-up in ten days, once updated labs are back.",
                    "reason": "Prefer a tighter interval given the medication change.",
                },
                cookies=cookies,
            )
            check("modify_followup", r.status_code == 200, "final wording: 'in ten days'")
        if monitor:
            r = await client.post(
                f"/api/care-plan/actions/{monitor['id']}/reject",
                json={
                    "category": "patient_limitation",
                    "remarks": "Patient checks BP at a pharmacy kiosk; no home monitor for a twice-daily protocol.",
                },
                cookies=cookies,
            )
            check("reject_monitoring", r.status_code == 200, f"{monitor['title'][:40]!r}")
        for a in others:  # decide everything else so finalize is clean
            await client.post(f"/api/care-plan/actions/{a['id']}/approve", json={}, cookies=cookies)

        # --- Finalize → tools + REAL reports
        r = await client.post(f"/api/care-plan/{plan['id']}/finalize", cookies=cookies)
        check("finalize_200", r.status_code == 200, f"status={r.status_code} {r.text[:120] if r.status_code != 200 else ''}")
        if r.status_code != 200:
            print(f"verify_hitl: FAIL ({failures} failed)")
            return 1
        out = r.json()
        executions = out["executions"]
        exec_action_ids = {e["action_id"] for e in executions}
        if monitor:
            check("rejected_not_executed", monitor["id"] not in exec_action_ids, "no tool ran for rejected action")
        ok_execs = [e for e in executions if e["status"] in ("ok", "success", "completed", "executed")]
        check("tools_executed", len(ok_execs) >= 2, f"{len(ok_execs)} ok of {len(executions)} executions")

        clin = out.get("clinician_summary") or ""
        pat = out.get("patient_instructions") or ""
        check("reports_generated", bool(clin) and bool(pat), f"clinician={len(clin)} chars patient={len(pat)} chars")
        if monitor:
            leak = monitor["title"].lower() in (clin + pat).lower()
            check("rejected_absent_from_reports", not leak, f"{monitor['title'][:40]!r} not mentioned")
        check("modified_final_in_reports", "ten days" in (clin + pat).lower(), "'ten days' present")

        # Idempotent re-finalize: no re-execution, same reports
        r2 = await client.post(f"/api/care-plan/{plan['id']}/finalize", cookies=cookies)
        check("refinalize_idempotent", r2.status_code == 200 and (r2.json().get("clinician_summary") or "") == clin, "stored reports returned")

    print("=" * 70)
    print(f"verify_hitl: {'PASS' if failures == 0 else f'FAIL ({failures} failed)'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
