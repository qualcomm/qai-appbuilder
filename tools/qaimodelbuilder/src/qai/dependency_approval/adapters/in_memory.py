# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory adapter for ``DepBrokerPort`` (PR-603).

This is the production adapter — dep-broker requests are transient
(single-worker, ephemeral queue). Persistence is unnecessary because
pending requests are only meaningful during the current process
lifetime.

The interactive approval loop (:meth:`InMemoryDepBroker.check_and_wait`)
mirrors V1 ``backend/security/dep_broker.py:161-240``: a denied command is
enqueued as a :class:`PendingRequest`, the WebUI is notified, and the
caller blocks until an operator approves / rejects (via :meth:`resolve`)
or the approval timeout elapses (auto-reject — V1 parity). V1 used a
``threading.Event``; V2 runs on a single asyncio loop, so we use an
``asyncio.Event`` per request kept in a side-table (the domain
:class:`PendingRequest` stays pure — no asyncio primitives leak into it).
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from qai.dependency_approval.application.ports import DepInstallNotifier
from qai.dependency_approval.domain import (
    DEFAULT_DENY_ARGS,
    PendingRequest,
    RequestStatus,
    find_denied_args,
    is_dep_install_command,
)
from qai.platform.logging import get_logger

__all__ = ["InMemoryDepBroker"]

_log = get_logger(__name__)

#: V1 parity: ``dep_broker.py:76`` ``approval_timeout_seconds`` default 120s.
_DEFAULT_APPROVAL_TIMEOUT_S = 120.0


def _default_pip_freeze() -> str:
    """Best-effort ``pip freeze`` capture (L-5 audit diff).

    V1 parity ``dep_broker.py:128-141``. Cross-platform (``pip`` exists on
    every supported runtime); never raises — returns ``""`` on any failure
    so the audit diff degrades to a no-op rather than breaking approval.
    """
    try:
        import subprocess

        result = subprocess.run(  # noqa: S603 - fixed argv
            [sys.executable, "-m", "pip", "freeze"],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:  # noqa: BLE001 - best-effort audit capture
        return ""


class InMemoryDepBroker:
    """In-memory implementation of :class:`DepBrokerPort`.

    Thread-safe within a single asyncio event loop (no concurrent
    mutations from multiple threads).
    """

    def __init__(
        self,
        *,
        enabled: bool = False,
        deny_args: tuple[str, ...] = DEFAULT_DENY_ARGS,
        approval_timeout_s: float = _DEFAULT_APPROVAL_TIMEOUT_S,
        notifier: DepInstallNotifier | None = None,
        pip_freeze: Callable[[], str] | None = None,
    ) -> None:
        self._requests: dict[str, PendingRequest] = {}
        #: Per-request resolution gates (id -> asyncio.Event). Kept off the
        #: domain object so :class:`PendingRequest` holds no asyncio types.
        self._events: dict[str, asyncio.Event] = {}
        #: L-5 — per-request ``pip freeze`` snapshot captured at enqueue,
        #: kept off the pure domain object (parallel to ``_events``).
        self._freeze_before: dict[str, str] = {}
        # Master switch. V1 default = ON (``forge_config.json`` dep_broker
        # ``enabled=true``); V2 ships OFF by user decision (2026-06-13) — the
        # operator opts in via /api/security/runtime-config. When disabled,
        # ``check`` / ``check_and_wait`` always allow.
        self._enabled = enabled
        self._deny_args = deny_args
        self._approval_timeout_s = max(0.0, float(approval_timeout_s))
        self._notifier = notifier
        #: L-5 — injectable for tests; defaults to a real best-effort
        #: ``pip freeze`` capture (V1 ``dep_broker.py:128-159`` audit diff).
        self._pip_freeze = pip_freeze or _default_pip_freeze

    # ------------------------------------------------------------------
    # Master switch (V1 reload_config parity)
    # ------------------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)

    # ------------------------------------------------------------------
    # Runtime config (M-3 — V1 reload_config parity, dep_broker.py:282-294)
    # ------------------------------------------------------------------
    @property
    def deny_args(self) -> tuple[str, ...]:
        return tuple(self._deny_args)

    def set_deny_args(self, deny_args: tuple[str, ...] | list[str]) -> None:
        """Replace the denied-argument list (V1 ``reload_config``).

        V1 parity (``dep_broker.py:285`` + ``main.py:3442-3458``): an explicit
        list is stored verbatim. In V1 the *constructor* defaults deny_args
        only when the config KEY is ABSENT; an explicitly-submitted EMPTY list
        is kept as empty — and an empty deny_args means "no args are denied",
        i.e. all dep-install commands are allowed. We honour that exactly:
        clearing the list is a deliberate operator action, NOT silently
        reverted to defaults. Only whitespace-only / non-string entries are
        dropped (V1 ``main.py:3442-3443`` filtered the same way).
        """
        cleaned = tuple(
            a.strip() for a in deny_args if isinstance(a, str) and a.strip()
        )
        self._deny_args = cleaned

    @property
    def approval_timeout_s(self) -> float:
        return self._approval_timeout_s

    def set_approval_timeout_s(self, seconds: float) -> None:
        """Set the approval auto-reject timeout (>= 0; 0 = wait forever)."""
        self._approval_timeout_s = max(0.0, float(seconds))

    # ------------------------------------------------------------------
    # Pending-queue management
    # ------------------------------------------------------------------
    async def get_pending(self) -> list[PendingRequest]:
        """Return all requests currently in PENDING state."""
        return [
            r
            for r in self._requests.values()
            if r.status == RequestStatus.PENDING
        ]

    async def resolve(self, request_id: str, decision: str) -> bool:
        """Resolve a pending request.

        Args:
            request_id: The request's unique id.
            decision: ``"approve"`` or ``"reject"``.

        Returns:
            ``True`` if successfully resolved; ``False`` if not found
            or already resolved.
        """
        req = self._requests.get(request_id)
        if req is None or req.status != RequestStatus.PENDING:
            return False
        if decision == "approve":
            req.status = RequestStatus.APPROVED
        elif decision == "reject":
            req.status = RequestStatus.REJECTED
        else:
            return False
        # Unblock the waiting ``check_and_wait`` (V1 ``req.event.set()``).
        ev = self._events.get(request_id)
        if ev is not None:
            ev.set()
        return True

    # ------------------------------------------------------------------
    # Interception
    # ------------------------------------------------------------------
    def is_dep_install_command(self, command: str) -> bool:
        """Return ``True`` iff ``command`` is a pip/uv install command."""
        return is_dep_install_command(command)

    def check(self, command: str) -> tuple[bool, str]:
        """Return ``(should_block, reason)`` for a dep-install command.

        Non-blocking probe. Block iff the broker is enabled AND ``command``
        contains a denied argument. Disabled broker → always allow.
        """
        if not self._enabled:
            return (False, "")
        denied = find_denied_args(command, self._deny_args)
        if not denied:
            return (False, "")
        reason = (
            f"包含被禁止的参数: {', '.join(denied)}。"
            "依赖安装需用户审批。"
        )
        return (True, reason)

    async def check_and_wait(self, command: str) -> tuple[bool, str]:
        """Full V1 closed loop: enqueue + block until approved/rejected.

        V1 parity ``dep_broker.py:161-240``.
        """
        if not self._enabled:
            return (False, "")
        if not is_dep_install_command(command):
            return (False, "")
        denied = find_denied_args(command, self._deny_args)
        if not denied:
            return (False, "")

        # ── Enqueue the pending request (V1 dep_broker.py:178-188) ────────
        req_id = uuid.uuid4().hex[:12]
        try:
            args = command.split()
        except Exception:  # noqa: BLE001 — defensive; split never raises
            args = [command]
        req = PendingRequest(
            id=req_id,
            command_args=args,
            requester="ai_coding.tool",
            created_at=datetime.now(timezone.utc),
            status=RequestStatus.PENDING,
            command=command,
            denied_args=list(denied),
        )
        event = asyncio.Event()
        self._requests[req_id] = req
        self._events[req_id] = event
        # L-5 — snapshot the environment BEFORE the (potential) install so an
        # approval can diff it against the post-install freeze (V1
        # dep_broker.py:184). Captured off-thread to avoid blocking the loop.
        try:
            self._freeze_before[req_id] = await asyncio.to_thread(
                self._pip_freeze
            )
        except Exception:  # noqa: BLE001 - best-effort audit capture
            self._freeze_before[req_id] = ""

        # ── Notify the WebUI (V1 _broadcast("dep_install_request", ...)) ──
        if self._notifier is not None:
            try:
                await self._notifier(req)
            except Exception:  # noqa: BLE001 — notify is best-effort
                pass

        # ── Block until approval / rejection / timeout ────────────────────
        try:
            if self._approval_timeout_s > 0:
                await asyncio.wait_for(
                    event.wait(), timeout=self._approval_timeout_s
                )
            else:
                await event.wait()
        except asyncio.TimeoutError:
            # Timeout → auto-reject (V1 dep_broker.py:215-218).
            if req.status == RequestStatus.PENDING:
                req.status = RequestStatus.REJECTED
        finally:
            self._events.pop(req_id, None)
            # Clean the request out of the queue once decided (V1
            # dep_broker.py:220-222 removes it from ``_pending``).
            self._requests.pop(req_id, None)

        if req.status == RequestStatus.APPROVED:
            # L-5 — schedule a background freeze diff. The caller runs the
            # install AFTER we return; we give it a short head start, then
            # capture the post-install freeze and log the added/removed
            # packages for audit (V1 dep_broker.py:224-233 background thread).
            before = self._freeze_before.pop(req_id, "")
            if before:
                self._schedule_freeze_diff(before, command)
            return (False, "")
        self._freeze_before.pop(req_id, None)
        reason = (
            f"包含被禁止的参数: {', '.join(denied)}。"
            f"用户已拒绝（或审批超时 {int(self._approval_timeout_s)}s）。"
        )
        return (True, reason)

    def _schedule_freeze_diff(self, before: str, command: str) -> None:
        """Spawn a background task that logs the post-install freeze diff.

        Best-effort, fire-and-forget (V1 ran this on a daemon thread). The
        15s delay lets the approved install actually run before we re-snapshot
        the environment. Never propagates errors.
        """

        async def _run() -> None:
            try:
                await asyncio.sleep(15.0)
                after = await asyncio.to_thread(self._pip_freeze)
                before_set = set(before.strip().splitlines())
                after_set = set(after.strip().splitlines())
                added = sorted(after_set - before_set)
                removed = sorted(before_set - after_set)
                if added or removed:
                    _log.info(
                        "dependency_approval.audit.freeze_diff command=%r "
                        "added=%s removed=%s",
                        command[:100],
                        added[:20],
                        removed[:20],
                    )
            except Exception:  # noqa: BLE001 - audit is best-effort
                _log.debug(
                    "dependency_approval.audit.freeze_diff_failed", exc_info=True
                )

        try:
            asyncio.get_running_loop().create_task(_run())
        except RuntimeError:  # pragma: no cover - no running loop (sync test)
            pass

    # ------------------------------------------------------------------
    # Test / internal helpers (not part of the port contract)
    # ------------------------------------------------------------------

    def add_request(self, request: PendingRequest) -> None:
        """Insert a request into the store (used by tests and callers)."""
        self._requests[request.id] = request
