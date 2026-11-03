# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared fixtures for the end-to-end (E2E) suite.

Design decisions (see ``FEATURE-INVENTORY.md`` for the evidence behind each):

* **Real backend subprocess.** Tests talk HTTP to a real ``python -m apps.api``
  process, not an in-process TestClient.
* **Port 8989, fail fast.** The suite assumes no other AppBuilder is running.
  If 8989 is taken we ABORT the whole session with the owning PID - we never
  kill a process we did not start (it is the user's real instance).
* **Isolated data files, shared keyring.** ``QAI_DATA__DATA_DIR`` points at a
  temp directory, so the database / ``_master.key`` / endpoint file are
  throw-away. The OS keyring is NOT isolated (the Windows credential store is
  keyed by service/key only). Consequences: never exercise logout (it deletes
  the user's real QAI Service JWT from the keyring), and any test that writes
  the keyring must create-and-delete its own ``e2e-`` prefixed data.
* **Platform selectors.** ``win_arm64`` / ``win_x64`` / ``linux_arm64`` markers
  restrict a test to one platform; no marker = every platform.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.e2e import _failure_logs

# tools/qaiappbuilder  (this file: <repo>/tests/e2e/conftest.py)
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Fixed by the owner's decision: E2E always uses the product's primary port.
E2E_PORT = 8989
E2E_HOST = "127.0.0.1"

#: Fixed so a session cookie minted by one backend run is valid for the next.
E2E_SESSION_SECRET = "qai-e2e-session-secret"

#: How long to wait for ``/api/system/health`` after spawning the backend.
STARTUP_TIMEOUT_S = 120.0

_PLATFORM_MARKERS = ("win_arm64", "win_x64", "linux_arm64")


# ---------------------------------------------------------------------------
# Platform detection / selection
# ---------------------------------------------------------------------------


def current_platform() -> str:
    """Return ``win_arm64`` / ``win_x64`` / ``linux_arm64`` / ``unsupported:<..>``."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arm = machine in ("arm64", "aarch64")
    x64 = machine in ("amd64", "x86_64", "x64")
    if system == "windows" and arm:
        return "win_arm64"
    if system == "windows" and x64:
        return "win_x64"
    if system == "linux" and arm:
        return "linux_arm64"
    return f"unsupported:{system}-{machine}"


_PLATFORM_KEY: pytest.StashKey[str] = pytest.StashKey()


def pytest_configure(config: pytest.Config) -> None:
    # Markers are registered in pyproject.toml (--strict-markers is on); this
    # hook only records the detected platform for the report header.
    config.stash[_PLATFORM_KEY] = current_platform()


def pytest_report_header(config: pytest.Config) -> list[str]:
    return [f"e2e platform: {config.stash.get(_PLATFORM_KEY, '?')}"]


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """Skip tests whose platform marker does not match this machine."""
    here = current_platform()
    for item in items:
        # Only touch tests under tests/e2e (this conftest is directory-scoped,
        # but be explicit in case it is ever imported more widely).
        if "e2e" not in item.keywords:
            continue
        wanted = [m for m in _PLATFORM_MARKERS if m in item.keywords]
        if wanted and here not in wanted:
            item.add_marker(
                pytest.mark.skip(
                    reason=f"platform: needs {'/'.join(wanted)}, this is {here}"
                )
            )


# ---------------------------------------------------------------------------
# Port check
# ---------------------------------------------------------------------------


def _port_is_free(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        # connect_ex == 0 means something is listening.
        return s.connect_ex((host, port)) != 0


def _describe_port_owner(port: int) -> str:
    """Best-effort ``pid / command line`` of whoever listens on ``port``."""
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover - psutil is a project dependency
        return "(psutil unavailable, owner unknown)"
    try:
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr and conn.laddr.port == port and conn.status == "LISTEN":
                if conn.pid is None:
                    return "(listening, pid not visible)"
                try:
                    proc = psutil.Process(conn.pid)
                    cmd = " ".join(proc.cmdline())[:200]
                    return f"pid {conn.pid}: {cmd}"
                except psutil.Error:
                    return f"pid {conn.pid}"
    except psutil.Error as exc:  # pragma: no cover
        return f"(could not inspect: {exc})"
    return "(owner not found)"


# ---------------------------------------------------------------------------
# Backend subprocess
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Backend:
    """A running E2E backend."""

    base_url: str
    port: int
    data_dir: Path
    pid: int
    log_path: Path


def _python_executable() -> str:
    """Interpreter that runs the backend.

    ``run_e2e.py`` always sets ``QAI_E2E_PYTHON`` to the product venv.  Without it (plain
    ``pytest tests/e2e``) the interpreter running pytest is used, which is usually NOT the
    product venv - a warning is emitted so a green run is not mistaken for a product-venv run.
    """
    py = os.environ.get("QAI_E2E_PYTHON")
    if not py:
        import warnings
        warnings.warn("QAI_E2E_PYTHON is not set: the backend runs on the pytest interpreter, not the product venv. "
                      "Use tests/e2e/run_e2e.py (or set QAI_E2E_PYTHON) to test the real runtime environment.",
                      stacklevel=2)
    return py or sys.executable


def _backend_env(data_dir: Path, *, auth_enabled: bool, pycache: Path) -> dict[str, str]:
    env = dict(os.environ)
    # The product venv has no installed ``qai`` package: it must come from src/.
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    env["PYTHONPYCACHEPREFIX"] = str(pycache)
    env["QAI_DATA__DATA_DIR"] = str(data_dir)
    env["QAI_AUTH__SESSION_SECRET"] = E2E_SESSION_SECRET
    env["QAI_AUTH__ENABLED"] = "true" if auth_enabled else "false"
    # Hermetic: the startup voice warm-up would load zipformer-zh, whose runner auto-downloads
    # ~165 MB of weights into the repo's models/ directory.
    env["QAI_APP_BUILDER__VOICE_WARMUP_ENABLED"] = "false"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def _wait_healthy(proc: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_S
    last_err = "no response yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            tail = _tail(log_path)
            raise RuntimeError(
                f"backend exited early with code {proc.returncode} before "
                f"becoming healthy.\n--- log tail ---\n{tail}"
            )
        try:
            r = httpx.get(f"{base_url}/api/system/health", timeout=3.0)
            if r.status_code == 200:
                return
            last_err = f"HTTP {r.status_code}"
        except httpx.HTTPError as exc:
            last_err = type(exc).__name__
        time.sleep(0.5)
    raise RuntimeError(
        f"backend not healthy after {STARTUP_TIMEOUT_S:.0f}s (last: {last_err}).\n"
        f"--- log tail ---\n{_tail(log_path)}"
    )


def _tail(path: Path, lines: int = 25) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "(log unreadable)"
    return "\n".join(text.splitlines()[-lines:])


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Stop ONLY the process we started (and its children)."""
    if proc.poll() is not None:
        return
    try:
        import psutil  # type: ignore[import-not-found]

        parent = psutil.Process(proc.pid)
        children = parent.children(recursive=True)
        parent.terminate()
        _, alive = psutil.wait_procs([parent, *children], timeout=15)
        for p in alive:
            p.kill()
    except Exception:  # noqa: BLE001 - last-resort fallback below
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def _start_backend(*, auth_enabled: bool) -> Iterator[Backend]:
    """Spawn one backend on 8989, yield it, always stop it and clean up.

    Only ONE backend may exist at a time (they share port 8989). Use this from
    a MODULE-scoped fixture so it is torn down before the next module's backend
    starts; never mix it with a session-scoped backend (the session one would
    still be alive and hold the port).
    """
    if not _port_is_free(E2E_HOST, E2E_PORT):
        # Fail the session; NEVER try to stop the occupant - it is not ours.
        pytest.exit(
            f"E2E aborted: port {E2E_PORT} is already in use by "
            f"{_describe_port_owner(E2E_PORT)}. E2E assumes no other AppBuilder "
            f"is running; stop it first (this suite will not kill it).",
            returncode=2,
        )

    work = Path(tempfile.mkdtemp(prefix="qai-e2e-"))
    data_dir = work / "data"
    data_dir.mkdir()
    log_path = work / "backend.log"
    base_url = f"http://{E2E_HOST}:{E2E_PORT}"

    with log_path.open("wb") as log:
        proc = subprocess.Popen(
            [_python_executable(), "-m", "apps.api", "--port", str(E2E_PORT)],
            cwd=str(REPO_ROOT),
            env=_backend_env(data_dir, auth_enabled=auth_enabled, pycache=work / "pyc"),
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    handle = _failure_logs.register(
        f"suite backend (auth {'on' if auth_enabled else 'off'}) log={log_path.name}",
        pid_of=lambda: proc.pid, log_of=lambda: log_path, returncode_of=proc.poll, port=E2E_PORT,
    )
    try:
        _wait_healthy(proc, base_url, log_path)
        yield Backend(
            base_url=base_url,
            port=E2E_PORT,
            data_dir=data_dir,
            pid=proc.pid,
            log_path=log_path,
        )
    finally:
        _failure_logs.unregister(handle)
        _stop(proc)
        shutil.rmtree(work, ignore_errors=True)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]):  # noqa: ANN201
    """When a test fails, keep the live backend's log + process state (see ``_failure_logs``). Passing tests keep nothing."""
    outcome = yield
    rep = outcome.get_result()
    if rep.failed and not _failure_logs.is_done(item.nodeid):
        err = f"{type(call.excinfo.value).__name__}: {call.excinfo.value}" if call.excinfo else rep.longreprtext[-600:]
        _failure_logs.snapshot_failure(REPO_ROOT, item.nodeid, rep.when, err)


@pytest.fixture(scope="module")
def backend() -> Iterator[Backend]:
    """Backend with the login gate OFF (the default for functional tests).

    Module-scoped, NOT session-scoped: the login-gate-ON suite needs a backend
    with different settings on the same port, so each module gets its own and the
    previous one is stopped first. Cost: one ~5-10s startup per module.
    """
    yield from _start_backend(auth_enabled=False)


@pytest.fixture(scope="module")
def auth_backend() -> Iterator[Backend]:
    """Backend with the Okta login gate ON (``QAI_AUTH__ENABLED=true``).

    Use only in modules that do NOT also request ``backend`` (two backends cannot
    share port 8989).
    """
    yield from _start_backend(auth_enabled=True)


@pytest.fixture
def http(backend: Backend) -> Iterator[httpx.Client]:
    """A plain HTTP client bound to the login-gate-OFF backend."""
    with httpx.Client(base_url=backend.base_url, timeout=15.0) as client:
        yield client


@pytest.fixture
def auth_http(auth_backend: Backend) -> Iterator[httpx.Client]:
    """A plain HTTP client bound to the login-gate-ON backend."""
    with httpx.Client(base_url=auth_backend.base_url, timeout=15.0) as client:
        yield client
