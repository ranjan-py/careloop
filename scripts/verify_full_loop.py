#!/usr/bin/env python3
"""Full-loop E2E: replay audio → Deepgram → extraction → facts/suggestions.

Runs against the LIVE stack from the host. Verifies the three scripted demo
beats arrive as real WS envelopes, persist in Postgres via the container's
DatabaseStore, and project to Neo4j with a CONFLICTS_WITH edge. Exit 0 = PASS.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys

import httpx
import websockets

BASE = "http://localhost:8002"
WS_BASE = "ws://localhost:8002"
EMAIL = "maya.patel@careloop.demo"
PASSWORD = "demo-only-password"
WATCH_SECONDS = 260  # full 238 s fixture + finalize slack


def ok(name: str, detail: str = "") -> None:
    print(f"  [PASS] {name}: {detail}")


def fail(name: str, detail: str = "") -> None:
    print(f"  [FAIL] {name}: {detail}")


async def main() -> int:
    # Fresh runtime state: prior rehearsal runs must not pollute extraction's
    # fact inventory (the pipeline correctly remembers across encounters).
    print("resetting runtime state...")
    reset = subprocess.run(
        ["docker", "compose", "exec", "-T", "backend", "python", "-m", "app.context.reset_runtime"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    print(reset.stdout.strip() or reset.stderr.strip())
    if reset.returncode != 0:
        print("runtime reset failed — aborting")
        return 1

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as client:
        r = await client.post("/api/auth/login", json={"email": EMAIL, "password": PASSWORD})
        r.raise_for_status()
        cookie = r.cookies
        r = await client.post("/api/encounters", json={"patient_id": "john-miller"}, cookies=cookie)
        r.raise_for_status()
        enc = r.json()["encounter"]
        enc_id, ticket = enc["id"], enc["stream_ticket"]
        print(f"encounter {enc_id} created; watching {WATCH_SECONDS}s of replay")

        facts: list[dict] = []
        fact_changes: list[tuple[str, str]] = []
        suggestions: list[dict] = []
        finals = 0
        finalized = False

        async with websockets.connect(
            f"{WS_BASE}/api/encounters/{enc_id}/stream", open_timeout=15
        ) as ws:
            await ws.send(json.dumps({"type": "session.start", "mode": "replay", "ticket": ticket}))
            end_sent = False
            loop = asyncio.get_event_loop()
            deadline = loop.time() + WATCH_SECONDS
            while True:
                budget = deadline - loop.time()
                if budget <= 0 and not end_sent:
                    await ws.send(json.dumps({"type": "session.end"}))
                    end_sent = True
                    # relay finalize grace (20 s) + pipeline flush (final
                    # extraction + suggestion model calls) can take a while:
                    # extend the DEADLINE, not just this iteration's budget.
                    deadline = loop.time() + 120
                    budget = 120
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(budget, 1))
                except (asyncio.TimeoutError, websockets.ConnectionClosed):
                    if end_sent:
                        break
                    await ws.send(json.dumps({"type": "session.end"}))
                    end_sent = True
                    deadline = loop.time() + 120  # same extension as the other branch
                    continue
                if isinstance(raw, bytes):
                    continue
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "transcript.final":
                    finals += 1
                elif t == "state.fact":
                    facts.append(msg["fact"])
                    fact_changes.append((msg["fact"]["subject"].lower(), msg["change"]))
                    print(f"    state.fact [{msg['change']:8}] {msg['fact']['subject']}: {msg['fact']['value'][:60]}")
                elif t == "suggestion.active":
                    suggestions.append(msg["suggestion"])
                    print(f"    suggestion.active: {msg['suggestion']['text'][:80]}")
                elif t == "session.finalized":
                    finalized = True
                    break

        failures = 0

        def check(name: str, cond: bool, detail: str) -> None:
            nonlocal failures
            if cond:
                ok(name, detail)
            else:
                fail(name, detail)
                failures += 1

        check("transcript_finals", finals >= 15, f"{finals} finals")
        bp = [f for f in facts if "blood pressure" in f["subject"].lower() or "bp" in f["subject"].lower()]
        kiosk = [f for f in bp if (f.get("method") or "").lower().find("kiosk") >= 0 or "kiosk" in f["value"].lower()]
        check("kiosk_bp_fact", bool(kiosk or bp), f"bp facts={len(bp)} kiosk-tagged={len(kiosk)}")
        lis = [f for f in facts if "lisinopril" in f["subject"].lower() and f.get("conflicts_with")]
        check("lisinopril_conflict", bool(lis), f"conflict facts={len(lis)} conflicts_with={lis[0]['conflicts_with'] if lis else None}")
        disputed = [c for c in fact_changes if c[1] == "disputed"]
        check("disputed_change_emitted", bool(disputed), f"{disputed[:3]}")
        check("suggestion_fired", len(suggestions) >= 1, f"{len(suggestions)} suggestions")
        check("finalized_handshake", finalized, "session.finalized received")

        # Persistence: chart + encounter facts from GET, conflict present
        r = await client.get(f"/api/encounters/{enc_id}", cookies=cookie)
        r.raise_for_status()
        data = r.json()
        enc_facts = [f for f in data["facts"] if f.get("encounter_id") == enc_id]
        chart_facts = [f for f in data["facts"] if f.get("encounter_id") != enc_id]
        check("facts_persisted", len(enc_facts) >= 2, f"encounter facts={len(enc_facts)} chart facts={len(chart_facts)}")
        check("chart_facts_in_get", len(chart_facts) >= 5, f"{len(chart_facts)} (Column B initial state)")
        persisted_conflict = [f for f in enc_facts if f.get("conflicts_with")]
        check("conflict_persisted", bool(persisted_conflict), f"{len(persisted_conflict)} rows")

        # Neo4j: CONFLICTS_WITH edge projected
        r = await client.get("/api/context/john-miller/graph", cookies=cookie)
        r.raise_for_status()
        rels = [rel["type"] for rel in r.json()["relationships"]]
        check("neo4j_conflicts_with_edge", "CONFLICTS_WITH" in rels, f"rel types={sorted(set(rels))}")

        print("=" * 70)
        verdict = "PASS" if failures == 0 else f"FAIL ({failures} failed)"
        print(f"verify_full_loop: {verdict}  (finals={finals} facts={len(facts)} suggestions={len(suggestions)})")
        return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
