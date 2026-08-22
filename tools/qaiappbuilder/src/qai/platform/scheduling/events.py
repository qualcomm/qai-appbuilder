# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain events for the scheduled-task feature.

Published by the scheduler on the shared
:class:`qai.platform.events.EventBus` when a task fires and its isolated agent
run finishes. A delivery bridge (in the chat context) subscribes and folds the
result back into the target conversation out-of-band, so the scheduler stays
free of any chat / transport import dependency (Clean Architecture: cross-
context coordination via domain events).

Every event carries value snapshots only (ids + plain strings); subscribers
MUST NOT mutate the payload.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from qai.platform.events import DomainEvent

__all__ = ["ScheduledTaskFired"]


@dataclass(frozen=True, slots=True, kw_only=True)
class ScheduledTaskFired(DomainEvent):
    """One scheduled task finished an isolated run.

    Attributes:
        run_id: identity of the persisted run row (``scheduling_task_run.id``).
            The frontend uses this — NOT the client-generated arrival
            timestamp — as the de-dup key for the notification bell so a live
            WS delivery and a subsequent reconnect backfill of the SAME fire
            produce ONE bell entry, not two. Empty only for legacy consumers
            that pre-date the durable-notification path (kept for backward
            compatibility with tests that construct the event without the
            run-record ceremony).
        task_id: identity of the task that fired.
        task_name: human label (falls back to the id when unset).
        conversation_id: delivery-target conversation.
        tab_id: delivery-target tab (where the result is folded in).
        ok: whether the isolated run succeeded.
        result_text: the run's final text (a short failure summary when
            ``ok`` is False).
    """

    event_type: ClassVar[str] = "scheduling.task_fired"

    task_id: str
    task_name: str
    conversation_id: str
    tab_id: str
    ok: bool
    result_text: str
    #: Persisted run-record id (see docstring); defaults to empty string so
    #: an event constructed without one still serialises cleanly. In
    #: production the scheduler ALWAYS supplies it (see ``_record_outcome``
    #: → ``_publish``).
    run_id: str = ""
