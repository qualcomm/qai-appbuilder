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
    "CuratedCatalogEntry",
    "McpInstallType",
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


# ---------------------------------------------------------------------------
# The phase-1 curated list.  All entries: packages + stdio + NO credentials.
# ---------------------------------------------------------------------------
_MCP_SERVERS_REPO = "https://github.com/modelcontextprotocol/servers"

CURATED_CATALOG: tuple[CuratedCatalogEntry, ...] = (
    CuratedCatalogEntry(
        id="filesystem",
        name="Filesystem",
        description=(
            "Read / write / search files under one or more directories you "
            "grant access to."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@modelcontextprotocol/server-filesystem", "<PATH>"),
        requires_args=("<PATH>",),
        homepage=f"{_MCP_SERVERS_REPO}/tree/main/src/filesystem",
    ),
    CuratedCatalogEntry(
        id="memory",
        name="Memory",
        description=(
            "A persistent knowledge-graph memory the model can write to and "
            "recall across turns."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@modelcontextprotocol/server-memory"),
        homepage=f"{_MCP_SERVERS_REPO}/tree/main/src/memory",
    ),
    CuratedCatalogEntry(
        id="sequential-thinking",
        name="Sequential Thinking",
        description=(
            "A structured step-by-step reasoning scratchpad tool for complex "
            "problem decomposition."
        ),
        install_type="npx",
        command="npx",
        args_template=(
            "-y",
            "@modelcontextprotocol/server-sequential-thinking",
        ),
        homepage=f"{_MCP_SERVERS_REPO}/tree/main/src/sequentialthinking",
    ),
    CuratedCatalogEntry(
        id="everything",
        name="Everything (reference)",
        description=(
            "The official reference server exercising every MCP feature "
            "(tools / resources / prompts) — great for testing."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@modelcontextprotocol/server-everything"),
        homepage=f"{_MCP_SERVERS_REPO}/tree/main/src/everything",
    ),
    # ── credential-free vendor-official npx servers (open the box, they work) ──
    CuratedCatalogEntry(
        id="playwright",
        name="Playwright",
        description=(
            "Browser automation for the model — navigate, click, type, "
            "screenshot, and read pages (Microsoft's official Playwright MCP)."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@playwright/mcp", "--headless"),
        homepage="https://github.com/microsoft/playwright-mcp",
    ),
    CuratedCatalogEntry(
        id="context7",
        name="Context7",
        description=(
            "Fetch up-to-date, version-specific documentation and code examples "
            "for libraries / frameworks so the model does not rely on stale "
            "knowledge (by Upstash)."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@upstash/context7-mcp"),
        homepage="https://github.com/upstash/context7",
    ),
    # ── credential-required npx servers (install dialog collects the key; the
    #    value is persisted in the config env — see ``secret_fields`` docs) ──
    CuratedCatalogEntry(
        id="github",
        name="GitHub",
        description=(
            "Operate GitHub repositories — search code, read / create issues "
            "and pull requests. Requires a GitHub personal access token."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@modelcontextprotocol/server-github"),
        env_schema=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        env_required=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        secret_fields=("GITHUB_PERSONAL_ACCESS_TOKEN",),
        homepage=f"{_MCP_SERVERS_REPO}/tree/main/src/github",
    ),
    CuratedCatalogEntry(
        id="brave-search",
        name="Brave Search",
        description=(
            "Web search via the Brave Search API (news, pages, and answers). "
            "Requires a Brave Search API key."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@modelcontextprotocol/server-brave-search"),
        env_schema=("BRAVE_API_KEY",),
        env_required=("BRAVE_API_KEY",),
        secret_fields=("BRAVE_API_KEY",),
        homepage=f"{_MCP_SERVERS_REPO}/tree/main/src/brave-search",
    ),
    CuratedCatalogEntry(
        id="tavily",
        name="Tavily",
        description=(
            "Web search + extraction tuned for research / deep-research agents. "
            "Requires a Tavily API key."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "tavily-mcp"),
        env_schema=("TAVILY_API_KEY",),
        env_required=("TAVILY_API_KEY",),
        secret_fields=("TAVILY_API_KEY",),
        homepage="https://github.com/tavily-ai/tavily-mcp",
    ),
    CuratedCatalogEntry(
        id="elasticsearch",
        name="Elasticsearch",
        description=(
            "Query an Elasticsearch cluster — list indices, inspect mappings, "
            "and search. Requires the cluster URL and an API key (by Elastic)."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@elastic/mcp-server-elasticsearch"),
        # ES_URL is a plain (non-secret) endpoint; ES_API_KEY is the secret.
        env_schema=("ES_URL", "ES_API_KEY"),
        env_required=("ES_URL", "ES_API_KEY"),
        secret_fields=("ES_API_KEY",),
        homepage="https://github.com/elastic/mcp-server-elasticsearch",
    ),
    CuratedCatalogEntry(
        id="confluence",
        name="Confluence",
        description=(
            "Read and manage Atlassian Confluence pages / spaces. Requires your "
            "Atlassian site name, account email, and an API token."
        ),
        install_type="npx",
        command="npx",
        args_template=("-y", "@aashari/mcp-server-atlassian-confluence"),
        # site name + email are plain identifiers; only the API token is secret.
        env_schema=(
            "ATLASSIAN_SITE_NAME",
            "ATLASSIAN_USER_EMAIL",
            "ATLASSIAN_API_TOKEN",
        ),
        env_required=(
            "ATLASSIAN_SITE_NAME",
            "ATLASSIAN_USER_EMAIL",
            "ATLASSIAN_API_TOKEN",
        ),
        secret_fields=("ATLASSIAN_API_TOKEN",),
        homepage="https://github.com/aashari/mcp-server-atlassian-confluence",
    ),
)


def get_catalog_entry(entry_id: str) -> CuratedCatalogEntry | None:
    """Return the curated entry with ``id == entry_id`` (or ``None``)."""
    for entry in CURATED_CATALOG:
        if entry.id == entry_id:
            return entry
    return None
