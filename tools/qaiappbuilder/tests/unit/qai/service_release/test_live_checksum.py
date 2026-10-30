from __future__ import annotations

import httpx
import pytest

from qai.service_release.infrastructure.download_engine import (
    _resolve_live_checksum,
)

HF_URL = "https://huggingface.co/o/r/resolve/main/m.gguf"
LIVE = "d" * 64
CATALOG = "a" * 64


def _factory(handler):
    def make(**kwargs):
        kwargs.pop("proxy", None)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    return make


@pytest.mark.asyncio
async def test_live_checksum_overrides_stale_catalog_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "HEAD"
        return httpx.Response(
            302,
            headers={"x-linked-etag": f'"{LIVE}"', "location": "https://cdn/x"},
        )

    got = await _resolve_live_checksum(_factory(handler), HF_URL, CATALOG)
    assert got == LIVE


@pytest.mark.asyncio
async def test_lookup_failure_falls_back_to_catalog_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    got = await _resolve_live_checksum(_factory(handler), HF_URL, CATALOG)
    assert got == CATALOG


@pytest.mark.asyncio
async def test_missing_or_malformed_etag_falls_back() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"x-linked-etag": "not-a-sha"})

    got = await _resolve_live_checksum(_factory(handler), HF_URL, CATALOG)
    assert got == CATALOG


@pytest.mark.asyncio
async def test_non_huggingface_url_makes_no_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be called")

    got = await _resolve_live_checksum(
        _factory(handler), "https://example.com/resolve/m.gguf", CATALOG
    )
    assert got == CATALOG
