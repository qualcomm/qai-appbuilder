# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer bridge: GoMaster REST/stream capability-proxy controller.

Builds the :class:`~qai.model_builder.infrastructure.gomaster_service_adapter.GomasterServiceAdapter`
(implementing :class:`~qai.model_builder.application.gomaster_graph_service.GomasterGraphServicePort`)
at the apps/api composition root — the only layer allowed to import both
``qai.model_builder.infrastructure`` and ``qai.platform.edition`` — and injects
it onto ``container.gomaster_service`` so the ``interfaces.http.routes.gomaster``
routes consume it by duck-typing (interfaces-stays-thin).

Auth (option (a)): the CEFlow Bearer token is resolved server-side — first from
the SecretStore (runtime-provisioned / user-set), then the edition-config
default — and injected by the adapter on every upstream call.

internal-only: returns ``None`` on external editions (the whole edition config +
the adapter module are physically excluded, and this bridge is is_internal
gated). All imports of the excluded packages are **local to the internal-gated
body** so a stripped external tree never triggers an ImportError at di import.
"""

from __future__ import annotations

from typing import Any

from qai.platform.logging import get_logger

__all__ = ["build_gomaster_service_controller"]

_log = get_logger(__name__)

_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"

# gomaster_* sub-path config keys copied verbatim into the adapter so it stays
# free of config-shape knowledge.
_PATH_KEYS = (
    "gomaster_auto_optimize_path",
    "gomaster_qnn_run_stream_path",
    "gomaster_model_graph_path",
    "gomaster_benchmark_pair_path",
    "gomaster_artifacts_path",
    "gomaster_outputs_path",
)


def build_gomaster_service_controller(*, container: Any) -> Any | None:
    """Build the GoMaster REST-proxy adapter, or ``None`` on external editions."""
    settings = getattr(container, "settings", None)
    if settings is None or not getattr(settings, "is_internal", False):
        return None

    try:
        from qai.platform.edition import get_query_services
        from qai.platform.edition.loader import get_cloud_provider_api_keys
        from qai.model_builder.infrastructure.gomaster_service_adapter import (
            GomasterServiceAdapter,
        )
    except Exception:  # pragma: no cover - excluded on external
        return None

    fields = get_query_services().get("gomaster")
    if not fields:
        return None
    # Config switch: this REST/stream capability plane belongs to the ``agent``
    # link (session-scoped auto-optimize/graph/benchmark). Only wire it when
    # gomaster_mode includes "agent"; the ``external`` one-click flow uses the
    # separate GomasterExternalOptimizeAdapter instead.
    if str(fields.get("gomaster_mode", "external")).lower() not in ("agent", "both"):
        return None
    endpoint = fields.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        return None

    verify = bool(fields.get("verify_tls", True)) and not bool(
        fields.get("insecure", False)
    )
    paths: dict[str, str] = {}
    for key in _PATH_KEYS:
        raw = fields.get(key)
        if isinstance(raw, str) and raw:
            paths[key] = raw

    secret_store = getattr(container, "secret_store", None)

    def _token_provider() -> str | None:
        if secret_store is not None:
            try:
                if secret_store.exists(_PROVIDER_SECRET_SERVICE, "gomaster"):
                    val = secret_store.get(_PROVIDER_SECRET_SERVICE, "gomaster")
                    if val:
                        return val
            except Exception:  # noqa: BLE001 — any failure ⇒ fall through
                pass
        try:
            return get_cloud_provider_api_keys().get("gomaster") or None
        except Exception:  # noqa: BLE001
            return None

    return GomasterServiceAdapter(
        base_url=endpoint,
        verify=verify,
        token_provider=_token_provider,
        paths=paths,
    )
