# Zipformer Chinese ASR WebUI

A standalone App Builder WebUI for `zipformer-zh`, the on-device Mandarin Chinese Zipformer RNN-T ASR model.

## How to run

Preferred: open QAI ModelBuilder, go to the **Apps / 应用** menu, then run, preview, stop, or package `zipformer-zh-webui` from there. The app requests preferred port `1977`; the host waits for `/health` and opens the browser.

Manual debug run:

```bat
run.bat
```

or:

```bat
start.bat 1977
```

## Required model

- Model pack: `zipformer-zh`
- Pack directory: `${APP_ROOT}/factory/chat_features/app-builder/models/zipformer-zh`
- Weights directory: `${APP_ROOT}/models/zipformer-zh`
- Context binaries: `encoder.bin`, `decoder.bin`, `joiner.bin`
- Token asset: `assets/tokens.txt` in the pack directory

The app loads the model once at startup and serializes inference requests because QNN contexts are not thread-safe.

## Input

The WebUI accepts either:

1. An uploaded audio file (`wav`, `mp3`, `flac`, `webm`, `m4a`, or `ogg`), or
2. A local path visible to the backend process.

Recommended audio: clean Mandarin Chinese speech. The model internally uses 16 kHz mono audio. Long audio is processed in 30-second chunks; the WebUI no longer enforces the original 120-second manifest limit.

Controls:

- `language`: fixed to `zh`
- `vad`: voice-activity/silence handling flag, default on
- `hotwords`: newline-separated names or technical terms to bias recognition where supported by the pack

## Output

The app shows:

- Full transcript (`fullText`)
- Segment table with `start`, `end`, and `text`
- SRT subtitle export
- Raw JSON and basic performance metrics

Canonical JSON shape:

```json
{
  "fullText": "今天的天气非常好。",
  "segments": [
    { "start": 0.0, "end": 3.4, "text": "今天的天气非常好。" }
  ]
}
```

## Known limitations

- Mandarin Chinese only; output is Simplified Chinese.
- No translation to English.
- No speaker diarization.
- No word-level timestamps.
- No calibrated confidence field.
- English-heavy or code-switched audio should use `whisper-base` instead.

## Packaging note

The produced package is not a fully offline runtime. The target machine still needs a QAI ModelBuilder Python environment, including the Python interpreter, `qai_appbuilder`, and the QNN runtime. The package does not bundle Python or the QNN SDK.
