# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ProcessBackedInferenceService.start()`` strips legacy model slots pre-spawn.

The daemon still honours ``models`` / ``default_model`` in
``<install_dir>/service_config.json``; every spawn (``/start`` and the
``load_model`` path, which both call ``start()``) must run the one-time,
marker-guarded migration on exactly that file, a migration failure must
never block the spawn, and slots the user writes after the migration must
survive later spawns.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from qai.model_runtime.infrastructure import process_service as ps_mod
from qai.model_runtime.infrastructure.process_service import (
    ProcessBackedInferenceService,
)

pytestmark = pytest.mark.unit


class _SpawnRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], int, str | None]] = []

    def __call__(self, args: list[str], port: int, model_name: str | None) -> None:
        self.calls.append((list(args), port, model_name))


def _make(install_dir: str) -> tuple[ProcessBackedInferenceService, _SpawnRecorder]:
    svc = ProcessBackedInferenceService(install_dir=install_dir)
    rec = _SpawnRecorder()
    svc._do_start = rec  # type: ignore[assignment]
    return svc, rec


@pytest.mark.asyncio
async def test_start_strips_legacy_slots_in_install_dir_service_config(
    tmp_path: Path,
) -> None:
    cfg_path = tmp_path / "service_config.json"
    cfg_path.write_text(
        json.dumps(
            {
                "port": 8910,
                "models": [{"name": "stale", "enabled": True}],
                "default_model": "stale",
            },
            indent=4,
        ),
        encoding="utf-8",
    )
    svc, rec = _make(str(tmp_path))

    await svc.start(model_name="m", port=8000, loglevel=3)

    assert json.loads(cfg_path.read_text(encoding="utf-8")) == {"port": 8910}
    assert (tmp_path / "service_config.json.bak-legacy-models").is_file()
    assert (tmp_path / "service_config.json.legacy-models-migrated").is_file()
    assert len(rec.calls) == 1


@pytest.mark.asyncio
async def test_second_start_keeps_user_written_models(tmp_path: Path) -> None:
    """One-time migration: a ``models[]`` the user adds after the first
    spawn is a real multi-model config and must survive later spawns."""
    cfg_path = tmp_path / "service_config.json"
    cfg_path.write_text(
        json.dumps({"port": 8910, "default_model": "stale"}), encoding="utf-8"
    )
    svc, rec = _make(str(tmp_path))
    await svc.start(model_name="m", port=8000, loglevel=3)
    assert json.loads(cfg_path.read_text(encoding="utf-8")) == {"port": 8910}

    user_doc = {"port": 8910, "models": [{"name": "mine", "enabled": True}]}
    cfg_path.write_text(json.dumps(user_doc), encoding="utf-8")
    await svc.start(model_name="m", port=8000, loglevel=3)

    assert json.loads(cfg_path.read_text(encoding="utf-8")) == user_doc
    assert len(rec.calls) == 2


@pytest.mark.asyncio
async def test_start_passes_install_dir_service_config_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(
        ps_mod.FileServiceConfigRepository,
        "strip_legacy_model_slots",
        staticmethod(lambda *, path: seen.append(path) or []),
    )
    svc, rec = _make(str(tmp_path))

    await svc.start(model_name="m", port=8000, loglevel=3)

    assert [Path(p) for p in seen] == [tmp_path / "service_config.json"]
    assert len(rec.calls) == 1


@pytest.mark.asyncio
async def test_migration_error_does_not_block_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*, path: Any) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(
        ps_mod.FileServiceConfigRepository,
        "strip_legacy_model_slots",
        staticmethod(_boom),
    )
    svc, rec = _make(str(tmp_path))

    await svc.start(model_name="m", port=8000, loglevel=3)

    assert len(rec.calls) == 1


@pytest.mark.asyncio
async def test_empty_install_dir_skips_migration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Any] = []
    monkeypatch.setattr(
        ps_mod.FileServiceConfigRepository,
        "strip_legacy_model_slots",
        staticmethod(lambda *, path: seen.append(path) or []),
    )
    svc, rec = _make("")

    await svc.start(model_name=None, port=8000, loglevel=3)

    assert seen == []
    assert len(rec.calls) == 1
