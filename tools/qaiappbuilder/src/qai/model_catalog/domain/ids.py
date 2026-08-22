# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Identifier value objects for the ``model_catalog`` bounded context.

Each identifier is a tiny :class:`~dataclasses.dataclass` wrapping a single
``value: str`` field.  Keeping them as proper value objects (rather than
bare ``str`` aliases) gives us:

* construction-time validation (non-empty, length-bounded);
* type safety -- a ``ModelEntryId`` is NOT interchangeable with a
  ``DownloadJobId`` even though both are strings underneath;
* equality / hashing for free via ``frozen=True``;
* a single place to evolve representation later (e.g. swap ULID for
  UUID v7) without touching call sites.

Construction goes through :meth:`generate` (uses an injected
:class:`~qai.platform.ids.IdGenerator`) or :meth:`of` (wraps an
already-existing string from persistence).  Domain code MUST NOT call
``new_ulid()`` directly to keep ID generation testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from qai.platform.ids import IdGenerator
from qai.platform.io_validator import assert_max_length, assert_non_empty

_MAX_ID_LENGTH = 128


def _validate_id(value: str, *, name: str) -> str:
    assert_non_empty(value, name=name)
    assert_max_length(value, max_length=_MAX_ID_LENGTH, name=name)
    return value


@dataclass(frozen=True, slots=True)
class ModelEntryId:
    """Stable identifier for a :class:`ModelEntry` aggregate.

    Typically a slug-like string (``"qwen-7b-q4-gguf"``) chosen by the
    catalog author, NOT a generated id.  We still validate length so a
    rogue catalog cannot inject 100KB strings into the system.
    """

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="ModelEntryId")

    @classmethod
    def of(cls, raw: str) -> ModelEntryId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ModelVersionId:
    """Stable identifier for a :class:`ModelVersion` entity."""

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="ModelVersionId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> ModelVersionId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> ModelVersionId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class DownloadJobId:
    """Stable identifier for a :class:`DownloadJob` aggregate."""

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="DownloadJobId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> DownloadJobId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> DownloadJobId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class SkillName:
    """Identifier for a :class:`SkillDefinition` (logical skill name).

    Skill names are human-authored slugs (``"code-review"`` /
    ``"summarise"``) that double as their primary key inside the catalog.
    """

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="SkillName")

    @classmethod
    def of(cls, raw: str) -> SkillName:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


__all__ = [
    "ModelEntryId",
    "ModelVersionId",
    "DownloadJobId",
    "SkillName",
]
