# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases: QR-image rendering and read-only challenge lookup (PR-204).

Adds two thin use cases that complete the QR-login surface introduced in
PR-047:

* :class:`LookupQrLoginChallengeUseCase` — non-mutating read of a
  :class:`QrLoginChallenge` by ``(instance_id, challenge_id)``.  Unlike
  :meth:`QrLoginPort.check_status` (which advances ``ISSUED → SCANNED``
  on first poll), this use case only verifies existence + ownership and
  is safe to invoke from idempotent endpoints (image rendering, WS
  filter validation).
* :class:`RenderQrImageUseCase` — encodes a deterministic identifier
  string into a PNG byte-string suitable for the legacy
  ``GET /api/{kind}/qr/{id}/image`` endpoint.

Why a separate ``Lookup`` use case?
-----------------------------------
The existing :class:`ConfirmQrLoginUseCase` always calls
:meth:`QrLoginPort.check_status` which mutates challenge state on first
poll (the legacy cc_handler proxy advances ISSUED → SCANNED).  Image
rendering must NOT advance the state machine — the user is rendering
the QR for the first time.  Reading directly from the repo via a small
``ChallengeReaderPort`` Protocol keeps the use case independent of the
state-machine adapter.

Encoding
--------
The QR image encodes the REAL provider QR-login URL that the wechatbot
SDK reported via its ``on_qr_url`` callback and which was persisted onto
:attr:`QrLoginChallenge.qr_url` by
:class:`qai.channels.adapters.qr_login.WechatPersonalQrLoginAdapter`.
This matches V1 (``backend/channels/wechat/api_routes.py:94``
``qrcode.make(qr_url)`` where ``qr_url`` came from the SDK), so the
rendered QR is scannable by the WeChat app.

When the SDK has not yet supplied a URL (``qr_url is None``) the use
case raises :class:`QrLoginChallengeNotFoundError` so the route returns
404 — V1 parity with ``api_routes.py:95-96`` which 404s until a URL is
available.  The client polls until the image becomes available.

Layering
--------
Application-layer use cases ordinarily delegate I/O to ports.  The
``qrcode`` library is a pure-Python image *encoder* (no network I/O,
no persistence), so importing it here is analogous to importing
``datetime`` — no port wrapping is required.  PNG rendering is
deterministic and synchronous, so the use case is sync as well.
"""

from __future__ import annotations

from io import BytesIO
from typing import Protocol, runtime_checkable

import qrcode

from qai.channels.application.ports import (
    ChannelInstanceRepositoryPort,
)
from qai.channels.domain import (
    ChannelInstanceId,
    ChannelKind,
    QrLoginChallenge,
    QrLoginChallengeNotFoundError,
)
from qai.channels.domain.errors import ChannelKindNotSupportedError


@runtime_checkable
class QrChallengeReaderPort(Protocol):
    """Read-only port for :class:`QrLoginChallenge` lookup.

    Matches the public surface of
    :class:`qai.channels.adapters.qr_login_repository.SqliteQrLoginChallengeRepository`
    that does NOT mutate state.  Defined locally so the use case is
    independent of the concrete repo class.
    """

    async def find(
        self, challenge_id: str
    ) -> QrLoginChallenge | None: ...


class LookupQrLoginChallengeUseCase:
    """Read-only lookup of a challenge by ``(instance_id, challenge_id)``.

    Validates that:

    * the instance exists and matches the requested ``kind``;
    * the challenge exists and belongs to the same instance.

    Does NOT advance the challenge state machine.  Use this from
    idempotent endpoints (image rendering, WS connection setup);
    callers that must drive the state machine continue to use
    :class:`ConfirmQrLoginUseCase`.
    """

    __slots__ = ("_instances", "_challenges")

    def __init__(
        self,
        *,
        instances: ChannelInstanceRepositoryPort,
        challenges: QrChallengeReaderPort,
    ) -> None:
        self._instances = instances
        self._challenges = challenges

    async def execute(
        self,
        instance_id: ChannelInstanceId,
        challenge_id: str,
        *,
        expected_kind: ChannelKind,
    ) -> QrLoginChallenge:
        # Validate instance + kind.  ``get`` raises
        # :class:`ChannelInstanceNotFoundError` if the id is unknown.
        instance = await self._instances.get(instance_id)
        if instance.kind is not expected_kind:
            # The slug on the URL prefix did not match the registered
            # kind for this instance.  Surface as "not found" rather
            # than leaking the registered kind.
            raise QrLoginChallengeNotFoundError(challenge_id)

        challenge = await self._challenges.find(challenge_id)
        if challenge is None:
            raise QrLoginChallengeNotFoundError(challenge_id)
        if challenge.instance_id_value != instance_id.value:
            # Cross-instance lookup attempt — same as missing.
            raise QrLoginChallengeNotFoundError(challenge_id)
        return challenge


class RenderQrImageUseCase:
    """Render a challenge into a PNG byte-string.

    Composition:

    * Load + verify the challenge via
      :class:`LookupQrLoginChallengeUseCase` (no state mutation).
    * Encode the challenge's REAL provider ``qr_url`` (reported by the
      wechatbot SDK ``on_qr_url`` callback) to a PNG using the bundled
      ``qrcode`` + ``Pillow`` runtime.
    * When the SDK has not yet supplied a URL, raise
      :class:`QrLoginChallengeNotFoundError` so the route 404s and the
      client keeps polling (V1 parity).

    Returns the raw PNG bytes; the route layer wraps them in a
    ``Response(content=..., media_type='image/png')``.
    """

    __slots__ = ("_lookup",)

    def __init__(
        self, *, lookup: LookupQrLoginChallengeUseCase
    ) -> None:
        self._lookup = lookup

    async def execute(
        self,
        instance_id: ChannelInstanceId,
        challenge_id: str,
        *,
        expected_kind: ChannelKind,
    ) -> bytes:
        if expected_kind is ChannelKind.FEISHU:
            # The route layer rejects feishu earlier; if we still
            # reach here, surface the same kind-not-supported error
            # the QR adapters use.
            raise ChannelKindNotSupportedError(expected_kind.value)

        # Verify the challenge exists + belongs to the instance and
        # pull its persisted real QR URL.
        challenge = await self._lookup.execute(
            instance_id, challenge_id, expected_kind=expected_kind
        )

        qr_url = challenge.qr_url
        if not qr_url:
            # The wechatbot SDK has not reported a QR URL yet (login flow
            # still bootstrapping).  404 so the client keeps polling —
            # V1 parity with ``api_routes.py:95-96``; we must NOT render a
            # placeholder QR the WeChat app cannot scan.
            raise QrLoginChallengeNotFoundError(challenge_id)

        img = qrcode.make(qr_url)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()


__all__ = [
    "QrChallengeReaderPort",
    "LookupQrLoginChallengeUseCase",
    "RenderQrImageUseCase",
]
