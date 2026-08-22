# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Registry that selects a :class:`SearchProviderPort` by id.

The ``web_search`` tool hands a query (and an optional ``provider`` id) to this
registry; the registry resolves the backend and runs it. A missing / unknown
provider id is an **explicit error** (not a silent empty result) so the model
sees a clear "no such provider" message and can correct itself — silently
returning ``[]`` would masquerade as "searched, found nothing".

Adding a new search backend = ``register(provider_id, provider)`` one more
implementation; nothing else changes.

Layering: stdlib + the sibling :mod:`ports` only. No ``qai.<context>`` import.
"""

from __future__ import annotations

from qai.platform.web_search.ports import (
    SearchProviderPort,
    SearchResult,
)

__all__ = ["SearchProviderRegistry", "UnknownSearchProviderError"]


class UnknownSearchProviderError(LookupError):
    """Raised when a requested provider id is not registered.

    Carries the offending id + the set of known ids so the tool handler can
    render an actionable message ("provider 'foo' not found; available: …").
    """

    def __init__(self, provider_id: str, available: list[str]) -> None:
        self.provider_id = provider_id
        self.available = available
        avail = ", ".join(available) if available else "(none registered)"
        super().__init__(
            f"search provider {provider_id!r} is not registered "
            f"(available: {avail})"
        )


class SearchProviderRegistry:
    """A ``{provider_id: SearchProviderPort}`` map with a default provider.

    The first provider registered becomes the default unless an explicit
    ``default`` id is set. ``search`` dispatches to the chosen provider and
    raises :class:`UnknownSearchProviderError` for an unknown / empty registry
    rather than returning an empty list.
    """

    __slots__ = ("_default_id", "_providers")

    def __init__(self) -> None:
        self._providers: dict[str, SearchProviderPort] = {}
        self._default_id: str | None = None

    def register(
        self,
        provider_id: str,
        provider: SearchProviderPort,
        *,
        default: bool = False,
    ) -> None:
        """Register ``provider`` under ``provider_id``.

        The first registration (or any with ``default=True``) becomes the
        default used when a ``web_search`` call omits ``provider``.
        """
        if not provider_id:
            raise ValueError("provider_id must be a non-empty string")
        self._providers[provider_id] = provider
        if default or self._default_id is None:
            self._default_id = provider_id

    @property
    def default_id(self) -> str | None:
        return self._default_id

    def provider_ids(self) -> list[str]:
        return sorted(self._providers)

    def __len__(self) -> int:
        return len(self._providers)

    async def search(
        self,
        query: str,
        *,
        count: int = 5,
        provider: str | None = None,
        **kwargs: object,
    ) -> list[SearchResult]:
        """Resolve a provider and run the search.

        ``provider`` selects the backend; ``None`` uses the default. Raises
        :class:`UnknownSearchProviderError` when the requested (or default)
        provider is absent — the caller turns that into a clear tool error.
        """
        provider_id = provider or self._default_id
        if not provider_id or provider_id not in self._providers:
            raise UnknownSearchProviderError(
                provider_id or "<default>", self.provider_ids()
            )
        backend = self._providers[provider_id]
        return await backend.search(query, count=count, **kwargs)
