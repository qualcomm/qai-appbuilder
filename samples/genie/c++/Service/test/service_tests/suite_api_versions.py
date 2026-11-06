#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import base64
import json
import queue
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .common import CrashEvent, TestResult, detect_modality, infer_backend, requests, wait_http_ok
from .managers import ServiceManager


def _result(name, passed, status_code=0, latency_ms=0, detail="", skipped=False, crashed=False, model_name="_api_versions_"):
    return TestResult(
        name=name,
        round_num=1,
        model_name=model_name,
        passed=passed,
        status_code=status_code,
        latency_ms=latency_ms,
        detail=detail,
        crashed=crashed,
        skipped=skipped,
    )


def _pick_models(models):
    by_device = {"gpu": [], "cpu": [], "npu": []}
    for model in models:
        by_device[infer_backend(model)[1]].append(model)
    preferred_gpu = next((m for m in by_device["gpu"] if "gemma-3n-e2b" in m.lower()), None)
    gpu = preferred_gpu or (by_device["gpu"][0] if by_device["gpu"] else None)
    if gpu is None:
        gpu = next((m for m in by_device["cpu"] if "gpt-oss-20b-mnn" in m.lower()), None)
    if gpu is None and by_device["cpu"]:
        gpu = by_device["cpu"][0]
    npu = next((m for m in by_device["npu"] if "qwen2.5_omini" in m.lower()), None)
    if npu is None:
        npu = next((m for m in by_device["npu"] if "qwen3_vl_4b" in m.lower()), None)
    if npu is None and by_device["npu"]:
        npu = by_device["npu"][0]
    return gpu, npu


def _model_config(models_root, model):
    model_dir = Path(models_root) / model
    return next((path for path in (model_dir / "genie_config.json", model_dir / "config.json") if path.exists()), None)


def _loaded_names(status):
    if not isinstance(status, dict):
        return []
    return [slot.get("model", "") for slot in status.get("slots", [])
            if isinstance(slot, dict) and slot.get("is_loaded")]


def _chat_request(base_url, path, model=None, stream=False, timeout=300):
    body = {
        "messages": [{"role": "user", "content": "Reply with one short word."}],
        "stream": stream,
        "max_tokens": 32,
    }
    if model is not None:
        body["model"] = model
    started = time.time()
    response = requests.post(base_url + path, json=body, timeout=timeout, stream=stream)
    return response, (time.time() - started) * 1000


def _response_content(response):
    try:
        message = response.json()["choices"][0]["message"]["content"]
        return message if isinstance(message, str) else json.dumps(message, ensure_ascii=False)
    except Exception:
        return ""


def _stream_signature(response):
    shapes = set()
    done = False
    for raw_line in response.iter_lines(decode_unicode=True):
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            done = True
            continue
        try:
            frame = json.loads(data)
            choice = (frame.get("choices") or [{}])[0]
            delta = choice.get("delta", {})
            shapes.add((tuple(sorted(frame)), tuple(sorted(choice)), tuple(sorted(delta))))
        except Exception:
            return False, shapes, done
    return response.status_code == 200 and bool(shapes) and done, shapes, done


def _service_contract_tests(base_url, results, gpu, npu):
    base_model = gpu or npu
    if not base_model:
        results.append(_result("service protocol contracts", False, detail="未发现可用于服务端契约测试的模型", skipped=True))
        return

    image_responses = []
    try:
        for path in ("/v1/images/generations", "/v2/images/generations", "/images/generations"):
            image_responses.append(requests.post(base_url + path, json={}, timeout=30))
        passed = all(response.status_code == 501 and response.content == b"" for response in image_responses)
        results.append(_result("image generation routes retain the known 501 contract", passed,
                               image_responses[0].status_code,
                               detail="; ".join(f"{response.status_code}/{response.text}" for response in image_responses)))
    except Exception as exc:
        results.append(_result("image generation routes retain the known 501 contract", False, detail=repr(exc)))

    try:
        response, latency = _chat_request(base_url, "/v1/chat/completions", base_model)
        content = _response_content(response)
        results.append(_result("v1 chat loads the requested model", response.status_code == 200 and bool(content.strip()),
                               response.status_code, latency, f"model={base_model}; answer_nonempty={bool(content.strip())}",
                               model_name=base_model))
    except Exception as exc:
        results.append(_result("v1 chat loads the requested model", False, detail=repr(exc), model_name=base_model))
        return

    try:
        response, latency = _chat_request(base_url, "/v1/chat/completions", "__api_versions_missing_model__")
        status = requests.get(base_url + "/v2/status", timeout=15)
        names = _loaded_names(status.json())
        passed = response.status_code == 404 and base_model in names
        results.append(_result("v1 unknown model does not evict the resident model", passed, response.status_code,
                               latency, f"resident_models_after_404={names}; expected_to_keep={base_model}", model_name=base_model))
    except Exception as exc:
        results.append(_result("v1 unknown model does not evict the resident model", False, detail=repr(exc), model_name=base_model))

    if gpu and npu and gpu != npu and infer_backend(gpu)[1] != infer_backend(npu)[1]:
        try:
            response, latency = _chat_request(base_url, "/v1/chat/completions", gpu)
            first_ok = response.status_code == 200 and bool(_response_content(response).strip())
            response, latency2 = _chat_request(base_url, "/v1/chat/completions", npu)
            second_ok = response.status_code == 200 and bool(_response_content(response).strip())
            status = requests.get(base_url + "/v2/status", timeout=15)
            names = _loaded_names(status.json())
            passed = first_ok and second_ok and status.status_code == 200 and len(names) == 1 and npu in names
            results.append(_result("v1 switch evicts the previous device-family model", passed, status.status_code,
                                   latency + latency2, f"models_after_switch={names}; expected_only={npu}", model_name=npu))
        except Exception as exc:
            results.append(_result("v1 switch evicts the previous device-family model", False, detail=repr(exc),
                                   model_name=npu))
    else:
        results.append(_result("v1 switch evicts the previous device-family model", False,
                               detail="缺少两个不同设备族模型", skipped=True))

    try:
        response, latency = _chat_request(base_url, "/v1/chat/completions")
        results.append(_result("v1 chat falls back to the loaded model", response.status_code == 200 and
                               bool(_response_content(response).strip()), response.status_code, latency,
                               f"answer_nonempty={bool(_response_content(response).strip())}"))
    except Exception as exc:
        results.append(_result("v1 chat falls back to the loaded model", False, detail=repr(exc)))

    for name, method, path, body in (
        ("v1 status uses the legacy shape", "get", "/v1/status", None),
        ("v1 contextsize uses the legacy shape", "post", "/v1/contextsize", {}),
        ("v1 profile returns the raw profile response", "get", "/v1/profile", None),
    ):
        try:
            response = getattr(requests, method)(base_url + path, json=body, timeout=30) if method == "post" else requests.get(base_url + path, timeout=30)
            if name.endswith("status uses the legacy shape"):
                data = response.json()
                passed = response.status_code == 200 and isinstance(data.get("loading"), str)
            elif name.endswith("contextsize uses the legacy shape"):
                data = response.json()
                passed = response.status_code == 200 and isinstance(data.get("contextsize"), int)
            else:
                data = response.json() if response.content else {}
                passed = response.status_code == 200 and isinstance(data, dict) and "models" not in data
            results.append(_result(name, passed, response.status_code, detail=response.text[:200]))
        except Exception as exc:
            results.append(_result(name, False, detail=repr(exc)))

    try:
        response = requests.post(base_url + "/v1/clear", json={}, timeout=30)
        results.append(_result("v1 clear has an empty response body", response.status_code == 200 and response.content == b"",
                               response.status_code, detail=response.text[:200]))
        response = requests.post(base_url + "/v1/stop", json={"text": "stop"}, timeout=30)
        results.append(_result("v1 stop has an empty response body", response.status_code == 200 and response.content == b"",
                               response.status_code, detail=response.text[:200]))
        response = requests.post(base_url + "/v1/textsplitter", json={"text": "one two three"}, timeout=60)
        passed = response.status_code == 200 and isinstance(response.json().get("content"), list)
        results.append(_result("v1 textsplitter falls back to the loaded model", passed, response.status_code,
                               detail=response.text[:200]))
        response = requests.post(base_url + "/v2/textsplitter", json={"text": "one two three"}, timeout=30)
        passed = response.status_code == 400 and response.json().get("error") == "model is required"
        results.append(_result("v2 textsplitter requires model", passed, response.status_code,
                               detail=response.text[:200]))
    except Exception as exc:
        results.append(_result("v1 control and textsplitter responses", False, detail=repr(exc)))


    try:
        response, latency = _chat_request(base_url, "/v2/chat/completions")
        passed = response.status_code == 400 and response.json().get("error") == "model is required"
        results.append(_result("v2 chat requires model", passed, response.status_code, latency, response.text[:200]))
    except Exception as exc:
        results.append(_result("v2 chat requires model", False, detail=repr(exc)))

    response_signatures = {}
    for path in ("/v1/models", "/v2/models", "/models"):
        try:
            response_signatures[path] = requests.get(base_url + path, timeout=30)
        except Exception as exc:
            results.append(_result("models routes share the same response", False, detail=f"{path}: {exc!r}"))
            response_signatures = {}
            break
    if response_signatures:
        responses = list(response_signatures.values())
        passed = all(r.status_code == 200 for r in responses) and len({r.content for r in responses}) == 1
        results.append(_result("models routes share the same response", passed, responses[0].status_code,
                               detail="; ".join(f"{path}={len(response.content)} bytes" for path, response in response_signatures.items())))

    if npu and gpu and npu != gpu and infer_backend(npu)[1] != infer_backend(gpu)[1]:
        models_to_load = (gpu, npu)
    else:
        models_to_load = (base_model,)
    for model in models_to_load:
        try:
            response, latency = _chat_request(base_url, "/v2/chat/completions", model)
            passed = response.status_code == 200 and bool(_response_content(response).strip())
            results.append(_result(f"v2 chat answers with {model}", passed, response.status_code, latency,
                                   f"answer_nonempty={bool(_response_content(response).strip())}", model_name=model))
        except Exception as exc:
            results.append(_result(f"v2 chat answers with {model}", False, detail=repr(exc), model_name=model))

    if len(models_to_load) == 2:
        for model in models_to_load:
            try:
                response, latency = _chat_request(base_url, "/v2/chat/completions", model)
                passed = response.status_code == 200 and bool(_response_content(response).strip())
                results.append(_result(f"v2 resident model continues to answer: {model}", passed,
                                       response.status_code, latency, model_name=model))
            except Exception as exc:
                results.append(_result(f"v2 resident model continues to answer: {model}", False,
                                       detail=repr(exc), model_name=model))

    try:
        v1_response, v1_latency = _chat_request(base_url, "/v1/chat/completions", base_model)
        v2_response, v2_latency = _chat_request(base_url, "/v2/chat/completions", base_model)
        v1_fields = sorted(v1_response.json()) if v1_response.status_code == 200 else []
        v2_fields = sorted(v2_response.json()) if v2_response.status_code == 200 else []
        results.append(_result("v1 and v2 non-stream chat fields match",
                               bool(v1_fields) and v1_fields == v2_fields,
                               v2_response.status_code, v1_latency + v2_latency,
                               f"v1_fields={v1_fields}; v2_fields={v2_fields}", model_name=base_model))
        v1_stream, _ = _chat_request(base_url, "/v1/chat/completions", base_model, stream=True)
        v1_valid, v1_shapes, v1_done = _stream_signature(v1_stream)
        v1_stream.close()
        v2_stream, _ = _chat_request(base_url, "/v2/chat/completions", base_model, stream=True)
        v2_valid, v2_shapes, v2_done = _stream_signature(v2_stream)
        v2_stream.close()
        passed = v1_valid and v2_valid and v1_done and v2_done and v1_shapes == v2_shapes
        results.append(_result("v1 and v2 streaming frames match and end with DONE", passed, 200,
                               detail=f"v1_shapes={v1_shapes}; v2_shapes={v2_shapes}; done={v1_done}/{v2_done}",
                               model_name=base_model))
    except Exception as exc:
        results.append(_result("v1 and v2 chat response shapes match", False, detail=repr(exc), model_name=base_model))

    for version in (1, 2):
        try:
            response, latency = _chat_request(base_url, f"/v{version}/completions", base_model)
            passed = response.status_code == 200 and bool(_response_content(response).strip())
            results.append(_result(f"v{version} completions alias uses the chat handler", passed,
                                   response.status_code, latency, model_name=base_model))
        except Exception as exc:
            results.append(_result(f"v{version} completions alias uses the chat handler", False,
                                   detail=repr(exc), model_name=base_model))

    try:
        status = requests.get(base_url + "/v2/status", timeout=30)
        data = status.json()
        expected_count = 2 if len(models_to_load) == 2 else 1
        names = _loaded_names(data)
        passed = (status.status_code == 200 and isinstance(data.get("slots"), list) and
                  data.get("loaded_models") == expected_count and all(model in names for model in models_to_load))
        results.append(_result("v2 status reports resident device slots", passed, status.status_code,
                               detail=f"loaded_models={data.get('loaded_models')}; slots={names}"))
        if len(models_to_load) == 2:
            results.append(_result("v2 keeps models from distinct device families resident", passed,
                                   status.status_code, detail=f"loaded_models={data.get('loaded_models')}; slots={names}"))
        else:
            results.append(_result("v2 keeps models from distinct device families resident", False,
                                   status.status_code, detail="缺少两个不同设备族模型", skipped=True))
        v1_status = requests.get(base_url + "/v1/status", timeout=30)
        no_prefix_status = requests.get(base_url + "/status", timeout=30)
        results.append(_result("no-prefix status matches v1 byte-for-byte",
                               no_prefix_status.status_code == v1_status.status_code and no_prefix_status.content == v1_status.content
                               and isinstance(no_prefix_status.json().get("loading"), str),
                               no_prefix_status.status_code, detail=f"v1={v1_status.text[:160]}; no_prefix={no_prefix_status.text[:160]}"))
    except Exception as exc:
        results.append(_result("v2 status reports resident device slots", False, detail=repr(exc)))
        results.append(_result("v2 keeps models from distinct device families resident", False, detail=repr(exc)))

    for name, method, path, body in (
        ("v2 contextsize uses the models array", "post", "/v2/contextsize", {"model": base_model}),
        ("v2 profile uses the models array", "get", "/v2/profile", None),
    ):
        try:
            response = getattr(requests, method)(base_url + path, json=body, timeout=30) if method == "post" else requests.get(base_url + path, timeout=30)
            data = response.json()
            passed = response.status_code == 200 and isinstance(data.get("models"), list) and isinstance(data.get("loaded_models"), int)
            results.append(_result(name, passed, response.status_code, detail=response.text[:250]))
            if path == "/v2/contextsize":
                v1_context = requests.post(base_url + "/v1/contextsize", json=body, timeout=30)
                alias = requests.post(base_url + "/contextsize", json=body, timeout=30)
                results.append(_result("no-prefix contextsize matches v1 byte-for-byte",
                                       alias.status_code == v1_context.status_code and alias.content == v1_context.content
                                       and isinstance(alias.json().get("contextsize"), int),
                                       alias.status_code, detail=f"v1={v1_context.text[:160]}; no_prefix={alias.text[:160]}"))
        except Exception as exc:
            results.append(_result(name, False, detail=repr(exc)))

    if models_to_load:
        model = models_to_load[0]
        try:
            response = requests.post(base_url + "/v2/clear", json={"model": model}, timeout=30)
            v2_clear = response.status_code == 200 and response.json().get("status") == "cleared"
            results.append(_result("v2 clear returns the cleared status", v2_clear, response.status_code,
                                   detail=response.text[:200], model_name=model))
            v1_clear = requests.post(base_url + "/v1/clear", json={"model": model}, timeout=30)
            alias = requests.post(base_url + "/clear", json={"model": model}, timeout=30)
            passed = alias.status_code == 200 and alias.content == v1_clear.content == b""
            results.append(_result("no-prefix clear matches v1 with an empty body", passed, alias.status_code,
                                   detail=f"v1={v1_clear.text}; unprefixed={alias.text}", model_name=model))
        except Exception as exc:
            results.append(_result("no-prefix clear matches v1 with an empty body", False, detail=repr(exc)))

        try:
            legacy_status = requests.get(base_url + "/status", timeout=15).json()
            legacy_loading = str(legacy_status.get("loading", "")).strip().lower()
            legacy_context = requests.post(base_url + "/contextsize", json={"model": model}, timeout=15)
            legacy_value = legacy_context.json().get("contextsize") if legacy_context.status_code == 200 else None
            legacy_clear = requests.post(base_url + "/clear", timeout=30)
            passed = (legacy_loading == "0" and isinstance(legacy_value, int) and legacy_value > 0
                      and legacy_clear.status_code < 400)
            results.append(_result("legacy client probe sequence works on the no-prefix routes", passed,
                                   legacy_clear.status_code,
                                   detail=f"status={legacy_status}; contextsize={legacy_value}; clear={legacy_clear.status_code}",
                                   model_name=model))
        except Exception as exc:
            results.append(_result("legacy client probe sequence works on the no-prefix routes", False,
                                   detail=repr(exc), model_name=model))

        try:
            response, latency = _chat_request(base_url, "/chat/completions")
            content = _response_content(response) if response.status_code == 200 else ""
            results.append(_result("no-prefix chat follows v1 and falls back to the loaded model",
                                   response.status_code == 200 and bool(content.strip()), response.status_code,
                                   latency, f"answer_nonempty={bool(content.strip())}"))
        except Exception as exc:
            results.append(_result("no-prefix chat follows v1 and falls back to the loaded model", False,
                                   detail=repr(exc)))

        for path in ("/v2/textsplitter", "/v1/textsplitter"):
            body = {"model": model, "text": "one two three", "max_length": 8}
            try:
                response = requests.post(base_url + path, json=body, timeout=60)
                passed = response.status_code == 200 and isinstance(response.json().get("content"), list)
                results.append(_result(f"{path} route responds", passed, response.status_code, detail=response.text[:200], model_name=model))
            except Exception as exc:
                results.append(_result(f"{path} route responds", False, detail=repr(exc), model_name=model))

        for path, expected in (("/v1/stop", ""), ("/v2/stop", "stopped")):
            try:
                response = requests.post(base_url + path, json={"model": model, "text": "stop"}, timeout=30)
                if path.startswith("/v1"):
                    passed = response.status_code == 200 and response.content == b""
                else:
                    passed = response.status_code == 200 and response.json().get("status") == expected
                results.append(_result(f"{path} response contract", passed, response.status_code,
                                       detail=response.text[:200], model_name=model))
            except Exception as exc:
                results.append(_result(f"{path} response contract", False, detail=repr(exc), model_name=model))

        try:
            response = requests.post(base_url + "/v2/unload", json={}, timeout=30)
            results.append(_result("v2 unload requires model", response.status_code == 400 and
                                   response.json().get("error") == "model is required",
                                   response.status_code, detail=response.text[:200], model_name=model))
            response = requests.post(base_url + "/v2/unload", json={"model": model}, timeout=120)
            passed = response.status_code == 200 and response.json().get("model") == model
            results.append(_result("v2 unload removes the named model", passed, response.status_code, detail=response.text[:200], model_name=model))
        except Exception as exc:
            results.append(_result("v2 unload removes the named model", False, detail=repr(exc), model_name=model))

    _v1_switch_edge_tests(base_url, results, gpu, npu)

    try:
        response = requests.post(base_url + "/v2/unload_all", timeout=120)
        passed = response.status_code == 200 and response.json().get("status") == "unloaded"
        results.append(_result("v2 unload_all clears all resident models", passed, response.status_code, detail=response.text[:200]))
    except Exception as exc:
        results.append(_result("v2 unload_all clears all resident models", False, detail=repr(exc)))


def _v1_switch_edge_tests(base_url, results, gpu, npu):
    if not gpu or not npu or gpu == npu or infer_backend(gpu)[1] == infer_backend(npu)[1]:
        results.append(_result("v1 concurrent model switches leave one resident", False,
                               detail="缺少两个不同设备族模型", skipped=True))
        results.append(_result("v1 switch interrupts an in-flight stream cleanly", False,
                               detail="缺少两个不同设备族模型", skipped=True))
        return

    try:
        resident_before = _loaded_names(requests.get(base_url + "/v2/status", timeout=30).json())
        unload = requests.post(base_url + "/v1/unload", timeout=120)
        unload_ok = unload.status_code == 200 and unload.content == b""
        if npu in resident_before:
            time.sleep(40)
        def switch_model(model):
            response, _ = _chat_request(base_url, "/v1/chat/completions", model, timeout=300)
            return response.status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(switch_model, model) for model in (gpu, npu)]
            request_statuses = [future.result(timeout=360) for future in futures]
        status = requests.get(base_url + "/v2/status", timeout=30)
        names = _loaded_names(status.json())
        passed = unload_ok and status.status_code == 200 and len(names) == 1 and names[0] in (gpu, npu)
        results.append(_result("v1 concurrent model switches leave one resident", passed, status.status_code,
                               detail=f"request_statuses={request_statuses}; final_models={names}"))
    except Exception as exc:
        results.append(_result("v1 concurrent model switches leave one resident", False, detail=repr(exc)))

    try:
        resident_before_warmup = _loaded_names(requests.get(base_url + "/v2/status", timeout=30).json())
        warmup, _ = _chat_request(base_url, "/v1/chat/completions", gpu, timeout=300)
        if warmup.status_code != 200:
            raise RuntimeError(f"stream warmup failed with status {warmup.status_code}")
        if npu in resident_before_warmup:
            time.sleep(40)
        started = threading.Event()
        stream_result = queue.Queue()

        def read_stream():
            body = {"model": gpu, "messages": [{"role": "user", "content":
                    "Write a long answer by repeating the word echo many times."}], "stream": True, "max_tokens": 2048}
            try:
                response = requests.post(base_url + "/v1/chat/completions", json=body, timeout=300, stream=True)
                done = False
                for raw_line in response.iter_lines(decode_unicode=True):
                    line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
                    if line.startswith("data: "):
                        started.set()
                        if line[6:] == "[DONE]":
                            done = True
                            break
                response.close()
                stream_result.put((response.status_code, done, None))
            except Exception as exc:
                stream_result.put((0, False, repr(exc)))
                started.set()

        reader = threading.Thread(target=read_stream, daemon=True)
        reader.start()
        stream_started = started.wait(timeout=120)
        if not stream_started:
            results.append(_result("v1 switch interrupts an in-flight stream cleanly", False,
                                   detail="流式请求在 120 秒内未产生首帧"))
            return
        switch_response, latency = _chat_request(base_url, "/v1/chat/completions", npu, timeout=300)
        reader.join(timeout=120)
        outcome = stream_result.get_nowait() if not reader.is_alive() and not stream_result.empty() else None
        followup, followup_latency = _chat_request(base_url, "/v1/chat/completions", npu, timeout=300)
        status = requests.get(base_url + "/v2/status", timeout=30)
        if outcome and outcome[1]:
            results.append(_result("v1 switch interrupts an in-flight stream cleanly", False,
                                   detail="流式请求在换模请求完成前自然结束，未能验证中断路径", skipped=True))
        else:
            passed = (switch_response.status_code == 200 and followup.status_code == 200 and
                      bool(_response_content(followup).strip()) and status.status_code == 200 and
                      status.json().get("loaded_models") == 1 and npu in _loaded_names(status.json()) and
                      outcome is not None and not reader.is_alive())
            results.append(_result("v1 switch interrupts an in-flight stream cleanly", passed,
                                   status.status_code, latency + followup_latency,
                                   f"stream={outcome}; switch_status={switch_response.status_code}; followup={followup.status_code}"))
    except Exception as exc:
        results.append(_result("v1 switch interrupts an in-flight stream cleanly", False, detail=repr(exc)))


def _v1_empty_state_contract(base_url, results):
    try:
        response = requests.post(base_url + "/v1/unload", timeout=120)
        passed = response.status_code == 200 and response.content == b""
        results.append(_result("v1 unload clears all models with an empty response", passed,
                               response.status_code, detail=response.text[:200]))
    except Exception as exc:
        results.append(_result("v1 unload clears all models with an empty response", False, detail=repr(exc)))

    try:
        status = requests.get(base_url + "/v2/status", timeout=30)
        v1_status = requests.get(base_url + "/v1/status", timeout=30)
        contextsize = requests.post(base_url + "/v1/contextsize", json={}, timeout=30)
        profile = requests.get(base_url + "/v1/profile", timeout=30)
        passed = (status.status_code == 200 and status.json().get("loaded_models") == 0
                  and v1_status.status_code == 200 and v1_status.json().get("loading") == "1")
        results.append(_result("v1 empty-state control queries keep their legacy shapes", passed,
                               v1_status.status_code, detail=f"v2_status={status.text[:80]}; v1_status={v1_status.text}; contextsize={contextsize.text}; profile={profile.status_code}/{profile.text[:80]}"))
        results.append(_result("v1 contextsize is zero without a model",
                               contextsize.status_code == 200 and contextsize.json().get("contextsize") == 0,
                               contextsize.status_code, detail=contextsize.text[:120]))
        results.append(_result("v1 profile is an empty body without a model",
                               profile.status_code == 200 and profile.content == b"",
                               profile.status_code, detail=profile.text[:120]))
    except Exception as exc:
        results.append(_result("v1 empty-state control queries keep their legacy shapes", False, detail=repr(exc)))

    try:
        response, latency = _chat_request(base_url, "/v1/chat/completions")
        passed = response.status_code == 400 and response.json().get("error") == "model is required"
        results.append(_result("v1 chat rejects a missing model when no model is loaded", passed,
                               response.status_code, latency, response.text[:200]))
    except Exception as exc:
        results.append(_result("v1 chat rejects a missing model when no model is loaded", False, detail=repr(exc)))


def _client_command(launcher, flags, host, version, placement):
    command = [*launcher, *flags]
    if "--host" not in command:
        command.extend(["--host", host])
    if version == 2:
        if placement == "start":
            command.insert(len(launcher), "--v2")
        elif placement == "middle":
            command.insert(command.index("--host"), "--v2")
        elif placement == "after_raw_file":
            command.insert(command.index("--raw_file") + 2, "--v2")
        else:
            command.append("--v2")
    return command


def _run_client_process(launcher, flags, host, version, placement="end", timeout=120):
    command = _client_command(launcher, flags, host, version, placement)
    completed = subprocess.run(command, cwd=str(Path(launcher[-1]).parent), capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=timeout)
    return completed.stdout + completed.stderr, completed.returncode


PYTHON_CLIENT = Path(__file__).resolve().parents[4] / "python" / "GenieAPIClient.py"
CPP_CLIENT_LABEL = "client stub"
PYTHON_CLIENT_LABEL = "python client stub"
TEXT_ONLY_FORMS = ("plain", "stream", "system_model", "key", "host")


def _expected_text_content(label, text):
    return [{"type": "text", "text": text}] if label == CPP_CLIENT_LABEL else text


def _canonical_body(raw):
    body = json.loads(raw)
    for message in body.get("messages", []):
        content = message.get("content")
        if isinstance(content, list) and len(content) == 1 and content[0].get("type") == "text":
            message["content"] = content[0].get("text")
    return body


def _check_client_failure_texts(label, launcher, host, results):
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        closed_host = f"127.0.0.1:{probe.getsockname()[1]}"
    cases = (
        ("error response", ["--prompt", "stub prompt", "--model", "stub-error"], host,
         ("get failed response code: 400", "the error message:", "stub error")),
        ("stream error response", ["--prompt", "stub prompt", "--model", "stub-error", "--stream"], host,
         ("get failed response code: 400", "the error message:", "stub error")),
        ("connection refused", ["--prompt", "stub prompt"], closed_host,
         ("curl_easy_perform() failed",)),
    )
    for name, flags, target, expected in cases:
        for version in (1, 2):
            try:
                output, _ = _run_client_process(launcher, flags, target, version, "end", timeout=60)
                passed = all(text in output for text in expected) and "stub reply" not in output
                passed = passed and f"/v{version}/chat/completions" in output
                results.append(_result(f"{label} {name} v{version}", passed, detail=output[-180:]))
            except Exception as exc:
                results.append(_result(f"{label} {name} v{version}", False, detail=repr(exc)))


def _run_client_stub_tests(args, out_dir, results):
    forms = ("plain", "stream", "system_model", "image", "audio", "raw_file", "bench", "key", "host",
             "error response", "stream error response", "connection refused")
    exe = Path(args.exe_dir) / "GenieAPIClient.exe" if args.exe_dir else None
    clients = []
    if not args.remote:
        if exe and exe.exists():
            clients.append((CPP_CLIENT_LABEL, [str(exe)]))
        if PYTHON_CLIENT.is_file():
            clients.append((PYTHON_CLIENT_LABEL, [sys.executable, str(PYTHON_CLIENT)]))
    available = {label for label, _ in clients}
    for label, missing in ((CPP_CLIENT_LABEL, "找不到 GenieAPIClient.exe"), (PYTHON_CLIENT_LABEL, "找不到 GenieAPIClient.py")):
        if label in available:
            continue
        reason = "远程模式不执行本地客户端桩测" if args.remote else missing
        for form in forms:
            for version in (1, 2):
                results.append(_result(f"{label} {form} v{version}", False, detail=reason, skipped=True))
    if len(clients) < 2:
        results.append(_result("python client request matches GenieAPIClient.exe", False,
                               detail="缺少 GenieAPIClient.exe 或 GenieAPIClient.py，无法对比请求", skipped=True))
    if not clients:
        return
    captured = {}

    work = Path(out_dir) / "api_versions_client_stub"
    work.mkdir(parents=True, exist_ok=True)
    image_path = work / "probe.png"
    audio_path = work / "probe.wav"
    image_bytes = b"\x89PNG\r\n\x1a\napi-version-probe"
    audio_bytes = b"RIFF\x10\x00\x00\x00WAVEapi-version-probe"
    image_path.write_bytes(image_bytes)
    audio_path.write_bytes(audio_bytes)
    raw_path = work / "raw.json"
    raw_body = {"model": "stub-model", "api_key": "", "stream": True,
                "messages": [{"role": "system", "content": "raw system"},
                             {"role": "user", "content": "raw prompt"}]}
    raw_path.write_text(json.dumps(raw_body), encoding="utf-8")
    bench_path = work / "bench.json"
    bench_path.write_text(json.dumps([{"question": "bench prompt", "img_path": "",
                                       "out_path": str(work / "bench-output.txt")}]), encoding="utf-8")
    records = []
    records_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length)
            body = json.loads(raw.decode("utf-8"))
            with records_lock:
                records.append({"path": self.path, "headers": dict(self.headers.items()), "body": body,
                                "raw": raw.decode("utf-8")})
            if body.get("model") == "stub-error":
                payload = json.dumps({"error": "stub error"}).encode("utf-8")
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
            elif body.get("stream"):
                payload = ("data: " + json.dumps({"choices": [{"delta": {"content": "stub reply"}}]}) +
                           "\n\ndata: [DONE]\n\n").encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
            else:
                payload = json.dumps({"choices": [{"message": {"content": "stub reply"}}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format, *args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    host = f"127.0.0.1:{server.server_address[1]}"
    cases = {
        "plain": (["--prompt", "stub prompt"], "start"),
        "stream": (["--prompt", "stub prompt", "--stream"], "end"),
        "system_model": (["--prompt", "stub prompt", "--system", "stub system", "--model", "stub-model"], "middle"),
        "image": (["--prompt", "describe", "--img", str(image_path)], "start"),
        "audio": (["--prompt", "transcribe", "--audio", str(audio_path)], "end"),
        "raw_file": (["--raw_file", str(raw_path)], "after_raw_file"),
        "bench": (["--bench", str(bench_path)], "end"),
        "key": (["--prompt", "stub prompt", "--key", "stub-key"], "middle"),
        "host": (["--prompt", "host probe", "--host", host], "end"),
    }
    runs = [(label, launcher, form, flags, placement)
            for form, (flags, placement) in cases.items() for label, launcher in clients]
    try:
        for label, launcher, form, flags, placement in runs:
            snapshots = {}
            for version in (1, 2):
                start = len(records)
                try:
                    if form == "bench" and (work / "bench-output.txt").exists():
                        (work / "bench-output.txt").unlink()
                    output, _ = _run_client_process(launcher, flags, host, version, placement, timeout=60)
                    record = records[start] if len(records) > start else None
                    expected_path = f"/v{version}/chat/completions"
                    passed = bool(record) and record["path"] == expected_path and "stub reply" in output
                    detail = f"path={record['path'] if record else None}; output={output[-180:]}"
                    if record:
                        body = record["body"]
                        passed = passed and record["headers"].get("Content-Type") == "application/json"
                        if form == "plain":
                            system = body["messages"][0]["content"]
                            user = body["messages"][1]["content"]
                            passed = passed and body.get("stream") is False and body.get("model") == "IBM-Granite-v3.1-8B"
                            passed = passed and system == _expected_text_content(label, "You are a helpful assistant.")
                            passed = passed and user == _expected_text_content(label, "stub prompt")
                        elif form == "stream":
                            passed = passed and body.get("stream") is True and record["headers"].get("Accept") == "text/event-stream"
                        elif form == "system_model":
                            passed = passed and body.get("model") == "stub-model"
                            passed = passed and body["messages"][0]["content"] == _expected_text_content(label, "stub system")
                        elif form == "image":
                            parts = body["messages"][1]["content"]
                            passed = passed and parts[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
                        elif form == "audio":
                            parts = body["messages"][1]["content"]
                            passed = passed and parts[1]["input_audio"]["data"] == base64.b64encode(audio_bytes).decode("ascii")
                        elif form == "raw_file":
                            passed = passed and body == raw_body
                        elif form == "bench":
                            passed = passed and body["messages"][1]["content"] == {"question": "bench prompt", "image": "", "audio": ""}
                            passed = passed and (work / "bench-output.txt").exists()
                        elif form == "key":
                            passed = passed and body.get("api_key") == "stub-key"
                        elif form == "host":
                            passed = passed and record["headers"].get("Host") == host
                        snapshots[version] = record
                        captured[(label, form, version)] = record
                    results.append(_result(f"{label} {form} v{version}", passed, 200 if record else 0,
                                           detail=detail))
                except Exception as exc:
                    results.append(_result(f"{label} {form} v{version}", False, detail=repr(exc)))
            first, second = snapshots.get(1), snapshots.get(2)
            if first and second:
                same_body = first["body"] == second["body"]
                same_headers = first["headers"] == second["headers"]
                results.append(_result(f"{label} {form} body and headers match across versions",
                                       same_body and same_headers, 200,
                                       detail=f"same_body={same_body}; same_headers={same_headers}"))
            if label == PYTHON_CLIENT_LABEL:
                for version in (1, 2):
                    cpp, py = captured.get((CPP_CLIENT_LABEL, form, version)), captured.get((label, form, version))
                    if not (cpp and py):
                        continue
                    same_raw = cpp["raw"] == py["raw"]
                    same_body = _canonical_body(cpp["raw"]) == _canonical_body(py["raw"])
                    same_request = same_body and (same_raw or form in TEXT_ONLY_FORMS)
                    same_path = cpp["path"] == py["path"]
                    same_type = cpp["headers"].get("Content-Type") == py["headers"].get("Content-Type")
                    same_stream_accept = (cpp["headers"].get("Accept") == "text/event-stream") == \
                                         (py["headers"].get("Accept") == "text/event-stream")
                    first_diff = next((i for i, (a, b) in enumerate(zip(cpp["raw"], py["raw"])) if a != b),
                                      min(len(cpp["raw"]), len(py["raw"])))
                    results.append(_result(f"python client stub {form} v{version} request matches GenieAPIClient.exe",
                                           same_request and same_path and same_type and same_stream_accept, 200,
                                           detail=f"same_raw={same_raw}; same_body={same_body}; same_path={same_path}; same_type={same_type}; "
                                                  f"same_stream_accept={same_stream_accept}; first_diff={first_diff}; "
                                                  f"cpp={cpp['raw'][first_diff:first_diff + 60]!r}; py={py['raw'][first_diff:first_diff + 60]!r}"))
        for label, launcher in clients:
            _check_client_failure_texts(label, launcher, host, results)
    finally:
        server.shutdown()
        server.server_close()
        server_thread.join(timeout=5)


def _asset(directory, suffixes):
    path = Path(directory) if directory else None
    if not path or not path.is_dir():
        return None
    return next((item for item in sorted(path.iterdir()) if item.is_file() and item.suffix.lower() in suffixes), None)


def _run_real_client_case(results, exe, args, version, name, flags, model, timeout=300):
    command_flags = list(flags)
    if "--model" not in command_flags and "--raw_file" not in command_flags and "--bench" not in command_flags:
        command_flags.extend(["--model", model])
    host = f"{args.host}:{args.port}"
    try:
        output_file = None
        if name == "bench":
            bench_entries = json.loads(Path(flags[1]).read_text(encoding="utf-8"))
            output_file = Path(bench_entries[0]["out_path"])
            if output_file.exists():
                output_file.unlink()
        output, _ = _run_client_process([str(exe)], command_flags, host, version, "end", timeout=timeout)
        response_text = output.split("Response: \n", 1)[-1].strip()
        answer = bool(response_text) and "get failed response code" not in output and \
                 "curl_easy_perform() failed" not in output and "stub reply" not in output
        if output_file:
            answer = answer and output_file.exists() and bool(output_file.read_text(encoding="utf-8", errors="replace").strip())
        results.append(_result(f"client real {name} v{version}", answer and f"/v{version}/chat/completions" in output,
                               200 if answer else 0, detail=output[-240:], model_name=model))
    except Exception as exc:
        results.append(_result(f"client real {name} v{version}", False, detail=repr(exc), model_name=model))


def _run_real_client_tests(args, out_dir, results, gpu, npu, service_ready=True):
    modes = ("plain", "stream", "system_model", "raw_file", "bench", "key")
    exe = Path(args.exe_dir) / "GenieAPIClient.exe" if args.exe_dir else None
    if not service_ready or args.remote or not exe or not exe.exists():
        reason = "服务未就绪" if not service_ready else ("远程模式跳过客户端真机用例" if args.remote else "找不到 GenieAPIClient.exe")
        for mode in modes + ("image", "audio", "raw_missing_model"):
            for version in (1, 2):
                results.append(_result(f"client real {mode} v{version}", False, detail=reason, skipped=True))
        return
    text_model = gpu or npu
    if not text_model:
        for mode in modes + ("image", "audio", "raw_missing_model"):
            for version in (1, 2):
                results.append(_result(f"client real {mode} v{version}", False, detail="缺少可用于文本验证的模型", skipped=True))
        return

    work = Path(out_dir) / "api_versions_client_real"
    work.mkdir(parents=True, exist_ok=True)
    raw_path = work / "raw.json"
    raw_body = {"model": text_model, "api_key": "", "stream": False,
                "messages": [{"role": "system", "content": "You are a helpful assistant."},
                             {"role": "user", "content": "Reply with one short word."}]}
    raw_path.write_text(json.dumps(raw_body), encoding="utf-8")
    bench_output = work / "bench-output.txt"
    bench_path = work / "bench.json"
    bench_path.write_text(json.dumps([{"question": "Reply with one short word.", "img_path": "",
                                       "out_path": str(bench_output)}]), encoding="utf-8")
    client_cases = (
        ("plain", ["--prompt", "Reply with one short word."]),
        ("stream", ["--prompt", "Reply with one short word.", "--stream"]),
        ("system_model", ["--prompt", "Reply with one short word.", "--system", "You are concise.", "--model", text_model]),
        ("raw_file", ["--raw_file", str(raw_path)]),
        ("bench", ["--bench", str(bench_path), "--model", text_model]),
        ("key", ["--prompt", "Reply with one short word.", "--key", "api-versions-test"]),
    )
    for name, flags in client_cases:
        _run_real_client_case(results, exe, args, 1, name, flags, text_model)

    image = _asset(Path(args.data_dir) / "img", {".png", ".jpg", ".jpeg"})
    audio = _asset(Path(args.data_dir) / "audio", {".wav", ".mp3", ".flac"})
    modalities = detect_modality(npu) if npu else set()
    if npu and image and "image" in modalities:
        _run_real_client_case(results, exe, args, 1, "image", ["--prompt", "Describe the image.", "--img", str(image)], npu)
    else:
        results.append(_result("client real image v1", False,
                               detail="缺少 NPU 图片模型或图片素材", skipped=True, model_name=npu or "_api_versions_"))
    if npu and audio and "audio" in modalities:
        _run_real_client_case(results, exe, args, 1, "audio", ["--prompt", "Transcribe the audio.", "--audio", str(audio)], npu)
    else:
        results.append(_result("client real audio v1", False,
                               detail="缺少支持音频的 NPU 模型或音频素材", skipped=True, model_name=npu or "_api_versions_"))
    if npu and not (image and "image" in modalities) and not (audio and "audio" in modalities):
        _run_real_client_case(results, exe, args, 1, "npu switch", ["--prompt", "Reply with one short word."], npu)

    if npu:
        try:
            status = requests.get(f"http://{args.host}:{args.port}/v2/status", timeout=30)
            names = _loaded_names(status.json())
            passed = status.status_code == 200 and status.json().get("loaded_models") == 1 and npu in names
            results.append(_result("client v1 leaves only the NPU model resident", passed, status.status_code,
                                   detail=f"models={names}; expected_only={npu}", model_name=npu))
        except Exception as exc:
            results.append(_result("client v1 leaves only the NPU model resident", False, detail=repr(exc), model_name=npu))
    else:
        results.append(_result("client v1 leaves only the NPU model resident", False,
                               detail="缺少 NPU 模型", skipped=True))

    for name, flags in client_cases:
        _run_real_client_case(results, exe, args, 2, name, flags, text_model)
    if gpu and npu and infer_backend(gpu)[1] != infer_backend(npu)[1]:
        try:
            status = requests.get(f"http://{args.host}:{args.port}/v2/status", timeout=30)
            names = _loaded_names(status.json())
            passed = status.status_code == 200 and status.json().get("loaded_models") == 2 and gpu in names and npu in names
            results.append(_result("client v2 preserves both device-family models", passed, status.status_code,
                                   detail=f"models={names}; expected={gpu},{npu}"))
        except Exception as exc:
            results.append(_result("client v2 preserves both device-family models", False, detail=repr(exc)))
    else:
        results.append(_result("client v2 preserves both device-family models", False,
                               detail="缺少两个不同设备族模型", skipped=True))

    if npu and image and "image" in modalities:
        _run_real_client_case(results, exe, args, 2, "image", ["--prompt", "Describe the image.", "--img", str(image)], npu)
    else:
        results.append(_result("client real image v2", False,
                               detail="缺少 NPU 图片模型或图片素材", skipped=True, model_name=npu or "_api_versions_"))
    if npu and audio and "audio" in modalities:
        _run_real_client_case(results, exe, args, 2, "audio", ["--prompt", "Transcribe the audio.", "--audio", str(audio)], npu)
    else:
        results.append(_result("client real audio v2", False,
                               detail="缺少支持音频的 NPU 模型或音频素材", skipped=True, model_name=npu or "_api_versions_"))

    missing_body = {"api_key": "", "stream": False,
                    "messages": [{"role": "system", "content": "You are a helpful assistant."},
                                 {"role": "user", "content": "Reply with one short word."}]}
    missing_path = work / "raw-missing-model.json"
    missing_path.write_text(json.dumps(missing_body), encoding="utf-8")
    host = f"{args.host}:{args.port}"
    for version in (1, 2):
        try:
            output, _ = _run_client_process([str(exe)], ["--raw_file", str(missing_path)], host, version, "after_raw_file", timeout=300)
            response_text = output.split("Response: \n", 1)[-1].strip()
            if version == 1:
                passed = "/v1/chat/completions" in output and bool(response_text) and \
                         "get failed response code" not in output and "curl_easy_perform() failed" not in output
            else:
                passed = "/v2/chat/completions" in output and "get failed response code: 400" in output
            results.append(_result(f"client real raw request missing model v{version}", passed,
                                   detail=output[-240:], model_name=text_model))
        except Exception as exc:
            results.append(_result(f"client real raw request missing model v{version}", False, detail=repr(exc), model_name=text_model))


def _run_api_versions_suite(args, models, remote_mode, out_dir):
    results = []
    crash_events = []
    startup_crash_recorded = False
    gpu, npu = _pick_models(models)
    _run_client_stub_tests(args, out_dir, results)

    service = None
    service_ready = False
    if remote_mode:
        try:
            wait_http_ok(f"http://{args.host}:{args.port}/models", timeout=10)
            service_ready = True
        except Exception as exc:
            results.append(_result("service availability", False, detail=repr(exc)))
    elif models and args.exe_dir and args.models:
        initial_model = gpu or models[0]
        config_path = _model_config(args.models, initial_model)
        if config_path:
            service = ServiceManager(args.exe_dir, args.host, args.port)
            service._log_dir = out_dir
            try:
                service.start(str(config_path), extra_args=["-d", "4"])
                wait_http_ok(f"http://{args.host}:{args.port}/models", timeout=120, process=service.process)
                service_ready = True
            except Exception as exc:
                startup_crashed = "exit_code=" in str(exc)
                results.append(_result("service startup", False, detail=repr(exc), crashed=startup_crashed,
                                       model_name=initial_model))
                if startup_crashed:
                    startup_crash_recorded = True
                    crash_events.append(CrashEvent(
                        timestamp=datetime.now().isoformat(), model_name=initial_model, round_num=1,
                        endpoint="SERVICE_STARTUP", detail=str(exc),
                        log_tail=ServiceManager.read_log_tail(service._stderr_log, 50)))
        else:
            results.append(_result("service startup", False, detail=f"找不到模型配置: {initial_model}", skipped=True,
                                   model_name=initial_model))
    else:
        results.append(_result("service startup", False, detail="缺少本地服务路径或模型", skipped=True))

    try:
        if service_ready:
            base_url = f"http://{args.host}:{args.port}"
            _service_contract_tests(base_url, results, gpu, npu)
            _run_real_client_tests(args, out_dir, results, gpu, npu, service_ready=True)
            _v1_empty_state_contract(base_url, results)
        else:
            results.append(_result("service protocol contracts", False, detail="服务未就绪，服务端契约未运行", skipped=True))
            _run_real_client_tests(args, out_dir, results, gpu, npu, service_ready=False)
    finally:
        if not remote_mode and service is not None:
            exit_code = service.get_exit_code()
            if exit_code is not None and not startup_crash_recorded:
                detail = f"服务在 api_versions 套件期间退出，exit_code={exit_code}"
                results.append(_result("service process exited during api_versions", False,
                                       detail=detail, crashed=True))
                crash_events.append(CrashEvent(
                    timestamp=datetime.now().isoformat(), model_name="_api_versions_", round_num=1,
                    endpoint="SERVICE_EXIT", detail=detail,
                    log_tail=ServiceManager.read_log_tail(service._stderr_log, 50)))
            service.stop()

    return results, [], crash_events
