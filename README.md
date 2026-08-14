# CareLoop

**Synthetic clinical AI prototype — not for patient care.**

CareLoop is a closed-loop clinical execution demo: a live encounter becomes another source of
longitudinal patient state; the AI surfaces missing information in real time; it produces a
structured care plan of Actions; the clinician approves, modifies, or rejects each one; approved
Actions execute through demo workflow tools; and every model decision and clinician override is
traced and evaluated.

All patient data is synthetic (Synthea-generated FHIR R4 plus a deterministic scenario overlay).
The demo encounter audio fixture is TTS-generated; what is real is the streaming pipeline —
Deepgram STT, OpenAI extraction/planning, Langfuse observability, Neo4j context graph.

## Quick start

```bash
cp .env.example .env   # then fill in OPENAI_API_KEY and DEEPGRAM_API_KEY
docker compose up --build -d   # first time; afterwards: docker compose up -d
make seed
```

- App: http://localhost:3000
- API: http://localhost:8000
- Langfuse: http://localhost:3100
- Neo4j browser: http://localhost:7474

Demo the encounter with **replay mode** (bundled scripted audio through real Deepgram) or a live
microphone in Chrome on localhost.

See `contracts/CONTRACTS.md` for the API/WS contract, `docs/architecture.md` for design, and
`VERIFICATION.md` for what has actually been verified against real services.
