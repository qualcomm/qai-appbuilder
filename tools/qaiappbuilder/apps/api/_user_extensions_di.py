# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Composition-root wiring for user extensions (chat tools + skills).

Extensions are a side channel (design doc §5.1): they contribute tools to the
SAME chat tool registry every built-in tool lives in, and skills to the SAME
skill scan the Skills page, the skill catalog in the system prompt and the
``skill`` tool all read. Nothing in ``qai.chat`` knows extensions exist.

Scan root: ``<data>/extensions/<name>/extension.json``; per-extension data:
``<data>/extensions-data/<name>/``. The extension API is defined in design
doc §4; this module only supplies the app services it exposes (secret
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

Skills -- ``<ext>/<skills.dir>/<id>/SKILL.md`` (design doc §5.2, §9.3):

* order is user > built-in > extension: the
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
import threading
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, TypeVar

from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import PromptAddenda
from qai.chat.application.use_cases.tool_advertise import TOOL_ORDER
from qai.chat.domain.mcp_server import McpServerConfig, McpTransport
from qai.chat.domain.tool_groups import add_group_members, remove_group_members
from qai.platform.logging import get_logger
from qai.platform.skills import (
    ContributedSkillRoot,
    SkillDiscovery,
    set_contributed_default_modes,
)
from qai.platform.user_extensions import (
    STATE_FILENAME,
    BuiltinExtensionError,
    ExtensionInstaller,
    ExtensionStateStore,
    ExtensionStatus,
    UnknownExtensionError,
    UserExtensionHost,
    discover_extension_dirs,
    settings_view,
    update_settings,
)

from ._global_proxy import build_global_proxy_provider, build_ssl_verify_provider

__all__ = [
    "BUILTIN_EXTENSIONS_DIR",
    "EXTENSIONS_DIRNAME",
    "RESERVED_TOOL_NAMES",
    "RegistryToolSink",
    "UserExtensionManager",
    "build_user_extension_host",
    "builtin_extensions_root",
    "extension_skill_roots",
    "prompt_addenda_for",
    "sync_extension_mcp",
    "wire_user_extensions_into_chat",
]

_LOG = get_logger("apps.api.user_extensions")
_T = TypeVar("_T")

#: ``<data>/extensions``.
EXTENSIONS_DIRNAME = "extensions"
#: ``<repo_root>/factory/extensions``: built-in extensions (design doc 4.16).
BUILTIN_EXTENSIONS_DIR = Path("factory") / "extensions"
#: ``SkillInfo.source`` prefix for extension-provided skills.
SKILL_SOURCE_PREFIX = "extension:"
#: ``McpServerStatus.source`` prefix for extension-provided MCP servers.
MCP_SOURCE_PREFIX = "extension:"

#: Built-in tool names an extension may never take, even when the built-in
#: is not registered right now (``computer`` / ``browser`` disabled, ``agent``
#: failed to build, ...). ``TOOL_ORDER`` lists every built-in tool name.
RESERVED_TOOL_NAMES: frozenset[str] = frozenset(TOOL_ORDER)


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

    def handler_for(self, name: str) -> Any:
        return self._tools.handler_for(name)


def build_user_extension_host(
    *,
    tools: Any,
    data_root: Path,
    builtin_root: Path | None = None,
    app_skill_dirs: list[Path] | None = None,
    state: ExtensionStateStore | None = None,
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
        builtin_root=builtin_root,
        taken_skill_ids=_taken_skill_ids,
        reserved_tool_names=RESERVED_TOOL_NAMES,
        on_tool_added=_join_group,
        on_tool_removed=_leave_groups,
        is_disabled=state.is_disabled if state is not None else None,
        stored_settings=state.settings if state is not None else None,
        secret_store=secret_store,
        proxy_url=proxy_url,
        ssl_verify=ssl_verify,
    )


def _join_group(name: str, group: str | None) -> None:
    """Host hook, BEFORE a tool is registered: join its permission group."""
    if group is not None:
        add_group_members(group, frozenset({name}))


def _leave_groups(name: str) -> None:
    """Host hook, AFTER a tool is unregistered: leave every group (built-ins stay)."""
    remove_group_members(frozenset({name}))


class UserExtensionManager:
    """Runtime management of the loaded extensions (list / reload / unload).

    Owns the one lock that serialises every host mutation. Permission groups
    follow each tool through the host's hooks (joined before it is registered,
    left after it is unregistered), so a tool is never callable outside its
    group. After each operation the skills' default-mode provider is re-bound;
    skills themselves need no re-sync (the scan reads the host's roots).

    Operations are synchronous (extension ``activate`` / ``on_unload`` code is
    plain Python); HTTP handlers and shutdown run them in a worker thread.
    Reads (``statuses``) never take the lock, so they never wait for a reload.
    """

    def __init__(self, host: UserExtensionHost, state: ExtensionStateStore | None = None) -> None:
        self._host = host
        self._state = state
        self._lock = threading.Lock()
        #: Async follow-up after a change (MCP servers live on the event loop).
        #: Bound by the composition root; awaited by :meth:`sync_runtime`.
        self.on_runtime_sync: Callable[[], Awaitable[None]] | None = None

    async def sync_runtime(self) -> None:
        """Apply the current extension state to the async subsystems (MCP).

        Called on the event loop after each (threaded) operation. Never raises.
        """
        if self.on_runtime_sync is None:
            return
        try:
            await self.on_runtime_sync()
        except Exception:  # noqa: BLE001 - never fail the management call
            _LOG.warning("user_extensions.runtime_sync_failed", exc_info=True)

    @property
    def host(self) -> UserExtensionHost:
        return self._host

    @property
    def state(self) -> ExtensionStateStore | None:
        return self._state

    @property
    def statuses(self) -> tuple[ExtensionStatus, ...]:
        return self._host.statuses

    def load_all(self) -> tuple[ExtensionStatus, ...]:
        return self._change(self._host.load_all)

    def reload_all(self) -> tuple[ExtensionStatus, ...]:
        """Unload every extension and scan the directory again."""
        return self._change(self._host.reload_all)

    def reload(self, name: str) -> ExtensionStatus:
        """Reload one extension. :class:`UnknownExtensionError` for an unknown name."""
        return self._change(lambda: self._host.reload(name))

    def set_enabled(self, name: str, enabled: bool) -> ExtensionStatus:
        """Persist the switch, then reload: disabling unloads it, enabling loads it."""

        def op() -> ExtensionStatus:
            self._host.reload(name)  # validates the name before anything is written
            self._require_state().set_disabled(name, not enabled)
            return self._host.reload(name)

        return self._change(op)

    # -- install (design doc §7.2) ----------------------------------------------

    @property
    def installer(self) -> ExtensionInstaller:
        return ExtensionInstaller(self._host.scan_root)

    def stage_zip(self, archive: Any) -> dict[str, Any]:
        """Unpack + validate an archive; returns the capability summary. Runs no code."""
        return self.installer.stage_zip(archive).summary()

    def stage_files(self, entries: Any) -> dict[str, Any]:
        """Validate a folder the browser uploaded as files. Runs no code."""
        return self.installer.stage_files(entries).summary()

    def stage_folder(self, folder: Path) -> dict[str, Any]:
        """Copy + validate a local folder; returns the capability summary. Runs no code."""
        return self.installer.stage_folder(folder).summary()

    def discard_staged(self, token: str) -> bool:
        return self.installer.discard(token)

    def install(self, token: str, *, replace: bool = False) -> ExtensionStatus:
        """Move a staged extension into place and load it (takes effect at once).

        An installed extension of the same name is unloaded first and replaced
        only with ``replace``; its data directory, secrets, settings and
        on/off switch are kept (an update, not a reinstall).
        """

        def op() -> ExtensionStatus:
            installer = self.installer
            name = installer.staged(token).manifest.name
            if self._host.is_builtin(name):
                raise BuiltinExtensionError(name)
            if replace:
                self._host.unload(name)
            installer.commit(token, replace=replace)
            return self._host.reload(name)

        return self._change(op)

    def unload(self, name: str) -> None:
        """Unload one extension (kept on disk; it comes back on the next reload).
        :class:`UnknownExtensionError` when there is no such extension."""

        def op() -> None:
            if not self._host.unload(name) and name not in {
                p.name for p in discover_extension_dirs(self._host.scan_root)
            }:
                raise UnknownExtensionError(name)

        self._change(op)

    def remove(self, name: str, *, purge_data: bool = False) -> None:
        """Uninstall: unload, delete the directory; with ``purge_data`` also its
        data directory, secrets and stored settings / switch."""

        def op() -> None:
            self._host.remove(name, purge_data=purge_data)
            if purge_data and self._state is not None:
                self._state.forget(name)

        self._change(op)

    def settings(self, name: str) -> list[dict[str, Any]]:
        """The settings card: every declared setting with its value (secrets:
        only whether they are set). Works for a disabled extension too."""
        manifest = self._host.manifest(name)
        return settings_view(manifest, self._require_state(), self._host.secrets_of(name))

    def update_settings(self, name: str, values: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Validate and save (all or nothing). Takes effect at once: ``ctx.settings``
        and ``ctx.secrets`` read live, so no reload is needed.
        :class:`SettingsValidationError` lists every bad field."""
        with self._lock:
            manifest = self._host.manifest(name)
            state, secrets = self._require_state(), self._host.secrets_of(name)
            update_settings(manifest, state, secrets, values)
            # MCP server configs embed settings / secrets: re-resolve them.
            self._host.refresh_mcp(name)
            return settings_view(manifest, state, secrets)

    def _require_state(self) -> ExtensionStateStore:
        if self._state is None:
            raise RuntimeError("extension switches are not persisted in this host")
        return self._state

    def unload_all(self, *, timeout: float | None = None) -> bool:
        """Unload everything. With ``timeout``, give up waiting for a running
        reload after that many seconds and return ``False`` (shutdown)."""
        if not self._lock.acquire(timeout=-1 if timeout is None else timeout):
            return False
        try:
            self._host.unload_all()
        finally:
            self._after_change()
            self._lock.release()
        return True

    def _change(self, op: Callable[[], _T]) -> _T:
        with self._lock:
            try:
                return op()
            finally:
                self._after_change()

    def _after_change(self) -> None:
        set_contributed_default_modes(self._host.skill_default_modes)


def wire_user_extensions_into_chat(*, chat: Any, container: Any) -> UserExtensionManager | None:
    """Load ``<data>/extensions/*`` into the chat tool registry + skill scan.

    Best-effort: never raises, so a broken extension can never stop startup.
    Returns the manager (also stashed on ``container.user_extensions``) or
    ``None`` when the chat registry is not the registry-backed adapter
    (stripped-down test containers).
    """
    tools = getattr(chat, "tools", None)
    if not isinstance(tools, RegistryBackedToolInvocation):
        return None
    try:
        data_root = container.data_paths.root
        state = ExtensionStateStore(data_root / "config" / STATE_FILENAME)
        host = build_user_extension_host(
            tools=tools,
            data_root=data_root,
            builtin_root=builtin_extensions_root(container),
            app_skill_dirs=_app_skill_dirs(container),
            state=state,
            secret_store=getattr(container, "secret_store", None),
            proxy_url=_live_provider(container, build_global_proxy_provider),
            ssl_verify=_live_provider(container, build_ssl_verify_provider),
        )
        manager = UserExtensionManager(host, state)
    except Exception:  # noqa: BLE001 - extensions must never break startup
        _LOG.warning("user_extensions.wire_failed", exc_info=True)
        return None
    try:
        manager.load_all()
    except Exception:  # noqa: BLE001 - extensions must never break startup
        # Never leave tools registered without their permission group.
        _LOG.warning("user_extensions.wire_failed", exc_info=True)
        with contextlib.suppress(Exception):
            manager.unload_all()
        return None
    manager.on_runtime_sync = lambda: sync_extension_mcp(container)
    # Stripped-down test containers may refuse the attribute.
    with contextlib.suppress(Exception):
        container.user_extensions = manager
    return manager


def builtin_extensions_root(container: Any) -> Path | None:
    """``<repo_root>/factory/extensions``: the extensions shipped with the app
    (read in place, packaged with ``factory/``). ``None`` when the container
    has no repo root (stripped-down test containers)."""
    repo_root = getattr(container, "repo_root", None)
    if repo_root is None:
        return None
    return Path(repo_root) / BUILTIN_EXTENSIONS_DIR


async def sync_extension_mcp(container: Any) -> None:
    """Make the MCP registry's contributed servers match the loaded extensions.

    Called after every extension change (startup, reload, enable / disable,
    uninstall, settings update). Desired-state: unchanged servers keep their
    session; the registry never writes them to ``mcp_servers.json``. A config
    the registry refuses (name clash, invalid) becomes a warning on its
    extension. Best-effort: never raises.
    """
    manager = getattr(container, "user_extensions", None)
    host = getattr(manager, "host", None)
    registry = getattr(getattr(container, "chat", None), "mcp_server_registry", None)
    sync = getattr(registry, "set_contributed_servers", None)
    if host is None or sync is None:
        return
    by_source: dict[str, list[McpServerConfig]] = {}
    invalid: dict[str, str] = {}
    for server in host.mcp_servers():
        try:
            cfg = McpServerConfig(
                name=server.name,
                transport=McpTransport(server.transport),
                command=server.command,
                args=server.args,
                env=dict(server.env),
                cwd=server.cwd,
                url=server.url,
                headers=dict(server.headers),
                timeout_s=server.timeout_s,
                secret_env_keys=server.secret_env_keys,
            )
        except ValueError as exc:
            invalid[server.name] = str(exc)
            continue
        by_source.setdefault(MCP_SOURCE_PREFIX + server.extension, []).append(cfg)
    try:
        refused = await sync(by_source)
    except Exception:  # noqa: BLE001 - MCP must never break extension management
        _LOG.warning("user_extensions.mcp_sync_failed", exc_info=True)
        return
    for name, reason in {**invalid, **refused}.items():
        for status in host.statuses:
            if name in status.mcp_servers:
                note = f"mcp server {name!r} not started: {reason}"
                if note not in status.warnings:
                    status.warnings.append(note)


def prompt_addenda_for(container: Any, selected: frozenset[str]) -> PromptAddenda:
    """The loaded extensions' prompt fragments for one turn (design doc §5.1.3).

    * ``always`` fragments of every loaded extension -> system prompt suffix.
      Sorted by extension name and wrapped per extension, so the text only
      changes when an extension is installed / reloaded / switched;
    * ``active`` fragments of the extensions in ``selected`` (the entries the
      user has selected this turn) -> the turn's context message.
    """
    manager = getattr(container, "user_extensions", None)
    host = getattr(manager, "host", None)
    if host is None:
        return PromptAddenda()
    return PromptAddenda(
        system_suffix=_render_fragments(host.prompt_fragments("always")),
        context=_render_fragments(host.prompt_fragments("active", extensions=selected))
        if selected
        else "",
    )


def _render_fragments(fragments: Any) -> str:
    by_ext: dict[str, list[str]] = {}
    for f in fragments:
        by_ext.setdefault(f.extension, []).append(f.text)
    return "\n\n".join(
        f'<extension_instructions extension="{name}">\n'
        + "\n\n".join(texts)
        + "\n</extension_instructions>"
        for name, texts in by_ext.items()
    )


def extension_skill_roots(container: Any) -> list[ContributedSkillRoot]:
    """The loaded extensions' skill roots, for ``SkillDiscovery``.

    Read on every scan; empty until the host is wired (``user_prefs`` is built
    earlier in phase 2) and for containers without one.
    """
    manager = getattr(container, "user_extensions", None)
    host = getattr(manager, "host", None)
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
