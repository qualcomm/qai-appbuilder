# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``command_policy`` bounded context (PR-603).

S7.5 lane L6 introduces this BC from scratch. The command_policy BC manages
execution profiles that define allowed/denied commands in the security
sandbox.

Field-name lock (v2.7 §3.1)
---------------------------
Once :class:`CommandPolicyServices` is wired into ``Container.command_policy``
its existing field names are part of the public namespace contract:
they may only be **tail-appended** by future PRs, never renamed or
removed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from qai.command_policy.adapters import InMemoryExecBroker
from qai.command_policy.application.ports import ExecBrokerPort
from qai.command_policy.application.use_cases import (
    GetExecProfilesUseCase,
)
from qai.command_policy.infrastructure.profile_loader import load_all

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container


__all__ = [
    "CommandPolicyServices",
    "build_command_policy_services",
]


@dataclass(slots=True)
class CommandPolicyServices:
    """Application services / ports for the ``command_policy`` namespace.

    Holds the broker port instance and the use case that routes consume.
    """

    broker: ExecBrokerPort
    get_exec_profiles_use_case: GetExecProfilesUseCase


def build_command_policy_services(container: "Container") -> CommandPolicyServices:
    """Wire the command_policy namespace.

    Execution profiles are static assets loaded once at construction time
    from ``<repo_root>/factory/config/exec_profiles/*.toml`` (the compiled
    factory product; V1 parity: ``config/exec_profiles/*.json``). This is the
    shipped-product location (under the ``factory/`` release ``[include]``),
    NOT the build-input ``factory/_source/`` — the latter is excluded from
    release artifacts and must never be read at runtime (AGENTS.md §3.8.1 /
    ``dev-environment.md §2.4``).     A missing asset directory yields an empty
    profile set (graceful — partial install). The master switch is read from
    ``container.settings.tools.command_policy_enabled``; re-enabled ON by user
    decision (2026-07-06 guard-rail redesign — repositioned to an
    LLM-misoperation guard-rail with user-in-the-loop ASK confirmation).
    Operators may still opt out at runtime via
    ``/api/security/runtime-config`` (M-2).
    """
    profiles_dir = container.repo_root / "factory" / "config" / "exec_profiles"
    profiles = load_all(profiles_dir)
    settings = getattr(container, "settings", None)
    tools_settings = getattr(settings, "tools", None) if settings else None
    enabled = bool(getattr(tools_settings, "command_policy_enabled", True))
    broker = InMemoryExecBroker(
        profiles=profiles,
        enabled=enabled,
        project_root=str(container.repo_root),
    )
    get_profiles = GetExecProfilesUseCase(broker=broker)
    return CommandPolicyServices(
        broker=broker,
        get_exec_profiles_use_case=get_profiles,
    )
