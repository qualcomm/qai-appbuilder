# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - the ``qai`` command line: help text and the read-only provider listing (INST-08).

``qai.bat`` is a thin launcher: it picks the venv (``--arch`` flag > ``data\\config\\host_arch`` > CPU), sets
``PYTHONPATH`` to this checkout and runs ``python -m apps.cli``. These tests run ``python -m apps.cli`` directly with
the test interpreter and ``QAI_DATA__DATA_DIR`` pointing at a throw-away folder (the same pattern as the CLI tests in
``test_model_builder.py``); the ``.bat`` wrapper itself is exercised once, only through ``--help``.

NOT covered (deliberately):

* **``qai install-pack-deps --help``.** It is NOT a help request: the command is registered with ``add_help=False``
  and forwards every argument to ``scripts/setup/install_app_builder_deps.py``, which does not parse arguments and goes
  straight to ``uv pip install`` against the PRODUCT venv. With the dependencies already present it only prints
  ``Checked N packages`` (verified: nothing in site-packages changed), but on a machine where something is missing it
  would download and install. Never invoked here. (KNOWN-ISSUES INST-ISS-01.)
* **``install-qairt`` / ``uninstall`` / ``api`` / ``code`` ... beyond ``--help``.** They install SDKs, delete the
  installation or start servers.
* **``qai config provider add|edit|remove|test`` and ``qai auth``.** They write the OS keyring (Credential Manager is not
  isolated) or make real network requests.
* **The ``--arch`` / ``host_arch`` selection inside ``qai.bat``** and the "venv missing" branch: they need a second venv or
  renaming the real one.
* The interactive wizards (``qai config setup``, ``provider add`` on a TTY).

Isolation (verified): every spawned CLI gets ``QAI_DATA__DATA_DIR`` set to a ``qai-e2e-cli-`` temp folder, so its database and
config are created there; ``stdin`` is closed so a prompt cannot hang the run; the module asserts the repo's ``data/`` folder
has no new or changed file and that the keyring holds no ``qai.cloud.*`` credential afterwards.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.e2e.conftest import REPO_ROOT, _python_executable

pytestmark = pytest.mark.e2e

# Commands whose --help is argparse's own (verified one by one; see the module docstring for the ones that are NOT).
SAFE_GROUPS = (
    "config", "auth", "service", "pack", "run", "conv", "code", "policy", "perm", "security", "audit",
    "channel", "skill", "dep", "exec", "service-release",
)
DELEGATING_BUT_ARGPARSE_FIRST = ("install-qairt", "uninstall", "api")


def _read_keyring() -> dict:
    try:
        import keyring
    except ImportError:  # pragma: no cover
        return {}
    out = {}
    for svc in ("qai.cloud.cloud_model", "qai.cloud.enterprise_cloud_model"):
        try:
            out[svc] = keyring.get_password(svc, "api_key")
        except Exception:  # noqa: BLE001
            out[svc] = None
    return out


_KEYRING_AT_START = _read_keyring()


@dataclass
class Run:
    rc: int
    out: str
    err: str


@pytest.fixture(scope="module")
def data_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="qai-e2e-cli-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="module", autouse=True)
def repo_data_untouched() -> Iterator[None]:
    """The repo's own ``data/`` folder must not change while these tests run (a wrongly isolated CLI would write there)."""
    root = REPO_ROOT / "data"

    def _snapshot() -> dict[str, tuple[int, int]]:
        if not root.exists():
            return {}
        return {
            str(p.relative_to(root)): (p.stat().st_size, int(p.stat().st_mtime))
            for p in root.rglob("*")
            if p.is_file() and "caches" not in p.parts and "pytest" not in p.parts
        }

    before = _snapshot()
    yield
    after = _snapshot()
    changed = sorted(k for k in set(before) | set(after) if before.get(k) != after.get(k))
    assert not changed, f"the CLI touched the product data folder: {changed[:10]}"


@pytest.fixture(scope="module")
def qai(data_dir: Path) -> Callable[..., Run]:
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT / "src"), str(REPO_ROOT)])
    env["PYTHONIOENCODING"] = "utf-8"
    env["QAI_DATA__DATA_DIR"] = str(data_dir)
    env["PYTHONWARNINGS"] = "ignore"

    def _run(*args: str, timeout: float = 120.0) -> Run:
        p = subprocess.run(
            [_python_executable(), "-m", "apps.cli", *args], cwd=str(REPO_ROOT), env=env,
            capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=timeout,
        )
        return Run(p.returncode, p.stdout, p.stderr)

    return _run


# --------------------------------------------------------------------------
# the top-level help
# --------------------------------------------------------------------------


def test_top_level_help_lists_every_command_group(qai: Callable[..., Run]) -> None:
    r = qai("--help")
    assert r.rc == 0
    assert all(("Warning" in l) or not l.strip() for l in r.err.splitlines()), f"unexpected stderr: {r.err[:300]}"
    assert r.out.startswith("usage: qai [-h] [--repo-root <path>] [--config <server.toml>] <command> ...")
    assert "QAIModelBuilder unified CLI" in r.out
    listed = re.findall(r"^    ([a-z][a-z0-9-]*)\s", r.out, flags=re.M)
    expected = {"config", "auth", "service", "install-qairt", "install-pack-deps", "uninstall", "compile-factory", "api", "pack",
                "run", "conv", "code", "policy", "perm", "security", "audit", "channel", "skill", "dep", "exec",
                "service-release", "build", "app"}
    assert set(listed) == expected, (set(listed) ^ expected)


def test_h_and_long_help_are_the_same(qai: Callable[..., Run]) -> None:
    assert qai("-h").out == qai("--help").out


def test_help_documents_the_two_global_options(qai: Callable[..., Run]) -> None:
    out = qai("--help").out
    assert "--repo-root <path>" in out and "--config <server.toml>" in out


def test_no_command_is_a_usage_error(qai: Callable[..., Run]) -> None:
    r = qai()
    assert r.rc == 2 and "the following arguments are required: <command>" in r.err
    assert r.out == ""


def test_unknown_command_lists_the_valid_ones_on_stderr(qai: Callable[..., Run]) -> None:
    r = qai("nosuchcmd")
    assert r.rc == 2 and "invalid choice: 'nosuchcmd'" in r.err and "'config'" in r.err and "'service-release'" in r.err
    assert r.out == ""


def test_install_is_not_a_command_but_install_qairt_is(qai: Callable[..., Run]) -> None:
    """``install`` is only the name of the module that registers ``install-qairt`` / ``install-pack-deps`` / ``uninstall``."""
    assert qai("install", "--help").rc == 2


# --------------------------------------------------------------------------
# per-group help
# --------------------------------------------------------------------------


@pytest.mark.parametrize("group", SAFE_GROUPS)
def test_every_group_has_a_working_help(qai: Callable[..., Run], group: str) -> None:
    r = qai(group, "--help")
    assert r.rc == 0, r.err[-500:]
    assert r.out.startswith(f"usage: qai {group}"), r.out[:120]


@pytest.mark.parametrize("group", ["app", "build"])
def test_app_and_build_help(qai: Callable[..., Run], group: str) -> None:
    r = qai(group, "--help")
    assert r.rc == 0 and r.out.startswith(f"usage: qai {group} [-h]")


@pytest.mark.parametrize("cmd", DELEGATING_BUT_ARGPARSE_FIRST)
def test_delegating_commands_whose_script_parses_arguments_first_print_their_own_help(qai: Callable[..., Run], cmd: str) -> None:
    r = qai(cmd, "--help")
    assert r.rc == 0 and re.match(r"usage: (install_qairt|uninstall|python -m apps\.api) \[-h\]", r.out)


def test_compile_factory_help_crashes_with_an_import_error(qai: Callable[..., Run]) -> None:
    """KNOWN GAP (current behaviour, pinned) - INST-ISS-02: ``qai compile-factory`` delegates to
    ``scripts.build.compile_factory``, which does not exist (``scripts/build`` only has ``model_builder_cli.py``). The
    command is advertised in ``qai --help`` but cannot run. If the script is restored or the command removed, update this."""
    r = qai("compile-factory", "--help")
    assert r.rc == 1
    assert "ImportError: cannot import name 'compile_factory' from 'scripts.build'" in r.err
    assert "Traceback" in r.err, "the error is a raw traceback, not a clean message"


@pytest.mark.parametrize(
    ("args", "needle"),
    [
        (("config", "provider", "--help"), "list        list configured providers (api_key masked)"),
        (("config", "--help"), "provider    cloud-model providers (list/add/edit/remove/test)"),
        (("service", "status", "--help"), "usage: qai service status [-h] [instance_id]"),
    ],
)
def test_nested_help_pages(qai: Callable[..., Run], args: tuple[str, ...], needle: str) -> None:
    r = qai(*args)
    assert r.rc == 0 and needle in r.out


def test_provider_help_promises_the_key_is_never_stored_in_plaintext(qai: Callable[..., Run]) -> None:
    out = qai("config", "provider", "--help").out
    assert "stored in the OS-keyring-backed SecretStore" in out and "never written to disk in" in out


# --------------------------------------------------------------------------
# qai config provider list
# --------------------------------------------------------------------------


def test_provider_list_on_a_fresh_data_dir_is_an_empty_json_document(qai: Callable[..., Run]) -> None:
    r = qai("config", "provider", "list")
    assert r.rc == 0, r.err[-800:]
    assert json.loads(r.out) == {"providers": []}
    assert r.out == '{\n  "providers": []\n}\n', "two-space indent, trailing newline"


def test_provider_list_logs_go_to_stderr_so_stdout_is_pure_json(qai: Callable[..., Run]) -> None:
    r = qai("config", "provider", "list")
    json.loads(r.out)
    assert "[INFO" in r.err and "[INFO" not in r.out


def test_provider_list_is_read_only_and_repeatable(qai: Callable[..., Run], data_dir: Path) -> None:
    a = qai("config", "provider", "list")
    snap = {str(p.relative_to(data_dir)): p.stat().st_size for p in data_dir.rglob("*") if p.is_file()}
    b = qai("config", "provider", "list")
    assert a.out == b.out
    assert {str(p.relative_to(data_dir)): p.stat().st_size for p in data_dir.rglob("*") if p.is_file()}.keys() == snap.keys()


def test_provider_list_uses_the_isolated_data_dir(qai: Callable[..., Run], data_dir: Path) -> None:
    qai("config", "provider", "list")
    assert (data_dir / "db" / "qai.db").is_file(), "the CLI created its database in QAI_DATA__DATA_DIR"


def _seed_provider(data_dir: Path, provider_id: str, config: dict) -> None:
    """Write a provider row exactly where ``PUT /api/model-catalog/providers/{id}`` does (``kv_user_prefs``), WITHOUT a key."""
    import sqlite3

    con = sqlite3.connect(str(data_dir / "db" / "qai.db"), timeout=15)
    try:
        con.execute("INSERT OR REPLACE INTO kv_user_prefs(key, value_json, updated_at) VALUES (?, ?, ?)",
                    (f"model_catalog.provider.{provider_id}", json.dumps(config), "2026-01-01T00:00:00+00:00"))
        con.commit()
    finally:
        con.close()


def test_provider_list_shows_a_stored_provider_with_derived_fields(qai: Callable[..., Run], data_dir: Path) -> None:
    qai("config", "provider", "list")  # make sure the schema exists
    cfg = {"base_url": "https://example.invalid/v1", "pinned": False, "models": [{"model_id": "e2e-cli-prov::m1", "name": "M One", "context_length": 8192}]}
    _seed_provider(data_dir, "e2e-cli-prov", cfg)
    try:
        r = qai("config", "provider", "list")
        assert r.rc == 0, r.err[-600:]
        rows = {p["provider_id"]: p for p in json.loads(r.out)["providers"]}
        c = rows["e2e-cli-prov"]["config"]
        assert {k: c[k] for k in cfg} == cfg
        assert c["auth_mode"] == "api_key" and c["has_api_key"] is False
        assert "api_key" not in c, "a provider stored without a key has no api_key field at all"
    finally:
        import sqlite3

        con = sqlite3.connect(str(data_dir / "db" / "qai.db"), timeout=15)
        con.execute("DELETE FROM kv_user_prefs WHERE key = ?", ("model_catalog.provider.e2e-cli-prov",))
        con.commit()
        con.close()


def test_a_plaintext_key_that_somehow_reached_the_store_is_masked_in_the_listing(qai: Callable[..., Run], data_dir: Path) -> None:
    """Defence in depth: if a hand-edited row carries ``api_key``, the CLI must print ``****``, never the secret."""
    import sqlite3

    secret = "E2E-CLI-PLAINTEXT-SENTINEL-5521"
    qai("config", "provider", "list")
    _seed_provider(data_dir, "e2e-cli-leak", {"base_url": "https://example.invalid/v1", "api_key": secret})
    try:
        r = qai("config", "provider", "list")
        assert r.rc == 0 and secret not in r.out and secret not in r.err
        (c,) = [p["config"] for p in json.loads(r.out)["providers"] if p["provider_id"] == "e2e-cli-leak"]
        assert c["api_key"] == "****"
    finally:
        con = sqlite3.connect(str(data_dir / "db" / "qai.db"), timeout=15)
        con.execute("DELETE FROM kv_user_prefs WHERE key = ?", ("model_catalog.provider.e2e-cli-leak",))
        con.commit()
        con.close()


def test_provider_list_orders_providers_by_id(qai: Callable[..., Run], data_dir: Path) -> None:
    import sqlite3

    qai("config", "provider", "list")
    ids = ["e2e-cli-b", "e2e-cli-a", "e2e-cli-c"]
    for pid in ids:
        _seed_provider(data_dir, pid, {"base_url": "https://example.invalid/v1"})
    try:
        got = [p["provider_id"] for p in json.loads(qai("config", "provider", "list").out)["providers"] if p["provider_id"].startswith("e2e-cli-")]
        assert got == sorted(ids)
    finally:
        con = sqlite3.connect(str(data_dir / "db" / "qai.db"), timeout=15)
        for pid in ids:
            con.execute("DELETE FROM kv_user_prefs WHERE key = ?", (f"model_catalog.provider.{pid}",))
        con.commit()
        con.close()

# --------------------------------------------------------------------------
# other read-only commands that are safe to run
# --------------------------------------------------------------------------


@pytest.mark.parametrize("args", [("pack", "list"), ("skill", "list")])
def test_list_commands_print_json_and_exit_0(qai: Callable[..., Run], args: tuple[str, ...]) -> None:
    r = qai(*args)
    assert r.rc == 0, r.err[-500:]
    json.loads(r.out)


def test_config_get_of_an_absent_key_is_an_empty_object(qai: Callable[..., Run]) -> None:
    r = qai("config", "get", "e2e-no-such-key")
    assert r.rc == 0 and json.loads(r.out) == {}


@pytest.mark.parametrize("args", [("pack", "export"), ("pack", "validate"), ("build", "--mode", "bogus")])
def test_missing_or_bad_arguments_exit_2(qai: Callable[..., Run], args: tuple[str, ...]) -> None:
    assert qai(*args).rc == 2


# --------------------------------------------------------------------------
# qai.bat (the Windows launcher)
# --------------------------------------------------------------------------


@pytest.fixture
def bat_env() -> dict[str, str]:
    env = dict(os.environ)
    env["QAI_DATA__DATA_DIR"] = tempfile.mkdtemp(prefix="qai-e2e-cli-")
    return env


@pytest.mark.skipif(os.name != "nt", reason="qai.bat is a Windows launcher")
def test_qai_bat_help_goes_through_the_launcher(bat_env: dict[str, str]) -> None:
    """Needs the real runtime venv under ``<repo>\\envs``; the launcher says so (exit 1) when it is absent."""
    venv_ok = any((REPO_ROOT / "envs" / n / "Scripts" / "python.exe").is_file()
                  for n in (".venv_arm64_313", ".venv_x64_313"))
    try:
        p = subprocess.run(["cmd", "/c", str(REPO_ROOT / "qai.bat"), "--help"], cwd=str(REPO_ROOT), env=bat_env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=120)
    finally:
        shutil.rmtree(bat_env["QAI_DATA__DATA_DIR"], ignore_errors=True)
    if not venv_ok:
        assert p.returncode == 1 and "Python runtime is not installed yet" in p.stdout
        pytest.skip("runtime venv is not installed on this machine")
    assert p.returncode == 0, p.stdout[-400:] + p.stderr[-400:]
    assert f"[INFO] CLI source root: {REPO_ROOT}" in p.stdout
    assert re.search(r"\[INFO\] CLI qai source: .*src\\qai\\__init__\.py", p.stdout), "the launcher loads THIS checkout's qai package"
    assert "usage: qai [-h]" in p.stdout and "QAIModelBuilder unified CLI" in p.stdout


@pytest.mark.skipif(os.name != "nt", reason="qai.bat is a Windows launcher")
def test_qai_bat_forwards_exit_codes(bat_env: dict[str, str]) -> None:
    venv_ok = any((REPO_ROOT / "envs" / n / "Scripts" / "python.exe").is_file()
                  for n in (".venv_arm64_313", ".venv_x64_313"))
    if not venv_ok:
        pytest.skip("runtime venv is not installed on this machine")
    try:
        p = subprocess.run(["cmd", "/c", str(REPO_ROOT / "qai.bat"), "nosuchcmd"], cwd=str(REPO_ROOT), env=bat_env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", stdin=subprocess.DEVNULL, timeout=120)
    finally:
        shutil.rmtree(bat_env["QAI_DATA__DATA_DIR"], ignore_errors=True)
    assert p.returncode == 2, "the launcher must not swallow the CLI's exit code"


# --------------------------------------------------------------------------
# nothing leaked
# --------------------------------------------------------------------------


def test_no_cloud_credential_was_created_by_this_module(qai: Callable[..., Run]) -> None:
    """Compare against the state at module start: this module never stores a key, so none may appear."""
    try:
        import keyring
    except ImportError:  # pragma: no cover
        pytest.skip("keyring is not installed in the test venv")
    now = {}
    for svc in ("qai.cloud.cloud_model", "qai.cloud.enterprise_cloud_model"):
        try:
            now[svc] = keyring.get_password(svc, "api_key")
        except Exception:  # noqa: BLE001
            now[svc] = None
    assert now == _KEYRING_AT_START, "a cloud api_key appeared or changed in the OS keyring during this module"
