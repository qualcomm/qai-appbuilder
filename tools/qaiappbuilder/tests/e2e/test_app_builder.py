# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 — App Builder: model gallery, manifests, deps, voice preference, cache (AB-01/02/09/10).

Endpoints (``interfaces/http/routes/app_builder/``): ``/api/app-builder/models``,
``/models/{id}/manifest|schema``, ``/taxonomy``, ``/deps-status``,
``/voice-preference``, ``/voice-input/preload``, ``/worker/status``, ``/cache``.

NOT covered here (deliberately):

* Running inference / runs / batch / apps (AB-04..08, AB-11..13) - needs the NPU
  and real weights (L3), or LLM.
* ``DELETE /models/{id}`` on a BUILT-IN model. The built-in protection (403) is
  the most important guard on this surface, but the weights live in the repo's
  ``models/`` directory (git-ignored, ~2 GB, 3 of 4 packs have no download URL),
  which the backend does NOT isolate. If the guard regressed, the test would
  delete real, unrecoverable files. Only 404 / 400 are tested.
* ``GET /deps-status/packs`` with the checker ENABLED: the first poll schedules a
  background ``pip install`` of any missing pack dependency into the backend's
  real venv. This module starts the backend with the checker disabled
  (``QAI_APP_BUILDER__DEP_CHECKER_ENABLED=false``) and tests that form only.

Isolation (verified): voice preference lives in the throw-away
``<data_dir>/db/qai.db`` (table ``app_builder_voice_pref``) and the blob cache
under ``<data_dir>/blobs/app_builder``; before/after the real ``models/`` tree
and the backend venv were unchanged.

Observed behaviour worth pinning (it differs from what one might assume):

* ``PUT /voice-preference`` does NOT check the model exists: a well-formed but
  unknown model id (or an unknown variant of a real model) is accepted with 200
  and later silently fails to warm up. Pinned as current behaviour.
* Voice-preference validation failures are **422** (domain error), while a
  malformed body is **400**; unknown model ids on the model routes are 404 with
  a bare ``{"detail": ...}`` rather than the project's error envelope.
* ``DELETE /cache`` removes files but leaves the (empty) sub-directories.
* A model's ``status`` depends on this machine's weights (e.g. real-esrgan is
  ``NotInstalled`` here), so tests only assert it is a known token.
"""

from __future__ import annotations

import shutil
import sqlite3
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import Backend, _start_backend

pytestmark = pytest.mark.e2e

A = "/api/app-builder"
VP = f"{A}/voice-preference"
BUILTIN = ("melotts-zh", "real-esrgan-x4plus", "whisper-base", "zipformer-zh")
# What each built-in pack takes in / gives out (product contract, see manifests).
IO_KINDS = {
    "melotts-zh": ("text", "audio"),
    "real-esrgan-x4plus": ("image", "image"),
    "whisper-base": ("audio", "json"),
    "zipformer-zh": ("audio", "json"),
}
MODEL_STATUSES = {"Ready", "NotInstalled", "Error"}
WORKER_STATES = {"idle", "loading", "ready", "busy", "shutting_down", "dead", "absent"}
LOADED_STATES = {"loading", "ready", "busy"}


@pytest.fixture(scope="module")
def backend() -> Iterator[Backend]:
    """Backend with the login gate off AND the dependency checker off (see module doc)."""
    mp = pytest.MonkeyPatch()
    mp.setenv("QAI_APP_BUILDER__DEP_CHECKER_ENABLED", "false")
    try:
        yield from _start_backend(auth_enabled=False)
    finally:
        mp.undo()


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


def _models(api: httpx.Client) -> list[dict]:
    r = api.get(f"{A}/models")
    assert r.status_code == 200, r.text
    return r.json()["items"]


# --------------------------------------------------------------------------
# AB-01 - model gallery
# --------------------------------------------------------------------------


def test_models_list_has_builtin_packs(api: httpx.Client) -> None:
    rows = _models(api)
    ids = [m["id"] for m in rows]
    assert len(ids) == len(set(ids)), f"duplicate model ids: {ids}"
    assert set(BUILTIN) <= set(ids)
    required = {
        "id", "title", "taxonomy", "enabled", "pinned", "user_imported",
        "status", "variant_status", "category", "icon", "auto_download",
    }
    for m in rows:
        assert required <= set(m), f"{m['id']} lacks {required - set(m)}"
        assert m["status"] in MODEL_STATUSES, m
        assert m["variant_status"], f"{m['id']} has no variants"
        for v in m["variant_status"]:
            assert v["id"] and v["status"] in MODEL_STATUSES, v
        if m["id"] in BUILTIN:
            assert m["user_imported"] is False, "built-ins must never look user-imported"


@pytest.mark.parametrize("model_id", BUILTIN)
def test_model_detail_matches_list_row(api: httpx.Client, model_id: str) -> None:
    row = next(m for m in _models(api) if m["id"] == model_id)
    r = api.get(f"{A}/models/{model_id}")
    assert r.status_code == 200
    assert r.json() == row


@pytest.fixture
def disabled_model(backend: Backend) -> Iterator[str]:
    """Mark one built-in row disabled in the THROW-AWAY database only (never the real models/)."""
    target = "melotts-zh"
    con = sqlite3.connect(str(backend.data_dir / "db" / "qai.db"), timeout=15)
    try:
        cur = con.execute("UPDATE app_builder_model_definition SET enabled = 0 WHERE id = ?", (target,))
        con.commit()
        assert cur.rowcount == 1, "test setup: the row to disable was not found"
        yield target
    finally:
        con.execute("UPDATE app_builder_model_definition SET enabled = 1 WHERE id = ?", (target,))
        con.commit()
        con.close()


def test_include_disabled_filter(api: httpx.Client, disabled_model: str) -> None:
    # Default (and explicit true) keep disabled rows, flagged enabled=false.
    for params in ({}, {"include_disabled": "true"}):
        rows = api.get(f"{A}/models", params=params).json()["items"]
        flagged = {m["id"]: m["enabled"] for m in rows}
        assert flagged[disabled_model] is False
        assert all(flagged[i] for i in BUILTIN if i != disabled_model)
    # include_disabled=false drops exactly that row and nothing else.
    r = api.get(f"{A}/models", params={"include_disabled": "false"})
    assert r.status_code == 200
    ids = [m["id"] for m in r.json()["items"]]
    assert disabled_model not in ids
    assert set(ids) == set(BUILTIN) - {disabled_model}
    assert all(m["enabled"] for m in r.json()["items"])
    # The detail route still serves a disabled model (it is hidden, not deleted).
    assert api.get(f"{A}/models/{disabled_model}").status_code == 200


def test_include_disabled_rejects_non_boolean(api: httpx.Client) -> None:
    bad = api.get(f"{A}/models", params={"include_disabled": "maybe"})
    assert bad.status_code == 400
    assert bad.json()["code"] == "request.validation"


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("e2e-no-such-model", 404),
        ("ZIPFORMER-ZH", 404),  # ids are case-sensitive
        ("BAD%20ID!", 400),  # violates [A-Za-z0-9._-]{1,128}
        ("..%2f", 404),
    ],
)
@pytest.mark.parametrize("suffix", ["", "/manifest", "/schema"])
def test_unknown_or_invalid_model_id(api: httpx.Client, path: str, status: int, suffix: str) -> None:
    r = api.get(f"{A}/models/{path}{suffix}")
    assert r.status_code == status, r.text


@pytest.mark.parametrize("model_id", BUILTIN)
def test_manifest_is_consistent_with_gallery(api: httpx.Client, model_id: str) -> None:
    r = api.get(f"{A}/models/{model_id}/manifest")
    assert r.status_code == 200, r.text
    mf = r.json()
    assert mf["model_id"] == model_id
    assert isinstance(mf["schema_version"], int) and mf["schema_version"] >= 1
    assert mf["display_name"] and mf["version"]
    # Exactly one default variant, and the variants match what the gallery shows.
    assert sum(1 for v in mf["variants"] if v["default"]) == 1
    row = next(m for m in _models(api) if m["id"] == model_id)
    assert [v["id"] for v in mf["variants"]] == [v["id"] for v in row["variant_status"]]
    assert mf["display_name"] == row["title"]
    kin, kout = IO_KINDS[model_id]
    assert mf["input_schema"]["kind"] == kin
    assert mf["output_schema"]["kind"] == kout
    assert mf["runtime"]["backend"] and mf["runner"]["script"]


@pytest.mark.parametrize("model_id", BUILTIN)
def test_schema_matches_manifest(api: httpx.Client, model_id: str) -> None:
    mf = api.get(f"{A}/models/{model_id}/manifest").json()
    r = api.get(f"{A}/models/{model_id}/schema")
    assert r.status_code == 200, r.text
    sc = r.json()
    assert sc["model_id"] == model_id
    assert sc["input_schema"]["kind"] == mf["input_schema"]["kind"]
    assert sc["output_schema"]["kind"] == mf["output_schema"]["kind"]
    assert sc["title"]


def test_taxonomy_counts_agree_across_endpoints(api: httpx.Client) -> None:
    """``/taxonomy``, ``/taxonomy/tree`` and each model's own ``taxonomy`` must agree."""
    by_models = Counter(tuple(m["taxonomy"]) for m in _models(api) if m["taxonomy"])
    flat = api.get(f"{A}/taxonomy")
    assert flat.status_code == 200
    by_flat = {tuple(n["path"]): n["model_count"] for n in flat.json()}
    assert by_flat == dict(by_models)

    tree = api.get(f"{A}/taxonomy/tree")
    assert tree.status_code == 200
    body = tree.json()
    assert body["version"] and body["groups"]
    tasks = {(g["id"], t["id"]): t for g in body["groups"] for t in g["tasks"]}
    for t in tasks.values():
        assert t["label"] and isinstance(t["io"], list) and t["model_count"] >= 0
    # Every populated leaf in the flat list appears in the tree with the same count;
    # tree leaves that no model uses must report 0.
    for path, count in by_flat.items():
        assert tasks[tuple(path)]["model_count"] == count
    assert all(t["model_count"] == 0 for k, t in tasks.items() if k not in by_flat)


# --------------------------------------------------------------------------
# AB-02 - dependency status
# --------------------------------------------------------------------------


def test_deps_status_overview(api: httpx.Client) -> None:
    r = api.get(f"{A}/deps-status")
    assert r.status_code == 200
    d = r.json()
    for k in ("qairt_env_present", "pack_root_present", "shared_dir_present", "sticky_worker_alive"):
        assert isinstance(d[k], bool), k
    # The repo ships the factory pack tree + shared dir; this is not machine-dependent.
    assert d["pack_root_present"] is True and d["shared_dir_present"] is True
    assert d["registered_pack_count"] >= len(BUILTIN)


def test_deps_status_packs_when_checker_disabled(api: httpx.Client) -> None:
    """Checker off => an empty, not-checking snapshot (and, crucially, no pip run)."""
    for _ in range(2):
        r = api.get(f"{A}/deps-status/packs")
        assert r.status_code == 200
        assert r.json() == {"checking": False, "packs": {}}


# --------------------------------------------------------------------------
# AB-09 - voice preference
# --------------------------------------------------------------------------


@pytest.fixture
def restore_voice_pref(api: httpx.Client) -> Iterator[None]:
    original = api.get(VP).json()
    yield
    api.put(VP, json={k: original[k] for k in ("enabled", "preferred_model_id", "preferred_variant_id")})


def _db_voice_row(backend: Backend) -> tuple | None:
    con = sqlite3.connect(str(backend.data_dir / "db" / "qai.db"), timeout=15)
    try:
        return con.execute(
            "SELECT enabled, preferred_model_id, preferred_variant_id FROM app_builder_voice_pref"
        ).fetchone()
    finally:
        con.close()


def test_voice_preference_default_is_zipformer(api: httpx.Client) -> None:
    """A fresh install prefers the Chinese streaming ASR (AB-09 startup warm-up target)."""
    assert api.get(VP).json() == {
        "enabled": True,
        "preferred_model_id": "zipformer-zh",
        "preferred_variant_id": "int8",
    }


@pytest.mark.usefixtures("restore_voice_pref")
def test_voice_preference_round_trip_and_persists(api: httpx.Client, backend: Backend) -> None:
    want = {"enabled": False, "preferred_model_id": "zipformer-zh", "preferred_variant_id": "int8"}
    r = api.put(VP, json=want)
    assert r.status_code == 200 and r.json() == want
    assert api.get(VP).json() == want
    # It really hit the database, not just memory.
    assert _db_voice_row(backend) == (0, "zipformer-zh", "int8")


@pytest.mark.usefixtures("restore_voice_pref")
def test_voice_preference_put_replaces_the_whole_record(api: httpx.Client) -> None:
    api.put(VP, json={"enabled": True, "preferred_model_id": "zipformer-zh", "preferred_variant_id": "int8"})
    # Omitted optional fields become null (PUT, not PATCH): variant must not survive.
    r = api.put(VP, json={"enabled": True, "preferred_model_id": "melotts-zh"})
    assert r.json() == {"enabled": True, "preferred_model_id": "melotts-zh", "preferred_variant_id": None}
    r = api.put(VP, json={"enabled": False})
    assert r.json() == {"enabled": False, "preferred_model_id": None, "preferred_variant_id": None}
    assert api.get(VP).json() == r.json()


@pytest.mark.usefixtures("restore_voice_pref")
@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"enabled": True, "preferred_model_id": "BAD ID!"}, 422, "app_builder.voice_preference_invalid"),
        ({"enabled": True, "preferred_model_id": ""}, 422, "app_builder.voice_preference_invalid"),
        ({"enabled": True, "preferred_model_id": "a" * 129}, 422, "app_builder.voice_preference_invalid"),
        ({"enabled": True, "preferred_variant_id": "int8"}, 422, "app_builder.voice_preference_invalid"),
        ({"preferred_model_id": "zipformer-zh"}, 400, "request.validation"),
    ],
)
def test_voice_preference_rejects_invalid_and_keeps_old_value(
    api: httpx.Client, body: dict, status: int, code: str
) -> None:
    before = api.get(VP).json()
    r = api.put(VP, json=body)
    assert r.status_code == status, r.text
    assert r.json()["code"] == code
    assert api.get(VP).json() == before, "a rejected PUT must not change the stored value"


@pytest.mark.usefixtures("restore_voice_pref")
def test_voice_preference_id_length_boundary(api: httpx.Client) -> None:
    ok = api.put(VP, json={"enabled": True, "preferred_model_id": "a" * 128})
    assert ok.status_code == 200 and ok.json()["preferred_model_id"] == "a" * 128


def test_voice_preference_malformed_bodies(api: httpx.Client) -> None:
    assert api.put(VP).status_code == 400  # no body
    r = api.put(VP, content=b"x", headers={"content-type": "application/json"})
    assert r.status_code == 400 and r.json()["code"] == "request.validation"


@pytest.mark.usefixtures("restore_voice_pref")
def test_voice_preference_does_not_check_model_exists(api: httpx.Client) -> None:
    """KNOWN GAP (current behaviour, pinned): unknown model / variant are accepted.

    If this starts returning 4xx that is an improvement - update this test.
    """
    r = api.put(VP, json={"enabled": True, "preferred_model_id": "e2e-no-such-model"})
    assert r.status_code == 200 and r.json()["preferred_model_id"] == "e2e-no-such-model"
    r = api.put(VP, json={"enabled": True, "preferred_model_id": "zipformer-zh", "preferred_variant_id": "zzz"})
    assert r.status_code == 200 and r.json()["preferred_variant_id"] == "zzz"


@pytest.mark.parametrize("method", ["post", "delete", "patch"])
def test_voice_preference_wrong_method(api: httpx.Client, method: str) -> None:
    assert getattr(api, method)(VP).status_code == 405


@pytest.mark.usefixtures("restore_voice_pref")
def test_voice_preload_is_skipped_when_voice_input_disabled(api: httpx.Client) -> None:
    """Disabled voice input must NOT load anything onto the NPU worker."""
    api.put(VP, json={"enabled": False})
    for body in (None, {"model_id": "melotts-zh"}):
        r = api.post(f"{A}/voice-input/preload", json=body) if body else api.post(f"{A}/voice-input/preload")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "skipped"
        assert "disabled" in r.json()["detail"]
    loaded = {m["model_id"] for m in api.get(f"{A}/worker/status").json()["loaded_models"]}
    assert "melotts-zh" not in loaded, "preload was skipped, so the model must not become resident"


# --------------------------------------------------------------------------
# AB-10 - resident worker + blob cache
# --------------------------------------------------------------------------


def test_worker_status_shape_and_invariants(api: httpx.Client) -> None:
    r = api.get(f"{A}/worker/status")
    assert r.status_code == 200
    w = r.json()
    assert isinstance(w["alive"], bool) and isinstance(w["multimodel"], bool)
    assert w["state"] in WORKER_STATES, w["state"]
    assert w["total_workers"] >= 0 and w["queued_runs"] >= 0
    assert 0 <= w["busy_workers"] <= w["total_workers"]
    known = {m["id"] for m in _models(api)}
    assert w["active_model_id"] is None or w["active_model_id"] in known
    for lm in w["loaded_models"]:
        assert lm["model_id"] in known
        assert lm["state"] in LOADED_STATES
        assert lm["age_seconds"] >= 0


@pytest.fixture
def blob_dir(api: httpx.Client, backend: Backend) -> Iterator[Path]:
    """The cache dir, asserted to be INSIDE the throw-away data dir before anything is written."""
    path = Path(api.get(f"{A}/cache/status").json()["blob_dir"])
    assert path.resolve().is_relative_to(backend.data_dir.resolve()), (
        f"refusing to touch {path}: not under the isolated data dir {backend.data_dir}"
    )
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_cache_status_counts_files_recursively(api: httpx.Client, blob_dir: Path) -> None:
    assert api.get(f"{A}/cache/status").json() == {
        "blob_count": 0, "total_bytes": 0, "blob_dir": str(blob_dir),
    }
    (blob_dir / "a.bin").write_bytes(b"x" * 100)
    (blob_dir / "sub" / "deep").mkdir(parents=True)
    (blob_dir / "sub" / "b.bin").write_bytes(b"y" * 250)
    (blob_dir / "sub" / "deep" / "c.bin").write_bytes(b"z" * 50)
    s = api.get(f"{A}/cache/status").json()
    assert (s["blob_count"], s["total_bytes"]) == (3, 400)  # directories are not counted


def test_cache_clear_deletes_every_file_and_is_idempotent(api: httpx.Client, blob_dir: Path) -> None:
    (blob_dir / "sub").mkdir()
    (blob_dir / "a.bin").write_bytes(b"1")
    (blob_dir / "sub" / "b.bin").write_bytes(b"22")
    r = api.delete(f"{A}/cache")
    assert r.status_code == 200 and r.json() == {"deleted_files": 2}
    assert [p for p in blob_dir.rglob("*") if p.is_file()] == []
    s = api.get(f"{A}/cache/status").json()
    assert (s["blob_count"], s["total_bytes"]) == (0, 0)
    assert api.delete(f"{A}/cache").json() == {"deleted_files": 0}


def test_cache_endpoints_when_directory_is_missing(api: httpx.Client, blob_dir: Path) -> None:
    shutil.rmtree(blob_dir)
    s = api.get(f"{A}/cache/status")
    assert s.status_code == 200
    assert (s.json()["blob_count"], s.json()["total_bytes"]) == (0, 0)
    assert api.delete(f"{A}/cache").json() == {"deleted_files": 0}


def test_cache_wrong_method(api: httpx.Client) -> None:
    assert api.post(f"{A}/cache").status_code == 405


# --------------------------------------------------------------------------
# DELETE /models - only the paths that cannot touch real files (see module doc)
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "params",
    [{}, {"variantIds": "fp16,int8", "deleteFiles": "false"}],
)
def test_delete_unknown_model_is_404_and_changes_nothing(api: httpx.Client, params: dict) -> None:
    before = [m["id"] for m in _models(api)]
    assert "e2e-no-such-model" not in before
    r = api.delete(f"{A}/models/e2e-no-such-model", params=params)
    assert r.status_code == 404, r.text
    assert [m["id"] for m in _models(api)] == before
    assert set(BUILTIN) <= set(before)


def test_delete_invalid_model_id_is_400(api: httpx.Client) -> None:
    r = api.delete(f"{A}/models/BAD%20ID!")
    assert r.status_code == 400
    assert r.json()["code"] == "app_builder.app_model_id_invalid"

