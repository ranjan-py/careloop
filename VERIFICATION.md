# CareLoop — Verification Ledger

Per the execution contract: a feature is complete only after implementation, tests, Dockerized
execution, real-integration smoke where applicable, and E2E validation. Valid states:
`NOT STARTED | IN PROGRESS | BLOCKED | PASS | FAIL`. No feature is marked PASS without evidence.

**Last updated:** 2026-08-14 (build day 1 — stack up, smokes PASS, seed PASS)
**Git commit:** see `git log`
**OpenAI model:** gpt-5.2 — verified against real key (startup probe + smoke)
**Deepgram model:** nova-3-medical — verified via real streaming smoke
**Host ports (deviation from spec §28, collisions with other stacks on this machine):** frontend 3002, backend 8002, Langfuse 3101, Neo4j 7474/7687

## Feature verification table

| Feature | Status | Test performed | Evidence/result | Known issue |
|---|---|---|---|---|
| Docker stack (compose up, health checks) | PASS | `docker compose up -d --wait`; all 10 services healthy | 2026-08-14: healthy after 3 real fixes (Neo4j env strict-validation, Langfuse healthcheck $HOSTNAME binding, host-port collisions) | first-run `--wait` exit masked by pipe — fixed process |
| Seeded patient (Synthea pipeline / overlay fallback) | PASS | `python -m app.context.seed` in container against live Postgres+Neo4j | 4 patients, 24 timeline events, 11 chart facts, 30 evidence snippets, 1730 feedback rows; REAL Synthea base bundle | feedback source aggregates internally inconsistent — documented choice in seed agent notes |
| Patient overview (pre-visit intelligence) | PASS | authenticated API reads: /patients, /patients/john-miller, timeline | priorities with severities, meds ehr_status, labs runtime-relative (K+ = today−92d exactly) | UI walkthrough pending E2E |
| Deepgram streaming (live mic) | IN PROGRESS | relay implemented + unit-tested; browser mic path pending Chrome walkthrough | code path identical to verified replay leg | E2E step 5 encore |
| Deepgram streaming (replay fixture) | PASS | `scripts/verify_replay.py` — real E2E through app WS against live stack | enc_c9890888bd82: 20 finals + 56 interims, diarization correct, finalize handshake clean, 21 segments persisted, ws_errors=0 | — |
| OpenAI fact extraction (per-condition subagents) | NOT STARTED | — | — | — |
| Medication-conflict preservation | NOT STARTED | — | — | — |
| Live next-best question/action | NOT STARTED | — | — | — |
| Care-plan generation (schema + fact-ID enums) | NOT STARTED | — | — | — |
| Evidence retrieval (BM25 → evidence_refs) | NOT STARTED | — | — | — |
| Approve / Modify / Reject + shared enum | NOT STARTED | — | — | — |
| Permission tiers (auto-exec + scripted denial) | NOT STARTED | — | — | — |
| Feedback persistence | NOT STARTED | — | — | — |
| Mocked tool execution | NOT STARTED | — | — | — |
| Clinician summary (leakage + fidelity guaranteed) | NOT STARTED | — | — | — |
| Patient instructions (leakage + fidelity guaranteed) | NOT STARTED | — | — | — |
| Neo4j projection + multi-hop provenance query | PASS (projection) | seed projected; /api/context/john-miller/graph read live | 35 nodes (Patient/Conditions/Meds/Labs/Obs/CareGaps/Encounters), 34 rels | provenance query exercised after AI loop exists (task 5) |
| Langfuse tracing (real trace ingested) | NOT STARTED | — | — | — |
| Langfuse prompt management | NOT STARTED | — | — | — |
| Online/session evaluators | NOT STARTED | — | — | — |
| Launch-criteria table | NOT STARTED | — | — | — |
| Offline eval suite (current config) | NOT STARTED | — | — | — |
| Synthetic feedback analytics | NOT STARTED | — | — | — |
| Golden-snapshot reset (`make reset-demo`) | NOT STARTED | — | — | — |
| OpenAI smoke | PASS | `python -m app.smoke` in container | gpt-5.2 structured extraction, ~2.0 s latency | — |
| Deepgram smoke | PASS | fixture streamed through real live WS (linear16/16k) | nova-3-medical, 35 finals, first transcript 2.2 s, scripted terms present | 2× real-time pacing stalls Deepgram — smoke uses 90 s slice at 1× |
| Langfuse smoke (trace from real OpenAI call) | PASS | span-wrapped real call; polled v2 observations API | trace a4d4577fbcc9ceaa6c7f40393d9027ce retrievable at localhost:3101 | v4 events_only mode removed /api/public/traces — use /api/public/v2/observations |
| Mandatory E2E acceptance (steps 0–32) | NOT STARTED | — | — | — |

## Smoke-test log

(append-only; commands + results, never secrets)

**2026-08-14 — `docker compose exec -T backend python -m app.smoke` → exit 0 (all PASS)**
```
openai     PASS  model=gpt-5.2 extracted medication='lisinopril' status='stopped taking two weeks ago' latency=2040ms
deepgram   PASS  model=nova-3-medical finals=35 first_transcript=2234ms contains_scripted_terms=True
langfuse   PASS  Trace a4d4577fbcc9ceaa6c7f40393d9027ce ingested; observations retrievable via /api/public/v2/observations
```
Debugging trail (documented, per §2.4 honesty rules): (1) Deepgram FAIL at 2× real-time pacing on the
full 238 s fixture — Deepgram streaming expects ≤ real-time; smoke now streams a 90 s slice at 1×.
(2) Langfuse FAIL ×2 — SDK v4 renamed `start_as_current_span` → `start_as_current_observation`, and
v4 events_only deployments removed `/api/public/traces` (data was verifiably in MinIO + ClickHouse
`events_full` the whole time; poll target moved to `/api/public/v2/observations`).

**2026-08-14 — `python -m app.context.seed` → exit 0**
```
clinicians 1 · patients 4 · timeline_events 24 · chart_facts 11 · evidence_snippets 30
feedback_events 1730 · neo4j_projection nodes=39 relationships=34
```

## E2E checklist runs

(append-only)

## Known limitations

- Single-microphone diarization treated as hint only; manual speaker toggle is the primary path.
- Fixture audio is TTS-generated; the streaming path through Deepgram is real.
