# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``configure_logging(file_path=...)``: the backend log file.

Covers: records (structlog-native AND foreign stdlib) reach the file, the file
is plain text without ANSI colours, size rotation keeps N backups, a repeat
``configure_logging`` call does not stack file handlers, and an unopenable
path only warns.
"""

from __future__ import annotations

import io
import json
import logging
from pathlib import Path

import pytest

from qai.platform.logging import configure_logging, get_logger
from qai.platform.logging import config as log_config

pytestmark = [pytest.mark.unit]


def _managed_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, "_qai_managed", False)]


def _flush() -> None:
    for h in _managed_handlers():
        h.flush()


@pytest.fixture(autouse=True)
def _restore_logging():
    yield
    # back to a stderr-only setup so later tests do not write into tmp files
    for h in _managed_handlers():
        logging.getLogger().removeHandler(h)
        h.close()
    configure_logging(stream=io.StringIO())


def test_records_reach_file_as_plain_text(tmp_path: Path) -> None:
    log_file = tmp_path / "logs" / "backend.log"  # parent does not exist yet
    configure_logging(stream=io.StringIO(), file_path=log_file)

    get_logger("qai.test").info("chat.turn.start", tab_id="t-1")
    logging.getLogger("uvicorn.error").warning("foreign stdlib record")
    _flush()

    text = log_file.read_text(encoding="utf-8")
    assert "chat.turn.start" in text
    assert "tab_id=t-1" in text
    assert "foreign stdlib record" in text
    assert "\x1b[" not in text  # no ANSI colour codes in the file


def test_json_file_format(tmp_path: Path) -> None:
    log_file = tmp_path / "backend.log"
    configure_logging(stream=io.StringIO(), file_path=log_file, file_fmt="json")

    get_logger("qai.test").warning("chat.turn.end", outcome="cancelled")
    _flush()

    rec = json.loads(log_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert rec["event"] == "chat.turn.end"
    assert rec["outcome"] == "cancelled"
    assert rec["level"] == "WARNING"
    assert "timestamp" in rec


def test_exception_traceback_is_written(tmp_path: Path) -> None:
    log_file = tmp_path / "backend.log"
    configure_logging(stream=io.StringIO(), file_path=log_file)
    try:
        raise RuntimeError("boom-xyz")
    except RuntimeError:
        get_logger("qai.test").exception("chat.turn.setup_failed")
    _flush()

    text = log_file.read_text(encoding="utf-8")
    assert "chat.turn.setup_failed" in text
    assert "boom-xyz" in text
    assert "Traceback" in text


def test_rotation_keeps_backups(tmp_path: Path) -> None:
    log_file = tmp_path / "backend.log"
    configure_logging(
        stream=io.StringIO(), file_path=log_file, file_max_bytes=2000, file_backup_count=2,
    )
    log = get_logger("qai.test")
    for i in range(200):
        log.info("filler", i=i, pad="x" * 50)
    _flush()

    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["backend.log", "backend.log.1", "backend.log.2"]
    assert log_file.stat().st_size <= 2000 + 500


def test_reconfigure_does_not_stack_file_handlers(tmp_path: Path) -> None:
    log_file = tmp_path / "backend.log"
    configure_logging(stream=io.StringIO(), file_path=log_file)
    configure_logging(stream=io.StringIO(), file_path=log_file)

    get_logger("qai.test").info("only-once-marker")
    _flush()

    assert log_file.read_text(encoding="utf-8").count("only-once-marker") == 1
    file_handlers = [h for h in _managed_handlers() if isinstance(h, logging.FileHandler)]
    assert len(file_handlers) == 1


def test_no_file_path_means_stderr_only(tmp_path: Path) -> None:
    configure_logging(stream=io.StringIO())
    assert not any(isinstance(h, logging.FileHandler) for h in _managed_handlers())


def test_unopenable_file_only_warns(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")  # a FILE where the log dir should be
    stream = io.StringIO()
    configure_logging(stream=stream, file_path=blocker / "backend.log")

    get_logger("qai.test").info("still-logged")
    assert "still-logged" in stream.getvalue()
    assert "cannot open log file" in capsys.readouterr().err


def test_failed_rollover_keeps_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Windows: the rename fails while another program holds the file open."""
    log_file = tmp_path / "backend.log"
    configure_logging(
        stream=io.StringIO(), file_path=log_file, file_max_bytes=500, file_backup_count=2,
    )

    def _locked(self, source, dest):  # noqa: ANN001
        raise PermissionError(32, "file in use")

    monkeypatch.setattr(log_config._SafeRotatingFileHandler, "rotate", _locked)
    log = get_logger("qai.test")
    for i in range(50):
        log.info("after-lock", i=i, pad="y" * 40)
    _flush()

    text = log_file.read_text(encoding="utf-8")
    assert "i=49" in text  # nothing lost, still appending to the same file
    assert not (tmp_path / "backend.log.1").exists()
