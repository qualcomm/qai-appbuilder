"""
infer_beit.py — BEiT Image Classification on Snapdragon X Elite (QNN DLC)

Model:    BEiT w8a16 QNN DLC
Input:    NHWC [1, 224, 224, 3] float32, range [0, 1]
Output:   [1, 1000] float32 logits
Labels:   ImageNet 1000 classes

Usage:
    python infer_beit.py [--input <image_path>] [--topk 5]

Defaults:
    --input   C:\\WoS_AI\\AutoCropV1\\test_image.jpg
    --topk    5

Requires:
    - ARM64 Python venv with qai_appbuilder installed
    - Model downloaded to C:\\WoS_AI\\BEiT\\
"""

import sys
import os
import argparse
import numpy as np
from PIL import Image

# ── stdout UTF-8 (avoid UnicodeEncodeError on Windows) ─────────────────────
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Paths ───────────────────────────────────────────────────────────────────
MODEL_PATH  = r"C:\WoS_AI\BEiT\beit-qualcomm_snapdragon_x_elite-qnn_dlc-w8a16\beit.dlc"
LABELS_PATH = r"C:\WoS_AI\BEiT\beit-qualcomm_snapdragon_x_elite-qnn_dlc-w8a16\labels.txt"
DEFAULT_IMG = r"C:\WoS_AI\AutoCropV1\test_image.jpg"

# ── CLI ─────────────────────────────────────────────────────────────────────
parser = argparse.ArgumentParser(description="BEiT inference via QNN DLC")
parser.add_argument("--input",  default=DEFAULT_IMG, help="Path to input image")
parser.add_argument("--topk",   type=int, default=5, help="Number of top predictions")
parser.add_argument("--model",  default=MODEL_PATH,  help="Path to .dlc model file")
parser.add_argument("--labels", default=LABELS_PATH, help="Path to labels.txt")
args = parser.parse_args()

# ── Load labels ─────────────────────────────────────────────────────────────
with open(args.labels, "r", encoding="utf-8") as f:
    labels = [line.strip() for line in f.readlines()]

# ── Preprocess image (NHWC, float32, [0,1]) ─────────────────────────────────
print(f"Loading image: {args.input}")
img = Image.open(args.input).convert("RGB").resize((224, 224))
arr = np.array(img, dtype=np.float32) / 255.0   # [224, 224, 3]
arr = arr[np.newaxis, ...]                        # [1, 224, 224, 3]
print(f"Input shape: {arr.shape}, dtype: {arr.dtype}, range: [{arr.min():.3f}, {arr.max():.3f}]")

# ── Load model ───────────────────────────────────────────────────────────────
print(f"\nLoading model: {args.model}")
print("(First load may take ~30-60s for graph compilation — this is normal)")
from qai_appbuilder import QNNContext

model = QNNContext("beit", args.model)

# ── Inference ────────────────────────────────────────────────────────────────
print("Running inference...")
outputs = model.Inference([arr])
logits = np.array(outputs[0])                    # [1, 1000]
print(f"Output shape: {logits.shape}")

# ── Softmax + Top-K ──────────────────────────────────────────────────────────
logits = logits.squeeze()                         # [1000]
exp_l  = np.exp(logits - logits.max())
probs  = exp_l / exp_l.sum()

top_idx = np.argsort(probs)[::-1][:args.topk]

print(f"\nTop-{args.topk} predictions:")
for rank, idx in enumerate(top_idx, 1):
    label = labels[idx] if idx < len(labels) else f"class_{idx}"
    print(f"  {rank}. {label:<40s} — {probs[idx]:.4f}")
