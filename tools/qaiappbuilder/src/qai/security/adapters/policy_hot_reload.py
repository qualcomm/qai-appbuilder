# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem-mtime watcher driving :class:`UpdatePolicyUseCase`.

PR-092 §2.1 C-9 / §17.5 #10 — restores the legacy policy hot-reload
behaviour from ``backend/security/policy.py:564-572``. A long-running
:func:`asyncio.create_task` polls the watched files'
``os.stat().st_mtime`` once per second; when any mtime changes the
watcher reads the new payload and invokes the update-policy use case
so the Policy aggregate is refreshed without a process restart.

Designed for the ``apps.api.lifespan`` startup path:

.. code-block:: python

    if container.settings.security.policy_hot_reload_enabled:
        watcher = PolicyHotReloadWatcher(
            watched_paths=(policy_yaml_path,),
            update_policy_use_case=container.security.update_policy_use_case,
            loader=load_rules_from_yaml,
        )
        await watcher.start()
        try:
            yield
        finally:
            await watcher.stop()

The watcher does **not** parse the policy file itself — that is the
caller-supplied ``loader`` callback's job. Keeping the parsing
out of the watcher means the same surface works for whatever file
format ``install`` chooses (yaml / json / toml).
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path

from qai.security.adapters.path_pattern import clear_normalise_cache
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from qai.security.application.use_cases.update_policy import (
        UpdatePolicyUseCase,
    )
    from qai.security.domain.entities import PolicyRule

__all__ = ["PolicyHotReloadWatcher"]


_LOGGER = logging.getLogger("qai.security.policy_hot_reload")

_DEFAULT_INTERVAL_SECONDS: float = 1.0


class PolicyHotReloadWatcher:
    """Async polling watcher: re-runs :class:`UpdatePolicyUseCase` on mtime change.

    Concurrency model:

    * One :func:`asyncio.create_task` per watcher; the task is
      cancelled on :meth:`stop`.
    * Each iteration runs ``os.stat`` for every watched path under
      :func:`asyncio.to_thread` so blocking syscalls don't stall the
      event loop on slow disks.
    * Loader / use case execution happens in the event loop; loaders
      that perform sync IO should themselves dispatch via
      :func:`asyncio.to_thread`.
    """

    __slots__ = (
        "_watched_paths",
        "_update_use_case",
        "_loader",
        "_interval",
        "_task",
        "_mtimes",
        "_stopped",
    )

    def __init__(
        self,
        *,
        watched_paths: Sequence["str | Path"],
        update_policy_use_case: "UpdatePolicyUseCase",
        loader: Callable[
            [], "Awaitable[tuple[PolicyRule, ...]] | tuple[PolicyRule, ...]"
        ],
        interval_seconds: float = _DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        if not watched_paths:
            raise ValueError("watched_paths must not be empty")
        if interval_seconds <= 0:
            raise ValueError(
                f"interval_seconds must be > 0, got {interval_seconds}"
            )
        self._watched_paths: tuple[Path, ...] = tuple(
            Path(p) for p in watched_paths
        )
        self._update_use_case = update_policy_use_case
        self._loader = loader
        self._interval = float(interval_seconds)
        self._task: asyncio.Task[None] | None = None
        self._mtimes: dict[str, float] = {}
        self._stopped = False

    async def start(self) -> None:
        """Begin polling. Idempotent — repeated calls are no-ops."""

        if self._task is not None and not self._task.done():
            return
        self._stopped = False
        # Seed mtimes so the first poll only fires when files actually
        # change after start-up (avoids redundant initial reload).
        self._mtimes = await asyncio.to_thread(
            self._snapshot_mtimes, self._watched_paths
        )
        self._task = asyncio.create_task(
            self._run(), name="qai.security.policy_hot_reload"
        )

    async def stop(self) -> None:
        """Cancel the watcher task and wait for it to exit."""

        self._stopped = True
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:  # pragma: no cover
            # We cancelled the watcher task ourselves; benign on stop().
            pass
        except Exception:  # noqa: BLE001 - best-effort shutdown
            _LOGGER.warning(
                "qai.security.policy_hot_reload.stop_task_cleanup_failed",
                exc_info=True,
            )

    async def _run(self) -> None:
        try:
            while not self._stopped:
                try:
                    await asyncio.sleep(self._interval)
                except asyncio.CancelledError:
                    return
                if self._stopped:
                    return
                try:
                    new_mtimes = await asyncio.to_thread(
                        self._snapshot_mtimes, self._watched_paths
                    )
                except Exception as exc:  # pragma: no cover - hardening
                    _LOGGER.debug(
                        "policy_hot_reload: stat failed (%s)", exc
                    )
                    continue
                if new_mtimes == self._mtimes:
                    continue
                self._mtimes = new_mtimes
                await self._reload()
        except asyncio.CancelledError:
            return

    # M-Sec-1 (fileguard-audit-2026-07-26) — DESIGN NOTE: policy-rule reload
    # is deliberately ORTHOGONAL to PathGrants, and does NOT participate in a
    # cross-table transaction with the grant repository.
    #
    # Rationale: a PolicyRule is a static allow/deny/ask classification of a
    # path pattern; a PathGrant is a subject-scoped ACL minted at ASK-approve
    # time (session / process / permanent). They are separate aggregates with
    # separate lifecycles. A rule reload changes the *default* classification
    # of a path, but it never invalidates a grant a user explicitly issued —
    # a grant is an override the operator consciously made and must survive a
    # config edit. Conversely, revoking now-"orphaned" grants inside this
    # watcher would (a) require a cross-context UoW spanning policy + grant
    # repos inside a best-effort background sweep (fragile), and (b) silently
    # delete authorizations the user still expects to hold.
    #
    # The check_permission cascade already re-reads BOTH the reloaded rules
    # AND the live grants on every decision (hard-deny rules beat grants; see
    # CheckPermissionUseCase), so a reload that tightens a rule is honoured
    # immediately for the deny direction WITHOUT touching grants. Grant expiry
    # is handled independently by TTL (session/process) + the pending-cleanup
    # sweep. No cross-table transaction is needed or wanted here.
    async def _reload(self) -> None:
        try:
            payload = self._loader()
            if asyncio.iscoroutine(payload):
                rules = await payload
            else:
                rules = payload
            if not isinstance(rules, tuple):
                rules = tuple(rules)
            await self._update_use_case.execute(
                new_rules=rules,
                reboot_reason="policy file mtime changed",
            )
            # L-Sec-1 review fix (2026-07-29): drop the path-normalise LRU on
            # every reload. The cache keys `normalize_path(str)` results by
            # input string; when the ruleset changes the cached *outputs*
            # remain semantically valid (normalization is pure), but a
            # reload IS the documented invalidation point (see
            # ``clear_normalise_cache`` docstring). Keep the call inside the
            # try/except so a hypothetical clear-failure never blocks the
            # actual reload log below.
            try:
                clear_normalise_cache()
            except Exception:  # noqa: BLE001 — hardening only
                _LOGGER.warning(
                    "policy_hot_reload: clear_normalise_cache failed",
                    exc_info=True,
                )
            _LOGGER.info(
                "policy_hot_reload: reloaded %d rule(s) from %s",
                len(rules),
                ", ".join(str(p) for p in self._watched_paths),
            )
        except Exception as exc:  # pragma: no cover - hardening
            _LOGGER.warning(
                "policy_hot_reload: reload failed (%s)", exc, exc_info=True
            )

    @staticmethod
    def _snapshot_mtimes(paths: tuple[Path, ...]) -> dict[str, float]:
        out: dict[str, float] = {}
        for p in paths:
            try:
                out[str(p)] = os.stat(p).st_mtime
            except OSError:
                # Missing file — record sentinel so a future create
                # event still triggers a reload via mtime delta.
                out[str(p)] = -1.0
        return out
