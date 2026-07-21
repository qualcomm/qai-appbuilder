# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Value objects for chat content and counters.

Includes:

* :class:`MessageRole` -- enum of allowed roles in a chat message.
* :class:`MessageContent` -- the textual payload (with optional media
  references) of a message.
* :class:`TokenCount` -- non-negative integer wrapped for type safety.
* :class:`ContextSize` -- a measurement of how many tokens a
  conversation currently occupies, plus the budget allowed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import NamedTuple

from qai.chat.domain.errors import (
    InvalidContextSizeError,
    InvalidMessageContentError,
)
from qai.platform.io_validator import (
    ValidationError as _IoValidationError,
)
from qai.platform.io_validator import (
    assert_max_length,
    assert_non_empty,
)

# Generous upper bound -- prevents pathological inputs from slipping
# through but still allows multi-megabyte prompts when the caller really
# wants them.  Higher-level config can tighten this further.
_MAX_CONTENT_LENGTH: int = 1_000_000


class MessageRole(str, Enum):
    """The role of a message author in a conversation."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"


@dataclass(frozen=True, slots=True)
class MessageContent:
    """Textual payload (with optional media references) of a message.

    Constraints:

    * ``text`` must be non-empty after stripping (otherwise raise
      :class:`InvalidMessageContentError`).
    * ``text`` must be <= ~1 MB to guard against runaway prompts.
    * ``media_refs`` is an optional tuple of opaque references to
      attached media (image / audio / file blobs); the chat domain
      treats them as opaque ids handed to it by upper layers.
    """

    text: str
    media_refs: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        try:
            assert_non_empty(self.text, name="MessageContent.text")
            assert_max_length(
                self.text,
                max_length=_MAX_CONTENT_LENGTH,
                name="MessageContent.text",
            )
        except _IoValidationError as exc:
            raise InvalidMessageContentError(str(exc)) from exc
        for ref in self.media_refs:
            if not isinstance(ref, str) or not ref:
                raise InvalidMessageContentError(
                    "media_refs entries must be non-empty strings",
                )

    @property
    def length(self) -> int:
        """Return the character length of the underlying text."""
        return len(self.text)

    def has_media(self) -> bool:
        return bool(self.media_refs)


@dataclass(frozen=True, slots=True)
class TokenCount:
    """Non-negative token counter.

    A dedicated VO (rather than a raw ``int``) lets us add semantic
    operations later (e.g. arithmetic with overflow check) and prevents
    accidental mixing with byte counts / latencies.
    """

    value: int

    def __post_init__(self) -> None:
        if not isinstance(self.value, int) or isinstance(self.value, bool):
            raise InvalidContextSizeError(
                f"TokenCount.value must be int, got {type(self.value).__name__}",
            )
        if self.value < 0:
            raise InvalidContextSizeError(
                f"TokenCount.value must be >= 0, got {self.value}",
            )

    def __add__(self, other: TokenCount) -> TokenCount:
        if not isinstance(other, TokenCount):
            return NotImplemented
        return TokenCount(self.value + other.value)


@dataclass(frozen=True, slots=True)
class ContextSize:
    """How many tokens a conversation occupies vs. its budget.

    Both fields are :class:`TokenCount` values; ``budget`` must be at
    least as large as ``used``.  ``ratio`` is convenient for logging.
    """

    used: TokenCount
    budget: TokenCount

    def __post_init__(self) -> None:
        if self.budget.value < self.used.value:
            raise InvalidContextSizeError(
                "ContextSize.budget must be >= ContextSize.used "
                f"(got used={self.used.value}, budget={self.budget.value})",
            )

    @property
    def ratio(self) -> float:
        """Return ``used / budget`` (0.0 when budget is zero)."""
        if self.budget.value == 0:
            return 0.0
        return self.used.value / self.budget.value

    def is_over_threshold(self, threshold: float) -> bool:
        """Return True iff usage ratio exceeds ``threshold`` (0..1)."""
        if not 0.0 <= threshold <= 1.0:
            raise InvalidContextSizeError(
                f"threshold must be in [0, 1], got {threshold!r}",
            )
        return self.ratio > threshold


class ContextUsage(NamedTuple):
    """Both the window-clamped AND the raw (un-clamped) context occupancy.

    The clamped pair (``used_clamped`` / ``ratio``) honours the
    :class:`ContextSize` invariant ``budget >= used`` so it can construct a
    :class:`ContextSize` value object: it floors at the window (e.g. 200K /
    100%) and never reveals an over-window history. The raw pair
    (``raw_used`` / ``raw_ratio``) preserves the TRUE occupancy so the UI badge
    can honestly surface an over-window state (e.g. ``raw_ratio`` 1.11 = 111%
    → "compaction imminent"). Negatives are always sanitised to 0.
    """

    used_clamped: int
    ratio: float
    raw_used: int
    raw_ratio: float


def compute_context_usage(used_raw: int, budget: int) -> ContextUsage:
    """Single source of truth for clamped + raw context-usage figures.

    Shared口径 between the main agent's :class:`CompactChatUseCase` and the
    sub-agent ``GET /api/chat/subagents/{id}`` badge so both report identical
    clamp / over-window semantics (judgement 1: one calculation, two callers).

    * ``raw_used`` = ``max(used_raw, 0)`` — negatives sanitised, over-window
      values PRESERVED (the badge needs them to show >100%).
    * ``used_clamped`` = ``min(raw_used, budget)`` — clamped to the window so a
      :class:`ContextSize` can be built without violating ``budget >= used``.
    * ``ratio`` / ``raw_ratio`` = the respective ``used / budget`` (0.0 when
      ``budget <= 0``).
    """
    safe_budget = budget if budget > 0 else 0
    raw_used = max(used_raw, 0)
    used_clamped = min(raw_used, safe_budget) if safe_budget > 0 else 0
    raw_ratio = (raw_used / safe_budget) if safe_budget > 0 else 0.0
    ratio = (used_clamped / safe_budget) if safe_budget > 0 else 0.0
    return ContextUsage(
        used_clamped=used_clamped,
        ratio=ratio,
        raw_used=raw_used,
        raw_ratio=raw_ratio,
    )


__all__ = [
    "MessageRole",
    "MessageContent",
    "TokenCount",
    "ContextSize",
    "ContextUsage",
    "compute_context_usage",
]
