# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Unit tests for scripts/setup/install_builder_env.py.

Everything runs in ``tmp_path``: no network, no real venv, no real SDK.
Network calls are replaced by stubs; the SDK "zip" is a tiny zip built on the fly.
"""

from __future__ import annotations

import importlib.util
import io
import json
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "setup" / "install_builder_env.py"


@pytest.fixture(scope="module")
def ibe():
    spec = importlib.util.spec_from_file_location("install_builder_env_under_test", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


VERSION = "9.9.9.1"


@pytest.fixture
def ctx(ibe, tmp_path):
    root = tmp_path / "app"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "qairt_release.json").write_text(
        json.dumps(
            {
                "qairt_version": VERSION,
                "qairt_sdk_root_template": str(tmp_path / "sdk" / "{version}"),
                "qairt_download_url_template": "https://example.invalid/v{version}.zip",
            }
        ),
        encoding="utf-8",
    )
    args = ibe.parse_args(
        ["--root", str(root), "--arch", "x64", "--envs-dir", str(tmp_path / "envs")]
    )
    c = ibe.build_ctx(args)
    c.rep._out = io.StringIO()  # capture events
    return c


def events(c):
    return [json.loads(l) for l in c.rep._out.getvalue().splitlines() if l.strip()]


def make_sdk_zip(path: Path, version: str = VERSION, with_marker: bool = True) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        base = f"qairt/{version}"
        zf.writestr(f"{base}/lib/python/placeholder.py", "x = 1\n")
        zf.writestr(f"{base}/bin/aarch64-windows-msvc/qairt-converter", "#!python\n")
        if with_marker:
            zf.writestr(f"{base}/bin/aarch64-windows-msvc/qnn-context-binary-generator.exe", "MZ-not-empty")
    return path


# ── pure helpers ──────────────────────────────────────────────────────────────


def test_marker_is_context_binary_generator(ibe, ctx):
    assert ibe.SDK_MARKER_REL == Path("bin/aarch64-windows-msvc/qnn-context-binary-generator.exe")
    assert ctx.marker == ctx.sdk_root / ibe.SDK_MARKER_REL


def test_step_weights_sum_to_100(ibe):
    assert sum(w for _, w in ibe.STEP_WEIGHTS) == 100


def test_pins_match_setup_qairt_env(ibe):
    """The converter pins must not drift from the validated list in setup_qairt_env.py."""
    src = (SCRIPT.parent / "setup_qairt_env.py").read_text(encoding="utf-8")
    for spec, _imp, _t in ibe.PACKAGES:
        if "==" in spec:
            assert f'"{spec}"' in src, f"{spec} not pinned in setup_qairt_env.py"


def test_status_reports_missing(ibe, ctx):
    st = ibe.status(ctx)
    assert st["ready"] is False
    assert [p["code"] for p in st["problems"]] == ["config_missing"]
    assert st["missing"] == ["config"]


def test_status_ready_when_json_and_disk_agree(ibe, ctx, tmp_path):
    sdk, venv = tmp_path / "real_sdk", tmp_path / "real_venv"
    (sdk / ibe.SDK_MARKER_REL).parent.mkdir(parents=True)
    (sdk / ibe.SDK_MARKER_REL).write_text("x")
    (venv / "Scripts").mkdir(parents=True)
    (venv / "Scripts" / "python.exe").write_text("x")
    ctx.config_file.parent.mkdir(parents=True)
    ctx.config_file.write_text(json.dumps({"qairt_sdk_root": str(sdk), "python_x64_venv": str(venv)}))
    st = ibe.status(ctx)
    assert st["ready"] is True and st["problems"] == [] and st["missing"] == []
    assert st["sdk_root"] == str(sdk) and st["venv_310"] == str(venv)


def test_status_stale_json_is_not_ready(ibe, ctx, tmp_path):
    """qairt_env.json exists but points at a SDK / venv that is gone -> NOT ready."""
    ctx.config_file.parent.mkdir(parents=True)
    ctx.config_file.write_text(
        json.dumps({"qairt_sdk_root": str(tmp_path / "gone"), "python_x64_venv": str(tmp_path / "gone2")})
    )
    st = ibe.status(ctx)
    assert st["ready"] is False
    assert [p["code"] for p in st["problems"]] == ["sdk_missing", "venv_missing"]


def test_release_json_drives_version_and_url(ctx):
    assert ctx.version == VERSION
    assert ctx.sdk_root.name == VERSION
    assert ctx.download_url == f"https://example.invalid/v{VERSION}.zip"


def test_cli_overrides_win(ibe, tmp_path):
    root = tmp_path / "app"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "qairt_release.json").write_text(
        json.dumps({"qairt_version": "1.0", "qairt_sdk_root_template": "C:/x/{version}",
                    "qairt_download_url_template": "https://a/{version}.zip"}), encoding="utf-8")
    a = ibe.parse_args(["--root", str(root), "--arch", "arm64", "--sdk-root", str(tmp_path / "s"),
                        "--download-url", "https://b/z.zip", "--sdk-version", "2.0"])
    c = ibe.build_ctx(a)
    assert (c.arch, c.version, c.download_url, c.sdk_root) == ("arm64", "2.0", "https://b/z.zip", tmp_path / "s")


# ── profile selection ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "off,cn,expect",
    [(None, 100, "cn"), (445, 132, "cn"), (200, 50, "official"), (320, 200, "official"),
     (100, 100, "official"), (None, None, "official"), (100, None, "official")],
)
def test_resolve_profile_auto(ibe, monkeypatch, off, cn, expect):
    def fake(url, proxy, timeout=6.0):
        return off if "pypi.org" in url else cn

    monkeypatch.setattr(ibe, "probe_ms", fake)
    assert ibe.resolve_profile("auto", "") == expect


def test_resolve_profile_forced(ibe):
    assert ibe.resolve_profile("cn", "") == "cn"
    assert ibe.resolve_profile("official", "") == "official"


def test_tool_env_cn_vs_official(ibe, ctx):
    ctx.proxy = "http://p:1"
    cn = ibe.tool_env(ctx, "cn")
    assert cn["UV_DEFAULT_INDEX"] == ibe.PYPI_CN and cn["HTTPS_PROXY"] == "http://p:1"
    assert "UV_PYTHON_INSTALL_MIRROR" in cn
    off = ibe.tool_env(ctx, "official")
    assert "UV_DEFAULT_INDEX" not in off and "UV_PYTHON_INSTALL_MIRROR" not in off


# ── reporter ──────────────────────────────────────────────────────────────────


def test_reporter_overall_is_monotonic(ibe):
    buf = io.StringIO()
    r = ibe.Reporter(ibe.STEP_WEIGHTS, out=buf)
    seen = []
    for step, _w in ibe.STEP_WEIGHTS:
        r.begin(step)
        r.progress(step, 50, 100, force=True)
        r.end(step)
    for line in buf.getvalue().splitlines():
        seen.append(json.loads(line)["overall"])
    assert seen == sorted(seen) and seen[-1] == 100.0


def test_reporter_lines_are_valid_json_with_unicode(ibe):
    buf = io.StringIO()
    r = ibe.Reporter(ibe.STEP_WEIGHTS, out=buf)
    r.log("deps", "安装中 ✓")
    obj = json.loads(buf.getvalue())
    assert obj["event"] == "log" and obj["line"] == "安装中 ✓"


def test_progress_throttled(ibe):
    buf = io.StringIO()
    r = ibe.Reporter(ibe.STEP_WEIGHTS, out=buf)
    r.begin("sdk_download")
    for i in range(50):
        r.progress("sdk_download", i, 100)
    n = sum(1 for l in buf.getvalue().splitlines() if json.loads(l)["event"] == "progress")
    assert n == 1


def test_progress_eta(ibe):
    buf = io.StringIO()
    r = ibe.Reporter(ibe.STEP_WEIGHTS, out=buf)
    r.begin("sdk_download")
    r.progress("sdk_download", 100_000, 1_100_000, speed=100_000, force=True)
    assert json.loads(buf.getvalue().splitlines()[-1])["eta"] == 10


def _eta(ibe, done, total, speed):
    buf = io.StringIO()
    r = ibe.Reporter(ibe.STEP_WEIGHTS, out=buf)
    r.begin("sdk_download")
    r.progress("sdk_download", done, total, speed=speed, force=True)
    return json.loads(buf.getvalue().splitlines()[-1])["eta"]


@pytest.mark.parametrize("speed", [0.0, 1e-9, 0.5, 1023.0])
def test_eta_is_none_when_speed_is_below_the_stall_floor(ibe, speed):
    # a stalled transfer's decayed EMA used to give total/1e-9 -> "2947935474527h"
    assert _eta(ibe, 2_000_000_000, 2_387_723_706, speed) is None


def test_eta_is_none_beyond_a_day_but_kept_just_under(ibe):
    assert _eta(ibe, 0, 100 * 1024, 1024.0) == 100  # 100 s: kept
    assert _eta(ibe, 0, 90_000 * 1024, 1024.0) is None  # 90000 s > 24 h: dropped
    assert _eta(ibe, 0, 86_400 * 1024, 1024.0) == 86_400  # exactly 24 h: kept


def test_meter_zeroes_a_decayed_speed_so_no_eta_is_shown(ibe, ctx, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(ibe.time, "monotonic", lambda: clock[0])
    m = ibe.Meter(ctx, None, 2_387_723_706)
    clock[0] += 1.0
    m.update(5_000_000)  # 5 MB/s
    assert m.speed > 1e6
    for _ in range(60):  # stalled: same size every second, the EMA decays x0.6 per tick
        clock[0] += 1.0
        m.update(5_000_000)
    assert m.speed == 0.0, "a decayed-but-positive speed must be floored to 0"


def test_aria2c_is_not_given_a_lowest_speed_limit(ibe, ctx, monkeypatch, tmp_path):
    """--lowest-speed-limit made aria2c abort slow-but-alive CDN links (errorCode=5), which looped the retries.
    Our own no-bytes-for-N-seconds watchdog is the stall detector."""
    seen = {}

    class P:
        returncode = 0
        pid = 1

        def poll(self):
            return 0

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        return P()

    monkeypatch.setattr(ibe.subprocess, "Popen", popen)
    meter = ibe.Meter(ctx, None, None)
    assert ibe._download_aria2c(ctx, Path("aria2c.exe"), "https://x/y.zip", tmp_path / "y.zip", meter, 90)
    assert not [a for a in seen["cmd"] if a.startswith("--lowest-speed-limit")]
    assert "-x16" in seen["cmd"] and "--max-tries=5" in seen["cmd"]


# ── downloads ─────────────────────────────────────────────────────────────────


def test_aria_summary_parsing_uses_real_bytes_not_file_size(ibe, tmp_path):
    """With 16 connections the target file is sparse (its length jumps to ~full at once); the real progress is
    the 'done' figure of aria2c's own summary line, and so is the speed."""
    log = tmp_path / "a.log"
    log.write_bytes(
        b"*** Download Progress Summary ***\n"
        b"[#a0e1ea 0B/0B CN:1 DL:0B]\n"
        b"[#a0e1ea 114688B/2387723706B(0%) CN:16 DL:91384B ETA:5m41s]\n"
        b"[#a0e1ea 212992B/2387723706B(0%) CN:16 DL:97285B ETA:5m20s]\n"
    )
    pos, prog = ibe._read_aria_progress(log, 0)
    assert prog == (212992, 2387723706, 97285.0) and pos == log.stat().st_size
    # nothing new -> nothing reported, position unchanged
    assert ibe._read_aria_progress(log, pos) == (pos, None)


def test_aria_summary_half_written_line_is_left_for_next_read(ibe, tmp_path):
    log = tmp_path / "a.log"
    log.write_bytes(b"[#a 100B/1000B(10%) CN:16 DL:50B]\n[#a 200B/1000B(20%) CN:16 DL:6")
    pos, prog = ibe._read_aria_progress(log, 0)
    assert prog == (100, 1000, 50.0)
    assert pos == len(b"[#a 100B/1000B(10%) CN:16 DL:50B]\n")
    with open(log, "ab") as fh:
        fh.write(b"0B]\n")
    assert ibe._read_aria_progress(log, pos)[1] == (200, 1000, 60.0)


def test_aria_summary_without_dl_field_and_missing_log(ibe, tmp_path):
    log = tmp_path / "a.log"
    log.write_bytes(b"[#a 5B/10B(50%) CN:1]\n")
    assert ibe._read_aria_progress(log, 0)[1] == (5, 10, 0.0)
    assert ibe._read_aria_progress(tmp_path / "none.log", 0) == (0, None)


def test_meter_takes_the_downloaders_own_speed(ibe, ctx, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(ibe.time, "monotonic", lambda: clock[0])
    m = ibe.Meter(ctx, None, 2_387_723_706)
    clock[0] += 0.2  # far less than a second: the EMA path would not update at all
    m.update(3_000_000, speed=2_500_000.0)
    assert m.speed == 2_500_000.0
    m.update(3_000_000, speed=10.0)  # below the stall floor -> 0, not a tiny positive number
    assert m.speed == 0.0


def test_aria2c_attempt_reports_progress_from_summary_not_file_size(ibe, ctx, monkeypatch, tmp_path):
    """The target is sparse-full-size from the first second; progress must still follow the summary lines."""
    dest = tmp_path / "y.zip"
    dest.write_bytes(b"\0" * 5000)  # looks 100% done on disk
    seen = []

    class P:
        returncode = 0
        pid = 1
        n = 0

        def poll(self):
            P.n += 1
            return None if P.n <= 2 else 0

    def popen(cmd, stdout=None, **kw):
        stdout.write(b"[#a 100B/5000B(2%) CN:16 DL:100B]\n")
        stdout.flush()
        return P()

    monkeypatch.setattr(ibe.subprocess, "Popen", popen)
    monkeypatch.setattr(ibe.time, "sleep", lambda s: None)
    meter = ibe.Meter(ctx, None, None)
    monkeypatch.setattr(meter, "update", lambda done, **k: seen.append((done, k.get("speed"))))
    assert ibe._download_aria2c(ctx, Path("aria2c.exe"), "https://x/y.zip", dest, meter, 90)
    assert seen and seen[0] == (100, 100.0), seen  # not 5000 (the on-disk size)
    assert meter.total == 5000
    assert not list(tmp_path.glob("*.aria2c.log")), "the progress log must not be left behind"


def test_aria2c_asks_for_exact_byte_summaries(ibe, ctx, monkeypatch, tmp_path):
    seen = {}

    class P:
        returncode = 0
        pid = 1

        def poll(self):
            return 0

    monkeypatch.setattr(ibe.subprocess, "Popen", lambda cmd, **kw: seen.setdefault("cmd", cmd) and P())
    ibe._download_aria2c(ctx, Path("aria2c.exe"), "https://x/y.zip", tmp_path / "y.zip", ibe.Meter(ctx, None, None), 90)
    assert "--summary-interval=1" in seen["cmd"] and "--human-readable=false" in seen["cmd"]


def test_download_skips_when_file_complete(ibe, ctx, tmp_path, monkeypatch):
    dest = tmp_path / "a.zip"
    make_sdk_zip(dest)
    monkeypatch.setattr(ibe, "_download_urllib", lambda *a, **k: pytest.fail("must not download"))
    ibe.download(ctx, "https://example.invalid/a.zip", dest, min_size=10, zip_test=True)


def test_download_retries_then_succeeds(ibe, ctx, tmp_path, monkeypatch):
    dest = tmp_path / "x.bin"
    monkeypatch.setattr(ibe.shutil, "which", lambda n: None)
    ctx.root.joinpath("data").mkdir(exist_ok=True)
    calls = {"n": 0}

    def flaky(c, url, d, meter):
        calls["n"] += 1
        if calls["n"] < 3:
            raise OSError("boom")
        d.write_bytes(b"x" * 100)

    monkeypatch.setattr(ibe, "_download_urllib", flaky)
    sleeps = []
    ibe.download(ctx, "https://example.invalid/x.bin", dest, min_size=50, total=100, sleep=sleeps.append)
    assert calls["n"] == 3 and dest.stat().st_size == 100
    assert sleeps == [2, 4]  # exponential backoff


def test_download_gives_up_with_network_code(ibe, ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(ibe.shutil, "which", lambda n: None)
    monkeypatch.setattr(ibe, "_download_urllib", lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    with pytest.raises(ibe.StepError) as ei:
        ibe.download(ctx, "https://example.invalid/x", tmp_path / "x", total=10, attempts=3, sleep=lambda s: None)
    assert ei.value.code == "network"


def test_download_rejects_truncated_file(ibe, ctx, tmp_path, monkeypatch):
    monkeypatch.setattr(ibe.shutil, "which", lambda n: None)
    dest = tmp_path / "t.bin"
    monkeypatch.setattr(ibe, "_download_urllib", lambda c, u, d, m: d.write_bytes(b"x" * 10))
    with pytest.raises(ibe.StepError):
        ibe.download(ctx, "https://example.invalid/t", dest, min_size=100, total=100, attempts=2,
                     sleep=lambda s: None)


def test_download_deletes_full_size_non_zip(ibe, ctx, tmp_path, monkeypatch):
    """A file that reached full size but is not a zip cannot be fixed by resuming: start over."""
    monkeypatch.setattr(ibe.shutil, "which", lambda n: None)
    dest = tmp_path / "bad.zip"
    seq = iter([b"N" * 100, None])

    def fake(c, u, d, m):
        data = next(seq)
        if data is None:
            make_sdk_zip(d)
        else:
            d.write_bytes(data)

    monkeypatch.setattr(ibe, "_download_urllib", fake)
    size_ok = make_sdk_zip(tmp_path / "ref.zip").stat().st_size
    ibe.download(ctx, "https://example.invalid/bad", dest, min_size=10, zip_test=True,
                 total=100 if size_ok < 100 else size_ok, attempts=3, sleep=lambda s: None)
    assert zipfile.is_zipfile(dest)


# ── SDK step ──────────────────────────────────────────────────────────────────


def no_7z(ibe, monkeypatch):
    monkeypatch.setattr(ibe, "find_extractor", lambda c: None)
    monkeypatch.setattr(ibe, "ensure_7za", lambda c: None)


def test_find_sdk_dir_layouts(ibe, tmp_path):
    for rel in (VERSION, f"qairt/{VERSION}", f"a/b/{VERSION}"):
        t = tmp_path / rel.replace("/", "_")
        (t / rel).mkdir(parents=True)
        assert ibe.find_sdk_dir(t, VERSION) == t / rel
    empty = tmp_path / "e"
    empty.mkdir()
    assert ibe.find_sdk_dir(empty, VERSION) is None


def test_sdk_from_user_zip_extracts_and_verifies_marker(ibe, ctx, tmp_path, monkeypatch):
    no_7z(ibe, monkeypatch)
    ctx.sdk_zip = make_sdk_zip(tmp_path / "mine.zip")
    ibe.step_sdk(ctx)
    assert ctx.marker.exists()
    assert ctx.sdk_zip.exists(), "a user-supplied zip must never be deleted"
    assert not (ctx.sdk_root.parent / "_extract_tmp").exists()
    states = {(e["step"], e["state"]) for e in events(ctx) if e["event"] == "step"}
    assert ("sdk_download", "skipped") in states and ("sdk_extract", "done") in states


def test_sdk_backup_launchers(ibe, ctx, tmp_path, monkeypatch):
    no_7z(ibe, monkeypatch)
    ctx.sdk_zip = make_sdk_zip(tmp_path / "mine.zip")
    ibe.step_sdk(ctx)
    bk = ctx.root / "data" / "sdk" / "qairt-scripts" / "aarch64-windows-msvc"
    assert (bk / "qairt-converter").exists() and (bk / "qnn-context-binary-generator.exe").exists()


def test_sdk_already_installed_is_skipped(ibe, ctx, monkeypatch):
    ctx.marker.parent.mkdir(parents=True)
    ctx.marker.write_text("x")
    monkeypatch.setattr(ibe, "download", lambda *a, **k: pytest.fail("must not download"))
    ibe.step_sdk(ctx)
    assert any(e["event"] == "step" and e["step"] == "sdk_download" and e["state"] == "skipped" for e in events(ctx))


def test_sdk_zip_without_marker_fails_and_keeps_user_zip(ibe, ctx, tmp_path, monkeypatch):
    no_7z(ibe, monkeypatch)
    ctx.sdk_zip = make_sdk_zip(tmp_path / "bad.zip", with_marker=False)
    with pytest.raises(ibe.StepError) as ei:
        ibe.step_sdk(ctx)
    assert ei.value.code == "extract" and ctx.sdk_zip.exists()


def test_sdk_downloaded_zip_is_kept_for_repairs(ibe, ctx, monkeypatch):
    no_7z(ibe, monkeypatch)
    monkeypatch.setattr(ibe, "remote_size", lambda u, p: 12345)
    monkeypatch.setattr(ibe, "check_disk", lambda *a, **k: None)

    def fake_download(c, url, dest, **k):
        dest.parent.mkdir(parents=True, exist_ok=True)  # the real download() does this
        make_sdk_zip(dest)

    monkeypatch.setattr(ibe, "download", fake_download)
    ibe.step_sdk(ctx)
    kept = ctx.root / "data" / "sdk" / "qairt" / f"v{VERSION}.zip"
    assert kept.exists() and not (ctx.dl_dir / "_qairt_tmp.zip").exists()
    assert ctx.marker.exists()


def test_sdk_prefers_vendor_zip_over_download(ibe, ctx, monkeypatch):
    no_7z(ibe, monkeypatch)
    v = ctx.root / "vendor" / "qairt"
    v.mkdir(parents=True)
    make_sdk_zip(v / f"v{VERSION}.zip")
    monkeypatch.setattr(ibe, "download", lambda *a, **k: pytest.fail("must not download"))
    ibe.step_sdk(ctx)
    assert ctx.marker.exists() and (v / f"v{VERSION}.zip").exists()


def test_missing_user_zip_is_reported(ibe, ctx, tmp_path):
    ctx.sdk_zip = tmp_path / "nope.zip"
    with pytest.raises(ibe.StepError) as ei:
        ibe.step_sdk(ctx)
    assert ei.value.code == "sdk_zip_missing"


# ── extractor selection ───────────────────────────────────────────────────────


def test_find_extractor_prefers_machine_7z(ibe, ctx, monkeypatch):
    monkeypatch.setattr(ibe.shutil, "which", lambda n: r"C:\tools\7z.exe" if n == "7z" else None)
    assert ibe.find_extractor(ctx) == r"C:\tools\7z.exe"


def test_find_extractor_uses_own_7za(ibe, ctx, monkeypatch):
    monkeypatch.setattr(ibe.shutil, "which", lambda n: None)
    for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(v, raising=False)
    own = ctx.root / "data" / "bin" / "7zr" / "7za.exe"
    own.parent.mkdir(parents=True)
    own.write_text("x")
    assert ibe.find_extractor(ctx) == str(own)


def test_find_extractor_none_when_nothing(ibe, ctx, monkeypatch):
    monkeypatch.setattr(ibe.shutil, "which", lambda n: None)
    for v in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.delenv(v, raising=False)
    assert ibe.find_extractor(ctx) is None


def test_7za_is_not_downloaded_when_machine_has_7z(ibe, ctx, tmp_path, monkeypatch):
    ctx.sdk_zip = make_sdk_zip(tmp_path / "mine.zip")
    monkeypatch.setattr(ibe, "find_extractor", lambda c: "FAKE-7Z")
    monkeypatch.setattr(ibe, "ensure_7za", lambda c: pytest.fail("7za must not be fetched"))
    monkeypatch.setattr(ibe, "extract_with_7z", lambda c, exe, z, out: ibe.extract_with_zipfile(c, z, out))
    ibe.step_sdk(ctx)
    assert ctx.marker.exists()


def test_falls_back_to_zipfile_when_7z_fails(ibe, ctx, tmp_path, monkeypatch):
    ctx.sdk_zip = make_sdk_zip(tmp_path / "mine.zip")
    monkeypatch.setattr(ibe, "find_extractor", lambda c: "FAKE-7Z")
    monkeypatch.setattr(ibe, "extract_with_7z", lambda *a: False)
    ibe.step_sdk(ctx)
    assert ctx.marker.exists()


def test_ensure_7za_is_nonfatal(ibe, ctx, monkeypatch):
    monkeypatch.setattr(ibe, "download", lambda *a, **k: (_ for _ in ()).throw(ibe.StepError("network", "x")))
    assert ibe.ensure_7za(ctx) is None


# ── config ────────────────────────────────────────────────────────────────────


def test_config_fields(ibe, ctx):
    ibe.step_config(ctx)
    cfg = json.loads(ctx.config_file.read_text(encoding="utf-8"))
    assert cfg["qairt_sdk_root"] == str(ctx.sdk_root)
    assert cfg["python_x64_venv"] == str(ctx.venv310)
    assert cfg["python_runtime_venv"].endswith(".venv_x64_313")
    assert cfg["qairt_runtime_subdir"] == "x86_64-windows-msvc"
    assert cfg["arch"] == "x86_64" and cfg["_version"] == VERSION
    for k in ("vs_vcvarsall", "vc_targets_path", "vs_cmake_path"):
        assert cfg[k] == ""
    assert not ctx.config_file.with_suffix(".json.tmp").exists()


def test_config_arm64_runtime(ibe, ctx):
    ctx.arch = "arm64"
    ibe.step_config(ctx)
    cfg = json.loads(ctx.config_file.read_text(encoding="utf-8"))
    assert cfg["python_runtime_venv"].endswith(".venv_arm64_313")
    assert cfg["qairt_runtime_subdir"] == "arm64x-windows-msvc" and cfg["arch"] == "aarch64"


def test_config_keeps_unrelated_keys_and_survives_garbage(ibe, ctx):
    ctx.config_file.parent.mkdir(parents=True)
    ctx.config_file.write_text(json.dumps({"custom": 1}), encoding="utf-8")
    ibe.step_config(ctx)
    assert json.loads(ctx.config_file.read_text(encoding="utf-8"))["custom"] == 1
    ctx.config_file.write_text("{ not json", encoding="utf-8")
    ibe.step_config(ctx)
    assert "qairt_sdk_root" in json.loads(ctx.config_file.read_text(encoding="utf-8"))


# ── venv / python / deps steps ────────────────────────────────────────────────


def test_python310_skipped_when_venv_exists(ibe, ctx, monkeypatch):
    ctx.venv_python.parent.mkdir(parents=True)
    ctx.venv_python.write_text("x")
    monkeypatch.setattr(ibe, "run_stream", lambda *a, **k: pytest.fail("must not run uv"))
    ibe.step_python310(ctx)


def test_python310_installs_and_falls_back_to_other_profile(ibe, ctx, monkeypatch):
    ctx.profile = "cn"
    uv = ctx.root / "data" / "bin" / "uv" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_text("x")
    seen = []

    def fake(c, step, cmd, env, cwd=None):
        sub = cmd[1:3]
        seen.append((sub, env.get("UV_PYTHON_INSTALL_MIRROR")))
        if sub == ["python", "find"]:
            return 1, []
        return (0, []) if env.get("UV_PYTHON_INSTALL_MIRROR") is None else (1, ["error sending request: connection refused"])

    monkeypatch.setattr(ibe, "run_stream", fake)
    monkeypatch.setattr(ibe.time, "sleep", lambda s: None)
    ibe.step_python310(ctx)
    assert ctx.profile == "official"
    # cn: first try + same-source retry, then official: 3 installs
    assert [s for s, _ in seen].count(["python", "install"]) == 3


def test_python310_failure_code(ibe, ctx, monkeypatch):
    uv = ctx.root / "data" / "bin" / "uv" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_text("x")
    monkeypatch.setattr(ibe, "run_stream", lambda *a, **k: (1, ["error sending request: connection refused"]))
    monkeypatch.setattr(ibe.time, "sleep", lambda s: None)
    with pytest.raises(ibe.StepError) as ei:
        ibe.step_python310(ctx)
    assert ei.value.code == "python_install"


def test_uv_missing_code(ibe, ctx, monkeypatch):
    monkeypatch.setattr(ibe.shutil, "which", lambda n: None)
    with pytest.raises(ibe.StepError) as ei:
        ibe.step_venv(ctx)
    assert ei.value.code == "uv_missing"


def test_deps_skipped_when_all_present(ibe, ctx, monkeypatch):
    monkeypatch.setattr(ibe, "missing_packages", lambda c: [])
    monkeypatch.setattr(ibe, "run_stream", lambda *a, **k: pytest.fail("must not install"))
    ibe.step_deps(ctx)
    assert any(e["event"] == "step" and e["step"] == "deps" and e["state"] == "skipped" for e in events(ctx))


def test_deps_installs_only_missing_and_splits_torch(ibe, ctx, monkeypatch):
    uv = ctx.root / "data" / "bin" / "uv" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_text("x")
    monkeypatch.setattr(ibe, "missing_packages", lambda c: [("onnx==1.19.1", False), ("torch==2.11.0+cpu", True)])
    cmds = []

    def fake(c, step, cmd, env, cwd=None):
        cmds.append([str(x) for x in cmd])
        return 0, []

    monkeypatch.setattr(ibe, "run_stream", fake)
    ibe.step_deps(ctx)
    assert any("onnx==1.19.1" in c and "torch==2.11.0+cpu" not in c for c in cmds)
    torch_cmd = next(c for c in cmds if "torch==2.11.0+cpu" in c)
    assert "--extra-index-url" in torch_cmd and ibe.TORCH_INDEX_OFFICIAL in torch_cmd


def test_deps_torch_uses_cn_mirror_first_on_cn(ibe, ctx, monkeypatch):
    ctx.profile = "cn"
    uv = ctx.root / "data" / "bin" / "uv" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_text("x")
    monkeypatch.setattr(ibe, "missing_packages", lambda c: [("torch==2.11.0+cpu", True)])
    cmds = []

    def fake(c, step, cmd, env, cwd=None):
        cmds.append([str(x) for x in cmd])
        return (1, ["cn mirror miss"]) if len(cmds) == 1 else (0, [])

    monkeypatch.setattr(ibe, "run_stream", fake)
    ibe.step_deps(ctx)
    assert "--find-links" in cmds[0] and ibe.TORCH_FINDLINKS_CN in cmds[0]
    assert "--extra-index-url" in cmds[1]


def test_deps_failure_code(ibe, ctx, monkeypatch):
    uv = ctx.root / "data" / "bin" / "uv" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_text("x")
    monkeypatch.setattr(ibe, "missing_packages", lambda c: [("onnx==1.19.1", False)])
    monkeypatch.setattr(ibe, "run_stream", lambda *a, **k: (1, ["resolver error"]))
    with pytest.raises(ibe.StepError) as ei:
        ibe.step_deps(ctx)
    assert ei.value.code == "deps" and "resolver error" in ei.value.message


# ── disk space ────────────────────────────────────────────────────────────────


def test_disk_space_error(ibe, ctx, monkeypatch):
    monkeypatch.setattr(ibe, "free_bytes", lambda p: 100 * 1024 * 1024)
    with pytest.raises(ibe.StepError) as ei:
        ibe.check_disk(ctx, need_venv=True, need_sdk=False, zip_total=None)
    assert ei.value.code == "disk_space" and "converter venv" in ei.value.message


def test_disk_space_ok_and_not_needed(ibe, ctx, monkeypatch):
    monkeypatch.setattr(ibe, "free_bytes", lambda p: 1 << 40)
    ibe.check_disk(ctx, True, True, 2 << 30)
    monkeypatch.setattr(ibe, "free_bytes", lambda p: 0)
    ibe.check_disk(ctx, False, False, None)


# ── single instance ───────────────────────────────────────────────────────────


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt lock")
def test_single_instance_lock(ibe, tmp_path):
    a = ibe.SingleInstance(tmp_path / "x.lock")
    b = ibe.SingleInstance(tmp_path / "x.lock")
    assert a.acquire() is True
    assert b.acquire() is False


# ── main() ────────────────────────────────────────────────────────────────────


def test_main_check_prints_one_json_line(ibe, ctx, capsys):
    rc = ibe.main(["--root", str(ctx.root), "--arch", "x64", "--envs-dir", str(ctx.envs_dir), "--check"])
    out = capsys.readouterr().out.strip().splitlines()
    assert rc == 0 and len(out) == 1 and json.loads(out[0])["ready"] is False


def test_main_bad_root_returns_2(ibe, tmp_path, capsys):
    rc = ibe.main(["--root", str(tmp_path / "nope")])
    assert rc == 2
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert last["event"] == "result" and last["ok"] is False and last["code"] == "bad_arguments"


@pytest.mark.skipif(sys.platform != "win32", reason="windows only")
def test_main_runs_steps_in_order_and_reports_success(ibe, ctx, monkeypatch, capsys):
    order = []
    for name in ("step_python310", "step_venv", "step_deps", "step_sdk", "step_config", "step_verify"):
        monkeypatch.setattr(ibe, name, lambda c, n=name: order.append(n))
    monkeypatch.setattr(ibe, "resolve_profile", lambda m, p: "official")
    monkeypatch.setattr(ibe, "check_disk", lambda *a, **k: None)
    rc = ibe.main(["--root", str(ctx.root), "--arch", "x64", "--envs-dir", str(ctx.envs_dir)])
    assert rc == 0
    assert order == ["step_python310", "step_venv", "step_deps", "step_sdk", "step_config", "step_verify"]
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    assert lines[0]["event"] == "start" and lines[-1] == {**lines[-1], "event": "result", "ok": True}


@pytest.mark.skipif(sys.platform != "win32", reason="windows only")
def test_main_maps_step_error_to_exit_1(ibe, ctx, monkeypatch, capsys):
    monkeypatch.setattr(ibe, "resolve_profile", lambda m, p: "official")
    monkeypatch.setattr(ibe, "check_disk", lambda *a, **k: None)
    monkeypatch.setattr(ibe, "step_python310", lambda c: (_ for _ in ()).throw(ibe.StepError("network", "no route")))
    rc = ibe.main(["--root", str(ctx.root), "--arch", "x64", "--envs-dir", str(ctx.envs_dir)])
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 1 and last["code"] == "network" and last["message"] == "no route"


@pytest.mark.skipif(sys.platform != "win32", reason="windows only")
def test_main_second_instance_is_rejected(ibe, ctx, monkeypatch, capsys):
    held = ibe.SingleInstance(ctx.dl_dir / "builder_env.lock")
    assert held.acquire()
    rc = ibe.main(["--root", str(ctx.root), "--arch", "x64", "--envs-dir", str(ctx.envs_dir)])
    last = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert rc == 4 and last["code"] == "already_running"


@pytest.mark.skipif(sys.platform != "win32", reason="windows only")
def test_main_unexpected_exception_is_reported_not_raised(ibe, ctx, monkeypatch, capsys):
    monkeypatch.setattr(ibe, "resolve_profile", lambda m, p: "official")
    monkeypatch.setattr(ibe, "check_disk", lambda *a, **k: None)
    monkeypatch.setattr(ibe, "step_python310", lambda c: 1 / 0)
    rc = ibe.main(["--root", str(ctx.root), "--arch", "x64", "--envs-dir", str(ctx.envs_dir)])
    lines = [json.loads(l) for l in capsys.readouterr().out.splitlines() if l.strip()]
    result = next(l for l in lines if l["event"] == "result")
    assert rc == 1 and result["code"] == "internal" and "ZeroDivisionError" in result["message"]


# ── network-failure retry + source switch (the 403-from-a-mirror incident) ────


NET403 = ["error: Failed to install seed packages", "cause: HTTP status client error (403 Forbidden) for url (https://x/pip.whl)"]
CONFLICT = ["error: No solution found when resolving dependencies:", "because foo>=2 and foo<1 are incompatible"]


@pytest.mark.parametrize("lines", [
    NET403,
    ["cause: HTTP status client error (404 Not Found)"],
    ["error sending request for url", "client error (Connect)", "tcp connect error"],
    ["operation timed out"],
    ["Failed to fetch: https://pypi.org/simple/pip/"],
    ["HTTP status server error (503 Service Unavailable)"],
    ["WARNING: Retrying (Retry(total=4)) ... Max retries exceeded with url"],
    ["SSLError: certificate verify failed"],
])
def test_network_failures_are_recognised(ibe, lines):
    assert ibe.is_network_failure(lines)


@pytest.mark.parametrize("lines", [
    CONFLICT,
    ["error: A virtual environment already exists at: venv"],
    ["error: a value is required for '--python <PYTHON>'"],
    ["No space left on device"],
    [],
])
def test_other_failures_are_not_network(ibe, lines):
    assert not ibe.is_network_failure(lines)


def scripted(ibe, monkeypatch, results):
    """run_stream stand-in: pops one (rc, lines) per call and records (profile index var, cmd)."""
    calls = []

    def fake(c, step, cmd, env, cwd=None):
        calls.append({"cmd": [str(x) for x in cmd], "index": env.get("UV_DEFAULT_INDEX"), "mirror_env": env.get("PIP_INDEX_URL")})
        return results[min(len(calls) - 1, len(results) - 1)]

    monkeypatch.setattr(ibe, "run_stream", fake)
    sleeps = []
    return calls, sleeps


def run(ibe, ctx, sleeps, builder=lambda p: ["uv", "x"]):
    return ibe.run_with_fallback(ctx, "venv", builder, sleep=sleeps.append)


def test_success_first_time_makes_one_call_and_no_noise(ibe, ctx, monkeypatch):
    calls, sleeps = scripted(ibe, monkeypatch, [(0, [])])
    assert run(ibe, ctx, sleeps)[0] == 0
    assert len(calls) == 1 and sleeps == []
    assert not [e for e in events(ctx) if e["event"] == "log"]


def test_one_transient_403_is_fixed_by_a_same_source_retry(ibe, ctx, monkeypatch):
    ctx.profile = "cn"
    calls, sleeps = scripted(ibe, monkeypatch, [(2, NET403), (0, [])])
    rc, _ = run(ibe, ctx, sleeps)
    assert rc == 0 and len(calls) == 2
    assert calls[0]["index"] == calls[1]["index"] == ibe.PYPI_CN  # SAME source both times
    assert sleeps == [3.0] and ctx.profile == "cn"


def test_two_failures_switch_to_the_other_source_and_keep_it(ibe, ctx, monkeypatch):
    ctx.profile = "cn"
    calls, sleeps = scripted(ibe, monkeypatch, [(2, NET403), (2, NET403), (0, [])])
    rc, _ = run(ibe, ctx, sleeps)
    assert rc == 0 and len(calls) == 3
    assert calls[0]["index"] == calls[1]["index"] == ibe.PYPI_CN
    assert calls[2]["index"] is None  # official = no mirror variable
    assert ctx.profile == "official", "the source that worked is kept for the later steps"


def test_switch_works_from_official_to_cn_too(ibe, ctx, monkeypatch):
    ctx.profile = "official"
    calls, sleeps = scripted(ibe, monkeypatch, [(2, NET403), (2, NET403), (0, [])])
    assert run(ibe, ctx, sleeps)[0] == 0
    assert calls[2]["index"] == ibe.PYPI_CN and ctx.profile == "cn"


def test_failing_everywhere_gives_up_after_exactly_three_attempts(ibe, ctx, monkeypatch):
    calls, sleeps = scripted(ibe, monkeypatch, [(2, NET403)])
    rc, tail = run(ibe, ctx, sleeps)
    assert rc == 2 and len(calls) == 3 and tail == NET403


def test_a_non_network_error_is_not_retried(ibe, ctx, monkeypatch):
    calls, sleeps = scripted(ibe, monkeypatch, [(2, CONFLICT)])
    rc, tail = run(ibe, ctx, sleeps)
    assert rc == 2 and len(calls) == 1 and sleeps == [] and tail == CONFLICT
    assert any("not a network error" in e["line"] for e in events(ctx) if e["event"] == "log")


@pytest.mark.parametrize("forced", ["cn", "official"])
def test_a_forced_mirror_is_never_switched(ibe, ctx, monkeypatch, forced):
    ctx.mirror, ctx.profile = forced, forced
    calls, sleeps = scripted(ibe, monkeypatch, [(2, NET403)])
    rc, _ = run(ibe, ctx, sleeps)
    assert rc == 2 and len(calls) == 2, "same-source retry only"
    assert len({c["index"] for c in calls}) == 1 and ctx.profile == forced
    assert any("forced with --mirror" in e["line"] for e in events(ctx) if e["event"] == "log")


def test_the_command_is_built_per_source(ibe, ctx, monkeypatch):
    ctx.profile = "cn"
    seen = []
    scripted(ibe, monkeypatch, [(2, NET403)])
    run(ibe, ctx, [], builder=lambda p: seen.append(p) or ["uv", p])
    assert seen == ["cn", "cn", "official"]


def test_venv_creation_uses_allow_existing_and_retries(ibe, ctx, monkeypatch):
    uv = ctx.root / "data" / "bin" / "uv" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_text("x")
    monkeypatch.setattr(ibe.time, "sleep", lambda s: None)
    calls, _ = scripted(ibe, monkeypatch, [(2, NET403), (0, [])])

    def fake(c, step, cmd, env, cwd=None):
        calls.append([str(x) for x in cmd])
        if len(calls) == 1:
            return 2, NET403
        (ctx.venv310 / "Scripts").mkdir(parents=True, exist_ok=True)
        (ctx.venv310 / "Scripts" / "activate.bat").write_text("x")
        return 0, []

    calls.clear()
    monkeypatch.setattr(ibe, "run_stream", fake)
    ibe.step_venv(ctx)
    venv_cmds = [c for c in calls if "venv" in c]
    assert len(venv_cmds) == 2 and all("--allow-existing" in c for c in venv_cmds), "a half-made venv must not break the retry"


def test_torch_falls_back_to_the_other_index_on_a_network_error(ibe, ctx, monkeypatch):
    uv = ctx.root / "data" / "bin" / "uv" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_text("x")
    ctx.profile = "cn"
    monkeypatch.setattr(ibe, "missing_packages", lambda c: [("torch==2.11.0+cpu", True)])
    monkeypatch.setattr(ibe.time, "sleep", lambda s: None)
    cmds = []

    def fake(c, step, cmd, env, cwd=None):
        cmds.append([str(x) for x in cmd])
        return (2, NET403) if len(cmds) <= 2 else (0, [])

    monkeypatch.setattr(ibe, "run_stream", fake)
    ibe.step_deps(ctx)
    assert ibe.TORCH_FINDLINKS_CN in " ".join(cmds[0]) and ibe.TORCH_FINDLINKS_CN in " ".join(cmds[1])
    assert ibe.TORCH_INDEX_OFFICIAL in " ".join(cmds[2]) and ctx.profile == "official"


def test_torch_missing_version_on_the_mirror_tries_the_other_index(ibe, ctx, monkeypatch):
    """Not a network error: the mirror simply lacks that exact version. The other index may have it."""
    uv = ctx.root / "data" / "bin" / "uv" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_text("x")
    ctx.profile = "cn"
    monkeypatch.setattr(ibe, "missing_packages", lambda c: [("torch==2.11.0+cpu", True)])
    cmds = []

    def fake(c, step, cmd, env, cwd=None):
        cmds.append([str(x) for x in cmd])
        return (1, ["ERROR: No matching distribution found for torch==2.11.0+cpu"]) if len(cmds) == 1 else (0, [])

    monkeypatch.setattr(ibe, "run_stream", fake)
    ibe.step_deps(ctx)
    assert len(cmds) == 2 and ibe.TORCH_INDEX_OFFICIAL in " ".join(cmds[1]) and ctx.profile == "official"


def test_torch_missing_version_is_not_chased_when_the_mirror_was_forced(ibe, ctx, monkeypatch):
    uv = ctx.root / "data" / "bin" / "uv" / "uv.exe"
    uv.parent.mkdir(parents=True)
    uv.write_text("x")
    ctx.profile, ctx.mirror = "cn", "cn"
    monkeypatch.setattr(ibe, "missing_packages", lambda c: [("torch==2.11.0+cpu", True)])
    cmds = []
    monkeypatch.setattr(ibe, "run_stream", lambda c, s, cmd, env, cwd=None: (cmds.append(1), (1, ["No matching distribution found"]))[1])
    with pytest.raises(ibe.StepError) as ei:
        ibe.step_deps(ctx)
    assert ei.value.code == "deps" and len(cmds) == 1


def test_mirror_choice_reaches_the_context(ibe, tmp_path):
    root = tmp_path / "app"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "qairt_release.json").write_text(
        '{"qairt_version":"1","qairt_sdk_root_template":"C:/x/{version}","qairt_download_url_template":"https://a/{version}.zip"}')
    for m in ("auto", "cn", "official"):
        c = ibe.build_ctx(ibe.parse_args(["--root", str(root), "--arch", "x64", "--mirror", m]))
        assert c.mirror == m


# ── SDK ownership marker (lets the uninstaller offer to delete ONLY an SDK we extracted) ──


def test_extracting_the_sdk_writes_the_owner_marker(ibe, ctx, tmp_path, monkeypatch):
    no_7z(ibe, monkeypatch)
    ctx.sdk_zip = make_sdk_zip(tmp_path / "mine.zip")
    ibe.step_sdk(ctx)
    m = ctx.sdk_root / ibe.OWNER_MARKER_NAME
    assert m.is_file()
    info = json.loads(m.read_text(encoding="utf-8"))
    assert info["version"] == VERSION and info["app_root"] == str(ctx.root)
    assert ibe.sdk_owned_by_us(ctx)


def test_an_sdk_that_was_already_there_is_not_claimed(ibe, ctx, monkeypatch):
    """Installed by the user / another tool before us: we skip it, so we must never mark it as ours."""
    ctx.marker.parent.mkdir(parents=True)
    ctx.marker.write_text("x")
    monkeypatch.setattr(ibe, "download", lambda *a, **k: pytest.fail("must not download"))
    ibe.step_sdk(ctx)
    assert not (ctx.sdk_root / ibe.OWNER_MARKER_NAME).exists()
    ibe.step_config(ctx)
    assert json.loads(ctx.config_file.read_text(encoding="utf-8"))["qairt_sdk_installed_by_app"] is False


def test_config_records_ownership_when_we_extracted_it(ibe, ctx, tmp_path, monkeypatch):
    no_7z(ibe, monkeypatch)
    ctx.sdk_zip = make_sdk_zip(tmp_path / "mine.zip")
    ibe.step_sdk(ctx)
    ibe.step_config(ctx)
    cfg = json.loads(ctx.config_file.read_text(encoding="utf-8"))
    assert cfg["qairt_sdk_installed_by_app"] is True and cfg["qairt_sdk_root"] == str(ctx.sdk_root)


def test_owner_marker_write_failure_is_not_fatal(ibe, ctx, tmp_path, monkeypatch):
    no_7z(ibe, monkeypatch)
    ctx.sdk_zip = make_sdk_zip(tmp_path / "mine.zip")
    real = ibe.Path.write_text

    def boom(self, *a, **k):
        if self.name == ibe.OWNER_MARKER_NAME:
            raise PermissionError("read-only")
        return real(self, *a, **k)

    monkeypatch.setattr(ibe.Path, "write_text", boom)
    ibe.step_sdk(ctx)  # must not raise
    assert ctx.marker.exists() and not ibe.sdk_owned_by_us(ctx)
    assert any("could not write" in e.get("line", "") for e in events(ctx) if e["event"] == "log")
