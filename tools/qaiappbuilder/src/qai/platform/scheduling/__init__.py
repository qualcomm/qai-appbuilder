# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Public API for ``qai.platform.scheduling``.

Two schedulers live here:

* :class:`BackgroundTaskManager` — a platform-neutral, edition-agnostic
  *static* periodic task scheduler (run-once-on-start + fixed interval), frozen
  at ``start``. See ``background_tasks.py``.
* :class:`SchedulerService` — a *runtime-dynamic* scheduler for the agent-facing
  scheduled-task feature (interval / one-shot / cron, add/remove at runtime,
  next-run computation, at-most-once firing). See ``scheduler_service.py``.

Plus the supporting value objects, schedule parser, and aiosqlite store.
"""

from __future__ import annotations

from .background_tasks import BackgroundTaskManager, TaskFunc
from .events import ScheduledTaskFired
from .schedule_parser import (
    CronUnavailableError,
    ScheduleParseError,
    next_run_at,
    parse_duration,
    parse_schedule,
)
from .scheduled_task import (
    Schedule,
    ScheduleKind,
    ScheduledTask,
    TaskRunRecord,
    TaskState,
)
from .scheduler_service import SchedulerService, TaskExecutor, TaskRunResult
from .task_store import (
    ScheduledTaskConflictError,
    ScheduledTaskNotFoundError,
    SqliteScheduledTaskStore,
)

__all__ = [
    "BackgroundTaskManager",
    "TaskFunc",
    "Schedule",
    "ScheduleKind",
    "ScheduledTask",
    "TaskState",
    "TaskRunRecord",
    "ScheduleParseError",
    "CronUnavailableError",
    "parse_schedule",
    "parse_duration",
    "next_run_at",
    "SqliteScheduledTaskStore",
    "ScheduledTaskConflictError",
    "ScheduledTaskNotFoundError",
    "SchedulerService",
    "TaskExecutor",
    "TaskRunResult",
    "ScheduledTaskFired",
]
