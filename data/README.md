# CareLoop data — ALL SYNTHETIC

**Synthetic clinical AI prototype — not for patient care. Nothing in this
directory is real patient data; no real PHI exists anywhere in this repo.**

| Path | What it is | Produced by |
|------|------------|-------------|
| `synthea/` | Raw Synthea cohort output + `selected_bundle.json` (base chart candidate). Gitignored-scale artifacts; regenerate at will. | `scripts/generate_synthea.sh` + `scripts/select_patient.py` |
| `scenario_overlay/john_miller_bundle.json` | The authoritative John Miller FHIR R4 bundle. Every scripted demo fact, dates as offsets from runtime now. Seed input for Postgres/Neo4j. | `scripts/scenario_overlay.py` (re-run on every `make seed`) |
| `scenario_overlay/secondary_patients.json` | 2-3 display-only synthetic patient stubs for Today's Patients. | `scripts/scenario_overlay.py` |
| `audio/miller_encounter.wav` | Frozen TTS two-voice encounter fixture, 16 kHz mono PCM (not yet generated — needs OPENAI_API_KEY). | `scripts/generate_fixture.py` from `docs/encounter_script.md` |
| `evidence/evidence_corpus.json` | 30 synthetic guideline snippets — the section 11 retrieval corpus. | hand-authored |
| `eval_cases/case_01..10.json` | Offline regression eval cases (section 22.2), hand-labeled. | hand-authored |
| `historical_feedback.json` | Precomputed synthetic feedback aggregates (section 23), exact RejectionCategory enum keys. | hand-authored |

Dates inside generated files are computed at generation time as offsets from
runtime "now" (e.g., potassium at exactly today-92d). **Re-run `make seed` the
morning of any demo** so displayed ages stay correct.
