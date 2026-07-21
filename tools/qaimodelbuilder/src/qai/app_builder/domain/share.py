# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``Share`` value object — a public token referencing a single :class:`Run`.

Models the legacy ``data/app_builder_share.db`` content and the schema
defined in ``qai-db-schema.md`` §3.4. Each share row binds a token
(``id``) to a ``run_id``; clients holding the token can fetch a
read-only view of the run via ``GET /api/app-builder/share/{token}``.

Lifecycle:

* ``created_at`` — set once on creation;
* ``expires_at`` — optional wall-clock expiry; ``None`` means "no
  expiry" (the adapter still surfaces the row as inactive once
  ``revoked`` is true);
* ``revoked`` — boolean flag; revocation is a soft delete so audit
  trails survive.

The VO has no behaviour beyond input validation; persistence is done
via :class:`ShareRepositoryPort` (see :mod:`qai.app_builder.application.ports`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from qai.app_builder.domain.value_objects import RunId

__all__ = ["ShareToken", "Share"]


_SHARE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_\-]{16,128}$")
"""URL-safe share tokens; 16..128 chars of base64url-ish alphabet.

The width keeps the token comfortably above a 64-bit guess threshold
while staying short enough for human-shareable URLs.
"""


@dataclass(frozen=True, slots=True, kw_only=True)
class ShareToken:
    """Opaque public identifier of a :class:`Share` row.

    Validated against :data:`_SHARE_TOKEN_RE` so adapters never need to
    sanitise it before composing SQL or URL paths.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise ValueError(
                f"ShareToken.value must be str, got {type(self.value).__name__}"
            )
        if not _SHARE_TOKEN_RE.match(self.value):
            raise ValueError(
                "ShareToken.value must match [A-Za-z0-9_-]{16,128}, "
                f"got {self.value!r}"
            )

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, kw_only=True)
class Share:
    """A single ``app_builder_share`` row.

    All datetimes MUST be tz-aware (UTC by convention; the adapter
    preserves whatever zone the producing :class:`Clock` supplied).
    """

    token: ShareToken
    run_id: RunId
    created_at: datetime
    expires_at: datetime | None = None
    revoked: bool = False

    def __post_init__(self) -> None:
        if self.created_at.tzinfo is None:
            raise ValueError("Share.created_at must be tz-aware")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            raise ValueError("Share.expires_at must be tz-aware if set")
        if not isinstance(self.revoked, bool):
            raise ValueError(
                "Share.revoked must be bool, "
                f"got {type(self.revoked).__name__}"
            )

    def is_active(self, *, now: datetime) -> bool:
        """Return True iff the share is currently usable.

        A share is usable when not revoked AND (no expiry OR not yet
        expired at ``now``). The check is pure — no I/O.
        """
        if self.revoked:
            return False
        if self.expires_at is None:
            return True
        if now.tzinfo is None:
            raise ValueError("now must be tz-aware")
        return now < self.expires_at
