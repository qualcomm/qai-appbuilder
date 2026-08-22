# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Sub-agent run orchestration — driving ONE sub-agent to a terminal state.

Separated from :mod:`qai.chat.adapters.subagent_bridge` along the seam that
actually matters: the bridge owns *admission and tracking* (register a job,
hand back a handle, cancel by id), while this module owns *execution* (build
the invocation, drain the event stream, classify whatever went wrong).  The
bridge's concerns change with the job registry; these change with the
sub-agent event protocol.

The runner never touches the job registry.  It returns the final text or
raises a typed :class:`SubAgentError`, and the bridge decides what that means
for the job — so there is exactly one place where a job reaches a terminal
state, and no way for a run to settle a job twice.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from qai.chat.application.ports import ToolInvocationRequest
from qai.chat.domain.ids import ConversationId, SubAgentSessionId, TabId
from qai.chat.domain.sub_agent_error import SubAgentError, SubAgentErrorKind
from qai.chat.domain.sub_agent_spawn_request import SubAgentSpawnRequest

__all__ = ["SubAgentRun", "SubAgentRunner", "classify_failure"]

#: Emitted when a run produced neither a terminal result nor any output.
_NO_OUTPUT = "[sub-agent produced no output]"


@dataclass(frozen=True, slots=True)
class SubAgentRun:
    """One admitted sub-agent run, fully resolved at spawn time.

    Attributes:
        job_id: the async job tracking this run.
        subagent_id: the pre-minted id the persisted session adopts.
        conv_id: owning conversation (durable; outlives any tab).
        tab_id: tab the run is displayed in.
        depth: the recursion depth preflight approved for this run.
        request: the typed spawn request.
        extra: verbatim extra ``iter_events`` kwargs from the caller.
    """

    job_id: str
    subagent_id: str
    conv_id: str
    tab_id: str
    depth: int
    request: SubAgentSpawnRequest
    extra: dict[str, Any]


class SubAgentRunner:
    """Runs one sub-agent through the agent tool's event stream.

    ``handler_factory`` is a zero-arg callable returning the
    ``AgentToolHandler``: a factory rather than the handler itself because the
    handler is constructed with a reference to the spawn bridge, so eager
    construction here would close a dependency cycle.
    """

    __slots__ = ("_handler_factory",)

    def __init__(self, handler_factory: Callable[[], Any]) -> None:
        self._handler_factory = handler_factory

    async def run(self, run: SubAgentRun) -> str:
        """Drain ``run``'s event stream and return its final text.

        Raises:
            SubAgentError: kind ``EXECUTION`` when the stream reported an
                error event rather than a result.
        """
        parts: list[str] = []
        final: str | None = None
        handler = self._handler_factory()
        stream = handler.iter_events(_invocation(run), **_kwargs(run))
        async for event in stream:
            etype = event.get("type")
            if etype == "subagent_output":
                parts.append(str(event.get("content", "")))
            elif etype == "subagent_done":
                final = str(event.get("result", "")) or None
            elif etype == "subagent_error":
                raise SubAgentError(
                    kind=SubAgentErrorKind.EXECUTION,
                    message=str(event.get("message", "unknown")),
                    subagent_id=run.subagent_id,
                )
        if final is not None:
            return final
        # No terminal event: fall back to whatever text the run streamed, so a
        # sub-agent that produced useful output but never signalled completion
        # still reports it instead of looking empty.
        return "".join(parts).strip() or _NO_OUTPUT


def _invocation(run: SubAgentRun) -> ToolInvocationRequest:
    """The tool invocation the sub-agent's own loop is driven from."""
    return ToolInvocationRequest(
        tab_id=TabId(run.tab_id),
        conversation_id=ConversationId(run.conv_id),
        tool_name="agent",
        arguments={
            "description": run.request.prompt,
            "subagent_type": run.request.agent_type,
        },
    )


def _kwargs(run: SubAgentRun) -> dict[str, Any]:
    """Merge caller-supplied kwargs with the ones the run itself dictates."""
    # ``spawn_depth`` is authoritative here and a caller value is dropped: the
    # depth that runs must be the depth preflight approved, or the recursion
    # ceiling means nothing.
    kwargs = {k: v for k, v in run.extra.items() if k != "spawn_depth"}
    kwargs.update(
        spawn_depth=run.depth,
        subagent_type=run.request.agent_type,
        model_hint=run.request.model_hint,
        preassigned_subagent_id=SubAgentSessionId(run.subagent_id),
    )
    if run.request.allow_child_spawn:
        kwargs["allow_spawn"] = True
    # ``allow_question`` is only ever an UN-exclusion: the sub-agent tool set
    # drops ``question`` by default, so passing the flag verbatim keeps the
    # default (no blocking dialog from a background run) and lets an explicit
    # grant reach the schema composer.
    if run.request.allow_question:
        kwargs["allow_question"] = True
    return kwargs


def classify_failure(exc: BaseException) -> SubAgentErrorKind:
    """Map an untyped run failure onto a :class:`SubAgentErrorKind`.

    A last-resort classifier for exceptions that reached the spawn path
    without being raised as a :class:`SubAgentError` — it inspects the message
    because that is the only signal such an exception carries.
    """
    text = str(exc).lower()
    if "budget" in text:
        return SubAgentErrorKind.BUDGET
    if "abort" in text or "interrupt" in text or "stopped" in text:
        return SubAgentErrorKind.INTERRUPTED
    return SubAgentErrorKind.EXECUTION
