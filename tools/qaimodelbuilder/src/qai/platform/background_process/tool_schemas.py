# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""LLM tool schema for the background-process manager.

Provides the OpenAI function-calling schema and cross-field validation
for the ``background_process`` tool.  The description is a CONDENSED form
of the tool description text (tools-JSON体积压缩 C1): the
action list, dev-server use-case, no-shell-backgrounding rule, and the
``ready`` semantics are all preserved, only the verbose examples were
dropped (see ``docs/90-refactor/background-process-design.md`` §8.1).

The schema is intentionally kept in this module (not inlined into
``tool_handlers.py``) so that the ``apps/api`` layer can import it
independently for tool-list endpoints and the OpenAPI snapshot without
pulling in the async handler machinery.

No imports from ``qai.ai_coding.*`` — this module is a pure-platform
artefact that the ``apps/api`` layer wires into whatever tool registry
it uses.
"""

from __future__ import annotations

from qai.platform.tool_docs import (
    AVAILABLE_TOOLS_SECTION,
    PREFER_DEDICATED_TOOLS_SECTION,
    SHELL_ALIAS_DESCRIPTION,
    SHELL_ALIAS_ENUM,
    SHELL_NOTES_SECTION,
    WORKDIR_GUIDANCE_SECTION,
)

__all__ = [
    "BACKGROUND_PROCESS_TOOL_DESCRIPTION",
    "BACKGROUND_PROCESS_TOOL_SCHEMA",
    "validate_params",
]

# ---------------------------------------------------------------------------
# Tool description
# ---------------------------------------------------------------------------
#
# The description composes shared fragments from ``qai.platform.tool_docs``
# (environment / shell notes / workdir guidance / prefer-dedicated-tools —
# identical wording to the ``exec`` tool) with tool-specific text
# (long-running semantics, action set, ``ready`` probe, ``inject_file_guard``).
# A single edit to a shared fragment therefore updates BOTH tools.

BACKGROUND_PROCESS_TOOL_DESCRIPTION: str = (
    "Run and manage long-running background processes for the current "
    "session.\n\n"
    "Use this for commands expected to keep running — dev servers, file "
    "watchers, local services, test watchers (`npm run dev`, `vite`, "
    "`bun --watch`, ...). Do NOT background them via the exec tool (`&`, "
    "`nohup`, `Start-Process`, etc.): processes started here are tracked, "
    "shown in the sidebar, and auto-stopped when the session ends.\n\n"
    "Actions: `start` (needs `command`; optional `workdir` / `description` "
    "/ `shell` / `ready`), `list`, `status`, `logs`, `stop`, `restart`. "
    "Pass `id` only for status/logs/stop/restart — never invent one when "
    "starting.\n\n"
    "Optional `shell` picks the interpreter (same aliases as the exec "
    "tool). Default `auto` picks pwsh/powershell/cmd on Windows. Use "
    "`shell='sh'` to run POSIX / bash scripts.\n\n"
    "Optional `ready` marks the process ready when its output matches "
    "`ready.pattern` or a TCP `ready.port` accepts connections; omit it "
    "to return immediately.\n\n"
    f"{AVAILABLE_TOOLS_SECTION}\n\n"
    f"{PREFER_DEDICATED_TOOLS_SECTION}\n\n"
    f"{WORKDIR_GUIDANCE_SECTION}\n\n"
    f"{SHELL_NOTES_SECTION}"
)

# ---------------------------------------------------------------------------
# JSON Schema (OpenAI function-calling format)
# ---------------------------------------------------------------------------

BACKGROUND_PROCESS_TOOL_SCHEMA: dict = {
    "type": "function",
    "function": {
        "name": "background_process",
        "description": BACKGROUND_PROCESS_TOOL_DESCRIPTION,
        "parameters": {
            "type": "object",
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["start", "list", "status", "logs", "stop", "restart"],
                    "description": "Operation to perform",
                },
                "command": {
                    "type": "string",
                    "description": (
                        "Required for start. Command to run as a tracked "
                        "background process."
                    ),
                },
                "workdir": {
                    "type": "string",
                    "description": (
                        "Working directory for start. Defaults to the "
                        "project directory."
                    ),
                },
                "description": {
                    "type": "string",
                    "description": "Short label shown in the sidebar",
                },
                "shell": {
                    "type": "string",
                    "enum": list(SHELL_ALIAS_ENUM),
                    "description": SHELL_ALIAS_DESCRIPTION,
                },
                "id": {
                    "type": "string",
                    "pattern": "^bgp",
                    "description": (
                        "Required for status, logs, stop, and restart"
                    ),
                },
                "ready": {
                    "type": "object",
                    "description": "Optional readiness probe for start",
                    "properties": {
                        "pattern": {"type": "string"},
                        "port": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 65535,
                        },
                        "timeout": {
                            "type": "integer",
                            "minimum": 1,
                        },
                    },
                },
            },
        },
    },
}

# ---------------------------------------------------------------------------
# Cross-field validation
# ---------------------------------------------------------------------------


def validate_params(params: dict) -> None:
    """Validate cross-field constraints for the ``background_process`` tool.

    Raises:
        ValueError: if a required field is missing for the given action.
    """
    action = params.get("action", "")

    if action == "start":
        if not (params.get("command") or "").strip():
            raise ValueError("command is required when action is start")
        return

    if action == "list":
        return

    # status / logs / stop / restart all require id
    if not params.get("id"):
        raise ValueError(
            "id is required when action is status, logs, stop, or restart"
        )
