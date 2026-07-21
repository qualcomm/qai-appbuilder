# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Composition-root helper: resolve the FileGuard *guard* token.

Background (2026-07-06 guard-only reversal, see
``docs/90-refactor/DESIGN-fileguard-guard-only-agent-exec-tree-2026-07-06.md``
section 4.2). The native ``guard64.dll`` was reversed so that a child
process is only *guarded* (routed through the ASK pipeline) when it
inherits a non-empty ``QAI_FILEGUARD_GUARD_TOKEN`` env var; every child
WITHOUT the marker is bypassed (allow-all). The marker propagates to the
whole subtree via ordinary Windows env inheritance.

Only the two subprocess-spawning LLM tools — ``exec`` and
``background_process`` — must inject this marker, so their spawned
process trees are guarded while all other host-spawned children (uv /
pip / mcp / worker / dep_checker / ...) stay un-marked and bypassed.

Layering: this module lives in the ``apps/api`` composition root, which
is the ONLY layer allowed to touch ``qai.security`` (the ``platform`` /
``tools`` / ``ai_coding`` spawn sites cannot import a bounded context —
``context-isolation`` import-linter contract). It resolves the runtime
token off the live native-guard adapter and hands the *value* (or a
zero-argument provider that re-reads it) down to the spawn sites, so no
cross-layer import is introduced and the token never enters the host
``os.environ`` (it is only added to the per-spawn child env copy).

Token identity: we reuse the SAME per-launch random token the native
adapter already generates (``get_trusted_infra_token`` — the historical
name; the DLL's guard-token check only tests *presence*, not value, and
the legacy trust-token no longer participates in the decision). Reusing
it avoids adding a second token generator for no behavioural gain.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing-only
    from .di import Container

__all__ = [
    "build_ask_pending_probe",
    "build_guard_token_provider",
    "resolve_guard_token",
]

_log = get_logger(__name__)


def resolve_guard_token(container: Container | None) -> str | None:
    """Return the live FileGuard guard-token, or ``None`` when unavailable.

    ``None`` is returned when the container / security context / native
    guard adapter is missing or disabled (``DisabledNativeFileGuard`` and
    an un-started ``NativeFileGuard`` both return ``None`` from
    ``get_trusted_infra_token``). A ``None`` token means the spawn site
    injects NOTHING, so the child stays bypassed — the safe, non-guarding
    degradation required by the design (no crash, no spurious ASK storm).

    Never raises — a diagnostics glitch must never block a tool spawn.
    """
    try:
        if container is None:
            return None
        security = getattr(container, "security", None)
        adapter = getattr(security, "native_file_guard", None)
        getter = getattr(adapter, "get_trusted_infra_token", None)
        if getter is None:
            return None
        token = getter()
    except Exception:  # noqa: BLE001 — token lookup must never break a spawn
        _log.debug("guard_token.lookup_failed", exc_info=True)
        return None
    if not isinstance(token, str) or not token:
        return None
    return token


def build_guard_token_provider(
    container: Container | None,
) -> Callable[[], str | None]:
    """Return a zero-arg provider that re-reads the live guard-token.

    A provider (rather than a snapshot) is handed to the spawn sites so
    each spawn reflects the CURRENT guard state: the native adapter is
    started lazily in lifespan, so a snapshot taken at wiring time could
    be ``None`` even though the guard is active by the time the first
    ``exec`` / ``background_process`` tool runs (State-Truth-First).
    """

    def _provider() -> str | None:
        return resolve_guard_token(container)

    return _provider


def build_ask_pending_probe(
    container: Container | None,
) -> Callable[[int], bool]:
    """Return ``probe(child_pid) -> bool``: is a native ASK pending on it?

    2026-07-08 — used by the exec tool's timeout to PAUSE instead of killing
    while the spawned child (e.g. powershell) is suspended by the native
    FileGuard hook waiting for the user to approve a file access. Without this
    the wall-clock timeout would force-kill the child mid-decision.

    Given the exec child's pid, returns ``True`` iff any pid currently holding
    a live native ASK (from ``PermissionWaitRegistry.pending_pids`` — the
    AUTHORITY, State-Truth-First) is that child OR a descendant of it (the
    real file access is often done by a grandchild; e.g. exec spawns the shell
    which spawns powershell). Descendant check uses ``psutil`` best-effort.

    Layering: lives in the apps composition root (the only layer allowed to
    read ``qai.security``); hands a context-neutral ``Callable[[int], bool]``
    to the tools/platform spawn sites (no cross-context import there). Never
    raises — a probe glitch must degrade to ``False`` so the deadline still
    fires (orphan-safe; we never STALL a kill on uncertainty).
    """

    def _probe(child_pid: int) -> bool:
        try:
            security = getattr(container, "security", None)
            registry = getattr(security, "permission_wait_registry", None)
            pids_getter = getattr(registry, "pending_pids", None)
            if pids_getter is None:
                return False
            ask_pids = pids_getter()
            if not ask_pids:
                return False
            # Fast path: the child itself is the one blocked on an ASK.
            if child_pid in ask_pids:
                return True
            # Otherwise check whether any ASK pid is a descendant of child_pid.
            try:
                import psutil  # local import — optional dep, apps-layer only

                child = psutil.Process(child_pid)
                descendant_pids = {p.pid for p in child.children(recursive=True)}
            except Exception:  # noqa: BLE001 — psutil miss / dead pid → no descendants
                return False
            return bool(ask_pids & descendant_pids)
        except Exception:  # noqa: BLE001 — probe must never stall a kill
            _log.debug("ask_pending_probe.failed", exc_info=True)
            return False

    return _probe
