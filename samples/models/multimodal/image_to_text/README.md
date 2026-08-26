# Image to Text

Models that extract text from images (OCR — Optical Character Recognition).

## Models

| Model | Task | Languages | AI Hub |
|-------|------|-----------|--------|
| [easy_ocr](easy_ocr/) | Image → detected text with bounding boxes | English + Chinese Simplified | [Link](https://aihub.qualcomm.com/compute/models/easy_ocr) |
| [tr_ocr](tr_ocr/) | Single-line image → recognized text string | English (handwritten / printed) | [Link](https://aihub.qualcomm.com/compute/models/trocr) |

### EasyOCR
EasyOCR uses a 2-stage pipeline:
1. **CRAFT Detector** — detects text regions
2. **CRNN Recognizer** — recognizes characters (6719-char vocabulary)

### TrOCR
TrOCR is a Transformer encoder-decoder model (based on `microsoft/trocr-small-handwritten`) that reads a **single line** of handwritten or printed text and outputs the recognized string. Both stages run on the NPU as QNN DLCs:
1. **ViT-Base-384 Encoder** (`encoder.dlc`) — encodes a 384×384 image into cross-attention KV cache
2. **Autoregressive Text Decoder** (`decoder.dlc`) — generates up to 20 tokens using a sliding-window self-attention KV cache (6 layers × 8 heads × head_dim 32)

## Quick Start

```bash
cd qai-appbuilder\samples

# EasyOCR — multi-region detection + recognition
python run_inference.py --model easy_ocr --args "--Image_Path path/to/image.png"

# TrOCR — single-line handwritten / printed text
python run_inference.py --model tr_ocr --args "--image path/to/image.jpg"
```

