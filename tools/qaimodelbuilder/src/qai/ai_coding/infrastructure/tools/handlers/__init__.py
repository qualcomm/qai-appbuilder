# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""10 production tool handlers (PR-101 / L1 lane).

Each public ``tool_*`` function is an async callable matching
:class:`qai.ai_coding.adapters.tool_bridge.ToolHandler` —
``Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]`` — and goes
through :class:`FileGuardPort` for security checks plus
:class:`FileBrokerPort` for optional pre/post processing.

The 10 tools:

* ``read``           — read file contents (text + line truncation)
* ``list``           — list one directory level (paginated, dirs + files)
* ``write``          — overwrite a file with given content
* ``edit``           — apply unique-text replacements to a file
* ``glob``           — find files matching a glob pattern
* ``grep``           — regex search across files
* ``exec``           — run a shell command on the host OS
* ``webfetch``       — fetch + extract a URL (markdown / text)
* ``apply_patch``    — atomic multi-file Add/Update/Delete patch
* ``appbuilder_run`` — currently returns ``ok=False`` with the
  stable ``error_code`` ``ai_coding.tool.appbuilder_not_wired`` so
  callers can detect the unwired state. The Sticky Worker that backs
  this tool lives outside the ai_coding context (in
  ``qai.app_builder``); when an application root composes the Sticky
  Worker and registers a real handler under the same name on the
  bridge, this stub is superseded without any signature change here.

Result envelopes
----------------
Every handler returns a ``dict`` with at minimum:

* ``ok: bool`` — success flag
* ``message: str`` — human-readable summary (used by Claude Code /
  OpenCode tool result formatting)

Successful handlers add tool-specific fields (``content``,
``files``, ``matches``, ``stdout``, ``exit_code`` etc.).  Failure
handlers add ``error_code: str`` and skip ``message`` if a stable
machine-readable code is what the route layer needs.

Path resolution
---------------
Tools accept absolute and relative paths.  Relative paths are
resolved against ``Path.cwd()`` — the application root (or the agent
harness in PR-108) is responsible for setting the cwd to the active
session's workspace before invoking the bridge.

Module layout
-------------
The handlers were split out of a single file into per-family
submodules (``read_write`` / ``search`` / ``exec`` / ``web`` /
``patch`` / ``appbuilder`` + shared ``_shared``).  This package
``__init__`` re-exports every public symbol so the import path
``qai.ai_coding.infrastructure.tools.handlers`` is unchanged for all
callers.
"""

from __future__ import annotations

from qai.ai_coding.infrastructure.tools.errors import ToolError, ToolGuardDenied
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    CLOUD_TOOL_DESCRIPTION_OVERRIDES,
    READ_LINE_TRUNCATED_SUFFIX,
    READ_MAX_BYTES,
    READ_MAX_LINE_LENGTH,
    READ_MAX_LINES,
    TOOL_SCHEMAS,
    ToolOutputThresholds,
    get_conversation_scope,
    get_project_skip_dirs,
    get_tool_output_thresholds,
    get_workspace_base,
    make_exec_advice,
    make_file_broker_advice,
    make_glob_advice,
    make_grep_advice,
    make_line_truncated_suffix,
    make_read_advice,
    make_truncation_advice,
    make_webfetch_advice,
    make_web_search_advice,
    expand_skill_placeholders,
    get_app_root,
    reset_app_root,
    reset_conversation_scope,
    reset_workspace_base,
    set_app_root,
    set_conversation_scope,
    set_project_skip_dirs,
    set_tool_output_thresholds,
    set_tool_result_store_roots,
    set_workspace_base,
    strip_ansi_escapes,
)
from qai.ai_coding.infrastructure.tools.handlers.appbuilder import (
    tool_appbuilder_run,
)
from qai.ai_coding.infrastructure.tools.handlers.exec import tool_exec
from qai.ai_coding.infrastructure.tools.handlers.patch import tool_apply_patch
from qai.ai_coding.infrastructure.tools.handlers.read_write import (
    tool_edit,
    tool_list,
    tool_read,
    tool_write,
)
from qai.ai_coding.infrastructure.tools.handlers.search import (
    tool_glob,
    tool_grep,
)
from qai.ai_coding.infrastructure.tools.handlers.web import (
    get_ssl_verify,
    set_global_proxy,
    set_ssl_verify,
    tool_webfetch,
)
from qai.ai_coding.infrastructure.tools.handlers.web_search import (
    tool_web_search,
)

__all__ = [
    "CLOUD_TOOL_DESCRIPTION_OVERRIDES",
    "READ_LINE_TRUNCATED_SUFFIX",
    "READ_MAX_BYTES",
    "READ_MAX_LINE_LENGTH",
    "READ_MAX_LINES",
    "TOOL_SCHEMAS",
    "ToolError",
    "ToolGuardDenied",
    "ToolOutputThresholds",
    "expand_skill_placeholders",
    "get_app_root",
    "get_conversation_scope",
    "get_project_skip_dirs",
    "get_ssl_verify",
    "get_tool_output_thresholds",
    "get_workspace_base",
    "make_exec_advice",
    "make_file_broker_advice",
    "make_glob_advice",
    "make_grep_advice",
    "make_line_truncated_suffix",
    "make_read_advice",
    "make_truncation_advice",
    "make_webfetch_advice",
    "make_web_search_advice",
    "reset_app_root",
    "reset_conversation_scope",
    "reset_workspace_base",
    "set_app_root",
    "set_conversation_scope",
    "set_global_proxy",
    "set_project_skip_dirs",
    "set_ssl_verify",
    "set_tool_output_thresholds",
    "set_tool_result_store_roots",
    "set_workspace_base",
    "strip_ansi_escapes",
    "tool_apply_patch",
    "tool_appbuilder_run",
    "tool_edit",
    "tool_exec",
    "tool_glob",
    "tool_grep",
    "tool_list",
    "tool_read",
    "tool_webfetch",
    "tool_web_search",
    "tool_write",
]
