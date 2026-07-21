# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cryptographic hash value objects for the qai platform layer.

Centralises the :class:`Hash256` primitive (64-char lower-case hex
SHA-256 digest) so every bounded context can share one implementation
instead of declaring its own copy.

The VO performs no I/O on construction; the explicit
:meth:`Hash256.of` convenience classmethod can either accept a
pre-computed hex digest (canonicalising case) or raw bytes / text and
return a SHA-256 digest in canonical lower-case form.

Cross-PR note (PR-026 schema doc §10.1): this module is the agreed
home for ``Hash256`` after PR-022 / PR-025 each shipped an identical
local copy. ``app_builder`` and ``model_catalog`` re-export the
platform symbol through their domain so existing callers need not be
aware of the move.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Final

__all__ = ["Hash256"]


_HEX64_RE: Final[re.Pattern[str]] = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True, kw_only=True)
class Hash256:
    """64-char lower-case hex SHA-256 digest.

    Construction validates the surface shape (regex) and rejects any
    non lower-case hex string. Adapters that compute the digest pass
    the canonical lower-case hex string in. Use :meth:`of` when you
    have raw bytes / text (or upper-case hex from an upstream API)
    and want a normalised digest in one step.

    The VO is hashable and immutable.
    """

    value: str

    def __post_init__(self) -> None:
        if not isinstance(self.value, str):
            raise TypeError(
                f"Hash256.value must be str, got {type(self.value).__name__}"
            )
        if not _HEX64_RE.match(self.value):
            raise ValueError(
                "Hash256.value must be 64 lower-case hex chars; "
                f"got {self.value!r}"
            )

    @classmethod
    def of(cls, raw: bytes | str) -> Hash256:
        """Compute a SHA-256 digest of ``raw`` and wrap it.

        ``str`` inputs that already look like a 64-char hex digest are
        treated as a pre-computed digest and merely normalised to
        lower-case (handy for upstream APIs that return upper-case).
        Otherwise the value is encoded as UTF-8 and hashed. ``bytes``
        inputs are always hashed.
        """

        if isinstance(raw, str):
            stripped = raw.strip()
            if len(stripped) == 64 and all(
                c in "0123456789abcdefABCDEF" for c in stripped
            ):
                return cls(value=stripped.lower())
            data = raw.encode("utf-8")
        else:
            data = raw
        digest = hashlib.sha256(data).hexdigest()
        return cls(value=digest)

    def __str__(self) -> str:
        return self.value
