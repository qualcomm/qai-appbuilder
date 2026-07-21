# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Dangerous write-directory classification (pure domain).

Mirrors the legacy ``backend/security/sandbox_policy_builder.py:803-838``
write-dir filtering: certain system roots and per-user profile directories
must never be granted sandbox write access, even if the use-case layer
resolved them as candidate ``write_dirs``.

Two classes of blocked targets:

* **Exact** — the directory itself is rejected (but not arbitrary
  sub-paths). Covers the per-user profile / temp roots resolved from the
  environment (``TEMP`` / ``TMP`` / ``APPDATA`` / ``LOCALAPPDATA`` /
  ``USERPROFILE``) plus the model-builder *workspace root* (historically
  the hard-coded ``C:\\WoS_AI``; now configurable — the caller injects the
  resolved value via ``workspace_root`` so changing the workspace makes the
  block list follow). Blocking the workspace *root* itself (not its
  ``<root>/<model>/...`` sub-dirs) prevents a skill from writing directly
  at the root while still allowing per-model artifact sub-dirs.
* **Prefix** — the directory *or any descendant* is rejected. Covers the
  Windows system roots (``C:\\Windows``, ``C:\\Program Files`` /
  ``(x86)``, ``C:\\ProgramData``, ``C:\\$Recycle.Bin``).

Purity
------

This module performs **no** environment or filesystem I/O. The five
env-derived exact roots are resolved from a caller-supplied ``env``
mapping (``os.environ`` is read by the *adapter* that calls this helper,
not here), and the configurable workspace root is supplied by the caller
(``apps.api._workspace_resolver.resolve_workspace_root`` via the adapter),
so the security domain stays import-pure.
"""

from __future__ import annotations

import ntpath
from collections.abc import Mapping

__all__ = [
    "BLOCKED_WRITE_DIRS_EXACT_ENV_KEYS",
    "BLOCKED_WRITE_DIRS_EXACT_FIXED",
    "BLOCKED_WRITE_DIRS_PREFIXES",
    "DEFAULT_WORKSPACE_ROOT",
    "blocked_write_dirs_fixed",
    "is_blocked_for_write",
]

# Environment variables whose resolved value is an *exact* blocked write
# directory. Matches legacy ``sandbox_policy_builder.py:806``.
BLOCKED_WRITE_DIRS_EXACT_ENV_KEYS: tuple[str, ...] = (
    "TEMP",
    "TMP",
    "APPDATA",
    "LOCALAPPDATA",
    "USERPROFILE",
)

# Canonical fallback for the model-builder workspace root when the caller
# does not inject a configured value. Mirrors
# ``apps.api._workspace_resolver.DEFAULT_WORKSPACE_ROOT`` /
# ``WorkspaceSettings.model_root`` so the three never drift. Kept in
# backslash form to match the legacy ``sandbox_policy_builder.py:810``
# literal (``_norm`` normalises either separator anyway).
DEFAULT_WORKSPACE_ROOT = r"C:\WoS_AI"

# Fixed exact-match blocked directories (no env resolution). Matches legacy
# ``sandbox_policy_builder.py:810``.
#
# Historically a hard-coded ``(r"C:\WoS_AI",)``. The workspace root is now
# configurable, so this module-level constant only carries the canonical
# *default*; the adapter that knows the live workspace root injects it via
# :func:`blocked_write_dirs_fixed` / the ``workspace_root`` argument of
# :func:`is_blocked_for_write`. Kept as a constant for backward
# compatibility (existing imports + the default-fallback behaviour).
BLOCKED_WRITE_DIRS_FIXED: tuple[str, ...] = (DEFAULT_WORKSPACE_ROOT,)

# Backwards-compatible alias kept descriptive for the fixed set.
BLOCKED_WRITE_DIRS_EXACT_FIXED = BLOCKED_WRITE_DIRS_FIXED


def blocked_write_dirs_fixed(
    workspace_root: str | None = None,
) -> tuple[str, ...]:
    """Return the fixed exact-match blocked roots for ``workspace_root``.

    The model-builder workspace root is configurable; the *adapter* layer
    resolves the live value (``resolve_workspace_root``) and passes it here
    so a user who changes the workspace makes the write-block list follow.
    When ``workspace_root`` is omitted (or blank) this falls back to the
    canonical :data:`DEFAULT_WORKSPACE_ROOT`, preserving the legacy
    behaviour for callers that do not yet inject it.
    """
    root = (workspace_root or "").strip() or DEFAULT_WORKSPACE_ROOT
    return (root,)

# Prefix-match blocked directories: the directory itself and any descendant
# is rejected. Matches legacy ``sandbox_policy_builder.py:812-818``.
BLOCKED_WRITE_DIRS_PREFIXES: tuple[str, ...] = (
    r"C:\Windows",
    r"C:\Program Files",
    r"C:\Program Files (x86)",
    r"C:\ProgramData",
    r"C:\$Recycle.Bin",
)


def _norm(path: str) -> str:
    """Windows case-insensitive normalisation (``os.path.normcase`` on Win).

    Uses :mod:`ntpath` directly so the function is deterministic regardless
    of the host platform (the policy targets Windows paths, and tests
    must reproduce the casefold/backslash behaviour on any CI lane).
    """
    return ntpath.normcase(ntpath.normpath(path))


def is_blocked_for_write(
    path: str,
    env: Mapping[str, str],
    *,
    workspace_root: str | None = None,
) -> bool:
    """Return ``True`` if ``path`` must be denied sandbox write access.

    Parameters
    ----------
    path:
        A candidate write directory (absolute Windows path).
    env:
        Environment snapshot (typically ``os.environ``) supplied by the
        adapter; the five ``BLOCKED_WRITE_DIRS_EXACT_ENV_KEYS`` are resolved
        from this mapping so the domain performs no ``os.environ`` access.
    workspace_root:
        The configurable model-builder workspace root, injected by the
        adapter (``resolve_workspace_root``). The workspace *root* itself is
        an exact-match blocked write dir (its ``<root>/<model>/...``
        descendants stay writable). When omitted it falls back to
        :data:`DEFAULT_WORKSPACE_ROOT` (backward-compatible).

    Semantics mirror legacy ``sandbox_policy_builder.py:820-836``:

    * exact match against the env-resolved profile/temp roots + the
      workspace root;
    * prefix match (``== prefix`` or ``startswith(prefix + sep)``) against
      the system roots.
    """
    if not path:
        return False

    np = _norm(path)

    # Exact-match blocked roots (env-resolved + workspace root).
    exact: set[str] = set()
    for key in BLOCKED_WRITE_DIRS_EXACT_ENV_KEYS:
        val = env.get(key, "")
        if val:
            exact.add(_norm(val))
    for fixed in blocked_write_dirs_fixed(workspace_root):
        exact.add(_norm(fixed))

    if np in exact:
        return True

    # Prefix-match blocked roots.
    for prefix in BLOCKED_WRITE_DIRS_PREFIXES:
        pref = _norm(prefix)
        if np == pref or np.startswith(pref + ntpath.sep):
            return True

    return False
