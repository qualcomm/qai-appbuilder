# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""The probe appends ``/v1`` only to a bare origin, never over an existing version."""

from __future__ import annotations

import httpx
import pytest

from qai.model_catalog.infrastructure.http_provider_probe import HttpProviderProbe


def _probe(seen: list[str]) -> HttpProviderProbe:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    return HttpProviderProbe(
        client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base,expected",
    [
        ("https://api.deepseek.com", "https://api.deepseek.com/v1/models"),
        ("https://api.deepseek.com/v1", "https://api.deepseek.com/v1/models"),
        ("https://open.bigmodel.cn/api/paas/v4", "https://open.bigmodel.cn/api/paas/v4/models"),
        ("https://dashscope.aliyuncs.com/compatible-mode/v1/", "https://dashscope.aliyuncs.com/compatible-mode/v1/models"),
    ],
)
async def test_models_url(base: str, expected: str) -> None:
    seen: list[str] = []
    result = await _probe(seen).probe(base_url=base, api_key="k")
    assert seen == [expected] and result.ok and result.model_ids == ("m1",)
