from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from qai.agent_sdk import AgentRunRequest, ModelRequiredError, QaiAgentClient
from qai.chat.domain.ids import ConversationId, TabId
from qai.chat.domain.stream_frame import StreamFrame


class _CreateConversation:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return type("Conversation", (), {"id": ConversationId.of("conv-1")})()


class _OpenTab:
    async def execute(self, request):
        return type("Tab", (), {"id": TabId.of("tab-1")})()


class _SetConversationWorkspace:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)
        return type("Conversation", (), {"id": request.conversation_id})()


class _StreamChat:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)

        async def _iter():
            yield StreamFrame.chunk(frame_id="frame-1", sequence=0, text="hello ")
            yield StreamFrame.chunk(frame_id="frame-2", sequence=1, text="world")
            yield StreamFrame.end(
                frame_id="frame-3",
                sequence=2,
                reason="completed",
                usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
            )

        return _iter()


class _ErrorStreamChat:
    def __init__(self) -> None:
        self.requests = []

    async def execute(self, request):
        self.requests.append(request)

        async def _iter():
            yield StreamFrame.error(
                frame_id="frame-err",
                sequence=0,
                code="provider_error",
                message="provider failed",
            )

        return _iter()


class _ProviderList:
    async def execute(self):
        return [{"id": "provider"}]


class _McpRegistry:
    def __init__(self) -> None:
        self.connect_all_calls = 0

    async def connect_all(self):
        self.connect_all_calls += 1


def _container(
    *,
    stream=None,
    with_set_workspace: bool = False,
    with_mcp: bool = False,
):
    create = _CreateConversation()
    stream = stream or _StreamChat()
    attrs = {
        "create_conversation_use_case": create,
        "open_tab_use_case": _OpenTab(),
        "stream_chat_use_case": stream,
    }
    set_workspace = None
    if with_set_workspace:
        set_workspace = _SetConversationWorkspace()
        attrs["set_conversation_workspace_use_case"] = set_workspace
    mcp_registry = None
    if with_mcp:
        mcp_registry = _McpRegistry()
        attrs["mcp_server_registry"] = mcp_registry
    chat = type(
        "Chat",
        (),
        attrs,
    )()
    model_catalog = type(
        "ModelCatalog",
        (),
        {"list_provider_configs_use_case": _ProviderList()},
    )()
    return type(
        "Container",
        (),
        {
            "chat": chat,
            "model_catalog": model_catalog,
            "_create": create,
            "_stream": stream,
            "_set_workspace": set_workspace,
            "_mcp_registry": mcp_registry,
        },
    )()


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_client_runs_one_turn_and_collects_frames() -> None:
    container = _container()
    async with QaiAgentClient(container=container) as client:
        result = await client.run(
            AgentRunRequest(
                prompt="Say hello",
                model="cloud-model",
                extra={"system_prompt": "Be concise."},
            )
        )

    assert result.final_text == "hello world"
    assert result.conversation_id == "conv-1"
    assert result.tab_id == "tab-1"
    assert result.run_id
    assert result.status == "completed"
    assert result.usage == {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
    }
    assert [frame.frame_type for frame in result.frames] == ["chunk", "chunk", "end"]
    assert container._stream.requests[0].model_hint == "cloud-model"
    assert container._stream.requests[0].extra == {
        "agent_only": True,
        "system_prompt": "Be concise.",
    }


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_client_reports_error_frame_as_failed_result() -> None:
    container = _container(stream=_ErrorStreamChat())
    async with QaiAgentClient(container=container) as client:
        result = await client.run(AgentRunRequest(prompt="Fail", model="m"))

    assert result.status == "failed"
    assert result.final_text == ""
    assert result.error is not None
    assert result.error["code"] == "provider_error"
    assert result.error["message"] == "provider failed"
    assert result.to_dict(include_frames=False)["error"]["code"] == "provider_error"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_client_sets_workspace_on_new_conversation(tmp_path) -> None:
    container = _container()
    with patch(
        "apps.api._workspace_grant_bridge.ensure_workspace_session_grant",
        new=AsyncMock(return_value=True),
    ) as grant_workspace:
        async with QaiAgentClient(container=container) as client:
            await client.run(AgentRunRequest(prompt="Use files", model="m", workspace=tmp_path))

    workspace = str(tmp_path.resolve())
    assert container._create.requests[0].meta == {"workspace": workspace}
    assert container._stream.requests[0].extra["_session_workspace_root"] == workspace
    grant_workspace.assert_awaited_once_with(
        container,
        conversation_id="conv-1",
        workspace=workspace,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_client_updates_workspace_on_existing_conversation(tmp_path) -> None:
    container = _container(with_set_workspace=True)
    with patch(
        "apps.api._workspace_grant_bridge.ensure_workspace_session_grant",
        new=AsyncMock(return_value=True),
    ) as grant_workspace:
        async with QaiAgentClient(container=container) as client:
            await client.run(
                AgentRunRequest(
                    prompt="Continue",
                    model="m",
                    conversation_id="existing-conv",
                    workspace=tmp_path,
                )
            )

    workspace_request = container._set_workspace.requests[0]
    assert workspace_request.conversation_id == ConversationId.of("existing-conv")
    assert workspace_request.workspace == str(tmp_path.resolve())
    assert container._stream.requests[0].extra["_session_workspace_root"] == str(
        tmp_path.resolve()
    )
    grant_workspace.assert_awaited_once_with(
        container,
        conversation_id="existing-conv",
        workspace=str(tmp_path.resolve()),
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_client_connects_mcp_when_requested() -> None:
    container = _container(with_mcp=True)
    async with QaiAgentClient(container=container) as client:
        await client.run(
            AgentRunRequest(prompt="Use configured tools", model="m", connect_mcp=True)
        )

    assert container._mcp_registry.connect_all_calls == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_client_agent_only_strips_model_builder_tool_mode() -> None:
    container = _container()
    async with QaiAgentClient(container=container) as client:
        await client.run(
            AgentRunRequest(
                prompt="General agent request",
                model="m",
                extra={"tool_mode": "model-build", "temperature": 0.1},
            )
        )

    assert container._stream.requests[0].extra == {
        "agent_only": True,
        "temperature": 0.1,
    }


@pytest.mark.unit
def test_agent_run_request_rejects_empty_prompt() -> None:
    with pytest.raises(ValueError, match="prompt"):
        AgentRunRequest(prompt="  ", model="m")


@pytest.mark.unit
@pytest.mark.parametrize("model", [None, "", "   "])
def test_agent_run_request_requires_model(model) -> None:
    with pytest.raises(ModelRequiredError, match="no model specified"):
        AgentRunRequest(prompt="hi", model=model)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_agent_client_records_client_turn_id_in_message_meta() -> None:
    container = _container()
    async with QaiAgentClient(container=container) as client:
        await client.run(
            AgentRunRequest(prompt="hi", model="m", client_turn_id="turn-7")
        )

    assert container._stream.requests[0].user_message_meta == {"client_turn_id": "turn-7"}
