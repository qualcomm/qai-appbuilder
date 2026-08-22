# Whisper Base English ASR WebUI

A standalone App Builder WebUI for `whisper-base`, the on-device English Whisper ASR model. This app reuses the layout and audio-player workflow from `zipformer-zh-webui`, but swaps inference to Whisper Base.

## How to run

Preferred: open QAI ModelBuilder, go to the **Apps / 应用** menu, then run, preview, stop, or package `whisper-base-webui` from there. The app requests preferred port `1979`; the host waits for `/health` and opens the browser.

Manual debug run:

```bat
run.bat
```

or:

```bat
start.bat 1979
```

On Linux/macOS:

```bash
./start.sh 1979
```

## Required model

- Model pack: `whisper-base`
- Pack directory: `${APP_ROOT}/factory/chat_features/app-builder/models/whisper-base`
- Weights directory: `${APP_ROOT}/models/whisper-base`
- Context binaries: `encoder.bin`, `decoder.bin`
- Pack assets: `assets/mel_filters.npz` and `assets/gpt2.tiktoken`

The app loads the model once at startup and serializes inference requests because QNN contexts are not thread-safe.

## Input

The WebUI accepts either:

1. An uploaded audio file (`wav`, `mp3`, `flac`, `webm`, `m4a`, or `ogg`), or
2. A local path visible to the backend process.

Recommended audio: clean English speech. The model internally uses 16 kHz mono audio. The WebUI processes long audio in 30-second VAD-gated chunks, so it avoids the Whisper pack runner's original 120-second whole-file limit.

Controls:

- `language`: speech language (default: `en`)
- `vad`: voice-activity/silence handling flag, default on

## Output

The app shows:

- Full transcript (`fullText`)
- Segment table with `start`, `end`, `text`, and `conf`
- SRT subtitle export
- Raw JSON and basic performance metrics

Canonical JSON shape:

```json
{
  "language": "en",
  "task": "transcribe",
  "fullText": "And so my fellow Americans...",
  "segments": [
    { "start": 0.0, "end": 30.0, "text": "And so my fellow Americans...", "conf": 0.91 }
  ]
}
```

## Known limitations

- This installed pack is English-only: output language is `en` and task is `transcribe`.
- No translate-to-English mode in this local pack variant.
- No speaker diarization.
- No word-level timestamps.
- Segment timestamps are approximate and are best treated as subtitle-level anchors, not frame-accurate alignment.
- Long-audio handling is implemented by the WebUI: it splits the source into 30-second chunks, skips silence chunks when VAD is enabled, runs Whisper per chunk, and offsets timestamps back to the original timeline.

## Packaging note

The produced package is not a fully offline runtime. The target machine still needs a QAI ModelBuilder Python environment, including the Python interpreter, `qai_appbuilder`, and the QNN runtime. The package does not bundle Python or the QNN SDK.
