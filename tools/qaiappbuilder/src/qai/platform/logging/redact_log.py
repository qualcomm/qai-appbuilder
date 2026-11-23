# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Mask credentials in log text before it leaves the machine.

Used by the "copy / export backend log" support feature: the user pastes the
result into an e-mail or a ticket, so anything that could act as a credential
is replaced with ``***`` first. Deliberately over-eager (a false positive only
hides a harmless value); ordinary log text is left untouched.
"""

from __future__ import annotations

import re

__all__ = ["MASK", "redact_log_text"]

MASK = "***"

# ``Authorization: Bearer <tok>`` / ``Bearer <tok>`` anywhere in a line.
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")

# key=value / key: value / "key": "value" for credential-like key names.
_KEYS = (
    r"api[_-]?key|apikey|access[_-]?key|secret[_-]?key|client[_-]?secret|secret|"
    r"password|passwd|pwd|token|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"session[_-]?token|auth[_-]?token|authorization|cookie|set-cookie|x-api-key|"
    r"private[_-]?key|credential|credentials|jwt|"
    # signed download URLs (S3 / Azure SAS / GitHub release assets)
    r"sig|signature|x-amz-signature|x-amz-credential|x-amz-security-token"
)
_KEY_VALUE = re.compile(
    rf"""(?ix)
    (?P<key>["']?\b(?:{_KEYS})\b["']?)      # the key, optionally quoted
    (?P<sep>\s*[:=]\s*)                     # = or :
    (?P<value>"[^"]*"|'[^']*'|[^\s,;&}}\]]+)  # quoted value or a bare token
    """
)

# Provider-style secret keys (OpenAI / Anthropic / DashScope "sk-...", GitHub, Slack, AWS).
_PROVIDER_KEYS = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16})\b"
)

# JSON Web Tokens: three base64url segments, the first starting with "eyJ".
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")

# Credentials embedded in a URL: scheme://user:password@host
_URL_CREDENTIALS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/:@]+:[^\s/@]+@")


def _mask_value(match: re.Match[str]) -> str:
    value = match.group("value")
    masked = value[0] + MASK + value[0] if value[:1] in "\"'" else MASK
    return f"{match.group('key')}{match.group('sep')}{masked}"


def redact_log_text(text: str) -> str:
    """Return ``text`` with credentials masked.

    Covers bearer tokens, keyed secrets, provider keys, JWTs and URL credentials.
    """
    text = _URL_CREDENTIALS.sub(rf"\1{MASK}:{MASK}@", text)
    text = _JWT.sub(MASK, text)
    text = _PROVIDER_KEYS.sub(MASK, text)
    text = _BEARER.sub(lambda m: f"{m.group(1)} {MASK}", text)
    return _KEY_VALUE.sub(_mask_value, text)
