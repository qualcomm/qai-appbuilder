# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""L1 - the OpenAI-compatible ``/v1/*`` surface, the parts that need no upstream model (CHAT-06).

Routes: ``GET /v1/models``, ``GET /v1/models/{id:path}``, ``POST /v1/chat/completions`` and the optional bearer guard
``server.openai_api_key`` (env ``QAI_SERVER__OPENAI_API_KEY``). These routes are the one part of the API that third-party
OpenAI clients depend on, so their shape is a contract.

NOT covered (deliberately) - and why:

* **A successful ``/v1/chat/completions`` reply, streaming or not.** The route needs the chosen model to have a context
  length, and the only source of one is a registered provider; a provider that can answer needs a key; and a provider key
  is written to the OS keyring (``apps/api/di.py`` hard-codes ``build_secret_store(prefer="auto")``, no setting or environment
  variable selects the encrypted-file store). Writing the user's keyring from a test is ruled out (FEATURE-INVENTORY, model hub).
  Chat itself IS covered end to end through ``/api/chat`` with a stand-in LLM (``test_chat_stream.py``), which exercises the same
  use case; what stays unverified is only the ``/v1`` envelope around a real reply: ``choices[0].message.content``, ``usage``,
  ``chat.completion.chunk`` deltas and ``tool_calls`` deltas. If the secret store becomes selectable, add them.
* Forwarding of sampling parameters, multi-turn ``messages``, ``tools`` and pass-through headers to the upstream: same reason.
* ``conversation_id`` resume (an OpenAI extension): needs a reply to be observable.

Isolation (verified): one backend with the guard ON and no chat endpoint configured (the guard-OFF case is in `test_openai_compat_open.py`). No provider is
registered with a key, nothing reaches the network, and the module asserts no cloud credential appeared in the OS keyring. The
catalog entries it registers (no URL fetched) are removed at the end of each test.

Observed behaviour worth pinning:

* ``/v1/models`` is the model catalog's entries plus two built-in seed names (``qai-default``, ``qai-fast``) - not the chat
  provider models. Nothing else is seeded (QAI Service is opt-in, so no ``qai-service::route-1``). ``created`` is the request time, so it changes each call.
* The guard answers **403** (not 401) with ``openai_compat.unauthorized``, requires exactly ``Bearer <key>`` (case-sensitive), and
  protects all three routes. Everything else under ``/api`` is unaffected by it.
* An invalid body is a **400** in the project's error envelope - not OpenAI's ``{"error": {...}}`` shape. A client that parses
  OpenAI errors will not understand it (CHAT-ISS-02).
* With no model reachable, the NON-streaming call is a **200 with empty content** and zeroed usage, while the STREAMING call
  reports the failure as ``data: {"error": ...}`` + ``[DONE]`` (CHAT-ISS-01).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator

import httpx
import pytest

from tests.e2e._owned_backend import owned_backend
from tests.e2e.test_chat_stream import _keyring_cloud_state, _set_env

pytestmark = pytest.mark.e2e

KEY = "e2e-v1-secret"
OK = {"Authorization": f"Bearer {KEY}"}
V = "/v1"
MSG = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
SEED = ["qai-default", "qai-fast"]
_START = _keyring_cloud_state()


@pytest.fixture(scope="module", autouse=True)
def keyring_untouched() -> Iterator[None]:
    yield
    assert _keyring_cloud_state() == _START


@pytest.fixture(scope="module")
def guarded() -> Iterator[httpx.Client]:
    old = _set_env({"QAI_SERVER__OPENAI_API_KEY": KEY, "QAI_CHAT__LLM_BASE_URL": None, "QAI_CHAT__LLM_API_KEY": None})
    try:
        with owned_backend() as ob:
            yield ob.start()
    finally:
        _set_env(old)


def _ids(cl: httpx.Client, headers: dict | None = OK) -> list[str]:
    r = cl.get(f"{V}/models", headers=headers)
    assert r.status_code == 200, r.text
    return [m["id"] for m in r.json()["data"]]


@pytest.fixture
def entry(guarded: httpx.Client) -> Iterator[callable]:
    made: list[str] = []

    def _add(mid: str) -> None:
        r = guarded.post("/api/model-catalog/entries", json={"model_id": mid, "name": mid, "provider": "local",
                                                              "source_url": "https://example.invalid/x", "description": "d", "tags": []})
        assert r.status_code == 201, r.text
        made.append(mid)

    yield _add
    for mid in made:
        guarded.delete(f"/api/model-catalog/entries/{mid}")


# --------------------------------------------------------------------------
# bearer guard
# --------------------------------------------------------------------------

GUARDED = [("GET", f"{V}/models"), ("GET", f"{V}/models/qai-default"), ("POST", f"{V}/chat/completions")]


@pytest.mark.parametrize(("method", "path"), GUARDED)
@pytest.mark.parametrize(
    "headers",
    [None, {"Authorization": "Bearer nope"}, {"Authorization": KEY}, {"Authorization": f"bearer {KEY}"},
     {"Authorization": f"Basic {KEY}"}, {"Authorization": ""}],
    ids=["none", "wrong", "no-scheme", "lowercase-scheme", "basic", "empty-header"],
)
def test_every_v1_route_refuses_a_missing_or_wrong_token(guarded: httpx.Client, method: str, path: str, headers: dict | None) -> None:
    r = guarded.request(method, path, headers=headers, json=MSG if method == "POST" else None)
    assert r.status_code == 403, r.text
    assert r.json() == {"type": "ForbiddenError", "code": "openai_compat.unauthorized",
                        "message": "Missing or invalid bearer token for the OpenAI-compatible API."}


@pytest.mark.parametrize(("method", "path"), GUARDED[:2])
def test_the_right_token_is_accepted(guarded: httpx.Client, method: str, path: str) -> None:
    assert guarded.request(method, path, headers=OK).status_code == 200


def test_the_guard_runs_before_body_validation(guarded: httpx.Client) -> None:
    """An unauthenticated caller learns nothing about the schema: a bad body is still 403."""
    r = guarded.post(f"{V}/chat/completions", json={"not": "valid"})
    assert r.status_code == 403 and r.json()["code"] == "openai_compat.unauthorized"


def test_the_guard_covers_only_v1(guarded: httpx.Client) -> None:
    assert guarded.get("/api/system/health").status_code == 200
    assert guarded.get("/api/chat/tools").status_code == 200


def test_an_error_response_never_echoes_the_token(guarded: httpx.Client) -> None:
    r = guarded.get(f"{V}/models", headers={"Authorization": f"Bearer {KEY}x"})
    assert r.status_code == 403 and KEY not in r.text


# --------------------------------------------------------------------------
# GET /v1/models
# --------------------------------------------------------------------------


def test_the_list_has_the_openai_shape(guarded: httpx.Client) -> None:
    body = guarded.get(f"{V}/models", headers=OK).json()
    assert list(body) == ["object", "data"] and body["object"] == "list"
    for m in body["data"]:
        assert sorted(m) == ["created", "id", "object", "owned_by"] and m["object"] == "model"
        assert isinstance(m["created"], int) and m["created"] > 1_700_000_000 and m["owned_by"] == "qai"


def test_a_fresh_install_lists_the_seed_models_once_each(guarded: httpx.Client) -> None:
    ids = _ids(guarded)
    assert sorted(ids) == sorted(SEED) and len(ids) == len(set(ids))


def test_registered_catalog_entries_are_listed_by_id_before_the_seed_names_and_disappear_when_removed(guarded: httpx.Client, entry) -> None:
    for mid in ("e2e-v1-b", "e2e-v1-a"):
        entry(mid)
    ids = _ids(guarded)
    assert ids == ["e2e-v1-a", "e2e-v1-b", "qai-default", "qai-fast"], "catalog entries sorted by id, then the built-in seed names"
    assert _ids(guarded) == ids, "the order is stable"
    assert guarded.delete("/api/model-catalog/entries/e2e-v1-a").status_code == 204
    assert _ids(guarded) == ["e2e-v1-b", "qai-default", "qai-fast"]


def test_a_catalog_entry_named_like_a_seed_model_is_not_listed_twice(guarded: httpx.Client, entry) -> None:
    entry("qai-default")
    ids = _ids(guarded)
    assert ids.count("qai-default") == 1 and ids.count("qai-fast") == 1
    assert ids == ["qai-default", "qai-fast"], "same set as a fresh install: the real entry replaces the seed one"

def test_the_provider_models_the_chat_ui_offers_are_not_listed(guarded: httpx.Client) -> None:
    """KNOWN GAP / DESIGN QUESTION (current behaviour, pinned) - CHAT-ISS-03: a model that exists only in a registered provider
    (what the model picker shows) is absent from ``/v1/models``, so an OpenAI client that discovers models cannot find it."""
    cfg = {"base_url": "https://example.invalid/v1", "pinned": False, "models": [{"model_id": "e2e-prov-model", "name": "P", "context_length": 8192}]}
    assert guarded.put("/api/model-catalog/providers/e2e-v1-prov", json={"config": cfg}).status_code == 200
    assert "e2e-prov-model" not in _ids(guarded)
    assert guarded.get(f"{V}/models/e2e-prov-model", headers=OK).status_code == 404


# --------------------------------------------------------------------------
# GET /v1/models/{id}
# --------------------------------------------------------------------------


def test_a_single_model_has_the_openai_shape(guarded: httpx.Client) -> None:
    m = guarded.get(f"{V}/models/qai-fast", headers=OK).json()
    assert sorted(m) == ["created", "id", "object", "owned_by"] and (m["id"], m["object"], m["owned_by"]) == ("qai-fast", "model", "qai")


def test_an_id_containing_colons_and_slashes_resolves(guarded: httpx.Client, entry) -> None:
    """The route is ``{model_id:path}``, so ids such as ``provider::model`` work."""
    entry("e2e-v1::colon-id")
    assert guarded.get(f"{V}/models/e2e-v1::colon-id", headers=OK).json()["id"] == "e2e-v1::colon-id"


@pytest.mark.parametrize("mid", ["nope", "QAI-DEFAULT", "qai-default/", "%20", "a/b/c"])
def test_an_unknown_model_is_a_404_in_the_project_envelope(guarded: httpx.Client, mid: str) -> None:
    r = guarded.get(f"{V}/models/{mid}", headers=OK)
    assert r.status_code == 404 and r.json()["code"] == "openai_compat.model_not_found" and r.json()["type"] == "NotFoundError"
    assert r.json()["details"]["resource_type"] == "model"


def test_model_ids_are_case_sensitive(guarded: httpx.Client) -> None:
    assert guarded.get(f"{V}/models/qai-default", headers=OK).status_code == 200
    assert guarded.get(f"{V}/models/Qai-Default", headers=OK).status_code == 404


def test_a_model_id_over_256_characters_is_rejected(guarded: httpx.Client) -> None:
    r = guarded.get(f"{V}/models/{'m' * 257}", headers=OK)
    assert r.status_code == 400 and "path.model_id" in r.json()["details"]["field_errors"]


def test_wrong_verbs_are_rejected(guarded: httpx.Client) -> None:
    for r in (guarded.post(f"{V}/models", headers=OK, json={}), guarded.delete(f"{V}/models/qai-default", headers=OK)):
        assert r.status_code == 405 and r.headers["allow"] == "GET"
    r = guarded.put(f"{V}/chat/completions", headers=OK, json={})
    assert r.status_code == 405 and r.headers["allow"] == "POST"
    assert guarded.get(f"{V}/chat/completions", headers=OK).status_code == 404, "GET on the POST-only path is an unmatched route, not a 405"


def test_unimplemented_openai_endpoints_are_404(guarded: httpx.Client) -> None:
    for path in ("/embeddings", "/completions", "/images/generations", "/audio/transcriptions"):
        assert guarded.post(f"{V}{path}", headers=OK, json={"model": "m", "input": "x"}).status_code in (404, 405), path


# --------------------------------------------------------------------------
# POST /v1/chat/completions: request validation (no upstream needed)
# --------------------------------------------------------------------------


def _errors(r: httpx.Response) -> dict:
    assert r.status_code == 400 and r.json()["code"] == "request.validation", r.text
    return r.json()["details"]["field_errors"]


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"messages": MSG["messages"]}, "body.model"),
        ({"model": "", "messages": MSG["messages"]}, "body.model"),
        ({"model": None, "messages": MSG["messages"]}, "body.model"),
        ({"model": "m" * 257, "messages": MSG["messages"]}, "body.model"),
        ({"model": "m"}, "body.messages"),
        ({"model": "m", "messages": []}, "body.messages"),
        ({"model": "m", "messages": [{"role": "wizard", "content": "x"}]}, "body.messages.0.role"),
        ({"model": "m", "messages": [{"content": "x"}]}, "body.messages.0.role"),
        ({"model": "m", "messages": [{"role": "user", "content": None}]}, "body.messages.0.content.str"),
        ({**MSG, "temperature": "hot"}, "body.temperature"),
    ],
)
def test_a_bad_request_body_is_a_400_naming_the_field(guarded: httpx.Client, body: dict, field: str) -> None:
    assert field in _errors(guarded.post(f"{V}/chat/completions", headers=OK, json=body))


def test_a_malformed_or_empty_body_is_a_400(guarded: httpx.Client) -> None:
    for content in (b"{not json", b"", b"[]", b"null", b'"text"'):
        r = guarded.post(f"{V}/chat/completions", headers={**OK, "Content-Type": "application/json"}, content=content)
        assert r.status_code == 400 and r.json()["code"] == "request.validation", content


def test_validation_errors_use_the_project_envelope_not_openais(guarded: httpx.Client) -> None:
    """KNOWN GAP / DESIGN QUESTION (current behaviour, pinned) - CHAT-ISS-02: an OpenAI SDK expects
    ``{"error": {"message", "type", "code"}}``; it gets ``{"type", "code", "message", "details"}`` and no ``error`` key."""
    body = guarded.post(f"{V}/chat/completions", headers=OK, json={}).json()
    assert "error" not in body and set(body) == {"type", "code", "message", "details"}


@pytest.mark.parametrize("role", ["system", "user", "assistant", "tool"])
def test_all_four_openai_roles_are_accepted(guarded: httpx.Client, role: str) -> None:
    r = guarded.post(f"{V}/chat/completions", headers=OK, json={"model": "m", "messages": [{"role": role, "content": "x"}]})
    assert r.status_code == 200


def test_content_may_be_a_list_of_parts(guarded: httpx.Client) -> None:
    body = {"model": "m", "messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}]}
    assert guarded.post(f"{V}/chat/completions", headers=OK, json=body).status_code == 200


def test_unknown_top_level_fields_are_ignored_not_rejected(guarded: httpx.Client) -> None:
    assert guarded.post(f"{V}/chat/completions", headers=OK, json={**MSG, "some_future_knob": 1, "logit_bias": {"1": 2}}).status_code == 200


# --------------------------------------------------------------------------
# POST /v1/chat/completions with no model reachable
# --------------------------------------------------------------------------


def test_non_streaming_with_no_model_is_a_200_with_empty_content(guarded: httpx.Client) -> None:
    """KNOWN GAP (current behaviour, pinned) - CHAT-ISS-01: the turn failed (``chat.model_context_length_missing``), but
    ``collect_completion`` only reads CHUNK and END frames, so the caller gets a successful, empty completion."""
    r = guarded.post(f"{V}/chat/completions", headers=OK, json=MSG)
    assert r.status_code == 200
    body = r.json()
    assert body["object"] == "chat.completion" and body["model"] == "m" and body["id"].startswith("chatcmpl-")
    assert body["choices"] == [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "stop"}]
    assert body["usage"] == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


def test_streaming_with_no_model_reports_the_error_in_the_stream(guarded: httpx.Client) -> None:
    r = guarded.post(f"{V}/chat/completions", headers=OK, json={**MSG, "stream": True})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache" and r.headers["x-accel-buffering"] == "no"
    lines = [l for l in r.text.split("\n\n") if l]
    assert lines[-1] == "data: [DONE]" and len(lines) == 2
    err = json.loads(lines[0].removeprefix("data: "))["error"]
    assert err["type"] == "stream_error" and err["code"] == "chat.model_context_length_missing"
    assert "Selected model 'm' has no valid context length" in err["message"]


def test_a_keyless_provider_model_is_reported_as_a_missing_key(guarded: httpx.Client) -> None:
    cfg = {"base_url": "https://example.invalid/v1", "pinned": False, "models": [{"model_id": "e2e-nokey", "name": "N", "context_length": 8192}]}
    assert guarded.put("/api/model-catalog/providers/e2e-v1-nokey", json={"config": cfg}).status_code == 200
    r = guarded.post(f"{V}/chat/completions", headers=OK, json={"model": "e2e-nokey", "messages": MSG["messages"], "stream": True})
    err = json.loads(r.text.split("\n\n")[0].removeprefix("data: "))["error"]
    assert err["code"] == "chat.llm.provider_api_key_missing" and err["type"] == "stream_error"
    assert "no API key configured" in err["message"]
    non_stream = guarded.post(f"{V}/chat/completions", headers=OK, json={"model": "e2e-nokey", "messages": MSG["messages"]})
    assert non_stream.status_code == 200 and non_stream.json()["choices"][0]["message"]["content"] == "", "same gap as above"


def test_each_v1_call_leaves_a_conversation_behind(guarded: httpx.Client) -> None:
    """A stateless ``/v1`` call provisions a chat conversation + tab; they are visible in the chat list."""
    before = len(guarded.get("/api/chat/conversations").json()["items"])
    guarded.post(f"{V}/chat/completions", headers=OK, json=MSG)
    guarded.post(f"{V}/chat/completions", headers=OK, json=MSG)
    after = len(guarded.get("/api/chat/conversations").json()["items"])
    assert after - before == 2
