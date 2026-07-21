# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""LLM-backed title generator adapters (PR-402 / S7.5 lane L4).

Migrates :func:`backend.title_generator.generate_title` (98 LOC) into
the chat bounded context as two :class:`TitleGeneratorPort`
implementations:

* :class:`HttpLLMTitleGenerator` — production adapter; one-shot
  ``POST /chat/completions`` against an OpenAI-compatible endpoint
  (``httpx.AsyncClient``, configurable timeout).  When the upstream is
  unconfigured (no ``base_url``) or the call fails, returns ``None`` so
  the use case falls back to :func:`fallback_title`.
* :class:`OfflineTitleGenerator` — silent adapter that always returns
  ``None``, used by tests and by deployments that never want to call
  the LLM.  Rather than wiring the HTTP adapter with an empty config
  (which produces noisy logs), the offline adapter is the canonical
  "do nothing" choice.

The adapters strictly comply with the port contract: NEVER raise.
Title generation is observability-grade per legacy
``backend/title_generator.py`` module docstring; an exception leaking
into the chat use case would block the actual chat reply.
"""

from __future__ import annotations

import asyncio
import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from qai.chat.application.ports import (
    ModelResolverPort,
    TitleGenerationRequest,
    TitleGeneratorPort,
)
from qai.platform.logging import get_logger

_log = get_logger(__name__)


def _is_loopback_endpoint(base_url: str | None) -> bool:
    """True when ``base_url`` points at the local machine (loopback host).

    Used to enforce "title generation never uses a LOCAL model" (user
    2026-06-17): a title request must never be sent to the on-device service,
    even via the statically-configured default endpoint. Mirrors
    ``model_resolver._is_loopback_host`` but kept self-contained here so the
    adapter has no cross-module dependency.
    """
    if not base_url:
        return False
    try:
        host = (urlparse(base_url).hostname or "").strip("[]")
    except (ValueError, AttributeError):
        return False
    if not host:
        return False
    if host.lower() == "local" + "host":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


# System instruction pinning the model to the "title generator" role.
#
# Root cause fixed (2026-07-12): the title request used to be a SINGLE
# ``role: user`` message carrying "generate a title ... User message: <text>".
# When the resolved cloud endpoint is an AGENT / tool-calling gateway (not a
# plain completion model), it treated the embedded user message as a TASK to
# execute and replied with an action narration (e.g. "我将安排一个子 Agent ...
# 让我启动它。<tool_calls> ...") or an unrelated chat turn. Truncated to
# ``max_tokens`` that garbage was stored verbatim as the conversation title.
# A dedicated ``system`` role that forbids task execution steers a
# well-behaved model back to emitting only a title; the ``_looks_like_title``
# guard in ``_clean_title`` is the backstop for models that still misbehave.
_TITLE_SYSTEM_PROMPT: str = (
    "You are a title generator. Your ONLY job is to output a short title "
    "(3-7 words) summarising the user's message, in the same language as the "
    "message. Do NOT answer, do NOT execute any task, do NOT call tools, do "
    "NOT explain. Output ONLY the title text: no quotes, no trailing "
    "punctuation, no newlines, no tool calls."
)

# Legacy prompt template — copied verbatim from
# ``backend/title_generator.py:TITLE_PROMPT`` so the LLM produces the
# same output distribution on the same upstream.
_TITLE_PROMPT_TEMPLATE: str = (
    "Based on the following user message, generate a concise title (3-7 words) "
    "in the same language as the message.\n"
    "Return ONLY the title text, no quotes, no punctuation at the end.\n"
    "\n"
    "User message:\n"
    "{user_message}"
)

# Cleaning constants.
_QUOTE_CHARS: tuple[str, ...] = (
    '"', "'", "\u201c", "\u201d", "\u2018", "\u2019",
)
_TITLE_HARD_CHAR_CAP: int = 50  # match legacy line 95
_TITLE_MIN_CHARS: int = 2       # match legacy line 100
_DEFAULT_MAX_TOKENS: int = 30   # match legacy line 60
_DEFAULT_TEMPERATURE: float = 0.3  # match legacy line 61

# --- "does this look like a title, not a chat/agent reply?" guard ---------
#
# Root cause (2026-07-12): a title request routed to an agent/tool-calling
# endpoint comes back as an ACTION narration or a full chat turn instead of a
# title, and the old ``_clean_title`` (quote-strip + 50-char truncate only)
# stored that garbage verbatim (observed DB titles: ``我将安排一个子 Agent 自动
# 完成整个流程。让我启动它。\n\n<tool_`` and an unrelated ``你发现旧形式的文件``).
# These markers reject such replies so the use case falls back to
# ``fallback_title`` (first user message truncated) — a faithful title source.

# Tool-call / markup leakage — a real title never contains these.
_NON_TITLE_MARKERS: tuple[str, ...] = (
    "<tool", "</tool", "[tool_call", "[tool_result",
    "```", "<function", "<|",
)

# Action / conversational openers (case-insensitive, matched at the START of
# the reply after stripping). A title summarises; it does not narrate an
# action or address the user. Kept bilingual (the endpoints seen misbehaving
# replied in zh/en).
_NON_TITLE_PREFIXES: tuple[str, ...] = (
    # Chinese action / reply openers
    "我将", "我会", "我来", "让我", "好的", "好，", "首先", "接下来",
    "当然", "明白", "收到", "这是", "以下是", "根据", "我可以", "我需要",
    "抱歉", "请稍", "正在",
    # English action / reply openers
    "i will", "i'll", "i am going", "i'm going", "let me", "sure,", "sure ",
    "okay", "ok,", "here is", "here's", "here are", "first,", "certainly",
    "i can ", "i need ", "based on", "to ",
)


def _looks_like_title(title: str) -> bool:
    """Heuristic: is *title* a genuine short title (not a chat/agent reply)?

    Returns ``False`` for replies that leaked tool-call markup, span multiple
    lines, or open with an action/conversational phrase — the shapes an
    agent/tool endpoint produces when it mistakes the title prompt for a task.
    A ``False`` result makes :func:`_clean_title` return ``None`` so the use
    case falls back to :func:`fallback_title`. Conservative by design: only
    clear non-title shapes are rejected, so ordinary short titles pass.
    """
    lowered = title.lower()
    # 1. Tool-call / code / markup leakage — never present in a real title.
    for marker in _NON_TITLE_MARKERS:
        if marker in lowered:
            return False
    # 2. Multi-line output — a title is a single line; a reply/narration wraps.
    if "\n" in title or "\r" in title:
        return False
    # 3. Action / conversational opener — a title summarises, it does not
    #    narrate ("我将...", "Let me ...", "Here is ...").
    for prefix in _NON_TITLE_PREFIXES:
        if lowered.startswith(prefix):
            return False
    return True


@dataclass(slots=True)
class HttpLLMTitleGenerator(TitleGeneratorPort):
    """Production :class:`TitleGeneratorPort` that calls an
    OpenAI-compatible ``/chat/completions`` endpoint.

    All five tunable knobs default to the legacy production values; an
    integration test or a non-OpenAI deployment may override them.

    When ``base_url`` is empty / ``None`` the adapter immediately
    returns ``None`` without attempting any HTTP call — useful for the
    "no chat settings configured" case where production wires the
    adapter but the deployment has no upstream.

    Provider-aware endpoint resolution
    ----------------------------------
    Like :class:`~qai.chat.adapters.prompt_enhancer.HttpPromptEnhancer`,
    the adapter accepts an optional :class:`ModelResolverPort` (the same
    provider-aware resolver the chat hot path uses).  When wired, the
    request is routed to the resolved model's provider ``base_url`` /
    ``api_key`` — so title summarisation works against a configured cloud
    model (e.g. cloud LLM service) even when the static ``llm_base_url`` is
    empty (V1 ``main.py:6830-6849`` resolved the upstream from the cloud
    model registry rather than a single hard-wired endpoint).  The static
    ``base_url`` / ``api_key`` / ``model_name`` remain the fallback default
    (used when no resolver is wired or resolution yields no endpoint).
    """

    base_url: str | None = None
    api_key: str | None = None
    model_name: str = "qai-default"
    max_tokens: int = _DEFAULT_MAX_TOKENS
    temperature: float = _DEFAULT_TEMPERATURE
    model_resolver: ModelResolverPort | None = None
    # Verify TLS certificates for the outbound title-summarisation HTTPS call.
    # Threaded from the unified ``Settings.ssl_verify`` switch (edition-derived
    # default); False relaxes TLS for internal / enterprise MITM gateways.
    ssl_verify: bool = True
    # Live Settings.ssl_verify provider (apps/api._global_proxy
    # .build_ssl_verify_provider). When set it is read at client-build time so a
    # runtime SSL toggle hot-applies to every new title-generation client; the
    # ``ssl_verify`` bool above is the back-compat fallback.
    ssl_verify_provider: Callable[[], bool] | None = None

    async def _resolve_endpoint(
        self,
        request: TitleGenerationRequest,
    ) -> tuple[str | None, str | None, str]:
        """Pick (base_url, api_key, model) for this title request.

        Prefers the provider-aware resolver, routing the title request to
        the SAME cloud model the conversation is using (``request.model_id``)
        — V1 parity: title summarisation ran against a cloud model, never the
        local on-device service.  Guards (user 2026-06-17 — "local models
        never use a model to generate the title; never silently use a cloud
        model for a local chat"):

        * **Refuse local** — if the resolved endpoint is ``is_local`` OR the
          finally-chosen ``base_url`` is a loopback host, we return no
          base_url so ``generate`` skips the HTTP call and the use case falls
          back to :func:`fallback_title` (first-message truncation).  A local
          chat is therefore titled by truncation, never by a model — and the
          local NPU is never occupied by a title request.
        * **Real model id** — we only use the resolver's endpoint when it
          carries a concrete ``base_url``; otherwise we fall back to the
          statically-configured default endpoint + the caller's model id (or
          the static default model name).
        """
        if self.model_resolver is not None and request.model_id:
            try:
                resolved = await self.model_resolver.resolve(request.model_id)
                _log.info(
                    "chat.title_generator.resolved",
                    requested_model_id=request.model_id,
                    is_local=resolved.is_local,
                    has_base_url=bool(resolved.base_url),
                    base_url=resolved.base_url,
                    has_api_key=bool(resolved.api_key),
                    api_model_id=resolved.api_model_id or resolved.model_id,
                )
                if resolved.is_local:
                    # Local model — refuse and let the use case fall back to
                    # the heuristic title (user 2026-06-17).
                    _log.info(
                        "chat.title_generator.skip_local",
                        requested_model_id=request.model_id,
                    )
                    return (None, None, request.model_id)
                if resolved.base_url:
                    return (
                        resolved.base_url,
                        resolved.api_key,
                        resolved.api_model_id
                        or resolved.model_id
                        or request.model_id,
                    )
            except Exception as exc:  # noqa: BLE001 — degrade to default
                _log.warning("chat.title_generator_resolve_failed", error=str(exc))
        # Fallback: static default endpoint + caller-supplied model override.
        # But never send a title request to a LOCAL (loopback) default endpoint
        # — a local deployment titles by truncation, not by the on-device model.
        if _is_loopback_endpoint(self.base_url):
            _log.info(
                "chat.title_generator.skip_loopback_default",
                requested_model_id=request.model_id,
                static_base_url=self.base_url,
            )
            return (None, None, request.model_id or self.model_name)
        _log.info(
            "chat.title_generator.use_static_default",
            requested_model_id=request.model_id,
            static_base_url=self.base_url,
            has_resolver=self.model_resolver is not None,
        )
        return (self.base_url, self.api_key, request.model_id or self.model_name)

    async def generate(
        self,
        request: TitleGenerationRequest,
    ) -> str | None:
        base_url, api_key, model_name = await self._resolve_endpoint(request)
        if not base_url:
            _log.info(
                "chat.title_generator.no_base_url",
                requested_model_id=request.model_id,
            )
            return None
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers: dict[str, str] = {
            "Content-Type": "application/json",
        }
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        prompt = _TITLE_PROMPT_TEMPLATE.format(user_message=request.user_message)
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": _TITLE_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }
        _log.info(
            "chat.title_generator.request",
            url=url,
            model=model_name,
            has_api_key=bool(api_key),
            timeout_seconds=request.timeout_seconds,
            temperature=self.temperature,
            max_tokens=self.max_tokens,
        )

        try:
            raw = await asyncio.wait_for(
                self._post_once(url=url, headers=headers, payload=payload,
                                timeout=request.timeout_seconds),
                timeout=request.timeout_seconds,
            )
        except asyncio.TimeoutError:
            _log.warning(
                "chat.title_generator_timeout",
                timeout_seconds=request.timeout_seconds,
                url=url,
                model=model_name,
            )
            return None
        except httpx.HTTPStatusError as exc:
            _log.warning(
                "chat.title_generator_http_error",
                status_code=exc.response.status_code,
                url=url,
                model=model_name,
                body=exc.response.text[:500],
            )
            return None
        except Exception as exc:  # noqa: BLE001 — port must never raise
            _log.warning(
                "chat.title_generator_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                url=url,
                model=model_name,
            )
            return None

        cleaned = _clean_title(raw) if raw is not None else None
        _log.info(
            "chat.title_generator.response",
            raw_present=raw is not None,
            raw_preview=(raw or "")[:80],
            cleaned_title=cleaned,
        )
        return cleaned

    async def _post_once(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, object],
        timeout: float,
    ) -> str | None:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            # Live read so a runtime Settings.ssl_verify toggle hot-applies to
            # each new client; falls back to the frozen bool when no provider.
            verify=(
                self.ssl_verify_provider()
                if self.ssl_verify_provider is not None
                else self.ssl_verify
            ),
        ) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return None
            content = (
                (choices[0].get("message") or {}).get("content") or ""
            )
            if not isinstance(content, str):
                return None
            return content.strip() or None


@dataclass(slots=True)
class OfflineTitleGenerator(TitleGeneratorPort):
    """No-op :class:`TitleGeneratorPort` — always returns ``None``.

    Used by the chat DI when no LLM upstream is configured, so the
    chat use case skips the round-trip and falls straight to the
    fallback heuristic.
    """

    async def generate(
        self,
        request: TitleGenerationRequest,
    ) -> str | None:
        return None


def _clean_title(raw: str) -> str | None:
    """Strip surrounding quotes, reject non-title replies, truncate, min-check.

    Public function (not just a method) so PR-401c-style adapters that
    plug a different transport can re-use the cleaning rules without
    duplicating them.  Returns ``None`` when:

    * the reply does not look like a title (:func:`_looks_like_title` —
      tool-call markup / multi-line / action opener → fall back to
      :func:`fallback_title`), or
    * the cleaned title is shorter than :data:`_TITLE_MIN_CHARS`
      (matching legacy behaviour).

    The non-title guard runs on the de-quoted but PRE-truncation text so a
    50-char cut cannot hide a trailing ``<tool_`` marker or a second line.
    """
    title = raw.strip()
    for q in _QUOTE_CHARS:
        if title.startswith(q):
            title = title[1:]
        if title.endswith(q):
            title = title[:-1]
    title = title.strip()
    # Reject agent/chat replies mis-returned as titles BEFORE truncation, so
    # the use case falls back to the first-user-message heuristic.
    if not _looks_like_title(title):
        return None
    if len(title) > _TITLE_HARD_CHAR_CAP:
        title = title[:_TITLE_HARD_CHAR_CAP]
    if len(title) < _TITLE_MIN_CHARS:
        return None
    return title


__all__ = [
    "HttpLLMTitleGenerator",
    "OfflineTitleGenerator",
]
