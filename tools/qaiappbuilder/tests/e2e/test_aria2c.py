# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - aria2c management: status / start / stop / cancel (MH-05).

``/api/aria2c/*`` reports and controls the shared aria2c RPC daemon (port **6800**) used by the Download Center. (The model-catalog
downloads in ``test_downloads.py`` use a different daemon on 6810.) The binary is looked for on ``PATH`` and then in ``<data>/bin``
(also ``<data>/bin/aria2c``); on Windows a missing one is downloaded from GitHub on the first ``start``.

How it is exercised without the internet: the repo's own ``data/bin/aria2c/aria2c.exe`` is put on the backend's ``PATH``; the "no binary"
case is only READ (``status``), because ``start`` would download one. Ports 6800 and 6810 must be free, otherwise the module skips.

NOT covered (deliberately):

* **The auto-install download** (``can_auto_install`` -> ``installing`` -> ``done`` / ``failed``): it reaches github.com.
* **A proxy for that download**, the RPC daemon being crash-restarted, and ``cancel`` of a real in-flight task (the Download Center
  needs a real release download; the model-catalog equivalent is in ``test_downloads.py``).
* The Windows Job Object that kills the daemon when the backend dies is checked only through its effect: no ``aria2c`` outlives the backend.

Isolation (verified): the daemon is started by a throw-away backend with ``cwd`` in the temp dir; the module asserts no ``aria2c`` process
outlives it and that port 6800 is free again, and records the set of ``aria2c`` PIDs it did not start.

Observed behaviour worth pinning:

* ``start`` is idempotent (same PID), ``stop`` is idempotent and always ``ok``; a stop takes several seconds (it asks the RPC to shut down
  before killing the process).
* ``status`` with no binary says ``available: true`` and ``can_auto_install: true`` with an EMPTY ``exe_path`` ("usable or installable").
* ``POST /cancel/{id}`` of an unknown id is a 200 ``cancelled: false``, and the id route accepts slashes.
* MH-ISS-06: if ``<data>/bin`` does not exist the daemon cannot start (``NotADirectoryError`` from the cwd) and ``start`` answers
  ``status: "error"`` - even though the binary was found.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import psutil
import pytest

from tests.e2e import conftest as c
from tests.e2e._owned_backend import OwnedBackend, owned_backend
from tests.e2e.conftest import REPO_ROOT

pytestmark = pytest.mark.e2e

A = "/api/aria2c"
ARIA_DIR = REPO_ROOT / "data" / "bin" / "aria2c"
RPC = 6800
STATUS_KEYS = {"available", "can_auto_install", "exe_path", "daemon_running", "daemon_pid", "rpc_port", "install_status", "install_error", "bin_dir"}


def _aria_pids() -> set[int]:
    return {p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("aria2c")}


@pytest.fixture(scope="module", autouse=True)
def daemon_ports_and_processes_clean() -> Iterator[None]:
    for port in (RPC, 6810):
        if not c._port_is_free(c.E2E_HOST, port):
            pytest.skip(f"port {port} (aria2c RPC) is in use by another process")
    if not (ARIA_DIR / "aria2c.exe").is_file():
        pytest.skip("data/bin/aria2c/aria2c.exe is not installed")
    before = _aria_pids()
    yield
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and (_aria_pids() - before):
        time.sleep(0.3)
    assert not (_aria_pids() - before), "an aria2c process outlived the backend that started it"
    assert c._port_is_free(c.E2E_HOST, RPC), "port 6800 is still held after the module"


def _wait_stopped(timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end and not c._port_is_free(c.E2E_HOST, RPC):
        time.sleep(0.3)


# --------------------------------------------------------------------------
# no binary anywhere (read-only)
# --------------------------------------------------------------------------


def test_status_with_no_binary_says_available_and_installable_but_has_no_path() -> None:
    """KNOWN GAP / DESIGN QUESTION (current behaviour, pinned) - MH-ISS-07: ``available: true`` with ``exe_path: ""``."""
    with owned_backend() as ob:
        cl = ob.start()
        s = cl.get(f"{A}/status").json()
        assert set(s) == STATUS_KEYS
        assert (s["available"], s["can_auto_install"], s["exe_path"]) == (True, True, "")
        assert (s["daemon_running"], s["daemon_pid"], s["rpc_port"], s["install_status"], s["install_error"]) == (False, None, RPC, "idle", "")
        assert Path(s["bin_dir"]) == ob.data_dir / "bin"


def test_stop_and_cancel_with_nothing_running_are_harmless() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        for _ in range(2):
            r = cl.post(f"{A}/stop")
            assert r.status_code == 200 and r.json() == {"status": "ok", "message": "aria2c daemon stopped"}
        for tid, shown in (("nope", "nope"), ("a/b/c", "a/b/c"), ("0" * 200, "0" * 200), ("a%20b", "a b"), ("a%2Fb", "a/b"), ("%C3%A9", "é")):
            r = cl.post(f"{A}/cancel/{tid}")
            assert r.status_code == 200 and r.json() == {"cancelled": False, "task_id": shown}, tid
        # a blank id is refused, not treated as "not found"
        for blank in ("%20", "%20%20"):
            r = cl.post(f"{A}/cancel/{blank}")
            assert r.status_code == 400 and r.json()["code"] == "service_release.invalid_download_request" and r.json()["message"] == "task_id is required"


def test_the_routes_accept_only_the_documented_methods() -> None:
    with owned_backend() as ob:
        cl = ob.start()
        assert cl.get(f"{A}/start").status_code in (404, 405) and cl.get(f"{A}/stop").status_code in (404, 405)
        assert cl.post(f"{A}/status").status_code in (404, 405) and cl.get(f"{A}/cancel/x").status_code in (404, 405)
        assert cl.delete(f"{A}/status").status_code in (404, 405)


# --------------------------------------------------------------------------
# own backends: the daemon's lifetime, and a missing bin directory
# --------------------------------------------------------------------------


def test_the_daemon_dies_with_the_backend() -> None:
    import tempfile

    before = _aria_pids()
    with owned_backend() as ob:
        (ob.data_dir / "bin").mkdir(parents=True, exist_ok=True)
        cl = ob.start(path_prepend=ARIA_DIR, cwd=Path(tempfile.gettempdir()))
        pid = cl.post(f"{A}/start").json()["pid"]
        assert pid in _aria_pids()
        ob.stop()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline and pid in _aria_pids():
        time.sleep(0.3)
    assert pid not in _aria_pids() and (_aria_pids() - before) == set()
    _wait_stopped()


# --------------------------------------------------------------------------
# the data/bin directory is missing
# --------------------------------------------------------------------------


def test_start_fails_when_the_bin_directory_does_not_exist_even_though_the_binary_is_found() -> None:
    """MH-ISS-06 (spawn failures are now explained): the daemon is spawned with ``cwd=<data>/bin``; when that folder is absent
    ``subprocess.Popen`` raises ``NotADirectoryError`` (WinError 267). The route answers ``error`` with the real reason
    (``aria2c failed to start: [WinError 267] ...``) and ``status`` carries the same text in ``install_error``.
    Creating the folder is all it takes to fix it (the previous tests do)."""
    import tempfile

    with owned_backend() as ob:
        assert not (ob.data_dir / "bin").exists()
        cl = ob.start(path_prepend=ARIA_DIR, cwd=Path(tempfile.gettempdir()))
        before = _aria_pids()
        r = cl.post(f"{A}/start")
        body = r.json()
        assert r.status_code == 200 and (body["status"], body["pid"]) == ("error", None)
        assert body["message"].startswith("aria2c failed to start") and "267" in body["message"], body
        assert _aria_pids() == before and c._port_is_free(c.E2E_HOST, RPC)
        s = cl.get(f"{A}/status").json()
        assert (s["daemon_running"], s["available"]) == (False, True)
        assert s["install_error"].startswith("aria2c failed to start") and "267" in s["install_error"], s
        assert "failed to start aria2c" in ob.log_text() and "267" in ob.log_text()
