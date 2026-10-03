# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Sticky-worker stop/restart adapter for QAIRT SDK hot-switching.

:class:`StickyWorkerLifecycle` is the only surface the QAIRT switch
service (``qai.platform.qairt_switch.service``) touches to recycle the
persistent App Builder worker. It is deliberately a thin, pure adapter:

* it never imports FastAPI, ``apps.api.di.Container``, or any
  ``apps.api.lifespan`` helper;
* every dependency is injected as a plain callable, so unit tests build
  one with in-process fakes instead of a real Container / subprocess.

The composition root (``apps.api._app_builder_di``) is responsible for
building the ``spawn_host`` callable from the *same* spec-construction
logic the boot-time lifespan hook uses (interpreter/QAIRT-env
resolution, FileGuard trust token, global proxy), so a switch-triggered
restart is behaviorally identical to the original boot-time spawn.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from .host import StickyWorkerHost

__all__ = ["StickyWorkerLifecycle"]

#: Zero-argument async factory that builds a fresh :class:`BootstrapSpec`
#: (re-reading ``qairt_env.json``, the FileGuard trust token, and the live
#: global proxy at call time — never cached from an earlier call), spawns
#: the worker subprocess, and returns the live host. Raises on failure —
#: callers decide whether a spawn failure should be swallowed (boot-time
#: graceful degradation) or propagated (switch-time rollback trigger).
SpawnHostFn = Callable[[], Awaitable["StickyWorkerHost"]]

#: Live getter/setter pair the lifecycle uses to read/write the single
#: process-wide "current sticky worker" reference (``container.
#: sticky_worker_host`` in production; a plain mutable box in tests).
HostGetter = Callable[[], "StickyWorkerHost | None"]
HostSetter = Callable[["StickyWorkerHost | None"], None]


class StickyWorkerLifecycle:
    """Stop/restart the persistent sticky worker around a QAIRT SDK switch.

    Not a general-purpose worker manager — it exists solely to give the
    QAIRT switch transaction a safe way to release the worker's held
    ``qai_appbuilder`` extension/DLLs before the wheel is replaced, then
    bring a fresh worker back up under the newly-activated QAIRT SDK.
    """

    __slots__ = ("_host_getter", "_host_setter", "_spawn_host")

    def __init__(
        self,
        *,
        host_getter: HostGetter,
        host_setter: HostSetter,
        spawn_host: SpawnHostFn,
    ) -> None:
        self._host_getter = host_getter
        self._host_setter = host_setter
        self._spawn_host = spawn_host

    @property
    def running(self) -> bool:
        """``True`` iff a live sticky worker is currently spawned."""
        host = self._host_getter()
        return host is not None and host.alive

    async def stop_for_qairt_switch(self) -> bool:
        """Gracefully stop the current worker, if any, before a switch.

        Uses the host's own graceful-shutdown-then-forced-kill sequence
        (:meth:`StickyWorkerHost.shutdown`) so no half-terminated process
        keeps the ``qai_appbuilder`` DLLs loaded while the switch service
        tries to replace the wheel.

        Returns ``True`` iff a live worker was actually stopped (the
        caller uses this to decide whether :meth:`start_after_qairt_switch`
        should be called during rollback/success); returns ``False`` when
        no worker was running (idempotent no-op).
        """
        host = self._host_getter()
        if host is None or not host.alive:
            self._host_setter(None)
            return False
        await host.shutdown(reason="qairt_switch")
        self._host_setter(None)
        return True

    async def start_after_qairt_switch(self) -> None:
        """Spawn a fresh worker under the now-active QAIRT SDK.

        Unlike the boot-time spawn (which swallows failures so a bad SDK
        never aborts application startup), this call lets a spawn
        failure propagate: the switch service's transaction treats it as
        a rollback trigger, not a silent degrade-to-one-shot.
        """
        host = await self._spawn_host()
        self._host_setter(host)
