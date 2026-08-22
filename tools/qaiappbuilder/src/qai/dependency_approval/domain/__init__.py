# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Domain layer for ``qai.dependency_approval``.

Pure Python entities and value objects — no framework dependencies
(domain-purity contract per import-linter forbidden set).
"""
from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Final

__all__ = [
    "DEFAULT_DENY_ARGS",
    "PendingRequest",
    "RequestStatus",
    "find_denied_args",
    "is_dep_install_command",
]


#: Default denied pip/uv install arguments (V1 parity:
#: ``backend/security/dep_broker.py:73-75``). These flags pull from
#: unverified sources (editable installs, git URLs, extra indices,
#: pre-releases) and require explicit operator approval.
DEFAULT_DENY_ARGS: Final[tuple[str, ...]] = (
    "-e",
    "git+",
    "--extra-index-url",
    "--pre",
)


#: Commands that trigger dep-broker interception (V1
#: ``_INSTALL_PATTERNS``).
_INSTALL_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(r"^(?:python\s+-m\s+)?pip3?\s+install\b", re.IGNORECASE),
    re.compile(r"^uv\s+pip\s+install\b", re.IGNORECASE),
    re.compile(r"^uv\s+add\b", re.IGNORECASE),
)

#: Read-only pip/uv commands that must NOT be intercepted (V1
#: ``_READONLY_PATTERNS``).
_READONLY_PATTERNS: Final[tuple[re.Pattern[str], ...]] = (
    re.compile(
        r"^(?:python\s+-m\s+)?pip3?\s+(?:list|show|freeze|check|config)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"^uv\s+pip\s+(?:list|show|freeze|check)\b", re.IGNORECASE
    ),
)


def is_dep_install_command(command: str) -> bool:
    """Return ``True`` iff ``command`` is a pip/uv *install* command.

    Read-only commands (``pip list``/``show``/``freeze``/``check``/
    ``config``, ``uv pip list`` …) return ``False``. Pure function
    mirroring V1 ``DepBroker.is_dep_install_command``
    (``dep_broker.py:81-92``).
    """
    cmd = command.strip()
    for pattern in _READONLY_PATTERNS:
        if pattern.search(cmd):
            return False
    for pattern in _INSTALL_PATTERNS:
        if pattern.search(cmd):
            return True
    return False


def find_denied_args(
    command: str, deny_args: tuple[str, ...] = DEFAULT_DENY_ARGS
) -> list[str]:
    """Return the denied arguments present in ``command`` (token-level).

    Mirrors V1 ``DepBroker._find_denied_args``
    (``dep_broker.py:94-126``) token-level matching:

    * exact token match (``--pre`` / ``-e`` / ``--extra-index-url``),
    * ``+``-suffixed prefix match (``git+`` matches ``git+https://…``),
    * ``=``-style prefix match (``--extra-index-url=https://…``).

    Pure function — no I/O. ``deny_args`` defaults to
    :data:`DEFAULT_DENY_ARGS`.
    """
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    found: list[str] = []
    for deny_pattern in deny_args:
        dp_lower = deny_pattern.lower()
        for token in tokens:
            tk_lower = token.lower()
            if tk_lower == dp_lower:
                found.append(deny_pattern)
                break
            if dp_lower.endswith("+") and tk_lower.startswith(dp_lower):
                found.append(deny_pattern)
                break
            if tk_lower.startswith(dp_lower + "="):
                found.append(deny_pattern)
                break
    return found


class RequestStatus(Enum):
    """Lifecycle states for a dependency-install request."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(slots=True)
class PendingRequest:
    """A dependency-install request awaiting operator decision.

    Invariants:
    * ``id`` is unique within the broker's in-memory store.
    * ``status`` starts as PENDING; only ``resolve()`` transitions it.
    * ``command_args`` captures the raw pip/uv install arguments.
    """

    id: str
    command_args: list[str]
    requester: str
    created_at: datetime
    status: RequestStatus = RequestStatus.PENDING
    #: The full command string the model tried to run (V1 pending DTO
    #: ``command`` — ``dep_broker.py:272``). Surfaced so the WebUI approval
    #: card shows exactly what was intercepted. Optional / tail-appended
    #: (defaults to the joined ``command_args`` when absent).
    command: str = ""
    #: The denied arguments that triggered interception (V1 ``denied_args``
    #: — ``dep_broker.py:273``). Drives the "why this was blocked" hint.
    denied_args: list[str] = field(default_factory=list)
