# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Rebuilding a broken MCP venv must not trip over Windows' 260-character limit.

Found while cleaning up a throwaway venv: a package that ships a deep tree of data
files (a skills bundle) lands paths beyond MAX_PATH inside
``<data>/mcp_packages/<name>/venv``; plain ``shutil.rmtree`` then reports "path not
found" for files that exist, so the venv could never be removed and re-installing
to repair it would fail with ``create_venv``.
"""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

from qai.chat.adapters import mcp_wheel_installer as inst


def _deep_tree(root: Path) -> Path:
    deep = root
    while len(str(deep)) < 300:  # beyond MAX_PATH
        deep = deep / ("segment_" + "x" * 20)
    return deep


@pytest.mark.skipif(sys.platform != "win32", reason="MAX_PATH is a Windows limit")
def test_a_tree_with_paths_beyond_max_path_can_be_removed(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    deep = _deep_tree(venv)
    os.makedirs("\\\\?\\" + str(deep))
    leaf = "\\\\?\\" + str(deep / "data.txt")
    Path(leaf).write_text("x")
    assert len(str(deep / "data.txt")) > 260

    inst._remove_tree(venv)

    assert not venv.exists()


def test_read_only_files_are_removed_too(tmp_path: Path) -> None:
    venv = tmp_path / "venv"
    (venv / "Lib").mkdir(parents=True)
    ro = venv / "Lib" / "locked.txt"
    ro.write_text("x")
    os.chmod(ro, stat.S_IREAD)

    inst._remove_tree(venv)

    assert not venv.exists()


def test_removing_a_plain_tree_still_works(tmp_path: Path) -> None:
    (tmp_path / "v" / "a" / "b").mkdir(parents=True)
    (tmp_path / "v" / "a" / "b" / "f.txt").write_text("x")
    inst._remove_tree(tmp_path / "v")
    assert not (tmp_path / "v").exists()
