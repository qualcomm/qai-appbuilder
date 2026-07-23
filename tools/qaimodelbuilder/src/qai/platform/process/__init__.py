# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Process-execution shared kernel (PR-041).

This sub-package is the cross-context home of the :class:`ProcessRunnerPort`
contract and its real subprocess-backed adapter. It lives under
``qai.platform.*`` (and not under any single bounded context) on purpose:

* ``qai.security`` exposes it as ``SecurityServices.process_runner`` (a
  plain subprocess runner) for policy-gated command execution + audit.
* ``qai.app_builder`` (PR-045) will reuse the **same** Port to launch
  user-built workers / runs without importing security.

The ``context-isolation`` import-linter contract forbids cross-context
imports; placing the Port here lets multiple contexts share it through
the platform shared kernel without any rule violation.

Public surface (curated)
------------------------

* :class:`ProcessRunnerPort` -- Protocol describing async streaming
  subprocess execution (stdout / stderr / completion).
* :class:`ProcessExecutionRequest` -- frozen value object capturing the
  caller's intent (``argv`` + ``cwd`` + ``env`` + ``timeout_s`` +
  ``output_byte_cap``).
* :class:`ProcessFrame` (and its sub-types) -- the discriminated union
  of frames yielded by the runner.
* :class:`ProcessExitStatus` -- exit-code + signal + truncation flags.
* :class:`SubprocessProcessRunner` -- real adapter using
  :func:`asyncio.create_subprocess_exec`. Available via the
  :mod:`qai.platform.process.subprocess_runner` module so test code that
  only needs the Port doesn't pay the import cost of the runtime
  adapter.
"""

from __future__ import annotations

from .arch import current_arch
from .bundled_path import (
    bundled_bin_dirs,
    prepend_bundled_paths,
    prepend_bundled_paths_to_process,
)
from .kill_group import ProcessKillGroup
from .ports import (
    ProcessExecutionRequest,
    ProcessExitStatus,
    ProcessFrame,
    ProcessFrameKind,
    ProcessRunnerPort,
    ProcessStartedFrame,
    ProcessStderrFrame,
    ProcessStdoutFrame,
    ProcessTerminatedFrame,
)
from .spawn_flags import no_window_creationflags
from .tree_kill import best_effort_tree_kill, terminate_process_tree

__all__ = [
    "current_arch",
    "ProcessExecutionRequest",
    "ProcessExitStatus",
    "ProcessFrame",
    "ProcessFrameKind",
    "ProcessKillGroup",
    "ProcessRunnerPort",
    "ProcessStartedFrame",
    "ProcessStderrFrame",
    "ProcessStdoutFrame",
    "ProcessTerminatedFrame",
    "best_effort_tree_kill",
    "bundled_bin_dirs",
    "no_window_creationflags",
    "prepend_bundled_paths",
    "prepend_bundled_paths_to_process",
    "terminate_process_tree",
]
