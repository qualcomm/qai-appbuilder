# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Sticky worker subsystem — long-running, multi-model App Builder host.

Re-implements the legacy
``backend/app_builder/runners/{sticky_worker,_runner_bootstrap}.py`` pair
inside the App Builder bounded context, structured so the host code is
testable in isolation and the bootstrap subprocess protocol is small,
line-oriented, and version-tagged.

Design SSOT: ``docs/30-ui-ux/voice-input-and-sticky-worker-multimodel.md``
(875 lines).

PR-301 scope (this package):

* :class:`StickyWorkerHost` — asyncio-side process manager;
* :class:`BootstrapProtocol` — JSON-line stdin/stdout RPC codec used by
  both host and the bootstrap entry point;
* :class:`LoadedModelEntry` — per-model bookkeeping inside the host.

PR-302 will follow up with the **runner_protocol v3.1** payload-level
event semantics (``status / result / done / progress / metrics / error``)
and a real ``_default_resolver`` that maps a model + variant to a
``ProcessExecutionRequest`` for the QAIRT worker entry script.

Public surface
--------------

The host is consumed by the app_builder DI module
(:mod:`apps.api._app_builder_di`) which wires it into the new
:class:`qai.app_builder.adapters.StickyWorkerStatusAdapter`. The HTTP
route layer never touches the host directly — it goes through the
``GetWorkerStatusUseCase`` so the layered import-linter contract holds.
"""

from __future__ import annotations

from .host import BootstrapSpec, StickyWorkerHost, StickyWorkerSpawnError
from .models import LoadedModelEntry, LoadModelRequest, RunRequest, WorkerEvent
from .protocol import BootstrapProtocol, ProtocolError, ProtocolFrame

__all__ = [
    "BootstrapProtocol",
    "BootstrapSpec",
    "LoadModelRequest",
    "LoadedModelEntry",
    "ProtocolError",
    "ProtocolFrame",
    "RunRequest",
    "StickyWorkerHost",
    "StickyWorkerSpawnError",
    "WorkerEvent",
]
