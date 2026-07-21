# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Skill-capability registration / unregistration use cases (PR-504).

The :class:`RegisterSkillCapabilityUseCase` is the security-side
gate skills go through before they appear in the runtime registry:

1. Run a configurable scanner (default:
   :func:`qai.security.infrastructure.skill_injection_scanner.scan`,
   injected at construction time so the application layer stays
   infrastructure-free) over the skill body.
2. If any *high*-severity threat is detected, raise
   :class:`SkillCapabilityViolation` — registration is blocked.
3. Otherwise admit the capability (medium-severity threats are
   recorded as ``scanner_warnings`` for the operator dashboard).
4. Append an :class:`AuditEntry` describing the registration.

The :class:`UnregisterSkillCapabilityUseCase` is the symmetric tear-
down: removes the capability from the registry and audits the
deregistration.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Sequence

from qai.platform.events import EventBus
from qai.platform.ids import IdGenerator
from qai.platform.time import Clock
from qai.security.domain.entities import AuditEntry
from qai.security.domain.skill_capability import (
    SkillCapability,
    SkillCapabilityViolation,
)
from qai.security.domain.value_objects import (
    PolicyAction,
    Resource,
    Subject,
)

from ..ports import (
    AuditSinkPort,
    SkillCapabilityRegistryPort,
)

__all__ = [
    "RegisterSkillCapabilityResult",
    "RegisterSkillCapabilityUseCase",
    "UnregisterSkillCapabilityUseCase",
]


# Type alias for the injected scanner function. Accepts a string body
# and returns an iterable of objects that expose ``pattern_name``,
# ``severity``, and ``matched_text`` attributes (the
# :class:`qai.security.infrastructure.skill_injection_scanner.Threat`
# protocol shape).
SkillBodyScanner = Callable[[str], Sequence[Any]]


def _empty_scanner(body: str) -> tuple[Any, ...]:
    """Default no-op scanner used when caller doesn't inject one.

    Returning the empty tuple admits any skill body. The DI wiring in
    ``apps/api/_security_di.py`` always passes the real
    ``infrastructure.skill_injection_scanner.scan`` so production
    flows do scan; this default is a safety net for tests that
    construct the use case ad-hoc.
    """
    return ()


@dataclass(frozen=True, slots=True, kw_only=True)
class RegisterSkillCapabilityResult:
    """Outcome of a successful capability registration."""

    skill_name: str
    audit_id: str
    scanner_warnings: tuple[str, ...]


class RegisterSkillCapabilityUseCase:
    def __init__(
        self,
        *,
        registry: SkillCapabilityRegistryPort,
        audit_sink: AuditSinkPort,
        clock: Clock,
        ids: IdGenerator,
        scanner: SkillBodyScanner = _empty_scanner,
        event_bus: EventBus | None = None,
    ) -> None:
        self._registry = registry
        self._audit_sink = audit_sink
        self._clock = clock
        self._ids = ids
        self._scanner = scanner
        self._event_bus = event_bus

    async def execute(
        self,
        *,
        skill_name: str,
        capability: SkillCapability,
        skill_body: str = "",
    ) -> RegisterSkillCapabilityResult:
        if not skill_name or not isinstance(skill_name, str):
            raise ValueError(
                f"skill_name must be a non-empty str, got {skill_name!r}"
            )
        threats = (
            tuple(self._scanner(skill_body)) if skill_body else ()
        )
        high = tuple(t for t in threats if t.severity == "high")
        warnings = tuple(
            t.pattern_name for t in threats if t.severity != "high"
        )
        if high:
            raise SkillCapabilityViolation(
                f"skill {skill_name!r} blocked: high-severity injection "
                f"threat ({', '.join(t.pattern_name for t in high)})",
                details={
                    "skill_name": skill_name,
                    "capability": capability.capability_name,
                    "threats": [
                        {
                            "pattern_name": t.pattern_name,
                            "severity": t.severity,
                            "matched_text": t.matched_text,
                        }
                        for t in threats
                    ],
                },
            )

        await self._registry.register(
            skill_name,
            capability,
            scanner_warnings=warnings,
        )

        audit_id = self._ids.new_id()
        note = (
            f"skill_capability_register: {skill_name} "
            f"(capability={capability.capability_name}, "
            f"warnings={len(warnings)})"
        )
        await self._audit_sink.append(
            AuditEntry(
                audit_id=audit_id,
                occurred_at=self._clock.now(),
                subject=Subject(
                    kind="system",
                    identifier="skill_capability_register",
                ),
                resource=Resource(kind="skill", identifier=skill_name),
                decision=PolicyAction.ALLOW,
                rule_id=None,
                note=note[:2048],
            )
        )

        return RegisterSkillCapabilityResult(
            skill_name=skill_name,
            audit_id=audit_id,
            scanner_warnings=warnings,
        )


class UnregisterSkillCapabilityUseCase:
    def __init__(
        self,
        *,
        registry: SkillCapabilityRegistryPort,
        audit_sink: AuditSinkPort,
        clock: Clock,
        ids: IdGenerator,
    ) -> None:
        self._registry = registry
        self._audit_sink = audit_sink
        self._clock = clock
        self._ids = ids

    async def execute(self, *, skill_name: str) -> str:
        """Remove ``skill_name`` from the registry and write an audit row.

        Returns the audit id. Idempotent — calling it twice for the
        same skill is fine; the second call simply writes another
        audit row.
        """
        if not skill_name or not isinstance(skill_name, str):
            raise ValueError(
                f"skill_name must be a non-empty str, got {skill_name!r}"
            )
        await self._registry.unregister(skill_name)
        audit_id = self._ids.new_id()
        await self._audit_sink.append(
            AuditEntry(
                audit_id=audit_id,
                occurred_at=self._clock.now(),
                subject=Subject(
                    kind="system",
                    identifier="skill_capability_unregister",
                ),
                resource=Resource(kind="skill", identifier=skill_name),
                decision=PolicyAction.ALLOW,
                rule_id=None,
                note=f"skill_capability_unregister: {skill_name}",
            )
        )
        return audit_id
