#!/usr/bin/env python
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Unified launcher for ``tests/e2e`` and ``tests/system``: run by feature category, get a report.

Standard library only, so it can be started by any Python 3.9+.

Which Python runs what (the point: test in the SAME environment users run in):

* the backend, and every child process the tests start, run on the PRODUCT venv
  (``%LOCALAPPDATA%\\QAIAppBuilder\\envs\\.venv_*``), which is never modified;
* the pytest process itself ALSO runs on the product venv's interpreter, so the library
  versions under test are the product's.  The product venv has no pytest, so pytest and
  pytest-asyncio live in a separate runner venv (``data/e2e-runner-venv``, created on first
  use from the product venv's base interpreter) whose site-packages is only APPENDED to
  ``sys.path``: the product venv always wins, and a library it lacks is never masked.
  The packages actually borrowed are printed and recorded in the report.
* ``--python`` / ``$QAI_E2E_TEST_PYTHON`` replace the test-side interpreter (then it needs
  pytest, pytest-asyncio, httpx, psutil, keyring, websockets itself).

Why one pytest process PER CATEGORY (not one for everything, not in parallel):

* every backend binds port 8989 (and aria2c 6810), so two cannot coexist ->
  strictly serial (``mb_system`` never starts a backend, but is still run serially);
* a category that crashes or hangs (we kill its whole process tree) does not
  take the others down, and each gets its own junit xml + log.

Examples::

    run_e2e.py --list                       # categories, files, test counts
    run_e2e.py                              # everything, serial
    run_e2e.py -c chat,security             # only these categories
    run_e2e.py -x model_builder,cli         # everything except these
    run_e2e.py -c mb_system                 # tests/system/model_builder (no backend needed)
    run_e2e.py -c settings -- -k proxy      # args after "--" go to pytest
    run_e2e.py --dry-run                    # show what would run

Output goes to ``<project>/data/e2e-reports/<timestamp>/`` (git-ignored)::

    report.html  summary.json  junit/<category>.xml  logs/<category>.log
    backend-logs/  (only when a test failed: the backend's log + process state at that moment)

Exit code: 0 = no failures/errors (skips are fine), 1 = failures/errors,
2 = usage / pre-flight problem (nothing was run).

The category table below is the ONLY place that maps test files to features.
A ``test_*.py`` under tests/e2e or tests/system that matches no category (or two) aborts
the run, so a new test module cannot silently drop out of the report.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import fnmatch
import html
import json
import os
import platform
import re
import socket
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent            # .../tests/e2e
TESTS = HERE.parent                               # .../tests
PROJECT = TESTS.parent                            # .../tools/qaiappbuilder
# Directories scanned for test_*.py, relative to tests/.  Every file is identified by its path
# below tests/ (e.g. ``e2e/test_cli.py``, ``system/model_builder/test_entrypoints.py``).
TEST_ROOTS = ("e2e", "system")
PORTS = {8989: "backend", 6810: "aria2c RPC"}     # must be free (see conftest.py)
# Managed apps started by test_app_builder_apps.py; that module SKIPS itself if any of these is busy.
APP_PORTS = {p: "app_builder managed app" for p in (18420, 18421, 18422, 18423, 18555)}
WATCHED = {**PORTS, **APP_PORTS}                  # checked for leftovers after every category

# ---------------------------------------------------------------------------
# Feature categories  (ordered; follows FEATURE-INVENTORY.md section 3)
# name -> (description, [globs]).  A glob without "/" matches the file NAME (any root);
# a glob with "/" matches the path below tests/ (e.g. ``system/model_builder/test_*.py``).
# ---------------------------------------------------------------------------
CATEGORIES: dict[str, tuple[str, list[str]]] = {
    "system": ("System & startup (/api/system/*)",
               ["test_sys_*.py"]),
    "auth": ("Login & access control (auth gate, CSRF)",
             ["test_auth_*.py"]),
    "chat": ("Chat, conversations, MCP, OpenAI-compatible API, uploads",
             ["test_chat_*.py", "test_openai_compat*.py", "test_uploads*.py"]),
    "app_builder": ("App Builder (app generation / packaging / runs)",
                    ["test_app_builder*.py"]),
    "model_builder": ("Model Builder (conversion, quantization, QAIRT env)",
                      ["test_model_builder.py", "test_quantization.py", "test_qairt_*.py"]),
    "model_hub": ("Model Hub, downloads, aria2c",
                  ["test_model_hub.py", "test_downloads*.py", "test_aria2c*.py"]),
    "service": ("Local model service & versions",
                ["test_service*.py"]),
    "security": ("Security & permissions (/api/security/*)",
                 ["test_secu_*.py"]),
    "settings": ("Settings, preferences, proxy, TLS verify",
                 ["test_settings.py", "test_set_tls_verify.py"]),
    "skills_tasks": ("Skills and scheduled tasks",
                     ["test_skills*.py", "test_scheduled_tasks.py"]),
    "search": ("Search-engine management (/api/search/engines)",
               ["test_search_engines*.py"]),
    "gallery": ("Gallery submission (local part only)",
                ["test_gallery.py"]),
    "events": ("Event stream (/events, SSE + WebSocket)",
               ["test_events*.py"]),
    "cli": ("qai.bat command line",
            ["test_cli.py"]),
    "mb_system": ("Model Builder system tests (tests/system: adapters, pack export, HTTP bridge, NPU)",
                  ["system/model_builder/test_*.py"]),
}

# Categories whose tests never start the backend: no need for the backend / aria2c ports to be free.
NO_BACKEND = {"mb_system"}


def needs_backend_ports(cats: list[str]) -> bool:
    return any(c not in NO_BACKEND for c in cats)

# Modules needed on the TEST side, as (import name, pip name).
TEST_SIDE_MODULES = [("pytest", "pytest"), ("pytest_asyncio", "pytest-asyncio"),
                     ("httpx", "httpx"), ("psutil", "psutil"), ("keyring", "keyring"),
                     ("websockets", "websockets")]


# ---------------------------------------------------------------------------
# Category <-> file mapping and self-check
# ---------------------------------------------------------------------------
def all_test_files() -> list[str]:
    """Every test module as a posix path below tests/ (``e2e/test_x.py``)."""
    return sorted(p.relative_to(TESTS).as_posix() for r in TEST_ROOTS for p in (TESTS / r).rglob("test_*.py"))


def _match(rel: str, pat: str) -> bool:
    return fnmatch.fnmatch(rel if "/" in pat else rel.rsplit("/", 1)[-1], pat)


def files_of(cat: str) -> list[str]:
    pats = CATEGORIES[cat][1]
    return [f for f in all_test_files() if any(_match(f, p) for p in pats)]


def check_mapping() -> list[str]:
    """Return problems: unclassified files, files in two categories, dead patterns."""
    problems: list[str] = []
    names: dict[str, list[str]] = {}
    for f in all_test_files():
        names.setdefault(f.rsplit("/", 1)[-1], []).append(f)
    for n, fs in names.items():
        if len(fs) > 1:
            problems.append(f"{n}: same file name in several places {fs} - rename one (reports and counts key on the path, "
                            "but pytest module names would collide)")
    owner: dict[str, list[str]] = {}
    for cat in CATEGORIES:
        for f in files_of(cat):
            owner.setdefault(f, []).append(cat)
    for f in all_test_files():
        cats = owner.get(f, [])
        if not cats:
            problems.append(f"{f}: not in any category - add it to CATEGORIES in run_e2e.py")
        elif len(cats) > 1:
            problems.append(f"{f}: matched by several categories {cats}")
    for cat, (_d, pats) in CATEGORIES.items():
        for p in pats:
            if not any(_match(f, p) for f in all_test_files()):
                problems.append(f"category '{cat}': pattern '{p}' matches no file")
    return problems


# ---------------------------------------------------------------------------
# Interpreters
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Runner venv: where pytest lives.
#
# The tests run on the PRODUCT venv's interpreter, so the library set under test
# is exactly what users get.  The product venv ships no pytest and must stay
# clean, so pytest (+ pytest-asyncio) is installed in a SEPARATE venv
# (data/e2e-runner-venv, git-ignored).  That venv is built from the product
# venv's own base interpreter (same Python / ABI) and its site-packages is only
# APPENDED to sys.path of the test process: whatever the product venv has always
# wins, and a library the product venv lacks can never be masked by a copy in the
# runner venv.  The backend (and every other child the tests start) runs on the
# plain product venv and never sees the runner venv.
# ---------------------------------------------------------------------------
RUNNER_VENV = Path(os.environ.get("QAI_E2E_RUNNER_VENV") or PROJECT / "data" / "e2e-runner-venv")
RUNNER_REQS = ["pytest>=8.3,<10.0", "pytest-asyncio>=1.0,<2.0"]   # same pins as pyproject [dev]
_BOOT = ("import os,sys,runpy;p=os.environ.get('QAI_E2E_DEPS');"
         "p and os.path.isdir(p) and sys.path.append(p);"
         "runpy.run_module('pytest',run_name='__main__',alter_sys=True)")
BACKEND_PY: str | None = None      # set in main(); the runner venv is used iff the test python IS this interpreter
OVERLAY: Path | None = None        # the runner venv's site-packages, once it exists


def _same(a: str | None, b: str | None) -> bool:
    return bool(a and b and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b)))


def pytest_cmd(py: str) -> list[str]:
    """argv prefix that runs pytest on ``py`` (runner-venv packages appended when ``py`` is the product venv)."""
    return [py, "-c", _BOOT] if _same(py, BACKEND_PY) else [py, "-m", "pytest"]


def _runner_python() -> Path:
    return RUNNER_VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _purelib(py: Path) -> Path | None:
    try:
        r = subprocess.run([str(py), "-c", "import sysconfig;print(sysconfig.get_paths()['purelib'])"],
                           capture_output=True, text=True, timeout=60)
        return Path(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, subprocess.SubprocessError, IndexError):
        return None


def _base_python(venv_py: str) -> str:
    """The interpreter the product venv was created from (pyvenv.cfg 'home'); falls back to this one."""
    cfg = Path(venv_py).resolve().parent.parent / "pyvenv.cfg"
    try:
        for line in cfg.read_text(encoding="utf-8").splitlines():
            k, _, v = line.partition("=")
            if k.strip().lower() == "home":
                for n in ("python.exe", "python3", "python"):
                    if (Path(v.strip()) / n).exists():
                        return str(Path(v.strip()) / n)
    except OSError:
        pass
    return sys.executable


def ensure_runner_venv(product_py: str) -> bool:
    """Create/refresh the runner venv (idempotent: nothing is reinstalled when pytest is already importable)."""
    global OVERLAY
    rp = _runner_python()
    if rp.exists():
        OVERLAY = _purelib(rp)
        if OVERLAY and (OVERLAY / "pytest").is_dir() and (OVERLAY / "pytest_asyncio").is_dir():
            return True
    print(f"preparing the test-runner venv {RUNNER_VENV} (pytest lives here; the product venv is not touched)")
    for cmd in ([_base_python(product_py), "-m", "venv", str(RUNNER_VENV)],
                [str(rp), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", *RUNNER_REQS]):
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            print((r.stdout + r.stderr)[-1500:], file=sys.stderr)
            return False
    OVERLAY = _purelib(rp)
    return bool(OVERLAY and (OVERLAY / "pytest").is_dir())


def borrowed_packages(py: str) -> list[str]:
    """Runner-venv distributions that the product venv itself does NOT provide (what the tests borrow)."""
    if not OVERLAY:
        return []
    code = ("import sys,os,json,importlib.metadata as m;o=sys.argv[1];"
            "n=lambda d:(d.metadata['Name'] or '').lower().replace('_','-');"
            "have={n(d) for d in m.distributions(path=[p for p in sys.path if os.path.abspath(p)!=os.path.abspath(o)])};"
            "print(json.dumps(sorted(n(d)+'=='+d.version for d in m.distributions(path=[o]) if n(d) not in have)))")
    try:
        r = subprocess.run([py, "-c", code, str(OVERLAY)], capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else []
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return []


def _probe(py: str, modules: list[str]) -> dict[str, bool] | None:
    pre = (f"import sys;sys.path.append({str(OVERLAY)!r});" if _same(py, BACKEND_PY) and OVERLAY else "")
    code = (pre + "import importlib.util as u,json;"
            f"print(json.dumps({{m: bool(u.find_spec(m)) for m in {modules!r}}}))")
    try:
        r = subprocess.run([py, "-c", code], capture_output=True, text=True, timeout=60)
        return json.loads(r.stdout.strip().splitlines()[-1]) if r.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None


def find_test_python(explicit: str | None) -> str | None:
    """--python > $QAI_E2E_TEST_PYTHON > the product venv (+ runner venv) > this interpreter > %TEMP%\\qai-e2e-venv."""
    if BACKEND_PY and Path(BACKEND_PY).exists() and not explicit and not os.environ.get("QAI_E2E_TEST_PYTHON"):
        ensure_runner_venv(BACKEND_PY)
    cands = [explicit, os.environ.get("QAI_E2E_TEST_PYTHON"), BACKEND_PY, sys.executable,
             str(Path(os.environ.get("TEMP", "")) / "qai-e2e-venv" / "Scripts" / "python.exe"),
             str(Path(os.environ.get("TMPDIR", "/tmp")) / "qai-e2e-venv" / "bin" / "python")]
    for c in cands:
        if not c or not Path(c).exists():
            continue
        have = _probe(c, ["pytest"])
        if have and have["pytest"]:
            return c
        if c == explicit or c == os.environ.get("QAI_E2E_TEST_PYTHON"):
            return c                      # user asked for it: let preflight say what is missing
    return None

def find_backend_python(explicit: str | None) -> str | None:
    """--backend-python > $QAI_E2E_PYTHON > product venv for this CPU."""
    if explicit:
        return explicit
    if os.environ.get("QAI_E2E_PYTHON"):
        return os.environ["QAI_E2E_PYTHON"]
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "QAIAppBuilder" / "envs"
    arm = platform.machine().lower() in ("arm64", "aarch64")
    for name in ((".venv_arm64_313", ".venv_x64_313") if arm else (".venv_x64_313", ".venv_arm64_313")):
        p = base / name / "Scripts" / "python.exe"
        if p.exists():
            return str(p)
    return None


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------
def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def preflight(test_py: str | None, backend_py: str | None, selected: list[str]) -> list[str]:
    errs: list[str] = []
    if not test_py:
        errs.append("no test-side Python with pytest found; pass --python or set QAI_E2E_TEST_PYTHON "
                    "(needs: " + ", ".join(p for _m, p in TEST_SIDE_MODULES) + ")")
    else:
        have = _probe(test_py, [m for m, _p in TEST_SIDE_MODULES])
        if have is None:
            errs.append(f"test Python is not runnable: {test_py}")
        else:
            miss = [p for m, p in TEST_SIDE_MODULES if not have[m]]
            if miss:
                errs.append(f"test Python {test_py} is missing: {', '.join(miss)}  "
                            f"(pip install {' '.join(miss)})")
    if not backend_py:
        errs.append("no backend Python found; set QAI_E2E_PYTHON or --backend-python "
                    "(the product venv, e.g. %LOCALAPPDATA%\\QAIAppBuilder\\envs\\.venv_arm64_313)")
    elif not Path(backend_py).exists():
        errs.append(f"backend Python does not exist: {backend_py}")
    for port, what in PORTS.items():
        if needs_backend_ports(selected) and port_busy(port):
            errs.append(f"port {port} ({what}) is already in use - stop whatever owns it; "
                        "the suite aborts rather than killing it")
    if "app_builder" in selected:
        for port in APP_PORTS:
            if port_busy(port):
                o = port_owner(port)
                errs.append(f"port {port} is in use (pid {o['pid']}, {o['cmd'] or 'owner unknown'}) - "
                            "test_app_builder_apps.py would SKIP all its tests; stop it first")
    return errs


# ---------------------------------------------------------------------------
# Running one category
# ---------------------------------------------------------------------------
def kill_tree(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        proc.kill()


def wait_ports_free(ports, timeout: float = 30.0) -> list[int]:
    ports = list(ports)
    end = time.time() + timeout
    busy = [p for p in ports if port_busy(p)]
    while busy and time.time() < end:
        time.sleep(1.0)
        busy = [p for p in ports if port_busy(p)]
    return busy


# psutil lives in the test-side interpreter (product venv), not necessarily in the launcher's, and the launcher
# stays stdlib-only: owner lookup and cleanup run in a tiny helper process on ``HELPER_PY``.
HELPER_PY: str | None = None       # set in main() to the test-side interpreter

_PS_HELPER = r'''
import json, os, sys
import psutil
mode, arg, me = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])

def owner(port):
    info = {"pid": None, "cmd": "", "cwd": "", "from_pytest": None}
    for c in psutil.net_connections("inet"):
        if c.laddr and c.laddr.port == port and c.status == "LISTEN" and c.pid:
            p = psutil.Process(c.pid)
            info.update(pid=c.pid, cmd=" ".join(p.cmdline())[:200])
            try:
                info["cwd"] = p.cwd()
                info["from_pytest"] = "PYTEST_CURRENT_TEST" in p.environ()
            except Exception:
                pass
            break
    return info

def kill(pid):
    p = psutil.Process(pid)
    if "PYTEST_CURRENT_TEST" not in p.environ():
        return f"pid {p.pid} was not started by a test run; not killed"
    chain = [p]
    while True:
        par = chain[-1].parent()
        if par is None or par.pid in (me, os.getpid()):
            break
        try:
            if "PYTEST_CURRENT_TEST" not in par.environ():
                break
        except Exception:
            break
        chain.append(par)
    victims = {x for c in chain for x in [c, *c.children(recursive=True)]}
    for v in victims:
        try:
            v.kill()
        except Exception:
            pass
    psutil.wait_procs(list(victims), timeout=8)
    return f"killed {len(victims)} process(es): " + ", ".join(str(v.pid) for v in sorted(victims, key=lambda v: v.pid))

print(json.dumps(owner(arg) if mode == "owner" else {"action": kill(arg)}))
'''


def _psutil_helper(mode: str, arg: int) -> dict | None:
    """Run ``_PS_HELPER`` on an interpreter that has psutil; None if there is none or it failed."""
    for py in dict.fromkeys(p for p in (HELPER_PY, sys.executable) if p):
        try:
            r = subprocess.run([py, "-c", _PS_HELPER, mode, str(arg), str(os.getpid())],
                               capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout.strip():
            try:
                return json.loads(r.stdout.strip().splitlines()[-1])
            except ValueError:
                continue
    return None


def port_owner(port: int) -> dict:
    """Best-effort owner of a listening port (pid / command line / whether a test run started it)."""
    info = {"port": port, "what": WATCHED.get(port, "?"), "pid": None, "cmd": "", "cwd": "", "from_pytest": None}
    got = _psutil_helper("owner", port)
    if got:
        info.update(got)
    return info


def find_leaks(baseline: set[int], settle: float = 10.0) -> list[dict]:
    """Watched ports that are busy now but were NOT busy when the run started.

    Waits up to ``settle`` seconds first: a stopped server's sockets can take a moment to go away.
    """
    new = [p for p in WATCHED if p not in baseline]
    end = time.time() + settle
    busy = [p for p in new if port_busy(p)]
    while busy and time.time() < end:
        time.sleep(1.0)
        busy = [p for p in new if port_busy(p)]
    return [port_owner(p) for p in busy]


def run_category(cat: str, test_py: str, env: dict, out: Path, extra: list[str],
                 timeout: int, quiet: bool) -> dict:
    junit = out / "junit" / f"{cat}.xml"
    log = out / "logs" / f"{cat}.log"
    files = [str(TESTS / f) for f in files_of(cat)]
    cmd = [*pytest_cmd(test_py), *files, f"--junitxml={junit}", *extra]
    # Short basetemp: sys.pycache_prefix (data/caches/pycache) re-roots every .pyc under the
    # source's full path, so the default %TEMP%\pytest-of-<user>\pytest-N\... overflows
    # Windows' 260-char MAX_PATH during py_compile (mb_system export tests).
    if os.name == "nt" and not any(a.startswith("--basetemp") for a in extra):
        _bt = Path(os.environ.get('SystemDrive', 'C:') + '\\') / 'qai-e2e' / cat
        _bt.parent.mkdir(parents=True, exist_ok=True)
        cmd.append(f"--basetemp={_bt}")
    res = {"name": cat, "description": CATEGORIES[cat][0], "files": files_of(cat),
           "command": cmd, "returncode": None, "status": "ERROR", "duration": 0.0,
           "note": "", "log": str(log), "junit": str(junit), "leaks": []}

    busy = wait_ports_free(([] if cat in NO_BACKEND else list(PORTS)) + (list(APP_PORTS) if cat == "app_builder" else []))
    if busy:
        res["note"] = f"ports still busy before start: {busy}; category not run"
        return res

    t0 = time.time()
    kw = {"start_new_session": True} if os.name != "nt" else \
         {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    proc = subprocess.Popen(cmd, cwd=str(PROJECT), env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, **kw)

    def pump() -> None:
        with open(log, "w", encoding="utf-8", errors="replace") as fh:
            for raw in iter(proc.stdout.readline, b""):
                line = raw.decode("utf-8", errors="replace")
                fh.write(line)
                if not quiet:
                    sys.stdout.write("    " + line)
                    sys.stdout.flush()

    th = threading.Thread(target=pump, daemon=True)
    th.start()
    try:
        proc.wait(timeout=timeout if timeout > 0 else None)
        res["returncode"] = proc.returncode
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        proc.wait()
        res["returncode"] = None
        res["status"] = "TIMEOUT"
        res["note"] = f"killed after {timeout}s (process tree terminated)"
    th.join(timeout=10)
    res["duration"] = time.time() - t0

    rc = res["returncode"]
    if res["status"] != "TIMEOUT":
        res["status"] = {0: "PASS", 1: "FAIL", 5: "NO TESTS"}.get(rc, "ERROR")
        if res["status"] == "ERROR":
            res["note"] = f"pytest exit code {rc} (2=interrupted, 3=internal error, 4=usage, 130=Ctrl-C)"
    return res


def kill_leak(o: dict) -> str:
    """Kill a leaked listener AND its reloader parents, but only if they carry PYTEST_CURRENT_TEST.

    That variable is inherited from the pytest process, so it proves the process was started by a
    test run; anything without it (e.g. a real product instance) is never touched.
    """
    if not o.get("pid"):
        return "owner unknown; not killed"
    got = _psutil_helper("kill", int(o["pid"]))
    if got is None:
        return "no interpreter with psutil available to inspect the process; not killed"
    return got.get("action", "could not kill")

# ---------------------------------------------------------------------------
# junit -> results
# ---------------------------------------------------------------------------
def parse_junit(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return []
    cases = []
    for tc in root.iter("testcase"):
        status, msg, detail = "passed", "", ""
        for child, st in (("failure", "failed"), ("error", "error"), ("skipped", "skipped")):
            node = tc.find(child)
            if node is not None:
                status, msg, detail = st, node.get("message") or "", (node.text or "")
                break
        cases.append({"id": f"{tc.get('classname', '')}::{tc.get('name', '')}",
                      "status": status, "time": float(tc.get("time") or 0),
                      "message": msg, "detail": detail})
    return cases


def tail(path: str, n: int = 40) -> str:
    try:
        return "".join(Path(path).read_text(encoding="utf-8", errors="replace").splitlines(True)[-n:])
    except OSError:
        return ""


def count(cases: list[dict]) -> dict:
    c = {"passed": 0, "failed": 0, "error": 0, "skipped": 0}
    for x in cases:
        c[x["status"]] += 1
    c["total"] = len(cases)
    return c


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def fmt_dur(s: float) -> str:
    m, sec = divmod(int(round(s)), 60)
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{sec:02d}s" if h else (f"{m}m{sec:02d}s" if m else f"{sec}s")


def write_reports(out: Path, meta: dict, results: list[dict]) -> None:
    for r in results:
        r["cases"] = parse_junit(Path(r["junit"]))
        r["counts"] = count(r["cases"])
        if r["status"] == "PASS" and r["counts"]["total"] and r["counts"]["total"] == r["counts"]["skipped"]:
            r["note"] = (r["note"] + " all tests skipped").strip()
    tot = {k: sum(r["counts"][k] for r in results) for k in ("total", "passed", "failed", "error", "skipped")}
    bad_cat = [r["name"] for r in results if r["status"] not in ("PASS",) or r.get("leaks")]
    leak_cats = [r["name"] for r in results if r.get("leaks")]
    meta = {**meta, "totals": tot, "bad_categories": bad_cat, "leaking_categories": leak_cats,
            "duration": sum(r["duration"] for r in results)}

    # ---- JSON
    jr = []
    for r in results:
        jr.append({k: r[k] for k in ("name", "description", "files", "status", "returncode",
                                     "duration", "note", "counts", "log", "junit", "leaks")} |
                  {"failures": [{"id": c["id"], "status": c["status"], "message": c["message"]}
                                for c in r["cases"] if c["status"] in ("failed", "error")]})
    (out / "summary.json").write_text(json.dumps({"meta": meta, "categories": jr}, indent=2,
                                                 ensure_ascii=False), encoding="utf-8")

    # ---- HTML
    e = html.escape
    color = {"PASS": "#1a7f37", "FAIL": "#cf222e", "ERROR": "#cf222e", "TIMEOUT": "#cf222e",
             "NO TESTS": "#9a6700"}
    rows, details = [], []
    for r in results:
        c = r["counts"]
        rows.append(
            f"<tr><td><a href='#{e(r['name'])}'>{e(r['name'])}</a></td><td>{e(r['description'])}</td>"
            f"<td style='color:{color.get(r['status'], '#000')}'><b>{e(r['status'])}</b>"
            f"{' <b style=color:#cf222e>+LEAK</b>' if r.get('leaks') else ''}</td>"
            f"<td>{c['total']}</td><td>{c['passed']}</td><td>{c['failed']}</td><td>{c['error']}</td>"
            f"<td>{c['skipped']}</td><td>{fmt_dur(r['duration'])}</td></tr>")
        d = [f"<h3 id='{e(r['name'])}'>{e(r['name'])} - <span style='color:{color.get(r['status'], '#000')}'>"
             f"{e(r['status'])}</span></h3>", f"<p>{e(r['description'])}<br>files: "
             f"{e(', '.join(r['files']))}<br>log: <code>{e(r['log'])}</code></p>"]
        if r["note"]:
            d.append(f"<p><b>Note:</b> {e(r['note'])}</p>")
        for lk in r.get("leaks", []):
            d.append(f"<p style='color:#cf222e'><b>LEFTOVER after this category:</b> port {lk['port']} "
                     f"({e(lk['what'])}) still listening - pid {lk['pid']}, <code>{e(lk['cmd'] or 'owner unknown')}</code>"
                     f"{', cwd <code>' + e(lk['cwd']) + '</code>' if lk['cwd'] else ''}"
                     f"{' [started by a test run]' if lk['from_pytest'] else ''}"
                     f"{' - ' + e(lk['action']) if lk.get('action') else ''}</p>")
        bad = [x for x in r["cases"] if x["status"] in ("failed", "error")]
        for x in bad:
            d.append(f"<details open><summary style='color:#cf222e'>{e(x['status'].upper())}: {e(x['id'])} "
                     f"({x['time']:.1f}s)</summary><pre>{e((x['message'] + chr(10) + x['detail'])[:6000])}</pre></details>")
        if not r["cases"] and r["status"] != "PASS":
            d.append(f"<details open><summary>No junit results - log tail</summary><pre>{e(tail(r['log']))}</pre></details>")
        skips: dict[str, int] = {}
        for x in r["cases"]:
            if x["status"] == "skipped":
                skips[x["message"][:160] or "(no reason)"] = skips.get(x["message"][:160] or "(no reason)", 0) + 1
        if skips:
            d.append("<details><summary>Skipped: " + str(sum(skips.values())) + "</summary><ul>" +
                     "".join(f"<li>{n} x {e(m)}</li>" for m, n in sorted(skips.items(), key=lambda kv: -kv[1])) +
                     "</ul></details>")
        details.append("\n".join(d))

    slow = sorted((x | {"cat": r["name"]} for r in results for x in r["cases"]),
                  key=lambda x: -x["time"])[:15]
    slow_html = "".join(f"<tr><td>{x['time']:.1f}s</td><td>{e(x['cat'])}</td><td>{e(x['id'])}</td></tr>"
                        for x in slow)
    verdict = "PASSED" if not tot["failed"] and not tot["error"] and not bad_cat else "FAILED"
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>QAI E2E report</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;max-width:1200px}}
table{{border-collapse:collapse}}td,th{{border:1px solid #d0d7de;padding:4px 10px;text-align:left}}
th{{background:#f6f8fa}}pre{{background:#f6f8fa;padding:8px;overflow:auto;white-space:pre-wrap}}
code{{background:#f6f8fa;padding:1px 4px}}</style></head><body>
<h1>QAI E2E report - <span style="color:{'#1a7f37' if verdict == 'PASSED' else '#cf222e'}">{verdict}</span></h1>
<p>{e(meta['started'])} &middot; platform <b>{e(meta['platform'])}</b> ({e(meta['machine'])}) &middot;
total time {fmt_dur(meta['duration'])}<br>
test python: <code>{e(str(meta['test_python']))}</code> ({e(str(meta['pytest']))})<br>
backend python: <code>{e(str(meta['backend_python']))}</code>
(test side = product venv: {meta.get('test_side_is_product_venv')}; test-only packages borrowed: {e(', '.join(meta.get('borrowed_from_runner_venv') or []) or '-')})<br>
git: {e(str(meta['git']))} &middot;
extra pytest args: <code>{e(' '.join(meta['pytest_args']) or '-')}</code></p>
<h2>Totals</h2><p>{tot['total']} tests: <b style="color:#1a7f37">{tot['passed']} passed</b>,
<b style="color:#cf222e">{tot['failed']} failed, {tot['error']} errors</b>, {tot['skipped']} skipped</p>
<table><tr><th>Category</th><th>Feature</th><th>Result</th><th>Total</th><th>Pass</th><th>Fail</th>
<th>Error</th><th>Skip</th><th>Time</th></tr>{''.join(rows)}</table>
<h2>Details</h2>{''.join(details)}
<h2>Slowest tests</h2><table><tr><th>Time</th><th>Category</th><th>Test</th></tr>{slow_html}</table>
</body></html>"""
    (out / "report.html").write_text(page, encoding="utf-8")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def collect_counts(test_py: str, env: dict) -> dict[str, int] | None:
    """Per-file test counts (key: path below tests/) via one ``--collect-only`` (no backend is started)."""
    try:
        r = subprocess.run([*pytest_cmd(test_py), *(str(TESTS / d) for d in TEST_ROOTS),
                            "--collect-only", "-q", "-p", "no:cacheprovider"],
                           cwd=str(PROJECT), env=env, capture_output=True, text=True, timeout=300,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    counts: dict[str, int] = {}
    for line in r.stdout.splitlines():
        m = re.match(r"^(?:.*?tests[\\/])?((?:%s)[\\/].*?test_\w+\.py)::" % "|".join(TEST_ROOTS), line)
        if m:
            k = m.group(1).replace("\\", "/")
            counts[k] = counts.get(k, 0) + 1
    return counts or None


def split_args(argv: list[str]) -> tuple[list[str], list[str]]:
    return (argv[:argv.index("--")], argv[argv.index("--") + 1:]) if "--" in argv else (argv, [])


def acquire_run_lock():
    """Take the single-instance lock; returns ``(handle, holder_description)``, handle None if someone else holds it.

    An OS-level lock on ``data/e2e-reports/.run_e2e.lock``: it vanishes with the process, however it dies (kill,
    crash, power loss), so a stale lock file can never block the next run.  The holder writes its pid + start time
    into the file for the message.
    """
    path = PROJECT / "data" / "e2e-reports" / ".run_e2e.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(path, "a+b")
    try:
        if os.name == "nt":
            import msvcrt
            fh.seek(1 << 20)   # lock a byte far beyond the text so the holder line stays readable by others
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        try:
            holder = fh.read().decode("utf-8", "replace").strip() or "holder unknown"
        except OSError:
            holder = "holder unknown"
        fh.close()
        return None, holder
    fh.seek(0)
    fh.truncate()
    fh.write(f"pid {os.getpid()} since {_dt.datetime.now():%Y-%m-%d %H:%M:%S}".encode())
    fh.flush()
    return fh, ""


_LOCK_FH = None   # kept open for the life of the process; closing it (or exiting) releases the lock


def main(argv: list[str]) -> int:
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    own, pytest_args = split_args(argv)
    ap = argparse.ArgumentParser(description="Run tests/e2e and tests/system by feature category and write a report. "
                                             "Arguments after '--' are passed to pytest.")
    ap.add_argument("-c", "--category", action="append", default=[],
                    help="comma-separated categories to run (default: all)")
    ap.add_argument("-x", "--exclude", action="append", default=[], help="comma-separated categories to skip")
    ap.add_argument("--list", action="store_true", help="list categories, files and test counts, then exit")
    ap.add_argument("--dry-run", action="store_true", help="print the pytest commands, run nothing")
    ap.add_argument("--python", help="test-side Python (default: the product venv + runner venv; see the module docstring)")
    ap.add_argument("--backend-python", help="Python that runs the backend (default: $QAI_E2E_PYTHON or product venv)")
    ap.add_argument("--timeout", type=int, default=5400, help="seconds per category before it is killed (0 = none)")
    ap.add_argument("--keep-leaks", action="store_true",
                    help="report leftover test servers but do not kill them (default: kill, only if they carry PYTEST_CURRENT_TEST)")
    ap.add_argument("--fail-fast", action="store_true", help="stop after the first category that does not pass")
    ap.add_argument("--reverse", action="store_true",
                    help="run the selected categories in reverse order (tells order-dependent failures from category-dependent ones)")
    ap.add_argument("--out", help="report directory (default: data/e2e-reports/<timestamp>)")
    ap.add_argument("-q", "--quiet", action="store_true", help="do not echo pytest output (still logged)")
    args = ap.parse_args(own)

    problems = check_mapping()
    if problems:
        print("Category table is out of date:", file=sys.stderr)
        for p in problems:
            print("  - " + p, file=sys.stderr)
        return 2

    def names(vals: list[str]) -> list[str]:
        return [n.strip() for v in vals for n in v.split(",") if n.strip()]

    wanted, skipped = names(args.category), names(args.exclude)
    unknown = [n for n in wanted + skipped if n not in CATEGORIES]
    if unknown:
        print(f"Unknown category: {', '.join(unknown)}\nValid: {', '.join(CATEGORIES)}", file=sys.stderr)
        return 2
    selected = [c for c in (wanted or CATEGORIES) if c not in skipped]
    if args.reverse:
        selected.reverse()
    if not selected:
        print("Nothing selected.", file=sys.stderr)
        return 2

    global BACKEND_PY
    backend_py = BACKEND_PY = find_backend_python(args.backend_python)
    test_py = find_test_python(args.python)
    global HELPER_PY
    HELPER_PY = test_py
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1", QAI_E2E_DEPS=str(OVERLAY or ''))
    if backend_py:
        env["QAI_E2E_PYTHON"] = backend_py

    if args.list:
        cnt = collect_counts(test_py, env) if test_py else None
        print(f"{'category':<14}{'files':>6}{'tests':>7}  description")
        gt = 0
        for cat, (desc, _p) in CATEGORIES.items():
            fs = files_of(cat)
            n = sum(cnt.get(f, 0) for f in fs) if cnt else None
            gt += n or 0
            print(f"{cat:<14}{len(fs):>6}{(n if n is not None else '?'):>7}  {desc}")
            if args.category or args.exclude:
                pass
            for f in fs:
                print(f"{'':<14}{'':>6}{(cnt.get(f, 0) if cnt else '?'):>7}    {f}")
        print(f"{'TOTAL':<14}{len(all_test_files()):>6}{(gt if cnt else '?'):>7}")
        if not cnt:
            print("(test counts unavailable: no usable test-side Python; see --python)")
        return 0

    errs = preflight(test_py, backend_py, selected)
    if errs and not args.dry_run:
        print("Pre-flight failed - nothing was run:", file=sys.stderr)
        for x in errs:
            print("  - " + x, file=sys.stderr)
        return 2

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) if args.out else PROJECT / "data" / "e2e-reports" / stamp
    if args.dry_run:
        for x in errs:
            print("WARNING (would abort a real run): " + x)
        print(f"report dir: {out}")
        for cat in selected:
            print(f"[{cat}] {test_py} -m pytest {' '.join(files_of(cat))} --junitxml=... {' '.join(pytest_args)}")
        return 0
    (out / "junit").mkdir(parents=True, exist_ok=True)
    global _LOCK_FH
    _LOCK_FH, holder = acquire_run_lock()
    if _LOCK_FH is None:
        print(f"Another run_e2e is already running ({holder}). Two runs would fight over ports 8989/6810 and "
              "corrupt each other's results - wait for it to finish (or stop it) and start again.", file=sys.stderr)
        return 2
    (out / "logs").mkdir(parents=True, exist_ok=True)

    ver = subprocess.run([*pytest_cmd(test_py), "--version"], capture_output=True, text=True)
    git = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=str(PROJECT), capture_output=True, text=True)
    dirty = subprocess.run(["git", "status", "--porcelain", "--", "."], cwd=str(PROJECT),
                           capture_output=True, text=True).stdout.strip()
    meta = {"started": _dt.datetime.now().isoformat(timespec="seconds"),
            "platform": platform.system().lower() + "-" + platform.machine().lower(),
            "machine": platform.platform(), "test_python": test_py, "backend_python": backend_py,
            "pytest": (ver.stdout or ver.stderr).strip(),
            "test_side_is_product_venv": _same(test_py, backend_py),
            "borrowed_from_runner_venv": borrowed_packages(test_py) if _same(test_py, backend_py) else [],
            "git": (git.stdout.strip() or "?") + (" (+uncommitted changes)" if dirty else ""),
            "pytest_args": pytest_args, "categories": selected}

    print(f"Report dir: {out}\nCategories: {', '.join(selected)}")
    print(f"Test python: {test_py}  ({'product venv' if meta['test_side_is_product_venv'] else 'NOT the product venv'})")
    if meta["borrowed_from_runner_venv"]:
        print("Borrowed from the runner venv (absent from the product venv): " + ", ".join(meta["borrowed_from_runner_venv"]))
    print()
    env["QAI_E2E_KEEP_DIR"] = str(out / "backend-logs")   # failed tests leave the backend log + process state here
    baseline = {p for p in WATCHED if port_busy(p)}   # busy before we start: not ours, never reported
    results: list[dict] = []
    for i, cat in enumerate(selected, 1):
        print(f"=== [{i}/{len(selected)}] {cat}: {CATEGORIES[cat][0]}  ({len(files_of(cat))} files)")
        r = run_category(cat, test_py, env, out, pytest_args, args.timeout, args.quiet)
        results.append(r)
        r["leaks"] = find_leaks(baseline)
        for lk in r["leaks"]:
            if args.keep_leaks:
                lk["action"] = "left running (--keep-leaks)"
            else:
                lk["action"] = kill_leak(lk)
                time.sleep(1.0)
            print(f"  !! LEFTOVER port {lk['port']} ({lk['what']}): pid {lk['pid']} {lk['cmd'][:90]}  -> {lk['action']}")
        c = count(parse_junit(Path(r["junit"])))
        print(f"=== {cat}: {r['status']}  {c['passed']} passed, {c['failed']} failed, {c['error']} errors, "
              f"{c['skipped']} skipped  ({fmt_dur(r['duration'])})" + (f"  - {r['note']}" if r["note"] else "") + "\n")
        if args.fail_fast and r["status"] != "PASS":
            print("--fail-fast: stopping.")
            break

    meta["categories"] = [r["name"] for r in results]
    write_reports(out, meta, results)
    summ = json.loads((out / "summary.json").read_text(encoding="utf-8"))["meta"]
    t = summ["totals"]
    print(f"Done: {t['total']} tests - {t['passed']} passed, {t['failed']} failed, {t['error']} errors, "
          f"{t['skipped']} skipped in {fmt_dur(summ['duration'])}")
    if summ["bad_categories"]:
        print("Not passing: " + ", ".join(summ["bad_categories"]))
    if summ.get("leaking_categories"):
        print("Left processes behind (ports): " + ", ".join(summ["leaking_categories"]))
    print(f"HTML : {out / 'report.html'}\nJSON : {out / 'summary.json'}")
    kept = sorted((out / "backend-logs").glob("*.txt")) if (out / "backend-logs").is_dir() else []
    if kept:
        print(f"Backend logs kept for {len(kept)} failing test(s): {out / 'backend-logs'}")
    return 1 if (t["failed"] or t["error"] or summ["bad_categories"]) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
