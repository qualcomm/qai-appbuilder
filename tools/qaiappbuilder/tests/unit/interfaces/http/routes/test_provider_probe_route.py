# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``POST /api/model-catalog/providers/probe`` - tests an unsaved URL + key."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from interfaces.http.routes.model_catalog import build_router
from qai.model_catalog.application.ports import ProviderProbeResult


class _Probe:
    def __init__(self, result: ProviderProbeResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str | None]] = []

    async def probe(self, *, base_url: str, api_key: str | None):
        self.calls.append((base_url, api_key))
        return self.result


def _client(result: ProviderProbeResult):
    probe = _Probe(result)
    container = SimpleNamespace(
        model_catalog=SimpleNamespace(
            provider_probe=probe, suggest_context_length=lambda m: 1000 + len(m)
        )
    )
    app = FastAPI()
    app.include_router(build_router(container=container))
    return TestClient(app), probe


def _post(client, **body):
    r = client.post("/api/model-catalog/providers/probe", json=body)
    assert r.status_code == 200
    return r.json()


def test_success_returns_models_and_suggested_context() -> None:
    client, probe = _client(ProviderProbeResult(ok=True, status=200, model_ids=("a", "bb")))
    body = _post(client, base_url=" https://x.test/v1 ", api_key=" k ")
    assert body["ok"] is True and body["model_ids"] == ["a", "bb"]
    assert body["context_lengths"] == {"a": 1001, "bb": 1002}
    assert probe.calls == [("https://x.test/v1", "k")]


@pytest.mark.parametrize(
    "result,code",
    [
        (ProviderProbeResult(ok=False, status=401, error="HTTP 401"), "unauthorized"),
        (ProviderProbeResult(ok=False, status=403, error="HTTP 403"), "unauthorized"),
        (ProviderProbeResult(ok=False, status=404, error="HTTP 404"), "not_found"),
        (ProviderProbeResult(ok=False, status=500, error="HTTP 500"), "http_error"),
        (ProviderProbeResult(ok=False, error="transport error: boom"), "unreachable"),
    ],
)
def test_failures_have_stable_codes(result, code) -> None:
    client, _ = _client(result)
    body = _post(client, base_url="https://x.test", api_key="k")
    assert body["ok"] is False and body["code"] == code and body["model_ids"] == []


def test_invalid_url_never_reaches_the_probe() -> None:
    client, probe = _client(ProviderProbeResult(ok=True, model_ids=("a",)))
    assert _post(client, base_url="not a url")["code"] == "invalid_url"
    assert probe.calls == []


def test_empty_key_is_sent_as_none() -> None:
    client, probe = _client(ProviderProbeResult(ok=True, model_ids=()))
    _post(client, base_url="https://x.test", api_key="  ")
    assert probe.calls[0][1] is None
