# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""AI coding HTTP routes for both Claude Code (CC) and OpenCode (OC).

S3 PR-035 scope (22 endpoints = 11 templates x 2 providers):

For each provider in {``cc``, ``oc``}:

- ``POST   /api/{cc|oc}/sessions``                            spawn
- ``GET    /api/{cc|oc}/sessions``                            list active
- ``GET    /api/{cc|oc}/sessions/history/all``                list all (history)
- ``DELETE /api/{cc|oc}/sessions/{session_id}``               terminate
- ``GET    /api/{cc|oc}/sessions/{session_id}/stream``        SSE stream
- ``POST   /api/{cc|oc}/sessions/{session_id}/tools/invoke``  invoke tool
- ``POST   /api/{cc|oc}/sessions/{session_id}/permissions``   request permission
- ``POST   /api/{cc|oc}/permissions/{request_id}/decide``     decide permission
- ``GET    /api/{cc|oc}/skills``                              discover skills
- ``POST   /api/{cc|oc}/skills``                              register skill
- ``GET    /api/{cc|oc}/health``                              provider health

Provider-abstraction design
---------------------------
The legacy backend ships **two** parallel implementations
(``ClaudeCodeSessionManager`` 2,600 LoC + the OpenCode counterpart). PR-023
collapsed them into a single ``CodingSession`` aggregate parameterised by
``Provider``; the application use cases (``SpawnCodingSessionUseCase``,
``StreamCodingSessionUseCase``, ...) are therefore **shared** between CC
and OC. This routes module mirrors that intent: it builds two sub-routers
sharing the same handlers, differing only in URL prefix and the
``Provider`` value bound at handler-construction time. There is **one**
:class:`CodingProviderPort` instance for the whole context (advertising
both providers via ``available_providers``).

SSE frame contract (S3-spec §4.4)
---------------------------------
The streaming endpoint emits ``text/event-stream`` with the following
shape; this contract is shared with PR-033 chat (see
``PR-035-manifest.md`` §"Coordination requests" for the cross-PR sync
status):

* message frame:   ``event: message\\ndata: <json>\\n\\n``
* error frame:     ``event: error\\ndata: <QaiError.to_dict()>\\n\\n``
                   followed by stream close
* heartbeat:       ``: ping\\n\\n`` (comment, emitted only when the use
                   case yields a ``ping``-kind frame; routes do not
                   inject heartbeats themselves)
* completion:      ``event: done\\ndata: {}\\n\\n``

Cross-PR / cross-context boundaries
-----------------------------------
* Imports are restricted to ``qai.ai_coding.application.*`` (use cases +
  ports + commands) and ``qai.ai_coding.domain.*`` (value objects /
  errors used in DTOs). No ``qai.ai_coding.adapters`` /
  ``infrastructure``. No other context (chat / security / channels)
  imported.
* Permissions decided here are **internal** to ``CodingSession`` and
  distinct from the security context's ``PermissionRequest`` aggregate
  (see PR-023 manifest §6.1).

Package layout
--------------
This surface was originally a single 2,487-line module; it is now split
into focused sibling modules (pure architectural refactor, zero
behaviour change):

* :mod:`._dto`             — shared wire DTOs + conversion helpers
* :mod:`._provider`        — the 11 shared CC/OC endpoints
* :mod:`._sessions`        — deferred legacy session routes
* :mod:`._config`          — config + credentials routes
* :mod:`._session_ext`     — abort / revert / checkpoint / context routes
* :mod:`._oc_service`      — OC-only subprocess control routes
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any  # noqa: F401 — ``Any`` re-exported for §schema pin

from fastapi import APIRouter

from qai.ai_coding.domain import Provider

from ._config import _CONFIG_KEY_WHITELIST, _register_cc_config_routes
from ._dto import _build_session_config
from ._oc_service import _register_oc_service_routes
from ._provider import _register_provider_routes
from ._session_ext import _register_session_extension_routes
from ._sessions import _register_cc_only_routes

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


def build_router(*, container: "Container") -> APIRouter:
    """Build the ai_coding router (mounts both ``/api/cc`` and ``/api/oc``).

    The returned router has no module-level state; it is rebuilt on
    every ``apps.api.main.create_app`` call. Both sub-routers share the
    same underlying use cases and ``CodingProviderPort`` instance.

    PR-105 expansions:

    * the previously CC-only ``_register_cc_only_routes`` helper now
      mounts on the OC sub-router as well (with ``oc-user-`` history
      ids), giving us the 11 deferred OC twin routes (sessions/*).
    * the previously CC-only ``_register_cc_config_routes`` helper
      now mounts on both sub-routers (the OC mount uses the OC
      SecretStore namespace + KV key under the hood).  Adds PUT
      ``/config`` for OC parity (CC continues to expose POST too).
    * a new ``_register_session_extension_routes`` helper attaches
      abort / revert / checkpoint / rewind / context_usage /
      context_size routes — mounted on BOTH CC and OC.
    * a new ``_register_oc_service_routes`` helper attaches the 4
      OC-only subprocess control routes (start / stop / status /
      logs).
    """
    aggregate = APIRouter(tags=["ai_coding"])

    cc_router = APIRouter(prefix="/api/cc", tags=["ai_coding", "cc"])
    _register_provider_routes(
        cc_router, container=container, provider=Provider.CLAUDE_CODE
    )
    # PR-104a + PR-105: deferred legacy session routes (sessions/*).
    # Same helper, CC parameters.
    _register_cc_only_routes(
        cc_router,
        container=container,
        provider=Provider.CLAUDE_CODE,
        url_prefix="/api/cc",
        history_id_prefix="cc-user-",
        history_source="claude_code",
    )
    # PR-104b + PR-105: config + credentials routes.  CC binding.
    _register_cc_config_routes(
        cc_router,
        container=container,
        provider=Provider.CLAUDE_CODE,
    )
    # PR-105: session-extension routes — CC twin.
    _register_session_extension_routes(
        cc_router,
        container=container,
        provider=Provider.CLAUDE_CODE,
        history_id_prefix="cc-user-",
    )

    oc_router = APIRouter(prefix="/api/oc", tags=["ai_coding", "oc"])
    _register_provider_routes(
        oc_router, container=container, provider=Provider.OPEN_CODE
    )
    # PR-105: OC twins of the deferred legacy session routes.
    _register_cc_only_routes(
        oc_router,
        container=container,
        provider=Provider.OPEN_CODE,
        url_prefix="/api/oc",
        history_id_prefix="oc-user-",
        history_source="opencode",
    )
    # PR-105: OC twins of the config + credentials routes.
    _register_cc_config_routes(
        oc_router,
        container=container,
        provider=Provider.OPEN_CODE,
    )
    # PR-105: session-extension routes — OC twin (same shapes as CC).
    _register_session_extension_routes(
        oc_router,
        container=container,
        provider=Provider.OPEN_CODE,
        history_id_prefix="oc-user-",
    )
    # PR-105: OC-only subprocess control routes.
    _register_oc_service_routes(oc_router, container=container)

    aggregate.include_router(cc_router)
    aggregate.include_router(oc_router)
    return aggregate


__all__ = ["build_router", "_CONFIG_KEY_WHITELIST", "_build_session_config"]
