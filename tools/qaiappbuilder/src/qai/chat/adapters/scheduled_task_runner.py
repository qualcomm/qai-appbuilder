# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Executor that drives one scheduled task as a native chat-agent turn.

This is the chat-context concrete for the scheduler's injected
:data:`~qai.platform.scheduling.scheduler_service.TaskExecutor` callback. It
lives here (not in ``qai.platform.scheduling``) because it drives the chat
:class:`~qai.chat.application.use_cases.streaming.StreamChatUseCase`.

Design (why a full agent turn, not a bare LLM call):
    An earlier version ran ONE direct, single-turn LLM stream: it built a
    minimal request from the task's prompt with empty history and accumulated
    the chunk text. That produced pure chit-chat — no tool access, no
    conversation history, no multi-round agentic loop — so a scheduled task
    could only *talk about* doing work, never actually do it.

    Instead we now open a real agent turn on the task's bound conversation via
    :meth:`StreamChatUseCase.collect_completion`. That drains one COMPLETE turn
    (tools, prior history, multi-round follow-up loop) to its terminal ``END``
    and performs every side effect internally — persisting the user prompt and
    the assistant reply, emitting the ``ChatStream*`` events, executing tools,
    and advancing tab state. The runner just supplies the turn's inputs and
    reports ``(ok, text)`` back to the scheduler.

Busy guard:
    A scheduled fire must never interrupt a live user turn on the same tab. If
    the tab is already streaming, the run is SKIPPED (reported as a benign
    success) and left for the next tick — the scheduler keeps ticking, so a
    recurring task retries naturally and a one-shot is not lost mid-conversation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.chat.domain.content import MessageContent
from qai.chat.domain.errors import ConversationLockedError, TabStateError
from qai.chat.domain.ids import ConversationId, TabId
from qai.platform.errors import QaiError
from qai.platform.logging import get_logger
from qai.platform.scheduling.scheduled_task import ScheduledTask
from qai.platform.scheduling.scheduler_service import (
    TaskDeferredError,
    TaskRunResult,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Awaitable, Callable

    from qai.chat.adapters.stream_abort_registry import (
        InMemoryStreamAbortRegistry,
    )
    from qai.chat.application.use_cases.streaming import StreamChatUseCase

__all__ = ["ScheduledTaskRunner"]

_log = get_logger("qai.chat.scheduling")


#: Tools force-disabled for GLOBAL (headless) scheduled tasks. A global task
#: runs in a dedicated system conversation with no user attached, so tools
#: that assume an interactive user in front of the machine must be banned
#: regardless of the per-task whitelist:
#:
#: * ``question`` — blocks the turn on a user answer that will never come;
#:   its wait has no internal timeout, so only the scheduler's 600s ceiling
#:   eventually strong-cancels it (surfaces as the misleading "scheduled task
#:   timed out after 600s"). A bound task keeps ``question`` — it IS attached
#:   to a real user conversation.
#: * ``computer`` — drives the user's desktop (mouse / keyboard / screen).
#:   A background scheduled run stealing the physical UI is user-hostile even
#:   when a user is present, and UAC / system dialogs are opaque to the tool
#:   so it can wedge on approval prompts.
_HEADLESS_BANNED_TOOLS: tuple[str, ...] = ("question", "computer")


class ScheduledTaskRunner:
    """Drives a scheduled task as one native chat-agent turn."""

    __slots__ = (
        "_stream_chat",
        "_abort_registry",
        "_skill_catalog_provider",
        "_global_session_provider",
    )

    def __init__(
        self,
        *,
        stream_chat_use_case: "StreamChatUseCase",
        abort_registry: "InMemoryStreamAbortRegistry",
        skill_catalog_provider: "Callable[[], Awaitable[list[str]]] | None" = None,
        global_session_provider: (
            "Callable[[ScheduledTask], Awaitable[tuple[ConversationId, TabId]]] | None"
        ) = None,
    ) -> None:
        """
        Args:
            stream_chat_use_case: the process-level chat turn use case; its
                :meth:`collect_completion` drains one complete agent turn
                (tools + history + multi-round loop) and performs all side
                effects (persistence, events, tab state) internally.
            abort_registry: the shared streaming registry; ``is_streaming`` is
                consulted to skip a fire while the tab is mid-turn rather than
                interrupt a live user.
            skill_catalog_provider: optional async callable returning the ids
                of the CURRENTLY-ENABLED skills (the Skill panel's on set). Used
                to invert a per-task skill *whitelist* into the turn's skill
                *disable* set. ``None`` (or an empty return) disables skill-
                whitelist enforcement (the run keeps all enabled skills).
            global_session_provider: async callable that, for a GLOBAL task
                (no bound conversation), returns the ``(conversation_id,
                tab_id)`` of a DEDICATED system conversation to run it in
                (idempotent get-or-create keyed off the task, so every run of
                the same global task lands in the same conversation — which
                doubles as its run history). ``None`` disables global tasks
                (a global task then fails with a clear error).
        """
        self._stream_chat = stream_chat_use_case
        self._abort_registry = abort_registry
        self._skill_catalog_provider = skill_catalog_provider
        self._global_session_provider = global_session_provider

    async def run(self, task: ScheduledTask) -> TaskRunResult:
        """Drive ``task``'s prompt as one agent turn; return ``(ok, text)``.

        Flow:
        * Skip (benign success) if the tab is already streaming — never
          interrupt a live user; the next tick retries.
        * Otherwise open a full turn on the task's conversation with the task's
          captured ``model_id`` as the model hint, drain it to ``END`` via
          :meth:`StreamChatUseCase.collect_completion`, and report the joined
          assistant text.

        Any :class:`ConversationLockedError` (a concurrent tab grabbed the lock
        between the busy check and the open) is treated as a skip. Other
        :class:`~qai.platform.errors.QaiError` and unexpected exceptions are
        reported as a failed run so the scheduler records ``state=error`` and
        keeps ticking.
        """
        if task.is_global:
            # Global task: no bound conversation — run in a dedicated system
            # conversation (idempotent get-or-create, stable per task, so every
            # run appends to the same conversation = its run history).
            if self._global_session_provider is None:
                return False, "global scheduled tasks are not available"
            conv_id, tab_id = await self._global_session_provider(task)
        else:
            tab_id = TabId.of(task.tab_id)
            conv_id = ConversationId.of(task.conversation_id)

        if self._abort_registry.is_streaming(tab_id):
            _log.info(
                "scheduling.run_skipped_busy",
                task_id=task.task_id,
                conversation_id=task.conversation_id,
                tab_id=task.tab_id,
            )
            # Do NOT run and do NOT consume this occurrence: signal a defer so
            # the scheduler re-arms the task shortly (it fires soon after the
            # busy turn ends) instead of dropping / silently completing it.
            raise TaskDeferredError("tab is streaming")

        _log.info(
            "scheduling.run_executor_start",
            task_id=task.task_id,
            conversation_id=task.conversation_id,
            tab_id=task.tab_id,
            model_hint=task.model_id,
        )

        # Imported lazily to keep this module free of a hard runtime dependency
        # on the application layer at import time (TYPE_CHECKING only above).
        from qai.chat.application.use_cases.streaming import StreamChatInput

        request = StreamChatInput(
            tab_id=tab_id,
            conversation_id=conv_id,
            user_message=MessageContent(text=task.prompt),
            model_hint=task.model_id,
            extra=await self._build_permission_extra(task),
        )
        try:
            text, _usage = await self._stream_chat.collect_completion(request)
        except (ConversationLockedError, TabStateError) as exc:
            # The tab became busy between the pre-check and the open (a live
            # user, or a same-tab task that raced past the tick's per-tab
            # serialisation). This occurrence did NOT run — defer + re-arm
            # rather than fail or silently complete it.
            raise TaskDeferredError("tab became busy mid-open") from exc
        except QaiError as exc:
            return False, str(exc)
        except Exception as exc:  # noqa: BLE001 — report as a failed run
            _log.warning(
                "scheduling.run_raised", task_id=task.task_id, error=str(exc)
            )
            return False, f"scheduled task run raised: {exc}"

        if not text.strip():
            return False, "scheduled task produced no output"
        return True, text

    async def _build_permission_extra(
        self, task: ScheduledTask
    ) -> dict[str, object] | None:
        """Translate the task's TOOL + SKILL whitelists into a turn ``extra``.

        The chat turn honours session-level *disable* sets on
        ``StreamChatInput.extra``: ``disabled_tools`` (read by
        ``StreamChatUseCase._session_disabled_tools``) and ``disabled_skills``
        (read by the skill-catalog builder). A per-task *whitelist* is the
        complement of a disable set, so:

        * tools — ``disabled_tools = TOOL_ORDER - enabled_tools`` (TOOL_ORDER
          is the single source of truth for advertised tool names);
        * skills — ``disabled_skills = <enabled skill catalog> -
          enabled_skills``, where the catalog is the CURRENTLY-ENABLED skills
          (the Skill panel's on set) obtained from
          ``skill_catalog_provider``. Names in the whitelist that are not in
          the catalog are ignored (a disabled/renamed skill can't be enabled).

        Each whitelist is applied independently; an unset whitelist adds no
        disable set (no restriction for that dimension).

        **Global tasks additionally hard-disable a set of interactive tools**
        (see :data:`_HEADLESS_BANNED_TOOLS`): a global task runs headless in a
        dedicated system conversation with no user attached, so any tool that
        assumes an interactive user — ``question`` (waits for a reply that
        never comes), ``computer`` (drives the user's desktop) — would wedge
        or misbehave. The ban is enforced on the runner side rather than in
        each tool so a session-bound task, whose result folds back into a
        real user conversation, keeps every tool available.
        """
        from qai.chat.application.use_cases.tool_advertise import TOOL_ORDER

        extra: dict[str, object] = {}
        # Compute the tool disable set.
        if task.enabled_tools:
            allowed = set(task.enabled_tools)
            disabled = [name for name in TOOL_ORDER if name not in allowed]
        else:
            disabled = []
        # Global tasks run headless in a dedicated system conversation, so
        # tools that assume an interactive user must be banned regardless of
        # the per-task whitelist. See ``_HEADLESS_BANNED_TOOLS`` for the list
        # + rationale per tool.
        if task.is_global:
            for banned in _HEADLESS_BANNED_TOOLS:
                if banned not in disabled:
                    disabled.append(banned)
        if disabled:
            extra["disabled_tools"] = disabled
        # Skill disable set (unchanged).
        if task.enabled_skills and self._skill_catalog_provider is not None:
            try:
                catalog = await self._skill_catalog_provider()
            except Exception as exc:  # noqa: BLE001 — best-effort; keep tools
                _log.warning(
                    "scheduling.skill_catalog_failed",
                    task_id=task.task_id,
                    error=str(exc),
                )
                catalog = []
            allowed_skills = set(task.enabled_skills)
            disabled_skills = [s for s in catalog if s not in allowed_skills]
            if disabled_skills:
                extra["disabled_skills"] = disabled_skills
        return extra or None
