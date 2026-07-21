# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""model_profiles — Model family parameter profiles (domain layer).

Pure business rule: for a given model ID, determine hardcoded parameter
constraints (temperature, top_p, max_tokens) based on model family matching.

11 model families with temperature/top_p locking:
  1. claude_opus        — Anthropic Claude Opus
  2. claude_sonnet      — Anthropic Claude Sonnet
  3. claude_haiku       — Anthropic Claude Haiku
  4. gpt_5              — OpenAI GPT-5 / o-series (temp/top_p locked to 1.0)
  5. gpt_4o             — OpenAI GPT-4o / GPT-4.1
  6. gpt_4_legacy       — OpenAI GPT-4 (older)
  7. doubao             — ByteDance Doubao / Volcano Ark
  8. gemini             — Google Gemini / VertexAI
  9. deepseek_reasoner  — DeepSeek R1 reasoner (temp/top_p locked to 1.0)
  10. deepseek           — DeepSeek general
  11. qwen_reasoner      — Qwen QwQ/QvQ reasoner (temp/top_p locked to 1.0)
  (+) qwen              — Qwen general
  (+) unknown           — fallback for unrecognized models

Design:
  - Domain layer: PURE — no I/O, no framework deps, no process-global
    mutable state, no threading primitives, no logging side effects
    (pure stdlib ``re`` + dataclasses only).
  - Does NOT import backend.* / features.* / apps.* / interfaces.*.
  - Four-layer override priority for ``max_tokens`` resolution:
      1. Explicit params from model config JSON
      2. Runtime-learned limit (from API 400 errors) — passed IN as a
         plain ``runtime_max_tokens_limit`` argument; the *mutable*
         learned-limit cache now lives behind
         :class:`qai.chat.application.ports.RuntimeLimitStorePort`
         (adapter: ``InMemoryRuntimeLimitStore``), keeping this domain
         module side-effect free (D2 dealign fix).
      3. Family profile (pattern matching)
      4. System hardcoded fallback (most conservative)

The runtime-limit *learning rule* itself is the pure function
:func:`extract_api_limit_from_error` (parse a numeric ceiling out of an
upstream 400 message); the application layer calls it and records the
result through the store port — the domain neither stores nor logs.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# Truncation constants (inlined — no external dependency)
# ─────────────────────────────────────────────────────────────────────────────

# Hard cap: single tool result never exceeds this (≈12.5K-15K tokens)
TOOL_RESULT_HARD_CAP_CHARS: int = 50_000

# Floor: even at high context usage, tool result never shrinks below this
TOOL_RESULT_MIN_CHARS: int = 10_000


# ─────────────────────────────────────────────────────────────────────────────
# Model Family Definitions (11 families + unknown fallback)
# ─────────────────────────────────────────────────────────────────────────────
# Each rule is matched in order; first match wins.
# Fields:
#   name: str                       — family identifier
#   pattern: re.Pattern             — regex to match model_id
#   max_tokens_max: int | None      — API hard limit on output tokens
#   max_tokens_default: int | None  — recommended default output size
#   tool_result_max_chars: int      — base chars for single tool result
#   supports_thinking: bool         — whether model supports reasoning tokens
#   temperature_fixed: float | None — if set, temp is LOCKED to this value
#   top_p_fixed: float | None       — if set, top_p is LOCKED to this value

_MODEL_FAMILIES: list[dict[str, Any]] = [
    # ── 1. Anthropic Claude Opus ─────────────────────────────────────────────
    {
        "name": "claude_opus",
        "pattern": re.compile(r"claude.*opus", re.IGNORECASE),
        "max_tokens_max": 32768,
        "max_tokens_default": 16384,
        "tool_result_max_chars": 50_000,
        "supports_thinking": True,
        "temperature_fixed": None,
        "top_p_fixed": None,
    },
    # ── 2. Anthropic Claude Sonnet ───────────────────────────────────────────
    {
        "name": "claude_sonnet",
        "pattern": re.compile(r"claude.*sonnet", re.IGNORECASE),
        "max_tokens_max": 64000,
        "max_tokens_default": 16384,
        "tool_result_max_chars": 50_000,
        "supports_thinking": True,
        "temperature_fixed": None,
        "top_p_fixed": None,
    },
    # ── 3. Anthropic Claude Haiku ────────────────────────────────────────────
    {
        "name": "claude_haiku",
        "pattern": re.compile(r"claude.*haiku", re.IGNORECASE),
        "max_tokens_max": 8192,
        "max_tokens_default": 4096,
        "tool_result_max_chars": 25_000,
        "supports_thinking": False,
        "temperature_fixed": None,
        "top_p_fixed": None,
    },
    # ── 4. OpenAI GPT-5 / o-series (reasoning models) ────────────────────────
    # temperature MUST be 1.0 (API enforced); top_p also locked.
    # Regex: gpt-5, gpt5, gpt-5.5, o1, o3-mini, o4-preview etc.
    {
        "name": "gpt_5",
        "pattern": re.compile(r"gpt-?5(?:\.\d+)?|(?:^|[^a-zA-Z])o[1-9]\b", re.IGNORECASE),
        "max_tokens_max": 32768,
        "max_tokens_default": 8192,
        "tool_result_max_chars": 25_000,
        "supports_thinking": True,
        "temperature_fixed": 1.0,
        "top_p_fixed": 1.0,
    },
    # ── 5. OpenAI GPT-4o / GPT-4.1 ──────────────────────────────────────────
    {
        "name": "gpt_4o",
        "pattern": re.compile(r"gpt-?4o|gpt-?4\.1", re.IGNORECASE),
        "max_tokens_max": 16384,
        "max_tokens_default": 8192,
        "tool_result_max_chars": 25_000,
        "supports_thinking": False,
        "temperature_fixed": None,
        "top_p_fixed": None,
    },
    # ── 6. OpenAI GPT-4 (legacy) ─────────────────────────────────────────────
    {
        "name": "gpt_4_legacy",
        "pattern": re.compile(r"gpt-?4(?!o|\.|/|-?[5-9])", re.IGNORECASE),
        "max_tokens_max": 4096,
        "max_tokens_default": 2048,
        "tool_result_max_chars": 15_000,
        "supports_thinking": False,
        "temperature_fixed": None,
        "top_p_fixed": None,
    },
    # ── 7. ByteDance Doubao / Volcano Ark ────────────────────────────────────
    {
        "name": "doubao",
        "pattern": re.compile(r"doubao|volc|ark", re.IGNORECASE),
        "max_tokens_max": 131072,
        "max_tokens_default": 16384,
        "tool_result_max_chars": 25_000,
        "supports_thinking": True,
        "temperature_fixed": None,
        "top_p_fixed": None,
    },
    # ── 8. Google Gemini / VertexAI ──────────────────────────────────────────
    {
        "name": "gemini",
        "pattern": re.compile(r"gemini|vertexai", re.IGNORECASE),
        "max_tokens_max": 65536,
        "max_tokens_default": 8192,
        "tool_result_max_chars": 50_000,
        "supports_thinking": True,
        "temperature_fixed": None,
        "top_p_fixed": None,
    },
    # ── 9. DeepSeek Reasoner (R1) — temp/top_p locked ────────────────────────
    {
        "name": "deepseek_reasoner",
        "pattern": re.compile(r"deepseek.*reason|deepseek.*r1", re.IGNORECASE),
        "max_tokens_max": 8192,
        "max_tokens_default": 4096,
        "tool_result_max_chars": 25_000,
        "supports_thinking": True,
        "temperature_fixed": 1.0,
        "top_p_fixed": 1.0,
    },
    # ── 10. DeepSeek (general) ───────────────────────────────────────────────
    {
        "name": "deepseek",
        "pattern": re.compile(r"deepseek", re.IGNORECASE),
        "max_tokens_max": 8192,
        "max_tokens_default": 4096,
        "tool_result_max_chars": 25_000,
        "supports_thinking": True,
        "temperature_fixed": None,
        "top_p_fixed": None,
    },
    # ── 11. Qwen Reasoner (QwQ/QvQ) — temp/top_p locked ─────────────────────
    {
        "name": "qwen_reasoner",
        "pattern": re.compile(r"qwq|qvq|qwen.*think", re.IGNORECASE),
        "max_tokens_max": 8192,
        "max_tokens_default": 4096,
        "tool_result_max_chars": 25_000,
        "supports_thinking": True,
        "temperature_fixed": 1.0,
        "top_p_fixed": 1.0,
    },
    # ── Qwen (general) ───────────────────────────────────────────────────────
    {
        "name": "qwen",
        "pattern": re.compile(r"qwen", re.IGNORECASE),
        "max_tokens_max": 8192,
        "max_tokens_default": 4096,
        "tool_result_max_chars": 25_000,
        "supports_thinking": False,
        "temperature_fixed": None,
        "top_p_fixed": None,
    },
    # ── Fallback: unknown model ──────────────────────────────────────────────
    {
        "name": "unknown",
        "pattern": re.compile(r".*"),
        "max_tokens_max": None,
        "max_tokens_default": None,
        "tool_result_max_chars": 25_000,
        "supports_thinking": False,
        "temperature_fixed": None,
        "top_p_fixed": None,
    },
]


# ─────────────────────────────────────────────────────────────────────────────
# Runtime-learned limit extraction (PURE — no caching, no I/O)
# ─────────────────────────────────────────────────────────────────────────────


def extract_api_limit_from_error(error_message: str) -> int | None:
    """Parse a numeric ``max_tokens`` ceiling out of an upstream 400 message.

    Pure business rule (the *learning* part of the legacy
    ``record_api_limit_error``): given an API error string, return the
    enforced limit ``N`` when it can be recognised, else ``None``.  The
    *caching* of the learned value is no longer done here — the
    application layer records it via
    :class:`qai.chat.application.ports.RuntimeLimitStorePort` so this
    domain function stays side-effect free.

    Supported error formats:
      - "expected a value <= 131072, but got 400000"  (Volcano/Ark)
      - "max_tokens cannot exceed 16384"              (OpenAI)
      - "must be less than or equal to 8192"          (Anthropic)

    Returns the positive limit on success, or ``None`` when nothing was
    extracted.
    """
    if not error_message:
        return None

    msg = error_message.lower()
    patterns = [
        r"expected a value\s*<=?\s*(\d+)",
        r"must be\s*<=?\s*(\d+)",
        r"cannot exceed\s+(\d+)",
        r"less than or equal to\s+(\d+)",
        r"max[_-]?tokens.*?(\d{3,})",
    ]

    for pattern in patterns:
        match = re.search(pattern, msg)
        if match:
            try:
                extracted = int(match.group(1))
            except (ValueError, IndexError):
                continue
            if extracted > 0:
                return extracted
    return None



# ─────────────────────────────────────────────────────────────────────────────
# ModelProfile dataclass
# ─────────────────────────────────────────────────────────────────────────────


@dataclass
class ModelProfile:
    """Resolved runtime profile for a single model (all override layers merged).

    Attributes with ``_fixed`` suffix indicate API-enforced locks:
    when set, the parameter MUST use that value regardless of user config.
    """

    model_id: str
    family_name: str
    context_length: int

    # max_tokens constraints
    max_tokens_max: int | None
    max_tokens_default: int | None

    # tool result truncation base
    tool_result_max_chars_base: int

    # temperature / top_p locks (None = no lock, user value respected)
    temperature_fixed: float | None = None
    top_p_fixed: float | None = None

    # metadata
    supports_thinking: bool = False
    explicit_overrides: dict[str, Any] = field(default_factory=dict)

    # ── Adaptive tool result truncation ──────────────────────────────────────

    def compute_tool_result_max(self) -> int:
        """Per-result tool-output char cap for this profile.

        Returns the family base budget clamped to
        ``[TOOL_RESULT_MIN_CHARS, TOOL_RESULT_HARD_CAP_CHARS]``, honouring an
        explicit ``tool_result_max_chars`` override when present.

        退化 #11 (subtask 4): the legacy context-usage pressure-shrink ladder
        (60% / 30% / 15% as the conversation filled up, keyed on
        ``current_used_tokens``) has been removed. The V2 callers never had a
        real ``current_used_tokens`` to feed it (it was hard-coded ``0``), so
        the ladder could never fire — it was dead code. The robust replacement
        for "one tool result must not blow the budget" is the oversized-output
        STORE (``tool_result_store`` / ``data/tool_results/``): the full body
        is persisted and the model ``read``s it back, instead of being shrunk
        in-prompt by a ratio that was never computed.
        """
        # Explicit override takes priority
        if "tool_result_max_chars" in self.explicit_overrides:
            override_val = int(self.explicit_overrides["tool_result_max_chars"])
            return min(TOOL_RESULT_HARD_CAP_CHARS, max(TOOL_RESULT_MIN_CHARS, override_val))

        base = min(self.tool_result_max_chars_base, TOOL_RESULT_HARD_CAP_CHARS)
        return min(TOOL_RESULT_HARD_CAP_CHARS, max(TOOL_RESULT_MIN_CHARS, base))

    # ── max_tokens resolution ────────────────────────────────────────────────

    def resolve_max_tokens(
        self,
        user_value: int | None = None,
        explicit_default: int | None = None,
        runtime_max_tokens_limit: int | None = None,
    ) -> int | None:
        """Compute final max_tokens to send to API.

        Priority: user_value > explicit_default > family default > None.
        All values clamped to min(runtime_learned, config_max, family_max,
        context).  ``runtime_max_tokens_limit`` is the runtime-learned
        ceiling (previously read from the in-domain global cache; now
        passed in by the application layer from
        :class:`~qai.chat.application.ports.RuntimeLimitStorePort` so this
        method stays pure).  Returns None if no value should be sent.
        """
        caps: list[int] = []
        if runtime_max_tokens_limit is not None and runtime_max_tokens_limit > 0:
            caps.append(runtime_max_tokens_limit)
        if "max" in self.explicit_overrides:
            try:
                caps.append(int(self.explicit_overrides["max"]))
            except (TypeError, ValueError):
                pass
        if self.max_tokens_max is not None:
            caps.append(self.max_tokens_max)
        if self.context_length > 0:
            caps.append(self.context_length)

        cap = min(caps) if caps else None

        def _clamp(v: int) -> int:
            return min(v, cap) if cap is not None else v

        if user_value is not None and user_value > 0:
            return _clamp(user_value)

        if explicit_default is not None and explicit_default > 0:
            return _clamp(explicit_default)

        if self.max_tokens_default is not None and self.max_tokens_default > 0:
            return _clamp(self.max_tokens_default)

        return None

    # ── temperature resolution ───────────────────────────────────────────────

    def resolve_temperature(
        self,
        user_value: float | None = None,
        explicit_supported: bool | None = None,
        explicit_default: float | None = None,
    ) -> float | None:
        """Compute final temperature for the API call.

        If family has temperature_fixed set, that value is ALWAYS used
        (reasoning models like GPT-5/o-series/DeepSeek-R1 require it).

        Returns None to omit the parameter (let API use its default).
        """
        if explicit_supported is False:
            return None

        # Family lock overrides everything (API will 400 otherwise).
        # The previous in-domain ``logger.warning`` when a user value is
        # ignored is dropped (D2: domain stays side-effect free); the
        # lock decision is fully expressed by the return value.
        if self.temperature_fixed is not None:
            return self.temperature_fixed

        if explicit_default is not None:
            try:
                return float(explicit_default)
            except (TypeError, ValueError):
                pass

        if user_value is not None:
            try:
                return float(user_value)
            except (TypeError, ValueError):
                pass

        return 0.7

    # ── top_p resolution ─────────────────────────────────────────────────────

    def resolve_top_p(
        self,
        user_value: float | None = None,
        explicit_supported: bool | None = None,
        explicit_default: float | None = None,
    ) -> float | None:
        """Compute final top_p for the API call.

        If family has top_p_fixed set, that value is ALWAYS used.
        Returns None to omit the parameter (let API use its default).
        """
        if explicit_supported is False:
            return None

        if self.top_p_fixed is not None:
            # Family lock wins; user value silently ignored (no domain
            # logging side effect — D2).
            return self.top_p_fixed

        if explicit_default is not None:
            try:
                return float(explicit_default)
            except (TypeError, ValueError):
                pass

        if user_value is not None:
            try:
                return float(user_value)
            except (TypeError, ValueError):
                pass

        return None


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def _match_family(model_id: str) -> dict[str, Any]:
    """Match model_id to its family profile (first match wins)."""
    for family in _MODEL_FAMILIES:
        if family["pattern"].search(model_id):
            return family
    return _MODEL_FAMILIES[-1]


def get_model_profile(
    model_id: str,
    context_length: int = 0,
    explicit_params: dict[str, Any] | None = None,
) -> ModelProfile:
    """Get the resolved runtime profile for a model.

    Args:
        model_id: Model identifier (e.g. "doubao-seed-2.0-code").
        context_length: Context window size from model catalog.
        explicit_params: Override params from model config JSON,
            e.g. {"supported": true, "max": 131072, "default": 16384}.

    Returns:
        ModelProfile with all override layers merged.
    """
    family = _match_family(model_id or "")
    explicit_params = explicit_params or {}

    overrides: dict[str, Any] = {}
    for key in ("max", "default", "tool_result_max_chars"):
        if key in explicit_params:
            overrides[key] = explicit_params[key]

    return ModelProfile(
        model_id=model_id,
        family_name=family["name"],
        context_length=context_length or 0,
        max_tokens_max=family["max_tokens_max"],
        max_tokens_default=family["max_tokens_default"],
        tool_result_max_chars_base=family["tool_result_max_chars"],
        temperature_fixed=family.get("temperature_fixed"),
        top_p_fixed=family.get("top_p_fixed"),
        supports_thinking=family.get("supports_thinking", False),
        explicit_overrides=overrides,
    )


def list_families() -> list[str]:
    """Return all family names in match priority order."""
    return [f["name"] for f in _MODEL_FAMILIES]


# ─────────────────────────────────────────────────────────────────────────────
# Model → context-window mapping (V1 backend/token_counter.py:107-181)
#
# Pure business rule: map a model id (or fuzzy prefix) to its maximum
# context-window token budget, with a 200K fallback for unknown models.
# Lives in the domain layer (no I/O, no framework deps) so both the
# application use cases (CompactChatUseCase) and the adapters
# (context_size_estimator) can resolve a real budget from the model id
# alone instead of relying on a hard-coded 8192 default.
#
# Moved here from ``qai.chat.adapters.context_size_estimator`` (PR-091)
# so the application layer can call it without violating the
# ``layered-chat`` import-linter contract (application ⇍ adapters).  The
# adapter keeps re-exporting :func:`get_context_limit` for backwards
# compatibility (its public API is unchanged).
# ─────────────────────────────────────────────────────────────────────────────

_CONTEXT_LIMITS: dict[str, int] = {
    # OpenAI-compatible / cloud frontier models
    "gpt-4o":           128_000,
    "gpt-4o-mini":      128_000,
    "gpt-4.1":        1_000_000,
    "gpt-5":          1_000_000,
    "claude-sonnet-4":  200_000,
    "claude-opus-4":    200_000,
    "claude-haiku-4":   200_000,
    "gemini-2.5":     1_000_000,
    "gemini-2-5":     1_000_000,
    # Open-weights families
    "qwen3":             32_768,
    "qwen2.5":          128_000,
    "llama-3.1":        131_072,
    "llama-3":            8_192,
    "deepseek-v3":       65_536,
    "glm-4":            128_000,
    # Fallback bucket
    "__unknown__":      200_000,
}

# Substring search order: most-specific first.  Used by
# :func:`_resolve_context_model` when no exact match exists.
_CONTEXT_PREFIX_BUCKETS: tuple[tuple[str, str], ...] = (
    ("gpt-4o-mini",      "gpt-4o-mini"),
    ("gpt-4o",           "gpt-4o"),
    ("gpt-4.1",          "gpt-4.1"),
    ("gpt-5",            "gpt-5"),
    ("claude-sonnet-4",  "claude-sonnet-4"),
    ("sonnet-4",         "claude-sonnet-4"),
    ("claude-opus-4",    "claude-opus-4"),
    ("opus-4",           "claude-opus-4"),
    ("claude-haiku-4",   "claude-haiku-4"),
    ("haiku-4",          "claude-haiku-4"),
    ("gemini-2.5",       "gemini-2.5"),
    ("gemini-2-5",       "gemini-2-5"),
    ("qwen2.5",          "qwen2.5"),
    ("qwen3",            "qwen3"),
    ("llama-3.1",        "llama-3.1"),
    ("llama-3",          "llama-3"),
    ("deepseek-v3",      "deepseek-v3"),
    ("glm-4",            "glm-4"),
)


def _resolve_context_model(model_id: str) -> str:
    """Map any model id to a canonical context-window bucket key.

    Strips ``provider::`` prefixes, lowercases, then:

    1. checks for an exact key match in :data:`_CONTEXT_LIMITS`;
    2. otherwise walks :data:`_CONTEXT_PREFIX_BUCKETS` (most-specific
       first) and returns the first match;
    3. falls back to ``"__unknown__"``.

    Reference: legacy ``backend/token_counter.py:_resolve_model``
    (lines 107-170) — adapted to the PR-091 model set.
    """
    s = (model_id or "").strip().lower()
    if "::" in s:
        s = s.split("::")[-1]
    if not s:
        return "__unknown__"
    if s in _CONTEXT_LIMITS:
        return s
    for needle, bucket in _CONTEXT_PREFIX_BUCKETS:
        if needle in s:
            return bucket
    return "__unknown__"


def get_context_limit(model_id: str) -> int:
    """Return the context window size (in tokens) for *model_id*.

    Always returns a positive integer; unknown models map to the
    ``"__unknown__"`` bucket (200K — large enough for most workloads
    yet small enough that the compressor still triggers when the
    history balloons).
    """
    return _CONTEXT_LIMITS.get(_resolve_context_model(model_id), 200_000)
