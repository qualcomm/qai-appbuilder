# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""SessionIndex aggregate — the channel-user → internal-session map.

Replaces legacy module-level state in
``backend/channels/wechat/cc_handler.py`` and
``backend/channels/feishu/api_routes.py``::

    _user_cc_sessions: dict[str, str] = {}     # eliminated
    _running_channels: list[str] = []          # eliminated

The aggregate scopes each entry to a ``(instance_id, channel_user_id)``
pair so two separate WeChat instances can have overlapping wxids
without collision — the legacy globals could not represent this.

A :class:`SessionIndex` is **mutable** (one entry per user) but its
internal ``_entries`` mapping is private; callers go through the
methods defined here so we preserve invariants and emit deltas.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from qai.platform.io_validator import (
    assert_max_length,
    assert_non_empty,
)
from qai.platform.time import ensure_aware_utc

from .ids import ChannelInstanceId, ChannelUserId

_MAX_REF_LENGTH = 256


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionIndexEntry:
    """One entry in a :class:`SessionIndex`.

    Fields:
    * ``instance_id`` / ``channel_user_id`` — composite key.
    * ``internal_user_id`` — optional internal user (``iam`` / ``security``
      context owns these strings; channels stores them opaquely).
    * ``coding_session_id`` — optional pointer to an
      :class:`~qai.ai_coding.domain.CodingSession` id (again, opaque).
    * ``updated_at`` — when this entry was last written.
    """

    instance_id: ChannelInstanceId
    channel_user_id: ChannelUserId
    internal_user_id: str | None = None
    coding_session_id: str | None = None
    updated_at: datetime

    def __post_init__(self) -> None:
        if self.internal_user_id is not None:
            assert_non_empty(
                self.internal_user_id,
                name="SessionIndexEntry.internal_user_id",
            )
            assert_max_length(
                self.internal_user_id,
                max_length=_MAX_REF_LENGTH,
                name="SessionIndexEntry.internal_user_id",
            )
        if self.coding_session_id is not None:
            assert_non_empty(
                self.coding_session_id,
                name="SessionIndexEntry.coding_session_id",
            )
            assert_max_length(
                self.coding_session_id,
                max_length=_MAX_REF_LENGTH,
                name="SessionIndexEntry.coding_session_id",
            )
        normalised = ensure_aware_utc(self.updated_at)
        if normalised is not self.updated_at:
            object.__setattr__(self, "updated_at", normalised)


@dataclass(slots=True)
class SessionIndex:
    """In-memory aggregate keyed by ``(instance_id, channel_user_id)``.

    Persistence is delegated to
    :class:`~qai.channels.application.ports.SessionIndexRepositoryPort`
    — domain code never touches a DB / file system / global dict.
    """

    _entries: dict[tuple[str, str], SessionIndexEntry] = field(
        default_factory=dict
    )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    @staticmethod
    def _key(
        instance_id: ChannelInstanceId, channel_user_id: ChannelUserId
    ) -> tuple[str, str]:
        return (instance_id.value, channel_user_id.value)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(
        self, key: tuple[ChannelInstanceId, ChannelUserId]
    ) -> bool:
        instance_id, channel_user_id = key
        return self._key(instance_id, channel_user_id) in self._entries

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def upsert(
        self,
        *,
        instance_id: ChannelInstanceId,
        channel_user_id: ChannelUserId,
        internal_user_id: str | None,
        coding_session_id: str | None,
        now: datetime,
    ) -> SessionIndexEntry:
        """Create or replace the entry for ``(instance_id, user)``.

        Returns the newly stored :class:`SessionIndexEntry`.
        """

        entry = SessionIndexEntry(
            instance_id=instance_id,
            channel_user_id=channel_user_id,
            internal_user_id=internal_user_id,
            coding_session_id=coding_session_id,
            updated_at=now,
        )
        self._entries[self._key(instance_id, channel_user_id)] = entry
        return entry

    def lookup(
        self,
        *,
        instance_id: ChannelInstanceId,
        channel_user_id: ChannelUserId,
    ) -> SessionIndexEntry | None:
        return self._entries.get(self._key(instance_id, channel_user_id))

    def remove(
        self,
        *,
        instance_id: ChannelInstanceId,
        channel_user_id: ChannelUserId,
    ) -> SessionIndexEntry | None:
        """Drop the entry, returning what was there (or ``None``)."""

        return self._entries.pop(
            self._key(instance_id, channel_user_id), None
        )

    def list_for_instance(
        self, instance_id: ChannelInstanceId
    ) -> tuple[SessionIndexEntry, ...]:
        return tuple(
            entry
            for (iid, _), entry in self._entries.items()
            if iid == instance_id.value
        )


__all__ = ["SessionIndex", "SessionIndexEntry"]
