# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ─────────────────────────────────────────────────────────────────────────────
#
# Shared utilities for Pose Estimation model inference scripts.
# Provides model download, QNN initialization helpers shared by
# mediapipe_hand and openpose (and any future pose estimation models).
#
# Also contains inline implementations of functions previously imported from
# qai_hub_models, to remove that dependency entirely.
#

from __future__ import annotations

import math
from pathlib import Path

import cv2
import numpy as np
import torch
from torchvision.ops import nms

from qai_appbuilder import QNNContext, Runtime, LogLevel, ProfilingLevel, QNNConfig
import install


# ─────────────────────────────────────────────────────────────────────────────
# MediaPipe Hand model constants (from qai_hub_models.models.mediapipe_hand.model)
# ─────────────────────────────────────────────────────────────────────────────

# https://github.com/metalwhale/hand_tracking/blob/b2a650d61b4ab917a2367a05b85765b81c0564f2/run.py
#        8   12  16  20
#        |   |   |   |
#        7   11  15  19
#    4   |   |   |   |
#    |   6   10  14  18
#    3   |   |   |   |
#    |   5---9---13--17
#    2    \         /
#     \    \       /
#      1    \     /
#       \    \   /
#        ------0-
HAND_LANDMARK_CONNECTIONS = [
    (0, 1), (1, 2), (2, 3), (3, 4),
    (5, 6), (6, 7), (7, 8),
    (9, 10), (10, 11), (11, 12),
    (13, 14), (14, 15), (15, 16),
    (17, 18), (18, 19), (19, 20),
    (0, 5), (5, 9), (9, 13), (13, 17), (0, 17),
]

# Palm detector model parameters
DETECT_DXY = 0.5
DETECT_DSCALE = 2.5
WRIST_CENTER_KEYPOINT_INDEX = 0
MIDDLE_FINDER_KEYPOINT_INDEX = 2
ROTATION_VECTOR_OFFSET_RADS = np.pi / 2


# ─────────────────────────────────────────────────────────────────────────────
# MediaPipePyTorchAsRoot context manager
# (from qai_hub_models.models._shared.mediapipe.utils)
# ─────────────────────────────────────────────────────────────────────────────

import sys
import os
import threading

_SOURCE_AS_ROOT_LOCK = threading.Lock()


class MediaPipePyTorchAsRoot:
    """
    Context manager that temporarily adds the MediaPipePyTorch repository
    to sys.path so that blazepalm / blazehand_landmark can be imported.

    Looks for the repo in common cache locations used by qai_hub_models,
    or falls back to the current working directory.
    """

    def __init__(self):
        self._repo_path = self._find_repo()
        self._added = False

    @staticmethod
    def _find_repo() -> str | None:
        # Try the qai_hub_models local store first
        candidates = [
            os.path.join(os.path.expanduser("~"), ".qaihm"),
        ]
        for base in candidates:
            for root, dirs, _ in os.walk(base):
                if "blazepalm.py" in os.listdir(root):
                    return root
        # Fallback: current directory
        return os.getcwd()

    def __enter__(self):
        _SOURCE_AS_ROOT_LOCK.acquire()
        if self._repo_path and self._repo_path not in sys.path:
            sys.path.insert(0, self._repo_path)
            self._added = True
        return self._repo_path

    def __exit__(self, *args):
        if self._added and self._repo_path in sys.path:
            sys.path.remove(self._repo_path)
        _SOURCE_AS_ROOT_LOCK.release()


# ─────────────────────────────────────────────────────────────────────────────
# Bounding box utilities (from qai_hub_models.utils.bounding_box_processing)
# ─────────────────────────────────────────────────────────────────────────────

def box_xywh_to_xyxy(box_cwh: torch.Tensor) -> torch.Tensor:
    """Convert center/W/H to top-left/bottom-right bounding box values."""
    if box_cwh.shape[-1] == 4:
        center = box_cwh[..., :2]
        wh_half = box_cwh[..., 2:] * 0.5
        return torch.cat((center - wh_half, center + wh_half), dim=-1)
    center = box_cwh[..., 0, :]
    wh_half = box_cwh[..., 1, :] * 0.5
    return torch.stack((center - wh_half, center + wh_half), dim=-2)


def box_xyxy_to_xywh(box_xy: torch.Tensor) -> torch.Tensor:
    """Convert top-left/bottom-right to center/W/H bounding box values."""
    if box_xy.shape[-1] == 4:
        xy1 = box_xy[..., :2]
        xy2 = box_xy[..., 2:]
        center = (xy1 + xy2) * 0.5
        wh = xy2 - xy1
        return torch.cat((center, wh), dim=-1)
    xy1 = box_xy[..., 0, :]
    xy2 = box_xy[..., 1, :]
    center = (xy1 + xy2) * 0.5
    wh = xy2 - xy1
    return torch.stack((center, wh), dim=-2)


def batched_nms(
    iou_threshold: float,
    score_threshold: float | None,
    boxes: torch.Tensor,
    scores: torch.Tensor,
    class_indices: torch.Tensor | None = None,
    *gather_additional_args: torch.Tensor,
) -> tuple[list[torch.Tensor], ...]:
    """Non maximum suppression over several batches."""
    from torchvision.ops import batched_nms as tv_batched_nms

    scores_out: list[torch.Tensor] = []
    boxes_out: list[torch.Tensor] = []
    class_indices_out: list[torch.Tensor] = []
    args_out: list[list[torch.Tensor]] = (
        [[] for _ in gather_additional_args] if gather_additional_args else []
    )

    for batch_idx in range(boxes.shape[0]):
        batch_scores = scores[batch_idx]
        batch_boxes = boxes[batch_idx]
        batch_args = [arg[batch_idx] for arg in gather_additional_args or []]
        batch_class_indices = (
            class_indices[batch_idx] if class_indices is not None else None
        )

        if score_threshold is not None:
            # Flatten to 1D before nonzero to handle extra batch/channel dims
            flat_scores = batch_scores.reshape(-1)
            scores_idx = torch.nonzero(flat_scores >= score_threshold).squeeze(-1)
            batch_scores = flat_scores[scores_idx]
            batch_boxes = batch_boxes[scores_idx]
            batch_class_indices = (
                batch_class_indices[scores_idx]
                if batch_class_indices is not None
                else None
            )
            batch_args = [arg[scores_idx] for arg in batch_args or []]

        if len(batch_scores > 0):
            if batch_class_indices is not None:
                nms_indices = tv_batched_nms(
                    batch_boxes[..., :4],
                    batch_scores,
                    batch_class_indices,
                    iou_threshold,
                )
            else:
                nms_indices = nms(batch_boxes[..., :4], batch_scores, iou_threshold)

            batch_boxes = batch_boxes[nms_indices]
            batch_scores = batch_scores[nms_indices]
            batch_class_indices = (
                batch_class_indices[nms_indices]
                if batch_class_indices is not None
                else None
            )
            batch_args = [arg[nms_indices] for arg in batch_args]

        boxes_out.append(batch_boxes)
        scores_out.append(batch_scores)
        if batch_class_indices is not None:
            class_indices_out.append(batch_class_indices)
        for arg_idx, arg in enumerate(batch_args):
            args_out[arg_idx].append(arg)

    if class_indices is None:
        return boxes_out, scores_out, *args_out
    return boxes_out, scores_out, class_indices_out, *args_out


def compute_box_corners_with_rotation(
    xc: torch.Tensor,
    yc: torch.Tensor,
    w: torch.Tensor,
    h: torch.Tensor,
    theta: torch.Tensor,
) -> torch.Tensor:
    """Compute (x, y) coordinates of box corners given center, size, and rotation."""
    batch_size = xc.shape[0]
    points = torch.tensor([[-1, -1, 1, 1], [-1, 1, -1, 1]], dtype=torch.float32).repeat(
        batch_size, 1, 1
    )
    points *= torch.stack((w / 2, h / 2), dim=-1).unsqueeze(dim=2)
    R = torch.stack(
        (
            torch.stack((torch.cos(theta), -torch.sin(theta)), dim=1),
            torch.stack((torch.sin(theta), torch.cos(theta)), dim=1),
        ),
        dim=1,
    )
    points = R @ points
    points = points + torch.stack((xc, yc), dim=1).unsqueeze(dim=2)
    return points.transpose(-1, -2)


def compute_box_affine_crop_resize_matrix(
    box_corners: torch.Tensor, output_image_size: tuple[int, int]
) -> list[np.ndarray]:
    """Compute affine transform matrices to crop/rescale box corners to output image size."""
    network_input_points = np.array(
        [[0, 0], [0, output_image_size[1] - 1], [output_image_size[0] - 1, 0]],
        dtype=np.float32,
    )
    affines: list[np.ndarray] = []
    for batch in range(box_corners.shape[0]):
        src = box_corners[batch][..., :3].detach().numpy()
        affines.append(cv2.getAffineTransform(src, network_input_points))
    return affines


def apply_directional_box_offset(
    offset: float | torch.Tensor,
    vec_start: torch.Tensor,
    vec_end: torch.Tensor,
    xc: torch.Tensor,
    yc: torch.Tensor,
) -> None:
    """Offset bounding box center in the direction of the supplied vector (in-place)."""
    xlen = vec_end[..., 0] - vec_start[..., 0]
    ylen = vec_end[..., 1] - vec_start[..., 1]
    vec_len = torch.sqrt(torch.float_power(xlen, 2) + torch.float_power(ylen, 2))
    xc += offset * (xlen / vec_len)
    yc += offset * (ylen / vec_len)


# ─────────────────────────────────────────────────────────────────────────────
# Image processing utilities (from qai_hub_models.utils.image_processing)
# ─────────────────────────────────────────────────────────────────────────────

def apply_affine_to_coordinates(
    coordinates: np.ndarray | torch.Tensor,
    affine: np.ndarray | torch.Tensor,
) -> np.ndarray | torch.Tensor:
    """Apply affine matrix to coordinates of shape [..., 2]."""
    return (affine[:, :2] @ coordinates.T + affine[:, 2:]).T


def compute_vector_rotation(
    vec_start: torch.Tensor,
    vec_end: torch.Tensor,
    offset_rads: float | torch.Tensor = 0,
) -> torch.Tensor:
    """Compute rotation angle of a vector with an added offset."""
    return (
        torch.atan2(
            vec_start[..., 1] - vec_end[..., 1],
            vec_start[..., 0] - vec_end[..., 0],
        )
        - offset_rads
    )


def denormalize_coordinates(
    coordinates: torch.Tensor,
    input_img_size: tuple[int, int],
    scale: float = 1.0,
    pad: tuple[int, int] = (0, 0),
) -> None:
    """Map detection coordinates from [0,1] to coordinates in the original image (in-place)."""
    img_0, img_1 = input_img_size
    pad_0, pad_1 = pad
    coordinates[..., 0] = ((coordinates[..., 0] * img_0 - pad_0) / scale).int()
    coordinates[..., 1] = ((coordinates[..., 1] * img_1 - pad_1) / scale).int()


def apply_batched_affines_to_frame(
    frame: np.ndarray, affines: list[np.ndarray], output_image_size: tuple[int, int]
) -> np.ndarray:
    """Generate one image per affine applied to the given frame."""
    assert frame.dtype == np.byte or frame.dtype == np.uint8
    imgs = []
    for affine in affines:
        img = cv2.warpAffine(frame, affine, output_image_size)
        imgs.append(img)
    return np.stack(imgs)


def numpy_image_to_torch(image: np.ndarray, to_float: bool = True) -> torch.Tensor:
    """Convert a Numpy image (uint8, [H W C] or [N H W C]) to a pyTorch tensor [N C H W] in [0,1]."""
    image_torch = torch.from_numpy(image)
    if len(image.shape) == 3:
        image_torch = image_torch.unsqueeze(0)
    image_torch = image_torch.permute(0, 3, 1, 2)
    if to_float:
        return image_torch.float() / 255.0
    return image_torch


# ─────────────────────────────────────────────────────────────────────────────
# MediaPipe anchor decoding (from qai_hub_models.models._shared.mediapipe.utils)
# ─────────────────────────────────────────────────────────────────────────────

def decode_preds_from_anchors(
    boxes_and_coordinates: torch.Tensor,
    img_size: tuple[int, int],
    anchors: torch.Tensor,
) -> torch.Tensor:
    """Decode predictions using the provided anchors."""
    assert boxes_and_coordinates.shape[-1] == anchors.shape[-1] == 2
    assert boxes_and_coordinates.shape[-3] == anchors.shape[-3]

    h_size, w_size = img_size
    offset = anchors[..., 0:1, :] * torch.tensor(
        [[w_size, h_size]], dtype=anchors.dtype
    )
    scale = anchors[..., 1:2, :]
    K = boxes_and_coordinates.shape[-2]
    mask = (torch.arange(K) != 1).view(K, 1)
    return boxes_and_coordinates * scale + (offset * mask)


# ─────────────────────────────────────────────────────────────────────────────
# Drawing utilities (from qai_hub_models.utils.draw)
# ─────────────────────────────────────────────────────────────────────────────

def draw_points(
    frame: np.ndarray,
    points: np.ndarray | torch.Tensor,
    color: tuple[int, int, int] = (0, 0, 0),
    size: int | list[int] = 10,
) -> None:
    """Draw the given points on the frame (in-place)."""
    if len(points.shape) == 1:
        points = points.reshape(-1, 2)
    assert isinstance(size, int) or len(size) == len(points)
    cv_keypoints = []
    for i, (x, y) in enumerate(points):
        curr_size = size if isinstance(size, int) else size[i]
        cv_keypoints.append(cv2.KeyPoint(int(x), int(y), curr_size))
    cv2.drawKeypoints(
        frame,
        cv_keypoints,
        outImage=frame,
        color=color,
        flags=cv2.DRAW_MATCHES_FLAGS_DRAW_RICH_KEYPOINTS,
    )


def draw_connections(
    frame: np.ndarray,
    points: np.ndarray | torch.Tensor,
    connections: list[tuple[int, int]] | None = None,
    color: tuple[int, int, int] = (0, 0, 0),
    size: int = 1,
) -> None:
    """Draw connecting lines between the given points on the frame (in-place)."""
    if len(points.shape) == 3:
        point_pairs = points
    else:
        assert connections is not None
        if len(points.shape) == 1:
            points = points.reshape(-1, 2)
        point_pairs = [
            (
                (int(points[i][0]), int(points[i][1])),
                (int(points[j][0]), int(points[j][1])),
            )
            for (i, j) in connections
        ]
    cv2.polylines(
        frame,
        np.asarray(point_pairs, dtype=np.int64),
        isClosed=False,
        color=color,
        thickness=size,
    )


def draw_box_from_corners(
    frame: np.ndarray,
    corners: np.ndarray | torch.Tensor,
    color: tuple[int, int, int] = (0, 0, 0),
    size: int = 3,
) -> None:
    """Draw a box using the 4 corner points (in-place)."""
    draw_points(frame, corners, color, size)
    draw_connections(frame, corners, [(0, 1), (0, 2), (1, 3), (2, 3)], color, size)


def draw_box_from_xyxy(
    frame: np.ndarray,
    top_left: np.ndarray | torch.Tensor | tuple[int, int],
    bottom_right: np.ndarray | torch.Tensor | tuple[int, int],
    color: tuple[int, int, int] = (0, 0, 0),
    size: int = 3,
    text: str | None = None,
) -> None:
    """Draw a box using top-left / bottom-right points (in-place)."""
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


# ─────────────────────────────────────────────────────────────────────────────
# display_or_save_image (from qai_hub_models.utils.display)
# ─────────────────────────────────────────────────────────────────────────────

def display_or_save_image(
    image,
    output_dir: str | None = None,
    filename: str = "image.png",
    desc: str = "image",
) -> bool:
    """
    If output_dir is set, save image to disk and return.
    Else try to display image. If displaying fails, save to a default location.
    """
    from PIL.Image import Image as PILImage
    from PIL.ImageShow import IPythonViewer, _viewers

    def _is_running_in_notebook() -> bool:
        try:
            from IPython.core.getipython import get_ipython
            if "IPKernelApp" not in get_ipython().config:
                return False
        except (ImportError, AttributeError):
            return False
        return True

    def _is_headless() -> bool:
        return (
            os.environ.get("SSH_TTY") is not None
            or os.environ.get("SSH_CLIENT") is not None
        )

    def _save(img, base_dir, fname):
        os.makedirs(base_dir, exist_ok=True)
        fpath = os.path.join(base_dir, fname)
        img.save(fpath)
        print(f"Saving {desc} to {fpath}")

    if output_dir is not None:
        _save(image, output_dir, filename)
        return False

    if _is_running_in_notebook():
        for viewer in _viewers:
            if isinstance(viewer, IPythonViewer):
                viewer.show(image)
                return True

    try:
        if not _is_headless():
            print(f"Displaying {desc}")
            image.show()
            return True
    except Exception:
        pass

    _save(image, os.path.join(Path.cwd(), "build"), filename)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# QNN context base class and helpers
# ─────────────────────────────────────────────────────────────────────────────

class PoseEstimationQNNContext(QNNContext):
    """
    Base QNN context for pose estimation models.
    Accepts a single input array and returns all output tensors as a list.
    """

    def Inference(self, input_data):
        return super().Inference([input_data])


def download_model(soc_id, model_name, model_path, help_url) -> bool:
    """
    Download a QAI Hub model if it is not already present on disk.

    Parameters
    ----------
    soc_id     : str   – SoC ID for model lookup (e.g. 'wos', '9075')
    model_name : str   – model name key used for hub lookup and log messages
    model_path : Path | str – destination file path for the .bin model
    help_url   : str   – URL shown when the download fails

    Returns
    -------
    bool – True if the model is available (already present or just downloaded),
           False if the download failed.
    """
    model_path = Path(model_path)
    if model_path.is_file():
        return True
    model_path.parent.mkdir(parents=True, exist_ok=True)
    desc = f"Downloading {model_name} model... "
    fail = (
        f"\nFailed to download {model_name} model. "
        f"Please prepare the model according to the steps in below link:\n{help_url}"
    )
    return install.download_qai_hubmodel(soc_id, model_name, str(model_path), desc=desc, fail=fail)


def init_htp_context(model_path, model_class, instance_name):
    """
    Configure the QNN runtime and instantiate a model context.
    Falls back to CPU if HTP is unavailable.

    Parameters
    ----------
    model_path    : Path | str  – path to the compiled .bin model
    model_class   : type        – QNNContext subclass to instantiate
    instance_name : str         – name passed to the QNNContext constructor

    Returns
    -------
    An instance of *model_class* ready for inference.
    """
    try:
        QNNConfig.Config(Runtime.HTP, LogLevel.WARN, ProfilingLevel.BASIC)
        return model_class(instance_name, str(model_path))
    except Exception as e:
        print(f"[WARNING] HTP backend unavailable ({e}), falling back to CPU runtime.")
        QNNConfig.Config(Runtime.CPU, LogLevel.WARN, ProfilingLevel.BASIC)
        return model_class(instance_name, str(model_path))
