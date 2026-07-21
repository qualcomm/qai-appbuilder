# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
runner_protocol
===============

App Builder Pack runner.py 的行式 JSON 协议工具。

子进程协议（v3.1 · C★.6 / I.4）：
  - 进程启动时 cwd = Pack 根目录（features/app-builder/models/<id>/）
  - stdin 读取**单行 JSON** 请求体 {runId, modelId, inputs, params, options, packDir, repoRoot}
  - stdout 每行一个 Event JSON：
      {"type":"status",   "state":"preparing|running"}
      {"type":"progress", "phase":"infer", "pct":42}
      {"type":"metrics",  "latencyMs":118, "memoryMB":64, "device":"htp"}
      {"type":"log",      "stream":"stdout", "line":"..."}        # 一般用 stderr 写日志即可
      {"type":"result",   "output":{...}}                          # 必须正好 1 条
      {"type":"done"}                                              # 正常结束必须最后 1 条
      {"type":"error",    "code":"...", "message":"..."}           # 失败时（与 done 互斥）
  - stderr 自由输出日志，被 backend 吸收到 logger（不进 SSE）。
  - 退出码：成功 0，失败非 0；后端用 done/error 事件判定结果，退出码作辅助。

每个 Pack 的 runner.py 必须 import：
    from runner_protocol import emit, read_request, fail
（PYTHONPATH 由 backend.app_builder.runners.python_script 注入 features/app-builder/shared/）
"""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from typing import Any


# 子进程 stdout 默认编码在 Windows 下是 cp1252/cp936，无法承载中日韩字符。
# 强制重配为 UTF-8（Python 3.7+ 支持 reconfigure），确保事件 JSON 中的非 ASCII
# 文本（OCR 中文结果、ASR 多语转写等）能稳定写入。失败时退化为 ascii-safe。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:   # pylint: disable=broad-except
    pass
try:
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:   # pylint: disable=broad-except
    pass


# ── stdout 写事件（行式 JSON） ────────────────────────────────────────────────

def emit(event: dict[str, Any]) -> None:
    """把一个事件写入 stdout 并立即 flush。

    使用方法（runner.py 内）：
        from runner_protocol import emit
        emit({"type":"status", "state":"preparing"})
        emit({"type":"result", "output":{"image_path":"data/outputs/x.png"}})
        emit({"type":"done"})
    """
    if not isinstance(event, dict):
        raise TypeError("emit() expects a dict, got %r" % type(event))
    if "type" not in event:
        raise ValueError("emit() event missing required 'type' field")
    line = json.dumps(event, ensure_ascii=False)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def status(state: str, **extra: Any) -> None:
    """Convenience: emit({'type':'status','state':state, ...})."""
    payload = {"type": "status", "state": state}
    payload.update(extra)
    emit(payload)


def progress(phase: str, pct: float, **extra: Any) -> None:
    """Convenience: emit({'type':'progress','phase':phase,'pct':pct, ...})."""
    payload = {"type": "progress", "phase": phase, "pct": float(pct)}
    payload.update(extra)
    emit(payload)


def metrics(latency_ms: float | None = None, memory_mb: float | None = None,
            device: str | None = None, **extra: Any) -> None:
    """Convenience: emit({'type':'metrics', ...})."""
    payload: dict[str, Any] = {"type": "metrics"}
    if latency_ms is not None:
        payload["latencyMs"] = float(latency_ms)
    if memory_mb is not None:
        payload["memoryMB"] = float(memory_mb)
    if device is not None:
        payload["device"] = device
    payload.update(extra)
    emit(payload)


def result(output: dict[str, Any], **extra: Any) -> None:
    """Convenience: emit({'type':'result','output':output, ...})."""
    payload: dict[str, Any] = {"type": "result", "output": output}
    payload.update(extra)
    emit(payload)


def done(**extra: Any) -> None:
    payload: dict[str, Any] = {"type": "done"}
    payload.update(extra)
    emit(payload)


def fail(code: str, message: str, **extra: Any) -> None:
    """Emit a single error event (does not exit; caller should sys.exit(1) afterwards)."""
    payload: dict[str, Any] = {"type": "error", "code": code, "message": message}
    payload.update(extra)
    emit(payload)


# ── stdin 读请求 ────────────────────────────────────────────────────────────

def read_request() -> dict[str, Any]:
    """从 stdin 读取请求 JSON。

    优先方式：若 sys.argv[1] 是一个存在的 .json 文件，直接从文件读取
    （命令行手动调试场景：python runner.py request.json）。
    否则从 stdin 读取单行 JSON（与 backend.PythonScriptRunner 写法一致）。
    """
    # 调试优先：python runner.py request.json
    if len(sys.argv) >= 2:
        candidate = sys.argv[1]
        if candidate.endswith(".json"):
            p = Path(candidate)
            with p.open("r", encoding="utf-8") as f:
                return json.load(f)
    # 正常路径：从 stdin 读单行 JSON（由 backend PythonScriptRunner 注入）
    raw = sys.stdin.readline()
    if raw and raw.strip():
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"invalid request JSON: {e}") from e
    raise RuntimeError("no request received on stdin (and no argv[1] file)")


# ── 顶层异常处理装饰器 ─────────────────────────────────────────────────────

def main_entry(fn):
    """Decorator: 把 runner.py 里 main() 包一层标准异常处理 + done。

    用法：
        @main_entry
        def main():
            req = read_request()
            ...
            result({"...": ...})

        if __name__ == '__main__':
            main()
    """
    def _wrapper():
        try:
            fn()
            # 若用户 main() 没显式 emit done，这里兜底（不重复 emit error 即可）
            done()
            sys.exit(0)
        except SystemExit:
            raise
        except Exception as e:
            fail(
                code=type(e).__name__.upper(),
                message=str(e),
                traceback=traceback.format_exc(limit=20),
            )
            sys.exit(1)
    return _wrapper
