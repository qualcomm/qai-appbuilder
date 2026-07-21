# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Keyword-based error classifier for chat streaming (PR-401a / S7.5 lane L4).

Migrates :func:`backend.chat_handler._is_prompt_too_long_error` and
:func:`backend.chat_handler._is_throttling_error` into the chat bounded
context.  These are **fallback** classifiers used when a structured
provider error code is unavailable; the structured path (HTTP status +
response body parsed via a provider-specific schema) is the responsibility
of the higher-level :func:`classify_api_error` added for P2-b (V1
``api_error_classifier.py`` parity).

Why two layers?  Real-world cloud LLM proxies sometimes mask provider
errors as raw HTTP 200 with the error embedded in the streamed body, so
the chat use case must be able to recognise prompt-too-long /
throttling categories from free-text alone.  The keyword sets here are
a literal copy of the legacy production list (with full provenance
comments inlined below) so the behavioural envelope of the legacy
``_stream_cloud`` retry path can be reproduced 1:1 in PR-401c.
"""

from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime


# ---------------------------------------------------------------------------
# Retry-After header parsing (rate-limit aware backoff)
# ---------------------------------------------------------------------------
# ``Retry-After`` (RFC 7231 §7.1.3) may take one of two forms:
#
#   * ``delay-seconds`` — a non-negative decimal integer number of seconds,
#     e.g. ``Retry-After: 120``.
#   * ``HTTP-date`` — an RFC 7231 (IMF-fixdate / obsolete RFC 850 / asctime)
#     absolute timestamp, e.g. ``Retry-After: Wed, 21 Oct 2015 07:28:00 GMT``.
#     The advised delay is ``max(0, http_date - now)``.
#
# We clamp the parsed delay to ``RETRY_AFTER_MAX_SECONDS`` so a hostile or
# buggy upstream cannot make the client sleep for hours. Absent / malformed /
# already-expired values return ``None`` so the caller falls back to its
# existing exponential backoff schedule (byte-for-byte unchanged behaviour).
RETRY_AFTER_MAX_SECONDS: float = 300.0
"""Hard ceiling (5 minutes) on any server-advised Retry-After delay.

A larger advised delay is clamped down to this value; the intent is a
sane bound on how long a single throttling retry will wait, not to
honour arbitrarily large upstream directives."""


def parse_retry_after(
    value: str | None,
    *,
    now: datetime | None = None,
    max_seconds: float = RETRY_AFTER_MAX_SECONDS,
) -> float | None:
    """Parse a ``Retry-After`` header value into a non-negative delay (s).

    Accepts either the ``delay-seconds`` integer form or an RFC 7231
    ``HTTP-date``. Returns the advised delay in seconds, clamped to
    ``[0, max_seconds]``.

    Returns ``None`` (→ caller keeps its existing exponential backoff) when:

    * *value* is ``None`` / empty / not a string;
    * the value is neither a valid integer nor a parseable HTTP-date;
    * an HTTP-date lies in the past (already expired → 0 wait is meaningless
      as a *server-advised* directive, so we defer to normal backoff).

    ``now`` is injectable for deterministic tests (defaults to the current
    UTC time). A negative integer ``delay-seconds`` is treated as malformed
    and returns ``None``.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None

    # Form 1: integer delay-seconds.
    if text.isdigit():
        # ``isdigit`` already excludes signs / decimals / whitespace, so any
        # all-digit token is a non-negative integer.
        seconds = float(int(text))
        if seconds < 0.0:  # defensive; isdigit precludes this
            return None
        return min(seconds, max_seconds)

    # Form 2: RFC 7231 HTTP-date.
    try:
        parsed = parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if parsed is None:
        return None
    # ``parsedate_to_datetime`` returns a naive datetime for legacy formats
    # lacking a timezone; RFC 7231 dates are always GMT, so treat naive as UTC.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)

    delta = (parsed - current).total_seconds()
    if delta <= 0.0:
        # Already expired → not a meaningful positive server-advised delay.
        return None
    return min(delta, max_seconds)


# ---------------------------------------------------------------------------
# Prompt-too-long
# ---------------------------------------------------------------------------
# Inclusion keywords: a message that contains ANY of these is classified
# as a prompt-too-long error.  Sources (provider-by-provider):
#
#   - "prompt is too long"          generic
#   - "context_length_exceeded"     OpenAI
#   - "maximum context length"      OpenAI
#   - "tokens > "                   AWS Bedrock ("Input tokens > 200000 ..." form)
#   - "input is too long"           several proxies
#   - "context window"              several
#   - "token limit"                 several
#   - "too many tokens"             Azure / variant
#   - "request too large"           Azure
#   - "reduce the length"           generic guidance
#   - "maximum allowed"             generic guidance
#   - "exceed max message tokens"   Doubao / Volces
#   - "exceed max input tokens"     Doubao variant
#
# Deliberately NOT included: "exceeds the maximum" — too broad; would
# false-positive on "request body exceeds the maximum size" and on
# "max_tokens: X exceeds the maximum allowed value", neither of which is
# a prompt overflow.
_PROMPT_TOO_LONG_INCLUSIONS: tuple[str, ...] = (
    "prompt is too long",
    "context_length_exceeded",
    "maximum context length",
    "tokens > ",
    "input is too long",
    "context window",
    "token limit",
    "too many tokens",
    "request too large",
    "reduce the length",
    "maximum allowed",
    "exceed max message tokens",
    "exceed max input tokens",
)

# Exclusion keyword: a message that hits the inclusion set is still
# rejected if it matches an exclusion phrase that indicates a parameter
# value bound rather than a prompt overflow.  Currently only "above
# maximum value" is filtered (e.g. "max_tokens above maximum value
# 32000"); since "above maximum value" and the inclusion phrases are
# mutually exclusive in practice, this is implemented as an early
# return rather than a post-filter to keep the function semantics easy
# to reason about.
_PROMPT_TOO_LONG_EXCLUSIONS: tuple[str, ...] = (
    "above maximum value",
)


def is_prompt_too_long_error(message: str) -> bool:
    """Return ``True`` iff *message* describes a prompt / context overflow.

    Behaviour notes (preserved from legacy):

    * Match is case-insensitive (the message is lowercased once).
    * The exclusion ``"above maximum value"`` is checked **before**
      inclusion so that ``"max_tokens above maximum value"`` is NOT
      classified as prompt-too-long even when other clauses in the
      message contain words like "maximum".
    * History bug: an earlier version excluded any message containing
      ``"invalidparameter"``, which silently broke prompt-too-long
      auto-retry on Doubao (where the error code is also called
      ``InvalidParameter``).  The current implementation never does a
      blanket invalid-parameter exclusion; only the precise
      ``"above maximum value"`` phrase is excluded.
    """
    if not isinstance(message, str) or not message:
        return False
    msg_lower = message.lower()
    if any(excl in msg_lower for excl in _PROMPT_TOO_LONG_EXCLUSIONS):
        return False
    return any(kw in msg_lower for kw in _PROMPT_TOO_LONG_INCLUSIONS)


# ---------------------------------------------------------------------------
# Throttling / rate-limit / temporary overload
# ---------------------------------------------------------------------------
# Inclusion keywords: any of these classifies the error as throttling
# (a transient condition that should trigger backoff + retry).
#
#   - "throttlingexception"   AWS Bedrock
#   - "too many tokens"       AWS Bedrock variant
#   - "rate_limit_exceeded"   OpenAI
#   - "too many requests"     generic 429 prose
#   - "quota exceeded"        Google / Azure
#   - "rate limit"            generic
#   - "serveroverloaded"      Doubao / Volces
#   - "toomanyrequests"       Doubao variant
#   - "service unavailable"   generic 503
#   - "overloaded"            Anthropic
#   - "server overload"       phrasing variant
#   - " 429"                  bare-status fallback (e.g. "API error 429")
#   - "(429)"                 bracketed status fallback
_THROTTLING_INCLUSIONS: tuple[str, ...] = (
    "throttlingexception",
    "too many tokens",
    "rate_limit_exceeded",
    "too many requests",
    "quota exceeded",
    "rate limit",
    "serveroverloaded",
    "toomanyrequests",
    "service unavailable",
    "overloaded",
    "server overload",
    " 429",
    "(429)",
)


def is_throttling_error(message: str) -> bool:
    """Return ``True`` iff *message* describes a throttling / overload error.

    Match is case-insensitive.  No exclusions.
    """
    if not isinstance(message, str) or not message:
        return False
    msg_lower = message.lower()
    return any(kw in msg_lower for kw in _THROTTLING_INCLUSIONS)


# ---------------------------------------------------------------------------
# P2-b: Structured status-code-based error classification
# (V1 api_error_classifier.py parity — core logic from 199 LOC)
# ---------------------------------------------------------------------------


def classify_api_error(
    *,
    status_code: int | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    provider: str | None = None,
    retry_after: str | None = None,
) -> str:
    """Classify an API error into a structured category.

    V1 parity (api_error_classifier.py): structured classification using
    HTTP status code + provider-specific error codes + keyword fallback.

    Returns one of:
    * ``"throttling"``       — rate limit / overload, should retry with backoff
    * ``"auth_failed"``      — authentication or permission error
    * ``"prompt_too_long"``  — context length exceeded
    * ``"content_filtered"`` — content policy violation
    * ``"model_unavailable"``— model not found or not deployed
    * ``"server_error"``     — upstream 5xx
    * ``"unknown"``          — unclassifiable

    Callers (``llm_stream.py`` HTTP error branches) should invoke this
    for secondary classification when the keyword-based classifiers are
    not conclusive.
    """
    message = (error_message or "").lower()
    code = (error_code or "").lower()

    # ── Status-code-based primary classification ──────────────────────
    if status_code is not None:
        # 429: always throttling (with or without Retry-After)
        if status_code == 429:
            return "throttling"

        # 401 / 403: authentication / permission errors
        if status_code in (401, 403):
            return "auth_failed"

        # 404: model not found / not deployed
        if status_code == 404:
            return "model_unavailable"

        # 503: service unavailable / overloaded
        if status_code == 503:
            # Check for explicit overload signals
            if "overloaded" in message or "serveroverloaded" in code:
                return "throttling"
            return "throttling"

        # 500 / 502 / 504: server error
        if status_code >= 500:
            return "server_error"

        # 400: inspect the error message for specific sub-categories
        if status_code == 400:
            # context_length_exceeded / prompt too long
            if is_prompt_too_long_error(error_message or ""):
                return "prompt_too_long"
            # content filter
            if "content_filter" in message or "content_policy" in message:
                return "content_filtered"

    # ── Error-code-based secondary classification ─────────────────────
    if code:
        if code in ("rate_limit_exceeded", "throttlingexception", "toomanyrequests"):
            return "throttling"
        if code in ("context_length_exceeded", "prompt_too_long"):
            return "prompt_too_long"
        if code in ("invalid_api_key", "authentication_error", "permission_denied"):
            return "auth_failed"
        if code in ("content_filter", "content_policy_violation", "responsible_ai"):
            return "content_filtered"
        if code in ("model_not_found", "model_not_available", "decommissioned"):
            return "model_unavailable"

    # ── Retry-After header presence → throttling ──────────────────────
    if retry_after:
        return "throttling"

    # ── Keyword fallback (supplements keyword-only classifiers) ────────
    if is_prompt_too_long_error(error_message or ""):
        return "prompt_too_long"
    if is_throttling_error(error_message or ""):
        return "throttling"

    return "unknown"


__all__ = [
    "classify_api_error",
    "is_prompt_too_long_error",
    "is_throttling_error",
    "parse_retry_after",
    "RETRY_AFTER_MAX_SECONDS",
]
