# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Desktop-control capability — platform shared kernel.

Provides the ``computer`` tool: native desktop screenshot (spanning every
monitor by default) plus mouse / keyboard input, executed inside an isolated
worker subprocess so a Win32 crash or block never touches the main API process.

Public surface:

* :data:`COMPUTER_TOOL_SCHEMA` / :func:`validate_params` — the LLM tool
  schema + fail-closed validation.
* :func:`handle_computer` / :func:`computer_approval` /
  :func:`format_approval_details` — the tool handler + approval helpers.
* :class:`ComputerSupervisor` + :class:`DesktopControllerPort` — the
  async controller and its Protocol.
* owner-registry helpers (:func:`register_computer_controller` /
  :func:`release_computer_sessions_for_owner`) and the worker smoke check
  (:func:`smoke_test_computer_worker`).

Cross-context discipline: the handler depends only on
:class:`DesktopControllerPort`; the ``apps/api`` bridge composes the
controller + image sink. Win32 / subprocess details never leak upward.
"""

from __future__ import annotations

from .ports import DesktopControllerPort, DesktopSessionPort
from .supervisor import (
    ComputerSupervisor,
    register_computer_controller,
    release_computer_sessions_for_owner,
    smoke_test_computer_worker,
)
from .tool_handlers import (
    computer_approval,
    format_approval_details,
    handle_computer,
)
from .tool_schemas import (
    COMPUTER_TOOL_DESCRIPTION,
    COMPUTER_TOOL_SCHEMA,
    validate_params,
)
from .types import (
    Action,
    Capabilities,
    Capture,
    DesktopError,
    Display,
    Point,
    SessionOptions,
    scroll_steps,
)

__all__ = [
    "COMPUTER_TOOL_DESCRIPTION",
    "COMPUTER_TOOL_SCHEMA",
    "Action",
    "Capabilities",
    "Capture",
    "ComputerSupervisor",
    "DesktopControllerPort",
    "DesktopError",
    "DesktopSessionPort",
    "Display",
    "Point",
    "SessionOptions",
    "computer_approval",
    "format_approval_details",
    "handle_computer",
    "register_computer_controller",
    "release_computer_sessions_for_owner",
    "scroll_steps",
    "smoke_test_computer_worker",
    "validate_params",
]
