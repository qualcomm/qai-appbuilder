# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain events emitted by the App Builder context.

Subscribers in other contexts (audit log, channels, telemetry) react
asynchronously through :class:`qai.platform.events.EventBus`. All events
are immutable, ``slots=True`` dataclasses inheriting
:class:`qai.platform.events.DomainEvent`.

Naming convention (S2 spec §9):
``event_type = "app_builder.<verb>_<subject>"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import ClassVar

from qai.app_builder.domain.import_plan import CommitId
from qai.app_builder.domain.run import RunFrame, RunStatus
from qai.app_builder.domain.value_objects import AppModelId, RunId
from qai.platform.events import DomainEvent

__all__ = [
    "RunStartedEvent",
    "RunFrameEvent",
    "RunCompletedEvent",
    "RunFailedEvent",
    "RunCancelledEvent",
    "ImportCommittedEvent",
    "ImportRolledBackEvent",
    "StickyWorkerDiedEvent",
    "StickyWorkerRecoveredEvent",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class RunStartedEvent(DomainEvent):
    """A :class:`Run` transitioned to ``RUNNING``."""

    event_type: ClassVar[str] = "app_builder.run_started"

    run_id: RunId
    model_id: AppModelId
    started_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class RunFrameEvent(DomainEvent):
    """A streaming :class:`RunFrame` was produced by the runner."""

    event_type: ClassVar[str] = "app_builder.run_frame"

    run_id: RunId
    frame: RunFrame


@dataclass(frozen=True, slots=True, kw_only=True)
class RunCompletedEvent(DomainEvent):
    """A :class:`Run` reached ``COMPLETED``."""

    event_type: ClassVar[str] = "app_builder.run_completed"

    run_id: RunId
    model_id: AppModelId
    finished_at: datetime
    artifact_count: int


@dataclass(frozen=True, slots=True, kw_only=True)
class RunFailedEvent(DomainEvent):
    """A :class:`Run` reached ``FAILED``."""

    event_type: ClassVar[str] = "app_builder.run_failed"

    run_id: RunId
    model_id: AppModelId
    finished_at: datetime
    error_message: str


@dataclass(frozen=True, slots=True, kw_only=True)
class RunCancelledEvent(DomainEvent):
    """A :class:`Run` reached ``CANCELLED``."""

    event_type: ClassVar[str] = "app_builder.run_cancelled"

    run_id: RunId
    model_id: AppModelId
    finished_at: datetime
    reason: str | None = None

    @property
    def status(self) -> RunStatus:
        return RunStatus.CANCELLED


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportCommittedEvent(DomainEvent):
    """An import plan was successfully committed."""

    event_type: ClassVar[str] = "app_builder.import_committed"

    commit_id: CommitId
    item_count: int
    committed_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class ImportRolledBackEvent(DomainEvent):
    """A previously-committed import was rolled back."""

    event_type: ClassVar[str] = "app_builder.import_rolled_back"

    commit_id: CommitId
    rolled_back_at: datetime


@dataclass(frozen=True, slots=True, kw_only=True)
class StickyWorkerDiedEvent(DomainEvent):
    """The persistent sticky worker process died unexpectedly.

    Published so the frontend can show a transient notification to the
    user that NPU inference is temporarily degraded (falling back to
    one-shot mode until the worker respawns).
    """

    event_type: ClassVar[str] = "app_builder.sticky_worker_died"

    reason: str
    pid: int | None = None
    will_respawn: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class StickyWorkerRecoveredEvent(DomainEvent):
    """The persistent sticky worker was successfully respawned.

    Published so the frontend can dismiss the degradation notification
    and inform the user that NPU inference is back to normal.
    """

    event_type: ClassVar[str] = "app_builder.sticky_worker_recovered"

    pid: int | None = None
    respawn_attempt: int = 1
