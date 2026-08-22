# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``sub_agent`` tool handler — unified sub-agent management interface.

Mirrors ``background_process`` exactly: one tool, multiple actions.
Gives the main agent (and the LLM) a clean, opaque interface to
sub-agents that are running in the background.

Actions
-------
* ``list``    — list sub-agents created in this conversation.
* ``status``  — current state, rounds, elapsed, in-flight tool.
* ``inspect`` — full transcript of one sub-agent.
* ``wait``    — block until the sub-agent settles; return its result.
* ``wait_any``— block until ANY of them settles.
* ``stop``    — abort a running sub-agent.

Async-job coupling (N.49)
-------------------------
Sub-agent execution is tracked by :class:`AsyncJobManager`: the bridge
registers one job per spawn with ``agent_id`` set to the sub-agent id,
which is how this handler resolves ``subagent_id -> job_id``.  ``list`` /
``status`` merge the job's admission state (``queued`` — waiting for a
concurrency slot — vs ``running``) into the session-repo view, because a
queued sub-agent looks identical to a running one from the repo alone.
``wait`` / ``wait_any`` await the job's completion event instead of only
polling the repo, and ``wait`` additionally marks the job's delivery as
suppressed: a synchronous ``wait`` IS the delivery, so the dispatcher must
not also push a SYSTEM_NOTICE for it.

Design principle
----------------
Sub-agents are opaque tools from the main agent's perspective — just
like ``exec`` processes managed by ``background_process``.  The LLM
never needs to know about ``SubAgentSession`` internals; it only needs
to know the id (returned by the ``agent`` tool at spawn time) and
which action to call.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from qai.chat.adapters.async_job_manager import AsyncJobKind, AsyncJobStatus
from qai.chat.domain.ids import SubAgentSessionId
from qai.chat.domain.sub_agent_session import SubAgentSessionStatus
from qai.platform.logging import get_logger

if TYPE_CHECKING:
    from qai.chat.adapters.async_job_manager import (
        AsyncJob,
        AsyncJobCompleted,
        AsyncJobManager,
    )
    from qai.chat.adapters.sub_agent_abort_registry import (
        InMemorySubAgentAbortRegistry,
    )
    from qai.chat.adapters.sub_agent_broadcaster import SubAgentStreamBroadcaster
    from qai.chat.adapters.subagent_bridge import SubAgentBridge
    from qai.chat.application.ports import (
        SubAgentSessionRepositoryPort,
        ToolInvocationRequest,
    )

__all__ = ["SUB_AGENT_TOOL_SCHEMA", "SubAgentToolHandler"]

_log = get_logger(__name__)

# How long ``wait`` polls before giving up (seconds).
_WAIT_MAX_TIMEOUT_S: float = 120.0
_WAIT_POLL_S: float = 0.5

# Terminal statuses — sub-agent has settled.
_TERMINAL = frozenset({
    SubAgentSessionStatus.DONE,
    SubAgentSessionStatus.ERROR,
    SubAgentSessionStatus.INTERRUPTED,
})


SUB_AGENT_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "sub_agent",
        "description": (
            "Observe and steer sub-agents you have spawned.\n\n"
            "A sub-agent is spawned via the ``agent`` tool, which returns "
            "IMMEDIATELY (t=0) with a handle naming the sub-agent id — it "
            "never blocks on the sub-agent's work.  **Its result is "
            "delivered to you automatically when it finishes: you NEVER "
            "need to poll for it.**  This tool is for the cases where you "
            "want more than the eventual result — check progress, read the "
            "full transcript, or stop a run.  If a sub-agent's result is "
            "all you need, do nothing: it will reach you.\n\n"
            "Actions:\n"
            "- **list**: enumerate every sub-agent this conversation has "
            "  spawned (id, status, rounds, type, depth, parent, started "
            "  / updated timestamps, task preview), each annotated with "
            "  its async-job state — ``job=running`` means it holds an "
            "  execution slot right now, ``job=queued`` means it is "
            "  admitted but WAITING for a slot (so zero progress so far, "
            "  and waiting on it will take longer than usual).  Use this "
            "  to rediscover ids after they scroll out of the visible "
            "  transcript.\n"
            "- **status** (id): one-line status snapshot — state, "
            "  rounds, currently-running tool, plus the async-job state "
            "  (queued / running / completed / failed / cancelled).  For "
            "  a settled sub-agent also includes its final output.  Read "
            "  only — does NOT block and does NOT consume the result.\n"
            "- **inspect** (id, optional max_chars): READ THE SUB-AGENT'S "
            "  FULL TRANSCRIPT so far — assistant reasoning, tool calls, "
            "  tool results — exactly what a user would see if they "
            "  opened the sub-agent's own tab.  Works on RUNNING "
            "  sub-agents (live state up to the last persisted round) "
            "  and TERMINAL ones (complete history).  Use this when the "
            "  user asks 'how is the sub-agent doing?' and ``status`` is "
            "  not enough.  Default budget 12000 chars, max 40000; when "
            "  the transcript exceeds budget the TAIL is preserved and "
            "  the head is replaced with a truncation marker.\n"
            "- **wait** (id, optional timeout_s): block until THIS "
            "  sub-agent finishes and return its result.  Default 30s, "
            "  max 120s.\n"
            "  Use this ONLY when you are genuinely blocked with nothing "
            "  else to do.  Results arrive on their own, so waiting is "
            "  almost never required — and a ``settled=false`` return "
            "  means only that your budget elapsed, NOT that anything is "
            "  wrong.  Do NOT re-issue ``wait`` in a loop: end your turn "
            "  instead and the result will be delivered to you.  Repeated "
            "  waiting burns whole minutes and gains nothing.\n"
            "  IMPORTANT: a successful ``wait`` CONSUMES the delivery — "
            "  you are reading the result synchronously here, so the "
            "  automatic completion notice for this sub-agent is "
            "  suppressed and will NOT arrive later as a separate "
            "  message.  Do not wait for a second announcement; "
            "  the text this call returns is the one and only delivery.\n"
            "- **wait_any** (optional timeout_s): block until ANY "
            "  running sub-agent in this conversation finishes and "
            "  return that one's id + result.  Useful when you "
            "  dispatched several in parallel and want to reap "
            "  whichever completes first.  Only the sub-agent it "
            "  actually returns has its delivery consumed; the others "
            "  keep running and will still announce themselves "
            "  normally when they finish.\n"
            "- **stop** (id): cancel a running sub-agent — releases its "
            "  execution slot (letting a queued sub-agent start) and "
            "  signals the run to unwind at its next round boundary.\n\n"
            "The sub-agent id is shown in the ``agent`` tool's handle "
            "result, in every ``list`` row, and in every ``inspect`` "
            "header.\n\n"
            "STEERING A RUNNING SUB-AGENT: to resume a sub-agent with "
            "additional work — a follow-up question, a correction, a "
            "next step — call the ``agent`` tool with "
            "``resume_subagent_id=<the-id>`` and a fresh ``description`` "
            "carrying the new instructions.  The sub-agent wakes with "
            "its FULL prior context (messages + tool outputs) and "
            "handles the new work as one more turn.  This is the "
            "reference design's ``hub send`` semantics — one channel "
            "for both 'wake up' and 'here is more to do'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "list", "status", "inspect", "wait",
                        "wait_any", "stop",
                    ],
                    "description": "Operation to perform.",
                },
                "id": {
                    "type": "string",
                    "description": (
                        "Sub-agent id (required for status / inspect / "
                        "wait / stop; ignored for list / wait_any). "
                        "Shown in the ``agent`` tool's spawn handle, in "
                        "each ``list`` row, and in every ``inspect`` "
                        "header."
                    ),
                },
                "timeout_s": {
                    "type": "number",
                    "minimum": 1,
                    "description": (
                        "For wait / wait_any: seconds to block "
                        "(default 30, max 120).  ``settled=false`` "
                        "means only that this budget elapsed — the "
                        "sub-agent is still running and its result "
                        "still arrives on its own.  End your turn "
                        "rather than re-issuing the wait."
                    ),
                },
                "max_chars": {
                    "type": "number",
                    "minimum": 1000,
                    "description": (
                        "For inspect only: character budget for the "
                        "returned transcript.  Default 12000, max "
                        "40000.  When the transcript exceeds the "
                        "budget the tail is kept (most recent "
                        "activity) and the head is replaced with a "
                        "``[transcript head truncated: N chars "
                        "omitted]`` marker."
                    ),
                },
            },
            "required": ["action"],
        },
    },
}


def _extract_final_text(session: Any) -> str:
    """Extract the sub-agent's final output from its persisted messages."""
    messages = getattr(session, "messages", None) or []
    # Walk backwards; find the last assistant message with real text.
    for msg in reversed(messages):
        role = getattr(getattr(msg, "role", None), "value", None)
        if role != "assistant":
            continue
        text = getattr(getattr(msg, "content", None), "text", "") or ""
        # Skip sentinels.
        if text in ("[tool_calls]", "[subagent_summary]", ""):
            continue
        return text
    return "(no output)"


def _format_subagent_transcript(session: Any) -> str:
    """Render a sub-agent's persisted ``Message`` list as concise markdown.

    Mirrors the reference design's ``formatSessionHistoryMarkdown`` for
    ``history://`` URLs — one section per message, tool call + result
    collapsed to a single line, sentinel content skipped.  Optimised
    for LLM consumption (the main agent reading it via ``sub_agent
    action=inspect``): compact, deterministic, no ANSI/emoji, no
    system-prompt noise.

    Format per assistant round (V1 shape ``build_round_messages``
    persists):

    * Round preamble: ``## Round N`` (from ``meta.round_index``).
    * Assistant lead-in text (skips the ``[tool_calls]`` sentinel).
    * For each tool_call card: ``- → {tool}({primary_arg})`` on one
      line, followed by ``  ↳ {result_head}`` for the paired result
      (result truncated to keep the transcript readable — the model
      can re-issue inspect with a larger budget for the full picture).
    * Sub-agent summary rows (``kind=subagent_summary``) are noted as
      ``- (dispatched sub-agent: <names>)`` since drilling into a
      grand-sub-agent from here would explode the transcript.
    * User / system_notice rows: ``**user:** …`` / ``**system:** …``.
    """
    messages = getattr(session, "messages", None) or []
    lines: list[str] = []
    for msg in messages:
        role = getattr(getattr(msg, "role", None), "value", None) or "user"
        text = getattr(getattr(msg, "content", None), "text", "") or ""
        tool_calls = tuple(getattr(msg, "tool_calls", None) or ())
        meta = getattr(msg, "meta", None)
        if not isinstance(meta, dict):
            meta = {}

        if role == "system":
            # Skip system-prompt noise entirely — the main agent's LLM
            # already has its own system prompt.  A "developer" note
            # from the sub-agent's runtime is not useful for progress
            # inspection.
            continue
        if role == "user":
            _text = text.strip()
            if not _text:
                continue
            _text = _text if len(_text) <= 400 else _text[:400] + "…"
            lines.append(f"**user:** {_text}")
            continue
        if role == "system_notice":
            _text = text.strip()
            if not _text:
                continue
            _text = _text if len(_text) <= 400 else _text[:400] + "…"
            lines.append(f"**[System notice]** {_text}")
            continue
        # role == "assistant" from here.
        round_idx = meta.get("round_index")
        if isinstance(round_idx, int) and not isinstance(round_idx, bool):
            lines.append(f"## Round {round_idx}")
        # Sub-agent summary marker (dispatched grand-sub-agent) — one line.
        if meta.get("kind") == "subagent_summary":
            blocks = meta.get("subAgentBlocks") or []
            names = []
            if isinstance(blocks, list):
                for b in blocks:
                    if isinstance(b, dict):
                        nm = b.get("name") or b.get("subagent_id") or "?"
                        names.append(str(nm))
            names_str = ", ".join(names) if names else "?"
            lines.append(f"- (dispatched sub-agent: {names_str})")
            continue
        # Assistant text (skip the [tool_calls] sentinel).
        if text and text != "[tool_calls]":
            lines.append(text)
        # Tool call cards — one line per call + paired result head.
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            tool_name = str(tc.get("tool") or "?")
            args = tc.get("args") or {}
            primary = _tool_primary_arg(tool_name, args if isinstance(args, dict) else {})
            call_line = f"- → {tool_name}({primary})" if primary else f"- → {tool_name}()"
            lines.append(call_line)
            output = tc.get("output")
            if isinstance(output, str) and output:
                head = output.replace("\n", " ⏎ ")
                if len(head) > 300:
                    head = head[:300] + "…"
                lines.append(f"  ↳ {head}")
    return "\n".join(lines) if lines else "(no transcript yet)"


def _tool_primary_arg(tool_name: str, args: dict[str, Any]) -> str:
    """Pick the most informative scalar arg of a tool call for one-line render."""
    for key in ("path", "file_path", "command", "pattern", "url", "query", "prompt", "name", "id"):
        v = args.get(key)
        if isinstance(v, str) and v:
            return v if len(v) <= 120 else v[:120] + "…"
        if isinstance(v, list) and v and all(isinstance(x, str) for x in v):
            joined = ", ".join(v)
            return joined if len(joined) <= 120 else joined[:120] + "…"
    return ""


class SubAgentToolHandler:
    """Handler for the ``sub_agent`` management tool.

    Wired by ``apps/api/_chat_di.py`` alongside the other chat tools.
    All dependencies are optional so the handler degrades gracefully
    when not fully wired (legacy / test stubs).
    """

    __slots__ = (
        "_abort_registry",
        "_ajm",
        "_bridge",
        "_dispatcher",
        "_stream_broadcaster",
        "_sub_agent_sessions",
    )

    def __init__(
        self,
        *,
        sub_agent_sessions: SubAgentSessionRepositoryPort | None = None,
        stream_broadcaster: SubAgentStreamBroadcaster | None = None,
        abort_registry: InMemorySubAgentAbortRegistry | None = None,
        dispatcher: Any = None,
        async_job_manager: AsyncJobManager | None = None,
        bridge: SubAgentBridge | None = None,
    ) -> None:
        self._sub_agent_sessions = sub_agent_sessions
        self._stream_broadcaster = stream_broadcaster
        self._abort_registry = abort_registry
        # AsyncJobManager — the admission/lifecycle registry every spawn is
        # tracked in.  Optional so unit stubs and any not-yet-migrated wiring
        # keep working: with no manager the actions fall back to the pure
        # session-repo behaviour (no job annotations, poll-only ``wait``).
        self._ajm = async_job_manager
        # SubAgentBridge — the ONE place a sub-agent is cancelled (AJM job
        # transition + per-sub-agent abort flag together).  Optional for the
        # same reason; ``stop`` degrades to the bare abort registry.
        self._bridge = bridge
        # BackgroundJobDispatcher — optional, so unit stubs (no
        # dispatcher wired) keep working.  When present, a successful
        # ``wait`` marks the sub-agent's terminal dedup_key as
        # "already delivered" so the dispatcher's own auto-notify
        # (which arrives via the ``SubAgentSessionTerminated`` event
        # bus subscription) is a no-op — a wait snapshot IS the
        # delivery.  Kept untyped (``Any``) to avoid an import cycle
        # with :mod:`background_job_dispatcher`.
        self._dispatcher = dispatcher

    async def execute(self, request: ToolInvocationRequest) -> str:
        action = (request.arguments.get("action") or "").strip().lower()
        if action == "list":
            return await self._list(request)
        if action == "status":
            return await self._status(request)
        if action == "inspect":
            return await self._inspect(request)
        if action == "wait":
            return await self._wait(request)
        if action == "wait_any":
            return await self._wait_any(request)
        if action == "stop":
            return await self._stop(request)
        return (
            f"[sub_agent error] unknown action '{action}'. "
            "Valid actions: list, status, inspect, wait, wait_any, stop."
        )

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------

    async def _list(self, request: ToolInvocationRequest) -> str:
        """List every sub-agent rooted at this conversation.

        Reports one line per sub-agent with the fields the main agent
        needs to pick a follow-up action:

        * ``id`` — for status / inspect / wait / stop
        * ``status`` — running / done / error / interrupted / user_owned
        * ``rounds`` — completed agentic rounds
        * ``type`` — profile (general / explore / …)
        * ``depth`` — 1 for a direct child, 2 for a grand-sub-agent, …
        * ``parent`` — direct-parent sub-agent id (``main`` for depth 1)
        * ``started`` — wall-clock time the session was spawned
        * ``updated`` — wall-clock time of the most recent activity
        * ``task`` — 80-char preview of the initial task description

        The parent + depth fields let the main agent reason about the
        sub-agent TREE (a spawn tree emerges when children spawn their
        own children with ``allow_spawn=True``).  ``started`` / ``updated``
        mirror the ``last_activity`` column reference designs surface via
        ``history://`` — with them the main agent can tell "this
        sub-agent has been running for 5 minutes, might be worth
        checking on" vs "it settled a while ago, its result is stale."
        """
        repo = self._sub_agent_sessions
        if repo is None:
            return (
                "No sub-agents have been created in this conversation yet."
            )
        try:
            sessions = await repo.list_by_root_conversation(
                request.conversation_id,
            )
        except Exception as exc:  # noqa: BLE001
            _log.warning("sub_agent.list.failed", error=repr(exc))
            return (
                "[sub_agent error] could not load sub-agents for this "
                "conversation."
            )
        jobs_by_agent = self._jobs_by_agent(request.conversation_id)
        if not sessions and not jobs_by_agent:
            return "No sub-agents have been created in this conversation yet."
        # Count the job-only rows too: a just-spawned sub-agent has a job but
        # no session row yet, and a header that said "0 sub-agent(s)" above two
        # listed ids would read as a bug to the model.
        session_ids = {s.id.value for s in sessions}
        total = len(sessions) + sum(
            1 for sa_id in jobs_by_agent if sa_id not in session_ids
        )
        lines = [f"{total} sub-agent(s) in this conversation:"]
        for i, s in enumerate(sessions, 1):
            preview = (s.prompt_preview or s.title or "").strip()[:80]
            parent = (
                s.parent_subagent_id.value
                if s.parent_subagent_id is not None
                else "main"
            )
            lines.append(
                f"{i}. id={s.id.value}\n"
                f"   status={s.status.value} · rounds={s.rounds} · "
                f"type={s.subagent_type} · depth={s.depth} · parent={parent}"
                f"{self._job_annotation(jobs_by_agent.pop(s.id.value, None))}\n"
                f"   started={s.created_at.isoformat()} · "
                f"updated={s.updated_at.isoformat()}\n"
                f"   task={preview or '(no preview)'}"
            )
        # Jobs with no persisted session yet: a spawn registers its job BEFORE
        # the sub-agent's first round persists a session row, so a just-spawned
        # (or still-queued) sub-agent would otherwise be invisible here — the
        # exact case the main agent is most likely asking about.
        for j, job in enumerate(jobs_by_agent.values(), len(sessions) + 1):
            lines.append(
                f"{j}. id={job.agent_id}\n"
                f"   status=(not persisted yet)"
                f"{self._job_annotation(job)}\n"
                f"   task=(spawned, no round persisted yet)"
            )
        lines.append(
            "\nFor a running sub-agent: ``sub_agent(action=inspect, id=...)`` "
            "reads its full transcript; ``sub_agent(action=wait, id=...)`` "
            "blocks until it settles.  To resume a settled sub-agent with "
            "more work (its prior context preserved), call ``agent`` with "
            "``resume_subagent_id=<id>`` and a new ``description``."
        )
        _log.info(
            "subagent.tool.list_via_ajm",
            extra={
                "conversation_id": self._owner_id(request.conversation_id),
                "session_count": len(sessions),
                "job_count": len(self._ajm_jobs(request.conversation_id)),
            },
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    async def _status(self, request: ToolInvocationRequest) -> str:
        sa_id = self._require_id(request)
        if sa_id is None:
            return (
                "[sub_agent error] 'id' is required for action=status. "
                "Use action=list to find sub-agent ids."
            )
        repo = self._sub_agent_sessions
        if repo is None:
            return "[sub_agent error] sub-agent repository is not wired."
        try:
            session = await repo.find(SubAgentSessionId.of(sa_id))
        except Exception as exc:  # noqa: BLE001
            _log.warning("sub_agent.status.failed", id=sa_id, error=repr(exc))
            return f"[sub_agent error] could not load sub-agent '{sa_id}'."
        job = self._find_job(request.conversation_id, sa_id)
        if session is None:
            if job is None:
                return (
                    f"[sub_agent error] no sub-agent found with id '{sa_id}'. "
                    "Use action=list to see available sub-agents."
                )
            # Job registered, no persisted round yet — report the admission
            # state truthfully instead of pretending the sub-agent is unknown.
            return (
                f"status=(not persisted yet) | rounds=0"
                f"{self._job_annotation(job)}"
            )
        status = session.status.value
        rounds = session.rounds
        # Current in-flight tool (live, from broadcaster).
        current_tool = ""
        if (
            self._stream_broadcaster is not None
            and session.status is SubAgentSessionStatus.RUNNING
        ):
            try:
                entry = self._stream_broadcaster.get(sa_id)
                if entry and entry.frames:
                    for frame in reversed(entry.frames):
                        p = frame.payload or {}
                        if p.get("type") == "subagent_tool":
                            tool_name = p.get("tool_name", "")
                            if tool_name:
                                current_tool = (
                                    f" | currently running: {tool_name}"
                                )
                            break
            except Exception:  # noqa: BLE001, S110 — best-effort live status
                pass
        result = (
            f"status={status} | rounds={rounds}"
            f"{self._job_annotation(job)}{current_tool}"
        )
        if session.status in _TERMINAL:
            final = _extract_final_text(session)
            result += f"\nFinal output:\n{final}"
            # Main agent has now seen this terminal result — suppress
            # the dispatcher's pending auto-notify for this dedup_key.
            self._suppress_dispatcher(sa_id, session.status.value)
        return result

    # ------------------------------------------------------------------
    # inspect
    # ------------------------------------------------------------------

    async def _inspect(self, request: ToolInvocationRequest) -> str:
        """Render the sub-agent's full transcript so far.

        Semantics mirror the reference design's ``history://<agentId>``
        internal URL: reach into the persisted session's ``messages``
        list (the SAME source the standalone sub-agent tab reads from)
        and format it as a concise markdown transcript so the main
        agent's LLM can reason about what the sub-agent actually did.

        Works on RUNNING sub-agents (returning the state up to the
        last persisted round — every round-end persist commits the
        assistant + tool messages, so this stays close to real time)
        AND terminal ones (complete history).

        Budgeting: the transcript can be long.  ``max_chars`` caps the
        returned text (default 12000, hard max 40000).  When over
        budget, the TAIL is preserved (most recent activity, which is
        what the model usually needs) and the HEAD is replaced with a
        ``[transcript head truncated: N chars omitted]`` marker so the
        model knows work is missing but sees the latest state clearly.
        """
        sa_id = self._require_id(request)
        if sa_id is None:
            return (
                "[sub_agent error] 'id' is required for action=inspect. "
                "Use action=list to find sub-agent ids."
            )
        repo = self._sub_agent_sessions
        if repo is None:
            return "[sub_agent error] sub-agent repository is not wired."
        try:
            session = await repo.find(SubAgentSessionId.of(sa_id))
        except Exception as exc:  # noqa: BLE001
            _log.warning("sub_agent.inspect.failed", id=sa_id, error=repr(exc))
            return f"[sub_agent error] could not load sub-agent '{sa_id}'."
        if session is None:
            return (
                f"[sub_agent error] no sub-agent found with id '{sa_id}'. "
                "Use action=list to see available sub-agents."
            )
        # Budget resolution — clamp to a hard upper bound so the caller
        # cannot balloon the LLM wire with a truly enormous transcript.
        raw_max = request.arguments.get("max_chars")
        try:
            max_chars = int(raw_max) if raw_max is not None else 12000
        except (TypeError, ValueError):
            max_chars = 12000
        max_chars = max(1000, min(40000, max_chars))
        transcript = _format_subagent_transcript(session)
        if len(transcript) <= max_chars:
            body = transcript
        else:
            # Tail-preserving truncation — keep the most recent activity
            # (the model reasons about "what state is the sub-agent in
            # NOW"; the earliest tool calls are usually context the
            # user cares less about).  Leave a header marker so the
            # model knows the head is elided.
            omitted = len(transcript) - max_chars
            marker = (
                f"[transcript head truncated: {omitted} chars omitted. "
                f"Re-issue with a larger max_chars to see more.]\n\n"
            )
            body = marker + transcript[-max_chars + len(marker):]
        # Header carries the key handles the main agent needs to
        # ACT on the observed state — id (echoed for cross-turn
        # copy-paste), status, rounds, and a reminder that it can
        # steer this sub-agent by re-issuing ``agent`` with the
        # ``resume_subagent_id`` param.  Same rationale as the
        # reference design's ``history://<id>`` header + adopt/revive
        # hints — the transcript is only useful if the agent knows
        # what levers it has to change the outcome.
        header_lines = [
            f"# Sub-agent {sa_id}",
            f"status={session.status.value} · rounds={session.rounds}",
        ]
        if session.status is SubAgentSessionStatus.RUNNING:
            header_lines.append(
                "This sub-agent is still running. Its result will be "
                "delivered to you automatically when it finishes — "
                "``inspect`` is for reading progress NOW, not for "
                "collecting the result."
            )
        elif session.status in _TERMINAL:
            header_lines.append(
                "This sub-agent has settled. To resume it with more "
                "work (its prior context preserved), call ``agent`` "
                f"with ``resume_subagent_id=\"{sa_id}\"`` and a new "
                "``description``."
            )
        header = "\n".join(header_lines) + "\n\n"
        return header + body

    # ------------------------------------------------------------------
    # wait
    # ------------------------------------------------------------------

    async def _wait(self, request: ToolInvocationRequest) -> str:
        sa_id = self._require_id(request)
        if sa_id is None:
            return (
                "[sub_agent error] 'id' is required for action=wait. "
                "Use action=list to find sub-agent ids."
            )
        repo = self._sub_agent_sessions
        if repo is None:
            return "[sub_agent error] sub-agent repository is not wired."
        timeout_s = self._resolve_timeout(request)

        job = self._find_job(request.conversation_id, sa_id)
        # Fast path — the job already settled, so its recorded outcome IS the
        # answer and no waiting is needed at all.
        if job is not None and job.is_terminal:
            return self._consume_job_result(job, sa_id)

        # Slow path — subscribe to the manager's completion fan-out so we wake
        # the instant this job settles, instead of only noticing on the next
        # repo poll.  The repo poll stays as the fallback settle source: a run
        # that terminates without a ``complete_job`` (legacy wiring, or no AJM
        # at all) must still be reapable here.
        settled: asyncio.Future[AsyncJobCompleted] | None = None
        dispose: Any = None
        job_id = job.id if job is not None else None
        if job_id is not None and self._ajm is not None:
            waited_id = job_id
            future: asyncio.Future[AsyncJobCompleted] = (
                asyncio.get_running_loop().create_future()
            )
            settled = future

            def _on_completed(event: AsyncJobCompleted) -> None:
                # One sink per owner receives EVERY completion of that
                # conversation — filter to the job this wait is about.
                if event.job_id == waited_id and not future.done():
                    future.set_result(event)

            dispose = self._ajm.register_delivery_sink(
                self._owner_id(request.conversation_id), _on_completed,
            )
        try:
            return await self._wait_loop(
                repo=repo,
                sa_id=sa_id,
                job_id=job_id,
                timeout_s=timeout_s,
                settled=settled,
            )
        finally:
            # A leaked sink both keeps firing and pins the owner's terminal
            # jobs out of dead-letter eviction — dispose unconditionally.
            if dispose is not None:
                dispose()

    async def _wait_loop(
        self,
        *,
        repo: SubAgentSessionRepositoryPort,
        sa_id: str,
        job_id: str | None,
        timeout_s: float,
        settled: "asyncio.Future[AsyncJobCompleted] | None",
    ) -> str:
        """Block until this sub-agent settles, the budget elapses, or it aborts.

        Two settle sources are watched together: the async-job completion
        future (instant, authoritative for the new spawn path) and the
        session-repo status (the fallback for runs whose terminal state only
        reaches the repo).  Also bails out early when the sub-agent's abort
        flag has been signalled — nothing is going to complete after that, so
        burning the remaining budget would only delay the parent turn.
        """
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            if settled is not None and settled.done():
                completed = self._job_by_id(settled.result().job_id)
                if completed is not None:
                    return self._consume_job_result(completed, sa_id)
            try:
                session = await repo.find(SubAgentSessionId.of(sa_id))
            except Exception as exc:  # noqa: BLE001
                _log.warning("sub_agent.wait.poll_failed", id=sa_id, error=repr(exc))
                return f"[sub_agent error] could not poll sub-agent '{sa_id}'."
            if session is None:
                return (
                    f"[sub_agent error] no sub-agent found with id '{sa_id}'."
                )
            if session.status in _TERMINAL:
                final = _extract_final_text(session)
                # Wait snapshot IS the delivery — mark this run's
                # terminal dedup_key so the dispatcher's own
                # ``SubAgentSessionTerminated`` handler skips a
                # redundant SYSTEM_NOTICE + coordinator wake.
                self._suppress_dispatcher(sa_id, session.status.value)
                self._suppress_job_delivery(job_id)
                return (
                    f"settled=true | status={session.status.value}"
                    f" | rounds={session.rounds}\n"
                    f"Result:\n{final}"
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return (
                    f"settled=false | status={session.status.value}"
                    f" | rounds={session.rounds}"
                    " — the wait budget elapsed; the sub-agent is still"
                    " running and its result will be delivered to you"
                    " automatically. End your turn instead of waiting again."
                )
            if self._abort_signalled(sa_id):
                return (
                    f"settled=false | status={session.status.value}"
                    f" | rounds={session.rounds}"
                    " — a stop was signalled for this sub-agent; it will "
                    "unwind at its next round boundary."
                )
            slice_s = min(_WAIT_POLL_S, remaining)
            if settled is None:
                await asyncio.sleep(slice_s)
            else:
                # Wake on whichever comes first: the completion event or the
                # next poll tick.  ``wait_for`` on a shielded future would
                # cancel it, so time-box with ``asyncio.wait`` instead.
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(settled), timeout=slice_s,
                    )

    # ------------------------------------------------------------------
    # wait_any
    # ------------------------------------------------------------------

    async def _wait_any(self, request: ToolInvocationRequest) -> str:
        """Block until ANY running sub-agent in this conversation settles.

        Mirrors the reference design's ``hub wait`` (bare, no ids) —
        the main agent dispatched N sub-agents and wants to process
        whichever finishes first, without having to poll each one.

        Semantics:

        * Scans every sub-agent under ``request.conversation_id``.
        * Returns AS SOON AS one of them enters a terminal status —
          reports which one settled and its final output.
        * When all sub-agents are ALREADY terminal at call time, returns
          the most-recently-settled one (still useful — a signal to
          reap results).
        * When NO sub-agent exists at all, returns an actionable error.
        * When the budget elapses with everyone still running, reports
          ``settled=false`` + a status snapshot of every candidate.

        Delivery semantics differ from ``wait`` deliberately: ONLY the
        sub-agent this call actually returns has its delivery consumed
        (suppressed).  Every other sub-agent is still running and its
        completion must still be announced normally — the parent agent asked
        for the FIRST result, not for ownership of all of them.

        Never blocks longer than ``timeout_s`` (default 30, max 120).
        A budget that elapses with nothing settled is NOT a signal to wait
        again — results are delivered on their own; see the schema's
        anti-polling note.
        """
        repo = self._sub_agent_sessions
        if repo is None:
            return "[sub_agent error] sub-agent repository is not wired."
        timeout_s = self._resolve_timeout(request)
        # Subscribe once for the whole conversation: any SUBAGENT job of this
        # owner settling is exactly the "any of them finished" signal, so a
        # single sink covers every candidate without per-id bookkeeping.
        first: asyncio.Future[AsyncJobCompleted] | None = None
        dispose: Any = None
        if self._ajm is not None:
            future: asyncio.Future[AsyncJobCompleted] = (
                asyncio.get_running_loop().create_future()
            )
            first = future

            def _on_any_completed(event: AsyncJobCompleted) -> None:
                if event.kind is AsyncJobKind.SUBAGENT and not future.done():
                    future.set_result(event)

            dispose = self._ajm.register_delivery_sink(
                self._owner_id(request.conversation_id), _on_any_completed,
            )
        try:
            return await self._wait_any_loop(
                repo=repo,
                conversation_id=request.conversation_id,
                timeout_s=timeout_s,
                first=first,
            )
        finally:
            if dispose is not None:
                dispose()

    async def _wait_any_loop(
        self,
        *,
        repo: SubAgentSessionRepositoryPort,
        conversation_id: Any,
        timeout_s: float,
        first: "asyncio.Future[AsyncJobCompleted] | None",
    ) -> str:
        deadline = asyncio.get_running_loop().time() + timeout_s
        while True:
            # Event path first: a completion already fanned out is the most
            # authoritative "one of them finished" answer available, and it
            # names the exact job whose delivery this call consumes.
            if first is not None and first.done():
                event = first.result()
                job = self._job_by_id(event.job_id)
                if job is not None and job.agent_id is not None:
                    return self._consume_job_result(job, job.agent_id)
            try:
                sessions = await repo.list_by_root_conversation(
                    conversation_id,
                )
            except Exception as exc:  # noqa: BLE001
                _log.warning(
                    "sub_agent.wait_any.poll_failed", error=repr(exc),
                )
                return (
                    "[sub_agent error] could not poll sub-agents for "
                    "this conversation."
                )
            if not sessions and not self._ajm_jobs(conversation_id):
                return (
                    "[sub_agent error] no sub-agents exist in this "
                    "conversation — nothing to wait for.  Spawn one "
                    "via the ``agent`` tool first."
                )
            # Find a terminal sub-agent; if several terminal, prefer the
            # MOST RECENTLY updated so a "reap the latest" call gets the
            # freshest result.
            terminal = [s for s in sessions if s.status in _TERMINAL]
            if terminal:
                pick = max(terminal, key=lambda s: s.updated_at)
                final = _extract_final_text(pick)
                self._suppress_dispatcher(pick.id.value, pick.status.value)
                # ONLY this sub-agent's delivery is consumed — the others keep
                # their pending SYSTEM_NOTICE (see the docstring).
                picked_job = self._find_job(conversation_id, pick.id.value)
                self._suppress_job_delivery(
                    picked_job.id if picked_job is not None else None,
                )
                return (
                    f"settled=true | id={pick.id.value}"
                    f" | status={pick.status.value}"
                    f" | rounds={pick.rounds}\n"
                    f"Result:\n{final}"
                )
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                # Nobody settled — describe what is still running.
                jobs_by_agent = self._jobs_by_agent(conversation_id)
                summary_lines = [
                    f"settled=false — {len(sessions)} sub-agent(s) still "
                    "running; each result will be delivered to you "
                    "automatically. End your turn instead of waiting again.",
                ]
                for s in sessions:
                    summary_lines.append(
                        f"  · {s.id.value}: status={s.status.value} "
                        f"rounds={s.rounds}"
                        f"{self._job_annotation(jobs_by_agent.get(s.id.value))}"
                    )
                return "\n".join(summary_lines)
            slice_s = min(_WAIT_POLL_S, remaining)
            if first is None:
                await asyncio.sleep(slice_s)
            else:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        asyncio.shield(first), timeout=slice_s,
                    )

    # ------------------------------------------------------------------
    # stop
    # ------------------------------------------------------------------

    async def _stop(self, request: ToolInvocationRequest) -> str:
        sa_id = self._require_id(request)
        if sa_id is None:
            return (
                "[sub_agent error] 'id' is required for action=stop. "
                "Use action=list to find sub-agent ids."
            )
        # The bridge is the ONE place a sub-agent is cancelled: it transitions
        # the AJM job to CANCELLED (releasing its concurrency slot so a queued
        # sub-agent can start — the bare abort registry cannot do that) AND
        # signals the per-sub-agent abort flag.  Fall back to the registry only
        # when no bridge is wired.
        bridge = self._bridge
        try:
            if bridge is not None:
                cancelled = await bridge.cancel(sa_id)
            elif self._abort_registry is not None:
                cancelled = self._abort_registry.abort(sa_id)
            else:
                return "[sub_agent error] abort registry is not wired."
        except Exception as exc:  # noqa: BLE001
            _log.warning("sub_agent.stop.failed", id=sa_id, error=repr(exc))
            return f"[sub_agent error] could not stop sub-agent '{sa_id}'."
        if cancelled:
            # Main agent has explicitly acknowledged the stop — suppress
            # every terminal-status variant of the pending dispatcher
            # notification for this sub-agent id.  The status the run
            # eventually reaches is one of these three; suppressing all
            # is idempotent (extras evict via LRU).
            for _status in ("interrupted", "done", "error"):
                self._suppress_dispatcher(sa_id, _status)
            return f"stop signal sent to sub-agent '{sa_id}'."
        return (
            f"sub-agent '{sa_id}' is not currently running "
            "(may have already completed or been stopped)."
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _require_id(self, request: ToolInvocationRequest) -> str | None:
        raw = request.arguments.get("id")
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
        return None

    def _suppress_dispatcher(self, sa_id: str, status_value: str) -> None:
        """Tell the :class:`BackgroundJobDispatcher` that this
        sub-agent's terminal dedup_key has been delivered
        synchronously — no auto-notify needed.  No-op when no
        dispatcher is wired (unit stubs) or when :meth:`mark_delivered`
        is not exposed.  Best-effort — never raises."""
        dispatcher = self._dispatcher
        if dispatcher is None:
            return
        mark = getattr(dispatcher, "mark_delivered", None)
        if mark is None:
            return
        try:
            mark(f"subagent:{sa_id}:{status_value}")
        except Exception:  # noqa: BLE001 — never mask tool result
            pass

    def _resolve_timeout(self, request: ToolInvocationRequest) -> float:
        """Clamp the caller's ``timeout_s`` into ``[1, _WAIT_MAX_TIMEOUT_S]``."""
        raw = request.arguments.get("timeout_s")
        try:
            timeout_s = float(raw) if raw is not None else 30.0
        except (TypeError, ValueError):
            timeout_s = 30.0
        return max(1.0, min(_WAIT_MAX_TIMEOUT_S, timeout_s))

    # -- AsyncJobManager access (all no-ops when no manager is wired) ----

    @staticmethod
    def _owner_id(conversation_id: Any) -> str:
        """The AJM owner key for a conversation.

        The bridge registers every spawn with ``owner_id=<conversation id
        STRING>``, while ``ToolInvocationRequest.conversation_id`` is the
        ``ConversationId`` value object — so the raw VO would never match a
        single job.  Normalise here, once, rather than at each call site.
        """
        return getattr(conversation_id, "value", conversation_id)

    def _ajm_jobs(self, conversation_id: Any) -> list[AsyncJob]:
        """Every SUBAGENT job owned by this conversation."""
        if self._ajm is None:
            return []
        return self._ajm.list_by_owner(
            self._owner_id(conversation_id), kind=AsyncJobKind.SUBAGENT,
        )

    def _jobs_by_agent(self, conversation_id: Any) -> dict[str, AsyncJob]:
        """This conversation's SUBAGENT jobs keyed by sub-agent id.

        ``AsyncJob.agent_id`` IS the sub-agent id (the bridge registers every
        spawn that way), which is the whole ``subagent_id -> job`` index this
        handler needs.  Jobs with no ``agent_id`` are skipped rather than
        guessed at.  Insertion order is registration order, so a later re-spawn
        of the same id naturally wins.
        """
        return {
            job.agent_id: job
            for job in self._ajm_jobs(conversation_id)
            if job.agent_id is not None
        }

    def _find_job(self, conversation_id: Any, sa_id: str) -> AsyncJob | None:
        """This sub-agent's job, or ``None`` (no manager / evicted / legacy)."""
        return self._jobs_by_agent(conversation_id).get(sa_id)

    def _job_by_id(self, job_id: str) -> AsyncJob | None:
        return None if self._ajm is None else self._ajm.get(job_id)

    @staticmethod
    def _job_annotation(job: AsyncJob | None) -> str:
        """`` · job=<state>`` suffix, or empty when there is nothing to add.

        Only ``queued`` and ``running`` are surfaced: those two are invisible
        in the session repo yet change what the main agent should do next
        (a queued sub-agent has made ZERO progress and is waiting for a
        concurrency slot).  A terminal job adds nothing the session status has
        not already said, so it is left out to keep the row readable.
        """
        if job is None:
            return ""
        if job.status is AsyncJobStatus.QUEUED:
            return " · job=queued (waiting for an execution slot)"
        if job.status is AsyncJobStatus.RUNNING:
            return " · job=running"
        return ""

    def _suppress_job_delivery(self, job_id: str | None) -> None:
        """Consume this job's delivery — a synchronous read IS the delivery.

        Stops the eventual :meth:`AsyncJobManager.complete_job` from fanning a
        completion event out to the conversation's sinks, which is what would
        otherwise append a redundant SYSTEM_NOTICE for a result the parent
        agent has already been handed inline.
        """
        if job_id is None or self._ajm is None:
            return
        self._ajm.mark_delivery_suppressed(job_id)
        _log.info(
            "subagent.tool.wait_suppressed_delivery",
            extra={"job_id": job_id},
        )

    def _consume_job_result(self, job: AsyncJob, sa_id: str) -> str:
        """Render a settled job's outcome and consume its delivery."""
        self._suppress_job_delivery(job.id)
        if job.status is AsyncJobStatus.CANCELLED:
            self._suppress_dispatcher(sa_id, "interrupted")
            return (
                f"settled=true | id={sa_id} | status=cancelled\n"
                "Result:\nThis sub-agent was stopped before it finished."
            )
        if job.error is not None:
            self._suppress_dispatcher(sa_id, "error")
            return (
                f"settled=true | id={sa_id} | status=failed\n"
                f"Result:\n{job.error.render_for_llm()}"
            )
        self._suppress_dispatcher(sa_id, "done")
        return (
            f"settled=true | id={sa_id} | status=completed\n"
            f"Result:\n{job.result_text or '(no output)'}"
        )

    def _abort_signalled(self, sa_id: str) -> bool:
        """Whether a stop has already been signalled for this sub-agent.

        Best-effort: a registry that does not expose the query simply reports
        ``False`` and the caller keeps waiting out its budget.
        """
        registry = self._abort_registry
        if registry is None:
            return False
        is_aborted = getattr(registry, "is_aborted", None)
        if is_aborted is None:
            return False
        try:
            return bool(is_aborted(sa_id))
        except Exception:  # noqa: BLE001 — never mask the wait result
            return False
