# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the platform ``background_process`` sub-module.

This is NOT a bounded-context namespace — it's a platform shared kernel
module (mirrors ``_uploads_di.py``). The factory builds a
:class:`SubprocessBackgroundProcessManager` bound to:

* the shared :class:`qai.platform.events.EventBus` so updates fan out on
  the existing ``GET /api/events`` SSE multiplexer;
* the daemon's ``data_root`` (under ``container.data_paths.root``) for
  the manifest / log / control-sentinel layout described in
  ``docs/90-refactor/background-process-design.md`` section 5;
* a freshly-allocated :class:`ProcessKillGroup` (Win32 Job Object with
  ``KILL_ON_JOB_CLOSE``) so a hard parent death cleans up every spawned
  child at OS level (AGENTS.md State-Truth-First iron-rule 5).

The kill group is held by the services dataclass so its lifetime is
bound to the Container (avoiding GC of the Win32 Job Object handle
while the manager still holds child PIDs). Lifespan does not consume
it directly; OS-level ``KILL_ON_JOB_CLOSE`` handles reap automatically
when the daemon exits.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from qai.platform.background_process import build_background_process_manager
from qai.platform.background_process.ports import (
    BackgroundProcessManagerPort,
)
from qai.platform.process import ProcessKillGroup

if TYPE_CHECKING:  # pragma: no cover
    from ._chat_di import ChatServices
    from .di import Container

__all__ = [
    "BackgroundProcessServices",
    "build_background_process_services",
    "wire_background_process_tool_into_chat",
]


@dataclass(frozen=True, slots=True)
class BackgroundProcessServices:
    """Namespace bundling the BG-process manager + its kill group.

    Frozen so the wired graph cannot be mutated post-build. ``manager`` is
    typed against the port so route / lifespan callers depend on the
    Protocol, not the concrete subprocess adapter.
    """

    manager: BackgroundProcessManagerPort
    kill_group: ProcessKillGroup


def build_background_process_services(
    container: "Container",
) -> BackgroundProcessServices:
    """Wire ``container.background_process``.

    The manager's ``data_root`` is the runtime data directory
    (``container.data_paths.root``); the manager itself resolves it to an
    absolute path. Tests construct their own
    :class:`SubprocessBackgroundProcessManager` against a ``tmp_path``
    rather than going through this builder.
    """
    kill_group = ProcessKillGroup()
    # The manager defaults ``clock`` to ``time.time`` (a ``Callable[[], float]``
    # producing a Unix timestamp).  ``container.clock`` is the project-wide
    # :class:`qai.platform.time.Clock` Protocol whose ``now()`` returns a
    # ``datetime`` — a different shape — so we do NOT inject it here and let
    # the manager use its stdlib default.  Tests that need a frozen clock
    # construct the manager directly with their own ``Callable[[], float]``.
    #
    # FileGuard guard-token provider (2026-07-06 guard-only reversal): the
    # ``background_process`` tool is one of the two LLM tools that MUST mark
    # their spawned subtree as guarded (``QAI_FILEGUARD_GUARD_TOKEN``). The
    # provider is resolved here in the composition root (the only layer
    # allowed to read the ``qai.security`` native-guard adapter) and re-reads
    # the live token per spawn; ``None`` (guard off / not started) injects
    # nothing → child bypassed (safe degradation).
    from ._guard_token import (
        build_ask_pending_probe,
        build_guard_token_provider,
    )
    from ._native_denial_probe import build_native_denial_probe

    manager = build_background_process_manager(
        events=container.events,
        data_root=container.data_paths.root,
        kill_group=kill_group,
        guard_token_provider=build_guard_token_provider(container),
        ask_pending_probe=build_ask_pending_probe(container),
        # D2-D: FileGuard denial probe (see apps/api/_native_denial_probe.py).
        # Fail-open when audit_query is not wired — the manager treats a
        # None probe (or one that returns "") as "no diagnostics to add".
        native_denial_probe=build_native_denial_probe(container),
        allow_x86=container.settings.security.allow_x86_processes,
    )
    return BackgroundProcessServices(
        manager=manager,
        kill_group=kill_group,
    )


def wire_background_process_tool_into_chat(
    *,
    chat: "ChatServices",
    container: "Container",
) -> tuple[str, ...]:
    """Post-build hook: register the ``background_process`` LLM tool on chat.

    Routes an LLM-emitted ``background_process`` tool call to
    :func:`~qai.platform.background_process.tool_handlers.handle_background_process`,
    backed by the live manager wired in ``container.background_process.manager``.

    Returns the names registered (``("background_process",)`` on success,
    ``()`` on a graceful no-op — e.g. the chat tools port is not the
    registry-backed adapter or registration raised). Mirrors the contract
    of :func:`apps.api._chat_di.wire_web_search_tool_into_chat`: best-effort,
    never blocks daemon startup; the actual bridge lives in
    :mod:`apps.api._chat_background_process_tool_bridge` so cross-context
    composition stays in one apps-layer module.

    Called from ``Container._build_contexts`` right after
    ``build_background_process_services`` so the registry sees the freshly-
    wired manager. Chat startup does NOT depend on this — if registration
    fails (or the chat tools port is a mock), the chat surface still
    works, only the ``background_process`` tool is invisible to the LLM.
    """
    from ._chat_background_process_tool_bridge import (
        register_background_process_tool_into_chat,
    )

    # 2026-07-09 — build the SAME FileGuardPort exec uses so background_process
    # ``start`` runs the identical command-level authorization (enforce_exec +
    # protected-write sentinel) before spawning. Best-effort: a build failure
    # degrades to no guard (allow-all), matching the tool's prior behaviour;
    # background stays registered so it keeps working.
    _file_guard: object | None = None
    try:
        from ._file_guard_bridge import build_file_guard

        _file_guard = build_file_guard(container)
    except Exception:  # noqa: BLE001 — never block tool registration
        _file_guard = None

    return register_background_process_tool_into_chat(
        tools=chat.tools,
        manager=container.background_process.manager,
        file_guard=_file_guard,
    )
