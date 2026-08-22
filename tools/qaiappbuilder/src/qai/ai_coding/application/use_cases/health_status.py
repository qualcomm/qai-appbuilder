# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use case: report ai_coding provider health, models and providers list.

Backs the legacy ``GET /api/cc/health`` and ``GET /api/oc/health``
routes plus the (now-folded) ``GET /api/oc/providers`` /
``GET /api/oc/models`` legacy endpoints.

Per PR-105's L1 lane brief ("models / providers — 折叠到 health"),
the providers/models lists are surfaced as part of the health
response instead of dedicated routes; this keeps the API surface
small and lets a WebUI client populate its provider picker with a
single round-trip.

The use case stays adapter-agnostic: it inspects the
:class:`CodingProviderPort` for available providers and, when the
adapter exposes an optional ``available_models`` capability via
duck-typing, surfaces that too.  Adapters that do not advertise
models simply omit the field (empty list).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from qai.ai_coding.application.ports import (
    CodingProviderPort,
    CodingSessionRepositoryPort,
)
from qai.ai_coding.domain import Provider, SessionStatus

__all__ = [
    "HealthStatusQuery",
    "HealthStatusResult",
    "HealthStatusUseCase",
    "ModelInfo",
    "ProviderInfo",
]


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ProviderInfo:
    """Per-provider metadata surfaced via the folded health response.

    ``id`` matches the :class:`Provider` enum value (``claude_code`` /
    ``open_code``).  ``name`` is a human-readable label used by the
    WebUI.  ``available`` reflects the adapter's runtime capability —
    ``False`` when the adapter has been booted but the underlying
    backend is unreachable (e.g. OpenCode binary not installed).
    """

    id: str
    name: str
    available: bool


@dataclass(frozen=True, slots=True, kw_only=True)
class ModelInfo:
    """Per-model metadata surfaced via the folded health response.

    Models are flat-listed (no nesting under provider) because the
    legacy WebUI's model picker treats the ``provider:model`` pair as
    the addressable unit.  ``provider_id`` is the parent provider's
    enum value.
    """

    id: str
    name: str
    provider_id: str


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthStatusQuery:
    """Input for :class:`HealthStatusUseCase`.

    The single ``provider`` argument gates which provider the response
    presents as the primary (i.e. matches the URL prefix
    ``/api/cc`` vs ``/api/oc``).  The ``providers`` + ``models`` lists
    are unfiltered — the same use case serves both endpoints.

    ``refresh`` (C1 model-source badge) forces a bypass of the model
    catalog's 5-minute cache so the WebUI's 🔄 button re-enumerates the
    upstream ``/v1/models`` (V1 ``?refresh=1`` parity).  Defaults to
    ``False`` so the routine health poll keeps using the cache.
    """

    provider: Provider
    refresh: bool = False


@dataclass(frozen=True, slots=True, kw_only=True)
class HealthStatusResult:
    """Return shape of :class:`HealthStatusUseCase`.

    Mirrors the legacy ``GET /api/{cc|oc}/health`` wire shape with
    additive ``providers`` / ``models`` lists folded in (formerly
    served by ``GET /api/oc/providers`` and similar).
    """

    provider: str
    available: bool
    available_providers: tuple[str, ...]
    providers: tuple[ProviderInfo, ...]
    models: tuple[ModelInfo, ...]
    # U-5: legacy V1 footer parity (additive per v2.7 §3.1 field-name
    # lock — appended with defaults so existing test constructions /
    # older clients keep working unchanged).
    sdk_available: bool = False
    sdk_version: str = ""
    auth_configured: bool = False
    auth_source: str = "none"
    active_sessions: int = 0
    total_sessions: int = 0
    # C1: V1 ``/api/cc/models`` model-source badge parity (additive per
    # v2.7 §3.1 field-name lock — appended with defaults so existing
    # test constructions / older clients keep working).  ``models_source``
    # is the V1 4-state string (upstream / cache / fallback-no-key /
    # fallback-error); ``models_base_url`` is the credential-stripped
    # ``scheme://netloc`` the catalog was (or would be) fetched from;
    # ``models_base_url_source`` is config / env / default;
    # ``models_error`` carries the fallback reason (or empty);
    # ``models_cached_age`` is the cache age in seconds when source ==
    # cache (else ``None``).  Adapters that don't advertise the catalog
    # probe leave the defaults (no badge data → WebUI hides the badge).
    models_source: str = ""
    models_base_url: str = ""
    models_base_url_source: str = ""
    models_error: str = ""
    models_cached_age: float | None = None


# ---------------------------------------------------------------------------
# Use case
# ---------------------------------------------------------------------------


_PROVIDER_DISPLAY_NAMES: dict[str, str] = {
    Provider.CLAUDE_CODE.value: "Claude Code",
    Provider.OPEN_CODE.value: "OpenCode",
}


class HealthStatusUseCase:
    """Application service for ``GET /api/{cc|oc}/health`` (folded)."""

    def __init__(
        self,
        *,
        coding_provider: CodingProviderPort,
        repository: CodingSessionRepositoryPort | None = None,
        credentials_use_cases: Mapping[str, object] | None = None,
        sdk_probe: Callable[[], tuple[bool, str]] | None = None,
        cc_non_secret_env_vars: tuple[str, ...] = (),
    ) -> None:
        self._coding_provider = coding_provider
        # U-5: optional collaborators.  Defaults preserve the historical
        # single-arg construction used by hand-rolled test fixtures.
        self._repository = repository
        # Maps a provider enum value (``claude_code`` / ``open_code``)
        # to its :class:`GetCodingCredentialsUseCase` instance.  The
        # CC and OC routes use distinct SecretStore namespaces +
        # variable whitelists, so the lookup is keyed by provider.
        self._credentials_use_cases = dict(credentials_use_cases or {})
        # DI-injected SDK probe.  Returns ``(available, version)``.  The
        # application layer never imports ``claude_agent_sdk`` directly
        # (domain-purity / import-linter); the probe closure lives in
        # the apps/infrastructure layer.
        self._sdk_probe = sdk_probe
        # P1-6: V1 ``_non_secret_vars`` parity (``api_routes.py:1569-1575``).
        # CC-only: when any of these env vars carry a value the auth
        # status flips to ``(True, "env")`` even when no SecretStore-
        # backed credential is configured.  Injected from the apps/api
        # layer so the application layer never hardcodes the V1 list
        # (more testable + easier to extend than the V1 inline literal).
        # Defaults to empty so existing hand-rolled test constructions
        # keep working unchanged.
        self._cc_non_secret_env_vars = tuple(cc_non_secret_env_vars)

    async def execute(self, query: HealthStatusQuery) -> HealthStatusResult:
        advertised = list(self._coding_provider.available_providers())
        provider_set = {p.value for p in advertised}

        providers: list[ProviderInfo] = []
        # Always include both providers in the folded list so the
        # WebUI knows the universe of options; ``available`` is
        # adapter-specific (per ``available_providers()`` advertisement).
        # P1-5: when the adapter exposes an ``is_available(provider=...)``
        # liveness probe (duck-typed), use the live result so OC's
        # ``available`` flag flips to ``False`` when the local OpenCode
        # service is offline (V1 ``opencode_session_manager.is_available``
        # parity).  Adapters that don't implement the probe (e.g. CC,
        # whose readiness is reflected through the SDK probe) keep the
        # static advertisement.
        live_probe = getattr(self._coding_provider, "is_available", None)
        for p in (Provider.CLAUDE_CODE, Provider.OPEN_CODE):
            advertised_here = p.value in provider_set
            available = advertised_here
            if advertised_here and callable(live_probe):
                try:
                    probe_result = live_probe(provider=p)
                    if hasattr(probe_result, "__await__"):
                        probe_result = await probe_result  # type: ignore[misc]
                    available = bool(probe_result)
                except TypeError:
                    # Adapter exposes ``is_available`` with a different
                    # signature (no ``provider`` kw) — fall through to
                    # the static advertisement rather than mis-reporting.
                    available = advertised_here
                except Exception:  # noqa: BLE001 — best-effort.
                    available = False
            providers.append(
                ProviderInfo(
                    id=p.value,
                    name=_PROVIDER_DISPLAY_NAMES.get(p.value, p.value),
                    available=available,
                )
            )

        # Optional: introspect adapter for ``available_models`` (duck
        # typed; not part of CodingProviderPort to keep the Protocol
        # minimal).  Adapters that do not expose it fall through to
        # the empty tuple.  ``force_refresh`` (C1) bypasses the model
        # catalog's 5-minute cache when the WebUI requests ?refresh=1.
        models: list[ModelInfo] = []
        list_models = getattr(self._coding_provider, "available_models", None)
        if callable(list_models):
            try:
                raw = self._call_models(list_models, query.refresh)
                if hasattr(raw, "__await__"):
                    raw = await raw  # type: ignore[misc]
                if isinstance(raw, list):
                    for entry in raw:
                        if isinstance(entry, dict):
                            mid = str(entry.get("id", ""))
                            mname = str(entry.get("name", mid))
                            mprovider = str(entry.get("provider_id", ""))
                            if mid:
                                models.append(
                                    ModelInfo(
                                        id=mid,
                                        name=mname,
                                        provider_id=mprovider,
                                    )
                                )
            except Exception:  # noqa: BLE001
                # Models list is best-effort; failure must not break health.
                models = []

        # C1: model-source badge metadata (V1 ``/api/cc/models``
        # parity).  Duck-typed like ``available_models`` so non-CC
        # adapters simply leave the defaults.  ``available_models``
        # above already refreshed the adapter's snapshot, so this read
        # is a cheap accessor (no second upstream round-trip).
        (
            models_source,
            models_base_url,
            models_base_url_source,
            models_error,
            models_cached_age,
        ) = await self._model_source_meta(query.refresh)

        # U-5: session counts (per the queried provider).
        active_sessions, total_sessions = await self._session_counts(query.provider)

        # U-5: auth configuration (per the queried provider).
        auth_configured, auth_source = await self._auth_status(query.provider)

        # U-5: SDK availability (CC only; OC has no embedded SDK).
        sdk_available, sdk_version = self._sdk_status(query.provider)

        return HealthStatusResult(
            provider=query.provider.value,
            available=query.provider in advertised,
            available_providers=tuple(p.value for p in advertised),
            providers=tuple(providers),
            models=tuple(models),
            sdk_available=sdk_available,
            sdk_version=sdk_version,
            auth_configured=auth_configured,
            auth_source=auth_source,
            active_sessions=active_sessions,
            total_sessions=total_sessions,
            models_source=models_source,
            models_base_url=models_base_url,
            models_base_url_source=models_base_url_source,
            models_error=models_error,
            models_cached_age=models_cached_age,
        )

    # ------------------------------------------------------------------
    # C1 helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _call_models(list_models: Callable[..., object], refresh: bool) -> object:
        """Invoke ``available_models`` with ``force_refresh`` when supported.

        Adapters predating C1 (e.g. the OpenCode adapter / hand-rolled
        test stubs) take no arguments; the newer CC adapter accepts a
        ``force_refresh`` keyword.  Try the richer signature first and
        gracefully fall back so neither shape breaks.
        """
        try:
            return list_models(force_refresh=refresh)
        except TypeError:
            return list_models()

    async def _model_source_meta(
        self, refresh: bool
    ) -> tuple[str, str, str, str, float | None]:
        """Read the adapter's V1-parity model-source metadata (best-effort).

        Returns ``(source, base_url, base_url_source, error, cached_age)``
        with empty/None defaults when the adapter doesn't advertise the
        ``model_source_meta`` probe.
        """
        probe = getattr(self._coding_provider, "model_source_meta", None)
        if not callable(probe):
            return "", "", "", "", None
        try:
            raw = probe(force_refresh=refresh)
            if hasattr(raw, "__await__"):
                raw = await raw  # type: ignore[misc]
        except TypeError:
            try:
                raw = probe()
                if hasattr(raw, "__await__"):
                    raw = await raw  # type: ignore[misc]
            except Exception:  # noqa: BLE001
                return "", "", "", "", None
        except Exception:  # noqa: BLE001 — badge metadata is best-effort.
            return "", "", "", "", None
        if not isinstance(raw, dict):
            return "", "", "", "", None
        source = str(raw.get("source") or "")
        base_url = str(raw.get("base_url") or "")
        base_url_source = str(raw.get("base_url_source") or "")
        error = str(raw.get("error") or "")
        cached_age_raw = raw.get("cached_age")
        cached_age = (
            float(cached_age_raw)
            if isinstance(cached_age_raw, (int, float))
            else None
        )
        return source, base_url, base_url_source, error, cached_age

    # ------------------------------------------------------------------
    # U-5 helpers
    # ------------------------------------------------------------------
    async def _session_counts(self, provider: Provider) -> tuple[int, int]:
        """Return ``(active, total)`` session counts for *provider*.

        ``total`` counts every session for the provider (incl.
        terminated); ``active`` counts those whose status is not
        ``TERMINATED``.  Returns ``(0, 0)`` when no repository was
        injected (hand-rolled test fixtures).
        """
        if self._repository is None:
            return 0, 0
        try:
            sessions = await self._repository.list_all()
        except Exception:  # noqa: BLE001 — counts are best-effort.
            return 0, 0
        total = 0
        active = 0
        for s in sessions:
            if getattr(s, "provider", None) is not provider:
                continue
            total += 1
            if getattr(s, "status", None) is not SessionStatus.TERMINATED:
                active += 1
        return active, total

    async def _auth_status(self, provider: Provider) -> tuple[bool, str]:
        """Return ``(auth_configured, auth_source)`` for *provider*.

        ``auth_source`` is ``"env"`` when any credential is set via the
        process environment, ``"store"`` when only the SecretStore has
        it, else ``"none"``.  Returns ``(False, "none")`` when no
        credentials use case was injected for the provider.

        P1-6: CC-only — when any of the injected
        ``cc_non_secret_env_vars`` (V1 ``_non_secret_vars``:
        ``GOOGLE_APPLICATION_CREDENTIALS`` / ``GOOGLE_CLOUD_PROJECT`` /
        ``CLAUDE_CODE_USE_FOUNDRY`` / ``ANTHROPIC_FOUNDRY_RESOURCE`` /
        ``ANTHROPIC_FOUNDRY_BASE_URL``) carry a value, auth is reported
        as configured via env even when no SecretStore-backed credential
        is set.  Mirrors V1 ``api_routes.py:1577,1604-1608``: ``env`` >
        ``store`` > ``none``.
        """
        # P1-6: env-only fast path for CC's non-secret env-driven auth
        # modes (Vertex AI / Foundry).  Checked FIRST so it takes
        # precedence over the SecretStore probe (V1 line 1606:
        # ``"env" if auth_from_env else …``).
        env_only_auth = False
        if (
            provider is Provider.CLAUDE_CODE
            and self._cc_non_secret_env_vars
            and any(os.environ.get(v) for v in self._cc_non_secret_env_vars)
        ):
            env_only_auth = True

        uc = self._credentials_use_cases.get(provider.value)
        if uc is None:
            if env_only_auth:
                return True, "env"
            return False, "none"
        try:
            result = await uc.execute()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — auth probe is best-effort.
            if env_only_auth:
                return True, "env"
            return False, "none"
        statuses = list(getattr(result, "statuses", ()) or ())
        configured = any(getattr(s, "configured", False) for s in statuses)
        if not configured:
            if env_only_auth:
                return True, "env"
            return False, "none"
        # env takes precedence over store when both present (V1 line
        # 1606-1608).  ``cc_non_secret_env_vars`` set OR any
        # SecretStore-tracked secret found in os.environ both qualify
        # as "env".
        if env_only_auth or any(getattr(s, "in_env", False) for s in statuses):
            return True, "env"
        return True, "store"

    def _sdk_status(self, provider: Provider) -> tuple[bool, str]:
        """Return ``(sdk_available, sdk_version)`` for *provider*.

        Only Claude Code carries an embedded SDK; OpenCode talks to a
        local HTTP service and has no in-process SDK, so it always
        reports ``(False, "")``.  The probe itself is DI-injected so
        the application layer never imports ``claude_agent_sdk``.
        """
        if provider is not Provider.CLAUDE_CODE or self._sdk_probe is None:
            return False, ""
        try:
            available, version = self._sdk_probe()
        except Exception:  # noqa: BLE001 — probe is best-effort.
            return False, ""
        return bool(available), str(version or "")
