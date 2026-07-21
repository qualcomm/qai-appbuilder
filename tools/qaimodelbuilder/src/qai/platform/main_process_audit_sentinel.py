# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Main-process audit sentinel: ``sys.addaudithook`` guard for protected paths.

This module is the MAIN-PROCESS AUDIT SENTINEL — the main-process arm of the
protected-paths defense (the third tier after the tool-handler checks and the
child-process audit sentinel's ``sitecustomize`` hook).
It installs a PEP 578 audit hook on the running interpreter so that ANY
in-process write into a protected prefix — including a direct
``open(qairt_path, "w")`` from some V2 code path that does NOT go through the
``write`` / ``edit`` tools — is denied by raising ``PermissionError`` from the
audited call site.

Why a deny-list, not V1's full PolicyCenter:
  V1's ``audit_hook.py`` drives the whole FileGuard policy (ASK dialogs, audit
  log IO, trusted-subprocess allow-lists). That requires careful re-entrancy /
  self-recursion handling because writing the audit log itself triggers
  ``open`` events. THIS hook is deliberately a pure DENY-LIST against
  :func:`qai.platform.protected_paths.is_write_blocked`: it never writes a log,
  never asks, never allow-lists, so it cannot recurse through its own IO. That
  keeps it tiny, always-safe to run unconditionally, and fully INDEPENDENT of
  FileGuard (which ships disabled). When FileGuard is later enabled it adds its
  OWN, separate hook/enforcement; the two do not conflict because this one only
  ever *denies* the (small, fixed) protected set and is a no-op for everything
  else — it can only make a write that FileGuard would also deny fail slightly
  earlier, never allow something FileGuard denies.

Events handled (write-intent only):
  ``open`` (write modes), ``os.open`` (write flags), ``os.rename`` /
  ``os.replace`` (destination + source), ``os.remove`` / ``os.unlink``,
  ``os.mkdir`` / ``os.makedirs`` / ``os.rmdir``, ``shutil.copyfile`` (dest).

Idempotent: install is a no-op after the first successful call.
Never raises at install time (a failed install logs a warning, never crashes
the app — but unlike a child sitecustomize, the main process is long-lived so a
failure here is surfaced for visibility).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from collections.abc import Callable

from qai.platform import protected_paths

logger = logging.getLogger("qai.platform.protected_paths.audit")

__all__ = ["install_protected_paths_audit_hook", "is_installed"]

# Per-thread re-entrancy guard. The hook itself must not recurse: although our
# deny check does no IO, ``os.path.abspath`` / ``GetLongPathNameW`` could in
# principle emit audit events on some platforms — the flag makes the hook a
# no-op while it is already executing on this thread.
_LOCAL = threading.local()

_INSTALLED = False

# P-17 §6.3 — optional, best-effort SYNC violation callback. When wired (by the
# apps layer via ``install_protected_paths_audit_hook(on_violation=...)``) it is
# invoked with ``(event: str, path: str)`` the instant a protected write is
# detected — BEFORE the ``PermissionError`` is raised — so the deny becomes
# observable (the in-process hook otherwise recorded NOTHING). CONTRACT:
#
#   * The signature is pure stdlib (``Callable[[str, str], None]``) — this
#     module lives in ``qai.platform`` and import-linter FORBIDS it from
#     importing ``qai.security``; the callback carries the security coupling on
#     the apps side, never here.
#   * It MUST be non-blocking / zero-IO: it runs inside the audit hook on the
#     audited call's thread, so any direct IO would re-enter PEP-578 and recurse.
#     The wired sink is expected to only enqueue into an in-memory queue.
#   * It is BEST-EFFORT: a callback that raises is swallowed and the
#     ``PermissionError`` is raised REGARDLESS — the deny (interception) semantic
#     never depends on the audit side succeeding.
#
# ``None`` (default) keeps the historical behaviour: the hook is inert wrt
# observability and only ever denies.
_ON_VIOLATION: Callable[[str, str], None] | None = None

# Write-intent ``os.open`` flags.
_WRITE_FLAGS = (
    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
)


def is_installed() -> bool:
    """True iff the audit hook has been installed in this process."""
    return _INSTALLED


def _open_mode_is_write(mode: object) -> bool:
    if isinstance(mode, str):
        return any(c in mode for c in ("w", "a", "x", "+"))
    if isinstance(mode, int):
        return bool(mode & _WRITE_FLAGS)
    return False


def _deny(path: object, prefix: str, event: str) -> None:
    raise PermissionError(
        "ProtectedPaths: refusing %s into protected path %r (under %r). This "
        "tree (e.g. the QAIRT SDK / C:\\Qualcomm) must not be modified — doing "
        "so corrupts the shared toolchain (e.g. truncating an .exe to 0 bytes, "
        "causing [WinError 193]). Use the data/sdk backup instead."
        % (event, str(path)[:160], prefix)
    )


def _check(path: object, event: str) -> None:
    if path is None:
        return
    # Narrow, explicit bypass for app-controlled restores INTO a protected tree
    # (e.g. the generator self-heal copying its backup back into C:\Qualcomm).
    # Set ``QAI_PROTECTED_PATHS_BYPASS=1`` only around that single operation.
    if os.environ.get("QAI_PROTECTED_PATHS_BYPASS", "") == "1":
        return
    try:
        spath = os.fspath(path)  # accepts str / bytes / os.PathLike
    except TypeError:
        return
    if isinstance(spath, bytes):
        try:
            spath = spath.decode("utf-8", "surrogateescape")
        except Exception:  # noqa: BLE001
            return
    if not isinstance(spath, str) or not spath:
        return
    matched = protected_paths.is_write_blocked(spath)
    if matched:
        # Best-effort bypass audit BEFORE denying. We are inside the
        # re-entrancy guard (``_LOCAL.in_hook`` is True) so an audit event the
        # callback might indirectly emit is a no-op — but the callback itself is
        # contractually zero-IO (enqueue only). A callback fault must NEVER stop
        # the deny: swallow anything it throws, then raise regardless.
        cb = _ON_VIOLATION
        if cb is not None:
            try:  # noqa: SIM105 — explicit swallow (contextlib import kept out)
                cb(event, spath)
            except Exception:  # noqa: BLE001,S110 — audit best-effort; deny wins
                pass
        _deny(spath, matched, event)


def _on_event(event: str, args: tuple) -> None:
    # Fast path: only a handful of write events matter.
    if event == "open":
        # args = (path, mode, flags)
        if len(args) >= 2 and _open_mode_is_write(args[1]):
            _enter_and_check(args[0] if args else None, event)
        elif len(args) >= 3 and _open_mode_is_write(args[2]):
            _enter_and_check(args[0] if args else None, event)
        return
    if event == "os.open":
        # args = (path, flags, mode)
        if len(args) >= 2 and _open_mode_is_write(args[1]):
            _enter_and_check(args[0], event)
        return
    if event in ("os.remove", "os.unlink", "os.mkdir", "os.rmdir", "os.truncate"):
        if args:
            _enter_and_check(args[0], event)
        return
    if event == "os.makedirs":
        # CPython emits os.mkdir per level; makedirs may also emit directly.
        if args:
            _enter_and_check(args[0], event)
        return
    if event in ("os.rename", "os.replace"):
        # args = (src, dst): block if EITHER side is protected.
        if len(args) >= 2:
            _enter_and_check(args[1], event)
        if len(args) >= 1:
            _enter_and_check(args[0], event)
        return
    if event == "shutil.copyfile":
        # args = (src, dst): block writes to a protected destination.
        if len(args) >= 2:
            _enter_and_check(args[1], event)
        return


def _enter_and_check(path: object, event: str) -> None:
    if getattr(_LOCAL, "in_hook", False):
        return
    _LOCAL.in_hook = True
    try:
        _check(path, event)
    finally:
        _LOCAL.in_hook = False


def _audit_hook(event: str, args: tuple) -> None:
    # PEP 578: raising here aborts the audited operation (this is how the write
    # is actually prevented). Any non-PermissionError is swallowed so an
    # unexpected bug never takes down the whole process via the audit hook.
    try:
        _on_event(event, args)
    except PermissionError:
        raise
    except Exception:  # noqa: BLE001
        return


def install_protected_paths_audit_hook(
    on_violation: Callable[[str, str], None] | None = None,
) -> bool:
    """Install the in-process protected-paths audit hook (idempotent).

    Returns True if installed (or already installed), False if installation
    failed. ALWAYS-ON: callers install this unconditionally at startup,
    independent of ``file_guard_enabled`` / the OS sandbox.

    ``on_violation`` — optional pure-stdlib sync callback ``(event, path)``
    invoked (best-effort, inside the re-entrancy guard, BEFORE the
    ``PermissionError``) each time a protected write is detected, so the deny
    becomes observable. It MUST be zero-IO / non-blocking (enqueue only) and a
    raising callback is swallowed — the deny is raised regardless. ``None``
    keeps the hook inert wrt observability (historical behaviour). See the
    :data:`_ON_VIOLATION` contract note. The parameter type is deliberately
    stdlib-only: this module may NOT import ``qai.security`` (import-linter).
    """
    global _INSTALLED, _ON_VIOLATION  # noqa: PLW0603 — module singleton state
    # Wiring the callback is independent of (and idempotent alongside) the
    # one-shot hook registration: a re-install with a fresh callback updates the
    # wired sink even though ``sys.addaudithook`` only ran once.
    if on_violation is not None:
        _ON_VIOLATION = on_violation
    if _INSTALLED:
        return True
    try:
        sys.addaudithook(_audit_hook)
        _INSTALLED = True
        logger.info(
            "protected_paths.audit_hook_installed prefixes=%d",
            len(protected_paths.protected_prefixes()),
        )
        return True
    except Exception:  # noqa: BLE001
        logger.warning(
            "protected_paths.audit_hook_install_failed — in-process write "
            "protection DISABLED (tool-layer + subprocess guards remain).",
            exc_info=True,
        )
        return False
