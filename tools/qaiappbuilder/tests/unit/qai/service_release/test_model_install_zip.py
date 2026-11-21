# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Installing a downloaded model zip into ``models/<id>``.

A failed install must say why (shown on the model card as-is), keep the zip
for a retry, put any previous install back, and leave no temp dir behind.
"""

from __future__ import annotations

import asyncio
import shutil
import threading
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from qai.service_release.adapters import filesystem_installer as fi
from qai.service_release.domain.errors import InstallFailedError
from qai.service_release.infrastructure.download_paths import DownloadPaths


@pytest.fixture
def installer(tmp_path: Path) -> fi.FileSystemArtifactInstaller:
    paths = DownloadPaths(
        default_download_dir=tmp_path / "downloads",
        bin_dir=tmp_path / "bin",
        models_dir=tmp_path / "models",
    )
    paths.default_download_dir.mkdir()
    return fi.FileSystemArtifactInstaller(paths=paths)


def _model_zip(tmp_path: Path, top: str = "qwen3-8b-8380") -> Path:
    path = tmp_path / "downloads" / f"{top}.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{top}/config.json", "{}")
        zf.writestr(f"{top}/weights.bin", b"\0" * 4096)
    return path


def _install(inst: fi.FileSystemArtifactInstaller, save: Path) -> object:
    return asyncio.run(
        inst.install_model(save_path=str(save), model_id="qwen3-8b", install_dir="qwen3-8b")
    )


def _leftovers(models: Path) -> list[str]:
    return sorted(p.name for p in models.iterdir() if p.name.endswith(("_tmp_extract", ".bak")))


def test_a_good_zip_is_flattened_into_the_model_dir(installer, tmp_path: Path) -> None:
    res = _install(installer, _model_zip(tmp_path))
    models = tmp_path / "models"
    assert res.ok and (models / "qwen3-8b" / "config.json").is_file()
    assert _leftovers(models) == []


def test_a_saved_error_page_is_reported_and_the_file_kept(installer, tmp_path: Path) -> None:
    bogus = tmp_path / "downloads" / "qwen3-8b-8380.zip"
    bogus.write_text("<html>502 Bad Gateway</html>")
    with pytest.raises(InstallFailedError, match="not a zip archive"):
        _install(installer, bogus)
    assert bogus.exists()
    assert not (tmp_path / "models" / "qwen3-8b").exists()


def test_not_enough_disk_space_fails_before_extracting(
    installer, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(fi.shutil, "disk_usage", lambda _p: SimpleNamespace(free=1024))
    with pytest.raises(InstallFailedError, match="not enough disk space"):
        _install(installer, _model_zip(tmp_path))
    assert not (tmp_path / "models" / "qwen3-8b").exists()


def test_an_unexpected_zipfile_error_restores_the_previous_install(
    installer, tmp_path: Path, monkeypatch
) -> None:
    old = tmp_path / "models" / "qwen3-8b"
    old.mkdir(parents=True)
    (old / "config.json").write_text("old")

    def boom(self, *_a, **_k):  # e.g. Deflate64 ("compression method not supported")
        raise NotImplementedError("That compression method is not supported")

    monkeypatch.setattr(zipfile.ZipFile, "extractall", boom)
    save = _model_zip(tmp_path)
    with pytest.raises(InstallFailedError, match="NotImplementedError"):
        _install(installer, save)

    assert (old / "config.json").read_text() == "old"
    assert save.exists()
    assert _leftovers(tmp_path / "models") == []


def test_a_failure_half_way_drops_the_partial_dir(installer, tmp_path: Path, monkeypatch) -> None:
    real_move = shutil.move
    calls = {"n": 0}

    def flaky_move(src, dst, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError(28, "No space left on device")
        return real_move(src, dst, *a, **k)

    monkeypatch.setattr(fi.shutil, "move", flaky_move)
    with pytest.raises(InstallFailedError, match="extraction failed"):
        _install(installer, _model_zip(tmp_path))
    models = tmp_path / "models"
    assert not (models / "qwen3-8b").exists()
    assert _leftovers(models) == []


def test_extraction_runs_off_the_event_loop(installer, tmp_path: Path, monkeypatch) -> None:
    main = threading.get_ident()
    seen: list[int] = []
    real = fi._extract_zip_to_dir

    def spy(*a, **k):
        seen.append(threading.get_ident())
        return real(*a, **k)

    monkeypatch.setattr(fi, "_extract_zip_to_dir", spy)
    _install(installer, _model_zip(tmp_path))
    assert seen and seen[0] != main
