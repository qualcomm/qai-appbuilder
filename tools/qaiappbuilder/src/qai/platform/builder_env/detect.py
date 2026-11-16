# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Model-conversion environment: is it usable? (pure, stdlib only)

The single switch the conversion scripts really depend on is
``data/config/qairt_env.json`` -- it says WHERE the QAIRT SDK and the x86_64
Python 3.10 converter venv are. But the file alone proves nothing (it can outlive
a deleted SDK, or be hand-edited), so the answer here is two-step:

1. read the JSON and take ``qairt_sdk_root`` and ``python_x64_venv`` from it;
2. verify those two paths on disk. What "valid" looks like depends on the host:

   ============== ======================================== =====================
   host           SDK marker (under ``qairt_sdk_root``)     venv interpreter
   ============== ======================================== =====================
   Windows        ``bin/aarch64-windows-msvc/qnn-context-  ``Scripts/python.exe``
                  binary-generator.exe``
   Linux x86_64   ``bin/x86_64-linux-clang/                ``bin/python``
                  qnn-onnx-converter``
   Linux aarch64  ``bin/aarch64-oe-linux-gcc11.2/          ``bin/python``
                  qnn-net-run``
   ============== ======================================== =====================

   The Linux markers are the ones ``scripts/init/install_qairt.py`` (run by
   ``setup.sh``) treats as "SDK installed". On Linux aarch64 ``setup.sh`` points
   ``python_x64_venv`` at ``venv_aarch64_312`` (no x86_64 venv exists there).

   On Linux the environment is created by ``setup.sh``; the in-app installer only
   supports Windows (see ``installer_supported``).

Ready only when every check passes. A missing / unreadable / incomplete JSON is
"not ready": the conversion scripts read the same file, so a SDK sitting in the
default location without a config would still fail them.

This module must stay equivalent to ``status()`` in
``scripts/setup/install_builder_env.py`` (the installer's own ``--check``); a unit
test compares the two.
"""

from __future__ import annotations

import json
import os
import platform
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

__all__ = [
    "SDK_MARKER_REL",
    "BuilderEnvProblem",
    "BuilderEnvStatus",
    "detect_builder_env",
    "installer_supported",
    "read_release_version",
    "sdk_marker_rel",
    "venv_python_rel",
]

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(\.\d+)?$")

#: A valid Windows SDK has this file (same marker the installer verifies).
SDK_MARKER_REL = Path("bin") / "aarch64-windows-msvc" / "qnn-context-binary-generator.exe"
_SDK_MARKER_LINUX_X64 = Path("bin") / "x86_64-linux-clang" / "qnn-onnx-converter"
_SDK_MARKER_LINUX_ARM64 = Path("bin") / "aarch64-oe-linux-gcc11.2" / "qnn-net-run"
_VENV_PY_REL_WIN = Path("Scripts") / "python.exe"
_VENV_PY_REL_POSIX = Path("bin") / "python"


def _host() -> tuple[str, str]:
    """``(os, arch)`` of this machine: os is ``windows`` / ``linux`` / ``other``,
    arch is ``x86_64`` / ``aarch64`` / the raw lower-cased machine name."""
    if sys.platform == "win32":
        os_name = "windows"
    elif sys.platform.startswith("linux"):
        os_name = "linux"
    else:
        os_name = "other"
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x86_64"
    elif machine in ("aarch64", "arm64"):
        arch = "aarch64"
    else:
        arch = machine
    return os_name, arch


def sdk_marker_rel(os_name: str | None = None, arch: str | None = None) -> Path:
    """The file that proves a QAIRT SDK for this host is installed."""
    host_os, host_arch = _host()
    os_name, arch = os_name or host_os, arch or host_arch
    if os_name == "linux":
        return _SDK_MARKER_LINUX_ARM64 if arch == "aarch64" else _SDK_MARKER_LINUX_X64
    return SDK_MARKER_REL


def venv_python_rel(os_name: str | None = None) -> Path:
    """The interpreter inside a venv on this host."""
    os_name = os_name or _host()[0]
    return _VENV_PY_REL_WIN if os_name == "windows" else _VENV_PY_REL_POSIX


def installer_supported() -> bool:
    """Can the in-app installer (``install_builder_env.py``) run on this host?

    Windows only. On Linux the environment is set up by ``setup.sh``."""
    return _host()[0] == "windows"


@dataclass(frozen=True, slots=True)
class BuilderEnvProblem:
    """One reason the environment is not ready. ``code`` is a stable identifier."""

    code: str
    path: str = ""
    key: str = ""


@dataclass(frozen=True, slots=True)
class BuilderEnvStatus:
    ready: bool
    problems: tuple[BuilderEnvProblem, ...] = field(default_factory=tuple)
    config_file: str = ""
    sdk_root: str = ""
    venv: str = ""
    #: QAIRT version the config names (``qairt_version`` / ``_version`` / a version-named SDK
    #: folder), "" when it names none. Informational only: it does not affect ``ready``.
    sdk_version: str = ""

    @property
    def missing(self) -> tuple[str, ...]:
        """Coarse components that are not usable: ``config`` / ``sdk`` / ``venv``."""
        return tuple(sorted({p.code.split("_")[0] for p in self.problems}))


def _expand(raw: str) -> str:
    return os.path.expandvars(os.path.expanduser(raw))


def detect_builder_env(config_file: Path) -> BuilderEnvStatus:
    """Check the conversion environment described by *config_file*."""
    problems: list[BuilderEnvProblem] = []
    cfg: dict = {}
    cfg_str = str(config_file)
    readable = False

    if not config_file.exists():
        problems.append(BuilderEnvProblem("config_missing", path=cfg_str))
    else:
        try:
            loaded = json.loads(config_file.read_text(encoding="utf-8-sig"))
            if isinstance(loaded, dict):
                cfg = loaded
                readable = True
            else:
                problems.append(BuilderEnvProblem("config_invalid", path=cfg_str))
        except (OSError, ValueError):
            problems.append(BuilderEnvProblem("config_invalid", path=cfg_str))

    sdk_raw = cfg.get("qairt_sdk_root")
    venv_raw = cfg.get("python_x64_venv")
    sdk_root = _expand(sdk_raw) if isinstance(sdk_raw, str) and sdk_raw else ""
    venv = _expand(venv_raw) if isinstance(venv_raw, str) and venv_raw else ""

    if readable:  # an empty-but-valid object ({}) still has to report the missing keys
        if not sdk_root:
            problems.append(BuilderEnvProblem("sdk_not_configured", key="qairt_sdk_root"))
        elif not os.path.isabs(sdk_root):
            problems.append(BuilderEnvProblem("sdk_path_not_absolute", path=sdk_root))
        elif not (Path(sdk_root) / sdk_marker_rel()).is_file():
            problems.append(BuilderEnvProblem("sdk_missing", path=sdk_root))

        if not venv:
            problems.append(BuilderEnvProblem("venv_not_configured", key="python_x64_venv"))
        elif not os.path.isabs(venv):
            problems.append(BuilderEnvProblem("venv_path_not_absolute", path=venv))
        elif not (Path(venv) / venv_python_rel()).is_file():
            problems.append(BuilderEnvProblem("venv_missing", path=venv))

    return BuilderEnvStatus(
        ready=not problems,
        problems=tuple(problems),
        config_file=cfg_str,
        sdk_root=sdk_root,
        venv=venv,
        sdk_version=_config_version(cfg, sdk_root),
    )


def _config_version(cfg: dict, sdk_root: str) -> str:
    for key in ("qairt_version", "_version"):
        val = cfg.get(key)
        if isinstance(val, str) and _VERSION_RE.match(val.strip()):
            return val.strip()
    name = Path(sdk_root.replace("\\", "/").rstrip("/")).name if sdk_root else ""
    return name if _VERSION_RE.match(name) else ""


def read_release_version(release_file: Path) -> str:
    """``qairt_version`` from ``scripts/qairt_release.json`` (what an install would fetch), or ""."""
    try:
        data = json.loads(Path(release_file).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return ""
    val = data.get("qairt_version") if isinstance(data, dict) else None
    return val.strip() if isinstance(val, str) and _VERSION_RE.match(val.strip()) else ""
