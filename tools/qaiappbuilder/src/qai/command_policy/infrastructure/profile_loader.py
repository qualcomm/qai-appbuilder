# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Exec profile loader for ``qai.command_policy`` (U-008b).

Loads static :class:`CommandProfile` assets from a directory of ``*.toml``
files at DI construction time. The TOML schema mirrors the V1 JSON
profiles (``config/exec_profiles/*.json``)::

    name = "git"
    description = "..."
    allowed_args = [...]
    denied_args = [...]

    [match]
    binary_glob = "**/git/bin/git.exe"

    [io_constraints]
    input_dirs = [...]
    output_dirs = [...]

The V1 ``match.binary_glob`` nested table maps to the flat
``CommandProfile.match_glob`` field. ``source_skill`` is a V2 addition (not
present in config-loaded TOML) and defaults to empty.

Infrastructure layer: may import the domain :class:`CommandProfile` and read
the filesystem (stdlib ``tomllib`` / ``pathlib``); takes no framework
dependency, so the layered-exec_broker contract (domain ⇐ infrastructure)
holds.
"""
from __future__ import annotations

import logging
import tomllib
from pathlib import Path
from typing import Any

from qai.command_policy.domain import CommandProfile

__all__ = ["load_all", "load_profile"]

_log = logging.getLogger(__name__)


def load_profile(data: dict[str, Any]) -> CommandProfile:
    """Map a parsed TOML mapping to an :class:`CommandProfile`.

    The nested ``[match]`` table's ``binary_glob`` (legacy single glob) is
    flattened to ``CommandProfile.match_glob``; its ``binary_globs`` list (new)
    maps to ``CommandProfile.match_globs``. New danger-classification lists
    ``ask_args`` / ``hard_deny_args`` (top-level) map to the same-named
    fields. Unknown/missing keys fall back to the dataclass defaults.
    """
    match = data.get("match")
    match_glob = ""
    match_globs: list[str] = []
    if isinstance(match, dict):
        match_glob = str(match.get("binary_glob", "") or "")
        _globs = match.get("binary_globs")
        if isinstance(_globs, list):
            match_globs = [str(g) for g in _globs if g]

    io_constraints = data.get("io_constraints")
    if not isinstance(io_constraints, dict):
        io_constraints = {}

    def _list(key: str) -> list[str]:
        val = data.get(key)
        return [str(x) for x in val] if isinstance(val, list) else []

    ask_rules_raw = data.get("ask_rules")
    ask_rules: list[dict] = []
    if isinstance(ask_rules_raw, list):
        for rule in ask_rules_raw:
            if isinstance(rule, dict) and rule.get("subcommand"):
                ask_rules.append(dict(rule))

    return CommandProfile(
        name=str(data.get("name", "")),
        description=str(data.get("description", "") or ""),
        match_glob=match_glob,
        match_globs=match_globs,
        allowed_args=_list("allowed_args"),
        denied_args=_list("denied_args"),
        hard_deny_args=_list("hard_deny_args"),
        ask_args=_list("ask_args"),
        ask_rules=ask_rules,
        ask_always=bool(data.get("ask_always", False)),
        io_constraints=dict(io_constraints),
        source_skill=str(data.get("source_skill", "") or ""),
    )


def load_all(profiles_dir: Path) -> list[CommandProfile]:
    """Load every ``*.toml`` profile under ``profiles_dir``.

    Returns the profiles sorted by filename for determinism. A missing or
    non-directory ``profiles_dir`` yields ``[]`` (graceful — the asset
    bundle may be absent in a partial install). Malformed TOML or profiles
    without a ``name`` are skipped with a warning rather than crashing
    startup.
    """
    profiles_dir = Path(profiles_dir)
    if not profiles_dir.is_dir():
        return []

    profiles: list[CommandProfile] = []
    for toml_path in sorted(profiles_dir.glob("*.toml")):
        try:
            with toml_path.open("rb") as fh:
                data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            _log.warning(
                "command_policy: skipping malformed profile %s: %s",
                toml_path.name,
                exc,
            )
            continue
        if not isinstance(data, dict) or not data.get("name"):
            _log.warning(
                "command_policy: skipping profile %s (missing 'name')",
                toml_path.name,
            )
            continue
        profiles.append(load_profile(data))
    return profiles
