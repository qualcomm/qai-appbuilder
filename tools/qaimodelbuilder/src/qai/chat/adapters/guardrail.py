# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-process guardrail controller (PR-401b / S7.5 lane L4).

Migrates :class:`backend.tool_guardrails.GuardrailController` (201 LOC,
2026-05-30 snapshot) into the chat bounded context's adapter layer.

The legacy controller is purely in-process state plus three threshold
constants; this implementation preserves the exact thresholds and
matching semantics so that PR-401c can use the new adapter as a drop-in
replacement for the legacy controller in the chat agentic loop.

Three abnormal patterns are detected, in order, on every
:meth:`InMemoryGuardrailController.check`:

1. **Exact-argument repeats** — the same ``(tool_name, args_hash)`` has
   appeared at the tail of the history N consecutive times.
   * N >= ``EXACT_REPEAT_BLOCK`` (5) → :data:`GuardrailDecision.BLOCK`
   * N >= ``EXACT_REPEAT_WARN`` (2) → :data:`GuardrailDecision.WARN`
2. **Consecutive failures** — the same ``tool_name`` has failed N
   consecutive times at the tail.
   * N >= ``FAILURE_HALT`` (8)  → :data:`GuardrailDecision.BLOCK`
   * N >= ``FAILURE_WARN`` (3)  → :data:`GuardrailDecision.WARN`
3. **Idempotent same-result** — for tools in :data:`IDEMPOTENT_TOOLS`
   (``read`` / ``glob`` / ``grep``), the same ``(args_hash,
   result_hash)`` has appeared N consecutive times at the tail.
   * N >= ``IDEMPOTENT_SAME_RESULT`` (2) → :data:`GuardrailDecision.WARN`

Otherwise the call is :data:`GuardrailDecision.ALLOW`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from qai.chat.application.ports import (
    GuardrailDecision,
    GuardrailPort,
    GuardrailVerdict,
)
from qai.platform.logging import get_logger

_log = get_logger(__name__)


# Thresholds (byte-for-byte ports of legacy constants).
EXACT_REPEAT_WARN: int = 2
EXACT_REPEAT_BLOCK: int = 5
FAILURE_WARN: int = 3
FAILURE_HALT: int = 8
IDEMPOTENT_SAME_RESULT: int = 2

IDEMPOTENT_TOOLS: frozenset[str] = frozenset({"read", "glob", "grep"})


# ---------------------------------------------------------------------------
# Internal value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class _ToolCallSignature:
    """``(tool_name, args_hash)`` pair used to identify repeat calls.

    ``args_hash`` is the first 16 hex chars of the SHA-256 of the
    arguments serialised with ``json.dumps(sort_keys=True,
    ensure_ascii=False)``.  Non-JSON-serialisable arguments fall back
    to ``repr(args)`` so the guardrail still produces *some* signature
    rather than crashing — this matches the legacy behaviour of
    ``json.dumps`` raising ``TypeError`` only on truly exotic inputs
    (datetime, custom objects), which the chat tool layer rarely
    encounters.
    """

    tool_name: str
    args_hash: str

    @classmethod
    def from_call(cls, tool_name: str, args: Any) -> _ToolCallSignature:
        try:
            raw = json.dumps(args, sort_keys=True, ensure_ascii=False)
        except TypeError:
            raw = repr(args)
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
        return cls(tool_name=tool_name, args_hash=digest)


@dataclass(slots=True)
class _CallRecord:
    """One row of the guardrail's call history."""

    signature: _ToolCallSignature
    success: bool
    result_hash: str


# ---------------------------------------------------------------------------
# Public adapter
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class InMemoryGuardrailController(GuardrailPort):
    """Default :class:`GuardrailPort` implementation.

    State is a single in-process list of :class:`_CallRecord` ordered
    chronologically.  The controller is not thread-safe; the chat use
    case is expected to instantiate one per turn (or per session) and
    invoke it sequentially.
    """

    _history: list[_CallRecord] = field(default_factory=list)

    # ------------------------------------------------------------------
    # Public API (GuardrailPort)
    # ------------------------------------------------------------------
    def check(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> GuardrailVerdict:
        if not self._history:
            return GuardrailVerdict(decision=GuardrailDecision.ALLOW, reason="")

        signature = _ToolCallSignature.from_call(tool_name, arguments)

        # 1. Exact-argument repeats.
        repeat_count = self._count_trailing_exact_repeats(signature)
        if repeat_count >= EXACT_REPEAT_BLOCK:
            reason = (
                f"tool {tool_name!r} called with identical arguments "
                f"{repeat_count} times in a row "
                f"(>= block threshold {EXACT_REPEAT_BLOCK})"
            )
            _log.warning("guardrail.block_exact_repeat", tool=tool_name, repeats=repeat_count)
            return GuardrailVerdict(decision=GuardrailDecision.BLOCK, reason=reason)
        if repeat_count >= EXACT_REPEAT_WARN:
            reason = (
                f"tool {tool_name!r} called with identical arguments "
                f"{repeat_count} times in a row "
                f"(>= warn threshold {EXACT_REPEAT_WARN})"
            )
            _log.warning("guardrail.warn_exact_repeat", tool=tool_name, repeats=repeat_count)
            return GuardrailVerdict(decision=GuardrailDecision.WARN, reason=reason)

        # 2. Consecutive failures.
        failure_count = self._count_trailing_failures(tool_name)
        if failure_count >= FAILURE_HALT:
            reason = (
                f"tool {tool_name!r} has failed {failure_count} times in a row "
                f"(>= halt threshold {FAILURE_HALT})"
            )
            _log.warning("guardrail.block_failure_streak", tool=tool_name, failures=failure_count)
            return GuardrailVerdict(decision=GuardrailDecision.BLOCK, reason=reason)
        if failure_count >= FAILURE_WARN:
            reason = (
                f"tool {tool_name!r} has failed {failure_count} times in a row "
                f"(>= warn threshold {FAILURE_WARN})"
            )
            _log.warning("guardrail.warn_failure_streak", tool=tool_name, failures=failure_count)
            return GuardrailVerdict(decision=GuardrailDecision.WARN, reason=reason)

        # 3. Idempotent tools returning identical results.
        if tool_name in IDEMPOTENT_TOOLS:
            same = self._count_trailing_idempotent_same_result(signature)
            if same >= IDEMPOTENT_SAME_RESULT:
                reason = (
                    f"idempotent tool {tool_name!r} returned the same result "
                    f"{same} times in a row; further calls add no information"
                )
                _log.warning(
                    "guardrail.warn_idempotent_same_result",
                    tool=tool_name,
                    repeats=same,
                )
                return GuardrailVerdict(decision=GuardrailDecision.WARN, reason=reason)

        return GuardrailVerdict(decision=GuardrailDecision.ALLOW, reason="")

    def observe(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        result: Any,
        success: bool = True,
    ) -> None:
        signature = _ToolCallSignature.from_call(tool_name, arguments)
        try:
            result_raw = json.dumps(result, sort_keys=True, ensure_ascii=False)
        except TypeError:
            result_raw = repr(result)
        result_hash = hashlib.sha256(result_raw.encode("utf-8")).hexdigest()[:16]
        self._history.append(
            _CallRecord(
                signature=signature,
                success=success,
                result_hash=result_hash,
            ),
        )

    def reset(self) -> None:
        self._history.clear()

    # ------------------------------------------------------------------
    # Test / introspection helpers
    # ------------------------------------------------------------------
    @property
    def history_length(self) -> int:
        """Number of recorded calls (test-only)."""
        return len(self._history)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _count_trailing_exact_repeats(self, signature: _ToolCallSignature) -> int:
        count = 0
        for record in reversed(self._history):
            if record.signature == signature:
                count += 1
            else:
                break
        return count

    def _count_trailing_failures(self, tool_name: str) -> int:
        count = 0
        for record in reversed(self._history):
            if record.signature.tool_name == tool_name and not record.success:
                count += 1
            else:
                break
        return count

    def _count_trailing_idempotent_same_result(
        self,
        signature: _ToolCallSignature,
    ) -> int:
        results: list[str] = []
        for record in reversed(self._history):
            if record.signature == signature:
                results.append(record.result_hash)
            else:
                break
        if not results:
            return 0
        first = results[0]
        count = 0
        for r in results:
            if r == first:
                count += 1
            else:
                break
        return count


__all__ = [
    "InMemoryGuardrailController",
    "EXACT_REPEAT_WARN",
    "EXACT_REPEAT_BLOCK",
    "FAILURE_WARN",
    "FAILURE_HALT",
    "IDEMPOTENT_SAME_RESULT",
    "IDEMPOTENT_TOOLS",
]
