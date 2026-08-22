# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Official MCP registry dynamic-source client (marketplace phase 2).

Fetches the *installable server list* from the official Model Context Protocol
registry (``https://registry.modelcontextprotocol.io``) over HTTP and maps each
listing into a :class:`qai.chat.domain.mcp_catalog.CuratedCatalogEntry` tagged
``source="registry"`` so it can be aggregated alongside the phase-1 static
``curated`` source WITHOUT a schema change.

Why a minimal client (no new dependency)
----------------------------------------
Like :mod:`qai.chat.infrastructure.mcp_client`, this uses the already-core
dependency ``httpx`` rather than pulling an official registry SDK — keeping the
cross-platform / no-new-dependency posture (AGENTS.md §8).  It speaks the
registry's public HTTP/JSON convention (``GET /v0/servers`` → ``{servers: [...],
metadata: {nextCursor, count}}``), mapping only the fields we need and
defaulting / skipping anything uncertain (better an installable subset than a
broken entry).

Registry listing shape (v0, schema 2025-12-11), the parts we map::

    {
      "servers": [
        {
          "server": {
            "name": "io.github.owner/pkg",     # reverse-DNS unique id
            "description": "...",
            "title": "Human Name",              # optional
            "version": "1.2.3",
            "websiteUrl": "https://...",        # optional
            "repository": {"url": "..."},       # optional
            "packages": [                        # local (stdio) install
              {
                "registryType": "npm"|"pypi",
                "identifier": "@scope/pkg" | "pkg",
                "transport": {"type": "stdio"},
                "environmentVariables": [
                  {"name": "API_KEY", "isRequired": true, "isSecret": true}
                ]
              }
            ],
            "remotes": [                         # remote (sse/http) install
              {
                "type": "streamable-http"|"sse",
                "url": "https://.../mcp",
                "headers": [
                  {"name": "Authorization", "isRequired": true,
                   "isSecret": true}
                ]
              }
            ]
          },
          "_meta": {"io.modelcontextprotocol.registry/official":
                    {"status": "active", "isLatest": true}}
        }
      ],
      "metadata": {"nextCursor": "...", "count": 30}
    }

Mapping policy
--------------
* Only ``status == "active"`` AND ``isLatest`` listings are surfaced (skip
  deprecated / superseded versions — the marketplace should not offer them).
* Prefer a ``packages`` (local stdio) install when present (``npm`` → ``npx``,
  ``pypi`` → ``uvx``); else fall back to the first ``remotes`` entry
  (``streamable-http`` → ``http`` transport, ``sse`` → ``sse``).
* A listing with neither a mappable package nor a mappable remote is skipped.
* Declared required env vars / headers become the entry's
  ``env_required`` / ``headers_required``; ``isSecret`` fields go into
  ``secret_fields`` (rendered as password inputs, stored via SecretStore).

Cross-context isolation
-----------------------
Imports only ``qai.chat.domain`` + stdlib + ``httpx``.  No imports of other
bounded contexts (``context-isolation`` contract).  ``httpx`` is legitimate in
this ``infrastructure`` layer, never in ``domain``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from typing import Any

import httpx

from qai.chat.domain.mcp_catalog import CuratedCatalogEntry

logger = logging.getLogger("qai.chat.mcp_registry_source")

__all__ = [
    "DEFAULT_REGISTRY_BASE_URL",
    "McpRegistrySourceError",
    "fetch_custom_source_page",
    "fetch_registry_entries",
    "fetch_registry_page",
    "map_listing_to_entry",
]

#: The official MCP registry base URL. Overridable at the call site (tests point
#: it at a fake). No trailing slash.
DEFAULT_REGISTRY_BASE_URL: str = "https://registry.modelcontextprotocol.io"

#: The listings endpoint (relative to the base URL).
_SERVERS_PATH: str = "/v0/servers"

#: Default per-fetch network timeout (seconds). Bounds a hung registry so the
#: marketplace never blocks the panel (State-Truth-First: graceful degrade).
#: 20s (was 8s) tolerates a slow / TLS-intercepting corporate network where the
#: first byte can take a while — 8s was tripping frequent spurious timeouts.
_DEFAULT_TIMEOUT_S: float = 20.0

#: Transient-failure retry policy for a single page fetch. A page request that
#: fails with a network/HTTP error is retried up to this many EXTRA times with a
#: short backoff, because the corporate proxy occasionally drops the first
#: connection — a quiet retry is far better UX than surfacing "refresh failed".
_FETCH_RETRIES: int = 2
_FETCH_RETRY_BACKOFF_S: float = 0.6

#: Hard cap on how many active listings we surface in one fetch (defence in
#: depth against an unbounded registry response + a sane marketplace size).
_MAX_ENTRIES: int = 200

#: The official registry's HARD per-page ``limit`` ceiling: a request with
#: ``limit > 100`` is rejected with HTTP 422 (empirically verified against
#: ``registry.modelcontextprotocol.io``). Every request MUST clamp its ``limit``
#: query parameter to this value; larger overall collection is done by paging
#: on ``metadata.nextCursor`` — NOT by asking for a bigger page. Kept distinct
#: from :data:`_MAX_ENTRIES` (our overall collection cap) so the two roles never
#: get conflated again (the conflation was the original 422 bug).
_MAX_PAGE_LIMIT: int = 100

#: The ``_meta`` key carrying the official registry status block.
_OFFICIAL_META_KEY: str = "io.modelcontextprotocol.registry/official"

#: HTTP status at/above which the registry response is treated as an error.
_HTTP_ERROR_STATUS: int = 400

#: Environment variable to RE-ENABLE TLS certificate verification for the
#: registry fetch. The DEFAULT is verify=False (no cert check) because this
#: deployment sits behind a TLS-intercepting proxy / self-signed corporate CA
#: whose chain the official registry cert cannot be validated against (symptom:
#: ``CERTIFICATE_VERIFY_FAILED``). Set this env truthy to opt BACK IN to strict
#: verification. Scoped strictly to THIS outbound registry-listing call — it
#: never affects any other TLS in the app.
_VERIFY_ENV_VAR: str = "QAI_CHAT_MCP_REGISTRY_VERIFY_TLS"


def _registry_tls_verify() -> bool:
    """Return whether to verify the registry's TLS cert (default False).

    Verification is OFF by default (the registry fetch tolerates a
    TLS-intercepting proxy out of the box). Returns ``True`` only when
    :data:`_VERIFY_ENV_VAR` is set to a truthy value
    (``1``/``true``/``yes``/``on``, case-insensitive) to opt back into strict
    checking. Read at call time so it can be flipped without a code change.
    """
    raw = os.environ.get(_VERIFY_ENV_VAR, "").strip().lower()
    return raw in ("1", "true", "yes", "on")


class McpRegistrySourceError(RuntimeError):
    """Raised when the registry fetch / parse fails.

    The registry adapter catches this and gracefully degrades to the curated
    source only (never propagates out of the ``list_catalog`` port surface).
    """


def _entry_id_from_name(name: str) -> str:
    """Derive a slug (server-name-safe) id from a reverse-DNS registry name.

    The registry ``name`` is e.g. ``io.github.owner/server-git``. An installed
    server name must match ``^[A-Za-z0-9][A-Za-z0-9_.\\-]*$`` (McpServerConfig),
    so we take the last path segment and strip anything unsafe. The full name is
    prefixed with ``registry:`` in the id-space handled by the registry adapter,
    but the DISPLAY id used as a default install name is the safe slug here.
    """
    tail = name.rsplit("/", 1)[-1] if "/" in name else name
    safe = "".join(c if (c.isalnum() or c in "_.-") else "-" for c in tail)
    safe = safe.strip("-._") or "server"
    # Ensure it starts with an alphanumeric (McpServerConfig name pattern).
    if not safe[0].isalnum():
        safe = "s" + safe
    return safe


def _map_env_vars(
    raw: Any,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Map a package's ``environmentVariables`` into (names, required, secrets)."""
    names: list[str] = []
    required: list[str] = []
    secrets: list[str] = []
    if not isinstance(raw, list):
        return (), (), ()
    for item in raw:
        if not isinstance(item, dict):
            continue
        vname = item.get("name")
        if not isinstance(vname, str) or not vname:
            continue
        names.append(vname)
        if bool(item.get("isRequired", False)):
            required.append(vname)
        if bool(item.get("isSecret", False)):
            secrets.append(vname)
    return tuple(names), tuple(required), tuple(secrets)


def _map_headers(
    raw: Any,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Map a remote's ``headers`` into (names, required, secrets)."""
    names: list[str] = []
    required: list[str] = []
    secrets: list[str] = []
    if not isinstance(raw, list):
        return (), (), ()
    for item in raw:
        if not isinstance(item, dict):
            continue
        hname = item.get("name")
        if not isinstance(hname, str) or not hname:
            continue
        names.append(hname)
        if bool(item.get("isRequired", False)):
            required.append(hname)
        if bool(item.get("isSecret", False)):
            secrets.append(hname)
    return tuple(names), tuple(required), tuple(secrets)


def _map_package(pkg: dict[str, Any]) -> tuple[str, str, tuple[str, ...]] | None:
    """Map a ``packages[]`` entry → (install_type, command, args_template).

    ``npm`` → ``npx -y <identifier>``. A non-stdio / non-npm package is
    unmappable (returns ``None``). The package version is intentionally NOT
    pinned into the args (registries move fast; ``npx`` resolves latest) —
    matching the curated set.

    Node-only launcher policy (intentional, mirrors the curated set): Python
    ``pypi`` packages are DELIBERATELY skipped rather than mapped to ``uvx``.
    On the target Windows-on-Snapdragon (ARM64) platform ``uvx`` isolates its
    own dependency resolution and frequently pulls native packages
    (e.g. ``cryptography``) with no ARM64 wheel, forcing a Rust build that
    fails; ``npx`` servers are pure-JS, auto-install their npm deps, and need no
    compiler. So a pypi listing is treated as "no mappable local package"
    (the caller then falls back to a remote endpoint, or skips the listing).

    No per-package TLS flag is injected here: the child-process certificate
    fallback (NODE_TLS_REJECT_UNAUTHORIZED) is applied centrally by the
    transport adapter (McpTransportClient._spawn_stdio) for EVERY stdio child,
    so these dynamic npm→npx commands are covered without duplicating it here.
    """
    transport = pkg.get("transport")
    ttype = transport.get("type") if isinstance(transport, dict) else "stdio"
    if ttype not in (None, "stdio"):
        return None
    identifier = pkg.get("identifier")
    if not isinstance(identifier, str) or not identifier:
        return None
    reg_type = str(pkg.get("registryType") or "").lower()
    if reg_type == "npm":
        # npm package names must not contain spaces or characters that
        # encodeURIComponent encodes (npm error EINVALIDTAGNAME).
        if " " in identifier or "%" in identifier:
            return None
        return "npx", "npx", ("-y", identifier)
    # pypi (uvx) and any other launcher are intentionally not supported — see
    # the Node-only policy in the docstring above.
    return None


def _is_active_latest(listing: dict[str, Any]) -> bool:
    """Return True when the listing's official meta marks it active + latest.

    A listing with NO official meta block is treated as active/latest (some
    mirrors omit it); an explicit non-active status or ``isLatest=False`` is
    rejected.  Internal registries (qgenie) may use status values like
    "published" or "approved" that are not "inactive"/"deprecated" — treat
    any non-explicitly-inactive status as active.
    """
    meta = listing.get("_meta")
    official = meta.get(_OFFICIAL_META_KEY) if isinstance(meta, dict) else None
    if not isinstance(official, dict):
        return True
    status = str(official.get("status") or "active").lower()
    if status in ("inactive", "deprecated", "removed", "disabled"):
        return False
    return official.get("isLatest") is not False


def _homepage_of(server: dict[str, Any]) -> str:
    """Best homepage URL for a listing: websiteUrl, else repository url."""
    website = server.get("websiteUrl")
    if isinstance(website, str) and website:
        return website
    repo = server.get("repository")
    if isinstance(repo, dict) and isinstance(repo.get("url"), str):
        return repo["url"]
    return ""


def _stdio_entry_from_packages(
    server: dict[str, Any], *, entry_id: str, display: str, description: str,
    homepage: str,
) -> CuratedCatalogEntry | None:
    """Build a stdio catalog entry from the first mappable ``packages`` item."""
    packages = server.get("packages")
    if not isinstance(packages, list):
        return None
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        mapped = _map_package(pkg)
        if mapped is None:
            continue
        install_type, command, args_template = mapped
        env_names, env_required, env_secrets = _map_env_vars(
            pkg.get("environmentVariables")
        )
        return CuratedCatalogEntry(
            id=entry_id,
            name=display,
            description=description,
            install_type=install_type,
            command=command,
            args_template=args_template,
            source="registry",
            env_schema=env_names,
            homepage=homepage,
            transport="stdio",
            env_required=env_required,
            secret_fields=env_secrets,
        )
    return None


def _remote_entry_from_remotes(
    server: dict[str, Any], *, entry_id: str, display: str, description: str,
    homepage: str,
) -> CuratedCatalogEntry | None:
    """Build an sse/http catalog entry from the first mappable ``remotes`` item."""
    remotes = server.get("remotes")
    if not isinstance(remotes, list):
        return None
    for remote in remotes:
        if not isinstance(remote, dict):
            continue
        # Try multiple common field names for the endpoint URL — different
        # registry implementations use different conventions.
        url = (
            remote.get("url")
            or remote.get("endpoint")
            or remote.get("href")
            or remote.get("serverUrl")
            or remote.get("uri")
            or ""
        )
        # Also check inside nested transportConfig / config objects.
        if not url:
            for cfg_key in ("transportConfig", "config", "transport"):
                sub = remote.get(cfg_key)
                if isinstance(sub, dict):
                    url = sub.get("url") or sub.get("endpoint") or ""
                    if url:
                        break
        if not url:
            continue  # skip remotes without any resolvable endpoint URL
        rtype = str(remote.get("type") or "").lower()
        if rtype in ("streamable-http", "http", "streamablehttp"):
            transport = "http"
        elif rtype == "sse":
            transport = "sse"
        elif rtype in ("", "websocket", "ws"):
            # Unknown/unspecified transport — default to http (streamable-http
            # is the modern standard; internal registries may omit the field).
            transport = "http"
        else:
            transport = "http"  # best-effort for any other value
        h_names, h_required, h_secrets = _map_headers(remote.get("headers"))
        return CuratedCatalogEntry(
            id=entry_id,
            name=display,
            description=description,
            install_type=transport,
            command="",
            args_template=(),
            source="registry",
            homepage=homepage,
            transport=transport,
            url=url,
            headers_schema=h_names,
            headers_required=h_required,
            secret_fields=h_secrets,
        )
    return None


def map_listing_to_entry(listing: dict[str, Any], *, source_url: str | None = None) -> CuratedCatalogEntry | None:
    """Map one registry listing → a ``source="registry"`` catalog entry.

    Returns ``None`` for a listing that is not active/latest, or that has no
    mappable install (neither a stdio package nor an sse/http remote). Never
    raises on a malformed listing — a bad entry is simply skipped.

    Prefers a local (stdio) package install; falls back to a remote endpoint.
    ``source_url`` is the URL from which this listing was fetched — used to
    construct the MCP endpoint for "routed" hub registries (e.g. qgenie) where
    the server object carries no explicit URL.
    """
    if not isinstance(listing, dict):
        return None
    server = listing.get("server")
    if not isinstance(server, dict):
        return None
    if not _is_active_latest(listing):
        return None
    name = server.get("name")
    if not isinstance(name, str) or not name:
        return None
    common = {
        "entry_id": _entry_id_from_name(name),
        "display": str(server.get("title") or name),
        "description": str(server.get("description") or ""),
        "homepage": _homepage_of(server),
    }
    entry = _stdio_entry_from_packages(server, **common) or _remote_entry_from_remotes(server, **common)
    if entry is not None:
        return entry
    # Routed hub pattern: the remote object carries no explicit URL because the
    # hub routes all requests through a single workspace endpoint.
    # /connect/{name}/mcp returns an HTML web page (MCPHub UI), NOT an MCP
    # transport stream — even with a valid Bearer token.  The actual MCP
    # transport endpoint is the workspace URL {hub_base}/mcp, which uses the
    # streamable-http (2025-03-26) protocol.
    if source_url and str(listing.get("registry_mode") or "").lower() == "routed":
        from urllib.parse import urlparse as _urlparse
        parsed = _urlparse(source_url)
        hub_base = f"{parsed.scheme}://{parsed.netloc}"
        # Prefer remotes[0].url when it's under the same hub host — the registry
        # may use underscores in the path (e.g. /connect/qgenie_chat/mcp) while
        # the server name uses hyphens; slug-based construction would mismatch.
        # Fall back to slug construction only when no explicit URL is given.
        remotes = server.get("remotes")
        remote: dict[str, Any] = {}
        if isinstance(remotes, list) and remotes and isinstance(remotes[0], dict):
            remote = remotes[0]
        _remote_url = str(remote.get("url") or "").strip()
        if _remote_url and _urlparse(_remote_url).netloc.lower() == parsed.netloc.lower():
            mcp_url = _remote_url
        else:
            # QGenie MCPHub uses underscores in endpoint paths even when the
            # registry server name contains hyphens (e.g. "qgenie-chat" →
            # /connect/qgenie_chat/mcp).  Normalise hyphens to underscores so
            # the constructed URL matches the actual server-side route.
            _slug = common["entry_id"].replace("-", "_")
            mcp_url = f"{hub_base}/connect/{_slug}/mcp"
        transport = "http"
        h_names, h_required, h_secrets = _map_headers(remote.get("headers"))
        # Check auth_type in publisher-provided meta for OAuth per-user flows.
        server_meta_dict = server.get("_meta")
        publisher_meta: dict[str, Any] = {}
        if isinstance(server_meta_dict, dict):
            publisher_meta = server_meta_dict.get(
                "io.modelcontextprotocol.registry/publisher-provided"
            ) or {}
        auth_types = publisher_meta.get("auth_type") or []
        # All QGenie MCPHub per-service endpoints share one auth server.
        # Use the hub root's /.well-known URL as the oauth_metadata_url so that
        # all entries from the same hub map to the SAME key in the frontend
        # preheat sentinel — only one OAuth flow is triggered for the whole hub.
        from urllib.parse import urlparse as _up
        _parsed = _up(mcp_url)
        oauth_metadata_url = f"{_parsed.scheme}://{_parsed.netloc}/.well-known/oauth-authorization-server"
        return CuratedCatalogEntry(
            id=common["entry_id"],
            name=common["display"],
            description=common["description"],
            install_type=transport,
            command="",
            args_template=(),
            source="registry",
            homepage=common["homepage"],
            transport=transport,
            url=mcp_url,
            headers_schema=h_names,
            headers_required=h_required,
            secret_fields=h_secrets,
            oauth_metadata_url=oauth_metadata_url,
        )
    return None


def _extract_next_cursor(payload: dict[str, Any]) -> str | None:
    """Return ``metadata.nextCursor`` from a registry page, or ``None``.

    The registry paginates via a cursor carried in ``metadata.nextCursor``; the
    LAST page omits it (or leaves it empty). A missing / empty / non-string
    cursor is normalised to ``None`` (= "no more pages").
    """
    meta = payload.get("metadata")
    if not isinstance(meta, dict):
        return None
    cursor = meta.get("nextCursor")
    if isinstance(cursor, str) and cursor:
        return cursor
    return None


def _why_dropped(listing: dict[str, Any]) -> str:
    """Return a short reason string why map_listing_to_entry would return None."""
    if not isinstance(listing, dict):
        return "not_dict"
    server = listing.get("server")
    if not isinstance(server, dict):
        return f"server_not_dict(type={type(listing.get('server')).__name__})"
    if not _is_active_latest(listing):
        meta = listing.get("_meta") or {}
        official = meta.get(_OFFICIAL_META_KEY) if isinstance(meta, dict) else None
        return f"not_active_latest(meta={official})"
    name = server.get("name")
    if not isinstance(name, str) or not name:
        return f"no_name(name={name!r})"
    # Both install paths returned None
    packages = server.get("packages")
    remotes = server.get("remotes")
    pkg_types = [p.get("registryType") for p in (packages or []) if isinstance(p, dict)]
    remote_urls = []
    for r in (remotes or []):
        if isinstance(r, dict):
            u = r.get("url") or r.get("endpoint") or r.get("href") or r.get("serverUrl") or r.get("uri") or ""
            remote_urls.append(f"{r.get('type','?')}:keys={list(r.keys())}:url={u[:40] if u else 'NO_URL'}")
    registry_mode = listing.get("registry_mode", "")
    return f"no_install(registry_mode={registry_mode!r}, pkg_types={pkg_types}, remotes={remote_urls})"


def _map_listings_page(
    listings: list[Any], *, limit: int, source_url: str | None = None
) -> tuple[CuratedCatalogEntry, ...]:
    """Map one page of raw listings → catalog entries (de-duped, capped)."""
    out: list[CuratedCatalogEntry] = []
    seen_ids: set[str] = set()
    dropped: list[str] = []
    for listing in listings:
        entry = map_listing_to_entry(listing, source_url=source_url)
        if entry is None:
            srv = listing.get("server") if isinstance(listing, dict) else {}
            display = (srv.get("title") or srv.get("name") or "?") if isinstance(srv, dict) else "?"
            reason = _why_dropped(listing)
            dropped.append(f"{display!r}:{reason}")
            continue
        if entry.id in seen_ids:
            continue
        seen_ids.add(entry.id)
        out.append(entry)
        if len(out) >= limit:
            break
    if dropped:
        logger.warning(
            "chat.mcp.listings_dropped total_in=%d mapped=%d dropped=%d: %s",
            len(listings), len(out), len(dropped), "; ".join(dropped[:20]),
        )
    else:
        logger.debug(
            "chat.mcp.listings_mapped total_in=%d mapped=%d",
            len(listings), len(out),
        )
    return tuple(out)


async def fetch_registry_page(
    *,
    base_url: str = DEFAULT_REGISTRY_BASE_URL,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    limit: int = _MAX_PAGE_LIMIT,
    cursor: str | None = None,
    search: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[tuple[CuratedCatalogEntry, ...], str | None]:
    """Fetch ONE page of the official registry, mapped, plus the next cursor.

    Performs a single ``GET {base_url}/v0/servers`` with query params:

    * ``limit`` — clamped to :data:`_MAX_PAGE_LIMIT` (100). A larger value is
      NOT sent (the registry 422s on ``limit > 100``); overall collection is
      done by paging on the returned cursor, not by a bigger page.
    * ``cursor`` — the opaque ``metadata.nextCursor`` from a prior page (omitted
      on the first page).
    * ``search`` — a server-side ``name`` substring filter (the registry only
      searches ``server.name``; the parameter name is literally ``search``).
      Omitted when empty / ``None``.

    Returns ``(entries, next_cursor)`` where ``next_cursor`` is ``None`` on the
    last page (no more results). On ANY network / HTTP / parse failure raises
    :class:`McpRegistrySourceError` so the caller can gracefully degrade.

    ``client`` lets a test inject a fake ``httpx.AsyncClient`` (mounted on a
    ``MockTransport``); when ``None`` a short-lived client is created + closed.
    TLS verification defaults to OFF (tolerates a TLS-intercepting proxy); set
    :data:`_VERIFY_ENV_VAR` to opt back into strict verification.
    """
    url = f"{base_url.rstrip('/')}{_SERVERS_PATH}"
    params: dict[str, str] = {"limit": str(min(limit, _MAX_PAGE_LIMIT))}
    if cursor:
        params["cursor"] = cursor
    if search and search.strip():
        params["search"] = search.strip()
    owns_client = client is None
    if client is not None:
        http = client
    else:
        verify = _registry_tls_verify()
        if not verify:
            logger.debug(
                "chat.mcp.registry_tls_verify_off (set %s=1 to enable)",
                _VERIFY_ENV_VAR,
            )
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s), verify=verify
        )
    try:
        # Retry the network GET on a transient failure (connection drop / 5xx /
        # timeout) — the corporate proxy occasionally drops the first attempt. A
        # 4xx (e.g. 422) is NOT retried (it is a deterministic request error).
        resp = None
        last_exc: Exception | None = None
        for attempt in range(_FETCH_RETRIES + 1):
            try:
                resp = await http.get(url, params=params)
            except (httpx.HTTPError, OSError) as exc:
                last_exc = exc
                logger.debug(
                    "chat.mcp.registry_fetch_attempt_failed attempt=%d/%d %s: %s",
                    attempt + 1,
                    _FETCH_RETRIES + 1,
                    type(exc).__name__,
                    str(exc) or repr(exc),
                )
                if attempt < _FETCH_RETRIES:
                    await asyncio.sleep(_FETCH_RETRY_BACKOFF_S * (attempt + 1))
                    continue
                # Out of retries — surface the real cause. Include the exception
                # TYPE name because several httpx errors (ConnectError /
                # ConnectTimeout / ReadError) have an EMPTY str(), which produced
                # a useless "http_error: " with no detail.
                detail = str(exc).strip() or repr(exc)
                raise McpRegistrySourceError(
                    f"http_error: {type(exc).__name__}: {detail}"
                ) from exc
            # Got a response. Retry ONLY on a transient 5xx; 4xx is terminal.
            if resp.status_code >= 500 and attempt < _FETCH_RETRIES:
                logger.debug(
                    "chat.mcp.registry_fetch_5xx attempt=%d status=%d",
                    attempt + 1,
                    resp.status_code,
                )
                await asyncio.sleep(_FETCH_RETRY_BACKOFF_S * (attempt + 1))
                continue
            break
        if resp is None:  # pragma: no cover — loop always sets resp or raises
            raise McpRegistrySourceError(
                f"http_error: {type(last_exc).__name__ if last_exc else 'unknown'}"
            )
        if resp.status_code >= _HTTP_ERROR_STATUS:
            raise McpRegistrySourceError(f"http_status_{resp.status_code}")
        try:
            payload = resp.json()
        except (ValueError, TypeError) as exc:
            raise McpRegistrySourceError(f"bad_json: {exc}") from exc
    finally:
        if owns_client:
            with contextlib.suppress(Exception):
                await http.aclose()

    if not isinstance(payload, dict):
        raise McpRegistrySourceError("unexpected response shape")
    listings = payload.get("servers")
    if not isinstance(listings, list):
        raise McpRegistrySourceError("missing 'servers' array")

    entries = _map_listings_page(listings, limit=limit)
    next_cursor = _extract_next_cursor(payload)
    return entries, next_cursor


async def fetch_registry_entries(
    *,
    base_url: str = DEFAULT_REGISTRY_BASE_URL,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    limit: int = _MAX_ENTRIES,
    client: httpx.AsyncClient | None = None,
) -> tuple[CuratedCatalogEntry, ...]:
    """Fetch + map the official registry's FIRST page into catalog entries.

    Thin back-compat wrapper over :func:`fetch_registry_page`: takes the first
    page and DISCARDS the pagination cursor (the phase-2 "load first page"
    behaviour). The ``limit`` query parameter sent to the registry is clamped to
    :data:`_MAX_PAGE_LIMIT` (100) inside :func:`fetch_registry_page` even when a
    caller passes the larger :data:`_MAX_ENTRIES` overall cap — so the request
    never triggers the registry's 422 (``limit > 100``).

    On ANY network / HTTP / parse failure raises :class:`McpRegistrySourceError`
    so the caller (the registry adapter) can gracefully degrade to the curated
    source only.
    """
    entries, _next = await fetch_registry_page(
        base_url=base_url,
        timeout_s=timeout_s,
        limit=limit,
        client=client,
    )
    return entries


# ---------------------------------------------------------------------------
# Multi-format custom source support
# ---------------------------------------------------------------------------

def _try_parse_mcp_registry_format(
    payload: Any,
    *,
    limit: int,
    source_url: str | None = None,
) -> tuple[tuple[CuratedCatalogEntry, ...], str | None] | None:
    """Try to parse MCP Registry API format: {servers: [...], metadata: {...}}.

    Returns (entries, next_cursor) on success, None if not this format.
    """
    if not isinstance(payload, dict):
        return None
    listings = payload.get("servers")
    if not isinstance(listings, list):
        return None
    entries = _map_listings_page(listings, limit=limit, source_url=source_url)
    next_cursor = _extract_next_cursor(payload)
    return entries, next_cursor


def _try_parse_smithery_format(
    payload: Any,
    *,
    limit: int,
) -> tuple[tuple[CuratedCatalogEntry, ...], str | None] | None:
    """Try to parse smithery.ai /servers format.

    Smithery returns:
    {
      "servers": [
        {
          "qualifiedName": "@owner/server-name",
          "displayName": "Human Name",
          "description": "...",
          "homepage": "https://...",
          "useCount": 123,
          "isDeployed": true,
          "createdAt": "...",
          "tools": [...]
        }
      ],
      "pagination": {"currentPage": 1, "pageSize": 10, "totalPages": 5}
    }

    Each smithery server can be opened at https://smithery.ai/server/<qualifiedName>
    and installed as a remote streamable-http endpoint if isDeployed.
    """
    if not isinstance(payload, dict):
        return None
    servers = payload.get("servers")
    if not isinstance(servers, list):
        return None
    # Smithery servers don't have the MCP registry "server" nesting — check by
    # looking for smithery-specific fields.
    if not servers:
        # Empty list — could be either format, treat as smithery only when
        # the pagination key is present.
        if "pagination" not in payload:
            return None
    else:
        first = servers[0] if servers else {}
        # MCP registry format has a "server" dict inside the listing; smithery
        # has flat fields like "qualifiedName" / "displayName".
        if isinstance(first, dict) and "server" in first:
            return None  # This is MCP registry format, not smithery.

    entries_out: list[CuratedCatalogEntry] = []
    seen_ids: set[str] = set()
    for item in servers:
        if not isinstance(item, dict):
            continue
        qname = item.get("qualifiedName") or item.get("name") or ""
        if not isinstance(qname, str) or not qname:
            continue
        display = str(item.get("displayName") or item.get("title") or qname)
        description = str(item.get("description") or "")
        homepage = str(item.get("homepage") or "")
        entry_id = _entry_id_from_name(qname)
        if entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)

        # Build a remote http entry pointing to the smithery server URL.
        # Smithery serves deployed servers at /server/<qualifiedName>.
        server_url = f"https://server.smithery.ai/{qname.lstrip('@')}/mcp"
        entries_out.append(
            CuratedCatalogEntry(
                id=entry_id,
                name=display,
                description=description,
                install_type="http",
                command="",
                args_template=(),
                source="registry",
                homepage=homepage or f"https://smithery.ai/server/{qname}",
                transport="http",
                url=server_url,
            )
        )
        if len(entries_out) >= limit:
            break

    pagination = payload.get("pagination")
    next_cursor: str | None = None
    if isinstance(pagination, dict):
        current = pagination.get("currentPage", 1)
        total = pagination.get("totalPages", 1)
        if isinstance(current, int) and isinstance(total, int) and current < total:
            next_cursor = str(current + 1)

    return tuple(entries_out), next_cursor


def _try_parse_generic_list_format(
    payload: Any,
    *,
    limit: int,
) -> tuple[tuple[CuratedCatalogEntry, ...], str | None] | None:
    """Try to parse a generic top-level array of server objects.

    Handles responses that are just a plain JSON array (or an object with a
    list under any of several common keys: 'items', 'data', 'results').
    Each item must have at least a 'name' field.
    """
    items: list[Any] | None = None
    if isinstance(payload, list):
        items = payload
    elif isinstance(payload, dict):
        for key in ("items", "data", "results", "entries"):
            val = payload.get(key)
            if isinstance(val, list):
                items = val
                break

    if items is None or not items:
        return None

    entries_out: list[CuratedCatalogEntry] = []
    seen_ids: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("id") or item.get("qualifiedName") or ""
        if not isinstance(name, str) or not name:
            continue
        display = str(item.get("title") or item.get("displayName") or name)
        description = str(item.get("description") or "")
        # Prefer explicit homepage/websiteUrl; do NOT fall back to "url"/"endpoint"
        # since those are MCP endpoints, not human-readable docs pages.
        homepage = str(item.get("homepage") or item.get("websiteUrl") or "")
        entry_id = _entry_id_from_name(name)
        if entry_id in seen_ids:
            continue
        seen_ids.add(entry_id)
        url = item.get("url") or item.get("endpoint") or ""
        # Resolve transport: check config.transport for ceflow-style objects.
        cfg = item.get("config")
        raw_transport = ""
        headers_schema: tuple[str, ...] = ()
        headers_required: tuple[str, ...] = ()
        secret_fields: tuple[str, ...] = ()
        if isinstance(cfg, dict):
            raw_transport = str(cfg.get("transport") or "")
            # config.headers keys with empty values = required headers to fill.
            cfg_headers = cfg.get("headers")
            if isinstance(cfg_headers, dict):
                all_hdrs = tuple(str(k) for k in cfg_headers if k)
                empty_hdrs = tuple(str(k) for k, v in cfg_headers.items() if k and not v)
                headers_schema = all_hdrs
                headers_required = empty_hdrs
                secret_fields = empty_hdrs  # treat required headers as secrets
        if not raw_transport:
            raw_transport = str(item.get("transport") or "")
        transport = "sse" if "sse" in raw_transport.lower() else "http"
        # Check for OAuth metadata endpoint (Orbit-style servers expose /.well-known).
        oauth_metadata_url = ""
        if isinstance(cfg, dict) and cfg.get("hosting") == "external":
            ext = cfg.get("externalEndpoint") or url
            if isinstance(ext, str) and ext:
                candidate = ext.rstrip("/") + "/.well-known/oauth-authorization-server"
                # Only set if the entry's own readme/description mentions OAuth.
                desc_lower = description.lower()
                if "oauth" in desc_lower or "entra" in desc_lower:
                    oauth_metadata_url = ext.rstrip("/")
        if isinstance(url, str) and url.startswith("http"):
            entry = CuratedCatalogEntry(
                id=entry_id,
                name=display,
                description=description,
                install_type="http",
                command="",
                args_template=(),
                source="registry",
                homepage=homepage,
                transport=transport,
                url=url,
                headers_schema=headers_schema,
                headers_required=headers_required,
                secret_fields=secret_fields,
                oauth_metadata_url=oauth_metadata_url,
            )
        else:
            # No HTTP URL — try stdio config from the registry item.
            # ceflow-style entries carry config.stdioCommand + config.stdioArgs.
            stdio_command = ""
            stdio_args: tuple[str, ...] = ()
            package_url = str(item.get("packageUrl") or "")
            if isinstance(cfg, dict):
                stdio_command = str(cfg.get("stdioCommand") or "")
                raw_args = cfg.get("stdioArgs")
                if isinstance(raw_args, list):
                    stdio_args = tuple(str(a) for a in raw_args if a)
            if not stdio_command:
                # Fallback: use name directly as npm package identifier only if
                # the name contains no spaces / invalid npm characters.
                if " " in name or "%" in name:
                    continue
                stdio_command = "npx"
                stdio_args = ("-y", name)
            entry = CuratedCatalogEntry(
                id=entry_id,
                name=display,
                description=description,
                install_type="npx" if stdio_command == "npx" else stdio_command,
                command=stdio_command,
                args_template=stdio_args,
                source="registry",
                homepage=homepage,
                transport="stdio",
                package_url=package_url,
            )
        entries_out.append(entry)
        if len(entries_out) >= limit:
            break

    if not entries_out:
        return None
    return tuple(entries_out), None


def _normalise_custom_url(url: str) -> str:
    """Rewrite well-known web-page URLs to their JSON API counterparts.

    Currently handled:
    - ``https://smithery.ai/...``  →  ``https://registry.smithery.ai/servers``
    - Internal QGenie MCPHub URLs  →  QGenie API endpoint (internal edition only;
      URLs resolved from ``qai.platform.edition.loader.get_mcp_hub_urls`` so
      that no internal domain literals appear in external artifacts)
    - Internal CEFlow URLs         →  CEFlow API endpoint (same loader pattern)
    """
    import re as _re
    import urllib.parse as _up

    url = url.strip()
    if _re.search(r"://(?:www\.)?smithery\.ai(?:/|$)", url, _re.IGNORECASE):
        return "https://registry.smithery.ai/servers"

    # Internal hubs: resolve host from loader so literals stay edition-excluded.
    try:
        from qai.platform.edition.loader import get_mcp_hub_urls as _hub_urls
        _h = _hub_urls()
    except ImportError:
        _h: dict[str, str] = {}
    _qgenie_browse = _h.get("qgenie_mcphub_url") or ""
    _ceflow_browse = _h.get("ceflow_mcphub_url") or ""
    _ceflow_api = _h.get("ceflow_normalised_url") or ""

    if _qgenie_browse:
        _qhost = _up.urlparse(_qgenie_browse).netloc
        if _qhost and _up.urlparse(url).netloc.lower() == _qhost.lower():
            return _re.sub(
                r"/workspace/mcp-connections/library.*$",
                "/api/registry/library/servers",
                _qgenie_browse,
            )

    if _ceflow_browse or _ceflow_api:
        _chost = _up.urlparse(_ceflow_browse or _ceflow_api).netloc
        if _chost and _up.urlparse(url).netloc.lower() == _chost.lower():
            return _ceflow_api or url

    return url


def _parse_custom_source_payload(
    payload: Any,
    *,
    limit: int,
    source_url: str | None = None,
) -> tuple[tuple[CuratedCatalogEntry, ...], str | None]:
    """Try each known format in priority order; raise if none match."""
    result = _try_parse_mcp_registry_format(payload, limit=limit, source_url=source_url)
    if result is not None:
        return result
    result = _try_parse_smithery_format(payload, limit=limit)
    if result is not None:
        return result
    result = _try_parse_generic_list_format(payload, limit=limit)
    if result is not None:
        return result
    raise McpRegistrySourceError("unrecognized_format: no known MCP server list format detected")


async def fetch_custom_source_page(
    *,
    url: str,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
    limit: int = _MAX_PAGE_LIMIT,
    cursor: str | None = None,
    search: str | None = None,
    bearer_token: str | None = None,
    cookie: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> tuple[tuple[CuratedCatalogEntry, ...], str | None]:
    """Fetch ONE page from a custom (user-provided) source URL.

    Unlike :func:`fetch_registry_page`, this function does NOT append
    ``/v0/servers`` to the URL — it fetches the URL exactly as given, then
    tries multiple known response formats in order:

    1. **MCP Registry API** — ``{servers: [...], metadata: {nextCursor}}``
    2. **Smithery** — ``{servers: [{qualifiedName, displayName, ...}], pagination}``
    3. **Generic list** — top-level array or object with ``items``/``data``/etc.

    If the direct URL returns nothing recognisable AND the URL does not already
    end with ``/v0/servers``, it automatically falls back to
    ``{url}/v0/servers`` (the MCP Registry path) — so a user who provides
    just the registry base URL (without the path) still works.

    ``cursor`` for smithery sources is a page number (as a string); for MCP
    registry sources it is the opaque ``metadata.nextCursor``.
    ``search`` is forwarded as the ``q`` or ``search`` query parameter
    (smithery uses ``q``; the official registry uses ``search``).
    """
    # Normalise well-known platform URLs to their real API endpoints.
    # smithery.ai web pages are not JSON — map to the registry API host.
    raw_url = url.strip()
    url = _normalise_custom_url(raw_url)
    # If the URL was rewritten by _normalise_custom_url, it is now a precise
    # API endpoint — skip the /v0/servers fallbacks entirely to avoid sending
    # requests to nonsense paths like /api/registry/library/servers/v0/servers.
    url_was_normalised = url != raw_url

    params: dict[str, str] = {"limit": str(min(limit, _MAX_PAGE_LIMIT))}
    if cursor:
        params["cursor"] = cursor
        params["page"] = cursor  # smithery uses "page"
    if search and search.strip():
        params["search"] = search.strip()
        params["q"] = search.strip()  # smithery uses "q"

    owns_client = client is None
    if client is not None:
        http = client
    else:
        verify = _registry_tls_verify()
        headers: dict[str, str] = {}
        if bearer_token:
            headers["Authorization"] = f"Bearer {bearer_token}"
        if cookie:
            headers["Cookie"] = cookie
        http = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s), verify=verify,
            follow_redirects=True, headers=headers,
        )

    async def _get_with_retry(target_url: str, qp: dict[str, str]) -> Any:
        resp = None
        last_exc: Exception | None = None
        for attempt in range(_FETCH_RETRIES + 1):
            try:
                resp = await http.get(target_url, params=qp)
            except (httpx.HTTPError, OSError) as exc:
                last_exc = exc
                if attempt < _FETCH_RETRIES:
                    await asyncio.sleep(_FETCH_RETRY_BACKOFF_S * (attempt + 1))
                    continue
                detail = str(exc).strip() or repr(exc)
                raise McpRegistrySourceError(
                    f"http_error: {type(exc).__name__}: {detail}"
                ) from exc
            if resp.status_code >= 500 and attempt < _FETCH_RETRIES:
                await asyncio.sleep(_FETCH_RETRY_BACKOFF_S * (attempt + 1))
                continue
            break
        if resp is None:
            raise McpRegistrySourceError(
                f"http_error: {type(last_exc).__name__ if last_exc else 'unknown'}"
            )
        if resp.status_code >= _HTTP_ERROR_STATUS:
            raise McpRegistrySourceError(f"http_status_{resp.status_code}")
        content_type = resp.headers.get("content-type", "")
        raw = resp.text
        if raw.lstrip().startswith("<"):
            raise McpRegistrySourceError(
                "not_json: the URL returned an HTML page, not a JSON API"
            )
        try:
            return resp.json()
        except (ValueError, TypeError) as exc:
            raise McpRegistrySourceError(f"bad_json: {exc}") from exc

    try:
        last_err: McpRegistrySourceError | None = None

        def _record(exc: McpRegistrySourceError) -> None:
            nonlocal last_err
            # Prefer a concrete HTTP/network error over a format-mismatch label —
            # it tells the user what actually went wrong (e.g. 401, HTML page).
            if last_err is None or str(last_err).startswith("unrecognized_format"):
                last_err = exc

        # Attempt 1: fetch the URL exactly as given and try all known formats.
        # If the direct URL returns HTML or an unrecognised format, fall through.
        try:
            payload = await _get_with_retry(url, params)
            return _parse_custom_source_payload(payload, limit=limit, source_url=url)
        except McpRegistrySourceError as exc:
            _record(exc)

        base_params: dict[str, str] = {"limit": str(min(limit, _MAX_PAGE_LIMIT))}
        if cursor:
            base_params["cursor"] = cursor
        if search and search.strip():
            base_params["search"] = search.strip()

        # Attempt 2: append /v0/servers to the exact URL given.
        # Skip if the URL was already rewritten by _normalise_custom_url — it
        # is already a precise API endpoint (e.g. /api/registry/library/servers
        # for qgenie), so appending /v0/servers would produce a garbage path.
        normalized = url.rstrip("/")
        if not url_was_normalised and not normalized.endswith(_SERVERS_PATH):
            try:
                payload2 = await _get_with_retry(normalized + _SERVERS_PATH, base_params)
                return _parse_custom_source_payload(payload2, limit=limit, source_url=url)
            except McpRegistrySourceError as exc:
                _record(exc)

        # Attempt 3: try the scheme+host root with /v0/servers.
        # Handles cases like https://host/workspace/page → https://host/v0/servers.
        # Skip if the URL was rewritten by _normalise_custom_url (already precise).
        from urllib.parse import urlparse as _urlparse
        parsed = _urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        root_api = root + _SERVERS_PATH
        if not url_was_normalised and root_api != normalized + _SERVERS_PATH:
            try:
                payload3 = await _get_with_retry(root_api, base_params)
                return _parse_custom_source_payload(payload3, limit=limit, source_url=url)
            except McpRegistrySourceError as exc:
                _record(exc)

        # Re-raise the most informative error captured (e.g. http_status_401,
        # not_json) so the user sees the actual failure reason.
        raise last_err or McpRegistrySourceError("unrecognized_format")
    finally:
        if owns_client:
            with contextlib.suppress(Exception):
                await http.aclose()
