#!/usr/bin/env python3
"""CareLoop — two-voice TTS encounter fixture generator (spec section 33).

Reads the scripted John Miller dialogue from docs/encounter_script.md, renders
each turn with OpenAI TTS using two clearly distinct voices (doctor vs patient),
stitches the turns with the script's 1-3 s inter-turn silences, and writes a
16 kHz mono 16-bit PCM WAV to data/audio/miller_encounter.wav — the exact
format the live pipeline sends to Deepgram (encoding=linear16, sample_rate=16000).

The fixture is SYNTHETIC audio (TTS); it must be streamed through the REAL
Deepgram pipeline in replay mode. Iterate until Deepgram's transcript is clean
and the scripted demo moments fire, then FREEZE the WAV and commit it.

Requires OPENAI_API_KEY at runtime (real API calls, uses stdlib HTTP only).

Usage:
  python3 scripts/generate_fixture.py --dry-run     # parse + plan only, no API calls
  OPENAI_API_KEY=... python3 scripts/generate_fixture.py
  python3 scripts/generate_fixture.py --script docs/encounter_script.md --out data/audio/miller_encounter.wav

Per-segment WAVs are cached in data/audio/segments/ (keyed by voice+text hash)
so iterating on one line does not re-bill the whole script.
"""

from __future__ import annotations

import argparse
import array
import hashlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import wave
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SCRIPT = REPO_ROOT / "docs" / "encounter_script.md"
DEFAULT_OUT = REPO_ROOT / "data" / "audio" / "miller_encounter.wav"
CACHE_DIR = REPO_ROOT / "data" / "audio" / "segments"

TARGET_RATE = 16000  # 16 kHz mono Int16 PCM — pinned pipeline format (spec 8)
DEFAULT_PAUSE_S = 1.5
LEADING_SILENCE_S = 1.0
TRAILING_SILENCE_S = 1.5

OPENAI_TTS_URL = "https://api.openai.com/v1/audio/speech"
TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")

# Two clearly distinct voices (spec 33). Overridable without editing code.
VOICES = {
    "doctor": os.environ.get("FIXTURE_DOCTOR_VOICE", "shimmer"),   # clinician — Dr. Maya Patel persona
    "patient": os.environ.get("FIXTURE_PATIENT_VOICE", "onyx"),    # John Miller, 58-year-old man
}
VOICE_INSTRUCTIONS = {
    "doctor": "A calm, warm, professional primary-care physician. Clear diction, "
              "measured pace, natural clinical tone. Pronounce medication names "
              "carefully: lisinopril, metformin.",
    "patient": "A 58-year-old man at a routine doctor visit. Plain-spoken, "
               "unhurried, slightly sheepish when admitting he stopped his "
               "medication. Natural conversational pace.",
}

TURN_RE = re.compile(r"^\*\*(Doctor|Patient):\*\*\s*(.+)$")
PAUSE_RE = re.compile(r"<!--\s*pause:\s*([0-9]+(?:\.[0-9]+)?)\s*s\s*-->")


def parse_script(path: Path) -> list[dict]:
    """Return ordered turns: {speaker, text, pause_before_s}. Pause markers
    between turns attach to the FOLLOWING turn; the first turn gets none."""
    turns: list[dict] = []
    pending_pause: float | None = None
    for raw in path.read_text().splitlines():
        line = raw.strip()
        m = PAUSE_RE.search(line)
        if m:
            pending_pause = float(m.group(1))
            continue
        m = TURN_RE.match(line)
        if m:
            if not turns:
                pause = 0.0  # no lead-in pause on the first turn
            elif pending_pause is not None:
                pause = pending_pause
            else:
                pause = DEFAULT_PAUSE_S
            turns.append({"speaker": m.group(1).lower(), "text": m.group(2).strip(), "pause_before_s": pause})
            pending_pause = None
    if not turns:
        raise SystemExit(f"No turns parsed from {path} — expected '**Doctor:**'/'**Patient:**' lines.")
    return turns


def tts_request(text: str, voice: str, instructions: str, api_key: str) -> bytes:
    """One OpenAI TTS call via stdlib HTTP; returns WAV bytes."""
    payload = json.dumps(
        {
            "model": TTS_MODEL,
            "voice": voice,
            "input": text,
            "instructions": instructions,
            "response_format": "wav",
        }
    ).encode()
    req = urllib.request.Request(
        OPENAI_TTS_URL,
        data=payload,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    last_err: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:500]
            last_err = RuntimeError(f"OpenAI TTS HTTP {exc.code}: {body}")
            if exc.code in (429, 500, 502, 503):
                time.sleep(2 * (attempt + 1))
                continue
            raise last_err from exc
        except urllib.error.URLError as exc:
            last_err = exc
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"OpenAI TTS failed after retries: {last_err}")


def wav_to_mono16k(wav_bytes: bytes) -> array.array:
    """Decode a WAV, downmix to mono, resample to 16 kHz Int16 (pure stdlib —
    no audioop, which was removed in Python 3.13)."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    if sampwidth != 2:
        raise RuntimeError(f"Expected 16-bit PCM from TTS, got sample width {sampwidth}")
    samples = array.array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        mono = array.array("h", (0 for _ in range(len(samples) // channels)))
        for i in range(len(mono)):
            acc = 0
            base = i * channels
            for c in range(channels):
                acc += samples[base + c]
            mono[i] = acc // channels
        samples = mono
    if rate == TARGET_RATE:
        return samples
    # Linear-interpolation resample rate -> 16000.
    n_in = len(samples)
    n_out = int(n_in * TARGET_RATE / rate)
    out = array.array("h", (0 for _ in range(n_out)))
    ratio = rate / TARGET_RATE
    for i in range(n_out):
        pos = i * ratio
        i0 = int(pos)
        i1 = min(i0 + 1, n_in - 1)
        frac = pos - i0
        out[i] = int(samples[i0] * (1.0 - frac) + samples[i1] * frac)
    return out


def silence(seconds: float) -> array.array:
    return array.array("h", bytes(2 * int(seconds * TARGET_RATE)))


def segment_cache_path(speaker: str, text: str) -> Path:
    key = hashlib.sha1(f"{TTS_MODEL}|{VOICES[speaker]}|{text}".encode()).hexdigest()[:16]
    return CACHE_DIR / f"{speaker}-{key}.wav"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--script", type=Path, default=DEFAULT_SCRIPT)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--dry-run", action="store_true", help="Parse and print the plan; no API calls")
    ap.add_argument("--no-cache", action="store_true", help="Ignore cached per-segment WAVs")
    args = ap.parse_args(argv)

    turns = parse_script(args.script)
    total_pause = sum(t["pause_before_s"] or 0.0 for t in turns)
    print(f"Parsed {len(turns)} turns from {args.script}")
    print(f"  doctor voice:  {VOICES['doctor']}   patient voice: {VOICES['patient']}   model: {TTS_MODEL}")
    print(f"  inter-turn silence total: {total_pause:.1f}s (+{LEADING_SILENCE_S}s lead-in, +{TRAILING_SILENCE_S}s tail)")
    for i, t in enumerate(turns, 1):
        pause = f"[pause {t['pause_before_s']:.1f}s] " if t["pause_before_s"] else ""
        print(f"  {i:2d}. {pause}{t['speaker']:7s} {t['text'][:70]}{'…' if len(t['text']) > 70 else ''}")

    if args.dry_run:
        print("\nDry run — no API calls made, no audio written.")
        return 0

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("ERROR: OPENAI_API_KEY is required to generate audio (real TTS calls).", file=sys.stderr)
        print("Use --dry-run to validate the script without a key.", file=sys.stderr)
        return 2

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    stitched = silence(LEADING_SILENCE_S)
    for i, t in enumerate(turns, 1):
        if t["pause_before_s"]:
            stitched.extend(silence(t["pause_before_s"]))
        cache = segment_cache_path(t["speaker"], t["text"])
        if cache.exists() and not args.no_cache:
            wav_bytes = cache.read_bytes()
            print(f"  [{i:2d}/{len(turns)}] cached  {t['speaker']}: {t['text'][:50]}…")
        else:
            print(f"  [{i:2d}/{len(turns)}] TTS     {t['speaker']}: {t['text'][:50]}…")
            wav_bytes = tts_request(t["text"], VOICES[t["speaker"]], VOICE_INSTRUCTIONS[t["speaker"]], api_key)
            cache.write_bytes(wav_bytes)
        stitched.extend(wav_to_mono16k(wav_bytes))
    stitched.extend(silence(TRAILING_SILENCE_S))

    args.out.parent.mkdir(parents=True, exist_ok=True)
    out_samples = stitched
    if sys.byteorder == "big":
        out_samples = array.array("h", stitched)
        out_samples.byteswap()
    with wave.open(str(args.out), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(TARGET_RATE)
        wf.writeframes(out_samples.tobytes())

    duration = len(stitched) / TARGET_RATE
    print(f"\nWrote {args.out}  ({duration:.1f}s, 16 kHz mono 16-bit PCM)")
    print("Next: stream it through real Deepgram (replay mode / smoke test); freeze once clean.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
