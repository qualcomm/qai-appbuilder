# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - search-engine management API ``/api/search/engines`` (SRCH-01).

These endpoints back the Agent settings "search source" group: the engine roster (``search_config.toml``) joined with the
health-score table, the per-engine manual override, the credential slot of keyed engines and the monthly quota. They read and
write the LOCAL database and secret store only. **Nothing here runs a search or touches the network** (``/status`` is documented
as "score state only, no live probing"), so SRCH-01's ``NET`` dependency in the inventory was a mislabel.

NOT covered (deliberately):

* **Running a search** (HTTP scrapers, keyed APIs, the Playwright ``google_browser`` engine, the fusion / deadlines): that needs
  the network or a browser and is a different feature.
* **Storing a credential**: ``PUT /{id}/credential`` with a non-empty key writes the OS keyring (Windows Credential Manager is NOT
  isolated), which the owner ruled out for the formal suite. Only the branches that are rejected BEFORE the secret store are sent
  (unknown engine, engine without a credential slot, empty / blank key, missing body). ``DELETE /{id}/credential`` for a keyed engine
  is likewise not sent. The module asserts the keyring holds no ``web_*`` entry before and after.
* How scores move on real searches (success / failure counting) and the browser engine's runtime (``browser_unavail`` skip).

Isolation (verified): one throw-away backend per test class of scenarios (``owned_backend``: temp data dir, port 8989); no
engine is ever contacted; the OS keyring is only READ.

Observed behaviour worth pinning:

* The roster has 21 engines. Defaults: ``google_browser``, ``mojeek`` and ``keenable`` are disabled by default (``manual_state`` ``forced_off``,
  status ``disabled``); every engine that requires a credential and has none stored shows ``needs_config`` whatever its manual state;
  keyless / no-credential engines (baidu, bing, duckduckgo, exa, firecrawl) are ``enabled``.
* ``manual_state`` accepts exactly ``auto`` / ``forced_on`` / ``forced_off`` (case-sensitive); anything else is 422, a missing or
  non-string state is 400. Forcing a default-off engine on makes it ``enabled``; ``auto`` afterwards keeps it ``enabled`` (the seed
  state only applies to an engine with no stored row).
* Quota ``PUT`` rejects ``monthly_limit < 1`` with 422 and non-integers with 400; the quota is also shown inline in the list for keyed
  engines only (``null`` for the others).
* State survives a backend restart on the same data dir (manual state, quota limit, quota notify flag).
* **Questionable, pinned as observed (SRCH-ISS-01):** the mutating routes accept ANY engine id. ``POST /no-such-engine/manual_state``
  answers 200 and stores a row; ``DELETE /no-such-engine/score`` answers 200; ``GET`` / ``PUT /no-such-engine/quota`` answer 200 with
  a default 1000 / the stored limit - for a keyless engine too (``bing`` has a quota row although the list shows ``null``). The
  unknown id never appears in the list or ``/status`` (they iterate the roster), but ``/quota`` lists every stored quota row.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator

import httpx
import pytest

from tests.e2e._owned_backend import OwnedBackend, owned_backend

pytestmark = pytest.mark.e2e

P = "/api/search/engines"
ROW_KEYS = {
    "engine_id", "display_name", "engine_type", "requires_credential", "credential_key", "enabled_by_default", "description_i18n_key",
    "priority_hint", "score", "manual_state", "success_rate", "status", "quota_usage", "quota_limit", "quota_remaining",
    "quota_exhausted", "quota_notify_enabled",
}
ENGINE_TYPES = {"http_keyless", "http_api", "browser"}
DEFAULT_OFF = {"google_browser", "mojeek", "keenable"}
NO_CREDENTIAL_ENABLED = {"baidu", "bing", "duckduckgo", "exa", "firecrawl"}
STATES = ("auto", "forced_on", "forced_off")
UNKNOWN = "e2e-no-such-engine"


def _keyring_web_entries() -> dict[str, bool] | None:
    """READ-ONLY: which ``web_*`` credential slots hold a value. ``None`` when the keyring cannot be read."""
    try:
        import keyring
    except ImportError:
        return None
    out: dict[str, bool] = {}
    for key in ("web_brave", "web_serpapi", "web_tavily", "web_gemini", "web_kagi", "web_jina", "web_tinyfish", "web_parallel",
                "web_synthetic", "web_kimi", "web_zai", "web_xai", "web_anthropic", "web_exa", "web_firecrawl"):
        try:
            out[key] = keyring.get_password("qai.model_catalog.provider", key) is not None
        except Exception:  # noqa: BLE001 - an unreadable keyring cannot have been written by this module
            return None
    return out


_KEYRING_AT_START = _keyring_web_entries()


@pytest.fixture(scope="module", autouse=True)
def keyring_untouched() -> Iterator[None]:
    yield
    assert _keyring_web_entries() == _KEYRING_AT_START, "a web-search credential appeared in / vanished from the OS keyring"


@pytest.fixture(scope="module")
def srch() -> Iterator[tuple[OwnedBackend, httpx.Client]]:
    with owned_backend() as ob:
        yield ob, ob.start()


@pytest.fixture
def cl(srch: tuple[OwnedBackend, httpx.Client]) -> httpx.Client:
    """The shared client, with the state the tests change put back first (the backend itself is shared by the module)."""
    c = srch[1]
    for eid in ("brave", "mojeek", "google_browser", "bing", "baidu"):
        c.post(f"{P}/{eid}/manual_state", json={"state": "auto" if eid not in DEFAULT_OFF else "forced_off"})
    c.put(f"{P}/brave/quota", json={"monthly_limit": 1000})
    c.put(f"{P}/brave/quota/notify", json={"enabled": True})
    return c


def _row(c: httpx.Client, eid: str) -> dict:
    rows = [e for e in c.get(P).json()["engines"] if e["engine_id"] == eid]
    assert len(rows) == 1, f"{eid}: {len(rows)} rows"
    return rows[0]


# --------------------------------------------------------------------------
# the list
# --------------------------------------------------------------------------


def test_list_returns_the_roster_with_a_stable_row_shape(cl: httpx.Client) -> None:
    r = cl.get(P)
    assert r.status_code == 200 and list(r.json()) == ["engines"]
    rows = r.json()["engines"]
    assert len(rows) == 21 and len({e["engine_id"] for e in rows}) == 21
    for e in rows:
        assert set(e) == ROW_KEYS, e["engine_id"]
        assert e["engine_type"] in ENGINE_TYPES and e["status"] in {"enabled", "disabled", "needs_config"}
        assert e["manual_state"] in STATES and isinstance(e["score"], int) and isinstance(e["priority_hint"], int)
        assert isinstance(e["success_rate"], float) and e["display_name"]


def test_exactly_one_browser_engine_is_listed_and_it_is_off_by_default(cl: httpx.Client) -> None:
    browsers = [e for e in cl.get(P).json()["engines"] if e["engine_type"] == "browser"]
    assert [b["engine_id"] for b in browsers] == ["google_browser"]
    b = browsers[0]
    assert (b["requires_credential"], b["enabled_by_default"], b["manual_state"], b["status"]) == (False, False, "forced_off", "disabled")


def test_default_off_engines_are_forced_off_and_disabled(cl: httpx.Client) -> None:
    for eid in DEFAULT_OFF:
        e = _row(cl, eid)
        assert (e["enabled_by_default"], e["manual_state"], e["status"]) == (False, "forced_off", "disabled"), eid


def test_keyed_engines_without_a_stored_key_need_config_and_show_a_quota(cl: httpx.Client) -> None:
    if _KEYRING_AT_START is None or any(_KEYRING_AT_START.values()):
        pytest.skip("the OS keyring already holds (or cannot be read for) a web-search key; 'needs_config' would not be observable")
    keyed = [e for e in cl.get(P).json()["engines"] if e["requires_credential"]]
    assert len(keyed) >= 10
    for e in keyed:
        assert e["credential_key"] == f"web_{e['engine_id']}", e["engine_id"]
        assert e["status"] == "needs_config", e["engine_id"]
        assert (e["quota_limit"], e["quota_usage"], e["quota_exhausted"]) == (1000, 0, False), e["engine_id"]


def test_engines_without_a_credential_slot_are_enabled_and_have_no_quota_in_the_list(cl: httpx.Client) -> None:
    for eid in NO_CREDENTIAL_ENABLED:
        e = _row(cl, eid)
        assert (e["requires_credential"], e["status"], e["manual_state"]) == (False, "enabled", "auto"), eid
        assert (e["quota_limit"], e["quota_usage"], e["quota_remaining"], e["quota_exhausted"], e["quota_notify_enabled"]) == (None,) * 5, eid


def test_status_endpoint_mirrors_the_list_without_quota(cl: httpx.Client) -> None:
    lst = {e["engine_id"]: e for e in cl.get(P).json()["engines"]}
    st = cl.get(f"{P}/status").json()["engines"]
    assert [s["engine_id"] for s in st] == list(lst)
    for s in st:
        assert set(s) == {"engine_id", "score", "manual_state", "enabled"}
        assert s["manual_state"] == lst[s["engine_id"]]["manual_state"] and s["score"] == lst[s["engine_id"]]["score"]


@pytest.mark.parametrize("method", ["post", "delete", "put"])
def test_the_collection_route_is_read_only(cl: httpx.Client, method: str) -> None:
    assert getattr(cl, method)(P).status_code == 405


# --------------------------------------------------------------------------
# manual state
# --------------------------------------------------------------------------


@pytest.mark.parametrize("state", STATES)
def test_manual_state_accepts_the_three_values_and_the_list_reflects_it(cl: httpx.Client, state: str) -> None:
    r = cl.post(f"{P}/bing/manual_state", json={"state": state})
    assert r.status_code == 200 and r.json() == {"engine_id": "bing", "manual_state": state, "score": 0}
    e = _row(cl, "bing")
    assert e["manual_state"] == state
    assert e["status"] == ("disabled" if state == "forced_off" else "enabled")


@pytest.mark.parametrize("state", ["bogus", "", "AUTO", "Forced_On", " auto", "on", "off"])
def test_an_unknown_manual_state_is_rejected_with_422_and_changes_nothing(cl: httpx.Client, state: str) -> None:
    cl.post(f"{P}/bing/manual_state", json={"state": "forced_off"})
    r = cl.post(f"{P}/bing/manual_state", json={"state": state})
    assert r.status_code == 422 and "must be one of" in r.json()["detail"]
    assert _row(cl, "bing")["manual_state"] == "forced_off"


@pytest.mark.parametrize("body", [{}, {"state": 123}, {"state": None}, {"state": ["auto"]}, {"status": "auto"}])
def test_a_missing_or_non_string_manual_state_is_a_400_validation_error(cl: httpx.Client, body: dict) -> None:
    r = cl.post(f"{P}/bing/manual_state", json=body)
    assert r.status_code == 400 and r.json()["code"] == "request.validation"


def test_forcing_a_default_off_engine_on_enables_it_and_auto_keeps_it_enabled(cl: httpx.Client) -> None:
    assert _row(cl, "mojeek")["status"] == "disabled"
    assert cl.post(f"{P}/mojeek/manual_state", json={"state": "forced_on"}).status_code == 200
    assert (_row(cl, "mojeek")["manual_state"], _row(cl, "mojeek")["status"]) == ("forced_on", "enabled")
    cl.post(f"{P}/mojeek/manual_state", json={"state": "auto"})
    assert (_row(cl, "mojeek")["manual_state"], _row(cl, "mojeek")["status"]) == ("auto", "enabled"), "the seed state only applies to an engine with no row"


def test_a_keyed_engine_without_a_key_stays_needs_config_whatever_its_manual_state(cl: httpx.Client) -> None:
    if _KEYRING_AT_START is None or _KEYRING_AT_START.get("web_brave"):
        pytest.skip("a real web_brave key is stored in the OS keyring")
    for state in STATES:
        cl.post(f"{P}/brave/manual_state", json={"state": state})
        assert (_row(cl, "brave")["manual_state"], _row(cl, "brave")["status"]) == (state, "needs_config")


def test_manual_state_does_not_disturb_other_engines(cl: httpx.Client) -> None:
    before = {e["engine_id"]: (e["manual_state"], e["status"]) for e in cl.get(P).json()["engines"]}
    cl.post(f"{P}/baidu/manual_state", json={"state": "forced_off"})
    after = {e["engine_id"]: (e["manual_state"], e["status"]) for e in cl.get(P).json()["engines"]}
    changed = {k for k in before if before[k] != after[k]}
    assert changed == {"baidu"}


def test_reset_score_keeps_the_manual_state_and_returns_the_snapshot(cl: httpx.Client) -> None:
    cl.post(f"{P}/bing/manual_state", json={"state": "forced_off"})
    r = cl.delete(f"{P}/bing/score")
    assert r.status_code == 200 and r.json() == {"engine_id": "bing", "manual_state": "forced_off", "score": 0}
    assert _row(cl, "bing")["manual_state"] == "forced_off"


# --------------------------------------------------------------------------
# credentials: only the branches rejected BEFORE the secret store
# --------------------------------------------------------------------------


def test_credential_for_an_unknown_engine_is_404(cl: httpx.Client) -> None:
    for call in (lambda: cl.put(f"{P}/{UNKNOWN}/credential", json={"api_key": "x"}), lambda: cl.delete(f"{P}/{UNKNOWN}/credential")):
        r = call()
        assert r.status_code == 404 and r.json() == {"detail": "unknown engine"}


@pytest.mark.parametrize("eid", ["bing", "baidu", "duckduckgo", "exa", "firecrawl", "mojeek", "google_browser"])
def test_an_engine_without_a_credential_slot_refuses_a_credential_with_400(cl: httpx.Client, eid: str) -> None:
    for call in (lambda: cl.put(f"{P}/{eid}/credential", json={"api_key": "x"}), lambda: cl.delete(f"{P}/{eid}/credential")):
        r = call()
        assert r.status_code == 400 and r.json() == {"detail": "engine does not accept a credential"}


@pytest.mark.parametrize("key", ["", "   ", "\t\n"])
def test_an_empty_or_blank_key_is_rejected_before_anything_is_stored(cl: httpx.Client, key: str) -> None:
    r = cl.put(f"{P}/brave/credential", json={"api_key": key})
    assert r.status_code == 400 and r.json() == {"detail": "api_key must not be empty"}


@pytest.mark.parametrize("body", [{}, {"api_key": None}, {"api_key": 5}, {"key": "x"}])
def test_a_missing_or_non_string_key_is_a_400_validation_error(cl: httpx.Client, body: dict) -> None:
    r = cl.put(f"{P}/brave/credential", json=body)
    assert r.status_code == 400 and r.json()["code"] == "request.validation"


# --------------------------------------------------------------------------
# quota
# --------------------------------------------------------------------------


def test_quota_shape_and_defaults(cl: httpx.Client) -> None:
    r = cl.get(f"{P}/brave/quota")
    assert r.status_code == 200
    q = r.json()
    assert set(q) == {"engine_id", "month_key", "usage_count", "monthly_limit", "notify_enabled", "remaining", "exhausted"}
    assert (q["engine_id"], q["usage_count"], q["monthly_limit"], q["remaining"], q["exhausted"], q["notify_enabled"]) == ("brave", 0, 1000, 1000, False, True)
    assert len(q["month_key"]) == 7 and q["month_key"][4] == "-"


@pytest.mark.parametrize("limit", [1, 5, 1000, 123456])
def test_setting_a_quota_limit_is_reflected_in_remaining_and_in_the_list(cl: httpx.Client, limit: int) -> None:
    r = cl.put(f"{P}/brave/quota", json={"monthly_limit": limit})
    assert r.status_code == 200 and (r.json()["monthly_limit"], r.json()["remaining"], r.json()["exhausted"]) == (limit, limit, False)
    assert cl.get(f"{P}/brave/quota").json()["monthly_limit"] == limit
    e = _row(cl, "brave")
    assert (e["quota_limit"], e["quota_remaining"], e["quota_exhausted"]) == (limit, limit, False)


@pytest.mark.parametrize("limit", [0, -1, -1000])
def test_a_quota_limit_below_one_is_422_and_changes_nothing(cl: httpx.Client, limit: int) -> None:
    cl.put(f"{P}/brave/quota", json={"monthly_limit": 7})
    r = cl.put(f"{P}/brave/quota", json={"monthly_limit": limit})
    assert r.status_code == 422 and r.json() == {"detail": "monthly_limit must be >= 1"}
    assert cl.get(f"{P}/brave/quota").json()["monthly_limit"] == 7


@pytest.mark.parametrize("value", ["x", 1.5, None, [3], {}])
def test_a_non_integer_quota_limit_is_a_400_validation_error(cl: httpx.Client, value: object) -> None:
    r = cl.put(f"{P}/brave/quota", json={"monthly_limit": value})
    assert r.status_code == 400 and r.json()["code"] == "request.validation"


def test_quota_notify_flag_round_trips_and_leaves_the_limit_alone(cl: httpx.Client) -> None:
    cl.put(f"{P}/brave/quota", json={"monthly_limit": 9})
    r = cl.put(f"{P}/brave/quota/notify", json={"enabled": False})
    assert r.status_code == 200 and (r.json()["notify_enabled"], r.json()["monthly_limit"]) == (False, 9)
    assert _row(cl, "brave")["quota_notify_enabled"] is False
    assert cl.put(f"{P}/brave/quota/notify", json={"enabled": True}).json()["notify_enabled"] is True


@pytest.mark.parametrize("body", [{}, {"enabled": "maybe"}, {"enabled": None}])
def test_a_bad_notify_body_is_a_400_validation_error(cl: httpx.Client, body: dict) -> None:
    r = cl.put(f"{P}/brave/quota/notify", json=body)
    assert r.status_code == 400 and r.json()["code"] == "request.validation"


def test_the_quota_collection_lists_only_engines_that_have_a_stored_row(cl: httpx.Client) -> None:
    cl.put(f"{P}/brave/quota", json={"monthly_limit": 11})
    ids = [q["engine_id"] for q in cl.get(f"{P}/quota").json()["quotas"]]
    assert "brave" in ids and len(ids) == len(set(ids))


# --------------------------------------------------------------------------
# pinned as observed: unknown ids are accepted (SRCH-ISS-01)
# --------------------------------------------------------------------------


def test_the_mutating_routes_accept_an_engine_id_that_is_not_in_the_roster(cl: httpx.Client) -> None:
    r = cl.post(f"{P}/{UNKNOWN}/manual_state", json={"state": "forced_on"})
    assert r.status_code == 200 and r.json()["engine_id"] == UNKNOWN
    assert cl.delete(f"{P}/{UNKNOWN}/score").status_code == 200
    assert cl.put(f"{P}/{UNKNOWN}/quota", json={"monthly_limit": 5}).json()["monthly_limit"] == 5
    assert cl.get(f"{P}/{UNKNOWN}/quota").status_code == 200


def test_an_unknown_engine_id_never_shows_up_in_the_list_or_status(cl: httpx.Client) -> None:
    cl.post(f"{P}/{UNKNOWN}/manual_state", json={"state": "forced_on"})
    assert UNKNOWN not in [e["engine_id"] for e in cl.get(P).json()["engines"]]
    assert UNKNOWN not in [e["engine_id"] for e in cl.get(f"{P}/status").json()["engines"]]
    assert len(cl.get(P).json()["engines"]) == 21


def test_a_keyless_engine_can_be_given_a_quota_row_although_the_list_hides_it(cl: httpx.Client) -> None:
    assert cl.put(f"{P}/bing/quota", json={"monthly_limit": 3}).status_code == 200
    assert cl.get(f"{P}/bing/quota").json()["monthly_limit"] == 3
    assert _row(cl, "bing")["quota_limit"] is None


@pytest.mark.parametrize("eid", ["..", "a%2Fb"])
def test_path_like_engine_ids_do_not_reach_the_handler(cl: httpx.Client, eid: str) -> None:
    assert cl.post(f"{P}/{eid}/manual_state", json={"state": "auto"}).status_code == 405


def test_a_long_and_a_non_ascii_engine_id_are_accepted_as_opaque_ids(cl: httpx.Client) -> None:
    for eid in ("x" * 300, "%E4%B8%AD"):
        r = cl.post(f"{P}/{eid}/manual_state", json={"state": "auto"})
        assert r.status_code == 200 and r.json()["manual_state"] == "auto"
    assert len(cl.get(P).json()["engines"]) == 21
