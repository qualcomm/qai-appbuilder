# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-process SDK facade for the QAI AppBuilder chat agent."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qai.chat.domain.content import MessageContent
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrame, StreamFrameType

__all__ = [
    "AgentFrame",
    "AgentRunRequest",
    "AgentRunResult",
    "QaiAgentClient",
    "QaiAgentError",
    "run_agent",
]


FrameCallback = Callable[["AgentFrame"], None | Awaitable[None]]


class ModelRequiredError(ValueError):
    """Raised when no model id was supplied.

    An agent turn never falls back to the configured default model: a
    scripted caller must always say which model it wants, otherwise a
    machine without that default's credentials fails far from the cause.
    """

    def __init__(self) -> None:
        super().__init__("no model specified: a model id is required (pass --model <model-id>)")


class QaiAgentError(RuntimeError):
    """Base error raised by the QAI AppBuilder agent SDK."""


@dataclass(frozen=True, slots=True, kw_only=True)
class AgentRunRequest:
    """One user turn to execute through the QAI AppBuilder chat agent."""

    prompt: str
    model: str
    conversation_id: str | None = None
    title: str = "QAI Agent"
    workspace: str | Path | None = None
    connect_mcp: bool = False
    agent_only: bool = True
    extra: Mapping[str, Any] | None = None
    client_turn_id: str | None = None
    user_message_meta: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ModelRequiredError()
        if self.conversation_id is not None and not str(self.conversation_id).strip():
            raise ValueError("conversation_id must be non-empty when supplied")
        if self.workspace is not None and not str(self.workspace).strip():
            raise ValueError("workspace must be non-empty when supplied")
        if self.client_turn_id is not None and not str(self.client_turn_id).strip():
            raise ValueError("client_turn_id must be non-empty when supplied")


@dataclass(frozen=True, slots=True)
class AgentFrame:
    """Stable SDK projection of a chat stream frame."""

    frame_id: str
    frame_type: str
    sequence: int
    payload: dict[str, Any]
    run_id: str | None = None
    run_sequence: int | None = None

    @classmethod
    def from_stream_frame(cls, frame: StreamFrame) -> "AgentFrame":
        frame_type = (
            frame.frame_type.value
            if hasattr(frame.frame_type, "value")
            else str(frame.frame_type)
        )
        return cls(
            frame_id=frame.frame_id,
            frame_type=frame_type,
            sequence=frame.sequence,
            payload=dict(frame.payload),
            run_id=getattr(frame, "run_id", None),
            run_sequence=getattr(frame, "run_sequence", None),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "frame_type": self.frame_type,
            "sequence": self.sequence,
            "payload": dict(self.payload),
            "run_id": self.run_id,
            "run_sequence": self.run_sequence,
        }


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """Buffered result for one completed agent turn."""

    conversation_id: str
    tab_id: str
    run_id: str
    final_text: str
    status: str
    error: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    frames: tuple[AgentFrame, ...] = field(default_factory=tuple)

    def to_dict(self, *, include_frames: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "conversation_id": self.conversation_id,
            "tab_id": self.tab_id,
            "run_id": self.run_id,
            "status": self.status,
            "final_text": self.final_text,
            "error": dict(self.error) if self.error is not None else None,
            "usage": self.usage,
        }
        if include_frames:
            payload["frames"] = [frame.to_dict() for frame in self.frames]
        return payload


@dataclass(frozen=True, slots=True)
class _PreparedRun:
    request: Any
    conversation_id: str
    tab_id: str
    run_id: str


class QaiAgentClient:
    """Async client for running QAI AppBuilder agent turns in process."""

    def __init__(
        self,
        *,
        repo_root: str | Path | None = None,
        config_file: str | Path | None = None,
        container: Any | None = None,
    ) -> None:
        self._repo_root = Path(repo_root) if repo_root is not None else None
        self._config_file = Path(config_file) if config_file is not None else None
        self._external_container = container
        self._container: Any | None = None
        self._container_cm: Any | None = None

    async def __aenter__(self) -> "QaiAgentClient":
        if self._external_container is not None:
            self._container = self._external_container
            return self

        from apps.cli._runtime import cli_container

        self._container_cm = cli_container(
            config_file=self._config_file,
            repo_root=self._repo_root,
        )
        self._container = await self._container_cm.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        if self._container_cm is not None:
            await self._container_cm.__aexit__(exc_type, exc, tb)
        self._container_cm = None
        self._container = None

    @property
    def container(self) -> Any:
        if self._container is None:
            raise QaiAgentError("QaiAgentClient must be used as an async context manager")
        return self._container

    async def has_configured_provider(self) -> bool:
        """Return whether the model catalog currently has any provider config."""

        try:
            rows = await self.container.model_catalog.list_provider_configs_use_case.execute()
        except Exception:  # noqa: BLE001 - availability probe; caller decides policy
            return False
        return bool(rows)

    async def run(
        self,
        request: AgentRunRequest | str,
        *,
        model: str | None = None,
        conversation_id: str | None = None,
        title: str = "QAI Agent",
        workspace: str | Path | None = None,
        connect_mcp: bool = False,
        agent_only: bool = True,
        extra: Mapping[str, Any] | None = None,
        on_frame: FrameCallback | None = None,
        include_frames: bool = True,
    ) -> AgentRunResult:
        """Run one agent turn and return the buffered final answer."""

        agent_request = _coerce_request(
            request,
            model=model,
            conversation_id=conversation_id,
            title=title,
            workspace=workspace,
            connect_mcp=connect_mcp,
            agent_only=agent_only,
            extra=extra,
        )
        prepared = await self._prepare(agent_request)
        frames: list[AgentFrame] = []
        parts: list[str] = []
        usage: dict[str, Any] | None = None
        error: dict[str, Any] | None = None
        status = "interrupted"
        saw_error = False

        async for frame in self._stream_prepared(prepared):
            if include_frames:
                frames.append(frame)
            if frame.frame_type == StreamFrameType.CHUNK.value:
                text = frame.payload.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif frame.frame_type == StreamFrameType.END.value:
                if not saw_error:
                    status = "completed"
                maybe_usage = frame.payload.get("usage")
                if isinstance(maybe_usage, dict):
                    usage = dict(maybe_usage)
            elif frame.frame_type == StreamFrameType.ERROR.value:
                status = "failed"
                saw_error = True
                if error is None:
                    error = dict(frame.payload)
            if on_frame is not None:
                await _call_maybe_async(on_frame, frame)

        return AgentRunResult(
            conversation_id=prepared.conversation_id,
            tab_id=prepared.tab_id,
            run_id=prepared.run_id,
            final_text="".join(parts),
            status=status,
            error=error,
            usage=usage,
            frames=tuple(frames),
        )

    async def stream(
        self,
        request: AgentRunRequest | str,
        *,
        model: str | None = None,
        conversation_id: str | None = None,
        title: str = "QAI Agent",
        workspace: str | Path | None = None,
        connect_mcp: bool = False,
        agent_only: bool = True,
        extra: Mapping[str, Any] | None = None,
    ) -> AsyncIterator[AgentFrame]:
        """Yield structured frames for one agent turn."""

        agent_request = _coerce_request(
            request,
            model=model,
            conversation_id=conversation_id,
            title=title,
            workspace=workspace,
            connect_mcp=connect_mcp,
            agent_only=agent_only,
            extra=extra,
        )
        prepared = await self._prepare(agent_request)
        async for frame in self._stream_prepared(prepared):
            yield frame

    async def _prepare(self, request: AgentRunRequest) -> _PreparedRun:
        from qai.chat.application.use_cases.conversation_management import (
            CreateConversationInput,
            SetConversationWorkspaceInput,
        )
        from qai.chat.application.use_cases.streaming import StreamChatInput
        from qai.chat.application.use_cases.tab_management import OpenTabInput

        c = self.container
        workspace = _normalize_workspace(request.workspace)
        extra = dict(request.extra or {})
        if (
            request.agent_only
            and _is_model_builder_tool_mode(extra.get("tool_mode"))
        ):
            extra.pop("tool_mode", None)
        extra["agent_only"] = request.agent_only
        if workspace is not None:
            extra["_session_workspace_root"] = workspace
        if request.connect_mcp:
            await self._connect_mcp_servers()

        if request.conversation_id is None:
            meta = {"workspace": workspace} if workspace is not None else None
            conversation = await c.chat.create_conversation_use_case.execute(
                CreateConversationInput(title=request.title, meta=meta)
            )
            conversation_id = conversation.id
        else:
            conversation_id = ConversationId.of(request.conversation_id)
            set_workspace = getattr(c.chat, "set_conversation_workspace_use_case", None)
            if workspace is not None and set_workspace is not None:
                await set_workspace.execute(
                    SetConversationWorkspaceInput(
                        conversation_id=conversation_id,
                        workspace=workspace,
                    )
                )

        if workspace is not None:
            # The HTTP workspace route creates the matching conversation-
            # scoped path grant through this apps-layer bridge. SDK/CLI runs
            # bypass that route, so mirror the same grant here; otherwise a
            # non-interactive read waits forever for a UI approval dialog.
            from apps.api._workspace_grant_bridge import (
                ensure_workspace_session_grant,
            )

            await ensure_workspace_session_grant(
                c,
                conversation_id=conversation_id.value,
                workspace=workspace,
            )

        tab = await c.chat.open_tab_use_case.execute(
            OpenTabInput(conversation_id=conversation_id.value)
        )
        tab_id = tab.id if not isinstance(tab.id, str) else TabId.of(tab.id)
        # ``run_id`` is generated locally. This build has no persisted
        # chat-run ledger, so ``client_turn_id`` is a caller-supplied tag only
        # (recorded in the user message meta); it is not an idempotency key.
        run_id = str(uuid.uuid4())

        user_meta = dict(request.user_message_meta or {})
        if request.client_turn_id:
            user_meta.setdefault("client_turn_id", request.client_turn_id)
        stream_request = StreamChatInput(
            tab_id=tab_id,
            conversation_id=conversation_id,
            user_message=MessageContent(text=request.prompt),
            model_hint=request.model,
            extra=extra,
            user_message_meta=user_meta or None,
        )
        return _PreparedRun(
            request=stream_request,
            conversation_id=conversation_id.value,
            tab_id=tab_id.value,
            run_id=run_id,
        )

    async def _stream_prepared(
        self,
        prepared: _PreparedRun,
    ) -> AsyncIterator[AgentFrame]:
        iterator = await self.container.chat.stream_chat_use_case.execute(prepared.request)
        try:
            async for frame in iterator:
                yield AgentFrame.from_stream_frame(frame)
        finally:
            await _close_async_iterator(iterator)

    async def _connect_mcp_servers(self) -> None:
        registry = getattr(getattr(self.container, "chat", None), "mcp_server_registry", None)
        connect_all = getattr(registry, "connect_all", None)
        if not callable(connect_all):
            return
        result = connect_all()
        if hasattr(result, "__await__"):
            await result


async def run_agent(
    prompt: str,
    *,
    model: str | None = None,
    conversation_id: str | None = None,
    title: str = "QAI Agent",
    workspace: str | Path | None = None,
    connect_mcp: bool = False,
    agent_only: bool = True,
    extra: Mapping[str, Any] | None = None,
    repo_root: str | Path | None = None,
    config_file: str | Path | None = None,
    on_frame: FrameCallback | None = None,
    include_frames: bool = True,
) -> AgentRunResult:
    """Convenience helper for one-shot SDK callers."""

    async with QaiAgentClient(repo_root=repo_root, config_file=config_file) as client:
        return await client.run(
            AgentRunRequest(
                prompt=prompt,
                model=model,
                conversation_id=conversation_id,
                title=title,
                workspace=workspace,
                connect_mcp=connect_mcp,
                agent_only=agent_only,
                extra=extra,
            ),
            on_frame=on_frame,
            include_frames=include_frames,
        )


def _coerce_request(
    request: AgentRunRequest | str,
    *,
    model: str | None,
    conversation_id: str | None,
    title: str,
    workspace: str | Path | None,
    connect_mcp: bool,
    agent_only: bool,
    extra: Mapping[str, Any] | None,
) -> AgentRunRequest:
    if isinstance(request, AgentRunRequest):
        return request
    return AgentRunRequest(
        prompt=request,
        model=model,
        conversation_id=conversation_id,
        title=title,
        workspace=workspace,
        connect_mcp=connect_mcp,
        agent_only=agent_only,
        extra=extra,
    )


def _normalize_workspace(workspace: str | Path | None) -> str | None:
    if workspace is None:
        return None
    path = Path(workspace).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"workspace directory does not exist: {path}")
    return str(path)


def _is_model_builder_tool_mode(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    return value.strip().lower() in {
        "model-build",
        "model_build",
        "model_builder",
    }


async def _call_maybe_async(callback: FrameCallback, frame: AgentFrame) -> None:
    result = callback(frame)
    if hasattr(result, "__await__"):
        await result


async def _close_async_iterator(iterator: AsyncIterator[Any]) -> None:
    aclose = getattr(iterator, "aclose", None)
    if callable(aclose):
        with contextlib.suppress(Exception):
            await aclose()
