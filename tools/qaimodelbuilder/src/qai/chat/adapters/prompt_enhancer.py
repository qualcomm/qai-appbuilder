# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""LLM-backed prompt enhancer adapter (PR-403 / S7.5 lane L4).

Migrates ``backend/main.py:_direct_chat_completion`` + ``enhance_prompt``
into the chat bounded context behind :class:`PromptEnhancerPort`.

The adapter calls an OpenAI-compatible ``/chat/completions`` endpoint
with a single user message containing the enhance instruction; this
deliberately bypasses the standard chat system-prompt assembly so
small models receive the enhance request unobscured (legacy comment
at line 7290-7294 of backend/main.py).

A companion :class:`OfflinePromptEnhancer` is provided for deployments
without an LLM upstream — it returns ``None`` so the use case raises a
``chat.prompt_enhance_empty_response`` validation error and the route
layer maps that to a 502.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from qai.chat.application.ports import (
    ModelResolverPort,
    PromptEnhanceRequest,
    PromptEnhancerPort,
)
from qai.platform.logging import get_logger

_log = get_logger(__name__)


# Legacy meta-prompt (preserved verbatim from
# ``backend/main.py:_ENHANCE_INSTRUCTION``).  The LLM receives this
# instruction prepended to the raw user text.
_ENHANCE_INSTRUCTION_PREFIX: str = (
    "Rewrite the following user prompt to be clearer, more specific, "
    "and easier for an LLM to follow.  Preserve the intent and the "
    "language of the original prompt.  Return ONLY the rewritten "
    "prompt — no preamble, no explanations, no quotes.\n\n"
    "Original prompt:\n"
)

_DEFAULT_TEMPERATURE: float = 0.4
_DEFAULT_MAX_TOKENS: int = 2000


@dataclass(slots=True)
class HttpPromptEnhancer(PromptEnhancerPort):
    """Production :class:`PromptEnhancerPort` over OpenAI-compatible HTTP.

    V1 parity (`useChat.js` / `usePromptEnhance.js`): prompt-enhance runs
    against the **selected model's** upstream, not a single hard-wired
    local endpoint.  V1 passed ``selected_model_id`` + ``provider`` to
    ``/api/prompt/enhance`` and the backend dispatched to that provider.

    To match that here, the adapter accepts an optional
    :class:`ModelResolverPort` (the same provider-aware resolver the chat
    hot path uses).  When present, the request's ``model_id`` is resolved
    to its provider's ``base_url`` / ``api_key`` (e.g. a cloud model like
    ``provider::model-name`` routes to the cloud LLM service) so enhance
    works even when the local on-device service is offline.  The
    ``base_url`` / ``api_key`` / ``model_name`` fields remain as the
    fallback default (used when no resolver is wired or resolution yields
    no endpoint).
    """

    base_url: str | None = None
    api_key: str | None = None
    model_name: str = "qai-default"
    max_tokens: int = _DEFAULT_MAX_TOKENS
    temperature: float = _DEFAULT_TEMPERATURE
    model_resolver: ModelResolverPort | None = None
    # Unified SSL switch (Settings.ssl_verify). Parity with the sibling
    # ``HttpLLMTitleGenerator`` which threads the same setting: prompt-enhance
    # routes to the SAME provider endpoints (via ``model_resolver``, e.g.
    # cloud LLM service), so it must honour ``ssl_verify`` too. Kept defaulting to
    # ``False`` to preserve the prior hard-coded ``verify=False`` behaviour for
    # callers/tests that construct the adapter without wiring the setting.
    ssl_verify: bool = False
    # Live Settings.ssl_verify provider (apps/api._global_proxy
    # .build_ssl_verify_provider). When set it is read at client-build time so a
    # runtime SSL toggle hot-applies to every new enhance client; the
    # ``ssl_verify`` bool above is the back-compat fallback.
    ssl_verify_provider: Callable[[], bool] | None = None

    async def _resolve_endpoint(
        self,
        request: PromptEnhanceRequest,
    ) -> tuple[str | None, str | None, str]:
        """Pick (base_url, api_key, model) for this enhance request.

        Prefers the provider-aware resolver (so the selected cloud model's
        endpoint is used); falls back to the statically-configured default
        endpoint when no resolver is wired or resolution fails / yields no
        base_url.
        """
        if self.model_resolver is not None:
            try:
                resolved = await self.model_resolver.resolve(
                    request.model_id or None,
                )
                if resolved.base_url:
                    return (
                        resolved.base_url,
                        resolved.api_key,
                        resolved.model_id or (request.model_id or self.model_name),
                    )
            except Exception as exc:  # noqa: BLE001 — degrade to default endpoint
                _log.warning("chat.prompt_enhance_resolve_failed", error=str(exc))
        # Fallback: static default endpoint + caller-supplied model override.
        return (
            self.base_url,
            self.api_key,
            request.model_id or self.model_name,
        )

    async def enhance(
        self,
        request: PromptEnhanceRequest,
    ) -> str | None:
        base_url, api_key, model = await self._resolve_endpoint(request)
        if not base_url:
            return None
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": _ENHANCE_INSTRUCTION_PREFIX + request.text,
                },
            ],
            "stream": False,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

        try:
            raw = await asyncio.wait_for(
                self._post_once(url=url, headers=headers, payload=payload,
                                timeout=request.timeout_seconds),
                timeout=request.timeout_seconds,
            )
        except asyncio.TimeoutError:
            _log.warning(
                "chat.prompt_enhance_timeout",
                timeout_seconds=request.timeout_seconds,
            )
            return None
        except Exception as exc:  # noqa: BLE001 — port must never raise
            _log.warning("chat.prompt_enhance_failed", error=str(exc))
            return None
        return _strip_enhance_artifacts(raw) if raw else None

    async def _post_once(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict,
        timeout: float,
    ) -> str | None:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout), verify=(self.ssl_verify_provider() if self.ssl_verify_provider is not None else self.ssl_verify)) as client:  # live read so runtime SSL toggle hot-applies; frozen bool fallback
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            choices = data.get("choices") or []
            if not choices:
                return None
            content = (choices[0].get("message") or {}).get("content") or ""
            if not isinstance(content, str):
                return None
            return content.strip() or None


@dataclass(slots=True)
class OfflinePromptEnhancer(PromptEnhancerPort):
    """No-op :class:`PromptEnhancerPort` — always returns ``None``."""

    async def enhance(
        self,
        request: PromptEnhanceRequest,
    ) -> str | None:
        return None


def _strip_enhance_artifacts(text: str) -> str:
    """Remove common LLM artefacts from enhanced output.

    Mirrors the legacy ``_strip_enhance_artifacts`` helper:

    * surrounding ``"`` / ``'`` / smart quotes
    * leading ``"Rewritten prompt:"`` / ``"Enhanced:"`` style preambles
    * trailing whitespace
    """
    cleaned = text.strip()
    # Strip surrounding quotes (ASCII + smart).
    for q in ('"', "'", "\u201c", "\u201d", "\u2018", "\u2019"):
        if cleaned.startswith(q):
            cleaned = cleaned[1:]
        if cleaned.endswith(q):
            cleaned = cleaned[:-1]
    # Strip a few common preamble lines.
    lower = cleaned.lower()
    for prefix in (
        "rewritten prompt:",
        "enhanced prompt:",
        "enhanced:",
        "improved prompt:",
        "improved:",
    ):
        if lower.startswith(prefix):
            cleaned = cleaned[len(prefix):].lstrip(" \n\r\t:")
            break
    return cleaned.strip()


__all__ = [
    "HttpPromptEnhancer",
    "OfflinePromptEnhancer",
]
