# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""App Builder Pack 依赖聚合的可复用纯逻辑（V1 对齐修复 D-1）。

本模块把原先内联在 ``scripts/setup/setup_qairt_env.py`` 里的 Pack 依赖解析 /
归一 / 聚合逻辑下沉为单一、可单测、平台中立的纯函数集合，供：

* ``scripts/setup/install_app_builder_deps.py``（安装阶段瘦入口）；
* ``scripts/setup/setup_qairt_env.py``（model-builder 环境助手，去重复用）。

设计说明（AGENTS.md 双判据）:

* **判据 2（对齐 V1）**：聚合行为对齐 V1
  ``setup_qairt_env.py::_aggregate_app_builder_requirements`` —— 遍历每个 Pack 的
  ``requirements.txt``、PEP 503 归一、first-wins 去重、跳过 vendor wheel 包。
* **判据 1（架构更优）**：纯 stdlib，无副作用、无子进程、无 Windows 专属调用，
  可在任意平台 import + 单测（跨平台前瞻约束）。实际安装由调用方负责，本模块只
  产出"要装什么"。

``_NO_DEPS_PKGS`` 修正了 V1 既有缺陷：``openai-whisper`` 的传递依赖
``numba`` / ``llvmlite`` 在 ARM64 Windows 无 wheel，必须 ``--no-deps`` 安装
（见 ``factory/chat_features/app-builder/models/whisper-base/requirements.txt`` 注释）。V1 的聚合器
不带 ``--no-deps``，本模块把这类包单独归到 ``no_deps`` 桶里交给调用方处理。
"""

from __future__ import annotations

import re
from pathlib import Path

__all__ = [
    "NO_DEPS_PKGS",
    "SKIP_PKGS",
    "aggregate",
    "normalize_name",
    "parse_requirements",
    "split_pkg_spec",
]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
# 永不从 Pack requirements 安装的包（两种拼写）。``qai-appbuilder`` 是 Qualcomm
# 提供的 vendor wheel（见 vendor/whl/），从 PyPI 拉会在 ARM64 Windows 上装到
# 错误/空 stub。对齐 V1 ``_APP_BUILDER_SKIP_PKGS``。
SKIP_PKGS: frozenset[str] = frozenset({"qai-appbuilder", "qai_appbuilder"})

# 必须以 ``--no-deps`` 安装的包（PEP 503 归一后的名字）。修正 V1 既有缺陷：
# ``openai-whisper`` 仅用于取 ``assets/gpt2.tiktoken`` 词表文件，runner 从不
# import 它本体；其传递依赖 numba/llvmlite 在 ARM64 Windows 无 wheel。
NO_DEPS_PKGS: frozenset[str] = frozenset({"openai-whisper"})


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------
def normalize_name(name: str) -> str:
    """PEP 503 归一：小写，连续的 ``[-_.]`` 折叠成单个 ``-``。"""
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def split_pkg_spec(line: str) -> tuple[str, str]:
    """把一行 requirement 拆成 ``(pkg_name, constraint)``。

    在第一个 ``<>=!~;[(`` 或空白处断开包名。例::

        "numpy>=1.24"                  -> ("numpy", ">=1.24")
        "Pillow"                       -> ("Pillow", "")
        "opencv-python-headless>=4.8"  -> ("opencv-python-headless", ">=4.8")
    """
    s = line.strip()
    cut = len(s)
    for i, ch in enumerate(s):
        if ch in "<>=!~;[(" or ch.isspace():
            cut = i
            break
    name = s[:cut].strip()
    rest = s[cut:].strip()
    return name, rest


def parse_requirements(req_path: Path) -> list[tuple[str, str, str]]:
    """解析一个 ``requirements.txt``，返回 ``[(pkg_name, constraint, raw_line)]``。

    跳过：空行、``#`` 注释（整行/行尾）、``-r`` / ``--requirement`` 嵌套、
    ``-e`` / ``--editable`` 可编辑安装、以及任何 ``--``/``-`` 开头的选项行。

    读失败（文件不存在 / 编码错误）时返回空列表，不抛异常——聚合永不因单个
    Pack 的 requirements 读不了而中断（对齐 V1 非致命语义）。
    """
    try:
        text = Path(req_path).read_text(encoding="utf-8")
    except OSError:
        return []

    out: list[tuple[str, str, str]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r") or line.startswith("--requirement"):
            continue
        if line.startswith("-e") or line.startswith("--editable"):
            continue
        if line.startswith("--") or line.startswith("-"):
            continue
        name, constraint = split_pkg_spec(line)
        if not name:
            continue
        out.append((name, constraint, line))
    return out


def aggregate(
    pack_root: Path,
) -> tuple[list[str], list[str], list[str]]:
    """聚合 ``pack_root/<id>/requirements.txt`` 的全部 Pack 依赖。

    遍历顺序 = pack 目录名字典序（确定性，对齐 V1）。跳过 ``_``/``.`` 开头目录、
    跳过 :data:`SKIP_PKGS`。同包不同约束时 **first-wins**（首个胜，对齐 V1）；
    属于 :data:`NO_DEPS_PKGS` 的包归入独立的 ``no_deps`` 桶（修正 D-2）。

    Returns:
        ``(normal_specs, no_deps_specs, sources)``：

        * ``normal_specs``  —— 普通安装 spec 列表（如 ``"numpy>=1.24"`` / ``"jieba"``），
          按 spec 字符串排序，确定性输出。
        * ``no_deps_specs`` —— 需 ``--no-deps`` 安装的 spec 列表（如
          ``"openai-whisper==20250625"``），同样排序。
        * ``sources``       —— 贡献了至少一个非 skip 依赖的 Pack id 列表（字典序）。
    """
    pack_root = Path(pack_root)
    normal: dict[str, str] = {}
    no_deps: dict[str, str] = {}
    sources: list[str] = []

    if not pack_root.exists():
        return [], [], []

    for pack_dir in sorted(p for p in pack_root.iterdir() if p.is_dir()):
        if pack_dir.name.startswith("_") or pack_dir.name.startswith("."):
            continue
        req_file = pack_dir / "requirements.txt"
        if not req_file.is_file():
            continue

        contributed = False
        for pkg_name, constraint, _line in parse_requirements(req_file):
            norm = normalize_name(pkg_name)
            if norm in SKIP_PKGS:
                continue
            contributed = True
            spec = (pkg_name + constraint) if constraint else pkg_name
            bucket = no_deps if norm in NO_DEPS_PKGS else normal
            bucket.setdefault(norm, spec)  # first-wins (对齐 V1)

        if contributed:
            sources.append(pack_dir.name)

    return (
        sorted(normal.values(), key=str.lower),
        sorted(no_deps.values(), key=str.lower),
        sources,
    )
