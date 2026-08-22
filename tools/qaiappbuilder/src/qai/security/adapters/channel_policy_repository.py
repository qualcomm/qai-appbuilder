# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""aiosqlite-backed :class:`ChannelPolicyRepositoryPort` (PR-501).

Schema: see migration ``008_create_channel_policy_schema.sql`` —
``security_channel_policy`` keyed by ``name`` with optional
``quota_window_seconds`` / ``quota_max_asks`` columns. Persistence is a
flat row per channel (no parent-child relation), so save semantics are
plain ``INSERT OR REPLACE``.

The legacy ``PolicyCenter._no_ui_channels`` configuration lived in a
JSON file column inside ``policy.json`` (``backend/security/policy.py:
377-484``); PR-501 promotes it to a first-class table keyed on
:attr:`qai.security.domain.value_objects.Channel.name` so the new
``GET /api/security/channels`` route in PR-504 can list / update them
without round-tripping through the policy aggregate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.platform.errors import PersistenceError

from qai.security.domain.entities import ChannelPolicy
from qai.security.domain.value_objects import AskQuotaWindow, Channel

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence import Database

__all__ = ["SqliteChannelPolicyRepository"]


class SqliteChannelPolicyRepository:
    """aiosqlite implementation of :class:`ChannelPolicyRepositoryPort`."""

    __slots__ = ("_db",)

    def __init__(self, *, db: "Database") -> None:
        self._db = db

    async def get(self, channel_name: str) -> ChannelPolicy | None:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT name, requires_ui, quota_window_seconds, "
                    "quota_max_asks, description "
                    "FROM security_channel_policy WHERE name = ?",
                    (channel_name,),
                )
                row = await cur.fetchone()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.channel_policy.load_failed",
                f"failed to load channel policy {channel_name!r}: {exc}",
                operation="channel_policy.get",
                cause=exc,
            ) from exc
        if row is None:
            return None
        return self._row_to_policy(row)

    async def list_all(self) -> list[ChannelPolicy]:
        try:
            async with self._db.connection() as conn:
                cur = await conn.execute(
                    "SELECT name, requires_ui, quota_window_seconds, "
                    "quota_max_asks, description "
                    "FROM security_channel_policy "
                    "ORDER BY name ASC",
                )
                rows = await cur.fetchall()
                await cur.close()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.channel_policy.list_failed",
                f"failed to list channel policies: {exc}",
                operation="channel_policy.list_all",
                cause=exc,
            ) from exc
        return [self._row_to_policy(r) for r in rows]

    async def save(self, policy: ChannelPolicy) -> None:
        quota = policy.quota
        params = (
            policy.name,
            1 if policy.requires_ui else 0,
            quota.window_seconds if quota else None,
            quota.max_asks if quota else None,
            policy.description,
        )
        try:
            async with self._db.connection() as conn:
                await conn.execute(
                    "INSERT OR REPLACE INTO security_channel_policy "
                    "(name, requires_ui, quota_window_seconds, "
                    "quota_max_asks, description) "
                    "VALUES (?, ?, ?, ?, ?)",
                    params,
                )
                await conn.commit()
        except Exception as exc:  # noqa: BLE001
            raise PersistenceError(
                "security.channel_policy.save_failed",
                f"failed to save channel policy {policy.name!r}: {exc}",
                operation="channel_policy.save",
                cause=exc,
            ) from exc

    @staticmethod
    def _row_to_policy(
        row: tuple[object, object, object, object, object],
    ) -> ChannelPolicy:
        name = str(row[0])
        requires_ui = bool(int(row[1] or 0))
        window_seconds = row[2]
        max_asks = row[3]
        description = str(row[4] or "")
        channel = Channel(name=name, requires_ui=requires_ui)
        quota: AskQuotaWindow | None = None
        if window_seconds is not None and max_asks is not None:
            quota = AskQuotaWindow(
                window_seconds=int(window_seconds),
                max_asks=int(max_asks),
            )
        return ChannelPolicy(
            channel=channel,
            quota=quota,
            description=description,
        )
