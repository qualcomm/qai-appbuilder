# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Update Policy use case.

Atomically replaces or amends the singleton :class:`Policy` aggregate,
emits :class:`PolicyChangedEvent`, and (optionally) raises the reboot
signal when the new policy differs in a way that requires the supervised
process to restart with ``REBOOT_EXIT_CODE = 75`` (refactor-plan §8.11).

PR-501 — non-fatal pattern-shadow detection
-------------------------------------------

After persisting the new policy the use case calls
:meth:`Policy.detect_shadows` and, when any warnings are returned,
publishes :class:`PolicyShadowDetectedEvent` carrying the warning
records. The save itself is *not* blocked by shadows — the operator
may legitimately want them (e.g. an explicit deny on a sub-tree of an
allowed prefix is the intended behaviour). The event lets the SSE
bridge surface them in the operator dashboard so they don't sit
silently in the rule list.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.platform.events import EventBus
from qai.platform.time import Clock

from qai.security.domain.entities import Policy, PolicyRule, PolicyShadowWarning
from qai.security.domain.events import (
    PolicyChangedEvent,
    PolicyShadowDetectedEvent,
)

from ..ports import PolicyRepositoryPort, RebootSignalPort

__all__ = ["UpdatePolicyResult", "UpdatePolicyUseCase"]


@dataclass(frozen=True, slots=True, kw_only=True)
class UpdatePolicyResult:
    """Outcome of :meth:`UpdatePolicyUseCase.execute_with_warnings`.

    Carries the saved :class:`Policy` plus any non-fatal shadow
    warnings detected during the update. The legacy
    :meth:`UpdatePolicyUseCase.execute` entry point still returns just
    the :class:`Policy` so existing route-layer callers keep working
    unchanged.

    ``requires_reboot`` indicates that the rule set changed in a way
    that needs a process restart to take full effect (e.g. the native
    file guard rules are seeded at startup). The use case no longer
    triggers the reboot automatically — the caller (route layer /
    frontend) decides whether to reboot immediately or defer.
    """

    policy: Policy
    shadow_warnings: tuple[PolicyShadowWarning, ...]
    requires_reboot: bool = False


class UpdatePolicyUseCase:
    """Replace the active Policy with a new ordered set of rules.

    The use case decides whether the change *requires* a reboot. The
    current heuristic is: any change in rule set or any change in version
    requires reboot. Adapters that want to relax this (e.g. live reload
    for non-critical scopes) can do so by post-processing
    :class:`PolicyChangedEvent` rather than calling this use case
    directly.
    """

    def __init__(
        self,
        *,
        policy_repository: PolicyRepositoryPort,
        reboot_signal: RebootSignalPort,
        event_bus: EventBus,
        clock: Clock,
    ) -> None:
        self._policies = policy_repository
        self._reboot = reboot_signal
        self._events = event_bus
        self._clock = clock

    async def execute(
        self,
        *,
        new_rules: tuple[PolicyRule, ...],
        reboot_reason: str = "policy changed",
        auto_reboot: bool = True,
    ) -> Policy:
        """Persist ``new_rules`` and return the saved :class:`Policy`.

        Backward-compatible signature retained for callers that don't
        need the full :class:`UpdatePolicyResult` (CLI, hot-reload,
        templates). By default ``auto_reboot=True`` preserves the legacy
        behaviour: when rules actually changed the process is restarted
        automatically. The HTTP route layer passes ``auto_reboot=False``
        so the frontend can ask the user before triggering a restart.
        """
        result = await self.execute_with_warnings(
            new_rules=new_rules,
            reboot_reason=reboot_reason,
            auto_reboot=auto_reboot,
        )
        return result.policy

    async def execute_with_warnings(
        self,
        *,
        new_rules: tuple[PolicyRule, ...],
        reboot_reason: str = "policy changed",
        auto_reboot: bool = True,
    ) -> UpdatePolicyResult:
        """Persist ``new_rules`` and return the policy + shadow warnings.

        Side-effects (in order):

        1. ``policy_repository.save(new_policy)``
        2. ``EventBus.publish(PolicyChangedEvent(...))``
        3. when shadows non-empty:
           ``EventBus.publish(PolicyShadowDetectedEvent(...))``
        4. when ``requires_reboot`` AND ``auto_reboot=True``:
           ``reboot_signal.request_reboot(...)``

        When ``auto_reboot=False`` the reboot is NOT triggered; instead
        the caller inspects ``result.requires_reboot`` and decides
        (e.g. the HTTP route surfaces it to the frontend for user
        confirmation via ``POST /api/system/reboot``).
        """
        if not isinstance(new_rules, tuple):
            raise TypeError(
                f"new_rules must be tuple, got {type(new_rules).__name__}"
            )
        now = self._clock.now()
        existing = await self._policies.load()
        new_policy = Policy(
            version=existing.version + 1,
            updated_at=now,
            rules=new_rules,
        )
        requires_reboot = _rules_changed(existing.rules, new_rules)
        await self._policies.save(new_policy)
        await self._events.publish(
            PolicyChangedEvent(
                old_version=existing.version,
                new_version=new_policy.version,
                occurred_at=now,
                requires_reboot=requires_reboot,
            )
        )

        warnings = new_policy.detect_shadows()
        if warnings:
            await self._events.publish(
                PolicyShadowDetectedEvent(
                    policy_version=new_policy.version,
                    occurred_at=now,
                    warnings=tuple(_warning_to_dict(w) for w in warnings),
                )
            )

        if requires_reboot and auto_reboot:
            await self._reboot.request_reboot(reason=reboot_reason)

        return UpdatePolicyResult(
            policy=new_policy,
            shadow_warnings=warnings,
            requires_reboot=requires_reboot,
        )


def _rules_changed(
    old_rules: tuple[PolicyRule, ...],
    new_rules: tuple[PolicyRule, ...],
) -> bool:
    if len(old_rules) != len(new_rules):
        return True
    for old, new in zip(old_rules, new_rules, strict=True):
        if old != new:
            return True
    return False


def _warning_to_dict(w: PolicyShadowWarning) -> dict[str, str]:
    """Serialise a :class:`PolicyShadowWarning` for the event payload.

    Intentionally drops down to plain ``str`` values so cross-process
    subscribers (SSE / WS bridges) can serialise without depending on
    the security domain types.
    """
    return {
        "shadower_rule_id": w.shadower_rule_id,
        "shadowed_rule_id": w.shadowed_rule_id,
        "scope": w.scope.value,
        "shadower_action": w.shadower_action.value,
        "shadowed_action": w.shadowed_action.value,
        "shadower_pattern": w.shadower_pattern,
        "shadowed_pattern": w.shadowed_pattern,
    }
