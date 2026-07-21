# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer cross-context bridge: chat local-model resolution → model_runtime.

Local-model routing fix
------------------------

The chat
:class:`~qai.chat.adapters.model_resolver.ProviderAwareModelResolver` needs to
answer "what is the base URL of the running local on-device inference service?"
so a user who selects a ``local::`` model has their chat turn routed to the
live GenieAPIService instead of the empty default endpoint baked into the chat
settings (which degrades to ``"[no LLM endpoint configured]"``).

V1 parity (``host_mode``-aware)
-------------------------------

V1 ``backend/chat_handler.py:_stream_local`` (lines 1832-1842) built
``local_url = "http://{host}:{port}"`` from
``forge_config_manager.service_launch_host`` / ``.service_launch_port``
(``forge_config_manager.py:258-272``), which are **connection-mode aware**:

* ``service_launch.host_mode == "remote"`` → ``(remote_host, remote_port)``
  (the "Remote machine" radio in the Connection panel);
* otherwise (``"local"``)                  → ``("127.0.0.1", local_port)``.

Both ports default to ``8910`` (V1). This bridge reproduces that exact
behaviour for V2, where the inference daemon lives in the ``model_runtime``
bounded context.

Per the import-linter ``context-isolation`` contract, ``qai.chat.*`` may NEVER
import ``qai.model_runtime.*`` directly.  This module — at the ``apps/api``
composition root — is the only place that legitimately sees the chat-side
abstraction (:data:`~qai.chat.application.ports.LocalEndpointProviderPort`)
together with the ``model_runtime`` inference service and the
``forge_config.service_launch`` document.

Resolution strategy
-------------------

1. Read ``forge.config service_launch`` once: ``host_mode`` /
   ``remote_host`` / ``remote_port`` / ``local_port``.
2. **Remote mode** → ``http://{remote_host}:{remote_port}/v1`` (V1
   ``service_launch_host``/``service_launch_port`` remote branch).  When
   ``remote_host`` is blank V1 falls back to the loopback host.
3. **Local mode** → host is always loopback; the port is the **live running
   daemon port** (``model_runtime.inference_service.status()["port"]`` — the
   real port the daemon was started on, e.g. ``9999``) when running, else the
   configured ``local_port``, else the typed Settings default
   (``Settings.model_runtime.default_port``, whose default is the V1 ``8910``).

Returns ``http://{host}:{port}/v1`` or ``None`` when no port can be
determined.  Never raises — any failure yields ``None`` so the resolver falls
back cleanly to the offline notice.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qai.platform.config.settings import LOOPBACK_HOST
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

logger = get_logger(__name__)

__all__ = [
    "ModelRuntimeLocalEndpointBridge",
    "make_local_service_endpoint_provider",
]

#: Loopback host used for local-mode chat traffic, matching V1
#: ``forge_config_manager.service_launch_host`` (returns ``"127.0.0.1"`` in
#: local mode / when ``remote_host`` is blank).  Sourced from the single
#: allow-listed Settings definition (no hard-coded literal in new code;
#: §3.6 ``check_no_magic_host_port`` compliant).
_LOCAL_HOST = LOOPBACK_HOST


class ModelRuntimeLocalEndpointBridge:
    """Resolve the live on-device service base URL via model_runtime (V1 parity).

    A zero-arg async callable satisfying the chat
    :data:`~qai.chat.application.ports.LocalEndpointProviderPort` contract:
    calling the instance returns the service base URL (e.g.
    ``"http://127.0.0.1:9999/v1"`` in local mode, or
    ``"http://<remote_host>:<remote_port>/v1"`` in remote mode) or ``None``.
    Duck-typed container access keeps the bridge tolerant of minimal test
    containers that do not wire every namespace.
    """

    __slots__ = ("_container",)

    def __init__(self, *, container: "Container") -> None:
        self._container = container

    async def __call__(self) -> str | None:
        service_launch = await self._forge_config_service_launch()

        mode = str(service_launch.get("host_mode") or "local").strip().lower()
        if mode == "remote":
            host, port = self._resolve_remote(service_launch)
        else:
            host = _LOCAL_HOST
            port = await self._resolve_local_port(service_launch)

        if not host or port is None:
            return None
        return f"http://{host}:{port}/v1"

    # ------------------------------------------------------------------
    # Remote mode (V1 service_launch_host/port remote branch)
    # ------------------------------------------------------------------
    def _resolve_remote(
        self, service_launch: dict[str, Any]
    ) -> tuple[str, int | None]:
        # V1: remote_host default "" → falls back to loopback; remote_port
        # default 8910.
        host = str(service_launch.get("remote_host") or "").strip() or _LOCAL_HOST
        port = self._coerce_port(service_launch.get("remote_port"))
        if port is None:
            port = self._settings_default_port()
        return host, port

    # ------------------------------------------------------------------
    # Local mode (V1 service_launch_port local branch + live running port)
    # ------------------------------------------------------------------
    async def _resolve_local_port(
        self, service_launch: dict[str, Any]
    ) -> int | None:
        # 1) Live running daemon port (most authoritative — the real port the
        #    daemon was started on, e.g. 9999).
        running_port = await self._running_port()
        if running_port is not None:
            return running_port
        # 2) forge.config service_launch.local_port.
        configured = self._coerce_port(service_launch.get("local_port"))
        if configured is not None:
            return configured
        # 3) Typed Settings default (V1 default 8910, see ModelRuntimeSettings).
        return self._settings_default_port()

    async def _running_port(self) -> int | None:
        model_runtime = getattr(self._container, "model_runtime", None)
        service = getattr(model_runtime, "inference_service", None)
        if service is None:
            return None
        try:
            status = await service.status()
        except Exception as exc:  # noqa: BLE001 — never crash chat routing
            logger.warning(
                "chat.local_endpoint_bridge.status_failed", error=str(exc)
            )
            return None
        if not isinstance(status, dict):
            return None
        if not status.get("running"):
            return None
        return self._coerce_port(status.get("port"))

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------
    async def _forge_config_service_launch(self) -> dict[str, Any]:
        """Return forge.config ``service_launch`` dict, or empty on failure."""
        user_prefs = getattr(self._container, "user_prefs", None)
        load_uc = getattr(user_prefs, "load_document_use_case", None)
        if load_uc is None:
            return {}
        try:
            doc = await load_uc.execute("forge.config")
        except Exception:  # noqa: BLE001 — convenience read; never fatal
            return {}
        if not isinstance(doc, dict):
            return {}
        service_launch = doc.get("service_launch")
        return service_launch if isinstance(service_launch, dict) else {}

    def _settings_default_port(self) -> int | None:
        settings = getattr(self._container, "settings", None)
        model_runtime_cfg = getattr(settings, "model_runtime", None)
        return self._coerce_port(getattr(model_runtime_cfg, "default_port", None))

    @staticmethod
    def _coerce_port(raw: Any) -> int | None:
        if raw is None:
            return None
        try:
            value = int(raw)
        except (ValueError, TypeError):
            return None
        if value < 1 or value > 65535:
            return None
        return value


def make_local_service_endpoint_provider(
    container: "Container",
) -> ModelRuntimeLocalEndpointBridge:
    """Build the chat local-endpoint provider bound to ``container``.

    Factory used by ``apps/api/_chat_di.py`` to wire
    :class:`~qai.chat.adapters.model_resolver.ProviderAwareModelResolver`'s
    ``local_endpoint_provider`` slot.  Returns a
    :class:`ModelRuntimeLocalEndpointBridge` (a zero-arg async callable
    satisfying ``LocalEndpointProviderPort``); construction is duck-typed
    so minimal test containers without ``model_runtime`` / ``user_prefs``
    still build (the bridge degrades to ``None`` at call time).
    """
    return ModelRuntimeLocalEndpointBridge(container=container)
