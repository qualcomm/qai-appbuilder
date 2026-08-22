#!/usr/bin/env python
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
Real-ESRGAN x4plus — App Builder Pack runner.

4x image super-resolution using Real-ESRGAN on QNN HTP NPU.
Supports arbitrary image sizes via tiled inference with overlap blending.

Input:  image file (PNG/JPG/JPEG/WebP/BMP/TIFF)
Output: JSON { output_path, original_size, upscaled_size, scale }

Model: real_esrgan_480x640_fp16.bin
  Input shape:  [1, 3, 480, 640]  (NCHW, FP16, pixel values 0-1)
  Output shape: [1, 3, 1920, 2560] (NCHW, FP16, pixel values 0-1)

Protocol imports (injected via PYTHONPATH from shared/):
  read_request, emit, progress, status, result, done, fail
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Optional

import numpy as np

# shared/ is on PYTHONPATH (injected by backend.app_builder.runners.python_script).
from runner_protocol import emit, progress, read_request, status, result, done, fail   # noqa: E402
from telemetry import measure, StageTimer   # noqa: E402
from image_io import read_image   # noqa: E402


# ----- Constants (matched to the FP16 .bin context binary) ------------------

MODEL_BIN_NAME = "real_esrgan_480x640_fp16.bin"
MODEL_BIN_FALLBACK = "real_esrgan_360x480_fp16.bin"

# Default tile dimensions (model input H x W)
TILE_H = 480
TILE_W = 640
SCALE = 4

# Tile overlap in input pixels (output overlap = this * SCALE)
DEFAULT_TILE_OVERLAP = 32

# Safety: wall-clock timeout for total inference
MAX_INFER_WALL_S = 600.0  # 10 minutes for very large images


# ----- Domain errors --------------------------------------------------------

class _UserError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


# ----- Path resolution ------------------------------------------------------

def _resolve_repo_root(req: dict[str, Any]) -> Path:
    raw = req.get("repoRoot") or "."
    p = Path(raw)
    if p.is_dir():
        return p.resolve()
    return Path(__file__).resolve().parents[4]


def _resolve_pack_dir(req: dict[str, Any], repo_root: Path) -> Path:
    raw = req.get("packDir")
    if raw:
        p = Path(raw)
        if p.is_dir():
            return p.resolve()
    return Path(__file__).resolve().parent


def _resolve_input_image(req: dict[str, Any], repo_root: Path) -> Path:
    inputs = req.get("inputs") or {}
    raw = inputs.get("image") or inputs.get("file") or inputs.get("input")
    if not raw:
        raise _UserError("INVALID_INPUT", "inputs.image is required")
    p = Path(raw)
    if not p.is_absolute():
        p = repo_root / p
    if not p.is_file():
        raise _UserError("INVALID_INPUT", f"image file not found: {p}")
    return p


def _resolve_model_path(repo_root: Path, pack_dir: Path,
                         bin_name: str) -> Path:
    """Locate the model .bin file in standard weight directories."""
    candidates = [
        pack_dir / "weights" / bin_name,
        pack_dir / bin_name,
        repo_root / "models" / "real-esrgan-x4plus" / bin_name,
        repo_root / "data" / "models" / "real-esrgan-x4plus" / bin_name,
    ]
    for c in candidates:
        if c.is_file():
            return c
    raise _UserError(
        "WEIGHTS_NOT_INSTALLED",
        f"Model file {bin_name} not found. Searched: "
        + ", ".join(str(c) for c in candidates),
    )


# ----- Tile position calculation --------------------------------------------

def _tile_positions(dim: int, tile: int, overlap: int) -> list[int]:
    """Compute tile start positions along one dimension with overlap."""
    if dim <= tile:
        return [0]
    positions: list[int] = []
    step = tile - overlap
    p = 0
    while p + tile <= dim:
        positions.append(p)
        p += step
    # Last tile aligned to edge
    if positions[-1] + tile < dim:
        last = dim - tile
        if last not in positions:
            positions.append(last)
    return positions


# ----- Pre/post processing --------------------------------------------------

def _preprocess_tile(tile_rgb: np.ndarray) -> np.ndarray:
    """Convert HWC uint8 RGB tile to NCHW float32 [0,1]."""
    arr = tile_rgb.astype(np.float32) / 255.0
    # HWC -> CHW -> NCHW
    return arr.transpose(2, 0, 1)[np.newaxis]


def _postprocess_tile(output: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    """Convert model output NCHW float [0,1] to HWC uint8 RGB."""
    # output shape: (1, 3, out_h, out_w) or flat — reshape as needed
    out = output.reshape(3, out_h, out_w)
    out = out.transpose(1, 2, 0)  # CHW -> HWC
    out = np.clip(out, 0.0, 1.0)
    return (out * 255.0).astype(np.uint8)


# ----- Overlap blending weights ---------------------------------------------

def _blend_weights(tile_w: int, tile_h: int, overlap_out: int,
                   at_left: bool, at_right: bool,
                   at_top: bool, at_bottom: bool) -> np.ndarray:
    """Generate 2D blending weight map for a tile (float32, HxWx1)."""
    x_blend = np.ones(tile_w, dtype=np.float32)
    y_blend = np.ones(tile_h, dtype=np.float32)

    if overlap_out > 1:
        if not at_left:
            x_blend[:overlap_out] = np.linspace(0, 1, overlap_out)
        if not at_right:
            x_blend[-overlap_out:] = np.linspace(1, 0, overlap_out)
        if not at_top:
            y_blend[:overlap_out] = np.linspace(0, 1, overlap_out)
        if not at_bottom:
            y_blend[-overlap_out:] = np.linspace(1, 0, overlap_out)

    weight_2d = y_blend[:, None] * x_blend[None, :]
    return weight_2d[:, :, np.newaxis]  # HxWx1 for broadcasting with HxWx3


# ----- OOM heuristic --------------------------------------------------------

def _is_oom(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return ("memory" in msg and ("out" in msg or "alloc" in msg or "exhaust" in msg)) \
        or "oom" in msg


# ----- Main -----------------------------------------------------------------

def main() -> None:
    req = read_request()
    repo_root = _resolve_repo_root(req)
    pack_dir = _resolve_pack_dir(req, repo_root)

    input_path = _resolve_input_image(req, repo_root)

    params = req.get("params") or {}

    # Parse tile_size param
    tile_size_str = str(params.get("tile_size", "480x640"))
    if tile_size_str == "360x480":
        tile_h, tile_w = 360, 480
        bin_name = MODEL_BIN_FALLBACK
    else:
        tile_h, tile_w = TILE_H, TILE_W
        bin_name = MODEL_BIN_NAME

    # Parse tile_overlap param
    try:
        tile_overlap = int(params.get("tile_overlap", DEFAULT_TILE_OVERLAP))
    except (TypeError, ValueError):
        tile_overlap = DEFAULT_TILE_OVERLAP
    tile_overlap = max(0, min(64, tile_overlap))

    status("preparing")

    # Import QNN helper
    try:
        from qnn_helper import QnnContext  # noqa: WPS433
    except Exception as e:
        raise _UserError(
            "QAI_APPBUILDER_UNAVAILABLE",
            f"qnn_helper import failed: {e}. Ensure shared/ is on PYTHONPATH.",
        ) from e

    # Load input image
    try:
        image = read_image(input_path)
    except FileNotFoundError as e:
        raise _UserError("INVALID_INPUT", str(e)) from e
    except Exception as e:
        raise _UserError("INVALID_INPUT", f"failed to decode image {input_path}: {e}") from e

    if image.ndim != 3 or image.shape[2] != 3:
        raise _UserError("INVALID_INPUT",
                         f"unsupported image shape {image.shape}; expected HWC RGB")

    orig_h, orig_w = int(image.shape[0]), int(image.shape[1])

    # Resolve model weight
    model_path = _resolve_model_path(repo_root, pack_dir, bin_name)

    # Load QNN context
    ctx = None
    timer = StageTimer(device="htp")
    try:
        try:
            with timer.stage("load_model", model=bin_name):
                ctx = QnnContext.load(str(model_path), runtime="Htp", log_level=1)
        except FileNotFoundError as e:
            raise _UserError("WEIGHTS_NOT_INSTALLED", str(e)) from e
        except NotImplementedError as e:
            raise _UserError("QAI_APPBUILDER_UNAVAILABLE", str(e)) from e
        except Exception as e:
            if _is_oom(e):
                raise _UserError("OUT_OF_MEMORY", str(e)) from e
            raise _UserError("INFER_ERROR", f"failed to load QNN context: {e}") from e

        status("processing")

        # Calculate tile positions
        x_positions = _tile_positions(orig_w, tile_w, tile_overlap)
        y_positions = _tile_positions(orig_h, tile_h, tile_overlap)
        total_tiles = len(x_positions) * len(y_positions)

        # Output canvas
        out_h = orig_h * SCALE
        out_w = orig_w * SCALE
        result_accum = np.zeros((out_h, out_w, 3), dtype=np.float32)
        result_weight = np.zeros((out_h, out_w, 1), dtype=np.float32)

        overlap_out = tile_overlap * SCALE
        tile_out_h = tile_h * SCALE
        tile_out_w = tile_w * SCALE

        deadline = time.time() + MAX_INFER_WALL_S
        tile_count = 0

        progress(0, total_tiles, "upscaling")

        with timer.stage("inference", tiles=total_tiles):
            for iy, y in enumerate(y_positions):
                for ix, x in enumerate(x_positions):
                    if time.time() > deadline:
                        raise _UserError("TIMEOUT",
                                         f"inference exceeded {MAX_INFER_WALL_S}s wall clock")

                    # Extract tile (clamp to image bounds)
                    x_start = max(0, min(x, orig_w - tile_w))
                    y_start = max(0, min(y, orig_h - tile_h))
                    x_end = x_start + tile_w
                    y_end = y_start + tile_h

                    # Handle images smaller than one tile
                    if orig_w < tile_w or orig_h < tile_h:
                        tile_rgb = np.zeros((tile_h, tile_w, 3), dtype=np.uint8)
                        paste_h = min(orig_h, tile_h)
                        paste_w = min(orig_w, tile_w)
                        tile_rgb[:paste_h, :paste_w, :] = image[:paste_h, :paste_w, :]
                    else:
                        tile_rgb = image[y_start:y_end, x_start:x_end, :]

                    # Preprocess
                    input_tensor = _preprocess_tile(tile_rgb)

                    # NPU inference
                    output_tensor = ctx.inference(input_tensor)

                    # Postprocess
                    sr_tile = _postprocess_tile(output_tensor, tile_out_h, tile_out_w)

                    # Compute blend weights
                    at_left = (x_start == 0)
                    at_right = (x_start + tile_w >= orig_w)
                    at_top = (y_start == 0)
                    at_bottom = (y_start + tile_h >= orig_h)

                    weights = _blend_weights(
                        tile_out_w, tile_out_h, overlap_out,
                        at_left, at_right, at_top, at_bottom,
                    )

                    # Accumulate onto output canvas
                    out_x = x_start * SCALE
                    out_y = y_start * SCALE
                    ry_end = min(out_y + tile_out_h, out_h)
                    rx_end = min(out_x + tile_out_w, out_w)
                    h_slice = ry_end - out_y
                    w_slice = rx_end - out_x

                    sr_region = sr_tile[:h_slice, :w_slice, :].astype(np.float32)
                    w_region = weights[:h_slice, :w_slice, :]

                    result_accum[out_y:ry_end, out_x:rx_end, :] += sr_region * w_region
                    result_weight[out_y:ry_end, out_x:rx_end, :] += w_region

                    tile_count += 1
                    progress(tile_count, total_tiles, "upscaling")

        # Normalize
        result_weight[result_weight == 0] = 1.0
        final_rgb = (result_accum / result_weight).clip(0, 255).astype(np.uint8)

        # Crop if image was smaller than tile
        if orig_w < tile_w or orig_h < tile_h:
            final_rgb = final_rgb[:orig_h * SCALE, :orig_w * SCALE, :]

        status("saving")

        # Determine output format from input
        suffix = input_path.suffix.lower()
        if suffix in (".png", ".bmp", ".tiff"):
            out_ext = ".png"
        else:
            out_ext = ".jpg"

        # Save output
        run_id = req.get("runId") or f"sr-{int(time.time())}"
        output_dir = repo_root / "data" / "outputs"
        output_dir.mkdir(parents=True, exist_ok=True)
        output_filename = f"{run_id}_SRx{SCALE}{out_ext}"
        output_path = output_dir / output_filename

        from PIL import Image  # noqa: WPS433
        out_img = Image.fromarray(final_rgb)
        if out_ext == ".jpg":
            out_img.save(output_path, quality=95, optimize=True)
        else:
            out_img.save(output_path)

        # Emit result
        rel_output = f"data/outputs/{output_filename}"
        result({
            "output_path": rel_output,
            "original_size": [orig_w, orig_h],
            "upscaled_size": [orig_w * SCALE, orig_h * SCALE],
            "scale": SCALE,
        })

        emit("metrics", timer.summary())

    finally:
        if ctx is not None:
            try:
                ctx.close()
            except Exception:
                pass

    done()


if __name__ == "__main__":
    try:
        main()
    except _UserError as ue:
        fail(ue.code, ue.message)
        sys.exit(1)
    except KeyboardInterrupt:
        fail("CANCELLED", "interrupted by user")
        sys.exit(130)
    except Exception as exc:
        fail("INFER_ERROR", f"unexpected error: {exc}")
        sys.exit(1)
