# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Internal data structures for the sticky-worker host.

Kept in a dedicated module so the host file stays focused on lifecycle
and IO and so unit tests can construct the value objects without
spawning a subprocess.

These are **infrastructure-side** types (host bookkeeping); the
application-layer DTOs surfaced through
:class:`qai.app_builder.application.ports.WorkerStatusPort` are
``LoadedModelInfo`` / ``WorkerPoolStatus`` in
:mod:`qai.app_builder.application.ports`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Mapping

__all__ = [
    "LoadedModelEntry",
    "LoadedModelState",
    "LoadModelRequest",
    "RunRequest",
    "WorkerEvent",
]


LoadedModelState = Literal["loading", "ready", "busy"]


@dataclass(slots=True)
class LoadedModelEntry:
    """Mutable per-model record held by :class:`StickyWorkerHost`.

    Mirrors the legacy host-side ``_LoadedModelInfo`` dataclass from
    ``backend/app_builder/runners/sticky_worker.py``. We keep it
    mutable because lifecycle transitions (``ready ↔ busy``) and
    timestamp updates are frequent and lock-protected by the host.
    """

    model_id: str
    variant_id: str | None
    last_used_at: float
    state: LoadedModelState = "ready"

    def touch(self, *, now: float | None = None) -> None:
        """Refresh :attr:`last_used_at` (≡ legacy ``last_used_at = now``)."""
        self.last_used_at = now if now is not None else time.time()

    @property
    def age_seconds(self) -> float:
        """Idle age, used by the idle-release scanner."""
        return max(0.0, time.time() - self.last_used_at)


@dataclass(frozen=True, slots=True, kw_only=True)
class LoadModelRequest:
    """Caller's intent to load a model into the worker.

    Captured as a value object so the host can be unit-tested without
    making the caller construct ad-hoc dicts. The fields mirror the
    legacy ``op:load`` JSON sent over stdin (see
    ``voice-input-and-sticky-worker-multimodel.md`` §2.1).
    """

    model_id: str
    variant_id: str | None
    runner_path: Path
    pack_dir: Path
    model_dir: Path
    repo_root: Path
    variant_context_bins: tuple[Path, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id must be a non-empty str")
        if self.variant_id is not None and not isinstance(self.variant_id, str):
            raise ValueError("variant_id must be str or None")
        for name, value in (
            ("runner_path", self.runner_path),
            ("pack_dir", self.pack_dir),
            ("model_dir", self.model_dir),
            ("repo_root", self.repo_root),
        ):
            if not isinstance(value, Path):
                raise ValueError(f"{name} must be a Path, got {type(value).__name__}")
        if not isinstance(self.variant_context_bins, tuple):
            raise ValueError("variant_context_bins must be a tuple of Paths")
        for i, p in enumerate(self.variant_context_bins):
            if not isinstance(p, Path):
                raise ValueError(
                    f"variant_context_bins[{i}] must be a Path, "
                    f"got {type(p).__name__}"
                )

    def to_op_payload(self) -> dict[str, Any]:
        """Serialise into the JSON-line payload sent over stdin (op=load)."""
        return {
            "op": "load",
            "modelId": self.model_id,
            "variantId": self.variant_id,
            "runnerPath": str(self.runner_path),
            "modelDir": str(self.model_dir),
            "repoRoot": str(self.repo_root),
            "packDir": str(self.pack_dir),
            "variantContextBins": [str(p) for p in self.variant_context_bins],
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class RunRequest:
    """Caller's intent to run inference on a previously-loaded model.

    Mirrors the legacy ``op:run`` JSON. ``inputs`` / ``params`` /
    ``options`` are passed verbatim to the bootstrap; the host treats
    them as opaque mappings.
    """

    run_id: str
    model_id: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    params: Mapping[str, Any] = field(default_factory=dict)
    options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("run_id must be a non-empty str")
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model_id must be a non-empty str")
        for name, value in (
            ("inputs", self.inputs),
            ("params", self.params),
            ("options", self.options),
        ):
            if not isinstance(value, Mapping):
                raise ValueError(f"{name} must be a Mapping")

    def to_op_payload(self) -> dict[str, Any]:
        """Serialise into the JSON-line payload sent over stdin (op=run)."""
        return {
            "op": "run",
            "runId": self.run_id,
            "modelId": self.model_id,
            "inputs": dict(self.inputs),
            "params": dict(self.params),
            "options": dict(self.options),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class WorkerEvent:
    """Generic event yielded by :meth:`StickyWorkerHost.execute_run`.

    A flat dict-like container. PR-302 will introduce typed subclasses
    (``ResultEvent`` / ``ProgressEvent`` / ``MetricsEvent`` / ...) once
    the runner_protocol v3.1 payload schema is locked. For PR-301 we
    only need a transport-shape ``{"type": str, ...}`` envelope.
    """

    type: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.type, str) or not self.type:
            raise ValueError("WorkerEvent.type must be a non-empty str")
        if not isinstance(self.payload, Mapping):
            raise ValueError("WorkerEvent.payload must be a Mapping")
