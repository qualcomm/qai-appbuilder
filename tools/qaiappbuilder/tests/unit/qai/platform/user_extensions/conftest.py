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

from qai.platform.user_extensions import UserExtensionHost, module_name_for


def manifest(name: str = "demo-ext", **extra: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "manifest_version": 1,
        "name": name,
        "version": "1.0.0",
        "description": f"{name} test extension",
    }
    data.update(extra)
    return data


def tool_decl(name: str, group: str | None = "read") -> dict[str, Any]:
    return {"name": name, "group": group}


ECHO_BODY = """
def activate(ctx):
    async def _h(call):
        return "echo:" + call.arguments.get("text", "")
    params = {{"type": "object", "properties": {{"text": {{"type": "string"}}}}}}
    ctx.register_tool({tool!r}, _h, description="echo", parameters=params)
"""


@pytest.fixture
def make_ext(tmp_path: Path) -> Iterator[Callable[..., Path]]:
    """``make_ext(name, tools=[...], code="...")`` -> the extension dir.

    ``code`` becomes ``main.py`` and ``entry`` is set to it (unless the
    manifest is passed whole).
    """
    root = tmp_path / "extensions"
    root.mkdir()
    names: list[str] = []

    def _make(
        name: str,
        *,
        tools: list[dict[str, Any]] | None = None,
        code: str | None = None,
        manifest_data: dict[str, Any] | None = None,
        **extra: Any,
    ) -> Path:
        ext = root / name
        ext.mkdir()
        if manifest_data is None:
            fields: dict[str, Any] = dict(extra)
            if code is not None:
                fields.setdefault("entry", "main.py")
            if tools is not None:
                fields["tools"] = tools
            manifest_data = manifest(name, **fields)
        (ext / "extension.json").write_text(json.dumps(manifest_data), encoding="utf-8")
        if code is not None:
            (ext / "main.py").write_text(textwrap.dedent(code), encoding="utf-8")
        names.append(name)
        return ext

    _make.root = root  # type: ignore[attr-defined]
    yield _make
    for name in names:
        module = module_name_for(name)
        for mod in [m for m in sys.modules if m == module or m.startswith(module + ".")]:
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

    def handler_for(self, name: str) -> Any:
        return self.handlers.get(name)


class FakeSecretStore:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], str] = {}

    def get(self, service: str, key: str) -> str:
        return self.data[(service, key)]

    def set(self, service: str, key: str, value: str) -> None:
        self.data[(service, key)] = value

    def delete(self, service: str, key: str) -> None:
        del self.data[(service, key)]

    def list_keys(self, service: str) -> list[str]:
        return [k for s, k in self.data if s == service]

    def exists(self, service: str, key: str) -> bool:
        return (service, key) in self.data


@pytest.fixture
def sink() -> FakeSink:
    return FakeSink()


@pytest.fixture
def new_host(make_ext, sink: FakeSink, tmp_path: Path) -> Callable[..., UserExtensionHost]:
    """``new_host(**kwargs)``: a host over ``make_ext.root`` with data under ``tmp_path/data``."""

    def _new(**kwargs: Any) -> UserExtensionHost:
        kwargs.setdefault("data_root", tmp_path / "data")
        return UserExtensionHost(make_ext.root, kwargs.pop("sink", sink), **kwargs)

    return _new
