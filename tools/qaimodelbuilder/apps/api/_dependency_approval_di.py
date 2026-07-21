# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``dependency_approval`` bounded context (PR-603).

S7.5 lane L6 introduces this BC from scratch. The dependency_approval BC
manages pending dependency-install approval requests in the security sandbox.

Field-name lock (v2.7 §3.1)
---------------------------
Once :class:`DependencyApprovalServices` is wired into ``Container.dependency_approval``
its existing field names are part of the public namespace contract:
they may only be **tail-appended** by future PRs, never renamed or
removed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from qai.dependency_approval.adapters import InMemoryDepBroker
from qai.dependency_approval.application.ports import DepBrokerPort
from qai.dependency_approval.application.use_cases import (
    GetPendingRequestsUseCase,
    ResolveRequestUseCase,
)

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container


__all__ = [
    "DependencyApprovalServices",
    "build_dependency_approval_services",
]


@dataclass(slots=True)
class DependencyApprovalServices:
    """Application services / ports for the ``dependency_approval`` namespace.

    Holds the broker port instance and the two use cases that routes
    consume.
    """

    broker: DepBrokerPort
    get_pending_requests_use_case: GetPendingRequestsUseCase
    resolve_request_use_case: ResolveRequestUseCase


def build_dependency_approval_services(container: "Container") -> DependencyApprovalServices:
    """Wire the dependency_approval namespace.

    The broker's master switch + approval timeout are read from
    ``container.settings.tools`` (``dependency_approval_enabled`` /
    ``dependency_approval_timeout_s``); V2 ships the switch OFF by user
    decision (2026-06-13) so the operator opts in via
    ``/api/security/runtime-config``. A WebUI notifier is injected so an
    intercepted dep-install pops the approval card in real time (V1
    ``dep_install_request`` SSE parity), published on the platform EventBus
    and forwarded by the ``/api/events`` route.
    """
    from apps.api._dependency_approval_notify_bridge import EventBusDepInstallNotifier

    settings = getattr(container, "settings", None)
    tools_settings = getattr(settings, "tools", None) if settings else None
    enabled = bool(getattr(tools_settings, "dependency_approval_enabled", False))
    timeout_s = float(
        getattr(tools_settings, "dependency_approval_timeout_s", 120)
    )
    # M-3: operator-tunable denied-arg list (V1 dep_broker.deny_args). Empty /
    # missing falls back to the in-memory default inside InMemoryDepBroker.
    deny_args_cfg = getattr(tools_settings, "dependency_approval_deny_args", None)

    events = getattr(container, "events", None)
    notifier = (
        EventBusDepInstallNotifier(event_bus=events)
        if events is not None
        else None
    )

    broker_kwargs: dict = {
        "enabled": enabled,
        "approval_timeout_s": timeout_s,
        "notifier": notifier,
    }
    if deny_args_cfg:
        broker_kwargs["deny_args"] = tuple(
            a for a in deny_args_cfg if isinstance(a, str) and a.strip()
        )
    broker = InMemoryDepBroker(**broker_kwargs)
    get_pending = GetPendingRequestsUseCase(broker=broker)
    resolve = ResolveRequestUseCase(broker=broker)
    return DependencyApprovalServices(
        broker=broker,
        get_pending_requests_use_case=get_pending,
        resolve_request_use_case=resolve,
    )
