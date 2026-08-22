# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Scheduled-task management HTTP routes (WebUI manager panel).

Routes (all under ``/api/scheduled-tasks``):

- ``GET  /``                 — list every scheduled task (optionally one
  conversation's, via ``?conversation_id=``).
- ``GET  /catalog``          — the tool + skill names the editor offers as
  per-task whitelists (checkbox sources).
- ``GET  /{task_id}``        — one task.
- ``PATCH /{task_id}``       — edit prompt / schedule / name / repeat /
  enabled_tools / enabled_skills.
- ``POST /{task_id}/pause``  — pause (``enabled=False``).
- ``POST /{task_id}/resume`` — resume (recomputes ``next_run_at`` from now).
- ``POST /{task_id}/run``    — run once immediately (off-schedule).
- ``DELETE /{task_id}``      — remove.

The MUTATING actions delegate to the SAME
:class:`~qai.chat.application.ports.ScheduledTaskToolPort` implementation the
LLM-facing ``scheduled_task`` tool uses, so schedule parsing, whitelist
coercion and next-run recomputation have a single source of truth (the route
is a thin transport adapter — it never re-implements that validation). The
handler takes a :class:`ToolInvocationRequest` and returns a JSON string; we
build the request, parse the string, and surface a clean typed envelope /
HTTP error. ``list`` / ``get`` / ``catalog`` read the store + tool registry
directly (they need no per-conversation tool scoping).

Skill whitelist note: ``enabled_skills`` is persisted + editable here, but the
runner does not yet enforce it at execution (it only exposes a skill *disable*
set to a turn). The catalog still lists skills so the UI can capture the
intent; enforcement is a later change. Tool whitelist IS enforced.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from qai.chat.application.use_cases.tool_advertise import (
    TOOL_ORDER,
    order_tool_names,
)
from qai.chat.application.ports import (
    ScheduledTaskToolPort,
    ToolInvocationRequest,
)
from qai.chat.domain.ids import ConversationId, TabId

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container
    from qai.platform.scheduling.scheduled_task import ScheduledTask
    from qai.platform.scheduling.task_store import SqliteScheduledTaskStore


#: Placeholder tab/conversation id for tool calls that target an already
#: resolved task_id (update / pause / resume / remove / run). Those actions
#: never read the request's tab/conversation, but ``ConversationId``/``TabId``
#: reject empty values — and a GLOBAL task carries ``None`` for both.
_UNBOUND_ID = "unbound"


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


class ScheduledTaskItem(BaseModel):
    """A task as the manager panel renders it (mirrors the tool's task view)."""
    # See ``qai.platform.config.settings.WorkspaceSettings`` for the same
    # opt-out and its full rationale. ``model_id`` here is the LLM model
    # identifier the scheduled task was configured with — a domain-specific
    # name that predates pydantic v2's ``model_`` reservation, and renaming
    # would break the wire contract with the frontend
    # (``ScheduledTaskItem`` is a DTO the manager panel deserialises).
    model_config = {"protected_namespaces": ()}

    task_id: str
    name: str
    conversation_id: str
    #: True when the task is not bound to a conversation (global): its result
    #: surfaces in the notification center + run history instead of a chat.
    is_global: bool = False
    #: Title of the bound conversation (resolved on the list path so the
    #: manager panel can show + link to it). Empty when the conversation is
    #: gone or unresolved.
    conversation_title: str = ""
    model_id: str | None = None
    prompt: str
    schedule: str
    state: str
    enabled: bool
    repeat_times: int | None = None
    completed_runs: int
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_status: str = ""
    last_error: str = ""
    enabled_tools: list[str] = []
    enabled_skills: list[str] = []
    #: Wall-clock intent (appended; both optional so older clients are
    #: unaffected): the explicit first fire of a recurring task (ISO-8601) and
    #: the east-of-UTC offset whose clock a daily/weekly schedule names.
    start_at: str | None = None
    tz_offset_minutes: int | None = None


class ScheduledTaskRunItem(BaseModel):
    """One recorded execution of a task (the run-history / notification body)."""

    ok: bool
    status: str
    result_text: str
    ran_at: str


class ScheduledTaskRunsResponse(BaseModel):
    runs: list[ScheduledTaskRunItem]
    total: int


class ScheduledTaskNotificationItem(BaseModel):
    """One UNREAD run projected as a notification bell entry.

    The frontend bell IS the projection of this endpoint's output — the WS
    ``scheduling.task_fired`` event is the fast live path but this endpoint
    is the durable source of truth (a fire's row lives in
    ``scheduling_task_run`` regardless of whether the WS delivery succeeded).
    Shape mirrors the frontend ``notifications.enqueue`` payload so the
    reconnect-backfill path merges into the same store schema the live path
    populates.
    """

    #: The run row id — stable across reconnects, so the store can de-dup
    #: (a live WS event and a subsequent backfill produce the SAME id and
    #: the store treats the second as a no-op).
    id: str
    task_id: str
    task_name: str
    #: Bound conversation id, empty string for GLOBAL tasks (the bell shows
    #: a "🌐 Global" badge and opens the full result modal instead of
    #: routing to a conversation).
    conversation_id: str
    ok: bool
    #: Truncated to 200 chars server-side to keep the backfill payload
    #: bounded even after weeks of unread history. The full text is still
    #: reachable via the per-task run-history view.
    result_preview: str
    ran_at: str


class ScheduledTaskNotificationListResponse(BaseModel):
    notifications: list[ScheduledTaskNotificationItem]
    total: int


class ScheduledTaskNotificationMarkResponse(BaseModel):
    """Ack for the two mark-read endpoints — carries the affected count so a
    caller can decide whether to update its local unread badge without a
    round-trip re-fetch."""

    marked: int


class ScheduledTaskListResponse(BaseModel):
    tasks: list[ScheduledTaskItem]
    total: int


class ScheduledTaskCatalogResponse(BaseModel):
    """Tool + skill names the editor offers as per-task whitelist options."""

    tools: list[str]
    skills: list[str]


class ScheduledTaskUpdateRequest(BaseModel):
    """Editable fields. Every field is optional; only present keys are applied.

    ``None`` for ``repeat_times`` explicitly means "unbounded"; to leave a
    field unchanged, omit it from the JSON body entirely (unset).
    """

    prompt: str | None = None
    schedule: str | None = None
    name: str | None = None
    repeat_times: int | None = None
    enabled_tools: list[str] | None = None
    enabled_skills: list[str] | None = None
    start_at: str | None = None


def _task_to_item(view: dict[str, Any]) -> ScheduledTaskItem:
    """Map a handler ``_task_view`` dict into the typed item."""
    return ScheduledTaskItem(
        task_id=str(view.get("task_id", "")),
        name=str(view.get("name", "")),
        conversation_id=str(view.get("conversation_id", "")),
        is_global=bool(view.get("is_global", False)),
        model_id=view.get("model_id"),
        prompt=str(view.get("prompt", "")),
        schedule=str(view.get("schedule", "")),
        state=str(view.get("state", "")),
        enabled=bool(view.get("enabled", False)),
        repeat_times=view.get("repeat_times"),
        completed_runs=int(view.get("completed_runs", 0) or 0),
        next_run_at=view.get("next_run_at"),
        last_run_at=view.get("last_run_at"),
        last_status=str(view.get("last_status", "")),
        last_error=str(view.get("last_error", "")),
        enabled_tools=list(view.get("enabled_tools", []) or []),
        enabled_skills=list(view.get("enabled_skills", []) or []),
        start_at=str(view.get("start_at") or "") or None,
        tz_offset_minutes=view.get("tz_offset_minutes"),
    )


def _domain_to_item(
    task: "ScheduledTask", *, conversation_title: str = ""
) -> ScheduledTaskItem:
    """Map a domain ``ScheduledTask`` into the typed item (list/get path)."""
    return ScheduledTaskItem(
        task_id=task.task_id,
        name=task.display_name,
        conversation_id=task.conversation_id or "",
        is_global=task.is_global,
        conversation_title=conversation_title,
        model_id=task.model_id,
        prompt=task.prompt,
        schedule=task.schedule.display,
        state=task.state.value,
        enabled=task.enabled,
        repeat_times=task.repeat_times,
        completed_runs=task.completed_runs,
        next_run_at=(
            task.next_run_at.isoformat() if task.next_run_at is not None else None
        ),
        last_run_at=(
            task.last_run_at.isoformat() if task.last_run_at is not None else None
        ),
        last_status=task.last_status,
        last_error=task.last_error,
        enabled_tools=list(task.enabled_tools),
        enabled_skills=list(task.enabled_skills),
        start_at=(
            task.schedule.start_at.isoformat()
            if task.schedule.start_at is not None
            else None
        ),
        tz_offset_minutes=task.schedule.tz_offset_minutes,
    )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the scheduled-task management router."""
    router = APIRouter(prefix="/api/scheduled-tasks", tags=["scheduled-tasks"])

    def _store() -> "SqliteScheduledTaskStore":
        store = getattr(container.chat, "scheduling_task_store", None)
        if store is None:
            raise HTTPException(
                status_code=503,
                detail="scheduled-task feature is not available",
            )
        return store

    def _handler() -> "ScheduledTaskToolPort":
        handler = getattr(container.chat, "scheduling_tool_handler", None)
        if handler is None:
            raise HTTPException(
                status_code=503,
                detail="scheduled-task feature is not available",
            )
        return handler

    async def _invoke(task: "ScheduledTask | None", args: dict[str, Any]) -> dict[str, Any]:
        """Run the tool handler with a task's bound tab/conversation.

        ``task`` supplies the tab/conversation the handler stamps onto its
        request (create-time capture is irrelevant for edits, but the handler
        signature requires them). For actions that target a task_id the
        handler ignores tab/conversation entirely, so an unbound task supplies
        a placeholder: a GLOBAL task (the create default) has
        ``conversation_id``/``tab_id`` of ``None``, and ``ConversationId.of``
        rejects both ``None`` and ``""`` — passing those straight through made
        every mutating route 500 on a global task. Raises ``HTTPException`` on
        a handler error.
        """
        handler = _handler()
        conv = ConversationId.of(
            (task.conversation_id if task is not None else None) or _UNBOUND_ID
        )
        tab = TabId.of((task.tab_id if task is not None else None) or _UNBOUND_ID)
        raw = await handler.execute(
            ToolInvocationRequest(
                tab_id=tab,
                conversation_id=conv,
                tool_name="scheduled_task",
                arguments=args,
            )
        )
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:  # pragma: no cover — defensive
            raise HTTPException(
                status_code=500, detail=f"scheduled-task tool returned invalid output: {exc}"
            ) from None
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            detail = (
                payload.get("error")
                if isinstance(payload, dict)
                else "scheduled-task operation failed"
            )
            raise HTTPException(status_code=400, detail=str(detail))
        return payload

    def _first_view(payload: dict[str, Any]) -> dict[str, Any]:
        """Extract the single task view an update/pause/resume returns."""
        for key in ("updated", "paused", "resumed", "created"):
            v = payload.get(key)
            if isinstance(v, dict):
                return v
        raise HTTPException(
            status_code=500, detail="scheduled-task tool returned no task view"
        )

    async def _resolve_titles(conv_ids: "set[str]") -> dict[str, str]:
        """Map each conversation id → its title (best-effort, empty on miss).

        Reads the ``chat`` context's conversation repo. A gone / unresolved
        conversation yields no entry, so the DTO's ``conversation_title``
        defaults to ``""`` (the panel then shows the id / omits the link).
        Per-id lookups; the scheduled-task count is small so this stays cheap.
        """
        repo = getattr(container.chat, "conversations", None)
        if repo is None:
            return {}
        out: dict[str, str] = {}
        for cid in conv_ids:
            if not cid:
                continue
            try:
                conv = await repo.find(ConversationId.of(cid))
            except Exception:  # noqa: BLE001 — best-effort; title is optional
                conv = None
            title = getattr(conv, "title", "") if conv is not None else ""
            if isinstance(title, str) and title:
                out[cid] = title
        return out

    @router.get("", response_model=ScheduledTaskListResponse)
    async def list_tasks(
        conversation_id: str | None = Query(default=None),
    ) -> ScheduledTaskListResponse:
        """List all scheduled tasks (or one conversation's)."""
        store = _store()
        if conversation_id:
            tasks = await store.list_by_conversation(conversation_id)
        else:
            tasks = await store.list_all()
        titles = await _resolve_titles({t.conversation_id for t in tasks})
        items = [
            _domain_to_item(t, conversation_title=titles.get(t.conversation_id, ""))
            for t in tasks
        ]
        return ScheduledTaskListResponse(tasks=items, total=len(items))

    @router.get("/{task_id}/runs", response_model=ScheduledTaskRunsResponse)
    async def list_runs(
        task_id: str,
        limit: int = Query(default=20, ge=1, le=200),
    ) -> ScheduledTaskRunsResponse:
        """Recent run history (newest first) — the full result text of each
        fire, used by the notification center + the task run-history view."""
        store = _store()
        runs = await store.list_runs(task_id, limit=limit)
        items = [
            ScheduledTaskRunItem(
                ok=r.ok,
                status=r.status,
                result_text=r.result_text,
                ran_at=r.ran_at.isoformat(),
            )
            for r in runs
        ]
        return ScheduledTaskRunsResponse(runs=items, total=len(items))

    @router.get(
        "/notifications/unread",
        response_model=ScheduledTaskNotificationListResponse,
    )
    async def list_unread_notifications(
        limit: int = Query(default=200, ge=1, le=500),
    ) -> ScheduledTaskNotificationListResponse:
        """Return every unread fire across all tasks, newest first.

        The bell is a PROJECTION of this endpoint: the frontend calls it on
        WebSocket connect AND on every WS reconnect, merging results into the
        local store keyed by ``id`` (a live WS ``scheduling.task_fired`` event
        that shares the same run row id de-dups against the backfill, so
        clients that stayed connected never see duplicates).

        Task-name resolution is per-run to keep the response self-contained —
        the WebUI otherwise would need a second round-trip for the label of
        every notification. Tasks that were deleted between fire and read are
        surfaced as their raw id so the notification is still actionable
        (opening it takes the user to the run history modal, not a broken
        task edit page).
        """
        store = _store()
        runs = await store.list_unread_runs(limit=limit)
        # Resolve names ONCE per unique task_id (a task fires many times
        # so runs cluster; caching by task_id keeps this at O(unique_tasks)
        # SELECTs, not O(unread) — production unread sets sit in the
        # single digits with even fewer unique tasks).
        name_by_id: dict[str, str] = {}
        for r in runs:
            if r.task_id in name_by_id:
                continue
            task = await store.get(r.task_id)
            # ``display_name`` = ``name`` if set, else the task id — matches
            # the tool-layer echo path (``ScheduledTaskToolPort``); an
            # empty-string ``name`` here would surface as a blank bell label,
            # which is worse than the raw id.
            name_by_id[r.task_id] = (
                task.display_name if task is not None else r.task_id
            )
        items = [
            ScheduledTaskNotificationItem(
                id=r.id,
                task_id=r.task_id,
                task_name=name_by_id.get(r.task_id, r.task_id),
                conversation_id=r.conversation_id,
                ok=r.ok,
                result_preview=r.result_text[:200],
                ran_at=r.ran_at.isoformat(),
            )
            for r in runs
        ]
        return ScheduledTaskNotificationListResponse(
            notifications=items, total=len(items)
        )

    @router.post(
        "/notifications/{run_id}/mark-read",
        response_model=ScheduledTaskNotificationMarkResponse,
    )
    async def mark_notification_read(
        run_id: str,
    ) -> ScheduledTaskNotificationMarkResponse:
        """Dismiss ONE bell entry. Idempotent — a re-mark on an already-read
        run returns ``marked=0`` and is a no-op (the store's ``UPDATE ...
        WHERE notified_at = ''`` guard makes this cheap). A run id the store
        does not know about also returns ``marked=0``; we do NOT raise 404
        because a stale client that lost sync (deleted task, DB reset) would
        otherwise loop-fail dismissing its old items."""
        changed = await _store().mark_run_read(run_id)
        return ScheduledTaskNotificationMarkResponse(marked=1 if changed else 0)

    @router.post(
        "/notifications/mark-all-read",
        response_model=ScheduledTaskNotificationMarkResponse,
    )
    async def mark_all_notifications_read() -> ScheduledTaskNotificationMarkResponse:
        """Bulk-dismiss the entire unread set. A fire that lands mid-request
        is not swept — the next bulk call picks it up. Returns the number of
        rows actually updated so the bell can update its badge count."""
        marked = await _store().mark_all_runs_read()
        return ScheduledTaskNotificationMarkResponse(marked=marked)

    @router.get("/catalog", response_model=ScheduledTaskCatalogResponse)
    async def catalog() -> ScheduledTaskCatalogResponse:
        """Tool + skill options the editor offers as per-task whitelists.

        Skills mirror the Skill panel (``GET /api/skills``): the CURRENTLY-
        ENABLED skill ids (mode != ``off``), which is exactly the set the
        scheduled runner can enforce a whitelist against. Uses the SAME
        ``user_prefs.list_skills_use_case`` source so the checkboxes and the
        runtime enforcement never drift.
        """
        tools = order_tool_names(list(TOOL_ORDER))
        skills: list[str] = []
        prefs = getattr(container, "user_prefs", None)
        uc = getattr(prefs, "list_skills_use_case", None) if prefs else None
        if uc is not None:
            try:
                result = await uc.execute()
                rows = result.get("skills", []) if isinstance(result, dict) else []
                skills = sorted(
                    str(s["skill_id"])
                    for s in rows
                    if isinstance(s, dict)
                    and s.get("enabled") is True
                    and s.get("skill_id")
                )
            except Exception:  # noqa: BLE001 — catalog is best-effort
                skills = []
        return ScheduledTaskCatalogResponse(tools=tools, skills=skills)

    @router.get("/{task_id}", response_model=ScheduledTaskItem)
    async def get_task(task_id: str) -> ScheduledTaskItem:
        """Return one scheduled task."""
        task = await _store().get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
        return _domain_to_item(task)

    @router.patch("/{task_id}", response_model=ScheduledTaskItem)
    async def update_task(
        task_id: str, body: ScheduledTaskUpdateRequest
    ) -> ScheduledTaskItem:
        """Edit a task's content / schedule / whitelists."""
        task = await _store().get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
        # Only forward keys the client actually set (exclude_unset), so an
        # omitted field is left unchanged while an explicit null (repeat_times)
        # is honoured as "unbounded".
        set_fields = body.model_dump(exclude_unset=True)
        args: dict[str, Any] = {"action": "update", "task_id": task_id}
        if "prompt" in set_fields:
            args["prompt"] = set_fields["prompt"]
        if "schedule" in set_fields:
            args["schedule"] = set_fields["schedule"]
        if "start_at" in set_fields:
            args["start_at"] = set_fields["start_at"]
        if "name" in set_fields:
            args["name"] = set_fields["name"]
        if "repeat_times" in set_fields:
            args["repeat"] = set_fields["repeat_times"]
        if "enabled_tools" in set_fields:
            args["enabled_tools"] = set_fields["enabled_tools"]
        if "enabled_skills" in set_fields:
            args["enabled_skills"] = set_fields["enabled_skills"]
        payload = await _invoke(task, args)
        return _task_to_item(_first_view(payload))

    @router.post("/{task_id}/pause", response_model=ScheduledTaskItem)
    async def pause_task(task_id: str) -> ScheduledTaskItem:
        """Pause a task (stops firing until resumed)."""
        task = await _store().get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
        payload = await _invoke(task, {"action": "pause", "task_id": task_id})
        return _task_to_item(_first_view(payload))

    @router.post("/{task_id}/resume", response_model=ScheduledTaskItem)
    async def resume_task(task_id: str) -> ScheduledTaskItem:
        """Resume a paused task (recomputes next run from now)."""
        task = await _store().get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
        payload = await _invoke(task, {"action": "resume", "task_id": task_id})
        return _task_to_item(_first_view(payload))

    @router.post("/{task_id}/run")
    async def run_task(task_id: str) -> dict[str, Any]:
        """Queue a task to run once on the next scheduler tick (off-schedule)."""
        task = await _store().get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
        payload = await _invoke(task, {"action": "run", "task_id": task_id})
        return {"queued": payload.get("queued", task_id)}

    @router.delete("/{task_id}")
    async def remove_task(task_id: str) -> dict[str, Any]:
        """Delete a task."""
        task = await _store().get(task_id)
        if task is None:
            raise HTTPException(status_code=404, detail=f"task {task_id!r} not found")
        payload = await _invoke(task, {"action": "remove", "task_id": task_id})
        return {"removed": payload.get("removed", task_id)}

    return router
