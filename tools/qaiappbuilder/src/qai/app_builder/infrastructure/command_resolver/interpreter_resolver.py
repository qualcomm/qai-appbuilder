# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Python interpreter resolvers — pluggable selectors for ``argv[0]``.

Mirrors the legacy ``backend/app_builder/runners/python_script.py::
_resolve_python_exe`` policy:

* ``"system"`` → :class:`SysExecutableResolver` (``sys.executable``)
* ``"arm64"``  → :class:`QairtEnvJsonResolver` reading
  ``<repo_root>/config/qairt_env.json`` (legacy compat) OR the new
  v2 path under ``data/config/qairt_env.json`` (post-cutover).
  Falls back to ``sys.executable`` when the env file is absent.

QAIRT environment hand-off (v2.7+ Pack runtime parity)
------------------------------------------------------

The legacy ``backend/app_builder/runners/python_script.py`` did more than
pick ``argv[0]``: it also injected the QAIRT SDK root (`QAIRT_ROOT` /
`QNN_SDK_ROOT`) plus the SDK's `bin/` and `lib/` directories at the
front of `PATH` so the spawned ``qai_appbuilder`` Python module could
load the QNN runtime DLLs. Without those env extras the Pack subprocess
fails with ``QAI_APPBUILDER_UNAVAILABLE`` even though the venv has the
``qai_appbuilder`` Python wheel installed.

To preserve that behaviour without re-introducing the legacy module, the
v2 :class:`QairtEnvJsonResolver` exposes:

* :attr:`qairt_root` — the resolved ``QAIRT_ROOT`` directory (Path) or
  ``None`` when the env file omits it.
* :meth:`extra_env` — dict of env vars the spawn must merge in
  (``QAIRT_ROOT`` / ``QNN_SDK_ROOT`` aliases).
* :meth:`path_segments` — list of absolute directories to *prepend* to
  ``PATH`` so the QNN DLLs are picked up before any system-wide copy.

The data lives in ``qairt_env.json`` so SDK upgrades are config-only —
no code change, no re-deploy. Production DI passes a resolver wired to
``data/config/qairt_env.json``; tests inject a hand-rolled instance
with :class:`SysExecutableResolver` as fallback.

Resolvers are pure: no side effects, no spawning. They are runtime
checked Protocols so DI can swap implementations in tests.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Protocol, runtime_checkable

from qai.platform.process.arch import current_arch

__all__ = [
    "PythonInterpreterResolver",
    "QairtEnvJsonResolver",
    "SysExecutableResolver",
]


@runtime_checkable
class PythonInterpreterResolver(Protocol):
    """Return the Python interpreter to use for a Pack runner."""

    def resolve(self) -> Path:
        """Return an absolute path to a working ``python`` executable.

        MUST not raise on missing config — fall back to a sane default.
        Callers detect "venv missing" by inspecting whether the file
        exists; a separate :class:`FileNotFoundError` is a programmer
        error.
        """
        ...


class SysExecutableResolver:
    """Always returns ``sys.executable``.

    Used for tests and the ``manifest.runner.venv == "system"`` policy.
    Returns no extra env / path segments — :meth:`extra_env` and
    :meth:`path_segments` always yield empty so callers can call them
    uniformly without isinstance-branching.
    """

    __slots__ = ()

    def resolve(self) -> Path:
        return Path(sys.executable)

    @property
    def qairt_root(self) -> Path | None:
        return None

    def extra_env(self) -> dict[str, str]:
        return {}

    def path_segments(self) -> list[str]:
        return []


class QairtEnvJsonResolver:
    """Read the venv path (and QAIRT SDK extras) from a ``qairt_env.json`` file.

    The file shape (legacy compat) accepts any of the following for the
    interpreter pointer:

    * ``{"venv_python": "<abs path>"}``
    * ``{"python_exe":  "<abs path>"}``
    * ``{"python_arm64_venv": "<abs venv dir>"}`` →
      ``<dir>/Scripts/python.exe`` is used.

    Plus the QAIRT SDK fields (parity with legacy
    ``backend/app_builder/runners/python_script.py``):

    * ``{"qairt_root": "<abs SDK root>"}`` — exported as both
      ``QAIRT_ROOT`` and (for backwards-compat with packs reading the
      old name) ``QNN_SDK_ROOT``; ``<root>\\bin\\arm64x-windows-msvc``
      and ``<root>\\lib\\arm64x-windows-msvc`` are prepended to
      ``PATH`` so the QNN runtime DLLs win against any system copy.

    String values may include ``%LOCALAPPDATA%`` / ``%USERPROFILE%`` /
    ``${HOME}`` — they are expanded via :func:`os.path.expandvars` and
    :func:`os.path.expanduser` before path resolution.

    Relative paths are resolved against ``repo_root`` (constructor
    arg). The resolver caches nothing — callers that want caching wrap
    it in their own LRU.

    When the env file is missing or unparseable, falls back to
    :class:`SysExecutableResolver` (instead of raising) so the
    inline-runner / dev workflow keeps working; in that case
    :attr:`qairt_root` is ``None`` and :meth:`extra_env` /
    :meth:`path_segments` return empty containers.
    """

    __slots__ = ("_env_file", "_repo_root", "_fallback")

    # QNN runtime subdir per process arch. The ``qairt_root``'s ``bin``
    # and ``lib`` sub-directories hold the QNN runtime DLLs the Pack
    # subprocess must find on ``PATH`` (arm64: QnnHtp*; x64: QnnCpu +
    # QnnModelDlc). ``bin`` is searched before ``lib`` (matches the
    # legacy module's ordering and how the Qualcomm SDK ships the
    # binaries). The active subdir is picked at runtime from the
    # ``qairt_runtime_subdir`` field (written by setup_qairt_env.py) or
    # falls back to :func:`current_arch`.
    _RUNTIME_SUBDIR_BY_ARCH: dict[str, str] = {
        "arm64": "arm64x-windows-msvc",
        "x64": "x86_64-windows-msvc",
    }

    def __init__(
        self,
        *,
        env_file: Path,
        repo_root: Path,
        fallback: PythonInterpreterResolver | None = None,
    ) -> None:
        if not isinstance(env_file, Path):
            raise TypeError("env_file must be a Path")
        if not isinstance(repo_root, Path):
            raise TypeError("repo_root must be a Path")
        self._env_file = env_file
        self._repo_root = repo_root
        self._fallback = fallback or SysExecutableResolver()

    # ------------------------------------------------------------------
    # Interpreter selection
    # ------------------------------------------------------------------
    def resolve(self) -> Path:
        data = self._load()
        if data is None:
            return self._fallback.resolve()
        # Try explicit python executable path first.
        for key in ("venv_python", "python_exe"):
            candidate = data.get(key)
            if isinstance(candidate, str) and candidate:
                p = self._resolve_path(candidate)
                if p.is_file():
                    return p
        # Try venv directory shape. Prefer the arch-resolved
        # ``python_runtime_venv`` (written by setup_qairt_env.py per
        # host arch); fall back to legacy ``python_arm64_venv`` so
        # pre-x64 configs keep byte-identical behaviour.
        venv_dir = data.get("python_runtime_venv") or data.get(
            "python_arm64_venv"
        )
        if isinstance(venv_dir, str) and venv_dir:
            p = self._resolve_path(venv_dir)
            python_exe = p / "Scripts" / "python.exe"
            if python_exe.is_file():
                return python_exe
            # POSIX fallback (testing on Linux/macOS dev box).
            python_exe_unix = p / "bin" / "python"
            if python_exe_unix.is_file():
                return python_exe_unix
        return self._fallback.resolve()

    # ------------------------------------------------------------------
    # QAIRT SDK extras (env + PATH)
    # ------------------------------------------------------------------
    @property
    def qairt_root(self) -> Path | None:
        """Resolved ``QAIRT_ROOT`` directory, or ``None`` if not configured."""
        data = self._load()
        if data is None:
            return None
        raw = data.get("qairt_root")
        if not isinstance(raw, str) or not raw:
            return None
        return self._resolve_path(raw)

    def extra_env(self) -> dict[str, str]:
        """Env vars to merge into the spawned subprocess.

        Returns ``{}`` when ``qairt_root`` is unset or unreadable —
        the caller (``_materialise_env``) is responsible for handling
        the missing-SDK case (the Pack subprocess will raise
        ``QAI_APPBUILDER_UNAVAILABLE`` at import time, which surfaces
        as a normal RUN_FAILED frame).
        """
        root = self.qairt_root
        if root is None:
            return {}
        root_str = str(root)
        return {
            # New canonical name (matches qai_appbuilder >= 2.46 docs).
            "QAIRT_ROOT": root_str,
            # Backwards-compat alias retained by legacy packs.
            "QNN_SDK_ROOT": root_str,
        }

    def _runtime_subdir(self, data: dict | None) -> str:
        """QNN runtime arch subdir (``arm64x-`` / ``x86_64-windows-msvc``).

        Prefers the ``qairt_runtime_subdir`` field written by
        ``setup_qairt_env.py``; falls back to :func:`current_arch` when
        the field is missing (pre-x64 configs → ``arm64x-windows-msvc``,
        byte-identical to the legacy hardcoding on ARM64 hosts).
        """
        if data is not None:
            raw = data.get("qairt_runtime_subdir")
            if isinstance(raw, str) and raw:
                return raw
        return self._RUNTIME_SUBDIR_BY_ARCH[current_arch()]

    def path_segments(self) -> list[str]:
        """Absolute directories to prepend to ``PATH`` (in order).

        Empty list when ``qairt_root`` is unset; otherwise the SDK's
        ``bin/`` and ``lib/`` arch sub-directories so the QNN runtime
        DLLs load from the SDK install rather than any system-wide
        copy. The arch subdir is chosen at runtime (``qairt_runtime_subdir``
        field, else :func:`current_arch`). Existence is *not* checked
        here (the SDK install layout is the user's responsibility); a
        missing dir simply means the loader falls through, which is the
        same behaviour as the legacy module.
        """
        data = self._load()
        root = self.qairt_root
        if root is None:
            return []
        subdir = self._runtime_subdir(data)
        return [
            str((root / "bin" / subdir).resolve()),
            str((root / "lib" / subdir).resolve()),
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _load(self) -> dict | None:
        """Load and decode the env file, or return ``None`` on failure."""
        try:
            data = json.loads(self._env_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data

    def _resolve_path(self, raw: str) -> Path:
        """Expand env vars / user dir, then anchor relative paths to repo_root."""
        expanded = os.path.expandvars(os.path.expanduser(raw))
        p = Path(expanded)
        if not p.is_absolute():
            p = (self._repo_root / p).resolve()
        return p
