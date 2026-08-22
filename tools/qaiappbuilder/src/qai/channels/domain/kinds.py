# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Channel kind enumeration for the channels bounded context.

A single :class:`ChannelKind` discriminator is used across **one** unified
:class:`ChannelInstance` aggregate — replacing the legacy ``feishu/`` /
``wechat/`` parallel sub-packages that produced two strongly connected
components in the old import graph (see
``docs/90-refactor/inventory/03-imports-dependencies.md`` §4 / §5).

Every adapter / port / use case treats the kind as a discriminator value
rather than hard-importing kind-specific code.  This is the central
design choice that keeps the new context flat and SCC-free.
"""

from __future__ import annotations

from enum import Enum


class ChannelKind(str, Enum):
    """Discriminator for the supported messaging providers.

    Values are lowercase ASCII strings so they can be safely embedded in
    log messages, event types, persisted rows and ``SecretStore``
    namespaces (e.g. ``"qai.channels.feishu"``) without escaping.
    """

    FEISHU = "feishu"
    WECHAT = "wechat"

    @classmethod
    def from_str(cls, raw: str) -> ChannelKind:
        """Return the enum member whose value equals ``raw``.

        Raises :class:`ValueError` if ``raw`` is not a known kind — this
        is a *programmer* error (the application layer should have
        validated upstream); the application layer wraps it into
        :class:`~qai.channels.domain.errors.ChannelKindNotSupportedError`.
        """

        try:
            return cls(raw)
        except ValueError as exc:
            allowed = ", ".join(sorted(m.value for m in cls))
            raise ValueError(
                f"unknown ChannelKind {raw!r}; allowed: [{allowed}]"
            ) from exc


__all__ = ["ChannelKind"]
