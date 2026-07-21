# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Thin use cases wrapping the MCP server registry (add / list / remove / test).

These are intentionally thin pass-throughs over
:class:`qai.chat.application.ports.McpServerRegistryPort`; they exist so the
HTTP route layer depends on an application use case (not the port adapter
directly) and so future cross-cutting logic (audit, quota) has a home without
touching routes.  Business rules (validation, secure-by-default gate) live in
the domain VO (:class:`qai.chat.domain.mcp_server.McpServerConfig`) and the
concrete registry adapter respectively.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from qai.chat.application.ports import (
    McpServerRegistryPort,
    McpServerStatus,
)
from qai.chat.domain.mcp_catalog import CuratedCatalogEntry

if TYPE_CHECKING:  # pragma: no cover
    from qai.chat.domain.mcp_server import McpServerConfig

__all__ = ["ManageMcpServersUseCase", "McpCatalogInstallError"]


class McpCatalogInstallError(ValueError):
    """Raised when a catalog install request is invalid.

    Maps to HTTP 400 (subclasses ``ValueError``, which the error middleware
    treats as a client error). Carries a human-readable reason (unknown entry,
    a missing required argument placeholder, etc.).
    """


@dataclass(slots=True)
class ManageMcpServersUseCase:
    """CRUD + connection-test + marketplace orchestration for MCP servers."""

    registry: McpServerRegistryPort

    async def list_servers(self) -> tuple[McpServerStatus, ...]:
        return await self.registry.list_servers()

    async def add_server(self, config: McpServerConfig) -> McpServerStatus:
        return await self.registry.add_server(config)

    async def remove_server(self, name: str) -> bool:
        return await self.registry.remove_server(name)

    async def test_server(self, name: str) -> McpServerStatus:
        return await self.registry.test_server(name)

    async def set_enabled(self, name: str, enabled: bool) -> McpServerStatus:
        """Flip one server's per-server ``enabled`` switch (on→connect, off→drop)."""
        return await self.registry.set_enabled(name, enabled)

    async def list_catalog(self) -> tuple[CuratedCatalogEntry, ...]:
        """Return the marketplace catalog (curated + already-cached registry).

        Browsable regardless of the execution gate and NEVER auto-fetches the
        network: returns the static curated source plus whatever dynamic
        registry entries are already cached. The dynamic source is fetched only
        by :meth:`refresh_catalog` (the user's explicit "load / refresh" click).
        """
        return await self.registry.list_catalog()

    async def refresh_catalog(self) -> tuple[CuratedCatalogEntry, ...]:
        """Fetch the dynamic official-registry source ON DEMAND, return catalog.

        Invoked when the user picks the "registry" source and clicks "load /
        refresh". Graceful on failure (degrades to curated + any prior cache).
        """
        return await self.registry.refresh_catalog()

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
        """Materialise a catalog entry into a server + connect it.

        Delegates resolution + config materialisation to the registry (which
        owns BOTH the curated and the cached dynamic-registry sources, so a
        registry entry — remote or keyed — is installable too). ``source``
        disambiguates a cross-source id collision. The registry substitutes
        ``<PLACEHOLDER>`` args (stdio), collects declared env / header values
        (routing secret HEADER values to the SecretStore; ENV values into the
        config env), enforces the entry's required arguments / env / headers
        (raising :class:`McpCatalogInstallError` → HTTP 400 on a missing one),
        and connects + persists through the SAME three-way gate as
        :meth:`add_server`.
        """
        return await self.registry.install_from_catalog(
            entry_id,
            name=name,
            arg_values=arg_values,
            env_values=env_values,
            header_values=header_values,
            source=source,
        )

    def global_enabled(self) -> bool:
        """Return the GLOBAL master switch state (the single truth source)."""
        return self.registry.global_enabled()

    async def set_global_enabled(self, enabled: bool) -> None:
        """Flip the GLOBAL master switch (on→connect all, off→disconnect all).

        Persists the new state (single truth source) and re-applies it to every
        configured server through the registry's real connect path.
        """
        await self.registry.set_global_enabled(enabled)

    async def browse_registry(
        self,
        *,
        search: str | None = None,
        cursor: str | None = None,
        limit: int = 30,
    ) -> tuple[tuple[CuratedCatalogEntry, ...], str | None]:
        """Browse ONE page of the dynamic official-registry source (user-driven).

        Delegates to the registry: reaches the network for the given ``search``
        (server-side ``name`` filter) + ``cursor``, returns ``(entries,
        next_cursor)``. Graceful — a failure returns ``((), None)`` and records
        the reason (never raises).
        """
        return await self.registry.browse_registry(
            search=search, cursor=cursor, limit=limit
        )
