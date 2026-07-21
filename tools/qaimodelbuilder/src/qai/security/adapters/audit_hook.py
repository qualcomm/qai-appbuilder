# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Adapter wrapping :mod:`qai.security.infrastructure.audit_hook` (PR-502).

The infrastructure module exposes a process-level
``install_audit_hook(...)`` factory; this adapter narrows that surface
to the :class:`qai.security.application.ports.AuditHookPort` Protocol so
the DI container, lifespan and route layer can manipulate the hook
without importing infrastructure directly.

Construction is **lazy**: the adapter does NOT call
``install_audit_hook`` in ``__init__``. The DI build runs at process
start while pytest test runs, ``install`` and other tooling all
import ``apps.api`` — installing the audit hook unconditionally there
would silently police all test IO. The lifespan hook (registered in
``apps.api.lifespan``) is the one place that decides — based on
``SecuritySettings.audit_hook_enabled`` — whether to flip the switch.

Typical wiring:

.. code-block:: python

    container = build_container()
    # Optional: seed a fresh policy snapshot from the repository
    container.security.audit_hook.set_policy_provider(
        lambda: container.security.policy_cache.snapshot()
    )
    if container.settings.security.audit_hook_enabled:
        container.security.audit_hook.install()
    try:
        yield
    finally:
        container.security.audit_hook.uninstall()
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from qai.security.domain.entities import Policy
from qai.security.infrastructure.audit_hook import (
    AuditHookHandle,
    Decision,
    install_audit_hook,
)

__all__ = ["AuditHookAdapter"]


def _empty_policy_provider() -> Policy:
    """Default no-op provider used until a real one is wired in.

    Returns a fresh empty :class:`Policy` so the audit hook's first
    decision is always :class:`Decision.DENY_DEFAULT`. This keeps the
    hook fail-closed if the lifespan layer ever forgets to call
    :meth:`AuditHookAdapter.set_policy_provider` — which in turn means
    integration tests will surface the wiring gap immediately rather
    than silently letting IO through.
    """

    return Policy.empty(now=datetime.now(timezone.utc))


class AuditHookAdapter:
    """Concrete :class:`AuditHookPort` driving the singleton audit hook.

    Holds the configuration the lifespan / DI layer hands in:

    * ``policy_provider`` — synchronous callable returning a current
      :class:`Policy` snapshot. Implementations typically wrap a cached
      version updated by ``UpdatePolicyUseCase`` after each save.
      Defaults to :func:`_empty_policy_provider` so the hook is
      fail-closed during any DI build window where the real provider
      has not been seeded yet.
    * ``on_violation`` — optional callback invoked just before the hook
      raises ``PermissionError``. Useful for stamping a synchronous
      audit row through a thread-safe queue.
    * ``extra_baseline_prefixes`` — additional path prefixes (typically
      the project root) that should bypass the Policy.

    The adapter never exposes the underlying :class:`AuditHookHandle`;
    callers go through :meth:`install` / :meth:`uninstall` and the
    :attr:`installed` property only.
    """

    __slots__ = (
        "_policy_provider",
        "_on_violation",
        "_extra_baseline_prefixes",
        "_extra_events",
        "_handle",
    )

    def __init__(
        self,
        *,
        policy_provider: Callable[[], Policy] | None = None,
        on_violation: Callable[[Decision, str, str], None] | None = None,
        extra_baseline_prefixes: tuple[str, ...] = (),
        extra_events: tuple[str, ...] = (),
    ) -> None:
        self._policy_provider: Callable[[], Policy] = (
            policy_provider if policy_provider is not None
            else _empty_policy_provider
        )
        self._on_violation = on_violation
        self._extra_baseline_prefixes = tuple(extra_baseline_prefixes)
        # PR-092 §2.2 H-17 / §17.5 #4 — extra audit events to handle on
        # top of the built-in set (typically driven by
        # ``Settings.security.audit_hook_extra_events``).
        self._extra_events = tuple(extra_events)
        self._handle: AuditHookHandle | None = None

    def set_policy_provider(
        self, provider: Callable[[], Policy]
    ) -> None:
        """Replace the policy provider used by subsequent ``install`` calls.

        If the hook is already installed, this also refreshes the live
        provider in place (the hook is re-installed idempotently).
        """

        self._policy_provider = provider
        if self._handle is not None and self._handle.installed:
            self.install()

    def install(self) -> None:
        """Install (or refresh) the audit hook.

        Idempotent: repeated calls re-register the same callback
        in-place and refresh the policy_provider / baseline state.
        """

        self._handle = install_audit_hook(
            policy_provider=self._policy_provider,
            on_violation=self._on_violation,
            extra_baseline_prefixes=self._extra_baseline_prefixes,
            extra_events=self._extra_events,
        )

    def uninstall(self) -> None:
        """Soft-disable the audit hook (idempotent)."""

        if self._handle is not None:
            self._handle.uninstall()

    @property
    def installed(self) -> bool:
        """``True`` while the hook is actively making decisions."""

        if self._handle is None:
            return False
        return self._handle.installed
