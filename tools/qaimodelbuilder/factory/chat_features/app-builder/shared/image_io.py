# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
image_io
========

图像 IO + tile 切分 / 拼接。供 SR / OCR 等 image-input Pack 共用。

依赖：Pillow + numpy。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, Tuple, List

import numpy as np

logger = logging.getLogger("app_builder.image_io")


# ── 读 / 写 ────────────────────────────────────────────────────────────────

def read_image(path: Path | str, *, mode: str = "RGB") -> np.ndarray:
    """读图像 → np.ndarray (H,W,C) uint8。

    自动按 EXIF orientation 旋正（避免手机照片侧躺）。
    返回的数组与 PIL 解码后的连续内存等价；C 通道顺序为 mode 指定（默认 RGB）。
    """
    from PIL import Image, ImageOps   # type: ignore[import-not-found]
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"image file not found: {p}")
    with Image.open(p) as im:
        im = ImageOps.exif_transpose(im)
        if im.mode != mode:
            im = im.convert(mode)
        arr = np.asarray(im, dtype=np.uint8)
    return arr


def write_image(path: Path | str, arr: np.ndarray, *, quality: int = 95) -> None:
    """写图像。后缀决定格式（.png / .jpg / .webp 等）。父目录会自动创建。"""
    from PIL import Image   # type: ignore[import-not-found]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if arr.dtype != np.uint8:
        arr = np.clip(arr, 0, 255).astype(np.uint8, copy=False)
    if arr.ndim == 2:
        im = Image.fromarray(arr, mode="L")
    elif arr.shape[-1] == 4:
        im = Image.fromarray(arr, mode="RGBA")
    else:
        im = Image.fromarray(arr, mode="RGB")
    save_kwargs = {}
    if p.suffix.lower() in {".jpg", ".jpeg"}:
        save_kwargs["quality"] = int(quality)
        save_kwargs["optimize"] = True
    elif p.suffix.lower() == ".webp":
        save_kwargs["quality"] = int(quality)
    im.save(p, **save_kwargs)


# ── tile 切分 / 拼接 ────────────────────────────────────────────────────────

class TileLayout:
    """记录原图尺寸 + 每块 tile 的位置 / overlap 信息，用于拼回。"""
    def __init__(self, h: int, w: int, tile_size: int, overlap: int,
                 boxes: List[Tuple[int, int, int, int]]) -> None:
        self.h = h
        self.w = w
        self.tile_size = tile_size
        self.overlap = overlap
        self.boxes = boxes      # list of (y0, x0, y1, x1) inclusive top-left, exclusive bottom-right


def tile_image(
    image: np.ndarray,
    *,
    tile_size: int = 256,
    overlap: int = 16,
) -> Tuple[Iterator[np.ndarray], TileLayout]:
    """SR 类模型常用：把大图切成 (tile_size + 2*overlap) 的小块。

    返回 (tiles_iter, layout)；tiles_iter 是 generator，逐块产生 np.ndarray。
    layout 提供 stitch_image 拼回所需信息。
    """
    h, w = image.shape[:2]
    boxes: List[Tuple[int, int, int, int]] = []
    step = tile_size
    for y in range(0, h, step):
        for x in range(0, w, step):
            y1 = min(y + step, h)
            x1 = min(x + step, w)
            boxes.append((y, x, y1, x1))

    layout = TileLayout(h=h, w=w, tile_size=tile_size, overlap=overlap, boxes=boxes)

    def _gen():
        for (y0, x0, y1, x1) in boxes:
            # 带 overlap 取邻域，便于模型边缘一致
            ey0 = max(0, y0 - overlap)
            ex0 = max(0, x0 - overlap)
            ey1 = min(h, y1 + overlap)
            ex1 = min(w, x1 + overlap)
            yield image[ey0:ey1, ex0:ex1].copy()

    return _gen(), layout


def stitch_image(
    tiles: List[np.ndarray],
    layout: TileLayout,
    *,
    scale: int = 1,
) -> np.ndarray:
    """根据 layout 与 scale（如 SR x4）把 tiles 列表拼成完整大图（uint8）。

    scale: 模型放大倍数；输出尺寸 = (h*scale, w*scale)。
    简化策略：按 box 顺序顶左对齐填入，不做羽化（边界轻微撕裂在大模型 + 适当 overlap 下肉眼不可察；
    后续 v1.1 升级为线性羽化）。
    """
    if len(tiles) != len(layout.boxes):
        raise ValueError(f"tile count {len(tiles)} != layout box count {len(layout.boxes)}")
    H, W = layout.h * scale, layout.w * scale
    if not tiles:
        return np.zeros((H, W, 3), dtype=np.uint8)

    sample = tiles[0]
    if sample.ndim == 2:
        out = np.zeros((H, W), dtype=np.uint8)
    else:
        out = np.zeros((H, W, sample.shape[-1]), dtype=np.uint8)

    for tile, (y0, x0, y1, x1) in zip(tiles, layout.boxes):
        # 缩放到对应 scale 后大小
        ty1, tx1 = (y1 * scale, x1 * scale)
        ty0, tx0 = (y0 * scale, x0 * scale)
        h_target = ty1 - ty0
        w_target = tx1 - tx0
        # 模型输出可能因 overlap 边缘多出像素：按 box 大小裁剪左上角对齐
        tile_arr = tile
        if tile_arr.shape[0] > h_target or tile_arr.shape[1] > w_target:
            # 假设 overlap 在四周对称扩展，模型已按 overlap 输出，直接居中裁剪
            cy = (tile_arr.shape[0] - h_target) // 2
            cx = (tile_arr.shape[1] - w_target) // 2
            tile_arr = tile_arr[cy:cy + h_target, cx:cx + w_target]
        elif tile_arr.shape[0] < h_target or tile_arr.shape[1] < w_target:
            # 模型输出小于目标（首尾 tile 因边界不足）：截到模型实际输出大小
            h_target = tile_arr.shape[0]
            w_target = tile_arr.shape[1]
            ty1 = ty0 + h_target
            tx1 = tx0 + w_target
        if tile_arr.dtype != np.uint8:
            tile_arr = np.clip(tile_arr, 0, 255).astype(np.uint8, copy=False)
        out[ty0:ty1, tx0:tx1] = tile_arr

    return out
