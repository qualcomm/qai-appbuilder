# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP-backed :class:`ProviderProbePort` (cloud provider connectivity test).

Issues a minimal ``GET {base_url}/v1/models`` (OpenAI-compatible) request to
verify an api_key / endpoint actually works, returning the reachable model
ids on success. Uses :mod:`httpx` (already a project dependency); the
transport stays behind the port so the use-case layer is transport-free.

Best-effort + non-raising: any transport / status error is reported as a
:class:`ProviderProbeResult` with ``ok=False`` and a human-readable ``error``
(the wizard surfaces it as a warning, never crashes the session).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from qai.model_catalog.application.ports import ProviderProbeResult

__all__ = ["HttpProviderProbe"]

_DEFAULT_TIMEOUT_S: float = 10.0


class HttpProviderProbe:
    """:class:`ProviderProbePort` backed by ``httpx.AsyncClient``."""

    __slots__ = ("_timeout_s", "_client_factory", "_ssl_verify_provider")

    def __init__(
        self,
        *,
        timeout_s: float = _DEFAULT_TIMEOUT_S,
        client_factory: Any | None = None,
        ssl_verify_provider: Callable[[], bool] | None = None,
    ) -> None:
        self._timeout_s = float(timeout_s)
        self._client_factory = client_factory
        # 缺口 fix — previously hardcoded ``verify=False``. Route through the
        # live Settings.ssl_verify provider so the global toggle governs the
        # connectivity probe; read at request time (hot-applies). When unset the
        # prior ``verify=False`` behaviour is preserved.
        self._ssl_verify_provider = ssl_verify_provider

    async def probe(
        self, *, base_url: str, api_key: str | None
    ) -> ProviderProbeResult:
        # The OpenAI-compatible models endpoint sits under ``/v1``. Operator
        # base_urls come in both flavours — with or without a trailing ``/v1``
        # (e.g. ``https://host/v1`` vs ``https://host``). Normalise to end at
        # ``/v1`` before appending ``/models`` so a base_url that already
        # carries ``/v1`` doesn't produce ``/v1/v1/models`` (→ 404). Mirrors
        # the CC adapter's ``_get_fetcher`` normalisation (claude_code.py).
        normalised = base_url.rstrip("/")
        if not normalised.endswith("/v1"):
            normalised = f"{normalised}/v1"
        url = f"{normalised}/models"
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
            # Anthropic-style providers use x-api-key; send both harmlessly.
            headers["x-api-key"] = api_key
        try:
            async with self._open_client() as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as exc:
            return ProviderProbeResult(
                ok=False, error=f"transport error: {exc}"
            )

        if resp.status_code >= 400:
            return ProviderProbeResult(
                ok=False,
                status=resp.status_code,
                error=f"HTTP {resp.status_code}",
            )
        return ProviderProbeResult(
            ok=True,
            status=resp.status_code,
            model_ids=_extract_model_ids(resp.text),
        )

    def _open_client(self) -> httpx.AsyncClient:
        if self._client_factory is not None:
            return self._client_factory()
        # Live read of the global SSL toggle (prior default preserved: no
        # provider → verify=False); a runtime toggle hot-applies per probe.
        verify = (
            self._ssl_verify_provider()
            if self._ssl_verify_provider is not None
            else False
        )
        return httpx.AsyncClient(timeout=self._timeout_s, verify=verify)


def _extract_model_ids(payload_text: str) -> tuple[str, ...]:
    """Best-effort parse of an OpenAI-style ``/v1/models`` body."""
    try:
        payload = json.loads(payload_text)
    except (json.JSONDecodeError, ValueError):
        return ()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return ()
    ids: list[str] = []
    for item in data:
        if isinstance(item, dict):
            mid = item.get("id") or item.get("model")
            if isinstance(mid, str) and mid:
                ids.append(mid)
    return tuple(ids)
