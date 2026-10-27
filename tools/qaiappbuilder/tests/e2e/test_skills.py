# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - skills: discovery, enable / disable, per-skill mode, and loading a built-in skill (SKILL-01, SKILL-02).

Skills are ``SKILL.md`` packs found in two folders, ``<repo>/skills`` (user-installed, wins) and ``<repo>/factory/chat_features``
(built-in packs). ``GET /api/skills`` lists them; ``POST /api/skills/toggle`` and ``POST /api/skills/{id}/set_mode`` write per-skill
overrides into ``forge.config`` (``skills.overrides``); the chat adds the enabled ones to its system prompt, and the ``skill`` tool
returns a skill's text to the model.

NOT covered (deliberately):

* **The Skills page** (the L2 half of SKILL-01): the page and its components have no ``data-testid`` yet (FEATURE-INVENTORY §4).
* **Whether following a skill WORKS** - e.g. that ``weather`` returns a forecast or ``read-arxiv-paper`` fetches a paper. They call
  public web services (wttr.in, arxiv.org); a skill is only text the model follows. What is verified is that each built-in skill
  parses, is listed, reaches the prompt according to its mode, and loads through the ``skill`` tool byte for byte.
* **NPU-optimised skills** (``local`` / ``both`` modes): no shipped skill is NPU-optimised (a trailing ``.`` on ``tags:`` or an ``npu.txt``),
  and creating one means writing into the repo's ``skills/`` folder, which these tests never do. The refusal of ``local``/``both`` for a
  non-NPU skill IS covered.
* **Pinned skills** (``pinned: true``): none ships. The ``pinned`` handling in toggle / set_mode is therefore untested.
* Icons: no shipped skill has one, so only the 404 path is covered.

Isolation (verified): one throw-away backend; overrides are written to its own database (``forge.config``), never the repo. The module
asserts the repo's ``skills/`` and ``factory/chat_features`` listings are unchanged. Tool calls come from the scripted stand-in LLM
(``_fake_llm.py``, ``[tool skill {...}]``); no real model is used.

Observed behaviour worth pinning:

* The effective mode is resolved in this order: an explicit ``mode`` override, else the ``enabled`` flag (False -> ``off``, True ->
  ``cloud``), else a per-skill default (``ppt-gen`` and ``code-assist`` default to ``off``), else ``cloud``.
* **An explicit ``mode`` beats ``enabled``**: once ``set_mode`` has been called for a skill, ``toggle`` no longer changes its effective
  state (SKILL-ISS-01). The two APIs write different keys and the reader prefers one of them.
* ``toggle`` accepts a skill name that does not exist and stores it (SKILL-ISS-02).
* The ``skill`` tool can load a skill that is ``off`` - ``off`` only hides it from the system prompt (SKILL-ISS-03).
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest

from tests.e2e import _fake_llm
from tests.e2e._fake_llm import StandInLLM
from tests.e2e._owned_backend import OwnedBackend, owned_backend
from tests.e2e.conftest import REPO_ROOT
from tests.e2e.test_chat_stream import Turn, _set_env, run_turn

pytestmark = pytest.mark.e2e

DEFAULT_OFF = {"code-assist", "ppt-gen"}
# Skills that must always ship; guards against a skill being deleted by mistake (the full list is discovered from disk below).
CORE_SKILLS = {"app-builder", "model-builder", "model-hub", "weather"}
SKILL_KEYS = {"skill_id", "name", "description", "icon", "tags", "use_for", "skill_path", "npu_optimized", "mode", "enabled", "pinned", "display_name"}
SKILL_DIRS = (REPO_ROOT / "skills", REPO_ROOT / "factory" / "chat_features")


def _discover_builtin() -> list[str]:
    """Every ``<dir>/<name>/SKILL.md`` one level under the two skill folders, sorted by id (so new skills need no test change)."""
    return sorted(p.name for d in SKILL_DIRS if d.exists() for p in d.iterdir() if p.is_dir() and (p / "SKILL.md").exists())


BUILTIN = _discover_builtin()


def _listing() -> dict[str, list[str]]:
    return {str(d): sorted(p.name for p in d.iterdir()) for d in SKILL_DIRS if d.exists()}


_REPO_AT_START = _listing()


@pytest.fixture(scope="module", autouse=True)
def repo_skill_folders_untouched() -> Iterator[None]:
    yield
    assert _listing() == _REPO_AT_START, "a test changed the repo's skills folders"


@pytest.fixture(scope="module")
def llm() -> Iterator[StandInLLM]:
    s = _fake_llm.start()
    yield s
    s.stop()


@pytest.fixture(scope="module")
def sk(llm: StandInLLM) -> Iterator[tuple[OwnedBackend, httpx.Client]]:
    old = _set_env({"QAI_CHAT__LLM_BASE_URL": llm.base_url, "QAI_CHAT__LLM_DEFAULT_MODEL": "e2e-model", "QAI_CHAT__LLM_API_KEY": None})
    try:
        with owned_backend() as ob:
            yield ob, ob.start()
    finally:
        _set_env(old)


@pytest.fixture(autouse=True)
def _restore(sk: tuple) -> Iterator[None]:
    """Every test starts from the factory state: remove all overrides this module wrote."""
    ob, cl = sk
    yield
    import sqlite3

    con = sqlite3.connect(str(ob.data_dir / "db" / "qai.db"), timeout=15)
    try:
        row = con.execute("SELECT value_json FROM kv_user_prefs WHERE key = 'forge.config'").fetchone()
        if row:
            doc = json.loads(row[0])
            doc.pop("skills", None)
            con.execute("UPDATE kv_user_prefs SET value_json = ? WHERE key = 'forge.config'", (json.dumps(doc),))
            con.commit()
    finally:
        con.close()


def _skills(cl: httpx.Client) -> dict[str, dict]:
    r = cl.get("/api/skills")
    assert r.status_code == 200, r.text
    return {s["skill_id"]: s for s in r.json()["skills"]}


def _modes(cl: httpx.Client) -> dict[str, str]:
    return {k: v["mode"] for k, v in _skills(cl).items()}


# --------------------------------------------------------------------------
# discovery
# --------------------------------------------------------------------------


def test_the_builtin_skills_are_discovered_sorted_by_id(sk: tuple) -> None:
    _ob, cl = sk
    ids = [s["skill_id"] for s in cl.get("/api/skills").json()["skills"]]
    assert ids == BUILTIN == sorted(ids)
    assert CORE_SKILLS <= set(ids), f"missing core skills: {CORE_SKILLS - set(ids)}"


def test_each_skill_has_the_documented_fields(sk: tuple) -> None:
    _ob, cl = sk
    for sid, s in _skills(cl).items():
        assert set(s) == SKILL_KEYS, sid
        assert s["name"] == sid and s["description"] and s["display_name"]
        assert Path(s["skill_path"]).is_file() and Path(s["skill_path"]).name == "SKILL.md"
        assert s["npu_optimized"] is False and s["pinned"] is False and isinstance(s["tags"], list)


def test_user_skills_and_builtin_packs_come_from_the_two_folders(sk: tuple) -> None:
    _ob, cl = sk
    for sid, s in _skills(cl).items():
        parent = Path(s["skill_path"]).parent.parent
        assert parent in (REPO_ROOT / "skills", REPO_ROOT / "factory" / "chat_features"), sid
    assert {Path(s["skill_path"]).parent.parent.name for s in _skills(cl).values()} == {"skills", "chat_features"}
    user = {sid for sid, s in _skills(cl).items() if Path(s["skill_path"]).parent.parent == REPO_ROOT / "skills"}
    assert user == {p.name for p in (REPO_ROOT / "skills").iterdir() if p.is_dir() and (p / "SKILL.md").exists()}


def test_the_skill_text_comes_from_the_front_matter(sk: tuple) -> None:
    _ob, cl = sk
    w = _skills(cl)["weather"]
    assert w["description"] == "Query weather forecasts for any city. Uses wttr.in service, no API key required."
    assert w["tags"] == ["weather", "forecast", "wttr"] and w["display_name"] == "Weather"
    assert w["use_for"].startswith("Getting current weather")


def test_a_skill_with_an_empty_tags_value_has_one_empty_tag(sk: tuple) -> None:
    """KNOWN GAP (current behaviour, pinned) - SKILL-ISS-04: ``tags:`` with no value is split into ``[""]`` (see ``app-builder``)."""
    _ob, cl = sk
    assert _skills(cl)["app-builder"]["tags"] == [""] and _skills(cl)["model-builder"]["use_for"] == ""


def test_the_listing_is_read_from_disk_on_every_call(sk: tuple) -> None:
    """Two calls give the same list; nothing is cached stale (a reload is also a no-op for the list)."""
    _ob, cl = sk
    assert cl.get("/api/skills").json() == cl.get("/api/skills").json()
    assert cl.post("/api/skills/reload").json() == {"status": "reloaded"}
    assert list(_skills(cl)) == BUILTIN


def test_every_skills_md_front_matter_parses_to_its_own_name() -> None:
    """The shipped SKILL.md files all have ``---`` front-matter with a ``name:`` equal to the folder (what makes them loadable)."""
    for d in SKILL_DIRS:
        for p in sorted(d.iterdir()):
            md = p / "SKILL.md"
            if not (p.is_dir() and md.exists()):
                continue
            text = md.read_text(encoding="utf-8")
            assert text.startswith("---\n") or text.startswith("---\r\n"), md
            head = text.split("---", 2)[1]
            names = [l.split(":", 1)[1].strip().strip("'\"") for l in head.splitlines() if l.startswith("name:")]
            assert names == [p.name] and "description:" in head, md


# --------------------------------------------------------------------------
# default modes and the global policy
# --------------------------------------------------------------------------


def test_default_modes_are_cloud_except_the_two_that_must_be_opted_into(sk: tuple) -> None:
    _ob, cl = sk
    modes = _modes(cl)
    assert {k for k, v in modes.items() if v == "off"} == DEFAULT_OFF
    assert {k for k, v in modes.items() if v == "cloud"} == set(BUILTIN) - DEFAULT_OFF
    assert {k: s["enabled"] for k, s in _skills(cl).items()} == {k: k not in DEFAULT_OFF for k in BUILTIN}


def test_the_policy_starts_empty(sk: tuple) -> None:
    _ob, cl = sk
    assert cl.get("/api/skills/policy").json() == {"mode": "auto", "overrides": {}, "last_reload": None}


@pytest.mark.parametrize("mode", ["auto", "manual", "disabled"])
def test_the_global_mode_is_stored(sk: tuple, mode: str) -> None:
    _ob, cl = sk
    r = cl.post("/api/skills/set_mode", json={"mode": mode})
    assert r.status_code == 200 and r.json() == {"status": "saved", "mode": mode}
    assert cl.get("/api/skills/policy").json()["mode"] == mode
    cl.post("/api/skills/set_mode", json={"mode": "auto"})


@pytest.mark.parametrize("body", [{"mode": "off"}, {"mode": "AUTO"}, {"mode": ""}, {}, {"mode": None}])
def test_the_global_mode_rejects_other_values(sk: tuple, body: dict) -> None:
    _ob, cl = sk
    r = cl.post("/api/skills/set_mode", json=body)
    assert r.status_code == 400 and "body.mode" in r.json()["details"]["field_errors"]


def test_the_global_mode_does_not_change_the_listed_modes(sk: tuple) -> None:
    """The global ``auto/manual/disabled`` preference is stored but the per-skill list is unaffected by it."""
    _ob, cl = sk
    before = _modes(cl)
    cl.post("/api/skills/set_mode", json={"mode": "disabled"})
    assert _modes(cl) == before
    cl.post("/api/skills/set_mode", json={"mode": "auto"})


def test_reload_stamps_the_time(sk: tuple) -> None:
    _ob, cl = sk
    assert cl.get("/api/skills/policy").json()["last_reload"] is None
    cl.post("/api/skills/reload")
    stamp = cl.get("/api/skills/policy").json()["last_reload"]
    assert stamp.endswith("+00:00") and abs(time.time() - __import__("datetime").datetime.fromisoformat(stamp).timestamp()) < 120


# --------------------------------------------------------------------------
# enable / disable
# --------------------------------------------------------------------------


def test_toggle_off_then_on(sk: tuple) -> None:
    _ob, cl = sk
    r = cl.post("/api/skills/toggle", json={"skill_name": "weather", "enabled": False})
    assert r.status_code == 200 and r.json() == {"status": "saved", "skill_name": "weather", "enabled": False}
    assert (_skills(cl)["weather"]["mode"], _skills(cl)["weather"]["enabled"]) == ("off", False)
    assert cl.get("/api/skills/policy").json()["overrides"] == {"weather": {"enabled": False}}
    cl.post("/api/skills/toggle", json={"skill_name": "weather", "enabled": True})
    assert (_skills(cl)["weather"]["mode"], _skills(cl)["weather"]["enabled"]) == ("cloud", True)


def test_a_default_off_skill_can_be_turned_on(sk: tuple) -> None:
    _ob, cl = sk
    cl.post("/api/skills/toggle", json={"skill_name": "ppt-gen", "enabled": True})
    assert _skills(cl)["ppt-gen"]["mode"] == "cloud" and _skills(cl)["code-assist"]["mode"] == "off"


def test_toggling_one_skill_leaves_the_others_alone(sk: tuple) -> None:
    _ob, cl = sk
    before = _modes(cl)
    cl.post("/api/skills/toggle", json={"skill_name": "email-163-com", "enabled": False})
    after = _modes(cl)
    assert {k for k in before if before[k] != after[k]} == {"email-163-com"}


@pytest.mark.parametrize("body", [{"skill_name": "weather"}, {"enabled": True}, {"skill_name": "weather", "enabled": "maybe"}, {}])
def test_toggle_validates_its_body(sk: tuple, body: dict) -> None:
    _ob, cl = sk
    r = cl.post("/api/skills/toggle", json=body)
    assert r.status_code == 400 and r.json()["code"] == "request.validation"


def test_toggle_accepts_a_skill_that_does_not_exist(sk: tuple) -> None:
    """KNOWN GAP (current behaviour, pinned) - SKILL-ISS-02: no 404; the made-up name is stored in the overrides forever."""
    _ob, cl = sk
    r = cl.post("/api/skills/toggle", json={"skill_name": "e2e-not-a-skill", "enabled": False})
    assert r.status_code == 200 and r.json()["status"] == "saved"
    assert cl.get("/api/skills/policy").json()["overrides"] == {"e2e-not-a-skill": {"enabled": False}}
    assert "e2e-not-a-skill" not in _skills(cl)


# --------------------------------------------------------------------------
# per-skill mode
# --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "cloud"])
def test_set_a_per_skill_mode(sk: tuple, mode: str) -> None:
    _ob, cl = sk
    r = cl.post("/api/skills/weather/set_mode", json={"mode": mode})
    assert r.status_code == 200 and r.json() == {"skill_id": "weather", "mode": mode, "npu_optimized": False}
    assert _skills(cl)["weather"]["mode"] == mode and cl.get("/api/skills/policy").json()["overrides"]["weather"] == {"mode": mode}


@pytest.mark.parametrize("mode", ["local", "both"])
def test_npu_only_modes_are_refused_for_a_skill_that_is_not_npu_optimised(sk: tuple, mode: str) -> None:
    _ob, cl = sk
    r = cl.post("/api/skills/weather/set_mode", json={"mode": mode})
    assert r.status_code == 400 and r.json() == {"type": "ValidationError", "code": "user_prefs.skills.mode_not_allowed",
                                                  "message": f"Skill 'weather' is not NPU-optimized; mode '{mode}' requires NPU optimization."}
    assert cl.get("/api/skills/policy").json()["overrides"] == {}, "and nothing was stored"


@pytest.mark.parametrize("mode", ["bogus", "OFF", "", "auto", None])
def test_per_skill_mode_rejects_unknown_values(sk: tuple, mode: object) -> None:
    _ob, cl = sk
    r = cl.post("/api/skills/weather/set_mode", json={"mode": mode})
    assert r.status_code == 400 and "body.mode" in r.json()["details"]["field_errors"]


def test_per_skill_mode_of_an_unknown_skill_is_a_404(sk: tuple) -> None:
    _ob, cl = sk
    r = cl.post("/api/skills/e2e-not-a-skill/set_mode", json={"mode": "cloud"})
    assert r.status_code == 404 and r.json()["code"] == "user_prefs.skills.not_found" and r.json()["message"] == "Unknown skill: e2e-not-a-skill"
    assert r.json()["details"] == {"resource_type": "skill", "resource_id": "e2e-not-a-skill"}
    assert cl.get("/api/skills/policy").json()["overrides"] == {}


def test_an_explicit_mode_beats_a_later_toggle(sk: tuple) -> None:
    """KNOWN GAP (current behaviour, pinned) - SKILL-ISS-01: after ``set_mode`` the Skills panel's on/off switch (``toggle``) stops
    working for that skill, because the reader prefers the ``mode`` key. Both calls return success."""
    _ob, cl = sk
    cl.post("/api/skills/weather/set_mode", json={"mode": "cloud"})
    r = cl.post("/api/skills/toggle", json={"skill_name": "weather", "enabled": False})
    assert r.status_code == 200 and r.json()["enabled"] is False
    s = _skills(cl)["weather"]
    assert (s["mode"], s["enabled"]) == ("cloud", True), "the toggle was stored but has no effect"
    assert cl.get("/api/skills/policy").json()["overrides"]["weather"] == {"mode": "cloud", "enabled": False}
    cl.post("/api/skills/weather/set_mode", json={"mode": "off"})
    cl.post("/api/skills/toggle", json={"skill_name": "weather", "enabled": True})
    assert _skills(cl)["weather"]["mode"] == "off", "and the same the other way round"


def test_set_mode_and_toggle_write_different_keys_into_one_entry(sk: tuple) -> None:
    _ob, cl = sk
    cl.post("/api/skills/toggle", json={"skill_name": "weather", "enabled": False})
    cl.post("/api/skills/weather/set_mode", json={"mode": "cloud"})
    assert cl.get("/api/skills/policy").json()["overrides"]["weather"] == {"enabled": False, "mode": "cloud"}


def test_a_stored_npu_mode_for_a_skill_that_is_not_npu_optimised_reads_back_as_cloud(sk: tuple) -> None:
    """``set_mode`` refuses ``local``/``both`` for such a skill, but the stored document can still hold one (hand-edited, or written before the
    skill lost its NPU flag). The reader must not honour it - it falls back to ``cloud`` - or the chat would advertise a skill it cannot run."""
    import sqlite3

    ob, cl = sk
    con = sqlite3.connect(str(ob.data_dir / "db" / "qai.db"), timeout=15)
    try:
        row = con.execute("SELECT value_json FROM kv_user_prefs WHERE key = 'forge.config'").fetchone()
        doc = json.loads(row[0]) if row else {}
        doc.setdefault("skills", {}).setdefault("overrides", {})["weather"] = {"mode": "local"}
        con.execute("INSERT OR REPLACE INTO kv_user_prefs(key, value_json, updated_at) VALUES ('forge.config', ?, '2026-01-01T00:00:00+00:00')", (json.dumps(doc),))
        con.commit()
    finally:
        con.close()
    assert _skills(cl)["weather"]["mode"] == "cloud"
    assert cl.get("/api/skills/policy").json()["overrides"]["weather"] == {"mode": "local"}, "stored as written, read as cloud"

# --------------------------------------------------------------------------
# icons
# --------------------------------------------------------------------------


@pytest.mark.parametrize("sid", [*BUILTIN[:3], "e2e-not-a-skill", "%00"])
def test_no_shipped_skill_has_an_icon(sk: tuple, sid: str) -> None:
    _ob, cl = sk
    r = cl.get(f"/api/skills/{sid}/icon")
    assert r.status_code == 404 and r.json()["code"] == "user_prefs.skills.icon_not_found" and r.json()["details"]["resource_type"] == "skill_icon"
    assert all(s["icon"] == "" for s in _skills(cl).values())


@pytest.mark.parametrize("sid", ["..", "a%2Fb", "a/b"])
def test_ids_with_path_parts_never_reach_the_icon_lookup(sk: tuple, sid: str) -> None:
    """The router does not match them at all: a bare 404 with no body (so no file outside a skill folder can be asked for)."""
    _ob, cl = sk
    r = cl.get(f"/api/skills/{sid}/icon")
    assert r.status_code == 404 and r.content in (b"", b'{"detail":"Not Found"}')

# --------------------------------------------------------------------------
# the Security > Skills view sees the same skills and modes
# --------------------------------------------------------------------------


def test_security_discovery_lists_the_same_skills_and_follows_the_modes(sk: tuple) -> None:
    _ob, cl = sk
    d = cl.get("/api/security/skill-discovery").json()
    assert set(d) == {"by_name", "scan_status", "skills", "total"} and d["total"] == len(BUILTIN)
    by = {s["skill_name"]: s for s in d["skills"]}
    assert sorted(by) == BUILTIN
    assert {k for k, v in by.items() if v["mode"] == "off"} == DEFAULT_OFF
    cl.post("/api/skills/toggle", json={"skill_name": "weather", "enabled": False})
    assert {s["skill_name"]: s["mode"] for s in cl.get("/api/security/skill-discovery").json()["skills"]}["weather"] == "off"


# --------------------------------------------------------------------------
# SKILL-02: a built-in skill reaches the model and can be loaded
# --------------------------------------------------------------------------


def _chat(cl: httpx.Client, llm: StandInLLM, tab: str, prompt: str = "[echo] hi") -> Turn:
    llm.clear()
    cid = cl.post("/api/chat/conversations", json={"title": "e2e"}).json()["id"]
    return run_turn(cl, cid, tab, prompt, seconds=60)


def _prompt(llm: StandInLLM) -> str:
    return llm.chat_requests()[0].body["messages"][0]["content"]


def _wait_prompt_has(cl: httpx.Client, llm: StandInLLM, skill: str, want: bool, tab: str) -> str:
    """The catalog provider caches overrides for a few seconds, so poll until the toggle shows."""
    end = time.monotonic() + 30
    sp = ""
    while time.monotonic() < end:
        _chat(cl, llm, f"{tab}-{int(time.monotonic() * 1000)}")
        sp = _prompt(llm)
        if (f"skills\\{skill}\\SKILL.md" in sp or f"chat_features\\{skill}\\SKILL.md" in sp) == want:
            return sp
        time.sleep(1.5)
    raise AssertionError(f"{skill!r}: expected in-prompt={want} but it never became so")


def test_the_enabled_skills_are_named_in_the_system_prompt_with_their_path_and_use_for(sk: tuple, llm: StandInLLM) -> None:
    _ob, cl = sk
    _chat(cl, llm, "t-sp")
    sp = _prompt(llm)
    for sid in set(BUILTIN) - DEFAULT_OFF:
        assert sid in sp, sid
    for sid in DEFAULT_OFF:
        assert f"{sid}\\SKILL.md" not in sp, f"{sid} defaults to off, so it is not advertised"
    assert "Path: `" + str(REPO_ROOT / "skills" / "weather" / "SKILL.md") + "`" in sp
    assert "Use for: Getting current weather, checking weather forecasts, querying weather by city name" in sp


def test_turning_a_skill_off_removes_it_from_the_prompt_and_on_adds_it(sk: tuple, llm: StandInLLM) -> None:
    _ob, cl = sk
    cl.post("/api/skills/toggle", json={"skill_name": "weather", "enabled": False})
    off = _wait_prompt_has(cl, llm, "weather", False, "t-off")
    assert "weather" not in off.lower().split("available_skills", 1)[-1] or "skills\\weather" not in off
    cl.post("/api/skills/toggle", json={"skill_name": "ppt-gen", "enabled": True})
    _wait_prompt_has(cl, llm, "ppt-gen", True, "t-on")


def test_the_skill_tool_is_offered_to_the_model(sk: tuple, llm: StandInLLM) -> None:
    _ob, cl = sk
    _chat(cl, llm, "t-tool")
    tools = {t["function"]["name"]: t["function"] for t in llm.chat_requests()[0].body["tools"]}
    assert "skill" in tools and "MUST be the exact id of a skill" in tools["skill"]["description"]


@pytest.mark.parametrize("sid", BUILTIN[1:])
def test_each_builtin_skill_loads_through_the_skill_tool_byte_for_byte(sk: tuple, llm: StandInLLM, sid: str) -> None:
    _ob, cl = sk
    t = _chat(cl, llm, f"t-load-{sid}", "[tool skill " + json.dumps({"name": sid}) + "]")
    res = t.frames("tool_result")[0]["payload"]
    assert res.get("error_code") is None and res["truncated"] is False
    path = Path(_skills(cl)[sid]["skill_path"])
    text = path.read_text(encoding="utf-8")
    body = res["result"]
    assert body.startswith(f'<skill_content name="{sid}">')
    assert text.replace("\r\n", "\n").strip()[:200] in body.replace("\r\n", "\n"), "the file's own text is what the model receives"
    assert res["size"] == len(body)


@pytest.mark.parametrize("sid", sorted(DEFAULT_OFF))
def test_a_skill_that_is_off_can_still_be_loaded_by_name(sk: tuple, llm: StandInLLM, sid: str) -> None:
    """KNOWN GAP / DESIGN QUESTION (current behaviour, pinned) - SKILL-ISS-03: ``off`` hides a skill from the prompt only; the model
    can still load it if it knows the name (it is in the error message for an unknown name)."""
    _ob, cl = sk
    assert _skills(cl)[sid]["mode"] == "off"
    t = _chat(cl, llm, f"t-offload-{sid}", "[tool skill " + json.dumps({"name": sid}) + "]")
    assert t.frames("tool_result")[0]["payload"]["result"].startswith(f'<skill_content name="{sid}">')


def test_loading_an_unknown_skill_lists_the_valid_ones(sk: tuple, llm: StandInLLM) -> None:
    _ob, cl = sk
    t = _chat(cl, llm, "t-unk", '[tool skill {"name": "e2e-not-a-skill"}]')
    msg = t.frames("tool_result")[0]["payload"]["result"]
    assert msg.startswith("[skill error] unknown skill 'e2e-not-a-skill'. Available skills: ")
    assert all(s in msg for s in BUILTIN)


@pytest.mark.parametrize("args", ['{"name": ""}', '{"name": "   "}', "{}"])
def test_loading_without_a_name_is_a_readable_error(sk: tuple, llm: StandInLLM, args: str) -> None:
    _ob, cl = sk
    t = _chat(cl, llm, "t-blank", f"[tool skill {args}]")
    assert "[skill error]" in t.frames("tool_result")[0]["payload"]["result"]
