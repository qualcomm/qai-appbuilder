# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-layer ports for ``qai.dependency_approval`` (PR-603).

The BC exposes a single :class:`DepBrokerPort` that manages the lifecycle
of pending dependency-install requests.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from qai.dependency_approval.domain import PendingRequest

__all__ = ["DepBrokerPort", "DepInstallNotifier"]


@runtime_checkable
class DepInstallNotifier(Protocol):
    """Side-channel that pushes a ``dep_install_request`` to the WebUI.

    Injected by the apps composition root (publishes a
    :class:`qai.platform.events.DomainEvent` on the EventBus, which the
    chat-events SSE forwards to the browser). Mirrors V1
    ``DepBroker._broadcast("dep_install_request", {...})``
    (``dep_broker.py:191-203``). Best-effort — a notifier failure must not
    break the approval wait.
    """

    async def __call__(self, request: PendingRequest) -> None:
        ...


@runtime_checkable
class DepBrokerPort(Protocol):
    """Port for dependency-install request approval queue.

    Implementations store requests in memory (single-worker deployment).
    """

    @property
    def enabled(self) -> bool:
        """Whether the broker actively intercepts dep-install commands."""
        ...

    def set_enabled(self, enabled: bool) -> None:
        """Hot-toggle the master switch (V1 ``reload_config`` parity)."""
        ...

    async def get_pending(self) -> list[PendingRequest]:
        """Return all requests currently in PENDING state."""
        ...

    async def resolve(self, request_id: str, decision: str) -> bool:
        """Resolve a pending request with 'approve' or 'reject'.

        Returns ``True`` if the request was found and transitioned;
        ``False`` if no pending request matches ``request_id``.
        """
        ...

    def is_dep_install_command(self, command: str) -> bool:
        """Return ``True`` iff ``command`` is a pip/uv *install* command.

        Read-only commands (``pip list``/``show``/``freeze`` …) return
        ``False``. Synchronous pure check (no I/O). Mirrors V1
        ``DepBroker.is_dep_install_command``.
        """
        ...

    def check(self, command: str) -> tuple[bool, str]:
        """Check a dep-install command for denied arguments.

        Returns ``(should_block, reason)``: ``should_block=True`` when
        the command contains a denied argument (``-e`` / ``git+`` /
        ``--extra-index-url`` / ``--pre`` by default) AND the broker is
        enabled. ``should_block=False`` (allow) when no denied args, or
        when the broker is disabled. This is the non-blocking probe (used
        by tests + the fast-path); the interactive approval flow is
        :meth:`check_and_wait`.
        """
        ...

    async def check_and_wait(self, command: str) -> tuple[bool, str]:
        """Full V1 closed loop: intercept → enqueue → block until decided.

        Mirrors V1 ``DepBroker.check`` (``dep_broker.py:161-240``):

        1. disabled broker / not a dep-install / no denied args → allow
           ``(False, "")`` immediately;
        2. otherwise enqueue a :class:`PendingRequest`, push the
           ``dep_install_request`` notification, and **block** until the
           operator approves / rejects via :meth:`resolve` or the approval
           timeout elapses;
        3. approve → allow ``(False, "")``; reject / timeout → block
           ``(True, reason)`` (timeout auto-rejects, V1 parity).
        """
        ...
