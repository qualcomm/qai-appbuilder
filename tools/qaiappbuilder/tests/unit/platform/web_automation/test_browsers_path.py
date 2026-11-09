# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
from __future__ import annotations

from pathlib import Path

import pytest

from qai.platform.web_automation.browsers_path import (
    BROWSERS_PATH_ENV,
    browsers_base_dir,
    configure_browsers_path,
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(BROWSERS_PATH_ENV, raising=False)


def test_sets_env_when_app_folder_has_chromium(tmp_path: Path) -> None:
    (tmp_path / "envs" / "ms-playwright" / "chromium-1228").mkdir(parents=True)
    got = configure_browsers_path(tmp_path)
    assert got == str(tmp_path / "envs" / "ms-playwright")
    assert browsers_base_dir() == got


def test_no_change_without_browser(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "envs" / "ms-playwright" / ".links").mkdir(parents=True)
    assert configure_browsers_path(tmp_path) is None
    import os

    assert BROWSERS_PATH_ENV not in os.environ


def test_user_setting_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "envs" / "ms-playwright" / "chromium-1228").mkdir(parents=True)
    monkeypatch.setenv(BROWSERS_PATH_ENV, r"D:\browsers")
    assert configure_browsers_path(tmp_path) == r"D:\browsers"
    assert browsers_base_dir() == r"D:\browsers"


def test_none_root() -> None:
    assert configure_browsers_path(None) is None
