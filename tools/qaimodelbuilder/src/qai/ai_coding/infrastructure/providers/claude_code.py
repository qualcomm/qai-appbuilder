# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Claude Code provider adapter (PR-046, PR-102).

Real upstream is the Anthropic Messages API.  PR-102 wires the SSE
streaming loop end-to-end through :class:`HttpTransportPort`; tests
inject :class:`InMemorySseTransport` to replay canned wire bytes.
When no transport is wired (or no API key is configured) the legacy
4-frame scripted fallback runs so historical tests / offline tooling
continue to work.

API key lookup
--------------
The adapter resolves the API key from
:class:`qai.platform.persistence.secrets.SecretStore` under the
service namespace ``qai.ai_coding.claude_code`` and key
``api_key``.  Operators configure the value via the platform's
secret-store CLI; the adapter never logs it.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any
from urllib.parse import urlsplit

from qai.ai_coding.domain import (
    CodingSessionConfig,
    CodingSessionId,
    CodingStreamFrame,
    Provider,
    StreamFrameKind,
    Workspace,
)
from qai.platform.errors import NotFoundError
from qai.platform.logging import get_logger
from qai.platform.persistence.secrets import SecretStore

from ..upstream_model_fetcher import UpstreamModelFetcher
from .base import HttpCodingProviderBase, ProviderHttpConfig
from .http_transport import HttpTransportPort

__all__ = ["CLAUDE_CODE_DEFAULT_CONFIG", "ClaudeCodeProvider"]

logger = get_logger(__name__)

#: SecretStore service namespace where the WebUI persists CC
#: credentials (mirrors ``CC_SECRET_SERVICE`` in the application
#: layer).  The streaming adapter resolves its key from
#: ``qai.ai_coding.claude_code/api_key`` (see ``CLAUDE_CODE_DEFAULT_
#: CONFIG``), but the *model-catalog* probe must mirror V1's
#: ``_cc_resolve_api_key`` which reads the operator-facing
#: ``ANTHROPIC_API_KEY`` / ``ANTHROPIC_AUTH_TOKEN`` entries the auth
#: panel writes here.  The adapter only *reads* these (never logs).
_CC_CRED_SERVICE = "ai_coding"

#: Default upstream base URL (Anthropic official).  Used as the V1
#: ``default`` base-url source when neither config nor env override.
_DEFAULT_BASE_URL = "https://api.anthropic.com"


CLAUDE_CODE_DEFAULT_CONFIG = ProviderHttpConfig(
    base_url="https://api.anthropic.com",
    api_key_service="qai.ai_coding.claude_code",
    api_key_name="api_key",
    connect_timeout_s=15.0,
    read_timeout_s=60.0,
)


# Default Anthropic Messages API parameters.  Operators that want to
# pin a specific model or reduce max_tokens can swap the config; PR-107
# (SDK 12 enhancements) extends this with mcp_servers / hooks /
# fallback_model / etc.
DEFAULT_MODEL = "claude-sonnet-4-20250514"
DEFAULT_MAX_TOKENS = 4096
ANTHROPIC_VERSION_HEADER = "2023-06-01"
# PR-107: Anthropic MCP-client beta header.  Required when the
# request body carries a populated ``mcp_servers`` array.  See
# https://docs.anthropic.com/en/api/messages-mcp.
ANTHROPIC_MCP_BETA = "mcp-client-2025-04-04"


class ClaudeCodeProvider(HttpCodingProviderBase):
    """:class:`CodingProviderPort` adapter for the Claude Code backend."""

    __slots__ = (
        "_config_reader",
        "_fetcher_key",
        "_last_source_meta",
        "_max_tokens",
        "_model",
        "_model_fetcher",
        "_stream_usage",
        "_streaming_session_id",
        "_verify_ssl",
    )

    def __init__(
        self,
        *,
        secret_store: SecretStore,
        config: ProviderHttpConfig | None = None,
        transport: HttpTransportPort | None = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        config_reader: (
            Callable[[], Awaitable[Mapping[str, Any]] | Mapping[str, Any]] | None
        ) = None,
        verify_ssl: bool = True,
    ) -> None:
        super().__init__(
            provider=Provider.CLAUDE_CODE,
            config=config or CLAUDE_CODE_DEFAULT_CONFIG,
            secret_store=secret_store,
            transport=transport,
        )
        self._model = model
        self._max_tokens = max_tokens
        # Tracks the session_id currently being streamed so
        # _map_stream_frame can stash the upstream conversation id.
        self._streaming_session_id: str | None = None
        # M5: per-turn token usage accumulator.  Anthropic emits token
        # counts on ``message_start.message.usage`` (input / cache tokens)
        # and ``message_delta.usage`` (cumulative output tokens); we fold
        # them here so the terminal END frame carries a ``usage`` payload
        # — mirroring the OpenCode adapter's ``_accumulate_step_usage`` →
        # END ``{"usage": ...}`` chain (open_code.py:599-621,506-507).
        # Without this, ``query_context_usage`` saw no CC usage frames and
        # the chat context-occupancy bar stayed at 0 (V1 ``session_manager``
        # surfaced live SDK ``last_input_tokens`` / ``total_*``).
        self._stream_usage: dict[str, int] = {}
        # C1 (model-source badge): optional async/sync reader that
        # returns the persisted CC config document (forge_config).  The
        # adapter uses it to (a) resolve the ``ANTHROPIC_BASE_URL``
        # config override and (b) source the ``model_list`` fallback —
        # mirroring V1's ``_cc_resolve_base_url`` + ``_cc_fallback_models``.
        # Injected from the apps/DI layer so the infrastructure adapter
        # never imports the application config use case (clean-arch /
        # import-linter).  ``None`` → no config override / no fallback.
        self._config_reader = config_reader
        # Reuse the (previously-orphaned) UpstreamModelFetcher: it owns
        # the 5-minute cache + the 3 upstream response-shape parsers.
        # base_url / api_key are resolved per-call (they can change when
        # the operator edits the auth panel), so we lazily rebuild the
        # fetcher when either changes.
        self._model_fetcher: UpstreamModelFetcher | None = None
        # Cache identity = (base_url, api_key fingerprint).  When it
        # changes (operator switched gateway / rotated key) we drop the
        # old fetcher so its 5-min cache doesn't serve stale rows — V1
        # parity with ``_cc_models_cache`` keyed on the same tuple.
        self._fetcher_key: tuple[str, str] | None = None
        self._verify_ssl = verify_ssl
        # Snapshot of the last catalog probe's V1-parity metadata
        # (source / base_url / base_url_source / error / cached_age) so
        # the folded health response can surface it without re-fetching.
        self._last_source_meta: dict[str, Any] = {
            "source": "fallback-no-key",
            "base_url": _DEFAULT_BASE_URL,
            "base_url_source": "default",
            "error": None,
            "cached_age": None,
        }

    # ------------------------------------------------------------------
    # C1: model-source catalog (V1 ``/api/cc/models`` parity, folded
    # into the health response).  ``available_models`` returns the flat
    # ``{id, name, provider_id}`` rows the folded health endpoint
    # consumes; ``model_source_meta`` exposes the V1 4-state ``source``
    # + ``base_url`` metadata for the WebUI badge.  Both are best-effort
    # and never raise (a failure degrades to the forge_config fallback).
    # ------------------------------------------------------------------
    async def available_models(
        self, *, force_refresh: bool = False
    ) -> list[dict[str, Any]]:
        """Return the CC model catalog (upstream-enumerated or fallback).

        Mirrors V1 ``_cc_get_models_dynamic``: resolves the operator's
        base_url + api_key, enumerates the upstream ``/v1/models`` (via
        the cached :class:`UpstreamModelFetcher`), and falls back to the
        ``model_list`` in forge_config on a missing key / upstream
        failure.  The V1 4-state ``source`` (+ base_url metadata) is
        stashed on :attr:`_last_source_meta` so :meth:`model_source_meta`
        can surface it on the same probe without a second round-trip.
        """
        cfg = await self._read_config()
        base_url, base_url_source = self._resolve_base_url(cfg)
        api_key = self._resolve_catalog_api_key()

        meta: dict[str, Any] = {
            "base_url": self._public_base_url(base_url),
            "base_url_source": base_url_source,
            "error": None,
            "cached_age": None,
        }

        # No key → never issue an anonymous upstream call; fall back to
        # the configured model_list (V1 ``fallback-no-key``).
        if not api_key:
            meta["source"] = "fallback-no-key"
            meta["error"] = "no api key configured"
            self._last_source_meta = meta
            return self._fallback_models(cfg)

        fetcher = self._get_fetcher(base_url, api_key)
        try:
            raw = await fetcher.fetch_models(force_refresh=force_refresh)
        except Exception as exc:  # noqa: BLE001 — degrade to fallback.
            logger.warning(
                "ai_coding.cc.models_fetch_failed",
                base_url=self._public_base_url(base_url),
                error=str(exc),
            )
            meta["source"] = "fallback-error"
            meta["error"] = str(exc)[:200]
            self._last_source_meta = meta
            return self._fallback_models(cfg)

        if not raw:
            meta["source"] = "fallback-error"
            meta["error"] = "upstream returned empty list"
            self._last_source_meta = meta
            return self._fallback_models(cfg)

        # Distinguish a fresh upstream hit from a cache hit so the badge
        # can show ⚡ Cached (V1 parity).  The fetcher exposes its cache
        # age via the monotonic timestamp it stamped on the last fetch.
        age = fetcher.cache_age_seconds()
        if not force_refresh and age is not None and age > 0.5:
            meta["source"] = "cache"
            meta["cached_age"] = round(age, 1)
        else:
            meta["source"] = "upstream"
        self._last_source_meta = meta
        return [
            {
                "id": str(entry.get("id", "")),
                "name": str(entry.get("id", "")),
                "provider_id": Provider.CLAUDE_CODE.value,
            }
            for entry in raw
            if entry.get("id")
        ]

    async def model_source_meta(
        self, *, force_refresh: bool = False
    ) -> dict[str, Any]:
        """Return the V1-parity model-source metadata for the badge.

        Shape mirrors V1 ``/api/cc/models``'s envelope (minus the
        ``models`` list, surfaced separately via
        :meth:`available_models`): ``{source, base_url, base_url_source,
        error, cached_age}``.  Re-runs the catalog probe so a
        ``?refresh=1`` request bypasses the cache; the folded health
        endpoint calls :meth:`available_models` first (which refreshes
        :attr:`_last_source_meta`) so this normally just returns the
        snapshot.
        """
        if force_refresh:
            await self.available_models(force_refresh=True)
        return dict(self._last_source_meta)

    # -- catalog helpers --------------------------------------------------
    async def _read_config(self) -> Mapping[str, Any]:
        if self._config_reader is None:
            return {}
        try:
            result = self._config_reader()
            if hasattr(result, "__await__"):
                result = await result  # type: ignore[misc]
            return result if isinstance(result, Mapping) else {}
        except Exception:  # noqa: BLE001 — config probe is best-effort.
            return {}

    def _resolve_base_url(self, cfg: Mapping[str, Any]) -> tuple[str, str]:
        """Return ``(raw_base_url, source)`` — V1 ``_cc_resolve_base_url``.

        Priority (high → low): forge_config ``auth_env.ANTHROPIC_BASE_URL``
        (``config``) > process env ``ANTHROPIC_BASE_URL`` (``env``) >
        the Anthropic default (``default``).
        """
        auth_env = cfg.get("auth_env")
        if isinstance(auth_env, Mapping):
            cfg_url = auth_env.get("ANTHROPIC_BASE_URL")
            if isinstance(cfg_url, str) and cfg_url.strip():
                return cfg_url.strip(), "config"
        env_url = os.environ.get("ANTHROPIC_BASE_URL")
        if env_url and env_url.strip():
            return env_url.strip(), "env"
        return _DEFAULT_BASE_URL, "default"

    def _resolve_catalog_api_key(self) -> str | None:
        """Resolve the catalog probe key — V1 ``_cc_resolve_api_key``.

        Priority: env ``ANTHROPIC_API_KEY`` > env ``ANTHROPIC_AUTH_TOKEN``
        > SecretStore ``ai_coding/ANTHROPIC_API_KEY`` > SecretStore
        ``ai_coding/ANTHROPIC_AUTH_TOKEN``.  Reads the operator-facing
        credential namespace the auth panel writes (distinct from the
        streaming adapter's ``qai.ai_coding.claude_code/api_key``).
        """
        for env_name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            val = os.environ.get(env_name)
            if val and val.strip():
                return val.strip()
        for key_name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            try:
                val = self._secret_store.get(_CC_CRED_SERVICE, key_name)
            except NotFoundError:
                continue
            if val and val.strip():
                return val.strip()
        return None

    def _resolve_api_key_chain(self) -> str | None:
        """Streaming-path V1 parity (overrides
        :meth:`HttpCodingProviderBase._resolve_api_key_chain`).

        Delegates to :meth:`_resolve_catalog_api_key` so streaming and
        catalog probe share a single resolver, mirroring V1
        ``backend/ai_coding/api_routes.py:145-169 _cc_resolve_api_key``
        which was used by every CC code-path (health, catalog,
        streaming).  When this returns ``None`` the base falls back to
        the legacy ``qai.ai_coding.claude_code/api_key`` streaming
        namespace, preserving any pre-existing operator deployments
        that wrote a key there directly.
        """
        return self._resolve_catalog_api_key()

    def _get_fetcher(self, base_url: str, api_key: str) -> UpstreamModelFetcher:
        # The upstream /v1/models endpoint sits under the ``/v1`` path;
        # the fetcher appends ``/models`` to its base_url, so normalise
        # the operator's base_url to end at ``/v1`` (V1 parity with
        # ``_cc_build_models_url``).
        normalised = base_url.rstrip("/")
        if not normalised.endswith("/v1"):
            normalised = f"{normalised}/v1"
        key = (normalised, api_key[:8] + str(len(api_key)))
        if self._model_fetcher is None or self._fetcher_key != key:
            self._model_fetcher = UpstreamModelFetcher(
                base_url=normalised,
                api_key=api_key,
                verify_ssl=self._verify_ssl,
            )
            self._fetcher_key = key
        return self._model_fetcher

    def _fallback_models(self, cfg: Mapping[str, Any]) -> list[dict[str, Any]]:
        """Return the forge_config ``model_list`` fallback rows.

        Mirrors V1 ``_cc_fallback_models``: the operator-curated
        ``model_list`` from forge_config, plus the currently-selected
        ``model`` (so it's always selectable even if absent from the
        list).  Returns an empty list when no config reader is wired.
        """
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        model_list = cfg.get("model_list")
        if isinstance(model_list, list):
            for entry in model_list:
                mid: str | None = None
                if isinstance(entry, str):
                    mid = entry.strip()
                elif isinstance(entry, Mapping):
                    raw_id = entry.get("id") or entry.get("model")
                    mid = str(raw_id).strip() if raw_id else None
                if mid and mid not in seen:
                    seen.add(mid)
                    rows.append(
                        {
                            "id": mid,
                            "name": mid,
                            "provider_id": Provider.CLAUDE_CODE.value,
                        }
                    )
        current = cfg.get("model")
        if isinstance(current, str) and current.strip() and current.strip() not in seen:
            rows.insert(
                0,
                {
                    "id": current.strip(),
                    "name": current.strip(),
                    "provider_id": Provider.CLAUDE_CODE.value,
                },
            )
        return rows

    @staticmethod
    def _public_base_url(base_url: str) -> str:
        """Strip path/query/credentials → ``scheme://netloc`` (V1 parity).

        V1 returns only ``scheme://netloc`` to the WebUI so an embedded
        token in the gateway URL never leaks into the badge.
        """
        try:
            parts = urlsplit(base_url)
            if parts.scheme and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}"
        except Exception:  # noqa: BLE001
            pass
        return base_url

    async def stream(
        self,
        *,
        session_id: CodingSessionId,
    ) -> AsyncIterator[CodingStreamFrame]:
        """Override base to track which session is streaming for resume capture."""
        self._streaming_session_id = session_id.value
        # M5: reset the per-turn usage accumulator at the start of each turn.
        self._stream_usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }
        return await super().stream(session_id=session_id)

    def set_claude_session_id(
        self, session_id: CodingSessionId, claude_session_id: str
    ) -> None:
        """Seed the upstream session id for resume on the next stream call.

        Called by the application layer when restoring a session from
        persistence that already carries a ``claude_session_id``.
        """
        record = self._handles.setdefault(session_id.value, {})
        record["claude_session_id"] = claude_session_id

    def get_claude_session_id(
        self, session_id: CodingSessionId
    ) -> str | None:
        """Return the upstream session id captured during streaming.

        The application layer calls this after a stream completes to
        persist the value on the :class:`CodingSession` aggregate.
        """
        record = self._handles.get(session_id.value, {})
        return record.get("claude_session_id")

    def _build_spawn_payload(
        self,
        *,
        handle: dict[str, Any],
        workspace: Workspace,
    ) -> dict[str, Any]:
        # Mark the messages endpoint so a future real adapter can
        # use it; the application layer treats this dict as opaque.
        handle["messages_endpoint"] = (
            f"{self._config.base_url.rstrip('/')}/v1/messages"
        )
        handle["model"] = self._model
        return handle

    def _build_stream_url(self) -> str:
        return f"{self._config.base_url.rstrip('/')}/v1/messages"

    async def _resolve_stream_url(self) -> str:
        """Resolve the streaming endpoint from the operator's config.

        V1 parity: the streaming wire request and the catalog probe must
        share a single ``ANTHROPIC_BASE_URL`` truth source.  V1 achieved
        this by writing ``auth_env.ANTHROPIC_BASE_URL`` into
        ``os.environ`` on every config save, so the SDK (streaming) and
        ``_cc_resolve_base_url`` (catalog) both read the same value
        (``backend/ai_coding/api_routes.py:116-137,421-458``).

        V2 keeps the same user-perceived behaviour without the global
        mutable ``os.environ`` side-effect: we resolve the base_url here
        through the *same* :meth:`_resolve_base_url` chain the catalog
        path uses (forge_config ``auth_env.ANTHROPIC_BASE_URL`` > process
        env ``ANTHROPIC_BASE_URL`` > Anthropic default), then append the
        Anthropic ``/v1/messages`` suffix.  This is symmetric with
        :meth:`_resolve_api_key_chain`, which already unified the api-key
        resolution across both paths.  Normalises a trailing ``/v1`` so a
        base ending in ``/v1`` does not yield ``/v1/v1/messages``.
        """
        cfg = await self._read_config()
        base_url, _source = self._resolve_base_url(cfg)
        normalised = base_url.rstrip("/")
        if normalised.endswith("/v1"):
            return f"{normalised}/messages"
        return f"{normalised}/v1/messages"

    def _session_config(
        self, session_id: CodingSessionId
    ) -> CodingSessionConfig:
        """Return the per-session :class:`CodingSessionConfig`.

        Convenience wrapper around the base class helper that also
        coerces ``None`` (no config supplied at spawn) into a default
        empty config so call sites can always assume a real value.
        """
        return self.session_config(session_id)

    def _build_stream_headers(
        self,
        *,
        api_key: str,
        session_id: CodingSessionId | None = None,
    ) -> dict[str, str]:
        # Anthropic uses ``x-api-key`` (lowercase) and a stable
        # ``anthropic-version`` header — see
        # https://docs.anthropic.com/en/api/messages.  ``Authorization``
        # is NOT used by Anthropic (legacy compatibility for
        # third-party gateways is handled by Anthropic-compatible
        # services that accept either header).
        headers: dict[str, str] = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION_HEADER,
        }
        # PR-107: opt into the MCP-client beta when the per-session
        # config carries a non-empty ``mcp_servers`` array.  This is
        # the only PR-107 field that requires a header (the rest land
        # in the JSON body).
        if session_id is not None:
            cfg = self._session_config(session_id)
            beta_flags: list[str] = []
            if cfg.mcp_servers:
                beta_flags.append(ANTHROPIC_MCP_BETA)
            # 2-H8: forward any operator-configured Anthropic beta flags
            # (V1 ``session_manager.py:1766-1774``) on the ``anthropic-beta``
            # header alongside the MCP beta.  Anthropic accepts a
            # comma-separated list; dedupe while preserving order so a
            # caller that also listed the MCP beta does not double it.
            for beta in cfg.betas:
                if beta not in beta_flags:
                    beta_flags.append(beta)
            if beta_flags:
                headers["anthropic-beta"] = ",".join(beta_flags)
        return headers

    def _build_stream_body(
        self, *, session_id: CodingSessionId, api_key: str
    ) -> dict[str, Any]:
        # Multi-turn: prefer the structured session history (populated
        # by send_message / _record_assistant_response /
        # _inject_tool_results) over the legacy flat text list.
        history = self._get_session_history(session_id)
        if history:
            messages = list(history)
        else:
            # Fallback: legacy path for sessions that used the old
            # single-turn send_message (flat text list in _handles).
            record = self._handles.get(session_id.value, {})
            messages = [
                {"role": "user", "content": text}
                for text in (record.get("messages") or [])
            ]
        if not messages:
            # Anthropic rejects an empty messages array; fall back to a
            # benign placeholder so the API call doesn't 400.
            messages = [{"role": "user", "content": "(no message)"}]
        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "stream": True,
            "messages": messages,
        }

        # C-1 (V1 ``session_manager.py:1390-1432`` parity): inject the
        # OS-context / Git-Bash path hint into the Anthropic Messages
        # top-level ``system`` field so the agent knows it is on Windows
        # under MSYS bash and translates ``C:\\foo`` -> ``/c/foo`` before
        # issuing shell/file tool calls.  V1 prepended this block to every
        # turn's system prompt; without it the model emits PowerShell
        # one-liners / Windows paths that fail in the agent shell.  The
        # hint is stashed on the handle at spawn time (single truth
        # source); ``_os_hint_for_session`` falls back to a live build for
        # sessions restored without a fresh spawn.
        os_hint = self._os_hint_for_session(session_id)
        if os_hint:
            body["system"] = os_hint

        # ------------------------------------------------------------------
        # NOTE (HTTP-fallback hardening): the Anthropic Messages API does
        # NOT accept ``resume`` / ``session_id`` top-level keys — and V1
        # had NO HTTP provider at all (it ran exclusively on the
        # ``claude_agent_sdk`` CLI, where ``resume`` is a CLI option, not a
        # wire field).  Sending them to a strict Anthropic endpoint 400s.
        # Multi-turn context is carried by replaying the full ``messages``
        # history (built above), which IS the Anthropic-native way to
        # continue a conversation.  The captured upstream id is still kept
        # on the handle for the SDK backend's ``resume=``; the HTTP body
        # just no longer ships the non-official keys.
        # ------------------------------------------------------------------

        # ------------------------------------------------------------------
        # PR-107: SDK 12-item enhancements → Anthropic Messages wire body
        # ------------------------------------------------------------------
        # Field routing (per task spec):
        #
        # Wire body (this method):
        #   - mcp_servers   → top-level ``mcp_servers`` array
        #   - max_budget_usd / fallback_model / task_budget / user /
        #     extra_args → flat-merged into ``metadata``
        #
        # Reserved for future agent runtime; currently descriptive only.
        # These fields are persisted via :meth:`session_config` for replay
        # across spawn but are not read by V2 active code paths
        # (``qai.chat`` 流式工具循环已覆盖 V1 chat_handler 全部能力，
        # 参见 GAP-PLAN F-2):
        #   - hooks / output_format / setting_sources / plugins /
        #     session_env
        #
        # Anthropic's API rejects unknown top-level keys with a 400, so
        # we explicitly DO NOT splat the whole config — every field is
        # routed individually.
        cfg = self._session_config(session_id)

        if cfg.mcp_servers:
            body["mcp_servers"] = [
                {
                    "name": server.name,
                    "url": server.url,
                    "transport": server.transport,
                    **(
                        {"authorization_token": server.auth.get("token")}
                        if "token" in server.auth
                        else {}
                    ),
                }
                for server in cfg.mcp_servers
            ]

        metadata: dict[str, Any] = {}
        if cfg.user is not None:
            # Anthropic's documented "stable user identifier" key.
            metadata["user_id"] = cfg.user
        if cfg.max_budget_usd is not None:
            metadata["max_budget_usd"] = cfg.max_budget_usd
        if cfg.fallback_model is not None:
            metadata["fallback_model"] = cfg.fallback_model
        if cfg.task_budget is not None:
            metadata["task_budget"] = cfg.task_budget
        if cfg.extra_args:
            # Operator escape hatch — flat-merge ``extra_args`` into
            # ``metadata`` so vendor-specific knobs (e.g. ``project``,
            # ``deployment``) ride along without code changes.  Every
            # value is a string by VO contract.
            for key, value in cfg.extra_args.items():
                metadata.setdefault(key, value)
        if metadata:
            body["metadata"] = metadata

        # ------------------------------------------------------------------
        # 2-H7: thinking / effort → Anthropic ``thinking`` wire field
        # ------------------------------------------------------------------
        # V1 (``session_manager.py:1712-1722,1839-1840``) forwarded BOTH a
        # coarse ``effort`` CLI keyword (low/medium/high/max) and an
        # explicit structured ``thinking`` (``ThinkingConfig`` dict whose
        # ``type`` is set) into ``ClaudeAgentOptions``.  V2 routes them into
        # the Anthropic Messages ``thinking`` body field:
        #
        #   * an explicit ``cfg.thinking`` (validated to carry ``type``)
        #     wins verbatim — it is the structured SDK payload;
        #   * else a ``cfg.effort`` keyword maps to an extended-thinking
        #     budget (``{"type":"enabled","budget_tokens":N}``) so the
        #     coarse CLI knob still drives real thinking depth.
        #
        # ``metadata.effort`` is ALSO surfaced (V1 kept the raw keyword on
        # the request metadata) so downstream telemetry / gateways that
        # key on it keep working.
        thinking_body = self._resolve_thinking_body(cfg)
        if thinking_body is not None:
            body["thinking"] = thinking_body
        if cfg.effort is not None:
            meta = body.setdefault("metadata", {})
            meta.setdefault("effort", cfg.effort)

        # ------------------------------------------------------------------
        # 2-H8: betas → ``anthropic-beta`` HEADER only (not a body key)
        # ------------------------------------------------------------------
        # The Anthropic Messages API has NO top-level ``add_dirs`` /
        # ``agents`` / ``betas`` body keys — sending them 400s a strict
        # endpoint.  ``betas`` is forwarded the official way, on the
        # ``anthropic-beta`` header (see :meth:`_build_stream_headers`).
        # ``add_dirs`` (extra readable dirs) and ``agents`` (named
        # sub-agents) are CLI-only capabilities with no Anthropic Messages
        # wire equivalent; they take effect under the SDK backend
        # (``ClaudeCodeSdkProvider._build_options``), which is the V1-aligned
        # default — so the HTTP fallback simply does not ship them.

        # ------------------------------------------------------------------
        # 2-H9: dynamic permission-rule replay — consume but do NOT ship
        # ------------------------------------------------------------------
        # ``permission_decisions`` is NOT an Anthropic Messages body key
        # (sending it 400s a strict endpoint).  We still DRAIN the queued
        # decision so it is applied exactly once and does not leak into a
        # later turn, but the HTTP fallback does not forward it on the wire.
        # The decision's user-perceived effect (resolving the gate, letting
        # the next turn proceed) is already realised at the application
        # layer: ``DecidePermissionUseCase`` transitions the aggregate and
        # ``forward_permission_decision`` resolves any awaiting future.  The
        # SDK backend (V1-aligned default) replays edits through its live
        # ``can_use_tool`` path.
        self._consume_permission_replay(session_id)

        return body

    @staticmethod
    def _resolve_thinking_body(
        cfg: CodingSessionConfig,
    ) -> dict[str, Any] | None:
        """Map ``cfg.thinking`` / ``cfg.effort`` to the wire thinking field.

        2-H7.  Precedence (V1 parity): an explicit structured
        ``thinking`` config wins; otherwise a coarse ``effort`` keyword
        maps to an extended-thinking budget.  Returns ``None`` when
        neither is set (the global default applies — no wire field).
        """
        if cfg.thinking is not None:
            # Already validated to carry a non-empty ``type`` in the VO.
            return dict(cfg.thinking)
        if cfg.effort is None:
            return None
        # Coarse keyword → extended-thinking budget.  Token budgets mirror
        # the legacy CLI's effort tiers (low→least, max→most).
        budget = {
            "low": 4096,
            "medium": 8192,
            "high": 16384,
            "max": 32768,
        }.get(cfg.effort)
        if budget is None:
            return None
        return {"type": "enabled", "budget_tokens": budget}

    # 2-H11: tool names that denote an Anthropic sub-agent ("Task")
    # dispatch.  When the mapper surfaces a TOOL_CALL / TOOL_RESULT for
    # one of these, :meth:`_subtask_frames` synthesises the matching
    # task_started / task_notification lifecycle frame (the native wire
    # never emits them — V1 ``session_manager.py:2044-2079`` produced
    # them from SDK Task messages).  Case-insensitive.
    _TASK_TOOL_NAMES = frozenset({"task", "agent", "subagent"})

    def _subtask_frames(
        self,
        *,
        kind: StreamFrameKind,
        payload: dict[str, Any],
    ) -> list[tuple[StreamFrameKind, dict[str, Any]]]:
        """Synthesise sub-task lifecycle frames around a Task tool (2-H11).

        * A ``TOOL_CALL`` whose ``tool`` is a sub-agent dispatch ("Task")
          → emit a :data:`TASK_STARTED` after the tool-call frame so the
          frontend opens a "🤖 sub-task" card (V1 ``TaskStartedMessage``).
        * The matching ``TOOL_RESULT`` → emit a :data:`TASK_NOTIFICATION`
          marking the sub-task done (V1 ``TaskNotificationMessage``).

        The task id is the tool call id (``call_id`` / ``id``) so the
        start and the notification correlate on the frontend.  Returns an
        empty list for every non-Task frame, so the historical
        single-agent flow is byte-for-byte unchanged.
        """
        if kind is StreamFrameKind.TOOL_CALL:
            tool = str(payload.get("tool") or "").strip().lower()
            if tool in self._TASK_TOOL_NAMES:
                task_id = str(
                    payload.get("call_id") or payload.get("id") or ""
                )
                args = payload.get("args")
                description = ""
                if isinstance(args, dict):
                    description = str(
                        args.get("description")
                        or args.get("prompt")
                        or args.get("task")
                        or ""
                    )
                return [
                    (
                        StreamFrameKind.TASK_STARTED,
                        {
                            "task_id": task_id,
                            "description": description,
                            "uuid": payload.get("id"),
                        },
                    )
                ]
        if kind is StreamFrameKind.TOOL_RESULT:
            tool = str(payload.get("tool") or "").strip().lower()
            if tool in self._TASK_TOOL_NAMES:
                task_id = str(
                    payload.get("call_id") or payload.get("id") or ""
                )
                return [
                    (
                        StreamFrameKind.TASK_NOTIFICATION,
                        {
                            "task_id": task_id,
                            "status": "completed",
                            "summary": str(payload.get("output") or ""),
                            "usage": None,
                        },
                    )
                ]
        return []

    # ------------------------------------------------------------------
    # M5: Anthropic token-usage accumulation
    # ------------------------------------------------------------------
    def _accumulate_anthropic_usage(self, usage: Any) -> None:
        """Fold one Anthropic ``usage`` object into the per-turn accumulator.

        Anthropic's ``usage`` shape (both ``message_start.message.usage`` and
        ``message_delta.usage``) carries ``input_tokens`` / ``output_tokens``
        plus optional ``cache_read_input_tokens`` /
        ``cache_creation_input_tokens``.  ``output_tokens`` arrives cumulative
        on ``message_delta`` so we MAX (not +=) it; the prompt-side counts
        appear once on ``message_start`` so a MAX is equally safe there.
        """
        if not isinstance(usage, dict):
            return

        def _as_int(value: Any) -> int:
            try:
                return int(value or 0)
            except (TypeError, ValueError):
                return 0

        acc = self._stream_usage
        # Prompt / output counters are reported cumulatively per turn — take
        # the running maximum so repeated message_delta frames don't double
        # count and a late refresh of input tokens still wins.
        for key, wire in (
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("cache_read_tokens", "cache_read_input_tokens"),
            ("cache_creation_tokens", "cache_creation_input_tokens"),
        ):
            seen = _as_int(usage.get(wire))
            if seen > acc.get(key, 0):
                acc[key] = seen

    def _snapshot_usage(self) -> dict[str, int]:
        """Return the END-frame ``usage`` payload from the accumulator.

        Cache tokens are added onto ``input_tokens`` so the context-window
        occupancy reflects the full prompt cost the model actually consumed
        (matching how the chat bar interprets the count).
        """
        acc = self._stream_usage
        input_total = (
            acc.get("input_tokens", 0)
            + acc.get("cache_read_tokens", 0)
            + acc.get("cache_creation_tokens", 0)
        )
        return {
            "input_tokens": input_total,
            "output_tokens": acc.get("output_tokens", 0),
        }

    def _map_stream_frame(
        self,
        *,
        envelope: dict[str, Any],
    ) -> tuple[StreamFrameKind, dict[str, Any]]:
        # Anthropic streams events with a ``type`` field
        # (``content_block_delta`` / ``message_stop`` / etc.).  The
        # mapping below is conservative — extensions are expected as
        # downstream PRs evolve real streaming.
        event_type = str(envelope.get("type") or envelope.get("kind") or "")

        # ------------------------------------------------------------------
        # Resume support: capture the upstream session/conversation id from
        # ``message_start`` events.  Anthropic emits this as the first SSE
        # event; the ``message.id`` field is the conversation continuity
        # token needed for subsequent ``resume: true`` requests.
        # ------------------------------------------------------------------
        if event_type == "message_start":
            msg = envelope.get("message") or {}
            upstream_id = msg.get("id") or envelope.get("session_id")
            if upstream_id and self._streaming_session_id is not None:
                record = self._handles.setdefault(
                    self._streaming_session_id, {}
                )
                record["claude_session_id"] = upstream_id
            # M5: the first SSE event carries the prompt-side token counts
            # (input + cache) on ``message.usage``.  Fold them in so the END
            # frame can report the full turn usage.
            self._accumulate_anthropic_usage(msg.get("usage"))
            # Surface as a text frame (metadata) — payload is informational.
            return StreamFrameKind.TEXT, {}

        if event_type == "content_block_delta":
            delta = dict(envelope.get("delta") or {})
            # Anthropic emits ``{"type": "text_delta", "text": "..."}``
            # for text and ``{"type": "input_json_delta", "partial_json":
            # "..."}`` for tool args.  We surface both as TEXT for now;
            # PR-104 (real conversation) will split tool partials out.
            if delta.get("type") == "input_json_delta":
                return StreamFrameKind.TEXT, {
                    "tool_partial_json": delta.get("partial_json") or "",
                }
            text = delta.get("text") or ""
            return StreamFrameKind.TEXT, {"text": text}
        if event_type in {"text", "message_delta"}:
            # M5: ``message_delta`` carries the cumulative ``usage.output_tokens``
            # (and sometimes refreshed input/cache counts) at the envelope top
            # level — accumulate before surfacing the text payload.
            if event_type == "message_delta":
                self._accumulate_anthropic_usage(envelope.get("usage"))
            payload = dict(
                envelope.get("delta") or envelope.get("payload") or {}
            )
            return StreamFrameKind.TEXT, payload
        if event_type == "content_block_start":
            block = dict(envelope.get("content_block") or {})
            if block.get("type") == "tool_use":
                return StreamFrameKind.TOOL_CALL, {
                    "id": block.get("id"),
                    "tool": block.get("name"),
                    "args": block.get("input") or {},
                }
            return StreamFrameKind.TEXT, {}
        if event_type in {"tool_use", "tool_call"}:
            return StreamFrameKind.TOOL_CALL, dict(envelope.get("payload") or {})
        if event_type in {"tool_result"}:
            return StreamFrameKind.TOOL_RESULT, dict(envelope.get("payload") or {})
        if event_type in {"message_stop", "end"}:
            # M5: emit the accumulated per-turn token usage on the terminal
            # END frame so ``query_context_usage`` (2-H2 chain) can write back
            # cumulative counters — same contract as the OpenCode adapter
            # (open_code.py:506-507).  ``input_tokens`` here is prompt+cache
            # (the context-window occupancy the chat bar reflects).
            return StreamFrameKind.END, {"usage": self._snapshot_usage()}
        if event_type in {"permission_request", "can_use_tool"}:
            # Harness-side approval gate (V1 ``can_use_tool``).  Anthropic's
            # native wire stream never emits this; concrete harness adapters
            # (or the scripted demo stream) synthesise it.  Surface as a
            # PERMISSION_REQUEST frame whose payload keys align with
            # ``PermissionRequestResponse`` (``tool_name`` / ``args``) so the
            # frontend can render the approval card and POST back the
            # ``request_id`` to ``/permissions/{request_id}/decide``.
            src = dict(envelope.get("payload") or envelope.get("permission") or {})
            return StreamFrameKind.PERMISSION_REQUEST, {
                "request_id": src.get("request_id") or src.get("id") or "",
                "tool_name": src.get("tool_name") or src.get("tool") or "",
                "args": dict(src.get("args") or src.get("input") or {}),
                "tool_use_id": src.get("tool_use_id"),
                "suggestions": list(src.get("suggestions") or []),
                "timeout_seconds": src.get("timeout_seconds"),
            }
        if event_type == "task_started":
            # Task/Agent sub-task lifecycle (V1 session_manager.py:2044-2051).
            # Anthropic's native HTTP SSE wire does not emit these; they are
            # surfaced when an upstream / harness injects Task Agent events
            # (same "harness-synthesised" pattern as permission_request above).
            src = dict(envelope.get("payload") or envelope)
            return StreamFrameKind.TASK_STARTED, {
                "task_id": src.get("task_id") or "",
                "description": src.get("description") or "",
                "uuid": src.get("uuid"),
            }
        if event_type == "task_progress":
            src = dict(envelope.get("payload") or envelope)
            return StreamFrameKind.TASK_PROGRESS, {
                "task_id": src.get("task_id") or "",
                "description": src.get("description") or "",
                "usage": dict(src.get("usage") or {}),
                "last_tool_name": src.get("last_tool_name"),
            }
        if event_type == "task_notification":
            src = dict(envelope.get("payload") or envelope)
            usage = src.get("usage")
            return StreamFrameKind.TASK_NOTIFICATION, {
                "task_id": src.get("task_id") or "",
                "status": src.get("status") or "",
                "summary": src.get("summary") or "",
                "usage": dict(usage) if isinstance(usage, dict) else None,
            }
        if event_type == "error":
            err = envelope.get("error") or envelope.get("payload") or {}
            return StreamFrameKind.ERROR, dict(err) if isinstance(err, dict) else {"message": str(err)}
        # Default: treat as text so unmapped events stay visible to the
        # client without breaking the SSE stream.
        return StreamFrameKind.TEXT, dict(envelope.get("payload") or {})
