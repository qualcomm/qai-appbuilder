# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Application-layer ports for ``qai.command_policy`` (PR-603).

The BC exposes :class:`ExecBrokerPort` for querying loaded exec profiles
and the broker's enabled state.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable

from qai.command_policy.domain import ExecAction, CommandProfile

__all__ = ["ExecBrokerPort"]


@runtime_checkable
class ExecBrokerPort(Protocol):
    """Port for exec-broker profile management.

    Implementations store profiles in memory (loaded at startup from
    configuration).
    """

    async def get_profiles(self) -> list[CommandProfile]:
        """Return all loaded exec profiles."""
        ...

    async def is_enabled(self) -> bool:
        """Return whether the exec broker is currently enabled."""
        ...

    def find_profile(self, command: str) -> CommandProfile | None:
        """Return the first profile matching ``command``'s binary.

        Returns ``None`` when the broker is disabled, has no profiles,
        or no profile's ``match_glob`` matches the extracted binary —
        in which case the command proceeds through the normal exec path.
        Synchronous (pure matching, no I/O). Mirrors V1
        ``ExecBroker.find_profile``.
        """
        ...

    def evaluate(
        self, command: str, *, project_root: str = ""
    ) -> tuple[ExecAction, str, CommandProfile | None]:
        """Classify ``command`` into ``ALLOW`` / ``ASK`` / ``DENY``.

        Returns ``(action, reason, matched_profile)``:

        * ``ALLOW`` — proceed through the normal exec path (also returned
          when the broker is disabled or no profile matched, with
          ``matched_profile=None``);
        * ``ASK``   — a ``ask_args`` danger flag matched; the caller should
          pop a permission dialog and let the user decide;
        * ``DENY``  — a hard-deny arg matched; the caller raises and feeds
          ``reason`` back to the LLM.
        """
        ...
