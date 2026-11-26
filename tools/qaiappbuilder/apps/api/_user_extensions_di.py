# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Composition-root wiring for user extensions (stage 1: chat tools).

Extensions are a side channel (design doc §3.5): they contribute tools to the
SAME chat tool registry every built-in tool lives in, and the agent sees them
as ordinary tools. Nothing in ``qai.chat`` knows extensions exist.

Scan root: ``<data>/extensions/<name>/extension.json``.

Stage-1 tool policy (design doc §8.7 ④) -- the declaration's attributes that
take effect:

* ``group`` (``read``/``edit``/``command``): the tool joins that persona
  permission group, so a persona lacking the group has it disabled exactly
  like a built-in member;
* ``mutating``: recorded only. The one read-only role appbuilder has, the
  ``explore`` sub-agent, is ALLOW-list based (``_EXPLORE_ALLOWED_TOOLS``),
  so every extension tool -- mutating or not -- is already hidden from it;
  no deny-set patch is needed (and the domain profile is left untouched).

Every other declared attribute is reported as unsupported by the host.

Must run AFTER every built-in tool is registered (end of phase 2) so a
built-in name always wins a collision. MCP tools are registered later (by
the lifespan's background ``connect_all``) but always carry the
``mcp__`` prefix, which an extension tool name cannot contain (``__`` is
rejected by the manifest validator), so the two never collide.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.domain.tool_groups import ALL_TOOL_GROUPS, add_group_members
from qai.platform.logging import get_logger
from qai.platform.user_extensions import UserExtensionHost

__all__ = [
    "EXTENSIONS_DIRNAME",
    "RegistryToolSink",
    "build_user_extension_host",
    "wire_user_extensions_into_chat",
]

_LOG = get_logger("apps.api.user_extensions")

#: ``<data>/extensions``.
EXTENSIONS_DIRNAME = "extensions"


class RegistryToolSink:
    """Adapts ``RegistryBackedToolInvocation`` to the host's ``ToolSink``."""

    def __init__(self, tools: Any) -> None:
        self._tools = tools

    def tool_names(self) -> frozenset[str]:
        return frozenset(self._tools.registered_tools)

    def register(
        self,
        name: str,
        handler: Callable[[Any], Awaitable[Any]],
        *,
        schema: Mapping[str, Any] | None,
    ) -> None:
        self._tools.register(name, handler, schema=schema)

    def unregister(self, name: str) -> None:
        self._tools.unregister(name)


def build_user_extension_host(*, tools: Any, data_root: Path) -> UserExtensionHost:
    return UserExtensionHost(data_root / EXTENSIONS_DIRNAME, RegistryToolSink(tools))


def wire_user_extensions_into_chat(*, chat: Any, container: Any) -> UserExtensionHost | None:
    """Load ``<data>/extensions/*`` into the chat tool registry.

    Best-effort: never raises, so a broken extension can never stop startup.
    Returns the host (also stashed on ``container.user_extensions``) or
    ``None`` when the chat registry is not the registry-backed adapter
    (stripped-down test containers).
    """
    tools = getattr(chat, "tools", None)
    if not isinstance(tools, RegistryBackedToolInvocation):
        return None
    try:
        host = build_user_extension_host(tools=tools, data_root=container.data_paths.root)
        host.load_all()
        _apply_tool_policy(host)
    except Exception:  # noqa: BLE001 - extensions must never break startup
        _LOG.warning("user_extensions.wire_failed", exc_info=True)
        return None
    # Stripped-down test containers may refuse the attribute.
    with contextlib.suppress(Exception):
        container.user_extensions = host
    return host


def _apply_tool_policy(host: UserExtensionHost) -> None:
    """Join extension tools to the persona permission groups they declare."""
    if not host.tools:
        return
    for group in ALL_TOOL_GROUPS:
        names = host.tools_of_group(group)
        if names:
            add_group_members(group, names)
