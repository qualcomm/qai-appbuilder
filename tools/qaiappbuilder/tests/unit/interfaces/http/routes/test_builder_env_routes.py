# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP surface of the model-conversion environment installer (real service + stand-in script)."""

from __future__ import annotations

import json
import sys
import textwrap
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from interfaces.http.error_handlers import register_error_handlers
from interfaces.http.routes import builder_env
from qai.platform.builder_env import SDK_MARKER_REL, BuilderEnvService

pytestmark = pytest.mark.unit

SLOW = "import time; time.sleep(60)"
OK = """
    import json
    print(json.dumps({"event": "step", "step": "deps", "state": "running", "overall": 10}), flush=True)
    print(json.dumps({"event": "progress", "step": "deps", "unit": "bytes", "done": 5, "total": 10, "speed": 2, "eta": 3, "overall": 40}), flush=True)
    print(json.dumps({"event": "result", "ok": True, "message": "ready"}), flush=True)
"""


def build(tmp_path: Path, body: str | None, *, service: bool = True):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    svc = None
    if service:
        script = tmp_path / "scripts" / "setup" / "install_builder_env.py"
        script.parent.mkdir(parents=True, exist_ok=True)
        if body is not None:
            script.write_text(textwrap.dedent(body), encoding="utf-8")
        svc = BuilderEnvService(script_path=script, python_exe=sys.executable, app_root=tmp_path, arch="x64")
    container = SimpleNamespace(data_paths=SimpleNamespace(root=data), builder_env_service=svc)
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(builder_env.build_router(container=container))
    return TestClient(app), container, svc


def wait_state(client: TestClient, want: str, timeout: float = 20.0) -> dict:
    end = time.time() + timeout
    body: dict = {}
    while time.time() < end:
        body = client.get("/api/system/builder-env").json()
        if body["job"] and body["job"]["state"] == want:
            return body
        time.sleep(0.05)
    raise AssertionError(f"never reached {want}: {body}")


def write_cfg(tmp_path: Path, payload) -> None:
    f = tmp_path / "data" / "config" / "qairt_env.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(payload), encoding="utf-8")


def test_get_when_nothing_is_installed(tmp_path):
    client, _, _ = build(tmp_path, OK)
    with client:
        r = client.get("/api/system/builder-env")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is False and body["missing"] == ["config"]
    assert body["problems"][0]["code"] == "config_missing"
    assert body["job"] is None and body["installable"] is True


def test_get_reflects_the_disk_with_no_restart(tmp_path):
    sdk, venv = tmp_path / "sdk", tmp_path / "venv"
    client, _, _ = build(tmp_path, OK)
    with client:
        write_cfg(tmp_path, {"qairt_sdk_root": str(sdk), "python_x64_venv": str(venv)})
        stale = client.get("/api/system/builder-env").json()
        assert stale["ready"] is False and stale["missing"] == ["sdk", "venv"]  # json present, disk empty
        (sdk / SDK_MARKER_REL).parent.mkdir(parents=True)
        (sdk / SDK_MARKER_REL).write_text("MZ")
        (venv / "Scripts").mkdir(parents=True)
        (venv / "Scripts" / "python.exe").write_text("MZ")
        done = client.get("/api/system/builder-env").json()
    assert done["ready"] is True and done["problems"] == [] and done["sdk_root"] == str(sdk)


def test_install_starts_a_job_and_progress_is_visible(tmp_path):
    client, _, _ = build(tmp_path, OK)
    with client:
        r = client.post("/api/system/builder-env/install", json={})
        assert r.status_code == 202 and r.json()["status"] == "queued"
        job_id = r.json()["job_id"]
        body = wait_state(client, "succeeded")
    job = body["job"]
    assert job["job_id"] == job_id and job["overall"] == 100.0 and job["error_code"] == ""
    assert (job["step"], job["done"], job["total"]) == ("deps", 5, 10)


def test_install_without_body_uses_defaults(tmp_path):
    client, _, _ = build(tmp_path, OK)
    with client:
        assert client.post("/api/system/builder-env/install").status_code == 202
        wait_state(client, "succeeded")


def test_failed_job_exposes_code_message_and_step(tmp_path):
    client, _, _ = build(tmp_path, """
        import json, sys
        print(json.dumps({"event": "step", "step": "sdk_download", "state": "running", "overall": 30}), flush=True)
        print(json.dumps({"event": "result", "ok": False, "code": "network", "message": "no route", "step": "sdk_download"}), flush=True)
        sys.exit(1)
    """)
    with client:
        client.post("/api/system/builder-env/install", json={})
        job = wait_state(client, "failed")["job"]
    assert (job["error_code"], job["error_message"], job["failed_step"]) == ("network", "no route", "sdk_download")


def test_second_install_while_running_is_409(tmp_path):
    client, _, svc = build(tmp_path, SLOW)
    with client:
        assert client.post("/api/system/builder-env/install", json={}).status_code == 202
        r = client.post("/api/system/builder-env/install", json={})
        assert r.status_code == 409
        assert "builder_env.already_running" in json.dumps(r.json())
        client.delete("/api/system/builder-env/install")
        wait_state(client, "cancelled")


def test_cancel_running_job(tmp_path):
    client, _, _ = build(tmp_path, SLOW)
    with client:
        client.post("/api/system/builder-env/install", json={})
        r = client.delete("/api/system/builder-env/install")
        assert r.status_code == 200 and r.json() == {"cancelled": True}
        assert wait_state(client, "cancelled")["job"]["error_code"] == "cancelled"


def test_cancel_when_idle(tmp_path):
    client, _, _ = build(tmp_path, OK)
    with client:
        r = client.delete("/api/system/builder-env/install")
    assert r.status_code == 200 and r.json() == {"cancelled": False}


def test_retry_after_failure_starts_a_new_job(tmp_path):
    client, _, svc = build(tmp_path, "import sys; sys.exit(5)")
    with client:
        first = client.post("/api/system/builder-env/install", json={}).json()["job_id"]
        wait_state(client, "failed")
        second = client.post("/api/system/builder-env/install", json={}).json()["job_id"]
        wait_state(client, "failed")
    assert first != second


@pytest.mark.parametrize("proxy", ["ftp://x", "127.0.0.1:8080", "file:///c:/x"])
def test_bad_proxy_is_400_and_nothing_starts(tmp_path, proxy):
    client, _, svc = build(tmp_path, OK)
    with client:
        r = client.post("/api/system/builder-env/install", json={"proxy": proxy})
        assert r.status_code == 400  # project convention: validation failures are 400 (error_handlers.py)
        assert "builder_env.invalid_request" in json.dumps(r.json())
        assert client.get("/api/system/builder-env").json()["job"] is None


def test_unknown_mirror_is_rejected_by_the_schema(tmp_path):
    client, _, _ = build(tmp_path, OK)
    with client:
        assert client.post("/api/system/builder-env/install", json={"mirror": "evil"}).status_code == 400


def test_overlong_proxy_is_rejected(tmp_path):
    client, _, _ = build(tmp_path, OK)
    with client:
        assert client.post("/api/system/builder-env/install", json={"proxy": "http://" + "a" * 400}).status_code == 400


def test_no_service_means_404_everywhere(tmp_path):
    client, _, _ = build(tmp_path, None, service=False)
    with client:
        for method, path in (("get", "/api/system/builder-env"), ("post", "/api/system/builder-env/install"),
                             ("delete", "/api/system/builder-env/install")):
            r = getattr(client, method)(path)
            assert r.status_code == 404, (method, path, r.text)
            assert "builder_env.unavailable" in json.dumps(r.json())


def test_missing_script_reports_not_installable_and_404_on_install(tmp_path):
    client, _, _ = build(tmp_path, None)  # service exists, script file does not
    with client:
        assert client.get("/api/system/builder-env").json()["installable"] is False
        assert client.post("/api/system/builder-env/install", json={}).status_code == 404


def test_response_never_leaks_the_proxy_or_environment(tmp_path):
    client, _, _ = build(tmp_path, OK)
    with client:
        client.post("/api/system/builder-env/install", json={"proxy": "http://user:secret@127.0.0.1:9"})
        text = json.dumps(wait_state(client, "succeeded"))
    assert "secret" not in text and "user:" not in text


def test_not_gated_on_qairt_env_json(tmp_path):
    """The feature must work when qairt_env.json does not exist yet (the install creates it)."""
    client, container, _ = build(tmp_path, OK)
    assert not (tmp_path / "data" / "config" / "qairt_env.json").exists()
    with client:
        assert client.get("/api/system/builder-env").status_code == 200
        assert client.post("/api/system/builder-env/install", json={}).status_code == 202
        wait_state(client, "succeeded")
