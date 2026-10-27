# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - App Builder model import: dry-run, commit, rollback, delete (AB-13).

Import takes a *candidate* - a model workdir holding ``app_pack/`` (``manifest.json`` + ``runner.py`` + ``weights/*.bin`` +
``_candidate.json``) or a bare manifest ``.json`` - turns it into a plan (``dry-run``), applies the plan (``commit``) and can undo it
(``rollback``). Everything here is fabricated under the throw-away data dir: ~1 MB zero-filled "weights" and a one-line ``runner.py``
that is only COMPILED by the importer, never run. No NPU, no model server, no network.

NOT covered (deliberately):

* **Running an imported model** (it would need a real runner and the NPU - AB-04).
* **``scan-bins``** beyond its empty answer: it inspects real ``.bin`` weights for a normalisation step.
* Multi-variant packs, ``required_catalog_ids`` resolution and ``input_presets`` migration (they need a real Pack).
* The Model Builder -> App Builder hand-over that produces ``_candidate.json`` (that is the model-builder flow, MB-*).

Isolation (verified): candidates live in ``<work>/c``; the importer writes only to ``<data>/app_builder``; the module asserts the repo's
``factory/`` and ``models/`` listings are unchanged.

Observed behaviour worth pinning:

* A plan item is ``add`` (new id), ``replace`` (id already installed - a ``bump`` policy suggests ``x.y.z+1``) or ``skip`` (hard error:
  weights under 1 MiB, ``runner.py`` that does not compile ...). A plan with only skips is ``is_noop``.
* ``commit`` ALWAYS answers 201 with a commit id - for an empty plan, a plan of skips, an unreadable source and a built-in id alike
  (APP-ISS-10). What happened is only visible in the gallery.
* ``rollback`` of an ``add`` removes the DATABASE row but leaves the files under ``user_models/`` and ``user_model_weights/`` on disk
  (APP-ISS-11); rollback of a ``replace`` restores the old row but leaves the NEW files, so row and disk disagree (APP-ISS-12).
* ``DELETE /models/{id}`` removes the row AND the files (the only path that cleans up).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e._owned_backend import OwnedBackend, owned_backend
from tests.e2e.conftest import REPO_ROOT

pytestmark = pytest.mark.e2e

AB = "/api/app-builder"
BUILTIN = ["melotts-zh", "real-esrgan-x4plus", "whisper-base", "zipformer-zh"]
PLAN_KEYS = {"model_id", "action", "source", "reason", "display_name", "generated_at", "errors", "conflicts", "suggested_version", "conflict_policy"}
MB = 1024 * 1024


def _tree(p: Path) -> list[str] | None:
    return sorted(str(x.relative_to(p)) for x in p.rglob("*")) if p.exists() else None


_REPO_AT_START = {d: _tree(REPO_ROOT / d) for d in ("factory", "models")}


@pytest.fixture(scope="module", autouse=True)
def repo_untouched() -> Iterator[None]:
    yield
    assert {d: _tree(REPO_ROOT / d) for d in ("factory", "models")} == _REPO_AT_START, "an import test wrote into the repo"


@pytest.fixture(scope="module")
def imp() -> Iterator[tuple[OwnedBackend, httpx.Client, Path]]:
    with owned_backend() as ob:
        cands = ob.work / "c"
        cands.mkdir()
        yield ob, ob.start(), cands


def make_pack(cands: Path, name: str, pid: str, *, version: str = "1.0.0", weights: int = MB + 7, runner: str = "def run(*a, **k):\n    return {}\n",
              ready: bool = True, title: str | None = None) -> Path:
    pack = cands / name / "app_pack"
    shutil.rmtree(cands / name, ignore_errors=True)
    (pack / "weights").mkdir(parents=True)
    (pack / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "modelId": pid, "displayName": title or f"E2E {pid}", "version": version,
        "taxonomy": {"group": "vision", "task": "demo", "tags": []}, "assets": {"installPath": f"models/{pid}/{pid}.bin"}}))
    (pack / "runner.py").write_text(runner)
    (pack / "weights" / f"{pid}.bin").write_bytes(b"\0" * weights)
    (pack / "_candidate.json").write_text(json.dumps({"packId": pid, "displayName": title or f"E2E {pid}", "ready": ready}))
    return cands / name


def _ids(cl: httpx.Client) -> list[str]:
    return [m["id"] for m in cl.get(f"{AB}/models").json()["items"]]


def _builtin_ids(cl: httpx.Client) -> list[str]:
    """Built-in models sort by TITLE, not id, so tests compare them as a set and only check the user-imported tail in order."""
    return sorted(i for i in _ids(cl) if i in BUILTIN)


def _plan(cl: httpx.Client, *paths: Path | str) -> dict:
    r = cl.post(f"{AB}/import/dry-run", json={"candidates": [str(p) for p in paths]})
    assert r.status_code == 200, r.text
    return r.json()


def _commit(cl: httpx.Client, plan: dict) -> str:
    r = cl.post(f"{AB}/import/commit", json={"items": plan["items"]})
    assert r.status_code == 201, r.text
    return r.json()["commit_id"]


def _files(ob: OwnedBackend) -> list[str]:
    return sorted(str(p.relative_to(ob.data_dir / "app_builder")).replace("\\", "/") for p in (ob.data_dir / "app_builder").rglob("*") if p.is_file())


def _db(ob: OwnedBackend, sql: str, *a: object) -> list[tuple]:
    con = sqlite3.connect(str(ob.data_dir / "db" / "qai.db"), timeout=15)
    try:
        return con.execute(sql, a).fetchall()
    finally:
        con.close()


@pytest.fixture
def cleanup(imp: tuple) -> Iterator[Callable[[str], None]]:
    ob, cl, _c = imp
    ids: list[str] = []
    yield ids.append
    for i in ids:
        cl.delete(f"{AB}/models/{i}")


# --------------------------------------------------------------------------
# dry-run
# --------------------------------------------------------------------------


def test_a_valid_pack_is_planned_as_an_add(imp: tuple) -> None:
    _ob, cl, c = imp
    pack = make_pack(c, "ok1", "e2e-ok1")
    plan = _plan(cl, pack)
    assert set(plan) == {"items", "is_empty", "is_noop"} and (plan["is_empty"], plan["is_noop"]) == (False, False)
    (item,) = plan["items"]
    assert set(item) == PLAN_KEYS
    assert (item["model_id"], item["action"], item["reason"], item["errors"], item["conflicts"]) == ("e2e-ok1", "add", None, [], [])
    assert Path(item["source"]) == pack / "app_pack" and item["display_name"] == "E2E e2e-ok1" and item["conflict_policy"] == "bump"
    assert item["suggested_version"] is None and item["generated_at"].endswith("+00:00")


def test_dry_run_changes_nothing(imp: tuple) -> None:
    ob, cl, c = imp
    pack = make_pack(c, "ok2", "e2e-ok2")
    before = (_files(ob), _ids(cl), _db(ob, "select count(*) from app_builder_import_commit"))
    _plan(cl, pack)
    assert (_files(ob), _ids(cl), _db(ob, "select count(*) from app_builder_import_commit")) == before


def test_no_candidates_is_an_empty_plan(imp: tuple) -> None:
    _ob, cl, _c = imp
    assert _plan(cl) == {"items": [], "is_empty": True, "is_noop": True}


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"weights": 1000}, "weights too small (1000 bytes; expected >= 1048576)"),
        ({"runner": "def (:\n"}, "runner.py compilation failed"),
    ],
)
def test_a_pack_with_a_hard_error_is_a_skip_and_the_plan_a_noop(imp: tuple, kw: dict, needle: str) -> None:
    _ob, cl, c = imp
    pid = "e2e-bad" + str(abs(hash(needle)) % 10_000)
    pack = make_pack(c, pid, pid, **kw)
    plan = _plan(cl, pack)
    (item,) = plan["items"]
    assert item["action"] == "skip" and needle in item["reason"] and any(needle in e for e in item["errors"])
    assert plan["is_noop"] is True and plan["is_empty"] is False


def test_the_weights_limit_is_one_mebibyte_inclusive(imp: tuple) -> None:
    _ob, cl, c = imp
    just_under = _plan(cl, make_pack(c, "w-under", "e2e-wu", weights=MB - 1))["items"][0]
    exact = _plan(cl, make_pack(c, "w-exact", "e2e-we", weights=MB))["items"][0]
    assert just_under["action"] == "skip" and exact["action"] == "add"


def test_a_workdir_that_is_not_ready_is_skipped(imp: tuple) -> None:
    _ob, cl, c = imp
    pack = make_pack(c, "notready", "e2e-notready", ready=False)
    plan = _plan(cl, pack)
    assert plan["items"] == [] or plan["items"][0]["action"] == "skip"
    assert plan["is_noop"] is True


def test_a_directory_without_a_ready_pack_gives_no_plan_item(imp: tuple) -> None:
    """An existing folder with no ``app_pack/_candidate.json`` is dropped from the plan (the UI then shows its first stage)."""
    _ob, cl, c = imp
    empty = c / "empty-folder"
    empty.mkdir()
    assert _plan(cl, empty)["items"] == []
    assert _plan(cl, c)["items"] == [], "and so is this whole candidates folder"


@pytest.mark.parametrize(("candidate", "model_id"), [("e2e-just-a-name", "e2e-just-a-name"), ("C:\\e2e-nope\\missing.json", "imported-0000")])
def test_a_path_that_does_not_exist_is_treated_as_a_model_id_hint(imp: tuple, candidate: str, model_id: str) -> None:
    """KNOWN GAP / DESIGN QUESTION (current behaviour, pinned) - APP-ISS-13: legacy compatibility - a string that is not a path on disk is read
    as a model id. A valid id becomes an ``add`` of that id; anything else becomes ``imported-NNNN``. Neither has any content behind it."""
    _ob, cl, _c = imp
    (item,) = _plan(cl, candidate)["items"]
    assert (item["model_id"], item["action"], item["reason"], item["source"]) == (model_id, "add", None, candidate)


def test_an_empty_candidate_string_is_ignored(imp: tuple) -> None:
    _ob, cl, _c = imp
    items = _plan(cl, "")["items"]
    assert len(items) <= 1 and all(i["model_id"].startswith("imported-") for i in items)

def test_a_bare_manifest_file_is_planned_too(imp: tuple) -> None:
    _ob, cl, c = imp
    f = c / "m_good.json"
    f.write_text(json.dumps({"id": "e2e-file", "title": "From File", "displayName": "From File Name", "taxonomy": ["vision", "demo"]}))
    (item,) = _plan(cl, f)["items"]
    assert (item["model_id"], item["action"], item["display_name"], Path(item["source"])) == ("e2e-file", "add", "From File Name", f)


@pytest.mark.parametrize(
    ("name", "content"),
    [("bad.json", "{nope"), ("notitle.json", json.dumps({"id": "e2e-nt"})), ("tax.json", json.dumps({"id": "e2e-tx", "title": "T", "taxonomy": "x"})),
     ("preset.json", json.dumps({"id": "e2e-ps", "title": "T", "input_presets": [{"x": 1}]})), ("empty.json", "")],
)
def test_an_invalid_manifest_file_is_a_skip_with_the_reason(imp: tuple, name: str, content: str) -> None:
    _ob, cl, c = imp
    f = c / name
    f.write_text(content)
    plan = _plan(cl, f)
    assert plan["is_noop"] is True and all(i["action"] == "skip" and i["reason"] for i in plan["items"])


@pytest.mark.parametrize("content", ["[]", '"text"', "5", "null"], ids=["list", "string", "number", "null"])
def test_a_manifest_file_that_is_valid_json_but_not_an_object_crashes_the_dry_run(imp: tuple, content: str) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-14: ``payload.get("id")`` raises ``AttributeError``, which the ``except (OSError,
    JSONDecodeError, ValueError, TypeError)`` does not list, so the WHOLE dry-run (every candidate in it) is a 500. A syntax error in the
    same file is handled (``test_an_invalid_manifest_file_is_a_skip_with_the_reason``). Same shape as MB-ISS-01."""
    _ob, cl, c = imp
    f = c / "notobject.json"
    f.write_text(content)
    good = make_pack(c, "alongside", "e2e-alongside")
    r = cl.post(f"{AB}/import/dry-run", json={"candidates": [str(f), str(good)]})
    assert r.status_code == 500 and r.json()["code"] == "internal.unexpected", "one bad file takes the good candidate down with it"
    assert _plan(cl, good)["items"][0]["action"] == "add"

def test_an_id_that_is_already_installed_becomes_a_replace_with_a_bumped_version(imp: tuple, cleanup: Callable) -> None:
    _ob, cl, c = imp
    cleanup("e2e-rep")
    v1 = make_pack(c, "rep1", "e2e-rep", version="1.0.0")
    _commit(cl, _plan(cl, v1))
    (item,) = _plan(cl, v1)["items"]
    assert item["action"] == "replace" and item["suggested_version"] == "1.0.1"
    assert item["conflicts"] == ["model id 'e2e-rep' already exists (version 1.0.0); choose a conflict policy"]


def test_a_built_in_id_is_planned_as_a_replace(imp: tuple) -> None:
    _ob, cl, c = imp
    (item,) = _plan(cl, make_pack(c, "bi", "zipformer-zh"))["items"]
    assert item["action"] == "replace" and item["conflicts"], "the plan itself does not forbid replacing a built-in model"


def test_the_same_id_in_two_candidates_is_planned_once(imp: tuple) -> None:
    _ob, cl, c = imp
    a, b = make_pack(c, "d1", "e2e-twin"), make_pack(c, "d2", "e2e-twin")
    items = _plan(cl, a, b)["items"]
    assert [i["model_id"] for i in items] == ["e2e-twin"] and Path(items[0]["source"]) == a / "app_pack", "the first wins"


def test_many_candidates_are_accepted(imp: tuple) -> None:
    _ob, cl, c = imp
    pack = make_pack(c, "many", "e2e-many")
    assert len(_plan(cl, *([pack] * 200))["items"]) == 1


@pytest.mark.parametrize("body", [{}, {"candidates": "x"}, {"candidates": [1]}, {"candidates": None}])
def test_dry_run_validates_its_body(imp: tuple, body: dict) -> None:
    _ob, cl, _c = imp
    r = cl.post(f"{AB}/import/dry-run", json=body)
    if body == {}:
        assert r.status_code == 200 and r.json()["is_empty"] is True, "candidates defaults to an empty list"
    else:
        assert r.status_code == 400 and r.json()["code"] == "request.validation"


# --------------------------------------------------------------------------
# commit
# --------------------------------------------------------------------------


def test_committing_an_add_installs_the_model_everywhere(imp: tuple, cleanup: Callable) -> None:
    ob, cl, c = imp
    cleanup("e2e-inst")
    before_files = _files(ob)
    cid = _commit(cl, _plan(cl, make_pack(c, "inst", "e2e-inst", title="Installed One")))
    assert len(cid) == 26
    assert "e2e-inst" in _ids(cl)
    d = cl.get(f"{AB}/models/e2e-inst").json()
    assert (d["title"], d["user_imported"], d["enabled"], d["pinned"], d["status"], d["category"]) == ("Installed One", True, True, False, "Ready", "demo")
    assert d["taxonomy"] == ["vision", "demo"] and d["builtin"] is False if "builtin" in d else True
    assert sorted(set(_files(ob)) - set(before_files)) == [
        "user_model_weights/models/e2e-inst/e2e-inst.bin", "user_models/e2e-inst/manifest.json", "user_models/e2e-inst/runner.py"]
    assert (ob.data_dir / "app_builder" / "user_model_weights" / "models" / "e2e-inst" / "e2e-inst.bin").stat().st_size == MB + 7
    assert _db(ob, "select version, user_imported, enabled from app_builder_model_definition where id = 'e2e-inst'") == [("1.0.0", 1, 1)]
    assert _db(ob, "select count(*) from app_builder_import_commit where id = ? and rolled_back_at is null", cid) == [(1,)]


def test_committing_a_replace_installs_the_new_version_over_the_old(imp: tuple, cleanup: Callable) -> None:
    ob, cl, c = imp
    cleanup("e2e-over")
    _commit(cl, _plan(cl, make_pack(c, "o1", "e2e-over", version="1.0.0", title="Old")))
    v2 = make_pack(c, "o2", "e2e-over", version="2.0.0", title="New")
    plan = _plan(cl, v2)
    assert plan["items"][0]["action"] == "replace"
    _commit(cl, plan)
    assert cl.get(f"{AB}/models/e2e-over").json()["title"] == "New"
    assert _db(ob, "select version, title from app_builder_model_definition where id = 'e2e-over'") == [("1.0.1", "New")], "the bumped suggestion, not 2.0.0"
    assert json.loads((ob.data_dir / "app_builder" / "user_models" / "e2e-over" / "manifest.json").read_text())["version"] == "1.0.1"


def test_commit_answers_201_for_things_that_installed_nothing(imp: tuple) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-10: an empty plan, a plan of skips, an unreadable source and an add of a built-in id
    all come back 201 with a commit id and change nothing. The caller cannot tell success from no-op."""
    ob, cl, _c = imp
    before = (sorted(_ids(cl)), _files(ob))
    skip = {"model_id": "e2e-s", "action": "skip", "source": "x"}
    ghost = {"model_id": "e2e-ghost", "action": "add", "source": "C:\\e2e-nope\\m.json"}
    builtin_add = {"model_id": "zipformer-zh", "action": "add", "source": "C:\\e2e-nope\\m.json"}
    builtin_rep = {"model_id": "zipformer-zh", "action": "replace", "source": "C:\\e2e-nope\\m.json"}
    for items in ([], [skip], [ghost], [builtin_add], [builtin_rep]):
        r = cl.post(f"{AB}/import/commit", json={"items": items})
        assert r.status_code == 201 and list(r.json()) == ["commit_id"], items
    assert (sorted(_ids(cl)), _files(ob)) == before
    assert cl.get(f"{AB}/models/zipformer-zh").json()["user_imported"] is False


def test_each_commit_gets_its_own_id_and_a_row(imp: tuple) -> None:
    ob, cl, _c = imp
    n0 = _db(ob, "select count(*) from app_builder_import_commit")[0][0]
    a = cl.post(f"{AB}/import/commit", json={"items": []}).json()["commit_id"]
    b = cl.post(f"{AB}/import/commit", json={"items": []}).json()["commit_id"]
    assert a != b and _db(ob, "select count(*) from app_builder_import_commit")[0][0] == n0 + 2


@pytest.mark.parametrize(
    "body",
    [{}, {"items": "x"}, {"items": [{}]}, {"items": [{"model_id": "x", "action": "add"}]}, {"items": [{"model_id": "", "action": "add", "source": "s"}]}],
)
def test_commit_validates_the_plan_shape(imp: tuple, body: dict) -> None:
    _ob, cl, _c = imp
    r = cl.post(f"{AB}/import/commit", json=body)
    if body == {}:
        assert r.status_code == 201, "an absent items list is an empty plan"
    elif isinstance(body.get("items"), list) and body["items"] and isinstance(body["items"][0], dict) and body["items"][0].get("model_id") == "":
        assert r.status_code == 400 and r.json()["code"] == "app_builder.import_plan_invalid"
    else:
        assert r.status_code == 400 and r.json()["code"] == "request.validation", r.text[:200]


def test_commit_rejects_an_unknown_action(imp: tuple) -> None:
    _ob, cl, _c = imp
    r = cl.post(f"{AB}/import/commit", json={"items": [{"model_id": "e2e-x1", "action": "explode", "source": "s"}]})
    assert r.status_code == 400 and r.json() == {
        "type": "ValidationError", "code": "app_builder.import_action_invalid", "message": "unknown import action 'explode'",
        "details": {"field_errors": {"action": ["'explode' is not a valid ImportAction"]}}}


def test_commit_trusts_the_plan_it_is_given_not_the_dry_run(imp: tuple, cleanup: Callable) -> None:
    """A plan item can be edited between dry-run and commit: the importer installs whatever ``model_id`` the item names."""
    ob, cl, c = imp
    cleanup("e2e-renamed")
    plan = _plan(cl, make_pack(c, "trust", "e2e-original"))
    plan["items"][0]["model_id"] = "e2e-renamed"
    _commit(cl, plan)
    ids = _ids(cl)
    assert ("e2e-renamed" in ids) != ("e2e-original" in ids)


# --------------------------------------------------------------------------
# rollback
# --------------------------------------------------------------------------


def test_rolling_back_an_add_removes_the_row_but_leaves_the_files(imp: tuple) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-11: ``rollback`` deletes the database row only; ``user_models/<id>/`` and
    ``user_model_weights/...`` (about 1 MB here, gigabytes for a real model) stay on disk and nothing lists them any more."""
    ob, cl, c = imp
    cid = _commit(cl, _plan(cl, make_pack(c, "rb1", "e2e-rb1")))
    files = [f for f in _files(ob) if "e2e-rb1" in f]
    assert len(files) == 3 and "e2e-rb1" in _ids(cl)
    r = cl.post(f"{AB}/import/rollback", json={"commit_id": cid})
    assert r.status_code == 200 and r.json() == {"commit_id": cid, "status": "rolled_back"}
    assert "e2e-rb1" not in _ids(cl) and _db(ob, "select count(*) from app_builder_model_definition where id = 'e2e-rb1'") == [(0,)]
    assert [f for f in _files(ob) if "e2e-rb1" in f] == files, "the files are still there"
    assert _db(ob, "select rolled_back_reason from app_builder_import_commit where id = ?", cid) == [("user_rollback",)]
    shutil_cleanup(ob, "e2e-rb1")


def shutil_cleanup(ob: OwnedBackend, pid: str) -> None:
    import shutil

    for sub in ("user_models", "user_model_weights/models"):
        shutil.rmtree(ob.data_dir / "app_builder" / sub / pid, ignore_errors=True)


def test_a_rolled_back_model_can_be_imported_again(imp: tuple, cleanup: Callable) -> None:
    ob, cl, c = imp
    pack = make_pack(c, "rb2", "e2e-rb2")
    cid = _commit(cl, _plan(cl, pack))
    cl.post(f"{AB}/import/rollback", json={"commit_id": cid})
    plan = _plan(cl, pack)
    assert plan["items"][0]["action"] == "add", "the row is gone, so it is new again"
    _commit(cl, plan)
    cleanup("e2e-rb2")
    assert "e2e-rb2" in _ids(cl)


def test_rolling_back_a_replace_restores_the_old_row_but_not_the_old_files(imp: tuple, cleanup: Callable) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-12: after rolling back a replace the database says version 1.0.0 / the old title,
    while ``user_models/<id>/manifest.json`` on disk is still the NEW 1.0.1."""
    ob, cl, c = imp
    cleanup("e2e-rb3")
    _commit(cl, _plan(cl, make_pack(c, "rb3a", "e2e-rb3", version="1.0.0", title="Old title")))
    cid2 = _commit(cl, _plan(cl, make_pack(c, "rb3b", "e2e-rb3", version="2.0.0", title="New title")))
    assert cl.post(f"{AB}/import/rollback", json={"commit_id": cid2}).status_code == 200
    assert _db(ob, "select version, title from app_builder_model_definition where id = 'e2e-rb3'") == [("1.0.0", "Old title")]
    assert json.loads((ob.data_dir / "app_builder" / "user_models" / "e2e-rb3" / "manifest.json").read_text())["version"] == "1.0.1"


def test_a_commit_can_be_rolled_back_only_once(imp: tuple) -> None:
    _ob, cl, _c = imp
    cid = cl.post(f"{AB}/import/commit", json={"items": []}).json()["commit_id"]
    assert cl.post(f"{AB}/import/rollback", json={"commit_id": cid}).status_code == 200
    r = cl.post(f"{AB}/import/rollback", json={"commit_id": cid})
    assert r.status_code == 422 and r.json() == {"type": "ImportConflictError", "code": "app_builder.import_conflict",
                                                  "message": f"commit_id {cid} already rolled back", "details": {"commit_id": cid}}


def test_rollback_of_an_unknown_commit(imp: tuple) -> None:
    _ob, cl, _c = imp
    r = cl.post(f"{AB}/import/rollback", json={"commit_id": "01M54WG9NKYCQ66SX5DP6C4RKT"})
    assert r.status_code == 422 and r.json()["message"] == "unknown commit_id 01M54WG9NKYCQ66SX5DP6C4RKT"


@pytest.mark.parametrize("cid", ["x", "", "z" * 26, "01m54wg9nkycq66sx5dp6c4rkt", "0" * 25, "0" * 27, "01M54WG9NKYCQ66SX5DP6C4RK!"])
def test_a_malformed_commit_id_is_a_400(imp: tuple, cid: str) -> None:
    _ob, cl, _c = imp
    r = cl.post(f"{AB}/import/rollback", json={"commit_id": cid})
    assert r.status_code == 400 and r.json()["code"] == "app_builder.commit_id_invalid" and "26-char" in r.json()["message"]


def test_a_missing_commit_id_is_a_400(imp: tuple) -> None:
    _ob, cl, _c = imp
    r = cl.post(f"{AB}/import/rollback", json={})
    assert r.status_code == 400 and r.json()["code"] == "request.validation" and "body.commit_id" in r.json()["details"]["field_errors"]


@pytest.mark.parametrize("cid", ["0" * 26, "I" * 26, "01M54WG9NKYCQ66SX5DP6C4RKU"])
def test_a_well_formed_but_unknown_commit_id_is_a_422(imp: tuple, cid: str) -> None:
    """The id pattern is looser than strict Crockford (``I`` passes); format-valid ids are looked up and not found."""
    _ob, cl, _c = imp
    r = cl.post(f"{AB}/import/rollback", json={"commit_id": cid})
    assert r.status_code == 422 and r.json()["code"] == "app_builder.import_conflict" and r.json()["message"] == f"unknown commit_id {cid}"

def test_rolling_back_an_old_commit_after_the_model_was_deleted_is_harmless(imp: tuple) -> None:
    _ob, cl, c = imp
    cid = _commit(cl, _plan(cl, make_pack(c, "rb4", "e2e-rb4")))
    assert cl.delete(f"{AB}/models/e2e-rb4").status_code == 200
    assert cl.post(f"{AB}/import/rollback", json={"commit_id": cid}).status_code == 200


# --------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------


def test_delete_removes_the_row_and_every_file(imp: tuple) -> None:
    ob, cl, c = imp
    _commit(cl, _plan(cl, make_pack(c, "del1", "e2e-del1")))
    r = cl.delete(f"{AB}/models/e2e-del1")
    assert r.status_code == 200 and r.json() == {"model_id": "e2e-del1", "mode": "full", "deleted_variants": [], "remaining_variants": [], "new_default": None, "warnings": []}
    assert "e2e-del1" not in _ids(cl) and not [f for f in _files(ob) if "e2e-del1" in f]
    assert cl.get(f"{AB}/models/e2e-del1").status_code == 404


def test_built_in_models_cannot_be_deleted(imp: tuple) -> None:
    _ob, cl, _c = imp
    for mid in BUILTIN:
        r = cl.delete(f"{AB}/models/{mid}")
        assert r.status_code == 403 and r.json()["code"] == "app_builder.app_model_builtin_protected"
        assert r.json()["message"] == f"Built-in model '{mid}' cannot be deleted. Only user-imported models can be removed."
    assert _builtin_ids(cl) == sorted(BUILTIN)


def test_delete_of_an_unknown_model_is_a_404_in_a_bare_detail(imp: tuple) -> None:
    _ob, cl, _c = imp
    r = cl.delete(f"{AB}/models/nope-x")
    assert r.status_code == 404 and r.json() == {"detail": "app_builder.app_model_not_found: app model nope-x not found"}


def test_scan_bins_with_nothing_to_scan(imp: tuple) -> None:
    _ob, cl, _c = imp
    r = cl.post(f"{AB}/import/scan-bins")
    assert r.status_code == 200 and r.json() == {"results": [], "needs_normalize": None, "diagnostics": None}
