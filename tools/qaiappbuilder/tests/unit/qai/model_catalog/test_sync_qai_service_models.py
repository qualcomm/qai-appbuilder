# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Tests for ``SyncQaiServiceModelsUseCase``."""

from __future__ import annotations

from typing import Any

from qai.model_catalog.application.ports import ProviderProbeResult
from qai.model_catalog.application.use_cases.sync_qai_service_models import (
    SyncQaiServiceModelsUseCase,
)

_STORED_ENTRY: dict[str, Any] = {
    "model_id": "qai-service::stored-model",
    "name": "Stored Model",
    "api_model_id": "stored-model",
    "context_length": 128_000,
    "description": "previously synced",
    "supports_streaming": True,
}

_BASE_CONFIG: dict[str, Any] = {
    "base_url": "http://gateway.example.test:8012/v1",
    "models": [],
    "pinned": False,
}


class _FakeRegistry:
    """Minimal in-memory stand-in for ``ProviderRegistryPort``."""

    def __init__(self, config: dict[str, Any] | None) -> None:
        self.config = config
        self.saved: dict[str, Any] | None = None

    async def list_provider_configs(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def get_provider_config(self, provider_id: str) -> dict[str, Any] | None:
        assert provider_id == "qai-service"
        return self.config

    async def save_provider_config(
        self, provider_id: str, config: dict[str, Any]
    ) -> None:
        assert provider_id == "qai-service"
        self.saved = config
        self.config = config


class _FakeProbe:
    """Stand-in for ``ProviderProbePort`` returning a canned result."""

    def __init__(self, result: ProviderProbeResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str | None]] = []

    async def probe(
        self, *, base_url: str, api_key: str | None
    ) -> ProviderProbeResult:
        self.calls.append((base_url, api_key))
        return self.result


def _config_with(models: list[dict[str, Any]]) -> dict[str, Any]:
    return {**_BASE_CONFIG, "models": models}


async def test_no_jwt_is_noop() -> None:
    registry = _FakeRegistry(_config_with([dict(_STORED_ENTRY)]))
    probe = _FakeProbe(ProviderProbeResult(ok=True, model_ids=("qwen3.8-max",)))
    use_case = SyncQaiServiceModelsUseCase(
        registry=registry, probe=probe, get_jwt=lambda: None
    )

    result = await use_case.execute()

    assert result.skipped_reason == "no_jwt"
    assert result.is_noop()
    assert registry.saved is None
    assert not probe.calls


async def test_probe_failure_keeps_existing_config() -> None:
    registry = _FakeRegistry(_config_with([dict(_STORED_ENTRY)]))
    probe = _FakeProbe(ProviderProbeResult(ok=False, error="HTTP 401"))
    use_case = SyncQaiServiceModelsUseCase(
        registry=registry, probe=probe, get_jwt=lambda: "jwt-token"
    )

    result = await use_case.execute()

    assert result.skipped_reason == "probe_failed"
    assert registry.saved is None
    assert registry.config is not None
    assert registry.config["models"] == [dict(_STORED_ENTRY)]


async def test_empty_provider_gets_only_live_models_and_no_route1() -> None:
    registry = _FakeRegistry(_config_with([]))
    probe = _FakeProbe(
        ProviderProbeResult(ok=True, model_ids=("qwen3.8-max",))
    )
    use_case = SyncQaiServiceModelsUseCase(
        registry=registry, probe=probe, get_jwt=lambda: "jwt-token"
    )

    result = await use_case.execute()

    assert result.added == ("qwen3.8-max",)
    assert registry.saved is not None
    ids = [m["api_model_id"] for m in registry.saved["models"]]
    assert ids == ["qwen3.8-max"]  # nothing is injected beyond the live roster


async def test_new_model_gets_minimal_entry_with_unconstrained_params() -> None:
    registry = _FakeRegistry(_config_with([]))
    probe = _FakeProbe(
        ProviderProbeResult(ok=True, model_ids=("qwen3.8-max",))
    )
    use_case = SyncQaiServiceModelsUseCase(
        registry=registry, probe=probe, get_jwt=lambda: "jwt-token"
    )

    await use_case.execute()

    new_entry = registry.saved["models"][0]
    assert new_entry["model_id"] == "qai-service::qwen3.8-max"
    assert new_entry["api_model_id"] == "qwen3.8-max"
    assert new_entry["name"] == "qwen3.8-max"
    assert new_entry["context_length"] == 1_000_000
    assert new_entry["supports_streaming"] is True
    assert new_entry["params"] == {
        "temperature": {"supported": True},
        "top_p": {"supported": True},
        "max_tokens": {"supported": True, "max": 1_000_000},
    }


async def test_route1_is_an_ordinary_model_dropped_when_not_live() -> None:
    route1 = {
        "model_id": "qai-service::route-1",
        "name": "Route 1",
        "api_model_id": "route-1",
        "context_length": 1_000_000,
    }
    registry = _FakeRegistry(_config_with([route1]))
    probe = _FakeProbe(ProviderProbeResult(ok=True, model_ids=("qwen3.8-max",)))
    use_case = SyncQaiServiceModelsUseCase(
        registry=registry, probe=probe, get_jwt=lambda: "jwt-token"
    )

    result = await use_case.execute()

    assert result.removed == ("route-1",)
    assert [m["api_model_id"] for m in registry.saved["models"]] == ["qwen3.8-max"]


async def test_vanished_model_is_removed() -> None:
    stale_entry = {
        "model_id": "qai-service::old-model",
        "name": "old-model",
        "api_model_id": "old-model",
        "context_length": 1_000_000,
        "supports_streaming": True,
        "params": {"temperature": {"supported": True}},
    }
    registry = _FakeRegistry(_config_with([dict(stale_entry)]))
    probe = _FakeProbe(ProviderProbeResult(ok=True, model_ids=()))
    use_case = SyncQaiServiceModelsUseCase(
        registry=registry, probe=probe, get_jwt=lambda: "jwt-token"
    )

    result = await use_case.execute()

    assert result.removed == ("old-model",)  # sole stored model, not live
    assert registry.saved["models"] == []


async def test_kept_model_fields_are_preserved_untouched() -> None:
    hand_tuned_entry = {
        "model_id": "qai-service::qwen3.8-max",
        "name": "My Custom Label",
        "api_model_id": "qwen3.8-max",
        "context_length": 42,
        "description": "hand-edited by the user",
        "supports_streaming": True,
        "params": {"temperature": {"supported": False}},
    }
    registry = _FakeRegistry(_config_with([dict(hand_tuned_entry)]))
    probe = _FakeProbe(
        ProviderProbeResult(ok=True, model_ids=("qwen3.8-max",))
    )
    use_case = SyncQaiServiceModelsUseCase(
        registry=registry, probe=probe, get_jwt=lambda: "jwt-token"
    )

    result = await use_case.execute()

    assert result.is_noop()
    assert registry.saved is None
    assert registry.config["models"] == [dict(hand_tuned_entry)]


async def test_missing_provider_config_is_noop() -> None:
    registry = _FakeRegistry(None)
    probe = _FakeProbe(ProviderProbeResult(ok=True, model_ids=("qwen3.8-max",)))
    use_case = SyncQaiServiceModelsUseCase(
        registry=registry, probe=probe, get_jwt=lambda: "jwt-token"
    )

    result = await use_case.execute()

    assert result.skipped_reason == "provider_not_configured"
    assert not probe.calls
