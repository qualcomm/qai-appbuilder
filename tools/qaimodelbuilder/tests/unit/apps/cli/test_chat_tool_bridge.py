"""Unit tests for ``apps.cli._chat_tool_bridge`` (Step 5 — tool whitelist).

Covers: (a) each tool's schema is present/correctly shaped; (b) each
handler, driven with a mocked :class:`~apps.api.di.Container`, returns a
result whose fields match the corresponding CLI subcommand's ``--json``
shape (``_model_to_dict`` / ``_run_to_dict``); (c) a long fixture result
correctly appears in :class:`~apps.cli._render.StreamFrameRenderer`'s fold
registry when rendered through a synthetic ``tool_result`` frame (reusing
``test_render.py``'s fold-testing pattern).
"""

from __future__ import annotations

import io
from datetime import datetime, timezone
from types import SimpleNamespace

from apps.cli import _chat_tool_bridge as bridge_mod
from apps.cli._render import RenderOptions, StreamFrameRenderer
from apps.cli.commands.pack import _model_to_dict
from apps.cli.commands.run import _run_to_dict
from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.run import Run, RunStatus
from qai.app_builder.domain.taxonomy import Taxonomy
from qai.app_builder.domain.value_objects import AppModelId, RunId
from qai.chat.adapters.tool_invocation import TOOL_ORDER, RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest
from qai.chat.domain.ids import ConversationId, TabId


class _Frame:
    """Minimal duck-typed stand-in for a ``StreamFrame`` (mirrors test_render.py)."""

    __slots__ = ("frame_type", "payload")

    def __init__(self, frame_type: str, payload: dict) -> None:
        self.frame_type = frame_type
        self.payload = payload


def _request(tool_name: str, arguments: dict | None = None) -> ToolInvocationRequest:
    return ToolInvocationRequest(
        tab_id=TabId.of("tab-1"),
        conversation_id=ConversationId.of("conv-1"),
        tool_name=tool_name,
        arguments=arguments or {},
    )


def _model(model_id: str, title: str) -> AppModelDefinition:
    return AppModelDefinition(
        id=AppModelId(value=model_id),
        title=title,
        taxonomy=Taxonomy(segments=("audio", "asr")),
    )


def _run(run_id: str, model_id: str) -> Run:
    return Run(
        id=RunId(value=run_id),
        model_id=AppModelId(value=model_id),
        inputs={"text": "hi"},
        status=RunStatus.COMPLETED,
        created_at=datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc),
    )


def _container(**overrides) -> SimpleNamespace:
    base = SimpleNamespace(
        chat=SimpleNamespace(tools=RegistryBackedToolInvocation()),
        user_prefs=SimpleNamespace(list_skills_use_case=None),
        app_builder=SimpleNamespace(
            list_app_models_use_case=None, list_runs_use_case=None
        ),
        model_runtime=SimpleNamespace(get_status_use_case=None),
    )
    for key, value in overrides.items():
        setattr(base, key, value)
    return base


class _AsyncResult:
    """Callable ``.execute()`` stub returning a fixed value."""

    def __init__(self, value) -> None:
        self._value = value

    async def execute(self, *args, **kwargs):
        return self._value


# ---------------------------------------------------------------------------
# (a) schemas
# ---------------------------------------------------------------------------


def test_schemas_are_openai_shaped_and_named_correctly():
    expected = {
        "qai_skill_list": bridge_mod.QAI_SKILL_LIST_SCHEMA,
        "qai_pack_list": bridge_mod.QAI_PACK_LIST_SCHEMA,
        "qai_service_status": bridge_mod.QAI_SERVICE_STATUS_SCHEMA,
        "qai_run_list": bridge_mod.QAI_RUN_LIST_SCHEMA,
    }
    for name, schema in expected.items():
        assert schema["type"] == "function"
        fn = schema["function"]
        assert fn["name"] == name
        assert isinstance(fn["description"], str) and fn["description"]
        assert fn["parameters"]["type"] == "object"
        assert isinstance(fn["parameters"]["properties"], dict)


def test_qai_run_list_schema_has_optional_limit_param():
    props = bridge_mod.QAI_RUN_LIST_SCHEMA["function"]["parameters"]["properties"]
    assert props["limit"]["type"] == "integer"


def test_tool_order_appends_new_names_without_reordering_existing():
    existing_prefix = (
        "read",
        "edit",
        "write",
        "apply_patch",
        "exec",
        "background_process",
        "glob",
        "grep",
        "webfetch",
        "web_search",
        "agent",
        "list_subagents",
        "skill",
        "todowrite",
        "question",
        "appbuilder_run",
        "appbuilder_batch_run",
    )
    assert TOOL_ORDER[: len(existing_prefix)] == existing_prefix
    for name in ("qai_skill_list", "qai_pack_list", "qai_service_status", "qai_run_list"):
        assert name in TOOL_ORDER


# ---------------------------------------------------------------------------
# register_cli_tools
# ---------------------------------------------------------------------------


def test_register_cli_tools_registers_all_four_with_schemas():
    container = _container()
    registered = bridge_mod.register_cli_tools(container)
    assert registered == (
        "qai_skill_list",
        "qai_pack_list",
        "qai_service_status",
        "qai_run_list",
    )
    tools = container.chat.tools
    assert set(registered) <= set(tools.registered_tools)
    schema_names = {s["function"]["name"] for s in tools.schemas()}
    assert set(registered) <= schema_names


# ---------------------------------------------------------------------------
# (b) handler behaviour matches the --json shape
# ---------------------------------------------------------------------------


async def test_qai_skill_list_handler_returns_use_case_result_verbatim():
    fixture = {"skills": [{"id": "s1", "enabled": True}], "last_reload": None}
    container = _container(user_prefs=SimpleNamespace(list_skills_use_case=_AsyncResult(fixture)))
    bridge_mod.register_cli_tools(container)

    result = await container.chat.tools.invoke(_request("qai_skill_list"))
    assert result.ok is True
    assert result.result == fixture


async def test_qai_pack_list_handler_matches_model_to_dict_shape(monkeypatch):
    import apps.cli.commands.pack as pack_mod

    async def _fake_seed(c) -> None:
        return None

    monkeypatch.setattr(pack_mod, "_seed_factory_packs_if_empty", _fake_seed)

    models = [_model("model-a", "Model A"), _model("model-b", "Model B")]
    container = _container(
        app_builder=SimpleNamespace(
            list_app_models_use_case=_AsyncResult(models),
            list_runs_use_case=None,
        )
    )
    bridge_mod.register_cli_tools(container)

    result = await container.chat.tools.invoke(_request("qai_pack_list"))
    assert result.ok is True
    assert result.result == {"items": [_model_to_dict(m) for m in models]}
    assert result.result["items"][0]["id"] == "model-a"
    assert result.result["items"][1]["title"] == "Model B"


async def test_qai_service_status_handler_returns_use_case_result_verbatim():
    fixture = {"running": True, "model": "whisper-base", "host": "127.0.0.1", "port": 8731}
    container = _container(model_runtime=SimpleNamespace(get_status_use_case=_AsyncResult(fixture)))
    bridge_mod.register_cli_tools(container)

    result = await container.chat.tools.invoke(_request("qai_service_status"))
    assert result.ok is True
    assert result.result == fixture


async def test_qai_run_list_handler_matches_run_to_dict_shape_and_default_limit():
    runs = [_run("A" * 26, "model-a"), _run("B" * 26, "model-a")]

    class _CapturingRunsUseCase:
        def __init__(self, value) -> None:
            self._value = value
            self.calls: list[dict] = []

        async def execute(self, *, limit, offset):
            self.calls.append({"limit": limit, "offset": offset})
            return self._value

    uc = _CapturingRunsUseCase(runs)
    container = _container(
        app_builder=SimpleNamespace(
            list_app_models_use_case=None,
            list_runs_use_case=uc,
        )
    )
    bridge_mod.register_cli_tools(container)

    result = await container.chat.tools.invoke(_request("qai_run_list"))
    assert result.ok is True
    assert result.result == {"items": [_run_to_dict(r) for r in runs]}
    assert uc.calls == [{"limit": 20, "offset": 0}]


async def test_qai_run_list_handler_honours_explicit_limit():
    class _CapturingRunsUseCase:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def execute(self, *, limit, offset):
            self.calls.append({"limit": limit, "offset": offset})
            return []

    uc = _CapturingRunsUseCase()
    container = _container(
        app_builder=SimpleNamespace(list_app_models_use_case=None, list_runs_use_case=uc)
    )
    bridge_mod.register_cli_tools(container)

    result = await container.chat.tools.invoke(_request("qai_run_list", {"limit": 5}))
    assert result.ok is True
    assert uc.calls == [{"limit": 5, "offset": 0}]


async def test_qai_run_list_handler_falls_back_to_default_on_invalid_limit():
    class _CapturingRunsUseCase:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def execute(self, *, limit, offset):
            self.calls.append({"limit": limit, "offset": offset})
            return []

    uc = _CapturingRunsUseCase()
    container = _container(
        app_builder=SimpleNamespace(list_app_models_use_case=None, list_runs_use_case=uc)
    )
    bridge_mod.register_cli_tools(container)

    for bad_limit in (0, -1, "bogus"):
        await container.chat.tools.invoke(_request("qai_run_list", {"limit": bad_limit}))
    assert all(call == {"limit": 20, "offset": 0} for call in uc.calls)


async def test_qai_run_list_handler_returns_empty_items_when_use_case_unwired():
    container = _container(
        app_builder=SimpleNamespace(list_app_models_use_case=None, list_runs_use_case=None)
    )
    bridge_mod.register_cli_tools(container)

    result = await container.chat.tools.invoke(_request("qai_run_list"))
    assert result.ok is True
    assert result.result == {"items": []}


# ---------------------------------------------------------------------------
# (c) long tool result correctly hits the fold + /show path
# ---------------------------------------------------------------------------


async def test_long_qai_pack_list_result_triggers_fold_registration(monkeypatch):
    import apps.cli.commands.pack as pack_mod

    async def _fake_seed(c) -> None:
        return None

    monkeypatch.setattr(pack_mod, "_seed_factory_packs_if_empty", _fake_seed)

    many_models = [_model(f"model-{i}", f"Model {i}") for i in range(30)]
    container = _container(
        app_builder=SimpleNamespace(
            list_app_models_use_case=_AsyncResult(many_models),
            list_runs_use_case=None,
        )
    )
    bridge_mod.register_cli_tools(container)

    result = await container.chat.tools.invoke(_request("qai_pack_list"))
    assert result.ok is True

    opts = RenderOptions(color=False, emoji=False, fold_lines=5)
    renderer = StreamFrameRenderer(opts, out=io.StringIO(), err=io.StringIO())
    renderer.render(_Frame("tool_result", {"result": result.result}))

    assert renderer.last_fold_index == 1
    folded = renderer.folded(1)
    assert folded is not None
    assert "model-29" in folded
