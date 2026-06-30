# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Deeper CPU-only smoke tests for the qai_appbuilder wheel.

Goes beyond `test_package_smoke.py` (which only checks Python-level imports
and constants) by exercising the pybind11 native extension and the QNN
backend DLL discovery path. None of these tests require a Snapdragon NPU
or a precompiled .bin model — they run on any ARM64 Windows host that
has the wheel installed, including GitHub-hosted windows-11-arm runners.

What is covered:
- pybind C++ extension loads and exposes set_log_level / set_profiling_level
- QNNConfig.Config(runtime=CPU) finds bundled QnnCpu.dll + QnnSystem.dll
- That same call routes through the pybind layer into native code
  (set_log_level + set_profiling_level are invoked at the end of Config())

What is NOT covered (and cannot be on a non-NPU runner):
- Loading a .bin context binary (HTP-compiled, fails on CPU runtime)
- Any actual inference
- HTP-only paths: PerfProfile, device infrastructure
"""

from __future__ import annotations

import importlib


def test_pybind_extension_exposes_native_symbols() -> None:
    """The compiled qai_appbuilder.appbuilder pybind module must load and
    expose the global-state functions that don't need a model.
    """
    appbuilder = importlib.import_module("qai_appbuilder.appbuilder")

    for symbol in ("set_log_level", "set_profiling_level"):
        assert hasattr(appbuilder, symbol), f"pybind module is missing {symbol}"
        assert callable(getattr(appbuilder, symbol)), f"{symbol} is not callable"


def test_set_log_level_calls_through_pybind() -> None:
    """Calling set_log_level should round-trip through C++ without raising.

    This proves the native .pyd is loadable on this architecture and that
    Python -> C++ argument marshalling for the simplest exposed function
    is intact.
    """
    from qai_appbuilder import LogLevel

    LogLevel.SetLogLevel(LogLevel.ERROR, "None")


def test_qnn_cpu_backend_dll_is_bundled_and_loadable() -> None:
    """Configuring with Runtime.CPU must succeed on any platform.

    QNNConfig.Config() resolves the wheel-bundled qai_appbuilder/libs/ dir
    (the qnn_lib_path argument was removed upstream — the wheel-bundled
    libs/ is the only supported location). It then asserts QnnCpu.dll
    and QnnSystem.dll exist, and finally invokes the pybind
    set_log_level + set_profiling_level functions.

    A pass therefore proves four things at once:
    1. the wheel packaged QnnCpu.dll
    2. the wheel packaged QnnSystem.dll
    3. the pybind extension is callable
    4. the bundled libs/ path resolution logic is correct on this runner
    """
    from qai_appbuilder import LogLevel, ProfilingLevel, QNNConfig, Runtime

    QNNConfig.Config(
        runtime=Runtime.CPU,
        log_level=LogLevel.ERROR,
        profiling_level=ProfilingLevel.OFF,
    )
