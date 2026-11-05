# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""MCP stdio timeouts: handshake cap, full budget for tool calls, no re-run on timeout.

A server registered with a big ``timeout_s`` (wheel installs use 600s) must:

* still fail a *handshake* fast when it is alive but not speaking MCP;
* let a long ``tools/call`` use the whole ``timeout_s``;
* and when a call DOES time out, NOT be run a second time (a retry would repeat
  side effects), while a genuinely dead session is still retried once.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import pytest

from qai.chat.adapters import McpServerRegistry, RegistryBackedToolInvocation
from qai.chat.domain.mcp_server import McpServerConfig
from qai.chat.infrastructure import mcp_client as infra
from qai.chat.infrastructure.mcp_client import (
    McpConnectionError,
    McpTimeoutError,
    McpTransportClient,
)


class _FakeProc:
    """Just the bits of ``asyncio.subprocess.Process`` the stdio client touches."""

    def __init__(self) -> None:
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.stderr.feed_eof()
        self.returncode: int | None = None
        self.written: list[bytes] = []
        outer = self

        class _Stdin:
            def write(self, data: bytes) -> None:
                outer.written.append(data)

            async def drain(self) -> None:
                return None

        self.stdin = _Stdin()


def _client(timeout_s: float) -> tuple[McpTransportClient, _FakeProc]:
    cfg = McpServerConfig(name="srv", command="x", timeout_s=timeout_s)
    c = McpTransportClient(cfg)
    proc = _FakeProc()
    c._proc = proc  # type: ignore[assignment]
    return c, proc


def _reply(proc: _FakeProc, rid: int, result: Any, after: float) -> asyncio.Task:
    async def _go() -> None:
        await asyncio.sleep(after)
        line = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}) + "\n"
        proc.stdout.feed_data(line.encode())

    return asyncio.ensure_future(_go())


# ── transport ───────────────────────────────────────────────────────────────


def test_timeout_error_is_a_connection_error_with_the_old_message() -> None:
    assert issubclass(McpTimeoutError, McpConnectionError)

    async def go() -> None:
        c, _ = _client(0.05)
        with pytest.raises(McpTimeoutError, match=r"timeout after 0\.05s"):
            await c._rpc("tools/call", {"name": "t", "arguments": {}})

    asyncio.run(go())


def test_handshake_requests_are_capped_even_with_a_huge_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(infra, "_STDIO_HANDSHAKE_CAP_S", 0.05)

    async def go() -> float:
        c, _ = _client(600.0)  # the wheel-install default
        t0 = time.monotonic()
        for method in ("initialize", "tools/list", "resources/list", "prompts/list"):
            with pytest.raises(McpTimeoutError):
                await c._rpc(method, {})
        return time.monotonic() - t0

    # If the cap regressed, each request would wait the full 600s; the outer
    # ``wait_for`` turns that into a prompt failure instead of a 40-minute hang.
    async def bounded() -> float:
        return await asyncio.wait_for(go(), timeout=10.0)

    assert asyncio.run(bounded()) < 5.0  # 4 x 0.05s, nowhere near 4 x 600s


def test_tools_call_may_use_the_full_timeout_not_the_handshake_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(infra, "_STDIO_HANDSHAKE_CAP_S", 0.05)

    async def go() -> Any:
        c, proc = _client(5.0)
        task = _reply(proc, 1, {"content": [{"type": "text", "text": "done"}]}, after=0.3)
        try:
            return await c._rpc("tools/call", {"name": "slow", "arguments": {}})
        finally:
            await task

    assert asyncio.run(go())["content"][0]["text"] == "done"  # 6x the handshake cap


def test_the_cap_never_extends_a_shorter_timeout_s() -> None:
    async def go() -> None:
        c, _ = _client(0.05)  # far below the 120s cap -> min() keeps 0.05
        with pytest.raises(McpTimeoutError):
            await c._rpc("initialize", {})

    asyncio.run(go())


# ── pooled call: no re-run on timeout, still retry a dead session ───────────


class _FakeClient:
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


def _registry(tmp_path: Any) -> McpServerRegistry:
    return McpServerRegistry(
        tools=RegistryBackedToolInvocation(),
        config_path=tmp_path / "mcp_servers.json",
        enabled=False,
        secret_store=None,
        ssl_verify=True,
        ssl_verify_provider=None,
        edition="internal",
    )


def _wire(reg: McpServerRegistry, clients: list[_FakeClient]) -> dict[str, int]:
    seen = {"acquire": 0, "close": 0}

    async def acquire(cfg: McpServerConfig, **_: Any) -> _FakeClient:
        seen["acquire"] += 1
        return clients.pop(0)

    async def close(name: str) -> None:
        seen["close"] += 1

    reg._acquire_client = acquire  # type: ignore[method-assign]
    reg._close_pooled = close  # type: ignore[method-assign]
    return seen


def test_a_timed_out_tool_is_not_run_a_second_time(tmp_path: Any) -> None:
    reg = _registry(tmp_path)
    first = _FakeClient(McpTimeoutError("timeout after 600.0s"))
    spare = _FakeClient("SHOULD NOT BE USED")
    seen = _wire(reg, [first, spare])
    cfg = McpServerConfig(name="srv", command="x", timeout_s=600.0)

    with pytest.raises(McpTimeoutError) as ei:
        asyncio.run(reg._pool_call_tool(cfg, "analyse", {}))

    assert first.calls == 1 and spare.calls == 0
    assert seen == {"acquire": 1, "close": 1}  # session reset, but no respawn + re-run
    msg = str(ei.value)
    assert "timeout after 600.0s" in msg and "do not assume it did" in msg


def test_a_dead_session_is_still_retried_exactly_once(tmp_path: Any) -> None:
    reg = _registry(tmp_path)
    dead = _FakeClient(McpConnectionError("server closed connection"))
    fresh = _FakeClient("ok")
    seen = _wire(reg, [dead, fresh])
    cfg = McpServerConfig(name="srv", command="x")

    assert asyncio.run(reg._pool_call_tool(cfg, "t", {})) == "ok"
    assert (dead.calls, fresh.calls) == (1, 1)
    assert seen == {"acquire": 2, "close": 1}


def test_a_second_dead_session_error_propagates(tmp_path: Any) -> None:
    reg = _registry(tmp_path)
    _wire(reg, [_FakeClient(McpConnectionError("a")), _FakeClient(McpConnectionError("b"))])
    with pytest.raises(McpConnectionError, match="b"):
        asyncio.run(reg._pool_call_tool(McpServerConfig(name="srv", command="x"), "t", {}))
