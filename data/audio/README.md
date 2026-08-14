# Audio fixtures (spec sections 8, 33)

**Fixture audio is TTS-generated (synthetic voices) — no real recordings.**

`miller_encounter.wav` — the frozen two-voice scripted John Miller encounter:
16 kHz mono 16-bit PCM WAV, produced by `scripts/generate_fixture.py` from
`docs/encounter_script.md` using OpenAI TTS (doctor and patient rendered with
two clearly distinct voices, stitched with 1-3 s inter-turn silences).

NOT YET GENERATED: producing it requires a real `OPENAI_API_KEY`:

```bash
python3 scripts/generate_fixture.py --dry-run   # validate the script parse, no key needed
OPENAI_API_KEY=... python3 scripts/generate_fixture.py
```

Replay mode streams this file through the REAL Deepgram pipeline at real-time
pace (spec 8). Iterate until Deepgram's transcript is clean and the scripted
demo moments fire reliably, then FREEZE the file and commit it (spec 33).

`segments/` holds per-turn TTS cache WAVs (safe to delete; regenerated on demand).
