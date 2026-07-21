# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Errors raised by ai_coding production tools."""

from __future__ import annotations

__all__ = ["ToolError", "ToolGuardDenied"]


class ToolError(Exception):
    """Base error for ai_coding production tool failures.

    Tool handlers convert these into ``ToolBridgeResult(ok=False,
    error_code=...)`` envelopes; they are NOT propagated as bare
    exceptions through :class:`RegistryBackedToolBridge.invoke`.
    """


class ToolGuardDenied(ToolError):
    """Raised by :class:`FileGuardPort` / :class:`FileBrokerPort` to deny a call.

    Carries a human-readable ``message`` (Chinese, matching legacy UX) and
    a stable ``error_code`` (matching the
    ``ai_coding.tool.<reason>`` namespace) for programmatic dispatch.
    """

    __slots__ = ("error_code", "message")

    def __init__(self, *, message: str, error_code: str) -> None:
        super().__init__(message)
        self.message = message
        self.error_code = error_code
