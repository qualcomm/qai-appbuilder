# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-layer ports for the ``model_runtime`` bounded context.

Defines the interface that adapters must implement to control the local
inference daemon (GenieAPIService).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from qai.model_runtime.domain.entities import ModelInfo
from qai.model_runtime.domain.enums import ServiceState

@runtime_checkable
class InferenceServicePort(Protocol):
    """Port for controlling the local inference daemon.

    Adapters may implement this via subprocess management + HTTP probing
    (production) or as an in-memory stub (testing / PR-604).
    """

    async def start(
        self,
        *,
        model_name: str | None = None,
        port: int | None = None,
        loglevel: int | None = None,
    ) -> None:
        """Start the inference daemon, optionally loading *model_name*.

        ``loglevel`` is forwarded to the daemon's ``-d`` CLI flag (V1
        ``backend/main.py:5266``); ``None`` means "use forge.config /
        adapter default".
        """
        ...

    async def stop(self) -> None:
        """Stop the inference daemon gracefully."""
        ...

    async def probe(
        self, *, host: str | None = None, port: int | None = None
    ) -> dict[str, Any]:
        """Quick health probe.

        When *host*/*port* are provided, probe that arbitrary address
        (V1 ``GET /api/service/probe?host=&port=`` — used by the Connection
        panel's "Test" button to reach a remote / local daemon, bypassing
        browser CORS). When omitted, probe the locally-managed daemon.

        Returns:
            ``{"reachable": bool, "alive": bool, "model": str | None}``
            (``alive`` kept for backward compatibility; ``reachable`` is the
            V1 field consumed by the Connection panel.)
        """
        ...

    async def status(self) -> dict[str, Any]:
        """Detailed status of the daemon.

        Returns:
            Dict with keys: ``state``, ``running``, ``pid``,
            ``uptime_seconds``, ``model``, ``port``, ``exe_path``,
            ``command``, ``memory_mb``.
        """
        ...

    async def load_model(self, model_name: str) -> None:
        """Load or switch to *model_name* in the running daemon."""
        ...

    async def get_logs(self) -> list[str]:
        """Return recent log lines from the daemon."""
        ...

    async def clear_logs(self) -> int:
        """Clear the retained log buffer.

        Returns:
            The write sequence number after clearing (V1 ``skip_from``):
            the frontend passes this back to :meth:`stream_logs` so cleared
            history is not replayed.
        """
        ...

    def stream_logs(self, *, skip: int = 0) -> AsyncIterator[str]:
        """Stream log lines as they are produced.

        Yields buffered lines (with sequence ``>= skip``) first, then new
        lines event-driven until the daemon exits. Mirrors V1's
        ``ServiceManager.stream_logs`` semantics used by the SSE endpoint.
        """
        ...

    async def list_models(self, *, models_root: str | None = None) -> list[ModelInfo]:
        """List models available on disk.

        When *models_root* is given, scan that directory (V1's
        ``service_launch.models_root_path``); otherwise fall back to the
        adapter's configured install dir.
        """
        ...

    async def get_state(self) -> ServiceState:
        """Return current service state."""
        ...

    def get_install_dir(self) -> str:
        """Return the filesystem path of the service installation."""
        ...


@runtime_checkable
class ServiceConfigRepositoryPort(Protocol):
    """Port for persisting the GenieAPIService ``service_config.json`` document.

    The **single source of truth** is the copy next to ``GenieAPIService.exe``
    inside the configured install root (the daemon reads it). There is no
    ``data/config/service_config.json`` fallback file.

    - **Path resolution**: when ``genie_root`` points at an existing
      GenieAPIService install directory, locate the ``service_config.json``
      inside it (recursive BFS, max 3 levels) and return its path. When
      GenieAPIService is **not installed** (no root / not found), return an
      empty string — the "not installed" sentinel.
    - **Load**: read the active file, deep-merging it onto the built-in
      defaults so missing keys are always populated. When not installed
      (empty/``None`` path), return the in-memory defaults **without writing
      anything to disk** (read-only display values only).
    - **Save**: deep-merge the submitted document onto the existing one and
      persist to the active path. When not installed, the save is rejected
      (``PreconditionFailedError``) — no zombie fallback file is created.

    The ``api_key`` masking / SecretStore handling lives in the use cases,
    not here — this port deals purely with the JSON document on disk.
    """

    def resolve_active_path(self, genie_root: str) -> str:
        """Return the active service_config.json path for *genie_root*.

        Returns the resolved absolute path as a string (so the wire ``meta``
        can surface it without the interfaces layer touching ``pathlib``), or
        an empty string when GenieAPIService is not installed.
        """
        ...

    def load(self, *, path: str | None = None) -> dict[str, Any]:
        """Load the document at *path*, merged onto the built-in defaults.

        When *path* is empty/``None`` (not installed), return the in-memory
        defaults without any disk side effect.
        """
        ...

    def save(self, data: dict[str, Any], *, path: str | None = None) -> None:
        """Persist *data* verbatim to the active path (plain write).

        No implicit merge: the caller (use case) performs any
        read-modify-write merging it needs before calling save. When *path*
        is empty/``None`` (not installed), raise ``PreconditionFailedError``.
        """
        ...


__all__ = ["InferenceServicePort", "ServiceConfigRepositoryPort"]
