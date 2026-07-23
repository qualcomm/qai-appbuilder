# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""In-memory adapter for ``ExecBrokerPort`` (PR-603).

This is the production adapter — exec profiles are loaded once at
startup and held in memory. They define the static whitelist/denylist
of executable commands for sandbox enforcement.
"""
from __future__ import annotations

import os

from qai.command_policy.domain import (
    ClassifyReason,
    ExecAction,
    CommandProfile,
    extract_args,
    extract_binary,
)

__all__ = ["InMemoryExecBroker"]


class InMemoryExecBroker:
    """In-memory implementation of :class:`ExecBrokerPort`.

    Profiles are injected at construction time; ``is_enabled`` is a
    simple toggle (defaults to ``False`` per S-1/D11 master-switch OFF).
    """

    def __init__(
        self,
        *,
        profiles: list[CommandProfile] | None = None,
        enabled: bool = False,
        project_root: str = "",
    ) -> None:
        self._profiles: list[CommandProfile] = profiles or []
        self._enabled: bool = enabled
        self._project_root: str = project_root

    async def get_profiles(self) -> list[CommandProfile]:
        """Return all loaded exec profiles."""
        return list(self._profiles)

    async def is_enabled(self) -> bool:
        """Return whether the exec broker is enabled."""
        return self._enabled

    @property
    def enabled(self) -> bool:
        """Synchronous master-switch read (parity with dep_broker).

        Used by the runtime-config GET surface, which is synchronous; the
        async :meth:`is_enabled` remains the port contract for callers in
        an async context.
        """
        return self._enabled

    def find_profile(self, command: str) -> CommandProfile | None:
        """Return the first profile matching ``command``'s binary.

        ``None`` when disabled / no profiles / no match (V1
        ``ExecBroker.find_profile``).
        """
        if not self._enabled or not self._profiles:
            return None
        binary = extract_binary(command)
        if not binary:
            return None
        for profile in self._profiles:
            if profile.matches_binary(binary):
                return profile
        return None

    def evaluate(
        self, command: str, *, project_root: str = ""
    ) -> tuple[ExecAction, ClassifyReason, CommandProfile | None]:
        """Classify ``command`` into ``ALLOW`` / ``ASK`` / ``DENY``.

        Returns ``(action, reason, matched_profile)``. When disabled or no
        profile matches, returns ``(ALLOW, "", None)`` so the command
        proceeds through the normal exec path.
        """
        profile = self.find_profile(command)
        if profile is None:
            return (ExecAction.ALLOW, ClassifyReason(), None)
        args = extract_args(command)
        root = project_root or self._project_root
        action, reason = profile.classify(
            args,
            root,
            workspace=root,
            temp_dir=os.environ.get("TEMP", ""),
        )
        return (action, reason, profile)

    # ------------------------------------------------------------------
    # Configuration helpers (not part of the port contract)
    # ------------------------------------------------------------------

    def set_enabled(self, enabled: bool) -> None:
        """Toggle the enabled state."""
        self._enabled = enabled

    def replace_profiles(self, profiles: list[CommandProfile]) -> None:
        """Atomically replace the loaded profile set (L-6 hot reload).

        V1 parity: ``exec_broker.py:304-311`` ``reload`` re-read the profile
        directory and swapped the in-memory set. Here the caller (the route /
        DI) re-runs ``profile_loader.load_all`` and hands us the fresh list so
        this adapter stays free of filesystem I/O (Clean-Arch: infrastructure
        does the load, the adapter holds the state).
        """
        self._profiles = list(profiles)

    def add_profile(self, profile: CommandProfile) -> None:
        """Add a profile to the store."""
        self._profiles.append(profile)
