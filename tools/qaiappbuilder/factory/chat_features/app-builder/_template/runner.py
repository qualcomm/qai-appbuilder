#!/usr/bin/env python
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
Pack Runner 模板（v3.1 · C★.6）
=================================

本文件展示 App Builder Pack 必须遵循的"行式 JSON 协议"。

接入要点：
  1. shared/ 已被后端通过 PYTHONPATH 注入，可直接 from runner_protocol import ...
  2. runner.py 进程的 cwd 总是 Pack 根目录；权重 weights/、资源 assets/ 的相对路径
     可直接使用 Path('weights/...').
  3. main() 中先 read_request() 读取 inputs/params/options，
     依次 emit() status / progress / metrics / result / done。
  4. 失败用 fail() 后 sys.exit(1)；正常退出 sys.exit(0)。

本模板不做实际推理；它只 echo 一遍输入并立刻 done，用于冒烟测试整条 SSE 链路。
"""

from __future__ import annotations

import sys

# 这两行 import 来自 features/app-builder/shared/，由后端注入 PYTHONPATH
from runner_protocol import emit, read_request, fail, status, result, done   # noqa: E402
from telemetry import measure                                                # noqa: E402


def main() -> None:
    req = read_request()
    inputs = req.get("inputs") or {}
    params = req.get("params") or {}

    status("preparing")

    # 这里通常加载模型；模板直接跳过
    status("running")

    with measure(device="cpu") as m:
        # —— 这里写真实推理 ——
        # 模板：直接 echo 输入字段（前端便于验证 SSE 链路是否打通）
        echo = {
            "received_inputs": inputs,
            "received_params": params,
            "note": "_template runner: replace with real inference logic",
        }

    emit({"type": "metrics", **m.summary()})
    result(echo)
    done()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:   # pylint: disable=broad-except
        fail(code=type(e).__name__.upper(), message=str(e))
        sys.exit(1)
