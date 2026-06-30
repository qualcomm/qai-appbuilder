# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Smoke tests for the installed qai_appbuilder wheel.

These tests intentionally avoid loading a real model so they can run in CI after
building and installing the wheel. The wheel build itself validates that the
native extension can be compiled and linked against the configured QAIRT/QNN SDK.
"""

from __future__ import annotations

import importlib


def test_package_imports() -> None:
    """Verify the top-level package can be imported.

    Args:
        None.

    Returns:
        None.
    """
    module = importlib.import_module("qai_appbuilder")

    assert hasattr(module, "__version__")


def test_qnn_api_symbols_are_exported() -> None:
    """Verify commonly used QNN Python API symbols are exported.

    Args:
        None.

    Returns:
        None.
    """
    module = importlib.import_module("qai_appbuilder")

    expected_symbols = [
        "QNNConfig",
        "QNNContext",
        "Runtime",
        "LogLevel",
        "ProfilingLevel",
        "PerfProfile",
        "DataType",
    ]

    for symbol in expected_symbols:
        assert hasattr(module, symbol), f"Missing exported symbol: {symbol}"


def test_qnn_constant_values() -> None:
    """Verify stable public constant values used by samples and applications.

    Args:
        None.

    Returns:
        None.
    """
    module = importlib.import_module("qai_appbuilder")

    assert module.Runtime.CPU == "Cpu"
    assert module.Runtime.HTP == "Htp"
    assert module.DataType.FLOAT == "float"
    assert module.DataType.NATIVE == "native"
    assert module.PerfProfile.DEFAULT == "default"
    assert module.PerfProfile.HIGH_PERFORMANCE == "high_performance"
    assert module.PerfProfile.BURST == "burst"