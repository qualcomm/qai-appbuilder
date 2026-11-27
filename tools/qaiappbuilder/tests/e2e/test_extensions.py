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

import io
import json
import textwrap
import time
import zipfile
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
    (ext,) = [e for e in body["extensions"] if not e.get("builtin")]
    builtin = next(e for e in body["extensions"] if e["name"] == "qualcomm-skills")
    assert builtin["builtin"] is True and builtin["state"] == "loaded"
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
    names = [e["name"] for e in cl.post("/api/extensions/reload").json()["extensions"] if not e.get("builtin")]
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
    # The Skills settings panel reads the security discovery list: same provider.
    panel = {s["skill_name"]: s for s in cl.get("/api/security/skill-discovery").json()["skills"]}
    assert panel["late-helper"]["provider"] == "extension:late-ext"
    assert panel["late-helper"]["source"] == "skills"


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


# -- install ---------------------------------------------------------------------------


def _ext_zip(name: str, *, reply: str, tool: str, version: str = "1.0.0") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr(f"{name}-{version}/extension.json", json.dumps({
            "manifest_version": 1, "name": name, "version": version, "description": "zip",
            "entry": "main.py", "tools": [{"name": tool, "group": "read"}],
        }))
        zf.writestr(f"{name}-{version}/main.py", textwrap.dedent(f"""
            def activate(ctx):
                ctx.register_tool({tool!r}, lambda call: {reply!r}, description="zip tool")
        """))
    return buf.getvalue()


def test_install_from_a_zip_upload_then_update_it(be: tuple, llm: StandInLLM) -> None:
    _ob, cl = be
    r = cl.post("/api/extensions/install/upload",
                files={"file": ("zip-ext.zip", _ext_zip("zip-ext", reply="z1", tool="zip_ext_hi"), "application/zip")})
    assert r.status_code == 200, r.text
    summary = r.json()
    assert summary["name"] == "zip-ext" and summary["runs_code"] is True and summary["replaces"] is False
    assert summary["tools"] == [{"name": "zip_ext_hi", "group": "read"}]
    assert "zip-ext" not in {e["name"] for e in cl.get("/api/extensions").json()["extensions"]}  # not yet

    status = cl.post(f"/api/extensions/install/{summary['token']}", json={}).json()
    assert status["state"] == "loaded" and status["tools"] == ["zip_ext_hi"], status
    assert _result(_call(cl, llm, "t-zip-1", "zip_ext_hi"))["result"] == "z1"

    # Same name again: 409 unless replace; replacing takes effect at once.
    again = cl.post("/api/extensions/install/upload",
                    files={"file": ("u.zip", _ext_zip("zip-ext", reply="z2", tool="zip_ext_hi", version="2.0.0"))}).json()
    assert again["installed_version"] == "1.0.0" and again["replaces"] is True
    assert cl.post(f"/api/extensions/install/{again['token']}", json={}).status_code == 409
    status = cl.post(f"/api/extensions/install/{again['token']}", json={"replace": True}).json()
    assert status["version"] == "2.0.0" and status["state"] == "loaded"
    assert _result(_call(cl, llm, "t-zip-2", "zip_ext_hi"))["result"] == "z2"


def test_install_from_a_folder_and_bad_uploads(be: tuple, tmp_path: Path) -> None:
    _ob, cl = be
    src = _write(tmp_path, "folder-ext", reply="f1", tool="folder_ext_hi")
    summary = cl.post("/api/extensions/install/folder", json={"path": str(src)}).json()
    assert summary["name"] == "folder-ext", summary
    assert cl.post(f"/api/extensions/install/{summary['token']}", json={}).json()["state"] == "loaded"

    bad = cl.post("/api/extensions/install/upload", files={"file": ("x.zip", b"not a zip")})
    assert bad.status_code == 400 and bad.json()["code"] == "extensions.bad_archive"
    assert cl.post("/api/extensions/install/0123456789abcdef", json={}).status_code == 404


# -- UI: modes + action buttons (route B) -----------------------------------------------


def test_ui_menu_and_action_route(be: tuple) -> None:
    ob, cl = be
    ext = ob.data_dir / "extensions" / "ui-ext"
    ext.mkdir(parents=True)
    (ext / "extension.json").write_text(json.dumps({
        "manifest_version": 1, "name": "ui-ext", "version": "1", "description": "ui", "entry": "main.py",
        "ui": {"label": "UI Ext", "icon": "*", "actions": [
            {"id": "greet", "label": "Greet", "fields": [{"key": "who", "type": "text", "label": "Who", "required": True}]},
            {"id": "boom", "label": "Boom"}]},
    }), encoding="utf-8")
    (ext / "main.py").write_text(textwrap.dedent("""
        from qai.ext import ActionResult

        def activate(ctx):
            ctx.register_action("greet", lambda c: ActionResult(message="hi " + c.values["who"], prompt="p"))
            def boom(c):
                raise RuntimeError("kaput")
            ctx.register_action("boom", boom)
    """), encoding="utf-8")
    assert cl.post("/api/extensions/ui-ext/reload").json()["state"] == "loaded"

    entries = {e["name"]: e for e in cl.get("/api/extensions/ui").json()["extensions"]}
    assert [a["id"] for a in entries["ui-ext"]["actions"]] == ["greet", "boom"]

    ok = cl.post("/api/extensions/ui-ext/actions/greet", json={"values": {"who": "bob"}}).json()
    assert ok == {"ok": True, "message": "hi bob", "prompt": "p"}
    bad = cl.post("/api/extensions/ui-ext/actions/greet", json={"values": {}})
    assert bad.status_code == 400 and "who" in bad.json()["details"]["field_errors"]
    err = cl.post("/api/extensions/ui-ext/actions/boom", json={}).json()
    assert err["ok"] is False and "kaput" in err["message"]
    assert cl.post("/api/extensions/ui-ext/actions/nope", json={}).status_code == 404

    cl.post("/api/extensions/ui-ext/disable")
    assert "ui-ext" not in {e["name"] for e in cl.get("/api/extensions/ui").json()["extensions"]}
    assert cl.post("/api/extensions/ui-ext/actions/greet", json={"values": {"who": "x"}}).status_code == 404

# -- HTTP routes ---------------------------------------------------------------------


def test_extension_http_routes_are_served_and_csrf_protected(be: tuple) -> None:
    ob, cl = be
    ext = ob.data_dir / "extensions" / "route-ext"
    ext.mkdir(parents=True)
    (ext / "extension.json").write_text(json.dumps({
        "manifest_version": 1, "name": "route-ext", "version": "1", "description": "routes", "entry": "main.py",
        "routes": [{"method": "GET", "path": "items/{item_id}"}, {"method": "POST", "path": "items"}],
    }), encoding="utf-8")
    (ext / "main.py").write_text(textwrap.dedent("""
        import json
        from qai.ext import RouteResponse

        def activate(ctx):
            store = ctx.data_dir / "items.json"
            def load():
                return json.loads(store.read_text()) if store.exists() else {}
            def add(req):
                items = load(); items[req.body["id"]] = req.body; store.write_text(json.dumps(items))
                return RouteResponse(body=req.body, status=201)
            def get(req):
                item = load().get(req.params["item_id"])
                return item if item else RouteResponse(body={"error": "not found"}, status=404)
            ctx.register_route("POST", "items", add)
            ctx.register_route("GET", "items/{item_id}", get)
    """), encoding="utf-8")
    assert cl.post("/api/extensions/route-ext/reload").json()["state"] == "loaded"

    r = cl.post("/api/ext/route-ext/items", json={"id": "a", "v": 1})
    assert r.status_code == 201, r.text
    assert cl.get("/api/ext/route-ext/items/a").json() == {"id": "a", "v": 1}
    assert cl.get("/api/ext/route-ext/items/zz").status_code == 404

    # Same protections as every /api route: a POST without the CSRF token is refused.
    with httpx.Client(base_url=ob.base_url, timeout=10.0) as bare:
        bare.get("/api/system/health")
        assert bare.post("/api/ext/route-ext/items", json={"id": "b"}).status_code == 403
    assert cl.get("/api/ext/route-ext/items/b").status_code == 404

    cl.post("/api/extensions/route-ext/disable")
    assert cl.get("/api/ext/route-ext/items/a").status_code == 404

# -- front-end module (route A) -------------------------------------------------------


def test_extension_frontend_files_are_served_and_confined(be: tuple) -> None:
    ob, cl = be
    ext = ob.data_dir / "extensions" / "web-ext"
    (ext / "web").mkdir(parents=True)
    (ext / "web" / "index.js").write_text("export function activate(api) {}\n", encoding="utf-8")
    (ext / "web" / "page.html").write_text("<script>alert(1)</script>", encoding="utf-8")
    (ext / "secret.txt").write_text("TOP-SECRET", encoding="utf-8")
    (ext / "extension.json").write_text(json.dumps({
        "manifest_version": 1, "name": "web-ext", "version": "2.5.0", "description": "web",
        "ui": {"label": "Web", "module": "web/index.js"},
    }), encoding="utf-8")
    assert cl.post("/api/extensions/web-ext/reload").json()["state"] == "loaded"

    entry = {e["name"]: e for e in cl.get("/api/extensions/ui").json()["extensions"]}["web-ext"]
    assert entry["module"].startswith("/api/ext/web-ext/_ui/index.js?v=2.5.0-")
    r = cl.get(entry["module"])
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/javascript")
    assert cl.get("/api/ext/web-ext/_ui/page.html").status_code == 404
    assert cl.get("/api/ext/web-ext/_ui/..%2fsecret.txt").status_code == 404
    assert cl.get("/api/ext/web-ext/_ui/../secret.txt").status_code == 404

    # The install summary says it runs JavaScript in the app's page.
    zip_bytes = io.BytesIO()
    with zipfile.ZipFile(zip_bytes, "w") as zf:
        zf.writestr("web2-ext/extension.json", json.dumps({
            "manifest_version": 1, "name": "web2-ext", "version": "1", "description": "w",
            "ui": {"label": "W2", "module": "web/index.js"}}))
        zf.writestr("web2-ext/web/index.js", "export function activate() {}")
    summary = cl.post("/api/extensions/install/upload", files={"file": ("w.zip", zip_bytes.getvalue())}).json()
    assert summary["runs_frontend_code"] is True and summary["runs_code"] is False
    cl.delete(f"/api/extensions/install/{summary['token']}")
