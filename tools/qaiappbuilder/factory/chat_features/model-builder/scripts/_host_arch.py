# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Host OS / arch detection shared by every model-builder script.

Single source of truth so the pipeline, converters, generator, and runner
agree on the same host classification. Never re-implement -- import from
here.

Priority:
  1. data/config/host_arch (written by Setup.bat --arch), which trumps
     runtime probes because x64 processes under WoS Prism emulation lie
     via platform.machine().
  2. sys.platform + platform.machine().

Returned host_os values:
  'windows-arm64' | 'windows-x64' | 'linux-aarch64' | 'linux-x64' | 'unknown'
"""
from __future__ import annotations

import os
import platform
import sys
from pathlib import Path

_ARM64_ALIASES = ("arm64", "aarch64")
_X64_ALIASES = ("x64", "amd64", "x86_64")


def _read_host_arch_file() -> str:
    """Read data/config/host_arch walking up from this file.

    Returns 'arm64' | 'x64' | '' (empty if not found / unrecognised).
    """
    here = Path(__file__).resolve()
    for parent in [here.parent, *here.parents]:
        candidate = parent / "data" / "config" / "host_arch"
        if candidate.is_file():
            try:
                value = candidate.read_text(encoding="utf-8").strip().lower()
            except OSError:
                return ""
            if value in _ARM64_ALIASES:
                return "arm64"
            if value in _X64_ALIASES:
                return "x64"
            return ""
        # Stop walking once we've clearly exited the repo (avoid touching
        # unrelated ancestor dirs). Repo root has both `factory` and `src`.
        if (parent / "factory").is_dir() and (parent / "src").is_dir():
            return ""
    return ""


def detect_host_os() -> str:
    """Return the canonical host_os tag for the CURRENT machine."""
    if sys.platform == "win32":
        arch = _read_host_arch_file()
        if not arch:
            machine = platform.machine().lower()
            arch = "arm64" if machine in _ARM64_ALIASES else "x64"
        return "windows-arm64" if arch == "arm64" else "windows-x64"
    if sys.platform.startswith("linux"):
        machine = platform.machine().lower()
        if machine in _ARM64_ALIASES:
            return "linux-aarch64"
        if machine in _X64_ALIASES:
            return "linux-x64"
    return "unknown"


def sdk_bin_subdir(host_os: str | None = None) -> str:
    """Return the QAIRT SDK bin/lib subdirectory for the current host."""
    host_os = host_os or detect_host_os()
    return {
        "windows-arm64": "aarch64-windows-msvc",
        "windows-x64": "x86_64-windows-msvc",
        "linux-aarch64": "aarch64-oe-linux-gcc11.2",
        "linux-x64": "x86_64-linux-clang",
    }.get(host_os, "x86_64-windows-msvc")


def has_local_htp(host_os: str | None = None) -> bool:
    """True when a real Hexagon NPU is on the current host (real HTP execute)."""
    return (host_os or detect_host_os()) in ("windows-arm64", "linux-aarch64")


def resolve_runtime_venv(cfg: dict) -> str:
    """Return the runtime venv path from qairt_env.json.

    Reads keys in order: python_runtime_venv (canonical) ->
    python_arm64_venv (legacy alias, still written by setup_qairt_env.py
    on x64 hosts where it points at .venv_x64_313).
    """
    for key in ("python_runtime_venv", "python_arm64_venv"):
        value = cfg.get(key, "")
        if value:
            return os.path.expandvars(value)
    return ""
