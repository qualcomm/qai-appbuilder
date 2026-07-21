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

from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from qai.chat.domain.mcp_server import McpServerConfig, McpTransport

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

    from qai.chat.application.ports import McpServerStatus


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
    source: str | None = Field(default=None, max_length=32)


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


# ---- Helpers --------------------------------------------------------------


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
        result = await _use_case().test_server(name)
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
    ) -> McpCatalogBrowseResponse:
        """Browse ONE page of the dynamic registry source (search + paginate).

        User-driven network fetch: the search / "load more" action IS the
        consent. Returns this page's registry entries + the ``next_cursor`` for
        the following page (``None`` = last page). Graceful — a failed browse
        degrades to an empty page + a ``registry_error`` banner (never 5xx).
        """
        uc = _use_case()
        browse = getattr(uc, "browse_registry", None)
        if not callable(browse):
            return McpCatalogBrowseResponse(entries=[], next_cursor=None)
        entries, next_cursor = await browse(
            search=search or None,
            cursor=cursor or None,
            limit=max(1, min(int(limit or 30), 100)),
        )
        registry_error = ""
        err_fn = getattr(_registry(), "registry_source_error", None)
        if callable(err_fn):
            try:
                registry_error = str(err_fn() or "")
            except Exception:  # noqa: BLE001 — best-effort
                registry_error = ""
        return McpCatalogBrowseResponse(
            entries=[_catalog_entry_to_model(e) for e in entries],
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
            result = await _use_case().install_from_catalog(
                entry_id,
                name=body.name or None,
                arg_values=dict(body.arg_values or {}),
                env_values=dict(body.env_values or {}),
                header_values=dict(body.header_values or {}),
                source=body.source or None,
            )
        except McpCatalogInstallError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
            ) from exc
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

    return router


__all__ = ["build_router"]
