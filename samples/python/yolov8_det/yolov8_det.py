# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import sys
import os
import time
sys.path.append(".")
sys.path.append("python")
import utils.install as install
import cv2
import numpy as np
import torch
import torchvision.transforms as transforms
from PIL import Image
from PIL.Image import fromarray as ImageFromArray
from torch.nn.functional import interpolate, pad
from torchvision.ops import nms
from typing import List, Tuple, Optional, Union, Callable
import argparse
import urllib.request
import shutil
import ssl

from qai_appbuilder import (QNNContext, OnnxRuntimeContext, Runtime, LogLevel, ProfilingLevel, PerfProfile, QNNConfig)
from pathlib import Path

####################################################################

MODEL_ID = "mqp35e9lm"
MODEL_NAME = "yolov8_det"
MODEL_HELP_URL = "https://github.com/qualcomm/qai-appbuilder/tree/main/samples/python/" + MODEL_NAME + "#" + MODEL_NAME + "-qnn-models"
IMAGE_SIZE = 640

# URL of the official YOLOv8n pre-trained weights.
YOLOV8N_PT_URL = (
    "https://github.com/ultralytics/assets/releases/download/v8.2.0/yolov8n.pt"
)

####################################################################

execution_ws = Path(os.getcwd())
qnn_dir = execution_ws / "qai_libs"

if not "python" in str(execution_ws):
    execution_ws = execution_ws / "python"

if not MODEL_NAME in str(execution_ws):
    execution_ws = execution_ws / MODEL_NAME

model_dir = execution_ws / "models"
model_path = model_dir /  "{}.bin".format(MODEL_NAME)

####################################################################

yolov8 = None

nms_score_threshold: float = 0.45
nms_iou_threshold: float = 0.7

# define class type
class_map = {
    0: "person",
    1: "bicycle",
    2: "car",
    3: "motorcycle",
    4: "airplane",
    5: "bus",
    6: "train",
    7: "truck",
    8: "boat",
    9: "traffic light",
    10: "fire hydrant",
    11: "stop sign",
    12: "parking meter",
    13: "bench",
    14: "bird",
    15: "cat",
    16: "dog",
    17: "horse",
    18: "sheep",
    19: "cow",
    20: "elephant",
    21: "bear",
    22: "zebra",
    23: "giraffe",
    24: "backpack",
    25: "umbrella",
    26: "handbag",
    27: "tie",
    28: "suitcase",
    29: "frisbee",
    30: "skis",
    31: "snowboard",
    32: "sports ball",
    33: "kite",
    34: "baseball bat",
    35: "baseball glove",
    36: "skateboard",
    37: "surfboard",
    38: "tennis racket",
    39: "bottle",
    40: "wine glass",
    41: "cup",
    42: "fork",
    43: "knife",
    44: "spoon",
    45: "bowl",
    46: "banana",
    47: "apple",
    48: "sandwich",
    49: "orange",
    50: "broccoli",
    51: "carrot",
    52: "hot dog",
    53: "pizza",
    54: "donut",
    55: "cake",
    56: "chair",
    57: "couch",
    58: "potted plant",
    59: "bed",
    60: "dining table",
    61: "toilet",
    62: "tv",
    63: "laptop",
    64: "mouse",
    65: "remote",
    66: "keyboard",
    67: "cell phone",
    68: "microwave",
    69: "oven",
    70: "toaster",
    71: "sink",
    72: "refrigerator",
    73: "book",
    74: "clock",
    75: "vase",
    76: "scissors",
    77: "teddy bear",
    78: "hair drier",
    79: "toothbrush"
}

def preprocess_PIL_image(image: Image) -> torch.Tensor:
    """Convert a PIL image into a pyTorch tensor with range [0, 1] and shape NCHW."""
    transform = transforms.Compose([transforms.PILToTensor()])  # bgr image
    img: torch.Tensor = transform(image)  # type: ignore
    img = img.float().unsqueeze(0) / 255.0  # int 0 - 255 to float 0.0 - 1.0
    return img

def torch_tensor_to_PIL_image(data: torch.Tensor) -> Image:
    """
    Convert a Torch tensor (dtype float32) with range [0, 1] and shape CHW into PIL image CHW
    """
    out = torch.clip(data, min=0.0, max=1.0)
    np_out = (out.permute(1, 2, 0).detach().numpy() * 255).astype(np.uint8)
    return ImageFromArray(np_out)

def batched_nms(
    iou_threshold: float,
    score_threshold: float,
    boxes: torch.Tensor,
    scores: torch.Tensor,
    *gather_additional_args,
) -> Tuple[List[torch.Tensor], ...]:
    """
    Non maximum suppression over several batches.

    Inputs:
        iou_threshold: float
            Intersection over union (IoU) threshold

        score_threshold: float
            Score threshold (throw away any boxes with scores under this threshold)

        boxes: torch.Tensor
            Boxes to run NMS on. Shape is [B, N, 4], B == batch, N == num boxes, and 4 == (x1, x2, y1, y2)

        scores: torch.Tensor
            Scores for each box. Shape is [B, N], range is [0:1]

        *gather_additional_args: torch.Tensor, ...
            Additional tensor(s) to be gathered in the same way as boxes and scores.
            In other words, each arg is returned with only the elements for the boxes selected by NMS.
            Should be shape [B, N, ...]

    Outputs:
        boxes_out: List[torch.Tensor]
            Output boxes. This is list of tensors--one tensor per batch.
            Each tensor is shape [S, 4], where S == number of selected boxes, and 4 == (x1, x2, y1, y2)

        boxes_out: List[torch.Tensor]
            Output scores. This is list of tensors--one tensor per batch.
            Each tensor is shape [S], where S == number of selected boxes.

        *args : List[torch.Tensor], ...
            "Gathered" additional arguments, if provided.
    """
    scores_out: List[torch.Tensor] = []
    boxes_out: List[torch.Tensor] = []
    args_out: List[List[torch.Tensor]] = (
        [[] for _ in gather_additional_args] if gather_additional_args else []
    )

    for batch_idx in range(0, boxes.shape[0]):
        # Clip outputs to valid scores
        batch_scores = scores[batch_idx]
        scores_idx = torch.nonzero(scores[batch_idx] >= score_threshold).squeeze(-1)
        batch_scores = batch_scores[scores_idx]
        batch_boxes = boxes[batch_idx, scores_idx]
        batch_args = (
            [arg[batch_idx, scores_idx] for arg in gather_additional_args]
            if gather_additional_args
            else []
        )

        if len(batch_scores > 0):
            nms_indices = nms(batch_boxes[..., :4], batch_scores, iou_threshold)
            batch_boxes = batch_boxes[nms_indices]
            batch_scores = batch_scores[nms_indices]
            batch_args = [arg[nms_indices] for arg in batch_args]

        boxes_out.append(batch_boxes)
        scores_out.append(batch_scores)
        for arg_idx, arg in enumerate(batch_args):
            args_out[arg_idx].append(arg)

    return boxes_out, scores_out, *args_out

def draw_box_from_xyxy(
    frame: np.ndarray,
    top_left: np.ndarray | torch.Tensor | Tuple[int, int],
    bottom_right: np.ndarray | torch.Tensor | Tuple[int, int],
    color: Tuple[int, int, int] = (0, 0, 0),
    size: int = 3,
    text: Optional[str] = None,
):
    """
    Draw a box using the provided top left / bottom right points to compute the box.

    Parameters:
        frame: np.ndarray
            np array (H W C x uint8, BGR)

        box: np.ndarray | torch.Tensor
            array (4), where layout is
                [xc, yc, h, w]

        color: Tuple[int, int, int]
            Color of drawn points and connection lines (RGB)

        size: int
            Size of drawn points and connection lines BGR channel layout

        text: None | str
            Overlay text at the top of the box.

    Returns:
        None; modifies frame in place.
    """
    if not isinstance(top_left, tuple):
        top_left = (int(top_left[0].item()), int(top_left[1].item()))
    if not isinstance(bottom_right, tuple):
        bottom_right = (int(bottom_right[0].item()), int(bottom_right[1].item()))
    cv2.rectangle(frame, top_left, bottom_right, color, size)
    if text is not None:
        cv2.putText(
            frame,
            text,
            (top_left[0], top_left[1] - 10),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.5,
            color,
            size,
        )

# YoloV8 class which inherited from the class QNNContext.
class YoloV8(QNNContext):
    def Inference(self, input_data):
        input_datas=[input_data]
        output_data = super().Inference(input_datas)
        return output_data

# ─────────────────────────────────────────────────────────────────────────────
# ONNX model download and conversion
# ─────────────────────────────────────────────────────────────────────────────

def download_and_convert_onnx_model(quantize: str = "fp16"):
    """
    1. Download ``yolov8n.pt`` from the official Ultralytics GitHub release
       (``YOLOV8N_PT_URL``) into the ``models/`` sub-directory.
    2. Export the weights to ONNX using the *ultralytics* package.
    3. Copy the exported file to ``models/yolov8_det.onnx``.

    Parameters
    ----------
    quantize : str
        Numeric format for the exported ONNX model.
        ``"fp16"``  – half-precision float (default, smaller & faster on NPU).
        ``"int8"``  – 8-bit integer quantisation (smallest, fastest on NPU).
        Any other value falls back to full float32.

    Returns
    -------
    bool
        ``True`` on success, ``False`` on failure.
    """
    import subprocess

    onnx_model_path = model_dir / "yolov8_det.onnx"

    # ────────────────────────────────────────────────────────────────────────
    # Fast-path: ONNX model already present
    # ────────────────────────────────────────────────────────────────────────
    if onnx_model_path.exists():
        print(f"[OK] ONNX model already exists: {onnx_model_path}")
        return True

    # ────────────────────────────────────────────────────────────────────────
    # Ensure ultralytics is available
    # ────────────────────────────────────────────────────────────────────────
    try:
        from ultralytics import YOLO  # type: ignore
    except ImportError:
        print("[INFO] 'ultralytics' package not found – installing …")
        ret = subprocess.run(
            [sys.executable, "-m", "pip", "install", "ultralytics"],
            capture_output=True, text=True
        )
        if ret.returncode != 0:
            print(f"[FAIL] Could not install ultralytics:\n{ret.stderr}")
            return False
        from ultralytics import YOLO  # type: ignore

    # ────────────────────────────────────────────────────────────────────────
    # Ensure onnx / onnxruntime / onnxconverter-common are available.
    # 'onnxconverter-common' provides the float16 conversion helper used
    # for FP16 export (keeps FP32 I/O so the existing inference path works
    # unchanged), and 'onnxruntime' provides the INT8 quantizer.
    # ────────────────────────────────────────────────────────────────────────
    for pkg in ("onnx", "onnxruntime", "onnxconverter-common"):
        import_name = pkg.replace("-", "_")
        try:
            __import__(import_name)
        except ImportError:
            print(f"[INFO] '{pkg}' not found – installing …")
            subprocess.run(
                [sys.executable, "-m", "pip", "install", pkg],
                capture_output=True, text=True
            )

    # ────────────────────────────────────────────────────────────────────────
    # Create models directory
    # ────────────────────────────────────────────────────────────────────────
    model_dir.mkdir(parents=True, exist_ok=True)

    # ────────────────────────────────────────────────────────────────────────
    # Step 1 – Download yolov8n.pt from the official Ultralytics release
    # ────────────────────────────────────────────────────────────────────────
    pt_path = model_dir / "yolov8n.pt"

    if pt_path.exists():
        print(f"[OK] YOLOv8n weights already cached: {pt_path}")
    else:
        print(f"[INFO] Downloading YOLOv8n weights from:\n       {YOLOV8N_PT_URL}")

        # Progress hook for urllib.request.urlretrieve
        def _reporthook(blocknum, blocksize, totalsize):
            downloaded = blocknum * blocksize
            if totalsize > 0:
                pct = min(downloaded * 100.0 / totalsize, 100.0)
                mb_done = downloaded / 1024 / 1024
                mb_total = totalsize / 1024 / 1024
                print(
                    f"\r[INFO]   {pct:5.1f}%  {mb_done:.1f} / {mb_total:.1f} MB",
                    end="", flush=True,
                )

        # Use an SSL context that tolerates self-signed / missing certs on
        # some Windows ARM64 environments.
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        try:
            import urllib.request as _urlreq
            opener = _urlreq.build_opener(
                _urlreq.HTTPSHandler(context=ssl_ctx)
            )
            _urlreq.install_opener(opener)
            _urlreq.urlretrieve(YOLOV8N_PT_URL, str(pt_path), _reporthook)
            print()  # newline after progress bar
        except Exception as exc:
            print(f"\n[FAIL] Download failed: {exc}")
            if pt_path.exists():
                pt_path.unlink()
            return False

        if not pt_path.exists() or pt_path.stat().st_size < 1_000_000:
            print("[FAIL] Downloaded file is missing or too small.")
            if pt_path.exists():
                pt_path.unlink()
            return False

        print(f"[OK] Weights saved: {pt_path}  "
              f"({pt_path.stat().st_size / 1024 / 1024:.1f} MB)")

    # ────────────────────────────────────────────────────────────────────────
    # Step 2 – Load weights with ultralytics and export to ONNX
    # ────────────────────────────────────────────────────────────────────────
    print(f"[INFO] Loading YOLOv8n weights from {pt_path} …")
    try:
        yolo_model = YOLO(str(pt_path))
    except Exception as exc:
        print(f"[FAIL] Could not load YOLOv8n weights: {exc}")
        return False

    quantize = quantize.lower().strip()
    use_half = quantize == "fp16"
    use_int8 = quantize == "int8"

    # Always export a plain FP32 ONNX first. We post-process it into FP16 /
    # INT8 afterwards. This is more reliable than ultralytics' built-in
    # half=/int8= flags which require CUDA (half) or emit TFLite (int8) and
    # would change the model I/O dtype (breaking HTP inference which feeds
    # float32 NCHW tensors).
    print(f"[INFO] Exporting YOLOv8n → ONNX (FP32 base graph) …")
    try:
        exported_path = yolo_model.export(
            format="onnx",
            imgsz=IMAGE_SIZE,
            opset=12,
            simplify=True,
            dynamic=False,
        )
        exported_path = Path(exported_path)
    except Exception as exc:
        print(f"[FAIL] ONNX export failed: {exc}")
        return False

    if not exported_path.exists():
        print(f"[FAIL] Exported file not found at: {exported_path}")
        return False

    # ────────────────────────────────────────────────────────────────────────
    # Step 2b – Quantise / convert the FP32 ONNX graph to FP16 or INT8.
    # The result is written directly to models/yolov8_det.onnx.
    # ────────────────────────────────────────────────────────────────────────
    try:
        if use_half:
            print("[INFO] Converting ONNX weights to FP16 …")
            import onnx
            from onnxconverter_common import float16

            fp32_model = onnx.load(str(exported_path))
            # keep_io_types=True keeps graph inputs/outputs as float32 so the
            # existing inference code (feeding float32 NCHW) works unchanged,
            # while internal weights/compute run in FP16 on the HTP.
            fp16_model = float16.convert_float_to_float16(
                fp32_model, keep_io_types=True
            )
            onnx.save(fp16_model, str(onnx_model_path))

        elif use_int8:
            print("[INFO] Quantising ONNX model to INT8 (dynamic) …")
            from onnxruntime.quantization import quantize_dynamic, QuantType

            quantize_dynamic(
                model_input=str(exported_path),
                model_output=str(onnx_model_path),
                weight_type=QuantType.QInt8,
            )

        else:  # fp32 / fallback
            print("[INFO] Keeping ONNX model in FP32 …")
            shutil.copy2(str(exported_path), str(onnx_model_path))

    except Exception as exc:
        print(f"[FAIL] {quantize.upper()} conversion failed: {exc}")
        print("[INFO] Falling back to FP32 ONNX model.")
        try:
            shutil.copy2(str(exported_path), str(onnx_model_path))
        except Exception as exc2:
            print(f"[FAIL] Could not copy fallback model: {exc2}")
            return False

    if not onnx_model_path.exists():
        print(f"[FAIL] Final ONNX model not found at: {onnx_model_path}")
        return False

    print(f"[OK] ONNX model saved to : {onnx_model_path}")
    print(f"     Format              : {quantize.upper()}")
    print(f"     Size                : {onnx_model_path.stat().st_size / 1024 / 1024:.1f} MB")

    return True


def download_fp16_onnx_model():
    """Backward-compatible wrapper – downloads & converts to FP16 ONNX."""
    return download_and_convert_onnx_model(quantize="fp16")

# ─────────────────────────────────────────────────────────────────────────────
# ONNX / OnnxRuntimeContext init path (WoS HTP via onnxruntime_qnn)
# ─────────────────────────────────────────────────────────────────────────────

def _init_onnx_htp(onnx_path: Path):
    """Initialise inference using OnnxRuntimeContext (onnxruntime_qnn HTP EP).

    OnnxRuntimeContext (from qai_appbuilder.qnncontext) automatically:
      - Imports onnxruntime_qnn and registers the QNN Execution Provider.
      - Selects the HTP (NPU) backend via QAI_ORTQNN_BACKEND=htp (default).
      - Enables FP16 precision on HTP (enable_htp_fp16_precision=1).
      - Falls back to EPContext precompile workaround on layout-transform errors.
      - Falls back to CPU if QNN EP is unavailable.

    Environment variables that influence behaviour (all optional):
      QAI_ORT_ONNX_USE_ORTQNN   : "1" (default) to use onnxruntime_qnn
      QAI_ORTQNN_BACKEND         : "htp" (default) | "cpu" | "gpu"
      QAI_ORTQNN_ENABLE_HTP_FP16 : "1" (default) to enable FP16 precision
      QAI_ORTQNN_TRY_CONTEXT_CACHE : "1" (default) to retry with EPContext
    """
    global yolov8

    if not onnx_path.is_file():
        print(f"[INFO] ONNX model not found: {onnx_path}")
        print(f"[INFO] Attempting to download & convert YOLOv8 ONNX model (format=fp16) …")

        # Download YOLOv8 weights and export to ONNX format.
        if not download_and_convert_onnx_model(quantize="fp16"):
            print(f"[FAIL] Could not obtain ONNX model")
            sys.exit(1)

    # Ensure onnxruntime_qnn is used (OnnxRuntimeContext checks this env var).
    os.environ.setdefault("QAI_ORT_ONNX_USE_ORTQNN", "1")
    # Default backend: HTP (NPU).
    os.environ.setdefault("QAI_ORTQNN_BACKEND", "htp")
    # Enable FP16 precision on HTP for better performance.
    os.environ.setdefault("QAI_ORTQNN_ENABLE_HTP_FP16", "1")
    # Allow EPContext precompile workaround on layout-transform failures.
    os.environ.setdefault("QAI_ORTQNN_TRY_CONTEXT_CACHE", "1")

    print(f"[INFO] Loading ONNX model via OnnxRuntimeContext: {onnx_path}")
    yolov8 = OnnxRuntimeContext(MODEL_NAME, str(onnx_path))

    provider_mode = yolov8.getProviderMode()
    print(f"[INFO] OnnxRuntimeContext provider mode: {provider_mode}")
    if provider_mode != "qnn-htp":
        print("[WARN] Model is NOT running on HTP (NPU). "
              "Check that onnxruntime_qnn is installed and the QNN EP is available.")

def model_download():
    ret = True

    desc = f"Downloading {MODEL_NAME} model... "
    fail = f"\nFailed to download {MODEL_NAME} model. Please prepare the model according to the steps in below link:\n{MODEL_HELP_URL}"
    ret = install.download_qai_hubmodel(MODEL_ID, model_path, desc=desc, fail=fail)

    if not ret:
        exit()

def Init(use_onnx: bool = False):
    """Initialise the runtime and load the model.

    Parameters
    ----------
    use_onnx  : Use models/yolov8_det.onnx via OnnxRuntimeContext
                (onnxruntime_qnn HTP EP).  On WoS this is the default when
                the ONNX file exists.
    """
    global yolov8

    model_dir.mkdir(parents=True, exist_ok=True)

    # ── WoS HTP + ONNX: default path when ONNX model exists ──────────────────
    onnx_path = model_dir / f"{MODEL_NAME}.onnx"
    if use_onnx:
        print("[INFO] Runtime: onnxruntime_qnn via OnnxRuntimeContext")
        _init_onnx_htp(onnx_path)
        return

    model_download()

    # Config AppBuilder environment.
    QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
    print(f"model_path:{model_path}")
    # Instance for YoloV8 objects.
    yolov8 = YoloV8("yolov8", str(model_path))

def Inference(input_image_path, output_image_path, show_image = True):
    global image_buffer, nms_iou_threshold, nms_score_threshold

    # Read and preprocess the image.
    image = Image.open(input_image_path)
    image = image.resize((IMAGE_SIZE, IMAGE_SIZE))
    outputImg = Image.open(input_image_path)
    outputImg = outputImg.resize((IMAGE_SIZE, IMAGE_SIZE))
    image = preprocess_PIL_image(image) # transfer raw image to torch tensor format (NCHW)
    # For ONNX models, keep NCHW format; for QNN models, permute to NHWC
    is_onnx = isinstance(yolov8, OnnxRuntimeContext) or (
        hasattr(yolov8, "isOnnxModel") and yolov8.isOnnxModel()
    )
    if not is_onnx:
        image = image.permute(0, 2, 3, 1)
    image = image.numpy()

    output_image = np.array(outputImg.convert("RGB"))  # transfer to numpy array

    if not is_onnx:
        # Burst the HTP.
        PerfProfile.SetPerfProfileGlobal(PerfProfile.BURST)
    t0 = time.perf_counter()
    if is_onnx:
        # OnnxRuntimeContext.Inference(input_list) -> list of output arrays
        model_output = yolov8.Inference([image])
    else:
        # YoloV8.Inference(input_data) wraps input in a list internally
        # and returns the list of output arrays directly.
        model_output = yolov8.Inference(image)
    elapsed = time.perf_counter() - t0
    print(f"[INFO] Inference time: {elapsed * 1000:.1f} ms")
    print(f"[DEBUG] model_output type: {type(model_output)}, len: {len(model_output) if hasattr(model_output, '__len__') else 'N/A'}")
    if hasattr(model_output, '__len__'):
        for i, output in enumerate(model_output):
            if hasattr(output, 'shape'):
                print(f"[DEBUG] Output {i} shape: {output.shape}")

    # Post-process the raw output (1, 84, 8400)
    # Both ONNX and QNN models return: (batch, 84, 8400) where 84 = 4 box coords + 80 class scores
    if len(model_output) == 1:
        # Raw output format: (batch, 84, 8400)
        raw_output = model_output[0]  # (1, 84, 8400)
        raw_output = torch.tensor(raw_output).permute(0, 2, 1)  # (1, 8400, 84)

        # YOLOv8 box format is cx, cy, w, h — convert to x1, y1, x2, y2
        cxcywh = raw_output[..., :4]  # (1, 8400, 4)
        cx, cy, w, h = cxcywh[..., 0], cxcywh[..., 1], cxcywh[..., 2], cxcywh[..., 3]
        x1 = cx - w / 2
        y1 = cy - h / 2
        x2 = cx + w / 2
        y2 = cy + h / 2
        pred_boxes = torch.stack([x1, y1, x2, y2], dim=-1)  # (1, 8400, 4) xyxy

        pred_scores = raw_output[..., 4:].max(dim=-1)[0]  # (1, 8400) - max class score
        pred_class_idx = raw_output[..., 4:].argmax(dim=-1)  # (1, 8400) - class index
    else:
        # Legacy format: separate outputs (boxes, scores, class_idx)
        pred_boxes = torch.tensor(model_output[0].reshape(1, -1, 4))
        pred_scores = torch.tensor(model_output[1].reshape(1, -1))
        pred_class_idx = torch.tensor(model_output[2].reshape(1, -1))

    if not is_onnx:
        # Reset the HTP.
        PerfProfile.RelPerfProfileGlobal()

    # Non Maximum Suppression on each batch
    pred_boxes, pred_scores, pred_class_idx = batched_nms(
        nms_iou_threshold,
        nms_score_threshold,
        pred_boxes,
        pred_scores,
        pred_class_idx,
    )

    # Add boxes to each batch
    for batch_idx in range(len(pred_boxes)):
        pred_boxes_batch = pred_boxes[batch_idx]
        pred_scores_batch = pred_scores[batch_idx]
        pred_class_idx_batch = pred_class_idx[batch_idx]
        for box, score, class_idx in zip(pred_boxes_batch, pred_scores_batch, pred_class_idx_batch):
            class_idx_item = class_idx.item() 
            class_name = class_map.get(class_idx_item, "Unknown")

            draw_box_from_xyxy(
                output_image,
                box[0:2].int(),
                box[2:4].int(),
                color=(0, 255, 0),
                size=2,
                text=f'{score.item():.2f} {class_name}'
            )

    #save and display the output_image
    output_image = Image.fromarray(output_image)
    output_image.save(output_image_path)

    if show_image:
        output_image.show()

def Release():
    global yolov8

    # Release the resources.
    del(yolov8)


def main(input_image_path=None, output_image_path=None, show_image=True, use_onnx=False):

    if input_image_path is None:
        input_image_path = execution_ws / "input.jpg"

    if output_image_path is None:
        output_image_path = execution_ws / "output.png"

    Init(use_onnx=use_onnx)

    Inference(input_image_path=input_image_path, output_image_path=output_image_path, show_image=show_image)

    Release()

    return "Yolo V8 Inference Result"

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description="Process a single image path.")
    parser.add_argument('--input_image_path', help='Path to the input image', default=None)
    parser.add_argument('--output_image_path', help='Path to the output image', default=None)
    parser.add_argument(
        '--onnx',
        action='store_true',
        help=(
            "Use models/yolov8_det.onnx via OnnxRuntimeContext "
            "(onnxruntime_qnn HTP EP). This is the default when the "
            "ONNX file exists in the models directory."
        ),
    )
    args = parser.parse_args()

    main(args.input_image_path, args.output_image_path, use_onnx=args.onnx)
