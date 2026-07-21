# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Fetches available models from an upstream OpenAI-compatible endpoint."""
from __future__ import annotations

import time
from typing import Any

import httpx


class UpstreamModelFetcher:
    """Fetches and caches model lists from an upstream API.

    Supports 3 response formats:

    1. OpenAI / Anthropic standard:
       ``{"data": [{"id": "model-name", ...}]}``

    2. Internal LLM gateway:
       ``{"models": [{"model_name": "claude-4-6-sonnet", "name": [...], ...}]}``

    3. Simple list (rare):
       ``[{"id": "model-1"}, ...]``

    Usage::

        fetcher = UpstreamModelFetcher(base_url="https://api.example.com/v1", api_key="sk-...")
        models = await fetcher.fetch_models()
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None = None,
        cache_ttl_seconds: float = 300.0,
        timeout: float = 8.0,
        verify_ssl: bool = False,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._cache_ttl = cache_ttl_seconds
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        # Cache state
        self._cached_models: list[dict[str, Any]] | None = None
        self._cached_at: float = 0.0

    async def fetch_models(self, *, force_refresh: bool = False) -> list[dict[str, Any]]:
        """Fetch models from upstream. Uses cache if fresh.

        Returns a normalized list of model dicts with at least ``id`` and
        ``object`` keys.
        """
        now = time.monotonic()
        if (
            not force_refresh
            and self._cached_models is not None
            and (now - self._cached_at) < self._cache_ttl
        ):
            return self._cached_models

        raw = await self._request_models()
        models = self._normalize(raw)
        self._cached_models = models
        self._cached_at = time.monotonic()
        return models

    def invalidate_cache(self) -> None:
        """Invalidate the cached model list."""
        self._cached_models = None
        self._cached_at = 0.0

    def cache_age_seconds(self) -> float | None:
        """Return the age (seconds) of the cached fetch, or ``None``.

        Used by the Claude Code adapter to distinguish a fresh upstream
        hit from a cache hit when populating the V1-parity model-source
        badge (``upstream`` vs ``cache``).  Returns ``None`` when no
        successful fetch has been cached yet.
        """
        if self._cached_models is None or self._cached_at <= 0.0:
            return None
        return time.monotonic() - self._cached_at

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _request_models(self) -> Any:
        """Make the HTTP GET to the upstream /models endpoint."""
        url = f"{self._base_url}/models"
        headers: dict[str, str] = {
            "User-Agent": "QAIModelBuilder/2.0",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
            # Also send x-api-key for Anthropic-style endpoints
            headers["x-api-key"] = self._api_key

        try:
            async with httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                verify=self._verify_ssl,
            ) as client:
                resp = await client.get(url, headers=headers)
        except httpx.TimeoutException as exc:
            raise RuntimeError(f"Upstream timeout fetching models: {exc}") from exc
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Upstream network error: {exc}") from exc

        if resp.status_code in (401, 403):
            raise RuntimeError(f"Upstream auth failed: HTTP {resp.status_code}")
        if resp.status_code == 404:
            raise RuntimeError(
                "Upstream endpoint not found (HTTP 404): "
                "the server may not implement /models"
            )
        if resp.status_code >= 400:
            snippet = (resp.text or "")[:200]
            raise RuntimeError(f"Upstream HTTP {resp.status_code}: {snippet}")

        try:
            return resp.json()
        except Exception as exc:
            raise RuntimeError(f"Upstream returned non-JSON response: {exc}") from exc

    def _normalize(self, body: Any) -> list[dict[str, Any]]:
        """Normalize various upstream response shapes into a standard list."""
        raw_items: list[Any]
        is_internal_gw = False

        if isinstance(body, list):
            # Format 3: bare list
            raw_items = body
        elif isinstance(body, dict):
            # Format 1: {"data": [...]} (OpenAI / Anthropic official)
            data = body.get("data")
            if isinstance(data, list):
                raw_items = data
            else:
                # Format 2: {"models": [...]} (internal LLM gateway)
                models = body.get("models")
                if isinstance(models, list):
                    raw_items = models
                    is_internal_gw = True
                else:
                    raise RuntimeError(
                        "Upstream response missing 'data' or 'models' array"
                    )
        else:
            raise RuntimeError("Upstream response is not a JSON object or array")

        results: list[dict[str, Any]] = []
        seen: set[str] = set()

        for item in raw_items:
            if not isinstance(item, dict):
                continue

            if is_internal_gw:
                model_id = self._extract_internal_gw_id(item)
            else:
                model_id = self._extract_standard_id(item)

            if not model_id or model_id in seen:
                continue
            seen.add(model_id)

            results.append({
                "id": model_id,
                "object": "model",
            })

        return results

    @staticmethod
    def _extract_standard_id(item: dict[str, Any]) -> str | None:
        """Extract model id from OpenAI/Anthropic format."""
        mid = item.get("id")
        if isinstance(mid, str) and mid.strip():
            return mid.strip()
        return None

    @staticmethod
    def _extract_internal_gw_id(item: dict[str, Any]) -> str | None:
        """Extract model id from internal LLM gateway format.

        Only keeps chat-capable models.
        """
        mt = item.get("model_type")
        if not (isinstance(mt, dict) and mt.get("is_chat")):
            return None
        mid = item.get("model_name")
        if isinstance(mid, str) and mid.strip():
            return mid.strip()
        return None
