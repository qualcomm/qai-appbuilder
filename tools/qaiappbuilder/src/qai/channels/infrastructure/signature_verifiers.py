# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Real :class:`WebhookSignatureVerifierPort` adapters (PR-047).

One implementation per :class:`ChannelKind`.  Each verifier maps to its
provider's documented signature scheme:

* WeChat Official Account: ``signature = sha1(sort([token, timestamp,
  nonce]))`` over the query string parameters
  (``signature``, ``timestamp``, ``nonce``).  The body itself is NOT
  signed — WeChat verifies origin via the four params.
* Feishu Open Platform: ``X-Lark-Signature: hmac_sha256(secret,
  timestamp + nonce + body)`` base64-encoded.

Each adapter takes a per-instance ``token`` (or HMAC ``secret``) at
construction; in production the apps layer reads this from the
:class:`SecretStoreCredentialsResolver` and passes it through.

Failures raise :class:`WebhookSignatureInvalidError`; missing
prerequisites (no token, no header) raise the same error rather than
silently passing — the Clean-Cutover principle (no soft-fail in
security-relevant code).
"""

from __future__ import annotations

import base64
import hashlib
import hmac

from qai.channels.domain import (
    ChannelKind,
    WebhookSignatureInvalidError,
)

__all__ = [
    "WechatSigVerifier",
    "FeishuSigVerifier",
]


def _headers_lower(headers: dict[str, str]) -> dict[str, str]:
    return {k.lower(): v for k, v in headers.items()}


def _raise_invalid(kind: ChannelKind, reason: str) -> None:
    raise WebhookSignatureInvalidError(
        kind.value, details={"reason": reason}
    )


class WechatSigVerifier:
    """Verifies the signature on inbound WeChat Official Account webhooks.

    WeChat's scheme: the query string includes ``signature``,
    ``timestamp`` and ``nonce``; the verifier sorts ``[token, timestamp,
    nonce]`` lexicographically, concatenates, and SHA-1 hashes.

    For our HTTP layer the four params arrive via headers:
    ``X-Wechat-Signature``, ``X-Wechat-Timestamp``, ``X-Wechat-Nonce``.
    The shared ``token`` is configured per-instance and supplied at
    construction.
    """

    KIND = ChannelKind.WECHAT
    __slots__ = ("_token",)

    def __init__(self, *, token: str) -> None:
        if not token:
            raise ValueError("WechatSigVerifier requires a non-empty token")
        self._token = token

    def verify(
        self,
        kind: ChannelKind,
        raw_body: bytes,
        headers: dict[str, str],
        *,
        instance_id: str | None = None,  # noqa: ARG002 — accepted for port compat
    ) -> None:
        if kind is not self.KIND:
            _raise_invalid(
                kind,
                f"{type(self).__name__} only handles {self.KIND.value}",
            )
        h = _headers_lower(headers)
        signature = h.get("x-wechat-signature", "")
        timestamp = h.get("x-wechat-timestamp", "")
        nonce = h.get("x-wechat-nonce", "")
        if not (signature and timestamp and nonce):
            _raise_invalid(
                kind, "missing required wechat signature headers"
            )
        items = sorted([self._token, timestamp, nonce])
        expected = hashlib.sha1(
            "".join(items).encode("utf-8"),
            usedforsecurity=False,
        ).hexdigest()
        if not hmac.compare_digest(expected, signature):
            _raise_invalid(kind, "wechat signature mismatch")


class FeishuSigVerifier:
    """Verifies the signature on inbound Feishu / Lark webhooks.

    Scheme: ``X-Lark-Signature = base64(hmac_sha256(secret,
    timestamp + nonce + body))``.

    Header convention for our HTTP layer:
    * ``X-Lark-Signature`` — base64-encoded HMAC-SHA256
    * ``X-Lark-Request-Timestamp`` — UNIX timestamp string
    * ``X-Lark-Request-Nonce`` — opaque nonce
    """

    KIND = ChannelKind.FEISHU
    __slots__ = ("_secret",)

    def __init__(self, *, secret: str) -> None:
        if not secret:
            raise ValueError(
                "FeishuSigVerifier requires a non-empty secret"
            )
        self._secret = secret

    def verify(
        self,
        kind: ChannelKind,
        raw_body: bytes,
        headers: dict[str, str],
        *,
        instance_id: str | None = None,  # noqa: ARG002 — accepted for port compat
    ) -> None:
        if kind is not self.KIND:
            _raise_invalid(
                kind,
                f"{type(self).__name__} only handles {self.KIND.value}",
            )
        h = _headers_lower(headers)
        signature_b64 = h.get("x-lark-signature", "")
        timestamp = h.get("x-lark-request-timestamp", "")
        nonce = h.get("x-lark-request-nonce", "")
        if not (signature_b64 and timestamp and nonce):
            _raise_invalid(
                kind, "missing required feishu signature headers"
            )
        message = (timestamp + nonce).encode("utf-8") + raw_body
        digest = hmac.new(
            self._secret.encode("utf-8"),
            message,
            hashlib.sha256,
        ).digest()
        expected_b64 = base64.b64encode(digest).decode("ascii")
        if not hmac.compare_digest(expected_b64, signature_b64):
            _raise_invalid(kind, "feishu signature mismatch")



