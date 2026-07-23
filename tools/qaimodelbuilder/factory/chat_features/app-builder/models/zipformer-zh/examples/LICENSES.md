# Examples — Licenses

> **Status: PLACEHOLDER.** This Pack is currently a scaffold; the example
> audio clips referenced by `manifest.json` (`mandarin-dialog.wav`,
> `mandarin-reading.wav`, `mandarin-light-accent.wav`) are **not** yet
> bundled. Do not attempt to "Run" the Pack from the App Builder UI
> until the real runner and these example assets are populated.

When implementing the real Pack, replace each row below with an
attribution + license statement for the actual audio used. Prefer CC0 /
public-domain sources (Common Voice zh-CN under CC0 — confirm each
individual clip's license, AISHELL test subsets where redistribution is
permitted, or recordings released by the contributor under CC0) so we
can ship them inside the repo without extra agreements.

| File                            | Source / Author | License | Notes |
|---------------------------------|-----------------|---------|-------|
| `mandarin-dialog.wav`           | _placeholder, replace with CC0 audio at v1.0 implementation time_ | _TBD_ | ~20–30 s of two-speaker conversational Mandarin (e.g. casual chat). Demonstrates segment-level timestamps over multiple turns. |
| `mandarin-reading.wav`          | _placeholder, replace with CC0 audio at v1.0 implementation time_ | _TBD_ | ~20–30 s of clear single-speaker Mandarin reading aloud (news / book passage). Happy-path WER baseline. |
| `mandarin-light-accent.wav`     | _placeholder, replace with CC0 audio at v1.0 implementation time_ | _TBD_ | ~20–30 s of Mandarin spoken with a light regional accent (Sichuan / Cantonese-influenced / northeastern, but still intelligible Putonghua). Demonstrates accent robustness vs. whisper-base. |

Recommended characteristics for real example audio:

- 16 kHz mono WAV (PCM 16-bit), to match `inputSchema.constraints` and
  avoid resampling overhead during smoke tests.
- 20–30 s duration each — long enough to exercise streaming chunking,
  short enough to keep the demo snappy.
- Clean recording — no music bed, no aggressive compression artifacts,
  ≥ 30 dB SNR. Save the noisy / far-field / very-long audio for a
  separate "limitations" demo, not the happy-path examples.
- File size ≤ 1 MB each (keep repo clone fast; 30 s @ 16 kHz mono PCM
  is ~960 KB raw).
- **Mandarin only.** Do NOT use code-switched zh+en clips here —
  zipformer-zh is Chinese-only and english-influenced examples make
  the demo look worse than it should. Use whisper-base's
  `mixed-zh-en-20s.wav` for that scenario.
- No personally identifiable information unless the source license
  explicitly permits redistribution.
