# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ProcessBackedInferenceService.status()['ready']``.

``ready`` is a separate ``GET /status`` probe (``loading``); ``running`` stays
Popen-derived and never flips on it. (Port of the ``TestProcessServiceReady``
class added to the internal ``test_model_runtime.py`` by a63d4f26.)
"""

from __future__ import annotations

import httpx
import pytest

from qai.model_runtime.domain.enums import ServiceState
from qai.model_runtime.infrastructure.process_service import (
    ProcessBackedInferenceService,
)

pytestmark = pytest.mark.unit


class TestProcessServiceReady:
    """``status()['ready']`` is a separate /status probe; ``running`` is Popen-only."""

    @staticmethod
    def _patch_httpx(monkeypatch: pytest.MonkeyPatch, handler) -> None:
        real = httpx.AsyncClient

        def _factory(*args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)

        monkeypatch.setattr(httpx, "AsyncClient", _factory)

    @staticmethod
    def _running_service() -> ProcessBackedInferenceService:
        svc = ProcessBackedInferenceService()
        svc._state = ServiceState.RUNNING
        svc._port = 8000
        return svc

    @pytest.mark.asyncio
    async def test_not_started_ready_is_none(self) -> None:
        st = await ProcessBackedInferenceService().status()
        assert st["running"] is False
        assert st["ready"] is None

    @pytest.mark.asyncio
    async def test_running_without_port_ready_is_none(self) -> None:
        svc = ProcessBackedInferenceService()
        svc._state = ServiceState.RUNNING
        st = await svc.status()
        assert st["running"] is True
        assert st["ready"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("handler_kind", "expected"),
        [
            ("loading_true", False),
            ("loading_false", True),
            ("not_found", True),
            ("connect_error", False),
        ],
    )
    async def test_ready_probe(
        self,
        monkeypatch: pytest.MonkeyPatch,
        handler_kind: str,
        expected: bool,
    ) -> None:
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            if handler_kind == "loading_true":
                return httpx.Response(200, json={"loading": "true"})
            if handler_kind == "loading_false":
                return httpx.Response(200, json={"loading": "false"})
            if handler_kind == "not_found":
                return httpx.Response(404)
            raise httpx.ConnectError("refused")

        self._patch_httpx(monkeypatch, handler)
        st = await self._running_service().status()
        assert seen == ["http://127.0.0.1:8000/status"]
        assert st["ready"] is expected
        assert st["running"] is True
        assert st["state"] == "running"
