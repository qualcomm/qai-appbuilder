# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Share use cases (PR-045).

Two use cases backing the public share-link feature:

* :class:`CreateShareUseCase` — create a fresh ``app_builder_share`` row
  for a given run; surfaces the generated :class:`ShareToken` so the
  caller can build the public URL.
* :class:`GetShareByTokenUseCase` — resolve a token to its :class:`Run`
  payload; rejects revoked / expired tokens.

Token generation
----------------
We do NOT reuse :class:`IdGenerator.new_id` (ULIDs are too predictable
when used as a public token: 48-bit timestamp + 80-bit random). Instead
we use :func:`secrets.token_urlsafe` so each token is 32 bytes of OS
entropy encoded as 43 url-safe characters.

This keeps the import-linter ``domain-purity`` rule clean: the use case
imports :mod:`secrets` (a stdlib module) only — no platform / adapters
references.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timedelta

from qai.app_builder.application.ports import (
    RunRepositoryPort,
    ShareRepositoryPort,
)
from qai.app_builder.domain.errors import ShareExpiredError
from qai.app_builder.domain.run import Run
from qai.app_builder.domain.share import Share, ShareToken
from qai.app_builder.domain.value_objects import RunId
from qai.platform.time import Clock

__all__ = ["CreateShareUseCase", "GetShareByTokenUseCase"]


_DEFAULT_TOKEN_BYTES = 32
"""32 bytes of entropy → 43 url-safe characters (well within
:data:`qai.app_builder.domain.share._SHARE_TOKEN_RE`'s 16..128 cap)."""


class CreateShareUseCase:
    """Create a public share token referencing an existing :class:`Run`.

    ``ttl_seconds`` may be ``None`` (no expiry) or a positive integer.
    The use case validates the run exists before issuing the token, so
    :class:`qai.app_builder.domain.errors.RunNotFoundError` is the only
    business error a caller can see.
    """

    def __init__(
        self,
        *,
        runs: RunRepositoryPort,
        shares: ShareRepositoryPort,
        clock: Clock,
    ) -> None:
        self._runs = runs
        self._shares = shares
        self._clock = clock

    async def execute(
        self,
        *,
        run_id: RunId,
        ttl_seconds: int | None = None,
    ) -> Share:
        # Verify the run exists (raises RunNotFoundError on miss).
        await self._runs.get(run_id)

        if ttl_seconds is not None:
            if not isinstance(ttl_seconds, int) or isinstance(
                ttl_seconds, bool
            ):
                raise ValueError(
                    "ttl_seconds must be int or None, got "
                    f"{type(ttl_seconds).__name__}"
                )
            if ttl_seconds <= 0:
                raise ValueError(
                    f"ttl_seconds must be > 0 when set, got {ttl_seconds}"
                )

        now = self._clock.now()
        expires_at: datetime | None = None
        if ttl_seconds is not None:
            expires_at = now + timedelta(seconds=ttl_seconds)

        token = ShareToken(value=secrets.token_urlsafe(_DEFAULT_TOKEN_BYTES))
        share = Share(
            token=token,
            run_id=run_id,
            created_at=now,
            expires_at=expires_at,
            revoked=False,
        )
        await self._shares.save(share)
        return share


class GetShareByTokenUseCase:
    """Resolve a :class:`ShareToken` to the underlying :class:`Run`.

    Returns a tuple of ``(share, run)`` so the caller can shape the
    response with both pieces (token metadata + run payload). Refuses
    revoked / expired tokens with :class:`ShareExpiredError`.
    """

    def __init__(
        self,
        *,
        runs: RunRepositoryPort,
        shares: ShareRepositoryPort,
        clock: Clock,
    ) -> None:
        self._runs = runs
        self._shares = shares
        self._clock = clock

    async def execute(self, *, token: ShareToken) -> tuple[Share, Run]:
        share = await self._shares.get_by_token(token)
        now = self._clock.now()
        if not share.is_active(now=now):
            raise ShareExpiredError(
                message=f"share token {token} is revoked or expired",
                details={"token": str(token)},
            )
        run = await self._runs.get(share.run_id)
        return share, run
