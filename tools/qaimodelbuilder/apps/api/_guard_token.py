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

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing-only
    from .di import Container

__all__ = [
    "build_ask_flush_for_pid",
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


def build_ask_flush_for_pid(
    container: Container | None,
) -> Callable[[int], Awaitable[list[str]]]:
    """Return ``flush(child_pid) -> Awaitable[list[str]]``: resolve its ASKs.

    Problem ② directed flush. When a chat session is STOPPED, the exec tool
    task is cancelled and the child process tree is killed — but nothing
    resolves the native ASK futures already queued in
    ``PermissionWaitRegistry`` for that child (or its descendants), so the
    FileGuard authorization dialogs keep popping until the 10s
    subprocess-gone backstop notices the pid died. This callable is the
    IMMEDIATE path: given the killed child's pid, it resolves every pending
    ASK whose registered pid == ``child_pid`` OR is a descendant of it as
    DENY, publishes a :class:`PermissionResolvedEvent` (``resolution=
    "stopped"``) so the front-end closes the dialog, and best-effort marks
    the durable store row resolved.

    Mirrors :func:`build_ask_pending_probe`'s pid + psutil-descendants
    matching but ACTS on the matches (resolve) instead of merely reporting
    them. It "silences the POPUP, not withdraws the REQUEST" (mirrors the
    ``/permission/cancel`` route): the domain aggregate is untouched — the
    published event is a pure UI-close signal.

    Layering: apps composition root — the only layer allowed to read
    ``qai.security``; hands a context-neutral ``Callable`` to the exec-tool
    cancel site. Resolving futures is synchronous/instant; the SSE publish
    is a fast ``await``. NEVER raises — a flush glitch must never break the
    cancel path (the caller re-raises ``CancelledError`` for responsiveness).
    Returns the list of request_ids actually flushed (``[]`` on any miss).
    """

    async def _flush(child_pid: int) -> list[str]:
        try:
            security = getattr(container, "security", None)
            registry = getattr(security, "permission_wait_registry", None)
            by_pid_getter = getattr(
                registry, "pending_request_ids_by_pid", None
            )
            if by_pid_getter is None:
                return []
            by_pid = by_pid_getter()
            if not by_pid:
                return []

            # Which pids to flush: the child itself + any of its descendants
            # still holding a live ASK. Descendant lookup is best-effort (the
            # tree may already be partly reaped by the time the cancel branch
            # calls us — the 10s backstop mops up anything psutil can no
            # longer see). Same matching shape as build_ask_pending_probe.
            target_pids: set[int] = {int(child_pid)}
            try:
                import psutil  # local import — optional dep, apps-layer only

                proc = psutil.Process(int(child_pid))
                target_pids |= {p.pid for p in proc.children(recursive=True)}
            except Exception:  # noqa: BLE001 — dead/unknown pid → child only
                pass

            rids: list[str] = []
            for pid in target_pids:
                rids.extend(by_pid.get(pid, ()))
            if not rids:
                return []

            events = getattr(container, "events", None)
            store = getattr(security, "permission_pending_store", None)
            clock = getattr(container, "clock", None)

            flushed: list[str] = []
            for rid in rids:
                # Resolve the queued ASK future as DENY (synchronous/instant).
                try:
                    woke = registry.resolve(rid, allow=False, scope="deny")
                except Exception:  # noqa: BLE001 — one bad rid must not abort
                    _log.debug(
                        "ask_flush.resolve_failed", exc_info=True
                    )
                    continue
                if not woke:
                    # Already resolved / unknown (a concurrent local response,
                    # or the backstop beat us) — nothing to close in the UI.
                    continue
                flushed.append(rid)
                # Publish the UI-close signal (fast await; best-effort).
                if events is not None:
                    try:
                        from qai.security.domain.events import (
                            PermissionResolvedEvent,
                        )
                        from qai.security.domain.value_objects import RequestId

                        occurred_at = (
                            clock.now() if clock is not None else None
                        )
                        if occurred_at is None:
                            from datetime import datetime, timezone

                            occurred_at = datetime.now(tz=timezone.utc)
                        await events.publish(
                            PermissionResolvedEvent(
                                request_id=RequestId(value=rid),
                                resolution="stopped",
                                occurred_at=occurred_at,
                            )
                        )
                    except Exception:  # noqa: BLE001 — publish glitch is non-fatal
                        _log.debug(
                            "ask_flush.publish_failed", exc_info=True
                        )
                # Best-effort durable mark so a restart / audit query can
                # distinguish this stop-flush from a user DENY.
                if store is not None:
                    try:
                        mark = getattr(store, "mark_resolved", None)
                        if mark is not None:
                            resolved_at = (
                                clock.now() if clock is not None else None
                            )
                            if resolved_at is None:
                                from datetime import datetime, timezone

                                resolved_at = datetime.now(tz=timezone.utc)
                            await mark(
                                request_id=rid,
                                resolved_at=resolved_at,
                                resolution="stopped",
                            )
                    except Exception:  # noqa: BLE001 — mark is best-effort
                        _log.debug("ask_flush.mark_failed", exc_info=True)
            return flushed
        except Exception:  # noqa: BLE001 — flush must never break the cancel path
            _log.debug("ask_flush.failed", exc_info=True)
            return []

    return _flush
