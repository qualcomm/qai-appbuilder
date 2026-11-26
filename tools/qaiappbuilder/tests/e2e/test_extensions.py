# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - user extensions in a real backend process: load at start, call from a chat turn, edit, reload, call again.

The extension lives in the throw-away backend's own ``<data>/extensions``; nothing in the repo is touched. Tool calls come from the
scripted stand-in LLM (``_fake_llm.py``, ``[tool name {...}]``); no real model is used.

Covered: ``GET /api/extensions``; a model-issued call to the extension tool returns its result; ``POST /api/extensions/{name}/reload``
makes an edited extension take effect without a restart; a new extension added at runtime appears after ``POST /api/extensions/reload``;
a broken reload is reported in the status and the tool disappears; an extension skill appears in ``GET /api/skills`` after reload.
"""

from __future__ import annotations

import json
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from tests.e2e import _fake_llm
from tests.e2e._fake_llm import StandInLLM
from tests.e2e._owned_backend import OwnedBackend, owned_backend
from tests.e2e.test_chat_stream import Turn, _set_env, run_turn

pytestmark = pytest.mark.e2e


def _write(data_dir: Path, name: str, *, reply: str, tool: str) -> Path:
    ext = data_dir / "extensions" / name
    ext.mkdir(parents=True, exist_ok=True)
    (ext / "extension.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "name": name,
                "version": "1.0.0",
                "description": "e2e",
                "entry": "main.py",
                "tools": [{"name": tool, "group": "read"}],
            }
        ),
        encoding="utf-8",
    )
    (ext / "main.py").write_text(
        textwrap.dedent(
            f"""
            from qai.ext import ExtensionContext, ToolCall

            def activate(ctx: ExtensionContext) -> None:
                def run(call: ToolCall) -> str:
                    return {reply!r} + ":" + str(call.arguments.get("who", ""))

                ctx.register_tool({tool!r}, run, description="e2e tool",
                                  parameters={{"type": "object", "properties": {{"who": {{"type": "string"}}}}}})
            """
        ),
        encoding="utf-8",
    )
    return ext


@pytest.fixture(scope="module")
def llm() -> Iterator[StandInLLM]:
    s = _fake_llm.start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def be(llm: StandInLLM) -> Iterator[tuple[OwnedBackend, httpx.Client]]:
    old = _set_env(
        {
            "QAI_CHAT__LLM_BASE_URL": llm.base_url,
            "QAI_CHAT__LLM_DEFAULT_MODEL": "e2e-model",
            "QAI_CHAT__LLM_API_KEY": None,
        }
    )
    try:
        with owned_backend() as ob:
            _write(ob.data_dir, "e2e-ext", reply="v1", tool="e2e_ext_hello")
            yield ob, ob.start()
    finally:
        _set_env(old)


def _call(cl: httpx.Client, llm: StandInLLM, tab: str, tool: str) -> Turn:
    llm.clear()
    cid = cl.post("/api/chat/conversations", json={"title": "e2e"}).json()["id"]
    return run_turn(cl, cid, tab, f"[tool {tool} " + json.dumps({"who": "e2e"}) + "]", seconds=60)


def _result(turn: Turn) -> dict:
    (frame,) = turn.frames("tool_result")
    return frame["payload"]


def test_the_extension_is_loaded_at_start(be: tuple) -> None:
    ob, cl = be
    body = cl.get("/api/extensions").json()
    assert body["directory"] == str(ob.data_dir / "extensions")
    (ext,) = body["extensions"]
    assert (ext["name"], ext["state"], ext["tools"], ext["error"]) == (
        "e2e-ext",
        "loaded",
        ["e2e_ext_hello"],
        "",
    )


def test_the_model_can_call_the_extension_tool(be: tuple, llm: StandInLLM) -> None:
    _ob, cl = be
    res = _result(_call(cl, llm, "t-call", "e2e_ext_hello"))
    assert res.get("error_code") is None, res
    assert res["result"] == "v1:e2e"
    # the tool was advertised to the model with the schema the host built
    tools = {t["function"]["name"]: t for t in llm.chat_requests()[0].body.get("tools", [])}
    assert tools["e2e_ext_hello"]["function"]["parameters"]["properties"] == {
        "who": {"type": "string"}
    }


def test_an_edit_takes_effect_after_reloading_that_extension(be: tuple, llm: StandInLLM) -> None:
    ob, cl = be
    _write(ob.data_dir, "e2e-ext", reply="v2", tool="e2e_ext_hello")
    r = cl.post("/api/extensions/e2e-ext/reload")
    assert r.status_code == 200 and r.json()["state"] == "loaded", r.text
    assert _result(_call(cl, llm, "t-after-edit", "e2e_ext_hello"))["result"] == "v2:e2e"


def test_a_new_extension_appears_after_reloading_all(be: tuple, llm: StandInLLM) -> None:
    ob, cl = be
    _write(ob.data_dir, "late-ext", reply="late", tool="late_ext_hello")
    names = [e["name"] for e in cl.post("/api/extensions/reload").json()["extensions"]]
    assert names == ["e2e-ext", "late-ext"]
    assert _result(_call(cl, llm, "t-late", "late_ext_hello"))["result"] == "late:e2e"


def test_an_extension_skill_appears_after_reload(be: tuple) -> None:
    ob, cl = be
    skill = ob.data_dir / "extensions" / "late-ext" / "skills" / "late-helper"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: late-helper\ndescription: e2e skill\n---\n# late-helper\n", encoding="utf-8"
    )
    assert "late-helper" not in {s["skill_id"] for s in cl.get("/api/skills").json()["skills"]}

    status = cl.post("/api/extensions/late-ext/reload").json()

    assert status["skills"] == ["late-helper"]
    listed = {s["skill_id"]: s for s in cl.get("/api/skills").json()["skills"]}
    assert listed["late-helper"]["source"] == "extension:late-ext"


def test_a_broken_reload_is_reported_and_the_tool_goes_away(be: tuple, llm: StandInLLM) -> None:
    ob, cl = be
    (ob.data_dir / "extensions" / "e2e-ext" / "main.py").write_text(
        "raise RuntimeError('broken on purpose')\n", encoding="utf-8"
    )
    status = cl.post("/api/extensions/e2e-ext/reload").json()
    assert status["state"] == "failed" and "broken on purpose" in status["error"]
    res = _result(_call(cl, llm, "t-broken", "e2e_ext_hello"))
    assert res.get("error_code"), res  # the model's call no longer reaches a handler


def test_unknown_extension_is_404(be: tuple) -> None:
    _ob, cl = be
    r = cl.post("/api/extensions/no-such-ext/reload")
    assert r.status_code == 404 and r.json()["code"] == "extensions.not_found"


# -- prompt fragments ------------------------------------------------------------------


def _messages(llm: StandInLLM) -> list[dict]:
    return llm.chat_requests()[0].body["messages"]


def test_prompt_fragments_reach_the_model(be: tuple, llm: StandInLLM) -> None:
    """'always' ends the system prompt; 'active' rides as a context message only when selected."""
    ob, cl = be
    ext = ob.data_dir / "extensions" / "prompt-ext"
    ext.mkdir(parents=True)
    (ext / "active.md").write_text("ACTIVE-MARKER: answer like a pirate", encoding="utf-8")
    (ext / "extension.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "name": "prompt-ext",
                "version": "1",
                "description": "e2e",
                "prompts": [
                    {"when": "always", "text": "ALWAYS-MARKER: be brief"},
                    {"when": "active", "file": "active.md"},
                ],
            }
        ),
        encoding="utf-8",
    )
    status = cl.post("/api/extensions/prompt-ext/reload").json()
    assert status["state"] == "loaded" and status["prompts"] == {"always": 1, "active": 1}, status

    def turn(tab: str, **query: str) -> list[dict]:
        llm.clear()
        cid = cl.post("/api/chat/conversations", json={"title": "e2e"}).json()["id"]
        t = run_turn(cl, cid, tab, "[echo] hi", seconds=60, **query)
        assert t.status == 200, t.body
        return _messages(llm)

    plain = turn("t-prompt-plain")
    active = turn("t-prompt-active", active_extensions=json.dumps(["prompt-ext"]))

    for msgs in (plain, active):
        assert msgs[0]["role"] == "system"
        assert (
            msgs[0]["content"]
            .rstrip()
            .endswith("ALWAYS-MARKER: be brief\n</extension_instructions>")
        )
    assert plain[0]["content"] == active[0]["content"], (
        "system prompt must not change with the selection"
    )
    assert not any("ACTIVE-MARKER" in str(m.get("content")) for m in plain)
    (ctx,) = [m for m in active if "ACTIVE-MARKER" in str(m.get("content"))]
    assert ctx["role"] == "user"
    assert active.index(ctx) == len(active) - 2  # right before the real user message
    assert active[-1]["content"] == "[echo] hi"


# -- MCP servers ------------------------------------------------------------------------

_MINI_MCP = Path(__file__).resolve().parents[1] / "fixtures" / "mini_mcp_server.py"


def _mcp_servers(cl: httpx.Client) -> dict[str, dict]:
    return {s["name"]: s for s in cl.get("/api/chat/mcp/servers").json()["servers"]}


def _wait_connected(cl: httpx.Client, name: str, *, timeout: float = 30.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        server = _mcp_servers(cl).get(name)
        if server and server["connected"]:
            return server
        time.sleep(0.3)
    raise AssertionError(f"{name} never connected: {_mcp_servers(cl).get(name)}")


def test_an_extension_mcp_server_is_usable_read_only_and_never_persisted(
    be: tuple, llm: StandInLLM
) -> None:
    ob, cl = be
    assert cl.patch("/api/chat/mcp/enabled", json={"enabled": True}).status_code == 200
    ext = ob.data_dir / "extensions" / "mcp-ext"
    ext.mkdir(parents=True)
    (ext / "server.py").write_text(_MINI_MCP.read_text("utf-8"), encoding="utf-8")
    (ext / "extension.json").write_text(
        json.dumps(
            {
                "manifest_version": 1,
                "name": "mcp-ext",
                "version": "1",
                "description": "e2e",
                "settings": [{"key": "token", "type": "secret", "label": "Token"}],
                "mcp_servers": [
                    {
                        "name": "mini",
                        "command": "${python}",
                        "args": ["${root}/server.py"],
                        "env": {"MINI_MCP_PREFIX": "${secret:token}|"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    # The secret store is the OS keyring, which outlives this throw-away data dir: start clean.
    cl.post("/api/extensions/mcp-ext/reload")
    assert (
        cl.put("/api/extensions/mcp-ext/settings", json={"values": {"token": None}}).status_code
        == 200
    )
    status = cl.post("/api/extensions/mcp-ext/reload").json()
    assert status["state"] == "loaded" and status["mcp_servers"] == []
    assert any("secret setting 'token' is not set" in w for w in status["warnings"]), status

    # Filling in the settings card starts the server -- no reload.
    r = cl.put("/api/extensions/mcp-ext/settings", json={"values": {"token": "TKN-123"}})
    assert r.status_code == 200, r.text
    server = _wait_connected(cl, "mcp-ext-mini")
    assert server["source"] == "extension:mcp-ext" and server["read_only"] is True
    assert server["env"] == {"MINI_MCP_PREFIX": "__secret__"}
    assert set(server["tool_names"]) == {"echo", "env", "cwd"}

    llm.clear()
    cid = cl.post("/api/chat/conversations", json={"title": "e2e"}).json()["id"]
    turn = run_turn(
        cl, cid, "t-mcp", "[tool mcp-ext-mini__echo " + json.dumps({"text": "hi"}) + "]", seconds=60
    )
    assert turn.status == 200, turn.body
    assert "TKN-123|hi" in json.dumps(_result(turn))

    # Read-only through the user API; never written to the user's file.
    assert cl.delete("/api/chat/mcp/servers/mcp-ext-mini").status_code == 409
    assert (
        cl.post("/api/chat/mcp/servers", json={"name": "mcp-ext-mini", "command": "x"}).status_code
        == 409
    )
    assert "mcp-ext-mini" not in (ob.data_dir / "config" / "mcp_servers.json").read_text("utf-8")

    # Disabling the extension removes the server and its tools.
    assert cl.post("/api/extensions/mcp-ext/disable").json()["state"] == "disabled"
    assert "mcp-ext-mini" not in _mcp_servers(cl)

    # Uninstall with purge: also removes the keyring secret.
    assert cl.delete("/api/extensions/mcp-ext", params={"purge_data": "true"}).status_code == 204
    assert not ext.exists()
