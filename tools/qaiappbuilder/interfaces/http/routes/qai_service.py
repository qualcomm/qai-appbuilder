# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Opt-in QAI Service gateway routes -- ``/api/qai-service/*``.

QAI Service ships DISABLED: no address, no provider entry, no models. It comes
into existence only when the user types the gateway address in Settings ->
Cloud Models and confirms. The address lives in the ``qai-service`` provider
entry (single persisted source of truth) and is mirrored into the process-wide
holder in :mod:`interfaces.http.auth.qai_service_token`, which is what the
SSO-token exchange and the quota proxy consult.

* ``GET    /api/qai-service``         -- status snapshot
* ``POST   /api/qai-service/connect`` -- save the address (no network I/O)
* ``POST   /api/qai-service/sync``    -- pull the model roster (needs the JWT)
* ``DELETE /api/qai-service``         -- forget the address, models and JWT

``connect`` deliberately does NOT contact the gateway: the JWT can only be
minted inside the Okta ``/callback``, so the frontend follows ``connect`` with a
silent re-login and then calls ``sync``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from interfaces.http.auth.qai_service_token import (
    clear_service_jwt,
    get_gateway_url,
    get_service_jwt,
    normalize_gateway_url,
    set_gateway_url,
)
from qai.model_catalog.application.use_cases.list_provider_configs import (
    QAI_SERVICE_PROVIDER_ID,
)
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container

logger = get_logger(__name__)

__all__ = ["build_router", "hydrate_gateway_url"]


class ConnectRequest(BaseModel):
    url: str


def _origin_from_provider(config: dict[str, Any] | None) -> str:
    base = config.get("base_url") if isinstance(config, dict) else None
    return normalize_gateway_url(base) if isinstance(base, str) else ""


async def hydrate_gateway_url(container: "Container") -> None:
    """Fill the gateway holder from the stored provider entry (startup)."""
    try:
        config = await container.model_catalog.provider_registry.get_provider_config(
            QAI_SERVICE_PROVIDER_ID
        )
        set_gateway_url(_origin_from_provider(config))
    except Exception:  # noqa: BLE001 -- never abort startup
        logger.warning("qai_service.hydrate_failed", exc_info=True)
        set_gateway_url("")


def build_router(*, container: "Container") -> APIRouter:
    router = APIRouter(prefix="/api/qai-service", tags=["qai-service"])
    registry = container.model_catalog.provider_registry

    async def _snapshot() -> dict[str, Any]:
        config = await registry.get_provider_config(QAI_SERVICE_PROVIDER_ID)
        models = config.get("models") if isinstance(config, dict) else None
        return {
            "configured": config is not None and bool(_origin_from_provider(config)),
            "base_url": get_gateway_url() or "",
            "has_token": get_service_jwt() is not None,
            "model_count": len(models) if isinstance(models, list) else 0,
        }

    @router.get("")
    async def status() -> dict[str, Any]:
        return await _snapshot()

    @router.post("/connect")
    async def connect(body: ConnectRequest) -> dict[str, Any]:
        origin = normalize_gateway_url(body.url)
        if not origin:
            raise HTTPException(
                status_code=422,
                detail="invalid_url: expected http(s)://host[:port]",
            )
        previous = get_gateway_url() or ""
        existing = await registry.get_provider_config(QAI_SERVICE_PROVIDER_ID)
        keep_models = (
            previous == origin
            and isinstance(existing, dict)
            and isinstance(existing.get("models"), list)
        )
        config: dict[str, Any] = dict(existing) if isinstance(existing, dict) else {}
        config["base_url"] = f"{origin}/v1"
        config["models"] = list(existing["models"]) if keep_models else []
        await registry.save_provider_config(QAI_SERVICE_PROVIDER_ID, config)
        if previous != origin:
            # A JWT minted by another broker is useless (and misleading) here.
            clear_service_jwt()
        set_gateway_url(origin)
        logger.info("qai_service.connect", base_url=origin)
        snap = await _snapshot()
        return {**snap, "needs_login": not snap["has_token"]}

    @router.post("/sync")
    async def sync() -> dict[str, Any]:
        result = await container.model_catalog.sync_qai_service_models_use_case.execute()
        snap = await _snapshot()
        return {
            **snap,
            "ok": result.ok,
            "added": list(result.added),
            "removed": list(result.removed),
            "skipped_reason": result.skipped_reason,
        }

    @router.delete("")
    async def disconnect() -> dict[str, Any]:
        await registry.delete_provider_config(QAI_SERVICE_PROVIDER_ID)
        clear_service_jwt()
        set_gateway_url("")
        logger.info("qai_service.disconnect")
        return await _snapshot()

    return router
