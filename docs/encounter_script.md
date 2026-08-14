# Scripted John Miller encounter — TTS fixture source (spec sections 6.2, 8, 33)

**Synthetic demo dialogue. TTS-generated fixture audio; no real patients, clinicians,
or recordings are involved. Not for patient care.**

This is the source text for `data/audio/miller_encounter.wav`, produced by
`scripts/generate_fixture.py` (OpenAI TTS, two clearly distinct voices, stitched
with the inter-turn silences marked below, rendered to 16 kHz mono PCM WAV).
The fixture streams through the REAL Deepgram pipeline in replay mode — what is
real is the streaming path, not the larynx.

## Machine-readable format (parsed by generate_fixture.py)

- A turn is a line starting with `**Doctor:**` or `**Patient:**`.
- `<!-- pause: N.Ns -->` between turns inserts N.N seconds of silence (spec 33
  requires 1-3 s inter-turn silences to exercise Deepgram endpointing and the
  section 9 debounce triggers). Turns without an explicit marker get 1.5 s.
- Everything else in this file is ignored by the parser.

Writing rules used below: drug names spelled normally in full ("lisinopril",
"metformin" — clean for both TTS and nova-3-medical), doses spoken as words
("ten milligrams"), blood pressure as words ("one fifty over ninety-five"),
no abbreviations the voices could mangle.

## Demo beats (in order — do not reorder)

1. Greeting / visit context
2. Kiosk BP ~150/95 mention (pharmacy kiosk, load-bearing sourcing)
3. Lisinopril stopped ~2 weeks ago
4. Dizziness timing relative to doses
5. No home BP monitor / no other BP medications
6. Wrap-up

---

## Script

**Doctor:** Good morning, John. It's good to see you again. I know it's been a little while since your last visit. How have you been feeling overall?

<!-- pause: 2.0s -->

**Patient:** Morning, doctor. I've been alright, mostly. Work has been busy. I know I missed that follow up visit last month, and I'm sorry about that.

<!-- pause: 1.5s -->

**Doctor:** No need to apologize, I'm glad you're here today. The main thing I want to talk about is your blood pressure. At your last two visits it was running high, around one fifty over the mid nineties. Have you had a chance to check it anywhere since then?

<!-- pause: 2.5s -->

**Patient:** Yeah, actually. I checked it on the machine at the grocery store, the one next to the pharmacy counter. Last week it said about one fifty over ninety five.

<!-- pause: 2.0s -->

**Doctor:** Okay, so the pharmacy kiosk at the grocery store. That matches what we've been seeing here in the clinic. Do you have a blood pressure cuff at home so you can check it regularly?

<!-- pause: 2.0s -->

**Patient:** No, I don't own one of those. I just use the machine at the store when I happen to be there. Maybe once a month or so.

<!-- pause: 2.5s -->

**Doctor:** That's helpful to know. Now, let's talk about your medicines. You should be taking lisinopril, ten milligrams, once a day for the blood pressure. Are you still taking that every day?

<!-- pause: 3.0s -->

**Patient:** Well, about that. I actually stopped taking the lisinopril about two weeks ago.

<!-- pause: 2.0s -->

**Doctor:** Okay. Tell me more about that. What made you stop?

<!-- pause: 1.5s -->

**Patient:** It was making me dizzy. Really lightheaded, like the room was tilting. It got to where I didn't want to drive in the mornings, so I just quit taking it.

<!-- pause: 2.5s -->

**Doctor:** I'm glad you told me. When you were getting dizzy, when did it happen? Was it soon after you took the pill, or later in the day?

<!-- pause: 2.0s -->

**Patient:** Mostly in the first couple of hours after I took it. I take it with breakfast, and by mid morning I'd feel woozy, especially if I stood up fast. By the afternoon it would mostly wear off.

<!-- pause: 2.5s -->

**Doctor:** That timing matters, so thank you. And since you stopped it two weeks ago, has the dizziness gone away?

<!-- pause: 1.5s -->

**Patient:** Yeah, pretty much completely. I feel steadier now. But I figured my pressure is probably creeping back up, which is why that store machine number worried me.

<!-- pause: 2.5s -->

**Doctor:** That's a fair concern, and we'll deal with it together. Are you taking anything else for blood pressure right now? Any other pills, or anything over the counter, or supplements?

<!-- pause: 2.0s -->

**Patient:** No, nothing else for pressure. The only other thing I take is the metformin for my sugar, a thousand milligrams in the morning and again at night. I haven't missed those.

<!-- pause: 2.5s -->

**Doctor:** Good, keep the metformin going exactly as you are, your diabetes has been stable and we're not changing anything there today. Here's what I'm thinking for the blood pressure. Your kidney tests and your potassium are a few months old now, so I want fresh labs before we decide on the next medicine. And we need a better way to measure your pressure than the store machine.

<!-- pause: 3.0s -->

**Patient:** That makes sense. I'd rather not go back on the one that made me dizzy, though.

<!-- pause: 1.5s -->

**Doctor:** Understood, and there are good alternatives we can consider once the labs are back. We'll get you set up before you leave today, and I want to see you again in a couple of weeks instead of waiting three months. Sound good?

<!-- pause: 2.0s -->

**Patient:** Sounds good, doctor. Thanks for not giving me a hard time about stopping the pill.

<!-- pause: 1.5s -->

**Doctor:** You told me exactly what I needed to know, that's what matters. Let's get those labs ordered.

---

## Freeze policy

Iterate on this script and the TTS rendering until Deepgram's transcript is clean
and every scripted demo moment fires reliably (kiosk BP fact, medication-conflict
fact, dizziness-timing suggestion, no-home-monitor fact). Then FREEZE
`data/audio/miller_encounter.wav` and commit it (spec 33). Regenerating the audio
after freeze requires re-verifying the full replay demo path.
