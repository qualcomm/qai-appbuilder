"""Unit tests for ``apps.cli._chat_build_tool_bridge`` (Step 7 — ``qai_build`` tool).

Covers: (a) the schema is present/correctly shaped; (b) the handler drives
the expected ``stream_chat_use_case.execute`` call with the expected
``extra`` shape (reusing ``commands/build.py``'s ``build_extra``); (c) a
long summarised result still hits :class:`~apps.cli._render.StreamFrameRenderer`'s
fold registry through the same ``tool_result``/``/show`` mechanism; (d) the
zero-provider precheck error path never drives a turn.
"""

from __future__ import annotations

import io
from types import SimpleNamespace

from apps.cli import _chat_build_tool_bridge as bridge_mod
from apps.cli._render import RenderOptions, StreamFrameRenderer
from apps.cli.commands.build import BuildSession
from apps.cli.commands.build import build_extra as build_module_build_extra
from qai.chat.adapters.tool_invocation import TOOL_ORDER, RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest
from qai.chat.domain.ids import ConversationId, TabId


class _Frame:
    __slots__ = ("frame_type", "payload")

    def __init__(self, frame_type: str, payload: dict) -> None:
        self.frame_type = frame_type
        self.payload = payload


def _request(arguments: dict | None = None) -> ToolInvocationRequest:
    return ToolInvocationRequest(
        tab_id=TabId.of("tab-1"),
        conversation_id=ConversationId.of("conv-1"),
        tool_name="qai_build",
        arguments=arguments or {},
    )


class _AsyncResult:
    def __init__(self, value) -> None:
        self._value = value
        self.calls: list = []

    async def execute(self, *args, **kwargs):
        self.calls.append(args or kwargs)
        return self._value


class _StreamChatStub:
    def __init__(self, frames: list) -> None:
        self._frames = frames
        self.calls: list = []

    async def execute(self, request):
        self.calls.append(request)

        async def _gen():
            for frame in self._frames:
                yield frame

        return _gen()


def _container(*, provider_configured: bool = True, frames: list | None = None):
    stream_stub = _StreamChatStub(frames or [])
    container = SimpleNamespace(
        chat=SimpleNamespace(
            tools=RegistryBackedToolInvocation(),
            create_conversation_use_case=_AsyncResult(
                SimpleNamespace(id=SimpleNamespace(value="conv-nested"))
            ),
            open_tab_use_case=_AsyncResult(SimpleNamespace(id="tab-nested")),
            stream_chat_use_case=stream_stub,
        ),
        model_catalog=SimpleNamespace(
            list_provider_configs_use_case=_AsyncResult(
                [{"provider_id": "local-genie"}] if provider_configured else []
            )
        ),
    )
    return container, stream_stub


# ---------------------------------------------------------------------------
# (a) schema
# ---------------------------------------------------------------------------


def test_schema_is_openai_shaped_and_named_correctly():
    schema = bridge_mod.QAI_BUILD_SCHEMA
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "qai_build"
    assert isinstance(fn["description"], str) and fn["description"]
    params = fn["parameters"]
    assert params["type"] == "object"
    for key in ("model_paths", "precision", "dataset", "instruction"):
        assert key in params["properties"]
    assert params["required"] == ["model_paths", "instruction"]


def test_tool_order_includes_qai_build_after_read_only_cli_tools():
    assert "qai_build" in TOOL_ORDER
    assert TOOL_ORDER.index("qai_build") > TOOL_ORDER.index("qai_run_list")


def test_register_cli_build_tool_registers_with_schema():
    container, _ = _container()
    registered = bridge_mod.register_cli_build_tool(container)
    assert registered == "qai_build"
    tools = container.chat.tools
    assert "qai_build" in tools.registered_tools
    schema_names = {s["function"]["name"] for s in tools.schemas()}
    assert "qai_build" in schema_names


# ---------------------------------------------------------------------------
# (b) handler drives the expected stream_chat_use_case.execute call
# ---------------------------------------------------------------------------


async def test_handler_drives_stream_chat_with_model_build_extra():
    frames = [
        _Frame("chunk", {"text": "已完成转换。"}),
        _Frame(
            "tool_call",
            {"tool_name": "convert_model", "arguments": {"path": "a.onnx"}},
        ),
        _Frame("tool_result", {"result": "ok"}),
        _Frame("end", {}),
    ]
    container, stream_stub = _container(frames=frames)
    bridge_mod.register_cli_build_tool(container)

    result = await container.chat.tools.invoke(
        _request(
            {
                "model_paths": ["a.onnx"],
                "precision": "fp16",
                "dataset": "./calib",
                "instruction": "把这个模型转换为 int8 并验证",
            }
        )
    )

    assert result.ok is True
    assert result.result["ok"] is True
    assert result.result["final_text"] == "已完成转换。"
    assert result.result["tool_calls"] == [
        {
            "tool_name": "convert_model",
            "arguments": {"path": "a.onnx"},
            "result": "ok",
        }
    ]

    assert len(stream_stub.calls) == 1
    request = stream_stub.calls[0]
    assert request.user_message.text == "把这个模型转换为 int8 并验证"
    assert request.extra == build_module_build_extra(
        BuildSession(
            model_paths=["a.onnx"], quant_precision="fp16", dataset_path="./calib"
        )
    )


async def test_handler_creates_a_fresh_conversation_and_tab():
    container, stream_stub = _container(frames=[_Frame("end", {})])
    bridge_mod.register_cli_build_tool(container)

    result = await container.chat.tools.invoke(
        _request({"model_paths": ["a.onnx"], "instruction": "转换"})
    )

    assert result.result["conversation_id"] == "conv-nested"
    assert container.chat.create_conversation_use_case.calls
    assert container.chat.open_tab_use_case.calls


# ---------------------------------------------------------------------------
# (c) a long summarised result still hits the fold + /show path
# ---------------------------------------------------------------------------


async def test_long_qai_build_result_triggers_fold_registration():
    long_text = "\n".join(f"结果行{i}" for i in range(30))
    frames = [_Frame("chunk", {"text": long_text}), _Frame("end", {})]
    container, _ = _container(frames=frames)
    bridge_mod.register_cli_build_tool(container)

    result = await container.chat.tools.invoke(
        _request({"model_paths": ["a.onnx"], "instruction": "转换"})
    )
    assert result.ok is True

    opts = RenderOptions(color=False, emoji=False, fold_lines=5)
    renderer = StreamFrameRenderer(opts, out=io.StringIO(), err=io.StringIO())
    renderer.render(_Frame("tool_result", {"result": result.result}))

    assert renderer.last_fold_index == 1
    folded = renderer.folded(1)
    assert folded is not None
    assert "结果行29" in folded


# ---------------------------------------------------------------------------
# (d) zero-provider precheck error path never drives a turn
# ---------------------------------------------------------------------------


async def test_handler_returns_error_and_skips_turn_when_no_provider_configured():
    container, stream_stub = _container(provider_configured=False)
    bridge_mod.register_cli_build_tool(container)

    result = await container.chat.tools.invoke(
        _request({"model_paths": ["a.onnx"], "instruction": "转换"})
    )

    assert result.ok is True
    assert result.result["ok"] is False
    assert "provider" in result.result["error"]
    assert stream_stub.calls == []
    assert container.chat.create_conversation_use_case.calls == []


async def test_handler_returns_error_when_model_paths_missing():
    container, stream_stub = _container()
    bridge_mod.register_cli_build_tool(container)

    result = await container.chat.tools.invoke(_request({"instruction": "转换"}))

    assert result.result["ok"] is False
    assert stream_stub.calls == []


async def test_handler_returns_error_when_instruction_missing():
    container, stream_stub = _container()
    bridge_mod.register_cli_build_tool(container)

    result = await container.chat.tools.invoke(_request({"model_paths": ["a.onnx"]}))

    assert result.result["ok"] is False
    assert stream_stub.calls == []
