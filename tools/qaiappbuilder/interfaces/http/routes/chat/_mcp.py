# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""MCP (Model Context Protocol) server management HTTP routes.

Endpoints (all under ``/api/chat/mcp``)::

    GET    /api/chat/mcp/servers                 — list servers + live status
    POST   /api/chat/mcp/servers                 — add / replace a server
    PATCH  /api/chat/mcp/servers/{name}          — flip per-server enabled switch
    DELETE /api/chat/mcp/servers/{name}          — remove a server + its tools
    POST   /api/chat/mcp/servers/{name}/test     — re-connect + re-discover
    GET    /api/chat/mcp/servers/{name}/resources — list one server's resources
    GET    /api/chat/mcp/servers/{name}/prompts  — list one server's prompts
    PATCH  /api/chat/mcp/enabled                 — flip the GLOBAL master switch
    GET    /api/chat/mcp/catalog                 — the curated marketplace source
    GET    /api/chat/mcp/catalog/browse          — browse the registry (search/page)

PURE V2 enhancement (V1 has no MCP). New routes only — no existing path /
method / payload changed (§3.1). Handlers are thin (``interfaces-stays-thin``):
parse → one ``execute(...)`` on ``container.chat.manage_mcp_servers_use_case``
→ serialise. The route NEVER imports ``qai.chat.adapters.*`` /
``.infrastructure.*`` — it reaches the registry only via the DI namespace.

Once a server is connected AND enabled its tools are registered on the shared
chat tool registry, so they surface automatically in ``GET /api/chat/tools`` and
are advertised to the LLM on the next turn (no per-route wiring needed).
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import asyncio
import base64
import contextlib
import hashlib
import os
import re
import secrets
import threading
import urllib.parse
import webbrowser
from typing import TYPE_CHECKING, Any, Literal

import httpx
from fastapi import APIRouter, HTTPException, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, field_validator

from qai.chat.domain.mcp_server import McpServerConfig, McpTransport
from qai.platform.config.settings import LOOPBACK_HOST, LOOPBACK_HOST_NAME

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

    from qai.chat.application.ports import McpServerStatus


def _relaxed_ssl_context(*, verify: bool):
    """Return an ``ssl.SSLContext`` honouring the global ``verify`` toggle.

    * ``verify=True``  → default context: ``check_hostname=True`` +
      ``verify_mode=CERT_REQUIRED`` (strict RFC 8446 / hostname check).
    * ``verify=False`` → hostname check off + ``CERT_NONE`` — kept for the
      internal edition's enterprise MITM TLS gateway (``Settings.ssl_verify``
      defaults to ``False`` on ``edition=internal``).

    Consolidates the four OAuth-flow call-sites in this module that previously
    hard-coded ``CERT_NONE`` regardless of edition, so external / strict-mode
    builds now get proper certificate verification (root cause: shipped MITM
    surface on external where ``ssl_verify=True``).
    """
    import ssl as _ssl

    ctx = _ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = _ssl.CERT_NONE
    return ctx

# ---- DTOs -----------------------------------------------------------------


class McpServerConfigModel(BaseModel):
    """Request/response body describing one MCP server config.

    Credential-bearing ``headers`` values are accepted on POST and persisted to
    the SecretStore; they are NEVER echoed back on GET (the response masks them
    with the ``__secret__`` sentinel — see :func:`_status_to_model`).
    """

    name: str = Field(min_length=1, max_length=128)
    transport: Literal["stdio", "sse", "http"] = "stdio"
    command: str | None = Field(default=None, max_length=4096)
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = Field(default=None, max_length=4096)
    url: str | None = Field(default=None, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = Field(default=30.0, gt=0.0, le=600.0)

    def to_domain(self) -> McpServerConfig:
        return McpServerConfig(
            name=self.name,
            transport=McpTransport(self.transport),
            command=self.command,
            args=tuple(self.args),
            env=dict(self.env),
            cwd=self.cwd,
            url=self.url,
            headers=dict(self.headers),
            timeout_s=self.timeout_s,
        )


class McpServerStatusModel(BaseModel):
    """One server's config + live connection status."""

    name: str
    transport: str
    command: str | None = None
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    url: str | None = None
    headers: dict[str, str] = Field(default_factory=dict)
    timeout_s: float = 30.0
    connected: bool = False
    tool_count: int = 0
    tool_names: list[str] = Field(default_factory=list)
    resource_count: int = 0
    prompt_count: int = 0
    enabled: bool = True
    error: str = ""


class McpResourceModel(BaseModel):
    """One MCP resource (``GET …/resources`` item)."""

    server_name: str
    uri: str
    name: str = ""
    mime_type: str = ""


class McpResourceListResponse(BaseModel):
    """``GET /api/chat/mcp/servers/{name}/resources`` body."""

    resources: list[McpResourceModel]


class McpPromptArgumentModel(BaseModel):
    """One declared prompt argument."""

    name: str
    description: str = ""
    required: bool = False


class McpPromptModel(BaseModel):
    """One MCP prompt (``GET …/prompts`` item)."""

    server_name: str
    name: str
    description: str = ""
    arguments: list[McpPromptArgumentModel] = Field(default_factory=list)


class McpPromptListResponse(BaseModel):
    """``GET /api/chat/mcp/servers/{name}/prompts`` body."""

    prompts: list[McpPromptModel]


class McpServerListResponse(BaseModel):
    """``GET /api/chat/mcp/servers`` body."""

    servers: list[McpServerStatusModel]
    enabled: bool = Field(
        description=(
            "Whether the master MCP execution gate (chat_mcp_enabled) is on. "
            "When False, servers can be configured but are not connected."
        ),
    )


class McpSetEnabledRequest(BaseModel):
    """``PATCH /api/chat/mcp/servers/{name}`` body — flip the per-server switch."""

    enabled: bool


class McpCatalogInstallRequest(BaseModel):
    """``POST /api/chat/mcp/catalog/{entry_id}/install`` body.

    ``name`` overrides the installed server name (defaults to the entry id).
    ``arg_values`` maps each ``<PLACEHOLDER>`` token (e.g. ``"<PATH>"``) to the
    user-supplied value; every placeholder in the entry's ``requires_args`` must
    be present and non-empty.

    Phase-2 (registry source): ``env_values`` supplies the declared env vars for
    a stdio+keyed server; ``header_values`` supplies the declared HTTP headers
    for a remote (sse/http) server. Secret HEADER values are routed to the
    SecretStore; secret ENV values are likewise externalised to the SecretStore
    (only a ``__secret__`` sentinel on disk), non-secret env persisted as-is.
    ``source`` (``"curated"`` / ``"registry"``) disambiguates a cross-source id
    collision so the exact browsed card is installed.
    """

    name: str | None = Field(default=None, max_length=128)
    arg_values: dict[str, str] = Field(default_factory=dict)
    env_values: dict[str, str] = Field(default_factory=dict)
    header_values: dict[str, str] = Field(default_factory=dict)
    source: str | None = Field(default=None, max_length=64)


class McpCatalogEntryModel(BaseModel):
    """One marketplace catalog entry (``GET /api/chat/mcp/catalog``).

    Curated entries carry the phase-1 fields; dynamic ``registry`` entries also
    populate the phase-2 fields (``transport`` / ``url`` for remotes,
    ``env_required`` / ``headers_schema`` / ``headers_required`` /
    ``secret_fields`` for the install form). All phase-2 fields default so a
    curated entry serialises unchanged (additive — §3.1).
    """

    id: str
    name: str
    description: str
    source: str
    install_type: str
    command: str
    args_template: list[str]
    requires_args: list[str] = Field(default_factory=list)
    env_schema: list[str] = Field(default_factory=list)
    homepage: str = ""
    # ── phase-2 dynamic-registry fields (additive) ──
    transport: str = "stdio"
    url: str = ""
    env_required: list[str] = Field(default_factory=list)
    headers_schema: list[str] = Field(default_factory=list)
    headers_required: list[str] = Field(default_factory=list)
    secret_fields: list[str] = Field(default_factory=list)
    headers_labels: dict[str, str] = Field(default_factory=dict)
    oauth_url: str = ""
    oauth_metadata_url: str = ""
    package_url: str = ""


class McpCatalogResponse(BaseModel):
    """``GET /api/chat/mcp/catalog`` body.

    ``sources`` lists the catalog source ids the UI should offer in its selector
    (``["curated", "registry"]`` — the dynamic registry source is always
    offered so the user can pick it and click "load / refresh"). Listing it does
    NOT mean the network was hit: ``registry`` entries only appear after the
    user explicitly refreshes. ``registry_error`` carries a short human-readable
    degradation reason when the last on-demand refresh could not reach the
    registry (empty otherwise) — surfaced as a soft UI banner; the catalog still
    lists the curated entries (graceful degrade).
    """

    entries: list[McpCatalogEntryModel]
    sources: list[str] = Field(default_factory=list)
    registry_error: str = ""


# ── phase-3 additive DTOs (global switch + registry browse) — tail-appended ──


class McpSetGlobalEnabledRequest(BaseModel):
    """``PATCH /api/chat/mcp/enabled`` body — flip the GLOBAL master switch."""

    enabled: bool


class McpCatalogBrowseResponse(BaseModel):
    """``GET /api/chat/mcp/catalog/browse`` body — one page of registry entries.

    ``entries`` are the mapped registry servers for this page (source="registry").
    ``next_cursor`` is the opaque cursor to fetch the NEXT page; ``None`` (or
    absent) means the last page was reached (hide the "load more" affordance).
    ``registry_error`` carries a short degradation reason when the browse could
    not reach the registry (empty when healthy) — surfaced as a soft banner.
    """

    entries: list[McpCatalogEntryModel]
    next_cursor: str | None = None
    registry_error: str = ""


# ── Registry source management DTOs (tail-appended) ────────────────────────


class McpRegistrySourceModel(BaseModel):
    """One registry source (builtin or user-added custom)."""

    id: str
    name: str
    url: str
    builtin: bool = False


class McpAddRegistrySourceRequest(BaseModel):
    """``POST /api/chat/mcp/catalog/sources`` body."""

    url: str = Field(min_length=1, max_length=2048)
    name: str = Field(default="", max_length=128)
    token: str = Field(default="", max_length=4096)
    cookie: str = Field(default="", max_length=8192)


# ── OAuth 2.1 + PKCE session DTOs (tail-appended) ──────────────────────────


#: The theme keys the callback page renders. A client-supplied key outside this
#: set is dropped: the page interpolates each value into a ``<style>`` block, so
#: the accepted surface is pinned here rather than being whatever the caller
#: happens to send.
_THEME_KEYS: frozenset[str] = frozenset({
    "bg-primary",
    "bg-secondary",
    "border",
    "text-primary",
    "text-secondary",
    "accent",
    "accent-hover",
})

#: A CSS color, and nothing else: ``#rgb`` / ``#rrggbb`` / ``#rrggbbaa``, the
#: ``rgb()``/``rgba()``/``hsl()``/``hsla()`` functional forms, or a bare CSS
#: identifier (a named color such as ``rebeccapurple``). Deliberately narrow —
#: it admits no ``;``, ``}``, ``<``, ``/`` or whitespace-separated second token,
#: which is what keeps a value from closing the style element it sits in.
_CSS_COLOR_RE = re.compile(
    r"""^(?:
        \#[0-9A-Fa-f]{3,8}
      | (?:rgb|rgba|hsl|hsla)\(\s*[0-9A-Za-z.%,\s/]{1,64}\)
      | [A-Za-z][A-Za-z0-9-]{0,31}
    )$""",
    re.VERBOSE,
)


def _sanitise_theme(theme: dict[str, str]) -> dict[str, str]:
    """Keep only known keys whose value is a well-formed CSS color.

    The OAuth callback page interpolates these values into a ``<style>`` block
    and serves the result on a localhost origin at the exact moment the flow
    holds an access token. Unvalidated, a value such as
    ``#fff}</style><script>…</script><style>x{`` closes the style element and
    injects script into that page — so validate here, at the boundary, and let
    the renderer fall back to its defaults for anything rejected. Whitelist
    rather than escape: these are colors, and a color that does not look like a
    color is a bug or an attack, never something to render best-effort.
    """
    return {
        key: value
        for key, value in theme.items()
        if key in _THEME_KEYS and _CSS_COLOR_RE.match(value)
    }


def _callback_page_css(theme: dict[str, str]) -> bytes:
    """Build the OAuth callback page's ``<style>`` body from ``theme``.

    Module-level (not a closure inside the callback handler) so the injection
    guard below is directly testable: this is the only place a client-supplied
    value reaches served HTML.
    """
    # Re-sanitise on read. The DTO validator already filtered what a request
    # can store, but the session is a plain dict and every value here lands
    # inside a <style> block, so this path does not depend on someone else
    # having validated first.
    th = _sanitise_theme(theme)
    bg   = th.get("bg-primary",     "#1a1a24")
    bg2  = th.get("bg-secondary",   bg)
    bdr  = th.get("border",         "#2e2e42")
    txt  = th.get("text-primary",   "#f0f0ff")
    sub  = th.get("text-secondary", "#8888aa")
    acc  = th.get("accent",         "#6c63ff")
    acc2 = th.get("accent-hover",   "#a78bfa")
    return (
        f"*{{margin:0;padding:0;box-sizing:border-box}}"
        f"body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
        f"background:{bg};color:{txt};"
        f"display:flex;align-items:center;justify-content:center;height:100vh;}}"
        f".card{{background:{bg2};border:1px solid {bdr};"
        f"border-radius:16px;padding:48px 56px;text-align:center;width:480px;}}"
        f".icon{{width:64px;height:64px;"
        f"background:linear-gradient(135deg,{acc},{acc2});"
        f"border-radius:50%;display:flex;align-items:center;justify-content:center;"
        f"margin:0 auto 24px;font-size:28px;color:#fff}}"
        f"h2{{font-size:20px;font-weight:600;margin-bottom:12px;color:{txt}}}"
        f"p{{font-size:14px;color:{sub};line-height:1.6}}"
    ).encode()


class McpOAuthStartRequest(BaseModel):
    """Optional body for ``POST /api/chat/mcp/catalog/{entry_id}/oauth/start``.

    ``theme`` carries the current app CSS variable values so the callback page
    can render with matching colors instead of hardcoded defaults. Values are
    NOT trusted as sent: the validator drops unknown keys and any value that is
    not a CSS color, so a session can only ever hold render-safe values.
    """

    theme: dict[str, str] = Field(default_factory=dict)

    @field_validator("theme")
    @classmethod
    def _validate_theme(cls, value: dict[str, str]) -> dict[str, str]:
        # Drop bad entries instead of raising 422: a themed callback page is
        # cosmetic, and failing the whole OAuth flow over one unrecognised
        # color would be a worse outcome than rendering the default palette.
        return _sanitise_theme(value)


class McpOAuthStartResponse(BaseModel):
    """``POST /api/chat/mcp/catalog/{entry_id}/oauth/start`` response."""

    auth_url: str
    session_id: str


class McpOAuthTokenResponse(BaseModel):
    """``GET /api/chat/mcp/catalog/{entry_id}/oauth/token`` response."""

    status: Literal["pending", "done", "error"]
    access_token: str = ""
    error: str = ""


# In-process OAuth session store: session_id → state dict.
# Intentionally module-level (survives route rebuilds within one process).
# Each entry: {code_verifier, done, access_token, error}
_OAUTH_SESSIONS: dict[str, dict[str, Any]] = {}

# In-flight dedup: hub_host → session_id for oauth_start calls that have not
# yet completed.  Prevents N concurrent preheat calls for the same hub from
# each running the full probe+headless+SSO chain in parallel.
_OAUTH_INFLIGHT: dict[str, str] = {}  # netloc → session_id

# Prefix of the throw-away Chromium ``--user-data-dir`` profiles created for the
# visible OAuth popup.  Shared by the creator and both reapers below so a rename
# cannot orphan the cleanup.
_OAUTH_PROFILE_PREFIX = "qai_oauth_"

# Give the browser a bounded window to finish writing the profile out; past this
# the popup is considered abandoned and its profile is swept by the next run.
_OAUTH_PROFILE_REAP_TIMEOUT_S: float = 15 * 60.0


def _reap_oauth_profile_when_closed(
    proc: Any,
    profile_dir: str,
    label: str,
) -> None:
    """Delete *profile_dir* once the popup browser *proc* exits.

    Waits in its own daemon thread: the caller runs on the OAuth worker thread
    and must return promptly so the callback server can accept the redirect, and
    the popup lives until the user finishes authenticating (potentially
    minutes).  A daemon thread also means a shutdown mid-flow does not hang the
    process — the leftover dir is then picked up by
    :func:`_sweep_stale_oauth_profiles` on the next OAuth attempt.
    """
    import shutil as _shutil
    import threading as _threading

    def _wait_then_rm() -> None:
        # Waits with NO timeout on purpose: deleting a profile while its browser
        # still holds it open would be a partial rmtree of a live profile.  An
        # abandoned popup is instead bounded by the sweeper's age gate, and this
        # thread is a daemon so an unfinished wait never delays shutdown.  A dead
        # handle raises here — fall through to the delete in that case.
        with contextlib.suppress(Exception):
            proc.wait()
        _shutil.rmtree(profile_dir, ignore_errors=True)

    _threading.Thread(
        target=_wait_then_rm,
        daemon=True,
        name=f"mcp-oauth-profile-reap-{label}",
    ).start()


def _sweep_stale_oauth_profiles() -> None:
    """Best-effort delete of OAuth popup profiles left by earlier runs.

    Only touches dirs matching :data:`_OAUTH_PROFILE_PREFIX` inside the temp
    dir, and only those older than the reap timeout, so a profile belonging to
    an in-flight popup (this process's or another instance's) is never yanked
    out from under a running browser.  Silent by design — cleanup must never
    fail an install.
    """
    import os as _os
    import shutil as _shutil
    import tempfile as _tempfile
    import time as _time

    try:
        root = _tempfile.gettempdir()
        cutoff = _time.time() - _OAUTH_PROFILE_REAP_TIMEOUT_S
        for name in _os.listdir(root):
            if not name.startswith(_OAUTH_PROFILE_PREFIX):
                continue
            path = _os.path.join(root, name)
            try:
                stale = _os.path.isdir(path) and _os.path.getmtime(path) <= cutoff
            except OSError:
                continue  # vanished / unreadable — nothing to reap
            if stale:
                _shutil.rmtree(path, ignore_errors=True)
    except OSError as exc:  # pragma: no cover — never break the OAuth flow
        logger.debug("chat.mcp.oauth_profile_sweep_failed: %s", exc)


def _oauth_open_browser(
    auth_url: str,
    session: dict[str, Any],
    label: str,
) -> None:
    """Schedule a daemon thread that opens the browser for *auth_url*.

    Intentionally a plain synchronous function — it just spawns a thread.  All
    the actual work (browser launch, profile reaping) happens in that thread,
    completely outside asyncio/anyio, so there is zero interaction with cancel
    scopes.

    *session* is not read here; it is handed to the worker so a future strategy
    can observe the flow completing without changing this signature.
    """
    t = threading.Thread(
        target=_oauth_open_browser_sync,
        args=(auth_url, session, label),
        daemon=True,
        name=f"mcp-oauth-browser-{label}",
    )
    t.start()


def _oauth_open_browser_sync(
    auth_url: str,
    session: dict[str, Any],
    label: str,
) -> None:
    """Open the OAuth authorization URL in a browser — runs in a daemon thread.

    On Windows this prefers Edge/Chrome ``--app`` mode, which opens a compact
    popup window (no address bar, centred) instead of a new browser tab, and
    falls back to the shell default when neither is installed.

    ``--window-size``/``--window-position`` only apply to a browser process this
    call actually starts: when an instance is already running, the launcher hands
    the URL to it and exits, and the flags are ignored.  Chromium's
    single-instance lock is keyed on ``--user-data-dir``, so passing a private
    throw-away profile is what forces a fresh process — that is why the popup
    gets a mkdtemp profile, and it is also what makes the Popen handle track the
    popup's own lifetime (see :func:`_reap_oauth_profile_when_closed`).

    *session* is currently unused; it is accepted so a caller-visible completion
    signal can be added without touching every call site.
    """
    del session  # reserved — see docstring

    import subprocess as _subprocess
    import sys as _sys

    try:
        if _sys.platform == "win32":
            import ctypes as _ctypes
            import os as _os
            import tempfile as _tempfile

            _w, _h = 600, 480
            try:
                _sw = _ctypes.windll.user32.GetSystemMetrics(0)
                _sh = _ctypes.windll.user32.GetSystemMetrics(1)
                _px = max(0, (_sw - _w) // 2)
                _py = max(0, (_sh - _h) // 2)
            except Exception:
                _px, _py = 400, 200

            # Reap any profile left behind by a previous run before creating a
            # new one, so a crash / hard kill (which skips the reaper below)
            # cannot accumulate dirs forever. A whole Chromium profile is tens
            # of MB, and OAuth install is a repeatable user action.
            _sweep_stale_oauth_profiles()

            _opened = False
            for _exe in (
                r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            ):
                if not _os.path.exists(_exe):
                    continue

                _tmpdir = _tempfile.mkdtemp(prefix=_OAUTH_PROFILE_PREFIX)
                _proc = _subprocess.Popen([
                    _exe,
                    f"--app={auth_url}",
                    f"--window-size={_w},{_h}",
                    f"--window-position={_px},{_py}",
                    f"--user-data-dir={_tmpdir}",
                    "--no-first-run",
                    "--no-default-browser-check",
                ])
                # Delete the profile once the popup closes instead of leaking it.
                _reap_oauth_profile_when_closed(_proc, _tmpdir, label)
                _opened = True
                break
            if not _opened:
                _os.startfile(auth_url)  # type: ignore[attr-defined]
        elif _sys.platform == "darwin":
            _subprocess.Popen(["open", auth_url])
        else:
            _subprocess.Popen(["xdg-open", auth_url])
        logger.info("chat.mcp.oauth_browser_opened label=%s", label)
    except Exception as exc:
        logger.warning("chat.mcp.oauth_browser_open_failed label=%s: %s", label, exc)
        try:
            import webbrowser as _wb
            _wb.open(auth_url, new=2)
        except Exception:
            pass



def _start_oauth_callback_thread(
    session: dict[str, Any],
    callback_port: int,
    *,
    on_token_saved: Any = None,
    ssl_verify: bool = True,
) -> None:
    """Start a one-shot HTTP callback server in a plain thread (NOT asyncio/anyio).

    The server accepts ONE connection at ``/callback?code=...``, exchanges the
    code for an access token using a synchronous HTTP call, stores the result in
    *session*, then shuts itself down.  Because this runs entirely in a daemon
    ``threading.Thread`` it has zero interaction with anyio's cancel scopes and
    cannot trigger the ``RuntimeError("Attempted to exit a cancel scope...")``
    that plagued the ``asyncio.start_server`` approach.

    ``on_token_saved`` is an optional zero-arg callable invoked (in the thread)
    after the token has been written to the session, e.g. to persist it to a
    SecretStore.
    """
    import http.server as _http_server
    import urllib.request as _urllib_request
    import urllib.error as _urllib_error
    import json as _json

    # Use an Event so the handler can signal the server to stop cleanly.
    _stop_event = threading.Event()

    class _Handler(_http_server.BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # type: ignore[override]
            pass  # suppress default access-log noise

        def do_GET(self) -> None:  # noqa: N802
            try:
                self._do_GET_inner()
            except Exception:
                pass  # swallow BrokenPipeError / ConnectionResetError from headless browsers

        def _do_GET_inner(self) -> None:
            if session.get("done"):
                self._reply(b"<h1>Done</h1>")
                _stop_event.set()
                return
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            code = (qs.get("code") or [""])[0]
            err_p = (qs.get("error") or [""])[0]
            if err_p:
                session["error"] = err_p
                session["done"] = True
                self._reply(b"<h1>Authentication cancelled</h1>")
                _stop_event.set()
                return
            if not code:
                session["error"] = "no_code"
                session["done"] = True
                self._reply(b"<h1>No authorization code received</h1>")
                _stop_event.set()
                return
            # Synchronous token exchange — safe from a plain thread.
            try:
                post_data = urllib.parse.urlencode({
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": session["redirect_uri"],
                    "client_id": session["client_id"],
                    "code_verifier": session["code_verifier"],
                }).encode()
                req = _urllib_request.Request(
                    session["token_endpoint"],
                    data=post_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                try:
                    ctx = _relaxed_ssl_context(verify=ssl_verify)
                except Exception:
                    ctx = None
                with _urllib_request.urlopen(req, context=ctx, timeout=15) as resp:
                    body = resp.read()
                tj = _json.loads(body)
                token_val: str = tj.get("access_token") or tj.get("token", "")
                session["access_token"] = token_val
                session["done"] = True
                if on_token_saved is not None:
                    try:
                        on_token_saved(token_val)
                    except Exception as _pe:
                        logger.warning("chat.mcp.oauth_token_persist_error: %s", _pe)
            except Exception as exc2:
                # Only mark as error if token was NOT successfully obtained.
                # ConnectionResetError from _reply must not overwrite a
                # successfully acquired token (headless browser closes the
                # connection immediately after sending the callback request).
                if not session.get("access_token"):
                    session["error"] = f"token_exchange_error:{exc2}"
                    session["done"] = True
            # Reply outside the token-exchange try block so a broken-pipe /
            # connection-reset from a headless browser cannot corrupt the
            # session state that was already committed above.
            try:
                _page_css = _callback_page_css(session.get("theme") or {})
                if session.get("error") and not session.get("access_token"):
                    self._reply(
                        b"<html><head><meta charset='utf-8'><style>"
                        + _page_css +
                        b"</style></head>"
                        b"<body><div class='card'>"
                        b"<div class='icon'>&#10007;</div>"
                        b"<h2>Authentication failed</h2>"
                        b"<p>Token exchange error. Please close this window and try again.</p>"
                        b"</div></body></html>"
                    )
                else:
                    self._reply(
                        b"<html><head><meta charset='utf-8'><style>"
                        + _page_css +
                        b"</style></head>"
                        b"<body><div class='card'>"
                        b"<div class='icon'>&#10003;</div>"
                        b"<h2>Authentication successful</h2>"
                        b"<p>You may close this window and return to QAI Model Builder.</p>"
                        b"</div></body></html>"
                    )
            except Exception:
                pass  # headless browser already closed the connection — ignore
            _stop_event.set()

        def _reply(self, body: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

    def _serve() -> None:
        try:
            srv = _http_server.HTTPServer((LOOPBACK_HOST, callback_port), _Handler)
            srv.timeout = 1.0  # poll _stop_event every second
            while not _stop_event.is_set():
                srv.handle_request()
            srv.server_close()
        except Exception as exc_srv:
            logger.warning("chat.mcp.oauth_callback_server_error port=%d: %s", callback_port, exc_srv)
            if not session.get("done"):
                session["error"] = f"callback_server_error:{exc_srv}"
                session["done"] = True

    t = threading.Thread(target=_serve, daemon=True, name=f"mcp-oauth-cb-{callback_port}")
    t.start()


def _status_to_model(
    status_obj: "McpServerStatus",
    *,
    resource_count: int = 0,
    prompt_count: int = 0,
) -> McpServerStatusModel:
    cfg = status_obj.config
    return McpServerStatusModel(
        name=cfg.name,
        transport=cfg.transport.value,
        command=cfg.command,
        args=list(cfg.args),
        env=dict(cfg.env),
        cwd=cfg.cwd,
        url=cfg.url,
        # Mask credential values — never echo a secret back to the client.
        headers={k: "__secret__" for k in cfg.headers},
        timeout_s=cfg.timeout_s,
        connected=status_obj.connected,
        tool_count=status_obj.tool_count,
        tool_names=list(status_obj.tool_names),
        resource_count=resource_count,
        prompt_count=prompt_count,
        enabled=getattr(cfg, "enabled", True),
        error=status_obj.error,
    )


def _catalog_entry_to_model(entry: object) -> McpCatalogEntryModel:
    """Serialise one catalog entry VO (curated or registry) into its DTO."""
    return McpCatalogEntryModel(
        id=entry.id,  # type: ignore[attr-defined]
        name=entry.name,  # type: ignore[attr-defined]
        description=entry.description,  # type: ignore[attr-defined]
        source=entry.source,  # type: ignore[attr-defined]
        install_type=entry.install_type,  # type: ignore[attr-defined]
        command=entry.command,  # type: ignore[attr-defined]
        args_template=list(entry.args_template),  # type: ignore[attr-defined]
        requires_args=list(entry.requires_args),  # type: ignore[attr-defined]
        env_schema=list(entry.env_schema),  # type: ignore[attr-defined]
        homepage=entry.homepage,  # type: ignore[attr-defined]
        transport=getattr(entry, "transport", "stdio"),
        url=getattr(entry, "url", ""),
        env_required=list(getattr(entry, "env_required", ())),
        headers_schema=list(getattr(entry, "headers_schema", ())),
        headers_required=list(getattr(entry, "headers_required", ())),
        secret_fields=list(getattr(entry, "secret_fields", ())),
        headers_labels=dict(getattr(entry, "headers_labels", None) or {}),
        oauth_url=getattr(entry, "oauth_url", ""),
        oauth_metadata_url=getattr(entry, "oauth_metadata_url", ""),
        package_url=getattr(entry, "package_url", ""),
    )


def _catalog_response(entries: object, registry: object) -> McpCatalogResponse:
    """Build the ``GET/POST …/catalog`` response from entry VOs + the registry.

    ``sources`` prefers the registry's declared source list (so ``registry``
    shows even before/without a successful fetch, i.e. the selector appears);
    falls back to the distinct entry sources. ``registry_error`` is the
    registry's last dynamic-source degradation reason (best-effort; a test stub
    without the accessor reports none).
    """
    models = [_catalog_entry_to_model(e) for e in entries]  # type: ignore[union-attr]
    sources: list[str] = []
    src_fn = getattr(registry, "catalog_sources", None)
    if callable(src_fn):
        try:
            sources = list(src_fn())
        except Exception:  # noqa: BLE001 — best-effort; fall back below
            sources = []
    if not sources:
        sources = sorted({m.source for m in models}) or ["curated"]
    registry_error = ""
    err_fn = getattr(registry, "registry_source_error", None)
    if callable(err_fn):
        try:
            registry_error = str(err_fn() or "")
        except Exception:  # noqa: BLE001 — best-effort
            registry_error = ""
    return McpCatalogResponse(
        entries=models, sources=sources, registry_error=registry_error
    )


def _counts(registry: object, name: str) -> tuple[int, int]:
    """Best-effort read of a server's (resource_count, prompt_count).

    The count accessors are additive on the concrete registry; a registry
    without them (test stub) reports zeros.
    """
    rc = getattr(registry, "resource_count", None)
    pc = getattr(registry, "prompt_count", None)
    r = rc(name) if callable(rc) else 0
    p = pc(name) if callable(pc) else 0
    return int(r or 0), int(p or 0)


# ---- Router factory -------------------------------------------------------


def build_router(*, container: "Container") -> APIRouter:
    """Build the MCP server management REST router bound to ``container``."""
    router = APIRouter(prefix="/api/chat/mcp", tags=["chat"])

    # Route every outbound OAuth HTTP call through the unified
    # Settings.ssl_verify toggle (same pattern as _chat_di.py / _channels_di.py
    # etc.). Reading the provider live at call time means a runtime SSL toggle
    # hot-applies to the NEXT OAuth flow without a restart. On the internal
    # edition (default ssl_verify=False) TLS is still relaxed for the corporate
    # MITM gateway; on external (default ssl_verify=True) certs are verified.
    from apps.api._global_proxy import build_ssl_verify_provider

    _ssl_verify_provider = build_ssl_verify_provider(container)

    def _use_case():
        uc = getattr(container.chat, "manage_mcp_servers_use_case", None)
        if uc is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="mcp registry not wired",
            )
        return uc

    def _enabled() -> bool:
        # The GLOBAL master switch truth source is the registry (persisted +
        # user-controllable), NOT the static Settings flag. Read it from the
        # registry when available; fall back to the Settings seed only when the
        # registry is unwired / predates the accessor (test stubs).
        registry = _registry()
        ge = getattr(registry, "global_enabled", None)
        if callable(ge):
            try:
                return bool(ge())
            except Exception:  # noqa: BLE001 — best-effort; fall back to seed
                pass
        chat_settings = getattr(container.settings, "chat", None)
        return bool(getattr(chat_settings, "chat_mcp_enabled", False))

    def _registry():
        return getattr(container.chat, "mcp_server_registry", None)

    async def _error_status_model(name: str, error: str) -> McpServerStatusModel:
        """A ``connected=False`` row for ``name`` that keeps its real config.

        The client upserts a status row into its server list by name, so a row
        invented from nothing silently rewrites the entry the user is looking
        at — a stdio server would start showing an ``http`` transport badge and
        an empty subtitle. Re-read the persisted config and override only the
        live-status fields; fall back to a bare row only when the config cannot
        be read at all (unwired registry, or the name really is unknown).
        """
        with contextlib.suppress(Exception):
            for st in await _use_case().list_servers():
                if st.config.name == name:
                    rc, pc = _counts(_registry(), name)
                    model = _status_to_model(st, resource_count=rc, prompt_count=pc)
                    return model.model_copy(
                        update={
                            "connected": False,
                            "tool_count": 0,
                            "tool_names": [],
                            "error": error,
                        }
                    )
        # No config to describe. Mirror the adapter's own not-found status
        # (``stdio`` + a placeholder command) rather than claiming ``http``,
        # which would assert a remote URL this server may never have had.
        return McpServerStatusModel(
            name=name,
            transport=McpTransport.STDIO.value,
            command="_",
            connected=False,
            error=error,
        )

    @router.get("/servers", response_model=McpServerListResponse)
    async def list_mcp_servers() -> McpServerListResponse:
        statuses = await _use_case().list_servers()
        registry = _registry()
        models = []
        for s in statuses:
            rc, pc = _counts(registry, s.config.name)
            models.append(_status_to_model(s, resource_count=rc, prompt_count=pc))
        return McpServerListResponse(servers=models, enabled=_enabled())

    @router.post("/servers", response_model=McpServerStatusModel)
    async def add_mcp_server(body: McpServerConfigModel) -> McpServerStatusModel:
        # Domain validation raises ValueError → mapped to 400 by the global
        # error middleware; catch here to return a clean 400 detail.
        try:
            config = body.to_domain()
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
        result = await _use_case().add_server(config)
        rc, pc = _counts(_registry(), result.config.name)
        return _status_to_model(result, resource_count=rc, prompt_count=pc)

    @router.delete(
        "/servers/{name}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
    )
    async def remove_mcp_server(name: str) -> None:
        removed = await _use_case().remove_server(name)
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"mcp server {name!r} not found",
            )
        return None

    @router.post("/servers/{name}/test", response_model=McpServerStatusModel)
    async def test_mcp_server(name: str) -> McpServerStatusModel:
        """Probe one server and report the result as a status row.

        A failed probe is a 200 with ``connected=False``, not an exception: the
        adapter already models "cannot connect" as a status, and letting an
        unexpected error escape leaves Starlette's middleware with no response
        to send (``RuntimeError('No response returned.')``) instead of a message
        the panel can show.
        """
        try:
            result = await _use_case().test_server(name)
        except Exception as exc:
            logger.warning("mcp test_server(%s) raised: %s", name, exc, exc_info=True)
            # The client upserts this row into its server list, so it must
            # describe the server as configured. Fabricating transport="http"
            # with no command/url relabels a stdio server in the UI and blanks
            # its subtitle; read the persisted config and only override the
            # live-status fields.
            return await _error_status_model(name, str(exc))
        rc, pc = _counts(_registry(), result.config.name)
        return _status_to_model(result, resource_count=rc, prompt_count=pc)

    @router.patch("/servers/{name}", response_model=McpServerStatusModel)
    async def set_mcp_server_enabled(
        name: str, body: McpSetEnabledRequest
    ) -> McpServerStatusModel:
        """Flip one server's per-server ``enabled`` switch (on→connect, off→drop)."""
        result = await _use_case().set_enabled(name, body.enabled)
        if not result.connected and result.error == "server not found":
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"mcp server {name!r} not found",
            )
        rc, pc = _counts(_registry(), result.config.name)
        return _status_to_model(result, resource_count=rc, prompt_count=pc)

    @router.patch("/enabled", response_model=McpServerListResponse)
    async def set_mcp_global_enabled(
        body: McpSetGlobalEnabledRequest,
    ) -> McpServerListResponse:
        """Flip the GLOBAL master switch (on→connect all, off→disconnect all).

        The switch truth source lives in the registry (persisted +
        user-controllable). Turning it ON (re)connects every per-server-enabled
        server; OFF disconnects them all (keeping the configs). Returns the
        updated server list so the UI reflects the new connection states in one
        round-trip.
        """
        uc = _use_case()
        setter = getattr(uc, "set_global_enabled", None)
        if callable(setter):
            await setter(body.enabled)
        statuses = await uc.list_servers()
        registry = _registry()
        models = []
        for s in statuses:
            rc, pc = _counts(registry, s.config.name)
            models.append(_status_to_model(s, resource_count=rc, prompt_count=pc))
        return McpServerListResponse(servers=models, enabled=_enabled())

    @router.get("/catalog/browse", response_model=McpCatalogBrowseResponse)
    async def browse_mcp_catalog(
        search: str | None = None,
        cursor: str | None = None,
        limit: int = 30,
        source_id: str | None = None,
    ) -> McpCatalogBrowseResponse:
        """Browse ONE page of a registry source (search + paginate).

        ``source_id`` selects the source: omit or ``"registry"`` for the
        built-in official registry; a custom uuid for a user-added source.
        User-driven network fetch: the search / "load more" action IS the
        consent. Graceful — a failed browse degrades to an empty page +
        ``registry_error`` banner (never 5xx).
        """
        uc = _use_case()
        browse = getattr(uc, "browse_registry", None)
        if not callable(browse):
            return McpCatalogBrowseResponse(entries=[], next_cursor=None)
        try:
            entries, next_cursor = await browse(
                search=search or None,
                cursor=cursor or None,
                limit=max(1, min(int(limit or 30), 100)),
                source_id=source_id or None,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat.mcp.browse_route_failed reason=%s", exc)
            return McpCatalogBrowseResponse(
                entries=[],
                next_cursor=None,
                registry_error=f"unexpected: {exc}",
            )
        registry_error = ""
        # Only surface the shared registry_source_error for the built-in
        # "registry" source — custom sources manage their own error path via
        # the browse_registry return value, so a stale shared error from a
        # previous request must not pollute a successful custom-source response.
        if not source_id or source_id in ("registry", "curated"):
            err_fn = getattr(_registry(), "registry_source_error", None)
            if callable(err_fn):
                try:
                    registry_error = str(err_fn() or "")
                except Exception:  # noqa: BLE001 — best-effort
                    registry_error = ""
        else:
            # For custom sources: if browse returned empty entries, read the
            # shared error as a best-effort degradation reason.
            if not entries:
                err_fn = getattr(_registry(), "registry_source_error", None)
                if callable(err_fn):
                    try:
                        registry_error = str(err_fn() or "")
                    except Exception:  # noqa: BLE001
                        registry_error = ""
        try:
            serialised = [_catalog_entry_to_model(e) for e in entries]
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat.mcp.browse_serialise_failed reason=%s", exc)
            return McpCatalogBrowseResponse(
                entries=[],
                next_cursor=None,
                registry_error=f"serialise_error: {exc}",
            )
        return McpCatalogBrowseResponse(
            entries=serialised,
            next_cursor=next_cursor,
            registry_error=registry_error,
        )

    @router.get("/catalog", response_model=McpCatalogResponse)
    async def get_mcp_catalog() -> McpCatalogResponse:
        """Return the marketplace catalog (curated + dynamic registry sources)."""
        entries = await _use_case().list_catalog()
        return _catalog_response(entries, _registry())

    @router.post("/catalog/refresh", response_model=McpCatalogResponse)
    async def refresh_mcp_catalog() -> McpCatalogResponse:
        """Fetch the dynamic official-registry source ON DEMAND + return catalog.

        Invoked by the panel's "load / refresh" action when the user picks the
        ``registry`` source — this is the ONLY endpoint that reaches the
        third-party registry over the network (the user's click is the consent).
        Graceful — a failed fetch degrades to curated + any prior cache (never
        5xx).
        """
        uc = _use_case()
        refresh = getattr(uc, "refresh_catalog", None)
        entries = await refresh() if callable(refresh) else await uc.list_catalog()
        return _catalog_response(entries, _registry())

    @router.post("/catalog/{entry_id}/install", response_model=McpServerStatusModel)
    async def install_from_catalog(
        entry_id: str, body: McpCatalogInstallRequest
    ) -> McpServerStatusModel:
        """Install one catalog entry (curated or registry — materialise + connect)."""
        from qai.chat.application.use_cases.manage_mcp_servers import (
            McpCatalogInstallError,
        )

        try:
            header_values = dict(body.header_values or {})
            # If the caller did not supply an Authorization header, try two fast
            # paths before falling through to the blocking connect():
            #
            # Fast-path 1: reuse a cached Bearer token from a prior install
            #   (SecretStore key: chat_mcp / <server_name>.Authorization).
            #   This avoids a full OAuth round-trip on re-install.
            #
            # Fast-path 2: if the entry's MCP endpoint requires OAuth but no
            #   cached token exists, return HTTP 401 mcp.oauth_required NOW so
            #   the frontend can run the OAuth flow first and then retry install
            #   with a Bearer token.  Without this the backend connect() would
            #   block waiting for a browser callback and hit the ~90 s Tauri
            #   fetch timeout.
            if not any(k.lower() == "authorization" for k in header_values):
                try:
                    _ss = getattr(_registry(), "_secret_store", None)
                    # The server name used as SecretStore key is entry_id
                    # (install_from_catalog sets name = entry_id by default).
                    _server_name = body.name or entry_id
                    if _ss is not None:
                        for _hk in ("Authorization", "authorization"):
                            try:
                                _v = _ss.get("chat_mcp", f"{_server_name}.{_hk}")
                                if _v and _v != "__secret__":
                                    header_values[_hk] = _v
                                    break
                            except Exception:
                                pass
                        # Fallback: scan all stored secrets for any valid Bearer token
                        # so a second entry on the same AS (e.g. Orbit after ODS) reuses
                        # the already-acquired token without re-running OAuth.
                        # SAFETY: only reuse a token from a server whose URL shares the
                        # same hostname as the entry we are installing, to prevent a
                        # token for server A from being injected into an unrelated server B.
                        if not any(k.lower() == "authorization" for k in header_values):
                            try:
                                # Resolve the target entry's URL for hostname matching.
                                from urllib.parse import urlparse as _urlparse
                                _target_host = ""
                                try:
                                    _entries_for_match = await _use_case().list_catalog()
                                    for _em in _entries_for_match:
                                        if getattr(_em, "id", None) == entry_id:
                                            _target_host = _urlparse(
                                                getattr(_em, "url", "") or ""
                                            ).netloc
                                            break
                                except Exception:
                                    pass
                                _all_keys = _ss.list_keys("chat_mcp")
                                for _k in _all_keys:
                                    if not (_k.endswith(".Authorization") or _k.endswith(".authorization")):
                                        continue
                                    # Check that the source server's URL has the same host.
                                    if _target_host:
                                        _src_server = _k.rsplit(".", 1)[0]
                                        _src_cfg = getattr(_registry(), "_servers", {}).get(_src_server)
                                        _src_url = getattr(_src_cfg, "url", "") or ""
                                        if _urlparse(_src_url).netloc != _target_host:
                                            continue
                                    try:
                                        _v = _ss.get("chat_mcp", _k)
                                        if _v and _v != "__secret__" and _v.startswith("Bearer "):
                                            header_values["Authorization"] = _v
                                            logger.info(
                                                "chat.mcp.install_token_reused_cross_entry entry=%s from_key=%s",
                                                entry_id, _k,
                                            )
                                            break
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                except Exception:
                    pass

            # Fast-path 2: still no token — probe the entry URL for a Bearer
            # OAuth challenge and fail fast so the frontend can do OAuth first.
            if not any(k.lower() == "authorization" for k in header_values):
                try:
                    _entries = await _use_case().list_catalog()
                    _entry_url = ""
                    for _e in _entries:
                        if getattr(_e, "id", None) == entry_id:
                            _entry_url = getattr(_e, "url", "") or ""
                            break
                    if _entry_url:
                        async with httpx.AsyncClient(
                            verify=_ssl_verify_provider(),
                            timeout=httpx.Timeout(8.0),
                        ) as _hc:
                            _probe = await _hc.post(
                                _entry_url,
                                json={"jsonrpc": "2.0", "id": 0,
                                      "method": "initialize",
                                      "params": {"protocolVersion": "2024-11-05",
                                                 "capabilities": {},
                                                 "clientInfo": {"name": "qai", "version": "0"}}},
                                headers={"Accept": "application/json, text/event-stream"},
                            )
                        if _probe.status_code == 401:
                            _ww = _probe.headers.get("www-authenticate", "")
                            if _ww.lower().startswith("bearer"):
                                logger.info(
                                    "chat.mcp.install_oauth_required entry=%s url=%s",
                                    entry_id, _entry_url,
                                )
                                return JSONResponse(
                                    status_code=status.HTTP_401_UNAUTHORIZED,
                                    content={
                                        "type": "UnauthorizedError",
                                        "code": "mcp.oauth_required",
                                        "message": (
                                            f"The MCP server {entry_id!r} requires OAuth "
                                            "authentication. Complete the browser login "
                                            "flow and retry with a Bearer token."
                                        ),
                                    },
                                )
                except Exception as _probe_exc:
                    logger.debug(
                        "chat.mcp.install_oauth_probe_error entry=%s: %s",
                        entry_id, _probe_exc,
                    )

            result = await _use_case().install_from_catalog(
                entry_id,
                name=body.name or None,
                arg_values=dict(body.arg_values or {}),
                env_values=dict(body.env_values or {}),
                header_values=header_values,
                source=body.source or None,
            )
        except McpCatalogInstallError as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "type": "ValidationError",
                    "code": "mcp.catalog_install_error",
                    "message": str(exc),
                },
            )
        rc, pc = _counts(_registry(), result.config.name)
        return _status_to_model(result, resource_count=rc, prompt_count=pc)

    @router.get(
        "/servers/{name}/resources", response_model=McpResourceListResponse
    )
    async def list_mcp_resources(name: str) -> McpResourceListResponse:
        """List one server's resources (only a CONNECTED server yields data).

        The registry's ``list_resources`` aggregates ALL connected servers;
        the route filters to the requested ``name`` so the front-end can show a
        per-server list. A disabled registry / un-connected server yields an
        empty list (never a resource of an un-enabled/unreachable server).
        """
        registry = _registry()
        if registry is None or not hasattr(registry, "list_resources"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="mcp registry not wired",
            )
        all_resources = await registry.list_resources()
        return McpResourceListResponse(
            resources=[
                McpResourceModel(
                    server_name=r.server_name,
                    uri=r.uri,
                    name=r.name,
                    mime_type=r.mime_type,
                )
                for r in all_resources
                if r.server_name == name
            ]
        )

    @router.get("/servers/{name}/prompts", response_model=McpPromptListResponse)
    async def list_mcp_prompts(name: str) -> McpPromptListResponse:
        """List one server's prompts (only a CONNECTED server yields data)."""
        registry = _registry()
        if registry is None or not hasattr(registry, "list_prompts"):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="mcp registry not wired",
            )
        all_prompts = await registry.list_prompts()
        return McpPromptListResponse(
            prompts=[
                McpPromptModel(
                    server_name=p.server_name,
                    name=p.name,
                    description=p.description,
                    arguments=[
                        McpPromptArgumentModel(
                            name=a.name,
                            description=a.description,
                            required=a.required,
                        )
                        for a in p.arguments
                    ],
                )
                for p in all_prompts
                if p.server_name == name
            ]
        )

    # ── Registry source management routes ────────────────────────────────────

    @router.get("/catalog/sources", response_model=list[McpRegistrySourceModel])
    async def list_catalog_sources() -> list[McpRegistrySourceModel]:
        """Return all registry sources: the built-in official registry plus any
        user-added custom sources."""
        registry = _registry()
        fn = getattr(registry, "list_registry_sources", None)
        if not callable(fn):
            return [McpRegistrySourceModel(
                id="registry", name="Online",
                url="https://registry.modelcontextprotocol.io", builtin=True
            )]
        return [McpRegistrySourceModel(**s) for s in fn()]

    @router.post(
        "/catalog/sources",
        response_model=McpRegistrySourceModel,
        status_code=status.HTTP_201_CREATED,
    )
    async def add_catalog_source(body: McpAddRegistrySourceRequest) -> McpRegistrySourceModel:
        """Add a custom registry source. Persists immediately."""
        registry = _registry()
        fn = getattr(registry, "add_registry_source", None)
        if not callable(fn):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="registry source management not available",
            )
        result = await fn(body.url, body.name or "", body.token or "", body.cookie or "")
        return McpRegistrySourceModel(**result)

    @router.delete(
        "/catalog/sources/{source_id}",
        status_code=status.HTTP_204_NO_CONTENT,
        response_model=None,
    )
    async def delete_catalog_source(source_id: str) -> None:
        """Delete a custom registry source by id. Built-in sources cannot be deleted."""
        registry = _registry()
        fn = getattr(registry, "remove_registry_source", None)
        if not callable(fn):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="registry source management not available",
            )
        removed = await fn(source_id)
        if not removed:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"registry source {source_id!r} not found",
            )
        return None

    # ── Registry source OAuth routes ─────────────────────────────────────────

    # Internal-edition preset source URLs are loaded lazily from
    # internal_config.toml so that no internal domain literals appear in
    # external artifacts (edition dual-form pattern §7).
    def _preset_source_urls() -> dict[str, str]:
        try:
            from qai.platform.edition.loader import get_mcp_hub_urls
            h = get_mcp_hub_urls()
            out: dict[str, str] = {}
            if h.get("qgenie_mcphub_url"):
                out["custom:qgenie-mcphub"] = h["qgenie_mcphub_url"]
            if h.get("ceflow_mcphub_url"):
                out["custom:ceflow-mcphub"] = h["ceflow_mcphub_url"]
            return out
        except Exception:
            return {}

    @router.post(
        "/catalog/sources/{source_id}/oauth/start",
        response_model=McpOAuthStartResponse,
    )
    async def source_oauth_start(
        source_id: str,
        body: McpOAuthStartRequest = McpOAuthStartRequest(),
    ) -> McpOAuthStartResponse:
        """Start an OAuth 2.1 PKCE flow for a registry source that returns 401.

        Discovers the authorization server, dynamically registers a client,
        builds the PKCE auth URL, opens the system browser, and starts a local
        callback server.  The access token is written to the registry's
        SecretStore on callback.  Poll ``oauth/token?session_id=...`` for status.
        """
        import traceback as _tb

        try:
            # Fast path: reuse a cached token from a previous authentication.
            # Key: chat_mcp / registry.<raw_id>.token (written on callback).
            try:
                _ss = getattr(_registry(), "_secret_store", None)
                if _ss is not None:
                    raw_id = source_id.removeprefix("custom:")
                    _cached = None
                    for _k in (f"registry.{raw_id}.token",):
                        try:
                            _v = _ss.get("chat_mcp", _k)
                            if _v and _v != "__secret__":
                                _cached = _v
                                break
                        except Exception:
                            pass
                    if _cached:
                        logger.info("chat.mcp.source_oauth_token_reused source=%s", source_id)
                        _session_id = secrets.token_urlsafe(24)
                        _OAUTH_SESSIONS[_session_id] = {
                            "code_verifier": "",
                            "token_endpoint": "",
                            "client_id": "",
                            "redirect_uri": "",
                            "source_id": source_id,
                            "done": True,
                            "access_token": _cached,
                            "error": "",
                        }
                        return McpOAuthStartResponse(auth_url="", session_id=_session_id)
            except Exception as _fast_exc:
                logger.debug("chat.mcp.source_oauth_fast_path_error source=%s: %s", source_id, _fast_exc)

            # Resolve the source URL synchronously (no network, just config lookup).
            source_url = _preset_source_urls().get(source_id, "")
            if not source_url:
                registry = _registry()
                sources_fn = getattr(registry, "list_registry_sources", None)
                if callable(sources_fn):
                    for src in sources_fn():
                        if src.get("id") == source_id:
                            source_url = src.get("url", "")
                            break
            if not source_url:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"registry source {source_id!r} not found",
                )

            # Create the session immediately and return — the caller sees
            # session_id right away and can start polling.  All network work
            # (discovery, registration, browser open) runs in a daemon thread
            # so the HTTP response is not delayed by those roundtrips.
            session_id = secrets.token_urlsafe(24)
            session: dict[str, Any] = {
                "code_verifier": "",
                "token_endpoint": "",
                "client_id": "",
                "redirect_uri": "",
                "source_id": source_id,
                "done": False,
                "access_token": "",
                "error": "",
                "theme": dict(body.theme) if body.theme else {},
            }
            _OAUTH_SESSIONS[session_id] = session

            def _persist_source_token(token_val: str) -> None:
                try:
                    registry = _registry()
                    ss = getattr(registry, "_secret_store", None)
                    if ss is not None and token_val:
                        raw_id = session["source_id"].removeprefix("custom:")
                        ss.set("chat_mcp", f"registry.{raw_id}.token", token_val)
                except Exception as _pe:
                    logger.warning("chat.mcp.source_oauth_persist_error: %s", _pe)

            def _source_oauth_background(
                _source_url: str,
                _session: dict[str, Any],
                _sid: str,
                _persist_fn: Any,
            ) -> None:
                import urllib.request as _ureq
                import json as _json2
                import ssl as _ssl2

                def _get_json_sync(url: str) -> dict[str, Any]:
                    try:
                        ctx = _relaxed_ssl_context(verify=_ssl_verify_provider())
                        with _ureq.urlopen(url, context=ctx, timeout=10) as r:
                            return _json2.loads(r.read())
                    except Exception:
                        return {}

                try:
                    # Probe for WWW-Authenticate header.
                    www_auth = ""
                    probe_status = 0
                    try:
                        ctx2 = _relaxed_ssl_context(verify=_ssl_verify_provider())
                        req = _ureq.Request(_source_url)
                        try:
                            with _ureq.urlopen(req, context=ctx2, timeout=10) as r:
                                probe_status = r.status
                                www_auth = r.headers.get("www-authenticate", "")
                        except Exception as _probe_http_exc:
                            # urllib raises HTTPError for 4xx/5xx — read headers from it.
                            import urllib.error as _uerr
                            if isinstance(_probe_http_exc, _uerr.HTTPError):
                                probe_status = _probe_http_exc.code
                                www_auth = _probe_http_exc.headers.get("www-authenticate", "")
                    except Exception as _probe_exc:
                        logger.warning("chat.mcp.source_oauth_probe_error source=%s: %s", _sid, _probe_exc)

                    as_meta: dict[str, Any] = {}
                    rm2 = re.search(r'resource_metadata=["\']?([^"\',\s>]+)["\']?', www_auth)
                    if rm2:
                        resource_meta = _get_json_sync(rm2.group(1).strip('"\''))
                        auth_servers = resource_meta.get("authorization_servers", [])
                        if isinstance(auth_servers, list) and auth_servers:
                            as_meta = _get_json_sync(
                                f"{auth_servers[0].rstrip('/')}/.well-known/oauth-authorization-server"
                            )

                    parsed2 = urllib.parse.urlparse(_source_url)
                    if not as_meta.get("authorization_endpoint"):
                        as_meta = _get_json_sync(
                            f"{parsed2.scheme}://{parsed2.netloc}"
                            f"/.well-known/oauth-authorization-server{parsed2.path}"
                        )
                    if not as_meta.get("authorization_endpoint"):
                        as_meta = _get_json_sync(
                            f"{parsed2.scheme}://{parsed2.netloc}/.well-known/oauth-authorization-server"
                        )

                    authorization_endpoint2: str = as_meta.get("authorization_endpoint", "")
                    token_endpoint2: str = as_meta.get("token_endpoint", "")
                    registration_endpoint2: str = as_meta.get("registration_endpoint", "")

                    if not authorization_endpoint2:
                        _session["error"] = (
                            f"OAuth metadata discovery failed for {_source_url!r}. "
                            f"probe_status={probe_status}. Ensure VPN/network access."
                        )
                        _session["done"] = True
                        return

                    # PKCE.
                    code_verifier2 = secrets.token_urlsafe(64)
                    code_challenge2 = (
                        base64.urlsafe_b64encode(
                            hashlib.sha256(code_verifier2.encode()).digest()
                        ).rstrip(b"=").decode()
                    )

                    # Local callback server on a random port.
                    import socket as _sock2
                    with _sock2.socket() as _s2:
                        _s2.bind((LOOPBACK_HOST, 0))
                        callback_port2 = _s2.getsockname()[1]
                    redirect_uri2 = f"http://{LOOPBACK_HOST_NAME}:{callback_port2}/callback"

                    # Dynamic client registration.
                    client_id2: str = ""
                    registered_scope2: str = ""
                    if registration_endpoint2:
                        try:
                            post_data2 = _json2.dumps({
                                "redirect_uris": [redirect_uri2],
                                "client_name": "QAIModelBuilder",
                                "grant_types": ["authorization_code"],
                                "response_types": ["code"],
                                "token_endpoint_auth_method": "none",
                            }).encode()
                            reg_req = _ureq.Request(
                                registration_endpoint2,
                                data=post_data2,
                                headers={"Content-Type": "application/json"},
                            )
                            ctx3 = _relaxed_ssl_context(verify=_ssl_verify_provider())
                            with _ureq.urlopen(reg_req, context=ctx3, timeout=10) as rr:
                                reg_json2 = _json2.loads(rr.read())
                                client_id2 = reg_json2.get("client_id", "")
                                registered_scope2 = reg_json2.get("scope", "")
                        except Exception as _reg_exc:
                            logger.warning("chat.mcp.source_oauth_dyn_reg_error source=%s: %s", _sid, _reg_exc)

                    if not client_id2:
                        _session["error"] = (
                            f"Could not obtain OAuth client_id for {_source_url!r}. "
                            f"registration_endpoint={registration_endpoint2!r}"
                        )
                        _session["done"] = True
                        return

                    scopes2 = as_meta.get("scopes_supported")
                    if registered_scope2:
                        scope2 = registered_scope2
                    elif isinstance(scopes2, list) and scopes2:
                        scope2 = " ".join(scopes2)
                    else:
                        scope2 = "openid profile"

                    state2 = secrets.token_urlsafe(16)
                    auth_url2 = authorization_endpoint2 + "?" + urllib.parse.urlencode({
                        "response_type": "code",
                        "client_id": client_id2,
                        "redirect_uri": redirect_uri2,
                        "code_challenge": code_challenge2,
                        "code_challenge_method": "S256",
                        "scope": scope2,
                        "state": state2,
                    })

                    # Fill in session fields now that we have all the details.
                    _session["code_verifier"] = code_verifier2
                    _session["token_endpoint"] = token_endpoint2
                    _session["client_id"] = client_id2
                    _session["redirect_uri"] = redirect_uri2

                    _start_oauth_callback_thread(
                        _session,
                        callback_port2,
                        on_token_saved=_persist_fn,
                        ssl_verify=_ssl_verify_provider(),
                    )
                    _oauth_open_browser(auth_url2, _session, f"source:{_sid}")

                except Exception as _bg_exc:
                    logger.warning("chat.mcp.source_oauth_bg_error source=%s: %s", _sid, _bg_exc)
                    _session["error"] = f"oauth_setup_error: {_bg_exc}"
                    _session["done"] = True

            threading.Thread(
                target=_source_oauth_background,
                args=(source_url, session, source_id, _persist_source_token),
                daemon=True,
                name=f"mcp-source-oauth-{source_id}",
            ).start()

            return McpOAuthStartResponse(auth_url="", session_id=session_id)

        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("chat.mcp.source_oauth_start_unhandled source=%s", source_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"OAuth start error ({type(exc).__name__}: {exc})",
            ) from exc

    @router.get(
        "/catalog/sources/{source_id}/oauth/token",
        response_model=McpOAuthTokenResponse,
    )
    async def source_oauth_token(source_id: str, session_id: str) -> McpOAuthTokenResponse:
        """Poll for the registry source OAuth token.  Returns ``status="pending"`` until done."""
        session = _OAUTH_SESSIONS.get(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"OAuth session {session_id!r} not found",
            )
        if not session["done"]:
            return McpOAuthTokenResponse(status="pending")
        if session["error"]:
            _OAUTH_SESSIONS.pop(session_id, None)
            return McpOAuthTokenResponse(status="error", error=session["error"])
        token = session["access_token"]
        _OAUTH_SESSIONS.pop(session_id, None)
        return McpOAuthTokenResponse(status="done", access_token=token)

    # ── OAuth 2.1 + PKCE routes ───────────────────────────────────────────────

    @router.get("/catalog/{entry_id}/oauth/diagnose")
    async def oauth_diagnose(entry_id: str) -> dict[str, Any]:
        """Diagnostic: walk the OAuth discovery chain for an entry and return all raw data."""
        try:
            entries = await _use_case().list_catalog()
        except Exception:
            entries = []
        entry_url = ""
        for e in entries:
            if getattr(e, "id", None) == entry_id:
                entry_url = getattr(e, "url", "") or ""
                break
        if not entry_url:
            raise HTTPException(status_code=404, detail=f"entry {entry_id!r} not found")

        result: dict[str, Any] = {"entry_url": entry_url, "steps": []}

        async def _fetch(url: str, label: str) -> dict[str, Any]:
            step: dict[str, Any] = {"label": label, "url": url}
            try:
                async with httpx.AsyncClient(verify=_ssl_verify_provider(), timeout=httpx.Timeout(10.0)) as hc:
                    r = await hc.get(url)
                    step["status"] = r.status_code
                    step["headers"] = dict(r.headers)
                    try:
                        step["body"] = r.json()
                    except Exception:
                        step["body_text"] = r.text[:500]
            except Exception as exc:
                step["error"] = str(exc)
            result["steps"].append(step)
            return step.get("body") or {}

        # Step 1: probe MCP endpoint
        probe_step: dict[str, Any] = {"label": "probe_mcp", "url": entry_url}
        try:
            async with httpx.AsyncClient(verify=_ssl_verify_provider(), timeout=httpx.Timeout(10.0)) as hc:
                probe = await hc.get(entry_url)
                probe_step["status"] = probe.status_code
                probe_step["www_authenticate"] = probe.headers.get("www-authenticate", "")
                try:
                    probe_step["body"] = probe.json()
                except Exception:
                    probe_step["body_text"] = probe.text[:500]
        except Exception as exc:
            probe_step["error"] = str(exc)
        result["steps"].append(probe_step)
        www_auth: str = probe_step.get("www_authenticate", "")

        # Step 2: resource_metadata URL from WWW-Authenticate
        rm = re.search(r'resource_metadata=["\']?([^"\',\s]+)["\']?', www_auth)
        resource_meta: dict[str, Any] = {}
        if rm:
            resource_meta = await _fetch(rm.group(1), "resource_metadata")

        # Step 3: authorization_servers from resource metadata
        auth_servers = resource_meta.get("authorization_servers", [])
        if isinstance(auth_servers, list) and auth_servers:
            as_url = auth_servers[0]
            as_meta = await _fetch(as_url, "authorization_server_meta_direct")
            if not as_meta.get("authorization_endpoint"):
                # Try RFC 8414 well-known path
                parsed = urllib.parse.urlparse(as_url)
                wk_url = f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server{parsed.path}"
                await _fetch(wk_url, "authorization_server_meta_wellknown")

        # Step 4: RFC 8414 fallbacks
        parsed = urllib.parse.urlparse(entry_url)
        await _fetch(
            f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server{parsed.path}",
            "fallback_path_scoped"
        )
        await _fetch(
            f"{parsed.scheme}://{parsed.netloc}/.well-known/oauth-authorization-server",
            "fallback_origin"
        )

        return result

    @router.post(
        "/catalog/{entry_id}/oauth/start",
        response_model=McpOAuthStartResponse,
    )
    async def oauth_start(
        entry_id: str,
        body: McpOAuthStartRequest = McpOAuthStartRequest(),
    ) -> McpOAuthStartResponse:  # noqa: PLR0912,PLR0915
        import traceback as _tb

        try:
            # 0. Fast path: reuse cached Authorization token from a previous install.
            #    Key: chat_mcp / <entry_id>.Authorization (same as _externalise_headers).
            #    We return the token optimistically — if it's expired the install will
            #    fail and the user can reinstall (which will re-run the full OAuth).
            #
            #    Fallback: if no token for this specific entry exists yet, scan ALL
            #    chat_mcp secrets for any Bearer token from a prior install of a
            #    different entry on the same AS (e.g. ODS and Orbit share the same
            #    qswat-api.qualcomm.com auth server — no need to re-run OAuth).
            try:
                _ss = getattr(_registry(), "_secret_store", None)
                if _ss is not None:
                    _cached_token = None
                    # Primary: exact match for this entry.
                    for _hk in ("Authorization", "authorization"):
                        try:
                            _v = _ss.get("chat_mcp", f"{entry_id}.{_hk}")
                            if _v and _v != "__secret__" and _v.startswith("Bearer "):
                                _cached_token = _v[len("Bearer "):]
                                break
                        except Exception:
                            pass
                    # Fallback: scan all stored secrets for any valid Bearer token
                    # (covers the case where another entry on the same AS was
                    # installed first — avoids repeating probe + discovery + headless
                    # SSO for every additional entry sharing the same auth server).
                    # SAFETY: only reuse from a server with the same URL hostname.
                    if not _cached_token:
                        try:
                            from urllib.parse import urlparse as _urlparse
                            _target_host = ""
                            try:
                                _entries_for_match = await _use_case().list_catalog()
                                for _em in _entries_for_match:
                                    if getattr(_em, "id", None) == entry_id:
                                        _target_host = _urlparse(
                                            getattr(_em, "url", "") or ""
                                        ).netloc
                                        break
                            except Exception:
                                pass
                            _all_keys = _ss.list_keys("chat_mcp")
                            for _k in _all_keys:
                                if not (_k.endswith(".Authorization") or _k.endswith(".authorization")):
                                    continue
                                if _target_host:
                                    _src_server = _k.rsplit(".", 1)[0]
                                    _src_cfg = getattr(_registry(), "_servers", {}).get(_src_server)
                                    _src_url = getattr(_src_cfg, "url", "") or ""
                                    if _urlparse(_src_url).netloc != _target_host:
                                        continue
                                try:
                                    _v = _ss.get("chat_mcp", _k)
                                    if _v and _v != "__secret__" and _v.startswith("Bearer "):
                                        _cached_token = _v[len("Bearer "):]
                                        logger.info(
                                            "chat.mcp.oauth_token_reused_cross_entry entry=%s from_key=%s",
                                            entry_id, _k,
                                        )
                                        break
                                except Exception:
                                    pass
                        except Exception:
                            pass
                    if _cached_token:
                        logger.info("chat.mcp.oauth_token_reused entry=%s", entry_id)
                        _session_id = secrets.token_urlsafe(24)
                        _OAUTH_SESSIONS[_session_id] = {
                            "code_verifier": "",
                            "token_endpoint": "",
                            "client_id": "",
                            "redirect_uri": "",
                            "done": True,
                            "access_token": _cached_token,
                            "error": "",
                        }
                        return McpOAuthStartResponse(
                            auth_url="", session_id=_session_id
                        )
            except Exception as _fast_exc:
                logger.debug("chat.mcp.oauth_fast_path_error entry=%s: %s", entry_id, _fast_exc)

            # 1. Look up the catalog entry URL.
            try:
                entries = await _use_case().list_catalog()
            except Exception:
                entries = []
            entry_url = ""
            for e in entries:
                if getattr(e, "id", None) == entry_id:
                    entry_url = getattr(e, "url", "") or ""
                    break
            if not entry_url:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"catalog entry {entry_id!r} not found or has no URL",
                )

            # 1b. In-flight dedup: if another oauth/start for the same hub host
            #     is already running, return its session_id so the caller can
            #     poll it instead of starting a parallel OAuth flow.
            _hub_host = urllib.parse.urlparse(entry_url).netloc
            if _hub_host and _hub_host in _OAUTH_INFLIGHT:
                _existing_sid = _OAUTH_INFLIGHT[_hub_host]
                if _existing_sid in _OAUTH_SESSIONS:
                    logger.info(
                        "chat.mcp.oauth_inflight_reused entry=%s hub=%s session=%s",
                        entry_id, _hub_host, _existing_sid,
                    )
                    return McpOAuthStartResponse(auth_url="", session_id=_existing_sid)
                else:
                    del _OAUTH_INFLIGHT[_hub_host]

            # 2. Probe MCP endpoint for WWW-Authenticate header (no SSL verify —
            #    Qualcomm corporate MITM CA; same posture as all other MCP clients).
            www_auth = ""
            probe_status = 0
            try:
                async with httpx.AsyncClient(verify=_ssl_verify_provider(), timeout=httpx.Timeout(10.0)) as hc:
                    probe = await hc.get(entry_url)
                    probe_status = probe.status_code
                    www_auth = probe.headers.get("www-authenticate", "")
                    logger.info(
                        "chat.mcp.oauth_probe entry=%s status=%s www_auth=%r",
                        entry_id, probe_status, www_auth,
                    )
            except Exception as exc:
                logger.warning("chat.mcp.oauth_probe_error entry=%s: %s", entry_id, exc)

            # 3. Discover OAuth metadata via two-step RFC 9728 → RFC 8414 chain.
            async def _get_json(url: str) -> dict[str, Any]:
                try:
                    async with httpx.AsyncClient(
                        verify=_ssl_verify_provider(),
                        timeout=httpx.Timeout(10.0),
                        follow_redirects=True,
                    ) as hc:
                        r = await hc.get(url)
                        if r.status_code == 200:
                            return r.json()
                except Exception:
                    pass
                return {}

            as_meta: dict[str, Any] = {}

            # Step A: resource_metadata URL → RFC 9728 document → authorization_servers list
            rm = re.search(r'resource_metadata=["\']?([^"\',\s>]+)["\']?', www_auth)
            if rm:
                resource_meta = await _get_json(rm.group(1).strip('"\''))
                auth_servers = resource_meta.get("authorization_servers", [])
                if isinstance(auth_servers, list) and auth_servers:
                    as_origin = auth_servers[0].rstrip("/")
                    # Step B: RFC 8414 well-known at that AS origin
                    as_meta = await _get_json(
                        f"{as_origin}/.well-known/oauth-authorization-server"
                    )

            # Fallback A: RFC 8414 path-scoped at entry origin
            if not as_meta.get("authorization_endpoint"):
                parsed = urllib.parse.urlparse(entry_url)
                as_meta = await _get_json(
                    f"{parsed.scheme}://{parsed.netloc}"
                    f"/.well-known/oauth-authorization-server{parsed.path}"
                )
            # Fallback B: RFC 8414 at entry origin (no path)
            if not as_meta.get("authorization_endpoint"):
                parsed = urllib.parse.urlparse(entry_url)
                as_meta = await _get_json(
                    f"{parsed.scheme}://{parsed.netloc}"
                    "/.well-known/oauth-authorization-server"
                )

            authorization_endpoint: str = as_meta.get("authorization_endpoint", "")
            token_endpoint: str = as_meta.get("token_endpoint", "")
            registration_endpoint: str = as_meta.get("registration_endpoint", "")

            if not authorization_endpoint:
                diag = f"probe_status={probe_status}, www_auth={www_auth!r}"
                logger.warning("chat.mcp.oauth_discovery_failed entry=%s %s", entry_id, diag)
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        f"OAuth metadata discovery failed for {entry_url!r}. "
                        f"Diagnostics: {diag}. "
                        "Ensure you are on the Qualcomm corporate network (VPN)."
                    ),
                )

            # 4. PKCE.
            code_verifier = secrets.token_urlsafe(64)
            code_challenge = (
                base64.urlsafe_b64encode(
                    hashlib.sha256(code_verifier.encode()).digest()
                ).rstrip(b"=").decode()
            )

            # 5. RFC 7591 Dynamic Client Registration.
            #    Use a random high port for the local callback server to avoid
            #    the port-80 permission issue on Windows (requires admin).
            import socket as _socket
            with _socket.socket() as _s:
                _s.bind((LOOPBACK_HOST, 0))
                callback_port = _s.getsockname()[1]
            redirect_uri = f"http://{LOOPBACK_HOST_NAME}:{callback_port}/callback"

            client_id: str = ""
            registered_scope: str = ""
            if registration_endpoint:
                try:
                    async with httpx.AsyncClient(
                        verify=_ssl_verify_provider(),
                        timeout=httpx.Timeout(10.0),
                        follow_redirects=True,
                    ) as hc:
                        reg_resp = await hc.post(
                            registration_endpoint,
                            json={
                                "redirect_uris": [redirect_uri],
                                "client_name": "QAIModelBuilder",
                                "grant_types": ["authorization_code"],
                                "response_types": ["code"],
                                "token_endpoint_auth_method": "none",
                            },
                        )
                    logger.info(
                        "chat.mcp.oauth_dyn_reg_resp entry=%s status=%s body=%s",
                        entry_id, reg_resp.status_code, reg_resp.text[:400],
                    )
                    if reg_resp.status_code in (200, 201):
                        reg_json = reg_resp.json()
                        client_id = reg_json.get("client_id", "")
                        # Use the scope the server actually granted (may be narrower
                        # than scopes_supported — e.g. qgenie only grants "mcp").
                        registered_scope = reg_json.get("scope", "")
                        logger.info(
                            "chat.mcp.oauth_dyn_reg entry=%s client_id=%s redirect_uri=%s",
                            entry_id, client_id, redirect_uri,
                        )
                    else:
                        logger.warning(
                            "chat.mcp.oauth_dyn_reg_failed entry=%s status=%s body=%s",
                            entry_id, reg_resp.status_code, reg_resp.text[:200],
                        )
                except Exception as reg_exc:
                    logger.warning(
                        "chat.mcp.oauth_dyn_reg_error entry=%s: %s", entry_id, reg_exc
                    )

            if not client_id:
                raise HTTPException(
                    status_code=status.HTTP_502_BAD_GATEWAY,
                    detail=(
                        f"Could not obtain OAuth client_id for {entry_url!r}. "
                        "Dynamic client registration failed or is not supported. "
                        f"registration_endpoint={registration_endpoint!r}"
                    ),
                )

            scopes = as_meta.get("scopes_supported")
            # Use the scope actually granted by registration (narrower than
            # scopes_supported). Fall back to "mcp" for qgenie-style hubs,
            # then to the full supported list.
            if registered_scope:
                scope = registered_scope
            elif isinstance(scopes, list) and scopes:
                scope = " ".join(scopes)
            else:
                scope = "openid profile"

            state = secrets.token_urlsafe(16)
            auth_url = authorization_endpoint + "?" + urllib.parse.urlencode({
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
                "scope": scope,
                "state": state,
            })

            session_id = secrets.token_urlsafe(24)
            session: dict[str, Any] = {
                "code_verifier": code_verifier,
                "token_endpoint": token_endpoint,
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "done": False,
                "access_token": "",
                "error": "",
                "theme": dict(body.theme) if body.theme else {},
            }
            _OAUTH_SESSIONS[session_id] = session
            # Register this hub as in-flight so concurrent oauth/start calls
            # for other entries on the same hub can reuse this session.
            if _hub_host:
                _OAUTH_INFLIGHT[_hub_host] = session_id

            # 6. One-shot local callback HTTP server running in a daemon thread.
            # Using threading instead of asyncio.start_server avoids
            # asyncio.Server.close() interfering with anyio cancel scopes.
            _start_oauth_callback_thread(
                session, callback_port, ssl_verify=_ssl_verify_provider()
            )

            # Open the browser for the user to authenticate; the callback thread
            # above captures the token when the redirect lands.
            _oauth_open_browser(auth_url, session, f"entry:{entry_id}")

            return McpOAuthStartResponse(auth_url=auth_url, session_id=session_id)

        except HTTPException:
            raise
        except Exception as exc:
            tb = _tb.format_exc()
            logger.exception("chat.mcp.oauth_start_unhandled entry=%s", entry_id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=f"OAuth start error ({type(exc).__name__}: {exc})\n{tb}",
            ) from exc

    @router.get(
        "/catalog/{entry_id}/oauth/token",
        response_model=McpOAuthTokenResponse,
    )
    async def oauth_token(
        entry_id: str, session_id: str
    ) -> McpOAuthTokenResponse:
        """Poll for the OAuth token.  Returns ``status="pending"`` until done."""
        session = _OAUTH_SESSIONS.get(session_id)
        if session is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"OAuth session {session_id!r} not found",
            )
        if not session["done"]:
            return McpOAuthTokenResponse(status="pending")
        if session["error"]:
            _OAUTH_SESSIONS.pop(session_id, None)
            # Clean up in-flight registry for this hub.
            try:
                _h = urllib.parse.urlparse(session.get("token_endpoint", "")).netloc
                if _h and _OAUTH_INFLIGHT.get(_h) == session_id:
                    del _OAUTH_INFLIGHT[_h]
            except Exception:
                pass
            return McpOAuthTokenResponse(status="error", error=session["error"])
        token = session["access_token"]
        _OAUTH_SESSIONS.pop(session_id, None)
        # Clean up in-flight registry for this hub.
        try:
            _h = urllib.parse.urlparse(session.get("token_endpoint", "")).netloc
            if _h and _OAUTH_INFLIGHT.get(_h) == session_id:
                del _OAUTH_INFLIGHT[_h]
        except Exception:
            pass
        return McpOAuthTokenResponse(status="done", access_token=token)

    return router


__all__ = ["build_router"]
