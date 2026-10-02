# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""CI-safe tests for TFLite format dispatch and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from qai_appbuilder.qnncontext import QNNContext


def _model_file(tmp_path: Path) -> str:
    path = tmp_path / "model.tflite"
    path.write_bytes(b"not a valid model")
    return str(path)


def test_tflite_dispatch_reports_disabled_feature(tmp_path: Path, monkeypatch) -> None:
    """A default wheel must fail clearly rather than falling into QNN loading."""
    import qai_appbuilder.qnncontext as qnncontext

    monkeypatch.setattr(qnncontext.sys, "platform", "linux")
    monkeypatch.delattr(qnncontext.appbuilder, "TFLiteQnnContext", raising=False)
    with pytest.raises(RuntimeError, match="TFLite support is disabled"):
        QNNContext(model_path=_model_file(tmp_path))


def test_windows_tflite_dispatch_reports_disabled_cpu_feature(tmp_path: Path, monkeypatch) -> None:
    import qai_appbuilder.qnncontext as qnncontext

    monkeypatch.setattr(qnncontext.sys, "platform", "win32")
    monkeypatch.delattr(qnncontext.appbuilder, "TFLiteCpuContext", raising=False)
    with pytest.raises(RuntimeError, match="Windows CPU TFLite support is disabled"):
        QNNContext(model_path=_model_file(tmp_path))


def test_tflite_rejects_qnn_only_constructor_options(tmp_path: Path, monkeypatch) -> None:
    import qai_appbuilder.qnncontext as qnncontext

    monkeypatch.setattr(qnncontext.sys, "platform", "linux")
    monkeypatch.setattr(qnncontext.appbuilder, "TFLiteQnnContext", object(), raising=False)
    with pytest.raises(ValueError, match="is_async"):
        QNNContext(model_path=_model_file(tmp_path), is_async=True)

    with pytest.raises(ValueError, match="deviceID"):
        QNNContext(model_path=_model_file(tmp_path), deviceID=1)

    with pytest.raises(ValueError, match="coreIdsStr"):
        QNNContext(model_path=_model_file(tmp_path), coreIdsStr="0")

    with pytest.raises(ValueError, match="enable_graphs"):
        QNNContext(model_path=_model_file(tmp_path), enable_graphs=["graph"])


def test_windows_cpu_dispatch_uses_cpu_binding(monkeypatch, tmp_path: Path) -> None:
    import qai_appbuilder.qnncontext as qnncontext

    class FakeContext:
        def __init__(self, model_name, model_path):
            self.model_path = model_path

        def getProviderMode(self):
            return "cpu"

        def release(self):
            return None

    monkeypatch.setattr(qnncontext.sys, "platform", "win32")
    monkeypatch.setattr(qnncontext.appbuilder, "TFLiteCpuContext", FakeContext, raising=False)
    monkeypatch.delattr(qnncontext.appbuilder, "TFLiteQnnContext", raising=False)
    context = QNNContext(model_path=_model_file(tmp_path))
    try:
        assert context.getProviderMode() == "cpu"
    finally:
        context.release()


def test_tflite_rejects_nonzero_graph_index_before_native_call(tmp_path: Path, monkeypatch) -> None:
    import qai_appbuilder.qnncontext as qnncontext

    class FakeContext:
        calls = []

        def __init__(self, model_name, model_path):
            self.model_path = model_path

        def Inference(self, input_data, graph_index):
            self.calls.append(graph_index)
            return []

        def release(self):
            return None

    monkeypatch.setattr(qnncontext.sys, "platform", "win32")
    monkeypatch.setattr(qnncontext.appbuilder, "TFLiteCpuContext", FakeContext, raising=False)
    context = QNNContext(model_path=_model_file(tmp_path))
    try:
        with pytest.raises(ValueError, match="graphIndex must be 0"):
            context.Inference([], graphIndex=1)
        assert FakeContext.calls == []
    finally:
        context.release()


def test_onnx_detection_remains_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "model.onnx"
    path.write_bytes(b"not a valid model")
    context = object.__new__(QNNContext)
    context._is_onnx_model = True
    context._is_tflite_model = False
    assert context.isOnnxModel()
    assert not context.isTFLiteModel()
