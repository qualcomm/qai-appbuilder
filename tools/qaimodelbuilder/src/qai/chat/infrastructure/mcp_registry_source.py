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
        return "npx", "npx", ("-y", identifier)
    # pypi (uvx) and any other launcher are intentionally not supported — see
    # the Node-only policy in the docstring above.
    return None


def _is_active_latest(listing: dict[str, Any]) -> bool:
    """Return True when the listing's official meta marks it active + latest.

    A listing with NO official meta block is treated as active/latest (some
    mirrors omit it); an explicit non-active status or ``isLatest=False`` is
    rejected.
    """
    meta = listing.get("_meta")
    official = meta.get(_OFFICIAL_META_KEY) if isinstance(meta, dict) else None
    if not isinstance(official, dict):
        return True
    if str(official.get("status") or "active") != "active":
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
        url = remote.get("url")
        if not isinstance(url, str) or not url:
            continue
        rtype = str(remote.get("type") or "").lower()
        if rtype in ("streamable-http", "http"):
            transport = "http"
        elif rtype == "sse":
            transport = "sse"
        else:
            continue
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


def map_listing_to_entry(listing: dict[str, Any]) -> CuratedCatalogEntry | None:
    """Map one registry listing → a ``source="registry"`` catalog entry.

    Returns ``None`` for a listing that is not active/latest, or that has no
    mappable install (neither a stdio package nor an sse/http remote). Never
    raises on a malformed listing — a bad entry is simply skipped.

    Prefers a local (stdio) package install; falls back to a remote endpoint.
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
    return _stdio_entry_from_packages(
        server, **common
    ) or _remote_entry_from_remotes(server, **common)


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


def _map_listings_page(
    listings: list[Any], *, limit: int
) -> tuple[CuratedCatalogEntry, ...]:
    """Map one page of raw listings → catalog entries (de-duped, capped)."""
    out: list[CuratedCatalogEntry] = []
    seen_ids: set[str] = set()
    for listing in listings:
        entry = map_listing_to_entry(listing)
        if entry is None:
            continue
        # De-dup by id (multiple listings can slugify to the same tail); the
        # first (active+latest) wins.
        if entry.id in seen_ids:
            continue
        seen_ids.add(entry.id)
        out.append(entry)
        if len(out) >= limit:
            break
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
