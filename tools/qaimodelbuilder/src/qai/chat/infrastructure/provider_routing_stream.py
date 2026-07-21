# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Provider-aware :class:`LLMStreamPort` wrapper (block-2 routing fix).

The single-endpoint :class:`HttpOpenAICompatibleLLMStream` is constructed
once at DI time with a fixed ``base_url`` / ``api_key`` / ``model`` pulled
from the chat settings.  That means every chat turn — regardless of which
model the user picked in the dropdown — was sent to that single endpoint.
Cloud models (``cloud_llm`` / ``provider_b`` / ...) could be listed and
selected but the actual chat request never reached the model's real
provider.

:class:`ProviderRoutingLLMStream` closes that gap.  It implements
:class:`~qai.chat.application.ports.LLMStreamPort` and sits in front of the
HTTP transport:

1. On each :meth:`stream` call it reads the request's ``model_hint`` and
   asks the injected
   :class:`~qai.chat.application.ports.ModelResolverPort` to resolve it to
   a concrete :class:`~qai.chat.application.ports.ResolvedModel`
   (``base_url`` + ``api_key`` + ``model_id`` + ``is_local``).  The
   resolver is provider-aware (see
   :class:`qai.chat.adapters.model_resolver.ProviderAwareModelResolver`):
   a selected cloud model resolves to *its* provider's endpoint + key
   (the key sourced from the OS-keyring ``SecretStore`` via the apps
   bridge), while local models keep the local service endpoint.

2. It then delegates to the right transport for the resolved endpoint,
   forwarding the request **with the resolved ``model_id`` as the
   ``model_hint``** so the on-wire ``model`` field is the concrete id the
   provider expects:

   * **Cloud models** (``resolved.is_local == False``) go to a
     :class:`HttpOpenAICompatibleLLMStream` configured for the provider
     endpoint (function-calling-style ``tool_calls`` / ``finish_reason``
     handling).
   * **Local on-device models** (``resolved.is_local == True``,
     ``local::`` prefix) go to a per-endpoint
     :class:`~qai.chat.adapters.local_model_stream.LocalModelStreamAdapter`
     built by the injected ``local_stream_factory``.  The local adapter
     mirrors V1 ``backend/chat_handler.py:_stream_local`` — it ends the
     stream only on ``[DONE]`` (not on GenieAPIService's per-frame
     ``finish_reason: ""``, which the cloud adapter mis-reads as a normal
     end and truncates the reply at the first content token), parses inline
     XML ``<tool_call>`` blocks, and filters GenieAPIService status lines
     (``Processing long text...`` / ``Preparing inference...`` /
     ``Inferencing...``) out of the assistant body.  The factory is wired
     at the ``apps/api`` composition root because the concrete
     ``LocalModelStreamAdapter`` lives in the ``qai.chat.adapters`` layer
     (above ``qai.chat.infrastructure`` in the Clean-Architecture layering
     contract), so this module only ever sees the abstract
     :class:`LLMStreamPort` it returns.

Cross-context discipline
------------------------
This module depends only on the abstract ``ModelResolverPort`` /
``ResolvedModel`` (both defined in ``qai.chat.application.ports``) and on
the chat-local HTTP transport.  It imports **nothing** from
``qai.model_catalog`` / ``qai.platform``; the concrete provider-config +
``SecretStore`` reads live entirely behind the apps-layer bridge that the
resolver was wired with (see :mod:`apps.api._model_resolver_bridge`), so
the ``context-isolation`` import-linter contract is preserved.

Backward compatibility
-----------------------
* When no resolver is wired (``model_resolver=None``) the wrapper behaves
  exactly like the default single-endpoint stream — DI / tests that do not
  configure a resolver keep their prior behaviour byte-for-byte.
* When the resolver returns an empty ``base_url`` (offline / unconfigured)
  the wrapper delegates to the default stream, which already degrades to
  the deterministic ``"[no LLM endpoint configured]"`` offline notice.
* Resolver exceptions never crash a turn: the wrapper falls back to the
  default stream and logs a warning.

Per-endpoint transports are memoised by ``(base_url, api_key, model_id)``
so repeated turns against the same provider reuse one adapter instance
(the underlying ``httpx.AsyncClient`` is still minted per stream call by
the adapter's ``client_factory``, so there is no shared connection-state
hazard).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable
from typing import Any

from qai.chat.application.ports import (
    LLMStreamPort,
    LLMStreamRequest,
    ModelResolverPort,
    ResolvedModel,
    RuntimeLimitStorePort,
)
from qai.chat.application.provider_cache_capability import (
    ProviderCacheCapabilityRegistry,
)
from qai.chat.domain.model_profiles import get_model_profile
from qai.chat.domain.stream_frame import StreamFrame
from qai.chat.infrastructure.llm_stream import HttpOpenAICompatibleLLMStream
from qai.platform.ids import IdGenerator
from qai.platform.logging import get_logger

__all__ = ["ProviderRoutingLLMStream"]

_log = get_logger(__name__)

# Reserved model-hint scheme prefix for query services (internal-only). A hint
# like ``query::cebot`` routes the turn to the injected query-service transport
# instead of a model-catalog provider — mirroring the ``local::`` prefix
# convention for on-device models, without polluting the provider registry with
# a non-model entry. See ``qai.chat.infrastructure.query_service``.
_QUERY_SERVICE_PREFIX = "query::"


# ---------------------------------------------------------------------------
# Vertex AI / Google Gemini "thinking" family detection (SIG-FLATTEN-GATE-1).
# ---------------------------------------------------------------------------
# Vertex AI / Google Gemini thinking models stream a ``thought_signature`` on
# each tool_call that the NEXT turn MUST echo back verbatim, or the API returns
# 400 ("content block is missing a thought_signature"). The flatten guard
# (``__flatten_no_signature__`` → ``flatten_tool_calls_without_signature``)
# degrades a signature-less historical tool round to plain text instead of
# 400ing. Historically that guard was PURELY opt-in (user had to declare
# ``params.thought_signature.required`` in ``cloud_models.json``), so a user who
# hand-added a Vertex Gemini model but forgot that field would get a hard 400
# instead of graceful degradation. We close that "config blind spot" by ALSO
# auto-inferring the requirement from the model id's family — WITHOUT touching
# the explicit-config path (an explicit declaration, including an explicit
# ``required: false``, always wins; see ``_thought_signature_required``).
#
# Conservative matcher (avoid误伤 non-Gemini models, whose tool rounds carry no
# signature and must NOT be folded to text): require BOTH a Vertex/Google
# provider marker AND a ``gemini`` model token. The model id may be the
# catalog ``provider::model`` form (e.g. a Vertex AI Gemini model id) or a
# bare wire id (``gemini-2.5-pro``); we scan both ``model_id`` and the optional
# ``api_model_id`` wire override.
_VERTEX_PROVIDER_RE = re.compile(r"(?:^|[:/\s])(?:vertex(?:ai)?|google)\b", re.I)
_GEMINI_MODEL_RE = re.compile(r"\bgemini\b", re.I)


def _is_vertex_gemini_family(*model_ids: str | None) -> bool:
    """True if any given id looks like a Vertex AI / Google Gemini model.

    Used ONLY to auto-enable the missing-signature flatten guard for the
    signature-requiring Gemini family. Deliberately narrow (provider marker +
    ``gemini`` token) so an ordinary non-Gemini cloud model is never matched
    (which would wrongly fold its signatureless tool rounds to text).
    """
    for mid in model_ids:
        if not mid:
            continue
        # A ``vertexai::``/``google::`` provider prefix alone is a strong signal;
        # otherwise require the ``gemini`` token to be present (a bare
        # ``gemini-2.5-pro`` wire id behind a generic OpenAI-compat proxy).
        if _VERTEX_PROVIDER_RE.search(mid) and _GEMINI_MODEL_RE.search(mid):
            return True
        if _VERTEX_PROVIDER_RE.search(mid) and "::" in mid:
            # ``vertexai::<anything>`` — the Vertex endpoint serves Gemini; the
            # provider prefix is authoritative even if the bare model token was
            # renamed (the user-warned "renamed id no longer matches" case).
            return True
    return False


def _thought_signature_required(
    model_params: dict[str, Any] | None,
    *model_ids: str | None,
) -> bool:
    """Decide whether to enable the missing-signature flatten guard.

    Precedence (SIG-FLATTEN-GATE-1):
    1. **Explicit config wins** — if ``params.thought_signature.required`` is
       declared (truthy OR explicit ``false``), honour it verbatim. This keeps
       the user's opt-in / opt-out authoritative and lets a user force-disable
       the guard on a Gemini model if they ever need to.
    2. **Family auto-inference** — otherwise, enable it when the model id looks
       like a Vertex/Google Gemini thinking model (closes the "forgot to
       declare it" blind spot).
    3. Else off (ordinary non-Gemini models — unchanged behaviour).
    """
    if isinstance(model_params, dict):
        ts = model_params.get("thought_signature")
        if isinstance(ts, dict) and "required" in ts:
            return bool(ts.get("required"))
    return _is_vertex_gemini_family(*model_ids)


def _inject_family_sampling_defaults(
    extra: dict[str, Any],
    *,
    model_id: str,
    is_local: bool,
) -> None:
    """Backfill the family **lock / default** sampling params in place.

    SINGLE收口点 for "what sampling params does a turn get when the caller did
    NOT spell them out?".  Previously the main agent did this in
    ``StreamChatUseCase._apply_sampling_params`` (application layer) while the
    autonomous sub-agent re-did a partial copy in
    ``agent_tool._open_round_stream`` (adapter layer) and the discussion
    speaker / social / validator paths did NOT do it at all — so a cloud
    sub-agent / discussion speaker could silently drop ``max_tokens`` (gateway
    truncates at 4096) and a family-locked model (GPT-5 / o-series /
    DeepSeek-R1 force ``temperature``) could 400 from a sub-agent / discussion
    turn that never injected the lock.

    Routing every ``LLMStreamRequest`` through ``_select_target`` makes this
    the natural choke point: backfill here and all callers (main agent, sub
    agent, discussion speaker/social/validator) get the identical family policy
    with ZERO per-call duplication.

    Semantics — byte-for-byte parity with the legacy
    ``_apply_sampling_params`` (streaming.py) precedence:

    * **temperature**: ``resolve_temperature(user_value=extra.get("temperature"))``
      — family **lock** (``temperature_fixed``) overrides whatever the caller
      put in ``extra`` (the API enforces it); else the caller's value is
      respected; else the classic ``0.7`` default.  Always non-None for cloud,
      so it is always written.
    * **top_p**: ``resolve_top_p(user_value=extra.get("top_p"))`` — family lock
      wins; else caller value; else omit (None → leave unset).
    * **max_tokens**: ``resolve_max_tokens(user_value=extra.get("max_tokens"))``
      — caller value > family default; ``None`` / ``<=0`` (unknown / ``query::``
      route) leaves it unset (no regression).

    Because each ``resolve_*`` is fed the caller's existing ``extra`` value as
    its ``user_value``, an intentional caller override (compression
    ``max_tokens=3000`` / ``temperature=0.3``) is **preserved** — only a family
    **lock** ever overrides it (which is an API correctness requirement), and a
    family **default** only fills a key the caller left absent.  This also makes
    the helper **idempotent**: if the main agent already populated these top
    level keys, re-resolving with the same values is a no-op.

    Local-model gate (parity with the prior sub-agent ``_is_local_model_hint``
    skip): local on-device models self-manage their generation window, so a
    cloud family ``max_tokens`` default (e.g. 4096) would mis-limit them — skip
    ``max_tokens`` entirely for local.  temperature / top_p were likewise never
    injected for the local path historically (the local adapter does not route
    through ``_apply_sampling_params``), so they stay un-injected here too — no
    behaviour change for local turns.
    """
    if is_local:
        return

    try:
        profile = get_model_profile(model_id or "")
    except Exception as exc:  # noqa: BLE001 — a profile lookup never breaks a turn
        _log.warning("chat.sampling_profile_failed", error=str(exc))
        return

    def _as_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _as_int(value: Any) -> int | None:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    resolved_temp = profile.resolve_temperature(
        user_value=_as_float(extra.get("temperature")),
    )
    if resolved_temp is not None:
        extra["temperature"] = resolved_temp

    resolved_top_p = profile.resolve_top_p(
        user_value=_as_float(extra.get("top_p")),
    )
    if resolved_top_p is not None:
        extra["top_p"] = resolved_top_p

    resolved_max_tokens = profile.resolve_max_tokens(
        user_value=_as_int(extra.get("max_tokens")),
    )
    if resolved_max_tokens is not None and resolved_max_tokens > 0:
        extra["max_tokens"] = resolved_max_tokens


class ProviderRoutingLLMStream:
    """Route each chat turn to its model's provider endpoint.

    Construction parameters:

    * ``default_stream`` — the fallback :class:`HttpOpenAICompatibleLLMStream`
      built from the chat settings.  Used when no resolver is wired, when
      resolution misses / fails, or when the resolved ``base_url`` is empty
      (offline mode).
    * ``model_resolver`` — abstract :class:`ModelResolverPort`; when
      provider-aware (the production wiring) it routes cloud model ids to
      their provider's ``base_url`` + ``api_key``.  ``None`` disables
      routing entirely (delegates to ``default_stream``).
    * ``ids`` — id generator forwarded to per-endpoint transports.
    * ``client_factory`` / ``timeout_seconds`` — forwarded to per-endpoint
      transports so tests can inject a mock transport that observes the
      ``base_url`` / ``Authorization`` actually used.
    * ``runtime_limit_store`` — the D2 :class:`RuntimeLimitStorePort`
      process singleton holding runtime-learned ``max_tokens`` ceilings.
      Forwarded to every per-endpoint transport so the learned limits are
      shared process-wide across providers (matching V1's process-global
      ``_runtime_limits`` dict).  ``None`` lets each transport fall back to
      its own per-instance store.
    * ``local_stream_factory`` — ``Callable[[ResolvedModel], LLMStreamPort]``
      producing the on-device streaming adapter for a resolved local model
      (``resolved.is_local == True``).  Wired at the ``apps/api`` composition
      root with :class:`~qai.chat.adapters.local_model_stream.LocalModelStreamAdapter`
      (the concrete adapter lives in the ``adapters`` layer above this
      ``infrastructure`` module, so it is injected rather than imported).
      ``None`` keeps the legacy behaviour where local models also go through
      the cloud HTTP transport (used by tests that don't wire local routing).

    Satisfies :class:`LLMStreamPort`.
    """

    __slots__ = (
        "_default",
        "_resolver",
        "_ids",
        "_client_factory",
        "_timeout",
        "_ssl_verify",
        "_ssl_verify_provider",
        "_cache",
        "_runtime_limit_store",
        "_local_stream_factory",
        "_local_cache",
        "_query_stream_factory",
        "_provider_cache_registry",
    )

    def __init__(
        self,
        *,
        default_stream: HttpOpenAICompatibleLLMStream,
        model_resolver: ModelResolverPort | None,
        ids: IdGenerator,
        client_factory: Any | None = None,
        timeout_seconds: float | None = None,
        ssl_verify: bool = True,
        ssl_verify_provider: Callable[[], bool] | None = None,
        runtime_limit_store: RuntimeLimitStorePort | None = None,
        local_stream_factory: Callable[[ResolvedModel], LLMStreamPort]
        | None = None,
        query_stream_factory: Callable[[str], LLMStreamPort | None]
        | None = None,
        provider_cache_registry: "ProviderCacheCapabilityRegistry | None" = None,
    ) -> None:
        self._default = default_stream
        self._resolver = model_resolver
        self._ids = ids
        self._client_factory = client_factory
        self._timeout = timeout_seconds
        # Unified SSL switch (Settings.ssl_verify): forwarded to every
        # per-provider transport built by ``_transport_for`` so cloud turns
        # routed to a selected provider honour the SAME verify setting as the
        # default single-endpoint stream.  Without this the per-provider
        # transports silently reverted to ``HttpOpenAICompatibleLLMStream``'s
        # ``ssl_verify=True`` default and failed SSL verification against
        # internal https gateways (cloud LLM service / internal LLM endpoint) on the internal
        # edition (where ``ssl_verify`` defaults to False) — surfacing as an
        # endless ``category=network`` connect-error retry loop.
        self._ssl_verify = ssl_verify
        # Live Settings.ssl_verify provider forwarded (not the frozen bool) into
        # every minted per-provider transport. Because the transports are CACHED
        # (``_cache``) but each reads the provider at request time (when it builds
        # its httpx client), a runtime SSL toggle hot-applies to all cached
        # transports automatically — NO cache invalidation is needed. The bool
        # above stays the back-compat fallback when no provider is injected.
        self._ssl_verify_provider = ssl_verify_provider
        self._runtime_limit_store = runtime_limit_store
        self._local_stream_factory = local_stream_factory
        self._query_stream_factory = query_stream_factory
        # 方案B: shared registry that learns each gateway's Anthropic prompt-
        # cache support from response usage. ``_select_target`` records the
        # model_hint → resolved base_url mapping here (the ONLY place the real
        # gateway URL is known) so the use-case / sub-agent layers — which only
        # hold a model_hint — can resolve the gateway key. ``None`` (legacy /
        # unit stubs) disables learning; every aging gate then falls back to the
        # prior unconditional-aging behaviour (byte-for-byte backward compat).
        self._provider_cache_registry = provider_cache_registry
        # ``(base_url, api_key, model_id) -> HttpOpenAICompatibleLLMStream``.
        self._cache: dict[
            tuple[str, str, str], HttpOpenAICompatibleLLMStream
        ] = {}
        # ``(base_url, api_key, model_id) -> LLMStreamPort`` for local models.
        self._local_cache: dict[tuple[str, str, str], LLMStreamPort] = {}

    def stream(
        self,
        request: LLMStreamRequest,
    ) -> AsyncIterator[StreamFrame]:
        """Resolve the request's model, then stream from its provider."""
        return self._iter(request)

    async def _iter(
        self, request: LLMStreamRequest
    ) -> AsyncIterator[StreamFrame]:
        target, effective_request = await self._select_target(request)
        async for frame in target.stream(effective_request):
            yield frame

    async def _select_target(
        self, request: LLMStreamRequest
    ) -> tuple[LLMStreamPort, LLMStreamRequest]:
        """Pick the transport + (possibly re-targeted) request for ``request``.

        Returns ``(transport, request)``.  On any miss / failure the
        default transport is returned with the original request unchanged.
        Local models (``resolved.is_local``) are routed to the on-device
        :class:`LocalModelStreamAdapter` built by ``local_stream_factory``
        (when wired); everything else uses the cloud HTTP transport.
        """
        # Query-service hints (``query::<id>``) bypass the model-catalog
        # resolver entirely — they are not model-catalog providers. Dispatch to
        # the injected query-service transport (internal-only).
        hint = request.model_hint or ""
        if hint.startswith(_QUERY_SERVICE_PREFIX):
            transport = self._query_transport_for(hint)
            if transport is not None:
                return transport, request
            # Miss (factory absent on external edition, or unknown id): fall
            # back to the default stream, but STRIP the ``query::`` hint first.
            # Forwarding the pseudo-hint as a real ``model`` would make the
            # cloud transport send "query::cebot" upstream and surface a
            # confusing "model not found" error; clearing it lets the default
            # stream use its own configured default model (or the offline
            # notice when unconfigured) — a clean graceful degrade.
            _log.warning(
                "chat.provider_routing.query_service_unavailable",
                model_hint=hint,
            )
            fallback_request = LLMStreamRequest(
                conversation_id=request.conversation_id,
                tab_id=request.tab_id,
                prompt=request.prompt,
                history=request.history,
                model_hint=None,
                extra=request.extra,
            )
            return self._default, fallback_request

        if self._resolver is None:
            return self._default, request

        resolved: ResolvedModel | None = None
        try:
            resolved = await self._resolver.resolve(request.model_hint)
        except Exception as exc:  # noqa: BLE001 — never crash a turn
            _log.warning(
                "chat.provider_routing.resolve_failed",
                model_hint=request.model_hint,
                error=str(exc),
            )
            return self._default, request

        if resolved is None or not resolved.base_url:
            # Offline / unconfigured / miss — default stream already
            # handles the empty-base_url offline-notice path.
            return self._default, request

        # 方案B: record the model_hint → resolved gateway base_url mapping so the
        # use-case / sub-agent aging gate (which only holds a model_hint) can
        # resolve the gateway key. Best-effort + None-safe: the query::/local/
        # fallback paths returned above never reach here with a base_url, and
        # ``note_route`` itself no-ops when either arg is falsy. Recorded under
        # BOTH the incoming ``request.model_hint`` (what the use-case layer will
        # later look up) — the resolved concrete id is not the lookup key the
        # aging gate uses.
        if self._provider_cache_registry is not None:
            self._provider_cache_registry.note_route(
                request.model_hint, resolved.base_url
            )

        transport: LLMStreamPort
        if resolved.is_local and self._local_stream_factory is not None:
            # On-device model: route to the LocalModelStreamAdapter (V1
            # ``_stream_local`` parity).  The cloud HTTP transport mis-reads
            # GenieAPIService's per-frame ``finish_reason: ""`` as a normal
            # stream end and truncates the reply at the first content token;
            # the local adapter ends only on ``[DONE]`` and filters the
            # service status lines out of the body.
            transport = self._local_transport_for(resolved)
        else:
            transport = self._transport_for(resolved)
        # Forward the resolved concrete model id as the on-wire model.
        # PR-fix-cloud-tools (2026-06-04): surface the resolved
        # ``is_local`` flag onto ``request.extra["__is_local_model__"]``
        # so the downstream HTTP transport's ``_build_payload`` can decide
        # whether to advertise tools as the OpenAI standard
        # ``payload["tools"]`` (cloud) or rely on the system-prompt
        # ``<tools>`` XML block (local models without function-calling
        # support).  Adapter-internal control key (double-underscore
        # prefix) so it is filtered out before the payload reaches the
        # wire (see ``_build_payload`` reserved-keys loop).
        merged_extra: dict[str, Any] = (
            dict(request.extra) if request.extra else {}
        )
        merged_extra["__is_local_model__"] = bool(resolved.is_local)
        # Thread the per-model sampling-parameter constraints (cloud catalog
        # ``models[].params``) so payload construction can honour the user's
        # explicit declarations (e.g. ``temperature.supported=false``) on top
        # of — and overriding — the hard-coded family-regex defaults.  This is
        # the safety net for the case the user warned about: a model whose id
        # was renamed no longer matches the family regex, so the explicit
        # config is the only thing that prevents an unsupported-param 400.
        model_params = resolved.params if isinstance(resolved.params, dict) else None
        if model_params:
            merged_extra["__model_params__"] = model_params
        # Vertex AI / Google Gemini thinking models need the pre-send history
        # flatten so a turn whose prior tool_calls lack a ``thought_signature``
        # does not 400. Decide via ``_thought_signature_required`` (SIG-FLATTEN-
        # GATE-1): explicit ``params.thought_signature.required`` wins; else
        # auto-infer from the Vertex/Gemini model family. NOTE: this is now
        # OUTSIDE the ``if model_params:`` guard — a user who hand-adds a Vertex
        # Gemini model with NO params at all still gets the guard (the previous
        # blind spot). Non-Gemini models stay unset and skip the flatten.
        if _thought_signature_required(
            model_params, resolved.model_id, resolved.api_model_id
        ):
            merged_extra["__flatten_no_signature__"] = True
        target_model_hint = (
            resolved.model_id
            if resolved.model_id and resolved.model_id != request.model_hint
            else request.model_hint
        )
        # Unified sampling-parameter收口 (C档 阶段1): backfill the family
        # lock / default temperature / top_p / max_tokens onto EVERY routed
        # request, so the main agent, autonomous sub-agent and discussion
        # speaker / social / validator paths all get the identical family
        # policy with no per-caller duplication. Idempotent + intentional
        # caller overrides preserved (see ``_inject_family_sampling_defaults``).
        # Keyed on the RESOLVED model id (the on-wire model) so it matches what
        # ``_build_payload`` sees. Local models skip max_tokens (window
        # self-managed); query::/unknown routes leave max_tokens unset.
        _inject_family_sampling_defaults(
            merged_extra,
            model_id=target_model_hint or "",
            is_local=bool(resolved.is_local),
        )
        effective_request = LLMStreamRequest(
            conversation_id=request.conversation_id,
            tab_id=request.tab_id,
            prompt=request.prompt,
            history=request.history,
            model_hint=target_model_hint,
            extra=merged_extra,
        )
        return transport, effective_request

    def _transport_for(
        self, resolved: ResolvedModel
    ) -> HttpOpenAICompatibleLLMStream:
        key = (
            resolved.base_url,
            resolved.api_key or "",
            resolved.model_id or "",
        )
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        kwargs: dict[str, Any] = {
            "base_url": resolved.base_url,
            "api_key": resolved.api_key,
            "model": resolved.model_id or "qai-default",
            "ids": self._ids,
            # A local on-device endpoint is keyless by design; a cloud provider
            # is expected to carry a key. This drives the transport's
            # pre-upstream "missing API key" guard so it fires only for cloud
            # providers the user hasn't set a key for (never for local models).
            "expects_api_key": not bool(resolved.is_local),
            # Honour the unified SSL switch on every per-provider transport
            # (parity with the default stream at _chat_di.py:1949). When a
            # ``client_factory`` is injected below it owns verify and this is
            # ignored; otherwise the transport's own default client factory
            # binds ``verify=ssl_verify``. We forward the LIVE provider too so
            # the cached transport reads Settings.ssl_verify at request time —
            # this is why the ``_cache`` needs no invalidation on a runtime
            # toggle (the frozen bool is the back-compat fallback).
            "ssl_verify": self._ssl_verify,
            "ssl_verify_provider": self._ssl_verify_provider,
        }
        if self._client_factory is not None:
            kwargs["client_factory"] = self._client_factory
        if self._timeout is not None:
            kwargs["timeout_seconds"] = self._timeout
        if self._runtime_limit_store is not None:
            kwargs["runtime_limit_store"] = self._runtime_limit_store
        transport = HttpOpenAICompatibleLLMStream(**kwargs)
        self._cache[key] = transport
        return transport

    def _local_transport_for(self, resolved: ResolvedModel) -> LLMStreamPort:
        """Build / reuse the on-device stream adapter for ``resolved``.

        Memoised by ``(base_url, api_key, model_id)`` just like the cloud
        transports.  Delegates construction to the injected
        ``local_stream_factory`` so the concrete
        :class:`LocalModelStreamAdapter` (``adapters`` layer) is never
        imported from this ``infrastructure`` module.
        """
        assert self._local_stream_factory is not None  # guarded by caller
        key = (
            resolved.base_url,
            resolved.api_key or "",
            resolved.model_id or "",
        )
        cached = self._local_cache.get(key)
        if cached is not None:
            return cached
        transport = self._local_stream_factory(resolved)
        self._local_cache[key] = transport
        return transport

    def _query_transport_for(self, model_hint: str) -> LLMStreamPort | None:
        """Build the query-service transport for ``model_hint`` (never cached).

        Delegates construction to the injected ``query_stream_factory`` so the
        concrete
        :class:`~qai.chat.infrastructure.query_service.adapter.QueryServiceAdapter`
        (built from edition config + ``SecretStore`` credential at the
        ``apps/api`` composition root) is created fresh on EACH turn. Not
        memoised: the factory re-reads the api_key from the ``SecretStore``
        every call, so a credential the user updated mid-session takes effect
        on the next turn without a restart (AGENTS.md §🔴 State-Truth-First —
        the credential's truth is the SecretStore, not a frozen construction
        snapshot). Adapter construction is cheap (object composition; the
        underlying httpx client is still minted per ``stream()`` call), so
        skipping the cache costs nothing meaningful. Returns ``None`` when no
        factory is wired (external edition) or the factory does not recognise
        the id — the caller then falls back to the default stream.
        """
        if self._query_stream_factory is None:
            return None
        return self._query_stream_factory(model_hint)


# Protocol compliance assertion (caught at import time).
_: type[LLMStreamPort] = ProviderRoutingLLMStream  # type: ignore[assignment]
