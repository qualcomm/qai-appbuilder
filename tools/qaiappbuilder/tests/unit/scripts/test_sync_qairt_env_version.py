# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Unit tests for scripts/setup/sync_qairt_env_version.py (all in tmp_path)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "setup" / "sync_qairt_env_version.py"
NEW = "2.51.40.260930"
OLD = "2.48.40.260702"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("sync_qairt_env_version_under_test", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = m
    spec.loader.exec_module(m)
    return m


@pytest.fixture
def release(tmp_path):
    p = tmp_path / "qairt_release.json"
    p.write_text(json.dumps({
        "qairt_version": NEW,
        "qairt_sdk_root_template": "C:\\Qualcomm\\AIStack\\QAIRT\\{version}",
        "qairt_download_url_template": "https://x/{version}/v{version}.zip",
    }), encoding="utf-8")
    return p


def _cfg(tmp_path, data, raw=None):
    p = tmp_path / "config" / "qairt_env.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(raw if raw is not None else json.dumps(data, indent=2), encoding="utf-8")
    return p


def _old_cfg(**extra):
    d = {
        "_version": OLD,
        "qairt_sdk_root": f"C:\\Qualcomm\\AIStack\\QAIRT\\{OLD}",
        "qairt_download_url": f"https://x/{OLD}/v{OLD}.zip",
        "python_x64_venv": "C:\\envs\\.venv_x64_310",
        "python_runtime_venv": "C:\\envs\\.venv_arm64_313",
        "qairt_runtime_subdir": "arm64x-windows-msvc",
        "arch": "aarch64",
        "qairt_sdk_installed_by_app": True,
    }
    d.update(extra)
    return d


def _run(mod, cfg, release):
    return mod.main(["x", str(cfg), str(release)])


def test_old_version_is_rewritten_other_keys_kept(mod, tmp_path, release, capsys):
    cfg = _cfg(tmp_path, _old_cfg(custom_key=[1, 2]))
    assert _run(mod, cfg, release) == 0
    out = json.loads(cfg.read_text(encoding="utf-8"))
    assert out["qairt_sdk_root"] == f"C:\\Qualcomm\\AIStack\\QAIRT\\{NEW}"
    assert out["_version"] == NEW
    assert out["qairt_download_url"] == f"https://x/{NEW}/v{NEW}.zip"
    assert out["qairt_sdk_installed_by_app"] is False
    for k in ("python_x64_venv", "python_runtime_venv", "qairt_runtime_subdir", "arch"):
        assert out[k] == _old_cfg()[k]
    assert out["custom_key"] == [1, 2]
    assert "[OK]" in capsys.readouterr().out
    assert not list(cfg.parent.glob("*.tmp"))


def test_matching_version_is_not_touched(mod, tmp_path, release, capsys):
    data = _old_cfg(_version=NEW, qairt_sdk_root=f"C:\\Qualcomm\\AIStack\\QAIRT\\{NEW}")
    raw = json.dumps(data, indent=4)
    cfg = _cfg(tmp_path, None, raw=raw)
    assert _run(mod, cfg, release) == 0
    assert cfg.read_text(encoding="utf-8") == raw  # byte-identical
    assert "[SKIP]" in capsys.readouterr().out


def test_only_build_number_differs_is_a_mismatch(mod, tmp_path, release):
    other = "2.51.40.250101"
    cfg = _cfg(tmp_path, _old_cfg(_version=other, qairt_sdk_root=f"C:\\Q\\{other}"))
    _run(mod, cfg, release)
    assert json.loads(cfg.read_text(encoding="utf-8"))["_version"] == NEW


def test_root_folder_disagrees_with_version_field(mod, tmp_path, release):
    cfg = _cfg(tmp_path, _old_cfg(_version=NEW))  # _version right, root still OLD
    _run(mod, cfg, release)
    assert json.loads(cfg.read_text(encoding="utf-8"))["qairt_sdk_root"].endswith(NEW)


def test_switch_style_qairt_root_is_kept_in_step(mod, tmp_path, release):
    d = _old_cfg(qairt_version=OLD)
    root = d.pop("qairt_sdk_root")
    d["qairt_root"] = root
    cfg = _cfg(tmp_path, d)
    _run(mod, cfg, release)
    out = json.loads(cfg.read_text(encoding="utf-8"))
    assert out["qairt_root"] == out["qairt_sdk_root"] == f"C:\\Qualcomm\\AIStack\\QAIRT\\{NEW}"
    assert out["qairt_version"] == NEW


def test_custom_root_without_version_name_and_matching_version(mod, tmp_path, release):
    cfg = _cfg(tmp_path, _old_cfg(_version=NEW, qairt_sdk_root="D:\\sdk\\qairt"))
    _run(mod, cfg, release)
    assert json.loads(cfg.read_text(encoding="utf-8"))["qairt_sdk_root"] == "D:\\sdk\\qairt"


def test_missing_config_is_noop(mod, tmp_path, release):
    cfg = tmp_path / "config" / "qairt_env.json"
    assert _run(mod, cfg, release) == 0
    assert not cfg.exists()


@pytest.mark.parametrize("raw", ["{not json", "[1, 2]"])
def test_invalid_config_left_alone(mod, tmp_path, release, raw, capsys):
    cfg = _cfg(tmp_path, None, raw=raw)
    assert _run(mod, cfg, release) == 0
    assert cfg.read_text(encoding="utf-8") == raw
    assert "[WARN]" in capsys.readouterr().out


def test_bad_release_is_warning_not_failure(mod, tmp_path, capsys):
    cfg = _cfg(tmp_path, _old_cfg())
    before = cfg.read_text(encoding="utf-8")
    assert _run(mod, cfg, tmp_path / "nope.json") == 0
    assert cfg.read_text(encoding="utf-8") == before
    assert "[WARN]" in capsys.readouterr().out


def test_bom_config_is_read(mod, tmp_path, release):
    cfg = tmp_path / "qairt_env.json"
    cfg.write_bytes(b"\xef\xbb\xbf" + json.dumps(_old_cfg()).encode())
    _run(mod, cfg, release)
    assert json.loads(cfg.read_text(encoding="utf-8"))["_version"] == NEW


def test_usage_error(mod):
    assert mod.main(["x"]) == 1


def test_result_reads_not_ready_until_new_sdk_installed(mod, tmp_path, release):
    """End goal: after the sync, the readiness check sees the NEW (absent) SDK -> not ready."""
    src = Path(__file__).resolve().parents[3] / "src"
    sys.path.insert(0, str(src))
    try:
        from qai.platform.builder_env.detect import SDK_MARKER_REL, detect_builder_env
    finally:
        sys.path.remove(str(src))
    old_sdk = tmp_path / OLD
    (old_sdk / SDK_MARKER_REL).parent.mkdir(parents=True)
    (old_sdk / SDK_MARKER_REL).write_text("x")
    venv = tmp_path / "venv"
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_text("x")
    rel = json.loads(release.read_text(encoding="utf-8"))
    rel["qairt_sdk_root_template"] = str(tmp_path / "{version}")
    release.write_text(json.dumps(rel), encoding="utf-8")
    cfg = _cfg(tmp_path, {"_version": OLD, "qairt_sdk_root": str(old_sdk), "python_x64_venv": str(venv)})

    assert detect_builder_env(cfg).ready  # stale file: wrongly "ready" before the fix
    _run(mod, cfg, release)
    st = detect_builder_env(cfg)
    assert not st.ready and "sdk" in st.missing
    assert st.sdk_root == str(tmp_path / NEW)
