# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Factory for the background-process manager.

Provides :func:`build_background_process_manager`, the single seam the
``apps/api`` DI layer calls to construct a
:class:`SubprocessBackgroundProcessManager` instance.  Kept in its own
module so the wiring is symmetric with the other ``qai.platform.*``
sub-packages (``platform/scheduling/factory.py`` /
``platform/persistence/secrets/factory.py``) and so the ``apps/api``
``_background_process_di.py`` stays a thin DI bridge.

The factory does **not** import :class:`Container` / FastAPI /
SQLAlchemy — callers pass the already-resolved primitives
(``events`` / ``data_root`` / optional ``kill_group``) so this module
can be re-used outside the HTTP app (e.g. CLI scripts, tests).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path

from qai.platform.events import EventBus
from qai.platform.process import ProcessKillGroup

from .manager import SubprocessBackgroundProcessManager

__all__ = ["build_background_process_manager"]


def build_background_process_manager(
    *,
    events: EventBus,
    data_root: Path,
    kill_group: ProcessKillGroup | None = None,
    guard_token_provider: Callable[[], str | None] | None = None,
    ask_pending_probe: Callable[[int], bool] | None = None,
    native_denial_probe: (
        Callable[[int, datetime], Awaitable[str]] | None
    ) = None,
    allow_x86: bool = False,
) -> SubprocessBackgroundProcessManager:
    """Construct a :class:`SubprocessBackgroundProcessManager`.

    Args:
        events: Shared :class:`EventBus` instance.  The manager publishes
            :class:`BackgroundProcessUpdated` / :class:`BackgroundProcessDeleted`
            events here so the ``/api/events`` SSE stream can re-broadcast
            them to web clients.
        data_root: Runtime data root (typically
            ``container.config.data_root``).  Reserved for future
            use; not currently consumed by the v1 manager which keeps
            all per-process state in memory.
        kill_group: Optional shared :class:`ProcessKillGroup`.  When
            provided, every spawned child is assigned to this Win32
            Job Object so a hard parent crash still reaps the
            children (AGENTS.md §🔴 State-Truth-First iron rule 5).
            Pass ``None`` to disable Job Object orphan-prevention (the
            manager still spawns / kills correctly; only the
            parent-crash safeguard is skipped).
        guard_token_provider: Optional zero-arg callable returning the
            live FileGuard guard-token (or ``None``). When provided and it
            returns a non-empty token, every spawned child receives
            ``QAI_FILEGUARD_GUARD_TOKEN`` so its subtree is guarded by the
            native ``guard64.dll``; ``None`` (or a ``None`` return) injects
            nothing and the child is bypassed. Injected by the
            ``apps/api`` composition root (the only layer allowed to read
            the ``qai.security`` native-guard adapter).
        ask_pending_probe: Optional callable ``(pid) -> bool`` reporting
            whether the FileGuard authorisation flow currently has an
            outstanding ``ask`` for that pid subtree; wired by the same
            composition root and consumed by the manager to decide whether
            to hold a subprocess in an ``awaiting-authorisation`` state
            vs. propagating its exit immediately.
        native_denial_probe: Optional async callable
            ``(root_pid, since) -> note_string`` composing the FileGuard native
            denial query with the diagnostic message builder. Injected by the
            apps/api composition root (which is the only layer that may read
            ``qai.security.**``). When provided, the manager calls it in
            ``_on_exit`` for non-zero-exit subprocesses to populate
            :attr:`Info.exit_diagnostics` with a FileGuard-authoritative note
            (surfaced to the LLM via ``background_process logs`` / ``status``).
            ``None`` keeps ``Info.exit_diagnostics`` empty (pre-D2 behaviour).
        allow_x86: When ``True``, injects ``QAI_GUARD_ALLOW_X86=1`` into
            every child process environment so the native guard64
            ``HookedCreateProcessW`` does not terminate 32-bit (x86)
            children. Default ``False`` (deny x86 — security default).
            Controlled by ``settings.security.allow_x86_processes``.

    Returns:
        A ready-to-use :class:`SubprocessBackgroundProcessManager`.
    """
    return SubprocessBackgroundProcessManager(
        events=events,
        data_root=data_root,
        kill_group=kill_group,
        guard_token_provider=guard_token_provider,
        ask_pending_probe=ask_pending_probe,
        native_denial_probe=native_denial_probe,
        allow_x86=allow_x86,
    )
