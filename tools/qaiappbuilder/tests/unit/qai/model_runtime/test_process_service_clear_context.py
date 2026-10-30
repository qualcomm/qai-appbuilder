# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Unit tests for ``ProcessBackedInferenceService.clear_context``.

Covers the re-entry guard (only one ``POST /clear`` in flight), the
not-running precondition, and that the guard resets after success/failure.
The HTTP client is faked; no daemon is spawned.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from qai.model_runtime.domain.enums import ServiceState
from qai.model_runtime.domain.errors import ServiceClearBusyError
from qai.model_runtime.infrastructure.process_service import (
    ProcessBackedInferenceService,
)
from qai.platform.errors import PreconditionFailedError

pytestmark = pytest.mark.unit


def _running_service(monkeypatch: pytest.MonkeyPatch) -> ProcessBackedInferenceService:
    svc = ProcessBackedInferenceService(install_dir="x", exe_name="GenieAPIService.exe")
    monkeypatch.setattr(svc, "_refresh_state", lambda: None)
    svc._state = ServiceState.RUNNING
    svc._port = 8910
    return svc


def _patch_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    gate: asyncio.Event | None = None,
    status: int = 200,
    raise_exc: Exception | None = None,
) -> list[str]:
    urls: list[str] = []

    class _FakeClient:
        def __init__(self, *a: Any, **k: Any) -> None:
            pass

        async def __aenter__(self) -> "_FakeClient":
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> httpx.Response:
            urls.append(url)
            if gate is not None:
                await gate.wait()
            if raise_exc is not None:
                raise raise_exc
            return httpx.Response(status, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return urls


async def test_clear_posts_to_daemon_and_resets_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _running_service(monkeypatch)
    urls = _patch_client(monkeypatch)

    await svc.clear_context()
    await svc.clear_context()  # guard must be released after success

    assert urls == ["http://127.0.0.1:8910/clear"] * 2


async def test_clear_rejected_when_not_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _running_service(monkeypatch)
    svc._state = ServiceState.STOPPED
    urls = _patch_client(monkeypatch)

    with pytest.raises(PreconditionFailedError) as ei:
        await svc.clear_context()

    assert ei.value.code == "model_runtime.service_not_running"
    assert urls == []


async def test_clear_reentry_is_rejected_while_in_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _running_service(monkeypatch)
    gate = asyncio.Event()
    urls = _patch_client(monkeypatch, gate=gate)

    first = asyncio.create_task(svc.clear_context())
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    with pytest.raises(ServiceClearBusyError):
        await svc.clear_context()
    assert len(urls) == 1  # the second call never reached the daemon

    gate.set()
    await first
    await svc.clear_context()  # usable again once the first one finished
    assert len(urls) == 2


async def test_clear_failure_releases_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = _running_service(monkeypatch)
    _patch_client(monkeypatch, raise_exc=httpx.ConnectError("boom"))

    with pytest.raises(PreconditionFailedError) as ei:
        await svc.clear_context()
    assert ei.value.code == "model_runtime.service_clear_failed"

    _patch_client(monkeypatch, status=500)
    with pytest.raises(PreconditionFailedError) as ei2:
        await svc.clear_context()
    assert ei2.value.code == "model_runtime.service_clear_failed"
