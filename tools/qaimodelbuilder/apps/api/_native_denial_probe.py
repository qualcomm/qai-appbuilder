# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Native FileGuard denial probe factory (D2-D composition helper).

Composes :meth:`~qai.security.application.ports.AuditQueryPort.query_native_denies_by_pid_tree`
with :func:`~qai.security.domain.native_guard_denial_message.build_native_guard_denial_note`
into a single stdlib-typed async callable that the ``exec`` handler and the
``background_process`` manager can accept without importing
``qai.security.**`` (their ``context-isolation`` import-linter contract
forbids that cross-context edge).

This is the ONLY layer that reads both ``qai.security`` APIs — apps/api is
the composition root, exactly where cross-context glue belongs.

Fail-open by design:

* When ``container.security`` or ``container.security.audit_query`` is not
  wired (headless / test container), the factory returns a no-op probe that
  always returns ``""`` — downstream callers append ``""`` and see no
  behaviour change from the pre-D2 baseline.
* When the audit query itself raises (DB error, protocol mismatch, whatever)
  the returned probe catches everything and returns ``""``. The
  ``AuditQueryPort.query_native_denies_by_pid_tree`` contract already says
  "never raises", but the belt-and-braces catch protects against a future
  contract drift or a mis-wired adapter.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — imports only for typing
    from .di import Container

__all__ = ["NativeDenialProbe", "build_native_denial_probe"]


#: The stdlib-typed callable shape both ``exec`` and ``background_process``
#: accept. Structurally identical to
#: :data:`qai.ai_coding.infrastructure.tools.handlers.exec.NativeGuardDenialProbe`
#: and
#: :data:`qai.platform.background_process.manager.NativeGuardDenialProbe`
#: (deliberately duplicated in the apps composition layer so this module
#: does not import either bounded context; each context owns its own type
#: alias and the shapes are kept in sync by a single conceptual contract).
NativeDenialProbe = Callable[[int, datetime], Awaitable[str]]


async def _noop_probe(_root_pid: int, _since: datetime) -> str:
    """Fallback probe returned when audit query is not wired.

    Always returns ``""`` so callers can unconditionally append the result.
    """
    return ""


def build_native_denial_probe(container: "Container") -> NativeDenialProbe:
    """Compose the FileGuard denial probe from the container's security context.

    Returns :func:`_noop_probe` when ``container.security`` or its
    ``audit_query`` is not wired (headless / test container / early-boot
    caller). Otherwise returns a closure over the live audit query port.

    The closure NEVER raises — a query failure yields ``""`` and the caller
    (exec exit_diagnostics assembler, background_process ``_on_exit``)
    proceeds as if there were no denials to report.
    """
    security = getattr(container, "security", None)
    audit_query = getattr(security, "audit_query", None) if security else None
    if audit_query is None:
        return _noop_probe

    # Import lazily inside the closure so a container without ``security``
    # never pays the qai.security import cost.
    from qai.security.domain.native_guard_denial_message import (
        build_native_guard_denial_note,
    )

    async def _probe(root_pid: int, since: datetime) -> str:
        try:
            denies = await audit_query.query_native_denies_by_pid_tree(
                root_pid=root_pid,
                since=since,
            )
        except Exception:  # noqa: BLE001 — belt-and-braces; port claims never-raise
            return ""
        try:
            return build_native_guard_denial_note(denies)
        except Exception:  # noqa: BLE001 — pure function but same defensive stance
            return ""

    return _probe
