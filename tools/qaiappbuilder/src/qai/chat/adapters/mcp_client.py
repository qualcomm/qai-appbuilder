# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""MCP server registry adapter for the chat bounded context.

Implements :class:`qai.chat.application.ports.McpServerRegistryPort`.  Owns:

* **Config persistence** — the set of :class:`McpServerConfig` is stored as a
  JSON file at ``<data>/config/mcp_servers.json`` (the daemon runtime config
  dir, AGENTS.md §2.4 ``data/config/`` = runtime user config).  This is the
  GLOBAL persistence choice (task spec): MCP servers are a workspace-wide
  capability, not per-conversation.  The choice is documented here so a future
  reader knows where the truth lives.  Credential-bearing header VALUES are
  NOT written to this file — they go to
  :class:`qai.platform.persistence.secrets.SecretStore` (AGENTS.md §3.3) and
  are re-hydrated at connect time.

* **Tool registration** — the KEY architectural win: instead of a bespoke
  advertise/invoke path, a connected server's tools are registered as
  ``handler + schema`` entries on the SHARED
  :class:`qai.chat.adapters.tool_invocation.RegistryBackedToolInvocation`.
  Because ``StreamChatUseCase._collect_tool_schemas`` advertises whatever that
  registry's ``schemas()`` returns and dispatches ``tool_call`` frames through
  its ``invoke()``, MCP tools automatically flow through the same
  advertise-filter → guardrail → truncator pipeline as the built-in tools with
  ZERO streaming-use-case change.  Each MCP tool is registered under its
  collision-safe ``{server}__{tool}`` qualified name (:attr:`McpTool.
  qualified_name`) and the handler routes back to the owning server.

* **Resources / prompts surface (Plan A — expose as tools)** — a connected
  server's *resources* and *prompts* are ALSO surfaced to the LLM as callable
  tools on the SAME shared registry, rather than injected into the streaming
  prompt assembly (Plan B).  Plan A is chosen because it reuses the entire
  tools pipeline already built above (advertise-filter / guardrail / truncator
  / sub-agent inheritance) with ZERO change to ``streaming.py`` — identical to
  the tool-bridge architecture — whereas Plan B would have to modify the
  streaming prompt-assembly main path (higher risk, forbidden file domain).
  For each connected server that advertises them, the registry auto-registers:

  * ``mcp__<server>__list_resources`` (no args) — enumerate the server's
    resources (uri / name / mime);
  * ``mcp__<server>__read_resource`` (``{"uri": str}``) — read one resource;
  * ``mcp__<server>__list_prompts`` (no args) — enumerate the server's prompts;
  * ``mcp__<server>__get_prompt`` (``{"name": str, "arguments"?: object}``) —
    render one prompt.

  These capability tools are registered ONLY for a server that is enabled +
  connected AND actually advertises the corresponding capability (a server with
  no resources gets no resource tools).  They are dropped the moment the server
  disconnects / is removed (same lifecycle as the direct tools), so the model
  can never reach a resource / prompt of an un-enabled or unreachable server.

* **Secure-by-default gate** — when ``enabled`` is ``False`` (the
  ``chat.chat_mcp_enabled`` Settings gate, default off) the registry never
  spawns a subprocess / opens a session; :meth:`list_servers` still returns the
  persisted configs (so the UI can show + edit them) but connections are not
  made and no tools are advertised.  Mirrors the ``SubprocessHookEngine``
  ``enabled=False`` gate.

Cross-context isolation
-----------------------
Imports only ``qai.chat.{domain,application,infrastructure}`` + stdlib +
``qai.platform.persistence.secrets`` (the platform SecretStore port, allowed
from any context).  No imports of other bounded contexts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from qai.chat.application.ports import (
    McpServerRegistryPort,
    McpServerStatus,
    ToolInvocationRequest,
)
from qai.chat.domain.mcp_catalog import (
    EXTERNAL_CURATED_CATALOG,
    CuratedCatalogEntry,
    build_internal_curated_catalog,
)
from qai.chat.domain.mcp_server import (
    McpPrompt,
    McpResource,
    McpServerConfig,
    McpTool,
    McpTransport,
)
from qai.chat.infrastructure.mcp_client import (
    McpConnectionError,
    McpTransportClient,
    call_tool as _mcp_call_tool,
    discover_prompts as _mcp_discover_prompts,
    discover_resources as _mcp_discover_resources,
    get_prompt as _mcp_get_prompt,
    read_resource as _mcp_read_resource,
)
from qai.chat.infrastructure.mcp_registry_source import (
    McpRegistrySourceError,
    fetch_custom_source_page as _fetch_custom_source_page,
    fetch_registry_entries as _fetch_registry_entries,
    fetch_registry_page as _fetch_registry_page,
)

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from qai.chat.adapters.tool_invocation import RegistryBackedToolInvocation
    from qai.platform.persistence.secrets import SecretStore

logger = logging.getLogger("qai.chat.mcp_registry")

__all__ = [
    "McpServerRegistry",
    "McpToolInvocationAdapter",
]

#: SecretStore namespace under which per-server header credentials live.
#: Keyed ``<server_name>.<header_name>``.
_SECRET_SERVICE: str = "chat_mcp"


def _human_error(raw: str) -> str:
    """Convert a raw McpConnectionError message to a user-readable string."""
    if "http_status_401" in raw:
        return "Authentication required (401) — check the Authorization header or re-run OAuth"
    if "http_status_403" in raw:
        return "Access denied (403) — check permissions or credentials"
    if "http_status_404" in raw:
        return "Endpoint not found (404) — check the server URL"
    if "html (not SSE stream)" in raw or "server returned HTML" in raw:
        return "Server returned an HTML page instead of MCP stream — check URL and Authorization"
    return raw


#: Soft upper bound on how many browsed registry entries the in-memory install
#: cache retains. The cache exists only so an install can resolve a browsed
#: entry by id; it must not grow unbounded as the user searches/pages many
#: times. On overflow the OLDEST entries are dropped (most-recently-browsed
#: kept) — see :meth:`McpServerRegistry._merge_registry_cache`.
_MAX_REGISTRY_CACHE: int = 500


class McpToolInvocationAdapter:
    """Bridge one MCP tool into a :class:`ToolInvocationPort`-style handler.

    The registry constructs one adapter per discovered tool and registers its
    :meth:`invoke` as the handler on the shared tool registry (paired with the
    OpenAI function-calling schema built from the tool's JSON-Schema).  When the
    LLM calls the tool, :meth:`invoke` opens a fresh connection to the owning
    server, calls the tool, and returns the textual result — flowing back
    through the same guardrail / truncator pipeline as any built-in tool.

    The call is dispatched through the registry's PERSISTENT session pool
    (``invoker``): the server's subprocess is spawned once and reused across
    many calls (the MCP session model), so there is no per-call ~6-13s cold
    start and stateful servers (memory graph, playwright session) keep their
    state between calls. A dead pooled session is transparently re-spawned by
    the pool. When no pool invoker is wired (e.g. a unit test constructing the
    adapter directly), it falls back to a one-shot connection.
    """

    __slots__ = ("_config_provider", "_tool_name", "_invoker")

    def __init__(
        self,
        *,
        tool_name: str,
        config_provider: "Any",
        invoker: "Any" = None,
    ) -> None:
        # ``config_provider`` is a zero-arg callable returning the CURRENT
        # McpServerConfig for the owning server (so an edited config / removed
        # server is reflected without re-registering the handler). Returns
        # ``None`` when the server was removed.
        # ``invoker`` is an async callable ``(config, tool_name, arguments) ->
        # str`` that routes through the registry's persistent session pool; when
        # ``None`` a one-shot connection is used (test / fallback).
        self._tool_name = tool_name
        self._config_provider = config_provider
        self._invoker = invoker

    async def invoke(self, request: ToolInvocationRequest) -> str:
        config = self._config_provider()
        if config is None:
            raise RuntimeError(
                f"mcp server for tool {self._tool_name!r} is no longer registered"
            )
        if self._invoker is not None:
            return await self._invoker(config, self._tool_name, request.arguments)
        return await _mcp_call_tool(config, self._tool_name, request.arguments)


def _to_openai_schema(tool: McpTool) -> dict[str, Any]:
    """Wrap an :class:`McpTool` into the OpenAI function-calling schema shape.

    The advertised NAME is the collision-safe qualified name so two servers'
    like-named tools never clash.  ``parameters`` is the tool's JSON-Schema
    ``inputSchema`` (an object schema); a missing / non-object schema falls back
    to the permissive empty-object schema so the model can still call it.
    """
    params = tool.schema if isinstance(tool.schema, dict) and tool.schema else {
        "type": "object",
        "properties": {},
    }
    desc = tool.description or f"MCP tool {tool.name} on server {tool.server_name}"
    # Prefix the description so the model knows the tool's provenance.
    desc = f"[MCP:{tool.server_name}] {desc}"
    return {
        "type": "function",
        "function": {
            "name": tool.qualified_name,
            "description": desc,
            "parameters": params,
        },
    }


# ---------------------------------------------------------------------------
# Resources / prompts capability tools (Plan A — expose as callable tools)
# ---------------------------------------------------------------------------
#: Prefix for the synthetic capability-tool names so they are visually grouped
#: and clearly attributable to the MCP resources/prompts surface.
_MCP_TOOL_PREFIX = "mcp__"


def _resource_tool_names(server_name: str) -> tuple[str, str]:
    """Return the (list_resources, read_resource) tool names for a server."""
    return (
        f"{_MCP_TOOL_PREFIX}{server_name}__list_resources",
        f"{_MCP_TOOL_PREFIX}{server_name}__read_resource",
    )


def _prompt_tool_names(server_name: str) -> tuple[str, str]:
    """Return the (list_prompts, get_prompt) tool names for a server."""
    return (
        f"{_MCP_TOOL_PREFIX}{server_name}__list_prompts",
        f"{_MCP_TOOL_PREFIX}{server_name}__get_prompt",
    )


def _list_resources_schema(server_name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _resource_tool_names(server_name)[0],
            "description": (
                f"[MCP:{server_name}] List the resources exposed by the "
                f"'{server_name}' MCP server (returns each resource's uri, "
                "name and mime type). Call this first to discover what you can "
                "read, then use the read_resource tool with a uri."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _read_resource_schema(server_name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _resource_tool_names(server_name)[1],
            "description": (
                f"[MCP:{server_name}] Read one resource from the '{server_name}' "
                "MCP server by its uri (obtain the uri from the list_resources "
                "tool). Returns the resource's text content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "uri": {
                        "type": "string",
                        "description": "The MCP resource uri to read.",
                    }
                },
                "required": ["uri"],
            },
        },
    }


def _list_prompts_schema(server_name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _prompt_tool_names(server_name)[0],
            "description": (
                f"[MCP:{server_name}] List the prompt templates exposed by the "
                f"'{server_name}' MCP server (returns each prompt's name, "
                "description and declared arguments). Call this to discover "
                "prompts, then use get_prompt to render one."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _get_prompt_schema(server_name: str) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": _prompt_tool_names(server_name)[1],
            "description": (
                f"[MCP:{server_name}] Render a named prompt template from the "
                f"'{server_name}' MCP server with the given arguments. Returns "
                "the assembled prompt text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "The prompt name (from list_prompts).",
                    },
                    "arguments": {
                        "type": "object",
                        "description": "Argument name→value map for the prompt.",
                    },
                },
                "required": ["name"],
            },
        },
    }


def _render_resource_list(resources: tuple[McpResource, ...]) -> str:
    """Render a resource list into a compact text block for the model."""
    if not resources:
        return "(no resources)"
    lines = [
        f"- {r.uri}"
        + (f" ({r.name})" if r.name else "")
        + (f" [{r.mime_type}]" if r.mime_type else "")
        for r in resources
    ]
    return "\n".join(lines)


def _render_prompt_list(prompts: tuple[McpPrompt, ...]) -> str:
    """Render a prompt list into a compact text block for the model."""
    if not prompts:
        return "(no prompts)"
    lines: list[str] = []
    for p in prompts:
        args = ", ".join(
            a.name + ("*" if a.required else "") for a in p.arguments
        )
        line = f"- {p.name}"
        if p.description:
            line += f": {p.description}"
        if args:
            line += f"  (args: {args})"
        lines.append(line)
    return "\n".join(lines)


class _McpResourceToolHandler:
    """Handlers for a server's resources capability tools (Plan A).

    ``config_provider`` returns the CURRENT :class:`McpServerConfig` for the
    owning server (or ``None`` if it was removed). Each handler opens a fresh
    connection to the server, so a removed / disconnected server fails just
    that one call rather than corrupting shared state.
    """

    __slots__ = ("_config_provider", "_client_provider")

    def __init__(self, *, config_provider: "Any", client_provider: "Any" = None) -> None:
        # ``client_provider`` is an async callable ``(config) -> live
        # McpTransportClient`` from the registry pool (reuse the persistent
        # session). When ``None``, falls back to a one-shot connection.
        self._config_provider = config_provider
        self._client_provider = client_provider

    async def list_resources(self, request: ToolInvocationRequest) -> str:
        config = self._config_provider()
        if config is None:
            return "[mcp error] server no longer registered"
        if self._client_provider is not None:
            client = await self._client_provider(config)
            resources = await client.list_resources()
        else:
            resources = await _mcp_discover_resources(config)
        return _render_resource_list(resources)

    async def read_resource(self, request: ToolInvocationRequest) -> str:
        config = self._config_provider()
        if config is None:
            return "[mcp error] server no longer registered"
        uri = str((request.arguments or {}).get("uri") or "").strip()
        if not uri:
            return "[mcp error] missing required argument 'uri'"
        if self._client_provider is not None:
            client = await self._client_provider(config)
            return await client.read_resource(uri)
        return await _mcp_read_resource(config, uri)


class _McpPromptToolHandler:
    """Handlers for a server's prompts capability tools (Plan A)."""

    __slots__ = ("_config_provider", "_client_provider")

    def __init__(self, *, config_provider: "Any", client_provider: "Any" = None) -> None:
        self._config_provider = config_provider
        self._client_provider = client_provider

    async def list_prompts(self, request: ToolInvocationRequest) -> str:
        config = self._config_provider()
        if config is None:
            return "[mcp error] server no longer registered"
        if self._client_provider is not None:
            client = await self._client_provider(config)
            prompts = await client.list_prompts()
        else:
            prompts = await _mcp_discover_prompts(config)
        return _render_prompt_list(prompts)

    async def get_prompt(self, request: ToolInvocationRequest) -> str:
        config = self._config_provider()
        if config is None:
            return "[mcp error] server no longer registered"
        args = request.arguments or {}
        name = str(args.get("name") or "").strip()
        if not name:
            return "[mcp error] missing required argument 'name'"
        prompt_args = args.get("arguments")
        if not isinstance(prompt_args, dict):
            prompt_args = {}
        if self._client_provider is not None:
            client = await self._client_provider(config)
            return await client.get_prompt(name, prompt_args)
        return await _mcp_get_prompt(config, name, prompt_args)


async def _install_mcp_package(
    entry: "CuratedCatalogEntry",
    *,
    server_name: str,
    data_dir: "Path",
    error_cls: "type[Exception]",
) -> "CuratedCatalogEntry":
    """Download a .mcp package, extract it, create a venv, install deps.

    Returns a new :class:`CuratedCatalogEntry` whose ``command`` and
    ``args_template`` point at the venv python + extracted server script,
    ready to be passed to :func:`_materialise_stdio_entry`.

    The package directory layout under ``data_dir/mcp_packages/<server_name>/``:
      extract/   — unzipped content
      venv/      — dedicated Python venv

    Re-installation (e.g. user clicks "Reinstall") re-uses the existing venv
    (pip install --upgrade) to avoid re-downloading the interpreter.
    """
    import asyncio as _asyncio
    import shutil as _shutil
    import subprocess as _subprocess
    import sys as _sys
    import zipfile as _zipfile
    from pathlib import Path as _Path

    pkg_url: str = getattr(entry, "package_url", "") or ""
    if not pkg_url:
        return entry

    pkg_dir = _Path(data_dir) / "mcp_packages" / server_name
    extract_dir = pkg_dir / "extract"
    venv_dir = pkg_dir / "venv"
    pkg_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Fetch the .mcp zip ────────────────────────────────────────────────
    zip_path = pkg_dir / "package.zip"
    loop = _asyncio.get_event_loop()

    def _download() -> None:
        if pkg_url.startswith("\\\\") or pkg_url.startswith("//"):
            # UNC / network share — plain file copy
            import shutil as _sh
            _sh.copy2(pkg_url, str(zip_path))
        else:
            import httpx as _httpx
            with _httpx.Client(verify=False, timeout=60.0, follow_redirects=True) as hc:
                with hc.stream("GET", pkg_url) as resp:
                    resp.raise_for_status()
                    with open(zip_path, "wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=65536):
                            fh.write(chunk)

    await loop.run_in_executor(None, _download)

    # ── 2. Extract ───────────────────────────────────────────────────────────
    def _extract() -> None:
        if extract_dir.exists():
            _shutil.rmtree(extract_dir)
        extract_dir.mkdir(parents=True)
        with _zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(extract_dir)

    await loop.run_in_executor(None, _extract)

    # ── 3. Locate the server script ──────────────────────────────────────────
    # The entry's args_template first non-flag token is the script name.
    script_name = "server.py"
    for tok in (entry.args_template or ()):
        if tok and not tok.startswith("-") and tok.endswith(".py"):
            script_name = tok
            break

    # Walk the extract directory to find the script.
    def _find_script() -> "_Path | None":
        for p in sorted(extract_dir.rglob(script_name)):
            return p
        return None

    script_path = await loop.run_in_executor(None, _find_script)
    if script_path is None:
        raise error_cls(
            f"package for {server_name!r} does not contain {script_name!r}"
        )
    script_root = script_path.parent

    # ── 3b. Patch requirements.txt: cap mcp to <2.0.0 ───────────────────────
    # mcp 2.0.0 removed mcp.server.fastmcp; server.py requires fastmcp from
    # mcp 1.x.  Without an upper bound, `pip install --upgrade` (run by
    # install.ps1) silently promotes to 2.0.0 and breaks the server.
    _req_txt_patch = script_root / "requirements.txt"
    if _req_txt_patch.exists():
        try:
            import re as _re
            _req_raw = _req_txt_patch.read_text(encoding="utf-8", errors="replace")
            # Replace bare `mcp>=X` or `mcp==X` that lack an upper bound with
            # a bounded form.  Only touch lines whose specifier list contains no
            # `<` constraint yet (avoid double-patching).
            _req_patched = _re.sub(
                r"^(mcp(?:[^<\n]*))$",
                lambda m: (
                    m.group(1).rstrip() + ",<2.0.0"
                    if "<" not in m.group(1)
                    else m.group(1)
                ),
                _req_raw,
                flags=_re.MULTILINE,
            )
            if _req_patched != _req_raw:
                _req_txt_patch.write_text(_req_patched, encoding="utf-8")
                logger.info(
                    "chat.mcp.req_patched server=%s: capped mcp to <2.0.0",
                    server_name,
                )
        except Exception as _exc_patch:
            logger.warning(
                "chat.mcp.req_patch_failed server=%s: %s", server_name, _exc_patch
            )

    # ── 4. Create / reuse venv ───────────────────────────────────────────────
    venv_python = venv_dir / "Scripts" / "python.exe"

    def _ensure_venv() -> None:
        # If an old venv exists without system-site-packages, tear it down and
        # recreate — otherwise pip will try to compile native wheels from source.
        pyvenv_cfg = venv_dir / "pyvenv.cfg"
        if venv_python.exists() and pyvenv_cfg.exists():
            cfg_text = pyvenv_cfg.read_text(encoding="utf-8", errors="replace").lower()
            if "include-system-site-packages = false" in cfg_text:
                _shutil.rmtree(venv_dir)
        if not venv_python.exists():
            # --system-site-packages lets the MCP venv inherit already-compiled
            # native packages from the QAI host venv (cryptography, etc.) so
            # pip never needs to build them from source (no Rust/OpenSSL needed).
            _subprocess.check_call(
                [_sys.executable, "-m", "venv",
                 "--system-site-packages", str(venv_dir)],
                timeout=120,
            )

    await loop.run_in_executor(None, _ensure_venv)

    # ── 5. Install dependencies ──────────────────────────────────────────────
    def _pip_install() -> None:
        req_txt = script_root / "requirements.txt"
        # --prefer-binary: use wheels over source builds whenever available;
        # avoids Rust/C compilation on platforms with no pre-built wheel.
        #
        # constraints file: freeze the versions of packages already in the host
        # venv (especially `mcp`) so pip never upgrades them and breaks the SDK.
        # We generate it on-the-fly from the host venv's installed packages.
        constraints_path = pkg_dir / "_host_constraints.txt"
        try:
            import subprocess as _sp2
            freeze_out = _sp2.check_output(
                [_sys.executable, "-m", "pip", "freeze", "--all"],
                timeout=30,
            ).decode("utf-8", errors="replace")
            # Editable installs (-e ...) are not valid as constraints; skip them.
            filtered = "\n".join(
                line for line in freeze_out.splitlines()
                if line and not line.startswith("-e ")
            )
            constraints_path.write_text(filtered, encoding="utf-8")
        except Exception:
            constraints_path = None  # type: ignore[assignment]

        base_pip_cmd = [str(venv_python), "-m", "pip", "install", "--quiet",
                        "--prefer-binary",
                        "--trusted-host", "pypi.org",
                        "--trusted-host", "files.pythonhosted.org"]
        if constraints_path is not None:
            base_pip_cmd += ["-c", str(constraints_path)]

        if not req_txt.exists():
            _subprocess.check_call(base_pip_cmd + ["mcp>=1.0.0,<2.0.0"], timeout=300)
        else:
            _subprocess.check_call(
                base_pip_cmd + ["-r", str(req_txt)],
                cwd=str(script_root),
                timeout=300,
            )

        # Verify that mcp.server.fastmcp is importable; if mcp>=2.0.0 was
        # already installed in the venv (e.g. by a previous install.ps1 run),
        # force-downgrade to the last compatible release.
        import subprocess as _sp3
        check = _sp3.run(
            [str(venv_python), "-c",
             "from mcp.server.fastmcp import FastMCP"],
            capture_output=True,
        )
        if check.returncode != 0:
            logger.warning(
                "chat.mcp.fastmcp_missing server=%s — forcing mcp<2.0.0",
                server_name,
            )
            _subprocess.check_call(
                base_pip_cmd + ["--upgrade", "mcp>=1.0.0,<2.0.0"],
                timeout=300,
            )

    await loop.run_in_executor(None, _pip_install)

    # ── 5b. Run post-install script if present (e.g. native messaging host) ──
    # Prefer install.ps1 (supports -VenvDir to reuse our venv); fall back to
    # install.bat only when no .ps1 is present.
    _install_ps1 = script_root / "install.ps1"
    _install_bat = script_root / "install.bat"

    if _install_ps1.exists():
        # Pass -VenvDir pointing to the venv we already created so that
        # install.ps1 reuses it instead of creating a second one under
        # %LOCALAPPDATA%\Qualcomm\Easywork\mcp-venv.
        _extra_ps1_args: "list[str]" = []
        try:
            if "[string]$VenvDir" in _install_ps1.read_text(
                encoding="utf-8", errors="replace"
            ):
                _extra_ps1_args = ["-VenvDir", str(venv_dir.resolve())]
        except Exception:
            pass
        _post_cmd: "list[str]" = [
            "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
            "-File", str(_install_ps1),
            *_extra_ps1_args,
        ]
    elif _install_bat.exists():
        # .bat must be run via cmd, not powershell -File
        _post_cmd = ["cmd", "/c", str(_install_bat)]
    else:
        _post_cmd = []

    if _post_cmd:
        _captured_post_cmd = _post_cmd  # captured for the closure below

        def _run_post_install() -> None:
            try:
                out = _subprocess.check_output(
                    _captured_post_cmd,
                    stderr=_subprocess.STDOUT,
                    timeout=300,
                ).decode("utf-8", errors="replace")
                logger.info(
                    "chat.mcp.post_install_ok server=%s\n%s",
                    server_name, out,
                )
            except _subprocess.CalledProcessError as _exc_pi:
                out = (_exc_pi.output or b"").decode("utf-8", errors="replace")
                logger.warning(
                    "chat.mcp.post_install_failed server=%s returncode=%s\n%s",
                    server_name, _exc_pi.returncode, out,
                )
            except Exception as _exc_pi:
                logger.warning(
                    "chat.mcp.post_install_error server=%s: %s",
                    server_name, _exc_pi,
                )

        await loop.run_in_executor(None, _run_post_install)

    # ── 6. Return patched entry using venv python + script path ─────────────
    from dataclasses import replace as _replace
    patched = _replace(
        entry,
        command=str(venv_python),
        args_template=(str(script_path),),
        package_url="",  # consumed; clear so _materialise_entry treats as plain stdio
    )
    logger.info(
        "chat.mcp.package_installed server=%s venv=%s script=%s",
        server_name, venv_python, script_path,
    )
    return patched


def _materialise_stdio_entry(
    entry: CuratedCatalogEntry,
    *,
    server_name: str,
    arg_values: dict[str, str],
    env_values: dict[str, str],
    error_cls: type[Exception],
) -> McpServerConfig:
    """Build a stdio config: substitute args + collect declared env vars."""
    for placeholder in entry.requires_args:
        if not str(arg_values.get(placeholder, "")).strip():
            raise error_cls(
                f"catalog entry {entry.id!r} requires argument {placeholder!r}"
            )
    substituted: list[str] = []
    for token in entry.args_template:
        if token in arg_values and str(arg_values[token]).strip():
            substituted.append(str(arg_values[token]).strip())
        elif token.startswith("<") and token.endswith(">"):
            continue
        else:
            substituted.append(token)
    env: dict[str, str] = {}
    for var in getattr(entry, "env_schema", ()):
        val = str(env_values.get(var, "")).strip()
        if val:
            env[var] = val
    for var in getattr(entry, "env_required", ()):
        if not str(env_values.get(var, "")).strip():
            raise error_cls(f"catalog entry {entry.id!r} requires env var {var!r}")
    # Which of the collected env keys are secrets (API keys / tokens): the
    # entry's declared secret_fields intersected with its env schema. These are
    # externalised to the SecretStore on persist (not written plain-text to the
    # on-disk config) — mirroring how remote header secrets are handled.
    env_schema = set(getattr(entry, "env_schema", ()))
    secret_env_keys = tuple(
        f for f in getattr(entry, "secret_fields", ()) if f in env_schema and f in env
    )
    return McpServerConfig(
        name=server_name,
        transport=McpTransport.STDIO,
        command=entry.command,
        args=tuple(substituted),
        env=env,
        # 90s (was 30s) to accommodate first-time cold starts of uvx/npx
        # (measured cold start ~13s); stdio servers are re-spawned on every
        # discover/call, so multiple startups stack up — avoid handshake timeout.
        timeout_s=90.0,
        enabled=True,
        secret_env_keys=secret_env_keys,
    )


def _materialise_remote_entry(
    entry: CuratedCatalogEntry,
    *,
    server_name: str,
    transport: str,
    header_values: dict[str, str],
    error_cls: type[Exception],
) -> McpServerConfig:
    """Build an sse/http config: use url + collect declared headers."""
    if not entry.url:
        raise error_cls(
            f"catalog entry {entry.id!r} has no url for remote transport"
        )
    headers: dict[str, str] = {}
    for hname in getattr(entry, "headers_schema", ()):
        val = str(header_values.get(hname, "")).strip()
        if val:
            headers[hname] = val
    for hname in getattr(entry, "headers_required", ()):
        if not str(header_values.get(hname, "")).strip():
            raise error_cls(f"catalog entry {entry.id!r} requires header {hname!r}")
    # Merge any extra headers passed by the caller (e.g. OAuth Bearer tokens
    # obtained outside the schema, such as Authorization from the frontend
    # /oauth/start flow). Only add if not already set by the schema loop above.
    for hname, hval in header_values.items():
        if hname not in headers and str(hval).strip():
            headers[hname] = str(hval).strip()
    return McpServerConfig(
        name=server_name,
        transport=McpTransport(transport),
        url=entry.url,
        headers=headers,
        # 120s for remote HTTP/SSE entries — semantic search tools (e.g. search_content
        # on QGenie MCPHub) can take 60-90s on cold start; 30s was too short.
        timeout_s=120.0,
        enabled=True,
    )


def _materialise_entry(
    entry: CuratedCatalogEntry,
    *,
    name: str | None,
    arg_values: dict[str, str],
    env_values: dict[str, str],
    header_values: dict[str, str],
    error_cls: type[Exception],
) -> McpServerConfig:
    """Build an :class:`McpServerConfig` from a catalog entry + user inputs.

    Dispatches on the entry ``transport``:

    * **stdio** (curated + registry packages) — substitute ``<PLACEHOLDER>`` args
      and collect declared env vars (``env_required`` enforced);
    * **sse / http** (registry remotes) — use ``url`` + collect declared headers
      (``headers_required`` enforced); ``add_server`` externalises each header
      VALUE to the SecretStore (``__secret__`` sentinel on disk).

    Raises ``error_cls`` (``McpCatalogInstallError``) on a missing required
    placeholder / env / header.
    """
    server_name = name or entry.id
    transport = str(getattr(entry, "transport", "stdio") or "stdio")
    if transport == "stdio":
        return _materialise_stdio_entry(
            entry,
            server_name=server_name,
            arg_values=arg_values,
            env_values=env_values,
            error_cls=error_cls,
        )
    return _materialise_remote_entry(
        entry,
        server_name=server_name,
        transport=transport,
        header_values=header_values,
        error_cls=error_cls,
    )


def resolve_curated_catalog(edition: str) -> tuple[CuratedCatalogEntry, ...]:
    """Return the curated catalog for *edition*, endpoints resolved.

    The internal Qualcomm MCP entries are composed HERE rather than being
    module-level constants in the domain, because their endpoint URLs are
    internal-only pure-data config (``PROJECT-RULES.md §3.8.1(2)``): they live
    in ``internal_config.toml [mcp_catalog]``, inside the
    ``qai.platform.edition`` package that ``manifest.toml [exclude]`` strips
    from external artifacts. ``qai/chat/domain/mcp_catalog.py`` itself SHIPS to
    both editions, so hard-coding intranet hostnames there would put them in
    the open-source drop verbatim.

    This is the same seam and the same ``ImportError`` fallback that
    ``browse_registry`` already uses for the registry-source preset URLs. On
    external the package is absent → no endpoints → no internal entries, which
    is exactly the intended external catalog (defence layers 1+2).
    """
    if edition != "internal":
        return EXTERNAL_CURATED_CATALOG
    try:
        from qai.platform.edition.loader import get_mcp_catalog_endpoints
    except ImportError:
        # Internal edition on a tree without the (excluded) edition package —
        # degrade to the external set rather than failing catalog listing.
        return EXTERNAL_CURATED_CATALOG
    endpoints = get_mcp_catalog_endpoints()
    internal = build_internal_curated_catalog(
        cebot_url=endpoints.get("cebot_url", ""),
        cebot_homepage=endpoints.get("cebot_homepage", ""),
        qgenie_mcphub_base=endpoints.get("qgenie_mcphub_base", ""),
    )
    return internal + EXTERNAL_CURATED_CATALOG


class McpServerRegistry(McpServerRegistryPort):
    """Concrete :class:`McpServerRegistryPort` backed by a JSON config file.

    See the module docstring for the persistence + tool-registration + gate
    design.  All public methods are ``async`` and safe under concurrent access
    (guarded by an ``asyncio.Lock``).
    """

    def __init__(
        self,
        *,
        tools: "RegistryBackedToolInvocation",
        config_path: Path,
        enabled: bool = False,
        secret_store: "SecretStore | None" = None,
        registry_source_base_url: str | None = None,
        registry_source_ttl_s: float = 900.0,
        ssl_verify: bool = True,
        ssl_verify_provider: "Callable[[], bool] | None" = None,
        edition: str = "internal",
    ) -> None:
        self._tools = tools
        self._config_path = config_path
        self._enabled = enabled
        self._secret_store = secret_store
        # Unified outbound-TLS switch (top-level ``Settings.ssl_verify``,
        # edition-derived default) threaded into every spawned / opened
        # ``McpTransportClient`` so MCP servers follow the same TLS-verification
        # policy as every other outbound client.
        self._ssl_verify = ssl_verify
        # Live Settings.ssl_verify provider forwarded into every spawned /
        # opened ``McpTransportClient`` so NEW MCP remote connections read the
        # global SSL toggle at connect time (already-open pooled clients keep
        # their old verify until reconnect — acceptable). Frozen bool fallback.
        self._ssl_verify_provider = ssl_verify_provider
        # Edition-derived catalog: internal builds show the internal entries
        # (endpoints resolved from the edition config) + the external ones;
        # external/packaged builds show EXTERNAL only. Resolved once at
        # construction — the edition and its config file are both fixed for the
        # process lifetime, so re-reading per ``list_catalog`` would be pure I/O.
        self._edition = edition
        self._base_catalog = resolve_curated_catalog(edition)
        # ── Phase-2 dynamic official-registry source (marketplace) ──
        # USER-DRIVEN, no backend gate: browsing the catalog NEVER auto-fetches
        # the third-party registry. ``list_catalog`` returns only the curated
        # source + whatever registry entries are already cached; the network
        # fetch happens ONLY on ``refresh_catalog`` — i.e. when the user
        # explicitly picks the "registry" source and clicks "load / refresh" in
        # the panel. So the user's click IS the consent to reach out; there is
        # no hidden operator flag they cannot control. Offline-safe: a failed
        # refresh degrades to curated + any prior cache (never raises).
        self._registry_source_base_url = registry_source_base_url
        self._registry_source_ttl_s = max(0.0, registry_source_ttl_s)
        # In-memory cache of the last successful dynamic-source fetch (+ the
        # monotonic timestamp it was fetched at) so re-opening the panel within
        # the TTL does not re-hit the network. ``None`` = never fetched.
        self._registry_cache: tuple[CuratedCatalogEntry, ...] | None = None
        self._registry_cache_at: float = 0.0
        # Last dynamic-source degradation reason (empty when the last fetch — or
        # the cache — is healthy), surfaced to the UI as a soft banner.
        self._registry_source_error: str = ""
        self._lock = asyncio.Lock()
        # name -> config
        self._servers: dict[str, McpServerConfig] = {}
        # name -> last live status fields
        self._connected: dict[str, bool] = {}
        self._errors: dict[str, str] = {}
        self._tool_names: dict[str, tuple[str, ...]] = {}
        # name -> discovered resource/prompt counts (for status projection);
        # only populated for a currently-connected server.
        self._resource_count: dict[str, int] = {}
        self._prompt_count: dict[str, int] = {}
        # qualified tool names we registered on the shared port, per server, so
        # remove/replace can drop exactly those.
        self._registered_qtools: dict[str, list[str]] = {}
        self._loaded = False
        # ── Persistent MCP session pool (name -> live McpTransportClient) ──
        # The MCP standard's session model: spawn/open a server's transport ONCE
        # and reuse it for many tools/call + *_list round-trips, instead of the
        # old "spawn per operation" (which paid a ~6-13s npx cold start on EVERY
        # call and lost stateful servers' state — memory graph / playwright
        # session — between calls). A pooled client is health-probed
        # (``is_alive`` — a real returncode/closed check, State-Truth-First) and
        # transparently re-spawned if the child died. Closed on
        # remove / disable / disconnect / shutdown.
        self._pool: dict[str, McpTransportClient] = {}
        # Guards pool mutation (acquire/close). Distinct from ``self._lock``
        # (which guards config/state) to avoid a slow spawn holding the config
        # lock and blocking read-only API like ``list_servers``.
        self._pool_lock = asyncio.Lock()
        # User-added custom registry sources: list of dicts with keys
        # {id, name, url, token, cookie}. Loaded from / persisted to
        # ``custom_registry_sources`` in mcp_servers.json.
        self._custom_sources: list[dict[str, str]] = []

    # ---- config file I/O -------------------------------------------------

    def _load_from_disk(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if not self._config_path.is_file():
                return
            raw = json.loads(self._config_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001 — config must never break startup
            logger.warning("chat.mcp.config_load_failed path=%s", self._config_path)
            return
        entries = (raw or {}).get("servers") or []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                cfg = self._config_from_dict(entry)
            except (ValueError, TypeError):
                continue
            self._servers[cfg.name] = cfg
        # Global master switch: the persisted file is the single truth source
        # once written. A file that carries ``global_enabled`` OVERRIDES the
        # constructor's default (the Settings seed); an older file WITHOUT the
        # key leaves ``self._enabled`` at the constructor default (backwards
        # compatible — an existing config keeps whatever the seed said).
        if isinstance(raw, dict) and "global_enabled" in raw:
            self._enabled = bool(raw.get("global_enabled"))
        # Load user-added custom registry sources (backwards-compat: older files
        # without this key leave _custom_sources as the empty-list default).
        custom = (raw or {}).get("custom_registry_sources")
        if isinstance(custom, list):
            for src in custom:
                if isinstance(src, dict) and isinstance(src.get("id"), str):
                    self._custom_sources.append({
                        "id": str(src.get("id", "")),
                        "name": str(src.get("name", "")),
                        "url": str(src.get("url", "")),
                        "token": str(src.get("token", "")),
                        "cookie": str(src.get("cookie", "")),
                    })

    def _persist_to_disk(self) -> None:
        try:
            self._config_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "global_enabled": self._enabled,
                "servers": [self._config_to_dict(c) for c in self._servers.values()],
                "custom_registry_sources": self._custom_sources,
            }
            # UTF-8, no BOM, LF (AGENTS.md §3.10).
            self._config_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
                newline="\n",
            )
        except Exception:  # noqa: BLE001 — persistence best-effort; never crash
            logger.warning("chat.mcp.config_persist_failed path=%s", self._config_path)

    def _config_to_dict(self, cfg: McpServerConfig) -> dict[str, Any]:
        """Serialise a config, stashing credential header + env values in SecretStore."""
        headers = self._externalise_headers(cfg)
        env = self._externalise_env(cfg)
        return {
            "name": cfg.name,
            "transport": cfg.transport.value,
            "command": cfg.command,
            "args": list(cfg.args),
            "env": env,
            "secret_env_keys": list(cfg.secret_env_keys),
            "cwd": cfg.cwd,
            "url": cfg.url,
            "headers": headers,
            "timeout_s": cfg.timeout_s,
            "enabled": cfg.enabled,
        }

    def _externalise_env(self, cfg: McpServerConfig) -> dict[str, str]:
        """Move each SECRET env VALUE into SecretStore; keep a sentinel on disk.

        Only env keys listed in ``cfg.secret_env_keys`` (API keys / tokens) are
        externalised — a non-secret env value (e.g. a plain endpoint URL) is
        persisted as-is. Mirrors :meth:`_externalise_headers` for remote headers
        (AGENTS.md §3.3: no plain-text credential on disk). When no SecretStore
        is wired, a secret value is DROPPED (never written plain-text) — the
        operator must re-enter it.
        """
        secret_keys = set(cfg.secret_env_keys)
        out: dict[str, str] = {}
        for key, val in cfg.env.items():
            if key not in secret_keys:
                out[key] = val  # plain, non-secret env — persist as-is
                continue
            if self._secret_store is not None:
                try:
                    self._secret_store.set(
                        _SECRET_SERVICE, f"{cfg.name}.env.{key}", val
                    )
                    out[key] = "__secret__"
                    continue
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "chat.mcp.secret_env_set_failed server=%s", cfg.name
                    )
            # No secret store / failure: do NOT persist the plain value.
            out[key] = "__secret__"
        return out

    def _externalise_headers(self, cfg: McpServerConfig) -> dict[str, str]:
        """Move each header VALUE into SecretStore; store a placeholder on disk.

        Returns a dict mapping header name → the sentinel ``"__secret__"`` so
        the on-disk config never carries a plain-text credential (AGENTS.md
        §3.3).  When no SecretStore is wired, the header values are dropped
        entirely (never written plain-text) — the operator must re-enter them.
        """
        out: dict[str, str] = {}
        for hname, hval in cfg.headers.items():
            if self._secret_store is not None:
                try:
                    self._secret_store.set(
                        _SECRET_SERVICE, f"{cfg.name}.{hname}", hval
                    )
                    out[hname] = "__secret__"
                    continue
                except Exception as _sec_exc:  # noqa: BLE001
                    logger.warning("chat.mcp.secret_set_failed server=%s: %s", cfg.name, _sec_exc)
            # No secret store / failure: do NOT persist the plain value.
            out[hname] = "__secret__"
        return out

    def _config_from_dict(self, entry: dict[str, Any]) -> McpServerConfig:
        """Build a config from a persisted dict, re-hydrating header secrets."""
        name = str(entry.get("name") or "")
        transport = McpTransport(str(entry.get("transport") or "stdio"))
        headers_raw = entry.get("headers") or {}
        headers: dict[str, str] = {}
        if isinstance(headers_raw, dict):
            for hname, hval in headers_raw.items():
                if hval == "__secret__" and self._secret_store is not None:
                    try:
                        headers[hname] = self._secret_store.get(
                            _SECRET_SERVICE, f"{name}.{hname}"
                        )
                        continue
                    except Exception:  # noqa: BLE001 — missing secret
                        # Keep sentinel so the connect path sees the header IS
                        # configured (prevents a false OAuth probe) and reports
                        # a clear "secret missing" error instead.
                        logger.warning(
                            "chat.mcp.secret_missing server=%s header=%s "
                            "(re-install the server to re-enter credentials)",
                            name, hname,
                        )
                        headers[hname] = "__secret__"
                        continue
                if isinstance(hval, str) and hval != "__secret__":
                    headers[hname] = hval
        args_raw = entry.get("args") or []
        env_raw = entry.get("env") or {}
        secret_env_raw = entry.get("secret_env_keys") or []
        secret_env_keys = (
            tuple(str(k) for k in secret_env_raw)
            if isinstance(secret_env_raw, list)
            else ()
        )
        # Re-hydrate env: a ``__secret__`` sentinel for a declared secret env key
        # is fetched back from the SecretStore; plain values pass through.
        env: dict[str, str] = {}
        if isinstance(env_raw, dict):
            for k, v in env_raw.items():
                ks, vs = str(k), str(v)
                if vs == "__secret__" and self._secret_store is not None:
                    try:
                        env[ks] = self._secret_store.get(
                            _SECRET_SERVICE, f"{name}.env.{ks}"
                        )
                        continue
                    except Exception:  # noqa: BLE001 — missing secret → skip env
                        continue
                if vs != "__secret__":
                    env[ks] = vs
        return McpServerConfig(
            name=name,
            transport=transport,
            command=entry.get("command"),
            args=tuple(str(a) for a in args_raw) if isinstance(args_raw, list) else (),
            env=env,
            cwd=entry.get("cwd"),
            url=entry.get("url"),
            headers=headers,
            timeout_s=float(entry.get("timeout_s") or 30.0),
            secret_env_keys=secret_env_keys,
            # Legacy configs (pre-marketplace) lack the key → default True
            # (backwards compatible: an existing server stays enabled).
            enabled=bool(entry.get("enabled", True)),
        )

    # ---- tool registration ----------------------------------------------

    def _register_tools(self, cfg: McpServerConfig, tools: tuple[McpTool, ...]) -> None:
        self._unregister_tools(cfg.name)
        qnames: list[str] = []
        for tool in tools:
            adapter = McpToolInvocationAdapter(
                tool_name=tool.name,
                config_provider=lambda n=cfg.name: self._servers.get(n),
                invoker=self._pool_call_tool,
            )
            self._tools.register(
                tool.qualified_name,
                adapter.invoke,
                schema=_to_openai_schema(tool),
            )
            qnames.append(tool.qualified_name)
        self._registered_qtools[cfg.name] = qnames

    def _register_capability_tools(
        self,
        cfg: McpServerConfig,
        *,
        has_resources: bool,
        has_prompts: bool,
    ) -> None:
        """Register the resources/prompts capability tools (Plan A).

        Only registers a capability's tools when the CONNECTED server actually
        advertises that capability (``has_resources`` / ``has_prompts``), so a
        server with no prompts never adds prompt tools. Appends to the server's
        existing registered-tool list so :meth:`_unregister_tools` drops them
        together with the direct tools.
        """
        qnames = self._registered_qtools.setdefault(cfg.name, [])
        provider = lambda n=cfg.name: self._servers.get(n)  # noqa: E731
        if has_resources:
            res_handler = _McpResourceToolHandler(
                config_provider=provider, client_provider=self._pool_client
            )
            list_name, read_name = _resource_tool_names(cfg.name)
            self._tools.register(
                list_name,
                res_handler.list_resources,
                schema=_list_resources_schema(cfg.name),
            )
            self._tools.register(
                read_name,
                res_handler.read_resource,
                schema=_read_resource_schema(cfg.name),
            )
            qnames.extend([list_name, read_name])
        if has_prompts:
            prompt_handler = _McpPromptToolHandler(
                config_provider=provider, client_provider=self._pool_client
            )
            list_name, get_name = _prompt_tool_names(cfg.name)
            self._tools.register(
                list_name,
                prompt_handler.list_prompts,
                schema=_list_prompts_schema(cfg.name),
            )
            self._tools.register(
                get_name,
                prompt_handler.get_prompt,
                schema=_get_prompt_schema(cfg.name),
            )
            qnames.extend([list_name, get_name])

    def _unregister_tools(self, name: str) -> None:
        for qname in self._registered_qtools.pop(name, []):
            self._tools.unregister(qname)

    # ---- connect / discover ---------------------------------------------

    async def _acquire_client(
        self, cfg: McpServerConfig, *, silent_oauth: bool = False
    ) -> McpTransportClient:
        """Return a LIVE persistent transport client for ``cfg`` (pool reuse).

        The MCP session model: reuse one spawned subprocess / HTTP client across
        many round-trips instead of re-spawning per operation. Returns the
        pooled client if it is still alive (``is_alive`` — a real
        returncode/closed probe); otherwise (never connected, or the child
        died) closes any stale client, spawns a fresh persistent one via
        ``connect()``, pools it, and returns it. Raises ``McpConnectionError``
        on connect failure (caller records the per-server error). Guarded by
        ``_pool_lock`` so concurrent invokes for the same server do not spawn
        duplicates.
        """
        async with self._pool_lock:
            client = self._pool.get(cfg.name)
            if client is not None and client.is_alive():
                return client
            # Stale / dead — drop it (best-effort close) and re-spawn.
            if client is not None:
                self._pool.pop(cfg.name, None)
                with contextlib.suppress(Exception):
                    await client.aclose()
            fresh = McpTransportClient(
                cfg,
                ssl_verify=self._ssl_verify,
                ssl_verify_provider=self._ssl_verify_provider,
                secret_store=self._secret_store,
                silent_oauth=silent_oauth,
            )
            await fresh.connect()  # spawn + initialize, KEPT open
            self._pool[cfg.name] = fresh
            return fresh

    async def _close_pooled(self, name: str) -> None:
        """Close + drop the pooled client for ``name`` (idempotent, best-effort)."""
        async with self._pool_lock:
            client = self._pool.pop(name, None)
        if client is not None:
            with contextlib.suppress(Exception):
                await client.aclose()

    async def _pool_client(self, config: McpServerConfig) -> McpTransportClient:
        """Client-provider for capability handlers: a live pooled session."""
        return await self._acquire_client(config)

    async def _pool_call_tool(
        self, config: McpServerConfig, tool_name: str, arguments: dict[str, Any]
    ) -> str:
        """Invoke ``tool_name`` on the server's PERSISTENT pooled session.

        Reuses the spawned subprocess (no per-call cold start). If the pooled
        session's transport died between calls (e.g. the server crashed), the
        first attempt raises, we drop the dead client and retry ONCE with a
        freshly-spawned session (State-Truth-First: probe + reconnect rather
        than assume the cached handle is good). A second failure propagates as a
        normal tool error (handled upstream → ``ok=False`` to the model).
        """
        client = await self._acquire_client(config)
        try:
            return await client.call_tool(tool_name, arguments)
        except (McpConnectionError, OSError, BrokenPipeError):
            # Dead session — drop it and retry once with a fresh spawn.
            await self._close_pooled(config.name)
            client = await self._acquire_client(config)
            return await client.call_tool(tool_name, arguments)

    async def _connect_and_register(
        self, cfg: McpServerConfig, *, silent_oauth: bool = False
    ) -> McpServerStatus:
        # ── Three-way gate (layer 1 + 2): GLOBAL master switch AND the
        # per-server ``enabled`` switch. Either off → keep the config but
        # DISCONNECT + unregister every tool/resource/prompt so the model can
        # reach NOTHING of this server (identical thoroughness to the global
        # gate). Layer 3 (connected) is the actual connect attempt below.
        if not self._enabled:
            self._connected[cfg.name] = False
            self._errors[cfg.name] = "mcp disabled (chat_mcp_enabled=false)"
            self._tool_names[cfg.name] = ()
            self._resource_count[cfg.name] = 0
            self._prompt_count[cfg.name] = 0
            self._unregister_tools(cfg.name)
            await self._close_pooled(cfg.name)
            return self._status_for(cfg)
        if not cfg.enabled:
            self._connected[cfg.name] = False
            self._errors[cfg.name] = "server disabled (enabled=false)"
            self._tool_names[cfg.name] = ()
            self._resource_count[cfg.name] = 0
            self._prompt_count[cfg.name] = 0
            self._unregister_tools(cfg.name)
            await self._close_pooled(cfg.name)
            return self._status_for(cfg)
        try:
            # (Re)connect with the CURRENT config. Close any existing pooled
            # session first so an edited config (changed command/args/env/
            # headers via add_server/update) always spawns a FRESH process —
            # otherwise _acquire_client would reuse the stale old-config client.
            # The fresh session is then KEPT OPEN in the pool for subsequent
            # tools/call (no re-spawn per call; stateful servers keep state).
            await self._close_pooled(cfg.name)
            client = await self._acquire_client(cfg, silent_oauth=silent_oauth)
            tools = await client.list_tools()
            try:
                resources = await client.list_resources()
            except Exception:  # noqa: BLE001 — capability optional
                resources = ()
            try:
                prompts = await client.list_prompts()
            except Exception:  # noqa: BLE001 — capability optional
                prompts = ()
        except McpConnectionError as exc:
            self._connected[cfg.name] = False
            self._errors[cfg.name] = _human_error(str(exc))
            self._tool_names[cfg.name] = ()
            self._resource_count[cfg.name] = 0
            self._prompt_count[cfg.name] = 0
            self._unregister_tools(cfg.name)
            await self._close_pooled(cfg.name)
            return self._status_for(cfg)
        except Exception as exc:  # noqa: BLE001 — never propagate out of the port
            self._connected[cfg.name] = False
            self._errors[cfg.name] = f"unexpected: {exc}"
            self._tool_names[cfg.name] = ()
            self._resource_count[cfg.name] = 0
            self._prompt_count[cfg.name] = 0
            self._unregister_tools(cfg.name)
            await self._close_pooled(cfg.name)
            return self._status_for(cfg)
        # Register the direct tools first (this resets the server's qtool list).
        self._register_tools(cfg, tools)
        # resources / prompts already discovered above (best-effort within
        # discover_all — a capability the server does not implement is an empty
        # tuple, not a failure).
        self._register_capability_tools(
            cfg,
            has_resources=bool(resources),
            has_prompts=bool(prompts),
        )
        self._resource_count[cfg.name] = len(resources)
        self._prompt_count[cfg.name] = len(prompts)
        self._connected[cfg.name] = True
        self._errors[cfg.name] = ""
        self._tool_names[cfg.name] = tuple(t.name for t in tools)
        return self._status_for(cfg)

    async def _safe_discover_resources(
        self, cfg: McpServerConfig
    ) -> tuple[McpResource, ...]:
        """Discover a connected server's resources; ``()`` if unsupported/failed.

        Reuses the server's PERSISTENT pooled session (no per-call spawn) — this
        is called for already-connected servers, so ``_acquire_client`` returns
        the live client. Never raises: a server that does not implement
        ``resources/list`` (rpc_error method-not-found) simply contributes no
        resources, so its resource capability tools are not registered.
        """
        try:
            client = await self._acquire_client(cfg)
            return await client.list_resources()
        except Exception:  # noqa: BLE001 — capability optional; best-effort
            return ()

    async def _safe_discover_prompts(
        self, cfg: McpServerConfig
    ) -> tuple[McpPrompt, ...]:
        """Discover a connected server's prompts; ``()`` if unsupported/failed.

        Reuses the persistent pooled session (no per-call spawn).
        """
        try:
            client = await self._acquire_client(cfg)
            return await client.list_prompts()
        except Exception:  # noqa: BLE001 — capability optional; best-effort
            return ()

    def _status_for(self, cfg: McpServerConfig) -> McpServerStatus:
        names = self._tool_names.get(cfg.name, ())
        return McpServerStatus(
            config=cfg,
            connected=self._connected.get(cfg.name, False),
            tool_count=len(names),
            tool_names=names,
            error=self._errors.get(cfg.name, ""),
        )

    # ---- port surface ----------------------------------------------------

    async def list_servers(self) -> tuple[McpServerStatus, ...]:
        async with self._lock:
            self._load_from_disk()
            return tuple(self._status_for(c) for c in self._servers.values())

    async def add_server(self, config: McpServerConfig) -> McpServerStatus:
        async with self._lock:
            self._load_from_disk()
            self._servers[config.name] = config
            self._persist_to_disk()
            if config.transport is McpTransport.STDIO:
                return await self._connect_and_register(config)
            # Remote (SSE/HTTP): persist config and return immediately so the
            # install dialog closes in < 1 s.  Connection + tool discovery run
            # in a background Task; the MCP panel refreshes its tool list once
            # the task finishes (or on the user's next status poll).
            self._connected[config.name] = False
            self._errors[config.name] = "connecting…"
            asyncio.get_event_loop().create_task(
                self._connect_and_register(config)
            )
            return self._status_for(config)

    async def remove_server(self, name: str) -> bool:
        async with self._lock:
            self._load_from_disk()
            existed = name in self._servers
            cfg = self._servers.pop(name, None)
            self._unregister_tools(name)
            self._connected.pop(name, None)
            self._errors.pop(name, None)
            self._tool_names.pop(name, None)
            self._resource_count.pop(name, None)
            self._prompt_count.pop(name, None)
            if cfg is not None and self._secret_store is not None:
                # Drop any persisted header credentials for this server.
                for hname in cfg.headers:
                    try:
                        self._secret_store.delete(
                            _SECRET_SERVICE, f"{name}.{hname}"
                        )
                    except Exception:  # noqa: BLE001 — best-effort cleanup
                        pass
                # Drop any persisted secret ENV credentials for this server.
                for ekey in cfg.secret_env_keys:
                    try:
                        self._secret_store.delete(
                            _SECRET_SERVICE, f"{name}.env.{ekey}"
                        )
                    except Exception:  # noqa: BLE001 — best-effort cleanup
                        pass
            # Close + drop the persistent pooled session for this server.
            await self._close_pooled(name)
            if existed:
                self._persist_to_disk()
            return existed

    async def set_enabled(self, name: str, enabled: bool) -> McpServerStatus:
        async with self._lock:
            self._load_from_disk()
            cfg = self._servers.get(name)
            if cfg is None:
                return McpServerStatus(
                    config=McpServerConfig(name=name or "unknown", command="_"),
                    connected=False,
                    error="server not found",
                )
            if cfg.enabled == enabled:
                # No change — return the current status (idempotent).
                return self._status_for(cfg)
            # Persist the flip on the frozen VO (dataclasses.replace).
            import dataclasses

            new_cfg = dataclasses.replace(cfg, enabled=enabled)
            self._servers[name] = new_cfg
            self._persist_to_disk()
            # Apply: ON → (re)connect + register; OFF → the gate in
            # ``_connect_and_register`` disconnects + unregisters.
            return await self._connect_and_register(new_cfg)

    def global_enabled(self) -> bool:
        """Return the GLOBAL master switch state (the single truth source).

        Reflects ``self._enabled`` — seeded from the constructor default
        (Settings.chat_mcp_enabled) and thereafter overridden by the persisted
        ``global_enabled`` in ``mcp_servers.json`` / by :meth:`set_global_enabled`.

        State-Truth-First: ensures the persisted value has been loaded BEFORE
        reading, so a standalone call returns the on-disk truth rather than the
        (possibly stale) constructor seed. ``_load_from_disk`` is synchronous,
        idempotent (``if self._loaded: return`` guard reads the file at most
        once) and lock-free — so calling it here adds no async / lock and cannot
        deadlock, keeping this method's synchronous signature intact.
        """
        self._load_from_disk()
        return self._enabled

    async def set_global_enabled(self, enabled: bool) -> None:
        """Flip the GLOBAL master switch, persist it, and re-apply every server.

        The global switch is the layer-1 gate in the three-way AND
        (global AND per-server enabled AND connected). Turning it:

        * ON → each configured + per-server-enabled server is (re)connected and
          its tools/resources/prompts registered;
        * OFF → every server is disconnected + unregistered (the ``not
          self._enabled`` branch of :meth:`_connect_and_register`), while the
          persisted configs are KEPT so the user can flip it back on.

        State-Truth-First: the per-server ``connected`` / ``error`` fields are
        driven by the REAL connect attempt (no optimistic write). The new state
        is persisted to ``mcp_servers.json`` (``global_enabled``) so it survives
        a restart as the truth source (overriding the Settings seed).
        """
        async with self._lock:
            self._load_from_disk()
            if self._enabled == enabled:
                # No change — still ensure disk carries the current value.
                self._persist_to_disk()
                return
            self._enabled = enabled
            self._persist_to_disk()
            # Re-apply to every configured server (connect on / disconnect off);
            # the gate inside ``_connect_and_register`` handles both directions.
            # Run all servers concurrently and with a per-server hard timeout so
            # a single hanging remote (SSE/HTTP) server cannot block the global
            # enable for the rest.
            async def _connect_one(cfg: McpServerConfig) -> None:
                if cfg.transport is McpTransport.STDIO:
                    await self._connect_and_register(cfg)
                    return
                _timeout = min(cfg.timeout_s + 10.0, 45.0)
                _task = asyncio.get_event_loop().create_task(
                    self._connect_and_register(cfg)
                )
                try:
                    await asyncio.wait_for(asyncio.shield(_task), timeout=_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "chat.mcp.global_enable.timeout name=%s timeout_s=%.0f",
                        cfg.name, _timeout,
                    )
                    self._connected[cfg.name] = False
                    self._errors[cfg.name] = (
                        f"connect/discover timed out after {_timeout:.0f}s"
                    )

            await asyncio.gather(
                *(_connect_one(cfg) for cfg in list(self._servers.values())),
                return_exceptions=True,
            )

    async def list_catalog(self) -> tuple[CuratedCatalogEntry, ...]:
        # USER-DRIVEN aggregation (no backend gate): return the static
        # ``curated`` source PLUS whatever dynamic ``registry`` entries are
        # already cached — but NEVER auto-fetch the network here. Opening the
        # marketplace panel must not silently reach out to a third-party
        # registry; the fetch only happens on ``refresh_catalog`` (the user's
        # explicit "load / refresh" click). On a cold start the cache is empty,
        # so this returns curated only until the user loads the registry source.
        return self._base_catalog + (self._registry_cache or ())

    async def refresh_catalog(self) -> tuple[CuratedCatalogEntry, ...]:
        """Fetch the dynamic official-registry source ON DEMAND, return catalog.

        This is the ONLY method that reaches the network — invoked when the user
        explicitly picks the "registry" source and clicks "load / refresh" in
        the panel (their click IS the consent to reach out; there is no hidden
        operator flag). Forces a fresh fetch (bypasses the TTL) and merges the
        result into the cache. Graceful — a failed fetch degrades to curated +
        any prior cache and records ``registry_source_error`` (never raises).
        """
        registry_entries = await self._get_registry_entries(force=True)
        return self._base_catalog + registry_entries

    async def browse_registry(
        self,
        *,
        search: str | None = None,
        cursor: str | None = None,
        limit: int = 30,
        source_id: str | None = None,
    ) -> tuple[tuple[CuratedCatalogEntry, ...], str | None]:
        """Browse ONE page of a registry source (user-driven).

        ``source_id`` selects which source to browse:
        - ``None`` / ``"registry"`` — the official registry (default)
        - ``"custom:<uuid>"`` — a user-added custom registry source

        Returns ``(entries, next_cursor)``.  Graceful on failure.
        """
        # Resolve source URL + credentials for this call.
        # Internal-edition preset URLs are loaded from internal_config.toml so
        # that the literals never appear in an external artifact.
        try:
            from qai.platform.edition.loader import get_mcp_hub_urls as _hub_urls
            _h = _hub_urls()
        except ImportError:
            _h: dict[str, str] = {}
        _PRESET_URLS: dict[str, str] = {}
        if _h.get("qgenie_mcphub_url"):
            _PRESET_URLS["qgenie-mcphub"] = _h["qgenie_mcphub_url"]
        if _h.get("ceflow_mcphub_url"):
            _PRESET_URLS["ceflow-mcphub"] = _h["ceflow_mcphub_url"]
        is_custom = bool(source_id and source_id.startswith("custom:"))
        custom_url: str | None = None
        bearer_token: str | None = None
        cookie: str | None = None
        if is_custom:
            raw_id = source_id.removeprefix("custom:")  # type: ignore[union-attr]
            if raw_id in _PRESET_URLS:
                custom_url = _PRESET_URLS[raw_id]
                # Try to read token from SecretStore (key: chat_mcp/registry.<raw_id>.token).
                if self._secret_store is not None:
                    try:
                        bearer_token = self._secret_store.get(
                            _SECRET_SERVICE, f"registry.{raw_id}.token"
                        ) or None
                        cookie = self._secret_store.get(
                            _SECRET_SERVICE, f"registry.{raw_id}.cookie"
                        ) or None
                    except Exception:  # noqa: BLE001
                        pass
            else:
                self._load_from_disk()
                src = next((s for s in self._custom_sources if s["id"] == raw_id), None)
                if src:
                    custom_url = src["url"]
                    bearer_token = src.get("token") or None
                    cookie = src.get("cookie") or None
        try:
            if is_custom and custom_url:
                entries, next_cursor = await _fetch_custom_source_page(
                    url=custom_url, limit=limit,
                    search=search or None, cursor=cursor or None,
                    bearer_token=bearer_token, cookie=cookie,
                )
            else:
                # Official MCP registry: standard /v0/servers pagination.
                kwargs: dict[str, Any] = {"limit": limit}
                if self._registry_source_base_url:
                    kwargs["base_url"] = self._registry_source_base_url
                if search:
                    kwargs["search"] = search
                if cursor:
                    kwargs["cursor"] = cursor
                entries, next_cursor = await _fetch_registry_page(**kwargs)
        except McpRegistrySourceError as exc:
            self._registry_source_error = str(exc)
            logger.warning("chat.mcp.registry_browse_failed reason=%s", exc)
            # If the server returned 401, the cached token is expired — evict it
            # so the next oauth_start call skips the fast-path and re-authenticates.
            if "http_status_401" in str(exc) and is_custom and raw_id in _PRESET_URLS:
                if self._secret_store is not None:
                    try:
                        self._secret_store.delete(_SECRET_SERVICE, f"registry.{raw_id}.token")
                        logger.info("chat.mcp.source_token_evicted source=%s", source_id)
                    except Exception:  # noqa: BLE001
                        pass
            return (), None
        except Exception as exc:  # noqa: BLE001 — never propagate out of the port
            self._registry_source_error = f"unexpected: {exc}"
            logger.warning("chat.mcp.registry_browse_unexpected reason=%s", exc)
            return (), None
        self._registry_source_error = ""
        self._merge_registry_cache(entries)
        return entries, next_cursor

    def _merge_registry_cache(
        self, entries: tuple[CuratedCatalogEntry, ...]
    ) -> None:
        """Accumulate browsed registry entries into the cache (de-dup by id).

        Keeps the FIRST-seen entry for a given id (a later page's duplicate does
        not overwrite it). Preserves ordering: existing cached entries first,
        then the newly-seen ones — so "load more" appends rather than reshuffles.

        Bounded by :data:`_MAX_REGISTRY_CACHE` (soft cap): the cache exists ONLY
        so a subsequent ``install_from_catalog(source="registry")`` can resolve a
        browsed entry, so it must not grow unbounded across many searches. When
        the merge exceeds the cap we keep the MOST RECENT entries (drop the
        oldest head) — the just-browsed items the user is most likely to install
        stay resolvable.
        """
        existing = self._registry_cache or ()
        seen = {e.id for e in existing}
        merged = list(existing)
        for entry in entries:
            if entry.id in seen:
                continue
            seen.add(entry.id)
            merged.append(entry)
        if len(merged) > _MAX_REGISTRY_CACHE:
            # Drop the oldest (head) entries, keep the newest tail.
            merged = merged[-_MAX_REGISTRY_CACHE:]
        self._registry_cache = tuple(merged)
        self._registry_cache_at = time.monotonic()

    async def _get_registry_entries(
        self, *, force: bool
    ) -> tuple[CuratedCatalogEntry, ...]:
        """Fetch the dynamic ``registry`` entries (cache-first, TTL-bounded).

        Within the TTL a cached result is returned WITHOUT a network call. On a
        fetch failure the method degrades gracefully: it records the reason and
        returns the last good cache if any (else an empty tuple) — it NEVER
        raises, so the panel always loads (State-Truth-First: the truth is "we
        could not reach the registry", surfaced via ``registry_source_error``,
        not a crash). Only ``refresh_catalog`` calls this (user-driven fetch).
        """
        now = time.monotonic()
        # NOTE: today the ONLY caller is ``refresh_catalog`` with ``force=True``
        # (user-driven refresh), so this cache-fresh short-circuit is currently
        # unreachable. It is kept deliberately as a cache-first seam: a future
        # caller (e.g. a ``list_catalog`` that opts into a bounded auto-refresh)
        # can pass ``force=False`` to reuse a still-fresh cache within the TTL
        # without a network round-trip. Not dead by oversight — reserved.
        cache_fresh = (
            self._registry_cache is not None
            and (now - self._registry_cache_at) < self._registry_source_ttl_s
        )
        if not force and cache_fresh:
            return self._registry_cache or ()
        try:
            kwargs: dict[str, Any] = {}
            if self._registry_source_base_url:
                kwargs["base_url"] = self._registry_source_base_url
            entries = await _fetch_registry_entries(**kwargs)
        except McpRegistrySourceError as exc:
            self._registry_source_error = str(exc)
            logger.warning("chat.mcp.registry_source_failed reason=%s", exc)
            # Degrade: keep serving the last good cache if we have one.
            return self._registry_cache or ()
        except Exception as exc:  # noqa: BLE001 — never propagate out of the port
            self._registry_source_error = f"unexpected: {exc}"
            logger.warning("chat.mcp.registry_source_unexpected reason=%s", exc)
            return self._registry_cache or ()
        self._registry_cache = entries
        self._registry_cache_at = now
        self._registry_source_error = ""
        return entries

    def catalog_sources(self) -> tuple[str, ...]:
        """Return the catalog source ids the UI should offer in its selector.

        ``curated`` first, then every id from :meth:`list_registry_sources` —
        derived rather than re-listed on purpose. The two used to build their
        edition-dependent tails independently and could disagree:
        ``catalog_sources`` appended ``custom:qgenie-mcphub`` /
        ``custom:ceflow-mcphub`` for ANY internal edition, while
        ``list_registry_sources`` only lists a hub whose URL actually resolves
        out of ``internal_config.toml``. On a tree where that file is missing
        the selector therefore offered a source with no URL behind it, and
        ``browse_registry`` — which likewise resolves preset URLs from the
        loader — could not resolve it either. Deriving one from the other makes
        "offered" and "resolvable" the same set by construction.
        """
        return ("curated", *(s["id"] for s in self.list_registry_sources()))

    def list_registry_sources(self) -> list[dict[str, Any]]:
        """Return all registry sources: built-in + user-added custom ones.

        Each entry: ``{id, name, url, builtin}``.
        Internal edition pre-seeds QGenie MCPHub and CEFlow; external edition
        only offers the public MCP registry. Custom user-added sources follow.
        """
        self._load_from_disk()
        result: list[dict[str, Any]] = [
            {
                "id": "registry",
                "name": "Anthropic官方MCP源",
                "url": "https://registry.modelcontextprotocol.io",
                "builtin": True,
            }
        ]
        if self._edition == "internal":
            try:
                from qai.platform.edition.loader import get_mcp_hub_urls as _hub_urls
                _h = _hub_urls()
            except ImportError:
                _h = {}
            if _h.get("qgenie_mcphub_url"):
                result.append({
                    "id": "custom:qgenie-mcphub",
                    "name": "QGenie MCPHub",
                    "url": _h["qgenie_mcphub_url"],
                    "builtin": True,
                })
            if _h.get("ceflow_mcphub_url"):
                result.append({
                    "id": "custom:ceflow-mcphub",
                    "name": "CEFlow",
                    "url": _h["ceflow_mcphub_url"],
                    "builtin": True,
                })
        for src in self._custom_sources:
            result.append({
                "id": f"custom:{src['id']}",
                "name": src.get("name") or src["url"],
                "url": src["url"],
                "builtin": False,
            })
        return result

    async def add_registry_source(
        self, url: str, name: str = "", token: str = "", cookie: str = ""
    ) -> dict[str, Any]:
        """Add a custom registry source or update credentials for a preset source.

        When ``url`` matches a preset source (qgenie / ceflow), saves the token
        to SecretStore and returns the preset source entry rather than creating a
        duplicate. Otherwise creates a new custom source entry.
        Returns ``{id, name, url, builtin}``.
        """
        import uuid as _uuid
        try:
            from qai.platform.edition.loader import get_mcp_hub_urls as _hub_urls
            _h = _hub_urls()
        except ImportError:
            _h = {}
        _PRESET_MAP: dict[str, tuple[str, str, str]] = {}
        if _h.get("qgenie_mcphub_url"):
            _PRESET_MAP[_h["qgenie_mcphub_url"]] = (
                "qgenie-mcphub", "QGenie MCPHub", "custom:qgenie-mcphub"
            )
        if _h.get("ceflow_mcphub_url"):
            _PRESET_MAP[_h["ceflow_mcphub_url"]] = (
                "ceflow-mcphub", "CEFlow", "custom:ceflow-mcphub"
            )
        url_clean = url.rstrip("/")
        for preset_url, (raw_id, preset_name, full_id) in _PRESET_MAP.items():
            if url_clean == preset_url.rstrip("/"):
                # Preset source: just save token/cookie to SecretStore.
                if self._secret_store is not None:
                    try:
                        if token:
                            self._secret_store.set(
                                _SECRET_SERVICE, f"registry.{raw_id}.token", token
                            )
                        if cookie:
                            self._secret_store.set(
                                _SECRET_SERVICE, f"registry.{raw_id}.cookie", cookie
                            )
                    except Exception:  # noqa: BLE001
                        pass
                return {"id": full_id, "name": preset_name, "url": preset_url, "builtin": True}
        # New custom source.
        self._load_from_disk()
        src_id = _uuid.uuid4().hex
        entry = {"id": src_id, "name": name or url, "url": url,
                 "token": token, "cookie": cookie}
        self._custom_sources.append(entry)
        self._persist_to_disk()
        return {"id": f"custom:{src_id}", "name": entry["name"],
                "url": url, "builtin": False}

    async def remove_registry_source(self, source_id: str) -> bool:
        """Remove a custom registry source by its full id (``custom:<uuid>``).

        Returns ``True`` if found and removed; ``False`` if not found.
        Built-in / preset sources cannot be removed.
        """
        _PRESET_IDS = {"registry", "qgenie-mcphub", "ceflow-mcphub"}
        self._load_from_disk()
        raw_id = source_id.removeprefix("custom:")
        if raw_id in _PRESET_IDS:
            return False
        before = len(self._custom_sources)
        self._custom_sources = [s for s in self._custom_sources if s["id"] != raw_id]
        if len(self._custom_sources) == before:
            return False
        self._persist_to_disk()
        return True

    def registry_source_error(self) -> str:
        """Return the last dynamic-source degradation reason (empty when healthy)."""
        return self._registry_source_error

    def _find_catalog_entry(
        self, entry_id: str, *, source: str | None = None
    ) -> CuratedCatalogEntry | None:
        """Resolve a catalog entry by id (+ optional source) across both sources.

        When ``source`` is given it disambiguates a cross-source id collision
        (a dynamic registry server whose slug happens to equal a curated id):
        ``"curated"`` looks up ONLY the static set, ``"registry"`` looks up ONLY
        the cached dynamic entries — so the user always installs the exact card
        they clicked. When ``source`` is ``None`` (legacy / unspecified) curated
        wins on a collision (the static, trusted set takes precedence). The
        registry side is served from the in-memory cache (a prior
        ``list_catalog`` populated it); this lookup performs NO network request
        so an install never blocks on the registry being reachable.

        The curated lookup goes through ``_base_catalog`` — the SAME
        edition-resolved tuple ``list_catalog`` serves — rather than the domain
        ``get_catalog_entry`` helper, which reads the module-level
        edition-independent constant. Two reasons: the internal entries now
        carry endpoints resolved at construction (a module-level lookup would
        return a URL-less shell), and an id that was never LISTED for this
        edition must not be installable by posting it directly.
        """
        if source == "registry":
            for entry in self._registry_cache or ():
                if entry.id == entry_id:
                    return entry
            return None
        curated = self._find_curated_entry(entry_id)
        if source == "curated":
            return curated
        if curated is not None:
            return curated
        for entry in self._registry_cache or ():
            if entry.id == entry_id:
                return entry
        return None

    def _find_curated_entry(self, entry_id: str) -> CuratedCatalogEntry | None:
        """Look ``entry_id`` up in this edition's resolved curated set."""
        for entry in self._base_catalog:
            if entry.id == entry_id:
                return entry
        return None

    async def _refetch_registry_entry(
        self, entry_id: str
    ) -> CuratedCatalogEntry | None:
        """Best-effort single-id re-fetch of a registry entry (State-Truth-First).

        The in-memory ``_registry_cache`` is only a FAST PATH for install
        resolution; the official registry is the TRUE source. When a
        ``source="registry"`` install misses the cache — e.g. the user browsed
        past :data:`_MAX_REGISTRY_CACHE` items and the earlier entry was evicted,
        or a restart cleared the cache — we go back to the registry rather than
        immediately failing.

        Reuses :meth:`browse_registry` (which fetches the first page for the
        given ``search`` term, merges results into the cache, and NEVER raises —
        a network failure returns an empty page). We search by ``entry_id`` since
        the registry only matches ``server.name`` substrings and the id is the
        name's slug tail — often a substring of the name, so this frequently
        hits. Best-effort: if the id is not a name substring (page has no match)
        or the network is down, we return ``None`` and the caller falls back to
        the original :class:`McpCatalogInstallError` (self-heals: the user can
        re-browse to the item). It NEVER upgrades a 400 into a 500.
        """
        # browse_registry swallows McpRegistrySourceError / unexpected errors
        # (returns an empty page), so this cannot raise for a network problem.
        await self.browse_registry(search=entry_id, limit=50)
        return self._find_catalog_entry(entry_id, source="registry")

    async def install_from_catalog(
        self,
        entry_id: str,
        *,
        name: str | None = None,
        arg_values: dict[str, str] | None = None,
        env_values: dict[str, str] | None = None,
        header_values: dict[str, str] | None = None,
        source: str | None = None,
    ) -> McpServerStatus:
        """Materialise a catalog entry (curated OR registry) into a server.

        Resolves the entry across BOTH sources (curated first, then the cached
        registry — no network), builds the right :class:`McpServerConfig` for
        its transport (phase 1: stdio + arg substitution; phase 2: also remote
        sse/http with ``url`` + headers, and stdio + required env keys), then
        delegates the connect + persist to :meth:`add_server` so the SAME
        three-way gate + SecretStore externalisation apply. Secret HEADER values
        (remote servers) and secret ENV values (stdio servers) are both routed
        to the SecretStore (only a ``__secret__`` sentinel on disk; re-hydrated
        at load and injected into the child process at spawn) — never persisted
        plain-text / echoed back to the client.

        Raises :class:`McpCatalogInstallError` (a ``ValueError`` subclass → HTTP
        400) on an unknown entry / a missing required argument / env / header.
        """
        # Imported lazily to avoid an application→adapter import at module load
        # (the error type lives with the use case, application layer).
        from qai.chat.application.use_cases.manage_mcp_servers import (
            McpCatalogInstallError,
        )

        entry = self._find_catalog_entry(entry_id, source=source)
        if entry is None:
            # State-Truth-First fallback: a registry entry may have been evicted
            # from the local cache (soft cap) or lost across a restart. Before
            # failing, go back to the TRUE source and re-fetch it by id once.
            # Only for the registry side (curated is a static in-process set that
            # never needs a fetch). ``source is None`` also falls through here
            # because curated already missed above, so the intent is registry.
            if source != "curated":
                entry = await self._refetch_registry_entry(entry_id)
        if entry is None:
            raise McpCatalogInstallError(f"unknown catalog entry {entry_id!r}")

        # If the entry carries a packageUrl (.mcp zip), download + extract it
        # into a per-entry directory under the data folder, create a venv, and
        # install dependencies.  The materialised config then uses the venv
        # python + the extracted server.py rather than the raw command.
        pkg_url = getattr(entry, "package_url", "") or ""
        if pkg_url:
            server_name = name or entry.id
            try:
                entry = await _install_mcp_package(
                    entry, server_name=server_name,
                    data_dir=self._config_path.parent.parent,
                    error_cls=McpCatalogInstallError,
                )
            except McpCatalogInstallError:
                raise
            except Exception as exc:
                raise McpCatalogInstallError(
                    f"package install failed for {entry_id!r}: {exc}"
                ) from exc

        config = _materialise_entry(
            entry,
            name=name,
            arg_values=dict(arg_values or {}),
            env_values=dict(env_values or {}),
            header_values=dict(header_values or {}),
            error_cls=McpCatalogInstallError,
        )
        return await self.add_server(config)

    async def test_server(self, name: str) -> McpServerStatus:
        async with self._lock:
            self._load_from_disk()
            cfg = self._servers.get(name)
            if cfg is None:
                return McpServerStatus(
                    config=McpServerConfig(name=name or "unknown", command="_"),
                    connected=False,
                    error="server not found",
                )
            # Do not open a browser window from an HTTP request — if no OAuth
            # token is cached the server will show as disconnected (user must
            # use the Install / Connect flow to authenticate).
            return await self._connect_and_register(cfg, silent_oauth=True)

    async def connect_all(self) -> None:
        """Connect every persisted server (called at startup when enabled).

        Best-effort: a failed server is recorded with its error and skipped;
        the others still connect.  A no-op when the registry is disabled.
        """
        async with self._lock:
            self._load_from_disk()
            if not self._enabled:
                return

            async def _connect_one_startup(cfg: McpServerConfig) -> None:
                if cfg.transport is McpTransport.STDIO:
                    await self._connect_and_register(cfg, silent_oauth=True)
                    return
                _timeout = min(cfg.timeout_s + 10.0, 45.0)
                _task = asyncio.get_event_loop().create_task(
                    self._connect_and_register(cfg, silent_oauth=True)
                )
                try:
                    await asyncio.wait_for(asyncio.shield(_task), timeout=_timeout)
                except asyncio.TimeoutError:
                    logger.warning(
                        "chat.mcp.connect_all.timeout name=%s timeout_s=%.0f",
                        cfg.name, _timeout,
                    )
                    self._connected[cfg.name] = False
                    self._errors[cfg.name] = (
                        f"connect/discover timed out after {_timeout:.0f}s"
                    )

            await asyncio.gather(
                *(_connect_one_startup(cfg) for cfg in list(self._servers.values())),
                return_exceptions=True,
            )

    async def aclose(self) -> None:
        # Tear down every persistent pooled session (terminate the spawned
        # subprocesses / close remote HTTP clients) and drop the registered tool
        # handlers so a re-wire does not double-register.
        async with self._pool_lock:
            clients = list(self._pool.values())
            self._pool.clear()
        for client in clients:
            with contextlib.suppress(Exception):
                await client.aclose()
        async with self._lock:
            for name in list(self._registered_qtools):
                self._unregister_tools(name)

    # ---- resources / prompts surface (MCP-RESOURCES-SURFACE) -------------
    # Every method here obeys the SAME gate as the tools surface:
    #   * registry disabled (``chat_mcp_enabled=false``) → empty / "mcp disabled"
    #   * per-server: only a CURRENTLY-connected server is queried; an
    #     unconnected / failed / unknown server is skipped (list) or errors
    #     (read/get). This is the SAME ``self._enabled`` + ``self._connected``
    #     truth the tools surface uses — no second, looser judgement.

    def _is_connected(self, name: str) -> bool:
        # Three-way AND (State-Truth-First — check all three explicitly rather
        # than relying only on the connect side-effect): GLOBAL master switch
        # AND the per-server ``enabled`` switch AND the live connection state.
        if not self._enabled:
            return False
        cfg = self._servers.get(name)
        if cfg is None or not cfg.enabled:
            return False
        return bool(self._connected.get(name, False))

    async def list_resources(self) -> tuple[McpResource, ...]:
        async with self._lock:
            self._load_from_disk()
            if not self._enabled:
                return ()
            out: list[McpResource] = []
            for cfg in self._servers.values():
                if not self._is_connected(cfg.name):
                    continue
                out.extend(await self._safe_discover_resources(cfg))
            return tuple(out)

    async def list_prompts(self) -> tuple[McpPrompt, ...]:
        async with self._lock:
            self._load_from_disk()
            if not self._enabled:
                return ()
            out: list[McpPrompt] = []
            for cfg in self._servers.values():
                if not self._is_connected(cfg.name):
                    continue
                out.extend(await self._safe_discover_prompts(cfg))
            return tuple(out)

    async def read_resource(self, server_name: str, uri: str) -> str:
        async with self._lock:
            self._load_from_disk()
            if not self._enabled:
                return "[mcp error] mcp disabled (chat_mcp_enabled=false)"
            if not self._is_connected(server_name):
                return f"[mcp error] server {server_name!r} is not connected"
            cfg = self._servers.get(server_name)
            if cfg is None:
                return f"[mcp error] server {server_name!r} not found"
        # Network call OUTSIDE the lock (the config is captured; a concurrent
        # remove only makes the read fail, never corrupts shared state). Reuses
        # the persistent pooled session (no per-call spawn).
        try:
            client = await self._acquire_client(cfg)
            return await client.read_resource(uri)
        except Exception as exc:  # noqa: BLE001 — surface as tool error text
            return f"[mcp error] read failed: {exc}"

    async def get_prompt(
        self, server_name: str, name: str, arguments: dict[str, Any]
    ) -> str:
        async with self._lock:
            self._load_from_disk()
            if not self._enabled:
                return "[mcp error] mcp disabled (chat_mcp_enabled=false)"
            if not self._is_connected(server_name):
                return f"[mcp error] server {server_name!r} is not connected"
            cfg = self._servers.get(server_name)
            if cfg is None:
                return f"[mcp error] server {server_name!r} not found"
        try:
            client = await self._acquire_client(cfg)
            return await client.get_prompt(name, arguments or {})
        except Exception as exc:  # noqa: BLE001 — surface as tool error text
            return f"[mcp error] get_prompt failed: {exc}"

    # ---- status projection accessors (for the HTTP routes) ----------------

    def resource_count(self, name: str) -> int:
        """Return the last-discovered resource count for a connected server."""
        return self._resource_count.get(name, 0)

    def prompt_count(self, name: str) -> int:
        """Return the last-discovered prompt count for a connected server."""
        return self._prompt_count.get(name, 0)
