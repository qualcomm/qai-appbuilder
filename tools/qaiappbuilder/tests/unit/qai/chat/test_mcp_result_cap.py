# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-MCP-server ``max_result_chars``: config, registry lookup, truncator, tool, HTTP."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from qai.chat.adapters import (
    AdaptiveToolResultTruncator,
    McpManageToolHandler,
    McpServerRegistry,
    RegistryBackedToolInvocation,
)
from qai.chat.adapters.mcp_manage_tool import MCP_MANAGE_TOOL_SCHEMA
from qai.chat.adapters.mcp_wheel_installer import WheelInstallResult
from qai.chat.application.ports import McpServerStatus, ToolResultTruncationRequest
from qai.chat.domain.mcp_server import McpServerConfig

QWEN = "qai-service::qwen3.7-max"
CLAUDE = "claude-sonnet-4"


def _cfg(name: str = "sm8975", **kw: Any) -> McpServerConfig:
    return McpServerConfig(name=name, command="x", **kw)


def _registry(tmp_path: Path) -> McpServerRegistry:
    return McpServerRegistry(
        tools=RegistryBackedToolInvocation(),
        config_path=tmp_path / "mcp_servers.json",
        enabled=False,  # persist + track configs, never spawn anything
        secret_store=None,
        ssl_verify=True,
        ssl_verify_provider=None,
        edition="internal",
    )


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── domain validation ───────────────────────────────────────────────────────


def test_default_is_unset_so_existing_servers_are_unchanged() -> None:
    assert _cfg().max_result_chars is None


@pytest.mark.parametrize("ok", [0, 10_000, 60_000, 2_000_000])
def test_valid_values_are_accepted(ok: int) -> None:
    assert _cfg(max_result_chars=ok).max_result_chars == ok


@pytest.mark.parametrize("bad", [-1, 1, 9_999, 2_000_001, True, 1.5, "60000"])
def test_invalid_values_are_rejected(bad: Any) -> None:
    with pytest.raises(ValueError):
        _cfg(max_result_chars=bad)


# ── persistence ─────────────────────────────────────────────────────────────


def test_value_round_trips_through_the_config_file(tmp_path: Path) -> None:
    _run(_registry(tmp_path).add_server(_cfg(max_result_chars=60_000)))
    on_disk = json.loads((tmp_path / "mcp_servers.json").read_text(encoding="utf-8"))
    assert on_disk["servers"][0]["max_result_chars"] == 60_000
    fresh = _registry(tmp_path)
    _run(fresh.list_servers())  # a new process loads its configs on first use
    assert fresh.result_budget_for_tool("sm8975__get_skill") == 60_000


def test_unset_value_is_not_written_and_junk_on_disk_is_ignored(tmp_path: Path) -> None:
    _run(_registry(tmp_path).add_server(_cfg()))
    path = tmp_path / "mcp_servers.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "max_result_chars" not in data["servers"][0]
    for junk in ("lots", -5, 5, True, 10**9, None):
        data["servers"][0]["max_result_chars"] = junk
        path.write_text(json.dumps(data), encoding="utf-8")
        fresh = _registry(tmp_path)
        loaded = _run(fresh.list_servers())
        assert [st.config.name for st in loaded] == ["sm8975"], junk  # the entry still loads
        assert fresh.result_budget_for_tool("sm8975__t") is None, junk


# ── registry lookup ─────────────────────────────────────────────────────────


def test_budget_lookup_by_tool_name(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    _run(reg.add_server(_cfg("sm8975", max_result_chars=80_000)))
    _run(reg.add_server(_cfg("plain")))
    assert reg.result_budget_for_tool("sm8975__get_skill") == 80_000
    assert reg.result_budget_for_tool("mcp__sm8975__read_resource") == 80_000
    assert reg.result_budget_for_tool("plain__anything") is None  # server without an override
    assert reg.result_budget_for_tool("exec") is None  # not an MCP tool
    assert reg.result_budget_for_tool("sm8975") is None  # no "__" boundary


def test_longest_server_prefix_wins_when_names_contain_underscores(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    _run(reg.add_server(_cfg("db", max_result_chars=20_000)))
    _run(reg.add_server(_cfg("db__replica", max_result_chars=90_000)))
    assert reg.result_budget_for_tool("db__replica__query") == 90_000
    assert reg.result_budget_for_tool("db__query") == 20_000


def test_re_adding_a_server_without_the_field_keeps_the_hand_written_value(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    _run(reg.add_server(_cfg(max_result_chars=60_000)))
    _run(reg.add_server(_cfg()))  # e.g. a UI save: the panel has no such field
    assert reg.result_budget_for_tool("sm8975__t") == 60_000


def test_explicit_zero_clears_it_and_a_new_value_replaces_it(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    _run(reg.add_server(_cfg(max_result_chars=60_000)))
    _run(reg.add_server(_cfg(max_result_chars=120_000)))
    assert reg.result_budget_for_tool("sm8975__t") == 120_000
    _run(reg.add_server(_cfg(max_result_chars=0)))
    assert reg.result_budget_for_tool("sm8975__t") is None
    _run(reg.add_server(_cfg()))  # and it stays cleared
    assert reg.result_budget_for_tool("sm8975__t") is None


# ── truncator ───────────────────────────────────────────────────────────────


def _req(model: str, n: int, tool: str = "sm8975__get_skill") -> ToolResultTruncationRequest:
    return ToolResultTruncationRequest(model_id=model, tool_name=tool, result_text="x" * n)


def test_without_a_provider_behaviour_is_unchanged() -> None:
    t = AdaptiveToolResultTruncator()
    qwen = t.truncate(_req(QWEN, 36_664))  # the real get_skill size
    assert qwen.truncated and qwen.omitted_chars > 0  # qwen's 30K budget cuts it
    assert not t.truncate(_req(CLAUDE, 36_664)).truncated  # claude's 50K budget does not
    assert t.tool_budget_provider is None


def test_a_server_cap_lets_the_whole_result_through() -> None:
    t = AdaptiveToolResultTruncator(tool_budget_provider=lambda name: 60_000)
    r = t.truncate(_req(QWEN, 36_664))
    assert not r.truncated and r.final_length == 36_664


def test_results_over_the_server_cap_are_still_truncated_head_and_tail() -> None:
    t = AdaptiveToolResultTruncator(tool_budget_provider=lambda name: 40_000)
    r = t.truncate(_req(QWEN, 90_000))
    assert r.truncated and "chars omitted" in r.text
    assert r.final_length < 90_000


def test_provider_sees_the_tool_name_and_none_means_family_budget() -> None:
    seen: list[str] = []

    def provider(name: str) -> int | None:
        seen.append(name)
        return 60_000 if name.startswith("sm8975__") else None

    t = AdaptiveToolResultTruncator(tool_budget_provider=provider)
    assert not t.truncate(_req(QWEN, 36_664, "sm8975__get_skill")).truncated
    assert t.truncate(_req(QWEN, 36_664, "other__tool")).truncated
    assert seen == ["sm8975__get_skill", "other__tool"]


@pytest.mark.parametrize("answer", [0, -3, True, "big", 1.5])
def test_nonsense_provider_answers_fall_back_to_the_family_budget(answer: Any) -> None:
    t = AdaptiveToolResultTruncator(tool_budget_provider=lambda name: answer)  # type: ignore[arg-type,return-value]
    assert t.truncate(_req(QWEN, 36_664)).truncated


def test_a_provider_that_raises_never_fails_the_tool_call() -> None:
    def boom(name: str) -> int:
        raise RuntimeError("registry gone")

    t = AdaptiveToolResultTruncator(tool_budget_provider=boom)
    assert t.truncate(_req(QWEN, 36_664)).truncated  # family budget applied, no exception


def test_ordered_slice_tools_are_unaffected() -> None:
    t = AdaptiveToolResultTruncator(tool_budget_provider=lambda name: 10_000)
    r = t.truncate(ToolResultTruncationRequest(model_id=QWEN, tool_name="read", result_text="x" * 20_000))
    assert not r.truncated  # ``read`` keeps its own ordered-slice contract


def test_registry_and_truncator_wire_together(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    _run(reg.add_server(_cfg("sm8975", max_result_chars=60_000)))
    t = AdaptiveToolResultTruncator(tool_budget_provider=reg.result_budget_for_tool)
    assert not t.truncate(_req(QWEN, 36_664, "sm8975__get_skill")).truncated
    assert t.truncate(_req(QWEN, 36_664, "someone_else__get_skill")).truncated


# ── mcp_manage tool ─────────────────────────────────────────────────────────


class _Reg:
    def __init__(self) -> None:
        self.servers: dict[str, McpServerConfig] = {}

    def global_enabled(self) -> bool:
        return True

    async def add_server(self, config: McpServerConfig) -> McpServerStatus:
        self.servers[config.name] = config
        return McpServerStatus(config=config, connected=True, tool_count=1, tool_names=("t",))

    async def list_servers(self) -> tuple[McpServerStatus, ...]:
        return tuple(McpServerStatus(config=c, connected=True) for c in self.servers.values())


async def _ok_install(wheel_path: str, **_: Any) -> WheelInstallResult:
    return WheelInstallResult(ok=True, dist_name="d", version="1", venv_dir="V", command="V/d.exe", args=())


def _tool(reg: _Reg, **arguments: Any) -> str:
    h = McpManageToolHandler(registry=reg, data_dir=Path("D"), install_fn=_ok_install)  # type: ignore[arg-type]
    return asyncio.run(h.execute(SimpleNamespace(arguments=arguments)))  # type: ignore[arg-type]


def test_install_wheel_passes_the_cap_through_and_status_shows_it() -> None:
    reg = _Reg()
    _tool(reg, action="install_wheel", name="s", wheel_path="w.whl", max_result_chars=150_000)
    assert reg.servers["s"].max_result_chars == 150_000
    assert "result cap 150000" in _tool(reg, action="status")


def test_omitted_zero_and_clamped_values() -> None:
    reg = _Reg()
    _tool(reg, action="add", name="a", command="c")
    assert reg.servers["a"].max_result_chars is None  # omitted -> registry keeps what it has
    _tool(reg, action="add", name="b", command="c", max_result_chars=0)
    assert reg.servers["b"].max_result_chars == 0  # explicit clear reaches the registry
    _tool(reg, action="add", name="c", command="c", max_result_chars=5)
    assert reg.servers["c"].max_result_chars == 10_000
    _tool(reg, action="add", name="d", command="c", max_result_chars=10**9)
    assert reg.servers["d"].max_result_chars == 2_000_000
    for i, junk in enumerate(["many", -4, True, None]):
        _tool(reg, action="add", name=f"j{i}", command="c", max_result_chars=junk)
        assert reg.servers[f"j{i}"].max_result_chars is None


def test_schema_documents_the_parameter() -> None:
    prop = MCP_MANAGE_TOOL_SCHEMA["function"]["parameters"]["properties"]["max_result_chars"]
    assert prop["type"] == "integer" and "2000000" in prop["description"]


# ── HTTP model ──────────────────────────────────────────────────────────────


def test_http_models_carry_the_field() -> None:
    from interfaces.http.routes.chat._mcp import McpServerConfigModel, _status_to_model

    m = McpServerConfigModel(name="s", transport="stdio", command="c", max_result_chars=75_000)
    assert m.to_domain().max_result_chars == 75_000
    assert McpServerConfigModel(name="s", transport="stdio", command="c").to_domain().max_result_chars is None
    with pytest.raises(ValueError):
        McpServerConfigModel(name="s", transport="stdio", command="c", max_result_chars=3_000_000)

    out = _status_to_model(McpServerStatus(config=_cfg(max_result_chars=75_000), connected=True))
    assert out.max_result_chars == 75_000
