# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
audio_io
========

音频读 / 写 / 重采样 / 通道缩混工具。供 ASR / TTS Pack 共用。

依赖（按可用性自动降级）：
  • soundfile            必需（解码 wav / flac / ogg）
  • numpy                必需
  • librosa              可选（高质量重采样；不可用时退回 polyphase）
  • scipy                可选（polyphase resample；不可用时退回简单线性插值）
  • imageio_ffmpeg       可选（webm/mp3/m4a 解码：用 ffmpeg.exe 转 wav）

返回约定：
  read_audio(path) → (samples_float32_mono, sample_rate)
  write_wav(path, samples_float32, sample_rate, *, channels=1)
  resample(samples, src_rate, dst_rate)
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Tuple

import numpy as np

# Re-export the canonical audio-format whitelist so callers that already import
# audio_io don't need a second import path. The constant itself lives in a
# numpy-free module (audio_formats) so the FastAPI backend (no numpy) can read
# it from registry.py without dragging in numpy/soundfile.
from audio_formats import SUPPORTED_AUDIO_FORMATS  # noqa: F401  (re-export)

logger = logging.getLogger("app_builder.audio_io")


def _read_via_soundfile(path: Path) -> Tuple[np.ndarray, int]:
    import soundfile as sf   # type: ignore[import-not-found]
    samples, sr = sf.read(str(path), dtype="float32", always_2d=False)
    return samples, int(sr)


def _read_via_ffmpeg(path: Path) -> Tuple[np.ndarray, int]:
    """走 ffmpeg.exe 转成临时 wav 再 sf.read。用于 webm/mp3/m4a 等 soundfile 不直接支持的格式。"""
    ffmpeg = shutil.which("ffmpeg") or _find_imageio_ffmpeg()
    if not ffmpeg:
        raise RuntimeError(
            f"Cannot decode {path.suffix} without ffmpeg. "
            "Install ffmpeg or use wav/flac/ogg input."
        )
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        tmp_path = Path(tmp.name)
    try:
        cmd = [
            ffmpeg, "-y", "-i", str(path),
            "-ac", "1", "-ar", "16000", "-acodec", "pcm_s16le",
            str(tmp_path),
        ]
        proc = subprocess.run(cmd, capture_output=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg failed (rc={proc.returncode}): "
                + proc.stderr.decode("utf-8", errors="replace")[:500]
            )
        return _read_via_soundfile(tmp_path)
    finally:
        try: tmp_path.unlink(missing_ok=True)
        except OSError: pass


def _find_imageio_ffmpeg() -> str | None:
    try:
        import imageio_ffmpeg   # type: ignore[import-not-found]
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:   # pylint: disable=broad-except
        return None


def read_audio(
    path: Path | str,
    *,
    target_sample_rate: int | None = None,
    mono: bool = True,
) -> Tuple[np.ndarray, int]:
    """读音频文件 → (samples_float32, sample_rate)。

    参数：
      target_sample_rate: 若给出，自动重采样到该值
      mono              : True 时若为多声道则平均缩混

    支持后缀（直接走 soundfile）：wav / flac / ogg / aiff
    其他后缀（webm / mp3 / m4a / mp4 ...）走 ffmpeg 转 wav 再读。
    """
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"audio file not found: {p}")

    ext = p.suffix.lower()
    direct_exts = {".wav", ".flac", ".ogg", ".aiff", ".aif"}

    if ext in direct_exts:
        samples, sr = _read_via_soundfile(p)
    else:
        samples, sr = _read_via_ffmpeg(p)

    if samples.ndim > 1 and mono:
        samples = samples.mean(axis=1).astype(np.float32, copy=False)
    samples = samples.astype(np.float32, copy=False)

    if target_sample_rate and target_sample_rate != sr:
        samples = resample(samples, sr, target_sample_rate)
        sr = target_sample_rate
    return samples, sr


def write_wav(
    path: Path | str,
    samples: np.ndarray,
    sample_rate: int,
    *,
    subtype: str = "PCM_16",
) -> None:
    """把 float32 / int16 一维或二维数组写成 wav 文件（创建父目录）。"""
    import soundfile as sf   # type: ignore[import-not-found]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    arr = np.asarray(samples)
    if arr.dtype == np.float32 or arr.dtype == np.float64:
        # 防止溢出：clip 到 [-1, 1]
        arr = np.clip(arr.astype(np.float32, copy=False), -1.0, 1.0)
    sf.write(str(p), arr, int(sample_rate), subtype=subtype)


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """重采样到目标 SR。按可用依赖逐级降级：librosa → scipy.polyphase → 线性插值。"""
    if src_rate == dst_rate:
        return samples
    if samples.size == 0:
        return samples

    # 1) librosa
    try:
        import librosa   # type: ignore[import-not-found]
        return librosa.resample(samples.astype(np.float32, copy=False), orig_sr=src_rate, target_sr=dst_rate)
    except Exception:   # pylint: disable=broad-except
        pass

    # 2) scipy.signal.resample_poly（多相滤波，质量好）
    try:
        from math import gcd
        from scipy.signal import resample_poly   # type: ignore[import-not-found]
        g = gcd(int(src_rate), int(dst_rate))
        up = int(dst_rate // g)
        down = int(src_rate // g)
        return resample_poly(samples, up, down).astype(np.float32, copy=False)
    except Exception:   # pylint: disable=broad-except
        pass

    # 3) 线性插值（精度低；仅作最后兜底）
    logger.warning("Falling back to naive linear resample %d → %d", src_rate, dst_rate)
    n_dst = int(round(len(samples) * dst_rate / src_rate))
    if n_dst <= 0:
        return np.zeros(0, dtype=np.float32)
    x_src = np.linspace(0, 1, num=len(samples), endpoint=False, dtype=np.float64)
    x_dst = np.linspace(0, 1, num=n_dst,        endpoint=False, dtype=np.float64)
    return np.interp(x_dst, x_src, samples).astype(np.float32, copy=False)


def db_rms(samples: np.ndarray) -> float:
    """计算 RMS dBFS（用于前端 VU 表 / 简单 VAD 阈值）。"""
    if samples.size == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(samples.astype(np.float32, copy=False)))))
    if rms <= 1e-9:
        return -120.0
    return 20.0 * float(np.log10(rms))
