# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-level pure services for the channels bounded context.

Currently exposes :class:`MessageSplitter` (PR-203) — a stateless
utility that chunks long outbound text into pieces no larger than a
provider's per-message size cap (5KB for personal WeChat).
"""

from __future__ import annotations

from .message_splitter import MessageSplitter

__all__ = ["MessageSplitter"]
