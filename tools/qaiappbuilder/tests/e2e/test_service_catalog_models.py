# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - Model download install/delete ``/api/service-catalog/*`` (disk side only, no network).

Endpoints (``interfaces/http/routes/service_catalog.py``): ``POST /install``,
``DELETE /install/{model_id:path}``, ``GET /local-status``. ``GET /`` (remote catalog) and
``POST /download`` need the network / start aria2c and are NOT exercised here (see
``test_service_versions.py`` for the same reasoning).

Isolation (verified): everything lives under the throw-away data dir (``models/``, ``downloads/``);
an autouse fixture refuses to run unless the data dir is a ``qai-e2e-*`` temp dir and empties both
folders before and after each test. Nothing is started (no aria2c, no GenieAPIService); the module
asserts that no ``aria2c`` process or port-6800 listener appeared.

Observed behaviour worth pinning (see ``KNOWN-ISSUES.md``):

* SVC-ISS-08: ``DELETE /install/{id}`` also deletes every directory whose name merely STARTS WITH
  ``{id}`` + ``-``/``_``/``.`` (``llama`` removes ``llama-3-8b-instruct``).
* SVC-ISS-09: with the default ``delete_zip=true`` the same prefix rule is applied to ``downloads/``,
  which also holds the SERVICE version packages (``downloads/2.48.40``): deleting model ``2`` removes them.
* SVC-ISS-10: the reinstall backup of ``qwen3.5`` is named ``qwen3.bak`` (``with_suffix``), so an
  unrelated ``models/qwen3.bak`` is deleted by a reinstall.

Verified NOT to be a problem (kept as regression tests): no path traversal through the delete id or the
``install_dir`` hint, and zip entries containing ``../`` cannot escape the install directory.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import Backend

pytestmark = pytest.mark.e2e

M = "/api/service-catalog"


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


def _aria2c_running() -> bool:
    out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq aria2c.exe"], capture_output=True, text=True).stdout
    if "aria2c.exe" in out.lower():
        return True
    with socket.socket() as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", 6800)) == 0


@pytest.fixture(scope="module", autouse=True)
def no_download_machinery_started() -> Iterator[None]:
    was_running = _aria2c_running()
    yield
    if not was_running:
        assert not _aria2c_running(), "aria2c was started during this module"


@pytest.fixture(autouse=True)
def clean_state(backend: Backend) -> Iterator[None]:
    root = backend.data_dir
    assert "qai-e2e-" in str(root), f"refusing to touch a non-throw-away data dir: {root}"

    def _wipe() -> None:
        for sub in ("models", "downloads"):
            shutil.rmtree(root / sub, ignore_errors=True)
        shutil.rmtree(root / "sentinel", ignore_errors=True)
        (root / "stray.zip").unlink(missing_ok=True)

    _wipe()
    yield
    _wipe()


def _models(backend: Backend) -> list[str]:
    d = backend.data_dir / "models"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _downloads(backend: Backend) -> list[str]:
    d = backend.data_dir / "downloads"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def _mkdir(root: Path, name: str, files: tuple[str, ...] = ("a.txt",)) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    for f in files:
        (d / f).write_text("x")
    return d


def _mkzip(path: Path, files: dict[str, str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return path


def _delete(api: httpx.Client, model_id: str, **params: object) -> httpx.Response:
    return api.delete(f"{M}/install/{model_id}", params=params or None)


@pytest.fixture
def models(backend: Backend) -> Path:
    return backend.data_dir / "models"


@pytest.fixture
def downloads(backend: Backend) -> Path:
    return backend.data_dir / "downloads"


# --------------------------------------------------------------------------
# delete: exact match, validation, 404
# --------------------------------------------------------------------------


def test_delete_model_removes_the_install_and_reports_it(api: httpx.Client, backend: Backend, models: Path) -> None:
    d = _mkdir(models, "qwen3-8b", ("a.txt", "weights.bin"))
    keep = _mkdir(models, "other")
    r = _delete(api, "qwen3-8b")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "deleted_install_dirs": [str(d)], "deleted_zip_files": [], "model_id": "qwen3-8b"}
    assert _models(backend) == ["other"] and keep.exists()


def test_delete_model_also_removes_its_downloaded_zip_and_empty_folder(api: httpx.Client, backend: Backend, models: Path, downloads: Path) -> None:
    _mkdir(models, "m1")
    z = _mkzip(downloads / "m1" / "m1.zip", {"a": "b"})
    (downloads / "m1" / "m1.zip.aria2").write_text("partial")
    r = _delete(api, "m1")
    assert r.status_code == 200 and r.json()["deleted_zip_files"] == [str(z)]
    assert _downloads(backend) == [], "the .aria2 control file goes too, then the empty folder"


def test_delete_model_keeps_a_download_folder_with_other_files(api: httpx.Client, backend: Backend, downloads: Path) -> None:
    _mkzip(downloads / "m1" / "m1.zip", {"a": "b"})
    (downloads / "m1" / "notes.txt").write_text("keep")
    assert _delete(api, "m1").status_code == 200
    assert (downloads / "m1" / "notes.txt").exists()


def test_delete_model_zip_only_is_a_success(api: httpx.Client, backend: Backend, downloads: Path) -> None:
    _mkzip(downloads / "ghost" / "z.zip", {"a": "b"})
    r = _delete(api, "ghost")
    assert r.status_code == 200 and r.json()["deleted_install_dirs"] == [] and len(r.json()["deleted_zip_files"]) == 1


def test_delete_model_delete_zip_false_leaves_downloads_alone(api: httpx.Client, backend: Backend, models: Path, downloads: Path) -> None:
    _mkdir(models, "m1")
    _mkzip(downloads / "m1" / "m1.zip", {"a": "b"})
    r = _delete(api, "m1", delete_zip="false")
    assert r.status_code == 200 and r.json()["deleted_zip_files"] == []
    assert _downloads(backend) == ["m1"] and (downloads / "m1" / "m1.zip").exists()


def test_delete_model_not_found(api: httpx.Client) -> None:
    r = _delete(api, "ghost")
    assert r.status_code == 404 and r.json()["code"] == "service_release.download_not_found"
    assert "no model artifacts found" in r.json()["message"]


@pytest.mark.parametrize("seg", ["%20", "%20%20"])
def test_delete_model_blank_id_is_400(api: httpx.Client, seg: str) -> None:
    r = _delete(api, seg)
    assert r.status_code == 400 and r.json()["code"] == "service_release.invalid_download_request"
    assert "model_id" in r.json()["details"]["field_errors"]


def test_delete_model_id_is_case_sensitive_and_exact_for_unicode(api: httpx.Client, backend: Backend, models: Path) -> None:
    """Names are compared as strings, so ``A`` does not match ``a`` (even on a case-insensitive filesystem)."""
    _mkdir(models, "a")
    _mkdir(models, "模型")
    assert _delete(api, "A").status_code == 404
    assert _models(backend) == ["a", "模型"]
    assert _delete(api, "%E6%A8%A1%E5%9E%8B").status_code == 200
    assert _models(backend) == ["a"]


def test_delete_model_bad_query_value(api: httpx.Client) -> None:
    r = _delete(api, "m1", delete_zip="maybe")
    assert r.status_code == 400 and "query.delete_zip" in r.json()["details"]["field_errors"]


# --------------------------------------------------------------------------
# SVC-ISS-08: prefix matching in models/
# --------------------------------------------------------------------------


def test_delete_model_deletes_directories_that_merely_start_with_the_id(api: httpx.Client, backend: Backend, models: Path) -> None:
    """SVC-ISS-08 (fixed): a prefix-matched directory (``qwen3-8b`` for id ``qwen3``) is only deleted when its
    ``.qai-install.json`` marker proves it was installed under that model_id. These fixtures carry no marker,
    so only the exact-name directory goes and every unrelated model survives."""
    for n in ("qwen3", "qwen3-8b", "qwen3-vl-8b", "qwen3_x", "qwen3.y", "qwen30", "qwen", "other", "qwen3-8b.bak"):
        _mkdir(models, n)
    (models / "qwen3-file").write_text("a file, not a directory")
    r = _delete(api, "qwen3")
    assert r.status_code == 200
    assert sorted(Path(p).name for p in r.json()["deleted_install_dirs"]) == ["qwen3"]
    assert _models(backend) == ["other", "qwen", "qwen3-8b", "qwen3-8b.bak", "qwen3-file", "qwen3-vl-8b", "qwen3.y", "qwen30", "qwen3_x"], \
        "unconfirmed prefix matches, '.bak' directories, plain files and longer names are left alone"


def test_delete_a_model_that_is_a_prefix_of_other_models_keeps_the_others(api: httpx.Client, backend: Backend, models: Path) -> None:
    """SVC-ISS-08 (fixed): realistic names; only the exact-name directory is removed."""
    for n in ("llama", "llama-2-7b", "llama-3-8b", "llama-3-8b-instruct"):
        _mkdir(models, n)
    assert _delete(api, "llama").status_code == 200
    assert _models(backend) == ["llama-2-7b", "llama-3-8b", "llama-3-8b-instruct"]


def test_deleting_a_base_model_keeps_its_unconfirmed_instruct_variant(api: httpx.Client, backend: Backend, models: Path) -> None:
    """SVC-ISS-08 (fixed): the ``-instruct`` directory has no marker tying it to ``llama-3-8b``, so it survives."""
    _mkdir(models, "llama-3-8b")
    _mkdir(models, "llama-3-8b-instruct")
    _mkdir(models, "llama-3-70b")
    assert _delete(api, "llama-3-8b").status_code == 200
    assert _models(backend) == ["llama-3-70b", "llama-3-8b-instruct"]


def test_delete_model_without_a_separator_does_not_match_longer_names(api: httpx.Client, backend: Backend, models: Path) -> None:
    _mkdir(models, "qwen30")
    _mkdir(models, "qwen3x")
    assert _delete(api, "qwen3").status_code == 404
    assert _models(backend) == ["qwen30", "qwen3x"]


# --------------------------------------------------------------------------
# SVC-ISS-09: delete_zip reaches the service version packages
# --------------------------------------------------------------------------


def test_delete_model_with_zip_flag_removes_service_version_packages(api: httpx.Client, backend: Backend, downloads: Path) -> None:
    """KNOWN GAP (current behaviour, pinned) - SVC-ISS-09: ``downloads/`` is shared with the service
    versions, so a model id that is a prefix of a version number deletes the version's package."""
    _mkzip(downloads / "2.48.40" / "Genie_v81.zip", {"a": "b"})
    _mkzip(downloads / "2.3.7" / "Genie_v73.zip", {"a": "b"})
    _mkzip(downloads / "qwen3-8b" / "m.zip", {"a": "b"})
    _mkzip(downloads / "7" / "x.zip", {"a": "b"})
    r = _delete(api, "2")
    assert r.status_code == 200 and r.json()["deleted_install_dirs"] == []
    assert sorted(Path(p).parent.name for p in r.json()["deleted_zip_files"]) == ["2.3.7", "2.48.40"]
    assert _downloads(backend) == ["7", "qwen3-8b"]


def test_delete_model_with_a_dotted_prefix_removes_a_service_package(api: httpx.Client, backend: Backend, downloads: Path) -> None:
    """KNOWN GAP (current behaviour, pinned) - SVC-ISS-09."""
    _mkzip(downloads / "2.48.40" / "Genie_v81.zip", {"a": "b"})
    assert _delete(api, "2.48").status_code == 200
    assert _downloads(backend) == []


def test_the_same_request_without_the_zip_flag_leaves_service_packages_alone(api: httpx.Client, backend: Backend, downloads: Path) -> None:
    _mkzip(downloads / "2.48.40" / "Genie_v81.zip", {"a": "b"})
    assert _delete(api, "2", delete_zip="false").status_code == 404
    assert _downloads(backend) == ["2.48.40"]


# --------------------------------------------------------------------------
# no path traversal (verified, regression tests)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "seg",
    [
        "%2e%2e", "%2E%2E", "..%5Csentinel", "a%5C..%5Csentinel", "..%2Fsentinel", "%2e%2e/sentinel",
        "%2F", "%5C", "C%3A%5CWindows",
    ],
)
def test_delete_model_id_cannot_leave_the_models_and_downloads_folders(api: httpx.Client, backend: Backend, models: Path, seg: str) -> None:
    _mkdir(models, "keep")
    sentinel = _mkdir(backend.data_dir, "sentinel", ("f.txt",))
    stray = backend.data_dir / "stray.zip"
    stray.write_text("x")
    r = _delete(api, seg)
    assert r.status_code in (404, 405), r.text
    assert (sentinel / "f.txt").exists() and stray.exists() and _models(backend) == ["keep"]


@pytest.mark.parametrize("seg", ["..", ".", "../sentinel", "sentinel/..", "keep/.."])
def test_delete_model_dot_segments_are_stopped_before_the_handler(api: httpx.Client, backend: Backend, models: Path, seg: str) -> None:
    _mkdir(models, "keep")
    r = _delete(api, seg)
    assert r.status_code in (404, 405)
    assert _models(backend) == ["keep"]


def test_delete_model_absolute_path_to_a_real_directory_deletes_nothing(api: httpx.Client, tmp_path: Path) -> None:
    outside = _mkdir(tmp_path, "outside")
    seg = str(outside).replace("\\", "%5C").replace(":", "%3A")
    r = _delete(api, seg)
    assert r.status_code == 404 and outside.exists() and (outside / "a.txt").exists()


# --------------------------------------------------------------------------
# install
# --------------------------------------------------------------------------


def _install(api: httpx.Client, zip_path: Path, **over: object) -> httpx.Response:
    body = {"save_path": str(zip_path), "model_id": "m", **over}
    return api.post(f"{M}/install", json=body)


@pytest.mark.parametrize("save_path", ["", "   "])
def test_install_requires_a_save_path(api: httpx.Client, save_path: str) -> None:
    r = api.post(f"{M}/install", json={"save_path": save_path})
    assert r.status_code == 400 and "save_path" in r.json()["details"]["field_errors"]


def test_install_rejects_a_missing_path_without_side_effects(api: httpx.Client, backend: Backend, tmp_path: Path) -> None:
    r = _install(api, tmp_path / "nope.zip")
    assert r.status_code == 422 and r.json()["code"] == "service_release.install_failed"
    assert "invalid save_path" in r.json()["message"]
    assert _models(backend) == []


def test_install_moves_a_raw_non_zip_file_into_the_model_dir(api: httpx.Client, models: Path, tmp_path: Path) -> None:
    """A non-.zip source is no longer rejected: GGUF models install as a raw file moved into the model dir."""
    raw = tmp_path / "x.gguf"
    raw.write_text("x")
    r = _install(api, raw, install_dir="raw")
    assert r.status_code == 200, r.text
    assert (models / "raw" / "x.gguf").read_text() == "x" and not raw.exists()


def test_install_corrupt_zip_is_422_and_leaves_nothing(api: httpx.Client, backend: Backend, tmp_path: Path) -> None:
    bad = tmp_path / "bad.zip"
    bad.write_bytes(b"not a zip")
    r = _install(api, bad)
    assert r.status_code == 422 and "corrupt archive" in r.json()["message"]
    assert bad.exists() and "bad" not in _models(backend)


def test_install_extracts_flattens_and_deletes_the_source_zip(api: httpx.Client, backend: Backend, models: Path, tmp_path: Path) -> None:
    z = _mkzip(tmp_path / "dl" / "qwen3-8b.zip", {"Top/config.json": "{}", "Top/weights/w.bin": "w"})
    r = _install(api, z, model_id="qwen3-8b", install_dir="qwen3-8b")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["model_id"] == "qwen3-8b" and body["zip_deleted"] is True
    assert body["install_path"] == str(models / "qwen3-8b")
    assert sorted(p.name for p in (models / "qwen3-8b").iterdir()) == [".qai-install.json", "config.json", "weights"]
    assert not z.exists() and not z.parent.exists(), "the empty download folder is removed too"


def test_install_keeps_multiple_top_level_entries(api: httpx.Client, models: Path, tmp_path: Path) -> None:
    z = _mkzip(tmp_path / "dl" / "m.zip", {"a/x.txt": "1", "b.txt": "2"})
    assert _install(api, z, install_dir="m").status_code == 200
    assert sorted(p.name for p in (models / "m").iterdir()) == [".qai-install.json", "a", "b.txt"]


def test_install_defaults_the_directory_name_to_the_zip_stem(api: httpx.Client, models: Path, tmp_path: Path) -> None:
    z = _mkzip(tmp_path / "dl" / "my model.zip", {"f.txt": "x"})
    r = _install(api, z, model_id="")
    assert r.status_code == 200 and r.json()["install_path"] == str(models / "my-model")
    assert r.json()["model_id"] == "my-model", "an empty model_id falls back to the directory name"


def test_install_always_writes_the_model_id_marker_and_the_variant_only_when_given(api: httpx.Client, models: Path, tmp_path: Path) -> None:
    z1 = _mkzip(tmp_path / "d1" / "a.zip", {"f.txt": "x"})
    z2 = _mkzip(tmp_path / "d2" / "b.zip", {"f.txt": "x"})
    _install(api, z1, install_dir="with-variant", variant_id="x2-elite")
    _install(api, z2, install_dir="no-variant")
    assert json.loads((models / "with-variant" / ".qai-install.json").read_text()) == {"model_id": "m", "variant_id": "x2-elite"}
    assert json.loads((models / "no-variant" / ".qai-install.json").read_text()) == {"model_id": "m"}, "model_id only; no variant_id"


@pytest.mark.parametrize(
    ("hint", "expected"),
    [
        ("ok name", "ok-name"),
        ("../evil", "evil"),
        ("..\\evil", "evil"),
        ("C:\\evil", "evil"),
        ("a/b/c", "c"),
        ("..", "STEM"),
        ("...", "STEM"),
        ("a/..", "STEM"),
        ("evil/..", "STEM"),
        (" ", "STEM"),
        ("tail. ", "tail"),
        ('we<ir>d:"n|a?m*e', "weirdname"),
    ],
)
def test_install_dir_hint_is_reduced_to_one_safe_segment(
    api: httpx.Client, backend: Backend, models: Path, tmp_path: Path, hint: str, expected: str
) -> None:
    z = _mkzip(tmp_path / "dl" / "mstem.zip", {"f.txt": "x"})
    r = _install(api, z, install_dir=hint)
    assert r.status_code == 200, r.text
    want = "mstem" if expected == "STEM" else expected
    assert r.json()["install_path"] == str(models / want)
    assert _models(backend) == [want]
    assert not list(tmp_path.rglob("evil")), "nothing was created outside models/"


def test_install_zip_entries_with_dot_dot_cannot_escape(api: httpx.Client, backend: Backend, models: Path, tmp_path: Path) -> None:
    z = _mkzip(tmp_path / "dl" / "slip.zip", {"../../evil.txt": "pwn", "ok.txt": "ok"})
    r = _install(api, z, install_dir="slip")
    assert r.status_code == 200, r.text
    assert not list(tmp_path.rglob("evil.txt")) and not (backend.data_dir / "evil.txt").exists()
    assert not (backend.data_dir.parent / "evil.txt").exists()
    assert (models / "slip" / "ok.txt").read_text() == "ok"


def test_install_is_idempotent_once_the_source_zip_is_gone(api: httpx.Client, models: Path, tmp_path: Path) -> None:
    """A successful install deletes the zip, so a repeat request reports the existing install instead of failing."""
    z = _mkzip(tmp_path / "dl" / "m.zip", {"f.txt": "x"})
    assert _install(api, z, install_dir="m").status_code == 200
    again = _install(api, z, install_dir="m")
    assert again.status_code == 200 and again.json()["install_path"] == str(models / "m") and again.json()["zip_deleted"] is True


def test_repeat_install_finds_the_directory_by_the_install_dir_hint_alone(api: httpx.Client, models: Path, tmp_path: Path) -> None:
    """The hint and the model id are looked up independently: here the id matches nothing on disk."""
    _mkdir(models, "hint-dir")
    r = _install(api, tmp_path / "gone.zip", model_id="unrelated-id", install_dir="hint-dir")
    assert r.status_code == 200 and r.json()["install_path"] == str(models / "hint-dir")
    assert r.json()["model_id"] == "unrelated-id", "the caller's id is echoed back"


def test_repeat_install_without_an_id_reports_the_directory_name_as_the_id(api: httpx.Client, models: Path, tmp_path: Path) -> None:
    _mkdir(models, "hint-dir")
    r = _install(api, tmp_path / "gone.zip", model_id="", install_dir="hint-dir")
    assert r.status_code == 200 and r.json()["model_id"] == "hint-dir"


def test_repeat_install_with_neither_a_hint_nor_an_id_cannot_find_anything(api: httpx.Client, models: Path, tmp_path: Path) -> None:
    _mkdir(models, "something")
    assert _install(api, tmp_path / "gone.zip", model_id="", install_dir="").status_code == 422


def test_install_missing_zip_with_an_empty_existing_directory_is_still_a_failure(api: httpx.Client, models: Path, tmp_path: Path) -> None:
    (models / "m").mkdir(parents=True)
    assert _install(api, tmp_path / "gone.zip", install_dir="m").status_code == 422


def test_install_missing_zip_finds_a_legacy_prefixed_directory(api: httpx.Client, models: Path, tmp_path: Path) -> None:
    """The idempotence check uses the same lenient prefix rule as delete (legacy per-variant layout)."""
    _mkdir(models, "qwen3-8b-8480-qnn2.44")
    r = _install(api, tmp_path / "gone.zip", model_id="qwen3-8b")
    assert r.status_code == 200 and r.json()["install_path"].endswith("qwen3-8b-8480-qnn2.44")


def test_reinstall_replaces_the_content_and_leaves_no_backup(api: httpx.Client, backend: Backend, models: Path, tmp_path: Path) -> None:
    old = _mkzip(tmp_path / "d1" / "m.zip", {"f.txt": "OLD", "old_only.txt": "o"})
    new = _mkzip(tmp_path / "d2" / "m.zip", {"f.txt": "NEW", "new_only.txt": "n"})
    for z in (old, new):
        assert _install(api, z, install_dir="m").status_code == 200
    assert _models(backend) == ["m"]
    assert (models / "m" / "f.txt").read_text() == "NEW" and not (models / "m" / "old_only.txt").exists()


def test_failed_reinstall_restores_the_previous_install(api: httpx.Client, backend: Backend, models: Path, tmp_path: Path) -> None:
    good = _mkzip(tmp_path / "d1" / "m.zip", {"f.txt": "GOOD"})
    assert _install(api, good, install_dir="m").status_code == 200
    bad = tmp_path / "d2" / "m.zip"
    bad.parent.mkdir()
    bad.write_bytes(b"corrupt")
    assert _install(api, bad, install_dir="m").status_code == 422
    assert _models(backend) == ["m"] and (models / "m" / "f.txt").read_text() == "GOOD"


# --------------------------------------------------------------------------
# SVC-ISS-10: backup directory name is truncated for dotted names
# --------------------------------------------------------------------------


def test_reinstalling_a_dotted_model_deletes_an_unrelated_directory_named_like_the_truncated_backup(
    api: httpx.Client, backend: Backend, models: Path, tmp_path: Path
) -> None:
    """KNOWN GAP (current behaviour, pinned) - SVC-ISS-10: ``Path.with_suffix('.bak')`` turns ``qwen3.5``
    into ``qwen3.bak``. The reinstall first removes any existing ``qwen3.bak`` - here the user's own
    directory with a file in it. (The service installer already fixed this exact bug; the model
    installer did not.) If this is fixed ``precious.txt`` survives - update this test then."""
    first = _mkzip(tmp_path / "d1" / "a.zip", {"f.txt": "v1"})
    assert _install(api, first, install_dir="qwen3.5").status_code == 200
    precious = _mkdir(models, "qwen3.bak", ("precious.txt",))
    second = _mkzip(tmp_path / "d2" / "b.zip", {"f.txt": "v2"})
    assert _install(api, second, install_dir="qwen3.5").status_code == 200
    assert not (precious / "precious.txt").exists(), "an unrelated directory was deleted"
    assert _models(backend) == ["qwen3.5"]


def test_reinstalling_a_dotless_model_does_not_touch_other_dot_bak_directories(
    api: httpx.Client, backend: Backend, models: Path, tmp_path: Path
) -> None:
    """Control for the test above: with no dot in the name the backup name is exact."""
    first = _mkzip(tmp_path / "d1" / "a.zip", {"f.txt": "v1"})
    assert _install(api, first, install_dir="qwen35").status_code == 200
    other = _mkdir(models, "qwen3.bak", ("precious.txt",))
    second = _mkzip(tmp_path / "d2" / "b.zip", {"f.txt": "v2"})
    assert _install(api, second, install_dir="qwen35").status_code == 200
    assert (other / "precious.txt").exists()


def test_a_leftover_backup_directory_is_not_reported_as_a_model(api: httpx.Client, models: Path) -> None:
    _mkdir(models, "leftover.bak", ("config.json",))
    r = api.get(f"{M}/local-status")
    assert r.status_code == 200 and "leftover.bak" not in (r.json().get("models") or {})
