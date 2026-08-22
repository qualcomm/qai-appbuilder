# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
Object Detection QNN Inference Script (model-builder)

Specialized for object detection models (e.g., YOLOv8, SSD).
Input: image file → Output: annotated image with bounding boxes.

Verified on: Windows on Snapdragon (WoS) ARM64, Snapdragon X Elite (V73)
QAIRT SDK: <QAIRT SDK version> (see data/config/qairt_env.json "_version")

Supported output formats:
  - YOLO format: [batch, num_boxes, 4+num_classes] (cx,cy,w,h,scores...)
  - SSD format:  separate boxes + scores + classes tensors

Usage:
  # Basic detection
  python inference/infer_detect.py --model yolov8_det.bin --input image.jpg

  # With custom thresholds and labels
  python inference/infer_detect.py --model yolov8_det.bin --input image.jpg \\
    --labels coco_labels.txt --conf 0.45 --iou 0.7

  # Save annotated output
  python inference/infer_detect.py --model yolov8_det.bin --input image.jpg \\
    --output detected.jpg

  # Batch detection
  python inference/infer_detect.py --model model.bin \\
    --input_dir ./images --output_dir ./results

Args:
  --model:       Path to QNN context binary (.bin)
  --input:       Path to input image
  --input_dir:   Directory of input images (batch mode)
  --output:      Path to save annotated output image
  --output_dir:  Directory to save annotated images (batch mode)
  --labels:      Path to class labels file (.txt one-per-line or .json list)
  --input_size:  Input image size (default: auto from model, e.g. 640)
  --conf:        Confidence threshold (default: 0.45)
  --iou:         NMS IoU threshold (default: 0.7)
  --runtime:     QNN runtime: Htp (default) or Cpu
  --log_level:   Log level 0-5 (default: 1)
"""

import argparse
import json
import os
import sys

# UTF-8 safe stdout/stderr (model-builder): Windows consoles default to a
# legacy code page (GBK/cp1252) and crash with UnicodeEncodeError when a
# print() contains non-ASCII (e.g. emoji ✅ / 中文). Reconfigure here so this
# script is "copy-and-run" safe regardless of the console code page; this also
# removes any need for the fragile `set PYTHONUTF8=1 &&` cmd workaround.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from pathlib import Path

import numpy as np

try:
    from PIL import Image, ImageDraw, ImageFont
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

from qai_appbuilder import QNNContext, Runtime, LogLevel, ProfilingLevel, QNNConfig, PerfProfile


# ─── env_config.json auto-discovery ──────────────────────────────────────────

def _find_qairt_env_config() -> dict:
    current = Path(__file__).resolve().parent
    for _ in range(10):
        candidate = current / "data" / "config" / "qairt_env.json"
        if not candidate.exists():
            candidate = current / "config" / "qairt_env.json"
        if candidate.exists():
            try:
                with open(candidate, encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        parent = current.parent
        if parent == current:
            break
        current = parent
    return {}


def _apply_env_config(cfg: dict) -> None:
    if not cfg:
        return
    sdk_root = cfg.get("qairt_sdk_root", "")
    if sdk_root and not os.environ.get("QAIRT_SDK_ROOT"):
        os.environ["QAIRT_SDK_ROOT"] = sdk_root


# ─── COCO 80-class labels (default) ──────────────────────────────────────────

COCO_LABELS = [
    "person","bicycle","car","motorcycle","airplane","bus","train","truck","boat",
    "traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat",
    "dog","horse","sheep","cow","elephant","bear","zebra","giraffe","backpack",
    "umbrella","handbag","tie","suitcase","frisbee","skis","snowboard","sports ball",
    "kite","baseball bat","baseball glove","skateboard","surfboard","tennis racket",
    "bottle","wine glass","cup","fork","knife","spoon","bowl","banana","apple",
    "sandwich","orange","broccoli","carrot","hot dog","pizza","donut","cake","chair",
    "couch","potted plant","bed","dining table","toilet","tv","laptop","mouse",
    "remote","keyboard","cell phone","microwave","oven","toaster","sink","refrigerator",
    "book","clock","vase","scissors","teddy bear","hair drier","toothbrush",
]

# Box colors (BGR-like for PIL)
BOX_COLORS = [
    (255, 56, 56), (255, 157, 151), (255, 112, 31), (255, 178, 29),
    (207, 210, 49), (72, 249, 10), (146, 204, 23), (61, 219, 134),
    (26, 147, 52), (0, 212, 187), (44, 153, 168), (0, 194, 255),
    (52, 69, 147), (100, 115, 255), (0, 24, 236), (132, 56, 255),
    (82, 0, 133), (203, 56, 255), (255, 149, 200), (255, 55, 199),
]


def load_labels(labels_path: str) -> list:
    p = Path(labels_path)
    if p.suffix == ".json":
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else list(data.values())
    else:
        with open(p, encoding="utf-8") as f:
            return [line.strip() for line in f if line.strip()]


def preprocess_image(path: str, input_size: int = 640):
    """Load and resize image to square, return NHWC float32 tensor and original size."""
    img = Image.open(path).convert("RGB")
    orig_w, orig_h = img.size
    img_resized = img.resize((input_size, input_size), Image.LANCZOS)
    arr = np.array(img_resized, dtype=np.float32) / 255.0
    return arr[np.newaxis, ...], orig_w, orig_h  # (1, H, W, 3) NHWC


def nms(boxes, scores, iou_threshold=0.7):
    """Simple NMS implementation."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = (x2 - x1) * (y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(i)
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        w = np.maximum(0.0, xx2 - xx1)
        h = np.maximum(0.0, yy2 - yy1)
        inter = w * h
        iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-6)
        order = order[np.where(iou <= iou_threshold)[0] + 1]
    return keep


def parse_yolo_output(output, orig_w, orig_h, input_size, conf_thresh, iou_thresh):
    """
    Parse YOLO-style output: [1, num_boxes, 4+num_classes] or [1, 4+num_classes, num_boxes].
    Returns list of (x1, y1, x2, y2, score, class_id).
    """
    out = output.squeeze()  # Remove batch dim

    # Handle transposed format [4+nc, num_boxes] → [num_boxes, 4+nc]
    if out.ndim == 2 and out.shape[0] < out.shape[1]:
        out = out.T

    if out.ndim != 2:
        return []

    num_boxes, num_cols = out.shape
    if num_cols < 5:
        return []

    # cx, cy, w, h in [0,1] relative to input_size
    cx, cy, bw, bh = out[:, 0], out[:, 1], out[:, 2], out[:, 3]
    class_scores = out[:, 4:]

    # Get best class per box
    class_ids = class_scores.argmax(axis=1)
    scores = class_scores.max(axis=1)

    # Filter by confidence
    mask = scores >= conf_thresh
    if not mask.any():
        return []

    cx, cy, bw, bh = cx[mask], cy[mask], bw[mask], bh[mask]
    scores, class_ids = scores[mask], class_ids[mask]

    # Convert to pixel coords (scale to original image)
    scale_x = orig_w / input_size
    scale_y = orig_h / input_size
    x1 = (cx - bw / 2) * input_size * scale_x
    y1 = (cy - bh / 2) * input_size * scale_y
    x2 = (cx + bw / 2) * input_size * scale_x
    y2 = (cy + bh / 2) * input_size * scale_y

    boxes = np.stack([x1, y1, x2, y2], axis=1)
    keep = nms(boxes, scores, iou_thresh)

    results = []
    for i in keep:
        results.append((
            float(x1[i]), float(y1[i]), float(x2[i]), float(y2[i]),
            float(scores[i]), int(class_ids[i])
        ))
    return results


def draw_detections(image_path, detections, labels, output_path):
    """Draw bounding boxes on image and save."""
    img = Image.open(image_path).convert("RGB")
    draw = ImageDraw.Draw(img)

    for x1, y1, x2, y2, score, class_id in detections:
        color = BOX_COLORS[class_id % len(BOX_COLORS)]
        label = labels[class_id] if labels and class_id < len(labels) else f"cls_{class_id}"
        text = f"{label} {score:.2f}"

        # Draw box
        draw.rectangle([x1, y1, x2, y2], outline=color, width=2)

        # Draw label background
        try:
            font = ImageFont.load_default()
            bbox = draw.textbbox((x1, y1 - 15), text, font=font)
        except Exception:
            bbox = (x1, y1 - 15, x1 + len(text) * 6, y1)
        draw.rectangle(bbox, fill=color)
        draw.text((x1, y1 - 15), text, fill=(255, 255, 255))

    img.save(output_path)
    return img


class DetectModel(QNNContext):
    def Inference(self, input_data):
        return super().Inference(input_data)


# ─── Core inference ───────────────────────────────────────────────────────────

def run_detect(model_path, input_path, output_path=None, labels_path=None,
               input_size=None, conf=0.45, iou=0.7, runtime="Htp", log_level=1):
    """Run object detection on a single image."""
    _apply_env_config(_find_qairt_env_config())

    model_path = os.path.abspath(model_path)
    model_name = Path(model_path).stem

    rt = Runtime.HTP if runtime.lower() == "htp" else Runtime.CPU
    ll = [LogLevel.ERROR, LogLevel.WARN, LogLevel.INFO, LogLevel.VERBOSE,
          LogLevel.DEBUG, LogLevel.DEBUG][min(log_level, 5)]
    QNNConfig.Config(rt, ll, ProfilingLevel.OFF)

    print(f"[INFO] Loading model: {model_path}")
    model = DetectModel(model_name, model_path)

    input_shapes = model.getInputShapes()
    output_shapes = model.getOutputShapes()
    print(f"[INFO] Input  shapes: {input_shapes}")
    print(f"[INFO] Output shapes: {output_shapes}")

    # Detect input format: NCHW (1,C,H,W) or NHWC (1,H,W,C)
    input_is_nchw = False
    if input_shapes:
        shape = input_shapes[0]
        if len(shape) == 4:
            if shape[1] in (1, 3, 4) and shape[1] < shape[2]:
                input_is_nchw = True
                if input_size is None:
                    input_size = shape[2]
            else:
                if input_size is None:
                    input_size = shape[1]
    if input_size is None:
        input_size = 640
    print(f"[INFO] Input format: {'NCHW' if input_is_nchw else 'NHWC'}")

    labels = load_labels(labels_path) if labels_path else COCO_LABELS
    print(f"[INFO] Using {len(labels)} class labels")

    print(f"[INFO] Processing: {input_path}")
    inp, orig_w, orig_h = preprocess_image(input_path, input_size)
    if input_is_nchw:
        inp = np.transpose(inp, (0, 3, 1, 2))  # (1,H,W,C) -> (1,C,H,W)
    print(f"[INFO] Input tensor: {inp.shape}  original: {orig_w}x{orig_h}")

    print("[INFO] Running inference...")
    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    try:
        outputs = model.Inference([inp])
    finally:
        PerfProfile.RelPerfProfileGlobal()

    # Parse detections from first output tensor
    raw_out = np.array(outputs[0])
    detections = parse_yolo_output(raw_out, orig_w, orig_h, input_size, conf, iou)

    print(f"\n[Results] Detected {len(detections)} objects (conf>{conf}, iou<{iou}):")
    for i, (x1, y1, x2, y2, score, class_id) in enumerate(detections):
        label = labels[class_id] if class_id < len(labels) else f"cls_{class_id}"
        print(f"  [{i+1}] {label:<20s}  conf={score:.3f}  box=({x1:.0f},{y1:.0f},{x2:.0f},{y2:.0f})")

    if output_path is None:
        p = Path(input_path)
        output_path = str(p.parent / f"{p.stem}_det{p.suffix}")

    if detections:
        draw_detections(input_path, detections, labels, output_path)
        print(f"\n[DONE] Annotated image saved: {output_path}")
    else:
        print("\n[INFO] No detections above threshold.")

    return detections


def run_batch_detect(model_path, input_dir, output_dir, labels_path=None,
                     input_size=None, conf=0.45, iou=0.7, runtime="Htp", log_level=1):
    """Batch object detection."""
    _apply_env_config(_find_qairt_env_config())

    model_path = os.path.abspath(model_path)
    model_name = Path(model_path).stem
    os.makedirs(output_dir, exist_ok=True)

    rt = Runtime.HTP if runtime.lower() == "htp" else Runtime.CPU
    ll = [LogLevel.ERROR, LogLevel.WARN, LogLevel.INFO, LogLevel.VERBOSE,
          LogLevel.DEBUG, LogLevel.DEBUG][min(log_level, 5)]
    QNNConfig.Config(rt, ll, ProfilingLevel.OFF)

    print(f"[INFO] Loading model: {model_path}")
    model = DetectModel(model_name, model_path)

    input_shapes = model.getInputShapes()
    input_is_nchw = False
    if input_shapes:
        shape = input_shapes[0]
        if len(shape) == 4:
            if shape[1] in (1, 3, 4) and shape[1] < shape[2]:
                input_is_nchw = True
                if input_size is None:
                    input_size = shape[2]
            else:
                if input_size is None:
                    input_size = shape[1]
    if input_size is None:
        input_size = 640

    labels = load_labels(labels_path) if labels_path else COCO_LABELS

    exts = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
    images = sorted([f for f in Path(input_dir).iterdir() if f.suffix.lower() in exts])
    print(f"[INFO] Found {len(images)} images\n")

    PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    try:
        for i, img_path in enumerate(images):
            out_path = str(Path(output_dir) / f"{img_path.stem}_det{img_path.suffix}")
            print(f"[{i+1}/{len(images)}] {img_path.name}")
            try:
                inp, orig_w, orig_h = preprocess_image(str(img_path), input_size)
                if input_is_nchw:
                    inp = np.transpose(inp, (0, 3, 1, 2))
                outputs = model.Inference([inp])
                raw_out = np.array(outputs[0])
                detections = parse_yolo_output(raw_out, orig_w, orig_h, input_size, conf, iou)
                print(f"  -> {len(detections)} objects detected")
                if detections:
                    draw_detections(str(img_path), detections, labels, out_path)
            except Exception as e:
                print(f"  [ERROR] {e}")
    finally:
        PerfProfile.RelPerfProfileGlobal()

    print(f"\n[DONE] Batch detection complete. Results in: {output_dir}")


def main():
    parser = argparse.ArgumentParser(
        description="Object Detection QNN Inference (model-builder)")
    parser.add_argument("--model", required=True, help="Path to QNN context binary (.bin)")
    parser.add_argument("--input", default=None, help="Input image path")
    parser.add_argument("--input_dir", default=None, help="Input directory (batch mode)")
    parser.add_argument("--output", default=None, help="Output annotated image path")
    parser.add_argument("--output_dir", default=None, help="Output directory (batch mode)")
    parser.add_argument("--labels", default=None, help="Labels file (.txt or .json)")
    parser.add_argument("--input_size", type=int, default=None,
                        help="Input image size (default: auto from model)")
    parser.add_argument("--conf", type=float, default=0.45, help="Confidence threshold (default: 0.45)")
    parser.add_argument("--iou", type=float, default=0.7, help="NMS IoU threshold (default: 0.7)")
    parser.add_argument("--runtime", default="Htp", choices=["Htp", "Cpu"])
    parser.add_argument("--log_level", type=int, default=1)
    args = parser.parse_args()

    if not HAS_PIL:
        print("[ERROR] Pillow required. Install: pip install Pillow")
        sys.exit(1)

    if args.input_dir:
        out_dir = args.output_dir or (args.input_dir + "_det")
        run_batch_detect(args.model, args.input_dir, out_dir, args.labels,
                         args.input_size, args.conf, args.iou, args.runtime, args.log_level)
    elif args.input:
        run_detect(args.model, args.input, args.output, args.labels,
                   args.input_size, args.conf, args.iou, args.runtime, args.log_level)
    else:
        parser.error("Provide --input (single image) or --input_dir (batch mode)")


if __name__ == "__main__":
    main()
