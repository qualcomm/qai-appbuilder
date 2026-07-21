# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Runtime detection of a gateway's Anthropic prompt-cache support (方案B).

Cache support is a GATEWAY-level property. We learn it at runtime from the
first response usage of each base_url: if the gateway echoes back any cache
field (prompt_tokens_details.cache_read_tokens / cache_write_tokens /
cache_read_input_tokens / cached_tokens) it supports caching → we KEEP tool-
output aging OFF (so the cached prefix stays byte-clean and history replays as
cache_read). If it does NOT echo cache fields it has no cache → we turn aging ON
(real per-round byte savings). Unknown (first round, before any usage) defaults
to "assume supports cache" → aging OFF (方案B initial policy).

Process-lifetime, in-memory, never persisted — gateway capability is stable
short-term; a restart simply re-learns on the first round (one round of aging-
off), which is cheap. Shared by BOTH the main agent (StreamChatUseCase) and the
sub-agent (AgentToolHandler) via a single DI-injected instance.
"""
from __future__ import annotations


class ProviderCacheCapabilityRegistry:
    __slots__ = ("_supports", "_route")

    def __init__(self) -> None:
        # base_url -> True(supports) / False(no_cache). Missing = unknown.
        self._supports: dict[str, bool] = {}
        # model_hint -> base_url (learned in _select_target so the use-case
        # layer, which only has model_hint, can resolve the gateway key).
        self._route: dict[str, str] = {}

    def note_route(self, model_hint: str | None, base_url: str | None) -> None:
        if model_hint and base_url:
            self._route[model_hint] = base_url

    def _key(self, model_hint: str | None) -> str | None:
        if not model_hint:
            return None
        # Prefer the learned base_url; fall back to model_hint itself (only
        # costs an extra first-round learn if the route isn't mapped yet).
        return self._route.get(model_hint, model_hint)

    def mark(self, model_hint: str | None, supports: bool) -> None:
        key = self._key(model_hint)
        if key is not None:
            self._supports[key] = supports

    def aging_enabled(self, model_hint: str | None) -> bool:
        """True → run aging (gateway has NO cache). False → skip aging
        (supports cache, OR unknown first round = 方案B default assume-supports)."""
        key = self._key(model_hint)
        if key is None:
            return False  # unknown → 方案B: assume supports → aging OFF
        supports = self._supports.get(key)
        if supports is None:
            return False  # unknown → aging OFF
        return not supports  # no_cache → aging ON; supports → aging OFF


__all__ = ["ProviderCacheCapabilityRegistry"]
