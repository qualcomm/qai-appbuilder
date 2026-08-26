# TrOCR — Handwritten / Printed Text Recognition on Snapdragon NPU

## Overview

**TrOCR** is a Transformer-based OCR model that reads a single line of text (handwritten or printed) from an image, running end-to-end on the Snapdragon NPU (HTP) via QAI AppBuilder.

- **Task**: Optical Character Recognition (single-line, image → text string)
- **Base Model**: [`microsoft/trocr-small-handwritten`](https://huggingface.co/microsoft/trocr-small-handwritten) (encoder-decoder)
- **Input**: RGB image (any size — cropped and resized to 384×384 internally)
- **Output**: Recognized text string
- **Platform**: Windows on Snapdragon (WoS), Snapdragon X Elite / X2 Elite
- **Runtime**: HTP (Hexagon NPU) via `qai_appbuilder.QNNContext`
- **Format**: QNN DLC (`encoder.dlc` + `decoder.dlc`)
- **AI Hub Model**: [trocr](https://aihub.qualcomm.com/compute/models/trocr)

## Model Architecture

Two-stage encoder-decoder pipeline (both stages run on the NPU):

| Stage | Model | Description |
| ----- | ----- | ----------- |
| 1. Image Encoder | ViT-Base-384 (`encoder.dlc`) | Encodes the 384×384 image into cross-attention KV cache |
| 2. Text Decoder  | Autoregressive Transformer (`decoder.dlc`) | Generates tokens one-by-one from the encoder KV + past tokens |

### Encoder (`encoder.dlc`)
- **Input**: `pixel_values` — `[1, 384, 384, 3]` float32, **NHWC**, value range `[0, 1]`
  (the exported DLC bakes `pixel_values * 2 - 1` internally, so the processor must emit raw `[0,1]` with mean=0, std=1)
- **Output**: `kv_cache_key_{0..5}`, `kv_cache_val_{0..5}` — each `[1, 8, 578, 32]` float32 (cross-attention KV consumed by every decoder step)

### Decoder (`decoder.dlc`) — autoregressive, 6 layers × 8 heads × head_dim 32
- **Inputs** (in `getInputName()` order — `index` comes **before** `input_ids`):
  - `index` `[1]` int32 — current decode step (0..19)
  - `input_ids` `[1, 1]` int32 — current token id
  - `kv_{i}_attn_key` / `kv_{i}_attn_val` `[1, 8, 19, 32]` — self-attention KV (past tokens)
  - `kv_{i}_cross_attn_key` / `kv_{i}_cross_attn_val` `[1, 8, 578, 32]` — cross-attention KV from encoder
- **Outputs**:
  - `next_token` `[1]` int32 — predicted next token (argmax is baked in)
  - `kv_cache_key_{i}` / `kv_cache_val_{i}` `[1, 8, 20, 32]` — updated self-attention KV

### Key implementation notes
- **KV cache sliding window**: decoder outputs `[1,8,20,32]`; drop the oldest position (`[:, :, 1:, :]`) to produce the next `[1,8,19,32]` input.
- **Max generation length**: **20 tokens** (hard limit enforced by the KV cache size / `index` range 0..19).
- **Tokenizer**: XLMRoberta / SentencePiece, `vocab=64002`, `BOS=0`, `EOS=2`, `PAD=1` — loaded from `microsoft/trocr-small-handwritten` via `TrOCRProcessor` (do **not** use a local `roberta-large` BPE vocab — wrong vocab size 50265 produces garbled output).
- **Decoder start token**: `EOS_ID` (2), not BOS — this matches `decoder_start_token_id = 2` in the HF config.
- **Input layout**: `DeiTImageProcessor` outputs NCHW `[1, 3, 384, 384]`; the script transposes to NHWC before feeding the DLC.
- **Text cropping**: the encoder squashes any aspect ratio into a fixed 384×384 square, so the script first crops the image to the text's bounding box (with a small margin) to avoid horizontal compression of glyphs.

## Requirements

```
pip install transformers Pillow numpy sentencepiece
```

`qai_appbuilder` must already be installed as part of the QAI AppBuilder samples environment.

## Quick Start

```bash
cd qai-appbuilder\samples
python run_inference.py --model tr_ocr
```

With a custom image:

```bash
python run_inference.py --model tr_ocr --args "--image path\to\image.jpg"
```

Or run the script directly:

```bash
python models\multimodal\image_to_text\tr_ocr\python\tr_ocr.py --image path\to\image.jpg
```

## Arguments

| Argument | Default | Description |
| -------- | ------- | ----------- |
| `--image` | `assets/sample_text.jpg` | Path to the input image (any size, RGB) |

## Model Download

On first run the script auto-downloads:

- **`trocr-qnn_dlc-float.zip`** from `qaihub-public-assets.s3.us-west-2.amazonaws.com` — extracted (flattened) into `models/` as:
  - `models/encoder.dlc`
  - `models/decoder.dlc`
- **Sample image** (`sample_text.jpg`) into `assets/`.
- **Tokenizer / image processor** from Hugging Face (`microsoft/trocr-small-handwritten`) — cached under the standard `~/.cache/huggingface/` path.

## Pipeline Summary

The script prints a 5-step progress trace:

1. `[0/5]` Ensure `encoder.dlc` + `decoder.dlc` + sample image are present (auto-download if missing).
2. `[1/5]` Initialize QNN HTP backend.
3. `[2/5]` Load `TrOCRProcessor` (tokenizer + `DeiTImageProcessor`, forced to mean=0/std=1).
4. `[3/5]` Load `encoder.dlc` and `decoder.dlc` onto the NPU.
5. `[4/5]`–`[5/5]` Preprocess → encoder → autoregressive decode (up to 20 tokens, stop on EOS) → print recognized text.

## Notes

- Best results are on **single-line** handwritten or printed text. Multi-line images should be split into lines first.
- The 20-token cap comes from the exported DLC's fixed KV cache size and cannot be raised without re-exporting the model.
- If you see garbled output like `"by d the by d the ..."`, it usually means a wrong tokenizer (BPE 50265 instead of SentencePiece 64002) is being loaded — verify the `[2/5]` line reports `vocab=64002`.

