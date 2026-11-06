# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - chat MCP server configuration: add / list / test / enable / remove / catalog (CHAT-08).

``/api/chat/mcp/*`` manages the list of Model Context Protocol servers the chat can use. Execution is gated by
``chat_mcp_enabled`` (default OFF, ``QAI_CHAT__CHAT_MCP_ENABLED``): with the gate off a configured server is stored but never started,
so everything here runs without spawning a process or opening a connection. The gate-ON behaviour and persistence across a
restart are in ``test_chat_mcp_gate.py``.

NOT covered (deliberately):

* **A server that really connects** (tools discovered, resources, prompts, ``tool_count``) - needs a real MCP server process.
* **``headers`` and secret ``env`` values.** The product writes each header VALUE to the OS keyring (service ``chat_mcp``) and keeps
  only a ``__secret__`` sentinel in the file. Writing the user's keyring from a test is ruled out, so NO request here carries
  ``headers``; the masking on ``GET`` is therefore untested. (An earlier exploration run did send two headers and the two entries had
  to be removed by hand - see KNOWN-ISSUES CHAT-ISS-05.)
* **``GET /catalog/browse`` and ``POST /catalog/refresh``**: they query the public MCP registry on the internet.
* **``POST /catalog/{id}/install``** for a real entry (runs ``npx`` / ``pip``), and the OAuth routes.
* ``GET /servers/{name}/resources|prompts`` for a connected server.

Isolation (verified): one throw-away backend with the gate OFF; every server uses a command that does not exist
(``C:\\e2e-nope\\...``) and is never started; ``mcp_servers.json`` lives in the throw-away data dir; the module checks the list of
credential-manager targets is unchanged afterwards.

Observed behaviour worth pinning:

* ``POST /servers`` is an **upsert**: a second POST with the same name replaces the first.
* Two different 400 shapes: field/type problems come in the project envelope (``request.validation`` + ``field_errors``), domain rules
  (name pattern, empty command/url, too many args) come as a bare ``{"detail": "..."}``.
* The name pattern is ``^[A-Za-z0-9][A-Za-z0-9_.\\-]*$``. ``command`` may contain a newline (only C0 controls other than TAB/LF/CR are refused).
* ``POST /servers/{name}/test`` for an UNKNOWN name is a 200 with a made-up row (``command: "_"``), not a 404 (CHAT-ISS-06).
* A custom registry source with a nonsense URL is accepted (201) (CHAT-ISS-07).
* Installing an id that is not curated queries the ONLINE registry before refusing it (8-13 s; CHAT-ISS-10) - so that one test depends on the network being
  reachable or failing fast.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Iterator

import httpx
import pytest

from tests.e2e._owned_backend import OwnedBackend, owned_backend

pytestmark = pytest.mark.e2e

M = "/api/chat/mcp"
NOPE = "C:\\e2e-nope\\never-run.exe"
GATE_OFF = "mcp disabled (chat_mcp_enabled=false)"
STATUS_KEYS = {"name", "transport", "command", "args", "env", "cwd", "url", "headers", "timeout_s", "connected", "tool_count", "tool_names",
               "resource_count", "prompt_count", "enabled", "error"}


def _cred_targets() -> frozenset[str]:
    try:
        out = subprocess.run(["cmdkey", "/list"], capture_output=True, text=True, timeout=30).stdout
    except Exception:  # noqa: BLE001
        return frozenset()
    return frozenset(l.strip() for l in out.splitlines() if l.strip().startswith("Target:"))


_CREDS_AT_START = _cred_targets()


@pytest.fixture(scope="module", autouse=True)
def credential_manager_untouched() -> Iterator[None]:
    yield
    assert _cred_targets() == _CREDS_AT_START, "this module changed the Windows credential manager"


@pytest.fixture(scope="module")
def mcp() -> Iterator[tuple[OwnedBackend, httpx.Client]]:
    with owned_backend() as ob:
        yield ob, ob.start()


@pytest.fixture
def servers(mcp: tuple) -> Iterator[Callable[..., httpx.Response]]:
    """Add servers; remove every one this test created."""
    _ob, cl = mcp
    made: list[str] = []

    def _add(name: str, **over: object) -> httpx.Response:
        body = {"name": name, "transport": "stdio", "command": NOPE, **over}
        r = cl.post(f"{M}/servers", json=body)
        if r.status_code == 200:
            made.append(name)
        return r

    yield _add
    for n in made:
        cl.delete(f"{M}/servers/{n}")


def _names(cl: httpx.Client) -> list[str]:
    return [s["name"] for s in cl.get(f"{M}/servers").json()["servers"]]


def _file(ob: OwnedBackend) -> dict:
    return json.loads((ob.data_dir / "config" / "mcp_servers.json").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# list / add
# --------------------------------------------------------------------------


def test_a_fresh_install_has_no_servers_and_the_gate_off(mcp: tuple) -> None:
    ob, cl = mcp
    assert cl.get(f"{M}/servers").json() == {"servers": [], "enabled": False}
    assert not (ob.data_dir / "config" / "mcp_servers.json").exists(), "nothing is written until something is configured"


def test_adding_a_stdio_server_returns_its_status_row(mcp: tuple, servers: Callable) -> None:
    _ob, cl = mcp
    r = servers("e2e-stdio", args=["--x", "y"], env={"PLAIN": "1"}, cwd="C:\\e2e-nope", timeout_s=12.5)
    assert r.status_code == 200
    row = r.json()
    assert set(row) == STATUS_KEYS
    assert row["name"] == "e2e-stdio" and row["transport"] == "stdio" and row["command"] == NOPE
    assert (row["args"], row["env"], row["cwd"], row["timeout_s"]) == (["--x", "y"], {"PLAIN": "1"}, "C:\\e2e-nope", 12.5)
    assert (row["connected"], row["tool_count"], row["tool_names"], row["enabled"], row["error"]) == (False, 0, [], True, GATE_OFF)
    assert (row["url"], row["headers"], row["resource_count"], row["prompt_count"]) == (None, {}, 0, 0)


def test_adding_a_remote_server_without_headers(mcp: tuple, servers: Callable) -> None:
    _ob, cl = mcp
    r = servers("e2e-remote", transport="http", command=None, url="http://127.0.0.1:9/mcp")
    row = r.json()
    assert r.status_code == 200 and (row["transport"], row["url"], row["command"], row["headers"]) == ("http", "http://127.0.0.1:9/mcp", None, {})
    r2 = servers("e2e-sse", transport="sse", command=None, url="http://127.0.0.1:9/sse")
    assert r2.json()["transport"] == "sse"


def test_the_list_keeps_insertion_order_and_matches_the_add_response(mcp: tuple, servers: Callable) -> None:
    _ob, cl = mcp
    rows = {n: servers(n).json() for n in ("e2e-c", "e2e-a", "e2e-b")}
    assert _names(cl) == ["e2e-c", "e2e-a", "e2e-b"]
    listed = {s["name"]: s for s in cl.get(f"{M}/servers").json()["servers"]}
    assert listed == rows


def test_defaults_when_only_the_required_fields_are_sent(mcp: tuple) -> None:
    _ob, cl = mcp
    r = cl.post(f"{M}/servers", json={"name": "e2e-min", "command": NOPE})
    try:
        row = r.json()
        assert (row["transport"], row["args"], row["env"], row["timeout_s"], row["enabled"]) == ("stdio", [], {}, 30.0, True)
    finally:
        cl.delete(f"{M}/servers/e2e-min")


def test_a_second_post_with_the_same_name_replaces_the_first(mcp: tuple, servers: Callable) -> None:
    _ob, cl = mcp
    servers("e2e-dup", command="C:\\e2e-nope\\one.exe", args=["1"])
    servers("e2e-dup", command="C:\\e2e-nope\\two.exe")
    rows = [s for s in cl.get(f"{M}/servers").json()["servers"] if s["name"] == "e2e-dup"]
    assert len(rows) == 1 and rows[0]["command"] == "C:\\e2e-nope\\two.exe" and rows[0]["args"] == []


def test_replacing_a_server_resets_its_enabled_switch(mcp: tuple, servers: Callable) -> None:
    _ob, cl = mcp
    servers("e2e-reset")
    assert cl.patch(f"{M}/servers/e2e-reset", json={"enabled": False}).json()["enabled"] is False
    assert servers("e2e-reset").json()["enabled"] is True, "re-posting makes it enabled again"


def test_the_config_file_holds_the_servers_in_a_documented_shape(mcp: tuple, servers: Callable) -> None:
    ob, cl = mcp
    servers("e2e-file", args=["a"], env={"K": "v"})
    doc = _file(ob)
    assert set(doc) == {"global_enabled", "servers", "custom_registry_sources"} and doc["global_enabled"] is False
    (entry,) = [s for s in doc["servers"] if s["name"] == "e2e-file"]
    assert entry == {"name": "e2e-file", "transport": "stdio", "command": NOPE, "args": ["a"], "env": {"K": "v"}, "secret_env_keys": [],
                     "cwd": None, "url": None, "headers": {}, "timeout_s": 30.0, "enabled": True}


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"name": "", "command": "x"}, "body.name"),
        ({"name": "a" * 129, "command": "x"}, "body.name"),
        ({"name": "e2e-v", "command": "x", "transport": "ws"}, "body.transport"),
        ({"name": "e2e-v", "command": "x", "timeout_s": 0}, "body.timeout_s"),
        ({"name": "e2e-v", "command": "x", "timeout_s": -1}, "body.timeout_s"),
        ({"name": "e2e-v", "command": "x", "timeout_s": 600.5}, "body.timeout_s"),
        ({"name": "e2e-v", "command": "x", "args": "not-a-list"}, "body.args"),
        ({"name": "e2e-v", "command": "x", "env": {"A": 1}}, "body.env.A"),
        ({"command": "x"}, "body.name"),
        ({"name": "e2e-v", "command": "x" * 4097}, "body.command"),
        ({"name": "e2e-v", "transport": "http", "url": "http://x/" + "a" * 4100}, "body.url"),
    ],
)
def test_field_and_type_errors_are_400s_in_the_project_envelope(mcp: tuple, body: dict, field: str) -> None:
    _ob, cl = mcp
    r = cl.post(f"{M}/servers", json=body)
    assert r.status_code == 400 and r.json()["code"] == "request.validation" and field in r.json()["details"]["field_errors"]
    assert "e2e-v" not in _names(cl)


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ({"name": "a b", "command": "x"}, "validation.string.pattern: name does not match required pattern"),
        ({"name": "-lead", "command": "x"}, "validation.string.pattern: name does not match required pattern"),
        ({"name": "a/b", "command": "x"}, "validation.string.pattern: name does not match required pattern"),
        ({"name": "é", "command": "x"}, "validation.string.pattern: name does not match required pattern"),
        ({"name": "e2e-v", "transport": "stdio"}, "validation.string.empty: command must not be empty"),
        ({"name": "e2e-v", "transport": "stdio", "command": ""}, "validation.string.empty: command must not be empty"),
        ({"name": "e2e-v", "transport": "http"}, "validation.string.empty: url must not be empty"),
        ({"name": "e2e-v", "transport": "sse", "url": ""}, "validation.string.empty: url must not be empty"),
        ({"name": "e2e-v", "command": "x", "args": ["a"] * 129}, "too many args (max 128)"),
        ({"name": "e2e-v", "command": "x", "args": ["a" * 4097]}, "validation.string.max_length: arg length 4097 exceeds maximum 4096"),
        ({"name": "e2e-v", "command": "x", "env": {f"K{i}": "v" for i in range(257)}}, "too many env entries (max 256)"),
    ],
)
def test_domain_rules_are_400s_with_a_bare_detail(mcp: tuple, body: dict, detail: str) -> None:
    _ob, cl = mcp
    r = cl.post(f"{M}/servers", json=body)
    assert r.status_code == 400 and set(r.json()) == {"detail"} and r.json()["detail"].startswith(detail), r.text[:200]
    assert "e2e-v" not in _names(cl)


@pytest.mark.parametrize("name", ["a", "A1", "a.b_c-d", "0lead", "x" * 128])
def test_valid_names_are_accepted(mcp: tuple, servers: Callable, name: str) -> None:
    assert servers(name).status_code == 200


def test_the_documented_limits_are_inclusive(mcp: tuple, servers: Callable) -> None:
    assert servers("e2e-lim", args=["a"] * 128, env={f"K{i}": "v" for i in range(256)}, timeout_s=600).status_code == 200
    assert servers("e2e-lim2", args=["a" * 4096]).status_code == 200


def test_a_newline_in_the_command_is_accepted(mcp: tuple, servers: Callable) -> None:
    """KNOWN GAP / DESIGN QUESTION (current behaviour, pinned) - CHAT-ISS-08: only C0 controls other than TAB/LF/CR are refused, so
    ``"a\\nb"`` is stored as a command. It can never run, but a stdio command is exec'd; a line break there is never meaningful."""
    assert servers("e2e-nl", command="a\nb").json()["command"] == "a\nb"
    assert servers("e2e-nul", command="a\x00b").status_code == 400, "NUL and other C0 controls are refused"


# --------------------------------------------------------------------------
# test / enable / remove
# --------------------------------------------------------------------------


def test_testing_a_server_with_the_gate_off_reports_it_disabled_without_starting_anything(mcp: tuple, servers: Callable) -> None:
    ob, cl = mcp
    servers("e2e-t1")
    row = cl.post(f"{M}/servers/e2e-t1/test").json()
    assert (row["connected"], row["error"], row["tool_count"]) == (False, GATE_OFF, 0) and row["name"] == "e2e-t1"
    assert "spawn_failed" not in ob.log_text(), "nothing was started"


def test_testing_an_unknown_server_is_a_200_with_a_made_up_row(mcp: tuple) -> None:
    """KNOWN GAP (current behaviour, pinned) - CHAT-ISS-06: no 404; the row says ``command: "_"`` for a server that does not exist."""
    _ob, cl = mcp
    r = cl.post(f"{M}/servers/e2e-never-added/test")
    assert r.status_code == 200
    row = r.json()
    assert (row["name"], row["transport"], row["command"], row["connected"]) == ("e2e-never-added", "stdio", "_", False)
    assert "e2e-never-added" not in _names(cl), "and it is not added by the probe"


def test_the_per_server_switch_flips_and_is_stored(mcp: tuple, servers: Callable) -> None:
    ob, cl = mcp
    servers("e2e-sw")
    assert cl.patch(f"{M}/servers/e2e-sw", json={"enabled": False}).json()["enabled"] is False
    assert [s["enabled"] for s in cl.get(f"{M}/servers").json()["servers"] if s["name"] == "e2e-sw"] == [False]
    assert [s["enabled"] for s in _file(ob)["servers"] if s["name"] == "e2e-sw"] == [False]
    assert cl.patch(f"{M}/servers/e2e-sw", json={"enabled": True}).json()["enabled"] is True


def test_the_switch_on_an_unknown_server_is_a_404(mcp: tuple) -> None:
    _ob, cl = mcp
    r = cl.patch(f"{M}/servers/e2e-never-added", json={"enabled": True})
    assert r.status_code == 404 and r.json() == {"detail": "mcp server 'e2e-never-added' not found"}


@pytest.mark.parametrize("body", [{}, {"enabled": "maybe"}, {"enabled": None}, {"enable": True}])
def test_the_switch_body_is_validated(mcp: tuple, servers: Callable, body: dict) -> None:
    _ob, cl = mcp
    servers("e2e-sv")
    r = cl.patch(f"{M}/servers/e2e-sv", json=body)
    assert r.status_code == 400 and "body.enabled" in r.json()["details"]["field_errors"]


def test_removing_a_server(mcp: tuple, servers: Callable) -> None:
    ob, cl = mcp
    servers("e2e-rm")
    r = cl.delete(f"{M}/servers/e2e-rm")
    assert r.status_code == 204 and r.content == b""
    assert "e2e-rm" not in _names(cl) and "e2e-rm" not in [s["name"] for s in _file(ob)["servers"]]
    again = cl.delete(f"{M}/servers/e2e-rm")
    assert again.status_code == 404 and again.json() == {"detail": "mcp server 'e2e-rm' not found"}


def test_resources_and_prompts_of_an_unknown_or_unconnected_server_are_empty_lists(mcp: tuple, servers: Callable) -> None:
    _ob, cl = mcp
    servers("e2e-rp")
    for name in ("e2e-rp", "e2e-never-added"):
        assert cl.get(f"{M}/servers/{name}/resources").json() == {"resources": []}
        assert cl.get(f"{M}/servers/{name}/prompts").json() == {"prompts": []}


def test_the_global_switch_body_is_validated(mcp: tuple) -> None:
    _ob, cl = mcp
    assert "body.enabled" in cl.patch(f"{M}/enabled", json={}).json()["details"]["field_errors"]
    assert cl.patch(f"{M}/enabled", json={"enabled": "maybe"}).status_code == 400


# --------------------------------------------------------------------------
# catalog (local part only)
# --------------------------------------------------------------------------

CURATED = ["filesystem", "memory", "sequential-thinking", "everything", "context7", "playwright", "github", "brave-search", "tavily", "elasticsearch"]


def test_the_curated_catalog_is_listed_locally(mcp: tuple) -> None:
    _ob, cl = mcp
    ents = cl.get(f"{M}/catalog").json()["entries"]
    ids = [e["id"] for e in ents]
    assert ids[: len(CURATED)] == CURATED and len(ids) == len(set(ids))
    assert all(e["source"] == "curated" for e in ents[: len(CURATED)])
    fs = ents[0]
    assert (fs["name"], fs["install_type"], fs["command"]) == ("Filesystem", "npx", "npx")
    assert {"requires_args", "env_required", "headers_required", "secret_fields", "transport", "homepage"} <= set(fs)


def test_installing_an_unknown_catalog_entry_asks_the_online_registry_first(mcp: tuple) -> None:
    """KNOWN GAP (current behaviour, pinned) - CHAT-ISS-10: an id that is not in the curated list is looked up in the ONLINE MCP registry before
    it is refused, so the 400 takes 8-13 s on a good connection and longer offline - an unknown id is answered by the internet, not by the
    local catalog. The client timeout here is 120 s so the answer does not depend on the network speed; only the answer is asserted, never
    how long it took. (The first full-suite run timed out at 30 s on exactly this call.)"""
    import httpx

    _ob, cl = mcp
    slow = httpx.Client(base_url=cl.base_url, headers=dict(cl.headers), cookies=cl.cookies, timeout=httpx.Timeout(120.0))
    try:
        r = slow.post(f"{M}/catalog/nope/install", json={})
    finally:
        slow.close()
    assert r.status_code == 400 and r.json() == {"type": "ValidationError", "code": "mcp.catalog_install_error", "message": "unknown catalog entry 'nope'"}
    assert "nope" not in _names(cl)

def test_installing_an_entry_with_a_missing_placeholder_is_refused_before_anything_runs(mcp: tuple) -> None:
    _ob, cl = mcp
    r = cl.post(f"{M}/catalog/filesystem/install", json={})
    assert r.status_code == 400 and r.json()["code"] == "mcp.catalog_install_error"
    assert "filesystem" not in _names(cl)


def test_the_registry_source_list_has_the_builtin_one_and_it_cannot_be_deleted(mcp: tuple) -> None:
    _ob, cl = mcp
    srcs = cl.get(f"{M}/catalog/sources").json()
    assert [(s["id"], s["builtin"], s["url"]) for s in srcs][:1] == [("registry", True, "https://registry.modelcontextprotocol.io")]
    r = cl.delete(f"{M}/catalog/sources/registry")
    assert r.status_code == 404 and r.json() == {"detail": "registry source 'registry' not found"}
    assert cl.get(f"{M}/catalog/sources").json() == srcs


def test_a_custom_registry_source_can_be_added_and_removed(mcp: tuple) -> None:
    _ob, cl = mcp
    r = cl.post(f"{M}/catalog/sources", json={"name": "e2e-src", "url": "https://registry.example.invalid"})
    assert r.status_code == 201
    src = r.json()
    assert src["builtin"] is False and src["id"].startswith("custom:") and (src["name"], src["url"]) == ("e2e-src", "https://registry.example.invalid")
    assert src["id"] in [s["id"] for s in cl.get(f"{M}/catalog/sources").json()]
    d = cl.delete(f"{M}/catalog/sources/{src['id']}")
    assert d.status_code in (200, 204)
    assert src["id"] not in [s["id"] for s in cl.get(f"{M}/catalog/sources").json()]
    assert cl.delete(f"{M}/catalog/sources/{src['id']}").status_code == 404


def test_a_nonsense_registry_url_is_accepted(mcp: tuple) -> None:
    """KNOWN GAP (current behaviour, pinned) - CHAT-ISS-07: ``"not a url"`` is stored and listed; the first browse of it fails later."""
    _ob, cl = mcp
    r = cl.post(f"{M}/catalog/sources", json={"name": "e2e-bad", "url": "not a url"})
    try:
        assert r.status_code == 201 and r.json()["url"] == "not a url"
    finally:
        cl.delete(f"{M}/catalog/sources/{r.json()['id']}")


def test_deleting_an_unknown_registry_source_is_a_404(mcp: tuple) -> None:
    _ob, cl = mcp
    r = cl.delete(f"{M}/catalog/sources/nope")
    assert r.status_code == 404 and r.json() == {"detail": "registry source 'nope' not found"}
