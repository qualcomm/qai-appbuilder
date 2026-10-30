"""Legacy ``models`` / ``default_model`` stripping for ``service_config.json``.

GenieAPIService still loads every enabled ``models[*]`` entry and lets
``default_model`` override its default handle, while QAI no longer keeps
those keys in sync. Already-installed files must therefore be migrated
exactly once per install (backed up, marker-guarded) without touching any
other key; a ``models[]`` the user writes afterwards must survive.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from qai.model_runtime.domain.service_config import (
    LEGACY_MODEL_SLOT_KEYS,
    strip_legacy_model_slot_keys,
)
from qai.model_runtime.infrastructure.service_config_repository import (
    FileServiceConfigRepository,
)

pytestmark = pytest.mark.unit

_BACKUP_NAME = "service_config.json.bak-legacy-models"
_MARKER_NAME = "service_config.json.legacy-models-migrated"

# Shape of a real pre-3.0 install (CRLF, 4-space indent, CJK comments).
_LEGACY_DOC = {
    "_comment": "GenieAPIService 配置",
    "port": 8910,
    "local_model": {"enabled": True},
    "_comment_models": "多模型并发配置",
    "models": [
        {
            "name": "qwen3-8b",
            "path": "qwen3-8b",
            "backend": "qnn",
            "device": "npu",
            "context_size": 8192,
            "enabled": True,
        }
    ],
    "_comment_default_model": "默认模型名称",
    "default_model": "qwen3-8b",
    "debug": {"status_update_content_visible": False},
}


def _write_legacy(path: Path) -> bytes:
    text = json.dumps(_LEGACY_DOC, indent=4, ensure_ascii=False).replace("\n", "\r\n")
    raw = (text + "\r\n").encode("utf-8")
    path.write_bytes(raw)
    return raw


def test_domain_strip_removes_only_legacy_keys() -> None:
    doc = json.loads(json.dumps(_LEGACY_DOC))
    removed = strip_legacy_model_slot_keys(doc)
    assert removed == list(LEGACY_MODEL_SLOT_KEYS)
    assert doc == {
        k: v for k, v in _LEGACY_DOC.items() if k not in LEGACY_MODEL_SLOT_KEYS
    }
    # Second pass on the cleaned doc is a no-op.
    assert strip_legacy_model_slot_keys(doc) == []


def test_legacy_file_is_stripped_backed_up_and_other_keys_kept(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = tmp_path / "service_config.json"
    original = _write_legacy(cfg)

    with caplog.at_level(logging.INFO):
        removed = FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg))

    assert removed == [
        "_comment_models",
        "models",
        "_comment_default_model",
        "default_model",
    ]
    raw_after = cfg.read_bytes()
    assert not raw_after.startswith(b"\xef\xbb\xbf")  # UTF-8 without BOM
    text_after = raw_after.decode("utf-8")
    assert "\r\n" in text_after and "\n" not in text_after.replace("\r\n", "")
    assert text_after.splitlines()[1].startswith('    "_comment"')  # indent kept
    expected = {
        k: v for k, v in _LEGACY_DOC.items() if k not in LEGACY_MODEL_SLOT_KEYS
    }
    after = json.loads(text_after)
    assert after == expected
    assert list(after) == list(expected)  # key order preserved

    backup = tmp_path / _BACKUP_NAME
    assert backup.read_bytes() == original
    assert not list(tmp_path.glob("*.tmp-legacy-models"))

    record = next(
        r for r in caplog.records if "legacy_model_slots_stripped" in r.getMessage()
    )
    msg = record.getMessage()
    assert "default_model" in msg and "models" in msg
    assert str(backup) in msg
    marker = tmp_path / _MARKER_NAME
    assert marker.is_file()
    assert str(marker) in msg


def test_rerun_is_noop_and_keeps_mtime(tmp_path: Path) -> None:
    cfg = tmp_path / "service_config.json"
    _write_legacy(cfg)
    FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg))
    content = cfg.read_bytes()
    mtime = cfg.stat().st_mtime_ns

    assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg)) == []
    assert cfg.read_bytes() == content
    assert cfg.stat().st_mtime_ns == mtime


def test_user_models_added_after_migration_are_preserved(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Multi-model slots are a real daemon capability: once migrated, a
    ``models[]`` the user hand-writes later must never be stripped."""
    cfg = tmp_path / "service_config.json"
    _write_legacy(cfg)
    assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg))

    user_doc = {
        "port": 8910,
        "models": [{"name": "user-model", "enabled": True}],
        "default_model": "user-model",
    }
    cfg.write_text(json.dumps(user_doc, indent=4), encoding="utf-8")
    content = cfg.read_bytes()
    mtime = cfg.stat().st_mtime_ns

    with caplog.at_level(logging.DEBUG):
        assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg)) == []
    assert cfg.read_bytes() == content
    assert cfg.stat().st_mtime_ns == mtime
    msg = next(
        r.getMessage() for r in caplog.records if "already_migrated" in r.getMessage()
    )
    assert str(tmp_path / _MARKER_NAME) in msg


def test_clean_file_first_run_writes_marker(tmp_path: Path) -> None:
    cfg = tmp_path / "service_config.json"
    cfg.write_text(json.dumps({"port": 8910}), encoding="utf-8")
    assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg)) == []
    assert (tmp_path / _MARKER_NAME).is_file()

    # A later user-written models[] is now protected too.
    cfg.write_text(json.dumps({"port": 8910, "models": []}), encoding="utf-8")
    assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg)) == []
    assert "models" in json.loads(cfg.read_text(encoding="utf-8"))


def test_failed_rewrite_writes_no_marker_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = tmp_path / "service_config.json"
    original = _write_legacy(cfg)

    import qai.model_runtime.infrastructure.service_config_repository as repo_mod

    real_replace = repo_mod.os.replace

    def _fail_replace(src: object, dst: object) -> None:
        raise PermissionError("locked by daemon")

    monkeypatch.setattr(repo_mod.os, "replace", _fail_replace)
    assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg)) == []
    assert cfg.read_bytes() == original
    assert not (tmp_path / _MARKER_NAME).exists()
    assert not list(tmp_path.glob("*.tmp-legacy-models"))

    monkeypatch.setattr(repo_mod.os, "replace", real_replace)
    assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg))
    assert "models" not in json.loads(cfg.read_text(encoding="utf-8"))
    assert (tmp_path / _MARKER_NAME).is_file()
    # The backup taken on the failed attempt still holds the original bytes.
    assert (tmp_path / _BACKUP_NAME).read_bytes() == original


def test_marker_write_failure_does_not_raise(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    cfg = tmp_path / "service_config.json"
    _write_legacy(cfg)
    real_write_text = Path.write_text

    def _write_text(self: Path, *args: object, **kwargs: object) -> int:
        if self.name == _MARKER_NAME:
            raise PermissionError("read-only")
        return real_write_text(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "write_text", _write_text)
    with caplog.at_level(logging.WARNING):
        removed = FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg))
    assert "models" in removed
    assert "models" not in json.loads(cfg.read_text(encoding="utf-8"))
    assert not (tmp_path / _MARKER_NAME).exists()
    assert any("marker_write_failed" in r.getMessage() for r in caplog.records)


def test_existing_backup_is_never_overwritten(tmp_path: Path) -> None:
    cfg = tmp_path / "service_config.json"
    _write_legacy(cfg)
    backup = tmp_path / _BACKUP_NAME
    backup.write_bytes(b"first-backup")

    assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg))
    assert backup.read_bytes() == b"first-backup"
    assert "models" not in json.loads(cfg.read_text(encoding="utf-8"))


def test_clean_file_is_not_written(tmp_path: Path) -> None:
    cfg = tmp_path / "service_config.json"
    original = json.dumps({"port": 8910, "debug": {"log_rule_matches": False}})
    cfg.write_text(original, encoding="utf-8")
    mtime = cfg.stat().st_mtime_ns

    assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg)) == []
    assert cfg.read_text(encoding="utf-8") == original
    assert cfg.stat().st_mtime_ns == mtime
    assert not (tmp_path / _BACKUP_NAME).exists()


@pytest.mark.parametrize("path", ["", None])
def test_not_installed_is_noop(path: str | None) -> None:
    assert FileServiceConfigRepository.strip_legacy_model_slots(path=path) == []


def test_missing_and_corrupt_files_are_left_alone(tmp_path: Path) -> None:
    missing = tmp_path / "gone" / "service_config.json"
    assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(missing)) == []
    assert not missing.exists()

    corrupt = tmp_path / "service_config.json"
    corrupt.write_text('{ "models": [', encoding="utf-8")
    assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(corrupt)) == []
    assert corrupt.read_text(encoding="utf-8") == '{ "models": ['
    assert not (tmp_path / _BACKUP_NAME).exists()
    # Not migrated: no marker, so a later (repaired) file is still migrated.
    assert not (tmp_path / _MARKER_NAME).exists()
    assert not (tmp_path / "gone").exists()


def test_bom_input_is_rewritten_without_bom(tmp_path: Path) -> None:
    cfg = tmp_path / "service_config.json"
    cfg.write_bytes(
        b"\xef\xbb\xbf" + json.dumps({"default_model": "x", "port": 1}, indent=2).encode()
    )
    assert FileServiceConfigRepository.strip_legacy_model_slots(path=str(cfg)) == [
        "default_model"
    ]
    raw = cfg.read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf")
    assert raw.decode("utf-8") == json.dumps({"port": 1}, indent=2)
