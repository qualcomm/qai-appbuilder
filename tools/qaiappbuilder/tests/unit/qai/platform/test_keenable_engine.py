# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Keenable engine: public title header, keyed endpoint, result mapping."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from qai.platform.web_search.independent.engines.base import EngineQuery
from qai.platform.web_search.independent.engines.keenable import (
    KeenableEngine,
    _APP_TITLE,
    _KEYED_ENDPOINT,
    _PUBLIC_ENDPOINT,
)
from qai.platform.web_search.independent.errors import EngineBlockedError


class _FakeHttp:
    def __init__(self, payload: Any):
        self.payload = payload
        self.calls: list[dict[str, Any]] = []
        self.post = AsyncMock(side_effect=self._post)

    async def _post(self, url: str, **kwargs: Any):
        self.calls.append({"url": url, **kwargs})
        return SimpleNamespace(json=lambda: self.payload)


@pytest.mark.asyncio
async def test_public_endpoint_sends_title_header_without_a_key():
    http = _FakeHttp(
        {"results": [{"title": "A", "url": "https://a.example", "snippet": "body A"}]}
    )
    engine = KeenableEngine(http_client=http)  # type: ignore[arg-type]
    hits = await engine.search(EngineQuery(query="hello", count=5))
    assert hits[0].url == "https://a.example"
    assert hits[0].snippet == "body A"
    assert http.calls[0]["url"] == _PUBLIC_ENDPOINT
    assert http.calls[0]["headers"]["X-Keenable-Title"] == _APP_TITLE
    assert "X-API-Key" not in http.calls[0]["headers"]
    assert http.calls[0]["json"] == {"query": "hello", "max_results": 5}


@pytest.mark.asyncio
async def test_keyed_endpoint_sends_api_key():
    http = _FakeHttp({"results": []})
    engine = KeenableEngine(credential="secret-key", http_client=http)  # type: ignore[arg-type]
    await engine.search(EngineQuery(query="hello", count=3))
    assert http.calls[0]["url"] == _KEYED_ENDPOINT
    assert http.calls[0]["headers"]["X-API-Key"] == "secret-key"
    assert http.calls[0]["headers"]["X-Keenable-Title"] == _APP_TITLE


@pytest.mark.asyncio
async def test_non_result_envelope_is_a_block():
    http = _FakeHttp({"error": "missing_app_title"})
    engine = KeenableEngine(http_client=http)  # type: ignore[arg-type]
    with pytest.raises(EngineBlockedError):
        await engine.search(EngineQuery(query="hello", count=5))
