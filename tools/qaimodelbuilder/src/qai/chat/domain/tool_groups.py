# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Tool permission groups — chat-domain single source of truth.

Defines which tool names belong to which permission group, and provides
the conversion from a persona's ``groups`` spec (a list of group ids,
possibly with restrictions) to the set of tool names that should be
DISABLED (i.e. hidden from the LLM's advertised tool list).

This module lives in the chat domain because the concept of "which tools
exist and how to categorize them" is a chat-context concern.  The
user_prefs domain only stores which groups a persona is *allowed* to use;
the chat domain interprets those permissions.

No framework dependencies — domain-purity contract.
"""
from __future__ import annotations

from typing import Any, Final

__all__ = [
    "ALL_TOOL_GROUPS",
    "TOOL_GROUP_MEMBERS",
    "groups_to_disabled_tools",
    "groups_file_restrictions",
]

#: Canonical tool group identifiers.
ALL_TOOL_GROUPS: Final[tuple[str, ...]] = ("read", "edit", "command")

#: Mapping from group id to frozenset of tool names belonging to that group.
#: Tools NOT listed here are "always available" (todowrite, question, agent,
#: skill, list_subagents, appbuilder_run, appbuilder_batch_run) and are never
#: filtered regardless of persona permissions.
TOOL_GROUP_MEMBERS: Final[dict[str, frozenset[str]]] = {
    "read": frozenset({"read", "glob", "grep", "webfetch", "web_search", "list"}),
    "edit": frozenset({"edit", "write", "apply_patch"}),
    "command": frozenset({"exec", "background_process"}),
}


def groups_to_disabled_tools(groups: list[Any]) -> frozenset[str]:
    """Convert a persona's ``groups`` list to a frozenset of disabled tool names.

    Tools whose group is NOT in the persona's ``groups`` list are disabled.
    Restricted groups (e.g. ``["edit", {"fileRegex": ...}]``) still count as
    having that group — the restriction is enforced separately at the
    file-guard / handler layer, not by hiding the tool schema from the LLM.

    Examples::

        >>> groups_to_disabled_tools(["read", "edit", "command"])
        frozenset()  # all groups enabled, nothing disabled
        >>> groups_to_disabled_tools(["read"])
        frozenset({'edit', 'write', 'apply_patch', 'exec', 'background_process'})
    """
    # Extract flat group ids from the groups spec (ignoring restriction dicts).
    enabled_group_ids: set[str] = set()
    for g in groups:
        if isinstance(g, str):
            enabled_group_ids.add(g)
        elif isinstance(g, list) and g and isinstance(g[0], str):
            enabled_group_ids.add(g[0])

    disabled: set[str] = set()
    for group_id, members in TOOL_GROUP_MEMBERS.items():
        if group_id not in enabled_group_ids:
            disabled.update(members)
    return frozenset(disabled)


def groups_file_restrictions(groups: list[Any]) -> dict[str, str] | None:
    """Extract file restrictions from a persona's groups spec.

    Returns a dict like ``{"edit": "\\\\.md$"}`` if the edit group has a
    ``fileRegex`` restriction, or ``None`` if there are no restrictions.
    """
    restrictions: dict[str, str] = {}
    for g in groups:
        if isinstance(g, list) and len(g) >= 2:
            gid = g[0]
            opts = g[1]
            if isinstance(gid, str) and isinstance(opts, dict):
                regex = opts.get("fileRegex")
                if isinstance(regex, str):
                    restrictions[gid] = regex
    return restrictions if restrictions else None
