# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""qai.security.domain — pure domain layer for the security context.

Contains entities, value objects, domain events and domain errors.

This package MUST NOT import any of:

* ``fastapi`` / ``starlette`` / ``uvicorn``
* ``sqlalchemy`` / ``aiosqlite``
* ``httpx`` / ``pydantic_settings``
* ``apps`` / ``interfaces``
* any other ``qai.<context>`` package (cross-context isolation contract)

The only allowed external dependencies are the standard library and
``qai.platform.*`` (errors / events / io_validator / time / ids / logging).
"""

from __future__ import annotations

from .dangerous_commands import (
    BUILTIN_DANGEROUS_COMMAND_PATTERNS,
    compile_extra_patterns,
    dangerous_command_patterns,
    match_dangerous_command,
)
from .exec_deny_reason import (
    ExecDenyReason,
    classify_exec_deny_reason,
    exec_deny_message,
)
from .skill_capability import SkillCapability, SkillCapabilityViolation

__all__ = [
    "BUILTIN_DANGEROUS_COMMAND_PATTERNS",
    "ExecDenyReason",
    "SkillCapability",
    "SkillCapabilityViolation",
    "classify_exec_deny_reason",
    "compile_extra_patterns",
    "dangerous_command_patterns",
    "exec_deny_message",
    "match_dangerous_command",
]
