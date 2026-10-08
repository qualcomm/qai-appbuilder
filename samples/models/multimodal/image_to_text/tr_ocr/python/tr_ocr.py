# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
import time
import zipfile
import argparse
import numpy as np

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Make the shared install helpers importable (shared/python/install.py).
sys.path.append(".")
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "..", "..", "..", "..", "shared", "python"))
import install

# ── Paths ──────────────────────────────────────────────────────────────────
BASE_DIR      = os.path.dirname(os.path.abspath(__file__))
MODEL_DIR     = os.path.abspath(os.path.join(BASE_DIR, "..", "models"))
ASSETS_DIR    = os.path.abspath(os.path.join(BASE_DIR, "..", "assets"))
TOKENIZER_DIR = os.path.join(BASE_DIR, "tokenizer")

# The model archive extracts to a "trocr-qnn_dlc-float/" subfolder containing
# encoder.dlc + decoder.dlc; the files are flattened directly into MODEL_DIR.
MODEL_ZIP_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/trocr/releases/v0.57.1/trocr-qnn_dlc-float.zip"
ENCODER_DLC   = os.path.join(MODEL_DIR, "encoder.dlc")
DECODER_DLC   = os.path.join(MODEL_DIR, "decoder.dlc")

# Default sample image used for inference.
SAMPLE_IMAGE_URL = "https://qaihub-public-assets.s3.us-west-2.amazonaws.com/qai-hub-models/models/trocr/v1/sample_text.jpg"
SAMPLE_IMAGE     = os.path.join(ASSETS_DIR, "sample_text.jpg")


def ensure_model():
    """Download + unzip the TrOCR DLCs into MODEL_DIR (via install.download_url).

    The archive nests its files under a "trocr-qnn_dlc-float/" folder; those
    files are flattened directly into MODEL_DIR so the .dlc files live at
    models/encoder.dlc and models/decoder.dlc.
    """
    if os.path.exists(ENCODER_DLC) and os.path.exists(DECODER_DLC):
        return

    os.makedirs(MODEL_DIR, exist_ok=True)
    zip_path = os.path.join(MODEL_DIR, os.path.basename(MODEL_ZIP_URL))

    print(f"[setup] Downloading TrOCR model from:\n        {MODEL_ZIP_URL}")
    ret = install.download_url(
        MODEL_ZIP_URL, zip_path,
        desc="Downloading TrOCR model (encoder.dlc + decoder.dlc) ...",
        fail=f"\nFailed to download the TrOCR model. Please download it manually from "
             f"{MODEL_ZIP_URL} and unzip it into {MODEL_DIR}.",
    )
    if not ret or not os.path.exists(zip_path):
        sys.exit(1)

    print(f"[setup] Extracting {os.path.basename(zip_path)} -> {MODEL_DIR}")
    import shutil
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            if member.is_dir():
                continue
            # Strip the leading archive folder so files land directly in MODEL_DIR.
            flat_name = os.path.basename(member.filename)
            if not flat_name:
                continue
            with zf.open(member) as src, open(os.path.join(MODEL_DIR, flat_name), "wb") as dst:
                shutil.copyfileobj(src, dst)

    if os.path.exists(zip_path):
        os.remove(zip_path)

    if not (os.path.exists(ENCODER_DLC) and os.path.exists(DECODER_DLC)):
        print(f"[setup] ERROR: encoder.dlc/decoder.dlc not found after extraction in {MODEL_DIR}")
        sys.exit(1)
    print(f"[setup] Model ready: {MODEL_DIR}")


def ensure_sample_image():
    """Download the default sample image into ASSETS_DIR (via install.download_url)."""
    if os.path.exists(SAMPLE_IMAGE):
        return

    os.makedirs(ASSETS_DIR, exist_ok=True)
    print(f"[setup] Downloading sample image from:\n        {SAMPLE_IMAGE_URL}")
    ret = install.download_url(
        SAMPLE_IMAGE_URL, SAMPLE_IMAGE,
        desc="Downloading TrOCR sample image ...",
        fail=f"\nFailed to download the sample image. Please download it manually from "
             f"{SAMPLE_IMAGE_URL} and place it at {SAMPLE_IMAGE}.",
    )
    if not ret or not os.path.exists(SAMPLE_IMAGE):
        sys.exit(1)
    print(f"[setup] Sample image ready: {SAMPLE_IMAGE}")


# ── Step 0: Ensure model + sample image are present ────────────────────────
print("[0/5] Preparing TrOCR model and sample image ...")
ensure_model()
ensure_sample_image()

# ── Constants (from metadata.json + getInputName() verification) ───────────
IMG_SIZE        = 384
NUM_LAYERS      = 6
NUM_HEADS       = 8
HEAD_DIM        = 32
CROSS_SEQ_LEN   = 578
SELF_KV_IN_LEN  = 19
MAX_GEN_TOKENS  = 20    # hard limit: decoder index range = [0..19]

# ── Step 1: Initialize QNN (MUST be first qai_appbuilder call) ─────────────
print("[1/5] Initializing QNN HTP backend ...")
from qai_appbuilder import QNNContext, QNNConfig, Runtime, LogLevel, ProfilingLevel
QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.OFF)
print("      QNN backend ready.")

# ── Step 2: Load tokenizer + image processor ───────────────────────────────
print("[2/5] Loading tokenizer ...")
import ssl
ssl._create_default_https_context = ssl._create_unverified_context
os.environ["TRANSFORMERS_VERBOSITY"] = "error"
from transformers import TrOCRProcessor

# The Qualcomm AI Hub DLCs are exported from microsoft/trocr-small-handwritten,
# which uses an XLMRoberta/SentencePiece tokenizer (vocab=64002) — NOT the
# roberta-large BPE vocab (50265) in the local ./tokenizer folder. Using the
# wrong vocab is what produced the garbled "by d the by d the ..." output.
HF_MODEL = "microsoft/trocr-small-handwritten"
processor = TrOCRProcessor.from_pretrained(HF_MODEL)
tokenizer = processor.tokenizer
image_processor = processor.image_processor

BOS_ID   = tokenizer.bos_token_id   # 0  (<s>)
EOS_ID   = tokenizer.eos_token_id   # 2  (</s>)
START_ID = EOS_ID                   # TrOCR decoder_start_token_id = 2 (</s>)
print(f"      vocab={tokenizer.vocab_size}  BOS={BOS_ID}  EOS={EOS_ID}  START={START_ID}")

# The exported encoder DLC bakes `pixel_values*2-1` internally and declares an
# input range of [0,1] (see model.py get_input_spec). The HF processor defaults
# to mean/std=0.5 which yields [-1,1] and would be double-normalized. Force the
# processor to emit raw [0,1] by setting mean=0, std=1.
image_processor.image_mean = [0.0, 0.0, 0.0]
image_processor.image_std  = [1.0, 1.0, 1.0]
image_processor.size = {"height": IMG_SIZE, "width": IMG_SIZE}

# ── Step 3: Load encoder + decoder on NPU ─────────────────────────────────
print("[3/5] Loading encoder.dlc (NPU) ...")
t0 = time.time()
encoder = QNNContext("trocr_encoder", ENCODER_DLC)
enc_in_names  = encoder.getInputName()
enc_out_names = encoder.getOutputName()
print(f"      Loaded in {(time.time()-t0)*1000:.0f} ms")
print(f"      Inputs : {enc_in_names}")
print(f"      Outputs: {enc_out_names}")

print("[3/5] Loading decoder.dlc (NPU) ...")
t0 = time.time()
decoder = QNNContext("trocr_decoder", DECODER_DLC)
dec_in_names  = decoder.getInputName()
dec_out_names = decoder.getOutputName()
print(f"      Loaded in {(time.time()-t0)*1000:.0f} ms")
print(f"      Inputs : {dec_in_names}")
print(f"      Outputs: {dec_out_names}")


# ── Step 4: Preprocessing ─────────────────────────────────────────────────
def crop_to_text(img):
    """
    Crop the image to the text's bounding box (with a small margin).

    TrOCR expects a single line of text that roughly fills the frame, because
    the encoder squashes any input into a fixed 384x384 square. An image with
    wide empty margins or a non-1:1 aspect ratio (e.g. a 600x120 banner) gets
    horizontally compressed, which distorts glyphs and makes the decoder drop
    repeated letters (e.g. "Qualcomm" -> "Qualcom"). Cropping to the ink first
    removes that distortion.
    """
    import numpy as _np
    gray = _np.asarray(img.convert("L"))
    # Text may be dark-on-light or light-on-dark; threshold at the midpoint.
    thr = (int(gray.max()) + int(gray.min())) / 2.0
    ink = gray < thr
    if not ink.any():                      # fully blank fallback: use whole image
        ink = gray > thr
    if not ink.any():
        return img
    xs = _np.where(ink.any(axis=0))[0]
    ys = _np.where(ink.any(axis=1))[0]
    x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
    pad = max(2, int(0.1 * (y1 - y0 + 1)))  # margin proportional to text height
    return img.crop((
        max(0, x0 - pad), max(0, y0 - pad),
        min(img.width,  x1 + 1 + pad), min(img.height, y1 + 1 + pad),
    ))


def preprocess_image(image_path: str) -> np.ndarray:
    """
    Load and preprocess image for TrOCR encoder.
    Returns float32 NHWC [1, 384, 384, 3], value range [0, 1].

    Steps: RGB -> crop to text bbox -> resize 384x384 (DeiTImageProcessor).
    Note: DeiTImageProcessor outputs NCHW [1,3,384,384]; must transpose to NHWC for QNN DLC.
    """
    from PIL import Image
    img = Image.open(image_path).convert("RGB")
    img = crop_to_text(img)
    result = image_processor(img, return_tensors="np")
    pv = result["pixel_values"]               # [1, 3, 384, 384] NCHW
    return pv.transpose(0, 2, 3, 1).astype(np.float32)  # [1, 384, 384, 3] NHWC


# ── Step 5: OCR inference ──────────────────────────────────────────────────
def ocr(image_path: str, verbose: bool = True) -> str:
    """
    Run TrOCR encoder-decoder on an image and return the recognized text.

    KV cache strategy (sliding window):
      - self-attn KV input:  [1, 8, 19, 32]  (past N tokens)
      - self-attn KV output: [1, 8, 20, 32]  (past N+1 tokens)
      - next input:          output[:, :, 1:, :]  (drop oldest, keep last 19)
    """
    if verbose:
        print(f"\n[OCR] Image: {image_path}")

    pixel_values = preprocess_image(image_path)
    if verbose:
        print(f"      pixel_values: shape={pixel_values.shape}  "
              f"min={pixel_values.min():.3f}  max={pixel_values.max():.3f}")

    # ── Encoder ──────────────────────────────────────────────────────────
    t_enc = time.time()
    enc_in_list = [{"pixel_values": pixel_values}[n] for n in enc_in_names]
    enc_outs = encoder.Inference(enc_in_list)
    enc_ms = (time.time() - t_enc) * 1000
    enc_out_dict = dict(zip(enc_out_names, enc_outs))

    if verbose:
        cross_norm = float(np.linalg.norm(np.array(enc_out_dict["kv_cache_key_0"])))
        print(f"      Encoder: {enc_ms:.0f} ms  (cross-KV[0] norm={cross_norm:.1f})")

    # ── Initialize decoder KV caches ────────────────────────────────────
    self_kv_key = [np.zeros((1, NUM_HEADS, SELF_KV_IN_LEN, HEAD_DIM), dtype=np.float32)
                   for _ in range(NUM_LAYERS)]
    self_kv_val = [np.zeros((1, NUM_HEADS, SELF_KV_IN_LEN, HEAD_DIM), dtype=np.float32)
                   for _ in range(NUM_LAYERS)]
    cross_kv_key = [np.array(enc_out_dict[f"kv_cache_key_{i}"], dtype=np.float32)
                    for i in range(NUM_LAYERS)]
    cross_kv_val = [np.array(enc_out_dict[f"kv_cache_val_{i}"], dtype=np.float32)
                    for i in range(NUM_LAYERS)]

    # ── Autoregressive decoding ──────────────────────────────────────────
    current_token = np.array([[START_ID]], dtype=np.int32)
    generated_ids = []
    t_dec = time.time()

    for step in range(MAX_GEN_TOKENS):
        index = np.array([step], dtype=np.int32)

        # Build input dict (use getInputName() order — Issue 12)
        dec_in_dict = {
            "index":    index,
            "input_ids": current_token,
        }
        for i in range(NUM_LAYERS):
            dec_in_dict[f"kv_{i}_attn_key"]       = self_kv_key[i]
            dec_in_dict[f"kv_{i}_attn_val"]        = self_kv_val[i]
            dec_in_dict[f"kv_{i}_cross_attn_key"]  = cross_kv_key[i]
            dec_in_dict[f"kv_{i}_cross_attn_val"]  = cross_kv_val[i]

        dec_in_list = [dec_in_dict[n] for n in dec_in_names]
        dec_outs = decoder.Inference(dec_in_list)

        if not dec_outs:
            if verbose:
                print(f"      [WARN] Decoder inference returned empty at step {step}")
            break

        dec_out_dict = dict(zip(dec_out_names, dec_outs))
        next_token_id = int(np.array(dec_out_dict["next_token"], dtype=np.int32).flat[0])

        if next_token_id == EOS_ID:
            if verbose:
                print(f"      EOS at step {step}")
            break

        generated_ids.append(next_token_id)
        current_token[0, 0] = next_token_id

        # Update self-attn KV (sliding window: drop oldest position)
        for i in range(NUM_LAYERS):
            new_k = np.array(dec_out_dict[f"kv_cache_key_{i}"], dtype=np.float32)  # [1,8,20,32]
            new_v = np.array(dec_out_dict[f"kv_cache_val_{i}"], dtype=np.float32)
            self_kv_key[i] = new_k[:, :, 1:, :]   # -> [1,8,19,32]
            self_kv_val[i] = new_v[:, :, 1:, :]

    dec_ms = (time.time() - t_dec) * 1000
    n_tokens = len(generated_ids)

    text = tokenizer.decode(generated_ids, skip_special_tokens=True).strip()

    if verbose:
        print(f"      Decoder: {dec_ms:.0f} ms  "
              f"({n_tokens} tokens, {dec_ms/max(n_tokens,1):.1f} ms/tok)")
        print(f"[OCR] Result: \"{text}\"")

    return text


# ── Main ──────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="TrOCR inference on Snapdragon X Elite NPU (QNN DLC)"
    )
    parser.add_argument(
        "--image",
        type=str,
        default=SAMPLE_IMAGE,
        help="Path to input image (default: assets/sample_text.jpg)"
    )
    args = parser.parse_args()

    print("\n[4/5] Models loaded. Starting OCR inference ...")
    print("=" * 60)

    result = ocr(args.image, verbose=True)

    print("\n" + "=" * 60)
    print("[5/5] TrOCR inference complete on Snapdragon X Elite NPU.")
    print(f"      Final OCR text: \"{result}\"")


if __name__ == "__main__":
    main()
