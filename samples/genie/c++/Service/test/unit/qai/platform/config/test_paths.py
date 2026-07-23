"""Unit tests for ``qai.platform.config.paths.DataPaths``."""

from __future__ import annotations

from pathlib import Path

from qai.platform.config.paths import DataPaths


def test_cli_sessions_dir_is_under_cache_dir(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path)
    assert paths.cli_sessions_dir == paths.cache_dir / "cli_sessions"
    assert paths.cli_sessions_dir == tmp_path / "cache" / "cli_sessions"


def test_cli_sessions_dir_is_not_created_eagerly(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path)
    assert not paths.cli_sessions_dir.exists()


def test_cli_sessions_dir_created_on_demand_via_ensure(tmp_path: Path) -> None:
    paths = DataPaths(tmp_path)
    created = paths.ensure(paths.cli_sessions_dir)
    assert created == paths.cli_sessions_dir
    assert paths.cli_sessions_dir.is_dir()
