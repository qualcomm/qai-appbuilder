# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Fixtures: write throw-away extensions under ``tmp_path``."""

from __future__ import annotations

import json
import sys
import textwrap
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest


def _manifest(name: str, tools: list[dict[str, Any]] | None, **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "api_version": 2,
        "description": {"en": f"{name} test extension"},
    }
    if tools is not None:
        data["python_package"] = "qai_ext_" + name.replace("-", "_")
        data["contributes"] = {"tools": tools}
    data.update(extra)
    return data


def tool_decl(name: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "group": "read", "result_category": "query", **extra}


ECHO_BODY = """
def wire_into_chat(ctx, services):
    async def _h(request):
        return "echo:" + request.arguments.get("text", "")
    ctx.register_chat_tool(
        {tool!r},
        _h,
        schema={{"type": "function", "function": {{"name": {tool!r},
            "description": "echo", "parameters": {{"type": "object"}}}}}},
    )
"""


@pytest.fixture
def make_ext(tmp_path: Path) -> Iterator[Callable[..., Path]]:
    """``make_ext(name, tools=[...], code="...")`` -> the extension dir."""
    root = tmp_path / "extensions"
    root.mkdir()
    packages: list[str] = []

    def _make(
        name: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        code: str | None = None,
        manifest: dict[str, Any] | None = None,
        **extra: Any,
    ) -> Path:
        ext = root / name
        ext.mkdir()
        data = manifest if manifest is not None else _manifest(name, tools, **extra)
        (ext / "extension.json").write_text(json.dumps(data), encoding="utf-8")
        if code is not None:
            package = "qai_ext_" + name.replace("-", "_")
            pkg = ext / "python" / package
            pkg.mkdir(parents=True)
            (pkg / "__init__.py").write_text(textwrap.dedent(code), encoding="utf-8")
            packages.append(package)
        return ext

    _make.root = root  # type: ignore[attr-defined]
    yield _make
    for package in packages:
        for mod in [m for m in sys.modules if m == package or m.startswith(package + ".")]:
            sys.modules.pop(mod, None)


class FakeSink:
    """In-memory ``ToolSink`` with pre-taken (built-in) names."""

    def __init__(self, builtin: tuple[str, ...] = ("read", "exec")) -> None:
        self.handlers: dict[str, Any] = {n: object() for n in builtin}
        self.schemas: dict[str, Any] = {}

    def tool_names(self) -> frozenset[str]:
        return frozenset(self.handlers)

    def register(self, name: str, handler: Any, *, schema: Any) -> None:
        self.handlers[name] = handler
        if schema is not None:
            self.schemas[name] = schema

    def unregister(self, name: str) -> None:
        self.handlers.pop(name, None)
        self.schemas.pop(name, None)


@pytest.fixture
def sink() -> FakeSink:
    return FakeSink()
