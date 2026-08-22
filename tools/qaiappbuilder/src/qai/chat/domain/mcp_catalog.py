# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Curated MCP marketplace catalog (phase-1 built-in 'curated' source).

The MCP marketplace lets the user browse a *source* (an MCP registry / catalog)
and install servers from it.  Phase 1 ships ONE built-in source — a static,
self-maintained ``curated`` list of high-frequency, **credential-free**,
locally-runnable (``npx`` → npm packages + ``stdio``) MCP servers.  A
future phase 2 will add a dynamic official-registry source; the entry shape here
is deliberately multi-source-ready (each entry tags its :attr:`source`) so both
can be aggregated by the registry without a schema change.

Pure data / value objects — no I/O, no ``httpx`` / ``apps`` / ``interfaces``
imports (domain-purity, AGENTS.md §3.5).  The registry
(:class:`qai.chat.adapters.mcp_client.McpServerRegistry`) reads this catalog and
materialises a chosen entry (plus any user-supplied argument values) into an
:class:`qai.chat.domain.mcp_server.McpServerConfig` via ``add_server``.

Curated selection policy
------------------------
Entries are npm packages that run locally via ``npx`` over ``stdio`` and whose
exact package name is verified to exist. Two kinds:

* **credential-free** (filesystem / memory / sequential-thinking / everything /
  playwright / context7) — install and run with no extra input;
* **credential-required** (github / brave-search / tavily / elasticsearch /
  confluence) — declare their required key(s) via ``env_schema`` +
  ``env_required`` + ``secret_fields`` so the install dialog collects them
  (secret ones rendered as password fields); the values are injected into the
  child process ``env`` at spawn (see ``secret_fields`` docs on persistence).
  These are included because the most-used real-world servers (code hosts, web
  search, data stores, wikis) inherently need credentials — omitting them would
  leave the marketplace feeling empty of anything useful.

All entries are implementation-tested to actually spawn + complete the MCP
handshake via ``npx`` on the target platform (servers that failed to launch —
Python-only ``fetch``, port-binding ``pdf``, stdout-flooding ``m365``, cluster-
control ``kubernetes`` [dropped as too high-risk], or placeholder packages —
are intentionally excluded).

Node-only launcher policy (intentional)
---------------------------------------
The curated set is deliberately **npx-only** — the Python ``uvx`` launcher is
NOT used.  On the target Windows-on-Snapdragon (ARM64) platform ``uvx`` isolates
its own dependency resolution and frequently pulls native packages
(e.g. ``cryptography``) that have no ARM64 pre-built wheel, forcing a
Rust source build that fails; ``npx`` servers are pure-JS, auto-install their
npm dependencies, and need no compiler.  The bundled portable Node.js toolchain
(resolved via the process PATH — see ``qai.platform.process.bundled_path``) is
preferred over any host Node install.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "CURATED_CATALOG",
    "EXTERNAL_CURATED_CATALOG",
    "INTERNAL_CURATED_CATALOG",
    "CuratedCatalogEntry",
    "McpInstallType",
    "build_internal_curated_catalog",
    "get_catalog_entry",
]


# The install type mirrors the launcher a packages+stdio server uses. Kept as a
# plain ``str`` alias (not an Enum) so the wire value is trivially serialisable
# and a future type (e.g. "docker") is an additive string, not a breaking enum.
# The curated set is npx-only (see the module docstring's Node-only policy); the
# alias still permits other values for dynamic-registry entries / remote
# transports ("sse" / "http").
McpInstallType = str  # "npx" (curated); dynamic/remote may use "sse" / "http"


@dataclass(frozen=True, slots=True, kw_only=True)
class CuratedCatalogEntry:
    """One entry in an MCP catalog source (marketplace listing).

    Fields:

    * ``id`` — stable slug (also the default installed-server ``name``);
    * ``name`` — human-readable display name;
    * ``description`` — one-line summary for the marketplace card;
    * ``source`` — the owning catalog source id (phase 1: always ``"curated"``;
      phase 2 dynamic-registry entries will carry e.g. ``"registry"``);
    * ``install_type`` — the packages+stdio launcher (curated: ``"npx"``);
    * ``command`` — the launcher executable (curated: ``"npx"``);
    * ``args_template`` — the argument list, possibly containing ``<PLACEHOLDER>``
      tokens the user fills at install time (e.g. ``<PATH>``);
    * ``requires_args`` — the placeholder token names the user MUST supply
      (empty tuple = installs with no extra input);
    * ``env_schema`` — declared environment-variable names (empty for the
      credential-free curated set; reserved for future entries);
    * ``homepage`` — docs / repo URL for the marketplace card link.

    Phase-2 dynamic-registry fields (tail-appended, all optional — AGENTS.md
    §3.1 append-only; a curated entry leaves them at their defaults so its
    shape / behaviour is byte-for-byte unchanged):

    * ``transport`` — ``"stdio"`` (the phase-1 packages+launcher default) or
      ``"sse"`` / ``"http"`` for a REMOTE registry server. Selects which of the
      command/args (stdio) vs ``url``/``headers_schema`` (remote) fields the
      installer materialises;
    * ``url`` — the remote endpoint (only for ``transport`` in {sse, http});
    * ``env_schema`` doubles as the declared ENV var names an installer must
      collect (a phase-2 stdio+key server declares its required key names
      here);
    * ``env_required`` — the subset of ``env_schema`` names that are REQUIRED
      (must be supplied, non-empty) at install time;
    * ``headers_schema`` — declared HTTP header names a remote server accepts
      (credential-bearing header values are routed to the SecretStore, never
      persisted plain-text);
    * ``headers_required`` — the subset of ``headers_schema`` names that are
      REQUIRED at install time;
    * ``secret_fields`` — the subset of ``env_schema`` + ``headers_schema``
      names whose VALUES are secrets (the UI renders them as password inputs).
      Secret VALUES are always externalised to the platform SecretStore — only a
      ``__secret__`` sentinel is written to the on-disk config (AGENTS.md §3.3),
      for BOTH a remote server's secret HEADER values and a stdio server's
      secret ENV values (the latter are re-hydrated from the SecretStore at load
      and injected into the child process at spawn). A field NOT in
      ``secret_fields`` is a plain, non-sensitive value persisted as-is (e.g. a
      non-secret endpoint URL env like ``ES_URL``).

    Pure value object.  The registry combines ``command`` + a substituted
    ``args_template`` (stdio) — or ``url`` + collected header/env values
    (remote / keyed) — into an :class:`McpServerConfig` at install time.
    """

    id: str
    name: str
    description: str
    install_type: McpInstallType
    command: str
    args_template: tuple[str, ...]
    source: str = "curated"
    requires_args: tuple[str, ...] = ()
    env_schema: tuple[str, ...] = ()
    homepage: str = ""
    # ── phase-2 dynamic-registry fields (tail-appended, optional) ──
    transport: str = "stdio"  # "stdio" | "sse" | "http"
    url: str = ""
    env_required: tuple[str, ...] = ()
    headers_schema: tuple[str, ...] = ()
    headers_required: tuple[str, ...] = ()
    secret_fields: tuple[str, ...] = ()
    # headers_labels: optional mapping from raw header key → human-readable label
    # shown in the install dialog. When absent the raw key is used as the label.
    # The actual HTTP header sent to the server always uses the raw key.
    headers_labels: dict[str, str] | None = None
    # oauth_url: DEPRECATED — kept for backwards-compat (ignored by new flow).
    # oauth_metadata_url: when set, the install dialog triggers the full
    # OAuth 2.1 + PKCE browser flow (backend discovers endpoints via RFC 8414,
    # exchanges code for a token, and auto-fills the Authorization field).
    # Set to the MCP server's /.well-known discovery base URL.
    oauth_url: str = ""
    oauth_metadata_url: str = ""
    # package_url: when set, the backend downloads the .mcp package (zip),
    # extracts server.py, creates a venv, installs dependencies, and runs
    # the server via the extracted python instead of the raw command.
    package_url: str = ""


# ---------------------------------------------------------------------------
# Qualcomm internal MCP entries (internal edition only).
#
# The entry SHAPE lives here (ids, display names, descriptions, header schema,
# i18n label keys) — it is code. The ENDPOINT VALUES do NOT: they are
# internal-only pure-data config, so per ``PROJECT-RULES.md §3.8.1(2)`` they
# live in ``src/qai/platform/edition/internal_config.toml [mcp_catalog]``, a
# subtree physically excluded from external artifacts. This module ships to
# BOTH editions (``src/qai/`` is on the release whitelist), so an intranet
# hostname written here would land verbatim in the open-source drop — the exact
# leak ``check_release --scan-sensitive`` exists to catch (defence layer 4; both
# hosts are registered keywords there, so re-hardcoding one fails the release).
# The hosts are deliberately not named even in this comment: the scan reads
# comments too, and a comment is the easiest place for a literal to survive a
# refactor unnoticed.
#
# The endpoints are injected at COMPOSITION time by
# ``qai.chat.adapters.mcp_client.resolve_curated_catalog`` under the existing
# ``is_internal`` gate — the same seam that already reads
# ``get_mcp_hub_urls()`` for the registry-source presets. This keeps the domain
# pure (no I/O, no literals) and keeps the external artifact literal-free by
# construction rather than by a reviewer noticing.
# ---------------------------------------------------------------------------

# The QGenie MCPHub-fronted servers: (path slug, entry id, display name,
# description). All three share one hub base URL, one OAuth discovery document,
# and one URL shape — ``{base}/connect/{slug}/mcp`` — so they are expanded from
# this table instead of being spelled out four times.
_QGENIE_HUB_SERVERS: tuple[tuple[str, str, str, str], ...] = (
    (
        "qgenie_chat",
        "qgenie-chat",
        "QGenie Chat",
        "Qualcomm AI chat assistant via QGenie MCPHub — access Qualcomm-hosted "
        "AI models and chat capabilities through the MCP protocol.",
    ),
    (
        "teams",
        "teams",
        "Teams",
        "Microsoft Teams integration via QGenie MCPHub — send messages, "
        "manage channels, and access Teams data through the MCP protocol.",
    ),
    (
        "outlook",
        "outlook",
        "Outlook",
        "Microsoft Outlook integration via QGenie MCPHub — read and send emails, "
        "manage calendar events, and access Outlook data through the MCP protocol.",
    ),
)


def build_internal_curated_catalog(
    *,
    cebot_url: str = "",
    cebot_homepage: str = "",
    qgenie_mcphub_base: str = "",
) -> tuple[CuratedCatalogEntry, ...]:
    """Build the internal curated entries from injected endpoint values.

    Pure function — the caller supplies the endpoints (internal edition: from
    ``internal_config.toml [mcp_catalog]``; external edition: nothing, because
    that whole package is excluded from the artifact).

    An entry whose endpoint did not resolve is OMITTED rather than emitted with
    an empty ``url``: a remote entry with no URL has nothing to connect to, so
    listing it would only offer the user an install that must fail. With no
    endpoints at all this returns ``()`` — which is precisely the external
    edition's correct catalog.
    """
    entries: list[CuratedCatalogEntry] = []

    # CEBot MCP — Qualcomm internal engineering assistant via HTTP + X-User-Id.
    if cebot_url:
        entries.append(
            CuratedCatalogEntry(
                id="CEBot_MCP",
                name="CEBot MCP",
                description=(
                    "Qualcomm internal engineering assistant via CEBot — query "
                    "technical knowledge, run diagnostics, and access Qualcomm "
                    "internal tools."
                ),
                install_type="http",
                command="",
                args_template=(),
                source="curated",
                transport="http",
                url=cebot_url,
                headers_schema=("X-User-Id",),
                headers_required=("X-User-Id",),
                secret_fields=(),
                headers_labels={
                    "X-User-Id": "mcpServers.market.headerLabels.cebotXUserId"
                },
                homepage=cebot_homepage or cebot_url,
            )
        )

    # QGenie MCPHub-fronted servers (OAuth 2.1 + PKCE via the hub's discovery
    # document). ``rstrip("/")`` so a trailing slash in the config cannot
    # produce a ``//connect`` URL that the hub rejects.
    if qgenie_mcphub_base:
        base = qgenie_mcphub_base.rstrip("/")
        oauth_metadata_url = f"{base}/.well-known/oauth-authorization-server"
        for slug, entry_id, display_name, description in _QGENIE_HUB_SERVERS:
            entries.append(
                CuratedCatalogEntry(
                    id=entry_id,
                    name=display_name,
                    description=description,
                    install_type="http",
                    command="",
                    args_template=(),
                    source="curated",
                    transport="http",
                    url=f"{base}/connect/{slug}/mcp",
                    oauth_metadata_url=oauth_metadata_url,
                    homepage=base,
                )
            )

    return tuple(entries)


# Endpoint-free default: ``()`` in EVERY edition. The internal edition's real
# entries are composed by ``resolve_curated_catalog`` (adapters layer) from the
# edition config; this constant exists so the module-level ``CURATED_CATALOG``
# below keeps its historical shape for callers that only need the static,
# edition-independent set.
INTERNAL_CURATED_CATALOG: tuple[CuratedCatalogEntry, ...] = (
    build_internal_curated_catalog()
)

# ---------------------------------------------------------------------------
# External (open-source) curated entries — shipped in BOTH editions.
# These are publicly available npm packages that run locally via npx.
# ---------------------------------------------------------------------------
EXTERNAL_CURATED_CATALOG: tuple[CuratedCatalogEntry, ...] = (
    # Filesystem MCP — local file read/write/search via npx.
    CuratedCatalogEntry(
        id="filesystem",
        name="Filesystem",
        description=(
            "Read, write, and search local files via MCP — exposes the directory "
            "you specify as a safe sandboxed workspace for the AI agent."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@modelcontextprotocol/server-filesystem", "<PATH>"),
        requires_args=("<PATH>",),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
    ),
    # Memory MCP — persistent key-value knowledge graph.
    CuratedCatalogEntry(
        id="memory",
        name="Memory",
        description=(
            "Persistent key-value memory for the AI agent — stores and retrieves "
            "facts across sessions using a local knowledge graph."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@modelcontextprotocol/server-memory"),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
    ),
    # Sequential Thinking MCP — structured multi-step reasoning.
    CuratedCatalogEntry(
        id="sequential-thinking",
        name="Sequential Thinking",
        description=(
            "Structured multi-step reasoning tool — breaks complex problems into "
            "sequential thought steps and lets the agent revise its reasoning."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@modelcontextprotocol/server-sequential-thinking"),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/sequentialthinking",
    ),
    # Everything MCP — local demo/test server with all capability types.
    CuratedCatalogEntry(
        id="everything",
        name="Everything",
        description=(
            "Reference MCP server exposing all capability types (tools, resources, "
            "prompts) — useful for testing MCP connectivity and client behaviour."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@modelcontextprotocol/server-everything"),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/everything",
    ),
    # Context7 MCP — up-to-date library documentation lookup.
    CuratedCatalogEntry(
        id="context7",
        name="Context7",
        description=(
            "Pull up-to-date documentation and code examples for any library "
            "directly into the AI's context — always current, never hallucinated."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@upstash/context7-mcp"),
        homepage="https://github.com/upstash/context7",
    ),
    # Playwright MCP — browser automation via Microsoft Playwright.
    CuratedCatalogEntry(
        id="playwright",
        name="Playwright",
        description=(
            "Browser automation via Microsoft Playwright — navigate pages, click, "
            "type, screenshot, and scrape content with a real Chromium browser."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@playwright/mcp@latest"),
        homepage="https://github.com/microsoft/playwright-mcp",
    ),
    # GitHub MCP — repository management via the GitHub API.
    CuratedCatalogEntry(
        id="github",
        name="GitHub",
        description=(
            "GitHub repository management via MCP — create issues, PRs, branches, "
            "search code, and manage files using the GitHub API. "
            "Requires a GitHub Personal Access Token."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@modelcontextprotocol/server-github"),
        env_schema=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        env_required=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        secret_fields=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/github",
    ),
    # Brave Search MCP — web and local search via the Brave Search API.
    CuratedCatalogEntry(
        id="brave-search",
        name="Brave Search",
        description=(
            "Web and local search powered by the Brave Search API — fetch real-time "
            "search results without tracking. Requires a Brave Search API key."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@modelcontextprotocol/server-brave-search"),
        env_schema=("BRAVE_API_KEY",),
        env_required=("BRAVE_API_KEY",),
        secret_fields=("BRAVE_API_KEY",),
        homepage="https://github.com/modelcontextprotocol/servers/tree/main/src/brave-search",
    ),
    # Tavily Search MCP — AI-optimised web search via the Tavily API.
    CuratedCatalogEntry(
        id="tavily",
        name="Tavily",
        description=(
            "AI-optimised web search via the Tavily Search API — retrieves "
            "relevant, clean search results ideal for RAG pipelines. "
            "Requires a Tavily API key."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "tavily-mcp@0.1.4"),
        env_schema=("TAVILY_API_KEY",),
        env_required=("TAVILY_API_KEY",),
        secret_fields=("TAVILY_API_KEY",),
        homepage="https://github.com/tavily-ai/tavily-mcp",
    ),
    # Elasticsearch MCP — query and manage Elasticsearch indices.
    CuratedCatalogEntry(
        id="elasticsearch",
        name="Elasticsearch",
        description=(
            "Query and manage Elasticsearch indices via MCP — search documents, "
            "inspect mappings, and run aggregations. "
            "Requires ES_URL (and optionally ES_USERNAME / ES_PASSWORD)."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@elastic/mcp-server-elasticsearch"),
        env_schema=("ES_URL", "ES_USERNAME", "ES_PASSWORD"),
        env_required=("ES_URL",),
        secret_fields=("ES_PASSWORD",),
        homepage="https://github.com/elastic/mcp-server-elasticsearch",
    ),
)

# ---------------------------------------------------------------------------
# CURATED_CATALOG — the full combined catalog for internal builds.
# External builds use EXTERNAL_CURATED_CATALOG only (see mcp_client.py).
# ---------------------------------------------------------------------------
CURATED_CATALOG: tuple[CuratedCatalogEntry, ...] = (
    INTERNAL_CURATED_CATALOG + EXTERNAL_CURATED_CATALOG
)


def get_catalog_entry(entry_id: str) -> CuratedCatalogEntry | None:
    """Return the curated entry with ``id == entry_id`` (or ``None``)."""
    for entry in CURATED_CATALOG:
        if entry.id == entry_id:
            return entry
    return None
