# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Discover, import and activate user extensions.

Lifecycle, per extension directory under the scan root:

1. :func:`~.manifest.load_manifest` validates ``extension.json``;
2. the ``entry`` (a ``.py`` file or a package directory) is imported under
   the private module name ``qai_user_ext_<name>`` (registered in
   :data:`sys.modules`, no ``sys.path`` change);
3. its ``activate(ctx)`` runs (synchronously, once);
4. tool registrations are STAGED on the context and committed to the
   :class:`ToolSink` only after ``activate`` returned, so a failing extension
   leaves nothing behind; its ``on_unload`` callbacks run (newest first).

Name policy (design doc §5.2): a tool keeps the name its manifest declares;
a name already taken -- by a built-in tool, an MCP tool or an earlier
extension -- is REFUSED for that one tool with a reason, the rest loads.

Stdlib-only; knows nothing about the chat context. The composition root
adapts the chat tool registry to :class:`ToolSink` and passes the app
services the context exposes (secret store, proxy, TLS setting).
"""

from __future__ import annotations

import asyncio
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import logging
import os
import re
import shutil
import sys
import time
import tomllib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Final, Literal, Protocol

from .manifest import (
    DEFAULT_SKILLS_DIR,
    PROMPT_MAX_CHARS,
    ExtensionManifest,
    ManifestError,
    ToolDeclaration,
    load_manifest,
)

__all__ = [
    "CONFIG_FILENAME",
    "DATA_DIRNAME",
    "ActionCall",
    "ActionResult",
    "ActionValidationError",
    "ExtensionContext",
    "ExtensionError",
    "ExtensionSecrets",
    "ExtensionSkillRoot",
    "ExtensionStatus",
    "PromptFragment",
    "RegisteredTool",
    "ResolvedMcpServer",
    "RouteRequest",
    "RouteResponse",
    "SecretStoreLike",
    "ToolCall",
    "ToolSink",
    "UnknownExtensionError",
    "UserExtensionHost",
    "call_handler",
    "discover_extension_dirs",
    "module_name_for",
    "run_action",
]

logger = logging.getLogger("qai.platform.user_extensions")

#: ``<ext>/config.toml``: the extension's own read-only defaults.
CONFIG_FILENAME: Final[str] = "config.toml"
#: ``<data>/extensions-data/<name>/``: per-extension writable data, kept apart
#: from the code directory so removing / replacing the code keeps the data.
DATA_DIRNAME: Final[str] = "extensions-data"
_MODULE_PREFIX: Final[str] = "qai_user_ext_"

#: ``disabled``: the user switched the extension off; nothing of it is loaded.
State = Literal["loaded", "failed", "disabled"]


class ExtensionError(Exception):
    """Raised by the extension API when it is misused or cannot do what was asked.

    Public (``qai.ext``). Examples: registering an undeclared tool, an invalid
    ``config.toml``, a disallowed secret key, no secret store available. Raised
    during ``activate`` it fails the extension's load; raised later (from a
    tool handler) the extension may catch it.
    """


class ToolSink(Protocol):
    """What the host needs from the chat tool registry."""

    def tool_names(self) -> frozenset[str]:
        """Every tool name currently registered (built-in, MCP, extensions)."""
        ...

    def register(
        self,
        name: str,
        handler: Callable[[Any], Awaitable[Any]],
        *,
        schema: Mapping[str, Any] | None,
    ) -> None: ...

    def unregister(self, name: str) -> None: ...

    def handler_for(self, name: str) -> Any:
        """The handler currently registered under ``name``, or ``None``."""
        ...


class SecretStoreLike(Protocol):
    """The subset of ``qai.platform.persistence.secrets.SecretStore`` used here."""

    def get(self, service: str, key: str) -> str: ...
    def set(self, service: str, key: str, value: str) -> None: ...
    def delete(self, service: str, key: str) -> None: ...
    def list_keys(self, service: str) -> list[str]: ...
    def exists(self, service: str, key: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class ToolCall:
    """What a tool handler receives. Public (``qai.ext``).

    ``arguments``: the arguments the model passed (a fresh dict per call).
    ``tool_name``: the tool being called. ``conversation_id`` / ``tab_id``:
    where the call comes from (opaque strings; use them as keys only).
    """

    arguments: dict[str, Any]
    tool_name: str
    conversation_id: str
    tab_id: str


@dataclass(frozen=True, slots=True)
class ActionCall:
    """What an action handler receives (a button in the extension's mode was
    submitted). Public (``qai.ext``).

    ``values``: the dialog's fields, validated against the manifest (secret
    fields included -- they are never stored by the host). ``conversation_id``
    / ``tab_id``: where the user clicked (may be empty).
    """

    action_id: str
    values: dict[str, Any]
    conversation_id: str
    tab_id: str


@dataclass(frozen=True, slots=True)
class ActionResult:
    """What an action handler may return (or return a plain ``str`` = message).
    Public (``qai.ext``).

    ``message``: shown to the user. ``prompt``: if set, sent as the user's
    next chat message in the current conversation ("do X with what I just
    entered"). ``ok=False`` shows ``message`` as an error.
    """

    message: str = ""
    prompt: str = ""
    ok: bool = True


@dataclass(frozen=True, slots=True)
class RouteRequest:
    """What a route handler receives. Public (``qai.ext``).

    ``params``: ``{name}`` path parameters. ``query``: query string (last value
    per key). ``body``: the decoded JSON body (``None`` when empty / not JSON).
    ``headers``: lower-cased request headers, without cookies / authorization.
    """

    method: str
    path: str
    params: dict[str, str]
    query: dict[str, str]
    body: Any
    headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class RouteResponse:
    """What a route handler may return for full control. Public (``qai.ext``).

    Otherwise return any JSON-serialisable value (sent as ``200`` JSON), or
    ``None`` (``204``). ``body`` here: JSON value, or ``str`` / ``bytes`` sent
    as-is with ``content_type``.
    """

    body: Any = None
    status: int = 200
    content_type: str = "application/json"
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    name: str
    extension: str
    declaration: ToolDeclaration
    #: The exact handler object put into the sink: unregistering checks the
    #: sink still holds it, so a tool registered over ours is never removed.
    handler: Any = None


@dataclass(frozen=True, slots=True)
class ExtensionSkillRoot:
    """One loaded extension's skills directory, as the skill scanner needs it.

    ``excluded``: absolute sub-paths the scanner must skip -- skill
    directories that escape the root through a link, and skills refused
    because their id was already taken.
    """

    extension: str
    path: Path
    excluded: tuple[Path, ...]
    skill_ids: tuple[str, ...]
    default_modes: Mapping[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class ExtensionStatus:
    """Operator-facing outcome of loading one extension directory."""

    name: str
    root: Path
    state: State
    version: str = ""
    error: str = ""
    tools: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)
    #: Number of prompt fragments per condition (``always`` / ``active``).
    prompts: dict[str, int] = field(default_factory=dict)
    #: App-wide names (``<extension>-<server>``) of its resolved MCP servers.
    mcp_servers: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ResolvedMcpServer:
    """A loaded extension's MCP server with every placeholder filled in.

    ``name`` is the app-wide server name ``<extension>-<server>``. Values
    that came from ``${secret:...}`` are listed in ``secret_env_keys`` /
    ``secret_header_keys`` so they are masked wherever the config is shown.
    Never persisted: it lives as long as the extension is loaded.
    """

    name: str
    extension: str
    transport: str
    command: str | None
    args: tuple[str, ...]
    env: Mapping[str, str]
    cwd: str | None
    url: str | None
    headers: Mapping[str, str]
    timeout_s: float
    secret_env_keys: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PromptFragment:
    """A loaded extension's prompt text, ready to inject."""

    extension: str
    when: str
    text: str


@dataclass(frozen=True, slots=True)
class _PendingTool:
    name: str
    handler: Callable[[Any], Awaitable[Any]]
    schema: Mapping[str, Any]
    declaration: ToolDeclaration


class ExtensionSecrets:
    """``ctx.secrets``: the extension's own credential space. Public (``qai.ext``).

    Backed by the app's secret store (OS keyring or encrypted file) under the
    service name ``ext.<extension name>``; another extension's secrets are not
    reachable. Keys must be non-empty and contain no ``/``, ``\\`` or ``..``.
    Every method raises :class:`ExtensionError` on a disallowed key or when no
    secret store is available.
    """

    def __init__(self, store: SecretStoreLike | None, extension: str) -> None:
        self._store = store
        self._service = f"ext.{extension}"

    def _require(self, key: str | None = None) -> SecretStoreLike:
        if self._store is None:
            raise ExtensionError("no secret store is available in this host")
        if key is not None and (
            not isinstance(key, str) or not key or any(c in key for c in "/\\\x00") or ".." in key
        ):
            raise ExtensionError(f"secret key {key!r} is not allowed (no '/', '\\', '..')")
        return self._store

    def get(self, key: str) -> str | None:
        """The stored value, or ``None`` when absent."""
        store = self._require(key)
        return store.get(self._service, key) if store.exists(self._service, key) else None

    def set(self, key: str, value: str) -> None:
        """Store ``value`` (a string) under ``key``, replacing any previous value."""
        if not isinstance(value, str):
            raise ExtensionError("secret values must be strings")
        self._require(key).set(self._service, key, value)

    def delete(self, key: str) -> None:
        """Remove ``key``; absent is not an error."""
        store = self._require(key)
        if store.exists(self._service, key):
            store.delete(self._service, key)

    def keys(self) -> list[str]:
        """The stored keys (never the values), sorted."""
        return sorted(self._require().list_keys(self._service))


class ExtensionContext:
    """The ``ctx`` passed to ``activate(ctx)``. Public (``qai.ext``).

    Created by the host only; an extension never constructs one. Its public
    members are the whole runtime API (design doc §4.4): ``name``,
    ``version``, ``root``, ``data_dir``, ``config``, ``settings``, ``logger``,
    ``secrets``, ``proxy_url()``, ``ssl_verify``, ``register_tool()``,
    ``on_unload()``.

    ``register_tool`` / ``on_unload`` are valid only while ``activate`` runs;
    everything else may also be used later, from tool handlers.
    """

    def __init__(
        self,
        manifest: ExtensionManifest,
        *,
        data_root: Path,
        taken_names: Callable[[], frozenset[str]],
        secret_store: SecretStoreLike | None = None,
        proxy_url: Callable[[], str | None] | None = None,
        ssl_verify: Callable[[], bool] | None = None,
        stored_settings: Callable[[str], Mapping[str, Any]] | None = None,
    ) -> None:
        self._manifest = manifest
        self._stored_settings = stored_settings or (lambda _n: {})
        self._data_dir = data_root / DATA_DIRNAME / manifest.name
        self._taken_names = taken_names
        self._proxy_url = proxy_url or (lambda: None)
        self._ssl_verify = ssl_verify or (lambda: True)
        self._pending: dict[str, _PendingTool] = {}
        self._actions: dict[str, Callable[[ActionCall], Any]] = {}
        self._routes: dict[tuple[str, str], Callable[[RouteRequest], Any]] = {}
        self._unload: list[Callable[[], None]] = []
        self._config: Mapping[str, Any] | None = None
        self._active = True
        #: The extension's own credential space (:class:`ExtensionSecrets`).
        self.secrets = ExtensionSecrets(secret_store, manifest.name)
        #: Logger ``qai.user_extension.<name>``; records go to the app log.
        self.logger = logging.getLogger(f"qai.user_extension.{manifest.name}")
        #: Host-side notes for the extension's status; not part of the extension API.
        self._warnings: list[str] = []

    # -- identity / environment ------------------------------------------

    @property
    def name(self) -> str:
        """The manifest ``name``."""
        return self._manifest.name

    @property
    def version(self) -> str:
        """The manifest ``version``."""
        return self._manifest.version

    @property
    def root(self) -> Path:
        """The extension's own directory (treat as read-only)."""
        return self._manifest.root

    @property
    def data_dir(self) -> Path:
        """Writable per-extension data directory, created on first access."""
        self._data_dir.mkdir(parents=True, exist_ok=True)
        return self._data_dir

    @property
    def config(self) -> Mapping[str, Any]:
        """``<ext>/config.toml`` parsed (read-only); ``{}`` when absent."""
        if self._config is None:
            path = self._manifest.root / CONFIG_FILENAME
            try:
                data = tomllib.loads(path.read_text(encoding="utf-8-sig")) if path.is_file() else {}
            except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
                raise ExtensionError(f"{CONFIG_FILENAME} is invalid: {exc}") from exc
            self._config = _freeze(data)
        return self._config

    @property
    def settings(self) -> Mapping[str, Any]:
        """The user's values for the manifest's ``settings[]`` (read-only).

        Every declared non-secret key is present: the user's value, else the
        declared ``default``, else ``None``. Read live on every access, so a
        change on the settings card takes effect without a reload. Secret
        settings are not here -- read them with ``ctx.secrets.get(key)``.
        """
        stored = self._stored_settings(self._manifest.name)
        return MappingProxyType(
            {
                s.key: stored.get(s.key, s.default)
                for s in self._manifest.settings
                if s.type != "secret"
            }
        )

    def proxy_url(self) -> str | None:
        """The app's current proxy URL (may embed credentials -- never log it)."""
        return self._proxy_url()

    @property
    def ssl_verify(self) -> bool:
        """The app's current TLS-verification setting; read it per request."""
        return self._ssl_verify()

    # -- registrations (only during activate) ----------------------------

    def register_tool(
        self,
        name: str,
        handler: Callable[[ToolCall], Any],
        *,
        description: str,
        parameters: Mapping[str, Any] | None = None,
    ) -> None:
        """Register chat tool ``name`` (declared in the manifest's ``tools``).

        ``handler(call: ToolCall)`` may be sync or async; its return value is
        the tool result. ``parameters`` is a JSON-schema object (default: no
        arguments). Misuse raises :class:`ExtensionError` and fails the load;
        a name already used by another tool only refuses this tool (warning).
        """
        self._require_active("register_tool")
        decl = self._manifest.tool(name)
        if decl is None:
            raise ExtensionError(f"register_tool({name!r}): not declared in the manifest's tools")
        if name in self._pending:
            raise ExtensionError(f"register_tool({name!r}): registered twice")
        if not callable(handler):
            raise ExtensionError(f"register_tool({name!r}): handler is not callable")
        if not isinstance(description, str) or not description.strip():
            raise ExtensionError(f"register_tool({name!r}): description must be a non-empty string")
        params = (
            dict(parameters) if parameters is not None else {"type": "object", "properties": {}}
        )
        if params.get("type") != "object":
            raise ExtensionError(
                f"register_tool({name!r}): parameters must be a JSON schema object"
            )
        try:  # sent to the model on every turn: must serialise
            json.dumps(params)
        except (TypeError, ValueError) as exc:
            raise ExtensionError(
                f"register_tool({name!r}): parameters is not JSON-serialisable: {exc}"
            ) from exc
        if name in self._taken_names():
            self._warnings.append(
                f"tool {name!r} refused: the name is already used by another tool"
            )
            return
        self._pending[name] = _PendingTool(
            name=name,
            handler=_adapt_handler(handler),
            schema={
                "type": "function",
                "function": {"name": name, "description": description, "parameters": params},
            },
            declaration=decl,
        )

    def register_action(self, action_id: str, handler: Callable[[ActionCall], Any]) -> None:
        """Handle action ``action_id`` (declared in the manifest's ``ui.actions``).

        ``handler(call: ActionCall)`` may be sync (runs in a worker thread) or
        async; it returns an :class:`ActionResult`, a ``str`` (a message) or
        ``None``. Raising shows the error to the user.
        """
        self._require_active("register_action")
        ui = self._manifest.ui
        if ui is None or ui.action(action_id) is None:
            raise ExtensionError(
                f"register_action({action_id!r}): not declared in the manifest's ui.actions"
            )
        if action_id in self._actions:
            raise ExtensionError(f"register_action({action_id!r}): registered twice")
        if not callable(handler):
            raise ExtensionError(f"register_action({action_id!r}): handler is not callable")
        self._actions[action_id] = handler

    def register_route(
        self, method: str, path: str, handler: Callable[[RouteRequest], Any]
    ) -> None:
        """Serve ``method /api/ext/<name>/<path>`` (declared in the manifest's ``routes``).

        ``handler(req: RouteRequest)`` may be sync (worker thread) or async.
        The route is behind the app's login and CSRF protection like every
        other ``/api`` route, and disappears when the extension is unloaded.
        """
        self._require_active("register_route")
        method = method.upper() if isinstance(method, str) else method
        if self._manifest.route(method, path) is None:
            raise ExtensionError(
                f"register_route({method!r}, {path!r}): not declared in the manifest's routes"
            )
        if (method, path) in self._routes:
            raise ExtensionError(f"register_route({method!r}, {path!r}): registered twice")
        if not callable(handler):
            raise ExtensionError(f"register_route({method!r}, {path!r}): handler is not callable")
        self._routes[(method, path)] = handler

    def on_unload(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` when the extension is unloaded or fails to load (newest first)."""
        self._require_active("on_unload")
        if not callable(callback):
            raise ExtensionError("on_unload: callback is not callable")
        self._unload.append(callback)

    # -- host side --------------------------------------------------------

    def _require_active(self, what: str) -> None:
        if not self._active:
            raise ExtensionError(f"{what}: only allowed while activate() runs")

    def _finish(self) -> list[_PendingTool]:
        self._active = False
        return list(self._pending.values())

    def _run_unload(self) -> None:
        self._active = False
        self._pending.clear()
        self._actions.clear()
        self._routes.clear()
        callbacks, self._unload = self._unload, []
        for callback in reversed(callbacks):
            try:
                callback()
            except KeyboardInterrupt:
                raise
            except BaseException:
                logger.warning(
                    "user_extensions.unload_callback_failed extension=%s", self.name, exc_info=True
                )


class UnknownExtensionError(LookupError):
    """No extension directory (and no loaded extension) has this name."""


#: ``on_tool_added(name, group)`` / ``on_tool_removed(name)``: let the
#: composition root keep per-tool policy (permission groups) in step with the
#: registry. ``on_tool_added`` runs BEFORE the tool is registered and
#: ``on_tool_removed`` AFTER it is unregistered, so the tool is never callable
#: without its policy.
ToolAddedHook = Callable[[str, "str | None"], None]
ToolRemovedHook = Callable[[str], None]


class UserExtensionHost:
    """Loads the extensions under ``scan_root`` into a :class:`ToolSink`.

    Mutations (load / reload / unload) are not thread-safe: the composition
    root serialises them with one lock. The read accessors (``statuses``,
    ``skill_roots``, ``tools``, ``skill_default_modes``) may run concurrently
    with a mutation on another thread: they work on snapshots.
    """

    def __init__(
        self,
        scan_root: Path,
        sink: ToolSink,
        *,
        data_root: Path,
        taken_skill_ids: Callable[[], frozenset[str]] | None = None,
        reserved_tool_names: frozenset[str] = frozenset(),
        on_tool_added: ToolAddedHook | None = None,
        on_tool_removed: ToolRemovedHook | None = None,
        is_disabled: Callable[[str], bool] | None = None,
        stored_settings: Callable[[str], Mapping[str, Any]] | None = None,
        secret_store: SecretStoreLike | None = None,
        proxy_url: Callable[[], str | None] | None = None,
        ssl_verify: Callable[[], bool] | None = None,
    ) -> None:
        """``stored_settings(name)``: the user's saved values for an
        extension's ``settings[]`` (read live by ``ctx.settings``).

        ``taken_skill_ids`` returns the ids of the app's own skills; an
        extension skill with one of those ids is refused (same rule as tools).

        ``reserved_tool_names``: built-in tool names an extension may never
        take, even when that built-in is not registered right now (disabled by
        configuration, or registered later than the extensions).

        ``is_disabled(name)``: the user's switch, read on every load; a
        disabled extension is recorded with state ``disabled`` and nothing of
        it (code, tools, skills) is loaded.
        """
        self._scan_root = scan_root
        self._sink = sink
        self._data_root = data_root
        self._taken_skill_ids = taken_skill_ids or frozenset
        self._reserved = frozenset(reserved_tool_names)
        self._on_tool_added = on_tool_added or (lambda _n, _g: None)
        self._on_tool_removed = on_tool_removed or (lambda _n: None)
        self._is_disabled = is_disabled or (lambda _n: False)
        self._stored_settings: Callable[[str], Mapping[str, Any]] = stored_settings or (
            lambda _n: {}
        )
        self._services = {
            "secret_store": secret_store,
            "proxy_url": proxy_url,
            "ssl_verify": ssl_verify,
            "stored_settings": stored_settings,
        }
        self._statuses: dict[str, ExtensionStatus] = {}
        self._tools: dict[str, RegisteredTool] = {}
        self._skill_roots: dict[str, ExtensionSkillRoot] = {}
        self._prompts: dict[str, tuple[PromptFragment, ...]] = {}
        #: name -> how many times it has been loaded (the cache-busting ``v``).
        self._load_tokens: dict[str, int] = {}
        self._mcp: dict[str, tuple[ResolvedMcpServer, ...]] = {}
        self._contexts: dict[str, ExtensionContext] = {}
        self._modules: dict[str, list[str]] = {}

    @property
    def scan_root(self) -> Path:
        return self._scan_root

    @property
    def statuses(self) -> tuple[ExtensionStatus, ...]:
        snap = dict(self._statuses)
        return tuple(snap[k] for k in sorted(snap))

    @property
    def tools(self) -> Mapping[str, RegisteredTool]:
        return dict(self._tools)

    def tools_of_group(self, group: str) -> frozenset[str]:
        tools = list(dict(self._tools).values())
        return frozenset(t.name for t in tools if t.declaration.group == group)

    def skill_roots(self) -> tuple[ExtensionSkillRoot, ...]:
        """Skill roots of loaded extensions, sorted by extension name."""
        snap = dict(self._skill_roots)
        return tuple(snap[k] for k in sorted(snap))

    def skill_default_modes(self) -> dict[str, str]:
        """``skill id -> default mode`` across loaded extensions (visible skills only)."""
        out: dict[str, str] = {}
        for root in self.skill_roots():
            for skill_id, mode in root.default_modes.items():
                if skill_id in root.skill_ids:
                    out.setdefault(skill_id, mode)
        return out

    def status(self, name: str) -> ExtensionStatus | None:
        return self._statuses.get(name)

    def manifest(self, name: str) -> ExtensionManifest:
        """Read ``name``'s manifest from disk (works for a disabled or failed
        extension too, as long as its ``extension.json`` is valid).

        :class:`UnknownExtensionError` when there is no such directory;
        :class:`ManifestError` when its manifest is invalid.
        """
        for ext_dir in discover_extension_dirs(self._scan_root):
            if ext_dir.name == name:
                return load_manifest(ext_dir)
        raise UnknownExtensionError(name)

    def secrets_of(self, name: str) -> ExtensionSecrets:
        """``name``'s secret space (the same one its ``ctx.secrets`` uses)."""
        return ExtensionSecrets(self._services["secret_store"], name)  # type: ignore[arg-type]

    def prompt_fragments(
        self, when: str, *, extensions: frozenset[str] | None = None
    ) -> tuple[PromptFragment, ...]:
        """Loaded fragments with condition ``when``, sorted by extension name
        (stable order: the ``always`` block must be byte-identical turn to
        turn). ``extensions`` restricts to those extension names."""
        snap = dict(self._prompts)
        return tuple(
            f
            for name in sorted(snap)
            if extensions is None or name in extensions
            for f in snap[name]
            if f.when == when
        )

    def load_all(self) -> tuple[ExtensionStatus, ...]:
        """Load every extension directory not loaded yet (sorted by name).

        A directory whose previous attempt failed is tried again.
        """
        for ext_dir in discover_extension_dirs(self._scan_root):
            if self._loaded_from(ext_dir):
                continue
            self._statuses.pop(ext_dir.name, None)  # a previous failure
            self._record(self.load(ext_dir))
        return self.statuses

    def reload_all(self) -> tuple[ExtensionStatus, ...]:
        """Unload everything, then load the scan root again (picks up new,
        changed and removed extension directories)."""
        self.unload_all()
        return self.load_all()

    def reload(self, name: str) -> ExtensionStatus:
        """Unload extension ``name`` (if loaded) and load its directory again.

        ``name`` is the extension's directory name (the manifest requires the
        two to be equal). Raises :class:`UnknownExtensionError` when there is
        neither such a directory nor such a loaded extension. A failed reload
        leaves the extension unloaded, with the failure in its status.
        """
        ext_dir = self._ext_dir(name)
        self.unload(name)
        status = self.load(ext_dir)
        self._record(status)
        return status

    def remove(self, name: str, *, purge_data: bool = False) -> None:
        """Uninstall: unload ``name`` and delete its directory under the scan root.

        ``purge_data`` also deletes ``<data>/extensions-data/<name>`` and every
        secret in its namespace. Raises :class:`UnknownExtensionError` when
        there is no such directory. The directory must sit directly in the
        scan root (a link pointing elsewhere is unlinked, not followed).
        """
        ext_dir = self._scan_root / name
        if not ext_dir.exists() and not ext_dir.is_symlink() and name not in self._statuses:
            raise UnknownExtensionError(name)
        if (
            ext_dir.parent != self._scan_root
            or name in {"", ".", ".."}
            or "/" in name
            or "\\" in name
        ):
            raise UnknownExtensionError(name)
        self.unload(name)
        _remove_tree(ext_dir)
        if purge_data:
            _remove_tree(self._data_root / DATA_DIRNAME / name)
            secrets = self.secrets_of(name)
            try:
                for key in secrets.keys():  # noqa: SIM118 - ExtensionSecrets, not a dict
                    secrets.delete(key)
            except ExtensionError:
                pass  # no secret store in this host

    def unload(self, name: str) -> bool:
        """Unload one extension: tools first, then ``on_unload``, then modules.

        Returns ``False`` when nothing was known under ``name``. Its status is
        dropped; its skills disappear from the next scan.
        """
        known = name in self._statuses
        for tool in [t for t in list(self._tools.values()) if t.extension == name]:
            self._unregister_tool(tool)
        ctx = self._contexts.pop(name, None)
        if ctx is not None:
            ctx._run_unload()
        self._skill_roots.pop(name, None)
        self._prompts.pop(name, None)
        self._mcp.pop(name, None)
        self._modules.pop(name, None)
        # Every module under the extension's private name, including submodules
        # first imported later (from a tool handler), so a reload never reuses one.
        _forget_modules(_own_modules(module_name_for(name)))
        self._statuses.pop(name, None)
        return known or ctx is not None

    def _record(self, status: ExtensionStatus) -> None:
        self._statuses[status.name] = status
        _log_status(status)

    def _loaded_from(self, ext_dir: Path) -> bool:
        return any(s.root == ext_dir and s.state != "failed" for s in list(self._statuses.values()))

    def _ext_dir(self, name: str) -> Path:
        for ext_dir in discover_extension_dirs(self._scan_root):
            if ext_dir.name == name:
                return ext_dir
        # Not on disk any more, but still loaded: reload == unload.
        known = self._statuses.get(name)
        if known is not None:
            return known.root
        raise UnknownExtensionError(name)

    def _unregister_tool(self, tool: RegisteredTool) -> None:
        """Remove ``tool`` from the sink only if the sink still holds OUR handler."""
        self._tools.pop(tool.name, None)
        if tool.handler is None or self._sink.handler_for(tool.name) is tool.handler:
            self._sink.unregister(tool.name)
            self._on_tool_removed(tool.name)
        else:
            logger.warning(
                "user_extensions.tool_replaced extension=%s tool=%s "
                "(another registrant owns the name now; left in place)",
                tool.extension,
                tool.name,
            )

    def load(self, ext_dir: Path) -> ExtensionStatus:
        """Load one extension directory. Never raises.

        A host-side failure after tools were committed (e.g. the skill scan)
        unloads the extension again and is reported in its status.
        """
        try:
            return self._load(ext_dir)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:
            logger.warning("user_extensions.load_failed dir=%s", ext_dir, exc_info=True)
            self.unload(ext_dir.name)
            return ExtensionStatus(
                name=ext_dir.name,
                root=ext_dir,
                state="failed",
                error=f"host error: {type(exc).__name__}: {exc}",
            )

    def _load(self, ext_dir: Path) -> ExtensionStatus:  # noqa: PLR0911, PLR0912, PLR0915 - one exit per outcome
        if not ext_dir.is_dir():
            return ExtensionStatus(
                name=ext_dir.name,
                root=ext_dir,
                state="failed",
                error="the extension directory no longer exists",
            )
        try:
            manifest = load_manifest(ext_dir)
        except ManifestError as exc:
            return ExtensionStatus(name=ext_dir.name, root=ext_dir, state="failed", error=str(exc))
        status = ExtensionStatus(
            name=manifest.name, root=ext_dir, state="failed", version=manifest.version
        )
        if manifest.name in self._statuses and self._statuses[manifest.name].state == "loaded":
            status.error = "an extension with this name is already loaded"
            return status
        if self._is_disabled(manifest.name):
            status.state = "disabled"
            return status
        try:
            fragments = _read_prompts(manifest)
        except ExtensionError as exc:
            status.error = f"ExtensionError: {exc}"
            return status
        if manifest.entry is None:
            status.state = "loaded"
            self._commit_skills(manifest, status)
            self._commit_prompts(manifest.name, fragments, status)
            self._commit_mcp(manifest, status)
            self._load_tokens[manifest.name] = self._load_tokens.get(manifest.name, 0) + 1
            return status

        ctx = ExtensionContext(
            manifest,
            data_root=self._data_root,
            taken_names=self._taken_tool_names,
            **self._services,  # type: ignore[arg-type]
        )
        module_name = module_name_for(manifest.name)
        try:
            _activate(_import_entry(manifest, module_name), ctx)
        except KeyboardInterrupt:
            raise
        except BaseException as exc:  # noqa: BLE001 - untrusted code; incl. SystemExit
            ctx._run_unload()
            _forget_modules(_own_modules(module_name))
            status.warnings.extend(ctx._warnings)
            status.error = f"{type(exc).__name__}: {exc}"
            return status

        self._modules[manifest.name] = _own_modules(module_name)
        self._contexts[manifest.name] = ctx
        committed: list[str] = []
        for pending in ctx._finish():
            # Re-check: another tool may have taken the name meanwhile.
            if pending.name in self._taken_tool_names():
                ctx._warnings.append(
                    f"tool {pending.name!r} refused: the name is already used by another tool"
                )
                continue
            # Policy first: the tool must never be callable without its group.
            self._on_tool_added(pending.name, pending.declaration.group)
            self._sink.register(pending.name, pending.handler, schema=pending.schema)
            self._tools[pending.name] = RegisteredTool(
                name=pending.name,
                extension=manifest.name,
                declaration=pending.declaration,
                handler=pending.handler,
            )
            committed.append(pending.name)
        for name in sorted(manifest.tool_names - set(committed)):
            if not any(repr(name) in w for w in ctx._warnings):
                ctx._warnings.append(f"tool {name!r} is declared but was never registered")
        for route in manifest.routes:
            if (route.method, route.path) not in ctx._routes:
                ctx._warnings.append(
                    f"route {route.method} {route.path} is declared but has no handler"
                )
        for action in manifest.ui.actions if manifest.ui is not None else ():
            if action.id not in ctx._actions:
                ctx._warnings.append(
                    f"action {action.id!r} is declared but has no handler (its button is hidden)"
                )
        status.state = "loaded"
        status.tools = tuple(committed)
        status.warnings.extend(ctx._warnings)
        self._commit_skills(manifest, status)
        self._commit_prompts(manifest.name, fragments, status)
        self._commit_mcp(manifest, status)
        self._load_tokens[manifest.name] = self._load_tokens.get(manifest.name, 0) + 1
        return status

    # -- UI (modes, action buttons) -----------------------------------------

    def ui_entries(self) -> list[dict[str, Any]]:
        """The Extension menu: loaded extensions that declare ``ui`` (by name).

        Only actions with a registered handler are listed. Field values are
        never included (a dialog always starts from the declared defaults).
        """
        out: list[dict[str, Any]] = []
        for status in self.statuses:
            if status.state != "loaded":
                continue
            try:
                manifest = load_manifest(status.root)
            except ManifestError:
                continue
            if manifest.ui is None:
                continue
            ctx = self._contexts.get(status.name)
            handled = set(ctx._actions) if ctx is not None else set()
            out.append(
                {
                    "name": status.name,
                    "label": manifest.ui.label,
                    "icon": manifest.ui.icon,
                    "description": manifest.description,
                    # Front-end module (route A): a URL under /api/ext/<name>/_ui/,
                    # versioned so a reload makes the browser fetch the new code.
                    "module": self.ui_module_url(status.name),
                    "actions": [
                        {
                            "id": a.id,
                            "label": a.label,
                            "description": a.description,
                            "fields": [_field_wire(f) for f in a.fields],
                        }
                        for a in manifest.ui.actions
                        if a.id in handled
                    ],
                }
            )
        return out

    def ui_module_dir(self, name: str) -> Path | None:
        """The directory of a LOADED extension's front-end module (the only
        directory of the extension served to the browser), else ``None``.

        Resolved on every call and checked to lie inside the extension, so a
        link swapped in after loading cannot expose anything else.
        """
        status = self._statuses.get(name)
        if status is None or status.state != "loaded":
            return None
        try:
            manifest = load_manifest(status.root)
        except ManifestError:
            return None
        if manifest.ui is None or manifest.ui.module_dir is None:
            return None
        root = manifest.root.resolve()
        directory = (manifest.root / manifest.ui.module_dir).resolve()
        if not directory.is_dir() or not directory.is_relative_to(root):
            return None
        return directory

    def ui_module_url(self, name: str) -> str | None:
        """``/api/ext/<name>/_ui/<file>?v=<version>`` for the extension's module.

        ``v`` changes with every load (the browser caches ES modules by URL and
        cannot unload them), so a reload always fetches the new code.
        """
        status = self._statuses.get(name)
        directory = self.ui_module_dir(name)
        if status is None or directory is None:
            return None
        try:
            ui = load_manifest(status.root).ui
        except ManifestError:
            return None
        if ui is None or ui.module_file is None or not (directory / ui.module_file).is_file():
            return None
        token = self._load_tokens.get(name, 0)
        return f"/api/ext/{name}/_ui/{ui.module_file}?v={status.version}-{token}"

    def route_handler(
        self, name: str, method: str, path: str
    ) -> tuple[Callable[[RouteRequest], Any], dict[str, str]] | None:
        """``(handler, path params)`` for a request to a loaded extension's
        route; literal routes win over parameterised ones. ``None`` if none."""
        status = self._statuses.get(name)
        ctx = self._contexts.get(name)
        if status is None or status.state != "loaded" or ctx is None:
            return None
        matches: list[tuple[int, Callable[[RouteRequest], Any], dict[str, str]]] = []
        for decl in ctx._manifest.routes:
            handler = ctx._routes.get((decl.method, decl.path))
            params = decl.match(method, path) if handler is not None else None
            if handler is not None and params is not None:
                matches.append((len(params), handler, params))
        if not matches:
            return None
        matches.sort(key=lambda m: m[0])
        return matches[0][1], matches[0][2]

    def route_methods(self, name: str, path: str) -> set[str]:
        """Methods with a handler at ``path`` (for a 405 + ``Allow``)."""
        ctx = self._contexts.get(name)
        if ctx is None or name not in self._statuses:
            return set()
        return {
            d.method
            for d in ctx._manifest.routes
            if (d.method, d.path) in ctx._routes and d.match(d.method, path) is not None
        }

    def action_handler(
        self, name: str, action_id: str
    ) -> tuple[Any, Callable[[ActionCall], Any]] | None:
        """``(declaration, handler)`` of a loaded extension's action, else ``None``."""
        status = self._statuses.get(name)
        ctx = self._contexts.get(name)
        if status is None or status.state != "loaded" or ctx is None:
            return None
        handler = ctx._actions.get(action_id)
        ui = ctx._manifest.ui
        decl = ui.action(action_id) if ui is not None else None
        if handler is None or decl is None:
            return None
        return decl, handler

    # -- MCP servers --------------------------------------------------------

    def mcp_servers(self) -> tuple[ResolvedMcpServer, ...]:
        """Every loaded extension's MCP servers, sorted by server name."""
        snap = dict(self._mcp)
        return tuple(sorted((s for v in snap.values() for s in v), key=lambda s: s.name))

    def refresh_mcp(self, name: str) -> None:
        """Re-resolve a loaded extension's servers (its settings changed)."""
        status = self._statuses.get(name)
        if status is None or status.state != "loaded":
            return
        try:
            manifest = load_manifest(status.root)
        except ManifestError:
            return  # the next reload reports it
        status.warnings = [w for w in status.warnings if not w.startswith("mcp server ")]
        self._commit_mcp(manifest, status)

    def _commit_mcp(self, manifest: ExtensionManifest, status: ExtensionStatus) -> None:
        resolved: list[ResolvedMcpServer] = []
        for decl in manifest.mcp_servers:
            try:
                resolved.append(self._resolve_mcp(manifest, decl))
            except ExtensionError as exc:
                status.warnings.append(f"mcp server {decl.name!r} not started: {exc}")
        if resolved:
            self._mcp[manifest.name] = tuple(resolved)
        else:
            self._mcp.pop(manifest.name, None)
        status.mcp_servers = tuple(s.name for s in resolved)

    def _resolve_mcp(self, manifest: ExtensionManifest, decl: Any) -> ResolvedMcpServer:
        name = manifest.name
        stored: Mapping[str, Any] = self._stored_settings(name)
        defaults = {s.key: s.default for s in manifest.settings}
        secrets = self.secrets_of(name)
        used_secret = False

        def fill(value: str) -> str:
            nonlocal used_secret

            def one(match: re.Match[str]) -> str:
                nonlocal used_secret
                ph = match.group(1)
                if ph == "root":
                    return str(manifest.root)
                if ph == "data_dir":
                    data_dir = self._data_root / DATA_DIRNAME / name
                    data_dir.mkdir(parents=True, exist_ok=True)
                    return str(data_dir)
                if ph == "python":
                    return sys.executable
                kind, _, key = ph.partition(":")
                if kind == "setting":
                    v = stored.get(key, defaults.get(key))
                    return "" if v is None else (str(v).lower() if isinstance(v, bool) else str(v))
                secret = secrets.get(key)  # kind == "secret" (validated by the manifest)
                if secret is None:
                    raise ExtensionError(f"secret setting {key!r} is not set")
                used_secret = True
                return secret

            return _PLACEHOLDER.sub(one, value)

        env: dict[str, str] = {}
        secret_env: list[str] = []
        for key, value in decl.env.items():
            used_secret = False
            env[key] = fill(value)
            if used_secret:
                secret_env.append(key)
        return ResolvedMcpServer(
            name=f"{name}-{decl.name}",
            extension=name,
            transport=decl.transport,
            command=fill(decl.command) if decl.command is not None else None,
            args=tuple(fill(a) for a in decl.args),
            env=env,
            # No default cwd (the app's, like a user's own server): a process
            # sitting in the extension directory would keep Windows from
            # deleting it on uninstall. Paths go through ${root} / ${data_dir}.
            cwd=fill(decl.cwd) if decl.cwd is not None else None,
            url=fill(decl.url) if decl.url is not None else None,
            headers={k: fill(v) for k, v in decl.headers.items()},
            timeout_s=decl.timeout_s,
            secret_env_keys=tuple(secret_env),
        )

    def _commit_prompts(
        self, name: str, fragments: tuple[PromptFragment, ...], status: ExtensionStatus
    ) -> None:
        if fragments:
            self._prompts[name] = fragments
        counts: dict[str, int] = {}
        for f in fragments:
            counts[f.when] = counts.get(f.when, 0) + 1
        status.prompts = counts

    def _taken_tool_names(self) -> frozenset[str]:
        """Names an extension may not take: registered now, or reserved."""
        return self._sink.tool_names() | self._reserved

    def _commit_skills(self, manifest: ExtensionManifest, status: ExtensionStatus) -> None:
        """Record the extension's skill root (only for a LOADED extension)."""
        claimed = {sid for r in self._skill_roots.values() for sid in r.skill_ids}
        root = _collect_skills(
            manifest,
            taken=self._taken_skill_ids(),
            claimed=frozenset(claimed),
            warnings=status.warnings,
        )
        if root is not None:
            self._skill_roots[manifest.name] = root
            status.skills = root.skill_ids

    def unload_all(self) -> None:
        """Unload every extension (reverse load order).

        Per extension: tools first, so no new call reaches an extension whose
        resources its ``on_unload`` callbacks are tearing down.
        """
        for name in sorted(set(self._statuses) | set(self._contexts), reverse=True):
            self.unload(name)


def module_name_for(extension: str) -> str:
    """The private :data:`sys.modules` name of an extension's entry."""
    return _MODULE_PREFIX + extension.replace("-", "_")


_PLACEHOLDER = re.compile(r"\$\{([^}]*)\}")


def _read_prompts(manifest: ExtensionManifest) -> tuple[PromptFragment, ...]:
    """Resolve ``prompts[]`` (read ``file`` entries now, once per load)."""
    ext_root = manifest.root.resolve()
    out: list[PromptFragment] = []
    for index, decl in enumerate(manifest.prompts):
        text = decl.text
        if decl.file is not None:
            path = manifest.root / decl.file
            try:
                inside = path.resolve(strict=True).is_relative_to(ext_root)
            except OSError as exc:
                raise ExtensionError(f"prompts[{index}].file {decl.file!r} not found") from exc
            if not inside:
                raise ExtensionError(f"prompts[{index}].file resolves outside the extension")
            try:
                text = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeDecodeError) as exc:
                raise ExtensionError(f"prompts[{index}].file cannot be read: {exc}") from exc
            if len(text) > PROMPT_MAX_CHARS:
                raise ExtensionError(
                    f"prompts[{index}].file is longer than {PROMPT_MAX_CHARS} characters"
                )
        assert text is not None
        text = text.strip()
        if text:
            out.append(PromptFragment(extension=manifest.name, when=decl.when, text=text))
    return tuple(out)


def _remove_tree(path: Path, *, attempts: int = 20) -> None:
    """Delete ``path``: a link is unlinked (its target is left alone).

    Retries for a few seconds on a sharing violation: on Windows a process
    that was just stopped (an MCP server started through a venv launcher, a
    file watcher) can hold a handle a moment longer.
    """
    for attempt in range(attempts):
        try:
            if path.is_symlink() or _is_junction(path):
                os.unlink(path) if path.is_symlink() else os.rmdir(path)
            elif path.is_dir():
                shutil.rmtree(path)
            elif path.exists():
                path.unlink()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.25)
        else:
            return


def _is_junction(path: Path) -> bool:
    is_junction = getattr(os.path, "isjunction", None)  # Python 3.12+
    return bool(is_junction and is_junction(path))


def discover_extension_dirs(scan_root: Path) -> list[Path]:
    """Immediate sub-directories of ``scan_root`` that may hold an extension.

    ``_``/``.``-prefixed directories (templates, VCS metadata, ``__pycache__``)
    are skipped. Sorted by name so load order -- and therefore which
    extension wins a name -- is stable.
    """
    if not scan_root.is_dir():
        return []
    return [
        entry
        for entry in sorted(scan_root.iterdir(), key=lambda p: p.name)
        if entry.is_dir() and not entry.name.startswith((".", "_"))
    ]


def _collect_skills(
    manifest: ExtensionManifest,
    *,
    taken: frozenset[str],
    claimed: frozenset[str],
    warnings: list[str],
) -> ExtensionSkillRoot | None:
    """Resolve ``<ext>/<skills.dir>`` into an :class:`ExtensionSkillRoot`.

    The root must resolve inside the extension; a skill directory whose real
    path escapes it (link / junction) is excluded. A skill id already used by
    an app skill or by an earlier extension is refused and excluded (design
    doc §5.2). ``None`` when there is no skills directory.
    """
    decl = manifest.skills
    ext_root = manifest.root.resolve()
    skills_root = manifest.root / decl.dir
    if not skills_root.is_dir():
        if decl.dir != DEFAULT_SKILLS_DIR:  # an explicit dir that is missing
            warnings.append(f"skills.dir {decl.dir!r} does not exist")
        for skill_id in sorted(decl.default_modes):
            warnings.append(f"skills.default_modes: no skill {skill_id!r} in this extension")
        return None
    try:
        confined = skills_root.resolve(strict=True)
    except OSError:
        return None
    if not confined.is_relative_to(ext_root):
        warnings.append(f"skills.dir {decl.dir!r} resolves outside the extension; ignored")
        return None

    excluded: list[Path] = []
    ids: list[str] = []
    present: set[str] = set()
    try:
        children = sorted(skills_root.iterdir())
    except OSError as exc:
        warnings.append(f"could not list skills.dir {decl.dir!r}: {exc}")
        return None
    for child in filter(_is_skill_dir, children):
        present.add(child.name)
        try:
            inside = child.resolve(strict=True).is_relative_to(confined)
        except OSError:
            inside = False
        if not inside:
            warnings.append(f"skill {child.name!r} resolves outside skills.dir; ignored")
            excluded.append(child)
            continue
        if child.name in taken or child.name in claimed:
            warnings.append(
                f"skill {child.name!r} refused: the id is already used by another skill"
            )
            excluded.append(child)
            continue
        if (child / "skill.policy.json").is_file():
            warnings.append(
                f"skill {child.name!r}: skill.policy.json is not supported and is ignored"
            )
        ids.append(child.name)
    for skill_id in sorted(set(decl.default_modes) - present):
        warnings.append(f"skills.default_modes: no skill {skill_id!r} in this extension")
    return ExtensionSkillRoot(
        extension=manifest.name,
        path=skills_root,
        excluded=tuple(excluded),
        skill_ids=tuple(ids),
        default_modes=dict(decl.default_modes),
    )


def _is_skill_dir(path: Path) -> bool:
    return path.is_dir() and ((path / "SKILL.md").is_file() or (path / "skill.json").is_file())


def _activate(module: ModuleType, ctx: ExtensionContext) -> None:
    activate = getattr(module, "activate", None)
    if not callable(activate):
        raise ExtensionError("the entry defines no activate(ctx) function")
    result = activate(ctx)
    if inspect.isawaitable(result):
        if inspect.iscoroutine(result):
            result.close()  # never awaited: avoid the "never awaited" warning
        raise ExtensionError("activate(ctx) must be a plain (non-async) function")


def _import_entry(manifest: ExtensionManifest, module_name: str) -> ModuleType:
    """Import ``entry`` (a ``.py`` file, or a package dir) as ``module_name``."""
    assert manifest.entry is not None
    target = manifest.root / manifest.entry
    is_package = target.is_dir()
    file = target / "__init__.py" if is_package else target
    if is_package and not file.is_file():
        raise ExtensionError(f"entry {manifest.entry!r} is a directory without __init__.py")
    if not is_package and not (target.is_file() and target.suffix.lower() == ".py"):
        raise ExtensionError(
            f"entry {manifest.entry!r} not found (a .py file or a package directory)"
        )
    ext_root = manifest.root.resolve()
    if not all(p.resolve().is_relative_to(ext_root) for p in (target, file)):
        raise ExtensionError(f"entry {manifest.entry!r} resolves outside the extension")
    if module_name in sys.modules:
        raise ExtensionError(f"module {module_name!r} is already imported")
    spec = importlib.util.spec_from_file_location(
        module_name,
        file,
        loader=_SourceOnlyLoader(module_name, str(file)),
        submodule_search_locations=[str(target)] if is_package else None,
    )
    if spec is None or spec.loader is None:
        raise ExtensionError(f"cannot import {target}")
    _ACTIVE_ROOTS[module_name] = manifest.root.resolve()
    importlib.invalidate_caches()  # directory listings cached by path finders
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _SourceOnlyLoader(importlib.machinery.SourceFileLoader):
    """Compile from source every time; never read or write cached bytecode.

    A cached ``.pyc`` is trusted when the source's mtime (whole seconds) and
    size match, so an edit that keeps the size and lands in the same second --
    normal when editing and reloading quickly -- would otherwise run the OLD
    code. The cache may also live outside the extension (``sys.pycache_prefix``,
    which appbuilder sets), so deleting ``__pycache__`` is not enough.
    """

    def get_code(self, fullname: str) -> Any:
        path = self.get_filename(fullname)
        return compile(self.get_data(path), path, "exec", dont_inherit=True)


class _ExtensionSubmoduleFinder(importlib.abc.MetaPathFinder):
    """Load an extension package's own submodules with :class:`_SourceOnlyLoader`."""

    def find_spec(self, fullname: str, path: Any, target: Any = None) -> Any:
        top = fullname.split(".", 1)[0]
        if top not in _ACTIVE_ROOTS or fullname == top:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or not isinstance(spec.loader, importlib.machinery.SourceFileLoader):
            return spec
        spec.loader = _SourceOnlyLoader(fullname, spec.loader.path)
        return spec


#: Module name -> extension root, for the submodule finder.
_ACTIVE_ROOTS: dict[str, Path] = {}
_SUBMODULE_FINDER = _ExtensionSubmoduleFinder()
if not any(isinstance(f, _ExtensionSubmoduleFinder) for f in sys.meta_path):
    sys.meta_path.insert(0, _SUBMODULE_FINDER)


def _own_modules(module_name: str) -> list[str]:
    """``module_name`` and its submodules -- never a stdlib / third-party module."""
    prefix = module_name + "."
    return sorted(m for m in sys.modules if m == module_name or m.startswith(prefix))


def _forget_modules(names: Iterable[str]) -> None:
    for name in names:
        sys.modules.pop(name, None)
        _ACTIVE_ROOTS.pop(name, None)


def _field_wire(f: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "key": f.key,
        "type": f.type,
        "label": f.label,
        "description": f.description,
        "required": f.required,
    }
    if f.type == "select":
        item["options"] = list(f.options)
    if f.type == "number":
        item["min"], item["max"] = f.min, f.max
    if f.type != "secret":
        item["default"] = f.default
    return item


async def run_action(
    decl: Any,
    handler: Callable[[ActionCall], Any],
    values: Mapping[str, Any],
    *,
    conversation_id: str = "",
    tab_id: str = "",
) -> ActionResult:
    """Validate ``values`` against the action's fields, then call ``handler``.

    Raises :class:`ActionValidationError` (bad fields, before any extension
    code runs). The handler's own exception propagates to the caller.
    """
    fields = {f.key: f for f in decl.fields}
    errors: dict[str, str] = {k: "is not a field of this action" for k in values if k not in fields}
    clean: dict[str, Any] = {}
    for key, f in fields.items():
        value = values.get(key)
        if value is None or value == "":
            if f.required:
                errors[key] = "is required"
            clean[key] = f.default if f.type != "secret" else None
            continue
        try:
            clean[key] = f.check(value)
        except ValueError as exc:
            errors[key] = str(exc)
    if errors:
        raise ActionValidationError(errors)
    call = ActionCall(
        action_id=decl.id, values=clean, conversation_id=conversation_id, tab_id=tab_id
    )
    is_async = inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        getattr(handler, "__call__", None)  # noqa: B004
    )
    result = handler(call) if is_async else await asyncio.to_thread(handler, call)
    if inspect.isawaitable(result):
        result = await result
    if result is None:
        return ActionResult()
    if isinstance(result, str):
        return ActionResult(message=result)
    if isinstance(result, ActionResult):
        return result
    raise ExtensionError(
        f"action {decl.id!r} returned {type(result).__name__}; return ActionResult, str or None"
    )


async def call_handler(handler: Callable[[Any], Any], arg: Any) -> Any:
    """Run an extension handler: async on the loop, sync in a worker thread."""
    is_async = inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        getattr(handler, "__call__", None)  # noqa: B004
    )
    result = handler(arg) if is_async else await asyncio.to_thread(handler, arg)
    if inspect.isawaitable(result):
        result = await result
    return result


class ActionValidationError(ValueError):
    """``errors`` maps a field key to a user-facing reason."""

    def __init__(self, errors: Mapping[str, str]) -> None:
        self.errors = dict(errors)
        super().__init__("; ".join(f"{k}: {v}" for k, v in self.errors.items()))


def _adapt_handler(handler: Callable[[ToolCall], Any]) -> Callable[[Any], Awaitable[Any]]:
    """Turn the chat registry's request into a :class:`ToolCall`.

    An ``async`` handler runs on the event loop; a plain function runs in a
    worker thread, so a blocking call (network, disk) cannot freeze chat.
    """
    is_async = inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
        getattr(handler, "__call__", None)  # noqa: B004 - async callable objects
    )

    async def _call(request: Any) -> Any:
        call = ToolCall(
            arguments=dict(getattr(request, "arguments", None) or {}),
            tool_name=str(getattr(request, "tool_name", "")),
            conversation_id=str(getattr(request, "conversation_id", "")),
            tab_id=str(getattr(request, "tab_id", "")),
        )
        result = handler(call) if is_async else await asyncio.to_thread(handler, call)
        if inspect.isawaitable(result):
            result = await result
        return result

    return _call


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(v) for v in value)
    return value


def _log_status(status: ExtensionStatus) -> None:
    if status.state == "loaded":
        logger.info(
            "user_extensions.loaded extension=%s version=%s tools=%s skills=%s",
            status.name,
            status.version,
            ",".join(status.tools) or "-",
            ",".join(status.skills) or "-",
        )
    else:
        logger.warning("user_extensions.failed extension=%s error=%s", status.name, status.error)
    for warning in status.warnings:
        logger.warning("user_extensions.warning extension=%s %s", status.name, warning)
