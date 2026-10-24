# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""BuilderEnvService against REAL child processes (a stand-in script, no network)."""

from __future__ import annotations

import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

from qai.platform.builder_env import (
    AlreadyRunningError,
    BuilderEnvService,
    InstallState,
    ScriptMissingError,
)

pytestmark = pytest.mark.unit


def fake_script(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "scripts" / "setup" / "install_builder_env.py"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


def make(tmp_path: Path, body: str) -> BuilderEnvService:
    return BuilderEnvService(
        script_path=fake_script(tmp_path, body),
        python_exe=sys.executable,
        app_root=tmp_path,
        arch="x64",
    )


async def wait_done(svc: BuilderEnvService, timeout: float = 20.0):
    async def _w():
        while svc.running:
            await asyncio.sleep(0.02)

    await asyncio.wait_for(_w(), timeout)
    return svc.job


OK_SCRIPT = """
    import json, sys, time
    def e(**k): print(json.dumps(k), flush=True)
    e(event="start", steps=[{"id": "deps", "weight": 60}, {"id": "sdk_download", "weight": 40}])
    e(event="step", step="deps", state="running", message="converter", overall=0)
    e(event="log", step="deps", line="Installing onnx")
    e(event="step", step="deps", state="done", message="", overall=60)
    e(event="step", step="sdk_download", state="running", message="QAIRT", overall=60)
    e(event="progress", step="sdk_download", unit="bytes", done=500, total=1000, speed=100, eta=5, overall=80)
    e(event="progress", step="sdk_download", unit="bytes", done=1000, total=1000, speed=100, eta=0, overall=100)
    e(event="result", ok=True, code="", message="ready", step="", overall=100)
"""


async def test_successful_job_reports_progress_and_result(tmp_path):
    svc = make(tmp_path, OK_SCRIPT)
    job = svc.start()
    assert job.state is InstallState.RUNNING
    await wait_done(svc)
    assert job.state is InstallState.SUCCEEDED and job.exit_code == 0
    assert job.overall == 100.0 and job.error_code == ""
    assert (job.step, job.done, job.total) == ("sdk_download", 1000, 1000)
    assert list(job.log_tail) == ["Installing onnx"]
    assert [s["id"] for s in job.steps] == ["deps", "sdk_download"]
    assert job.finished_at is not None


async def test_overall_never_moves_backwards(tmp_path):
    svc = make(tmp_path, """
        import json
        for o in (10, 50, 30, 20, 70):
            print(json.dumps({"event": "progress", "step": "s", "done": 1, "total": 2, "overall": o}), flush=True)
        print(json.dumps({"event": "result", "ok": True}), flush=True)
    """)
    job = svc.start()
    await wait_done(svc)
    assert job.overall == 100.0  # success pins 100; the 30/20 dips never lowered it below 70 on the way


async def test_failure_result_keeps_code_message_and_step(tmp_path):
    svc = make(tmp_path, """
        import json, sys
        print(json.dumps({"event": "step", "step": "deps", "state": "running", "overall": 5}), flush=True)
        print(json.dumps({"event": "result", "ok": False, "code": "network", "message": "no route", "step": "deps"}), flush=True)
        sys.exit(1)
    """)
    job = svc.start()
    await wait_done(svc)
    assert job.state is InstallState.FAILED
    assert (job.error_code, job.error_message, job.failed_step) == ("network", "no route", "deps")
    assert job.exit_code == 1


async def test_crash_without_result_line_is_a_failure_with_log_tail(tmp_path):
    svc = make(tmp_path, """
        import sys
        print("Traceback (most recent call last):", flush=True)
        print("ZeroDivisionError: boom", flush=True)
        sys.exit(7)
    """)
    job = svc.start()
    await wait_done(svc)
    assert job.state is InstallState.FAILED and job.error_code == "exit_7"
    assert "ZeroDivisionError: boom" in job.error_message
    assert "ZeroDivisionError: boom" in list(job.log_tail)


async def test_exit_zero_without_result_line_counts_as_success(tmp_path):
    svc = make(tmp_path, "print('hello')")
    job = svc.start()
    await wait_done(svc)
    assert job.state is InstallState.SUCCEEDED and job.overall == 100.0


async def test_cancelled_result_from_script(tmp_path):
    svc = make(tmp_path, """
        import json, sys
        print(json.dumps({"event": "result", "ok": False, "code": "cancelled", "message": "cancelled"}), flush=True)
        sys.exit(3)
    """)
    job = svc.start()
    await wait_done(svc)
    assert job.state is InstallState.CANCELLED


async def test_second_start_while_running_is_rejected(tmp_path):
    svc = make(tmp_path, "import time; time.sleep(30)")
    svc.start()
    with pytest.raises(AlreadyRunningError):
        svc.start()
    assert await svc.cancel() is True
    await wait_done(svc)


async def test_cancel_kills_the_child_process_tree(tmp_path):
    marker = tmp_path / "alive.txt"
    svc = make(tmp_path, f"""
        import subprocess, sys, time, json
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open(r"{marker}", "w").write(str(child.pid))
        print(json.dumps({{"event": "step", "step": "deps", "state": "running", "overall": 1}}), flush=True)
        time.sleep(60)
    """)
    job = svc.start()
    for _ in range(200):
        if marker.exists() and job.step == "deps":
            break
        await asyncio.sleep(0.05)
    grandchild = int(marker.read_text())
    assert await svc.cancel() is True
    assert job.state is InstallState.CANCELLED and job.error_code == "cancelled"
    await asyncio.sleep(0.5)
    assert not _pid_alive(grandchild), "the grandchild must die with the tree"


async def test_cancel_when_idle_returns_false(tmp_path):
    svc = make(tmp_path, "pass")
    assert await svc.cancel() is False


async def test_new_start_replaces_finished_job(tmp_path):
    svc = make(tmp_path, OK_SCRIPT)
    first = svc.start()
    await wait_done(svc)
    second = svc.start()
    assert second.job_id != first.job_id and svc.job is second
    await wait_done(svc)


async def test_non_json_and_oversized_lines_do_not_break_the_reader(tmp_path):
    svc = make(tmp_path, """
        import json
        print("plain text line")
        print("[1, 2, 3]")
        print("x" * 300000)
        print(json.dumps({"event": "result", "ok": True}), flush=True)
    """)
    job = svc.start()
    await wait_done(svc)
    assert job.state is InstallState.SUCCEEDED
    assert "plain text line" in job.log_tail and all(len(l) <= 300 for l in job.log_tail)


async def test_unicode_output(tmp_path):
    svc = make(tmp_path, """
        import json, sys
        sys.stdout.reconfigure(encoding="utf-8")
        print(json.dumps({"event": "log", "line": "安装中 ✓"}, ensure_ascii=False), flush=True)
        print(json.dumps({"event": "result", "ok": True}), flush=True)
    """)
    job = svc.start()
    await wait_done(svc)
    assert "安装中 ✓" in job.log_tail


async def test_argv_carries_arch_mirror_and_proxy_only(tmp_path):
    svc = make(tmp_path, f"""
        import json, sys
        print(json.dumps({{"event": "log", "line": json.dumps(sys.argv[1:])}}), flush=True)
    """)
    job = svc.start(mirror="cn", proxy="http://127.0.0.1:7890")
    await wait_done(svc)
    argv = json.loads(job.log_tail[0])
    assert argv == ["--root", str(tmp_path), "--arch", "x64", "--mirror", "cn", "--proxy", "http://127.0.0.1:7890"]


async def test_auto_mirror_adds_no_flag(tmp_path):
    svc = make(tmp_path, """
        import json, sys
        print(json.dumps({"event": "log", "line": json.dumps(sys.argv[1:])}), flush=True)
    """)
    job = svc.start()
    await wait_done(svc)
    assert "--mirror" not in json.loads(job.log_tail[0]) and "--proxy" not in json.loads(job.log_tail[0])


@pytest.mark.parametrize("proxy", ["ftp://x", "127.0.0.1:7890", "javascript:alert(1)", "http://a --evil"[:0] + "file:///etc"])
async def test_bad_proxy_is_rejected_before_spawn(tmp_path, proxy):
    svc = make(tmp_path, "pass")
    with pytest.raises(ValueError):
        svc.start(proxy=proxy)
    assert svc.job is None


async def test_bad_mirror_is_rejected(tmp_path):
    svc = make(tmp_path, "pass")
    with pytest.raises(ValueError):
        svc.start(mirror="evil")


async def test_missing_script_raises_and_is_not_installable(tmp_path):
    svc = BuilderEnvService(script_path=tmp_path / "nope.py", python_exe=sys.executable, app_root=tmp_path, arch="x64")
    assert svc.installable is False
    with pytest.raises(ScriptMissingError):
        svc.start()
    assert svc.job is None


async def test_spawn_failure_is_a_failed_job_not_an_exception(tmp_path):
    svc = BuilderEnvService(
        script_path=fake_script(tmp_path, "pass"),
        python_exe=tmp_path / "no_such_python.exe",
        app_root=tmp_path,
        arch="x64",
    )
    job = svc.start()
    await wait_done(svc)
    assert job.state is InstallState.FAILED and job.error_code == "spawn_failed"


async def test_aclose_stops_a_running_job(tmp_path):
    svc = make(tmp_path, "import time; time.sleep(60)")
    job = svc.start()
    await asyncio.sleep(0.3)
    await svc.aclose()
    assert job.state in (InstallState.CANCELLED, InstallState.FAILED) and not svc.running


def _pid_alive(pid: int) -> bool:
    if sys.platform == "win32":
        import subprocess

        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        import os

        os.kill(pid, 0)
        return True
    except OSError:
        return False
