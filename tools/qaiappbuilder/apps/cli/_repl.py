# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``apps.cli._repl`` — long-lived interactive REPL kernel for streaming CLI.

Shared by ``qai build`` (Model Builder chat session) and ``qai app <pack>``
(App Builder inference session). Distinct from ``apps.cli._runtime`` (which
is for one-shot ``qai <verb>`` commands): a REPL session keeps a single
``Container`` alive for its whole lifetime so the EventBus subscriptions and
agentic streams stay live across many turns (D0-B: this is a long-lived
runtime sibling to ``serve.py``'s supervisor, NOT a one-shot ``run_use_case``).

Responsibilities
----------------
* :func:`repl_container` — ``async with`` the live Container for the session.
* :class:`SlashDispatcher` — unified ``/`` command routing. The rule
  (design §4.3, locked): *the first non-whitespace character of the line is
  ``/``* → command; otherwise the whole line is input content.
* :func:`async_read_line` — non-blocking line input via ``prompt_toolkit``
  (falls back to ``run_in_executor(input)`` so a missing/limited terminal
  still works and the event loop is never blocked).
* :class:`InterruptController` — two-stage Ctrl+C (first cancels the current
  turn, second within a short window exits), mirroring ``serve.py`` signal
  discipline.
* :class:`PermissionBridge` — subscribes to the EventBus permission topic and
  hands events to the REPL main coroutine via an ``asyncio.Queue`` (the bus
  handler runs in a background task and must NOT prompt directly).
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

from apps.cli._runtime import cli_container
from apps.api.di import Container

__all__ = [
    "repl_container",
    "SlashDispatcher",
    "SlashCommand",
    "async_read_line",
    "InterruptController",
    "PermissionBridge",
    "resolve_pending_permissions",
    "is_slash_command",
    "split_slash",
]


# ---------------------------------------------------------------------------
# Long-lived container
# ---------------------------------------------------------------------------


def repl_container(
    *,
    config_file: Path | None = None,
    repo_root: Path | None = None,
) -> Any:
    """Open a long-lived :class:`Container` for one REPL session.

    Thin re-export of :func:`apps.cli._runtime.cli_container`; the session
    enters it once (``async with``) and reuses ``c`` for every turn. Naming
    it separately documents intent: this context spans an interactive
    session, not a single command.
    """
    return cli_container(config_file=config_file, repo_root=repo_root)


# ---------------------------------------------------------------------------
# Slash command dispatch
# ---------------------------------------------------------------------------


def is_slash_command(line: str) -> bool:
    """Return True iff the first non-whitespace char of ``line`` is ``/``.

    Locked rule (design §4.3): Windows paths (``C:\\...`` / ``.\\x``) never
    start with ``/`` so they are unambiguously input content. A user who
    needs a literal leading-``/`` input quotes it or uses a relative path.
    """
    stripped = line.lstrip()
    return stripped.startswith("/")


def split_slash(line: str) -> tuple[str, str]:
    """Split a slash line into ``(command, rest)`` (command without ``/``).

    ``"/precision fp16,w8a8"`` → ``("precision", "fp16,w8a8")``.
    ``"/exit"`` → ``("exit", "")``.
    """
    stripped = line.lstrip()[1:]  # drop leading '/'
    parts = stripped.split(None, 1)
    if not parts:
        return "", ""
    cmd = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    return cmd, rest


SlashHandler = Callable[[str], Awaitable[bool]]
"""Handler signature: receives the argument string, returns ``True`` to keep
the REPL running or ``False`` to request exit."""


class SlashCommand:
    """A registered slash command (name, help text, async handler)."""

    __slots__ = ("name", "help", "handler", "aliases")

    def __init__(
        self,
        name: str,
        help: str,
        handler: SlashHandler,
        *,
        aliases: tuple[str, ...] = (),
    ) -> None:
        self.name = name
        self.help = help
        self.handler = handler
        self.aliases = aliases


class SlashDispatcher:
    """Registry + dispatcher for ``/`` commands.

    ``dispatch`` returns ``(handled, keep_running)``:
    * ``handled`` — whether the line was a slash command at all.
    * ``keep_running`` — whether the REPL should continue (a ``/exit``
      handler returns ``False`` here).
    Non-command lines return ``(False, True)`` so the caller treats them as
    input content.
    """

    __slots__ = ("_commands",)

    def __init__(self) -> None:
        self._commands: dict[str, SlashCommand] = {}

    def register(
        self,
        name: str,
        help: str,
        handler: SlashHandler,
        *,
        aliases: tuple[str, ...] = (),
    ) -> None:
        cmd = SlashCommand(name, help, handler, aliases=aliases)
        self._commands[name] = cmd
        for alias in aliases:
            self._commands[alias] = cmd

    def commands(self) -> list[SlashCommand]:
        """Unique registered commands (de-duped across aliases), by name."""
        seen: dict[str, SlashCommand] = {}
        for cmd in self._commands.values():
            seen[cmd.name] = cmd
        return sorted(seen.values(), key=lambda c: c.name)

    async def dispatch(self, line: str) -> tuple[bool, bool]:
        if not is_slash_command(line):
            return (False, True)
        cmd_name, rest = split_slash(line)
        cmd = self._commands.get(cmd_name)
        if cmd is None:
            sys.stdout.write(
                f"未知命令: /{cmd_name}（/help 查看全部命令）\n"
            )
            sys.stdout.flush()
            return (True, True)
        keep = await cmd.handler(rest)
        return (True, keep)

    def render_help(self) -> str:
        """Format the registered commands as a help block."""
        out = ["可用命令:"]
        for cmd in self.commands():
            alias = (
                f" (别名: {', '.join('/' + a for a in cmd.aliases)})"
                if cmd.aliases
                else ""
            )
            out.append(f"  /{cmd.name:<12} {cmd.help}{alias}")
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Async line input
# ---------------------------------------------------------------------------


_PTK_SESSION: Any = None
_PTK_TRIED = False


def _get_ptk_session() -> Any:
    """Lazily build a single ``prompt_toolkit`` PromptSession (or None).

    Returns ``None`` when prompt_toolkit is unavailable or stdin is not a
    usable terminal, so :func:`async_read_line` can fall back to ``input()``.
    """
    global _PTK_SESSION, _PTK_TRIED
    if _PTK_TRIED:
        return _PTK_SESSION
    _PTK_TRIED = True
    try:
        if not sys.stdin.isatty():
            _PTK_SESSION = None
            return None
        from prompt_toolkit import PromptSession  # noqa: PLC0415

        _PTK_SESSION = PromptSession()
    except Exception:  # noqa: BLE001 — no terminal / import failure
        _PTK_SESSION = None
    return _PTK_SESSION


async def async_read_line(prompt: str = "") -> str:
    """Read one line of input without blocking the event loop.

    Uses ``prompt_toolkit`` (async ``prompt_async``) when available; falls
    back to ``input()`` wrapped in ``run_in_executor`` so the loop keeps
    servicing stream consumption + EventBus callbacks while we wait. Raises
    :class:`EOFError` on Ctrl+D / closed stdin (caller treats as exit).
    """
    session = _get_ptk_session()
    if session is not None:
        # prompt_toolkit runs its own input loop cooperatively.
        return await session.prompt_async(prompt)

    loop = asyncio.get_running_loop()

    def _blocking() -> str:
        if prompt:
            sys.stdout.write(prompt)
            sys.stdout.flush()
        return input()

    return await loop.run_in_executor(None, _blocking)


# ---------------------------------------------------------------------------
# Two-stage Ctrl+C
# ---------------------------------------------------------------------------


class InterruptController:
    """Two-stage Ctrl+C: first cancels the current turn, second exits.

    The REPL turn loop wraps the active stream task and checks
    :meth:`should_exit` after a ``KeyboardInterrupt``. The first interrupt
    sets ``interrupt_event`` (turn cancellation); a second within
    ``window_seconds`` flags exit.
    """

    __slots__ = ("_window", "_last_ts", "interrupt_event")

    def __init__(self, *, window_seconds: float = 1.5) -> None:
        self._window = window_seconds
        self._last_ts = 0.0
        self.interrupt_event = asyncio.Event()

    def signal(self) -> bool:
        """Record an interrupt; return ``True`` if it should EXIT the REPL.

        Returns ``False`` for the first interrupt (cancel current turn).
        """
        now = time.monotonic()
        if (now - self._last_ts) <= self._window and self._last_ts > 0.0:
            return True
        self._last_ts = now
        self.interrupt_event.set()
        return False

    def reset(self) -> None:
        """Clear the per-turn interrupt flag (call at the start of a turn)."""
        self.interrupt_event = asyncio.Event()


# ---------------------------------------------------------------------------
# Permission bridge (EventBus → main coroutine queue)
# ---------------------------------------------------------------------------


class PermissionBridge:
    """Bridge EventBus permission events to the REPL main coroutine.

    D0-B / D5: the bus delivers events on a background task; that task must
    NOT prompt the user directly (it has no access to the input loop). It
    enqueues the event envelope into an ``asyncio.Queue`` the main loop
    drains between turns (or while waiting on a stream), then prompts and
    decides.

    NOTE (research 2026-06-11): chat streaming exec does **not** currently
    raise the ai_coding ``PERMISSION_REQUESTED`` gate (it relies on
    file_guard, default off), so this bridge is defensive — it works if/when
    a permission event is published, but ``qai build`` may never see one on
    the pure chat-streaming path.
    """

    __slots__ = ("_queue", "_subscription", "_event_type")

    def __init__(
        self, *, event_type: str = "ai_coding.requested_permission"
    ) -> None:
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._subscription: Any = None
        self._event_type = event_type

    async def subscribe(self, container: Container) -> None:
        events = getattr(container, "events", None)
        if events is None:
            return

        async def _handler(envelope: Any) -> None:
            # Runs in the bus background task: only enqueue, never prompt.
            await self._queue.put(envelope)

        with contextlib.suppress(Exception):
            self._subscription = await events.subscribe(
                self._event_type, _handler
            )

    async def unsubscribe(self) -> None:
        if self._subscription is not None:
            with contextlib.suppress(Exception):
                await self._subscription.unsubscribe()
            self._subscription = None

    def get_nowait(self) -> Any | None:
        """Return a pending permission envelope, or ``None`` if the queue is
        empty (non-blocking poll for the main loop between turns)."""
        try:
            return self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None

    async def drain(self) -> AsyncIterator[Any]:
        """Yield all currently-queued permission envelopes (non-blocking)."""
        while True:
            envelope = self.get_nowait()
            if envelope is None:
                return
            yield envelope


async def resolve_pending_permissions(
    container: Container,
    bridge: "PermissionBridge",
    *,
    allow_set: set[str] | None = None,
    prompt: Callable[[str], Awaitable[str]] | None = None,
) -> None:
    """Drain queued permission events, prompt the operator, and decide.

    D5: this is the interactive gate. For each queued event it prompts with
    a custom terminal confirmation (§3.9.2 — ``[y]/[N]/[a]lways``, no native
    ``confirm()``) and resolves via the ai_coding
    ``decide_permission_use_case`` (the ONLY use case that re-opens the
    ``PERMISSION_REQUESTED`` gate — security's ``approve`` does not, two
    distinct request-id spaces).

    ``allow_set`` (per-session, caller-owned) records tool names the operator
    chose ``[a]lways`` for so identical tools auto-approve without re-asking.

    NOTE (research 2026-06-11): chat streaming exec does not currently raise
    this gate, so on the pure ``qai build`` path this loop usually has nothing
    to drain. It is exercised if/when a permission event is published.
    """
    allow_set = allow_set if allow_set is not None else set()
    prompt = prompt or _default_permission_prompt

    decide_uc = _decide_permission_use_case(container)
    if decide_uc is None:
        return

    async for envelope in bridge.drain():
        event = getattr(envelope, "event", envelope)
        session_id = getattr(event, "session_id", None)
        request_id = getattr(event, "request_id", None)
        tool_name = getattr(event, "tool_name", None)
        if session_id is None or request_id is None:
            continue
        tool_label = getattr(tool_name, "value", str(tool_name))

        if tool_label in allow_set:
            await _decide(decide_uc, session_id, request_id, approved=True)
            continue

        answer = (
            await prompt(
                f"  ⚠ Agent 请求执行: {tool_label}\n"
                "     允许? [y]es / [N]o / [a]lways(本会话) › "
            )
        ).strip().lower()
        if answer in ("a", "always"):
            allow_set.add(tool_label)
            await _decide(decide_uc, session_id, request_id, approved=True)
        elif answer in ("y", "yes"):
            await _decide(decide_uc, session_id, request_id, approved=True)
        else:
            await _decide(decide_uc, session_id, request_id, approved=False)


async def _default_permission_prompt(text: str) -> str:
    return await async_read_line(text)


def _decide_permission_use_case(container: Container) -> Any | None:
    ai_coding = getattr(container, "ai_coding", None)
    if ai_coding is None:
        return None
    return getattr(ai_coding, "decide_permission_use_case", None)


async def _decide(
    decide_uc: Any, session_id: Any, request_id: Any, *, approved: bool
) -> None:
    """Build + execute the ai_coding DecidePermissionCommand."""
    try:
        from qai.ai_coding.application.use_cases.decide_permission import (  # noqa: PLC0415
            DecidePermissionCommand,
        )
        from qai.ai_coding.domain import PermissionDecision  # noqa: PLC0415

        decision = (
            PermissionDecision.APPROVED
            if approved
            else PermissionDecision.REJECTED
        )
        await decide_uc.execute(
            DecidePermissionCommand(
                session_id=session_id,
                request_id=request_id,
                decision=decision,
            )
        )
    except Exception:  # noqa: BLE001 — never crash the REPL on a decide error
        with contextlib.suppress(Exception):
            sys.stderr.write("权限决定失败（已忽略）\n")
            sys.stderr.flush()
