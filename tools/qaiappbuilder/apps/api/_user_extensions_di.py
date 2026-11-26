# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Composition-root wiring for user extensions (chat tools + skills).

Extensions are a side channel (design doc §4.1): they contribute tools to the
SAME chat tool registry every built-in tool lives in, and skills to the SAME
skill scan the Skills page, the skill catalog in the system prompt and the
``skill`` tool all read. Nothing in ``qai.chat`` knows extensions exist.

Scan root: ``<data>/extensions/<name>/extension.json``; per-extension data:
``<data>/extensions-data/<name>/``. The extension API is defined in design
doc §10.0; this module only supplies the app services it exposes (secret
store, live proxy URL, live TLS-verification setting).

Tools -- ``tools[].group`` (``read``/``edit``/``command``) makes the tool join
that persona permission group, so a persona lacking the group has it
disabled exactly like a built-in member. The one read-only role appbuilder
has, the ``explore`` sub-agent, is ALLOW-list based
(``_EXPLORE_ALLOWED_TOOLS``), so every extension tool is hidden from it.

Must run AFTER every built-in tool is registered (end of phase 2) so a
built-in name always wins a collision. MCP tools are registered later (by
the lifespan's background ``connect_all``) and their names always contain
``__`` (``server__tool`` or ``mcp__server__tool``), which an extension
tool name cannot contain (rejected by the manifest validator), so the two
never collide.

Skills -- ``<ext>/<skills.dir>/<id>/SKILL.md`` (design doc §4.2):

* order is user > built-in > extension (as in QAIModelBuilder): the
  extension roots are appended to ``user_prefs.skill_discovery`` through
  :func:`extension_skill_roots`, so an app skill always wins;
* a skill id already used by an app skill or an earlier extension is
  REFUSED (excluded from the scan) with a reason, never shadowed silently;
* every extension skill carries ``source = "extension:<name>"``;
* ``skills.default_modes`` feeds :func:`resolve_skill_mode` after the
  built-in default table.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.domain.tool_groups import ALL_TOOL_GROUPS, add_group_members
from qai.platform.logging import get_logger
from qai.platform.skills import (
    ContributedSkillRoot,
    SkillDiscovery,
    set_contributed_default_modes,
)
from qai.platform.user_extensions import UserExtensionHost

from ._global_proxy import build_global_proxy_provider, build_ssl_verify_provider

__all__ = [
    "EXTENSIONS_DIRNAME",
    "RegistryToolSink",
    "build_user_extension_host",
    "extension_skill_roots",
    "wire_user_extensions_into_chat",
]

_LOG = get_logger("apps.api.user_extensions")

#: ``<data>/extensions``.
EXTENSIONS_DIRNAME = "extensions"
#: ``SkillInfo.source`` prefix for extension-provided skills.
SKILL_SOURCE_PREFIX = "extension:"


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


def build_user_extension_host(
    *,
    tools: Any,
    data_root: Path,
    app_skill_dirs: list[Path] | None = None,
    secret_store: Any = None,
    proxy_url: Callable[[], str | None] | None = None,
    ssl_verify: Callable[[], bool] | None = None,
) -> UserExtensionHost:
    """``app_skill_dirs``: the app's own skill roots; their ids are taken."""
    dirs = list(app_skill_dirs or [])

    def _taken_skill_ids() -> frozenset[str]:
        if not dirs:
            return frozenset()
        return frozenset(s.skill_id for s in SkillDiscovery(dirs).scan())

    return UserExtensionHost(
        data_root / EXTENSIONS_DIRNAME,
        RegistryToolSink(tools),
        data_root=data_root,
        taken_skill_ids=_taken_skill_ids,
        secret_store=secret_store,
        proxy_url=proxy_url,
        ssl_verify=ssl_verify,
    )


def wire_user_extensions_into_chat(*, chat: Any, container: Any) -> UserExtensionHost | None:
    """Load ``<data>/extensions/*`` into the chat tool registry + skill scan.

    Best-effort: never raises, so a broken extension can never stop startup.
    Returns the host (also stashed on ``container.user_extensions``) or
    ``None`` when the chat registry is not the registry-backed adapter
    (stripped-down test containers).
    """
    tools = getattr(chat, "tools", None)
    if not isinstance(tools, RegistryBackedToolInvocation):
        return None
    try:
        host = build_user_extension_host(
            tools=tools,
            data_root=container.data_paths.root,
            app_skill_dirs=_app_skill_dirs(container),
            secret_store=getattr(container, "secret_store", None),
            proxy_url=_live_provider(container, build_global_proxy_provider),
            ssl_verify=_live_provider(container, build_ssl_verify_provider),
        )
    except Exception:  # noqa: BLE001 - extensions must never break startup
        _LOG.warning("user_extensions.wire_failed", exc_info=True)
        return None
    try:
        host.load_all()
        _apply_tool_policy(host)
        set_contributed_default_modes(host.skill_default_modes)
    except Exception:  # noqa: BLE001 - extensions must never break startup
        # Never leave tools registered without their permission group.
        _LOG.warning("user_extensions.wire_failed", exc_info=True)
        with contextlib.suppress(Exception):
            host.unload_all()
        return None
    # Stripped-down test containers may refuse the attribute.
    with contextlib.suppress(Exception):
        container.user_extensions = host
    return host


def extension_skill_roots(container: Any) -> list[ContributedSkillRoot]:
    """The loaded extensions' skill roots, for ``SkillDiscovery``.

    Read on every scan; empty until the host is wired (``user_prefs`` is built
    earlier in phase 2) and for containers without one.
    """
    host = getattr(container, "user_extensions", None)
    if host is None:
        return []
    return [
        ContributedSkillRoot(
            path=root.path,
            source=SKILL_SOURCE_PREFIX + root.extension,
            excluded=root.excluded,
            only=frozenset(root.skill_ids),
        )
        for root in host.skill_roots()
    ]


def _live_provider(
    container: Any, factory: Callable[[Any], Callable[[], Any]]
) -> Callable[[], Any] | None:
    """The app's live proxy / TLS provider; ``None`` (host default) for bare containers."""
    if getattr(container, "settings", None) is None:
        return None
    return factory(container)


def _app_skill_dirs(container: Any) -> list[Path]:
    user_prefs = getattr(container, "user_prefs", None)
    discovery = getattr(user_prefs, "skill_discovery", None)
    dirs = getattr(discovery, "skills_dirs", None)
    return list(dirs) if isinstance(dirs, list) else []


def _apply_tool_policy(host: UserExtensionHost) -> None:
    """Join extension tools to the persona permission groups they declare."""
    if not host.tools:
        return
    for group in ALL_TOOL_GROUPS:
        names = host.tools_of_group(group)
        if names:
            add_group_members(group, names)
