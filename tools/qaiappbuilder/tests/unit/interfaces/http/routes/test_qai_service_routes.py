# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Opt-in QAI Service routes: connect / sync / disconnect + inert-by-default."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from interfaces.http.auth import qai_service_token as tok
from interfaces.http.routes import auth as auth_routes
from interfaces.http.routes.qai_service import build_router, hydrate_gateway_url


class _Registry:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    async def get_provider_config(self, pid: str):
        return self.rows.get(pid)

    async def save_provider_config(self, pid: str, cfg: dict[str, Any]) -> None:
        self.rows[pid] = dict(cfg)

    async def delete_provider_config(self, pid: str) -> None:
        self.rows.pop(pid, None)


class _Sync:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self):
        self.calls += 1
        return SimpleNamespace(
            ok=True, added=("m1",), removed=(), skipped_reason=None
        )


@pytest.fixture
def env():
    registry, sync = _Registry(), _Sync()
    container = SimpleNamespace(
        model_catalog=SimpleNamespace(
            provider_registry=registry, sync_qai_service_models_use_case=sync
        )
    )
    app = FastAPI()
    app.include_router(build_router(container=container))
    tok.set_gateway_url(None)
    tok.clear_service_jwt()
    yield TestClient(app), registry, sync, container
    tok.set_gateway_url(None)
    tok.clear_service_jwt()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("http://gw.test:8012", "http://gw.test:8012"),
        ("  https://gw.test/v1/ ", "https://gw.test"),
        ("gw.test:8012", ""),
        ("ftp://gw.test", ""),
        ("http://", ""),
        ("http://gw.test?x=1", ""),
        ("", ""),
    ],
)
def test_normalize(raw: str, expected: str) -> None:
    assert tok.normalize_gateway_url(raw) == expected


def test_connect_saves_provider_and_sets_holder(env) -> None:
    client, registry, _sync, _c = env
    r = client.post("/api/qai-service/connect", json={"url": "http://gw.test:8012/"})
    assert r.status_code == 200
    body = r.json()
    assert body["configured"] is True and body["needs_login"] is True
    assert registry.rows["qai-service"]["base_url"] == "http://gw.test:8012/v1"
    assert registry.rows["qai-service"]["models"] == []
    assert tok.get_gateway_url() == "http://gw.test:8012"


def test_connect_rejects_bad_url_without_side_effects(env) -> None:
    client, registry, _s, _c = env
    r = client.post("/api/qai-service/connect", json={"url": "not a url"})
    assert r.status_code == 422
    assert registry.rows == {}
    assert tok.get_gateway_url() is None


def test_connect_new_url_drops_old_jwt_same_url_keeps_models(env) -> None:
    client, registry, _s, _c = env
    client.post("/api/qai-service/connect", json={"url": "http://a.test"})
    registry.rows["qai-service"]["models"] = [{"api_model_id": "x"}]
    tok.store_service_jwt("jwt-a", 3600)
    client.post("/api/qai-service/connect", json={"url": "http://a.test"})
    assert tok.get_service_jwt() == "jwt-a"
    assert registry.rows["qai-service"]["models"] == [{"api_model_id": "x"}]
    client.post("/api/qai-service/connect", json={"url": "http://b.test"})
    assert tok.get_service_jwt() is None
    assert registry.rows["qai-service"]["models"] == []


def test_sync_delegates_to_use_case(env) -> None:
    client, _r, sync, _c = env
    r = client.post("/api/qai-service/sync")
    assert r.status_code == 200 and sync.calls == 1
    assert r.json()["added"] == ["m1"]


def test_disconnect_forgets_everything(env) -> None:
    client, registry, _s, _c = env
    client.post("/api/qai-service/connect", json={"url": "http://a.test"})
    tok.store_service_jwt("jwt-a", 3600)
    r = client.delete("/api/qai-service")
    assert r.status_code == 200 and r.json()["configured"] is False
    assert registry.rows == {} and tok.get_service_jwt() is None
    assert tok.get_gateway_url() == ""


@pytest.mark.asyncio
async def test_hydrate_from_provider_and_empty(env) -> None:
    _client, registry, _s, container = env
    await hydrate_gateway_url(container)
    assert tok.get_gateway_url() == ""
    registry.rows["qai-service"] = {"base_url": "http://gw.test:8012/v1", "models": []}
    await hydrate_gateway_url(container)
    assert tok.get_gateway_url() == "http://gw.test:8012"


def test_config_is_inert_until_url_is_held(env) -> None:
    # An empty holder (no provider entry) overrides ANY base_url left in a
    # pre-existing runtime user_config.toml, so an upgraded install stays inert.
    tok.set_gateway_url("")
    assert auth_routes._resolve_qai_service_config().base_url == ""
    tok.set_gateway_url("http://gw.test:8012")
    assert auth_routes._resolve_qai_service_config().base_url == "http://gw.test:8012"
    tok.set_gateway_url("")
    assert auth_routes._resolve_qai_service_config().base_url == ""


@pytest.mark.asyncio
async def test_exchange_is_a_noop_without_a_gateway_and_runs_with_one(
    env, monkeypatch
) -> None:
    calls: list[str] = []

    async def fake_exchange(id_token: str, *, base_url: str, **_kw: Any):
        calls.append(base_url)
        return "jwt-x", 3600

    monkeypatch.setattr(tok, "exchange_id_token", fake_exchange)

    tok.set_gateway_url("")
    await auth_routes._exchange_qai_service_token("id-token")
    assert calls == [] and tok.get_service_jwt() is None

    tok.set_gateway_url("http://gw.test:8012")
    await auth_routes._exchange_qai_service_token("id-token")
    assert calls == ["http://gw.test:8012"] and tok.get_service_jwt() == "jwt-x"
