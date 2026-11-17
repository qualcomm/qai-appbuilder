# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``--provider-file`` overlay: loading, precedence and credential handling."""

from __future__ import annotations

import json
import logging

import pytest

from apps.api._model_resolver_bridge import ModelCatalogProviderLookupBridge
from apps.api._provider_file_overlay import (
    ProviderFileError,
    ProviderOverlay,
    load_provider_file,
)

pytestmark = pytest.mark.unit

_SECRET = "sk-file-secret-123456"


def _write(tmp_path, payload) -> str:
    path = tmp_path / "providers.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


def _provider(**over):
    base = {
        "provider_id": "qgenie",
        "base_url": "https://qgenie-api.qualcomm.com/v1",
        "api_key": _SECRET,
        "models": [{"model_id": "azure::gpt-6-luna", "context_length": 128000}],
    }
    base.update(over)
    return base


class _Registry:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = 0

    async def list_provider_configs(self):
        self.calls += 1
        return list(self.rows)


class _Store:
    def __init__(self, values):
        self.values = values

    def exists(self, service, key):
        return (service, key) in self.values

    def get(self, service, key):
        return self.values[(service, key)]


# --------------------------------------------------------------------- loading
def test_loads_provider_with_double_colon_model_id(tmp_path):
    overlay = load_provider_file(_write(tmp_path, {"providers": [_provider()]}))

    assert isinstance(overlay, ProviderOverlay)
    assert overlay.rows()[0]["config"]["models"][0]["model_id"] == "azure::gpt-6-luna"
    assert overlay.api_key_for("qgenie") == _SECRET


def test_env_reference_is_resolved_from_environment(tmp_path):
    path = _write(tmp_path, {"providers": [_provider(api_key="${MY_KEY}")]})

    overlay = load_provider_file(path, environ={"MY_KEY": "from-env"})

    assert overlay.api_key_for("qgenie") == "from-env"


def test_unset_env_reference_is_an_error_not_a_missing_key(tmp_path):
    path = _write(tmp_path, {"providers": [_provider(api_key="${MY_KEY}")]})

    with pytest.raises(ProviderFileError, match="MY_KEY"):
        load_provider_file(path, environ={})


@pytest.mark.parametrize(
    "payload, message",
    [
        ({}, "non-empty 'providers'"),
        ({"providers": []}, "non-empty 'providers'"),
        ({"providers": [_provider(provider_id="")]}, "provider_id"),
        ({"providers": [_provider(base_url="")]}, "base_url"),
        ({"providers": [_provider(models=[])]}, "models"),
        ({"providers": [_provider(models=[{"model_id": "m"}])]}, "context_length"),
        (
            {"providers": [_provider(models=[{"model_id": "m", "context_length": 0}])]},
            "context_length",
        ),
        ({"providers": [_provider(), _provider()]}, "duplicate provider_id"),
        (
            {
                "providers": [
                    _provider(provider_id="a"),
                    _provider(provider_id="b"),
                ]
            },
            "listed by both",
        ),
    ],
)
def test_invalid_files_are_rejected_with_a_specific_message(tmp_path, payload, message):
    with pytest.raises(ProviderFileError, match=message):
        load_provider_file(_write(tmp_path, payload))


def test_missing_and_malformed_files_are_rejected(tmp_path):
    with pytest.raises(ProviderFileError, match="cannot read"):
        load_provider_file(tmp_path / "nope.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ProviderFileError, match="not valid JSON"):
        load_provider_file(bad)


def test_error_messages_and_repr_never_contain_the_key(tmp_path):
    overlay = load_provider_file(_write(tmp_path, {"providers": [_provider()]}))
    assert _SECRET not in repr(overlay.providers[0])
    assert _SECRET not in str(overlay.providers[0])

    with pytest.raises(ProviderFileError) as exc:
        load_provider_file(_write(tmp_path, {"providers": [_provider(base_url="")]}))
    assert _SECRET not in str(exc.value)


# ---------------------------------------------------------------------- bridge
@pytest.mark.asyncio
async def test_overlay_provider_routes_the_model_with_its_own_key(tmp_path):
    overlay = load_provider_file(_write(tmp_path, {"providers": [_provider()]}))
    bridge = ModelCatalogProviderLookupBridge(
        provider_registry=_Registry(), secret_store=None, provider_overlay=overlay
    )

    endpoint = await bridge.lookup_for_model("azure::gpt-6-luna")

    assert endpoint is not None
    assert endpoint.provider_id == "qgenie"
    assert endpoint.base_url == "https://qgenie-api.qualcomm.com/v1"
    assert endpoint.api_key == _SECRET
    assert await bridge.context_window("azure::gpt-6-luna") == 128000


@pytest.mark.asyncio
async def test_overlay_wins_over_a_saved_provider_for_the_same_model(tmp_path):
    saved = {
        "provider_id": "saved",
        "config": {
            "base_url": "https://saved.example/v1",
            "models": [{"model_id": "azure::gpt-6-luna", "context_length": 8}],
        },
    }
    overlay = load_provider_file(_write(tmp_path, {"providers": [_provider()]}))
    bridge = ModelCatalogProviderLookupBridge(
        provider_registry=_Registry([saved]), secret_store=None, provider_overlay=overlay
    )

    endpoint = await bridge.lookup_for_model("azure::gpt-6-luna")

    assert endpoint.provider_id == "qgenie"


@pytest.mark.asyncio
async def test_models_not_in_the_overlay_still_use_saved_providers(tmp_path):
    saved = {
        "provider_id": "saved",
        "config": {
            "base_url": "https://saved.example/v1",
            "models": [{"model_id": "other-model", "context_length": 1000}],
        },
    }
    store = _Store({("qai.model_catalog.provider", "saved"): "saved-key"})
    overlay = load_provider_file(_write(tmp_path, {"providers": [_provider()]}))
    bridge = ModelCatalogProviderLookupBridge(
        provider_registry=_Registry([saved]), secret_store=store, provider_overlay=overlay
    )

    endpoint = await bridge.lookup_for_model("other-model")

    assert endpoint.provider_id == "saved"
    assert endpoint.api_key == "saved-key"


@pytest.mark.asyncio
async def test_overlay_provider_never_falls_back_to_a_stored_key_for_the_same_id(tmp_path):
    # A stored key for provider id "qgenie" must not be sent to the file's endpoint.
    store = _Store({("qai.model_catalog.provider", "qgenie"): "STORED-KEY"})
    overlay = load_provider_file(_write(tmp_path, {"providers": [_provider(api_key=None)]}))
    bridge = ModelCatalogProviderLookupBridge(
        provider_registry=_Registry(), secret_store=store, provider_overlay=overlay
    )

    endpoint = await bridge.lookup_for_model("azure::gpt-6-luna")

    assert endpoint.api_key is None


@pytest.mark.asyncio
async def test_without_an_overlay_behaviour_is_unchanged():
    saved = {
        "provider_id": "saved",
        "config": {
            "base_url": "https://saved.example/v1",
            "models": [{"model_id": "m", "context_length": 5}],
        },
    }
    store = _Store({("qai.model_catalog.provider", "saved"): "saved-key"})
    registry = _Registry([saved])
    bridge = ModelCatalogProviderLookupBridge(provider_registry=registry, secret_store=store)

    endpoint = await bridge.lookup_for_model("m")

    assert (endpoint.provider_id, endpoint.api_key) == ("saved", "saved-key")
    assert await bridge.context_window("m") == 5
    assert registry.calls == 2


@pytest.mark.asyncio
async def test_overlay_is_memory_only_nothing_written_to_the_secret_store(tmp_path):
    class _WriteTrap(_Store):
        def set(self, *a, **k):  # pragma: no cover - must not be called
            raise AssertionError("overlay must not write to the secret store")

        put = delete = set

    overlay = load_provider_file(_write(tmp_path, {"providers": [_provider()]}))
    bridge = ModelCatalogProviderLookupBridge(
        provider_registry=_Registry(), secret_store=_WriteTrap({}), provider_overlay=overlay
    )

    await bridge.lookup_for_model("azure::gpt-6-luna")


@pytest.mark.asyncio
async def test_key_is_not_logged_during_lookup(tmp_path, caplog):
    overlay = load_provider_file(_write(tmp_path, {"providers": [_provider()]}))
    bridge = ModelCatalogProviderLookupBridge(
        provider_registry=_Registry(), secret_store=None, provider_overlay=overlay
    )
    with caplog.at_level(logging.DEBUG):
        await bridge.lookup_for_model("azure::gpt-6-luna")
        await bridge.context_window("azure::gpt-6-luna")

    assert _SECRET not in caplog.text
