# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Bundled-executable PATH resolution — prefer OUR tools over the host's.

Problem
-------
When the application (or any child process it spawns) invokes an external
command by a **bare name** (``uvx``, ``npx``, ``git`` …), the OS resolves it
against ``PATH``. On a developer / user machine ``PATH`` frequently points at an
UNRELATED tool — e.g. ``C:\\Programs\\Python\\Python312\\Scripts\\uvx.exe`` — rather
than the copy this application ships / manages under its own runtime roots. That
makes behaviour depend on whatever happens to be first on the host ``PATH``.

Solution (generic, not per-command)
------------------------------------
Rather than patch every individual spawn site (MCP client, hook engine,
background-process manager, exec tool, model-builder pipeline, download engines
…), this module provides ONE place that computes the application-owned binary
directories and prepends them to a ``PATH`` string. Because virtually every
spawn site inherits the parent process environment (either ``env=None`` or a
copy of ``os.environ``), prepending these directories to the CURRENT process's
``os.environ["PATH"]`` **once at startup** makes every subsequent child prefer
our tools automatically — the generic, universal fix.

Configuration (not hard-coded)
------------------------------
The directory *roots* + probe *patterns* are declared in
``factory/config/exec_path_dirs.json`` (a RUNTIME BASELINE, like
``file_guard_paths.json``; edit that file to change the set). Each root's
``path`` may carry ``%ENV%`` variables (expanded at load; a root whose variable
is unset is skipped) and the special ``${DATA_ROOT}`` token (substituted with
the resolved runtime data root). Only directories that actually EXIST on disk
are returned, in the declared order (earlier roots win). When the config file is
absent / malformed, a built-in default set (the two canonical roots below) is
used so the fix keeps working even without the file.

Application-owned roots (State-Truth-First, AGENTS.md §5)
---------------------------------------------------------
1. ``%LOCALAPPDATA%\\QAIModelBuilder`` — the per-user application data root the
   ARM64 venv + ``Setup.bat`` create (its ``node`` subdir holds the bundled
   portable Node.js toolchain: ``npx`` / ``npm`` / ``pnpm`` / ``node``).
2. ``<data_root>/bin`` — the current project's download/binary directory.

Cross-platform (AGENTS.md §8)
-----------------------------
A missing / empty ``LOCALAPPDATA`` (non-Windows, stripped env) leaves the
``%LOCALAPPDATA%`` root's ``%VAR%`` unexpanded, so that root is silently skipped
— never crashes, never imports anything Windows-only.

Layering (AGENTS.md §3.5): imports the stdlib only (no cross-context / config
imports), so the platform shared kernel stays dependency-light.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

__all__ = [
    "bundled_bin_dirs",
    "prepend_bundled_paths",
    "prepend_bundled_paths_to_process",
]

#: Location of the runtime baseline config, relative to the repo/install root.
_CONFIG_RELPATH: tuple[str, ...] = ("factory", "config", "exec_path_dirs.json")

#: The ``${DATA_ROOT}`` token, substituted with the resolved runtime data root.
_DATA_ROOT_TOKEN: str = "${DATA_ROOT}"

#: Built-in default roots used when the config file is absent / malformed. Mirrors
#: the shipped ``exec_path_dirs.json`` so the fix keeps working without the file.
_DEFAULT_ROOTS: tuple[dict[str, Any], ...] = (
    {
        "path": r"%LOCALAPPDATA%\QAIModelBuilder",
        "subdirs": ["", "bin", "Scripts", "node"],
        "globs": ["envs/*/Scripts", "envs/*/bin"],
    },
    {
        "path": _DATA_ROOT_TOKEN + r"\bin",
        "subdirs": [""],
        "include_children": True,
    },
)


def _expand_root_path(raw: str, data_root: Path | None) -> str:
    """Expand ``${DATA_ROOT}`` + ``%ENV%`` in a root ``path``.

    Returns "" (skip this root) when a required source is unavailable: an unset
    ``%ENV%`` variable (``os.path.expandvars`` leaves the ``%VAR%`` literal — we
    treat a residual ``%`` as "unset") or a ``${DATA_ROOT}`` token with no
    ``data_root`` provided. Mirrors ``_workspace_resolver._expand_env_path``.
    """
    if not raw:
        return ""
    if _DATA_ROOT_TOKEN in raw:
        if data_root is None:
            return ""
        raw = raw.replace(_DATA_ROOT_TOKEN, str(data_root))
    expanded = os.path.expandvars(raw)
    if "%" in expanded:
        return ""  # an unexpanded %VAR% remained → variable unset → skip
    return expanded


def _candidates_for_root(root: dict[str, Any], data_root: Path | None) -> list[Path]:
    """Expand one config root entry into concrete candidate directories."""
    raw_path = root.get("path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return []
    base_str = _expand_root_path(raw_path.strip(), data_root)
    if not base_str:
        return []
    try:
        base = Path(base_str)
    except (TypeError, ValueError):  # pragma: no cover — defensive
        return []

    out: list[Path] = []

    subdirs = root.get("subdirs")
    if isinstance(subdirs, list):
        for sub in subdirs:
            if not isinstance(sub, str):
                continue
            out.append(base / sub if sub else base)
    elif subdirs is None:
        out.append(base)

    globs = root.get("globs")
    if isinstance(globs, list):
        for pattern in globs:
            if not isinstance(pattern, str) or not pattern:
                continue
            try:
                out.extend(sorted(base.glob(pattern)))
            except OSError:  # pragma: no cover — glob on a bad root
                pass

    if bool(root.get("include_children", False)):
        try:
            out.extend(sorted(c for c in base.iterdir() if c.is_dir()))
        except OSError:
            pass

    return out


def _load_config_roots(repo_root: Path | None) -> tuple[dict[str, Any], ...]:
    """Load root entries from ``factory/config/exec_path_dirs.json``.

    Falls back to :data:`_DEFAULT_ROOTS` when ``repo_root`` is unknown or the
    file is absent / malformed / empty. Never raises — a config typo must not
    wedge startup.
    """
    if repo_root is None:
        return _DEFAULT_ROOTS
    cfg_path = repo_root.joinpath(*_CONFIG_RELPATH)
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — absent / unreadable / malformed → defaults
        return _DEFAULT_ROOTS
    roots = data.get("roots") if isinstance(data, dict) else None
    if not isinstance(roots, list) or not roots:
        return _DEFAULT_ROOTS
    parsed = tuple(r for r in roots if isinstance(r, dict))
    return parsed or _DEFAULT_ROOTS


def _dedup_existing_dirs(candidates: list[Path]) -> list[str]:
    """Return existing candidate dirs as strings, de-duped, order preserved."""
    seen: set[str] = set()
    out: list[str] = []
    for cand in candidates:
        try:
            if not cand.is_dir():
                continue
            key = os.path.normcase(str(cand))
        except OSError:  # pragma: no cover — defensive against odd FS states
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(str(cand))
    return out


def bundled_bin_dirs(
    data_root: Path | None = None, *, repo_root: Path | None = None
) -> tuple[str, ...]:
    """Return application-owned executable directories, highest priority first.

    Roots + probe patterns come from ``factory/config/exec_path_dirs.json``
    (located under ``repo_root``); a missing / malformed file falls back to the
    built-in defaults. ``%LOCALAPPDATA%\\QAIModelBuilder`` and ``<data_root>/bin``
    are resolved at runtime — never hard-coded. Only directories that exist are
    returned (existence is the real state; a non-existent candidate is skipped).

    Both ``data_root`` and ``repo_root`` are optional so callers without a
    resolved root (early startup / tests) still get whatever resolves.
    """
    candidates: list[Path] = []
    for root in _load_config_roots(repo_root):
        candidates.extend(_candidates_for_root(root, data_root))
    return tuple(_dedup_existing_dirs(candidates))


def prepend_bundled_paths(
    path_value: str | None,
    data_root: Path | None = None,
    *,
    repo_root: Path | None = None,
) -> str:
    """Return ``path_value`` with the bundled bin dirs prepended.

    Pure string transform (no ``os.environ`` mutation) so callers that build a
    child ``env`` dict can reuse it. Any bundled dir that already appears in
    ``path_value`` is removed from its old position first, so the result is
    idempotent: re-applying never stacks duplicate entries, and the bundled
    dirs always win resolution.
    """
    bundled = bundled_bin_dirs(data_root, repo_root=repo_root)
    existing = path_value or ""
    if not bundled:
        return existing
    bundled_keys = {os.path.normcase(p) for p in bundled}
    # Drop any prior occurrence of a bundled dir so re-prepending is idempotent.
    tail_parts = [
        p
        for p in existing.split(os.pathsep)
        if p and os.path.normcase(p) not in bundled_keys
    ]
    prefix = os.pathsep.join(bundled)
    if not tail_parts:
        return prefix
    return prefix + os.pathsep + os.pathsep.join(tail_parts)


def prepend_bundled_paths_to_process(
    data_root: Path | None = None, *, repo_root: Path | None = None
) -> tuple[str, ...]:
    """Prepend the bundled bin dirs to THIS process's ``os.environ['PATH']``.

    Call once at startup. Because child processes inherit ``os.environ`` (spawn
    sites use ``env=None`` or a copy of ``os.environ``), this single mutation
    makes every subsequently-spawned child prefer application-owned tools.

    Idempotent: re-invoking does not stack duplicate entries. Returns the tuple
    of directories that were prepended (possibly empty).
    """
    bundled = bundled_bin_dirs(data_root, repo_root=repo_root)
    if not bundled:
        return ()
    # Re-prepend even if a bundled dir already appears later in PATH: the point
    # is PRIORITY — a bundled dir might currently sit AFTER an unrelated host
    # tool dir (the exact defect this fixes). The internal de-dup keeps the
    # bundled prefix free of repeats, so re-invocation stays idempotent in
    # effect (bundled dirs win; host order after them is preserved).
    os.environ["PATH"] = prepend_bundled_paths(
        os.environ.get("PATH", ""), data_root, repo_root=repo_root
    )
    return bundled
