# CareLoop Architecture

**Synthetic clinical AI prototype — not for patient care.**

## The loop

```text
Longitudinal patient state (Synthea FHIR R4 → Postgres → Neo4j projection)
        ↓
Pre-visit intelligence ("What matters today?")
        ↓
Live encounter — Deepgram streaming STT (mic or replay fixture)
        ↓
Per-condition extraction subagents → conflict-preserving fact model
        ↓
Next-best-question suggestions (deterministic triggers)
        ↓
Structured care plan of Actions (schema-validated, retrieval-backed evidence)
        ↓
Clinician control: Approve / Modify / Reject (+ permission tiers)
        ↓
Mocked workflow tools (server-side tier enforcement, one scripted denial)
        ↓
Clinician summary + patient instructions (leakage- and fidelity-checked)
        ↓
Langfuse traces + evaluators + launch-criteria gate → feedback analytics
```

## Key decisions

- **Postgres authoritative, Neo4j as idempotent projection.** All writes commit to Postgres;
  the graph is rebuilt from it (`make rebuild-graph`). The graph earns its place with multi-hop
  provenance traversal: instruction → approved action → recommendation → source utterance.
- **Conflict preservation over overwrite.** EHR-sourced facts (`synthea_ehr`) and encounter facts
  (`patient_report`) are structurally distinct; contradictions become `CONFLICTS_WITH` edges and
  a `disputed` state, never a silent update.
- **Model output cannot mutate state.** Every AI output passes Pydantic validation; fact-ID
  references are constrained to enums of currently-valid IDs; the backend decides persistence.
- **Hand-rolled orchestration, deliberately.** Per-condition subagents (HTN / T2D / CKD-risk)
  fanned out by a small orchestrator, merged and deduplicated — chosen over a framework so
  context assembly, trace granularity, and the human-in-the-loop hard stop stay fully visible.
- **Provider seam.** One internal model client; provider/model/prompt-version are configuration.
  Prompts live in Langfuse prompt management and are fetched by version.
- **Honest failure states.** Deepgram/OpenAI/Neo4j/Langfuse failures surface as explicit
  degraded states; there are no fake fallback outputs anywhere.
- **Replay mode is a first-class tier.** A TTS-generated two-voice fixture streams through the
  real Deepgram socket at real-time pace — the integration is real; only the larynx is synthetic.

## Failure-mode ontology (evaluator + rejection taxonomy)

Deterministic gates: schema validity, rejected-action leakage, modified-action fidelity,
tool-selection validity, permission behavior. Model-graded (prototype only): grounding,
unsupported-fact detection, care-plan completeness against the canonical gap list,
next-best-action quality. Clinician rejection categories form the shared seven-value enum used
across the modal, persistence, analytics, and evals. Launch criteria render green/red from
`make eval`.

*(Filled in as each subsystem lands; see VERIFICATION.md for what has actually been verified.)*
