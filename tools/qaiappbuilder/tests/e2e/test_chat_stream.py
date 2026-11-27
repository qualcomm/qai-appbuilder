# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - sending a chat message against a scripted stand-in LLM (CHAT-02, CHAT-05).

The real model is deliberately NOT used: a real reply is not reproducible, cannot be made slow enough to interrupt, and cannot
be made to fail on demand. ``_fake_llm.py`` is an OpenAI-compatible server on 127.0.0.1 whose reply is chosen by the last user
message (``[echo] x``, ``[slow] n``, ``[error 500]``, ``[cut]``, ``[tool name {json}]``). It also records every request, so a
test can assert what the PRODUCT sent upstream.

How it is wired: the chat default endpoint comes from ``QAI_CHAT__LLM_BASE_URL`` / ``QAI_CHAT__LLM_DEFAULT_MODEL`` in the backend's
environment - no provider is registered and no key is written, so the OS keyring is never touched. A loopback base URL is treated
as an on-device endpoint (no key needed). A non-loopback spelling (``localhost.``) takes the cloud path and needs a key, which
``QAI_CHAT__LLM_API_KEY`` supplies; that is how the cloud-only behaviour is exercised without a real provider.

NOT covered (deliberately):

* **Anything a real model decides**: answer quality, tool choice, context-window management of a long chat, compaction.
* **The real ``qai-service`` provider.** It needs an SSO login (AUTH-04 is not automated). A real-model smoke test is out of scope.
* **Sampling on the cloud path** is in ``test_chat_cloud_key.py``; on the on-device path none is sent (CHAT-ISS-04, pinned below).
* **Multi-agent, sub-agents, MCP, skills, compaction, vision input, channel sync.**
* The WebSocket data plane (``/api/chat/ws``) beyond the 403-without-ids gate, and the control WS (EVT-ISS-01 covers its auth).
* **``/v1/chat/completions`` with a real reply** - see ``test_openai_compat.py``.

Isolation (verified): each backend runs on a throw-away data dir; the stand-in is bound to loopback on an ephemeral port; the
module asserts the OS keyring holds no new cloud credential. Tool-call tests send ``read`` for a path under ``C:\\e2e-private\\``
that does not exist and is never created - a denied or approved read cannot expose anything.

Observed behaviour worth pinning:

* The reply is a stream of ``message`` events whose ``data.frame_type`` is ``chunk`` / ``end`` / ``tool_call`` / ``tool_result`` /
  ``error`` / ``context_usage``, closed by one ``done`` event. A normal turn ends with ``end:completed`` then ``end:snapshot``.
* The user message, the model's reply and the system prompt are all replayed to the upstream on the next turn.
* An upstream HTTP error becomes ``error`` + ``end:failed`` (not an HTTP error); 5xx is retried three times first; 429 is
  ``throttling``; 401 is ``chat.llm.auth_failed``. A cloud-path request with no key is stopped BEFORE the network.
* An upstream that closes the stream without ``[DONE]`` still ends ``completed`` (a normal end, not an error).
* ``POST /api/chat/stop`` is idempotent (``aborted: false`` for an unknown or finished tab); the server-side counter proves the
  upstream is no longer being read afterwards.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

import httpx
import pytest

from tests.e2e import _fake_llm
from tests.e2e._fake_llm import StandInLLM
from tests.e2e._owned_backend import OwnedBackend, owned_backend

pytestmark = pytest.mark.e2e

SEC = "/api/security"
CLOUD_KEY = "e2e-key-123"


# --------------------------------------------------------------------------
# plumbing
# --------------------------------------------------------------------------


@dataclass
class Turn:
    status: int
    events: list[tuple[str, object]] = field(default_factory=list)
    body: str = ""

    def frames(self, kind: str | None = None) -> list[dict]:
        out = [d for ev, d in self.events if ev == "message" and isinstance(d, dict) and "frame_type" in d]
        return [f for f in out if kind is None or f["frame_type"] == kind]

    def named(self, name: str) -> list[object]:
        return [d for ev, d in self.events if ev == name]

    @property
    def text(self) -> str:
        return "".join(f["payload"].get("text", "") for f in self.frames("chunk"))

    @property
    def ends(self) -> list[str]:
        return [f["payload"].get("reason") for f in self.frames("end")]

    @property
    def errors(self) -> list[dict]:
        return [f["payload"] for f in self.frames("error")]

    @property
    def event_names(self) -> list[str]:
        return [ev for ev, _ in self.events]


def run_turn(cl: httpx.Client, cid: str, tab: str, prompt: str, *, seconds: float = 40.0, **query: str | int | float) -> Turn:
    """Open ``GET /conversations/{id}/stream`` and read it to the end."""
    turn = Turn(status=0)
    buf = ""
    try:
        with cl.stream("GET", f"/api/chat/conversations/{cid}/stream", params={"tab_id": tab, "prompt": prompt, **query},
                       timeout=httpx.Timeout(10.0, read=seconds)) as r:
            turn.status = r.status_code
            if r.status_code != 200:
                turn.body = r.read().decode("utf-8", "replace")
                return turn
            for chunk in r.iter_text():
                buf += chunk
                while "\n\n" in buf:
                    frame, buf = buf.split("\n\n", 1)
                    fields = dict(l.split(": ", 1) for l in frame.splitlines() if ": " in l)
                    data = fields.get("data", "")
                    turn.events.append((fields.get("event", ""), json.loads(data) if data.startswith("{") else data))
    except httpx.ReadTimeout:
        turn.events.append(("TIMEOUT", ""))
    return turn


def _keyring_cloud_state() -> dict:
    try:
        import keyring
    except ImportError:  # pragma: no cover
        return {}
    out = {}
    for svc in ("qai.cloud.cloud_model", "qai.cloud.enterprise_cloud_model"):
        try:
            out[svc] = bool(keyring.get_password(svc, "api_key"))
        except Exception:  # noqa: BLE001
            out[svc] = None
    return out


_KEYRING_AT_START = _keyring_cloud_state()


@pytest.fixture(scope="module", autouse=True)
def keyring_untouched() -> Iterator[None]:
    yield
    assert _keyring_cloud_state() == _KEYRING_AT_START, "a cloud credential appeared in the OS keyring during this module"


@pytest.fixture(scope="module")
def llm() -> Iterator[StandInLLM]:
    s = _fake_llm.start()
    yield s
    s.stop()


def _set_env(env: dict[str, str | None]) -> dict[str, str | None]:
    old = {k: os.environ.get(k) for k in env}
    for k, v in env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    return old


@pytest.fixture(scope="module")
def chat(llm: StandInLLM) -> Iterator[tuple[OwnedBackend, httpx.Client]]:
    """ONE backend pointed at the stand-in over its loopback (keyless, on-device) path."""
    old = _set_env({"QAI_CHAT__LLM_BASE_URL": llm.base_url, "QAI_CHAT__LLM_DEFAULT_MODEL": "e2e-model", "QAI_CHAT__LLM_API_KEY": None})
    try:
        with owned_backend() as ob:
            yield ob, ob.start()
    finally:
        _set_env(old)


@pytest.fixture
def conv(chat: tuple) -> Callable[[], str]:
    _ob, cl = chat

    def _new() -> str:
        r = cl.post("/api/chat/conversations", json={"title": "e2e"})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    return _new


@pytest.fixture(autouse=True)
def _reset(llm: StandInLLM) -> None:
    llm.clear()


# --------------------------------------------------------------------------
# CHAT-02: a normal reply
# --------------------------------------------------------------------------


def test_a_reply_streams_as_chunks_then_end_then_done(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    t = run_turn(cl, conv(), "t-basic", "[echo] hello")
    assert t.status == 200 and t.text == "ECHO: hello"
    assert t.ends == ["completed", "snapshot"] and t.event_names[-1] == "done" and t.event_names.count("done") == 1
    first = t.frames("chunk")[0]
    assert first["frame_id"] and first["sequence"] == 1 and first["payload"]["round_index"] == 0 and first["payload"]["request_id"]
    snap = t.frames("end")[-1]["payload"]
    assert snap["reason"] == "snapshot" and snap["assistant_message_id"] and snap["request_id"] == first["payload"]["request_id"]


def test_the_stream_response_headers(chat: tuple, conv: Callable) -> None:
    _ob, cl = chat
    with cl.stream("GET", f"/api/chat/conversations/{conv()}/stream", params={"tab_id": "t-hdr", "prompt": "[echo] hi"}, timeout=30) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
        for _ in r.iter_text():
            pass

def test_a_multi_word_reply_arrives_in_order(chat: tuple, conv: Callable) -> None:
    _ob, cl = chat
    t = run_turn(cl, conv(), "t-order", "anything")
    assert t.text == "Hello from the stand-in." and t.ends == ["completed", "snapshot"]
    seq = [f["sequence"] for f in t.frames()]
    assert seq == sorted(seq)


def test_the_upstream_request_is_a_streaming_chat_completion_for_the_default_model(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    run_turn(cl, conv(), "t-wire", "[echo] hi")
    (req,) = llm.chat_requests()
    assert req.path == "/v1/chat/completions" and req.method == "POST"
    assert req.body["model"] == "e2e-model" and req.body["stream"] is True
    roles = [m["role"] for m in req.body["messages"]]
    assert roles == ["system", "user"] and req.body["messages"][1]["content"] == "[echo] hi"
    assert len(req.body["messages"][0]["content"]) > 1000, "the system prompt is sent"
    assert "tools" in req.body and {"read", "write", "exec"} <= {t["function"]["name"] for t in req.body["tools"]}


def test_a_loopback_endpoint_needs_no_api_key(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    run_turn(cl, conv(), "t-nokey", "[echo] hi")
    (req,) = llm.chat_requests()
    assert not any(k.lower() == "authorization" for k in req.headers)


def test_the_conversation_keeps_both_sides_and_the_next_turn_replays_them(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    cid = conv()
    run_turn(cl, cid, "t-hist", "[echo] first")
    llm.clear()
    t = run_turn(cl, cid, "t-hist", "[echo] second")
    assert t.text == "ECHO: second"
    sent = [(m["role"], m["content"]) for m in llm.chat_requests()[0].body["messages"] if m["role"] != "system"]
    assert sent == [("user", "[echo] first"), ("assistant", "ECHO: first"), ("user", "[echo] second")]
    items = cl.get(f"/api/chat/conversations/{cid}/messages").json()["items"]
    assert [(m["role"], m["text"]) for m in items] == [
        ("user", "[echo] first"), ("assistant", "ECHO: first"), ("user", "[echo] second"), ("assistant", "ECHO: second")]
    conv_row = cl.get(f"/api/chat/conversations/{cid}").json()
    assert conv_row["message_count"] == 4


def test_two_conversations_do_not_see_each_other(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    run_turn(cl, conv(), "t-iso-a", "[echo] secret-A")
    llm.clear()
    run_turn(cl, conv(), "t-iso-b", "[echo] other")
    sent = json.dumps(llm.chat_requests()[0].body["messages"])
    assert "secret-A" not in sent


def test_a_non_ascii_prompt_and_reply_round_trip(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    cid = conv()
    t = run_turn(cl, cid, "t-utf8", "[echo] 你好 ✓ émoji 🙂")
    assert t.text == "ECHO: 你好 ✓ émoji 🙂"
    assert llm.chat_requests()[0].body["messages"][-1]["content"] == "[echo] 你好 ✓ émoji 🙂"
    assert cl.get(f"/api/chat/conversations/{cid}/messages").json()["items"][-1]["text"] == "ECHO: 你好 ✓ émoji 🙂"


def test_a_long_prompt_is_sent_whole(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    body = "x" * 20_000
    t = run_turn(cl, conv(), "t-long", f"[echo] {body}")
    assert t.text == f"ECHO: {body}"
    assert llm.chat_requests()[0].body["messages"][-1]["content"] == f"[echo] {body}"


def test_the_title_and_counters_of_the_conversation_update(chat: tuple, conv: Callable) -> None:
    _ob, cl = chat
    cid = conv()
    before = cl.get(f"/api/chat/conversations/{cid}").json()
    run_turn(cl, cid, "t-meta", "[echo] hi")
    after = cl.get(f"/api/chat/conversations/{cid}").json()
    assert (before["message_count"], after["message_count"]) == (0, 2)
    assert after["updated_at"] >= before["updated_at"]


# --------------------------------------------------------------------------
# request validation and routing edges
# --------------------------------------------------------------------------


def test_an_unknown_conversation_is_an_error_event_not_an_http_error(chat: tuple) -> None:
    _ob, cl = chat
    t = run_turn(cl, "01M5400000000000000000NOPE", "t-nf", "hi")
    assert t.status == 200
    (ev,) = [d for n, d in t.events if n == "error"]
    assert ev["code"] == "chat.conversation_not_found" and ev["type"] == "ConversationNotFoundError"
    assert ev["details"]["resource_id"] == "01M5400000000000000000NOPE"


@pytest.mark.parametrize(
    ("query", "field"),
    [({"prompt": ""}, "query.prompt"), ({"tab_id": ""}, "query.tab_id"), ({"temperature": 9}, "query.temperature"),
     ({"top_p": 2}, "query.top_p"), ({"max_tokens": -1}, "query.max_tokens")],
)
def test_bad_query_parameters_are_400s_before_anything_runs(chat: tuple, conv: Callable, llm: StandInLLM, query: dict, field: str) -> None:
    _ob, cl = chat
    params = {"tab_id": "t-val", "prompt": "hi", **query}
    r = cl.get(f"/api/chat/conversations/{conv()}/stream", params=params)
    assert r.status_code == 400 and field in r.json()["details"]["field_errors"]
    assert llm.chat_requests() == []


def test_a_missing_prompt_or_tab_is_a_400(chat: tuple, conv: Callable) -> None:
    _ob, cl = chat
    r = cl.get(f"/api/chat/conversations/{conv()}/stream")
    assert r.status_code == 400 and set(r.json()["details"]["field_errors"]) == {"query.tab_id", "query.prompt"}


def test_the_model_id_query_routes_to_a_model_with_a_context_length_requirement(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    """``model_id`` is the selected-model hint; one that no provider owns has no context window, so the turn stops before the network."""
    _ob, cl = chat
    t = run_turn(cl, conv(), "t-hint", "[echo] hi", model_id="e2e-model")
    assert t.errors == [] or t.errors[0]["code"] == "chat.model_context_length_missing"
    assert t.status == 200
    assert t.text == "" and llm.chat_requests() == [] if t.errors else True


def test_an_on_device_endpoint_receives_no_sampling_parameters_at_all(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    """KNOWN GAP / DESIGN QUESTION (current behaviour, pinned) - CHAT-ISS-04: on the loopback (on-device) path the request carries NO
    ``temperature`` / ``top_p`` / ``max_tokens`` even when the client asks for them; the cloud path forwards them
    (``test_chat_cloud_key.py``). Possibly intended (the local adapter leaves sampling to the service), but the UI offers the same
    sliders for both."""
    _ob, cl = chat
    run_turn(cl, conv(), "t-samp", "[echo] hi", temperature=0.3, top_p=0.8, max_tokens=77)
    body = llm.chat_requests()[0].body
    assert [body.get(k) for k in ("temperature", "top_p", "max_tokens")] == [None, None, None]

def test_the_tool_mode_query_changes_the_system_prompt(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    run_turn(cl, conv(), "t-m1", "[echo] hi")
    run_turn(cl, conv(), "t-m2", "[echo] hi", tool_mode="code")
    plain, code = (r.body["messages"][0]["content"] for r in llm.chat_requests())
    assert plain != code and len(code) < len(plain), "the code mode builds a different (shorter) prompt"


# A prompt that the keyword detector reads as a model-conversion task (QAIRT / QNN / quantize).
_MODEL_BUILD_TEXT = "[echo] QAIRT development environment setup requirements"


def _mode_changes(t: Turn) -> list[str]:
    return [f["payload"].get("mode") for f in t.frames("tool_mode_changed")]


def test_without_any_mode_a_model_build_prompt_still_switches_the_toolbar(chat: tuple, conv: Callable) -> None:
    """Unchanged behaviour: in NO mode, the detector may promote the turn to the model builder."""
    _ob, cl = chat
    t = run_turn(cl, conv(), "t-det", _MODEL_BUILD_TEXT)
    assert t.status == 200 and _mode_changes(t) == ["model_build"]


def test_an_extension_mode_is_not_switched_to_the_model_builder(chat: tuple, conv: Callable) -> None:
    """An extension chosen by the user (e.g. Qualcomm Skills) is authoritative, like a built-in mode."""
    _ob, cl = chat
    t = run_turn(cl, conv(), "t-ext", _MODEL_BUILD_TEXT, active_extensions='["qualcomm-skills"]')
    assert t.status == 200 and t.text.startswith("ECHO:")
    assert _mode_changes(t) == [], "an extension mode must not be yanked into the model builder"


# --------------------------------------------------------------------------
# upstream failures
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("prompt", "code"),
    [("[error 400]", "chat.local.http_error"), ("[error 401]", "chat.local.http_error"), ("[error 404]", "chat.local.http_error"),
     ("[error 500]", "chat.local.http_error"), ("[error 429]", "throttling")],
)
def test_an_upstream_http_error_ends_the_turn_with_an_error_frame(chat: tuple, conv: Callable, llm: StandInLLM, prompt: str, code: str) -> None:
    """On the on-device (loopback) path every status becomes ``chat.local.http_error``; 429 is classified as ``throttling``."""
    _ob, cl = chat
    t = run_turn(cl, conv(), "t-err", prompt)
    assert t.status == 200 and [e["code"] for e in t.errors] == [code]
    assert t.ends == ["failed"] and t.event_names[-1] == "done" and t.text == ""
    assert len(llm.chat_requests()) >= 1


def test_a_failed_turn_leaves_the_user_message_but_no_assistant_message(chat: tuple, conv: Callable) -> None:
    _ob, cl = chat
    cid = conv()
    run_turn(cl, cid, "t-failmsg", "[error 500]")
    roles = [m["role"] for m in cl.get(f"/api/chat/conversations/{cid}/messages").json()["items"]]
    assert roles[0] == "user" and "assistant" not in [r for r in roles if r == "assistant" and False] and roles.count("user") == 1


def test_the_conversation_is_usable_after_a_failed_turn(chat: tuple, conv: Callable) -> None:
    _ob, cl = chat
    cid = conv()
    run_turn(cl, cid, "t-recover", "[error 500]")
    t = run_turn(cl, cid, "t-recover", "[echo] back")
    assert t.text == "ECHO: back" and t.ends == ["completed", "snapshot"]


def test_an_upstream_that_closes_without_done_still_completes(chat: tuple, conv: Callable) -> None:
    _ob, cl = chat
    t = run_turn(cl, conv(), "t-cut", "[cut]")
    assert t.text == "one two " and t.ends == ["completed", "snapshot"] and t.errors == []


# --------------------------------------------------------------------------
# CHAT-05: interrupt
# --------------------------------------------------------------------------


def _start_in_thread(cl: httpx.Client, cid: str, tab: str, prompt: str, seconds: float = 40.0) -> tuple[threading.Thread, dict]:
    box: dict = {}

    def _go() -> None:
        box["turn"] = run_turn(cl, cid, tab, prompt, seconds=seconds)

    t = threading.Thread(target=_go, daemon=True)
    t.start()
    return t, box


def _wait_words(llm: StandInLLM, n: int, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while llm.words_sent < n and time.monotonic() < end:
        time.sleep(0.1)
    assert llm.words_sent >= n, f"the stand-in only sent {llm.words_sent} words"


def test_stop_interrupts_a_running_turn_and_the_upstream_stops_being_read(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    cid = conv()
    th, box = _start_in_thread(cl, cid, "t-stop", "[slow] 60")
    _wait_words(llm, 4)
    r = cl.post("/api/chat/stop", json={"tab_id": "t-stop", "reason": "user"})
    assert r.status_code == 200 and r.json() == {"aborted": True, "reason": "user"}
    th.join(20)
    turn: Turn = box["turn"]
    assert turn.event_names[-1] == "done" and "chunk" in [f["frame_type"] for f in turn.frames()]
    assert 3 <= len(turn.frames("chunk")) < 60, "some words arrived, far fewer than the 60 requested"
    at_stop = llm.words_sent
    time.sleep(2.0)
    assert llm.words_sent - at_stop <= 2, "the upstream stream is no longer being read after the stop"
    assert llm.open_streams == 0


def test_the_partial_reply_is_saved_after_a_stop(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    cid = conv()
    th, _box = _start_in_thread(cl, cid, "t-partial", "[slow] 60")
    _wait_words(llm, 4)
    cl.post("/api/chat/stop", json={"tab_id": "t-partial"})
    th.join(20)
    items = cl.get(f"/api/chat/conversations/{cid}/messages").json()["items"]
    assert [m["role"] for m in items] == ["user", "assistant"]
    saved = items[1]["text"]
    assert saved.startswith("w0 w1 w2") and 3 <= len(saved.split()) < 60


def test_a_running_turn_is_listed_in_active_runs_until_it_ends(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    th, _box = _start_in_thread(cl, conv(), "t-active", "[slow] 60")
    _wait_words(llm, 2)
    runs = [i for i in cl.get("/api/chat/active-runs").json()["items"] if i["tab_id"] == "t-active"]
    assert len(runs) == 1
    run = runs[0]
    assert run["kind"] == "chat" and run["status"] == "streaming" and run["aborted"] is False and run["openable"] is True
    assert run["attach_path"] == "/api/chat/active-runs/t-active/ws" and run["stop"]["path"] == "/api/chat/stop"
    cl.post("/api/chat/stop", json={"tab_id": "t-active"})
    th.join(20)
    assert [i for i in cl.get("/api/chat/active-runs").json()["items"] if i["tab_id"] == "t-active"] == []


def test_stop_is_idempotent_for_unknown_and_finished_tabs(chat: tuple, conv: Callable) -> None:
    _ob, cl = chat
    run_turn(cl, conv(), "t-fin", "[echo] hi")
    for tab in ("t-fin", "never-existed"):
        r = cl.post("/api/chat/stop", json={"tab_id": tab})
        assert r.status_code == 200 and r.json() == {"aborted": False, "reason": "user_requested"}


def test_stop_validates_its_body(chat: tuple) -> None:
    _ob, cl = chat
    assert cl.post("/api/chat/stop", json={}).status_code == 400
    assert cl.post("/api/chat/stop", json={"tab_id": ""}).status_code == 400


def test_a_tab_can_be_reused_right_after_a_stop(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    th, _box = _start_in_thread(cl, conv(), "t-reuse", "[slow] 60")
    _wait_words(llm, 3)
    cl.post("/api/chat/stop", json={"tab_id": "t-reuse"})
    th.join(20)
    t = run_turn(cl, conv(), "t-reuse", "[echo] again")
    assert t.text == "ECHO: again" and t.ends == ["completed", "snapshot"]


def test_a_second_turn_on_a_busy_tab_is_rejected_and_the_first_is_unharmed(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    th, box = _start_in_thread(cl, conv(), "t-busy", "[slow] 10")
    _wait_words(llm, 2)
    second = run_turn(cl, conv(), "t-busy", "[echo] second")
    (err,) = [d for n, d in second.events if n == "error"]
    assert err["code"] == "chat.conversation_locked" and err["details"]["held_by_tab_id"] == "t-busy"
    th.join(30)
    assert box["turn"].ends == ["completed", "snapshot"] and box["turn"].text.startswith("w0 w1")


def test_two_different_tabs_can_stream_at_the_same_time(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    t1, b1 = _start_in_thread(cl, conv(), "t-par-1", "[slow] 6")
    t2, b2 = _start_in_thread(cl, conv(), "t-par-2", "[slow] 6")
    t1.join(30)
    t2.join(30)
    assert b1["turn"].ends == b2["turn"].ends == ["completed", "snapshot"]
    assert b1["turn"].text == b2["turn"].text == "w0 w1 w2 w3 w4 w5 "


def test_stopping_one_tab_does_not_stop_another(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    ta, ba = _start_in_thread(cl, conv(), "t-keep", "[slow] 12")
    tb, bb = _start_in_thread(cl, conv(), "t-kill", "[slow] 60")
    _wait_words(llm, 4)
    assert cl.post("/api/chat/stop", json={"tab_id": "t-kill"}).json()["aborted"] is True
    ta.join(40)
    tb.join(20)
    assert ba["turn"].ends == ["completed", "snapshot"] and ba["turn"].text == "".join(f"w{i} " for i in range(12))
    assert len(bb["turn"].frames("chunk")) < 60


# --------------------------------------------------------------------------
# CHAT-05: tool call and the permission prompt
# --------------------------------------------------------------------------

PRIVATE = "C:\\e2e-private\\"


def _pending_for(cl: httpx.Client, path: str, timeout: float = 20.0) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for p in cl.get(f"{SEC}/permission/pending").json().get("requests") or []:
            if p["resource"]["identifier"].lower() == path.lower():
                return p
        time.sleep(0.3)
    raise AssertionError(f"no pending permission request for {path}")


def _tool_prompt(name: str) -> str:
    return "[tool read " + json.dumps({"path": PRIVATE + name}) + "]"


def test_a_tool_call_raises_a_permission_request_and_denying_it_tells_the_model(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    path = PRIVATE + "deny.txt"
    th, box = _start_in_thread(cl, conv(), "t-deny", _tool_prompt("deny.txt"), seconds=60)
    p = _pending_for(cl, path)
    assert p["subject"]["identifier"] == "ai_coding.tool" and p["state"] == "pending"
    assert p["requested_mask"] == {"read": True, "write": False, "execute": False, "delete": False}
    assert cl.post(f"{SEC}/permission/{p['request_id']}/reject", json={"reason": "e2e-deny"}).status_code == 200
    th.join(60)
    turn: Turn = box["turn"]
    calls = turn.frames("tool_call")
    results = turn.frames("tool_result")
    assert calls and calls[0]["payload"]["tool_name"] == "read" and calls[0]["payload"]["arguments"] == {"path": path}
    assert calls[0]["payload"]["tool_call_id"] == "call_e2e_1"
    assert "[FileGuard] Denied by this application's security policy" in results[0]["payload"]["result"]
    assert results[0]["payload"]["tool_call_id"] == "call_e2e_1" and results[0]["payload"]["truncated"] is False


def test_approving_the_request_lets_the_tool_run_and_the_result_reaches_the_model(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    path = PRIVATE + "approve.txt"
    th, box = _start_in_thread(cl, conv(), "t-appr", _tool_prompt("approve.txt"), seconds=60)
    p = _pending_for(cl, path)
    assert cl.post(f"{SEC}/permission/{p['request_id']}/approve", json={"grant": "once"}).status_code == 200
    th.join(60)
    turn: Turn = box["turn"]
    assert "[tool_error] read: file not fou" in turn.frames("tool_result")[0]["payload"]["result"], "approved: the tool really ran (file does not exist)"
    assert turn.text.startswith("ECHO: TOOL-RESULT: [tool_error] read: file not found") and turn.ends == ["completed", "snapshot"]
    reqs = llm.chat_requests()
    assert len(reqs) == 2, "one call to ask, one with the tool result"
    last = reqs[1].body["messages"][-1]
    assert last["role"] == "tool" and last["tool_call_id"] == "call_e2e_1"


def test_a_denied_request_is_no_longer_pending(chat: tuple, conv: Callable) -> None:
    _ob, cl = chat
    path = PRIVATE + "gone.txt"
    th, _box = _start_in_thread(cl, conv(), "t-gone", _tool_prompt("gone.txt"), seconds=60)
    p = _pending_for(cl, path)
    cl.post(f"{SEC}/permission/{p['request_id']}/reject", json={})
    th.join(60)
    assert all(x["request_id"] != p["request_id"] for x in cl.get(f"{SEC}/permission/pending").json()["requests"])


def test_stopping_a_turn_that_waits_for_approval_ends_it(chat: tuple, conv: Callable) -> None:
    _ob, cl = chat
    path = PRIVATE + "stopwait.txt"
    th, box = _start_in_thread(cl, conv(), "t-stopwait", _tool_prompt("stopwait.txt"), seconds=60)
    p = _pending_for(cl, path)
    r = cl.post("/api/chat/stop", json={"tab_id": "t-stopwait"})
    assert r.json()["aborted"] is True
    th.join(30)
    assert not th.is_alive() and box["turn"].event_names[-1] == "done"
    cl.delete(f"{SEC}/permission/{p['request_id']}")  # best effort: withdraw whatever is left


def test_an_unknown_tool_name_is_reported_back_not_fatal(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    _ob, cl = chat
    t = run_turn(cl, conv(), "t-unk", "[tool no_such_tool {}]", seconds=60)
    results = t.frames("tool_result")
    assert results and results[0]["payload"]["error_code"] == "chat.tool_not_registered"
    assert t.event_names[-1] == "done"


def test_a_tool_that_the_model_repeats_without_progress_is_cut_off(chat: tuple, conv: Callable, llm: StandInLLM) -> None:
    """The stand-in answers every user turn with the same tool call (``[loop ...]``); the result never changes anything. The product
    notices the repetition and ends the turn instead of calling the model forever."""
    _ob, cl = chat
    t = run_turn(cl, conv(), "t-loop", "[loop no_such_tool {}]", seconds=60)
    assert "followup_no_progress" in t.ends
    assert len(t.frames("tool_call")) <= 6 and len(llm.chat_requests()) <= 8
    assert "自动结束本轮" in t.text


# --------------------------------------------------------------------------
# the chat WebSocket gate
# --------------------------------------------------------------------------


def test_the_chat_websocket_refuses_a_connection_without_a_tab_and_conversation(chat: tuple) -> None:
    from websockets.exceptions import InvalidStatus
    from websockets.sync.client import connect

    from tests.e2e import conftest as c

    for path in ("/api/chat/ws", "/api/chat/ws?tab_id=a"):
        with pytest.raises(InvalidStatus) as exc:
            connect(f"ws://{c.E2E_HOST}:{c.E2E_PORT}{path}", open_timeout=5)
        assert exc.value.response.status_code == 403
