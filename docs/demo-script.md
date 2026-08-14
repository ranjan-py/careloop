# CareLoop — Demo Script (30-minute onsite; ~15 min content + Q&A)

Preflight (before entering the building): `make reset-demo` · containers warm · Do Not Disturb on ·
pre-logged into Langfuse (3101) and Neo4j browser (7474) · `.env` excluded from editor ·
present from this laptop as localhost · mic permission pre-granted in demo browser profile ·
phone hotspot ready · backup video on laptop AND phone.

Every segment is independently skippable — interruptions must not cascade.

## 0:00–2:30 — Framing (no screen yet, or title slide)
- The problem in their economics: "For a risk-bearing org, the gap between learning something
  about a patient and completing the right next action is where outcomes and money are lost."
- Thesis, verbatim from the spec: "Ambient capture is only the input. The valuable system
  continuously reduces the time between learning something important about a patient and safely
  completing the right next action."
- Disclose the build method NOW: "I wrote an execution contract for a coding agent — real
  integrations only, BLOCKED-not-DONE, verification gates — and directed it. Same discipline you
  need to run clinical agents safely." (Have the spec document ready as an exhibit.)

## 2:30–4:00 — Pre-visit intelligence  [screen: /patients/john-miller]
- Open on "What matters today?" — priorities, timeline, care gaps. THIS is the product motion;
  the encounter is one more signal source.
- Point at the synthetic badge + provenance metadata on a fact (Synthea FHIR R4 → overlay).

## 4:00–6:30 — Live encounter  [screen: /encounters/:id/live — REPLAY MODE]
- ≤90 seconds of transcription attention, total. Start replay; note interim vs final styling and
  the real Deepgram connection status.
- The two scripted beats to narrate as they land in Column B:
  1. Kiosk BP ~150/95 arrives as a patient_report fact with method: pharmacy_kiosk.
  2. Lisinopril conflict: EHR-active vs patient-stopped → disputed, both preserved, CONFLICTS_WITH.
- Column C fires the next-best question (dizziness timing). Dismiss control, rationale, dedup.
- (Live mic ONLY if time remains and the room is quiet — this is the encore, never the act.)

## 6:30–10:00 — Care plan + clinician control  [screen: /encounters/:id/care-plan]
- End Visit → structured Actions with rationale, facts-used, retrieval-backed Evidence.
- Approve one (note permission tier). Modify one (edited text is what propagates — say it).
- Reject Home BP monitoring → modal → "Patient limitation" → kiosk remark. "Feedback captured.
  No automatic model change is made."
- Finalize → execution checklist incl. the auto-executed low-risk action AND the one denied
  tool call (permission tier enforced server-side, visible in trace).
- Summaries: rejected action provably absent; modified action in its final form.
- **If only 2 minutes existed, this screen is the demo.**

## 10:00–13:00 — Observability + evals  [screens: /ai-operations, Langfuse 3101]
- Trace Summary (from Postgres): the whole encounter tree incl. context-assembly spans,
  per-condition subagent spans, the denial event.
- One click into local Langfuse: the real trace, prompt versions, token/cost. (Trace generated
  early — ingestion is async.)
- Evals tab: online evaluators + the launch-criteria table green/red. Offline suite: 10 cases,
  judge-vs-human agreement stated honestly. "Regression harness, not validation."
- Analytics: decision mix, rejection reasons, high-friction drill-down ("38% reject — no home
  monitor") → this is the system learning operational reality.

## 13:00–15:00 — Architecture + scoping (1 slide each)
- Architecture: FHIR→Postgres→Neo4j projection, provider seam, hand-rolled orchestration and why.
- The cut list: P0/P1/P2 + out-of-scope, cuts made mid-build. Production deltas: BAAs, PHI
  redaction pre-trace, tenant isolation, read-only EHR posture, RxNorm + clinician confirm for
  ASR-derived med facts.
- Invite Q&A deliberately.

## Ops card (memorize; numbers filled after E2E)
- First-transcript latency: ___ · fact-extraction p50/p95: ___/___ · care-plan gen: ___
- Tokens + $ per encounter: ___ · extrapolation at 50 clinicians: ___/mo, dominated by ___
- Scale failure list: per-utterance fan-out cost, WS connection scaling, Neo4j write
  amplification, prompt-version migration mid-encounter, eval coverage decay.

## Failure drills (rehearsed, not improvised)
- Backend dies mid-demo → "this is why the spec mandates honest error states" → pivot to
  pre-seeded completed encounter (tier 2) or recording (tier 3) without breaking stride.
- Deepgram/OpenAI unreachable → same pivot; hotspot if it's venue Wi-Fi.
- 5-minute compressed version: framing (1) → care-plan screen (3) → launch criteria (1).
