# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: report context-window usage for a coding session.

Backs the legacy
``GET /api/cc/sessions/{id}/context_usage`` (CC-only legacy route)
and ``GET /api/oc/sessions/{id}/context_size`` routes.

Implementation note (U-010 / 2-H2)
----------------------------------
These use cases report REAL cumulative token usage read directly off the
:class:`CodingSession` aggregate.  The aggregate carries the same
cumulative counters V1 exposed via ``ClaudeCodeSession.to_dict``
(``total_input_tokens`` / ``total_output_tokens`` / ``last_input_tokens``
/ ``context_window`` / ``total_tool_calls``).

Truth-source chain (State-Truth-First — the numbers are authoritative
provider counts, never estimated here):

1. The CC / OC provider emits per-turn ``usage`` frames while streaming.
2. :class:`StreamCodingSessionUseCase._record_frame_usage`
   (``stream_coding_session.py``) parses those frames and calls
   :meth:`CodingSession.record_token_usage` (``domain.entities``), which
   accumulates ``total_input_tokens`` / ``total_output_tokens``, REPLACES
   ``last_input_tokens`` with the most-recent turn's input count, and
   learns ``context_window`` when a positive value is reported.
3. The aggregate is persisted by the repository, so a reloaded session
   still carries the cumulative counters.
4. These use cases read those persisted fields; they perform NO local
   estimation and stay decoupled from the specific provider counter
   source (the aggregate is the single truth-source).

An EMPTY aggregate (a freshly-created session that has not streamed a
turn yet) legitimately returns ``0`` for every counter — that is the
INITIAL state, not a permanent stub.  ``context_limit`` / ``max_tokens``
prefer the learned :attr:`CodingSession.context_window`; when it has not
been learned yet they fall back to the optional
:attr:`CodingSessionConfig.model`-derived limit or the global default
(``200_000``).  ``usage_pct`` / ``percentage`` is the most-recent-turn
input occupancy (``last_input_tokens / context_limit``), matching V1's
``usage_pct`` semantics.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.ai_coding.application.ports import (
    CodingSessionRepositoryPort,
)
from qai.ai_coding.domain import CodingSessionId

__all__ = [
    "ContextSizeResult",
    "ContextUsageQuery",
    "ContextUsageResult",
    "GetContextSizeUseCase",
    "GetContextUsageUseCase",
]


#: Default context-window cap used when no model-specific lookup is
#: configured.  Matches the legacy hard-coded fallback in
#: ``backend/ai_coding/api_routes.py`` / ``opencode_api_routes.py``
#: (200,000 tokens — the Claude 3.5 Sonnet ceiling).
_DEFAULT_CONTEXT_LIMIT: int = 200_000


# ---------------------------------------------------------------------------
# DTOs
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextUsageQuery:
    """Input for both context use cases.

    The same query DTO is reused for ``context_usage`` (CC-flavoured,
    richer payload) and ``context_size`` (OC-flavoured, smaller
    payload) so a future caller that wants both can drive a single
    use case.
    """

    session_id: CodingSessionId


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextUsageResult:
    """Return shape of :class:`GetContextUsageUseCase`.

    Mirrors the legacy ``GET /api/cc/sessions/{id}/context_usage``
    wire shape ``{ok, totalTokens, maxTokens, percentage}``.  The
    field names follow the legacy CC SDK contract; the route layer
    re-shapes if needed.
    """

    ok: bool
    total_tokens: int
    max_tokens: int
    percentage: float


@dataclass(frozen=True, slots=True, kw_only=True)
class ContextSizeResult:
    """Return shape of :class:`GetContextSizeUseCase`.

    Mirrors the legacy ``GET /api/oc/sessions/{id}/context_size``
    wire shape with the eight-key payload that the WebUI consumes.
    Token counters carry the aggregate's REAL cumulative usage (U-010 /
    2-H2 — see module docstring); they are ``0`` only for a session that
    has not streamed a turn yet (initial state, not a permanent stub).
    """

    last_input_tokens: int
    total_input_tokens: int
    total_output_tokens: int
    total_tool_calls: int
    turn_count: int
    context_limit: int
    usage_pct: float
    model: str


# ---------------------------------------------------------------------------
# Use cases
# ---------------------------------------------------------------------------


class GetContextUsageUseCase:
    """Application service for ``GET /api/cc/sessions/{id}/context_usage``."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        default_limit: int = _DEFAULT_CONTEXT_LIMIT,
    ) -> None:
        self._repository = repository
        self._default_limit = default_limit

    async def execute(self, query: ContextUsageQuery) -> ContextUsageResult:
        # Validate session exists (raises CodingSessionNotFoundError → 422).
        session = await self._repository.get(query.session_id)
        # U-010 / 2-H2: the aggregate now tracks cumulative token usage
        # (written back by :class:`StreamCodingSessionUseCase` from the
        # provider ``usage`` frame).  ``total_tokens`` mirrors V1
        # ``ClaudeCodeSession.to_dict`` (``total_input_tokens +
        # total_output_tokens``); ``max_tokens`` uses the learned
        # context-window when known (V1 ``context_window``), else the
        # configured default.  ``percentage`` is the most-recent-turn
        # input occupancy against the limit (V1 ``usage_pct`` semantics:
        # ``last_input_tokens / context_limit``), the best approximation
        # of "how full is the context right now".
        total_tokens = session.total_input_tokens + session.total_output_tokens
        max_tokens = (
            session.context_window
            if session.context_window > 0
            else self._default_limit
        )
        last_input = session.last_input_tokens
        percentage = (
            round(last_input / max_tokens, 4)
            if max_tokens > 0 and last_input > 0
            else 0.0
        )
        return ContextUsageResult(
            ok=True,
            total_tokens=total_tokens,
            max_tokens=max_tokens,
            percentage=percentage,
        )


class GetContextSizeUseCase:
    """Application service for ``GET /api/oc/sessions/{id}/context_size``."""

    def __init__(
        self,
        *,
        repository: CodingSessionRepositoryPort,
        default_limit: int = _DEFAULT_CONTEXT_LIMIT,
    ) -> None:
        self._repository = repository
        self._default_limit = default_limit

    async def execute(self, query: ContextUsageQuery) -> ContextSizeResult:
        session = await self._repository.get(query.session_id)
        # Derive a model name from the optional config.  The aggregate
        # exposes :attr:`config.model` when PR-107 wired it; otherwise
        # the legacy WebUI will show the empty string.
        config = getattr(session, "config", None)
        model = ""
        if config is not None:
            model = str(getattr(config, "model", "") or "")
        # U-010 / 2-H2: turn count = number of user messages on the
        # aggregate (V1 ``turn_count`` parity for the WebUI progress
        # indicator).  Token counters are read off the aggregate's
        # cumulative usage fields (written by the streaming use case
        # from the provider ``usage`` frame), mirroring V1
        # ``get_cc_session_context_size`` (api_routes.py:2342-2353):
        # ``context_limit`` prefers the learned ``context_window``,
        # ``usage_pct`` = ``last_input_tokens / context_limit``.
        turn_count = len(session.messages)
        context_limit = (
            session.context_window
            if session.context_window > 0
            else self._default_limit
        )
        last_input = session.last_input_tokens
        usage_pct = (
            round(last_input / context_limit, 4)
            if context_limit > 0 and last_input > 0
            else 0.0
        )
        return ContextSizeResult(
            last_input_tokens=last_input,
            total_input_tokens=session.total_input_tokens,
            total_output_tokens=session.total_output_tokens,
            total_tool_calls=session.total_tool_calls,
            turn_count=turn_count,
            context_limit=context_limit,
            usage_pct=usage_pct,
            model=model,
        )
