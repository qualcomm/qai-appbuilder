# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``extension.json`` parsing + validation for user extensions.

appbuilder's own manifest format (design doc §4.2). Example::

    {
        "manifest_version": 1,
        "name": "demo-ext",
        "version": "1.0.0",
        "description": "What it does",
        "entry": "main.py",
        "tools": [{"name": "demo_echo", "group": "read"}],
        "skills": {"dir": "skills", "default_modes": {"demo-skill": "both"}},
    }

Rules:

* ``manifest_version`` must be :data:`MANIFEST_VERSION`; an unknown key is an
  error (a typo must not silently disable a feature). A later format bumps
  the version instead of adding keys an older host would reject.
* ``name``: ``^[a-z][a-z0-9]*(-[a-z0-9]+)*$``, <= 64, equals the directory name.
* ``description``: a string or a ``{locale: text}`` object.
* ``entry`` (optional): relative path of a ``.py`` file or of a package
  directory (with ``__init__.py``) that defines ``activate(ctx)``.
* ``tools[].name``: ``[a-z0-9_]``, starts with a letter, does not end with
  ``_`` (the model-side name sanitizer strips it), <= 64, no ``__``
  (reserved for MCP ``server__tool``); ``tools[].group``: ``read`` / ``edit``
  / ``command`` / ``null`` -- required, so the author decides consciously
  whether persona permission groups can switch the tool off.
* ``skills.dir`` (default ``skills``) and ``skills.default_modes``
  (``skill id -> off/cloud/local/both``).
* ``prompts[]``: ``{when: always|active, text | file}`` (design doc §5.1.3).
* ``settings[]``: fields of the user-editable settings card,
  ``{key, type: text|secret|bool|number|select, label, ...}``; ``secret``
  values are write-only and stored in the extension's secret space.

Pure stdlib so the platform package stays dependency-free.
"""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

__all__ = [
    "DEFAULT_SKILLS_DIR",
    "MANIFEST_FILENAME",
    "MANIFEST_VERSION",
    "MCP_TRANSPORTS",
    "PROMPT_MAX_CHARS",
    "PROMPT_WHEN",
    "ROUTE_METHODS",
    "SETTING_TYPES",
    "UI_MODULE_SUFFIXES",
    "ActionDeclaration",
    "ExtensionManifest",
    "ManifestError",
    "McpServerDeclaration",
    "PromptDeclaration",
    "RouteDeclaration",
    "SettingDeclaration",
    "SkillsDeclaration",
    "ToolDeclaration",
    "UiDeclaration",
    "load_manifest",
    "parse_manifest",
    "placeholders_in",
]

MANIFEST_FILENAME: Final[str] = "extension.json"
#: The manifest format this host implements.
MANIFEST_VERSION: Final[int] = 1
DEFAULT_SKILLS_DIR: Final[str] = "skills"

_EXT_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_TOOL_NAME_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z]([a-z0-9_]*[a-z0-9])?$")
_NAME_MAX: Final[int] = 64
#: Forward-slash relative path; no ``..``, no leading ``/`` or ``.`` segment.
_REL_PATH_RE: Final[re.Pattern[str]] = re.compile(
    r"^[A-Za-z0-9_-][A-Za-z0-9_.-]*(/[A-Za-z0-9_-][A-Za-z0-9_.-]*)*/?$"
)

_GROUPS: Final[frozenset[str | None]] = frozenset({"read", "edit", "command", None})
_SKILL_MODES: Final[frozenset[str]] = frozenset({"off", "cloud", "local", "both"})

_TOP_KEYS: Final[frozenset[str]] = frozenset(
    {
        "manifest_version",
        "name",
        "version",
        "description",
        "entry",
        "tools",
        "skills",
        "prompts",
        "settings",
        "mcp_servers",
        "dynamic_mcp_servers",
        "ui",
        "routes",
    }
)
_ROUTE_KEYS: Final[frozenset[str]] = frozenset({"method", "path", "max_body_mb", "reload_runtime"})
#: Default / ceiling of one request body for an extension route (MB).
ROUTE_BODY_DEFAULT_MB: Final[int] = 10
ROUTE_BODY_MAX_MB: Final[int] = 1024
ROUTE_METHODS: Final[frozenset[str]] = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})
#: ``status``, ``hosts/{host_id}``: lowercase segments, ``{param}`` captures one segment.
_ROUTE_SEGMENT_RE: Final[re.Pattern[str]] = re.compile(
    r"^([a-z0-9][a-z0-9_.-]*|\{[a-z][a-z0-9_]*\})$"
)
_MAX_ROUTES: Final[int] = 32
_ROUTE_PATH_MAX: Final[int] = 200
_UI_KEYS: Final[frozenset[str]] = frozenset({"label", "icon", "actions", "module"})
#: ``ui.module`` must be an ES module in a SUBDIRECTORY (only that directory is
#: served to the browser; the extension's Python and config stay private).
UI_MODULE_SUFFIXES: Final[tuple[str, ...]] = (".js", ".mjs")
_ACTION_KEYS: Final[frozenset[str]] = frozenset({"id", "label", "description", "fields"})
_ACTION_ID_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_ICON_MAX: Final[int] = 8
_MAX_ACTIONS: Final[int] = 8
_MCP_KEYS: Final[frozenset[str]] = frozenset(
    {"name", "transport", "command", "args", "env", "cwd", "url", "headers", "timeout_s"}
)
_MCP_STDIO_ONLY: Final[tuple[str, ...]] = ("command", "args", "env", "cwd")
_MCP_REMOTE_ONLY: Final[tuple[str, ...]] = ("url", "headers")
MCP_TRANSPORTS: Final[frozenset[str]] = frozenset({"stdio", "http", "sse"})
#: ``${root}`` / ``${data_dir}`` / ``${python}`` / ``${setting:KEY}`` / ``${secret:KEY}``.
_PLACEHOLDER_RE: Final[re.Pattern[str]] = re.compile(r"\$\{([^}]*)\}")
_PLAIN_PLACEHOLDERS: Final[frozenset[str]] = frozenset({"root", "data_dir", "python"})
_SETTING_KEYS: Final[frozenset[str]] = frozenset(
    {"key", "type", "label", "description", "default", "required", "options", "min", "max"}
)
#: ``secret`` values go to the extension's secret space and are write-only.
SETTING_TYPES: Final[frozenset[str]] = frozenset({"text", "secret", "bool", "number", "select"})
_SETTING_KEY_RE: Final[re.Pattern[str]] = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SETTING_TEXT_MAX: Final[int] = 4000
_PROMPT_KEYS: Final[frozenset[str]] = frozenset({"text", "file", "when"})
#: ``always``: end of the system prompt on every turn (changes only when the
#: extension is installed / reloaded, so the prompt cache stays valid).
#: ``active``: only while the user has this extension's entry selected; sent
#: as a context message, never in the system prompt.
PROMPT_WHEN: Final[frozenset[str]] = frozenset({"always", "active"})
#: Per fragment, after reading ``file``: keeps one extension from flooding
#: every turn's context.
PROMPT_MAX_CHARS: Final[int] = 8000
_REQUIRED_TOP: Final[tuple[str, ...]] = ("manifest_version", "name", "version", "description")
_TOOL_KEYS: Final[frozenset[str]] = frozenset({"name", "group"})
_SKILLS_KEYS: Final[frozenset[str]] = frozenset({"dir", "default_modes"})


class ManifestError(ValueError):
    """The manifest is missing, unreadable or invalid. The message names the field."""


@dataclass(frozen=True, slots=True)
class ToolDeclaration:
    """One ``tools[]`` entry."""

    name: str
    group: str | None


@dataclass(frozen=True, slots=True)
class SkillsDeclaration:
    """``skills``: the directory (relative to the extension root) + default modes."""

    dir: str = DEFAULT_SKILLS_DIR
    default_modes: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SettingDeclaration:
    """One ``settings[]`` entry: a field the user fills in on the settings card."""

    key: str
    type: str
    label: str
    description: str = ""
    default: Any = None
    required: bool = False
    options: tuple[str, ...] = ()
    min: float | None = None
    max: float | None = None

    def check(self, value: Any) -> Any:
        """The value normalised, or :class:`ValueError` with a user-facing reason."""
        if self.type in ("text", "secret"):
            if not isinstance(value, str):
                raise ValueError("must be text")
            if len(value) > _SETTING_TEXT_MAX:
                raise ValueError(f"is longer than {_SETTING_TEXT_MAX} characters")
            return value
        if self.type == "bool":
            if not isinstance(value, bool):
                raise ValueError("must be true or false")
            return value
        if self.type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("must be a number")
            if self.min is not None and value < self.min:
                raise ValueError(f"must be >= {self.min:g}")
            if self.max is not None and value > self.max:
                raise ValueError(f"must be <= {self.max:g}")
            return value
        if value not in self.options:  # select
            raise ValueError(f"must be one of {', '.join(self.options)}")
        return value


@dataclass(frozen=True, slots=True)
class RouteDeclaration:
    """One ``routes[]`` entry, served at ``/api/ext/<extension>/<path>``."""

    method: str
    path: str
    #: Largest request body this route accepts (an upload route raises it).
    max_body_mb: int = ROUTE_BODY_DEFAULT_MB
    reload_runtime: bool = False

    @property
    def segments(self) -> tuple[str, ...]:
        return tuple(self.path.split("/"))

    def match(self, method: str, path: str) -> dict[str, str] | None:
        """Path parameters when ``method path`` matches this route, else ``None``."""
        if method != self.method:
            return None
        parts = path.strip("/").split("/") if path.strip("/") else []
        if len(parts) != len(self.segments):
            return None
        params: dict[str, str] = {}
        for seg, part in zip(self.segments, parts, strict=True):
            if seg.startswith("{"):
                if not part:
                    return None
                params[seg[1:-1]] = part
            elif seg != part:
                return None
        return params


@dataclass(frozen=True, slots=True)
class ActionDeclaration:
    """One ``ui.actions[]`` entry: a button in the extension's mode; clicking
    it opens a dialog with ``fields`` (same types as ``settings[]``)."""

    id: str
    label: str
    description: str = ""
    fields: tuple[SettingDeclaration, ...] = ()


@dataclass(frozen=True, slots=True)
class UiDeclaration:
    """``ui``: the extension's entry in the composer's Extension menu."""

    label: str
    icon: str = ""
    actions: tuple[ActionDeclaration, ...] = ()
    #: Relative path of the extension's front-end ES module, e.g. ``web/index.js``.
    module: str | None = None

    @property
    def module_dir(self) -> str | None:
        """The directory served to the browser (the module's parent), e.g. ``web``."""
        return self.module.rsplit("/", 1)[0] if self.module else None

    @property
    def module_file(self) -> str | None:
        """The module's path inside :attr:`module_dir`, e.g. ``index.js``."""
        return self.module.rsplit("/", 1)[1] if self.module else None

    def action(self, action_id: str) -> ActionDeclaration | None:
        return next((a for a in self.actions if a.id == action_id), None)


@dataclass(frozen=True, slots=True)
class McpServerDeclaration:
    """One ``mcp_servers[]`` entry. String values may hold placeholders
    (resolved by the host at load; see :func:`placeholders_in`)."""

    name: str
    transport: str = "stdio"
    command: str | None = None
    args: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout_s: float = 30.0


@dataclass(frozen=True, slots=True)
class PromptDeclaration:
    """One ``prompts[]`` entry: exactly one of ``text`` / ``file`` is set."""

    when: str
    text: str | None = None
    #: Relative path (validated) of a UTF-8 text file inside the extension.
    file: str | None = None


@dataclass(frozen=True, slots=True)
class ExtensionManifest:
    name: str
    version: str
    description: str
    root: Path
    #: Relative path of the Python entry (file or package dir); ``None`` = no code.
    entry: str | None = None
    tools: tuple[ToolDeclaration, ...] = ()
    skills: SkillsDeclaration = field(default_factory=SkillsDeclaration)
    prompts: tuple[PromptDeclaration, ...] = ()
    settings: tuple[SettingDeclaration, ...] = ()
    mcp_servers: tuple[McpServerDeclaration, ...] = ()
    ui: UiDeclaration | None = None
    routes: tuple[RouteDeclaration, ...] = ()
    #: The extension's code may add MCP servers at run time
    #: (``ctx.register_mcp_server``); shown at install so the user knows.
    dynamic_mcp_servers: bool = False

    def route(self, method: str, path: str) -> RouteDeclaration | None:
        return next((r for r in self.routes if r.method == method and r.path == path), None)

    @property
    def tool_names(self) -> frozenset[str]:
        return frozenset(t.name for t in self.tools)

    def tool(self, name: str) -> ToolDeclaration | None:
        return next((t for t in self.tools if t.name == name), None)


def load_manifest(ext_dir: Path) -> ExtensionManifest:
    """Read + validate ``<ext_dir>/extension.json``."""
    path = ext_dir / MANIFEST_FILENAME
    if not path.is_file():
        raise ManifestError(f"{MANIFEST_FILENAME} not found in {ext_dir}")
    try:
        # ``utf-8-sig`` tolerates a BOM written by Windows editors.
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(f"cannot read {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ManifestError(f"{path} is not valid JSON: {exc}") from exc
    return parse_manifest(raw, root=ext_dir)


def parse_manifest(raw: Any, *, root: Path) -> ExtensionManifest:
    """Validate an already-decoded manifest object."""
    obj = _object(raw, "manifest")
    _known_keys(obj, _TOP_KEYS, where="")
    _require(obj, _REQUIRED_TOP, where="")
    version_no = obj["manifest_version"]
    if version_no != MANIFEST_VERSION or isinstance(version_no, bool):
        raise ManifestError(
            f"manifest_version {version_no!r} is not supported (this host implements "
            f"{MANIFEST_VERSION})"
        )
    name = _parse_name(obj["name"], root=root)
    version = obj["version"]
    if not isinstance(version, str) or not version.strip():
        raise ManifestError("version must be a non-empty string")
    entry = obj.get("entry")
    if entry is not None:
        entry = _rel_path(entry, label="entry")
    tools = _parse_tools(obj.get("tools"))
    if tools and entry is None:
        raise ManifestError("tools requires entry (the code that registers them)")
    ui = _parse_ui(obj.get("ui"))
    if ui is not None and ui.actions and entry is None:
        raise ManifestError("ui.actions requires entry (the code that handles them)")
    return ExtensionManifest(
        ui=ui,
        name=name,
        version=version,
        description=_text(obj["description"], label="description"),
        root=root,
        entry=entry,
        tools=tools,
        skills=_parse_skills(obj.get("skills")),
        prompts=_parse_prompts(obj.get("prompts")),
        settings=(settings := _parse_settings(obj.get("settings"))),
        mcp_servers=_parse_mcp_servers(obj.get("mcp_servers"), settings),
        routes=_parse_routes(obj.get("routes"), has_entry=entry is not None),
        dynamic_mcp_servers=_parse_dynamic_mcp(obj.get("dynamic_mcp_servers"), entry),
    )


def _parse_dynamic_mcp(raw: Any, entry: str | None) -> bool:
    if raw is None:
        return False
    if not isinstance(raw, bool):
        raise ManifestError("dynamic_mcp_servers must be true or false")
    if raw and entry is None:
        raise ManifestError("dynamic_mcp_servers requires entry (the code that registers them)")
    return raw


def _parse_routes(  # noqa: PLR0912 - validate each independent route field
    raw: Any, *, has_entry: bool
) -> tuple[RouteDeclaration, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError("routes must be an array")
    if raw and not has_entry:
        raise ManifestError("routes requires entry (the code that handles them)")
    if len(raw) > _MAX_ROUTES:
        raise ManifestError(f"routes: at most {_MAX_ROUTES} routes")
    out: list[RouteDeclaration] = []
    for index, item in enumerate(raw):
        where = f"routes[{index}]"
        entry = _object(item, where)
        _known_keys(entry, _ROUTE_KEYS, where=where)
        _require(entry, ("method", "path"), where=where)
        method, path = entry["method"], entry["path"]
        if method not in ROUTE_METHODS:
            raise ManifestError(
                f"{where}.method {method!r} must be one of {', '.join(sorted(ROUTE_METHODS))}"
            )
        if not isinstance(path, str) or not path or len(path) > _ROUTE_PATH_MAX:
            raise ManifestError(f"{where}.path must be a non-empty string")
        segments = path.split("/")
        if not all(_ROUTE_SEGMENT_RE.match(s) for s in segments):
            raise ManifestError(
                f"{where}.path {path!r}: segments are lowercase [a-z0-9_.-] or {{param}}, "
                "no leading / trailing slash"
            )
        names = [s[1:-1] for s in segments if s.startswith("{")]
        if len(names) != len(set(names)):
            raise ManifestError(f"{where}.path {path!r} repeats a parameter name")
        max_mb = entry.get("max_body_mb", ROUTE_BODY_DEFAULT_MB)
        if (
            isinstance(max_mb, bool)
            or not isinstance(max_mb, int)
            or not 1 <= max_mb <= ROUTE_BODY_MAX_MB
        ):
            raise ManifestError(f"{where}.max_body_mb must be an integer 1..{ROUTE_BODY_MAX_MB}")
        reload_runtime = entry.get("reload_runtime", False)
        if not isinstance(reload_runtime, bool):
            raise ManifestError(f"{where}.reload_runtime must be a boolean")
        decl = RouteDeclaration(
            method=method, path=path, max_body_mb=max_mb, reload_runtime=reload_runtime
        )
        shape = tuple("{}" if s.startswith("{") else s for s in segments)
        for other in out:
            if (
                other.method == method
                and tuple("{}" if s.startswith("{") else s for s in other.segments) == shape
            ):
                raise ManifestError(
                    f"{where}: {method} {path} clashes with {other.method} {other.path}"
                )
        out.append(decl)
    return tuple(out)


def _parse_ui(raw: Any) -> UiDeclaration | None:
    if raw is None:
        return None
    obj = _object(raw, "ui")
    _known_keys(obj, _UI_KEYS, where="ui")
    _require(obj, ("label",), where="ui")
    icon = obj.get("icon", "")
    if not isinstance(icon, str) or len(icon) > _ICON_MAX:
        raise ManifestError(f"ui.icon must be a short string (an emoji, <= {_ICON_MAX} chars)")
    raw_actions = obj.get("actions", [])
    if not isinstance(raw_actions, list):
        raise ManifestError("ui.actions must be an array")
    if len(raw_actions) > _MAX_ACTIONS:
        raise ManifestError(f"ui.actions: at most {_MAX_ACTIONS} actions")
    actions: list[ActionDeclaration] = []
    for index, item in enumerate(raw_actions):
        where = f"ui.actions[{index}]"
        entry = _object(item, where)
        _known_keys(entry, _ACTION_KEYS, where=where)
        _require(entry, ("id", "label"), where=where)
        action_id = entry["id"]
        if not isinstance(action_id, str) or not _ACTION_ID_RE.match(action_id):
            raise ManifestError(f"{where}.id {action_id!r} must match ^[a-z][a-z0-9_]{{0,63}}$")
        if any(a.id == action_id for a in actions):
            raise ManifestError(f"{where}.id {action_id!r} is declared twice")
        raw_fields = entry.get("fields", [])
        if not isinstance(raw_fields, list):
            raise ManifestError(f"{where}.fields must be an array")
        fields: list[SettingDeclaration] = []
        for fi, field_raw in enumerate(raw_fields):
            f = _parse_setting(field_raw, where=f"{where}.fields[{fi}]")
            if any(x.key == f.key for x in fields):
                raise ManifestError(f"{where}.fields[{fi}].key {f.key!r} is declared twice")
            fields.append(f)
        actions.append(
            ActionDeclaration(
                id=action_id,
                label=_text(entry["label"], label=f"{where}.label"),
                description=_text(entry["description"], label=f"{where}.description")
                if "description" in entry
                else "",
                fields=tuple(fields),
            )
        )
    return UiDeclaration(
        label=_text(obj["label"], label="ui.label"),
        icon=icon,
        actions=tuple(actions),
        module=_parse_ui_module(obj.get("module")),
    )


def _parse_ui_module(raw: Any) -> str | None:
    if raw is None:
        return None
    module = _rel_path(raw, label="ui.module")
    if "/" not in module:
        raise ManifestError(
            f"ui.module {module!r} must be inside a subdirectory such as 'web/index.js' "
            "(only that directory is served to the browser)"
        )
    if not module.endswith(UI_MODULE_SUFFIXES):
        raise ManifestError(f"ui.module {module!r} must be a .js or .mjs file")
    return module


def placeholders_in(value: str) -> list[str]:
    """The placeholder bodies in ``value`` (``"${secret:k}"`` -> ``["secret:k"]``)."""
    return _PLACEHOLDER_RE.findall(value)


def _parse_mcp_servers(
    raw: Any, settings: tuple[SettingDeclaration, ...]
) -> tuple[McpServerDeclaration, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError("mcp_servers must be an array")
    kinds = {s.key: s.type for s in settings}
    out: list[McpServerDeclaration] = []
    for index, item in enumerate(raw):
        decl = _parse_mcp_server(item, where=f"mcp_servers[{index}]", kinds=kinds)
        if any(d.name == decl.name for d in out):
            raise ManifestError(f"mcp_servers[{index}].name {decl.name!r} is declared twice")
        out.append(decl)
    return tuple(out)


def _parse_mcp_server(item: Any, *, where: str, kinds: Mapping[str, str]) -> McpServerDeclaration:
    entry = _object(item, where)
    _known_keys(entry, _MCP_KEYS, where=where)
    _require(entry, ("name",), where=where)
    name = entry["name"]
    if not isinstance(name, str) or len(name) > _NAME_MAX or not _EXT_NAME_RE.match(name):
        raise ManifestError(
            f"{where}.name {name!r} must match ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ (max {_NAME_MAX})"
        )
    transport = entry.get("transport", "stdio")
    if transport not in MCP_TRANSPORTS:
        raise ManifestError(f"{where}.transport {transport!r} must be stdio, http or sse")
    stdio = transport == "stdio"
    required, forbidden = ("command", _MCP_REMOTE_ONLY) if stdio else ("url", _MCP_STDIO_ONLY)
    if required not in entry:
        raise ManifestError(f"{where} with transport {transport!r} needs '{required}'")
    for key in forbidden:
        if key in entry:
            raise ManifestError(f"{where}.{key} is not valid for transport {transport!r}")
    timeout = entry.get("timeout_s", 30.0)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0:
        raise ManifestError(f"{where}.timeout_s must be a positive number")
    decl = McpServerDeclaration(
        name=name,
        transport=transport,
        command=_mcp_str(entry, "command", where),
        args=tuple(_mcp_str_list(entry.get("args"), f"{where}.args")),
        env=_mcp_str_map(entry.get("env"), f"{where}.env"),
        cwd=_mcp_str(entry, "cwd", where),
        url=_mcp_str(entry, "url", where),
        headers=_mcp_str_map(entry.get("headers"), f"{where}.headers"),
        timeout_s=float(timeout),
    )
    for value in _mcp_values(decl):
        for ph in placeholders_in(value):
            _check_placeholder(ph, where=where, kinds=kinds)
    return decl


def _mcp_values(decl: McpServerDeclaration) -> list[str]:
    values = [v for v in (decl.command, decl.cwd, decl.url) if v is not None]
    return [*values, *decl.args, *decl.env.values(), *decl.headers.values()]


def _check_placeholder(ph: str, *, where: str, kinds: Mapping[str, str]) -> None:
    if ph in _PLAIN_PLACEHOLDERS:
        return
    kind, _, key = ph.partition(":")
    if kind == "setting" and kinds.get(key) not in (None, "secret"):
        return
    if kind == "secret" and kinds.get(key) == "secret":
        return
    if kind in ("setting", "secret"):
        wanted = "secret" if kind == "secret" else "non-secret"
        raise ManifestError(f"{where}: ${{{ph}}} must name a declared {wanted} setting")
    raise ManifestError(
        f"{where}: unknown placeholder ${{{ph}}} (use root, data_dir, python, "
        "setting:KEY or secret:KEY)"
    )


def _mcp_str(entry: Mapping[str, Any], key: str, where: str) -> str | None:
    if key not in entry:
        return None
    value = entry[key]
    if not isinstance(value, str) or not value.strip():
        raise ManifestError(f"{where}.{key} must be a non-empty string")
    return value


def _mcp_str_list(raw: Any, where: str) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(v, str) for v in raw):
        raise ManifestError(f"{where} must be an array of strings")
    return list(raw)


def _mcp_str_map(raw: Any, where: str) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) for k, v in raw.items()
    ):
        raise ManifestError(f"{where} must be an object of string values")
    return dict(raw)


def _parse_settings(raw: Any) -> tuple[SettingDeclaration, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError("settings must be an array")
    out: list[SettingDeclaration] = []
    for index, item in enumerate(raw):
        decl = _parse_setting(item, where=f"settings[{index}]")
        if any(s.key == decl.key for s in out):
            raise ManifestError(f"settings[{index}].key {decl.key!r} is declared twice")
        out.append(decl)
    return tuple(out)


def _parse_setting(item: Any, *, where: str) -> SettingDeclaration:  # noqa: PLR0912 - one check per field
    entry = _object(item, where)
    _known_keys(entry, _SETTING_KEYS, where=where)
    _require(entry, ("key", "type", "label"), where=where)
    key, kind = entry["key"], entry["type"]
    if not isinstance(key, str) or not _SETTING_KEY_RE.match(key):
        raise ManifestError(f"{where}.key {key!r} must match ^[a-z][a-z0-9_]{{0,63}}$")
    if kind not in SETTING_TYPES:
        raise ManifestError(
            f"{where}.type {kind!r} must be one of {', '.join(sorted(SETTING_TYPES))}"
        )
    options: tuple[str, ...] = ()
    if kind == "select":
        raw_opts = entry.get("options")
        if (
            not isinstance(raw_opts, list)
            or not raw_opts
            or not all(isinstance(o, str) and o for o in raw_opts)
            or len(set(raw_opts)) != len(raw_opts)
        ):
            raise ManifestError(f"{where}.options must be a non-empty list of distinct strings")
        options = tuple(raw_opts)
    elif "options" in entry:
        raise ManifestError(f"{where}.options is only valid for type 'select'")
    bounds: dict[str, float] = {}
    for b in ("min", "max"):
        if b in entry:
            if kind != "number":
                raise ManifestError(f"{where}.{b} is only valid for type 'number'")
            v = entry[b]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ManifestError(f"{where}.{b} must be a number")
            bounds[b] = float(v)
    required = entry.get("required", False)
    if not isinstance(required, bool):
        raise ManifestError(f"{where}.required must be true or false")
    decl = SettingDeclaration(
        key=key,
        type=kind,
        label=_text(entry["label"], label=f"{where}.label"),
        description=_text(entry["description"], label=f"{where}.description")
        if "description" in entry
        else "",
        required=required,
        options=options,
        min=bounds.get("min"),
        max=bounds.get("max"),
    )
    if "default" not in entry:
        return decl
    if kind == "secret":
        raise ManifestError(f"{where}.default is not allowed for a secret")
    try:
        default = decl.check(entry["default"])
    except ValueError as exc:
        raise ManifestError(f"{where}.default {exc}") from None
    return dataclasses.replace(decl, default=default)


def _parse_prompts(raw: Any) -> tuple[PromptDeclaration, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError("prompts must be an array")
    out: list[PromptDeclaration] = []
    for index, item in enumerate(raw):
        where = f"prompts[{index}]"
        entry = _object(item, where)
        _known_keys(entry, _PROMPT_KEYS, where=where)
        _require(entry, ("when",), where=where)
        when = entry["when"]
        if when not in PROMPT_WHEN:
            raise ManifestError(f"{where}.when {when!r} must be 'always' or 'active'")
        has_text, has_file = "text" in entry, "file" in entry
        if has_text == has_file:
            raise ManifestError(f"{where} needs exactly one of 'text' or 'file'")
        if has_text:
            text = entry["text"]
            if not isinstance(text, str) or not text.strip():
                raise ManifestError(f"{where}.text must be a non-empty string")
            if len(text) > PROMPT_MAX_CHARS:
                raise ManifestError(f"{where}.text is longer than {PROMPT_MAX_CHARS} characters")
            out.append(PromptDeclaration(when=when, text=text))
        else:
            out.append(
                PromptDeclaration(when=when, file=_rel_path(entry["file"], label=f"{where}.file"))
            )
    return tuple(out)


def _parse_name(name: Any, *, root: Path) -> str:
    if not isinstance(name, str) or len(name) > _NAME_MAX or not _EXT_NAME_RE.match(name):
        raise ManifestError(
            f"name {name!r} must match ^[a-z][a-z0-9]*(-[a-z0-9]+)*$ (max {_NAME_MAX})"
        )
    if name != root.name:
        raise ManifestError(f"name {name!r} must equal its directory name {root.name!r}")
    return name


def _parse_tools(raw: Any) -> tuple[ToolDeclaration, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ManifestError("tools must be an array")
    out: list[ToolDeclaration] = []
    for index, item in enumerate(raw):
        where = f"tools[{index}]"
        entry = _object(item, where)
        _known_keys(entry, _TOOL_KEYS, where=where)
        _require(entry, ("name", "group"), where=where)
        name = entry["name"]
        if (
            not isinstance(name, str)
            or len(name) > _NAME_MAX
            or "__" in name
            or not _TOOL_NAME_RE.match(name)
        ):
            raise ManifestError(
                f"{where}.name {name!r} must be lower-case letters, digits and '_', "
                f"start with a letter, not end with '_', max {_NAME_MAX}, no '__'"
            )
        if any(t.name == name for t in out):
            raise ManifestError(f"{where}.name {name!r} is declared twice")
        group = entry["group"]
        if group not in _GROUPS:
            raise ManifestError(f"{where}.group {group!r} must be one of read, edit, command, null")
        out.append(ToolDeclaration(name=name, group=group))
    return tuple(out)


def _parse_skills(raw: Any) -> SkillsDeclaration:
    if raw is None:
        return SkillsDeclaration()
    obj = _object(raw, "skills")
    _known_keys(obj, _SKILLS_KEYS, where="skills")
    skills_dir = _rel_path(obj.get("dir", DEFAULT_SKILLS_DIR), label="skills.dir")
    modes = _object(obj.get("default_modes", {}), "skills.default_modes")
    for skill_id, mode in modes.items():
        if mode not in _SKILL_MODES:
            raise ManifestError(
                f"skills.default_modes[{skill_id!r}] {mode!r} must be one of "
                "off, cloud, local, both"
            )
    return SkillsDeclaration(dir=skills_dir, default_modes=dict(modes))


def _object(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be a JSON object")
    return value


def _known_keys(obj: dict[str, Any], allowed: frozenset[str], *, where: str) -> None:
    unknown = sorted(set(obj) - allowed)
    if unknown:
        prefix = f"{where}: " if where else ""
        raise ManifestError(
            f"{prefix}unknown field(s) {', '.join(map(repr, unknown))}; "
            f"allowed: {', '.join(sorted(allowed))}"
        )


def _require(obj: dict[str, Any], keys: tuple[str, ...], *, where: str) -> None:
    for key in keys:
        if key not in obj:
            prefix = f"{where} " if where else ""
            raise ManifestError(f"{prefix}missing required field {key!r}")


def _rel_path(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not _REL_PATH_RE.match(value) or ".." in value.split("/"):
        raise ManifestError(
            f"{label} {value!r} must be a relative path like 'skills' or 'src/main.py'"
        )
    return value.rstrip("/")


def _text(value: Any, *, label: str) -> str:
    """A plain string, or the ``en`` entry of a ``{locale: text}`` mapping."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        for key in ("en", "zh-CN", "zh-TW"):
            text = value.get(key)
            if isinstance(text, str):
                return text
        first = next(iter(value.values()))
        if isinstance(first, str):
            return first
    raise ManifestError(f"{label} must be a string or a locale->string object")
