# Examples — Licenses

| File         | Source / Author                                                                                                                       | License       | Notes |
|--------------|---------------------------------------------------------------------------------------------------------------------------------------|---------------|-------|
| `jfk.wav`    | "Ich bin ein Berliner" excerpt — President John F. Kennedy, 1963. Sourced from the QAI AppBuilder reference sample (whisper_asr_shared). | Public domain | ~11 s English speech, 16 kHz mono. Smoke-test clip used by the upstream Qualcomm Whisper sample. |
| `jfk.npz`    | NumPy-archived form of `jfk.wav` (same source).                                                                                        | Public domain | Optional precomputed audio array used by the reference sample's `load_demo_audio()`; not consumed by `runner.py` directly. |
