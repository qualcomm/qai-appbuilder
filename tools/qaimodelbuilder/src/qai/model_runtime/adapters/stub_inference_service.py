# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory stub adapter for :class:`InferenceServicePort`.

This stub lets tests and dev environments exercise the model_runtime
BC end-to-end without requiring the real inference daemon binary.
The production path is the subprocess + HTTP adapter under
:mod:`qai.model_runtime.infrastructure` (selected by
``apps/api/_model_runtime_di.py`` when the daemon binary is present);
this stub is selected when the binary is absent or in tests that pin
the in-memory branch.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

from qai.model_runtime.application.ports import InferenceServicePort
from qai.model_runtime.domain.entities import ModelInfo
from qai.model_runtime.domain.enums import ServiceState


class StubInferenceService:
    """In-memory stub implementing :class:`InferenceServicePort`.

    Simulates daemon state transitions and log buffering without
    spawning real processes.

    Parameters:
        install_dir: Path reported by :meth:`get_install_dir`.
        default_port: Port used when none is specified in :meth:`start`.
    """

    def __init__(
        self,
        *,
        install_dir: str = "",
        default_port: int = 0,
    ) -> None:
        self._install_dir = install_dir
        self._default_port = default_port
        self._state: ServiceState = ServiceState.STOPPED
        self._loaded_model: str | None = None
        self._port: int | None = None
        self._logs: list[str] = []
        # Monotonic write counter (V1 ``_total_written``) backing skip_from.
        self._total_written: int = 0
        self._command: str = ""

    # ------------------------------------------------------------------
    # InferenceServicePort implementation
    # ------------------------------------------------------------------

    def _append(self, line: str) -> None:
        self._logs.append(line)
        self._total_written += 1

    async def start(
        self,
        *,
        model_name: str | None = None,
        port: int | None = None,
        loglevel: int | None = None,
    ) -> None:
        self._state = ServiceState.RUNNING
        self._port = port if port is not None else self._default_port
        if model_name:
            self._loaded_model = model_name
        self._command = f"GenieAPIService -p {self._port}"
        self._append(f"Service started on port {self._port}")

    async def stop(self) -> None:
        self._state = ServiceState.STOPPED
        self._loaded_model = None
        self._port = None
        self._append("Service stopped")

    async def probe(
        self, *, host: str | None = None, port: int | None = None
    ) -> dict[str, Any]:
        # Arbitrary-address probe (V1 Connection "Test"): the stub cannot
        # reach a real socket, so report unreachable deterministically.
        if host is not None and port is not None:
            return {"reachable": False, "alive": False, "model": None}
        alive = self._state == ServiceState.RUNNING
        return {
            "reachable": alive,
            "alive": alive,
            "model": self._loaded_model if alive else None,
        }

    async def status(self) -> dict[str, Any]:
        running = self._state == ServiceState.RUNNING
        return {
            "state": self._state.value,
            "running": running,
            "pid": None,
            "uptime_seconds": None,
            "model": self._loaded_model,
            "port": self._port,
            "exe_path": "",
            "command": self._command if running else "",
            "memory_mb": 0.0,
        }

    async def load_model(self, model_name: str) -> None:
        self._loaded_model = model_name
        self._append(f"Model loaded: {model_name}")

    async def get_logs(self) -> list[str]:
        return list(self._logs)

    async def clear_logs(self) -> int:
        self._logs.clear()
        return self._total_written

    async def stream_logs(self, *, skip: int = 0) -> AsyncIterator[str]:
        # Emit the current buffer (respecting skip), then terminate — the
        # stub has no live process producing further lines.
        buf = list(self._logs)
        buf_start = self._total_written - len(buf)
        for i, line in enumerate(buf):
            if buf_start + i >= skip:
                yield line
        # Yield control once so the route's StreamingResponse can complete.
        await asyncio.sleep(0)

    async def list_models(self, *, models_root: str | None = None) -> list[ModelInfo]:
        # Stub returns an empty list; production adapter scans disk.
        return []

    async def get_state(self) -> ServiceState:
        return self._state

    def get_install_dir(self) -> str:
        return self._install_dir


__all__ = ["StubInferenceService"]
