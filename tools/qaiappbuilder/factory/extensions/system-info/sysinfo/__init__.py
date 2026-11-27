# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""System Info extension: one read-only tool, ``system_info``, and two buttons.

Nothing here changes the machine: it only reads CPU, memory, disk and network
state through ``psutil``.
"""

from __future__ import annotations

import json
from typing import Any

from qai.ext import ActionCall, ActionResult, ExtensionContext, ToolCall

from .collect import SECTIONS, collect

_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {"type": "string", "enum": list(SECTIONS)},
            "description": (
                "Which parts to read. Omit for the overview (system, cpu, memory, disk, network "
                "and the COUNTS of connections and open ports). Name connections or listening "
                "to get their rows. "
                "system = host name, OS, uptime, battery; cpu; memory; disk = space per drive; "
                "network = interfaces, addresses, traffic; connections = established "
                "connections with remote address and process; listening = open ports and "
                "which are reachable from the network."
            ),
        }
    },
}

_DESCRIPTION = (
    "Read facts about THIS computer (read-only): CPU model, cores and usage; memory and swap; "
    "disk space per drive; network interfaces, IP/MAC addresses and traffic; established network "
    "connections; and listening ports with whether each is reachable from the network or only "
    "from this machine. Use it instead of guessing. Returns JSON."
)

_PROMPTS = {
    "overview": (
        "Give me an overview of this computer: CPU, memory, disk space and network status. "
        "Use the system_info tool and keep it short; point out anything that looks tight "
        "(high memory use, a nearly full drive)."
    ),
    "exposed_ports": (
        "Which ports on this computer are open to the network? Use the system_info tool with "
        "the listening section, group them by process, and say which ones are only local."
    ),
}


def activate(ctx: ExtensionContext) -> None:
    def system_info(call: ToolCall) -> str:
        raw = call.arguments.get("sections")
        sections = [str(s) for s in raw] if isinstance(raw, list) else None
        try:
            data = collect(sections)
        except ImportError as exc:  # psutil missing: say so instead of crashing the turn
            return json.dumps({"error": f"psutil is not available: {exc}"})
        return json.dumps(data, ensure_ascii=False, default=str)

    ctx.register_tool("system_info", system_info, description=_DESCRIPTION, parameters=_PARAMETERS)

    def ask(prompt: str):
        def handler(_call: ActionCall) -> ActionResult:
            return ActionResult(prompt=prompt)

        return handler

    for action_id, prompt in _PROMPTS.items():
        ctx.register_action(action_id, ask(prompt))
