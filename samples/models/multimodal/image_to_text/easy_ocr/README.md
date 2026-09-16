# EasyOCR — Optical Character Recognition on Snapdragon NPU

## Overview

**EasyOCR** detects and recognizes text in images (English + Chinese Simplified), running on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Optical Character Recognition (OCR)
- **Languages**: English + Chinese Simplified
- **Input**: RGB image
- **Output**: Detected text with bounding boxes
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Model**: [easy_ocr](https://aihub.qualcomm.com/compute/models/easy_ocr)

## Model Architecture

2-stage pipeline:

| Stage | Model | Description |
| ----- | ----- | ----------- |
| 1. Text Detection | CRAFT Detector | Detects text regions as bounding boxes |
| 2. Text Recognition | CRNN Recognizer | Recognizes characters (6719-char vocabulary) |

Character data files (in `assets/Char/`):
- `ch_en_character.bin` — English + Chinese character set
- `ch_en_lang_char.bin` — Language character mapping
- `en_character.bin` — English-only character set
- `en_lang_char.bin` — English language character mapping

## Requirements

```bash
cd qai-appbuilder\samples
pip install -r models\multimodal\image_to_text\easy_ocr\python\requirements.txt
```

> **ARM64 Windows (WoS) note:** PyPI has no `win_arm64` wheels for
> `numpy` / `opencv-python` / `scipy` / `scikit-image` on Python 3.13, so a
> plain `pip install -r requirements.txt` cannot build them from source and
> `easyocr` fails to import. Run the helper script **once, before**
> installing `requirements.txt`, to pull matching prebuilt wheels from the
> community `cgohlke/win_arm64-wheels` bundle and install `easyocr` itself
> with `--no-deps` (so pip doesn't try to pull a mismatched `numpy` from
> PyPI):
> ```bash
> python models\multimodal\image_to_text\easy_ocr\python\_setup_arm64_wheels.py
> python -m pip install --no-deps easyocr
> python -m pip install -r models\multimodal\image_to_text\easy_ocr\python\requirements.txt
> ```
> If `easy_ocr.py` prints `[WARNING] EasyOCR is installed but a native
> dependency failed to load...`, it means `numpy` and `scipy` came from
> mismatched builds (scipy's `_fblas` extension can't find its OpenBLAS
> DLL) — re-run `_setup_arm64_wheels.py` to reinstall the matching `numpy`
> from the same bundle as `scipy`.

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model easy_ocr --args "--Image_Path path\to\image.png"
```

Or run directly:
```bash
python models\multimodal\image_to_text\easy_ocr\python\easy_ocr.py --Image_Path path\to\image.png
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--Image_Path` | `assets/ch_en.png` | Path to the input image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Model Download

Models are automatically downloaded on first run to `models/`:
- `easy_ocr_Detector.bin` — CRAFT text detector
- `easy_ocr_Recognizer.bin` — CRNN text recognizer
- `easy_ocr_CN_Detector.bin` — Chinese text detector
- `easy_ocr_CN_Recognizer.bin` — Chinese text recognizer

## Notes

- The character data files in `assets/Char/` are tracked in git (small binary files needed at runtime).
- SimSun font (`simsun.ttc`) is auto-downloaded for Chinese text rendering.
