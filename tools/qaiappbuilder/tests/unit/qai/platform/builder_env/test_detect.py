# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Detection of the model-conversion environment (JSON first, then verify the disk)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

from qai.platform.builder_env import SDK_MARKER_REL, detect_builder_env

pytestmark = pytest.mark.unit


def _make_sdk(root: Path) -> Path:
    marker = root / SDK_MARKER_REL
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("MZ")
    return root


def _make_venv(venv: Path) -> Path:
    py = venv / "Scripts" / "python.exe"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("MZ")
    return venv


def _cfg(tmp_path: Path, payload) -> Path:
    f = tmp_path / "config" / "qairt_env.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return f


def codes(st) -> list[str]:
    return [p.code for p in st.problems]


def test_ready_when_json_and_disk_agree(tmp_path):
    sdk, venv = _make_sdk(tmp_path / "sdk"), _make_venv(tmp_path / "venv")
    st = detect_builder_env(_cfg(tmp_path, {"qairt_sdk_root": str(sdk), "python_x64_venv": str(venv)}))
    assert st.ready and st.problems == () and st.missing == ()
    assert st.sdk_root == str(sdk) and st.venv == str(venv)


def test_missing_config_is_not_ready(tmp_path):
    st = detect_builder_env(tmp_path / "config" / "qairt_env.json")
    assert not st.ready and codes(st) == ["config_missing"] and st.missing == ("config",)


@pytest.mark.parametrize("text", ["{ not json", "[]", "null", '"str"', ""])
def test_unreadable_or_wrong_shape_config(tmp_path, text):
    st = detect_builder_env(_cfg(tmp_path, text))
    assert not st.ready and codes(st) == ["config_invalid"]


def test_utf8_bom_config_is_accepted(tmp_path):
    sdk, venv = _make_sdk(tmp_path / "sdk"), _make_venv(tmp_path / "venv")
    f = tmp_path / "c.json"
    f.write_bytes(b"\xef\xbb\xbf" + json.dumps({"qairt_sdk_root": str(sdk), "python_x64_venv": str(venv)}).encode())
    assert detect_builder_env(f).ready


def test_json_present_but_sdk_deleted_is_not_ready(tmp_path):
    """The whole point: a stale qairt_env.json must NOT read as 'ready'."""
    venv = _make_venv(tmp_path / "venv")
    sdk = tmp_path / "gone"
    st = detect_builder_env(_cfg(tmp_path, {"qairt_sdk_root": str(sdk), "python_x64_venv": str(venv)}))
    assert not st.ready and codes(st) == ["sdk_missing"] and st.missing == ("sdk",)
    assert st.problems[0].path == str(sdk)


def test_sdk_dir_without_marker_is_not_ready(tmp_path):
    (tmp_path / "sdk" / "bin").mkdir(parents=True)  # directory exists but is not a valid SDK
    venv = _make_venv(tmp_path / "venv")
    st = detect_builder_env(_cfg(tmp_path, {"qairt_sdk_root": str(tmp_path / "sdk"), "python_x64_venv": str(venv)}))
    assert codes(st) == ["sdk_missing"]


def test_json_present_but_venv_deleted_is_not_ready(tmp_path):
    sdk = _make_sdk(tmp_path / "sdk")
    st = detect_builder_env(_cfg(tmp_path, {"qairt_sdk_root": str(sdk), "python_x64_venv": str(tmp_path / "nope")}))
    assert codes(st) == ["venv_missing"] and st.missing == ("venv",)


def test_both_missing_reports_both(tmp_path):
    st = detect_builder_env(
        _cfg(tmp_path, {"qairt_sdk_root": str(tmp_path / "a"), "python_x64_venv": str(tmp_path / "b")})
    )
    assert codes(st) == ["sdk_missing", "venv_missing"] and st.missing == ("sdk", "venv")


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({}, ["sdk_not_configured", "venv_not_configured"]),
        ({"qairt_sdk_root": ""}, ["sdk_not_configured", "venv_not_configured"]),
        ({"qairt_sdk_root": 5, "python_x64_venv": None}, ["sdk_not_configured", "venv_not_configured"]),
    ],
)
def test_incomplete_config_keys(tmp_path, payload, expected):
    assert codes(detect_builder_env(_cfg(tmp_path, payload))) == expected


def test_relative_paths_are_rejected(tmp_path):
    st = detect_builder_env(_cfg(tmp_path, {"qairt_sdk_root": "sdk", "python_x64_venv": "venv"}))
    assert codes(st) == ["sdk_path_not_absolute", "venv_path_not_absolute"]


def test_env_vars_are_expanded(tmp_path, monkeypatch):
    sdk, venv = _make_sdk(tmp_path / "sdk"), _make_venv(tmp_path / "venv")
    monkeypatch.setenv("QAI_T_ROOT", str(tmp_path))
    st = detect_builder_env(
        _cfg(tmp_path, {"qairt_sdk_root": "%QAI_T_ROOT%/sdk" if sys.platform == "win32" else "$QAI_T_ROOT/sdk",
                        "python_x64_venv": "%QAI_T_ROOT%/venv" if sys.platform == "win32" else "$QAI_T_ROOT/venv"})
    )
    assert st.ready and st.sdk_root.endswith("sdk") and st.venv.endswith("venv")


def test_extra_keys_are_ignored(tmp_path):
    sdk, venv = _make_sdk(tmp_path / "sdk"), _make_venv(tmp_path / "venv")
    cfg = {"qairt_sdk_root": str(sdk), "python_x64_venv": str(venv), "arch": "x86_64", "whatever": [1]}
    assert detect_builder_env(_cfg(tmp_path, cfg)).ready


def test_detect_is_read_only(tmp_path):
    f = _cfg(tmp_path, {"qairt_sdk_root": str(tmp_path / "x"), "python_x64_venv": str(tmp_path / "y")})
    before = (f.read_bytes(), f.stat().st_mtime_ns)
    detect_builder_env(f)
    assert (f.read_bytes(), f.stat().st_mtime_ns) == before
    assert not (tmp_path / "x").exists()


# ── the installer script's own --check must agree with the backend ────────────

SCRIPT = Path(__file__).resolve().parents[5] / "scripts" / "setup" / "install_builder_env.py"


@pytest.fixture(scope="module")
def ibe():
    spec = importlib.util.spec_from_file_location("ibe_consistency", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _script_status(ibe, root: Path, config: Path) -> dict:
    app = root / "app"
    (app / "scripts").mkdir(parents=True, exist_ok=True)
    (app / "scripts" / "qairt_release.json").write_text(
        json.dumps({"qairt_version": "1.0", "qairt_sdk_root_template": str(root / "d" / "{version}"),
                    "qairt_download_url_template": "https://x/{version}.zip"}), encoding="utf-8")
    ctx = ibe.build_ctx(ibe.parse_args(["--root", str(app), "--arch", "x64", "--envs-dir", str(root / "e")]))
    # point the script at the same config file the backend reads
    type(ctx).config_file = property(lambda self, c=config: c)
    return ibe.status(ctx)


@pytest.mark.parametrize("scenario", ["ready", "no_config", "bad_json", "sdk_gone", "venv_gone", "empty", "relative"])
def test_script_check_matches_backend(ibe, tmp_path, scenario):
    sdk, venv = tmp_path / "sdk", tmp_path / "venv"
    cfgfile = tmp_path / "config" / "qairt_env.json"
    if scenario == "ready":
        _make_sdk(sdk), _make_venv(venv)
        _cfg(tmp_path, {"qairt_sdk_root": str(sdk), "python_x64_venv": str(venv)})
    elif scenario == "bad_json":
        _cfg(tmp_path, "{ nope")
    elif scenario == "sdk_gone":
        _make_venv(venv)
        _cfg(tmp_path, {"qairt_sdk_root": str(sdk), "python_x64_venv": str(venv)})
    elif scenario == "venv_gone":
        _make_sdk(sdk)
        _cfg(tmp_path, {"qairt_sdk_root": str(sdk), "python_x64_venv": str(venv)})
    elif scenario == "empty":
        _cfg(tmp_path, {})
    elif scenario == "relative":
        _cfg(tmp_path, {"qairt_sdk_root": "sdk", "python_x64_venv": "venv"})
    backend = detect_builder_env(cfgfile)
    script = _script_status(ibe, tmp_path, cfgfile)
    assert script["ready"] == backend.ready
    assert [p["code"] for p in script["problems"]] == [p.code for p in backend.problems]
    assert script["missing"] == list(backend.missing)
