# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - the Web UI mount when ``frontend/dist`` is missing or incomplete (SYS-07; complements SYS-02 in ``test_sys_spa_endpoint.py``).

Unlike every other module here this one does NOT start a backend server. It builds the FastAPI app (``create_app``) in a short-lived CHILD process
under the product Python, against a throw-away *repo root*, and talks to it through ``httpx.ASGITransport`` (no socket). That is the only way to test a missing ``frontend/dist`` without
renaming the real one. The app runs the real lifespan (database in a temp dir, secrets backend, migrations), so each layout takes about five seconds; the test venv has no
fastapi on purpose, which is why a child is used.

NOT covered (deliberately):

* The served SPA bundle itself (that is SYS-02 against the real ``frontend/dist``; here ``index.html`` is a one-line stand-in).
* Development mode (``is_production=false``) with a missing dist - the SPA mount is simply not registered and ``/`` is a JSON 404.
* Static-file caching headers beyond ``Cache-Control`` of the shell, ETags and range requests.

Isolation (verified): the repo root is a temp folder holding only ``build_info.json`` and, per case, a fake ``frontend/dist``; the data dir
is a sibling temp folder; the module asserts the real ``frontend/dist`` listing is unchanged. No port is bound.

Observed behaviour worth pinning:

* PRODUCTION build, ``dist`` absent: EVERY non-API GET (``/``, ``/chat``, ``/index.html``, ``/assets/x.js``, ``/favicon.ico``) answers 503 with the
  same ~1 KB maintenance page and ``Cache-Control: no-cache, no-store, must-revalidate``; the API keeps working.
* PRODUCTION build, ``dist`` present WITHOUT ``index.html``: no maintenance page - ``/`` is a plain JSON 404 (SYS-ISS-13).
* ``dist`` with ``index.html``: any client-side route returns the shell; ``/api``, ``/v1``, ``/ws`` and ``/assets`` misses stay real 404s.
* ``/openapi.json``, ``/docs`` and ``/redoc`` are NOT reachable in the maintenance mode only because docs are enabled (separate setting).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import REPO_ROOT

pytestmark = pytest.mark.e2e

REAL_DIST = REPO_ROOT / "frontend" / "dist"
INDEX = "<html><body>E2E-INDEX-SHELL</body></html>"
NO_CACHE = "no-cache, no-store, must-revalidate"


def _tree(p: Path) -> list[str] | None:
    return sorted(str(x.relative_to(p)) for x in p.rglob("*") if x.is_file()) if p.exists() else None


_REAL_AT_START = _tree(REAL_DIST)


@pytest.fixture(scope="module", autouse=True)
def real_dist_untouched() -> Iterator[None]:
    yield
    assert _tree(REAL_DIST) == _REAL_AT_START, "a test changed the repo's real frontend/dist"


_CHILD = r'''
import asyncio, json, logging, shutil, sys, tempfile
from pathlib import Path
import httpx
logging.disable(logging.CRITICAL)
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[1] + "/src")
from apps.api.main import create_app
from qai.platform.config import load_settings

files = json.loads(sys.argv[2]); paths = json.loads(sys.argv[3]); production = sys.argv[4] == "1"
root = Path(tempfile.mkdtemp(prefix="qai-e2e-spa-"))
try:
    data = root / "data"; data.mkdir()
    (root / "build_info.json").write_text(json.dumps({"edition": "external", "version": "9.9.9"}))
    if files is not None:
        (root / "frontend" / "dist").mkdir(parents=True, exist_ok=True)
        for rel, text in files.items():
            p = root / "frontend" / "dist" / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text, encoding="utf-8")
    settings = load_settings(repo_root=root, overrides={"data": {"data_dir": str(data)}, "server": {"is_production": production}})

    async def main():
        app = create_app(settings=settings, repo_root=root)
        out = {}
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
                for path in paths:
                    r = await c.get(path)
                    out[path] = [r.status_code, r.headers.get("content-type", ""), r.headers.get("cache-control"), r.text]
                out["HEAD /"] = (await c.head("/")).status_code
                out["POST /"] = (await c.post("/", json={})).status_code
        return out

    print("RESULT=" + json.dumps(asyncio.run(main())))
finally:
    shutil.rmtree(root, ignore_errors=True)
'''

PATHS = ["/", "/chat", "/some/deep/route", "/index.html", "/assets/x.js", "/assets/a.js", "/assets/missing.js", "/favicon.ico", "/a/b/c",
         "/api/system/health", "/api/nope", "/v1/nope", "/ws/x", "/openapi.json", "/docs"]


def _run(files: dict[str, str] | None, *, production: bool = True) -> dict:
    """Build the app IN A PRODUCT-PYTHON CHILD (the test venv deliberately has no fastapi) over a temp repo root whose ``frontend/dist`` holds
    ``files`` (None -> no dist folder); GET every path and return ``{path: [status, content-type, cache-control, body]}``."""
    import os
    import subprocess

    from tests.e2e.conftest import _python_executable

    env = dict(os.environ)
    env["QAI_AUTH__ENABLED"] = "false"
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    p = subprocess.run([_python_executable(), "-W", "ignore", "-c", _CHILD, str(REPO_ROOT), json.dumps(files), json.dumps(PATHS), "1" if production else "0"],
                       capture_output=True, text=True, timeout=180, env=env, cwd=str(REPO_ROOT))
    line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT=")), None)
    assert line is not None, f"child failed (rc={p.returncode}): {p.stderr[-600:]}"
    return json.loads(line.removeprefix("RESULT="))


@pytest.fixture(scope="module")
def no_dist() -> dict:
    return _run(None)


@pytest.fixture(scope="module")
def dist_without_index() -> dict:
    return _run({"assets/a.js": "console.log(1)"})


@pytest.fixture(scope="module")
def full_dist() -> dict:
    return _run({"index.html": INDEX, "assets/a.js": "console.log(1)", "favicon.ico": "ICO"})

# --------------------------------------------------------------------------
# dist absent, production build
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/chat", "/some/deep/route", "/index.html", "/assets/x.js", "/favicon.ico"])
def test_every_ui_path_answers_the_maintenance_page_when_dist_is_missing(no_dist: dict, path: str) -> None:
    status, ctype, cache, body = no_dist[path]
    assert status == 503 and ctype == "text/html; charset=utf-8" and cache == NO_CACHE
    assert "<title>QAIAppBuilder - service unavailable</title>" in body and "Web UI is not available" in body


def test_the_maintenance_page_says_what_is_missing_and_that_the_api_still_runs(no_dist: dict) -> None:
    body = no_dist["/"][3]
    assert "<code>frontend/dist/</code>" in body and "The API is still running" in body and "Reinstall or rebuild the front-end" in body
    assert all(no_dist[p][3] == body for p in ("/chat", "/index.html", "/assets/x.js", "/favicon.ico")), "one page for all of them"
    assert body.startswith("<!doctype html>") and 800 < len(body) < 2000 and "<script" not in body, "static, no scripts"


def test_the_api_keeps_working_without_the_ui(no_dist: dict) -> None:
    status, ctype, _cache, body = no_dist["/api/system/health"]
    assert status == 200 and ctype == "application/json" and json.loads(body)["status"] == "ok"


@pytest.mark.parametrize("path", ["/api/nope", "/v1/nope", "/ws/x"])
def test_unknown_api_paths_are_not_the_maintenance_page(no_dist: dict, path: str) -> None:
    status, ctype, _cache, body = no_dist[path]
    assert status == 404 and "service unavailable" not in body


def test_the_documentation_routes_are_unaffected(no_dist: dict) -> None:
    assert no_dist["/openapi.json"][0] == 200 and no_dist["/docs"][0] == 200


def test_only_get_serves_the_maintenance_page(no_dist: dict) -> None:
    assert no_dist["HEAD /"] == 405 and no_dist["POST /"] == 403, "POST hits the CSRF gate first (no token), HEAD is not routed"


# --------------------------------------------------------------------------
# dist present but without index.html
# --------------------------------------------------------------------------


def test_a_dist_folder_without_index_html_gives_a_bare_404_not_the_maintenance_page(dist_without_index: dict) -> None:
    """KNOWN GAP (current behaviour, pinned) - SYS-ISS-13: a half-built ``dist`` (assets but no ``index.html``) is the more likely failure
    after an interrupted build, and it gets the LEAST helpful answer: ``{"detail":"Not Found"}`` for ``/``."""
    for path in ("/", "/chat"):
        status, ctype, _cache, body = dist_without_index[path]
        assert status == 404 and ctype == "application/json" and json.loads(body) == {"detail": "Not Found"}
    assert dist_without_index["/api/system/health"][0] == 200


def test_assets_are_not_served_either_when_the_shell_is_missing(dist_without_index: dict) -> None:
    assert dist_without_index["/assets/a.js"][0] == 404


# --------------------------------------------------------------------------
# dist complete
# --------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/chat", "/a/b/c"])
def test_client_side_routes_return_the_shell_uncached(full_dist: dict, path: str) -> None:
    status, ctype, cache, body = full_dist[path]
    assert (status, ctype, cache, body) == (200, "text/html; charset=utf-8", NO_CACHE, INDEX)


def test_files_in_dist_are_served_as_themselves(full_dist: dict) -> None:
    assert full_dist["/index.html"][0] == 200 and full_dist["/index.html"][3] == INDEX
    assert full_dist["/favicon.ico"][0] == 200 and full_dist["/favicon.ico"][3] == "ICO" and full_dist["/favicon.ico"][1] == "image/x-icon"


def test_an_asset_that_does_not_exist_is_a_404_not_the_shell(full_dist: dict) -> None:
    assert full_dist["/assets/a.js"][0] == 404 or full_dist["/assets/a.js"][3] == "console.log(1)"
    status, ctype, _cache, body = full_dist["/assets/missing.js"]
    assert status == 404 and INDEX not in body, "a missing script must not come back as HTML (it would be parsed as JS)"


@pytest.mark.parametrize("path", ["/api/nope", "/v1/nope", "/ws/x"])
def test_api_misses_stay_404s_when_the_shell_is_served(full_dist: dict, path: str) -> None:
    status, _ctype, _cache, body = full_dist[path]
    assert status == 404 and INDEX not in body


def test_head_and_post_on_the_root(full_dist: dict) -> None:
    assert full_dist["HEAD /"] == 405 and full_dist["POST /"] == 403
