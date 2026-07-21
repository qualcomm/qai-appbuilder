# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``Run`` aggregate root — one execution of an :class:`AppModelDefinition`.

State machine
-------------

``PENDING`` → ``RUNNING`` → ``STREAMING`` → ``COMPLETED``
                 │              │              │
                 ▼              ▼              ▼
              ``FAILED`` ◀──────┴──────▶ ``CANCELLED``

Allowed transitions (encoded in :data:`_ALLOWED_TRANSITIONS`):

* ``PENDING``    → ``RUNNING`` | ``CANCELLED``
* ``RUNNING``    → ``STREAMING`` | ``COMPLETED`` | ``FAILED`` | ``CANCELLED``
* ``STREAMING``  → ``COMPLETED`` | ``FAILED`` | ``CANCELLED``
* terminal: ``COMPLETED`` | ``FAILED`` | ``CANCELLED`` — no further transitions.

Terminal status invariants (HANDOFF §4.3 — never silently swallow
errors): once a run reaches ``FAILED`` it MUST carry a non-empty
:attr:`error_message`; once it reaches ``CANCELLED`` the optional
:attr:`error_message` is informational only.

The aggregate is implemented as an **immutable dataclass**: every state
transition returns a new :class:`Run` instance (functional style). This
matches the pattern used by S1 PR-012 platform VOs and keeps the test
matrix simple — fakes never need to worry about shared mutable state.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import Enum

from qai.app_builder.domain.artifact import Artifact
from qai.app_builder.domain.errors import (
    RunAlreadyTerminatedError,
    RunInvalidTransitionError,
)
from qai.app_builder.domain.value_objects import AppModelId, RunId

__all__ = ["RunStatus", "RunFrame", "Run"]


class RunStatus(str, Enum):
    """Lifecycle states for :class:`Run`."""

    PENDING = "pending"
    RUNNING = "running"
    STREAMING = "streaming"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in (
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        )


_ALLOWED_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset(
        {RunStatus.RUNNING, RunStatus.CANCELLED}
    ),
    RunStatus.RUNNING: frozenset(
        {
            RunStatus.STREAMING,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.STREAMING: frozenset(
        {
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }
    ),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


@dataclass(frozen=True, slots=True, kw_only=True)
class RunFrame:
    """A single chunk of streaming output emitted by the runner.

    The shape is intentionally minimal so adapters can map it to SSE,
    WebSocket frames, or batch JSON without translation. ``payload`` is
    an opaque mapping that the application layer surfaces verbatim to
    presenters; the domain assigns no semantics to it.

    Cross-context note (manifest §5): the ``chat`` context (PR-021)
    defines a similarly-shaped ``StreamFrame``. The two shapes are
    intentionally kept independent because the semantics of
    ``sequence`` differ — chat is per-turn, app builder is per-run —
    and a shared frame VO would couple the two contexts in a way
    §3.2 isolation forbids.
    """

    sequence: int
    payload: dict[str, object]

    def __post_init__(self) -> None:
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise ValueError(
                f"RunFrame.sequence must be int, got {type(self.sequence).__name__}"
            )
        if self.sequence < 0:
            raise ValueError(
                f"RunFrame.sequence must be >= 0, got {self.sequence}"
            )
        if not isinstance(self.payload, dict):
            raise ValueError(
                "RunFrame.payload must be a dict, "
                f"got {type(self.payload).__name__}"
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class Run:
    """One execution of an :class:`AppModelDefinition`.

    The aggregate keeps a :class:`tuple` of attached :class:`Artifact`
    instances so it stays hashable / immutable.
    """

    id: RunId
    model_id: AppModelId
    inputs: dict[str, object]
    status: RunStatus = RunStatus.PENDING
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    artifacts: tuple[Artifact, ...] = field(default_factory=tuple)
    error_message: str | None = None
    # PR-F1 (F-15) — append-only structured failure code (v2.7 §3.1).
    # Surfaced verbatim from the runner subprocess's NDJSON ``error``
    # event (`{"type":"error","code":"WEIGHTS_NOT_INSTALLED",…}` —
    # behavior parity with V1 ``_UserError("WEIGHTS_NOT_INSTALLED",…)``
    # in ``features/app-builder/models/<pack>/runner.py``). The DTO /
    # SSE layers transmit it alongside ``error_message`` so the frontend
    # can dispatch to i18n-friendly toasts (``voiceInput.weightsMissing``
    # → "guide user to AppBuilder", ``voiceInput.encodeFailed`` → "audio
    # decode error", default → ``voiceInput.inferenceFailed``). ``None``
    # for non-FAILED runs and for FAILED runs whose underlying failure
    # was not a structured ``_UserError`` (in which case
    # ``error_message`` carries the only diagnostic the user sees).
    error_code: str | None = None
    # 缺口 #6 — append-only inference-latency metric (v2.7 §3.1).
    # The runner subprocess streams a ``metrics`` NDJSON event carrying
    # ``latencyMs`` (pure inference latency, distinct from the run's
    # end-to-end wall-clock ``duration_ms`` derived from
    # started_at/finished_at). V1 parity: ``useAppBuilder.js:601-606``
    # stores ``run.metrics = {latencyMs, memoryMB, device, …}`` from the
    # ``metrics`` event and ``HistoryPanel.js:237-238`` shows
    # ``r.metrics.latencyMs``ms in the history "Inference" column;
    # V1 persists it in ``last_results.json`` (``_persistRunAsLastResult``).
    # V2 surfaces it through ``GetMetricsForRunUseCase`` → ``RunMetrics`` →
    # ``RunMetricsResponse.latency_ms`` so the same column survives a
    # restart. ``None`` when the runner emitted no ``metrics`` event (the
    # frontend then honestly falls back to ``duration_ms`` / "—").
    inference_latency_ms: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.inputs, dict):
            raise ValueError(
                "Run.inputs must be a dict, "
                f"got {type(self.inputs).__name__}"
            )
        if not isinstance(self.artifacts, tuple):
            raise ValueError("Run.artifacts must be a tuple")
        if self.created_at.tzinfo is None:
            raise ValueError("Run.created_at must be tz-aware")
        for ts_name, ts in (
            ("started_at", self.started_at),
            ("finished_at", self.finished_at),
        ):
            if ts is not None and ts.tzinfo is None:
                raise ValueError(f"Run.{ts_name} must be tz-aware if set")
        if self.status == RunStatus.FAILED and not self.error_message:
            raise ValueError(
                "Run.error_message must be set when status is FAILED"
            )
        if self.inference_latency_ms is not None and (
            not isinstance(self.inference_latency_ms, (int, float))
            or isinstance(self.inference_latency_ms, bool)
            or self.inference_latency_ms < 0
        ):
            raise ValueError(
                "Run.inference_latency_ms must be a number >= 0 or None"
            )

    # ── Transitions ────────────────────────────────────────────────────

    def _check_transition(self, target: RunStatus) -> None:
        if self.status.is_terminal:
            raise RunAlreadyTerminatedError(
                message=(
                    f"Run {self.id} already in terminal status "
                    f"{self.status.value!r}; cannot move to {target.value!r}"
                ),
                details={
                    "run_id": str(self.id),
                    "current": self.status.value,
                    "target": target.value,
                },
            )
        if target not in _ALLOWED_TRANSITIONS[self.status]:
            raise RunInvalidTransitionError(
                message=(
                    f"Run {self.id} cannot transition "
                    f"{self.status.value!r} → {target.value!r}"
                ),
                details={
                    "run_id": str(self.id),
                    "current": self.status.value,
                    "target": target.value,
                },
            )

    def start(self, *, now: datetime) -> Run:
        """``PENDING`` → ``RUNNING``."""
        self._check_transition(RunStatus.RUNNING)
        return replace(self, status=RunStatus.RUNNING, started_at=now)

    def begin_streaming(self) -> Run:
        """``RUNNING`` → ``STREAMING``."""
        self._check_transition(RunStatus.STREAMING)
        return replace(self, status=RunStatus.STREAMING)

    def complete(self, *, now: datetime) -> Run:
        """Move into ``COMPLETED`` from any non-terminal status."""
        self._check_transition(RunStatus.COMPLETED)
        return replace(self, status=RunStatus.COMPLETED, finished_at=now)

    def fail(
        self,
        *,
        now: datetime,
        message: str,
        code: str | None = None,
    ) -> Run:
        """Move into ``FAILED`` with a mandatory error message.

        ``code`` is an optional structured failure code (PR-F1, v2.7
        §3.1 append-only). Pre-PR-F1 callers using ``fail(now=, message=)``
        keep working byte-for-byte; new callers pass ``code=`` to surface
        the runner's NDJSON ``error.code`` (e.g. ``"WEIGHTS_NOT_INSTALLED"``)
        to the SSE / REST layer for i18n dispatch.
        """
        if not isinstance(message, str) or not message.strip():
            raise ValueError("fail() requires a non-empty message")
        if code is not None and (
            not isinstance(code, str) or not code.strip()
        ):
            raise ValueError(
                "fail() code must be a non-empty string or None"
            )
        self._check_transition(RunStatus.FAILED)
        return replace(
            self,
            status=RunStatus.FAILED,
            finished_at=now,
            error_message=message,
            error_code=code,
        )

    def cancel(self, *, now: datetime, reason: str | None = None) -> Run:
        """Move into ``CANCELLED`` (caller-initiated)."""
        self._check_transition(RunStatus.CANCELLED)
        return replace(
            self,
            status=RunStatus.CANCELLED,
            finished_at=now,
            error_message=reason,
        )

    # ── Metrics ───────────────────────────────────────────────────────

    def with_inference_latency(self, latency_ms: float | None) -> Run:
        """Return a copy carrying the runner-reported inference latency.

        ``latency_ms`` comes from the runner's ``metrics`` NDJSON event
        (``latencyMs``); it is the pure inference time, independent of the
        run's end-to-end wall-clock. Passing ``None`` (no ``metrics`` event)
        leaves the field unset. Validation is enforced by ``__post_init__``.
        Unlike the lifecycle transitions this does not gate on terminal
        status — the caller sets the latency right before ``complete()``.
        """
        return replace(self, inference_latency_ms=latency_ms)

    # ── Artifacts ─────────────────────────────────────────────────────

    def attach_artifact(self, artifact: Artifact) -> Run:
        """Return a copy with ``artifact`` appended.

        Attaching to a terminal run is rejected (the run is immutable
        once finished): this keeps adapters from "back-filling" outputs
        after the user already saw a final status.
        """
        if not isinstance(artifact, Artifact):
            raise ValueError(
                "attach_artifact requires an Artifact, "
                f"got {type(artifact).__name__}"
            )
        if self.status.is_terminal:
            raise RunAlreadyTerminatedError(
                message=(
                    f"Cannot attach artifact to terminal run {self.id} "
                    f"(status={self.status.value!r})"
                ),
                details={
                    "run_id": str(self.id),
                    "current": self.status.value,
                },
            )
        return replace(self, artifacts=(*self.artifacts, artifact))
