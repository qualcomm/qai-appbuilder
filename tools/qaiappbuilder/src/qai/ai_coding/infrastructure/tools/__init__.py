# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Production tool implementations for the ``ai_coding`` bounded context.

PR-101 ports the 9 production tools from the legacy ``backend/tools/``
package into the new ``src/qai/ai_coding/infrastructure/tools/`` namespace
under Clean Architecture rules:

* every tool is an ``async`` callable matching ``ToolHandler``
  (``Callable[[dict], Awaitable[dict]]``) so it plugs straight into
  :class:`qai.ai_coding.adapters.tool_bridge.RegistryBackedToolBridge`;
* security checks are delegated to the injected
  :class:`qai.ai_coding.application.ports.FileGuardPort`, never to
  ``qai.security`` directly (that would break the
  ``context-isolation`` importlinter contract);
* optional pre/post processing (always_exclude, snapshot redirect,
  result truncation) goes through the
  :class:`qai.ai_coding.application.ports.FileBrokerPort`.

The tool functions return ``dict[str, object]`` (not ``str``) so the
:class:`ToolBridgePort` envelope can carry both successful results and
structured failures.  The legacy ``str`` formatting used by Claude Code
/ OpenCode lives in the route layer instead, where it can be tested
independently of the security plumbing.
"""

from __future__ import annotations

from qai.ai_coding.infrastructure.tools.errors import ToolGuardDenied
from qai.ai_coding.infrastructure.tools.file_broker import NoopFileBroker
from qai.ai_coding.infrastructure.tools.file_guard import NoopFileGuard
from qai.ai_coding.infrastructure.tools.registry import build_default_tool_handlers

__all__ = [
    "NoopFileBroker",
    "NoopFileGuard",
    "ToolGuardDenied",
    "build_default_tool_handlers",
]
