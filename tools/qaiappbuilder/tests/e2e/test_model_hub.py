# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - Model Hub / model catalog: download-catalog ``entries`` and cloud ``providers`` (MH-01 / MH-03).

Endpoints (``interfaces/http/routes/model_catalog.py``, prefix ``/api/model-catalog``):
``/entries``, ``/entries/{id}``, ``/entries/{id}/versions[/{vid}]``, ``/providers``,
``/providers/{id}``, ``/cloud-models``, ``/cloud-models/permissions``, ``/qgenie-quota``,
``/query-services``, plus the not-found/validation edges of ``/verify`` and ``/download``.

NOT covered (deliberately):

* **Anything that sends an ``api_key``.** ``PUT /providers/{id}`` with a real key writes the
  OS keyring, which is NOT isolated (Windows Credential Manager). The owner decided: no keyring
  writes in the formal suite. Consequently these are untested: the key reaching the SecretStore,
  ``has_api_key`` flipping to true, and a real key being replaced. The only ``api_key`` values sent
  here are ones that provably never reach the store (``""``, ``"****"``, a non-string); a final
  fixture asserts the keyring holds nothing for this module's ids.
* **Network.** ``/release-manifest/refresh``, a real download, and ``/verify`` against a real blob
  need the network / real files (MH-02). Only their not-found and validation edges run.
* **The qgenie / query-service paths on an INTERNAL edition** - this checkout is the external edition.

Isolation (verified): every row lives in the throw-away ``<data_dir>/db/qai.db``
(``model_catalog_*`` tables and ``kv_user_prefs``); the real data dir was untouched. Startup
spawns a best-effort background model-roster sync (inert without a QAI Service address), so no
test assumes a list is empty - each one filters by its own ``e2e``-prefixed ids.

Observed behaviour worth pinning (it differs from what one might assume):

* Domain ``ValueError`` is NOT mapped to a 4xx: a non-http(s) ``source_url`` or case-insensitively
  duplicated ``tags`` on ``POST /entries``, and a bad ``storage_category`` / ``storage_name`` on
  ``POST /verify``, all return **500** (``internal.unexpected``) instead of 400.
* ``provider_id`` accepts up to 128 chars, but the row key is ``model_catalog.provider.<id>``
  (23-char prefix) and ``kv_user_prefs.key`` is limited to 128, so ids of 106-128 chars -> **503**.
* An entry id containing ``/`` is accepted by ``POST`` but can never be addressed again
  (``GET`` 404, ``DELETE`` 405): it can only be removed from the database.
* There is no way to delete a provider over HTTP: ``PUT {"config": {}}`` leaves an empty row listed.
* ``pinned`` is read with ``bool()``, so the string ``"false"`` pins the provider.
* One corrupt row makes ``GET /entries`` fail for EVERY entry (503).
"""

from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Callable, Iterator
from urllib.parse import quote

import httpx
import pytest

from tests.e2e.conftest import Backend

pytestmark = pytest.mark.e2e

M = "/api/model-catalog"
SHA = "a" * 64
PERMISSION_VALUES = {"unknown", "allowed", "denied"}


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


@pytest.fixture
def api(http: httpx.Client) -> httpx.Client:
    http.get("/api/system/uptime")
    http.headers["X-QAI-CSRF"] = http.cookies.get("qai_csrf")
    return http


def _uid(prefix: str = "e2e-mh") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def sql(backend: Backend) -> Callable[..., list[tuple]]:
    """Run SQL against the THROW-AWAY database only."""
    path = str(backend.data_dir / "db" / "qai.db")

    def _run(q: str, args: tuple = ()) -> list[tuple]:
        con = sqlite3.connect(path, timeout=15)
        try:
            cur = con.execute(q, args)
            con.commit()
            return cur.fetchall()
        finally:
            con.close()

    return _run


def _entry_body(model_id: str, **over: object) -> dict:
    return {
        "model_id": model_id, "name": over.pop("name", "E2E Model"), "provider": "local",
        "source_url": "https://example.invalid/model.gguf", "description": "d", "tags": [], **over,
    }


@pytest.fixture
def make_entry(api: httpx.Client, sql: Callable[..., list[tuple]]) -> Iterator[Callable[..., dict]]:
    """Create entries and remove them afterwards (by SQL, so unaddressable ids are cleaned too)."""
    created: list[str] = []

    def _make(model_id: str | None = None, **over: object) -> dict:
        mid = model_id or _uid()
        r = api.post(f"{M}/entries", json=_entry_body(mid, **over))
        assert r.status_code == 201, r.text
        created.append(mid)
        return r.json()

    yield _make
    for mid in created:
        sql("DELETE FROM model_catalog_entry WHERE id = ?", (mid,))


def _seed_version(sql: Callable[..., list[tuple]], parent: str, vid: str, status: str = "published") -> None:
    sql(
        "INSERT INTO model_catalog_version (id,parent_model_id,checksum_algorithm,checksum_value,"
        "size_bytes,manifest_url,status) VALUES (?,?,?,?,?,?,?)",
        (vid, parent, "sha256", SHA, 123, "https://example.invalid/v", status),
    )


@pytest.fixture
def provider_ids() -> list[str]:
    """Ids this test created; the final guard asserts the keyring holds nothing for them."""
    return _PROVIDER_IDS


_PROVIDER_IDS: list[str] = []


@pytest.fixture(scope="module", autouse=True)
def keyring_untouched() -> Iterator[None]:
    """READ-ONLY guard: no keyring entry may exist for any provider id this module used.

    If a regression made the mask / empty / non-string ``api_key`` reach the SecretStore, the
    entry would be our own ``e2e-`` id; remove it so the real credential store is left clean,
    then fail.
    """
    yield
    try:
        import keyring
    except ImportError:  # pragma: no cover
        return
    leaked: list[str] = []
    for pid in _PROVIDER_IDS:
        try:
            if keyring.get_password("qai.model_catalog.provider", pid) is not None:
                leaked.append(pid)
                keyring.delete_password("qai.model_catalog.provider", pid)
        except Exception:  # noqa: BLE001 - an unreadable keyring cannot have been written by us
            pass
    assert not leaked, f"an api_key that must not be stored reached the OS keyring for: {leaked}"


def _put_provider(api: httpx.Client, pid: str, config: dict) -> httpx.Response:
    _PROVIDER_IDS.append(pid)
    return api.put(f"{M}/providers/{quote(pid, safe='')}", json={"config": config})


def _provider(api: httpx.Client, pid: str) -> dict | None:
    rows = [p for p in api.get(f"{M}/providers").json()["providers"] if p["provider_id"] == pid]
    return rows[0]["config"] if rows else None


# --------------------------------------------------------------------------
# MH-01 - entries
# --------------------------------------------------------------------------


def test_list_has_stable_shape_and_sorted_order(api: httpx.Client, make_entry: Callable[..., dict]) -> None:
    tag = _uid("e2e-ord")
    # SQLite's default collation is binary: upper-case sorts before lower-case.
    for suffix, name in (("z", "Zed"), ("a1", "alpha"), ("a2", "Alpha"), ("m", "Mid")):
        make_entry(f"{tag}-{suffix}", name=name)
    r = api.get(f"{M}/entries")
    assert r.status_code == 200
    rows = r.json()["entries"]
    mine = [(e["name"], e["model_id"]) for e in rows if e["model_id"].startswith(tag)]
    assert mine == [("Alpha", f"{tag}-a2"), ("Mid", f"{tag}-m"), ("Zed", f"{tag}-z"), ("alpha", f"{tag}-a1")]
    assert [(e["name"], e["model_id"]) for e in rows] == sorted((e["name"], e["model_id"]) for e in rows)
    for e in rows:
        assert set(e) == {"model_id", "name", "provider", "source_url", "description", "tags",
                          "current_version_id", "versions"}
        assert e["provider"] in {"local", "ollama", "openai_compat", "anthropic", "generic_cloud"}


def test_register_get_list_remove_round_trip(api: httpx.Client, make_entry: Callable[..., dict]) -> None:
    mid = _uid()
    created = make_entry(mid, name="Round Trip", description="desc", tags=["Vision", "LLM"],
                         source_url="http://127.0.0.1:9/x")
    assert created == {
        "model_id": mid, "name": "Round Trip", "provider": "local", "source_url": "http://127.0.0.1:9/x",
        "description": "desc", "tags": ["llm", "vision"],  # lower-cased AND sorted
        "current_version_id": None, "versions": [],
    }
    assert api.get(f"{M}/entries/{mid}").json() == created
    assert created in api.get(f"{M}/entries").json()["entries"]
    assert api.get(f"{M}/entries/{mid}/versions").json() == {"model_id": mid, "versions": []}
    assert api.delete(f"{M}/entries/{mid}").status_code == 204
    assert api.get(f"{M}/entries/{mid}").status_code == 404
    assert mid not in [e["model_id"] for e in api.get(f"{M}/entries").json()["entries"]]


def test_register_duplicate_is_409_and_keeps_the_original(api: httpx.Client, make_entry: Callable[..., dict]) -> None:
    mid = _uid()
    make_entry(mid, name="Original")
    r = api.post(f"{M}/entries", json=_entry_body(mid, name="Imposter"))
    assert r.status_code == 409 and r.json()["code"] == "model_catalog.entry_conflict"
    assert api.get(f"{M}/entries/{mid}").json()["name"] == "Original"


@pytest.mark.parametrize("provider", ["local", "ollama", "openai_compat", "anthropic", "generic_cloud"])
def test_every_provider_kind_is_accepted(api: httpx.Client, make_entry: Callable[..., dict], provider: str) -> None:
    assert make_entry(provider=provider)["provider"] == provider


@pytest.mark.parametrize(
    ("over", "field"),
    [
        ({"provider": "bogus"}, None),
        ({"model_id": ""}, "body.model_id"),
        ({"model_id": "a" * 129}, "body.model_id"),
        ({"name": ""}, "body.name"),
        ({"name": "n" * 256}, "body.name"),
        ({"source_url": ""}, "body.source_url"),
        ({"source_url": "h" * 2049}, "body.source_url"),
        ({"description": "d" * 4097}, "body.description"),
    ],
)
def test_register_rejects_invalid_input_with_400(api: httpx.Client, over: dict, field: str | None) -> None:
    body = _entry_body(_uid(), **over) if "model_id" not in over else {**_entry_body("x"), **over}
    r = api.post(f"{M}/entries", json=body)
    assert r.status_code == 400, r.text
    if field is None:
        assert r.json()["code"] == "model_catalog.invalid_provider"
    else:
        assert r.json()["code"] == "request.validation" and field in r.json()["details"]["field_errors"]


def test_register_length_boundaries_are_inclusive(api: httpx.Client, make_entry: Callable[..., dict]) -> None:
    assert make_entry("b" * 128)["model_id"] == "b" * 128
    assert make_entry(name="n" * 255)["name"] == "n" * 255
    assert make_entry(description="d" * 4096)["description"] == "d" * 4096


@pytest.mark.parametrize(
    ("tags", "code"),
    [([""], "validation.string.empty"), (["x" * 65], "validation.string.max_length")],
)
def test_register_rejects_bad_tags_with_400(api: httpx.Client, tags: list[str], code: str) -> None:
    r = api.post(f"{M}/entries", json=_entry_body(_uid(), tags=tags))
    assert r.status_code == 400 and r.json()["code"] == code


@pytest.mark.parametrize(
    "over",
    [
        {"source_url": "not a url"},
        {"source_url": "file:///C:/Windows/win.ini"},
        {"source_url": "ftp://example.invalid/x"},
        {"tags": ["A", "a"]},
    ],
)
def test_register_domain_errors_are_500_not_400(api: httpx.Client, over: dict) -> None:
    """KNOWN GAP (current behaviour, pinned): a domain ``ValueError`` is not mapped to a 4xx.

    A client error (bad URL scheme, duplicate tag) is reported as ``internal.unexpected``.
    If this starts returning 400 that is an improvement - update this test.
    """
    mid = _uid()
    r = api.post(f"{M}/entries", json=_entry_body(mid, **over))
    assert r.status_code == 500 and r.json()["code"] == "internal.unexpected"
    assert api.get(f"{M}/entries/{mid}").status_code == 404, "a rejected registration must not persist"


def test_only_http_and_https_sources_are_accepted(api: httpx.Client, make_entry: Callable[..., dict]) -> None:
    assert make_entry(source_url="http://127.0.0.1/x")["source_url"] == "http://127.0.0.1/x"
    assert make_entry(source_url="https://example.invalid/x")["source_url"] == "https://example.invalid/x"


@pytest.mark.parametrize("raw", ["e2e 模型", "e2e?q", "e2e#h", "e2e%pct"])
def test_ids_with_url_special_characters_work_when_encoded(
    api: httpx.Client, make_entry: Callable[..., dict], raw: str
) -> None:
    mid = f"{_uid()}-{raw}"
    make_entry(mid)
    enc = quote(mid, safe="")
    assert api.get(f"{M}/entries/{enc}").json()["model_id"] == mid
    assert api.delete(f"{M}/entries/{enc}").status_code == 204


def test_an_id_with_a_slash_can_be_created_but_never_addressed(
    api: httpx.Client, make_entry: Callable[..., dict]
) -> None:
    """KNOWN GAP (current behaviour, pinned): ``POST`` accepts ``/`` in ``model_id``, but the item
    routes cannot reach it, so over HTTP it can never be read back or deleted (only by SQL).
    If either becomes possible (or ``POST`` starts rejecting it), update this test."""
    mid = f"{_uid()}/slash"
    make_entry(mid)  # fixture removes it by SQL
    assert mid in [e["model_id"] for e in api.get(f"{M}/entries").json()["entries"]]
    enc = quote(mid, safe="")
    assert api.get(f"{M}/entries/{enc}").status_code == 404
    assert api.delete(f"{M}/entries/{enc}").status_code == 405


def test_unknown_entry_is_404_on_every_item_route(api: httpx.Client) -> None:
    ghost = _uid("e2e-ghost")
    for call in (
        lambda: api.get(f"{M}/entries/{ghost}"),
        lambda: api.delete(f"{M}/entries/{ghost}"),
        lambda: api.get(f"{M}/entries/{ghost}/versions"),
        lambda: api.delete(f"{M}/entries/{ghost}/versions/v1"),
        lambda: api.post(f"{M}/download", json={"model_id": ghost, "version_id": "v1", "target_filename": "x.bin"}),
        lambda: api.post(f"{M}/verify", json={"model_id": ghost, "version_id": "v1",
                                              "storage_category": "models", "storage_name": "x.bin"}),
    ):
        r = call()
        assert r.status_code == 404 and r.json()["code"] == "model_catalog.entry_not_found", r.text
        assert r.json()["details"]["resource_id"] == ghost


def test_unknown_version_of_a_real_entry_is_404(api: httpx.Client, make_entry: Callable[..., dict]) -> None:
    mid = make_entry()["model_id"]
    for r in (
        api.delete(f"{M}/entries/{mid}/versions/v-nope"),
        api.post(f"{M}/download", json={"model_id": mid, "version_id": "v-nope", "target_filename": "x.bin"}),
        api.post(f"{M}/verify", json={"model_id": mid, "version_id": "v-nope",
                                      "storage_category": "models", "storage_name": "x.bin"}),
    ):
        assert r.status_code == 404 and r.json()["code"] == "model_catalog.version_not_found", r.text


def test_versions_are_listed_ordered_and_consistent(
    api: httpx.Client, make_entry: Callable[..., dict], sql: Callable[..., list[tuple]]
) -> None:
    mid = make_entry()["model_id"]
    for vid, status in (("v-b", "published"), ("v-a", "installed"), ("v-c", "corrupted")):
        _seed_version(sql, mid, vid, status)
    sql("UPDATE model_catalog_entry SET current_version_id = ? WHERE id = ?", ("v-a", mid))
    entry = api.get(f"{M}/entries/{mid}").json()
    assert [(v["version_id"], v["status"]) for v in entry["versions"]] == [
        ("v-a", "installed"), ("v-b", "published"), ("v-c", "corrupted")]
    assert entry["current_version_id"] == "v-a"
    v = entry["versions"][0]
    assert v == {"version_id": "v-a", "parent_model_id": mid, "checksum_algorithm": "sha256",
                 "checksum_value": SHA, "size_bytes": 123, "manifest_url": "https://example.invalid/v",
                 "status": "installed"}
    assert api.get(f"{M}/entries/{mid}/versions").json() == {"model_id": mid, "versions": entry["versions"]}
    listed = next(e for e in api.get(f"{M}/entries").json()["entries"] if e["model_id"] == mid)
    assert listed["versions"] == entry["versions"]


def test_removing_a_version_clears_current_when_it_was_current(
    api: httpx.Client, make_entry: Callable[..., dict], sql: Callable[..., list[tuple]]
) -> None:
    mid = make_entry()["model_id"]
    for vid in ("v-keep", "v-cur"):
        _seed_version(sql, mid, vid, "installed")
    sql("UPDATE model_catalog_entry SET current_version_id = ? WHERE id = ?", ("v-cur", mid))
    assert api.delete(f"{M}/entries/{mid}/versions/v-keep").status_code == 204
    e = api.get(f"{M}/entries/{mid}").json()
    assert [v["version_id"] for v in e["versions"]] == ["v-cur"] and e["current_version_id"] == "v-cur"
    assert api.delete(f"{M}/entries/{mid}/versions/v-cur").status_code == 204
    e = api.get(f"{M}/entries/{mid}").json()
    assert e["versions"] == [] and e["current_version_id"] is None, "no dangling current_version_id"
    assert api.get(f"{M}/entries/{mid}").status_code == 200, "removing a version keeps the entry"


def test_removing_an_entry_cascades_to_its_versions(
    api: httpx.Client, make_entry: Callable[..., dict], sql: Callable[..., list[tuple]]
) -> None:
    mid = make_entry()["model_id"]
    _seed_version(sql, mid, f"{mid}-v1")
    _seed_version(sql, mid, f"{mid}-v2", "installed")
    assert len(sql("SELECT id FROM model_catalog_version WHERE parent_model_id = ?", (mid,))) == 2
    assert api.delete(f"{M}/entries/{mid}").status_code == 204
    assert sql("SELECT id FROM model_catalog_version WHERE parent_model_id = ?", (mid,)) == []


def test_database_constraints_back_up_the_api(sql: Callable[..., list[tuple]]) -> None:
    """Defence in depth: the schema itself refuses a bad scheme / provider / checksum, even if a
    caller bypassed the API."""
    ins = ("INSERT INTO model_catalog_entry (id,name,provider,source_url,description,taxonomy_tags_json,"
           "current_version_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)")
    for args in ((_uid(), "n", "local", "ftp://x", "", "[]", None, "t", "t"),
                 (_uid(), "n", "bogus", "http://x", "", "[]", None, "t", "t"),
                 (_uid(), "", "local", "http://x", "", "[]", None, "t", "t")):
        with pytest.raises(sqlite3.IntegrityError):
            sql(ins, args)


def test_a_corrupt_row_takes_down_the_whole_list(
    api: httpx.Client, make_entry: Callable[..., dict], sql: Callable[..., list[tuple]]
) -> None:
    """KNOWN GAP (current behaviour, pinned): one undecodable row makes ``GET /entries`` 503 for
    everyone (and that row's own ``GET`` too). If listing becomes tolerant, update this test."""
    good = make_entry()["model_id"]
    bad = _uid("e2e-corrupt")
    sql("INSERT INTO model_catalog_entry (id,name,provider,source_url,description,taxonomy_tags_json,"
        "current_version_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (bad, "n", "local", "http://x", "", "not json", None, "t", "t"))
    try:
        r = api.get(f"{M}/entries")
        assert r.status_code == 503 and r.json()["code"] == "model_catalog.entry.tags_corrupt"
        assert api.get(f"{M}/entries/{bad}").status_code == 503
        assert api.get(f"{M}/entries/{good}").status_code == 200, "an intact entry is still readable by id"
    finally:
        sql("DELETE FROM model_catalog_entry WHERE id = ?", (bad,))
    assert api.get(f"{M}/entries").status_code == 200


# --------------------------------------------------------------------------
# download / verify - the edges that need neither network nor files
# --------------------------------------------------------------------------


def test_download_jobs_endpoints_for_unknown_jobs(api: httpx.Client) -> None:
    assert isinstance(api.get(f"{M}/download-jobs").json()["jobs"], list)
    ghost = _uid("e2e-job")
    for r in (api.delete(f"{M}/download/{ghost}"), api.get(f"{M}/download/{ghost}/progress")):
        assert r.status_code == 404 and r.json()["code"] == "model_catalog.download_job_not_found"


@pytest.mark.parametrize(
    ("over", "field"),
    [({"model_id": ""}, "model_id"), ({"version_id": ""}, "version_id"), ({"target_filename": ""}, "target_filename"),
     ({"target_filename": "f" * 256}, "target_filename")],
)
def test_download_request_validation(api: httpx.Client, over: dict, field: str) -> None:
    r = api.post(f"{M}/download", json={"model_id": "m", "version_id": "v", "target_filename": "x.bin", **over})
    assert r.status_code == 400 and f"body.{field}" in r.json()["details"]["field_errors"]


def test_verify_request_validation_and_domain_errors(api: httpx.Client, make_entry: Callable[..., dict]) -> None:
    mid = make_entry()["model_id"]
    ok = {"model_id": mid, "version_id": "v-nope", "storage_category": "models", "storage_name": "x.bin"}
    r = api.post(f"{M}/verify", json={**ok, "storage_name": "n" * 256})
    assert r.status_code == 400 and "body.storage_name" in r.json()["details"]["field_errors"]
    assert api.post(f"{M}/verify", json={k: v for k, v in ok.items() if k != "version_id"}).status_code == 400
    # KNOWN GAP (pinned): domain ValueErrors from the storage key are 500, not 400.
    for over in ({"storage_category": "Bad Cat"}, {"storage_name": "../x"}, {"storage_name": "a/b"}):
        r = api.post(f"{M}/verify", json={**ok, **over})
        assert r.status_code == 500 and r.json()["code"] == "internal.unexpected", over


# --------------------------------------------------------------------------
# MH-03 - providers and the derived cloud-model list
# --------------------------------------------------------------------------


def test_providers_list_shape(api: httpx.Client) -> None:
    r = api.get(f"{M}/providers")
    assert r.status_code == 200
    for p in r.json()["providers"]:
        assert set(p) == {"provider_id", "config"} and isinstance(p["config"], dict)


def test_put_provider_stores_the_config_and_returns_it(api: httpx.Client, sql: Callable[..., list[tuple]]) -> None:
    pid = _uid("e2e-prov")
    cfg = {"base_url": "https://example.invalid/v1", "pinned": False,
           "models": [{"model_id": f"{pid}::m1", "name": "M One", "context_length": 8192}]}
    r = _put_provider(api, pid, cfg)
    assert r.status_code == 200 and r.json() == {"provider_id": pid, "config": cfg}
    stored = _provider(api, pid)
    assert stored is not None
    assert {k: stored[k] for k in cfg} == cfg
    # Derived, read-only fields the UI uses; never the secret itself.
    assert stored["auth_mode"] == "api_key" and isinstance(stored["has_api_key"], bool)
    row = sql("SELECT value_json FROM kv_user_prefs WHERE key = ?", (f"model_catalog.provider.{pid}",))
    assert len(row) == 1 and '"api_key"' not in row[0][0]


def test_providers_list_only_contains_provider_rows(api: httpx.Client, sql: Callable[..., list[tuple]]) -> None:
    """``kv_user_prefs`` is a shared table. A key that merely CONTAINS the provider prefix, or has a
    different one, must not show up as a provider (the id is the key with the prefix SLICED off, so a
    leaked stray row would appear as a mangled, truncated id - assert on the exact set instead)."""
    tag = _uid("e2e-only")
    pid = f"{tag}-real"
    _put_provider(api, pid, {"a": 1})
    stray = (f"x.model_catalog.provider.{tag}-stray", f"e2e.unrelated.{tag}")
    for key in stray:
        sql("INSERT INTO kv_user_prefs (key,value_json,updated_at) VALUES (?,?,?)", (key, '{"z":1}', "t"))
    try:
        rows = api.get(f"{M}/providers").json()["providers"]
        # Every row whose id mentions this test's tag, however mangled, must be the one real provider.
        assert [p["provider_id"] for p in rows if tag in p["provider_id"] or "stray" in p["provider_id"]] == [pid]
        assert len(rows) == len({p["provider_id"] for p in rows}), "duplicate provider ids listed"
    finally:
        for key in stray:
            sql("DELETE FROM kv_user_prefs WHERE key = ?", (key,))


def test_provider_rows_are_listed_in_id_order(api: httpx.Client) -> None:
    tag = _uid("e2e-pord")
    for s in ("c", "a", "b"):
        _put_provider(api, f"{tag}-{s}", {})
    ids = [p["provider_id"] for p in api.get(f"{M}/providers").json()["providers"] if p["provider_id"].startswith(tag)]
    assert ids == [f"{tag}-a", f"{tag}-b", f"{tag}-c"]


def test_put_provider_replaces_the_whole_config(api: httpx.Client) -> None:
    pid = _uid("e2e-prov")
    _put_provider(api, pid, {"base_url": "https://a.invalid", "pinned": True, "models": [{"model_id": f"{pid}::m"}]})
    _put_provider(api, pid, {"base_url": "https://b.invalid"})
    cfg = _provider(api, pid)
    assert cfg is not None and cfg["base_url"] == "https://b.invalid"
    assert "models" not in cfg and "pinned" not in cfg, "PUT is a replace, not a merge"
    assert [m for m in api.get(f"{M}/cloud-models").json()["models"] if m["provider"] == pid] == []


@pytest.mark.parametrize("secret_like", ["", "****", 123])
def test_api_key_values_that_must_not_be_stored_are_stripped(
    api: httpx.Client, sql: Callable[..., list[tuple]], secret_like: object
) -> None:
    """``""`` / the ``****`` mask / a non-string never reach the SecretStore, and ``api_key`` is
    never persisted in the config document either way (guarded by ``keyring_untouched``).

    The PUT response is the request body echoed back verbatim: the use case works on a COPY, so the
    caller's ``api_key`` is still there (a use case that popped from the caller's dict would make the
    echo silently lose a field the client sent).
    """
    pid = _uid("e2e-prov")
    sent = {"base_url": "https://x.invalid", "api_key": secret_like}
    r = _put_provider(api, pid, dict(sent))
    assert r.status_code == 200
    assert r.json() == {"provider_id": pid, "config": sent}
    row = sql("SELECT value_json FROM kv_user_prefs WHERE key = ?", (f"model_catalog.provider.{pid}",))
    assert '"api_key"' not in row[0][0]
    cfg = _provider(api, pid)
    assert cfg is not None and "api_key" not in cfg


def test_a_legacy_plaintext_api_key_is_masked_on_read(api: httpx.Client, sql: Callable[..., list[tuple]]) -> None:
    """A row written before the SecretStore existed may still hold a plaintext key; GET must never echo it."""
    pid = _uid("e2e-legacy")
    _PROVIDER_IDS.append(pid)
    plaintext = f"sk-e2e-not-a-real-key-{uuid.uuid4().hex}"
    sql("INSERT INTO kv_user_prefs (key,value_json,updated_at) VALUES (?,?,?)",
        (f"model_catalog.provider.{pid}", '{"base_url":"https://x.invalid","api_key":"%s"}' % plaintext, "t"))
    try:
        r = api.get(f"{M}/providers")
        assert plaintext not in r.text, "a plaintext api_key was returned by GET /providers"
        assert _provider(api, pid)["api_key"] == "****"
        sql("UPDATE kv_user_prefs SET value_json = ? WHERE key = ?",
            ('{"base_url":"https://x.invalid","api_key":""}', f"model_catalog.provider.{pid}"))
        assert _provider(api, pid)["api_key"] == "", "an empty key is shown as empty, not masked"
    finally:
        sql("DELETE FROM kv_user_prefs WHERE key = ?", (f"model_catalog.provider.{pid}",))


def test_auth_mode_is_derived_from_the_provider_id(api: httpx.Client, sql: Callable[..., list[tuple]]) -> None:
    pid = _uid("e2e-prov")
    _put_provider(api, pid, {})
    assert _provider(api, pid)["auth_mode"] == "api_key"
    # The reserved QAI Service id is keyless (SSO). This writes ONLY the throw-away database (an
    # empty config, no api_key => nothing reaches the keyring); the row is removed afterwards so the
    # shared module backend is left as it was.
    _put_provider(api, "qai-service", {})
    try:
        cfg = _provider(api, "qai-service")
        assert cfg is not None and cfg["auth_mode"] == "sso"
    finally:
        sql("DELETE FROM kv_user_prefs WHERE key = ?", ("model_catalog.provider.qai-service",))


def test_provider_request_validation(api: httpx.Client) -> None:
    pid = _uid("e2e-prov")
    for body in ({}, {"config": None}, {"config": [1]}, {"config": "x"}):
        r = api.put(f"{M}/providers/{pid}", json=body)
        assert r.status_code == 400 and r.json()["code"] == "request.validation", body
    assert _provider(api, pid) is None, "a rejected PUT must not create the row"
    r = api.put(f"{M}/providers/{'p' * 129}", json={"config": {}})
    assert r.status_code == 400 and r.json()["code"] == "model_catalog.provider_config_invalid"


def test_provider_id_length_limit_is_lower_than_advertised(api: httpx.Client) -> None:
    """KNOWN GAP (current behaviour, pinned): 128 is advertised, but the row key carries a 23-char
    prefix and ``kv_user_prefs.key`` is capped at 128, so 106..128 fail with 503.
    If the limits are reconciled, update this test."""
    assert _put_provider(api, "p" * 105, {}).status_code == 200
    for n in (106, 128):
        r = _put_provider(api, "q" * n, {})
        assert r.status_code == 503 and r.json()["code"] == "model_catalog.provider.save_failed", n


def test_providers_cannot_be_deleted_over_http(api: httpx.Client) -> None:
    """KNOWN GAP (current behaviour, pinned): no delete route; "removing" leaves an empty row listed
    (the CLI's ``provider remove`` does the same and additionally drops the stored key)."""
    pid = _uid("e2e-prov")
    _put_provider(api, pid, {"models": [{"model_id": f"{pid}::m"}]})
    assert api.delete(f"{M}/providers/{pid}").status_code == 405
    assert api.get(f"{M}/providers/{pid}").status_code in (404, 405)
    _put_provider(api, pid, {})
    cfg = _provider(api, pid)
    assert cfg is not None and "models" not in cfg


def test_provider_ids_with_spaces_and_unicode(api: httpx.Client) -> None:
    pid = f"{_uid('e2e')} 供应商"
    assert _put_provider(api, pid, {"a": 1}).json() == {"provider_id": pid, "config": {"a": 1}}
    assert _provider(api, pid)["a"] == 1


def test_cloud_models_flattens_providers_with_full_projection(api: httpx.Client) -> None:
    pid = _uid("e2e-prov")
    _put_provider(api, pid, {"models": [
        {"model_id": f"{pid}::m1", "name": "M One", "context_length": 8192, "description": "d",
         "supports_streaming": False, "api_model_id": "m1-wire", "params": {"temperature": 0.2}},
        {"model_id": f"{pid}::m2"},
    ]})
    mine = {m["model_id"]: m for m in api.get(f"{M}/cloud-models").json()["models"] if m["provider"] == pid}
    assert mine[f"{pid}::m1"] == {
        "model_id": f"{pid}::m1", "name": "M One", "provider": pid, "context_length": 8192, "description": "d",
        "supports_streaming": False, "is_local": False, "api_model_id": "m1-wire", "params": {"temperature": 0.2}}
    assert mine[f"{pid}::m2"] == {  # defaults for everything omitted
        "model_id": f"{pid}::m2", "name": f"{pid}::m2", "provider": pid, "context_length": None, "description": "",
        "supports_streaming": True, "is_local": False, "api_model_id": None, "params": None}


def test_cloud_models_never_contain_credentials(api: httpx.Client) -> None:
    pid = _uid("e2e-prov")
    _put_provider(api, pid, {"base_url": "https://x.invalid", "api_key": "****", "models": [{"model_id": f"{pid}::m"}]})
    text = api.get(f"{M}/cloud-models").text
    assert "api_key" not in text and "base_url" not in text, "only the catalog shape may be exposed"


def test_cloud_models_orders_pinned_providers_first(api: httpx.Client) -> None:
    tag = _uid("e2e-ord")
    _put_provider(api, f"{tag}-b", {"models": [{"model_id": f"{tag}-b::x"}]})
    _put_provider(api, f"{tag}-a", {"pinned": True, "models": [{"model_id": f"{tag}-a::x"}]})
    _put_provider(api, f"{tag}-c", {"models": [{"model_id": f"{tag}-c::x"}]})
    ids = [m["model_id"] for m in api.get(f"{M}/cloud-models").json()["models"] if m["model_id"].startswith(tag)]
    assert ids == [f"{tag}-a::x", f"{tag}-b::x", f"{tag}-c::x"]  # pinned first, then provider-id order


@pytest.mark.parametrize("bad", ["maybe", "yes", 2, None, [True]])
def test_a_non_boolean_supports_streaming_cannot_break_the_whole_list(api: httpx.Client, bad: object) -> None:
    """The use case coerces a non-bool to True BEFORE the response model sees it. Without that, the
    response model's ``bool`` rejects values such as ``"maybe"`` / ``2`` / ``None`` and one badly
    edited provider turns ``GET /cloud-models`` into a 500 for EVERY provider (the chat model
    picker would go blank)."""
    bad_pid, good_pid = _uid("e2e-prov"), _uid("e2e-prov")
    _put_provider(api, bad_pid, {"models": [{"model_id": f"{bad_pid}::m", "supports_streaming": bad}]})
    _put_provider(api, good_pid, {"models": [{"model_id": f"{good_pid}::m"}]})
    r = api.get(f"{M}/cloud-models")
    assert r.status_code == 200, r.text
    by_id = {m["model_id"]: m for m in r.json()["models"]}
    assert by_id[f"{bad_pid}::m"]["supports_streaming"] is True, "defaults to True when not a real boolean"
    assert f"{good_pid}::m" in by_id, "a good provider must survive a bad neighbour"


def test_cloud_models_drops_malformed_rows_and_coerces_bad_fields(api: httpx.Client) -> None:
    pid = _uid("e2e-prov")
    _put_provider(api, pid, {"models": [
        "string", None, 5, {"name": "no id"}, {"model_id": ""}, {"model_id": 7},
        {"model_id": f"{pid}::coerce", "name": 123, "context_length": "8k", "description": 9,
         "supports_streaming": "yes", "api_model_id": "", "params": [1]},
    ]})
    mine = [m for m in api.get(f"{M}/cloud-models").json()["models"] if m["provider"] == pid]
    assert [m["model_id"] for m in mine] == [f"{pid}::coerce"], "one bad row must not blank the list"
    m = mine[0]
    assert (m["name"], m["context_length"], m["description"], m["supports_streaming"]) == (f"{pid}::coerce", None, "", True)
    assert m["api_model_id"] is None and m["params"] is None


def test_cloud_models_ignores_providers_without_a_model_list(api: httpx.Client) -> None:
    pid = _uid("e2e-prov")
    _put_provider(api, pid, {"models": {"model_id": "x"}})
    assert [m for m in api.get(f"{M}/cloud-models").json()["models"] if m["provider"] == pid] == []


def test_pinned_is_truthiness_not_a_strict_boolean(api: httpx.Client) -> None:
    """KNOWN GAP (current behaviour, pinned): ``bool("false")`` is True, so the STRING "false"
    pins a provider, and ``context_length: true`` is read as the integer 1.
    If strict typing is added, update this test."""
    tag = _uid("e2e-ord")
    _put_provider(api, f"{tag}-z", {"models": [{"model_id": f"{tag}-z::x"}]})
    _put_provider(api, f"{tag}-a", {"pinned": "false", "models": [{"model_id": f"{tag}-a::x", "context_length": True}]})
    ms = [m for m in api.get(f"{M}/cloud-models").json()["models"] if m["model_id"].startswith(tag)]
    assert [m["model_id"] for m in ms] == [f"{tag}-a::x", f"{tag}-z::x"]
    assert ms[0]["context_length"] == 1


def test_cloud_model_permissions_snapshot_shape(api: httpx.Client) -> None:
    r = api.get(f"{M}/cloud-models/permissions")
    assert r.status_code == 200
    perms = r.json()["permissions"]
    assert isinstance(perms, dict)
    for per_model in perms.values():
        assert set(per_model.values()) <= PERMISSION_VALUES


def test_external_edition_hides_internal_only_surfaces(api: httpx.Client) -> None:
    ed = api.get("/api/system/edition").json()
    if ed.get("is_internal"):
        pytest.skip("internal edition: the qgenie / query-services surfaces are live and need the network")
    q = api.get(f"{M}/qgenie-quota")
    assert q.status_code == 200
    body = q.json()
    assert body["available"] is False and body["error"] == "not_configured" and body["models"] == {}
    assert body["cost"] is None
    assert api.get(f"{M}/qgenie-quota", params={"force": "true"}).json()["available"] is False
    assert api.get(f"{M}/query-services").json() == {"services": []}


@pytest.mark.parametrize(
    ("method", "path"),
    [("post", f"{M}/providers"), ("delete", f"{M}/providers"), ("put", f"{M}/entries"),
     ("patch", f"{M}/entries/x"), ("post", f"{M}/cloud-models")],
)
def test_wrong_methods_are_405(api: httpx.Client, method: str, path: str) -> None:
    assert getattr(api, method)(path).status_code == 405
