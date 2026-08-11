# Audio Generation

Text-to-speech (TTS) models that synthesize natural-sounding speech from text,
running fully on the Snapdragon NPU (HTP) via QAI AppBuilder.

## Models

| Model | Task | Language | Output | AI Hub |
|-------|------|----------|--------|--------|
| [pipertts_en](pipertts_en/) | Text-to-Speech | English | 22050 Hz mono WAV | [pipertts_en](https://aihub.qualcomm.com/compute/models/pipertts_en) |
| [melotts_zh](melotts_zh/) | Text-to-Speech | Chinese (中文) | 44100 Hz mono WAV | [melotts_zh](https://aihub.qualcomm.com/compute/models/melotts_zh) |

Both models are VITS-style pipelines (text encoder → flow → HiFi-GAN decoder)
whose NPU stages run as separate QNN context binaries. Models are downloaded
automatically on first run into each model's `models/` folder, with the correct
chipset-specific package (Snapdragon X Elite / X2 Elite) selected via
`install.detect_device_model()`.

## Quick Start

```bash
cd qai-appbuilder\samples

# English (PiperTTS)
python run_inference.py --model pipertts_en --args "--text 'Hello, this is a test.'"

# Chinese (MeloTTS)
python run_inference.py --model melotts_zh --args "--text '中文是中国的语言文字。'"
```

## Dependencies

### PiperTTS-EN

```bat
pip install gruut numpy torch
```

### MeloTTS-ZH

MeloTTS-ZH has a broad dependency tree with platform-specific version pins.
**Do not** use a plain `pip install melotts` — use the one-shot setup script
instead:

```bat
cd models\audio\audio_generation\melotts_zh\python
setup_env.bat
```

`setup_env.bat` supports both **ARM64** (Windows on Snapdragon) and
**x86/AMD64** Python (3.10–3.13). It auto-detects the installed interpreter and
its architecture, then:

1. Installs core runtime deps (`qai_appbuilder`, `numpy`, `soundfile`, …).
2. Installs `torch` from the PyTorch CPU-only index (required for ARM64; also
   used on x86/AMD64 to avoid pulling a CUDA-enabled wheel).
3. Installs `melotts` from a patched sdist (the PyPI sdist is broken).
4. Installs the remaining deps with `--no-deps` from `requirements.txt` (pins
   `transformers==4.56.2` / `tokenizers==0.22.2` / `huggingface_hub==0.34.4`).
5. Patches `melo/text/japanese.py` to make it import-safe without MeCab.

> ℹ️ Each Python interpreter has its **own** `site-packages`. If you switch
> between x86/AMD64 and ARM64 Python, re-run `setup_env.bat` for the new
> interpreter. See [melotts_zh/README.md](melotts_zh/README.md) for the full
> setup guide and troubleshooting.
