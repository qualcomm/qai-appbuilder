# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
telemetry
=========

Pack runner 进程内的延迟 / 内存 / 设备指标采集。

提供：
  * measure()     — 单段计时 context manager（向后兼容）
  * StageTimer    — 多阶段计时器，分别测量模型加载 / 推理 / 释放等各阶段

MVP 不强制依赖 psutil，缺包时优雅降级（只报 latency 与 device 字段）。
"""

from __future__ import annotations

import contextlib
import time
from typing import Any, Iterator, List

try:
    import psutil   # type: ignore[import-not-found]
    _HAS_PSUTIL = True
except ImportError:
    _HAS_PSUTIL = False


# ── 单段计时（向后兼容） ─────────────────────────────────────────────────────

class _MeasureHandle:
    def __init__(self) -> None:
        self.latency_ms: float = 0.0
        self.peak_memory_mb: float | None = None
        self.device: str = "unknown"
        self._t0 = time.perf_counter()
        self._proc = psutil.Process() if _HAS_PSUTIL else None
        self._peak_rss = 0

    def set_device(self, device: str) -> None:
        self.device = device

    def _sample_memory(self) -> None:
        if self._proc is None:
            return
        try:
            rss = self._proc.memory_info().rss
            if rss > self._peak_rss:
                self._peak_rss = rss
        except (OSError, psutil.Error):  # type: ignore[union-attr]
            pass

    def summary(self) -> dict:
        out: dict = {
            "latencyMs": round(self.latency_ms, 2),
            "device":    self.device,
        }
        if self.peak_memory_mb is not None:
            out["memoryMB"] = round(self.peak_memory_mb, 2)
        return out


@contextlib.contextmanager
def measure(*, device: str = "unknown") -> Iterator[_MeasureHandle]:
    """Context manager: 量度推理段的延迟和峰值内存。

        with measure(device='htp') as m:
            ... # 推理
        emit({"type":"metrics", **m.summary()})
    """
    h = _MeasureHandle()
    h.set_device(device)
    h._sample_memory()
    try:
        yield h
    finally:
        h._sample_memory()
        h.latency_ms = (time.perf_counter() - h._t0) * 1000.0
        if h._peak_rss:
            h.peak_memory_mb = h._peak_rss / (1024.0 * 1024.0)


# ── 多阶段计时器 ─────────────────────────────────────────────────────────────

class StageTimer:
    """多阶段计时器：独立测量模型加载 / 推理 / 释放等各阶段的耗时。

    用法（runner.py 内）：
        from telemetry import StageTimer

        timer = StageTimer(device="htp")

        with timer.stage("load_encoder", model="encoder.bin"):
            encoder = QnnContext.load(...)
        with timer.stage("load_decoder", model="decoder.bin"):
            decoder = QnnContext.load(...)

        with timer.stage("infer"):
            result = encoder.run(...)

        with timer.stage("release"):
            encoder.close()
            decoder.close()

        # 产出 metrics 事件（向后兼容 latencyMs + 新增 stages 明细）
        emit({"type": "metrics", **timer.summary()})

    summary() 输出：
        {
          "latencyMs": 16080.5,          ← 所有 stage 总和
          "device": "htp",
          "stages": [
            {"name": "load_encoder", "latencyMs": 5200.1, "model": "encoder.bin"},
            {"name": "load_decoder", "latencyMs": 4800.3, "model": "decoder.bin"},
            {"name": "infer",        "latencyMs": 5900.7},
            {"name": "release",      "latencyMs": 179.4},
          ],
          "memoryMB": 512.3              ← 可选（需 psutil）
        }
    """

    def __init__(self, *, device: str = "unknown") -> None:
        self.device = device
        self.stages: List[dict[str, Any]] = []
        self._proc = psutil.Process() if _HAS_PSUTIL else None
        self._peak_rss: int = 0

    @contextlib.contextmanager
    def stage(self, name: str, *, accumulate: bool = False, **meta: Any) -> Iterator[None]:
        """测量单个阶段的耗时。

        Args:
            name:  阶段名称（如 'load_encoder' / 'infer' / 'release'）
            accumulate: True 时若同名 stage 已存在，则把本次耗时累加到现有条目
                       （用于循环内重复进入同一阶段，如 chunk loop 中的 preprocess）。
                       默认 False 每次新增独立条目。
            **meta: 附加元数据（如 model='encoder.bin'），会原样附到该 stage 条目上
        """
        self._sample_memory()
        t0 = time.perf_counter()
        try:
            yield
        finally:
            ms = (time.perf_counter() - t0) * 1000.0
            self._sample_memory()
            if accumulate:
                # 找到首个同名 entry 累加；找不到则新建
                existing = next((e for e in self.stages if e.get("name") == name), None)
                if existing is not None:
                    existing["latencyMs"] = round(existing["latencyMs"] + ms, 2)
                    if meta:
                        existing.update(meta)
                    return
            entry: dict[str, Any] = {"name": name, "latencyMs": round(ms, 2)}
            if meta:
                entry.update(meta)
            self.stages.append(entry)

    def _sample_memory(self) -> None:
        if self._proc is None:
            return
        try:
            rss = self._proc.memory_info().rss
            if rss > self._peak_rss:
                self._peak_rss = rss
        except (OSError, Exception):
            pass

    @property
    def total_ms(self) -> float:
        """所有已记录阶段的总耗时。"""
        return sum(s["latencyMs"] for s in self.stages)

    @property
    def peak_memory_mb(self) -> float | None:
        if self._peak_rss:
            return self._peak_rss / (1024.0 * 1024.0)
        return None

    def summary(self) -> dict[str, Any]:
        """生成 metrics 事件的 payload（向后兼容 latencyMs 字段 + 新增 stages）。"""
        out: dict[str, Any] = {
            "latencyMs": round(self.total_ms, 2),
            "device":    self.device,
            "stages":    self.stages,
        }
        mem = self.peak_memory_mb
        if mem is not None:
            out["memoryMB"] = round(mem, 2)
        return out
