# CareLoop — Verification Ledger

Per the execution contract: a feature is complete only after implementation, tests, Dockerized
execution, real-integration smoke where applicable, and E2E validation. Valid states:
`NOT STARTED | IN PROGRESS | BLOCKED | PASS | FAIL`. No feature is marked PASS without evidence.

**Last updated:** 2026-08-14 (build day 1)
**Git commit:** see `git log`
**OpenAI model:** pending startup verification against real key
**Deepgram model:** nova-3-medical (pending smoke verification)

## Feature verification table

| Feature | Status | Test performed | Evidence/result | Known issue |
|---|---|---|---|---|
| Docker stack (compose up, health checks) | IN PROGRESS | — | — | — |
| Seeded patient (Synthea pipeline / overlay fallback) | IN PROGRESS | — | — | — |
| Patient overview (pre-visit intelligence) | IN PROGRESS | — | — | — |
| Deepgram streaming (live mic) | NOT STARTED | — | — | — |
| Deepgram streaming (replay fixture) | NOT STARTED | — | — | — |
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
| Neo4j projection + multi-hop provenance query | NOT STARTED | — | — | — |
| Langfuse tracing (real trace ingested) | NOT STARTED | — | — | — |
| Langfuse prompt management | NOT STARTED | — | — | — |
| Online/session evaluators | NOT STARTED | — | — | — |
| Launch-criteria table | NOT STARTED | — | — | — |
| Offline eval suite (current config) | NOT STARTED | — | — | — |
| Synthetic feedback analytics | NOT STARTED | — | — | — |
| Golden-snapshot reset (`make reset-demo`) | NOT STARTED | — | — | — |
| OpenAI smoke | BLOCKED | awaiting key in `.env` | — | user supplies key |
| Deepgram smoke | BLOCKED | awaiting key in `.env` | — | user supplies key |
| Langfuse smoke (trace from real OpenAI call) | BLOCKED | awaiting key in `.env` | — | user supplies key |
| Mandatory E2E acceptance (steps 0–32) | NOT STARTED | — | — | — |

## Smoke-test log

(append-only; commands + results, never secrets)

## E2E checklist runs

(append-only)

## Known limitations

- Single-microphone diarization treated as hint only; manual speaker toggle is the primary path.
- Fixture audio is TTS-generated; the streaming path through Deepgram is real.
