# Speech Translator - On-device Chinese <-> English Speech Translation on Snapdragon NPU

## Overview

**Speech Translator** is a command-line application that listens to speech in one language and
speaks the translation in the other, entirely on the device. The neural networks (speech
recognition, machine translation and speech synthesis) run on the Snapdragon NPU (HTP) through
QAI AppBuilder as QNN context binaries. After the one-time model download no audio and no text
leave the machine.

- **Task**: Speech-to-speech translation (ASR -> MT -> TTS)
- **Directions**: Chinese -> English (`zh2en`, default) and English -> Chinese (`en2zh`)
- **Input**: microphone, a WAV file, or plain text (skips ASR)
- **Output**: translated text on the console, plus a WAV file and (optionally) playback
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU), model format `.bin` (QNN context binary) only
- **Entry point**: [`SpeechTranslator.py`](SpeechTranslator.py) (self-contained single file)

## Pipelines

```
zh2en   Chinese speech --[Zipformer ASR]--> Chinese text --[OpusMT zh->en]--> English text --[PiperTTS-EN]--> English speech
en2zh   English speech --[Zipformer ASR]--> English text --[OpusMT en->zh]--> Chinese text --[MeloTTS-ZH]--> Chinese speech
```

Zipformer is a single Mandarin + English streaming ASR model, so it is shared by both directions.
A run loads only the models its direction needs, and `--text` skips the ASR model completely
(it is neither downloaded nor loaded).

## Models

| Stage | Model | Files (all `.bin`) | Used by | Download size* | Sample README |
| ----- | ----- | ------------------ | ------- | -------------- | ------------- |
| ASR | Zipformer (Mandarin + English, streaming RNN-T) | `encoder.bin`, `decoder.bin`, `joiner.bin` | both | ~130 MB | [zipformer](../../models/audio/speech_recognition/zipformer/README.md) |
| MT | OpusMT zh->en (MarianMT) | `encoder.bin`, `decoder.bin` | `zh2en` | ~269 MB | [opus_mt_zh_en](../../models/multimodal/text_embedding/opus_mt_zh_en/README.md) |
| MT | OpusMT en->zh (MarianMT) | `encoder.bin`, `decoder.bin` | `en2zh` | ~270 MB | [opus_mt_en_zh](../../models/multimodal/text_embedding/opus_mt_en_zh/README.md) |
| TTS | PiperTTS-EN (22.05 kHz) | `encoder.bin`, `sdp.bin`, `flow.bin`, `decoder.bin` | `zh2en` | ~69 MB | [pipertts_en](../../models/audio/audio_generation/pipertts_en/README.md) |
| TTS | MeloTTS-ZH (44.1 kHz) | `encoder.bin`, `flow.bin`, `decoder.bin` | `en2zh` | ~331 MB | [melotts_zh](../../models/audio/audio_generation/melotts_zh/README.md) |

\* Size of the zip downloaded from Qualcomm AI Hub.

Runs on the **CPU** (not the NPU): audio capture/playback, feature extraction (fbank), the MarianMT
tokenizer, the PiperTTS G2P (`gruut`), the MeloTTS text front-end (G2P + a Chinese BERT, `torch`),
and all text post-processing described below.

## Requirements

Set up Python and QAI AppBuilder for Windows on Snapdragon first: see
[docs/python.md](../../../docs/python.md). Then install the packages for the direction you need.

**Common (both directions)**

```bat
pip install qai_appbuilder numpy scipy soundfile sounddevice requests tqdm transformers sentencepiece
```

**`zh2en`** additionally needs

```bat
pip install gruut
```

PyTorch and MeloTTS are **not** needed for `zh2en`.

**`en2zh`** additionally needs PyTorch (CPU build) and the patched `melotts` package, which has a
broad, platform-sensitive dependency tree. Do not `pip install melotts` directly; set it up with the
helper from the MeloTTS-ZH sample and follow its
[Environment Setup](../../models/audio/audio_generation/melotts_zh/README.md#environment-setup)
(Option A: `setup_env.bat`). Per-interpreter note: install into the same Python you run this
script with.

**Optional, recommended**

| Package / tool | Why |
| -------------- | --- |
| `kaldi_native_fbank` | Reference-quality ASR features. Without it a pure-numpy fallback is used. |
| `ffmpeg` on `PATH` | Silence-based VAD for `--audio` files: long recordings are split at pauses and each segment is decoded separately. Without it the file is decoded in one pass. |
| `g2p_en` | Phonetic repair of the English ASR transcript (`en2zh`). Skipped if missing. |

On start the script checks the required packages for the chosen options and exits with code `2` and
a list of what is missing if something is absent.

## Quick Start

Run from any directory:

```bat
cd samples\apps\speech-translator

:: Chinese speech from the microphone -> English speech
python SpeechTranslator.py

:: English WAV file -> Chinese speech
python SpeechTranslator.py --direction en2zh --audio in.wav

:: --audio without a file: download a sample clip into input\ and use it
::   en2zh -> jfk.wav (AI Hub),  zh2en -> 0.wav (sherpa-onnx test wav)
python SpeechTranslator.py --direction en2zh --audio
python SpeechTranslator.py --direction zh2en --audio

:: Text only (no microphone, no ASR model): translate and speak
python SpeechTranslator.py --direction zh2en --text "你好，世界"

:: Save the speech to a file and do not play it
python SpeechTranslator.py --text "今天天气真好" --no_play --out hello.wav
```

The first run downloads the models for the chosen direction (see [Model download](#model-download)),
and for `en2zh` also a Chinese BERT (`hfl/chinese-roberta-wwm-ext-large`, ~1.3 GB) from HuggingFace.
Later runs start from the local files.

Sample console output (illustrative; the exact wording depends on the models):

```
============================================================
  SPEECH TRANSLATION RESULT
============================================================
  Direction : zh2en
  Source    : 你好，世界
  Translated: Hello, world.
============================================================
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--direction {zh2en,en2zh}` | `zh2en` | Translation direction. |
| `--audio [FILE]` | - | Input WAV file. Mutually exclusive with `--text`. Given **without** a file, a sample clip is downloaded once into `input/` and used: `jfk.wav` ([AI Hub](https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/whisper_asr_shared/v1/audio/jfk.wav)) for `en2zh`, `0.wav` ([sherpa-onnx](https://huggingface.co/csukuangfj/sherpa-onnx-streaming-zipformer-zh-14M-2023-02-23/resolve/main/test_wavs/0.wav)) for `zh2en`. If `--audio` and `--text` are both absent, the microphone is used. |
| `--text TEXT` | - | Translate this text directly (source language must match the direction). The ASR model is not loaded. |
| `--record_seconds N` | `15` | Maximum microphone recording length. Recording stops by itself ~1 s after you stop speaking, so this is only a ceiling. |
| `--keep_capture` | off | Save the raw microphone recording to `output/mic_<direction>_<timestamp>.wav`. Off by default: captures are not written to disk. |
| `--out FILE` | `output/<direction>_<timestamp>.wav` | Output WAV path. A relative path is relative to the **current directory**. |
| `--no_play` | off | Do not play the result through the speakers (it is still saved). |
| `--chipset {x_elite,x2_elite,8380,8480}` | auto | Force the target chip, i.e. which model binaries to download and load. |
| `--models_dir DIR` | `./models` | Where model binaries are stored (one sub-folder per model). |
| `--strict_tls` | off | Use Python's default strict X.509 checking for downloads (see [Troubleshooting](#troubleshooting)). |
| `--debug` | off | Print the full traceback on errors instead of a one-line message. |

Exit codes: `0` success, `1` runtime error (download, model, input), `2` missing Python packages,
`130` interrupted with Ctrl+C.

## Model download

Models are downloaded automatically on first use into `models/<model_name>/` next to the script
(or `--models_dir`), from the Qualcomm AI Hub release assets. Context binaries are compiled per
chip, so the URL depends on the chip:

| Chip | `--chipset` value | Notes |
| ---- | ----------------- | ----- |
| Snapdragon X Elite | `x_elite` / `8380` | HTP v73 |
| Snapdragon X2 Elite | `x2_elite` / `8480` | |

The chip is detected from the CPU name (`PROCESSOR_IDENTIFIER`, then `wmic`, then PowerShell).
If it cannot be recognised the script stops with a message asking for `--chipset` instead of
guessing, because a binary built for the other chip may fail to load.

| Model | Release | Source |
| ----- | ------- | ------ |
| Zipformer | v0.55.0 (`float`) | `qaihub-public-assets` (S3, us-west-2) |
| OpusMT zh->en | v0.65.0 | `qaihub-public-assets` (S3, us-west-2) |
| OpusMT en->zh | v0.65.0 | `qaihub-public-assets` (S3, us-west-2) |
| PiperTTS-EN | v0.65.0 | `qaihub-public-assets` (S3, us-west-2) |
| MeloTTS-ZH | v0.65.0 (`w8a16`) | `qaihub-public-assets` (S3, us-west-2) |

Zipformer is deliberately kept on v0.55.0 `float`: the v0.65.0 `mixed` joiner clamps its logits
to >= 0 (often all zero), which decodes to garbage such as `廪廪廪...` or an empty string, and the
v0.65.0 `float` build only outputs a single character. An unstamped Zipformer install whose
`metadata.json` reports another precision / QAIRT version is replaced automatically.

Also fetched on first use: Zipformer `tokens.txt` (pinned to the `v0.55.0` tag of
`qualcomm/ai-hub-models`), the Marian tokenizer files for OpusMT (pinned HuggingFace revision), and
for `en2zh` the MeloTTS config (pinned HuggingFace revision) and the BERT, which the `melo` package
fetches itself.

Resulting layout:

```
speech-translator/
├── SpeechTranslator.py
├── README.md
├── models/                    # created on first run
│   ├── zipformer/             # encoder.bin, decoder.bin, joiner.bin, metadata.json, tokens.txt
│   ├── opus_mt_zh_en/         # encoder.bin, decoder.bin, tokenizer/
│   ├── opus_mt_en_zh/         # encoder.bin, decoder.bin, tokenizer/
│   ├── pipertts_en/           # encoder.bin, sdp.bin, flow.bin, decoder.bin
│   └── melotts_zh/            # <release folder>/{encoder,flow,decoder}.bin + melo_config_zh.json
├── input/                     # created by a bare --audio (default sample clips)
└── output/                    # created on first run (result WAV files)
```

Downloads are written to a `.part` file and renamed only when complete, so an interrupted download
never leaves a half-written file. **An existing model folder is reused as it is**: to move to a newer
release, delete the corresponding `models/<model_name>/` folder and run again.

## How it works

1. **Capture** - the microphone is read at its native rate with a small energy-based endpointer
   (noise-floor calibration, pre-roll so the first syllable is kept, stops ~1 s after speech).
   The audio is resampled to 16 kHz, de-biased and level-normalised.
2. **ASR (Zipformer, NPU)** - 80-bin Kaldi fbank features go through the chunked streaming
   encoder, then a modified beam search (beam 4) over the decoder/joiner. For WAV files, `ffmpeg`
   `silencedetect` splits the audio at pauses; each segment is decoded on a fresh encoder state and
   repeated words from overlapping windows are removed. For `en2zh` the Chinese tokens are masked
   out of the vocabulary so unclear English is not turned into stray Chinese characters.
3. **Text clean-up (CPU)**
   - `zh2en`: light clause punctuation is restored in the unpunctuated Chinese transcript, because
     MarianMT translates punctuated sentences much better.
   - `en2zh`: the ALL-CAPS Zipformer transcript is repaired (stutters, contractions, `a`/`an`,
     capitalisation, punctuation, and optional phonetic repair of split words).
   - English text passed with `--text` that already has normal casing is used unchanged.
4. **Translation (OpusMT, NPU)** - long input is split into chunks that fit the model and each chunk
   is translated with a KV-cached beam search (beam 4, no repeated 3-grams). If a result looks
   suspiciously short for its source (early end-of-sentence), the chunk is retried with greedy
   decoding, and for `zh2en` split at a clause boundary.
5. **Synthesis (NPU)** - PiperTTS-EN (`zh2en`) or MeloTTS-ZH (`en2zh`). Long text is chunked to stay
   inside the fixed input windows (512 phoneme / phone slots) and the audio is joined with short
   pauses.
6. **Performance** - the HTP is raised to the BURST profile once per task (ASR, translation,
   synthesis) rather than per inference call.

## Use as a library

`SpeechTranslator.py` does nothing on import, so it can be driven from your own program:

```python
import SpeechTranslator as st

st.set_chipset(None)          # optional: "x_elite" / "x2_elite" / None = auto-detect
st.Init("zh2en", need_asr=False)
result = st.Inference("zh2en", text="你好，世界")
print(result["translated_text"])          # "Hello, world."
audio, sr = result["audio"], result["sample_rate"]   # float32 mono, 22050 Hz
st.save_wav(audio, "hello.wav", sr)
st.Release("zh2en")
```

`Inference()` also accepts `audio_path="in.wav"` (needs `Init(direction)` with the default
`need_asr=True`) or no input at all to record from the microphone.

## Troubleshooting

**Download fails with `CERTIFICATE_VERIFY_FAILED ... CA cert does not include key usage extension`**
Python 3.13 enables strict X.509 checking, which rejects the certificate chain of the AI Hub S3
buckets. By default the script clears only that strictness flag; certificate chain and host name
verification stay on. Passing `--strict_tls` restores Python's default and will reproduce the
error on such a setup. Behind a proxy, set `HTTPS_PROXY`.

**`Cannot map CPU ... to a supported chip`**
Pass `--chipset x_elite` or `--chipset x2_elite` explicitly.

**The context binary fails to load**
The binary was built for a different chip. Check `--chipset`, delete the model folder and let it
download again.

**`[ERROR] Missing required Python packages`**
Install what is listed (see [Requirements](#requirements)). For `en2zh`, `torch` and `melo` come
from the MeloTTS-ZH setup; mismatched `transformers` / `huggingface_hub` versions are the most
common cause of import errors there (see that README's troubleshooting section).

**`Empty transcription` / the recording was cut off or never started**
Check the input device and level. The endpointer calibrates the noise floor for about 0.3 s after
`measuring background noise`; stay quiet until `>>> speak now <<<` appears. In a noisy room use
`--audio` with a recorded file. `--keep_capture` saves the microphone recording so a bad
recognition can be reproduced.

**Long WAV file loses words after a pause**
Install `ffmpeg` and put it on `PATH` so the silence-based VAD can split the file.

**A warning about `sacremoses`** is harmless for this application.

## Limitations

- A single voice per language; no speaker selection or voice cloning.
- The pipeline is sequential (record, recognise, translate, speak). It is not a real-time
  streaming interpreter.
- ASR output of Zipformer has no punctuation; punctuation and casing are restored heuristically,
  so unusual proper nouns, numbers or heavy accents can still be mistranslated.
- Fixed model windows apply (OpusMT 256 tokens, PiperTTS/MeloTTS 512 slots); the application splits
  long text, but very long inputs take proportionally longer.
- The MeloTTS flow and decoder binaries use fixed quantisation parameters that are compiled into
  the script. If you substitute a different release of that model, they must be re-derived;
  otherwise the audio will be distorted.
- Windows on Snapdragon X Elite / X2 Elite only (the binaries are chip specific).

## License

The sample code is under the BSD-3-Clause license (see the file header). The models and tokenizers
are distributed under their own licenses; see the Qualcomm AI Hub model pages and the HuggingFace
model cards of `Helsinki-NLP/opus-mt-zh-en`, `Helsinki-NLP/opus-mt-en-zh`,
`myshell-ai/MeloTTS-Chinese` and `hfl/chinese-roberta-wwm-ext-large`.
