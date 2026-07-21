# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Web-search tool handler (``web_search``).

Resolves a query (and optional ``provider`` id + ``count``) through an injected
search-provider registry and returns a structured, ranked result list. The
registry is the pluggable extension point (``qai.platform.edition.web_search``);
the registry / providers are constructed at the ``apps/api`` DI seam ONLY on
internal editions (``settings.is_internal``), so on external editions
``search_registry`` is ``None`` and this tool is not even registered (see
``registry.build_default_tool_handlers``).

This module deliberately does NOT import ``qai.platform.edition.web_search``:
that package is physically excluded from external artifacts. The handler speaks
to the registry purely through its ``search(...)`` duck-type, so a stripped
external tree never triggers an ImportError merely by importing this handler
module.
"""

from __future__ import annotations

from typing import Any

from qai.ai_coding.application.ports import FileGuardPort
from qai.ai_coding.infrastructure.tools.errors import ToolError
from qai.ai_coding.infrastructure.tools.handlers._shared import _ok

_DEFAULT_COUNT = 5
_MAX_COUNT = 50


async def tool_web_search(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
    search_registry: Any | None = None,
) -> dict[str, Any]:
    # ``file_guard`` is accepted for signature parity with the other tool
    # handlers; web_search performs no filesystem access.
    _ = file_guard

    if search_registry is None:
        # Defensive: the tool should not be registered at all when no registry
        # is wired (external edition). If it somehow is, fail clearly.
        raise ToolError(
            "web_search: no search provider is configured in this build"
        )

    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        raise ToolError("web_search: 'query' argument is required")

    count_raw = args.get("count")
    if count_raw is None:
        count = _DEFAULT_COUNT
    else:
        try:
            count = int(count_raw)
        except (TypeError, ValueError):
            count = _DEFAULT_COUNT
        if count <= 0:
            count = _DEFAULT_COUNT
        count = min(count, _MAX_COUNT)

    provider_raw = args.get("provider")
    provider = (
        provider_raw if isinstance(provider_raw, str) and provider_raw else None
    )

    try:
        results = await search_registry.search(
            query.strip(), count=count, provider=provider
        )
    except ToolError:
        raise
    except LookupError as exc:
        # Unknown / unregistered provider id — surface the registry's clear
        # "available providers" message rather than a silent empty result.
        raise ToolError(f"web_search: {exc}") from exc
    except Exception as exc:  # noqa: BLE001 — network / upstream failures
        raise ToolError(f"web_search: search failed: {exc}") from exc

    rendered = [
        {
            "title": getattr(r, "title", ""),
            "url": getattr(r, "url", ""),
            "snippet": getattr(r, "snippet", ""),
            "score": getattr(r, "score", None),
            "source": getattr(r, "source", ""),
        }
        for r in results
    ]

    provider_label = provider or "default"
    return _ok(
        f"web_search ok ({len(rendered)} result(s), provider={provider_label})",
        query=query.strip(),
        provider=provider_label,
        count=len(rendered),
        results=rendered,
    )
