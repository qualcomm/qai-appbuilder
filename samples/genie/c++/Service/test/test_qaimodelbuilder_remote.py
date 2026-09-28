"""QAIModelBuilder remote chat + multimodal upload verification.

Drives the real QAIModelBuilder HTTP surface end-to-end against an already
running QAIModelBuilder + GenieAPIService pair (either the "same-machine
two-process" topology or any other reachable deployment):
  - CSRF double-submit handshake (GET /api/system/health -> qai_csrf cookie)
  - create a conversation (POST /api/chat/conversations)
  - optionally upload an image/audio file (POST /api/images|audio/upload)
  - send a prompt over the SSE chat stream
    (GET /api/chat/conversations/{id}/stream)
  - for the image/audio scenarios, fetch the prompt-snapshot debug capture
    (GET /api/prompt-snapshot/{request_id}) and check the wire-level
    image_url.url / input_audio.data encoding contract QAIModelBuilder
    actually sent to GenieAPIService.

Consolidates two throwaway diagnostic scripts used during this
investigation session -- tmp/text_only_test.py (the "text" scenario,
verifying the local::<model> context_window() DI fix) and
tmp/mm_upload_test.py (the "image"/"audio" scenarios, verifying the
image/audio base64 encoding contract) -- into one reusable, parameterized
regression tool. The CSRF handshake / SSE parsing / multimodal encoding
logic is kept as-is; only host/port/model/file-path/output-path values
were moved from hardcoded module constants into CLI flags.
"""
from __future__ import annotations

import argparse
import base64
import http.client
import io
import json
import sys
import urllib.parse

if sys.platform == "win32" and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    # Idempotent wrap: avoid double-wrapping (would close the shared buffer
    # on GC and break a sibling module that already wrapped it once).
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BASE_HOST = "127.0.0.1"
BASE_PORT = 8989


def _conn():
    return http.client.HTTPConnection(BASE_HOST, BASE_PORT, timeout=180)


def _request(method, path, cookies, body=None, extra_headers=None, params=None):
    if params:
        path = path + "?" + urllib.parse.urlencode(params)
    conn = _conn()
    headers = {}
    if cookies:
        headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in cookies.items())
    if body is not None:
        headers["Content-Type"] = "application/json"
    if extra_headers:
        headers.update(extra_headers)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    set_cookie = resp.getheader("Set-Cookie")
    return resp.status, raw, set_cookie, conn


def get_csrf():
    status, raw, set_cookie, conn = _request("GET", "/api/system/health", {})
    conn.close()
    cookies = {}
    if set_cookie:
        for part in set_cookie.split(","):
            kv = part.strip().split(";")[0]
            if "=" in kv:
                k, v = kv.split("=", 1)
                k = k.strip()
                if k == "qai_csrf":
                    cookies[k] = v.strip()
    if "qai_csrf" not in cookies:
        raise RuntimeError(f"no qai_csrf cookie in response headers: {set_cookie!r}")
    return cookies


def csrf_request(method, path, cookies, body=None, params=None):
    headers = {}
    if method.upper() not in ("GET", "HEAD", "OPTIONS"):
        headers["X-QAI-CSRF"] = cookies["qai_csrf"]
    status, raw, _, conn = _request(method, path, cookies, body=body, extra_headers=headers, params=params)
    conn.close()
    return status, raw


def sse_chat(cookies, conversation_id, prompt, model_id, tab_id):
    """GET the SSE stream endpoint and collect all events. Returns (status, events_list, raw_text)."""
    params = {"tab_id": tab_id, "prompt": prompt, "model_id": model_id}
    path = f"/api/chat/conversations/{conversation_id}/stream?" + urllib.parse.urlencode(params)
    conn = _conn()
    headers = {"Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items())}
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    status = resp.status
    events = []
    if status != 200:
        raw = resp.read()
        conn.close()
        return status, events, raw.decode("utf-8", errors="replace")
    buf = b""
    event_name = None
    data_lines = []
    full_text_parts = []
    while True:
        chunk = resp.read(4096)
        if not chunk:
            break
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            line = line.decode("utf-8", errors="replace").rstrip("\r")
            full_text_parts.append(line)
            stripped = line.strip()
            if not stripped:
                if event_name:
                    payload_raw = "\n".join(data_lines)
                    try:
                        payload = json.loads(payload_raw) if payload_raw else {}
                    except Exception:
                        payload = {"raw": payload_raw}
                    events.append((event_name, payload))
                    if event_name in ("done", "error"):
                        conn.close()
                        return status, events, "\n".join(full_text_parts)
                event_name, data_lines = None, []
                continue
            if stripped.startswith("event:"):
                event_name = stripped[6:].strip()
            elif stripped.startswith("data:"):
                data_lines.append(stripped[5:].strip())
    conn.close()
    return status, events, "\n".join(full_text_parts)


def find_request_id(events):
    """Search every event payload (recursively) for a request_id-like field."""
    def _walk(obj):
        if isinstance(obj, dict):
            for key in ("request_id", "requestId"):
                if key in obj and isinstance(obj[key], str):
                    return obj[key]
            for v in obj.values():
                found = _walk(v)
                if found:
                    return found
        elif isinstance(obj, list):
            for item in obj:
                found = _walk(item)
                if found:
                    return found
        return None

    for _name, payload in events:
        rid = _walk(payload)
        if rid:
            return rid
    return None


def run_text_only_scenario(model_name, prompt):
    """Plain-text-only SSE chat scenario (no upload).

    Verifies context_window() DI resolution for a given local model: the
    chat.model_context_length_missing error must not appear, and a real
    non-empty reply must come back.
    """
    cookies = get_csrf()
    result = {"kind": "text"}
    status, raw = csrf_request("POST", "/api/chat/conversations", cookies, body={"title": "text-only-test"})
    result["create_conv_status"] = status
    if status not in (200, 201):
        result["error"] = f"create conversation failed: {raw[:300]!r}"
        return result
    conv = json.loads(raw)
    conversation_id = conv.get("id")
    result["conversation_id"] = conversation_id

    tab_id = f"text-only-tab-{conversation_id}"
    model_id = f"local::{model_name}"
    status, events, raw_text = sse_chat(cookies, conversation_id, prompt, model_id, tab_id)
    result["sse_status"] = status
    result["sse_event_names"] = [e for e, _ in events]
    if status != 200:
        result["error"] = f"SSE non-200: {status}; body={raw_text[:500]!r}"
        return result

    error_events = [p for n, p in events if n == "error"]
    message_events = [p for n, p in events if n == "message"]
    result["error_events"] = error_events
    result["message_events"] = message_events
    joined_text = " ".join(json.dumps(p, ensure_ascii=False) for p in message_events)
    result["reply_text_preview"] = joined_text[:500]
    result["reply_nonempty"] = bool(joined_text.strip())
    result["has_context_length_missing_error"] = any(
        (isinstance(p, dict) and p.get("code") == "chat.model_context_length_missing")
        for p in error_events + message_events
    )
    return result


def run_multimodal_scenario(kind, file_path, mime_type, model_name, prompt_prefix):
    """Image/audio upload -> markdown-embedded prompt -> SSE chat ->
    prompt-snapshot debug capture.

    Checks the wire-level image_url.url / input_audio.data encoding
    contract (data-URI prefix for images, pure base64 without any prefix
    for audio).
    """
    cookies = get_csrf()
    result = {"kind": kind}
    title = f"mm-upload-test-{kind}"
    status, raw = csrf_request("POST", "/api/chat/conversations", cookies, body={"title": title})
    result["create_conv_status"] = status
    if status not in (200, 201):
        result["error"] = f"create conversation failed: {raw[:300]!r}"
        return result
    conv = json.loads(raw)
    conversation_id = conv.get("id")
    result["conversation_id"] = conversation_id

    with open(file_path, "rb") as f:
        content = f.read()
    b64 = base64.b64encode(content).decode("ascii")
    result["file_size_bytes"] = len(content)

    upload_path = "/api/images/upload" if kind == "image" else "/api/audio/upload"
    upload_body = {
        "conv_id": conversation_id,
        "msg_id": f"mm-test-{kind}",
        "b64_data": b64,
        "mime_type": mime_type,
    }
    status, raw = csrf_request("POST", upload_path, cookies, body=upload_body)
    result["upload_status"] = status
    if status not in (200, 201):
        result["error"] = f"{upload_path} failed: {raw[:300]!r}"
        return result
    upload_resp = json.loads(raw)
    upload_url = upload_resp.get("url")
    result["upload_url"] = upload_url
    if not upload_url:
        result["error"] = f"upload response missing url: {upload_resp}"
        return result

    filename = file_path.replace("/", "\\").split("\\")[-1]
    if kind == "image":
        prompt_media = f"![{filename}]({upload_url})"
    else:
        prompt_media = f"[audio:{filename}]({upload_url})"
    prompt = prompt_prefix + "\n" + prompt_media

    tab_id = f"mm-test-tab-{conversation_id}"
    model_id = f"local::{model_name}"
    status, events, raw_text = sse_chat(cookies, conversation_id, prompt, model_id, tab_id)
    result["sse_status"] = status
    result["sse_event_names"] = [e for e, _ in events]
    if status != 200:
        result["error"] = f"SSE non-200: {status}; body={raw_text[:500]!r}"
        return result

    error_events = [p for n, p in events if n == "error"]
    if error_events:
        result["error"] = f"SSE reported error event(s): {error_events}"
    message_events = [p for n, p in events if n == "message"]
    joined_text = " ".join(json.dumps(p, ensure_ascii=False) for p in message_events)
    result["reply_text_preview"] = joined_text[:300]
    result["reply_nonempty"] = bool(joined_text.strip())

    request_id = find_request_id(events)
    result["request_id"] = request_id
    if not request_id:
        result["snapshot_error"] = "no request_id found in SSE events"
        return result

    status, raw = csrf_request("GET", f"/api/prompt-snapshot/{request_id}", cookies)
    result["snapshot_status"] = status
    if status != 200:
        result["snapshot_error"] = f"prompt-snapshot fetch failed: {raw[:500]!r}"
        return result
    snapshot = json.loads(raw)
    messages = snapshot.get("messages") or []
    result["snapshot_message_roles"] = [m.get("role") for m in messages if isinstance(m, dict)]

    # Find the most recent user message with array content (should contain
    # the image_url / input_audio block we just uploaded).
    target_block = None
    for m in reversed(messages):
        if not isinstance(m, dict) or m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("image_url", "input_audio"):
                    target_block = block
                    break
        if target_block:
            break
    result["found_content_block"] = target_block is not None
    if target_block is None:
        result["snapshot_error"] = "no image_url/input_audio content block found in captured user message"
        result["all_user_messages_content"] = [
            m.get("content") for m in messages if isinstance(m, dict) and m.get("role") == "user"
        ]
        return result

    if target_block.get("type") == "image_url":
        url_val = target_block.get("image_url", {}).get("url", "")
        result["image_url_prefix"] = url_val[:60]
        result["image_url_len"] = len(url_val)
        result["image_url_has_data_uri_prefix"] = url_val.startswith("data:") and ";base64," in url_val[:100]
    else:
        data_val = target_block.get("input_audio", {}).get("data", "")
        result["input_audio_prefix"] = data_val[:60]
        result["input_audio_len"] = len(data_val)
        result["input_audio_has_no_prefix"] = not data_val.startswith("data:")
        # A pure base64 string should decode cleanly.
        try:
            base64.b64decode(data_val[:200] + "==", validate=False)
            result["input_audio_looks_like_pure_base64"] = True
        except Exception as exc:  # noqa: BLE001
            result["input_audio_looks_like_pure_base64"] = False
            result["input_audio_decode_error"] = str(exc)

    return result


def main():
    global BASE_HOST, BASE_PORT

    parser = argparse.ArgumentParser(
        description="QAIModelBuilder remote chat / multimodal upload verification "
                     "(requires an already-running QAIModelBuilder + GenieAPIService pair; "
                     "this script does not manage any process lifecycle itself).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--host", default="127.0.0.1", help="QAIModelBuilder API host")
    parser.add_argument("--port", type=int, default=8989, help="QAIModelBuilder API port")
    parser.add_argument("--model", default="qwen2.5_omini_8480-2.42",
                         help="Local model id, without the 'local::' prefix (script adds it)")
    parser.add_argument("--scenario", choices=["text", "image", "audio", "all"], default="all",
                         help="Which scenario(s) to run")
    parser.add_argument("--prompt", default="请用一句话回答：1加1等于几？",
                         help="Prompt text used by the 'text' scenario")
    parser.add_argument("--mm_prompt_prefix", default="请描述我提供的素材，并用一句话回答。",
                         help="Prompt prefix used by the 'image'/'audio' scenarios, before the "
                              "markdown-embedded upload reference")
    parser.add_argument("--image_path", default=None,
                         help="Path to a test image file (required for the 'image' scenario)")
    parser.add_argument("--image_mime", default="image/png", help="MIME type of --image_path")
    parser.add_argument("--audio_path", default=None,
                         help="Path to a test audio file (required for the 'audio' scenario)")
    parser.add_argument("--audio_mime", default="audio/wav", help="MIME type of --audio_path")
    parser.add_argument("--out", default=None,
                         help="Also write the JSON result to this path (default: stdout only)")
    args = parser.parse_args()

    BASE_HOST = args.host
    BASE_PORT = args.port

    scenarios = ["text", "image", "audio"] if args.scenario == "all" else [args.scenario]
    results = {}

    for kind in scenarios:
        if kind == "text":
            results["text"] = run_text_only_scenario(args.model, args.prompt)
        elif kind == "image":
            if not args.image_path:
                results["image"] = {"kind": "image", "error": "--image_path is required for the 'image' scenario"}
                continue
            results["image"] = run_multimodal_scenario(
                "image", args.image_path, args.image_mime, args.model, args.mm_prompt_prefix)
        elif kind == "audio":
            if not args.audio_path:
                results["audio"] = {"kind": "audio", "error": "--audio_path is required for the 'audio' scenario"}
                continue
            results["audio"] = run_multimodal_scenario(
                "audio", args.audio_path, args.audio_mime, args.model, args.mm_prompt_prefix)

    output = json.dumps(results, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(output)
    print(output)

    # Original diagnostic scripts had no exit-code convention; this adds a
    # minimal one (non-zero when any scenario reports an "error") so the
    # script can later be wired into automated regression gating.
    if any(isinstance(r, dict) and r.get("error") for r in results.values()):
        sys.exit(1)


if __name__ == "__main__":
    main()
