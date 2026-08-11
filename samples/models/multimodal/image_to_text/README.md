# Image to Text

Models that extract text from images (OCR — Optical Character Recognition).

## Models

| Model | Task | Languages | AI Hub |
|-------|------|-----------|--------|
| [easy_ocr](easy_ocr/) | Image → detected text with bounding boxes | English + Chinese Simplified | [Link](https://aihub.qualcomm.com/compute/models/easy_ocr) |

EasyOCR uses a 2-stage pipeline:
1. **CRAFT Detector** — detects text regions
2. **CRNN Recognizer** — recognizes characters (6719-char vocabulary)

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model easy_ocr --args "--Image_Path path/to/image.png"
```
