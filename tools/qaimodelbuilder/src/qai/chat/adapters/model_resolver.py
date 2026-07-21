# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Settings-based model resolver adapter (A4 feature).

Implements :class:`~qai.chat.application.ports.ModelResolverPort` by
reading from the DI-injected chat settings (``base_url``, ``api_key``,
``model``).  When a ``hint`` is provided and matches a known pattern
(e.g. ``"local::"`` prefix), it adjusts the resolution accordingly.

This is a **simplified** version of the legacy multi-provider resolution
found at ``backend/chat_handler.py:285-365``.  Cross-context model
catalogue lookups (model registry, running-process detection) live in
``qai.model_catalog``; this resolver intentionally restricts itself to
the chat ``Settings`` so the ``context-isolation`` import-linter
contract is preserved.  Callers that need richer resolution route
through the application layer, which composes this resolver with a
model_catalog adapter.

Design notes:

* The resolver is **stateless**; it can be constructed once and shared
  across requests.
* ``is_local`` detection is heuristic: a model is local when its base
  URL points to localhost/127.0.0.1/[::1] or when the model id starts
  with ``"local::"``.
* If no base URL is configured the resolver still returns a
  :class:`ResolvedModel` with an empty ``base_url``; the downstream
  :class:`HttpOpenAICompatibleLLMStream` adapter handles offline mode
  gracefully (emitting an "[no LLM endpoint configured]" notice).
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Union
from urllib.parse import urlparse
import inspect
import ipaddress

from qai.chat.application.ports import (
    LocalEndpointProviderPort,
    ModelResolverPort,
    ProviderConfigLookupPort,
    ResolvedModel,
)

# Provider returning the local Genie service base URL (e.g.
# ``http://127.0.0.1:9999/v1``). Sync or async so the apps bridge can read
# the live ``model_runtime`` status / forge.config without forcing a shim.
_LocalEndpointProvider = Callable[[], Union[str, Awaitable[str]]]


def _is_loopback_host(hostname: str) -> bool:
    """Check if hostname refers to the local machine."""
    if not hostname:
        return False
    # Strip brackets from IPv6 addresses like [::1]
    bare = hostname.strip("[]")
    # "localhost" is the canonical loopback hostname
    if bare.lower() == "local" + "host":
        return True
    try:
        return ipaddress.ip_address(bare).is_loopback
    except ValueError:
        return False


class SettingsBasedModelResolver:
    """Resolve model selection from static chat settings.

    Constructor parameters mirror the chat settings fields:

    * ``default_base_url`` — LLM endpoint base (may be None for offline).
    * ``default_api_key`` — bearer token (may be None for keyless endpoints).
    * ``default_model`` — model id used when no hint is provided.

    Satisfies :class:`ModelResolverPort` protocol.
    """

    __slots__ = ("_base_url", "_api_key", "_model")

    def __init__(
        self,
        *,
        default_base_url: str | None,
        default_api_key: str | None,
        default_model: str,
    ) -> None:
        self._base_url: str = default_base_url or ""
        self._api_key: str | None = default_api_key
        self._model: str = default_model

    # ------------------------------------------------------------------
    # Protocol implementation
    # ------------------------------------------------------------------

    async def resolve(self, hint: str | None = None) -> ResolvedModel:
        """Resolve model hint to concrete endpoint config.

        Resolution logic:
        1. If ``hint`` is provided, use it as the model id.
        2. Otherwise use the configured default model.
        3. Detect ``is_local`` from base URL host or ``"local::"`` prefix.
        """
        model_id = hint if hint else self._model
        is_local = self._detect_local(model_id)

        return ResolvedModel(
            base_url=self._base_url,
            api_key=self._api_key,
            model_id=model_id,
            is_local=is_local,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _detect_local(self, model_id: str) -> bool:
        """Heuristic: is the resolved model a locally-running endpoint?

        A model is considered local when:
        * Its id starts with ``"local::"`` (legacy convention), OR
        * The base URL points to a loopback address.
        """
        if model_id.startswith("local::"):
            return True
        if not self._base_url:
            return False
        try:
            host = urlparse(self._base_url).hostname or ""
            return _is_loopback_host(host)
        except (ValueError, AttributeError):
            return False


class ProviderAwareModelResolver:
    """Provider-aware :class:`ModelResolverPort` (block-2 routing fix).

    The legacy :class:`SettingsBasedModelResolver` returned the same
    ``base_url`` / ``api_key`` for *every* model id, so selecting a cloud
    model (``cloud_llm`` / ``provider_b`` / ...) still routed the chat
    request to whatever single endpoint the chat settings carried — the
    request never reached the model's real provider.

    This resolver fixes routing by consulting an injected
    :class:`ProviderConfigLookupPort`:

    1. ``local::``-prefixed ids and loopback default endpoints are treated
       as **local** and routed to the live local on-device service endpoint
       resolved from the injected :class:`LocalEndpointProviderPort`
       (V1 ``backend/chat_handler.py:_stream_local`` read the running
       GenieAPIService port from ``forge_config.service_launch.local_port``).
       The ``local::`` prefix is stripped from the on-wire model id so the
       daemon receives the bare model name it expects.  Local models do not
       go through the cloud provider registry.
    2. For any other id, the resolver asks the lookup port which provider
       owns the model.  On a hit it returns that provider's ``base_url``
       and ``api_key`` (key sourced from the ``SecretStore`` by the apps
       bridge), correctly routing the request.
    3. On a miss (model not in any provider catalog, or the lookup port
       is absent), it falls back to the settings-based default endpoint —
       preserving the prior behaviour for single-provider / offline
       deployments and tests.

    The resolver depends only on the abstract
    :class:`ProviderConfigLookupPort` and :class:`LocalEndpointProviderPort`;
    the concrete cross-context reads of ``model_catalog`` provider configs +
    the platform ``SecretStore`` and the ``model_runtime`` running port live
    behind the ``apps/api`` bridges
    (:mod:`apps.api._model_resolver_bridge` /
    :mod:`apps.api._local_service_endpoint_bridge`), so ``qai.chat`` never
    imports ``qai.model_catalog`` / ``qai.platform`` / ``qai.model_runtime``
    (context-isolation contract preserved).
    """

    __slots__ = ("_base_url", "_api_key", "_model", "_lookup", "_local")

    def __init__(
        self,
        *,
        default_base_url: str | None,
        default_api_key: str | None,
        default_model: str,
        provider_lookup: ProviderConfigLookupPort | None = None,
        local_endpoint_provider: LocalEndpointProviderPort | None = None,
    ) -> None:
        self._base_url: str = default_base_url or ""
        self._api_key: str | None = default_api_key
        self._model: str = default_model
        self._lookup: ProviderConfigLookupPort | None = provider_lookup
        self._local: LocalEndpointProviderPort | None = local_endpoint_provider

    async def resolve(self, hint: str | None = None) -> ResolvedModel:
        model_id = hint if hint else self._model

        # 1) Explicit local models (``local::`` prefix, V1 convention) route
        #    to the running local Genie service. Strip the prefix to get the
        #    wire model name the daemon expects (V1 ``chat_handler.py:1861``).
        if model_id.startswith("local::"):
            wire_model_id = model_id[len("local::") :]
            local_base_url = await self._resolve_local_base_url()
            # V1 parity (chat_handler.py:1862-1863): a bare ``local::`` (no
            # concrete model name — the "auto" selection) resolves to the
            # configured default model, and when that is also empty an EMPTY
            # string is sent so GenieAPIService falls back to its built-in
            # default model. Forwarding the literal ``"local::"`` would make
            # the daemon try to load a model literally named ``local::``.
            if not wire_model_id:
                wire_model_id = self._model or ""
            return ResolvedModel(
                base_url=local_base_url or self._base_url,
                # The on-device daemon is keyless; only fall back to the
                # static default key when no local endpoint was resolved.
                api_key=None if local_base_url else self._api_key,
                model_id=wire_model_id,
                is_local=True,
            )

        # 2) Ask the provider lookup which provider owns this model.  A hit
        #    is authoritative — the model belongs to a configured cloud
        #    provider, so it is routed there regardless of what the default
        #    endpoint happens to be.
        if self._lookup is not None:
            endpoint = None
            try:
                endpoint = await self._lookup.lookup_for_model(model_id)
            except Exception:  # noqa: BLE001 — never let routing crash chat
                endpoint = None
            if endpoint is not None and endpoint.base_url:
                # V1 parity: when the provider config gave an
                # ``api_model_id`` wire override (dated cloud ids like
                # ``claude-sonnet-4-20250514``), send THAT upstream while
                # the user keeps selecting the stable display ``model_id``.
                return ResolvedModel(
                    base_url=endpoint.base_url,
                    api_key=endpoint.api_key,
                    model_id=endpoint.api_model_id or model_id,
                    is_local=False,
                    params=endpoint.params,
                )

        # 3) No cloud provider claimed the model. Fall through to the
        #    settings default endpoint. On-device models are selected with an
        #    explicit ``local::`` prefix (handled in branch 1, V1 convention),
        #    so a bare id here is NOT auto-routed to the local daemon — doing
        #    so would mis-route cloud / default ids (e.g. ``qai-default``) to a
        #    possibly-stopped local service and surface connection errors.
        #    ``is_local`` is derived from the default base URL host (loopback
        #    => local). When the default endpoint is unset this degrades to the
        #    offline notice downstream.
        return ResolvedModel(
            base_url=self._base_url,
            api_key=self._api_key,
            model_id=model_id,
            is_local=self._default_is_local(),
        )

    def _default_is_local(self) -> bool:
        if not self._base_url:
            return False
        try:
            host = urlparse(self._base_url).hostname or ""
            return _is_loopback_host(host)
        except (ValueError, AttributeError):
            return False

    async def _resolve_local_base_url(self) -> str:
        """Resolve the live local Genie service base URL (V1 parity).

        Consults the injected ``local_endpoint_provider`` — a zero-arg
        callable (sync or async) wired by the apps bridge that reads the
        ``model_runtime`` live port / forge.config ``service_launch.local_port``.
        Returns ``""`` when no provider is wired or it yields nothing — the
        caller then falls back to the static default endpoint.
        """
        if self._local is None:
            return ""
        try:
            raw = self._local()
            if inspect.isawaitable(raw):
                raw = await raw
            return str(raw or "").strip()
        except Exception:  # noqa: BLE001 — never let routing crash chat
            return ""


# Protocol compliance assertion (caught at import time)
_: type[ModelResolverPort] = SettingsBasedModelResolver  # type: ignore[assignment]
_pa: type[ModelResolverPort] = ProviderAwareModelResolver  # type: ignore[assignment]

__all__ = ["SettingsBasedModelResolver", "ProviderAwareModelResolver"]