# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Unified security-audit funnel (application, P-17 §6.3).

Before this facade the security system wrote deny/audit records through
FOUR scattered landing sites with hand-rolled, drifting shapes:

* ``#8`` — the canonical ``security_audit_entry`` table via
  :class:`~qai.security.application.ports.AuditSinkPort` (queryable, feeds
  ``/api/security/audit/recent`` → the AuditLog UI). **The system of record.**
* ``#7`` — ``emergency_audit.jsonl`` (FileGuard fail-closed) — WRITE-ONLY,
  hand-rolled ``json.dumps`` + ``open(...,"a")`` in the apps bridge.
* ``#1`` — ``file_broker_audit.jsonl`` (FileBroker deny) — WRITE-ONLY, a
  SECOND hand-rolled JSONL writer with a slightly DIFFERENT schema (no
  ``caller`` / ``mode`` keys).

This facade is the single funnel. It:

1. builds a canonical domain :class:`AuditEntry` and appends it to the
   injected :class:`AuditSinkPort` (so records that previously vanished into
   write-only JSONL become queryable via the same ``#8`` surface — closing
   the observability gap), and
2. forwards the same event to any injected **JSONL fallback sink(s)** for
   durable last-resort observability when the DB is unavailable (the exact
   scenario ``emergency_audit.jsonl`` exists for). Both writes are INDEPENDENT
   and BEST-EFFORT — one failing never blocks the other, and neither ever
   raises into the caller (audit must never break a fail-closed deny path).

Layering: this is a pure application service. It depends only on the domain
:class:`AuditEntry` / VOs and the ``AuditSinkPort`` protocol + a ``Clock`` /
``IdGenerator``. It performs NO direct IO — the JSONL write is an injected
callable owned by the apps layer (so the ``security`` context stays free of
filesystem coupling and the ``domain-purity`` / ``layered`` contracts hold).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING

from qai.security.domain.entities import AuditEntry
from qai.security.domain.value_objects import PolicyAction, Resource, Subject

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.ids import IdGenerator
    from qai.platform.time import Clock
    from qai.security.application.ports import AuditSinkPort

__all__ = ["JsonlAuditRecord", "SecurityAuditFacade"]


#: The dict shape handed to an injected JSONL fallback sink. Superset of the
#: two historical schemas (#7 emergency: ts/op/path/decision/caller/source/
#: reason/mode; #1 file_broker: ts/op/path/decision/source/reason) so the
#: unified funnel can drive both without losing a field. Keys a sink does not
#: care about are simply ignored by that sink.
JsonlAuditRecord = dict


class SecurityAuditFacade:
    """Single entry point for emitting a security audit record."""

    def __init__(
        self,
        *,
        audit_sink: "AuditSinkPort | None",
        clock: "Clock",
        ids: "IdGenerator",
        jsonl_sinks: "Sequence[Callable[[JsonlAuditRecord], None]] | None" = None,
    ) -> None:
        self._audit_sink = audit_sink
        self._clock = clock
        self._ids = ids
        self._jsonl_sinks: tuple[Callable[[JsonlAuditRecord], None], ...] = (
            tuple(jsonl_sinks) if jsonl_sinks else ()
        )

    async def record(
        self,
        *,
        subject: Subject,
        resource: Resource,
        decision: PolicyAction,
        op: str = "",
        caller: str = "",
        reason: str = "",
        source: str = "",
        mode: str = "",
        rule_id: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        """Emit one audit record through every configured landing site.

        Returns the minted ``audit_id`` (empty string if id generation itself
        failed — the method never raises: audit must not break a deny path).

        The canonical ``AuditSinkPort`` append and each JSONL fallback write
        are independent best-effort operations.
        """
        try:
            audit_id = self._ids.new_id()
        except Exception:  # noqa: BLE001 — never break the caller's deny path
            audit_id = ""

        # (1) canonical queryable sink (#8) — best-effort.
        if self._audit_sink is not None and audit_id:
            try:
                entry = AuditEntry(
                    audit_id=audit_id,
                    occurred_at=self._clock.now(),
                    subject=subject,
                    resource=resource,
                    decision=decision,
                    rule_id=rule_id,
                    correlation_id=correlation_id,
                    note=reason,
                    op=op,
                )
                await self._audit_sink.append(entry)
            except Exception:  # noqa: BLE001 — DB down → JSONL still records it
                pass

        # (2) JSONL fallback sink(s) (#7 / #1) — durable last-resort, best-
        # effort, independent of (1) so a DB outage is still observable.
        if self._jsonl_sinks:
            record: JsonlAuditRecord = {
                "ts": self._iso_now(),
                "op": op,
                "path": resource.identifier,
                "decision": decision.value,
                "caller": caller,
                "source": source,
                "reason": reason,
                "mode": mode,
            }
            for sink in self._jsonl_sinks:
                try:
                    sink(record)
                except Exception:  # noqa: BLE001 — a bad sink cannot break audit
                    pass

        return audit_id

    def _iso_now(self) -> str:
        try:
            return (
                self._clock.now().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            )
        except Exception:  # noqa: BLE001
            return ""
