# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""A tiny stdio MCP server (stdlib only) for tests.

Tools:
* ``echo(text)``   -> ``"<MINI_MCP_PREFIX><text>"``
* ``env(name)``    -> the value of an environment variable (``"<unset>"``)
* ``cwd()``        -> the working directory

Speaks newline-delimited JSON-RPC 2.0 on stdin / stdout, enough of MCP for
``initialize`` / ``tools/list`` / ``tools/call``; every other request gets a
method-not-found error (so resources / prompts are simply absent).
"""

from __future__ import annotations

import json
import os
import sys

_TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
    },
    {
        "name": "env",
        "description": "Read an environment variable",
        "inputSchema": {"type": "object", "properties": {"name": {"type": "string"}}},
    },
    {"name": "cwd", "description": "Working directory", "inputSchema": {"type": "object"}},
]


def _result(rid: object, result: object) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _text(value: str) -> dict:
    return {"content": [{"type": "text", "text": value}]}


def _handle(msg: dict) -> dict | None:  # noqa: PLR0911 - one reply per method
    rid, method, params = msg.get("id"), msg.get("method"), msg.get("params") or {}
    if rid is None:
        return None  # notification
    if method == "initialize":
        return _result(
            rid,
            {
                "protocolVersion": params.get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mini", "version": "1"},
            },
        )
    if method == "tools/list":
        return _result(rid, {"tools": _TOOLS})
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name == "echo":
            return _result(
                rid, _text(os.environ.get("MINI_MCP_PREFIX", "") + str(args.get("text", "")))
            )
        if name == "env":
            return _result(rid, _text(os.environ.get(str(args.get("name")), "<unset>")))
        if name == "cwd":
            return _result(rid, _text(os.getcwd()))
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "method not found"}}


def main() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        reply = _handle(json.loads(line))
        if reply is not None:
            sys.stdout.write(json.dumps(reply) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
