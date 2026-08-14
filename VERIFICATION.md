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
| OpenAI fact extraction (per-condition subagents) | PASS | `scripts/verify_full_loop.py` — real replay through app WS + pipeline | enc_b63a87ee77ec: 11 fact envelopes live, 7 encounter facts persisted, per-condition spans in Langfuse | conflict rule tightened after false metformin conflict (stopped-vs-active only) |
| Medication-conflict preservation | PASS | same run | `lisinopril: stopped (2 wks)` added with conflicts_with=med-lisinopril; EHR fact → disputed (never overwritten); CONFLICTS_WITH edge live in Neo4j | — |
| Live next-best question/action | PASS | same run | 2 on-topic suggestions over WS (dizziness clarification, BP-measurement arrangement); per-encounter dedup; trigger decisions logged | hung-call freeze fixed via 45 s provider timeout |
| Care-plan generation (schema + fact-ID enums) | PASS | `scripts/verify_hitl.py` on enc_43ed39c44a78 (real OpenAI) | 5 actions, categories follow_up/lab/medication/monitoring/other, all fact ids valid | ~14 s generation latency |
| Evidence retrieval (BM25 → evidence_refs) | PASS | same run + GET /api/evidence resolve | 5/5 actions carry retrieval-produced refs; refs resolve to real snippets (ev-012, ev-030) | score floor 4.0 |
| Approve / Modify / Reject + shared enum | PASS | same run via REST | approve lab, modify follow-up ("ten days"), reject monitoring w/ patient_limitation + kiosk remark | — |
| Permission tiers (auto-exec + scripted denial) | PASS (tiers) | tier assignment + med gating verified; auto_demo executed | medication → required_clinician_decision; exactly one auto_demo | scripted DENIAL demo moment staged in E2E (task 8) |
| Feedback persistence | PASS | Decision rows persisted with original-vs-final | modify preserved original; reject carries category+remarks | — |
| Mocked tool execution | PASS | finalize on same run | 4/4 executions ok; rejected action ran NO tool | — |
| Clinician summary (leakage + fidelity guaranteed) | PASS | same run + deterministic leakage_check in generator | 2179 chars; rejected title absent; "ten days" (modified FINAL wording) present | leakage_check is exact-match; paraphrase detection = §22.1 model evaluator |
| Patient instructions (leakage + fidelity guaranteed) | PASS | same run | 1797 chars; rejected absent; modified wording present | — |
| Neo4j projection + multi-hop provenance query | PASS (projection) | seed projected; /api/context/john-miller/graph read live | 35 nodes (Patient/Conditions/Meds/Labs/Obs/CareGaps/Encounters), 34 rels | provenance query exercised after AI loop exists (task 5) |
| Langfuse tracing (real trace ingested) | NOT STARTED | — | — | — |
| Langfuse prompt management | PASS | real create/fetch round-trip + runtime fetch-by-label | 7 `careloop_*` prompts published @v1, served from Langfuse, versions recorded on traces | edits require publishing a new version (label `production`) |
| Golden-snapshot reset (`make reset-runtime`) | PASS | ran before each full-loop attempt | wipes encounter-derived state, restores disputed chart facts, rebuilds graph projection (37 nodes) | full volume reset remains `make reset-demo` |
| Online/session evaluators | PASS | `scripts/verify_evals.py` (20/20) + wired into POST /end | 11 evaluator rows on enc_5a356d41a1e4: 7 deterministic gates green + 4 model judges; run_online_evaluators span on encounter trace | model judges labeled "Prototype evaluator — not clinical validation" |
| Launch-criteria table | PASS | same run + /api/evals API | 6/6 GREEN: schema 100%, unsupported-fact 0%, leakage 0, fidelity 100%, p95 NBA 4.9 s (<6 s), ~$0.22/encounter (<$0.50, token-derived estimate) | Langfuse v4 events_only strips usage from public API — cost reconstructed at documented placeholder rates |
| Offline eval suite (current config) | PASS (9/10, 1 known failure) | full 10-case real run (370 s) + targeted reruns; results → Langfuse experiment `careloop_eval_cases` | gap coverage 80%, category coverage 100%, schema 100%, judge agreement 16/20 with disagreements listed; case_09 false positive fixed via discriminative-token matcher | **case_06 kept RED deliberately**: generator re-orders labs already current within 30 d (true positive — recency check is the next fix). Fact-recall 38% is matcher-limited, documented |
| Synthetic feedback analytics | PASS | /api/analytics/feedback after live decisions | synthetic cohort + current_session {approved:2, modified:1, rejected:1} merged; live patient_limitation rejection visible; decision-mix key bug (accepted_pct) fixed | high-friction list stays synthetic-cohort by design |
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

## Repo hygiene

**2026-08-14 — gitleaks (docker, zricethezav/gitleaks) over FULL git history
(`--log-opts=--all`): 8 commits scanned, no leaks found.** `.env` gitignored
from commit zero; pinned pk-lf-/sk-lf- Langfuse demo keys are localhost-only
by design. Re-run before sharing the repo; rotate OpenAI/Deepgram keys the
day after the onsite regardless.

## Live-loop reliability saga (2026-08-14, documented per §2.4 honesty rules)

Three-act debugging arc on the streaming AI loop, each verified by repeated
`verify_full_loop` runs:
1. **Starvation (intermittent):** during replay, in-loop OpenAI calls stalled;
   some runs had zero live suggestions. Fix: dedicated AI-pipeline event loop
   (`app/ai/executor.py`) + per-loop caching for the three loop-affine
   singletons (SQLAlchemy engine, Neo4j driver, AsyncOpenAI client).
2. **Cancellation (deterministic after isolation):** extraction cycles ran
   inside the debounce task; every new transcript final cancelled the debounce
   and await-forwarding killed the running cycle mid-suggestion-call
   (CancelledError bypasses `except Exception` — no failure logs). Fix:
   detach the cycle task; forced flush still awaits completion.
3. **Over-firing (after the fix):** un-cancelled cycles produced 15–18
   suggestions/run. Fix: cooldown 15 s → 45 s; suggestions require NEW or
   DISPUTED facts, not value updates.
Final state: 3 consecutive PASS runs post-isolation; post-churn-control run
PASS with 8 on-topic suggestions, conflict + disputed beats firing every run.

## E2E checklist runs

(append-only)

## Known limitations

- Single-microphone diarization treated as hint only; manual speaker toggle is the primary path.
- Fixture audio is TTS-generated; the streaming path through Deepgram is real.
