# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Runtime (user-extension) members of the persona permission groups."""

from __future__ import annotations

import pytest

from qai.chat.domain import tool_groups


@pytest.fixture(autouse=True)
def _restore():
    snapshot = dict(tool_groups.TOOL_GROUP_MEMBERS)
    yield
    tool_groups.TOOL_GROUP_MEMBERS.clear()
    tool_groups.TOOL_GROUP_MEMBERS.update(snapshot)


def test_add_then_disable_by_persona() -> None:
    tool_groups.add_group_members("command", frozenset({"ext_run"}))
    assert "ext_run" in tool_groups.groups_to_disabled_tools(["read"])
    assert "ext_run" not in tool_groups.groups_to_disabled_tools(["read", "command"])


def test_remove_keeps_builtin_members() -> None:
    tool_groups.add_group_members("read", frozenset({"ext_look"}))
    tool_groups.remove_group_members(frozenset({"ext_look", "read"}))
    assert "ext_look" not in tool_groups.TOOL_GROUP_MEMBERS["read"]
    assert "read" in tool_groups.TOOL_GROUP_MEMBERS["read"]  # built-in survives


def test_unknown_group_rejected() -> None:
    with pytest.raises(ValueError, match="unknown tool group"):
        tool_groups.add_group_members("network", frozenset({"x"}))
