# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - Service page backend: log stream / clear (SVC-02), config read/write (SVC-03), and the
read-only parts of status / probe / models that those depend on.

Endpoints (``interfaces/http/routes/model_runtime.py``): ``/api/service/{status,probe,models,logs,
logs/clear}``, ``/api/config`` (GET/POST), and the not-installed edges of ``/api/service/{start,
stop,load-model}``.

NOT covered (deliberately):

* **Starting the daemon.** No test here ever spawns a process. A real run needs
  ``GenieAPIService.exe`` (not installed here), and a stand-in executable is blocked by the
  backend's native file guard unless that security switch is turned off - the owner decided the
  suite must not weaken the backend's security settings. Consequently UNTESTED: the live log
  stream (real lines, ``skip`` offsets on a non-empty buffer, ring-buffer wrap-around, ``clear``
  while running), crash handling, ``stop`` of a running daemon, and "daemon dies with the backend".
  Every "installed" fixture below is an inert placeholder file that is never executed.
* **Anything that stores an ``api_key``.** ``POST /api/config`` mirrors a real key into the OS
  keyring (Windows Credential Manager - NOT isolated), and also *reads* it: if the real user has a
  cloud key stored, every save re-writes it. So the tests only send ``""`` / ``"****"`` / no key,
  and the POST tests are SKIPPED when a real credential exists (a read-only check); a final guard
  fails if the module caused a credential to appear.

Isolation (verified): all files live in the throw-away data dir (``<data_dir>/bin/<name>`` and
``<data_dir>/models``); the fixtures delete what they create and reset ``genie_service.root_path``.

Observed behaviour worth pinning (it differs from what one might assume):

* ``POST /api/service/start`` and ``/load-model`` with nothing installed return **500**, while
  ``POST /api/config`` in the same state returns a proper **412**.
* The UI's normal GET -> POST round trip **erases an ``api_key`` that exists only in the JSON
  file**: GET masks it as ``****`` and the save resolves ``****`` from the keyring, finds nothing,
  and writes ``""``.
* A stale ``genie_service.root_path`` makes ``/api/config`` fall back to defaults while
  ``/api/service/status`` still finds the real install - the two disagree. The same happens when
  a newer install appears next to the configured one.
* ``POST /api/config`` does no schema validation (a string where a section is expected is stored),
  and a malformed config file is silently replaced on the next save.
* A config file whose JSON is a list (not an object) makes ``GET /api/config`` return 500.
"""

from __future__ import annotations

import http.server
import json
import os
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import Backend

pytestmark = pytest.mark.e2e

S = "/api/service"
CFG = "/api/config"
FC = "/api/forge-config"
KEY_SERVICES = ("qai.cloud.cloud_model", "qai.cloud.enterprise_cloud_model")


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


def _stored_credentials() -> list[str]:
    """READ-ONLY: which of the two cloud-key services currently hold a credential."""
    try:
        import keyring
    except ImportError:  # pragma: no cover
        return []
    found: list[str] = []
    for svc in KEY_SERVICES:
        try:
            if keyring.get_password(svc, "api_key") is not None:
                found.append(svc)
        except Exception:  # noqa: BLE001 - an unreadable keyring cannot be written by the save path either
            pass
    return found


_CREDS_BEFORE: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def keyring_untouched() -> Iterator[None]:
    """Record the credential state, and fail (after cleaning up) if this module created one."""
    _CREDS_BEFORE[:] = _stored_credentials()
    yield
    created = [s for s in _stored_credentials() if s not in _CREDS_BEFORE]
    if created:
        import keyring

        for svc in created:
            try:
                keyring.delete_password(svc, "api_key")
            except Exception:  # noqa: BLE001
                pass
    assert not created, f"a cloud api_key reached the OS keyring during this module: {created}"


@pytest.fixture
def no_real_key() -> None:
    """Saving re-writes any stored cloud key, so POST /api/config tests need an empty keyring."""
    if _CREDS_BEFORE:
        pytest.skip("a real cloud api_key is stored in the OS keyring; saving would re-write it")


def _set_root(api: httpx.Client, value: str) -> None:
    r = api.post(FC, json={"config": {"genie_service": {"root_path": value}}})
    assert r.status_code == 200, r.text


@pytest.fixture
def install(backend: Backend, api: httpx.Client) -> Iterator[Callable[..., Path]]:
    """Create inert fake installs under ``<data_dir>/bin``; remove them and reset the root after."""
    bin_dir = backend.data_dir / "bin"
    created_bin = not bin_dir.exists()
    made: list[Path] = []

    def _make(
        name: str,
        config: dict | str | bytes | None = None,
        *,
        exe: bool = True,
        mtime_offset: float = 0.0,
        nested: str = "",
    ) -> Path:
        d = bin_dir / name
        target = d / nested if nested else d
        target.mkdir(parents=True, exist_ok=True)
        if exe:
            (d / "GenieAPIService.exe").write_text("placeholder - never executed", encoding="utf-8")
        if config is not None:
            f = target / "service_config.json"
            if isinstance(config, dict):
                f.write_text(json.dumps(config), encoding="utf-8")
            elif isinstance(config, bytes):
                f.write_bytes(config)
            else:
                f.write_text(config, encoding="utf-8")
        t = time.time() + mtime_offset
        os.utime(d, (t, t))
        made.append(d)
        return d

    yield _make
    _set_root(api, "")
    for d in made:
        shutil.rmtree(d, ignore_errors=True)
    if created_bin:
        try:
            bin_dir.rmdir()
        except OSError:
            pass


def _disk(path: Path | str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _cfg(api: httpx.Client) -> dict:
    r = api.get(CFG)
    assert r.status_code == 200, r.text
    return r.json()


def _status(api: httpx.Client) -> dict:
    r = api.get(f"{S}/status")
    assert r.status_code == 200, r.text
    return r.json()


def require_not_installed(api: httpx.Client) -> None:
    if _status(api)["exe_path"]:
        pytest.skip("a GenieAPIService is discoverable from the throw-away data dir on this machine")


def _sse(api: httpx.Client, **params: object) -> tuple[httpx.Response, str, float]:
    t0 = time.monotonic()
    with api.stream("GET", f"{S}/logs", params=params, timeout=httpx.Timeout(10.0, read=8.0)) as r:
        body = "".join(r.iter_text())
    return r, body, time.monotonic() - t0


def _field_errors(r: httpx.Response) -> dict:
    assert r.status_code == 400, r.text
    assert r.json()["code"] == "request.validation"
    return r.json()["details"]["field_errors"]


# --------------------------------------------------------------------------
# not installed
# --------------------------------------------------------------------------


def test_status_when_not_installed(api: httpx.Client) -> None:
    require_not_installed(api)
    st = _status(api)
    assert st == {
        "state": "stopped", "running": False, "ready": None, "pid": None, "uptime_seconds": None, "model": None,
        "port": None, "exe_path": "", "command": "", "memory_mb": 0.0, "path_warning": "",
    }


def test_models_when_not_installed_is_a_list(api: httpx.Client) -> None:
    r = api.get(f"{S}/models")
    assert r.status_code == 200
    assert isinstance(r.json()["models"], list)
    assert "models_root_path" in r.json()


def test_config_defaults_when_not_installed(api: httpx.Client, backend: Backend) -> None:
    require_not_installed(api)
    body = _cfg(api)
    assert body["meta"] == {"using_default_config": True, "config_file_path": "", "genie_root_configured": False}
    cfg = body["config"]
    assert {"cloud_model", "enterprise_cloud_model", "routing", "local_model"} <= set(cfg)
    assert "models" not in cfg and "default_model" not in cfg, "dead model-slot keys are not defaults"
    assert cfg["cloud_model"]["api_key"] == "" and cfg["enterprise_cloud_model"]["api_key"] == ""
    assert _cfg(api) == body  # reading has no side effect
    assert not list(backend.data_dir.rglob("service_config.json")), "a read must not create a config file"


def test_config_save_is_refused_when_not_installed(api: httpx.Client, backend: Backend) -> None:
    require_not_installed(api)
    for payload in ({"config": {"default_model": "x"}}, {"config": {}}):
        r = api.post(CFG, json=payload)
        assert r.status_code == 412, r.text
        assert r.json()["code"] == "model_runtime.service_not_installed"
    assert not list(backend.data_dir.rglob("service_config.json")), "no zombie fallback file may be created"


@pytest.mark.parametrize("payload", [None, {"config": [1, 2]}, {"config": "x"}, {}])
def test_config_save_validation(api: httpx.Client, payload: object) -> None:
    r = api.post(CFG, json=payload) if payload is not None else api.post(CFG)
    errs = _field_errors(r)
    assert any(k.startswith("body") for k in errs)


def test_config_wrong_method(api: httpx.Client) -> None:
    assert api.put(CFG, json={"config": {}}).status_code == 405


def test_start_and_load_model_with_nothing_installed(api: httpx.Client) -> None:
    """KNOWN GAP (current behaviour, pinned): 500, whereas saving the config is a clean 412.

    Safe to call: nothing is installed, so no process can be spawned (guarded by
    ``require_not_installed``). If these start returning 412/4xx that is an improvement.
    """
    require_not_installed(api)
    assert api.post(f"{S}/start", json={}).status_code == 500
    r = api.post(f"{S}/load-model", json={"model_name": "no-such-model"})
    assert r.status_code == 500 and r.json()["code"] == "internal.unexpected"
    assert _status(api)["state"] == "stopped"


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        (f"{S}/start", {"port": 0}, "body.port"),
        (f"{S}/start", {"port": 65536}, "body.port"),
        (f"{S}/start", {"loglevel": 9}, "body.loglevel"),
        (f"{S}/load-model", {"model_name": ""}, "body.model_name"),
        (f"{S}/load-model", {}, "body.model_name"),
    ],
)
def test_start_and_load_validation_rejects_before_anything_runs(
    api: httpx.Client, path: str, body: dict, field: str
) -> None:
    assert field in _field_errors(api.post(path, json=body))


def test_stop_when_nothing_is_running_is_ok(api: httpx.Client) -> None:
    for _ in range(2):
        r = api.post(f"{S}/stop")
        assert r.status_code == 200 and r.json()["status"] == "stopped"


# --------------------------------------------------------------------------
# SVC-02: logs
# --------------------------------------------------------------------------


def test_log_stream_when_stopped_sends_done_and_closes(api: httpx.Client) -> None:
    require_not_installed(api)
    r, body, elapsed = _sse(api)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    assert r.headers["x-accel-buffering"] == "no"
    assert body == "data: [DONE]\n\n"
    assert elapsed < 6, "the stream must close on its own, not hang"


def test_log_stream_skip_beyond_the_buffer_is_empty_not_an_error(api: httpx.Client) -> None:
    require_not_installed(api)
    r, body, _ = _sse(api, skip=99999)
    assert r.status_code == 200 and body == "data: [DONE]\n\n"


@pytest.mark.parametrize("skip", ["-1", "abc", "1.5", ""])
def test_log_stream_rejects_bad_skip(api: httpx.Client, skip: str) -> None:
    r = api.get(f"{S}/logs", params={"skip": skip})
    assert "query.skip" in _field_errors(r)


def test_log_clear_is_idempotent_and_reports_the_offset(api: httpx.Client) -> None:
    require_not_installed(api)
    for _ in range(2):
        r = api.post(f"{S}/logs/clear")
        assert r.status_code == 200
        assert r.json() == {"status": "cleared", "skip_from": 0}


def test_log_endpoints_reject_wrong_methods(api: httpx.Client) -> None:
    assert api.post(f"{S}/logs").status_code == 405
    assert api.get(f"{S}/logs/clear").status_code in (404, 405)


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("params", "field"),
    [({"port": 0}, "query.port"), ({"port": 65536}, "query.port"), ({"port": "abc"}, "query.port")],
)
def test_probe_validates_port(api: httpx.Client, params: dict, field: str) -> None:
    assert field in _field_errors(api.get(f"{S}/probe", params=params))


class _Listener:
    """A throw-away HTTP server owned by the test; answers every GET with ``code``."""

    def __init__(self) -> None:
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                self.send_response(outer.code)
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *a: object) -> None:
                pass

        self.code = 200
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


@pytest.fixture
def listener() -> Iterator[_Listener]:
    lst = _Listener()
    yield lst
    try:
        lst.close()
    except Exception:  # noqa: BLE001
        pass


def _probe(api: httpx.Client, **params: object) -> dict:
    r = api.get(f"{S}/probe", params=params)
    assert r.status_code == 200, r.text
    return r.json()


DOWN = {"reachable": False, "alive": False, "model": None}


def test_probe_local_daemon_when_stopped(api: httpx.Client) -> None:
    assert _probe(api) == DOWN


@pytest.mark.parametrize(
    ("code", "up"), [(200, True), (401, True), (404, True), (500, False), (503, False)]
)
def test_probe_arbitrary_address_status_mapping(api: httpx.Client, listener: _Listener, code: int, up: bool) -> None:
    listener.code = code
    got = _probe(api, host="127.0.0.1", port=listener.port)
    assert got["reachable"] is up and got["alive"] is up


def test_probe_needs_both_host_and_port_to_leave_the_local_daemon(api: httpx.Client, listener: _Listener) -> None:
    """With a live listener on the port, host-only / port-only still probe the (stopped) local daemon."""
    assert _probe(api, port=listener.port) == DOWN
    assert _probe(api, host="127.0.0.1") == DOWN


def test_probe_reports_down_when_the_listener_goes_away(api: httpx.Client, listener: _Listener) -> None:
    assert _probe(api, host="127.0.0.1", port=listener.port)["alive"] is True
    listener.close()
    assert _probe(api, host="127.0.0.1", port=listener.port) == DOWN


def test_probe_unresolvable_host_is_down_not_an_error(api: httpx.Client) -> None:
    assert _probe(api, host="no-such-host.invalid", port=80) == DOWN


# --------------------------------------------------------------------------
# models scan (read-only listing of <data_dir>/models)
# --------------------------------------------------------------------------


@pytest.fixture
def model_dirs(backend: Backend) -> Iterator[Callable[[str, dict], Path]]:
    root = backend.data_dir / "models"
    created_root = not root.exists()
    made: list[Path] = []

    def _mk(rel: str, files: dict) -> Path:
        d = root / rel
        d.mkdir(parents=True, exist_ok=True)
        for fn, data in files.items():
            (d / fn).write_bytes(data if isinstance(data, bytes) else data.encode())
        made.append(root / rel.split("/")[0])
        return d

    yield _mk
    for d in made:
        shutil.rmtree(d, ignore_errors=True)
    if created_root:
        try:
            root.rmdir()
        except OSError:
            pass


def _models(api: httpx.Client) -> dict[str, dict]:
    r = api.get(f"{S}/models")
    assert r.status_code == 200, r.text
    return {m["name"]: m for m in r.json()["models"]}


def test_models_scan_detects_format_and_skips_unusable_dirs(
    api: httpx.Client, model_dirs: Callable[[str, dict], Path]
) -> None:
    ctx = json.dumps({"dialog": {"context": {"size": 4096}}})
    model_dirs("e2e-qnn", {"genie_config.json": ctx, "w.bin": b"x" * 1024, "tokenizer.json": "{}"})
    model_dirs("e2e-qnn-no-tokenizer", {"config.json": "{}", "w.bin": b"x"})
    model_dirs("e2e-gguf", {"config.json": "{}", "model.GGUF": b"x" * (3 * 1024 * 1024)})
    model_dirs("e2e-mnn", {"config.json": "{}", "llm.mnn": b"x"})
    model_dirs("e2e-unknown", {"config.json": "{}", "readme.txt": "hi"})
    model_dirs("e2e-no-config", {"model.gguf": b"x"})
    model_dirs("e2e-nested/deep/e2e-inner", {"config.json": "{}", "a.gguf": b"x"})
    got = _models(api)
    assert got["e2e-qnn"]["format"] == "qnn" and got["e2e-qnn"]["context_length"] == 4096
    assert got["e2e-gguf"]["format"] == "gguf", "suffix match is case-insensitive"
    assert got["e2e-mnn"]["format"] == "mnn"
    assert got["e2e-inner"]["format"] == "gguf", "model dirs are found at any depth"
    for skipped in ("e2e-qnn-no-tokenizer", "e2e-unknown", "e2e-no-config"):
        assert skipped not in got
    assert got["e2e-gguf"]["size_mb"] == 3.0
    assert got["e2e-gguf"]["is_running"] is False


def test_models_gguf_wins_over_mnn_and_genie_config_wins_over_config(
    api: httpx.Client, model_dirs: Callable[[str, dict], Path]
) -> None:
    model_dirs("e2e-both", {
        "genie_config.json": json.dumps({"dialog": {"context": {"size": 111}}}),
        "config.json": json.dumps({"dialog": {"context": {"size": 222}}}),
        "a.mnn": b"x",
    })
    model_dirs("e2e-all-formats", {"config.json": "{}", "a.mnn": b"x", "b.gguf": b"x", "c.bin": b"x", "tokenizer.json": "{}"})
    got = _models(api)
    assert got["e2e-both"]["context_length"] == 111
    assert got["e2e-both"]["config_path"].endswith("genie_config.json")
    assert got["e2e-all-formats"]["format"] == "gguf"


def test_models_context_length_sources(api: httpx.Client, model_dirs: Callable[[str, dict], Path]) -> None:
    model_dirs("e2e-ctx-prompt", {"config.json": "{}", "a.gguf": b"x", "prompt.json": json.dumps({"context_size": 6000})})
    model_dirs("e2e-ctx-none", {"config.json": "{}", "a.gguf": b"x"})
    model_dirs("e2e-ctx-bad", {"config.json": "{not json", "a.gguf": b"x"})
    got = _models(api)
    assert got["e2e-ctx-prompt"]["context_length"] == 6000
    assert got["e2e-ctx-none"]["context_length"] is None
    assert got["e2e-ctx-bad"]["context_length"] is None, "a malformed config must not break the listing"


def test_models_listing_ignores_stray_files(api: httpx.Client, backend: Backend, model_dirs: Callable[[str, dict], Path]) -> None:
    model_dirs("e2e-ok", {"config.json": "{}", "a.gguf": b"x"})
    stray = backend.data_dir / "models" / "e2e-stray.txt"
    stray.write_text("x")
    try:
        assert "e2e-ok" in _models(api)
    finally:
        stray.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# status: install discovery / path warning
# --------------------------------------------------------------------------


def test_status_reports_exe_path_of_an_install_under_data_bin(api: httpx.Client, install: Callable[..., Path]) -> None:
    require_not_installed(api)
    d = install("e2e-v1", {})
    st = _status(api)
    assert st["exe_path"] == str(d / "GenieAPIService.exe")
    assert st["state"] == "stopped" and st["running"] is False
    assert st["path_warning"] == ""


@pytest.mark.parametrize("name", ["e2e v1", "e2e-版本"])
def test_status_warns_about_spaces_and_non_ascii_in_the_install_path(
    api: httpx.Client, install: Callable[..., Path], name: str
) -> None:
    require_not_installed(api)
    install(name, {})
    warning = _status(api)["path_warning"]
    assert warning and "non-ASCII" in warning and name in warning


# --------------------------------------------------------------------------
# SVC-03: config read
# --------------------------------------------------------------------------


def test_config_reads_the_installed_file(api: httpx.Client, install: Callable[..., Path]) -> None:
    require_not_installed(api)
    d = install("e2e-v1", {"default_model": "from-file", "custom_top": {"a": 1}})
    body = _cfg(api)
    assert body["config"]["default_model"] == "from-file"
    assert body["config"]["custom_top"] == {"a": 1}, "unknown keys survive the read"
    assert "routing" in body["config"], "defaults are merged under the file"
    assert body["meta"]["config_file_path"] == str(d / "service_config.json")
    assert body["meta"]["using_default_config"] is False


def test_config_api_key_in_the_file_is_masked_in_the_response(api: httpx.Client, install: Callable[..., Path]) -> None:
    require_not_installed(api)
    secret, secret2 = "E2E-PLAINTEXT-SENTINEL-8841", "E2E-PLAINTEXT-SENTINEL-ENT-2217"
    install("e2e-v1", {
        "cloud_model": {"base_url": "https://x.invalid", "api_key": secret},
        "enterprise_cloud_model": {"base_url": "https://y.invalid", "api_key": secret2},
    })
    r = api.get(CFG)
    assert r.status_code == 200
    assert secret not in r.text and secret2 not in r.text
    assert r.json()["config"]["cloud_model"]["api_key"] == "****"
    assert r.json()["config"]["enterprise_cloud_model"]["api_key"] == "****"


def test_config_without_a_stored_key_reports_empty_not_a_mask(api: httpx.Client, install: Callable[..., Path]) -> None:
    require_not_installed(api)
    if _CREDS_BEFORE:
        pytest.skip("a real cloud api_key is stored in the OS keyring; the response would legitimately be masked")
    install("e2e-v1", {"cloud_model": {"api_key": ""}, "enterprise_cloud_model": {"api_key": ""}})
    cfg = _cfg(api)["config"]
    assert cfg["cloud_model"]["api_key"] == "" and cfg["enterprise_cloud_model"]["api_key"] == ""


def test_config_reads_a_utf8_bom_file(api: httpx.Client, install: Callable[..., Path]) -> None:
    require_not_installed(api)
    install("e2e-v1", b"\xef\xbb\xbf" + json.dumps({"default_model": "with-bom"}).encode())
    assert _cfg(api)["config"]["default_model"] == "with-bom"


def test_config_malformed_file_falls_back_to_defaults(api: httpx.Client, install: Callable[..., Path]) -> None:
    require_not_installed(api)
    d = install("e2e-v1", "{not json")
    body = _cfg(api)
    assert "default_model" not in body["config"]
    assert body["meta"]["config_file_path"] == str(d / "service_config.json")


def test_config_file_holding_a_json_list_is_a_500(api: httpx.Client, install: Callable[..., Path]) -> None:
    """KNOWN GAP (current behaviour, pinned): a non-object document is not handled like a malformed one."""
    require_not_installed(api)
    install("e2e-v1", "[1, 2]")
    r = api.get(CFG)
    assert r.status_code == 500 and r.json()["code"] == "internal.unexpected"


# --------------------------------------------------------------------------
# SVC-03: config write
# --------------------------------------------------------------------------


def test_config_save_merges_into_the_file_and_keeps_unknown_keys(
    api: httpx.Client, install: Callable[..., Path], no_real_key: None
) -> None:
    require_not_installed(api)
    d = install("e2e-v1", {
        "default_model": "orig",
        "cloud_model": {"base_url": "https://keep.invalid", "api_key": "", "extra_user_key": 7},
        "custom_top": {"a": 1},
    })
    r = api.post(CFG, json={"config": {"default_model": "新模型", "routing": {"enabled": False}}})
    assert r.status_code == 200 and r.json() == {"status": "saved"}
    disk = _disk(d / "service_config.json")
    assert disk["default_model"] == "新模型"
    assert disk["routing"]["enabled"] is False
    assert disk["routing"].get("cache"), "sibling defaults inside a partially-saved section are kept"
    assert disk["custom_top"] == {"a": 1}
    assert disk["cloud_model"]["extra_user_key"] == 7
    assert disk["cloud_model"]["base_url"] == "https://keep.invalid"
    assert {"local_model", "routing", "cloud_model"} <= set(disk), "defaults are materialised on first save"
    assert _cfg(api)["config"]["default_model"] == "新模型"


def test_config_save_file_format(api: httpx.Client, install: Callable[..., Path], no_real_key: None) -> None:
    require_not_installed(api)
    d = install("e2e-v1", {})
    assert api.post(CFG, json={"config": {"default_model": "新模型"}}).status_code == 200
    raw = (d / "service_config.json").read_bytes()
    assert not raw.startswith(b"\xef\xbb\xbf"), "no BOM is written"
    text = raw.decode("utf-8")
    assert "新模型" in text, "non-ASCII is written literally, not \\u-escaped"
    second_line = text.splitlines()[1]
    assert second_line.startswith("    ") and not second_line.startswith("     "), "indent is 4 spaces"


def test_config_save_replaces_lists_instead_of_merging(
    api: httpx.Client, install: Callable[..., Path], no_real_key: None
) -> None:
    require_not_installed(api)
    d = install("e2e-v1", {})
    for value in (["a", "b", "c"], ["z"], []):
        assert api.post(CFG, json={"config": {"prompt_optimization": {"allowed_tools": value}}}).status_code == 200
        assert _disk(d / "service_config.json")["prompt_optimization"]["allowed_tools"] == value


def test_config_save_accepts_unknown_keys_and_wrong_types(
    api: httpx.Client, install: Callable[..., Path], no_real_key: None
) -> None:
    """KNOWN GAP (current behaviour, pinned): no schema validation on save.

    A string where an object section belongs is stored as-is. The file stays readable (GET is 200),
    but the section is then not an object. If validation is added this becomes a 4xx - update it.
    """
    require_not_installed(api)
    d = install("e2e-v1", {})
    assert api.post(CFG, json={"config": {"brand_new": {"x": 1}}}).status_code == 200
    assert _disk(d / "service_config.json")["brand_new"] == {"x": 1}
    assert api.post(CFG, json={"config": {"routing": "oops"}}).status_code == 200
    assert _disk(d / "service_config.json")["routing"] == "oops"
    assert api.get(CFG).status_code == 200


def test_config_save_on_a_malformed_file_replaces_it_with_defaults_plus_the_change(
    api: httpx.Client, install: Callable[..., Path], no_real_key: None
) -> None:
    """KNOWN GAP (current behaviour, pinned): the unreadable original is discarded without a backup."""
    require_not_installed(api)
    d = install("e2e-v1", "{not json")
    assert api.post(CFG, json={"config": {"default_model": "after-corrupt"}}).status_code == 200
    disk = _disk(d / "service_config.json")
    assert disk["default_model"] == "after-corrupt" and "routing" in disk
    assert not list(d.glob("service_config.json.*")) and not list(d.glob("*.bak")), "no backup is kept"


def test_ui_round_trip_erases_an_api_key_that_only_lives_in_the_file(
    api: httpx.Client, install: Callable[..., Path], no_real_key: None
) -> None:
    """KNOWN GAP (current behaviour, pinned): GET masks the key as ``****``; saving that mask looks the
    key up in the keyring, finds nothing, and writes ``""`` - the key in the file is lost.

    Realistic trigger: an installer- or hand-written key. Nothing here touches the keyring (an empty
    key is never stored). If the save is fixed to keep the existing file value, update this test.
    """
    require_not_installed(api)
    d = install("e2e-v1", {"cloud_model": {"base_url": "https://x.invalid", "api_key": "FILE-ONLY-KEY"}})
    got = _cfg(api)["config"]
    assert got["cloud_model"]["api_key"] == "****"
    assert api.post(CFG, json={"config": got}).status_code == 200
    assert _disk(d / "service_config.json")["cloud_model"]["api_key"] == ""
    assert _cfg(api)["config"]["cloud_model"]["api_key"] == ""


def test_config_save_with_an_empty_key_keeps_the_field_empty_and_stores_nothing(
    api: httpx.Client, install: Callable[..., Path], no_real_key: None
) -> None:
    require_not_installed(api)
    d = install("e2e-v1", {})
    r = api.post(CFG, json={"config": {"cloud_model": {"base_url": "https://x.invalid", "api_key": ""}}})
    assert r.status_code == 200 and "secret_store_errors" not in r.json()
    assert _disk(d / "service_config.json")["cloud_model"]["api_key"] == ""
    assert _stored_credentials() == []


# --------------------------------------------------------------------------
# SVC-03: which config file is "the" file (forge-config root vs data/bin discovery)
# --------------------------------------------------------------------------


def test_forge_root_selects_the_config_file_and_tolerates_whitespace(
    api: httpx.Client, install: Callable[..., Path]
) -> None:
    require_not_installed(api)
    a = install("e2e-a", {"default_model": "A"})
    _set_root(api, str(a))
    assert _cfg(api)["config"]["default_model"] == "A"
    _set_root(api, f"  {a}  ")
    body = _cfg(api)
    assert body["meta"]["config_file_path"] == str(a / "service_config.json"), "whitespace around the root is ignored"
    assert body["config"]["default_model"] == "A"


def test_config_search_depth_below_the_root_is_three_levels(api: httpx.Client, install: Callable[..., Path]) -> None:
    require_not_installed(api)
    d = install("e2e-deep", {"default_model": "L3"}, exe=False, nested="l1/l2/l3")
    _set_root(api, str(d))
    assert _cfg(api)["config"]["default_model"] == "L3"
    install("e2e-deeper", {"default_model": "L4"}, exe=False, nested="l1/l2/l3/l4")
    _set_root(api, str(d.parent / "e2e-deeper"))
    assert _cfg(api)["config"]["default_model"] != "L4", "a file four levels down is not found"


@pytest.mark.parametrize("kind", ["missing-dir", "relative", "dir-without-config", "a-file"])
def test_unusable_forge_root_masks_the_real_install(
    api: httpx.Client, install: Callable[..., Path], backend: Backend, kind: str
) -> None:
    """KNOWN GAP (current behaviour, pinned): a configured-but-unusable root is NOT replaced by
    auto-discovery. ``/api/config`` falls back to defaults (and refuses to save with 412) while
    ``/api/service/status`` still finds the install - and ``using_default_config`` is False even
    though no file resolved, so the UI would show defaults as if they were the real config.
    """
    require_not_installed(api)
    real = install("e2e-real", {"default_model": "REAL"})
    assert _cfg(api)["config"]["default_model"] == "REAL"  # discovery works without a root
    bin_dir = backend.data_dir / "bin"
    if kind == "missing-dir":
        root = str(bin_dir / "deleted-version")
    elif kind == "relative":
        root = r"data\bin\deleted"
    elif kind == "dir-without-config":
        (bin_dir / "e2e-empty").mkdir()
        root = str(bin_dir / "e2e-empty")
    else:
        (bin_dir / "e2e-afile.txt").write_text("x")
        root = str(bin_dir / "e2e-afile.txt")
    try:
        _set_root(api, root)
        body = _cfg(api)
        assert "default_model" not in body["config"], "defaults are shown, not the real install's file"
        assert body["meta"]["config_file_path"] == ""
        assert body["meta"]["using_default_config"] is False
        assert _status(api)["exe_path"] == str(real / "GenieAPIService.exe")
        assert api.post(CFG, json={"config": {"default_model": "x"}}).status_code == 412
    finally:
        shutil.rmtree(bin_dir / "e2e-empty", ignore_errors=True)
        (bin_dir / "e2e-afile.txt").unlink(missing_ok=True)


def test_status_exe_follows_the_newest_install_while_config_follows_the_forge_root(
    api: httpx.Client, install: Callable[..., Path]
) -> None:
    """KNOWN GAP (current behaviour, pinned): after a newer install appears next to the configured
    one, the daemon would be launched from the new directory while the UI keeps editing the old
    directory's ``service_config.json``."""
    require_not_installed(api)
    old = install("e2e-a", {"default_model": "A"}, mtime_offset=0)
    new = install("e2e-b", {"default_model": "B"}, mtime_offset=100)
    _set_root(api, str(old))
    assert _cfg(api)["meta"]["config_file_path"] == str(old / "service_config.json")
    assert _status(api)["exe_path"] == str(new / "GenieAPIService.exe")


def test_forge_root_can_be_reset(api: httpx.Client, install: Callable[..., Path]) -> None:
    require_not_installed(api)
    a = install("e2e-a", {"default_model": "A"})
    _set_root(api, str(a.parent / "e2e-gone"))
    assert "default_model" not in _cfg(api)["config"]
    _set_root(api, "")
    assert _cfg(api)["config"]["default_model"] == "A"
