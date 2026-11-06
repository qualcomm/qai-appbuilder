# ---------------------------------------------------------------------
# Copyright (c) 2024 Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

# Command compatible with the C++ GenieAPIClient (samples/genie/c++/Service/examples/GenieAPIClient).
# python GenieAPIClient.py --prompt "How to fish?"
# python GenieAPIClient.py --prompt "How to fish?" --stream
# python GenieAPIClient.py --prompt "Describe it" --img photo.png --v2

import base64
import http.client
import json
import os
import sys

USAGE = """Usage: python GenieAPIClient.py [options]
  --prompt TEXT     Question to ask. Default is `Hi!`
  --system TEXT     System prompt. Default is `You are a helpful assistant.`
  --model NAME      Model name. Default is `IBM-Granite-v3.1-8B`
  --stream          Answer in stream mode
  --img PATH        Local image file
  --audio PATH      Local audio file
  --host HOST:PORT  Server address. Default is `127.0.0.1:8910`
  --key KEY         Value of the `api_key` field of the request
  --raw_file PATH   Send the JSON file as the request body
  --bench PATH      Ask every {question, img_path, out_path} item of a JSON list
  --v2              Use /v2/chat/completions instead of /v1/chat/completions
  -h, --help        Show this message"""

VALUE_OPTIONS = {
    "--prompt": "prompt",
    "--system": "system",
    "--model": "model",
    "--img": "picture_path",
    "--audio": "audio_path",
    "--host": "host",
    "--key": "key",
    "--raw_file": "raw_file",
    "--bench": "bench",
}

FORCE_STOP_BYTES = 500


class Options:
    def __init__(self):
        self.prompt = ""
        self.picture_path = ""
        self.audio_path = ""
        self.system = "You are a helpful assistant."
        self.model = "IBM-Granite-v3.1-8B"
        self.raw_file = ""
        self.stream = False
        self.api_v2 = False
        self.host = "127.0.0.1:8910"
        self.key = ""
        self.bench = ""


class Reply:
    def __init__(self):
        self.message = ""
        self.text = ""
        self.stopped = False
        self.reasoning_started = False
        self.reasoning_ended = False


def parse_args(argv):
    opts = Options()
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-h", "--help"):
            print(USAGE)
            sys.exit(0)
        elif arg == "--stream":
            opts.stream = True
        elif arg == "--v2":
            opts.api_v2 = True
        elif arg in VALUE_OPTIONS and i + 1 < len(argv):
            i += 1
            setattr(opts, VALUE_OPTIONS[arg], argv[i])
        i += 1
    return opts


def encode_binary(path):
    if not path:
        return ""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        raise RuntimeError("open file path:" + path + " failed\n")
    return base64.b64encode(data).decode("ascii")


def build_request_body(model, user, system, stream, key):
    body = {
        "model": model,
        "api_key": key,
        "stream": stream,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    return json.dumps(body, indent=4, ensure_ascii=False)


def build_user_content_v1(question, img_path, audio_path):
    return {
        "question": question,
        "image": encode_binary(img_path),
        "audio": encode_binary(audio_path),
    }


def build_messages_content(opts):
    if not opts.picture_path and not opts.audio_path:
        return opts.system, opts.prompt
    system = [{"type": "text", "text": opts.system}]
    user = [{"type": "text", "text": opts.prompt}]
    if opts.picture_path:
        user.append({
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + encode_binary(opts.picture_path)},
        })
    if opts.audio_path:
        user.append({
            "type": "input_audio",
            "input_audio": {"data": encode_binary(opts.audio_path)},
        })
    return system, user


def handle_stream_line(line, opts, reply):
    if not line.startswith("data: "):
        return True
    payload = line[6:]
    if payload == "[DONE]":
        return False
    try:
        frame = json.loads(payload)
        choices = frame.get("choices") if isinstance(frame, dict) else None
        if not choices:
            return True
        choice = choices[0]
        status_message = choice.get("status_message")
        if isinstance(status_message, str):
            print("[Status] " + status_message, flush=True)
        delta = choice.get("delta")
        if not isinstance(delta, dict):
            return True
        reasoning = delta.get("reasoning_content")
        if isinstance(reasoning, str):
            if not reply.reasoning_started:
                reply.reasoning_started = True
                print("\n[Thinking]\n", end="", flush=True)
            print(reasoning, end="", flush=True)
        part = delta.get("content")
        if isinstance(part, str):
            if reply.reasoning_started and not reply.reasoning_ended and part:
                reply.reasoning_ended = True
                print("\n[Answer]\n", end="", flush=True)
            print(part, end="", flush=True)
            reply.text += part
            if opts.bench and len(reply.text.encode("utf-8")) > FORCE_STOP_BYTES:
                print("force stop!", flush=True)
                reply.stopped = True
    except (ValueError, TypeError, AttributeError) as exc:
        print(str(exc), flush=True)
        print(line, flush=True)
    return True


def consume_stream(resp, opts, reply):
    chunks = []
    for raw in resp:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        chunks.append(line)
        if reply.stopped:
            continue
        if not handle_stream_line(line, opts, reply):
            break
    reply.message = "\n".join(chunks)
    if not reply.stopped:
        print(flush=True)


def report_plain_reply(reply):
    full_response = ""
    try:
        response_json = json.loads(reply.message)
        full_response = json.dumps(response_json, indent=4, ensure_ascii=False)
        msg = response_json["choices"][0]["message"]
        reasoning = msg.get("reasoning_content")
        if isinstance(reasoning, str):
            print("[Thinking]\n" + reasoning + "\n[Answer]", flush=True)
        content = msg["content"]
        if not isinstance(content, str):
            raise TypeError("type must be string, but is " + type(content).__name__)
        reply.text = content
        print(reply.text, flush=True)
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        print(str(exc), flush=True)
        print(full_response or reply.message, flush=True)


def do(opts, path, body):
    reply = Reply()
    headers = {"Content-Type": "application/json"}
    if opts.stream:
        print("reponse mode: stream mode", flush=True)
        headers["Accept"] = "text/event-stream"
    else:
        print("reponse mode: non-stream mode", flush=True)
    print("Response: ", flush=True)

    conn = None
    status = 0
    try:
        conn = http.client.HTTPConnection(opts.host)
        conn.request("POST", path, body=body.encode("utf-8"), headers=headers)
        resp = conn.getresponse()
        status = resp.status
        if opts.stream and status == 200:
            consume_stream(resp, opts, reply)
        else:
            reply.message = resp.read().decode("utf-8", errors="replace")
    except (OSError, http.client.HTTPException) as exc:
        print("curl_easy_perform() failed: " + str(exc), flush=True)
        return reply.text
    finally:
        if conn is not None:
            conn.close()

    if status != 200:
        print("get failed response code: " + str(status), flush=True)
        print("the error message: " + reply.message, flush=True)
        return reply.text

    if not opts.stream and reply.message:
        report_plain_reply(reply)
    return reply.text


def bench_loop(opts, path):
    if not opts.bench:
        return False
    try:
        with open(opts.bench, encoding="utf-8-sig") as f:
            entries = json.load(f)
    except OSError:
        raise RuntimeError("open file path: " + opts.bench + " failed")
    for entry in entries:
        body = build_request_body(
            opts.model,
            build_user_content_v1(entry["question"], entry["img_path"], ""),
            opts.system, opts.stream, opts.key)
        print("current: " + entry["out_path"], flush=True)
        result = do(opts, path, body)
        if not result:
            print("Error occur, exit", flush=True)
            return True
        parent = os.path.dirname(entry["out_path"])
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(entry["out_path"], "w", encoding="utf-8") as out:
            out.write(result)
    return True


def main(argv):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    opts = parse_args(argv)
    path = "/v2/chat/completions" if opts.api_v2 else "/v1/chat/completions"
    print("url path: http://" + opts.host + path, flush=True)

    try:
        if bench_loop(opts, path):
            return 1
        if opts.raw_file:
            try:
                with open(opts.raw_file, encoding="utf-8-sig") as f:
                    raw = json.load(f)
            except OSError:
                print("open file path:" + opts.raw_file + " failed", flush=True)
                return 1
            opts.stream = bool(raw.get("stream", False)) if isinstance(raw, dict) else False
            body = json.dumps(raw, indent=4, ensure_ascii=False)
        else:
            if not opts.prompt:
                if not opts.picture_path and not opts.audio_path:
                    opts.prompt = "Hi!"
                else:
                    opts.prompt = "What is it descript?"
            system, user = build_messages_content(opts)
            body = build_request_body(opts.model, user, system, opts.stream, opts.key)
        do(opts, path, body)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
        print(str(exc), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
