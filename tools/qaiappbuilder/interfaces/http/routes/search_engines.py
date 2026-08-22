# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Independent web-search engine management routes — ``/api/search/engines``.

Backs the Agent settings panel's "search source" group (plan §11.3): the
per-engine list is NOT hardcoded in the frontend but pulled from here, joining
the ``[[search_engines]]`` specs (``search_config.toml`` in the shared-kernel
``qai.platform.web_search`` package) with the ``search_engine_score``
health-scoring table.

Three primary endpoints (plan §11.3):

* ``GET /api/search/engines`` — the engine list with merged score / manual
  state / success rate and a derived ``status``.
* ``POST /api/search/engines/{engine_id}/manual_state`` — set the manual
  override (``auto`` / ``forced_on`` / ``forced_off``).
* ``DELETE /api/search/engines/{engine_id}/score`` — reset the health score.

Plus per-engine credential management for keyed engines
(``requires_credential=true``):

* ``PUT /api/search/engines/{engine_id}/credential`` — store the engine's API
  key in the SecretStore (never echoed back).
* ``DELETE /api/search/engines/{engine_id}/credential`` — remove it
  (idempotent).

Plus one optional probe endpoint (plan §15.8) that reports score state only,
no live network probing.

Edition parity: the multi-engine ``web`` provider and its engine roster live in
the shared-kernel package ``qai.platform.web_search`` (shipped to BOTH
editions), so these engines run and accrue health scores internally AND
externally. This management surface therefore mirrors that reach — it is
available under both editions, not internal-only. Availability degrades only
when the roster / ``ScoreStore`` cannot be imported or no ``database`` is wired
(``_load_edition`` returns ``None``): the list / status endpoints then return an
empty array and the mutating endpoints 404. (The intranet ``cebot`` backend
remains internal-only, but it is a separate provider with no per-engine roster
row and is not surfaced here.)
"""

from __future__ import annotations

from contextlib import suppress
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from qai.platform.errors import NotFoundError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from apps.api.di import Container

__all__ = ["build_router"]

# SecretStore namespace shared with the CEBot bridge / model-catalog provider
# path; keyed engines name a ``credential_key`` (e.g. ``web_brave``) stored here.
_PROVIDER_SECRET_SERVICE = "qai.model_catalog.provider"  # noqa: S105 — namespace id, not a secret

# Derived per-engine status values (plan §11.3, aligned with FrontendSettings).
_STATUS_ENABLED = "enabled"
_STATUS_DISABLED = "disabled"
_STATUS_NEEDS_CONFIG = "needs_config"


# ---------------------------------------------------------------------------
# Request / Response DTOs
# ---------------------------------------------------------------------------


class SearchEngineDTO(BaseModel):
    """One engine row: static spec fields joined with live scoring state."""

    engine_id: str
    display_name: str
    engine_type: str
    requires_credential: bool
    credential_key: str
    enabled_by_default: bool
    description_i18n_key: str
    priority_hint: int
    score: int
    manual_state: str
    success_rate: float
    status: str
    # Optional per-engine quota snapshot (populated when QuotaStore is available).
    # None for credential-free/keyless engines that do not track usage.
    quota_usage: int | None = None
    quota_limit: int | None = None
    quota_remaining: int | None = None
    quota_exhausted: bool | None = None
    quota_notify_enabled: bool | None = None

class SearchEnginesResponse(BaseModel):
    """Envelope for ``GET /api/search/engines``."""

    engines: list[SearchEngineDTO]


class ManualStateRequest(BaseModel):
    """Body for ``POST /api/search/engines/{engine_id}/manual_state``."""

    state: str


class ManualStateResponse(BaseModel):
    """Post-update snapshot of the engine's manual override + score."""

    engine_id: str
    manual_state: str
    score: int


class CredentialRequest(BaseModel):
    """Body for ``PUT /api/search/engines/{engine_id}/credential``."""

    api_key: str


class CredentialResponse(BaseModel):
    """Post-mutation snapshot after a credential set / delete."""

    ok: bool
    engine_id: str
    status: str


class EngineStatusDTO(BaseModel):
    """One engine's score state for the optional probe endpoint (§15.8)."""

    engine_id: str
    score: int
    manual_state: str
    enabled: bool


class EnginesStatusResponse(BaseModel):
    """Envelope for ``GET /api/search/engines/status`` (score state only)."""

    engines: list[EngineStatusDTO]


class QuotaDTO(BaseModel):
    """One engine's current-month quota state."""

    engine_id: str
    month_key: str
    usage_count: int
    monthly_limit: int
    notify_enabled: bool
    remaining: int
    exhausted: bool


class QuotasResponse(BaseModel):
    """Envelope for ``GET /api/search/engines/quota``."""

    quotas: list[QuotaDTO]


class QuotaUpdateRequest(BaseModel):
    """Body for ``PUT /api/search/engines/{engine_id}/quota``."""

    monthly_limit: int


class QuotaNotifyRequest(BaseModel):
    """Body for ``PUT /api/search/engines/{engine_id}/quota/notify``."""

    enabled: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _secret_exists(store: Any | None, key: str) -> bool:
    """Whether a credential is provisioned; any failure ⇒ treat as absent."""
    if store is None or not key:
        return False
    try:
        return bool(store.exists(_PROVIDER_SECRET_SERVICE, key))
    except Exception:  # noqa: BLE001 — any failure ⇒ no usable credential
        return False


def _load_edition() -> tuple[Any, Any] | None:
    """Return ``(get_search_engines, ScoreStore)``, or ``None`` on failure.

    The engine roster + ``ScoreStore`` live in the shared-kernel package
    ``qai.platform.web_search`` (both editions); this import does not depend on
    the external-excluded ``qai.platform.edition``, so the management surface is
    available under both editions. ``None`` only when the shared kernel cannot
    be imported (should never happen on a well-formed build) — a defensive
    degrade-to-unavailable path.
    """
    try:
        from qai.platform.web_search.config import get_search_engines
        from qai.platform.web_search.independent.scoring import ScoreStore
    except Exception:  # noqa: BLE001 — defensive; degrade to "not available"
        return None
    return get_search_engines, ScoreStore


def _build_score_store(score_store_cls: Any, get_engines: Any, database: Any) -> Any:
    """Build a ``ScoreStore`` seeded with the roster's default-off engines.

    Every construction site must pass the same ``default_off_engines`` set, or
    this management surface would report an engine as enabled while the search
    chain skips it (the flag decides the seed ``manual_state`` for engines with
    no stored row yet).
    """
    default_off = frozenset(
        str(spec.get("engine_id"))
        for spec in get_engines()
        if isinstance(spec, dict) and not spec.get("enabled_by_default", True)
    )
    return score_store_cls(database, default_off_engines=default_off)


def _spec_str(spec: dict[str, object], key: str) -> str:
    value = spec.get(key)
    return value if isinstance(value, str) else ""


def _spec_bool(spec: dict[str, object], key: str) -> bool:
    return bool(spec.get(key, False))


def _spec_int(spec: dict[str, object], key: str) -> int:
    value = spec.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def _derive_status(
    engine_id: str,
    *,
    requires_credential: bool,
    credential_key: str,
    store: Any,
    secret_store: Any | None,
) -> str:
    """Derive an engine's ``status`` (needs_config / enabled / disabled)."""
    if requires_credential and not _secret_exists(secret_store, credential_key):
        return _STATUS_NEEDS_CONFIG
    if await store.is_enabled(engine_id):
        return _STATUS_ENABLED
    return _STATUS_DISABLED


async def _build_engine_row(
    spec: dict[str, object],
    store: Any,
    secret_store: Any | None,
    quota_store: Any | None = None,
) -> SearchEngineDTO | None:
    """Join one ``[[search_engines]]`` spec with its scoring state (§11.3)."""
    engine_id = _spec_str(spec, "engine_id")
    if not engine_id:
        return None
    requires_credential = _spec_bool(spec, "requires_credential")
    credential_key = _spec_str(spec, "credential_key")
    state = await store.get_state(engine_id)
    engine_status = await _derive_status(
        engine_id,
        requires_credential=requires_credential,
        credential_key=credential_key,
        store=store,
        secret_store=secret_store,
    )

    # Quota snapshot (keyed engines only — keyless engines don't track usage).
    quota_usage: int | None = None
    quota_limit: int | None = None
    quota_remaining: int | None = None
    quota_exhausted: bool | None = None
    quota_notify_enabled: bool | None = None
    if requires_credential and quota_store is not None:
        try:
            qi = await quota_store.get_quota(engine_id)
            quota_usage = qi.usage_count
            quota_limit = qi.monthly_limit
            quota_remaining = qi.remaining
            quota_exhausted = qi.exhausted
            quota_notify_enabled = qi.notify_enabled
        except Exception:  # noqa: BLE001 — quota is informational
            pass

    return SearchEngineDTO(
        engine_id=engine_id,
        display_name=_spec_str(spec, "display_name"),
        engine_type=_spec_str(spec, "engine_type"),
        requires_credential=requires_credential,
        credential_key=credential_key,
        enabled_by_default=_spec_bool(spec, "enabled_by_default"),
        description_i18n_key=_spec_str(spec, "description_i18n_key"),
        priority_hint=_spec_int(spec, "priority_hint"),
        score=state.score,
        manual_state=state.manual_state,
        success_rate=state.success_rate,
        status=engine_status,
        quota_usage=quota_usage,
        quota_limit=quota_limit,
        quota_remaining=quota_remaining,
        quota_exhausted=quota_exhausted,
        quota_notify_enabled=quota_notify_enabled,
    )


async def _build_status_row(spec: dict[str, object], store: Any) -> EngineStatusDTO | None:
    """Score-state-only row for the optional probe endpoint (§15.8)."""
    engine_id = _spec_str(spec, "engine_id")
    if not engine_id:
        return None
    state = await store.get_state(engine_id)
    return EngineStatusDTO(
        engine_id=engine_id,
        score=state.score,
        manual_state=state.manual_state,
        enabled=await store.is_enabled(engine_id),
    )


def _find_spec(get_search_engines: Any, engine_id: str) -> dict[str, object] | None:
    """Return the roster spec for ``engine_id`` (by ``engine_id`` field)."""
    for spec in get_search_engines():
        if isinstance(spec, dict) and _spec_str(spec, "engine_id") == engine_id:
            return spec
    return None


def _credential_context(container: Container, engine_id: str) -> tuple[dict[str, object], Any, Any]:
    """Resolve ``(spec, store, secret_store)`` for a credential mutation.

    Enforces the shared preconditions of the credential endpoints: the
    shared-kernel roster / ``database`` must be available (else 404),
    ``engine_id`` must name a roster spec (404), the spec must declare
    ``requires_credential`` with a non-empty ``credential_key`` (400), and a
    ``SecretStore`` must be wired (503).
    """
    loaded = _load_edition()
    database = getattr(container, "database", None)
    if loaded is None or database is None:
        raise HTTPException(status_code=404, detail="search engines not available")
    get_search_engines, score_store_cls = loaded
    spec = _find_spec(get_search_engines, engine_id)
    if spec is None:
        raise HTTPException(status_code=404, detail="unknown engine")
    if not _spec_bool(spec, "requires_credential") or not _spec_str(spec, "credential_key"):
        raise HTTPException(status_code=400, detail="engine does not accept a credential")
    secret_store = getattr(container, "secret_store", None)
    if secret_store is None:
        raise HTTPException(status_code=503, detail="secret store unavailable")
    return spec, _build_score_store(score_store_cls, get_search_engines, database), secret_store


async def _collect_engine_rows(container: Container) -> list[SearchEngineDTO]:
    """Build the merged engine rows, or ``[]`` when the roster is unavailable."""
    loaded = _load_edition()
    database = getattr(container, "database", None)
    if loaded is None or database is None:
        return []
    get_search_engines, score_store_cls = loaded
    store = _build_score_store(score_store_cls, get_search_engines, database)
    secret_store = getattr(container, "secret_store", None)
    # Build a QuotaStore for inline quota snapshots (degrades to None).
    quota_store: Any | None = None
    try:
        from qai.platform.web_search.independent.quota import QuotaStore as QS

        quota_store = QS(database)
    except Exception:  # noqa: BLE001 — degrade gracefully
        pass
    rows: list[SearchEngineDTO] = []
    for spec in get_search_engines():
        if not isinstance(spec, dict):
            continue
        row = await _build_engine_row(spec, store, secret_store, quota_store)
        if row is not None:
            rows.append(row)
    return rows


async def _collect_status_rows(container: Container) -> list[EngineStatusDTO]:
    """Build the score-only status rows, or ``[]`` when unavailable (§15.8)."""
    loaded = _load_edition()
    database = getattr(container, "database", None)
    if loaded is None or database is None:
        return []
    get_search_engines, score_store_cls = loaded
    store = _build_score_store(score_store_cls, get_search_engines, database)
    rows: list[EngineStatusDTO] = []
    for spec in get_search_engines():
        if not isinstance(spec, dict):
            continue
        row = await _build_status_row(spec, store)
        if row is not None:
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_router(*, container: Container) -> APIRouter:
    """Build the search-engine management router (both editions).

    The ``web`` engine roster ships to both editions, so this surface is not
    internal-only; it degrades to empty / 404 only when the shared-kernel roster
    or the ``database`` is unavailable.
    """
    router = APIRouter(prefix="/api/search/engines", tags=["search-engines"])

    def _mutating_store() -> Any:
        """Build a ``ScoreStore`` for a mutating call, or 404 when unavailable."""
        loaded = _load_edition()
        database = getattr(container, "database", None)
        if loaded is None or database is None:
            raise HTTPException(status_code=404, detail="search engines not available")
        get_engines, score_store_cls = loaded
        return _build_score_store(score_store_cls, get_engines, database)

    @router.get("", response_model=SearchEnginesResponse)
    async def list_engines() -> SearchEnginesResponse:
        """List engines with merged health-scoring state (plan §11.3).

        Available on both editions (the ``web`` roster ships to both); returns
        an empty array only when the shared-kernel roster / ``ScoreStore`` or the
        ``database`` is unavailable, so the frontend renders no rows.
        """
        return SearchEnginesResponse(engines=await _collect_engine_rows(container))

    @router.post("/{engine_id}/manual_state", response_model=ManualStateResponse)
    async def set_manual_state(engine_id: str, body: ManualStateRequest) -> ManualStateResponse:
        """Set the manual override (``auto`` / ``forced_on`` / ``forced_off``)."""
        store = _mutating_store()
        try:
            updated = await store.set_manual_state(engine_id, body.state)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return ManualStateResponse(
            engine_id=updated.engine_id,
            manual_state=updated.manual_state,
            score=updated.score,
        )

    @router.delete("/{engine_id}/score", response_model=ManualStateResponse)
    async def reset_score(engine_id: str) -> ManualStateResponse:
        """Reset the engine's health score / counters (plan §12.11)."""
        store = _mutating_store()
        updated = await store.reset_score(engine_id)
        return ManualStateResponse(
            engine_id=updated.engine_id,
            manual_state=updated.manual_state,
            score=updated.score,
        )

    @router.get("/status", response_model=EnginesStatusResponse)
    async def engines_status() -> EnginesStatusResponse:
        """Optional probe endpoint (plan §15.8): score state only, no network.

        Reports each configured engine's current score / manual state / enabled
        flag straight from ``search_engine_score``; deliberately does no live
        connectivity probing (first-phase scope per §15.8).
        """
        return EnginesStatusResponse(engines=await _collect_status_rows(container))

    @router.put("/{engine_id}/credential", response_model=CredentialResponse)
    async def set_credential(engine_id: str, body: CredentialRequest) -> CredentialResponse:
        """Store the API key for a keyed engine (``requires_credential=true``).

        Rejects a blank key (400) and engines that take no credential (400).
        On success the engine's derived ``status`` is recomputed (typically
        flipping ``needs_config`` → ``enabled``).
        """
        api_key = body.api_key.strip()
        if not api_key:
            raise HTTPException(status_code=400, detail="api_key must not be empty")
        spec, store, secret_store = _credential_context(container, engine_id)
        credential_key = _spec_str(spec, "credential_key")
        try:
            secret_store.set(_PROVIDER_SECRET_SERVICE, credential_key, api_key)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        status = await _derive_status(
            engine_id,
            requires_credential=True,
            credential_key=credential_key,
            store=store,
            secret_store=secret_store,
        )
        return CredentialResponse(ok=True, engine_id=engine_id, status=status)

    @router.delete("/{engine_id}/credential", response_model=CredentialResponse)
    async def delete_credential(engine_id: str) -> CredentialResponse:
        """Remove a keyed engine's stored API key (idempotent).

        Deleting an absent credential is treated as success; the engine reverts
        to ``needs_config``.
        """
        spec, _store, secret_store = _credential_context(container, engine_id)
        credential_key = _spec_str(spec, "credential_key")
        with suppress(NotFoundError):
            secret_store.delete(_PROVIDER_SECRET_SERVICE, credential_key)
        return CredentialResponse(ok=True, engine_id=engine_id, status=_STATUS_NEEDS_CONFIG)

    # --- Quota management endpoints ---

    def _quota_store() -> Any:
        """Build a ``QuotaStore`` for quota calls, or 404 when unavailable."""
        from qai.platform.web_search.independent.quota import QuotaStore as QS

        database = getattr(container, "database", None)
        if database is None:
            raise HTTPException(status_code=404, detail="database not available")
        return QS(database)

    @router.get("/quota", response_model=QuotasResponse)
    async def list_quotas() -> QuotasResponse:
        """List all engines' current-month quota states."""
        store = _quota_store()
        infos = await store.get_all_quotas()
        return QuotasResponse(
            quotas=[
                QuotaDTO(
                    engine_id=i.engine_id,
                    month_key=i.month_key,
                    usage_count=i.usage_count,
                    monthly_limit=i.monthly_limit,
                    notify_enabled=i.notify_enabled,
                    remaining=i.remaining,
                    exhausted=i.exhausted,
                )
                for i in infos
            ]
        )

    @router.get("/{engine_id}/quota", response_model=QuotaDTO)
    async def get_engine_quota(engine_id: str) -> QuotaDTO:
        """Get one engine's current-month quota state."""
        store = _quota_store()
        info = await store.get_quota(engine_id)
        return QuotaDTO(
            engine_id=info.engine_id,
            month_key=info.month_key,
            usage_count=info.usage_count,
            monthly_limit=info.monthly_limit,
            notify_enabled=info.notify_enabled,
            remaining=info.remaining,
            exhausted=info.exhausted,
        )

    @router.put("/{engine_id}/quota", response_model=QuotaDTO)
    async def set_engine_quota(engine_id: str, body: QuotaUpdateRequest) -> QuotaDTO:
        """Set the monthly usage limit for an engine.

        Takes effect immediately for the current month. The default is 1000
        (matching typical free-tier API allowances).
        """
        if body.monthly_limit < 1:
            raise HTTPException(status_code=422, detail="monthly_limit must be >= 1")
        store = _quota_store()
        info = await store.set_monthly_limit(engine_id, body.monthly_limit)
        return QuotaDTO(
            engine_id=info.engine_id,
            month_key=info.month_key,
            usage_count=info.usage_count,
            monthly_limit=info.monthly_limit,
            notify_enabled=info.notify_enabled,
            remaining=info.remaining,
            exhausted=info.exhausted,
        )

    @router.put("/{engine_id}/quota/notify", response_model=QuotaDTO)
    async def set_quota_notify(engine_id: str, body: QuotaNotifyRequest) -> QuotaDTO:
        """Enable or disable usage-threshold notifications for an engine.

        When disabled, the engine still tracks usage and enforces the limit,
        but no frontend toast/popup is emitted at thresholds.
        """
        store = _quota_store()
        info = await store.set_notify_enabled(engine_id, body.enabled)
        return QuotaDTO(
            engine_id=info.engine_id,
            month_key=info.month_key,
            usage_count=info.usage_count,
            monthly_limit=info.monthly_limit,
            notify_enabled=info.notify_enabled,
            remaining=info.remaining,
            exhausted=info.exhausted,
        )

    return router
