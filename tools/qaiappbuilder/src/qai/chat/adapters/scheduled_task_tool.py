# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Agent-facing ``scheduled_task`` tool: create / manage scheduled tasks.

A single compressed tool (one schema, an ``action`` discriminator) rather than
seven separate tools, so the advertised tool surface stays small. The handler
dispatches on ``action`` to the runtime scheduler
(:class:`~qai.platform.scheduling.SchedulerService`) and its store:

* ``create``  — schedule a new self-contained task (``prompt`` + ``schedule``).
* ``list``    — list this conversation's tasks.
* ``update``  — change a task's prompt / schedule / name / repeat.
* ``pause``   — stop a task firing (keeps the row).
* ``resume``  — re-arm a paused task from now.
* ``remove``  — delete a task.
* ``run``     — fire a task once immediately, off its schedule.

Contract (matches the tool registry): the handler is ``async execute(request)
-> str`` and NEVER raises — every failure is returned as a human-readable
string so the caller records it as the tool result rather than crashing the
turn. The returned text is what the model sees.

Delivery target: a created task is bound to the calling turn's
``conversation_id`` / ``tab_id`` so its later result is folded back into this
conversation (see the completion bridge).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING, Any

from qai.chat.application.ports import ToolInvocationRequest
from qai.chat.domain.ids import ConversationId
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger
from qai.platform.scheduling import (
    CronUnavailableError,
    Schedule,
    ScheduleParseError,
    ScheduledTask,
    SchedulerService,
    SqliteScheduledTaskStore,
    TaskState,
    parse_schedule,
)
from qai.platform.time import Clock, SystemClock, from_iso8601

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable

    from qai.chat.application.ports import ConversationRepositoryPort

__all__ = ["SCHEDULED_TASK_TOOL_SCHEMA", "ScheduledTaskToolHandler"]

_log = get_logger("qai.chat.scheduling")

_ACTIONS = ("create", "list", "update", "pause", "resume", "remove", "run")

SCHEDULED_TASK_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "scheduled_task",
        "description": (
            "Create and manage scheduled tasks — self-contained prompts that "
            "fire automatically at a later time.\n"
            "\n"
            "How to use each action:\n"
            "- 'create': add a new task from a prompt + schedule (+ optional "
            "  repeat count).\n"
            "- 'list': inspect existing tasks. Use this BEFORE any action that "
            "  needs a task_id — never guess an id.\n"
            "- 'update' / 'pause' / 'resume' / 'run' / 'remove': operate on "
            "  one existing task by task_id.\n"
            "- To stop a task the user no longer wants: 'list' first, find its "
            "  task_id, then 'remove'. Do NOT try to guess the id.\n"
            "\n"
            "Runtime contract (READ BEFORE COMPOSING A PROMPT):\n"
            "- Every fire runs headless: no user is watching, and the run has "
            "  NO access to this chat or its history.\n"
            "- The run CANNOT ask follow-up questions or wait for a reply — "
            "  interactive tools are disabled. Compose one prompt that stands "
            "  entirely on its own; if you're tempted to ask the user for "
            "  clarification before setting the schedule, do that first HERE "
            "  in this chat, then encode the resolved intent into the prompt.\n"
            "- Prefer ONE task with a repeat count over creating multiple "
            "  near-duplicate one-shot tasks. See 'repeat' for the pattern.\n"
            "\n"
            "Where the result lands (READ CAREFULLY — do NOT default to "
            "conversation just because the user is asking from this chat):\n"
            "- DEFAULT — do not pass 'deliver' at all. The task is GLOBAL, "
            "  not tied to any chat. When it fires, the user is notified via "
            "  the notification center (a bell/toast) and can read the full "
            "  result there or in the task's run history panel. This is the "
            "  RIGHT choice for essentially every reminder, periodic report, "
            "  or standalone job — including tasks the user asked for FROM "
            "  this chat. The user asking here is not a reason to bind here.\n"
            "- Pass deliver='conversation' ONLY when the user has made it "
            "  EXPLICIT that they want the result to land inside this specific "
            "  conversation (e.g. 'post the daily brief in this chat', 'reply "
            "  here every hour'). Binding a task to this chat means every fire "
            "  appends an assistant turn HERE, which quickly floods a working "
            "  conversation with periodic side traffic — the exact reason the "
            "  global default exists. When in doubt, choose the default.\n"
            "\n"
            "Schedule forms are documented on the 'schedule' parameter."
        ),
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(_ACTIONS),
                    "description": (
                        "The operation: 'create' a new task; 'list' this "
                        "conversation's tasks; 'update' / 'pause' / 'resume' / "
                        "'remove' an existing task by 'task_id'; 'run' a task "
                        "once immediately off its schedule."
                    ),
                },
                "task_id": {
                    "type": "string",
                    "description": (
                        "Target task id (required for update / pause / resume "
                        "/ remove / run; ignored for create / list)."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "The self-contained instruction the scheduled run "
                        "executes (required for create; optional for update)."
                    ),
                },
                "schedule": {
                    "type": "string",
                    "description": (
                        "When to run (required for create; optional for "
                        "update). Supported forms:\n"
                        "- 'daily HH:MM±ZZ:ZZ' — every day at a wall-clock "
                        "time in the stated timezone, e.g. 'daily 07:30+08:00'.\n"
                        "- 'weekly <dow> HH:MM±ZZ:ZZ' — every week on that "
                        "weekday (mon..sun) at that wall-clock time, e.g. "
                        "'weekly mon 09:00+08:00'.\n"
                        "- 'every <duration>' — repeating, e.g. 'every 2h'.\n"
                        "- '30m' / '2h' / '1d' — one-time, that far from now.\n"
                        "- '0 9 * * *' — 5-field cron, interpreted in UTC.\n"
                        "- an ISO-8601 timestamp — one-time at that instant.\n"
                        "IMPORTANT: whenever the user asks for a daily or "
                        "weekly clock time ('every morning at 7:30', 'Mondays "
                        "at 9am'), USE the 'daily'/'weekly' forms WITH the "
                        "user's UTC offset. Do NOT hand-convert the time into a "
                        "bare cron expression or an ISO timestamp: a bare cron "
                        "names a UTC wall clock, so '30 7 * * *' fires at 15:30 "
                        "local for a +08:00 user — the wrong time."
                    ),
                },
                "start_at": {
                    "type": "string",
                    "description": (
                        "Optional ISO-8601 timestamp WITH offset (e.g. "
                        "'2026-08-02T09:00:00+08:00') giving the FIRST time a "
                        "RECURRING task fires; later fires follow the schedule's "
                        "own cadence. Use it for 'every 2 hours, starting at "
                        "09:00'. Only meaningful for recurring schedules "
                        "('every ...', 'daily ...', 'weekly ...', cron); a "
                        "one-time schedule ignores it. Accepted on create and "
                        "update; on update, omitting it keeps the task's "
                        "existing first-run time."
                    ),
                },
                "name": {
                    "type": "string",
                    "description": "Optional human label for the task.",
                },
                "repeat": {
                    "type": "integer",
                    "description": (
                        "How many times the task should fire in total. Omit "
                        "for the natural default: a one-shot schedule ('30m', "
                        "an ISO timestamp) fires exactly ONCE; a recurring "
                        "schedule ('every ...', 'daily ...', 'weekly ...', a "
                        "cron expression) fires FOREVER. Set to a positive "
                        "integer N to bound a recurring schedule to exactly N "
                        "fires, after which the task auto-completes.\n"
                        "\n"
                        "USE THIS INSTEAD OF DUPLICATING TASKS. When the user "
                        "asks for something like 'remind me every minute, "
                        "three times', the correct answer is ONE task with "
                        "schedule='every 1m' + repeat=3 — not three separate "
                        "one-shot tasks. Same for 'every hour for a workday': "
                        "schedule='every 1h' + repeat=8, one task."
                    ),
                },
                "enabled_tools": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional tool whitelist: the tool names the scheduled "
                        "run is restricted to (e.g. ['read','grep']). Omit or "
                        "leave empty for no restriction — the run uses the full "
                        "default tool set. Accepted on create and update."
                    ),
                },
                "enabled_skills": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Optional skill whitelist: the skill ids the scheduled "
                        "run is allowed to load. When set, the run is "
                        "restricted to these skills (out of the currently "
                        "enabled skills); everything else is disabled for that "
                        "run. Omit or leave empty for no restriction. Accepted "
                        "on create and update."
                    ),
                },
                "deliver": {
                    "type": "string",
                    "enum": ["global", "conversation"],
                    "description": (
                        "For 'create' only. Delivery mode of the task's result. "
                        "OMIT THIS PARAMETER (or explicitly 'global') for the "
                        "default — a global task whose result surfaces via the "
                        "notification center + run history. Pass 'conversation' "
                        "ONLY when the user has explicitly asked for the result "
                        "to appear inside THIS chat. Being invoked from this "
                        "chat is NOT such a request — the notification center "
                        "reaches the user regardless of where they invoked the "
                        "tool. See the top-level tool description for details."
                    ),
                },
                "scope": {
                    "type": "string",
                    "enum": ["conversation", "all"],
                    "description": (
                        "For 'list' only: 'conversation' (default) lists tasks "
                        "created in THIS conversation; 'all' lists every "
                        "scheduled task across all conversations, each with its "
                        "owning conversation_id."
                    ),
                },
            },
        },
    },
}


def _err(message: str) -> str:
    return json.dumps({"ok": False, "error": message}, ensure_ascii=False)


def _ok(payload: dict[str, Any]) -> str:
    return json.dumps({"ok": True, **payload}, ensure_ascii=False)


def _task_view(task: ScheduledTask) -> dict[str, Any]:
    """A compact, model-facing view of a task (no internal version/CAS)."""
    return {
        "task_id": task.task_id,
        "name": task.display_name,
        "conversation_id": task.conversation_id or "",
        "is_global": task.is_global,
        "model_id": task.model_id,
        "prompt": task.prompt,
        "schedule": task.schedule.display,
        "state": task.state.value,
        "enabled": task.enabled,
        "repeat_times": task.repeat_times,
        "completed_runs": task.completed_runs,
        "next_run_at": (
            task.next_run_at.isoformat() if task.next_run_at is not None else None
        ),
        "last_run_at": (
            task.last_run_at.isoformat() if task.last_run_at is not None else None
        ),
        "last_status": task.last_status,
        "last_error": task.last_error,
        "enabled_tools": list(task.enabled_tools),
        "enabled_skills": list(task.enabled_skills),
        # Wall-clock intent (UI echo): the explicit first fire of a recurring
        # task and the zone whose clock its cron fields name.
        "start_at": (
            task.schedule.start_at.isoformat()
            if task.schedule.start_at is not None
            else ""
        ),
        "tz_offset_minutes": task.schedule.tz_offset_minutes,
    }


def _coerce_start_at(value: Any) -> datetime | None:
    """Coerce a ``start_at`` argument into a tz-aware UTC datetime.

    ``None`` / blank means "not supplied". Anything else must be an ISO-8601
    timestamp carrying an offset; a malformed value raises :class:`ValueError`
    so the caller turns it into a tool error (the tool contract never raises).
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return from_iso8601(text)


class ScheduledTaskToolHandler:
    """Dispatch handler for the ``scheduled_task`` tool."""

    __slots__ = (
        "_scheduler",
        "_store",
        "_ids",
        "_clock",
        "_conversations",
        "_default_model_provider",
    )

    def __init__(
        self,
        *,
        scheduler: SchedulerService,
        store: SqliteScheduledTaskStore,
        ids: IdGenerator,
        clock: Clock | None = None,
        conversations: "ConversationRepositoryPort | None" = None,
        default_model_provider: "Callable[[], Awaitable[str | None]] | None" = None,
    ) -> None:
        self._scheduler = scheduler
        self._store = store
        self._ids = ids
        self._clock = clock or SystemClock()
        # Optional: used at create time to capture the conversation's current
        # model so the scheduled run uses the SAME model the user picked.
        self._conversations = conversations
        # Optional fallback: the user's globally-selected model
        # (``ui.preferences.selected_model_id``). Consulted when the target
        # conversation has no persisted assistant message carrying a model_id
        # yet — e.g. a task created on the FIRST turn of a brand-new
        # conversation, where "last assistant model" would be empty and the
        # scheduled run would otherwise fall back to the offline default
        # ("[no LLM endpoint configured]"). Best-effort; ``None`` disables it.
        self._default_model_provider = default_model_provider

    async def execute(self, request: ToolInvocationRequest) -> str:
        args = request.arguments or {}
        action = str(args.get("action", "")).strip().lower()
        if action not in _ACTIONS:
            return _err(
                f"unknown action {action!r}; expected one of {', '.join(_ACTIONS)}"
            )
        try:
            if action == "create":
                return await self._create(request, args)
            if action == "list":
                return await self._list(request, args)
            # All remaining actions target an existing task by id.
            task_id = str(args.get("task_id", "")).strip()
            if not task_id:
                return _err(f"action {action!r} requires 'task_id'")
            if action == "update":
                return await self._update(task_id, args)
            if action == "pause":
                return await self._set_enabled(task_id, enabled=False)
            if action == "resume":
                return await self._set_enabled(task_id, enabled=True)
            if action == "remove":
                return await self._remove(task_id)
            if action == "run":
                return await self._run_now(task_id)
        except Exception as exc:  # noqa: BLE001 — tool contract: never raise
            _log.warning(
                "scheduling.tool_failed", action=action, error=str(exc)
            )
            return _err(f"scheduled_task {action} failed: {exc}")
        return _err(f"unhandled action {action!r}")  # pragma: no cover

    # -- actions ----------------------------------------------------------
    async def _create(
        self, request: ToolInvocationRequest, args: dict[str, Any]
    ) -> str:
        prompt = str(args.get("prompt", "")).strip()
        schedule_text = str(args.get("schedule", "")).strip()
        if not prompt:
            return _err("create requires a non-empty 'prompt'")
        if not schedule_text:
            return _err("create requires a 'schedule'")
        try:
            start_at = _coerce_start_at(args.get("start_at"))
        except ValueError as exc:
            return _err(f"invalid 'start_at': {exc}")
        try:
            schedule = parse_schedule(
                schedule_text, now=self._clock.now(), start_at=start_at
            )
        except CronUnavailableError as exc:
            return _err(str(exc))
        except ScheduleParseError as exc:
            return _err(str(exc))
        repeat = self._coerce_repeat(args.get("repeat"))
        # Default GLOBAL: no conversation/tab binding. deliver='conversation'
        # attaches the task to the calling chat so its result folds back in.
        deliver = str(args.get("deliver", "global")).strip().lower()
        bind = deliver == "conversation"
        conv_id = request.conversation_id.value if bind else None
        tab_id = request.tab_id.value if bind else None
        # Capture the user's current model regardless (the resolver falls back
        # to the globally-selected model when the conversation has none), so a
        # global task still runs on a real model.
        model_id = await self._resolve_conversation_model(
            request.conversation_id
        )
        task = ScheduledTask(
            task_id=self._ids.new_id(),
            prompt=prompt,
            schedule=schedule,
            conversation_id=conv_id,
            tab_id=tab_id,
            name=str(args.get("name", "")).strip(),
            model_id=model_id,
            repeat_times=repeat,
            enabled_tools=self._coerce_names(args.get("enabled_tools")),
            enabled_skills=self._coerce_names(args.get("enabled_skills")),
            created_at=self._clock.now(),
        )
        stored = await self._scheduler.add(task)
        return _ok({"created": _task_view(stored)})

    async def _resolve_conversation_model(
        self, conversation_id: ConversationId
    ) -> str | None:
        """Capture the model the scheduled run should use.

        Resolution ladder (first hit wins):

        1. the most recent assistant message in the target conversation that
           recorded a ``model_id`` (the model the user was chatting with);
        2. the user's globally-selected model from
           ``ui.preferences.selected_model_id`` via ``default_model_provider``
           — this is what the WebUI sends as ``model_hint`` on a live turn, so
           it is the right capture when the conversation has NO persisted
           assistant turn yet (a task created on the FIRST turn of a brand-new
           conversation: ``conv.messages`` holds only the user prompt, so step
           1 is empty);
        3. ``None``.

        Why the fallback matters: the runner passes the captured id straight
        into ``StreamChatInput.model_hint``. An empty capture resolves to the
        settings default model, whose endpoint is typically unconfigured
        offline — the scheduled run then produces only
        ``[no LLM endpoint configured]``. Capturing the selected model here
        makes the run use the same working (cloud) endpoint the live turn used.

        Best-effort throughout: any lookup failure degrades to the next rung
        rather than blocking create.
        """
        if self._conversations is not None:
            try:
                conv = await self._conversations.find(conversation_id)
            except Exception:  # noqa: BLE001 — best-effort capture
                conv = None
            if conv is not None:
                for msg in reversed(conv.messages):
                    model_id = getattr(msg, "model_id", None)
                    if isinstance(model_id, str) and model_id:
                        return model_id
        if self._default_model_provider is not None:
            try:
                fallback = await self._default_model_provider()
            except Exception:  # noqa: BLE001 — best-effort fallback
                fallback = None
            if isinstance(fallback, str) and fallback:
                return fallback
        return None

    async def _list(
        self, request: ToolInvocationRequest, args: dict[str, Any]
    ) -> str:
        scope = str(args.get("scope", "conversation")).strip().lower()
        if scope == "all":
            tasks = await self._store.list_all()
        else:
            tasks = await self._store.list_by_conversation(
                request.conversation_id.value
            )
        return _ok(
            {
                "scope": "all" if scope == "all" else "conversation",
                "count": len(tasks),
                "tasks": [_task_view(t) for t in tasks],
            }
        )

    async def _update(self, task_id: str, args: dict[str, Any]) -> str:
        task = await self._store.get(task_id)
        if task is None:
            return _err(f"task {task_id!r} not found")
        changes: dict[str, Any] = {}
        if "prompt" in args:
            prompt = str(args.get("prompt", "")).strip()
            if not prompt:
                return _err("'prompt' cannot be empty")
            changes["prompt"] = prompt
        if "name" in args:
            changes["name"] = str(args.get("name", "")).strip()
        if "repeat" in args:
            changes["repeat_times"] = self._coerce_repeat(args.get("repeat"))
        sched_changes, error = self._resolve_schedule_change(task, args)
        if error is not None:
            return _err(error)
        changes.update(sched_changes)
        if "enabled_tools" in args:
            changes["enabled_tools"] = self._coerce_names(args.get("enabled_tools"))
        if "enabled_skills" in args:
            changes["enabled_skills"] = self._coerce_names(args.get("enabled_skills"))
        if not changes:
            return _err(
                "update requires at least one of: prompt, schedule, start_at, "
                "name, repeat, enabled_tools, enabled_skills"
            )
        updated = await self._store.save(task.with_changes(**changes))
        return _ok({"updated": _task_view(updated)})

    def _resolve_schedule_change(
        self, task: ScheduledTask, args: dict[str, Any]
    ) -> tuple[dict[str, Any], str | None]:
        """Derive the schedule-related changes an update implies.

        ``schedule`` and ``start_at`` are two views of ONE value object, so they
        resolve together: either key re-derives the :class:`Schedule` (and with
        it ``next_run_at`` / ``state``). Returns ``(changes, error)`` — a
        non-``None`` error is a human-readable message for the caller to wrap
        (the tool contract never raises).
        """
        # An omitted ``start_at`` keeps the task's existing first-run time
        # rather than silently clearing what the user set.
        if "start_at" in args:
            try:
                start_at = _coerce_start_at(args.get("start_at"))
            except ValueError as exc:
                return {}, f"invalid 'start_at': {exc}"
        else:
            start_at = task.schedule.start_at

        schedule: Schedule | None = None
        if "schedule" in args:
            schedule_text = str(args.get("schedule", "")).strip()
            if not schedule_text:
                return {}, "'schedule' cannot be empty"
            try:
                schedule = parse_schedule(
                    schedule_text, now=self._clock.now(), start_at=start_at
                )
            except (CronUnavailableError, ScheduleParseError) as exc:
                return {}, str(exc)
        elif "start_at" in args and task.schedule.recurring:
            # Only the first fire moved: patch the existing schedule instead of
            # re-parsing its text (re-parsing a bare duration would re-anchor it
            # to now). A one-shot has no first-run time to set — the same rule
            # ``parse_schedule`` applies.
            schedule = replace(task.schedule, start_at=start_at)
        if schedule is None:
            return {}, None

        # Recompute the next fire from the new schedule. Anchor on the task's
        # REAL ``last_run_at`` (the same rule ``SchedulerService.set_enabled``
        # uses when re-arming a resumed task): a task that has already fired
        # must not be dragged back to its ``start_at``. ``start_at`` means the
        # FIRST fire, so once a run happened it is spent — passing ``None`` here
        # made an edit re-apply a still-future start_at and silently skip every
        # occurrence in between (an hourly task due 09:30 jumped to 18:00).
        # Preserve a PAUSED task's paused state — changing the schedule must not
        # silently un-pause it (``resume`` is the explicit un-pause path); a
        # paused row keeps enabled=False so it stays dormant regardless.
        from qai.platform.scheduling import next_run_at

        nxt = next_run_at(
            schedule, now=self._clock.now(), last_run_at=task.last_run_at
        )
        changes: dict[str, Any] = {"schedule": schedule, "next_run_at": nxt}
        if task.state is not TaskState.PAUSED:
            changes["state"] = (
                TaskState.SCHEDULED if nxt is not None else TaskState.COMPLETED
            )
        return changes, None

    async def _set_enabled(self, task_id: str, *, enabled: bool) -> str:
        task = await self._scheduler.set_enabled(task_id, enabled)
        if task is None:
            return _err(f"task {task_id!r} not found")
        verb = "resumed" if enabled else "paused"
        return _ok({verb: _task_view(task)})

    async def _remove(self, task_id: str) -> str:
        removed = await self._scheduler.remove(task_id)
        if not removed:
            return _err(f"task {task_id!r} not found")
        return _ok({"removed": task_id})

    async def _run_now(self, task_id: str) -> str:
        task = await self._store.get(task_id)
        if task is None:
            return _err(f"task {task_id!r} not found")
        # Fire immediately by making it due now; the next tick picks it up.
        # (Deferring to the tick keeps a single execution path and the
        # at-most-once advance; no separate ad-hoc runner.) Force SCHEDULED
        # + enabled so a paused / completed task can still be run on demand —
        # ``due_tasks`` only surfaces enabled SCHEDULED rows.
        await self._store.save(
            task.with_changes(
                next_run_at=self._clock.now(),
                state=TaskState.SCHEDULED,
                enabled=True,
            )
        )
        return _ok(
            {
                "queued": task_id,
                "note": "will run on the next scheduler tick",
            }
        )

    @staticmethod
    def _coerce_repeat(value: Any) -> int | None:
        if value is None:
            return None
        try:
            n = int(value)
        except (TypeError, ValueError):
            return None
        return n if n > 0 else None

    @staticmethod
    def _coerce_names(value: Any) -> tuple[str, ...]:
        """Coerce a whitelist arg into a tuple of non-empty names.

        Accepts a list / tuple of strings (or stringifiable values); anything
        else (``None``, a bare string, malformed input) yields the empty tuple
        — i.e. "no restriction", the safe default.
        """
        if not isinstance(value, (list, tuple)):
            return ()
        names: list[str] = []
        for item in value:
            name = str(item).strip()
            if name:
                names.append(name)
        return tuple(names)
