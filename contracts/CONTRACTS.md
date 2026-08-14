# CareLoop — Integration Contracts (v2)

Frozen contract for parallel build agents. Any change to this file requires updating BOTH
backend and frontend. The authoritative product spec is
`../../FABLE5_ALTITUDE_DEMO_REQUIREMENTS.md` (referred to as "spec" below).

**v2 (post-scaffold reconciliation).** `backend/app/schemas/core.py` is now the CANONICAL
field-level definition of every model; `frontend/src/lib/types.ts` mirrors it. v2 pins:
- The one-time WS ticket rides inside the encounter object as `encounter.stream_ticket`
  (returned by POST /api/encounters; also re-issued on GET /api/encounters/{id} while live).
- `TranscriptSegment.ts` is a float — seconds from session start.
- New client→server WS message: `{"type":"suggestion.dismiss","suggestion_id"}` — dismissals
  persist server-side (spec §8C logging).
- New route: `GET /api/evidence?ids=<comma-separated>` → `{snippets: [{id,title,body,topic_tags}]}`.
- `POST /api/encounters/{id}/end` and `POST /api/care-plan/{id}/finalize` are IDEMPOTENT:
  repeat calls return existing state and never re-run tools.
- Finalize `executions[]` rows: `{id, action_id, tool_name, status, permission_tier, result?, error?}`.
- `GET /api/health` deps values are objects: `{status: "ok"|"unconfigured"|"unreachable", detail}`.
- PatientDetail.priorities are objects `{id,label,detail?,severity?}`; care_gaps `{id,label,detail?}`;
  MedicationSummary uses `ehr_status`; LabSummary has `unit?` + required `observed_at`;
  PatientListItem includes `display_only`.
- Launch-criteria bounds (spec §22.1 "stated bounds"): p95 next-best-action latency **< 6 s**;
  cost per encounter **< $0.50**.
- Category→tool mapping (spec §15): lab→create_demo_lab_order, follow_up→schedule_demo_followup,
  medication→save_demo_medication_review, other→generate_patient_instructions,
  referral→create_demo_referral, monitoring→send_demo_outreach_task.

## Services & ports (spec §28)

| Service        | Container      | Host port |
|----------------|----------------|-----------|
| frontend       | frontend       | 3000      |
| backend        | backend        | 8000      |
| app Postgres   | app-postgres   | (none)    |
| Neo4j browser  | neo4j          | 7474      |
| Neo4j bolt     | neo4j          | 7687      |
| Langfuse web   | langfuse-web   | 3100      |
| all other Langfuse services | —  | (none — stripped) |

## REST API (spec §27)

All routes under `/api`. JSON bodies. Auth: session cookie set by login (fake local auth).

```
POST /api/auth/login          {email, password} -> {ok, clinician}
POST /api/auth/logout         -> {ok}
GET  /api/auth/me             -> {clinician}

GET  /api/patients                        -> {patients: [PatientListItem]}
GET  /api/patients/{patient_id}           -> {patient: PatientDetail}       # incl. priorities[], care_gaps[]
GET  /api/patients/{patient_id}/timeline  -> {events: [TimelineEvent]}

POST /api/encounters                      {patient_id} -> {encounter}
GET  /api/encounters/{encounter_id}       -> {encounter, facts, suggestions, transcript}
POST /api/encounters/{encounter_id}/end   -> {encounter, care_plan}         # runs spec §12 pipeline
WS   /api/encounters/{encounter_id}/stream                                   # see WS protocol

POST /api/care-plan/actions/{action_id}/approve  {} -> {action}
POST /api/care-plan/actions/{action_id}/modify   {final_title, final_description, reason} -> {action}
POST /api/care-plan/actions/{action_id}/reject   {category: RejectionCategory, remarks} -> {action}
POST /api/care-plan/{care_plan_id}/finalize      -> {care_plan, executions, clinician_summary, patient_instructions}

GET  /api/context/{patient_id}/graph      -> {nodes, relationships}          # read live from Neo4j
GET  /api/context/{patient_id}/provenance/{instruction_or_action_id} -> {path: [GraphHop]}  # multi-hop query, spec §18
GET  /api/analytics/feedback              -> {decision_mix, reasons, high_friction, cohort_label}
GET  /api/evals/{encounter_id}            -> {results: [EvalResult], launch_criteria: [CriterionRow]}
GET  /api/ops/trace-summary/{encounter_id} -> {rows: [TraceSummaryRow]}      # from app Postgres, NOT Langfuse
GET  /api/health                          -> {status, deps: {postgres, neo4j, langfuse, openai, deepgram}}
```

## WebSocket protocol — `/api/encounters/{id}/stream`

- **Binary frames (client→server):** raw audio, 16 kHz mono Int16 PCM (AudioWorklet-downsampled).
- **Text frames (both directions):** JSON envelope `{"type": string, ...payload}`.

Client→server:
```
{"type":"session.start", "mode":"live"|"replay", "ticket": string}   # ticket from POST /api/encounters (one-time, short-lived)
{"type":"speaker.correct", "segment_id": string, "speaker":"doctor"|"patient"}
{"type":"session.end"}                                                # triggers finalize; server flushes Deepgram
```

Server→client:
```
{"type":"transcript.interim", "segment": {id, speaker, text, ts}}
{"type":"transcript.final",   "segment": {id, speaker, text, ts}}
{"type":"state.fact",         "fact": Fact, "change":"added"|"updated"|"disputed"}
{"type":"suggestion.active",  "suggestion": Suggestion}               # max 1–2 active
{"type":"suggestion.remove",  "suggestion_id": string}
{"type":"conn.status",        "deepgram":"connected"|"reconnecting"|"degraded"|"error", "detail": string}
{"type":"session.finalizing"} / {"type":"session.finalized", "encounter_id": string}
{"type":"error", "scope":"deepgram"|"openai"|"internal", "message": string, "recoverable": bool}
```

Replay mode: same socket, same server pipeline; server streams `data/audio/miller_encounter.wav`
to Deepgram at real-time pace instead of client audio (spec §8).

## Core models (mirrored: backend Pydantic `backend/app/schemas/core.py` ↔ frontend TS `frontend/src/lib/types.ts`)

```
Fact {
  id, fact_type,            # medication_status | observation | symptom | lab | condition | care_gap
  subject, value,
  source_type,              # ehr | patient_report | clinician
  source_class,             # synthea_ehr | patient_report        (spec §6.1)
  method?,                  # e.g. pharmacy_kiosk
  reported_at, ingested_at, # ISO timestamps; display ages derived at render time (spec §7)
  confidence, verification_status,   # unverified | confirmed | disputed
  encounter_id?, conflicts_with?     # fact id
}

Suggestion { id, encounter_id, kind: "question"|"info_gap"|"action", text, rationale, created_at,
             status: "active"|"dismissed"|"superseded" }

CarePlanAction {              # spec §13
  id, care_plan_id, category, # lab | medication | monitoring | follow_up | referral | other
  title, description, rationale,
  patient_facts_used: [fact_id], evidence_refs: [evidence_id],   # retrieval-produced (spec §11)
  risk_level: "low"|"medium"|"high",
  permission: "auto_demo"|"clinician_review"|"required_clinician_decision",
  status: "pending"|"approved"|"modified"|"rejected"|"executed"|"denied",
  decision?: { clinician_id, decided_at, model_version, prompt_version,
               final_title?, final_description?, reason?,        # modify
               category?: RejectionCategory, remarks? }          # reject
}

RejectionCategory — SINGLE shared enum (spec §14), exact labels everywhere incl. analytics:
  "missing_patient_information" | "clinical_disagreement" | "patient_limitation" |
  "operational_workflow_limitation" | "organization_protocol_constraint" |
  "requires_supervision_escalation" | "other"
Display labels: "Missing patient information", "Clinical disagreement", "Patient limitation",
  "Operational/workflow limitation", "Organization/protocol constraint",
  "Requires supervision/escalation", "Other"

EvalResult { evaluator, kind: "deterministic"|"model", score, passed, detail, encounter_id, prompt_version }
CriterionRow { name, target, actual, passed }        # launch-criteria table, spec §22.1
TraceSummaryRow { step, status: "PASS"|"FAIL"|"DEGRADED"|"BLOCKED"|"DENIED", detail, latency_ms? }
```

## Cross-cutting rules

- Backend decides what persists; model output validated via Pydantic before any write (spec §10).
- Postgres authoritative; Neo4j is an idempotent MERGE projection keyed on stable IDs (spec §17).
- Suggestion dedup keys scoped PER ENCOUNTER (spec §9).
- Every screen shows the persistent banner: "Synthetic clinical AI prototype — not for patient care."
  Patient pages add: "Synthetic patient — Synthea-generated FHIR R4".
- No secrets in logs/traces. WS auth via one-time ticket (above), not query-string tokens.
- UI vocabulary: care-plan items are called "Actions" (spec §25).

## Design tokens (frontend — spec §25, echo-not-clone)

- Display font: serif (Newsreader or similar Google serif) for hero/headlines; sans (DM Sans/Inter) body;
  mono uppercase letterspaced eyebrow labels.
- Palette: ink `#1B2430` on warm off-white `#F7F6F3`; slate grays for tables; ONE strong blue CTA
  (rounded-full pills); green for pass/positive; red for alerts/fail; thin warm-tan hairline dividers.
- Cards with generous whitespace; status chips; desktop-first 1280px+.
