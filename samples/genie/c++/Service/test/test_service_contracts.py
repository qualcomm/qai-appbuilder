#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import argparse
import json
import socket
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

LEDGER_PREFIX = "x-genie-prompt-"
LONG_STORY = "Write a very long, detailed story about a dragon, at least 800 words."


class Skip(Exception):
    pass


class Client:
    def __init__(self, host, port, model, timeout):
        self.host = host
        self.port = port
        self.base = f"http://{host}:{port}"
        self.model = model
        self.timeout = timeout

    def request(self, method, path, body=None, timeout=None):
        data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return resp.status, dict((k.lower(), v) for k, v in resp.headers.items()), resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, dict((k.lower(), v) for k, v in exc.headers.items()), exc.read()
        except Exception as exc:
            return -1, {}, repr(exc).encode()

    def chat_body(self, messages, stream=False, max_tokens=24):
        return {"model": self.model, "messages": messages, "stream": stream, "max_tokens": max_tokens}

    def chat(self, messages, max_tokens=24, timeout=None):
        status, headers, raw = self.request("POST", "/v1/chat/completions",
                                            self.chat_body(messages, False, max_tokens), timeout)
        parsed = None
        try:
            parsed = json.loads(raw.decode("utf-8", "replace"))
        except ValueError:
            pass
        return status, headers, parsed, raw

    def chat_text(self, prompt, max_tokens=24, timeout=None):
        return self.chat([{"role": "user", "content": prompt}], max_tokens, timeout)

    def stream(self, prompt, max_tokens, result, first_text=None, timeout=None):
        body = self.chat_body([{"role": "user", "content": prompt}], True, max_tokens)
        req = urllib.request.Request(self.base + "/v1/chat/completions", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        result.update(http=None, chunks=0, done=False, error=None)
        try:
            with urllib.request.urlopen(req, timeout=timeout or max(self.timeout, 600)) as resp:
                result["http"] = resp.status
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        result["done"] = True
                        break
                    try:
                        frame = json.loads(payload)
                    except ValueError:
                        continue
                    for choice in frame.get("choices", []):
                        delta = choice.get("delta", {})
                        if "status" not in choice and (delta.get("content") or delta.get("reasoning_content")):
                            result["chunks"] += 1
                            if first_text is not None:
                                first_text.set()
        except Exception as exc:
            result["error"] = repr(exc)[:200]
        return result

    def alive(self):
        status, _, _ = self.request("GET", "/status", timeout=10)
        return status == 200


def require(cond, detail):
    if not cond:
        raise AssertionError(detail)


def require_alive(client, tag):
    require(client.alive(), f"service not responding after {tag}")


def check_content_formats(client):
    question = "Reply with the single word OK."
    variants = {
        "string": [{"role": "user", "content": question}],
        "parts_array": [{"role": "user", "content": [{"type": "text", "text": question}]}],
        "system_parts_array": [
            {"role": "system", "content": [{"type": "text", "text": "You are a terse assistant."}]},
            {"role": "user", "content": [{"type": "text", "text": question}]},
        ],
    }
    for name, messages in variants.items():
        status, _, parsed, raw = client.chat(messages)
        require(status == 200, f"{name}: HTTP {status} {raw[:200]!r}")
        require(parsed and parsed.get("choices"), f"{name}: response has no choices")
    require_alive(client, "content formats")


def ledger_of(headers):
    ledger = {}
    for key, value in headers.items():
        if key.startswith(LEDGER_PREFIX):
            try:
                ledger[key[len(LEDGER_PREFIX):]] = int(value)
            except ValueError:
                pass
    return ledger


def check_prompt_ledger(client):
    status, headers, parsed, raw = client.chat_text("Say hi.")
    require(status == 200, f"HTTP {status} {raw[:200]!r}")
    ledger = ledger_of(headers)
    if not ledger:
        raise Skip("response carries no X-Genie-Prompt-* headers (prompt_optimization.fidelity disabled?)")
    ctx = ledger.get("context-size", 0)
    require(ctx > 0, f"context-size must be positive: {ledger}")
    require(ledger.get("tokens-out", 0) <= ctx, f"tokens-out exceeds context-size: {ledger}")
    require(ledger.get("messages-kept", 0) + ledger.get("messages-dropped", 0) == ledger.get("messages-in", -1),
            f"kept+dropped != in: {ledger}")
    if "skills-kept" in ledger:
        tiers = ledger.get("skills-l2", 0) + ledger.get("skills-l1", 0) + ledger.get("skills-l0", 0)
        require(tiers == ledger["skills-kept"], f"skill tiers do not sum to skills-kept: {ledger}")

    filler = "The quick brown fox jumps over the lazy dog while the committee reviews the quarterly plan. " * 12
    history = []
    for i in range(40):
        history.append({"role": "user", "content": f"Note {i}: {filler}"})
        history.append({"role": "assistant", "content": f"Noted {i}."})
    history.append({"role": "user", "content": "Reply with only the word DONE."})
    status, headers, parsed, raw = client.chat(history, max_tokens=16)
    require(status == 200, f"oversized history: HTTP {status} {raw[:200]!r}")
    ledger = ledger_of(headers)
    require(ledger.get("messages-in") == len(history),
            f"messages-in {ledger.get('messages-in')} != sent {len(history)}")
    require(ledger.get("messages-dropped", 0) + ledger.get("messages-truncated", 0) > 0,
            f"oversized history was neither dropped nor truncated: {ledger}")
    require(ledger.get("tokens-out", 0) <= ledger.get("context-size", 0), f"tokens-out exceeds context: {ledger}")
    require_alive(client, "oversized history")


def check_same_model_parallel(client):
    results = []

    def one(i):
        results.append(client.chat_text(f"Count from 1 to {5 + i}.", 32)[0])

    threads = [threading.Thread(target=one, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(client.timeout)
    require(results == [200, 200], f"parallel non-stream results: {results}")
    require_alive(client, "parallel non-stream")
    streams = [{}, {}]
    threads = [threading.Thread(target=client.stream, args=(f"Tell a short story {i}.", 96, streams[i]))
               for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(client.timeout)
    for s in streams:
        require(s.get("http") == 200 and s.get("done") and not s.get("error"), f"parallel stream failed: {s}")
    require_alive(client, "parallel stream")


def check_readonly_endpoints_hammer(client):
    stop = threading.Event()
    errors = []

    def hammer(path):
        while not stop.is_set():
            status, _, raw = client.request("GET", path, timeout=30)
            if status not in (200, 503):
                errors.append((path, status, raw[:80]))

    paths = ["/v1/models", "/v1/models", "/status", "/profile?model=" + urllib.parse.quote(client.model)]
    threads = [threading.Thread(target=hammer, args=(p,)) for p in paths]
    for t in threads:
        t.start()
    status, _, _, raw = client.chat_text("Say hi.", 16)
    time.sleep(2)
    stop.set()
    for t in threads:
        t.join(60)
    require(status == 200, f"chat during hammering: HTTP {status} {raw[:200]!r}")
    require(not errors, f"read-only endpoint errors: {errors[:5]}")
    require_alive(client, "hammer")


def check_stop_spam(client):
    result, first = {}, threading.Event()
    gen = threading.Thread(target=client.stream, args=(LONG_STORY, 300, result, first))
    gen.start()
    first.wait(180)
    queued = []
    waiter = threading.Thread(target=lambda: queued.append(client.chat_text("Count from 1 to 20.", 48, 300)[0]))
    waiter.start()
    for _ in range(15):
        client.request("POST", "/stop", {"text": "stop", "model": client.model}, timeout=20)
        time.sleep(0.3)
    gen.join(180)
    waiter.join(300)
    require(result.get("http") == 200 and not result.get("error"), f"stream during /stop spam: {result}")
    require(queued == [200], f"queued request during /stop spam: {queued}")
    status, _, _, raw = client.chat_text("Count from 1 to 20.", 48)
    require(status == 200, f"request after /stop spam: HTTP {status} {raw[:200]!r}")
    require_alive(client, "/stop spam")


def check_clear_spam(client):
    results = [{}, {}]
    threads = [threading.Thread(target=client.stream, args=(f"Tell a short story {i}.", 96, results[i]))
               for i in range(2)]
    for t in threads:
        t.start()
    for _ in range(30):
        client.request("POST", "/clear", {"model": client.model}, timeout=60)
        time.sleep(0.2)
    for t in threads:
        t.join(300)
    for r in results:
        require(r.get("http") == 200 and not r.get("error"), f"stream during /clear spam: {r}")
    require_alive(client, "/clear spam")


def check_unload_during_stream(client):
    result, first = {}, threading.Event()
    gen = threading.Thread(target=client.stream, args=(LONG_STORY, 600, result, first))
    gen.start()
    first.wait(180)
    time.sleep(1.0)
    status, _, raw = client.request("POST", "/unload", {"model": client.model}, timeout=90)
    require(status in (200, 204), f"/unload: HTTP {status} {raw[:200]!r}")
    gen.join(180)
    require(not gen.is_alive(), "stream did not terminate after /unload")
    require_alive(client, "/unload during stream")
    status, _, _, raw = client.chat_text("Say OK.", 16, 400)
    require(status == 200, f"reload after /unload: HTTP {status} {raw[:200]!r}")


class RawChat:
    def __init__(self, client, prompt, stream, max_tokens):
        self.sock = socket.create_connection((client.host, client.port), timeout=client.timeout)
        self.sock.settimeout(client.timeout)
        self.file = self.sock.makefile("rb")
        body = json.dumps(client.chat_body([{"role": "user", "content": prompt}], stream, max_tokens),
                          ensure_ascii=False).encode("utf-8")
        head = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: {client.host}:{client.port}\r\n"
                "Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\nConnection: keep-alive\r\n\r\n").encode("ascii")
        self.sock.sendall(head + body)

    def read_status(self):
        status = int(self.file.readline().decode("latin-1").split()[1])
        while self.file.readline() not in (b"\r\n", b"\n", b""):
            pass
        return status

    def wait_for_data(self, min_lines, timeout):
        deadline = time.monotonic() + timeout
        seen = 0
        while time.monotonic() < deadline and seen < min_lines:
            line = self.file.readline()
            if not line:
                break
            if line.strip().startswith(b"data:") or line.strip().startswith(b'{"'):
                seen += 1
        return seen

    def abort(self):
        try:
            self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("HH", 1, 0))
        except OSError:
            pass
        for call in (lambda: self.sock.shutdown(socket.SHUT_RDWR), self.file.close, self.sock.close):
            try:
                call()
            except OSError:
                pass


def followup_seconds(client):
    started = time.monotonic()
    status, _, parsed, raw = client.chat_text("Reply with only OK.", 8)
    require(status == 200 and parsed and parsed.get("choices"), f"follow-up failed: HTTP {status} {raw[:200]!r}")
    return time.monotonic() - started


def check_disconnect_stream(client):
    conn = RawChat(client, LONG_STORY, True, 1024)
    require(conn.read_status() == 200, "stream request not accepted")
    require(conn.wait_for_data(3, client.timeout) >= 3, "stream produced no frames before disconnect")
    conn.abort()
    elapsed = followup_seconds(client)
    require(elapsed < client.timeout, f"follow-up after stream disconnect took {elapsed:.1f}s")
    require_alive(client, "stream disconnect")


def check_disconnect_prefill(client):
    prompt = "Summarize these notes briefly: " + ("The meeting reviewed the design, risks, and next steps. " * 900)
    conn = RawChat(client, prompt, False, 8)
    time.sleep(1.5)
    conn.abort()
    elapsed = followup_seconds(client)
    require(elapsed < client.timeout, f"follow-up after prefill disconnect took {elapsed:.1f}s")
    require_alive(client, "prefill disconnect")


def check_disconnect_queued(client):
    active = RawChat(client, LONG_STORY, True, 1024)
    require(active.read_status() == 200, "active stream not accepted")
    require(active.wait_for_data(3, client.timeout) >= 3, "active stream produced no frames")
    queued = RawChat(client, "This queued request must not start after its client left.", False, 8)
    time.sleep(2.0)
    queued.abort()
    time.sleep(1.5)
    active.abort()
    elapsed = followup_seconds(client)
    require(elapsed < client.timeout, f"follow-up after queued disconnect took {elapsed:.1f}s")
    require_alive(client, "queued disconnect")


def check_model_required(client):
    missing = "__no_such_model__"
    cases = [
        ("POST", "/contextsize", {"model": missing}, 404),
        ("GET", "/contextsize", None, 404),
        ("POST", "/stop", {"text": "stop"}, 400),
        ("POST", "/stop", {"text": "stop", "model": missing}, 404),
        ("POST", "/clear", {}, 400),
        ("POST", "/clear", {"model": missing}, 404),
        ("POST", "/textsplitter", {"text": "a b c"}, 400),
        ("POST", "/textsplitter", {"text": "a b c", "model": missing}, 404),
        ("POST", "/unload", {}, 400),
        ("POST", "/unload", {"model": missing}, 404),
        ("GET", "/profile?model=" + missing, None, 404),
        ("POST", "/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}, 400),
    ]
    bad = []
    for method, path, body, expected in cases:
        status, _, raw = client.request(method, path, body, timeout=30)
        if status != expected:
            bad.append(f"{method} {path} {body} -> {status} (expected {expected}) {raw[:80]!r}")
    require(not bad, "; ".join(bad))
    status, _, _, raw = client.chat_text("Say hi.", 8, 400)
    require(status == 200, f"load via chat: HTTP {status} {raw[:200]!r}")
    quoted = urllib.parse.quote(client.model)
    for req_body in (None, {"model": client.model}):
        status, _, raw = client.request("POST", "/contextsize", req_body or {}, timeout=30)
        require(status == 200, f"POST /contextsize {req_body}: HTTP {status} {raw[:120]!r}")
        body = json.loads(raw.decode("utf-8", "replace"))
        entries = {e.get("model"): e.get("contextsize", 0) for e in body.get("models", [])}
        require(entries.get(client.model, 0) > 0, f"POST /contextsize {req_body} body: {body}")
        require(body.get("loaded_models") == len(entries), f"POST /contextsize {req_body} loaded_models: {body}")
        if req_body:
            require(len(entries) == 1, f"POST /contextsize should list only the requested model: {body}")
    status, _, raw = client.request("POST", "/contextsize?model=" + quoted, {}, timeout=30)
    require(status == 200, f"POST /contextsize?model=: HTTP {status} {raw[:120]!r}")
    status, _, raw = client.request("GET", "/profile", timeout=30)
    require(status == 200, f"/profile (all): HTTP {status} {raw[:120]!r}")
    body = json.loads(raw.decode("utf-8", "replace"))
    require(client.model in {e.get("model") for e in body.get("models", [])}, f"/profile (all) body: {body}")
    require_alive(client, "model-required checks")


def check_status_shape(client):
    status, _, _, raw = client.chat_text("Say hi.", 8, 400)
    require(status == 200, f"load via chat: HTTP {status} {raw[:200]!r}")
    status, _, raw = client.request("GET", "/status", timeout=15)
    require(status == 200, f"/status: HTTP {status}")
    body = json.loads(raw.decode("utf-8", "replace"))
    require("loading" not in body, f"/status still reports legacy loading: {body}")
    slots = body.get("slots")
    require(isinstance(slots, list) and {s.get("device") for s in slots} == {"npu", "gpu", "cpu"},
            f"/status slots: {slots}")
    require(all("is_default" not in s for s in slots), f"/status slot reports is_default: {slots}")
    resident = {s["model"] for s in slots if s.get("is_loaded")}
    require(client.model in resident, f"{client.model} not resident in slots: {slots}")
    require(body.get("loaded_models", 0) >= 1, f"loaded_models: {body}")


GROUPS = {
    "api_contract": [
        ("model_required", check_model_required),
        ("status_shape", check_status_shape),
    ],
    "content_formats": [("content_formats", check_content_formats)],
    "prompt_ledger": [("prompt_ledger", check_prompt_ledger)],
    "concurrency": [
        ("same_model_parallel", check_same_model_parallel),
        ("readonly_endpoints_hammer", check_readonly_endpoints_hammer),
        ("stop_spam", check_stop_spam),
        ("clear_spam", check_clear_spam),
        ("unload_during_stream", check_unload_during_stream),
    ],
    "disconnect": [
        ("disconnect_stream", check_disconnect_stream),
        ("disconnect_prefill", check_disconnect_prefill),
        ("disconnect_queued", check_disconnect_queued),
    ],
}


def pick_model(client):
    status, _, raw = client.request("GET", "/v1/models", timeout=15)
    if status != 200:
        return None
    try:
        models = json.loads(raw.decode("utf-8", "replace")).get("data", [])
    except ValueError:
        return None
    loaded = [m for m in models if m.get("is_loaded")]
    chosen = (loaded or models or [{}])[0]
    return chosen.get("id") or chosen.get("name")


def main():
    parser = argparse.ArgumentParser(description="GenieAPIService HTTP black-box contract checks "
                                                 "against an already running service")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8910)
    parser.add_argument("--model", default=None, help="model name; default: first loaded model of /v1/models")
    parser.add_argument("--group", default="all", help="comma list of: all," + ",".join(GROUPS))
    parser.add_argument("--timeout", type=int, default=180)
    args = parser.parse_args()

    client = Client(args.host, args.port, args.model, args.timeout)
    if not client.alive():
        print(f"service at {client.base} is not reachable", file=sys.stderr)
        return 2
    client.model = args.model or pick_model(client)
    if not client.model:
        print("no model available; pass --model", file=sys.stderr)
        return 2

    selected = list(GROUPS) if args.group == "all" else [g.strip() for g in args.group.split(",")]
    unknown = [g for g in selected if g not in GROUPS]
    if unknown:
        print(f"unknown group(s): {unknown}", file=sys.stderr)
        return 2

    failed = 0
    print(f"model={client.model} groups={selected}", flush=True)
    for group in selected:
        for name, fn in GROUPS[group]:
            started = time.monotonic()
            try:
                fn(client)
                outcome, detail = "PASS", ""
            except Skip as exc:
                outcome, detail = "SKIP", str(exc)
            except Exception as exc:
                outcome, detail = "FAIL", f"{type(exc).__name__}: {exc}"
                failed += 1
            print(f"[{outcome}] {group}/{name} ({time.monotonic() - started:.1f}s) {detail}", flush=True)
            if outcome == "FAIL" and not client.alive():
                print("service is down; aborting remaining checks", flush=True)
                return 1
    print(f"failed={failed}", flush=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
