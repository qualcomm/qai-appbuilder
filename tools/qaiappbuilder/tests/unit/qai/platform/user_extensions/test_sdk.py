# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai.ext``: the public extension SDK (design doc §4.4).

The SDK's surface is part of the extension API, so these tests pin it: a
change here must be a deliberate API change (design doc §4.8).
"""

from __future__ import annotations

import inspect
import os
import subprocess
import sys
import textwrap
from dataclasses import fields
from pathlib import Path
from types import SimpleNamespace

import qai.ext as sdk
from qai.platform.user_extensions import host

from .conftest import FakeSecretStore, FakeSink, tool_decl

#: The documented runtime members of ``ctx`` (design doc §4.4).
CTX_MEMBERS = {
    "name",
    "version",
    "root",
    "data_dir",
    "config",
    "settings",
    "logger",
    "secrets",
    "proxy_url",
    "ssl_verify",
    "register_tool",
    "register_action",
    "register_route",
    "register_mcp_server",
    "on_unload",
}


def test_exports_are_exactly_the_documented_ones() -> None:
    assert sorted(sdk.__all__) == [
        "ActionCall",
        "ActionResult",
        "ExtensionContext",
        "ExtensionError",
        "ExtensionSecrets",
        "RouteRequest",
        "RouteResponse",
        "ToolCall",
    ]
    public = {n for n in vars(sdk) if not n.startswith("_") and n != "annotations"}
    assert public == set(sdk.__all__)


def test_exports_are_the_host_types() -> None:
    """The SDK names must be the very classes the host hands to extensions."""
    assert sdk.ExtensionContext is host.ExtensionContext
    assert sdk.ToolCall is host.ToolCall
    assert sdk.ExtensionError is host.ExtensionError
    assert sdk.ExtensionSecrets is host.ExtensionSecrets


def test_tool_call_fields() -> None:
    assert [f.name for f in fields(sdk.ToolCall)] == [
        "arguments",
        "tool_name",
        "conversation_id",
        "tab_id",
    ]


def test_ctx_public_members_match_the_doc() -> None:
    cls = {n for n, _ in inspect.getmembers(sdk.ExtensionContext) if not n.startswith("_")}
    instance_attrs = {"secrets", "logger"}  # set in __init__
    assert cls | instance_attrs == CTX_MEMBERS


def test_secrets_public_methods() -> None:
    public = {n for n, _ in inspect.getmembers(sdk.ExtensionSecrets) if not n.startswith("_")}
    assert public == {"get", "set", "delete", "keys"}


def test_register_tool_signature() -> None:
    sig = inspect.signature(sdk.ExtensionContext.register_tool)
    params = sig.parameters
    assert list(params) == ["self", "name", "handler", "description", "parameters"]
    assert params["description"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["parameters"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["parameters"].default is None


def test_importing_the_sdk_loads_no_app_layer() -> None:
    """``import qai.ext`` must stay cheap: no chat / apps / third-party layers."""
    code = textwrap.dedent(
        """
        import sys
        import qai.ext
        loaded = sorted(m for m in sys.modules if m.split(".")[0] in {"qai", "apps", "interfaces"})
        print("\\n".join(loaded))
        """
    )
    src = str(Path(sdk.__file__).resolve().parents[1])
    env = {"PYTHONPATH": src, "SYSTEMROOT": os.environ.get("SYSTEMROOT", "")}
    out = subprocess.run(  # noqa: S603 - fixed argv, test-only
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, env=env
    ).stdout.split()
    assert set(out) <= {
        "qai",
        "qai.ext",
        "qai.platform",
        "qai.platform.user_extensions",
        "qai.platform.user_extensions.host",
        "qai.platform.user_extensions.installer",
        "qai.platform.user_extensions.manifest",
        "qai.platform.user_extensions.settings",
        "qai.platform.user_extensions.state",
    }, out


async def test_extension_using_the_sdk_loads(make_ext, new_host, sink: FakeSink) -> None:
    code = """
    from qai.ext import ExtensionContext, ExtensionError, ToolCall

    def activate(ctx: ExtensionContext) -> None:
        assert isinstance(ctx, ExtensionContext)

        def lookup(call: ToolCall) -> str:
            assert isinstance(call, ToolCall)
            try:
                ctx.secrets.set("../escape", "x")
            except ExtensionError as exc:
                return "caught: " + type(exc).__name__
            return "not caught"

        ctx.register_tool("demo_lookup", lookup, description="d")
    """
    make_ext("demo-ext", tools=[tool_decl("demo_lookup")], code=code)
    (status,) = new_host(secret_store=FakeSecretStore()).load_all()
    assert status.state == "loaded", status.error
    request = SimpleNamespace(
        arguments={}, tool_name="demo_lookup", conversation_id="c", tab_id="t"
    )
    assert await sink.handlers["demo_lookup"](request) == "caught: ExtensionError"
