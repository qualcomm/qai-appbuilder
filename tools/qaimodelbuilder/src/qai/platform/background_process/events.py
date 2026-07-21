# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain events for the background-process manager.

This module defines the two :class:`~qai.platform.events.DomainEvent`
subclasses that the :class:`SubprocessBackgroundProcessManager`
publishes on the shared :class:`qai.platform.events.EventBus`:

- :class:`BackgroundProcessUpdated` — fired on every meaningful state
  transition of a tracked process (``starting`` -> ``running`` ->
  ``ready`` -> ``exited`` / ``failed`` / ``stopped``), on output growth
  past the 500 ms debounce window, and on port-inference changes.
- :class:`BackgroundProcessDeleted` — fired exactly once when a tracked
  process is removed from the manager's task map (terminate + remove
  flow, including ``stop_session`` cleanup and shutdown drain).

The two event shapes are ``Updated`` (fired on every meaningful state
transition) and ``Deleted`` (fired once on removal)::

    Updated -> { info: Info, scope: str }
    Deleted -> { session_id: str, process_id: str, scope: str }

Field renames go ``camelCase`` -> ``snake_case`` only (``sessionID`` ->
``session_id``, ``processID`` -> ``process_id``); semantics and value
ranges are preserved verbatim so the frontend sidebar contract
(``plugins/sidebar-background-processes.tsx``) keeps working when the
events are re-broadcast over the ``/api/events`` SSE stream
(see ``docs/90-refactor/background-process-design.md`` §3.3).

Subscriber contract
-------------------

- Every event carries **value snapshots only** (the embedded
  :class:`~qai.platform.background_process.ports.Info` is itself a
  frozen dataclass with ``slots=True``). Subscribers MUST NOT mutate
  the payload; if a subscriber needs to feed it back into the manager
  it MUST go through :class:`BackgroundProcessManagerPort` instead.
- ``scope`` is the storage scope key the manager assigns at
  ``StartInput`` resolution time. The v1 manager always emits
  ``"global"`` (single in-memory task map per daemon); the field is
  kept so SSE consumers retain a stable filter key for future
  multi-instance support.
- Frozen dataclasses + slots prevent subscribers from accidentally
  mutating the payload after the bus has handed it to them; see
  :mod:`qai.platform.events.types`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from qai.platform.events import DomainEvent

from .ports import Info

__all__ = [
    "BackgroundProcessDeleted",
    "BackgroundProcessUpdated",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class BackgroundProcessUpdated(DomainEvent):
    """A tracked background process changed observable state.

    Fired by the manager on:

    - Status transitions (``starting`` -> ``running`` / ``ready`` ->
      terminal).
    - First port inference / port-set change (``Info.ports`` delta).
    - Output growth, debounced to one event per 500 ms per process
      (``PUBLISH_DEBOUNCE_S`` in the manager).

    The embedded :attr:`info` is a fresh snapshot — the manager never
    mutates an existing ``Info`` instance, so subscribers can safely
    retain references for diffing across events.
    """

    event_type: ClassVar[str] = "background_process.updated"

    info: Info
    scope: str


@dataclass(frozen=True, slots=True, kw_only=True)
class BackgroundProcessDeleted(DomainEvent):
    """A tracked background process was removed from the task map.

    Fired exactly once per process on:

    - ``stop()`` / ``restart()`` with ``remove=True`` (the latter emits
      ``Deleted`` for the old id only when the id is actually retired;
      restart reuses the id and does NOT emit ``Deleted``).
    - ``stop_session()`` cleanup of session processes.
    - ``shutdown()`` drain of all tracked processes.

    Carries only id strings (``session_id`` + ``process_id``) so
    subscribers do not need to hold a stale ``Info`` reference after
    the manager has dropped the record.
    """

    event_type: ClassVar[str] = "background_process.deleted"

    session_id: str
    process_id: str
    scope: str
