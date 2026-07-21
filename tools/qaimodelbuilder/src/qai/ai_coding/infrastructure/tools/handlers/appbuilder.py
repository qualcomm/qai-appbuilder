# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""App Builder Model Pack tool handler stub (``appbuilder_run``).

This stub returns ``ok=False`` with the stable ``error_code``
``ai_coding.tool.appbuilder_not_wired`` so callers can detect the
unwired state. The Sticky Worker that backs this tool lives outside
the ai_coding context (in ``qai.app_builder``); when an application
root composes the Sticky Worker and registers a real handler under
the same name on the bridge, this stub is superseded without any
signature change here.
"""

from __future__ import annotations

from typing import Any

from qai.ai_coding.application.ports import FileGuardPort
from qai.ai_coding.infrastructure.tools.errors import ToolError


async def tool_appbuilder_run(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,  # noqa: ARG001
) -> dict[str, Any]:
    model_id = args.get("modelId")
    inputs = args.get("inputs")
    if not isinstance(model_id, str) or not model_id:
        raise ToolError(
            "appbuilder_run: 'modelId' argument is required and must be string"
        )
    if not isinstance(inputs, dict):
        raise ToolError(
            "appbuilder_run: 'inputs' argument is required and must be object"
        )
    return {
        "ok": False,
        "error_code": "ai_coding.tool.appbuilder_not_wired",
        "message": (
            "appbuilder_run is not wired in this build — no Sticky "
            "Worker handler is registered for the appbuilder_run tool. "
            "Callers should fall back to the in-house model_catalog "
            "runner or skip the call."
        ),
        "modelId": model_id,
        "params": args.get("params") or {},
        "variantId": args.get("variantId"),
    }
