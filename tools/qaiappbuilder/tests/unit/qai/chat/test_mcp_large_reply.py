# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""A stdio MCP reply is ONE JSON line; replies over asyncio's 64 KiB default died.

Real incident (2026-11): ``sm8975__get_skill`` returned a ~75 KB reply line;
``StreamReader.readline`` raised ``ValueError: Separator is found, but chunk is
longer than limit``, the tool call failed, and the model was shown "None". These
tests use a REAL child process (a tiny stdio MCP server) so the actual pipe limit
is exercised, not a fake.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from qai.chat.adapters import McpServerRegistry, RegistryBackedToolInvocation
from qai.chat.domain.mcp_server import McpServerConfig
from qai.chat.infrastructure import mcp_client as infra
from qai.chat.infrastructure.mcp_client import (
    McpConnectionError,
    McpResponseTooLargeError,
    McpTransportClient,
)

_SERVER = textwrap.dedent(
    """
    import json, sys

    def send(obj):
        sys.stdout.write(json.dumps(obj) + "\\n")
        sys.stdout.flush()

    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, rid = msg.get("method"), msg.get("id")
        if rid is None:
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                "serverInfo": {"name": "big", "version": "1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [{
                "name": "blob", "description": "d",
                "inputSchema": {"type": "object", "properties": {"n": {"type": "integer"}}}}]}})
        elif method == "tools/call":
            n = int((msg["params"].get("arguments") or {}).get("n", 0))
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": "x" * n}]}})
        else:
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
    """
)


def _server_cfg(tmp_path: Path) -> McpServerConfig:
    script = tmp_path / "big_server.py"
    script.write_text(_SERVER, encoding="utf-8")
    return McpServerConfig(name="big", command=sys.executable, args=(str(script),), timeout_s=60)


def _run(coro: Any, timeout: float = 90.0) -> Any:
    # Outer bound: a regression must fail promptly, never hang the suite.
    async def bounded() -> Any:
        return await asyncio.wait_for(coro, timeout=timeout)

    return asyncio.run(bounded())


def test_a_reply_far_over_64_kib_is_read_in_full(tmp_path: Path) -> None:
    async def go() -> tuple[int, int]:
        c = McpTransportClient(_server_cfg(tmp_path))
        await c.connect()
        try:
            small = await c.call_tool("blob", {"n": 1_000})
            big = await c.call_tool("blob", {"n": 300_000})  # ~4.6x the old limit
            return len(small), len(big)
        finally:
            await c.aclose()

    assert _run(go()) == (1_000, 300_000)


def test_the_session_keeps_working_after_a_big_reply(tmp_path: Path) -> None:
    async def go() -> list[int]:
        c = McpTransportClient(_server_cfg(tmp_path))
        await c.connect()
        try:
            return [len(await c.call_tool("blob", {"n": n})) for n in (200_000, 5, 150_000, 7)]
        finally:
            await c.aclose()

    assert _run(go()) == [200_000, 5, 150_000, 7]


def test_a_reply_beyond_the_hard_limit_fails_clearly_and_closes_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(infra, "_MAX_LINE_BYTES", 100_000)  # keep the test small

    async def go() -> tuple[str, Any]:
        c = McpTransportClient(_server_cfg(tmp_path))
        await c.connect()
        try:
            with pytest.raises(McpResponseTooLargeError) as ei:
                await c.call_tool("blob", {"n": 400_000})
            return str(ei.value), c._proc
        finally:
            await c.aclose()

    msg, proc = _run(go())
    assert "larger than" in msg and "tool DID run" in msg
    assert proc is None  # session closed: the unread tail of the line is gone with it


def test_too_large_is_a_connection_error_but_a_distinct_one() -> None:
    assert issubclass(McpResponseTooLargeError, McpConnectionError)


# ── pooled call: no retry, because the tool already ran ─────────────────────


class _Client:
    def __init__(self, outcome: Any) -> None:
        self._outcome = outcome
        self.calls = 0

    def is_alive(self) -> bool:
        return True

    async def call_tool(self, name: str, args: dict[str, Any]) -> str:
        self.calls += 1
        if isinstance(self._outcome, BaseException):
            raise self._outcome
        return self._outcome

    async def aclose(self) -> None:
        return None


def _registry(tmp_path: Path) -> McpServerRegistry:
    return McpServerRegistry(
        tools=RegistryBackedToolInvocation(),
        config_path=tmp_path / "mcp_servers.json",
        enabled=False,
        secret_store=None,
        ssl_verify=True,
        ssl_verify_provider=None,
        edition="internal",
    )


def _wire(reg: McpServerRegistry, clients: list[_Client]) -> dict[str, int]:
    seen = {"acquire": 0, "close": 0}

    async def acquire(cfg: McpServerConfig, **_: Any) -> _Client:
        seen["acquire"] += 1
        return clients.pop(0)

    async def close(name: str) -> None:
        seen["close"] += 1

    reg._acquire_client = acquire  # type: ignore[method-assign]
    reg._close_pooled = close  # type: ignore[method-assign]
    return seen


def test_a_too_large_reply_is_not_retried(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    first = _Client(McpResponseTooLargeError("reply too large"))
    spare = _Client("SHOULD NOT BE USED")
    seen = _wire(reg, [first, spare])

    with pytest.raises(McpResponseTooLargeError):
        _run(reg._pool_call_tool(McpServerConfig(name="s", command="x"), "t", {}), timeout=10)

    assert (first.calls, spare.calls) == (1, 0)
    assert seen == {"acquire": 1, "close": 1}


def test_other_connection_errors_are_still_retried_once(tmp_path: Path) -> None:
    reg = _registry(tmp_path)
    dead, fresh = _Client(McpConnectionError("server closed connection")), _Client("ok")
    seen = _wire(reg, [dead, fresh])
    assert _run(reg._pool_call_tool(McpServerConfig(name="s", command="x"), "t", {}), timeout=10) == "ok"
    assert (dead.calls, fresh.calls) == (1, 1) and seen["acquire"] == 2
