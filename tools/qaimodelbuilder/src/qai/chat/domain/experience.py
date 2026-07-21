# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``Experience`` entity for the chat experience-library feature.

The experience library lets users save snippets that proved useful in
past conversations and recall them later.  An :class:`Experience` is a
small, frozen record consisting of:

* an :class:`ExperienceId`;
* a category (free-form short string used for grouping);
* the textual ``content``;
* optional metadata (free-form opaque dict);
* a ``created_at`` timestamp.

Validation:

* category and content non-empty + length-bounded;
* metadata, if present, must be a ``dict``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from qai.chat.domain.ids import ExperienceId
from qai.platform.io_validator import (
    ValidationError as _IoValidationError,
)
from qai.platform.io_validator import (
    assert_max_length,
    assert_non_empty,
)
from qai.platform.time import ensure_aware_utc

_MAX_CATEGORY_LENGTH: int = 64
_MAX_CONTENT_LENGTH: int = 100_000


def _validate_field(
    value: str,
    *,
    name: str,
    max_length: int,
) -> str:
    try:
        assert_non_empty(value, name=name)
        assert_max_length(value, max_length=max_length, name=name)
    except _IoValidationError as exc:
        raise ValueError(str(exc)) from exc
    return value


@dataclass(frozen=True, slots=True, kw_only=True)
class Experience:
    """A reusable knowledge snippet saved by the user."""

    id: ExperienceId
    category: str
    content: str
    created_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, ExperienceId):
            raise TypeError(
                "Experience.id must be ExperienceId, got "
                f"{type(self.id).__name__}",
            )
        _validate_field(
            self.category,
            name="Experience.category",
            max_length=_MAX_CATEGORY_LENGTH,
        )
        _validate_field(
            self.content,
            name="Experience.content",
            max_length=_MAX_CONTENT_LENGTH,
        )
        ensure_aware_utc(self.created_at)
        if not isinstance(self.metadata, dict):
            raise TypeError(
                "Experience.metadata must be dict, got "
                f"{type(self.metadata).__name__}",
            )


@dataclass(frozen=True, slots=True, kw_only=True)
class CategoryStat:
    """Aggregate count of stored experiences in a single category.

    PR-095 / S9 audit §2.3 A-3.  The legacy frontend rendered the
    experience-library category list with per-category counts so the
    user could see at a glance which buckets were populated.  The
    refactor-era ``list_categories()`` returns just the names, dropping
    the counts; this value object pairs each name with its count and
    backs the new :meth:`SqliteExperienceRepository.list_categories_with_counts`.
    """

    name: str
    count: int

    def __post_init__(self) -> None:
        _validate_field(
            self.name,
            name="CategoryStat.name",
            max_length=_MAX_CATEGORY_LENGTH,
        )
        if not isinstance(self.count, int) or self.count < 0:
            raise ValueError(
                "CategoryStat.count must be a non-negative int, got "
                f"{self.count!r}"
            )


__all__ = ["CategoryStat", "Experience"]
