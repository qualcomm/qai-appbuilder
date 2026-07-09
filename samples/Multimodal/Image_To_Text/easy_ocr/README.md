# EasyOCR — Optical Character Recognition on Snapdragon NPU

## Overview

**EasyOCR** is an optical character recognition (OCR) model that detects and recognizes text in images, running on the Snapdragon NPU (HTP) via QAI AppBuilder. It supports both English and Chinese (Simplified) text recognition using a two-stage pipeline (text detector + text recognizer).

- **Task**: Optical Character Recognition (OCR)
- **Languages**: English + Chinese (Simplified)
- **Input**: RGB image with text
- **Output**: Annotated image with detected text regions and recognized text
- **Platform**: Windows on Snapdragon (WoS), x86 Windows, ARM64 Linux, x86 Linux
- **Runtime**: HTP (Hexagon NPU)
- **AI Hub Models**: easy_ocr_Detector, easy_ocr_Recognizer, easy_ocr_CN_Detector, easy_ocr_CN_Recognizer

## Model Architecture

EasyOCR uses a two-stage pipeline:

| Stage | Model File | Description |
| ----- | ---------- | ----------- |
| Text Detector | `easy_ocr_EasyOCRDetector_Ch_En.bin` | CRAFT-based detector: image → text region heatmaps [1, 304, 400, 2] + features [1, 32, 304, 400] |
| Text Recognizer | `easy_ocr_EasyOCRRecognizer_Ch_En.bin` | CRNN-based recognizer: text crop → character probabilities [1, T, 6719] |

**Input size**: 608×800 (H×W)  
**Character set**: 6719 characters (English + Chinese Simplified)

## Requirements

```
pip install easyocr opencv-python
```

> **ARM64 Windows (Python 3.13+) note:** `easyocr` cannot be installed directly via `pip install easyocr` on ARM64 Windows because its dependency `Shapely` has no prebuilt ARM64 wheel. Install with:
> ```
> pip install scikit-image python-bidi pyclipper ninja imageio tifffile lazy-loader
> pip install easyocr --no-deps
> ```

## Quick Start

```bash
cd qai-appbuilder\samples
python Multimodal\Image_To_Text\easy_ocr\easy_ocr.py
```

With a custom image:
```bash
python Multimodal\Image_To_Text\easy_ocr\easy_ocr.py --Image_Path path\to\image.png
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--Image_Path` | `ch_en.png` (auto-downloaded) | Path to the input image |
| `--chipset` | Auto-detected | SoC ID for model download |

## Required Files

The following files must be present in the script directory:
- `Char/ch_en_character.bin` — character set (GB18030 encoded)
- `Char/ch_en_lang_char.bin` — language character set
- `simsun.ttc` — SimSun font for Chinese text rendering (auto-downloaded)

## Model Download

Models are automatically downloaded on first run:
- `easy_ocr_EasyOCRDetector_Ch_En.bin` — Chinese+English text detector
- `easy_ocr_EasyOCRRecognizer_Ch_En.bin` — Chinese+English text recognizer
- `simsun.ttc` — SimSun font for rendering Chinese characters

## Input/Output

| | Format | Description |
| - | ------ | ----------- |
| **Input** | Image file (JPG/PNG) | Any size, resized to 608×800 |
| **Output** | Annotated image | Green bounding boxes + recognized text overlay |

## Pipeline Details

```
Image file
    ↓ PIL.Image.open
    ↓ pil_resize_pad → 608×800
    ↓ reformat_input → numpy array
    ↓ resize_aspect_ratio (CRAFT preprocessing)
    ↓ normalizeMeanVariance
    ↓ Text Detector (NPU)
Feature map [1,32,304,400] + Score map [1,304,400,2]
    ↓ reshape + getDetBoxes (CRAFT post-processing)
    ↓ adjustResultCoordinates
    ↓ group_text_box → horizontal_list + free_list
    ↓ For each text region:
      ↓ crop + resize to [1, 1, 64, 1000]
      ↓ Text Recognizer (NPU)
      ↓ CTC greedy decode → text string
    ↓ draw bounding boxes (OpenCV)
    ↓ draw text (PIL + SimSun font)
    ↓ pil_undo_resize_pad → original size
Annotated image
```

## Example Output

```
Calling EasyOCR_Detector::Inference on NPU
Calling EasyOCR_Recognizer::Inference on NPU
Hello World
你好世界
```

The output image shows green bounding boxes around detected text regions with the recognized text displayed above each box.

## Notes

- The model uses greedy CTC decoding for Chinese text recognition.
- The recognizer input is padded to a fixed width of 1000 pixels.
- Chinese characters are rendered using the SimSun (宋体) font.
- For English-only OCR, the English-only models (`easy_ocr_EasyOCRDetector.bin`, `easy_ocr_EasyOCRRecognizer.bin`) can be used instead.
