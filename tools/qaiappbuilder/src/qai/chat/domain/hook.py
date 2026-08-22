# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Hook configuration value objects for the chat agentic loop.

A *hook* lets the operator run a local shell command at well-defined
points of an agent turn — e.g. run ``ruff`` after every ``edit``, or
log every tool call to an audit pipeline.  This mirrors the
Claude-Agent-SDK "hooks" concept and was migrated into the chat
bounded context from the (now-removed) ``ai_coding`` agent harness so
the single, wired chat agentic loop owns the capability.

Cross-context note
------------------
``HookEvent`` deliberately re-declares the event names rather than
importing them from ``qai.ai_coding.domain`` — the ``context-isolation``
import-linter contract forbids chat from importing another context.
The string values are kept byte-identical to the legacy enum so any
persisted configuration remains interchangeable.

These are pure value objects (no I/O); the actual dispatch lives in
:class:`qai.chat.application.ports.HookEnginePort` adapters under
``qai.chat.infrastructure``.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from qai.platform.io_validator import (
    ValidationError as _IoValidationError,
    assert_max_length,
    assert_no_control_chars,
    assert_non_empty,
)

__all__ = [
    "HookConfig",
    "HookDecision",
    "HookEvent",
]

_MAX_COMMAND_LENGTH: int = 4096


class HookDecision(str, Enum):
    """Verdict a ``pre_tool_call`` hook may return to the agent loop.

    Mirrors the Claude-Agent-SDK ``PreToolUseHookSpecificOutput.
    permissionDecision`` semantics (``allow`` / ``deny`` / ``ask``), lifted
    into this project as a **local shell-command interceptor**: an operator
    hook whose stdout is a JSON object may steer the prospective tool call
    instead of merely observing it.

    * :attr:`ALLOW` — proceed with the call (the default when a hook prints
      nothing / non-JSON, so a plain logging hook is byte-for-byte
      unchanged);
    * :attr:`DENY` — block the call; the loop synthesizes a
      ``[hook_blocked] {reason}`` ``tool_result`` (fed back to the model so
      it can adapt) WITHOUT executing the tool;
    * :attr:`ASK` — treated exactly like :attr:`DENY` in this project (the
      chat loop has no interactive per-tool approval channel for operator
      hooks); the reason is surfaced so the model understands it must not
      call the tool. Kept as a distinct value for SDK-shape fidelity and
      forward-compat if an approval channel is added later.

    Values are the SDK's lowercase literals so a hook script authored
    against Claude Code's ``PreToolUse`` contract emits the same strings.
    """

    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


class HookEvent(str, Enum):
    """Lifecycle points an operator hook can intercept around a turn.

    Lifecycle order::

        on_session_start
            -> on_user_input
            -> pre_message
                -> pre_tool_call -> post_tool_call
            -> post_message
            -> on_complete | on_truncate | on_error
        on_session_end

    Values are byte-identical to the legacy ``ai_coding`` ``HookEvent``
    so persisted configs survive the migration.  All ten events are fired
    by the chat streaming loop (``on_user_input`` fires once per turn right
    after ``on_session_start`` with the user's text; ``post_message`` fires
    after the assistant's reply is finalized, before ``on_complete``).
    """

    PRE_TOOL_CALL = "pre_tool_call"
    POST_TOOL_CALL = "post_tool_call"
    PRE_MESSAGE = "pre_message"
    POST_MESSAGE = "post_message"
    ON_ERROR = "on_error"
    ON_COMPLETE = "on_complete"
    ON_USER_INPUT = "on_user_input"
    ON_SESSION_START = "on_session_start"
    ON_SESSION_END = "on_session_end"
    ON_TRUNCATE = "on_truncate"


@dataclass(frozen=True, slots=True, kw_only=True)
class HookConfig:
    """One hook registration: event + shell command + timeout.

    ``command`` is run by the system shell (so pipes / redirection
    work) at the configured ``event``; ``timeout_s`` bounds the
    execution so a hung hook never stalls the agent loop.
    """

    event: HookEvent
    command: str
    timeout_s: float = 30.0

    def __post_init__(self) -> None:
        try:
            assert_non_empty(self.command, name="command")
            assert_max_length(
                self.command, max_length=_MAX_COMMAND_LENGTH, name="command"
            )
            assert_no_control_chars(self.command, name="command")
        except _IoValidationError as exc:
            raise ValueError(str(exc)) from exc
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
