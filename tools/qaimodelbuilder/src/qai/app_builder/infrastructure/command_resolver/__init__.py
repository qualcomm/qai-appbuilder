# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pack-runner command resolution (PR-302).

Replaces PR-045's ``_default_resolver = lambda r, m: None`` placeholder
with a real ``(model_id, variant_id) → ProcessExecutionRequest`` map.

Two pieces:

* :class:`RunnerSpec` — the per-Pack registry entry (script path, cwd,
  env extras, timeout). PR-303 will populate the registry from the
  ``manifest.json`` schema v1; PR-302 ships an in-memory registry that
  callers (DI / tests) build directly.

* :class:`InMemoryRunnerCommandRegistry` — Protocol-implementing
  registry; turns ``(run, model)`` into a fully-formed
  :class:`ProcessExecutionRequest`.

* :class:`PythonInterpreterResolver` — pluggable resolver of the Python
  interpreter to use (default: ``sys.executable``; ``QAIRTEnvFile``
  resolver reads ``config/qairt_env.json`` for the legacy parity path
  used by the production app — but ``config/qairt_env.json`` is opted
  out of release artifacts in v2.7 §1, so by default we read it from
  the data directory under ``DataPaths.runner_dir`` if the file
  exists, else fall back to ``sys.executable``).

The resolver glue is wired into :class:`ProcessBackedAppRunner` via
the new ``CommandResolver`` factory in
:mod:`qai.app_builder.infrastructure.process_runner`.
"""

from __future__ import annotations

from .interpreter_resolver import (
    PythonInterpreterResolver,
    QairtEnvJsonResolver,
    SysExecutableResolver,
)
from .registry import (
    InMemoryRunnerCommandRegistry,
    RunnerCommandRegistryPort,
    RunnerSpec,
    build_command_resolver,
)

__all__ = [
    "InMemoryRunnerCommandRegistry",
    "PythonInterpreterResolver",
    "QairtEnvJsonResolver",
    "RunnerCommandRegistryPort",
    "RunnerSpec",
    "SysExecutableResolver",
    "build_command_resolver",
]
