# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - search-engine management API: restart persistence and clean start (SRCH-01, second half).

Split out of ``test_search_engines.py`` because each test here starts its OWN backend (``owned_backend``), and a module must not hold a
module-scoped backend on port 8989 while its tests open another one. See that module for scope, the NOT-covered list and the
observed behaviour. Same rules: no search is run, the OS keyring is never written.
"""

from __future__ import annotations

import pytest

from tests.e2e._owned_backend import owned_backend

pytestmark = pytest.mark.e2e

P = "/api/search/engines"
DEFAULT_OFF = {"google_browser", "mojeek", "keenable"}


def _row(c, eid: str) -> dict:
    rows = [e for e in c.get(P).json()["engines"] if e["engine_id"] == eid]
    assert len(rows) == 1, f"{eid}: {len(rows)} rows"
    return rows[0]


def test_manual_state_and_quota_settings_survive_a_restart() -> None:
    with owned_backend() as ob:
        c = ob.start()
        c.post(f"{P}/bing/manual_state", json={"state": "forced_off"})
        c.post(f"{P}/mojeek/manual_state", json={"state": "forced_on"})
        c.put(f"{P}/brave/quota", json={"monthly_limit": 7})
        c.put(f"{P}/brave/quota/notify", json={"enabled": False})
        c.close()
        ob.stop()
        c = ob.start()
        assert (_row(c, "bing")["manual_state"], _row(c, "bing")["status"]) == ("forced_off", "disabled")
        assert (_row(c, "mojeek")["manual_state"], _row(c, "mojeek")["status"]) == ("forced_on", "enabled")
        b = _row(c, "brave")
        assert (b["quota_limit"], b["quota_notify_enabled"]) == (7, False)


def test_a_fresh_data_dir_starts_from_the_defaults() -> None:
    with owned_backend() as ob:
        c = ob.start()
        rows = {e["engine_id"]: e for e in c.get(P).json()["engines"]}
        assert {k for k, e in rows.items() if e["manual_state"] != "auto"} == DEFAULT_OFF
        assert c.get(f"{P}/quota").json() == {"quotas": []}
        assert all(e["score"] == 0 for e in rows.values())


def test_listing_and_changing_state_does_not_make_the_backend_talk_to_a_search_engine() -> None:
    """Compares the log before and after: the START-UP lines legitimately print the engine names (``web_search.independent.registered``)."""
    with owned_backend() as ob:
        c = ob.start()
        before = ob.log_text().splitlines()
        assert any("web_search.independent.registered" in line for line in before), "the start-up line this test relies on to prove the log is live is gone"
        c.get(P)
        c.get(f"{P}/status")
        c.post(f"{P}/brave/manual_state", json={"state": "forced_on"})
        new = ob.log_text().splitlines()[len(before):]
        hosts = ("google.com", "bing.com", "duckduckgo", "mojeek", "api.search.brave", "tavily", "http://", "https://")
        assert not [line for line in new if any(h in line.lower() for h in hosts)], new
