# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Discover, import and wire user extensions (stage 1: chat tools only).

Lifecycle, per extension directory under the scan root:

1. :func:`~.manifest.load_manifest` validates ``extension.json``;
2. the ``python_package`` is imported from ``<ext>/python/<package>/``
   (registered in :data:`sys.modules` under its own name, no ``sys.path``
   change);
3. ``build_services(ctx)`` then ``wire_into_chat(ctx, services)`` run, both
   optional, exactly as in the QAIModelBuilder contract;
4. registrations are STAGED on the context and committed to the
   :class:`ToolSink` only after both hooks returned, so a failing extension
   leaves nothing behind (no partial registration to roll back).

Name policy (design doc §3.6 / §8.6, user decision): a tool keeps the name
its manifest declares. A name already taken -- by a built-in tool, an MCP
tool or an earlier extension -- is REFUSED for that one tool with an
operator-facing reason; the rest of the extension still loads.

Everything here is stdlib-only and knows nothing about the chat context:
the composition root adapts the chat tool registry to :class:`ToolSink`.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Final, Literal, Protocol

from .manifest import ExtensionManifest, ManifestError, ToolDeclaration, load_manifest

__all__ = [
    "ExtensionContractError",
    "ExtensionStatus",
    "RegisteredTool",
    "ToolSink",
    "UserExtensionContext",
    "UserExtensionHost",
    "discover_extension_dirs",
]

logger = logging.getLogger("qai.platform.user_extensions")

#: ``ExtensionContext`` methods of the ModelBuilder contract this host does not
#: implement yet. Calling one is a no-op that records a warning (design doc
#: §1.4 "降级而非拒绝"), so a ModelBuilder extension still loads its tools.
_DEGRADED_METHODS: Final[tuple[str, ...]] = (
    "register_mode",
    "register_prompt_fragment",
    "register_router",
    "register_search_provider",
    "register_search_tool",
    "register_curated_mcp_entry",
    "register_mcp_registry_source",
    "register_skill_market_source",
    "register_login_claims",
    "register_query_mapper",
    "register_query_transport",
    "register_workspace_provider",
    "register_cli_result_renderer",
    "register_cli_retired_config_key",
    "register_cli_command",
    "register_qairt_participant",
    "register_read_only_root",
)

State = Literal["loaded", "failed"]


class ExtensionContractError(Exception):
    """An extension broke the host contract (undeclared tool, bad schema, ...)."""


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


@dataclass(frozen=True, slots=True)
class RegisteredTool:
    name: str
    extension: str
    declaration: ToolDeclaration
    advertised: bool


@dataclass(slots=True)
class ExtensionStatus:
    """Operator-facing outcome of loading one extension directory."""

    name: str
    root: Path
    state: State
    version: str = ""
    error: str = ""
    tools: tuple[str, ...] = ()
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _PendingTool:
    name: str
    handler: Callable[[Any], Awaitable[Any]]
    schema: Mapping[str, Any] | None
    declaration: ToolDeclaration


class UserExtensionContext:
    """The ``ctx`` handed to ``build_services`` / ``wire_into_chat``.

    Implements the subset of the QAIModelBuilder ``ExtensionContext`` that
    stage 1 supports; see :data:`_DEGRADED_METHODS` for the rest.
    """

    def __init__(
        self,
        manifest: ExtensionManifest,
        *,
        taken_names: Callable[[], frozenset[str]],
    ) -> None:
        self._manifest = manifest
        self._taken_names = taken_names
        self._pending: dict[str, _PendingTool] = {}
        self._rollbacks: list[Callable[[], None]] = []
        self._closed = False
        self.warnings: list[str] = []

    # -- identity ---------------------------------------------------------

    @property
    def name(self) -> str:
        return self._manifest.name

    @property
    def root(self) -> Path:
        return self._manifest.root

    @property
    def manifest(self) -> ExtensionManifest:
        return self._manifest

    # -- supported registrations -----------------------------------------

    def register_chat_tool(
        self,
        name: str,
        handler: Callable[..., Any],
        *,
        schema: Any = None,
        descriptor: Any = None,
        validate_arguments: bool = True,
    ) -> None:
        """Stage one chat tool. Mirrors ModelBuilder's signature.

        Contract errors (raise, failing the hook): the name is not declared in
        ``contributes.tools``, the handler is not callable, the schema is not
        an OpenAI function schema for ``name``, or the name is staged twice.

        A name already used by another tool is NOT a contract error: that one
        tool is refused (warning) and the extension keeps loading.
        """
        self._require_open("register_chat_tool")
        decl = self._manifest.tool(name)
        if decl is None:
            raise ExtensionContractError(
                f"register_chat_tool({name!r}): not declared in contributes.tools"
            )
        if not callable(handler):
            raise ExtensionContractError(f"register_chat_tool({name!r}): handler is not callable")
        if name in self._pending:
            raise ExtensionContractError(f"register_chat_tool({name!r}): registered twice")
        if schema is not None:
            _check_schema(name, schema)
        if descriptor is not None:
            self.warnings.append(
                f"register_chat_tool({name!r}): descriptor= is ignored; "
                "the declaration in contributes.tools is used"
            )
        if name in self._taken_names():
            self.warnings.append(
                f"tool {name!r} refused: the name is already used by another tool"
            )
            return
        self._pending[name] = _PendingTool(
            name=name,
            handler=_as_async(handler),
            schema=dict(schema) if schema is not None else None,
            declaration=decl,
        )

    def on_rollback(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` if this extension fails to load (newest first)."""
        self._require_open("on_rollback")
        if not callable(callback):
            raise ExtensionContractError("on_rollback: callback is not callable")
        self._rollbacks.append(callback)

    def load_config(self, *_args: Any, **_kwargs: Any) -> Any:
        raise ExtensionContractError(
            "load_config is not supported yet (contributes.settings is not implemented)"
        )

    # -- degraded registrations ------------------------------------------

    def __getattr__(self, attr: str) -> Any:
        if attr in _DEGRADED_METHODS:

            def _degraded(*_args: Any, **_kwargs: Any) -> None:
                self.warnings.append(f"{attr} is not supported yet and was ignored")

            return _degraded
        raise AttributeError(
            f"{type(self).__name__!s} has no attribute {attr!r} (not part of the "
            "supported extension API)"
        )

    # -- host side --------------------------------------------------------

    def _require_open(self, what: str) -> None:
        if self._closed:
            raise ExtensionContractError(f"{what}: called after the WIRE phase ended")

    def _close(self) -> list[_PendingTool]:
        self._closed = True
        return list(self._pending.values())

    def _rollback(self) -> None:
        self._closed = True
        self._pending.clear()
        for callback in reversed(self._rollbacks):
            try:
                callback()
            except Exception:
                logger.warning(
                    "user_extensions.rollback_callback_failed extension=%s", self.name,
                    exc_info=True,
                )


class UserExtensionHost:
    """Loads every extension under ``scan_root`` into a :class:`ToolSink`."""

    def __init__(self, scan_root: Path, sink: ToolSink) -> None:
        self._scan_root = scan_root
        self._sink = sink
        self._statuses: dict[str, ExtensionStatus] = {}
        self._tools: dict[str, RegisteredTool] = {}
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
            name=manifest.name,
            root=ext_dir,
            state="failed",
            version=manifest.version,
            warnings=list(manifest.warnings),
        )
        if manifest.name in self._statuses and self._statuses[manifest.name].state == "loaded":
            status.error = "an extension with this name is already loaded"
            return status
        if manifest.python_package is None:
            status.state = "loaded"
            return status

        ctx = UserExtensionContext(manifest, taken_names=self._sink.tool_names)
        package = manifest.python_package
        try:
            module = _import_package(manifest)
            build = getattr(module, "build_services", None)
            wire = getattr(module, "wire_into_chat", None)
            services = build(ctx) if callable(build) else None
            if callable(wire):
                wire(ctx, services)
        except Exception as exc:  # noqa: BLE001 - extension code is untrusted
            ctx._rollback()
            _forget_modules(_own_modules(package))
            status.warnings.extend(ctx.warnings)
            status.error = f"{type(exc).__name__}: {exc}"
            return status

        self._modules[manifest.name] = _own_modules(package)
        committed: list[str] = []
        for pending in ctx._close():
            # Re-check: another tool may have taken the name meanwhile.
            if pending.name in self._sink.tool_names():
                ctx.warnings.append(
                    f"tool {pending.name!r} refused: the name is already used by another tool"
                )
                continue
            self._sink.register(pending.name, pending.handler, schema=pending.schema)
            self._tools[pending.name] = RegisteredTool(
                name=pending.name,
                extension=manifest.name,
                declaration=pending.declaration,
                advertised=pending.schema is not None,
            )
            committed.append(pending.name)
        undeclared = sorted(manifest.tool_names - set(committed))
        for name in undeclared:
            if not any(name in w for w in ctx.warnings):
                ctx.warnings.append(f"tool {name!r} is declared but was never registered")
        status.state = "loaded"
        status.tools = tuple(committed)
        status.warnings.extend(ctx.warnings)
        return status

    def unload_all(self) -> None:
        """Unregister every committed tool and forget imported modules."""
        for name in list(self._tools):
            self._sink.unregister(name)
        self._tools.clear()
        for modules in self._modules.values():
            _forget_modules(modules)
        self._modules.clear()
        self._statuses.clear()


def discover_extension_dirs(scan_root: Path) -> list[Path]:
    """Immediate sub-directories of ``scan_root`` that may hold an extension.

    ``_``/``.``-prefixed directories (templates, VCS metadata) and
    ``__pycache__`` are skipped, matching ModelBuilder. Sorted by name so
    load order -- and therefore which extension wins a name -- is stable.
    """
    if not scan_root.is_dir():
        return []
    out = []
    for entry in sorted(scan_root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name.startswith((".", "_")):
            continue
        out.append(entry)
    return out


def _import_package(manifest: ExtensionManifest) -> ModuleType:
    package = manifest.python_package
    assert package is not None
    pkg_dir = manifest.root / "python" / package
    init = pkg_dir / "__init__.py"
    if not init.is_file():
        raise ExtensionContractError(f"python package not found: {init}")
    if package in sys.modules:
        raise ExtensionContractError(f"module {package!r} is already imported by another owner")
    spec = importlib.util.spec_from_file_location(
        package, init, submodule_search_locations=[str(pkg_dir)]
    )
    if spec is None or spec.loader is None:
        raise ExtensionContractError(f"cannot import {init}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[package] = module
    spec.loader.exec_module(module)
    return module


def _own_modules(package: str) -> list[str]:
    """``package`` and its submodules -- never a stdlib / third-party module."""
    prefix = package + "."
    return sorted(m for m in sys.modules if m == package or m.startswith(prefix))


def _forget_modules(names: Iterable[str]) -> None:
    for name in names:
        sys.modules.pop(name, None)


def _as_async(handler: Callable[..., Any]) -> Callable[[Any], Awaitable[Any]]:
    async def _call(request: Any) -> Any:
        result = handler(request)
        if inspect.isawaitable(result):
            result = await result
        return result

    return _call


def _check_schema(name: str, schema: Any) -> None:
    if not isinstance(schema, Mapping) or schema.get("type") != "function":
        raise ExtensionContractError(
            f"register_chat_tool({name!r}): schema must be an OpenAI function schema "
            '({"type": "function", "function": {...}})'
        )
    function = schema.get("function")
    if not isinstance(function, Mapping) or function.get("name") != name:
        raise ExtensionContractError(
            f"register_chat_tool({name!r}): schema.function.name must be {name!r}"
        )


def _log_status(status: ExtensionStatus) -> None:
    if status.state == "loaded":
        logger.info(
            "user_extensions.loaded extension=%s version=%s tools=%s",
            status.name, status.version, ",".join(status.tools) or "-",
        )
    else:
        logger.warning(
            "user_extensions.failed extension=%s error=%s", status.name, status.error
        )
    for warning in status.warnings:
        logger.warning("user_extensions.warning extension=%s %s", status.name, warning)
