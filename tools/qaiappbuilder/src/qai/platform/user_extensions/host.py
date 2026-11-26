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
import importlib.util
import inspect
import json
import logging
import sys
import tomllib
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Final, Literal, Protocol

from .manifest import (
    DEFAULT_SKILLS_DIR,
    ExtensionManifest,
    ManifestError,
    ToolDeclaration,
    load_manifest,
)

__all__ = [
    "CONFIG_FILENAME",
    "DATA_DIRNAME",
    "ExtensionContext",
    "ExtensionError",
    "ExtensionSecrets",
    "ExtensionSkillRoot",
    "ExtensionStatus",
    "RegisteredTool",
    "SecretStoreLike",
    "ToolCall",
    "ToolSink",
    "UserExtensionHost",
    "discover_extension_dirs",
    "module_name_for",
]

logger = logging.getLogger("qai.platform.user_extensions")

#: ``<ext>/config.toml``: the extension's own read-only defaults.
CONFIG_FILENAME: Final[str] = "config.toml"
#: ``<data>/extensions-data/<name>/``: per-extension writable data, kept apart
#: from the code directory so removing / replacing the code keeps the data.
DATA_DIRNAME: Final[str] = "extensions-data"
_MODULE_PREFIX: Final[str] = "qai_user_ext_"

State = Literal["loaded", "failed"]


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
class RegisteredTool:
    name: str
    extension: str
    declaration: ToolDeclaration


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
    ``version``, ``root``, ``data_dir``, ``config``, ``logger``, ``secrets``,
    ``proxy_url()``, ``ssl_verify``, ``register_tool()``, ``on_unload()``.

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
    ) -> None:
        self._manifest = manifest
        self._data_dir = data_root / DATA_DIRNAME / manifest.name
        self._taken_names = taken_names
        self._proxy_url = proxy_url or (lambda: None)
        self._ssl_verify = ssl_verify or (lambda: True)
        self._pending: dict[str, _PendingTool] = {}
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
        callbacks, self._unload = self._unload, []
        for callback in reversed(callbacks):
            try:
                callback()
            except Exception:
                logger.warning(
                    "user_extensions.unload_callback_failed extension=%s", self.name, exc_info=True
                )


class UserExtensionHost:
    """Loads every extension under ``scan_root`` into a :class:`ToolSink`."""

    def __init__(
        self,
        scan_root: Path,
        sink: ToolSink,
        *,
        data_root: Path,
        taken_skill_ids: Callable[[], frozenset[str]] | None = None,
        secret_store: SecretStoreLike | None = None,
        proxy_url: Callable[[], str | None] | None = None,
        ssl_verify: Callable[[], bool] | None = None,
    ) -> None:
        """``taken_skill_ids`` returns the ids of the app's own skills; an
        extension skill with one of those ids is refused (same rule as tools)."""
        self._scan_root = scan_root
        self._sink = sink
        self._data_root = data_root
        self._taken_skill_ids = taken_skill_ids or frozenset
        self._services = {
            "secret_store": secret_store,
            "proxy_url": proxy_url,
            "ssl_verify": ssl_verify,
        }
        self._statuses: dict[str, ExtensionStatus] = {}
        self._tools: dict[str, RegisteredTool] = {}
        self._skill_roots: dict[str, ExtensionSkillRoot] = {}
        self._contexts: dict[str, ExtensionContext] = {}
        self._modules: dict[str, list[str]] = {}

    @property
    def scan_root(self) -> Path:
        return self._scan_root

    @property
    def statuses(self) -> tuple[ExtensionStatus, ...]:
        return tuple(self._statuses[k] for k in sorted(self._statuses))

    @property
    def tools(self) -> Mapping[str, RegisteredTool]:
        return dict(self._tools)

    def tools_of_group(self, group: str) -> frozenset[str]:
        return frozenset(t.name for t in self._tools.values() if t.declaration.group == group)

    def skill_roots(self) -> tuple[ExtensionSkillRoot, ...]:
        """Skill roots of loaded extensions, sorted by extension name."""
        return tuple(self._skill_roots[k] for k in sorted(self._skill_roots))

    def skill_default_modes(self) -> dict[str, str]:
        """``skill id -> default mode`` across loaded extensions (visible skills only)."""
        out: dict[str, str] = {}
        for root in self.skill_roots():
            for skill_id, mode in root.default_modes.items():
                if skill_id in root.skill_ids:
                    out.setdefault(skill_id, mode)
        return out

    def load_all(self) -> tuple[ExtensionStatus, ...]:
        for ext_dir in discover_extension_dirs(self._scan_root):
            status = self.load(ext_dir)
            self._statuses[status.name] = status
            _log_status(status)
        return self.statuses

    def load(self, ext_dir: Path) -> ExtensionStatus:
        """Load one extension directory. Never raises."""
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
        if manifest.entry is None:
            status.state = "loaded"
            self._commit_skills(manifest, status)
            return status

        ctx = ExtensionContext(
            manifest,
            data_root=self._data_root,
            taken_names=self._sink.tool_names,
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
            if pending.name in self._sink.tool_names():
                ctx._warnings.append(
                    f"tool {pending.name!r} refused: the name is already used by another tool"
                )
                continue
            self._sink.register(pending.name, pending.handler, schema=pending.schema)
            self._tools[pending.name] = RegisteredTool(
                name=pending.name, extension=manifest.name, declaration=pending.declaration
            )
            committed.append(pending.name)
        for name in sorted(manifest.tool_names - set(committed)):
            if not any(repr(name) in w for w in ctx._warnings):
                ctx._warnings.append(f"tool {name!r} is declared but was never registered")
        status.state = "loaded"
        status.tools = tuple(committed)
        status.warnings.extend(ctx._warnings)
        self._commit_skills(manifest, status)
        return status

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
        """Unregister tools, then run ``on_unload`` callbacks, then forget modules.

        Tools go first so no new call reaches an extension whose resources its
        callbacks are tearing down.
        """
        for name in list(self._tools):
            self._sink.unregister(name)
        self._tools.clear()
        for name in sorted(self._contexts, reverse=True):
            self._contexts[name]._run_unload()
        self._contexts.clear()
        self._skill_roots.clear()
        for modules in self._modules.values():
            _forget_modules(modules)
        self._modules.clear()
        self._statuses.clear()


def module_name_for(extension: str) -> str:
    """The private :data:`sys.modules` name of an extension's entry."""
    return _MODULE_PREFIX + extension.replace("-", "_")


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
    spec = importlib.util.spec_from_file_location(
        module_name, file, submodule_search_locations=[str(target)] if is_package else None
    )
    if module_name in sys.modules:
        raise ExtensionError(f"module {module_name!r} is already imported")
    if spec is None or spec.loader is None:
        raise ExtensionError(f"cannot import {target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _own_modules(module_name: str) -> list[str]:
    """``module_name`` and its submodules -- never a stdlib / third-party module."""
    prefix = module_name + "."
    return sorted(m for m in sys.modules if m == module_name or m.startswith(prefix))


def _forget_modules(names: Iterable[str]) -> None:
    for name in names:
        sys.modules.pop(name, None)


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
