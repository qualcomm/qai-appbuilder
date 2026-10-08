# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""CI-safe tests for ExecuTorch .pte dispatch and validation."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _model_file(tmp_path: Path) -> str:
    path = tmp_path / "model.pte"
    path.write_bytes(b"not a valid program")
    return str(path)


@pytest.fixture
def executorch_context_module():
    module_names = (
        "qai_appbuilder",
        "qai_appbuilder.appbuilder",
        "qai_appbuilder.qnncontext",
        "qai_appbuilder.geniecontext",
        "qai_appbuilder.onnxwrapper",
    )
    saved_modules = {name: sys.modules.get(name) for name in module_names}
    for name in module_names:
        sys.modules.pop(name, None)
    sys.modules["qai_appbuilder.appbuilder"] = ModuleType("qai_appbuilder.appbuilder")
    try:
        yield importlib.import_module("qai_appbuilder.qnncontext")
    finally:
        for name in module_names:
            sys.modules.pop(name, None)
        for name, module in saved_modules.items():
            if module is not None:
                sys.modules[name] = module


def test_executorch_dispatch_reports_disabled_feature(tmp_path: Path, monkeypatch, executorch_context_module) -> None:
    qnncontext = executorch_context_module

    monkeypatch.delattr(qnncontext.appbuilder, "ExecuTorchContext", raising=False)
    with pytest.raises(RuntimeError, match="ExecuTorch support is disabled"):
        qnncontext.QNNContext(model_path=_model_file(tmp_path))


def test_executorch_rejects_qnn_only_constructor_options(tmp_path: Path, monkeypatch, executorch_context_module) -> None:
    qnncontext = executorch_context_module

    class FakeContext:
        def __init__(self, model_name, model_path, backend_lib_path=""):
            self.model_path = model_path

        def getProviderMode(self):
            return "xnnpack"

        def getOutputShapes(self, graph_index=0):
            return []

        def release(self):
            return None

    monkeypatch.setattr(qnncontext.appbuilder, "ExecuTorchContext", FakeContext, raising=False)
    with pytest.raises(ValueError, match="is_async"):
        qnncontext.QNNContext(model_path=_model_file(tmp_path), is_async=True)
    with pytest.raises(ValueError, match="deviceID"):
        qnncontext.QNNContext(model_path=_model_file(tmp_path), deviceID=1)
    with pytest.raises(ValueError, match="coreIdsStr"):
        qnncontext.QNNContext(model_path=_model_file(tmp_path), coreIdsStr="0")
    with pytest.raises(ValueError, match="enable_graphs"):
        qnncontext.QNNContext(model_path=_model_file(tmp_path), enable_graphs=["forward"])


def test_executorch_dispatch_uses_native_binding(tmp_path: Path, monkeypatch, executorch_context_module) -> None:
    qnncontext = executorch_context_module

    class FakeContext:
        def __init__(self, model_name, model_path, backend_lib_path=""):
            self.model_path = model_path

        def getProviderMode(self):
            return "xnnpack"

        def getOutputShapes(self, graph_index=0):
            return []

        def release(self):
            return None

    monkeypatch.setattr(qnncontext.appbuilder, "ExecuTorchContext", FakeContext, raising=False)
    context = qnncontext.QNNContext(model_path=_model_file(tmp_path))
    try:
        assert context.isExecuTorchModel()
        assert context.getProviderMode() == "xnnpack"
    finally:
        context.release()
