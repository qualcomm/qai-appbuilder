# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Source tripwires for wiring that no unit test can drive end to end.

The behaviour behind each of these is covered elsewhere (``test_mcp_large_reply``,
``test_failed_tool_result_text``); what those cannot see is
whether the production call sites still USE it -- ``StreamChatUseCase`` is an
11k-line orchestrator and ``build_chat_services`` needs a full container. If one
of these fails, someone removed the call site: re-wire it (or replace the tripwire
with a real test) rather than deleting the assertion.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[4]
_STREAMING = _ROOT / "src/qai/chat/application/use_cases/streaming.py"
_INFRA_MCP = _ROOT / "src/qai/chat/infrastructure/mcp_client.py"


def _text(p: Path) -> str:
    return p.read_text(encoding="utf-8").replace("\r\n", "\n")


def test_turn_loop_replaces_a_failed_empty_result_with_the_error_text() -> None:
    src = _text(_STREAMING)
    m = re.search(
        r"raw_result = invocation\.result\n(?:.*\n){0,12}?\s*"
        r"_failed_text = _failed_invocation_text\(invocation\)\n"
        r"\s*if _failed_text is not None:\n\s*raw_result = _failed_text",
        src,
    )
    assert m, "streaming.py no longer substitutes the error text for a failed, empty result"


def test_stdio_children_are_spawned_with_a_raised_stream_limit() -> None:
    src = _text(_INFRA_MCP)
    spawn = src[src.index("create_subprocess_exec(\n") :]
    assert "limit=_MAX_LINE_BYTES" in spawn[: spawn.index(")\n")], (
        "the stdio child is spawned without limit=: replies over 64 KiB will fail again"
    )
