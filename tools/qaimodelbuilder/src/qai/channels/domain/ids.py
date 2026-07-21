# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Identifier value objects for the channels bounded context.

Each identifier wraps a single ``value: str`` field, validated on
construction and frozen so it can be safely used as a dict key / in
sets / inside other VOs.

Kind-agnostic by design: a :class:`ChannelInstanceId` is the same shape
regardless of whether the underlying provider is Feishu or WeChat
(the kind itself is carried by the :class:`ChannelInstance`
aggregate, not by the id).
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
class ChannelInstanceId:
    """Stable identifier for a :class:`ChannelInstance` aggregate."""

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="ChannelInstanceId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> ChannelInstanceId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> ChannelInstanceId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ChannelMessageId:
    """Stable identifier for a :class:`ChannelMessage` entity."""

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="ChannelMessageId")

    @classmethod
    def generate(cls, ids: IdGenerator) -> ChannelMessageId:
        return cls(ids.new_id())

    @classmethod
    def of(cls, raw: str) -> ChannelMessageId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class ChannelUserId:
    """External user identifier as supplied by a channel provider.

    The string value has provider-specific semantics (open_id for
    Feishu, wxid for personal WeChat, etc.) but the domain layer treats
    it as opaque — only the :class:`SessionIndex` aggregate maps it to
    internal user / coding-session ids.
    """

    value: str

    def __post_init__(self) -> None:
        _validate_id(self.value, name="ChannelUserId")

    @classmethod
    def of(cls, raw: str) -> ChannelUserId:
        return cls(raw)

    def __str__(self) -> str:
        return self.value


__all__ = [
    "ChannelInstanceId",
    "ChannelMessageId",
    "ChannelUserId",
]
