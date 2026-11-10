# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""GenieAPIService output is kept in ``data/logs/genie_service.log``.

A real child process stands in for the service: the Python interpreter, run
under the service's exe name, prints a few lines and exits with a code. The
file must hold the start record, every output line and the exit record, and
``backend.log`` (the root logger) must get one ``exited_unexpectedly`` line
with the exit code.
"""

from __future__ import annotations

import logging
import shutil
import sys
import time
from pathlib import Path

import pytest

from qai.model_runtime.domain.enums import ServiceState
from qai.model_runtime.infrastructure.process_service import (
    ProcessBackedInferenceService,
)

pytestmark = [pytest.mark.unit]

_SCRIPT = """
import sys
print("genie: loading model", flush=True)
print("genie: QNN error 1002 - device lost", flush=True)
sys.exit(int(sys.argv[1]))
"""


def _make_service(tmp_path: Path, log_path: Path | None) -> tuple[ProcessBackedInferenceService, Path]:
    install = tmp_path / "svc"
    install.mkdir()
    exe = install / ("FakeGenie.exe" if sys.platform == "win32" else "FakeGenie")
    shutil.copy2(sys.executable, exe)  # a runnable binary under the service's name
    if sys.platform == "win32":
        # a copied python.exe needs its DLLs / stdlib next to it
        base = Path(sys.base_prefix)
        for dll in base.glob("*.dll"):
            shutil.copy2(dll, install / dll.name)
        (install / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
    script = tmp_path / "fake_genie.py"
    script.write_text(_SCRIPT, encoding="utf-8")
    svc = ProcessBackedInferenceService(
        install_dir=str(install),
        exe_name=exe.name,
        output_log_path=log_path,
        output_log_max_bytes=1024 * 1024,
        output_log_backup_count=2,
    )
    return svc, script


def _wait_for_exit(svc: ProcessBackedInferenceService, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        thread = svc._log_thread
        if thread is not None and not thread.is_alive():
            return
        time.sleep(0.05)
    raise AssertionError("fake service did not exit")


def _flush(svc: ProcessBackedInferenceService) -> None:
    out = svc._output_logger
    if out is not None:
        for h in out.handlers:
            h.flush()


def test_output_and_unexpected_exit_are_recorded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    log_path = tmp_path / "logs" / "genie_service.log"
    svc, script = _make_service(tmp_path, log_path)
    caplog.set_level(logging.INFO, logger="qai.model_runtime.infrastructure.process_service")

    svc._do_start([str(script), "3"], 18999, "qwen3-test")
    _wait_for_exit(svc)
    _flush(svc)

    text = log_path.read_text(encoding="utf-8")
    assert "service started pid=" in text
    assert "model=qwen3-test" in text
    assert "genie: loading model" in text
    assert "genie: QNN error 1002 - device lost" in text
    assert "service exited" in text
    assert "exit_code=3 (0x00000003)" in text
    assert "expected=False" in text

    unexpected = [r for r in caplog.records if "genie_service.exited_unexpectedly" in r.getMessage()]
    assert len(unexpected) == 1
    msg = unexpected[0].getMessage()
    assert "exit_code=3" in msg
    assert "device lost" in msg  # last output lines are quoted
    assert unexpected[0].levelno == logging.WARNING

    # the status poll observes the same exit: still exactly one record
    svc._refresh_state()
    assert svc._state is ServiceState.ERROR
    _flush(svc)
    assert log_path.read_text(encoding="utf-8").count("service exited") == 1
    assert len([r for r in caplog.records if "genie_service.exited" in r.getMessage()]) == 1


def test_raw_output_does_not_reach_backend_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    svc, script = _make_service(tmp_path, tmp_path / "genie_service.log")
    caplog.set_level(logging.DEBUG)

    svc._do_start([str(script), "0"], 18998, None)
    _wait_for_exit(svc)

    # each output line goes to the file only; backend.log gets the exit record,
    # which quotes the LAST lines on purpose (so a crash is explained in place)
    raw = [r for r in caplog.records if r.getMessage().strip() == "genie: loading model"]
    assert raw == []
    assert not any(
        "genie: loading model" in r.getMessage()
        for r in caplog.records
        if "genie_service.exited" not in r.getMessage()
    )


def test_no_output_log_path_keeps_buffer_only(tmp_path: Path) -> None:
    svc, script = _make_service(tmp_path, None)

    svc._do_start([str(script), "0"], 18997, None)
    _wait_for_exit(svc)

    assert svc._output_logger is None
    assert any("loading model" in line for line in svc._log_buffer)
    assert not list(tmp_path.glob("**/genie_service.log"))
