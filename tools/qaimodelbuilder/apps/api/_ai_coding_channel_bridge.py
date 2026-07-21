# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer cross-context bridge: channels → ai_coding (S9 PR-093 §2.1 C-7 + PR-097 R-10).

Routes channel messages and slash-commands into the
:mod:`qai.ai_coding` use cases (SendUserMessage / DecidePermission /
interrupt).  The :class:`~qai.channels.application.ports.MessageBridgePort`
interface keeps channels free of any direct ``qai.ai_coding`` import
— per the import-linter ``context-isolation`` contract this bridge
is the ONLY place that legitimately sees both contexts.

S9 PR-093 §2.1 C-7 deliverable: restore the
``backend/channels/wechat/cc_handler.py:79-376`` +
``backend/channels/feishu/cc_handler.py:50-667`` semantics by
exposing two concrete operations through the bridge:

1. **deliver()** — accept a parsed channel message + optional
   command, route to the appropriate ai_coding use case, surface
   the assistant reply through a :class:`BridgeReply`.
2. **interrupt()** — channel ``/stop`` command terminates the
   currently-running ai_coding session.

Permission model (no per-request approval in channels)
------------------------------------------------------

v0.5 wechat/feishu CC ran in a ``dontAsk + disallowed_tools``
restricted mode (``wechat/cc_handler.py:21-26``) — there is NO
per-tool-call approval interaction in channels and never was.  So
this bridge intentionally exposes no ``decide_permission`` path:

* channel ``/grant`` is FileGuard *path* pre-authorisation handled
  by :class:`apps.api._channel_grant_bridge.ChannelGrantBridge`
  (security context), unrelated to ai_coding ``PERMISSION_REQUEST``.
* per-request approval (``decide_permission_use_case`` /
  ``/permissions/{id}/decide``) is a WebUI-only flow; channel CC
  sessions do not surface ``PERMISSION_REQUEST`` frames to users.

PR-097 R-10 — streaming variant with tool progress
--------------------------------------------------

:meth:`stream_with_tools` is a streaming sibling of :meth:`deliver`
yielding text deltas *and* tool-progress lines as they arrive.
The dispatch bridge in :mod:`apps.api._channel_dispatch_bridge`
consumes the stream, feeds tool events into a per-message
:class:`~qai.channels.application.tool_progress_aggregator.ToolProgressAggregator`,
and pushes batched icon-prefixed progress lines through the
realtime delivery service.

The bridge does NOT touch :class:`qai.chat.*` — normal chat goes
through :mod:`apps.api._chat_message_bridge`.  The dispatch
selector lives in :mod:`apps.api._channel_dispatch_bridge` and
picks between the two based on whether the user has an active CC
or OC session.

Optional dependency
-------------------
``qai.ai_coding`` is required at runtime by S5 wiring; this module
imports it lazily inside :meth:`deliver` so test environments that
swap in a stub use case don't pay the import cost on bootstrap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from qai.platform.errors import ApplicationError
from qai.platform.logging import get_logger

from qai.channels.application.ports import BridgeReply, MessageBridgePort
from qai.channels.domain import (
    ChannelMessage,
    Command,
    MessageBridgeUnavailableError,
)

from qai.channels.application.use_cases.session_index import (
    BindSessionIndexCommand,
)
from qai.channels.application.use_cases.tool_event_aggregator import (
    coerce_args as _coerce_args_pure,
    normalise_event as _normalise_event_pure,
)

from qai.ai_coding.application.channel_subcommand import (
    format_session_list as _format_session_list_pure,
    format_session_status as _format_session_status_pure,
    format_unknown_subcommand as _format_unknown_subcommand_pure,
    missing_field_text as _missing_field_text_pure,
    needs_id_text as _needs_id_text_pure,
)

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

logger = get_logger(__name__)

__all__ = [
    "AiCodingChannelBridge",
    "AiCodingStreamEvent",
    "AiCodingToChannelNotifier",
]


class AiCodingStreamEvent:
    """Lightweight discriminated event yielded by :meth:`stream_with_tools`.

    Three event kinds (string constants — no Enum to keep the apps
    layer free of new public Enum surface):

    * ``"text"`` — assistant text delta (carry text in :attr:`text`).
    * ``"tool"`` — tool invocation lifecycle (carry :attr:`tool_name`,
      :attr:`tool_args`, :attr:`tool_status` ∈
      ``"running" / "success" / "error"``).
    * ``"done"`` — terminal marker (carries the final reply text in
      :attr:`text`; emitted exactly once per stream).
    """

    __slots__ = (
        "kind",
        "text",
        "tool_name",
        "tool_args",
        "tool_status",
    )

    def __init__(
        self,
        *,
        kind: str,
        text: str = "",
        tool_name: str = "",
        tool_args: dict[str, Any] | None = None,
        tool_status: str = "",
    ) -> None:
        self.kind = kind
        self.text = text
        self.tool_name = tool_name
        self.tool_args = tool_args or {}
        self.tool_status = tool_status


class AiCodingChannelBridge(MessageBridgePort):
    """:class:`MessageBridgePort` impl that fronts ``qai.ai_coding``.

    The bridge inspects the inbound :class:`ChannelMessage` +
    optional :class:`Command`:

    * Bare-text messages with an active CC/OC session → forward to
      ``SendUserMessageUseCase`` and accumulate the streamed reply.
    * ``/cc <subcommand>`` / ``/oc <subcommand>`` commands → dispatch
      to the corresponding ai_coding use case (status / rename /
      delete / list / use / new …).
    * ``/stop`` → invoke ``InterruptSessionUseCase`` against the
      active session.

    Active-session lookup is delegated to the channel
    :class:`~qai.channels.application.ports.SessionIndexRepositoryPort`
    so this bridge stays stateless.

    The bridge depends only on the *public*
    ``container.ai_coding`` surface (real DI field names per §3.1,
    see :mod:`apps.api._ai_coding_di`):

    * ``send_user_message_use_case``
    * ``interrupt_session_use_case``
    * slash-command path binds the real fields:
      ``spawn_coding_session_use_case`` (``new``),
      ``list_coding_sessions_use_case`` (``list``),
      ``set_active_session_use_case`` (``use``),
      ``get_coding_session_use_case`` (``status``),
      ``rename_session_use_case`` (``rename``),
      ``terminate_coding_session_use_case`` (``close``/``delete``),
      ``change_workspace_use_case`` (``cd``),
      ``restore_coding_session_use_case`` (``fork``).

    Missing fields surface as a polite "暂不可用" reply (or
    :class:`MessageBridgeUnavailableError` on the plain-text path) so
    the dispatch bridge can fall back gracefully rather than 500.
    """

    __slots__ = ("_container",)

    def __init__(self, *, container: "Container") -> None:
        self._container = container

    async def deliver(
        self,
        message: ChannelMessage,
        command: Command | None,
        active_session_id: str | None = None,
    ) -> BridgeReply:
        ai_coding = getattr(self._container, "ai_coding", None)
        if ai_coding is None:
            raise MessageBridgeUnavailableError(
                "ai_coding context not wired"
            )

        # Slash-command path
        if command is not None:
            return await self._dispatch_command(
                ai_coding=ai_coding,
                message=message,
                command=command,
                active_session_id=active_session_id,
            )

        # Plain-text path: requires an active CC/OC session resolved
        # via the channel session index (the dispatch bridge guards
        # this; defensive check keeps the bridge robust to direct calls).
        send_uc = getattr(ai_coding, "send_user_message_use_case", None)
        if send_uc is None:
            raise MessageBridgeUnavailableError(
                "ai_coding context missing send_user_message_use_case"
            )

        session_id = self._resolve_active_session(message, active_session_id)
        if not session_id:
            raise MessageBridgeUnavailableError(
                "no active ai_coding session for this user"
            )

        return await self._stream_message(
            send_uc=send_uc,
            session_id=session_id,
            message=message,
        )

    async def interrupt(self, *, session_id: str) -> bool:
        """Invoke the ai_coding interrupt use case on ``session_id``.

        Returns ``True`` when the session was running and was
        successfully signalled to abort.
        """
        ai_coding = getattr(self._container, "ai_coding", None)
        if ai_coding is None:
            return False
        interrupt_uc = getattr(
            ai_coding, "interrupt_session_use_case", None
        )
        if interrupt_uc is None:
            return False
        # 缺陷修复（日志: InterruptSessionUseCase.execute() got an unexpected
        # keyword argument 'session_id'）: the real use case takes a single
        # ``InterruptSessionCommand`` positional and returns an
        # ``InterruptSessionResult`` (NOT a bool).  The previous
        # ``execute(session_id=...)`` kwarg call raised TypeError → ``/stop``
        # never actually interrupted the running CC/OC turn.
        from qai.ai_coding.domain import CodingSessionId
        from qai.ai_coding.application.use_cases.interrupt_session import (
            InterruptSessionCommand,
        )

        try:
            result = await interrupt_uc.execute(
                InterruptSessionCommand(
                    session_id=CodingSessionId(value=session_id)
                )
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.ai_coding_bridge.interrupt_failed",
                session_id=session_id,
                error=str(exc),
            )
            return False
        # ``InterruptSessionResult.interrupted`` is the real "was there a
        # live turn to cancel" flag; degrade gracefully for any stub that
        # returns a bare bool / None.
        interrupted = getattr(result, "interrupted", None)
        if interrupted is None:
            return bool(result) if result is not None else True
        return bool(interrupted)

    async def _abort(self, *, session_id: str) -> bool:
        """Hard-abort the active turn via ``AbortSessionUseCase`` (D-5).

        V0.5 ``wechat/oc_handler.py:291-299`` / ``feishu/oc_handler.py``:
        OpenCode ``/oc stop`` first tries a soft interrupt and, on failure,
        falls through to a HARD abort (``oc_manager.abort_session``).  This
        helper backs that second step on the V2
        :class:`AbortSessionUseCase` (which prefers the OpenCode native
        ``POST /session/{id}/abort`` endpoint over generic terminate).

        Returns ``True`` when the abort use case reported ``aborted``;
        ``False`` when the use case is unwired or the abort failed (so the
        caller surfaces "没有正在执行的任务").  Never raises an
        :class:`ApplicationError` upward — a "session already terminated"
        abort attempt degrades to ``False`` (there was nothing to stop).
        """
        ai_coding = getattr(self._container, "ai_coding", None)
        if ai_coding is None:
            return False
        abort_uc = getattr(ai_coding, "abort_session_use_case", None)
        if abort_uc is None:
            return False
        from qai.ai_coding.domain import CodingSessionId
        from qai.ai_coding.application.use_cases.abort_revert import (
            AbortSessionCommand,
        )

        try:
            result = await abort_uc.execute(
                AbortSessionCommand(
                    session_id=CodingSessionId(value=session_id)
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.ai_coding_bridge.abort_failed",
                session_id=session_id,
                error=str(exc),
            )
            return False
        aborted = getattr(result, "aborted", None)
        if aborted is None:
            return bool(result) if result is not None else True
        return bool(aborted)

    # ------------------------------------------------------------------
    # Internal command + streaming helpers
    # ------------------------------------------------------------------
    async def _dispatch_command(
        self,
        *,
        ai_coding: Any,
        message: ChannelMessage,
        command: Command,
        active_session_id: str | None = None,
    ) -> BridgeReply:
        verb = command.verb.lower()
        args = command.args
        sub = args[0].lower() if args else ""

        # /cc help / /oc help — handled by the dispatch bridge directly
        # before calling us; so for cc/oc verbs the first arg is a
        # subcommand we route here.
        if verb in ("cc", "oc"):
            return await self._cc_oc_subcommand(
                ai_coding=ai_coding,
                message=message,
                verb=verb,
                sub=sub,
                rest=args[1:],
                active_session_id=active_session_id,
            )

        if verb == "stop":
            session_id = self._resolve_active_session(
                message, active_session_id
            )
            if session_id:
                ok = await self.interrupt(session_id=session_id)
                text = (
                    "\u23f9\ufe0f 当前任务已停止，可以发送新消息继续。"
                    if ok
                    else "\u2139\ufe0f 当前没有正在执行的 AI 编程任务。"
                )
                return BridgeReply(
                    reply_text=text, coding_session_id=session_id
                )
            return BridgeReply(
                reply_text="\u2139\ufe0f 当前没有正在执行的 AI 编程任务。",
                coding_session_id=None,
            )

        # Unknown commands fall through to the dispatch bridge — we
        # don't synthesise a reply here so the dispatch bridge can
        # decide between "unknown command" / "route to chat".
        return BridgeReply(reply_text="", coding_session_id=None)

    async def _cc_oc_subcommand(
        self,
        *,
        ai_coding: Any,
        message: ChannelMessage,
        verb: str,
        sub: str,
        rest: tuple[str, ...],
        active_session_id: str | None = None,
    ) -> BridgeReply:
        """Dispatch a ``/cc`` / ``/oc`` subcommand to the real ai_coding
        use case with the real command object.

        Replaces the former ``_SUB_TO_UC`` field-name-string mapping
        (which referenced non-existent DI fields like
        ``create_session_use_case`` and called every use case with an
        impossible ``execute(kind=, user_id=, instance_id=, args=)``
        signature).  Each subcommand now binds to the real DI field on
        ``container.ai_coding`` (see :mod:`apps.api._ai_coding_di`) and
        builds the matching ``*Command`` / ``*Query`` dataclass.

        ``verb`` selects the provider: ``cc`` → ``Provider.CLAUDE_CODE``,
        ``oc`` → ``Provider.OPEN_CODE``.
        """
        # Lazy import so the apps layer pays no import cost on bootstrap
        # and test environments can stub the container surface.
        from qai.ai_coding.domain import (
            CodingSessionId,
            MessageContent,
            Provider,
            Workspace,
        )
        from qai.ai_coding.application.use_cases.spawn_coding_session import (
            SpawnCodingSessionCommand,
        )
        from qai.ai_coding.application.use_cases.list_coding_sessions import (
            ListCodingSessionsQuery,
        )
        from qai.ai_coding.application.use_cases.set_active_session import (
            SetActiveSessionCommand,
        )
        from qai.ai_coding.application.use_cases.get_coding_session import (
            GetCodingSessionQuery,
        )
        from qai.ai_coding.application.use_cases.rename_session import (
            RenameSessionCommand,
        )
        from qai.ai_coding.application.use_cases.terminate_coding_session import (
            TerminateCodingSessionCommand,
        )
        from qai.ai_coding.application.use_cases.change_workspace import (
            ChangeWorkspaceCommand,
        )
        from qai.ai_coding.application.use_cases.restore_coding_session import (
            RestoreCodingSessionCommand,
        )

        provider = (
            Provider.CLAUDE_CODE if verb == "cc" else Provider.OPEN_CODE
        )

        def _missing(field: str) -> BridgeReply:
            return BridgeReply(
                reply_text=_missing_field_text_pure(verb, sub, field),
                coding_session_id=None,
            )

        try:
            if sub == "new":
                uc = getattr(
                    ai_coding, "spawn_coding_session_use_case", None
                )
                if uc is None:
                    return _missing("spawn_coding_session_use_case")
                # V0.5 parity (``wechat/cc_handler.py:113-116`` /
                # ``oc:91-94``): ``/cc new`` REQUIRES an explicit working
                # directory and the directory must already exist.  V2 had
                # silently defaulted to ``"."`` (server CWD) with no
                # existence check — a behaviour退化 that created sessions in
                # the wrong place when the user forgot the path.  Restore the
                # two guards before spawning.
                if not rest:
                    return BridgeReply(
                        reply_text=f"\u2753 用法：/{verb} new <目录路径> [会话名称]",
                        coding_session_id=None,
                    )
                workspace_path = rest[0]
                from pathlib import Path as _Path

                if not _Path(workspace_path).is_dir():
                    return BridgeReply(
                        reply_text=f"\u26a0\ufe0f 目录不存在：{workspace_path}",
                        coding_session_id=None,
                    )
                initial = " ".join(rest[1:]).strip() if len(rest) > 1 else ""
                session = await uc.execute(
                    SpawnCodingSessionCommand(
                        provider=provider,
                        workspace=Workspace(path=workspace_path),
                        initial_prompt=(
                            MessageContent(text=initial) if initial else None
                        ),
                        title=None,
                    )
                )
                sid = str(session.session_id)
                # Bind the channel user → new session so subsequent
                # plain-text messages route to ai_coding (v1 parity).
                await self._bind_owner(message, sid)
                return BridgeReply(
                    reply_text=(
                        f"\u2705 已创建 /{verb} 会话 {sid}\n"
                        f"工作目录：{workspace_path}"
                    ),
                    coding_session_id=sid,
                )

            if sub == "list":
                uc = getattr(
                    ai_coding, "list_coding_sessions_use_case", None
                )
                if uc is None:
                    return _missing("list_coding_sessions_use_case")
                sessions = await uc.execute(
                    ListCodingSessionsQuery(scope="active")
                )
                rows = [
                    s for s in sessions if s.provider is provider
                ]
                # D-2 (V0.5 ``wechat/cc_handler.py:142-145``): mark the
                # current session with ▶ so the user sees which session is
                # active.  The dispatch-resolved active id is threaded in via
                # ``active_session_id``.
                current_sid = self._resolve_active_session(
                    message, active_session_id
                )
                return BridgeReply(
                    reply_text=self._format_session_list(
                        verb, rows, current_sid
                    ),
                    coding_session_id=None,
                )

            if sub == "use":
                if not rest:
                    return self._needs_id(verb, sub)
                uc = getattr(
                    ai_coding, "set_active_session_use_case", None
                )
                if uc is None:
                    return _missing("set_active_session_use_case")
                # 4-M2 — resolve numeric index / ID prefix against the
                # provider's session list (V1 ``wechat/cc_handler.py:151-164``):
                # ``/cc use 1`` → list[0]; ``/cc use <prefix>`` → unique
                # prefix match; otherwise treat as a full id.
                sid_or_err = await self._resolve_session_ref(
                    ai_coding=ai_coding, provider=provider, ref=rest[0]
                )
                if isinstance(sid_or_err, BridgeReply):
                    return sid_or_err
                sid = sid_or_err
                session = await uc.execute(
                    SetActiveSessionCommand(
                        session_id=CodingSessionId(value=sid)
                    )
                )
                await self._bind_owner(message, str(session.session_id))
                return BridgeReply(
                    reply_text=f"\u2705 已切换到会话 {session.session_id}",
                    coding_session_id=str(session.session_id),
                )

            if sub == "status":
                uc = getattr(
                    ai_coding, "get_coding_session_use_case", None
                )
                if uc is None:
                    return _missing("get_coding_session_use_case")
                sid = rest[0] if rest else self._resolve_active_session(message, active_session_id)
                if not sid:
                    return BridgeReply(
                        reply_text="\u2139\ufe0f 当前没有活动的 AI 编程会话。",
                        coding_session_id=None,
                    )
                session = await uc.execute(
                    GetCodingSessionQuery(
                        session_id=CodingSessionId(value=sid)
                    )
                )
                return BridgeReply(
                    reply_text=self._format_session_status(session),
                    coding_session_id=str(session.session_id),
                )

            if sub == "rename":
                # 4-M4 — implicit active session when only a new title is
                # given.  V1 takes the active session id when the user does
                # not type an explicit id (``/cc rename <新标题>``).
                if not rest:
                    return BridgeReply(
                        reply_text=(
                            f"\u2753 用法：/{verb} rename [会话id] <新标题>"
                        ),
                        coding_session_id=None,
                    )
                uc = getattr(ai_coding, "rename_session_use_case", None)
                if uc is None:
                    return _missing("rename_session_use_case")
                if len(rest) >= 2:
                    # First token may be an explicit session id/ref; resolve
                    # it.  If it does not resolve to a session, treat the
                    # whole ``rest`` as the new title for the active session.
                    resolved = await self._resolve_session_ref(
                        ai_coding=ai_coding,
                        provider=provider,
                        ref=rest[0],
                        soft=True,
                    )
                    if isinstance(resolved, str):
                        sid = resolved
                        new_title = " ".join(rest[1:]).strip()
                    else:
                        sid = self._resolve_active_session(message, active_session_id)
                        new_title = " ".join(rest).strip()
                else:
                    sid = self._resolve_active_session(message, active_session_id)
                    new_title = rest[0].strip()
                if not sid:
                    return BridgeReply(
                        reply_text="\u2139\ufe0f 当前没有活动的 AI 编程会话。",
                        coding_session_id=None,
                    )
                session = await uc.execute(
                    RenameSessionCommand(
                        session_id=CodingSessionId(value=sid),
                        new_title=new_title,
                    )
                )
                return BridgeReply(
                    reply_text=f"\u2705 已重命名为：{new_title}",
                    coding_session_id=str(session.session_id),
                )

            if sub in ("close", "delete"):
                # 4-M3 — distinguish close (deactivate, keep history) from
                # delete (destroy).  V1 ``help_text.py:144-150``.  Both
                # accept an implicit active session id when none is given.
                if rest:
                    resolved = await self._resolve_session_ref(
                        ai_coding=ai_coding, provider=provider, ref=rest[0]
                    )
                    if isinstance(resolved, BridgeReply):
                        return resolved
                    sid = resolved
                else:
                    sid = self._resolve_active_session(message, active_session_id)
                    if not sid:
                        return self._needs_id(verb, sub)
                if sub == "close":
                    # close → deactivate: keep the session (history
                    # recoverable via /cc list + /cc use), just clear the
                    # active binding so plain text no longer routes to it.
                    #
                    # V0.5 parity (``wechat/cc_handler.py:169-186`` /
                    # ``feishu/cc_handler.py`` close branch): ``/cc close`` is a
                    # PURE detach — it must NOT interrupt a running task.  The
                    # session (and any in-flight work) is preserved and can be
                    # re-entered with ``/cc use``; only ``/cc delete`` destroys
                    # it.  An earlier V2 revision called
                    # ``interrupt_session_use_case`` here, which diverged from
                    # V0.5 by stopping the running task on a mere mode-exit.
                    # Removed so close is detach-only (the binding clear below
                    # is the only effect).
                    await self._bind_owner(message, "")
                    return BridgeReply(
                        reply_text=(
                            f"\u2705 已关闭会话 {sid}"
                            "（历史保留，可用 /{0} list 重新打开）".format(verb)
                        ),
                        coding_session_id=None,
                    )
                # delete → destroy.
                uc = getattr(
                    ai_coding, "terminate_coding_session_use_case", None
                )
                if uc is None:
                    return _missing("terminate_coding_session_use_case")
                await uc.execute(
                    TerminateCodingSessionCommand(
                        session_id=CodingSessionId(value=sid),
                        reason="user_request",
                    )
                )
                await self._bind_owner(message, "")
                return BridgeReply(
                    reply_text=f"\u2705 已删除会话 {sid}",
                    coding_session_id=None,
                )

            if sub == "cd":
                uc = getattr(ai_coding, "change_workspace_use_case", None)
                if uc is None:
                    return _missing("change_workspace_use_case")
                sid = self._resolve_active_session(message, active_session_id)
                if not sid:
                    return BridgeReply(
                        reply_text="\u2139\ufe0f 当前没有活动的 AI 编程会话。",
                        coding_session_id=None,
                    )
                if not rest:
                    # 回退-5 (V1 ``wechat/cc_handler.py:325-327``): no-arg
                    # ``/cc cd`` shows the current working directory before
                    # the usage hint, rather than only printing usage.
                    cur_dir = await self._current_workspace(ai_coding, sid)
                    if cur_dir:
                        return BridgeReply(
                            reply_text=(
                                f"\U0001f4c1 当前工作目录：{cur_dir}\n\n"
                                f"用法：/{verb} cd <新工作目录>"
                            ),
                            coding_session_id=sid,
                        )
                    return BridgeReply(
                        reply_text=f"\u2753 用法：/{verb} cd <新工作目录>",
                        coding_session_id=sid,
                    )
                new_path = rest[0]
                await uc.execute(
                    ChangeWorkspaceCommand(
                        session_id=CodingSessionId(value=sid),
                        new_workspace=Workspace(path=new_path),
                    )
                )
                return BridgeReply(
                    reply_text=f"\u2705 已切换工作目录：{new_path}",
                    coding_session_id=sid,
                )

            if sub == "fork":
                # 4-M4 — implicit active session when no id given.
                uc = getattr(
                    ai_coding, "restore_coding_session_use_case", None
                )
                if uc is None:
                    return _missing("restore_coding_session_use_case")
                if rest:
                    resolved = await self._resolve_session_ref(
                        ai_coding=ai_coding, provider=provider, ref=rest[0]
                    )
                    if isinstance(resolved, BridgeReply):
                        return resolved
                    sid = resolved
                else:
                    sid = self._resolve_active_session(message, active_session_id)
                    if not sid:
                        return self._needs_id(verb, sub)
                # 任务5 / D-3 (V0.5 ``wechat/cc_handler.py:264-265``): a fork
                # only makes sense once the session has an UPSTREAM provider
                # session to branch from.  V0.5 gates on
                # ``session.claude_session_id`` (set after the first message
                # established the Claude Code connection).  V2 persists the
                # same field on the aggregate (``entities.py:253``).  Reject a
                # fork on a session that has never started a conversation so we
                # do not fork an empty context.  Best-effort read — if the
                # session/use-case is unavailable we skip the guard rather than
                # block the fork (degrade to V2's立即 fork semantics).
                if not await self._session_has_upstream(ai_coding, sid):
                    return BridgeReply(
                        reply_text=(
                            "\u26a0\ufe0f 当前会话尚未开始对话"
                            "（未建立连接），无法 fork"
                        ),
                        coding_session_id=sid,
                    )
                result = await uc.execute(
                    RestoreCodingSessionCommand(
                        session_id=CodingSessionId(value=sid),
                        fork=True,
                    )
                )
                new_sid = str(result.session.session_id)
                await self._bind_owner(message, new_sid)
                return BridgeReply(
                    reply_text=f"\u2705 已 fork 出新会话 {new_sid}",
                    coding_session_id=new_sid,
                )

            if sub == "stop":
                sid = self._resolve_active_session(message, active_session_id)
                if sid:
                    ok = await self.interrupt(session_id=sid)
                    if ok:
                        return BridgeReply(
                            reply_text="\u23f9\ufe0f 当前任务已停止。",
                            coding_session_id=sid,
                        )
                    # D-5 (V0.5 ``wechat/oc_handler.py:291-299``): OpenCode
                    # ``/oc stop`` is a TWO-STEP stop — soft interrupt first,
                    # and if that found no live turn to cancel, fall through to
                    # a HARD abort (``oc_manager.abort_session`` parity, V2
                    # ``AbortSessionUseCase`` → OpenCode native
                    # ``POST /session/{id}/abort``).  CC has no native abort
                    # (``cc_handler.py:368`` is interrupt-only), so the second
                    # step is OC-only.
                    if provider is Provider.OPEN_CODE:
                        aborted = await self._abort(session_id=sid)
                        if aborted:
                            return BridgeReply(
                                reply_text=(
                                    "\u23f9\ufe0f 已中止 OpenCode 任务。"
                                    "可以发送新消息继续对话。"
                                ),
                                coding_session_id=sid,
                            )
                    return BridgeReply(
                        reply_text="\u2139\ufe0f 当前没有正在执行的任务。",
                        coding_session_id=sid,
                    )
                return BridgeReply(
                    reply_text="\u2139\ufe0f 当前没有正在执行的任务。",
                    coding_session_id=None,
                )

            # 4-M5 — /cc models / /cc model channel-side support.
            # V1 ``wechat/cc_handler.py:265-311``: list configured models
            # with the active one marked; select by index or name and
            # persist to the ai_coding config doc.
            if sub == "models":
                return await self._cc_oc_models(
                    ai_coding=ai_coding, verb=verb
                )
            if sub == "model":
                return await self._cc_oc_model(
                    ai_coding=ai_coding, verb=verb, rest=rest
                )
        except ApplicationError as exc:
            return BridgeReply(
                reply_text=f"\u26a0\ufe0f {exc!s}",
                coding_session_id=None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.ai_coding_bridge.subcommand_failed",
                verb=verb,
                sub=sub,
                error=str(exc),
            )
            return BridgeReply(
                reply_text=f"\u26a0\ufe0f /{verb} {sub} 执行失败: {exc!s}",
                coding_session_id=None,
            )

        # Unknown subcommand (incl. ``models`` / ``model`` which V2
        # manages via the WebUI settings, not channel commands).
        return BridgeReply(
            reply_text=_format_unknown_subcommand_pure(verb, sub),
            coding_session_id=None,
        )

    def _needs_id(self, verb: str, sub: str) -> BridgeReply:
        return BridgeReply(
            reply_text=_needs_id_text_pure(verb, sub),
            coding_session_id=None,
        )

    async def _resolve_session_ref(
        self,
        *,
        ai_coding: Any,
        provider: Any,
        ref: str,
        soft: bool = False,
    ) -> "str | BridgeReply":
        """Resolve a ``/cc use`` style session reference to a full id.

        4-M2 — V1 ``wechat/cc_handler.py:151-164``:

        * a pure-digit ``ref`` selects the N-th (1-based) session from the
          provider's active session list;
        * a short ``ref`` is matched as a unique session-id prefix;
        * otherwise it is treated verbatim as a full session id.

        Returns the resolved id string, or a :class:`BridgeReply` carrying
        a user-facing error (out-of-range index / ambiguous prefix).  When
        ``soft`` is ``True`` an unresolved non-digit ref returns the raw
        ``ref`` string instead of an error (used by ``rename`` where the
        first token may actually be the new title rather than an id).
        """
        list_uc = getattr(ai_coding, "list_coding_sessions_use_case", None)
        rows: list[Any] = []
        if list_uc is not None:
            try:
                from qai.ai_coding.application.use_cases.list_coding_sessions import (
                    ListCodingSessionsQuery,
                )

                sessions = await list_uc.execute(
                    ListCodingSessionsQuery(scope="active")
                )
                rows = [s for s in sessions if s.provider is provider]
            except Exception:  # noqa: BLE001 — fall back to verbatim id
                rows = []

        if ref.isdigit():
            idx = int(ref) - 1
            if idx < 0 or idx >= len(rows):
                return BridgeReply(
                    reply_text=(
                        f"\u26a0\ufe0f 序号 {ref} 超出范围"
                        f"（当前共 {len(rows)} 个会话）。发送 /cc list 查看。"
                    ),
                    coding_session_id=None,
                )
            return str(rows[idx].session_id)

        # Prefix match against the active session ids.
        matched = [
            s for s in rows if str(s.session_id).startswith(ref)
        ]
        if len(matched) == 1:
            return str(matched[0].session_id)
        if len(matched) > 1:
            return BridgeReply(
                reply_text=(
                    "\u26a0\ufe0f 匹配到多个会话，请提供更长的 ID 前缀。"
                ),
                coding_session_id=None,
            )
        # No prefix match: verbatim full id (or soft → raw ref).
        return ref

    async def _cc_oc_models(self, *, ai_coding: Any, verb: str) -> BridgeReply:
        """List configured CC/OC models with the active one marked (4-M5)."""
        config = await self._load_coding_config(ai_coding, verb)
        model_list = _get_cc_model_list(config)
        current = config.get("model") or model_list[0]
        lines = [f"\U0001f4cb /{verb} 可用模型："]
        for i, m in enumerate(model_list, start=1):
            marker = "\u2705" if m == current else f"{i}."
            lines.append(f"{marker} {m}")
        if current not in model_list:
            lines.append(f"\n\u2139\ufe0f 当前模型：{current}（不在列表中）")
        lines.append(
            f"\n发送 /{verb} model <序号> 或 /{verb} model <模型名> 切换模型"
        )
        return BridgeReply(
            reply_text="\n".join(lines), coding_session_id=None
        )

    async def _cc_oc_model(
        self, *, ai_coding: Any, verb: str, rest: tuple[str, ...]
    ) -> BridgeReply:
        """Show or switch the current CC/OC model (4-M5)."""
        config = await self._load_coding_config(ai_coding, verb)
        model_list = _get_cc_model_list(config)
        if not rest:
            current = config.get("model") or model_list[0]
            return BridgeReply(
                reply_text=(
                    f"\U0001f5a5\ufe0f /{verb} 当前模型：{current}\n\n"
                    f"发送 /{verb} models 查看可用模型\n"
                    f"发送 /{verb} model <序号|模型名> 切换"
                ),
                coding_session_id=None,
            )
        arg = rest[0].strip()
        if arg.isdigit():
            idx = int(arg) - 1
            if idx < 0 or idx >= len(model_list):
                return BridgeReply(
                    reply_text=(
                        f"\u26a0\ufe0f 序号 {arg} 超出范围"
                        f"（当前共 {len(model_list)} 个模型）。"
                        f"发送 /{verb} models 查看。"
                    ),
                    coding_session_id=None,
                )
            new_model = model_list[idx]
        else:
            new_model = arg
        save_uc = getattr(ai_coding, self._save_config_field(verb), None)
        if save_uc is None:
            return BridgeReply(
                reply_text=f"\u26a0\ufe0f /{verb} model 暂不可用（配置未注册）。",
                coding_session_id=None,
            )
        from qai.ai_coding.application.use_cases.manage_coding_config import (
            SaveCodingConfigCommand,
        )

        await save_uc.execute(
            SaveCodingConfigCommand(updates={"model": new_model})
        )
        return BridgeReply(
            reply_text=f"\u2705 /{verb} 模型已切换为：{new_model}",
            coding_session_id=None,
        )

    @staticmethod
    def _get_config_field(verb: str) -> str:
        return (
            "get_coding_config_use_case"
            if verb == "cc"
            else "get_oc_coding_config_use_case"
        )

    @staticmethod
    def _save_config_field(verb: str) -> str:
        return (
            "save_coding_config_use_case"
            if verb == "cc"
            else "save_oc_coding_config_use_case"
        )

    async def _load_coding_config(
        self, ai_coding: Any, verb: str
    ) -> dict[str, Any]:
        get_uc = getattr(ai_coding, self._get_config_field(verb), None)
        if get_uc is None:
            return {}
        try:
            doc = await get_uc.execute()
        except Exception:  # noqa: BLE001 — degrade to empty config
            return {}
        return doc if isinstance(doc, dict) else {}

    async def _bind_owner(
        self, message: ChannelMessage, coding_session_id: str | None
    ) -> None:
        """Best-effort bind/UNBIND channel user → coding session via the
        session index (v1 ``set_user_cc_session`` parity).

        缺陷修复（日志: ``SessionIndexEntry.coding_session_id must not be
        empty``）: ``/cc close`` / ``/cc delete`` clear the binding by
        passing ``""`` here, but :class:`SessionIndexEntry` rejects an
        EMPTY string (it accepts ``None`` to mean "no active coding
        session").  The empty-string write therefore failed validation,
        the error was swallowed (best-effort), and the binding was NEVER
        cleared — so after ``/cc close`` plain-text messages still routed
        into the (now closed) CC session via ``has_active_coding``, which
        is exactly the "普通会话也不通" symptom (everything kept going to
        CC and hit the stream error).  Coercing ``""`` → ``None`` makes the
        clear path persist a real "no coding session" entry.

        Failures are swallowed — binding is a convenience so plain-text
        follow-ups route to ai_coding; a failure must not abort the
        command's primary effect (the spawn/use/close already succeeded).
        """
        channels = getattr(self._container, "channels", None)
        if channels is None:
            return
        bind_uc = getattr(channels, "bind_session_index_use_case", None)
        if bind_uc is None:
            return
        # Empty string means "clear the binding" — the entity models that
        # as ``None`` (empty string fails its non-empty invariant).
        normalised_session_id = coding_session_id or None
        try:
            await bind_uc.execute(
                BindSessionIndexCommand(
                    instance_id=message.instance_id,
                    channel_user_id=message.sender,
                    internal_user_id=None,
                    coding_session_id=normalised_session_id,
                )
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "channels.ai_coding_bridge.bind_owner_failed",
                coding_session_id=coding_session_id,
                error=str(exc),
            )

    @staticmethod
    def _format_session_list(
        verb: str, sessions: list[Any], active_session_id: str | None = None
    ) -> str:
        return _format_session_list_pure(verb, sessions, active_session_id)

    @staticmethod
    def _format_session_status(session: Any) -> str:
        return _format_session_status_pure(session)

    @staticmethod
    async def _current_workspace(ai_coding: Any, session_id: str) -> str:
        """Best-effort read of a session's current workspace path (回退-5).

        Used by no-arg ``/cc cd`` to echo the current working directory
        (V1 ``wechat/cc_handler.py:325-327``).  Returns ``""`` when the
        session / use case is unavailable so the caller falls back to the
        plain usage hint — never raises.
        """
        get_uc = getattr(ai_coding, "get_coding_session_use_case", None)
        if get_uc is None:
            return ""
        try:
            from qai.ai_coding.domain import CodingSessionId
            from qai.ai_coding.application.use_cases.get_coding_session import (
                GetCodingSessionQuery,
            )

            session = await get_uc.execute(
                GetCodingSessionQuery(
                    session_id=CodingSessionId(value=session_id)
                )
            )
        except Exception:  # noqa: BLE001 — informational echo, never abort
            return ""
        workspace = getattr(session, "workspace", None)
        return str(getattr(workspace, "path", "") or "")

    @staticmethod
    async def _session_has_upstream(ai_coding: Any, session_id: str) -> bool:
        """Whether a session has an upstream provider session to fork (任务5).

        V0.5 ``wechat/cc_handler.py:264-265`` rejects ``/cc fork`` when
        ``session.claude_session_id`` is unset (the provider connection is
        only established after the first message).  V2 persists the same
        ``claude_session_id`` on the aggregate (``entities.py:253``), so we
        read it via :class:`GetCodingSessionQuery` and treat a truthy value
        as "ready to fork".

        Best-effort: when the use case / session is unavailable we return
        ``True`` so the caller does NOT block the fork — the existing V2
        behaviour (immediate fork) is the safe default and we only ADD the
        V0.5 reject when we can positively confirm there is no upstream id.
        """
        get_uc = getattr(ai_coding, "get_coding_session_use_case", None)
        if get_uc is None:
            return True
        try:
            from qai.ai_coding.domain import CodingSessionId
            from qai.ai_coding.application.use_cases.get_coding_session import (
                GetCodingSessionQuery,
            )

            session = await get_uc.execute(
                GetCodingSessionQuery(
                    session_id=CodingSessionId(value=session_id)
                )
            )
        except Exception:  # noqa: BLE001 — never block fork on a read error
            return True
        return bool(getattr(session, "claude_session_id", None))

    async def _send_then_stream(
        self,
        *,
        ai_coding: Any,
        session_id: str,
        text: str,
        image_b64: str | None = None,
        image_mime: str | None = None,
    ) -> AsyncIterator[Any]:
        """Drive V2's two-step send-then-stream coding protocol.

        缺陷X 修复 — the bridge previously called a non-existent
        ``send_uc.execute(session_id=, text=, user_id=)`` "streaming"
        contract; the real DI-bound use cases split send from stream
        (``apps/api/_ai_coding_di.py`` binds both
        ``send_user_message_use_case`` + ``stream_coding_session_use_case``):

        1. ``SendUserMessageUseCase.execute(SendUserMessageCommand)`` —
           appends the user message to the aggregate and forwards it to
           the provider so the live turn picks it up (non-streaming,
           returns a ``SendUserMessageResult``).
        2. ``StreamCodingSessionUseCase.execute(StreamCodingSessionCommand)``
           — async generator yielding :class:`CodingStreamFrame` objects.

        This mirrors V1's ``handle_cc_message`` →
        ``cc_manager.send_message(...)`` flow (``backend/channels/wechat/
        cc_handler.py:379-543``) where the manager both delivers the
        message and yields the streamed frames; V2 splits the same flow
        across two use cases but the user-perceived behaviour (send →
        stream reply + tool progress) is identical.

        M3 (image passthrough) — ``image_b64`` / ``image_mime`` ride the
        ``SendUserMessageCommand`` (the command already carries the
        fields; the provider's ``attach_image`` hook stages them into the
        next multimodal request — ``send_user_message.py:70-71,122-137``).
        Mirrors V1 ``cc_handler.handle_cc_message(image_b64, image_mime)``
        (``backend/channels/wechat/channel.py:1532-1554`` →
        ``cc_handler.py:379-389,543``).

        Returns the streaming async iterator (step 2); the caller
        consumes it and maps each frame via :meth:`_map_coding_frame`.
        """
        from qai.ai_coding.domain import CodingSessionId, MessageContent
        from qai.ai_coding.application.use_cases.send_user_message import (
            SendUserMessageCommand,
        )
        from qai.ai_coding.application.use_cases.stream_coding_session import (
            StreamCodingSessionCommand,
        )

        send_uc = getattr(ai_coding, "send_user_message_use_case", None)
        stream_uc = getattr(ai_coding, "stream_coding_session_use_case", None)
        if send_uc is None or stream_uc is None:
            raise MessageBridgeUnavailableError(
                "ai_coding context missing send_user_message_use_case / "
                "stream_coding_session_use_case"
            )

        sid = CodingSessionId(value=session_id)
        # Step 1 — record + forward the user message (with optional image).
        try:
            await send_uc.execute(
                SendUserMessageCommand(
                    session_id=sid,
                    content=MessageContent(text=text),
                    image_b64=image_b64,
                    image_mime=image_mime,
                )
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MessageBridgeUnavailableError(
                f"ai_coding send_user_message failed: {exc}"
            ) from exc

        # Step 2 — open the streaming iterator over the live turn.
        try:
            return await stream_uc.execute(
                StreamCodingSessionCommand(session_id=sid)
            )
        except ApplicationError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MessageBridgeUnavailableError(
                f"ai_coding stream_coding_session failed: {exc}"
            ) from exc

    async def _stream_message(
        self,
        *,
        send_uc: Any,  # noqa: ARG002 — kept for signature stability
        session_id: str,
        message: ChannelMessage,
        image_b64: str | None = None,
        image_mime: str | None = None,
    ) -> BridgeReply:
        """Run send-then-stream and accumulate streamed text deltas.

        缺陷X — rewritten to the real two-step protocol via
        :meth:`_send_then_stream`; the streamed frames are mapped through
        :meth:`_map_coding_frame` (same canonical events
        :meth:`stream_with_tools` yields).  M3 — forwards an optional
        inbound image to the coding turn.
        """
        ai_coding = getattr(self._container, "ai_coding", None)
        if ai_coding is None:
            raise MessageBridgeUnavailableError("ai_coding context not wired")

        iterator = await self._send_then_stream(
            ai_coding=ai_coding,
            session_id=session_id,
            text=message.content.text,
            image_b64=image_b64,
            image_mime=image_mime,
        )

        text_parts: list[str] = []
        try:
            async for frame in iterator:
                event = self._map_coding_frame(frame)
                if event is None:
                    continue
                if event.kind == "text" and event.text:
                    text_parts.append(event.text)
                elif event.kind == "error":
                    raise MessageBridgeUnavailableError(
                        event.text or "ai_coding error"
                    )
        except MessageBridgeUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MessageBridgeUnavailableError(
                f"ai_coding stream failed: {exc}"
            ) from exc

        reply_text = "".join(text_parts).strip() or "（AI 编程助手未返回内容）"
        return BridgeReply(
            reply_text=reply_text, coding_session_id=session_id
        )

    async def stream_with_tools(
        self,
        message: ChannelMessage,
        session_id: str,
        *,
        image_b64: str | None = None,
        image_mime: str | None = None,
    ) -> AsyncIterator["AiCodingStreamEvent"]:
        """PR-097 R-10 — yield text deltas and tool events as they arrive.

        Used by :mod:`apps.api._channel_dispatch_bridge` so the
        dispatch can feed tool events into
        :class:`~qai.channels.application.tool_progress_aggregator.ToolProgressAggregator`
        and surface batched icon-prefixed progress lines via the
        realtime delivery service (PR-097 R-4).

        缺陷X — rewired to V2's real two-step send-then-stream protocol
        (:meth:`_send_then_stream`): the previous code called a
        non-existent ``send_uc.execute(session_id=, text=, user_id=)``
        streaming contract which raised ``TypeError`` against the real
        :class:`SendUserMessageUseCase` (single ``command`` arg, returns a
        non-streaming ``SendUserMessageResult``).  The real streaming
        entry is :class:`StreamCodingSessionUseCase`.

        M3 — ``image_b64`` / ``image_mime`` are forwarded on the
        ``SendUserMessageCommand`` so CC/OC识图 works through IM channels
        (V1 ``cc_handler.handle_cc_message(image_b64, image_mime)``).

        Yields:
            :class:`AiCodingStreamEvent` items with ``kind`` in
            ``{"text", "tool", "subagent", "turn_warning", "done"}``.
            ``done`` is emitted last with the accumulated final reply text.

        Raises:
            :class:`MessageBridgeUnavailableError` — when ai_coding
            is not wired or the underlying use case fails.
        """
        ai_coding = getattr(self._container, "ai_coding", None)
        if ai_coding is None:
            raise MessageBridgeUnavailableError(
                "ai_coding context not wired"
            )

        iterator = await self._send_then_stream(
            ai_coding=ai_coding,
            session_id=session_id,
            text=message.content.text,
            image_b64=image_b64,
            image_mime=image_mime,
        )

        text_parts: list[str] = []
        try:
            async for frame in iterator:
                # 4-M11 / 4-M12 / M4 — recognise out-of-band lifecycle
                # frames (turn_warning + sub-agent / task_progress) first;
                # these ride dedicated event kinds the dispatch surfaces.
                special = _extract_lifecycle_event(frame)
                if special is not None:
                    yield special
                    continue
                event = self._map_coding_frame(frame)
                if event is None:
                    continue
                if event.kind == "text":
                    if event.text:
                        text_parts.append(event.text)
                        yield event
                elif event.kind == "tool":
                    yield event
                elif event.kind == "error":
                    raise MessageBridgeUnavailableError(
                        event.text or "ai_coding error"
                    )
        except MessageBridgeUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise MessageBridgeUnavailableError(
                f"ai_coding stream failed: {exc}"
            ) from exc

        final_text = (
            "".join(text_parts).strip() or "（AI 编程助手未返回内容）"
        )
        yield AiCodingStreamEvent(kind="done", text=final_text)

    def _map_coding_frame(
        self, frame: Any
    ) -> "AiCodingStreamEvent | None":
        """Map a real :class:`CodingStreamFrame` → :class:`AiCodingStreamEvent`.

        缺陷X root-cause: the previous bridge consumed events via
        :func:`_normalise_event`, which reads ``event.type`` /
        ``event.content`` — a shape that does NOT match the real
        :class:`qai.ai_coding.domain.CodingStreamFrame`
        (``.kind: StreamFrameKind`` + ``.payload: dict``).  Frames
        therefore normalised to an empty kind and were silently dropped.
        This mapper reads the REAL frame contract (verified against
        ``stream_coding_session.py`` + ``providers/claude_code.py``):

        * ``TEXT``        → ``{"text": "..."}`` delta (or a
          ``turn_warning`` payload handled earlier by
          :func:`_extract_lifecycle_event`).
        * ``TOOL_CALL``   → ``{"id", "tool"/"tool_name"/"name", "args"/"input"}``.
        * ``TOOL_RESULT`` → optional ``{"is_error": bool}``.
        * ``ERROR``       → ``{"code", "message", "details": {...}}``.
        * ``END`` / ``PERMISSION_REQUEST`` / task_* → no channel text
          event here (END is terminal; task_* handled by
          :func:`_extract_lifecycle_event`).

        Returns ``None`` for frames that produce no channel-facing event
        (e.g. END, empty TEXT, permission gate).
        """
        kind = getattr(frame, "kind", None)
        kind_value = str(getattr(kind, "value", kind) or "").lower()
        payload = getattr(frame, "payload", None)
        if not isinstance(payload, dict):
            payload = {}

        if kind_value == "text":
            text = payload.get("text")
            if isinstance(text, str) and text:
                return AiCodingStreamEvent(kind="text", text=text)
            return None
        if kind_value == "tool_call":
            tool_name = str(
                payload.get("tool")
                or payload.get("tool_name")
                or payload.get("name")
                or ""
            )
            args = payload.get("args")
            if args is None:
                args = payload.get("input")
            return AiCodingStreamEvent(
                kind="tool",
                tool_name=tool_name,
                tool_args=_coerce_args(args),
                tool_status="running",
            )
        if kind_value == "tool_result":
            is_error = bool(payload.get("is_error", False))
            tool_name = str(
                payload.get("tool")
                or payload.get("tool_name")
                or payload.get("name")
                or ""
            )
            return AiCodingStreamEvent(
                kind="tool",
                tool_name=tool_name,
                tool_args=_coerce_args(
                    payload.get("args") or payload.get("input")
                ),
                tool_status="error" if is_error else "success",
            )
        if kind_value == "error":
            msg = (
                payload.get("message")
                or payload.get("error")
                or "ai_coding error"
            )
            return AiCodingStreamEvent(kind="error", text=str(msg))
        # END / PERMISSION_REQUEST / task_* → no plain channel event here.
        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _resolve_active_session(
        self,
        message: ChannelMessage,  # noqa: ARG002 — kept for call-site symmetry
        active_session_id: str | None = None,
    ) -> str | None:
        """Resolve the user's active CC/OC session for no-arg subcommands.

        Bug fix (用户 2026-06-18 飞书 ``/cc 1`` → ``/cc status`` 误报)：
        this used to be an always-``None`` stub.  Its docstring claimed
        "the dispatch bridge resolves the active session before calling
        us" — true for the plain-text stream path, but the *command*
        path (``/cc status`` / ``rename`` / ``cd`` / ``fork`` / ``stop``
        with no explicit id) read this stub and so ALWAYS saw ``None`` →
        the user got "当前没有活动的 AI 编程会话" immediately after a
        successful ``/cc use``.

        The dispatch bridge (``apps/api/_channel_dispatch_bridge.py``)
        already resolves the active coding session id from the channel
        :class:`SessionIndexRepositoryPort`
        (``find(instance_id, sender)``) BEFORE routing the command, and
        now threads it down as ``active_session_id``.  We simply return
        that resolved value so切换后状态查询能读到当前会话 — mirroring
        V0.5 where ``/cc use`` writes ``user_cc_sessions[open_id]`` and
        ``/cc status`` reads the SAME dict
        (``backend/channels/feishu/cc_handler.py:133`` write,
        ``:159-160`` read).

        Returns ``active_session_id`` (the dispatch-resolved value) or
        ``None`` when the user has no active coding session.
        """
        return active_session_id


# 4-M5 — built-in fallback CC/OC model list (V1
# ``backend/channels/session_commands.py:37-65`` ``_DEFAULT_CC_MODEL_LIST``
# + ``get_cc_model_list``).  Only used when the ai_coding config doc does
# not carry a ``model_list`` field.
_DEFAULT_CC_MODEL_LIST: tuple[str, ...] = (
    "claude-sonnet-4-6",
    "claude-sonnet-4-6:1M",
    "claude-haiku-4-5-20251001",
)


def _get_cc_model_list(config: dict[str, Any]) -> list[str]:
    """Return the configured CC/OC model id list (V1 parity).

    Reads ``config["model_list"]`` which may be a list of plain id strings
    or a list of ``{"id": ..., "label": ..., }`` objects.  Falls back to
    :data:`_DEFAULT_CC_MODEL_LIST` when absent / empty.
    """
    custom = config.get("model_list")
    if isinstance(custom, list) and custom:
        result: list[str] = []
        for m in custom:
            if isinstance(m, str) and m:
                result.append(m)
            elif isinstance(m, dict) and m.get("id"):
                result.append(str(m["id"]))
        if result:
            return result
    return list(_DEFAULT_CC_MODEL_LIST)


def _extract_lifecycle_event(event: Any) -> "AiCodingStreamEvent | None":
    """Recognise turn_warning / sub-agent lifecycle frames (4-M11 / 4-M12).

    The pure :func:`normalise_event` only models delta / tool / error.  A
    couple of ai_coding stream frames carry channel-relevant lifecycle
    information the channel side must render but ``normalise_event`` drops:

    * **turn_warning** (4-M11 / 2-H12) — emitted by
      :class:`StreamCodingSessionUseCase` after a turn END when an over-turn
      threshold tier (20/25/30…) is crossed.  Rides a TEXT-kind frame with a
      ``turn_warning`` payload, or a dict event ``{"type": "turn_warning",
      ...}`` / ``{"turn_warning": {...}}``.  Surfaced as a
      ``kind="turn_warning"`` event so the dispatch pushes it through
      :meth:`AiCodingToChannelNotifier.turn_warning_sync`.
    * **sub-agent** (4-M12 / 2-H11) — V1 ``subagent_start`` /
      ``subagent_done`` / ``subagent_error`` (dict-shaped), and the V2
      equivalents ``TASK_STARTED`` / ``TASK_NOTIFICATION`` frames.  Rendered
      to channel text under ``kind="subagent"``.

    Returns ``None`` for any non-lifecycle event so the caller falls through
    to the generic normalisation.
    """
    payload: dict[str, Any] = {}
    etype = ""
    fkind = ""
    if isinstance(event, dict):
        etype = str(event.get("type", "")).lower()
        payload = {k: v for k, v in event.items() if k != "type"}
    else:
        etype = str(getattr(event, "type", "")).lower()
        kind_obj = getattr(event, "kind", None)
        fkind = str(getattr(kind_obj, "value", kind_obj) or "").lower()
        raw_payload = getattr(event, "payload", None)
        if isinstance(raw_payload, dict):
            payload = raw_payload

    # ── turn_warning ──────────────────────────────────────────────────
    tw = payload.get("turn_warning")
    if etype == "turn_warning" or isinstance(tw, dict):
        info = tw if isinstance(tw, dict) else payload
        message = str(info.get("message", "")).strip()
        if not message:
            tc = info.get("turn_count")
            message = (
                f"⚠️ 当前会话已达到 {tc} 轮对话，建议尽快创建新会话。"
                if tc
                else "⚠️ 当前会话轮次较多，建议尽快创建新会话。"
            )
        return AiCodingStreamEvent(kind="turn_warning", text=message)

    # ── sub-agent lifecycle ───────────────────────────────────────────
    if etype in ("subagent_start",) or fkind in ("task_started",):
        desc = str(
            payload.get("description")
            or payload.get("name")
            or payload.get("task")
            or ""
        ).strip()
        text = (
            f"\U0001f916 子任务已启动：{desc}" if desc else "\U0001f916 子任务已启动"
        )
        return AiCodingStreamEvent(kind="subagent", text=text)
    if etype in ("subagent_done",) or fkind in ("task_notification",):
        desc = str(
            payload.get("description") or payload.get("name") or ""
        ).strip()
        text = (
            f"\u2705 子任务完成：{desc}" if desc else "\u2705 子任务完成"
        )
        return AiCodingStreamEvent(kind="subagent", text=text)
    if etype in ("subagent_error",):
        err = str(
            payload.get("message") or payload.get("error") or ""
        ).strip()
        text = (
            f"\u26a0\ufe0f 子任务出错：{err}" if err else "\u26a0\ufe0f 子任务出错"
        )
        return AiCodingStreamEvent(kind="subagent", text=text)

    # ── sub-task progress (M4) ────────────────────────────────────────
    # V0.5 ``wechat/cc_handler.py:625-656`` / ``feishu/cc_handler.py:441-471``
    # pushed a sub-task progress line (tokens / tool count / elapsed /
    # last tool) as the task ran.  ai_coding emits a ``TASK_PROGRESS`` frame
    # (``claude_code.py:957-963``, payload ``{task_id, description,
    # usage:{total_tokens, tool_uses, duration_ms}, last_tool_name}``) that
    # was previously dropped here.  Surface it as a channel text line via the
    # ``subagent`` render path (``_stream_ai_coding`` delivers it inline),
    # matching V0.5's ``⏳ 子任务进行中`` line.
    if etype in ("task_progress", "subagent_progress") or fkind in (
        "task_progress",
    ):
        task_id = str(payload.get("task_id") or "")
        task_short = task_id[:8] if task_id else "?"
        usage = payload.get("usage")
        usage = usage if isinstance(usage, dict) else {}
        last_tool = str(payload.get("last_tool_name") or "").strip()
        tokens = usage.get("total_tokens", 0)
        tool_uses = usage.get("tool_uses", 0)
        dur_ms = usage.get("duration_ms", 0)
        dur_s = dur_ms / 1000 if dur_ms else 0
        meta_parts: list[str] = []
        if last_tool:
            meta_parts.append(f"\U0001f527 {last_tool}")
        if tokens:
            meta_parts.append(f"{tokens:,} tokens")
        if tool_uses:
            meta_parts.append(f"{tool_uses} 工具")
        if dur_s:
            meta_parts.append(f"{dur_s:.1f}s")
        progress_line = f"\u23f3 子任务进行中 [{task_short}]"
        if meta_parts:
            progress_line += "\n" + " \u00b7 ".join(meta_parts)
        return AiCodingStreamEvent(kind="subagent", text=progress_line)

    return None


def _normalise_event(event: Any) -> tuple[str, dict[str, Any]]:
    """Normalise an ai_coding stream event into ``(kind, payload)``.

    A-1 step1: the pure implementation moved to
    :func:`qai.channels.application.use_cases.tool_event_aggregator.normalise_event`.
    Kept as a module-private alias so internal callers (and any test that
    imports the private name) continue to resolve.
    """
    return _normalise_event_pure(event)


def _coerce_args(value: Any) -> dict[str, Any]:
    """Coerce a tool-args payload into a plain dict for the formatter.

    A-1 step1: the pure implementation moved to
    :func:`qai.channels.application.use_cases.tool_event_aggregator.coerce_args`.
    Kept as a module-private alias for internal callers + test imports.
    """
    return _coerce_args_pure(value)


# ---------------------------------------------------------------------------
# Reverse bridge re-export (A-1 step2)
# ---------------------------------------------------------------------------
# :class:`AiCodingToChannelNotifier` moved to ``_ai_coding_notify_bridge`` to
# keep this module focused on the forward (channels → ai_coding) direction.
# Re-exported here so ``__all__`` and every existing importer of
# ``apps.api._ai_coding_channel_bridge.AiCodingToChannelNotifier`` keep working.
from ._ai_coding_notify_bridge import AiCodingToChannelNotifier  # noqa: E402
