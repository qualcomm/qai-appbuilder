# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Context-size lookup waits (bounded) for the daemon's model to finish loading.

``load-model`` returns once the process is spawned; the daemon answers
``POST /contextsize`` with 0 until ``GET /status`` reports ``loading ==
false``. Port of the a63d4f26 additions to the internal
``test_local_model_context_window_lookup.py`` (the stubs below are the minimal
subset those tests need).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from apps.api import _model_resolver_bridge
from apps.api._model_resolver_bridge import ModelCatalogProviderLookupBridge

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _ModelInfo:
    """Shape of ``qai.model_runtime`` ``ModelInfo`` the bridge reads."""

    def __init__(self, name: str, context_length: int | None) -> None:
        self.name = name
        self.context_length = context_length


class _LocalModels:
    """``ListModelsUseCase`` seam: the on-disk registry scan result."""

    def __init__(self, *models: _ModelInfo) -> None:
        self._models = list(models)

    async def execute(self, *, models_root: str | None = None) -> list[_ModelInfo]:
        return list(self._models)


class _ProviderRegistry:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def list_provider_configs(self) -> list[dict[str, Any]]:
        return list(self._rows)


class _SecretStore:
    def exists(self, service: str, key: str) -> bool:
        return True

    def get(self, service: str, key: str) -> str:
        return "test-key"


class _RunningLocalModels(_LocalModels):
    """Registry stub plus the ``running_model`` seam (a script of answers; the last repeats)."""

    def __init__(self, *models: _ModelInfo, running: list[tuple[str, int] | None]) -> None:
        super().__init__(*models)
        self._running = list(running)

    async def running_model(self) -> tuple[str, int] | None:
        return self._running.pop(0) if len(self._running) > 1 else self._running[0]


class _Daemon:
    """Scripted GenieAPIService ``/status`` + ``/contextsize`` behind ``httpx.AsyncClient``."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        status: list[Any],
        contextsize: list[int],
    ) -> None:
        self.status = list(status)
        self.contextsize = list(contextsize)
        self.events: list[str] = []
        real_client = httpx.AsyncClient
        transport = httpx.MockTransport(self._handler)
        monkeypatch.setattr(
            httpx, "AsyncClient", lambda **kw: real_client(transport=transport, **kw),
        )

    def _handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/status":
            self.events.append("status")
            answer = self.status.pop(0) if len(self.status) > 1 else self.status[0]
            if isinstance(answer, int):
                return httpx.Response(answer)
            return httpx.Response(200, json={"loading": answer})
        assert request.url.path == "/contextsize"
        self.events.append("contextsize")
        value = self.contextsize.pop(0) if len(self.contextsize) > 1 else self.contextsize[0]
        return httpx.Response(200, json={"contextsize": value})


def _ready_bridge(
    monkeypatch: pytest.MonkeyPatch,
    running: list[tuple[str, int] | None],
) -> ModelCatalogProviderLookupBridge:
    monkeypatch.setattr(_model_resolver_bridge, "_MODEL_READY_POLL_S", 0.001)
    return ModelCatalogProviderLookupBridge(
        provider_registry=_ProviderRegistry([]),
        secret_store=_SecretStore(),
        local_models=_RunningLocalModels(_ModelInfo("Qwen3-8B", 4096), running=running),
    )


async def test_running_context_size_waits_while_status_loading(monkeypatch) -> None:
    daemon = _Daemon(monkeypatch, status=["true", "true", "false"], contextsize=[8192])
    bridge = _ready_bridge(monkeypatch, [("qwen3-8b", 18989)])

    assert await bridge.context_window("local::Qwen3-8B") == 8192
    assert daemon.events == ["status", "status", "status", "contextsize"]


async def test_running_context_size_retries_zero_after_loading_done(monkeypatch) -> None:
    daemon = _Daemon(monkeypatch, status=["false"], contextsize=[0, 8192])
    bridge = _ready_bridge(monkeypatch, [("Qwen3-8B", 18989)])

    assert await bridge.context_window("local::Qwen3-8B") == 8192
    assert daemon.events.count("contextsize") == 2


async def test_running_context_size_stops_when_model_stops_running(monkeypatch) -> None:
    daemon = _Daemon(monkeypatch, status=["true"], contextsize=[8192])
    bridge = _ready_bridge(
        monkeypatch, [("Qwen3-8B", 18989), ("Qwen3-8B", 18989), None],
    )
    monkeypatch.setattr(_model_resolver_bridge, "_MODEL_READY_TIMEOUT_S", 60.0)

    assert await bridge.context_window("local::Qwen3-8B") == 4096
    assert daemon.events == ["status", "status"]


async def test_running_context_size_legacy_service_without_status_tries_once(monkeypatch) -> None:
    daemon = _Daemon(monkeypatch, status=[404], contextsize=[8192])
    bridge = _ready_bridge(monkeypatch, [("Qwen3-8B", 18989)])

    assert await bridge.context_window("local::Qwen3-8B") == 8192
    assert daemon.events == ["status", "contextsize"]


async def test_running_context_size_legacy_service_zero_falls_back_without_polling(
    monkeypatch,
) -> None:
    daemon = _Daemon(monkeypatch, status=[404], contextsize=[0])
    bridge = _ready_bridge(monkeypatch, [("Qwen3-8B", 18989)])
    monkeypatch.setattr(_model_resolver_bridge, "_MODEL_READY_TIMEOUT_S", 60.0)

    assert await bridge.context_window("local::Qwen3-8B") == 4096
    assert daemon.events == ["status", "contextsize"]


async def test_running_context_size_times_out_while_loading_and_falls_back(monkeypatch) -> None:
    daemon = _Daemon(monkeypatch, status=["true"], contextsize=[8192])
    bridge = _ready_bridge(monkeypatch, [("Qwen3-8B", 18989)])
    monkeypatch.setattr(_model_resolver_bridge, "_MODEL_READY_TIMEOUT_S", 0.05)

    assert await bridge.context_window("local::Qwen3-8B") == 4096
    assert "contextsize" not in daemon.events
