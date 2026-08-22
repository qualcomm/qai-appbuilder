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
    subtitle_param_description,
)

__all__ = [
    "BACKGROUND_PROCESS_TOOL_DESCRIPTION",
    "BACKGROUND_PROCESS_TOOL_SCHEMA",
    "DESCRIPTION_PARAM_DESCRIPTION",
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
#
# 2026-08-09: that claim used to have a hole — the shared set was six
# TOOL-level fragments, so the ``description`` PARAMETER was NOT covered and
# this file's wording had drifted to "Short label shown in the sidebar" while
# ``exec``'s said the four things that actually elicit a label. The 7th
# fragment (:func:`subtitle_param_description`) closes it; see
# ``DESCRIPTION_PARAM_DESCRIPTION`` below.

BACKGROUND_PROCESS_TOOL_DESCRIPTION: str = (
    "Supervised long-running processes for this session. `start` returns "
    "immediately with an `id`; the process keeps running while you do other "
    "work.\n\n"
    "# When to use it\n"
    "- A service, watcher, debuggable browser, or anything that must "
    "OUTLIVE the call MUST start here, not through `exec` (`npm run dev`, "
    "`vite`, `bun --watch`, `chrome --remote-debugging-port`).\n"
    "- Nothing else to do while it runs? Use `exec` (`timeout=0` waits with "
    "no timeout, output streams live on the tool card). Slowness alone is "
    "NEVER the reason to come here — `exec` handles long work on its own.\n"
    "- NEVER background through the shell (`&`, `nohup`, `Start-Process`). "
    "Processes started here are tracked, shown in the sidebar, and stopped "
    "with the session.\n\n"
    "# Collecting the outcome\n"
    "When a process ENDS, its outcome is delivered to you automatically — "
    "you do NOT need to poll for it. What this tool is for is the rest: "
    "readiness, mid-flight inspection, and shutting things down. A process "
    "you started stays YOUR responsibility until you have observed its "
    "outcome or stopped it — a service left running past the work that "
    "needed it is a leak.\n"
    "- **`wait`** (`id`, optional `timeout_s`, default 30, cap 120): blocks "
    "until the process settles, then returns its final status AND its "
    "output together. Use it ONLY when you are genuinely blocked with "
    "nothing else to do; `settled: false` means only that your budget "
    "elapsed, so end your turn rather than re-issuing `wait` in a loop. "
    "NEVER poll `status` in a loop either.\n"
    "- **`ready`** on `start` (`pattern` regex on output, or TCP `port`, "
    "plus `timeout`): makes `start` itself return only once the process is "
    "actually usable. Readiness MUST be observed — process creation alone "
    "is not readiness.\n"
    "- Report what you OBSERVED (exit status, real output) plus the `id` — "
    "never just \"I started it\".\n"
    "- **`stop`** it once it is no longer needed. NEVER end your work with "
    "an unexamined process still in flight and the user left to ask.\n\n"
    "# Actions\n"
    "- **`start`**: needs `command`; optional `workdir` / `description` / "
    "`shell` / `ready`. Never pass `id`.\n"
    "- **`list`**: this session's processes.\n"
    "- **`status`**: one process by `id` (running vs settled + exit code).\n"
    "- **`logs`**: retained output tail by `id`.\n"
    "- **`wait`**: block for the outcome (see above).\n"
    "- **`stop`**: terminate the process tree.\n"
    "- **`restart`**: stop + re-spawn with the original command.\n"
    "`id` applies to status / logs / wait / stop / restart only.\n\n"
    "Optional `shell` picks the interpreter (same aliases as `exec`); "
    "default `auto` picks pwsh/powershell/cmd on Windows, `shell='sh'` runs "
    "POSIX / bash scripts.\n\n"
    f"{AVAILABLE_TOOLS_SECTION}\n\n"
    f"{PREFER_DEDICATED_TOOLS_SECTION}\n\n"
    f"{WORKDIR_GUIDANCE_SECTION}\n\n"
    f"{SHELL_NOTES_SECTION}"
)

#: Wording for the ``description`` PARAMETER, from the shared 7th fragment.
#:
#: Same skeleton as ``exec`` (who reads it → shape → examples →
#: encouragement); only the tool-specific holes differ. The examples name
#: long-running processes rather than one-shot commands because that is what
#: this tool starts — shown "Run unit tests" a model writes labels for
#: commands, and the dev servers and watchers that actually live here would
#: get mis-shaped ones. The surface names the process sidebar as well as the
#: card (a long-running process is listed there for its whole life, which is
#: the tool card's audience plus everyone who looks later), and the
#: encouragement covers EVERY start: a tracked process outlives the call, so
#: unlike a trivial one-liner there is no such thing as one not worth naming.
#:
#: Deliberately NOT added to ``required``: ``exec`` also requires only
#: ``["command"]`` and still gets labels reliably, so clear wording is doing
#: the work and a hard requirement would only turn an omission into a
#: tool-call error. The manager's command-line fallback
#: (``manager._derive_description``) covers whatever still arrives empty.
DESCRIPTION_PARAM_DESCRIPTION: str = subtitle_param_description(
    examples=(
        "Dev server for web UI",
        "Watch and rebuild frontend",
        "Chrome with remote debugging",
    ),
    surface="the tool-card subtitle in the chat and the process sidebar",
    encourage="every process you start",
)

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
                    "enum": [
                        "start", "list", "status", "logs", "wait",
                        "stop", "restart",
                    ],
                    "description": "Operation to perform",
                },
                "timeout_s": {
                    "type": "number",
                    "minimum": 1,
                    "description": (
                        "For `wait` only: how many seconds to block waiting "
                        "for the process to finish (default 30, capped at "
                        "120). `settled: false` means only that this budget "
                        "elapsed — the process is still running and its "
                        "outcome reaches you on its own, so end your turn "
                        "rather than waiting again."
                    ),
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
                    "description": DESCRIPTION_PARAM_DESCRIPTION,
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

    # status / logs / wait / stop / restart all require id
    if not params.get("id"):
        raise ValueError(
            "id is required when action is status, logs, stop, or restart"
        )
