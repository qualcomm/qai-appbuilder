# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - App Builder runs without an NPU, and share links (AB-14, AB-12).

AB-14: this machine HAS an NPU, but a run is made to fail before it needs one: ``inputs`` that are missing or point at a file that does
not exist. What is verified is the failure path every user without a working NPU or model also meets - a run is created, reaches
``failed`` with a stable ``error_code`` and a readable message, the SSE stream says so, the worker stays usable, and nothing is left
half-done. (A true "no NPU" environment is not simulated; the product reports ``ASSETS_NOT_INSTALLED`` for missing model files, which is
observed here, not forced.)

AB-12: ``POST /share`` mints an unguessable token for a run; ``GET /share/{token}`` shows the run to anyone holding it.

NOT covered (deliberately):

* **A run that SUCCEEDS** (needs the NPU and real inputs - AB-04), artifacts, ratings, the run WebSocket, batch runs.
* **Cancelling a run in flight**: a failing run ends in under a second here, and a slow one needs the NPU.
* **The share link in a browser** (the SPA page ``/share/<token>``).

Isolation (verified): one throw-away backend; inputs are strings or a path under ``C:\\e2e-nope`` that does not exist; nothing is read or
written outside the data dir. ``zipformer-zh`` and ``zipformer-zh`` are used because they fail on a missing ``audio`` input BEFORE loading
any model, in well under a second.

Observed behaviour worth pinning:

* ``POST /runs`` answers 201 ``pending`` at once; the failure arrives later, so a client must poll or stream (APP-ISS-15 asks whether a
  missing required input should be a 400 up front).
* The share view is PUBLIC to the backend's callers: no session or CSRF is needed for ``GET`` (that is the point of a share link).
* Deleting a run (``DELETE /history/runs/{id}``) also makes every share token of it answer ``share_not_found``.
* ``ttl_seconds: true`` is accepted as 1 second (APP-ISS-20).
* A share cannot be revoked: there is no revoke route, and ``revoked`` is always ``false`` in what the API returns (APP-ISS-16).
* ``ttl_seconds`` has no upper bound: 10**10 s is accepted and gives a link that expires in the year 2343 (APP-ISS-17).
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from tests.e2e._owned_backend import OwnedBackend, owned_backend

pytestmark = pytest.mark.e2e

AB = "/api/app-builder"
RUN_KEYS = {"id", "model_id", "status", "created_at", "started_at", "finished_at", "inputs", "artifacts", "error_message", "rating", "error_code"}
TERMINAL = {"succeeded", "failed", "cancelled"}
ULID = re.compile(r"^[0-9A-Z]{26}$")
NO_SUCH_RUN = "01M0000000000000000000NOPE"


@pytest.fixture(scope="module")
def runs() -> Iterator[tuple[OwnedBackend, httpx.Client]]:
    with owned_backend() as ob:
        cl = ob.start()
        cl.timeout = httpx.Timeout(60.0)
        yield ob, cl


def _wait(cl: httpx.Client, rid: str, seconds: float = 60.0) -> dict:
    end = time.monotonic() + seconds
    last: dict = {}
    while time.monotonic() < end:
        last = cl.get(f"{AB}/runs/{rid}").json()
        if last["status"] in TERMINAL:
            return last
        time.sleep(0.3)
    raise AssertionError(f"run {rid} did not finish: {last}")


@pytest.fixture
def failed_run(runs: tuple) -> Callable[..., dict]:
    _ob, cl = runs

    def _make(model: str = "zipformer-zh", inputs: dict | None = None) -> dict:
        r = cl.post(f"{AB}/runs", json={"model_id": model, "inputs": inputs if inputs is not None else {}})
        assert r.status_code == 201, r.text
        return _wait(cl, r.json()["id"])

    return _make


# --------------------------------------------------------------------------
# AB-14: runs that cannot complete
# --------------------------------------------------------------------------


def test_a_run_is_created_pending_and_ends_failed_with_a_reason(runs: tuple) -> None:
    _ob, cl = runs
    r = cl.post(f"{AB}/runs", json={"model_id": "zipformer-zh", "inputs": {}})
    assert r.status_code == 201
    created = r.json()
    assert set(created) == RUN_KEYS and ULID.match(created["id"])
    assert (created["status"], created["started_at"], created["finished_at"], created["artifacts"], created["error_code"], created["rating"]) == ("pending", None, None, [], None, None)
    done = _wait(cl, created["id"])
    assert (done["status"], done["error_code"], done["error_message"]) == ("failed", "INVALID_INPUT", "inputs.audio is required (path to source audio file)")
    assert done["started_at"] and done["finished_at"] and done["finished_at"] >= done["started_at"] >= done["created_at"]
    assert done["artifacts"] == [] and done["inputs"] == {}


@pytest.mark.parametrize(
    ("model", "inputs", "code", "needle"),
    [
        ("zipformer-zh", {}, "INVALID_INPUT", "inputs.audio is required"),
        ("real-esrgan-x4plus", {"image": "C:\\e2e-nope\\x.png"}, "INVALID_INPUT", "image file not found: C:\\e2e-nope\\x.png"),
        ("zipformer-zh", {"audio": "C:\\e2e-nope\\a.wav"}, "INVALID_INPUT", "e2e-nope"),
    ],
)
def test_missing_or_unreadable_inputs_fail_with_invalid_input(failed_run: Callable, model: str, inputs: dict, code: str, needle: str) -> None:
    run = failed_run(model, inputs)
    assert run["status"] == "failed" and run["error_code"] == code and needle in run["error_message"]
    assert run["inputs"] == inputs, "the inputs are kept as sent"


def test_the_not_installed_model_is_reported_as_invalid_input_not_as_not_installed(failed_run: Callable, runs: tuple) -> None:
    """KNOWN GAP / DESIGN QUESTION (current behaviour, pinned) - APP-ISS-18: ``real-esrgan-x4plus`` is ``NotInstalled`` in the gallery, yet
    a run of it is accepted and fails on its INPUT first; the user is never told the model is missing."""
    _ob, cl = runs
    assert next(m for m in cl.get(f"{AB}/models").json()["items"] if m["id"] == "real-esrgan-x4plus")["status"] == "NotInstalled"
    run = failed_run("real-esrgan-x4plus", {"image": "C:\\e2e-nope\\x.png"})
    assert run["error_code"] == "INVALID_INPUT" and "installed" not in (run["error_message"] or "").lower()


def test_the_run_stream_reports_the_failure_and_closes(runs: tuple) -> None:
    _ob, cl = runs
    rid = cl.post(f"{AB}/runs", json={"model_id": "zipformer-zh", "inputs": {}}).json()["id"]
    events, buf = [], ""
    with cl.stream("GET", f"{AB}/runs/{rid}/stream", timeout=httpx.Timeout(10.0, read=60.0)) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        for chunk in r.iter_text():
            buf += chunk
    for frame in buf.split("\n\n"):
        f = dict(l.split(": ", 1) for l in frame.splitlines() if ": " in l)
        if f:
            events.append((f.get("event"), f.get("data", "")))
    names = [e for e, _ in events]
    assert names[0] == "state" and "frame" in names
    assert any('"status": "streaming"' in d or '"status":"streaming"' in d for e, d in events if e == "state")
    assert any("INVALID_INPUT" in d for e, d in events if e == "frame"), events[-3:]
    assert _wait(cl, rid)["status"] == "failed"


def test_the_stream_of_a_finished_run_still_closes(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    run = failed_run()
    with cl.stream("GET", f"{AB}/runs/{run['id']}/stream", timeout=httpx.Timeout(10.0, read=30.0)) as r:
        assert r.status_code == 200
        body = "".join(r.iter_text())
    assert "INVALID_INPUT" in body or "failed" in body


def test_a_failed_run_does_not_break_the_next_one(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    a, b = failed_run(), failed_run("zipformer-zh")
    assert (a["status"], b["status"]) == ("failed", "failed") and a["id"] != b["id"]
    assert cl.get(f"{AB}/worker/status").status_code == 200 and cl.get("/api/system/health").status_code == 200


def test_several_runs_at_once_all_finish(runs: tuple) -> None:
    _ob, cl = runs
    ids = [cl.post(f"{AB}/runs", json={"model_id": m, "inputs": {}}).json()["id"] for m in ("zipformer-zh",) * 4]
    assert len(set(ids)) == 4
    assert [_wait(cl, i)["status"] for i in ids] == ["failed"] * 4


def test_the_list_shows_runs_newest_first_and_filters(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    failed_run("zipformer-zh")
    last = failed_run("zipformer-zh")
    rows = cl.get(f"{AB}/runs").json()["runs"]
    assert list(cl.get(f"{AB}/runs").json()) == ["runs", "limit", "offset"] and rows[0]["id"] == last["id"]
    assert [r["created_at"] for r in rows] == sorted((r["created_at"] for r in rows), reverse=True)
    assert all(r["model_id"] == "zipformer-zh" for r in cl.get(f"{AB}/runs", params={"model_id": "zipformer-zh"}).json()["runs"])
    assert all(r["status"] == "failed" for r in cl.get(f"{AB}/runs", params={"status": "failed"}).json()["runs"])
    assert len(cl.get(f"{AB}/runs", params={"limit": 1}).json()["runs"]) == 1
    assert cl.get(f"{AB}/runs", params={"model_id": "no-such-model"}).json() == {"runs": [], "limit": 50, "offset": 0}


def test_a_status_filter_with_an_unknown_value_is_ignored(runs: tuple, failed_run: Callable) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-19: ``?status=bogus`` is not a 400; it returns the unfiltered list."""
    _ob, cl = runs
    failed_run()
    assert cl.get(f"{AB}/runs", params={"status": "bogus"}).json() == cl.get(f"{AB}/runs").json()


@pytest.mark.parametrize(("params", "field"), [({"limit": 0}, "query.limit"), ({"limit": 501}, "query.limit"), ({"limit": "x"}, "query.limit")])
def test_list_limits_are_validated(runs: tuple, params: dict, field: str) -> None:
    _ob, cl = runs
    r = cl.get(f"{AB}/runs", params=params)
    assert r.status_code == 400 and field in r.json()["details"]["field_errors"]


@pytest.mark.parametrize(
    ("body", "field"),
    [({"inputs": {}}, "body.model_id"), ({"model_id": "", "inputs": {}}, "body.model_id"), ({"model_id": "m" * 129, "inputs": {}}, "body.model_id"),
     ({"model_id": "zipformer-zh", "inputs": []}, "body.inputs"), ({"model_id": "zipformer-zh", "inputs": "x"}, "body.inputs")],
)
def test_create_validates_its_body(runs: tuple, body: dict, field: str) -> None:
    _ob, cl = runs
    r = cl.post(f"{AB}/runs", json=body)
    assert r.status_code == 400 and r.json()["code"] == "request.validation" and field in r.json()["details"]["field_errors"]


def test_inputs_default_to_empty_when_omitted(runs: tuple) -> None:
    _ob, cl = runs
    r = cl.post(f"{AB}/runs", json={"model_id": "zipformer-zh"})
    assert r.status_code == 201 and r.json()["inputs"] == {}
    _wait(cl, r.json()["id"])


def test_an_unknown_model_is_a_422_and_creates_no_run(runs: tuple) -> None:
    _ob, cl = runs
    before = len(cl.get(f"{AB}/runs").json()["runs"])
    r = cl.post(f"{AB}/runs", json={"model_id": "no-such-model", "inputs": {}})
    assert r.status_code == 422 and r.json() == {"type": "AppModelNotFoundError", "code": "app_builder.app_model_not_found",
                                                  "message": "app model no-such-model not found", "details": {"model_id": "no-such-model"}}
    assert len(cl.get(f"{AB}/runs").json()["runs"]) == before


def test_an_unknown_variant_is_not_checked_up_front(runs: tuple) -> None:
    _ob, cl = runs
    r = cl.post(f"{AB}/runs", json={"model_id": "zipformer-zh", "inputs": {}, "variant": "no-such-variant"})
    assert r.status_code == 201
    _wait(cl, r.json()["id"])


def test_run_lookups_validate_the_id(runs: tuple) -> None:
    _ob, cl = runs
    bad = cl.get(f"{AB}/runs/short")
    assert bad.status_code == 400 and bad.json()["code"] == "app_builder.run_id_invalid"
    for path in (f"{AB}/runs/{NO_SUCH_RUN}", f"{AB}/runs/{NO_SUCH_RUN}/stream"):
        r = cl.get(path)
        assert r.status_code == 422 and r.json()["code"] == "app_builder.run_not_found" and r.json()["details"] == {"run_id": NO_SUCH_RUN}


def test_cancelling_a_finished_run_is_refused(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    run = failed_run()
    r = cl.delete(f"{AB}/runs/{run['id']}")
    assert r.status_code == 422 and r.json()["code"] == "app_builder.run_already_terminated"
    assert r.json()["message"] == f"Run {run['id']} already in terminal status 'failed'; cannot move to 'cancelled'"
    assert cl.get(f"{AB}/runs/{run['id']}").json()["status"] == "failed"


def test_cancelling_an_unknown_run(runs: tuple) -> None:
    _ob, cl = runs
    r = cl.delete(f"{AB}/runs/{NO_SUCH_RUN}")
    assert r.status_code == 422 and r.json()["code"] == "app_builder.run_not_found"


def test_deleting_a_run_from_the_history(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    run = failed_run()
    r = cl.delete(f"{AB}/history/runs/{run['id']}")
    assert r.status_code == 200 and r.json() == {"run_id": run["id"], "status": "deleted"}
    assert cl.get(f"{AB}/runs/{run['id']}").status_code == 422 and run["id"] not in [x["id"] for x in cl.get(f"{AB}/runs").json()["runs"]]
    again = cl.delete(f"{AB}/history/runs/{run['id']}")
    assert again.status_code == 404 and again.json() == {"detail": f"app_builder.run_not_found: run {run['id']} not found"}


# --------------------------------------------------------------------------
# AB-12: share links
# --------------------------------------------------------------------------


def test_a_share_is_minted_for_a_run(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    run = failed_run()
    r = cl.post(f"{AB}/share", json={"run_id": run["id"]})
    assert r.status_code == 201
    s = r.json()
    assert set(s) == {"token", "run_id", "created_at", "expires_at", "revoked"}
    assert re.fullmatch(r"[A-Za-z0-9_-]{43}", s["token"]), "32 random bytes, url-safe base64"
    assert (s["run_id"], s["expires_at"], s["revoked"]) == (run["id"], None, False) and s["created_at"].endswith("+00:00")


def test_tokens_are_unique_and_a_run_can_have_several(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    run = failed_run()
    tokens = [cl.post(f"{AB}/share", json={"run_id": run["id"]}).json()["token"] for _ in range(20)]
    assert len(set(tokens)) == 20


def test_the_share_view_shows_the_share_and_the_run(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    run = failed_run()
    s = cl.post(f"{AB}/share", json={"run_id": run["id"]}).json()
    v = cl.get(f"{AB}/share/{s['token']}")
    assert v.status_code == 200 and set(v.json()) == {"share", "run"}
    assert v.json()["share"] == s and v.json()["run"] == run == cl.get(f"{AB}/runs/{run['id']}").json()


def test_the_share_view_needs_no_session_or_csrf(runs: tuple, failed_run: Callable) -> None:
    ob, cl = runs
    run = failed_run()
    token = cl.post(f"{AB}/share", json={"run_id": run["id"]}).json()["token"]
    r = httpx.get(ob.base_url + f"{AB}/share/{token}", timeout=10)
    assert r.status_code == 200 and r.json()["run"]["id"] == run["id"], "a bare client with no cookie sees it"


def test_creating_a_share_needs_the_csrf_token(runs: tuple, failed_run: Callable) -> None:
    ob, cl = runs
    run = failed_run()
    r = httpx.post(ob.base_url + f"{AB}/share", json={"run_id": run["id"]}, timeout=10)
    assert r.status_code == 403 and r.json()["code"] == "security.csrf.missing"


def test_ttl_sets_an_expiry_that_many_seconds_ahead(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    run = failed_run()
    before = datetime.now(timezone.utc)
    s = cl.post(f"{AB}/share", json={"run_id": run["id"], "ttl_seconds": 3600}).json()
    exp = datetime.fromisoformat(s["expires_at"])
    assert timedelta(seconds=3590) < exp - before < timedelta(seconds=3610) and s["revoked"] is False


def test_an_expired_link_is_refused_with_a_422(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    run = failed_run()
    token = cl.post(f"{AB}/share", json={"run_id": run["id"], "ttl_seconds": 1}).json()["token"]
    assert cl.get(f"{AB}/share/{token}").status_code == 200
    time.sleep(2.2)
    r = cl.get(f"{AB}/share/{token}")
    assert r.status_code == 422 and r.json() == {"type": "ShareExpiredError", "code": "app_builder.share_expired",
                                                  "message": f"share token {token} is revoked or expired", "details": {"token": token}}


@pytest.mark.parametrize("ttl", [0, -1, 1.5, "x", None])
def test_ttl_must_be_a_positive_integer_or_absent(runs: tuple, failed_run: Callable, ttl: object) -> None:
    _ob, cl = runs
    run = failed_run()
    r = cl.post(f"{AB}/share", json={"run_id": run["id"], "ttl_seconds": ttl})
    if ttl is None:
        assert r.status_code == 201 and r.json()["expires_at"] is None
    else:
        assert r.status_code == 400 and r.json()["code"] == "request.validation" and "body.ttl_seconds" in r.json()["details"]["field_errors"], ttl


def test_a_boolean_ttl_is_taken_as_one_second(runs: tuple, failed_run: Callable) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-20: the use case rejects ``bool`` explicitly, but the request model coerces ``true`` to
    ``1`` first, so ``ttl_seconds: true`` mints a link that expires after ONE second instead of being refused."""
    _ob, cl = runs
    run = failed_run()
    r = cl.post(f"{AB}/share", json={"run_id": run["id"], "ttl_seconds": True})
    assert r.status_code == 201
    s = r.json()
    assert datetime.fromisoformat(s["expires_at"]) - datetime.fromisoformat(s["created_at"]) == timedelta(seconds=1)
    assert cl.post(f"{AB}/share", json={"run_id": run["id"], "ttl_seconds": False}).status_code == 400, "false becomes 0, which IS refused"


def test_the_list_pages_with_limit_and_offset(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    for _ in range(3):
        failed_run()
    full = cl.get(f"{AB}/runs").json()
    assert (full["limit"], full["offset"]) == (50, 0)
    page = cl.get(f"{AB}/runs", params={"limit": 1, "offset": 1}).json()
    assert (page["limit"], page["offset"]) == (1, 1) and [r["id"] for r in page["runs"]] == [full["runs"][1]["id"]]
    assert cl.get(f"{AB}/runs", params={"offset": 99999}).json() == {"runs": [], "limit": 50, "offset": 99999}
    assert cl.get(f"{AB}/runs", params={"offset": -1}).status_code == 400

def test_there_is_no_upper_bound_on_the_lifetime(runs: tuple, failed_run: Callable) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-17: ``ttl_seconds=10**10`` (317 years) is accepted."""
    _ob, cl = runs
    run = failed_run()
    s = cl.post(f"{AB}/share", json={"run_id": run["id"], "ttl_seconds": 10**10}).json()
    assert datetime.fromisoformat(s["expires_at"]).year >= 2300


def test_a_share_cannot_be_revoked(runs: tuple, failed_run: Callable) -> None:
    """KNOWN GAP (current behaviour, pinned) - APP-ISS-16: ``revoked`` exists in every response but nothing can set it - there is no
    revoke route (DELETE and PATCH are 405) - so a leaked link stays valid until it expires or its run is deleted."""
    _ob, cl = runs
    run = failed_run()
    token = cl.post(f"{AB}/share", json={"run_id": run["id"]}).json()["token"]
    for method in ("delete", "patch", "put"):
        assert getattr(cl, method)(f"{AB}/share/{token}", **({} if method == "delete" else {"json": {"revoked": True}})).status_code == 405
    assert cl.get(f"{AB}/share/{token}").json()["share"]["revoked"] is False


def test_deleting_the_run_kills_its_share_links(runs: tuple, failed_run: Callable) -> None:
    _ob, cl = runs
    run = failed_run()
    token = cl.post(f"{AB}/share", json={"run_id": run["id"]}).json()["token"]
    other = failed_run()
    other_token = cl.post(f"{AB}/share", json={"run_id": other["id"]}).json()["token"]
    cl.delete(f"{AB}/history/runs/{run['id']}")
    r = cl.get(f"{AB}/share/{token}")
    assert r.status_code == 422 and r.json()["code"] == "app_builder.share_not_found"
    assert cl.get(f"{AB}/share/{other_token}").status_code == 200, "other runs' links are untouched"


def test_sharing_an_unknown_or_malformed_run(runs: tuple) -> None:
    _ob, cl = runs
    r = cl.post(f"{AB}/share", json={"run_id": NO_SUCH_RUN})
    assert r.status_code == 422 and r.json()["code"] == "app_builder.run_not_found"
    r = cl.post(f"{AB}/share", json={"run_id": "x"})
    assert r.status_code == 400 and r.json()["code"] == "app_builder.run_id_invalid"
    r = cl.post(f"{AB}/share", json={})
    assert r.status_code == 400 and "body.run_id" in r.json()["details"]["field_errors"]


@pytest.mark.parametrize(("token", "status", "code"), [
    ("A" * 16, 422, "app_builder.share_not_found"), ("A" * 43, 422, "app_builder.share_not_found"), ("A" * 128, 422, "app_builder.share_not_found"),
    ("A" * 15, 400, "app_builder.share_token_invalid"), ("A" * 129, 400, "app_builder.share_token_invalid"),
    ("A" * 20 + ".", 400, "app_builder.share_token_invalid"), ("A" * 20 + "%20", 400, "app_builder.share_token_invalid")])
def test_the_token_format_is_checked_before_the_lookup(runs: tuple, token: str, status: int, code: str) -> None:
    _ob, cl = runs
    r = cl.get(f"{AB}/share/{token}")
    assert r.status_code == status and r.json()["code"] == code


def test_a_token_with_a_slash_never_reaches_the_handler(runs: tuple) -> None:
    _ob, cl = runs
    assert cl.get(f"{AB}/share/a/b").status_code == 404
