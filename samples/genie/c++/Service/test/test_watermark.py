#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
"""SynthID z-test self-validation regression script

Validates GenieAPIService watermarking (GGUF and QNN backends):
  - GENIE_WATERMARK_ENABLE=1: generated text must pass z-test (z > Z_ALPHA ~= 2.326)
  - Watermark off: z-score should be well below threshold
  - Human text control: z-score should be well below threshold

No torch/transformers dependency. Tokenization uses lightweight HuggingFace
tokenizers library (optional). If tokenizers is unavailable, z-test steps are
gracefully skipped and only service startup / generation is verified.

Usage (local mode, auto start/stop service):
    python test_watermark.py --exe_dir PATH --config PATH [--tokenizer PATH]

Usage (remote mode, connect to already-running service):
    python test_watermark.py --remote --host 127.0.0.1 --port 8910 [--tokenizer PATH]

Usage (no tokenizer.json available -- reconstruct it from the .gguf file itself):
    python test_watermark.py --remote --host 127.0.0.1 --port 8910 \
        --gguf_model PATH/TO/model.gguf --model on-disk-model-dir-name

--gguf_model is a fallback used when --tokenizer is omitted or its path does
not exist: the tokenizer is rebuilt directly from the GGUF file's own
tokenizer.ggml.* KV metadata (see tool/extract_gguf_tokenizer.py), so no
external tokenizer.json and no extra 'pip install gguf' dependency are
required.

Detection logic is backend-agnostic: both GGUF and QNN backends have been
verified to support token-level watermarking. The MNN backend currently does
not support watermarking due to architectural limitations (black-box engine with
no per-token candidate logit interception opportunity).
检测逻辑与后端无关，GGUF/QNN 后端目前均已验证真实生效；MNN 后端因架构限制
（黑盒引擎，无法拿到逐 token 候选词打分机会）暂不支持。
"""

import argparse
import base64
import io
import json
import math
import os
import sys
import time
from pathlib import Path

if sys.platform == "win32" and getattr(sys.stdout, "encoding", "").lower() != "utf-8":
    # 幂等包装：若已是 utf-8(例如 test_service 模块已包装过一次),不要重复包装,
    # 否则旧 TextIOWrapper 被 GC 时会关闭底层 buffer,导致共享该 buffer 的新包装对象
    # 报 "I/O operation on closed file"(test_watermark.py 会 import test_service,两者
    # 若各自无条件包装一次即会触发此问题)。
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

try:
    import requests
except ImportError:
    print("ERROR: 'requests' library is not installed. Run: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# SynthID 30-layer tournament constants
# Table: torch.randint(0,2,(65536,),generator=torch.Generator().manual_seed(0))
# packed LSB-first into 8192 bytes. Inlined to keep the script self-contained.
# Extracted from alg/synthid_watermark/synthid_table.h (do not edit by hand).
# ---------------------------------------------------------------------------
WM_TABLE_B64 = (
    '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'
)

WM_TABLE = base64.b64decode("".join(WM_TABLE_B64.split()))
assert len(WM_TABLE) == 8192, f"bad table length {len(WM_TABLE)}"

WM_N_LAYERS   = 30
WM_TABLE_SIZE = 65536
WM_LCG_MULT   = 6364136223846793005
WM_MASK64     = (1 << 64) - 1
WM_DEFAULT_KEYS = [
    654, 400, 836, 123, 340, 443, 597, 160,  57,  29,
    590, 639,  13, 715, 468, 990, 966, 226, 324, 585,
    118, 504, 421, 521, 129, 669, 732, 225,  90, 960,
]
Z_ALPHA = 2.326  # one-sided alpha = 0.01

# Human-text control sample (no watermark expected).
# Taken from synthid_hf_detect_phi4.py reference script.
HUMAN_TEXT = (
    "The city library opens at nine every morning except Mondays. Last winter I "
    "started going there twice a week, mostly to use the quiet reading room on the "
    "second floor. The staff keep the radiators on high, so by noon the place feels "
    "almost too warm for a coat. I usually bring a notebook and transcribe whatever "
    "passages strike me as useful; handwriting them helps me remember. On my way out "
    "I return the previous week's books and pick up the holds that have arrived. The "
    "walk home takes about twenty minutes along the river, and in December the sun is "
    "already low enough that the bridges cast long shadows across the ice."
)

PROMPTS = [
    ("Write a detailed essay of about 400 words describing a week of hiking "
     "in the mountains, including the weather, the trails, and the campsites."),
    ("Summarize the following travel diary: July 20, 2023. Our first vacation "
     "in the United States. We reached Point State Park where the three rivers meet. "
     "Pittsburgh calls itself the Steel City. Today it runs on healthcare and tech. "
     "Day 7: drive to Virginia Beach. Day 8: full beach day. Day 9: pack and go home."),
]


# ---------------------------------------------------------------------------
# SynthID z-test (ported 1:1 from synthid_hf_detect_phi4.py)
# Hash chain: h = LCG(1, prev_ctx_tokens); g(t,l) = table[LCG(LCG(h,t),key_l) % 65536]
# ---------------------------------------------------------------------------

def _wm_lcg(h, d):
    return ((h + d) * WM_LCG_MULT + 1) & WM_MASK64


def ztest_synthid(ids, ctx_size=4):
    """Weighted mean g-value z-test over the generated token id sequence.

    Returns dict with z_score/mean_g/n_scored/p_value, or None if n_scored==0.
    Skips the first ctx_size tokens and masks repeated contexts, mirroring
    the sampler's seen_ctx bookkeeping and the HF detection mask.
    """
    m = WM_N_LAYERS
    alpha = [10.0 - 9.0 * l / (m - 1) for l in range(m)]
    scale = m / sum(alpha)
    alpha = [a * scale for a in alpha]
    sum_a2 = sum(a * a for a in alpha)

    n = 0
    w_sum = 0.0
    seen_ctx = set()
    for i in range(ctx_size, len(ids)):
        h = 1
        for t in ids[i - ctx_size:i]:
            h = _wm_lcg(h, t)
        if h in seen_ctx:
            continue
        seen_ctx.add(h)
        tok = ids[i]
        for l, a in enumerate(alpha):
            k = _wm_lcg(_wm_lcg(h, tok), WM_DEFAULT_KEYS[l]) % WM_TABLE_SIZE
            w_sum += a * ((WM_TABLE[k >> 3] >> (k & 7)) & 1)
        n += 1
    if n == 0:
        return None

    z = (w_sum - n * m / 2) / math.sqrt(n * sum_a2 / 4)
    return {
        "n_scored": n,
        "mean_g":   w_sum / (m * n),
        "z_score":  z,
        "p_value":  0.5 * math.erfc(z / math.sqrt(2)),
    }


# ---------------------------------------------------------------------------
# Tokenizer shim (HuggingFace tokenizers library, optional)
# ---------------------------------------------------------------------------

def _try_load_tokenizer(tokenizer_path, gguf_model_path=None):
    """Try to load a HuggingFace Tokenizer; return None on failure.

    Resolution order:
      1. ``tokenizer_path`` (--tokenizer), if given and the file exists.
      2. ``gguf_model_path`` (--gguf_model), if given: the tokenizer is
         reconstructed directly from the .gguf file's own
         ``tokenizer.ggml.*`` KV metadata (see
         tool/extract_gguf_tokenizer.extract_tokenizer_from_gguf), so no
         external tokenizer.json or ``pip install gguf`` is required.
    If neither yields a usable tokenizer, z-test steps are skipped.
    """
    if not tokenizer_path and not gguf_model_path:
        return None

    try:
        from tokenizers import Tokenizer  # type: ignore
    except ImportError as e:
        _log(f"[tokenizer] WARNING: 'tokenizers' package not installed ({e}) -- z-test will be skipped")
        return None

    if tokenizer_path:
        if os.path.isfile(tokenizer_path):
            try:
                tok = Tokenizer.from_file(str(tokenizer_path))
                _log(f"[tokenizer] loaded {tokenizer_path}")
                return tok
            except Exception as e:
                _log(f"[tokenizer] WARNING: could not load {tokenizer_path} ({e})")
        else:
            _log(f"[tokenizer] WARNING: --tokenizer path not found: {tokenizer_path}")

    if gguf_model_path:
        if not os.path.isfile(gguf_model_path):
            _log(f"[tokenizer] WARNING: --gguf_model path not found: {gguf_model_path}")
        else:
            try:
                from extract_gguf_tokenizer import extract_tokenizer_from_gguf  # noqa: E402
                tok_json = extract_tokenizer_from_gguf(gguf_model_path, verbose=False)
                tok = Tokenizer.from_str(json.dumps(tok_json))
                _log(f"[tokenizer] reconstructed from GGUF metadata: {gguf_model_path}")
                return tok
            except Exception as e:
                _log(f"[tokenizer] WARNING: could not reconstruct tokenizer from GGUF ({e})")

    _log("[tokenizer] no usable tokenizer -- z-test will be skipped")
    return None


def _tokenize(tok, text):
    """Return token id list (no special tokens)."""
    return tok.encode(text, add_special_tokens=False).ids


def _log(msg):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    try:
        print(line, flush=True)
    except (ValueError, OSError):
        # sys.stdout may be in bad state after the tokenizers Rust extension
        # initialises its thread-pool (common on Windows under winrs pipe).
        # Fall back to a direct fd-1 write that bypasses Python's I/O layer.
        try:
            os.write(1, (line + "\n").encode("utf-8", errors="replace"))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Service lifecycle helpers (imported from test_service.py)
# ---------------------------------------------------------------------------

_TEST_DIR = Path(__file__).parent
sys.path.insert(0, str(_TEST_DIR))
from test_service import ServiceManager, wait_port_open  # noqa: E402

# extract_gguf_tokenizer.py lives in tool/ (sibling of test/), not test/ itself --
# it is a general-purpose GGUF-tokenizer-reconstruction utility, not test code.
_TOOL_DIR = _TEST_DIR.parent / "tool"
sys.path.insert(0, str(_TOOL_DIR))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _chat_completion(base_url, prompt, n_predict=512, timeout=300, model="watermark-test"):
    """POST /v1/chat/completions (OpenAI-compatible) and return reply text.

    ``model`` defaults to a placeholder name that only resolves in local mode,
    where the service is started with a single primary model via ``-c`` and any
    model name in the request is accepted. Remote mode (multi-model
    service_config.json environments) requires passing the real on-disk model
    directory name via --model, or the service returns 404.
    """
    payload = {
        "model":      model,
        "stream":     False,
        "messages":   [{"role": "user", "content": prompt}],
        "max_tokens": n_predict,
    }
    resp = requests.post(
        f"{base_url}/v1/chat/completions",
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload), timeout=timeout,
    )
    resp.raise_for_status()
    j = resp.json()
    choices = j.get("choices") or []
    if not choices:
        return ""
    msg = choices[0].get("message") or {}
    content = msg.get("content", "")
    if isinstance(content, list):
        content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
    return content


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def _result(label, status, detail=""):
    icon = {PASS: "[OK]", FAIL: "[!!]", SKIP: "[--]"}[status]
    suffix = f"  ({detail})" if detail else ""
    _log(f"{icon} {label}{suffix}")


# ---------------------------------------------------------------------------
# Individual test functions
# ---------------------------------------------------------------------------

def _test_service_startup(base_url):
    """Verify service is reachable."""
    try:
        r = requests.get(f"{base_url}/v1/models", timeout=10)
        ok = r.status_code in (200, 404)
        _result("service_startup", PASS if ok else FAIL, f"http={r.status_code}")
        return ok
    except Exception as e:
        _result("service_startup", FAIL, str(e))
        return False


def _test_watermark_detection(base_url, tok, ctx_size, n_texts, n_predict, model="watermark-test"):
    """Generate watermarked texts and assert z-score > Z_ALPHA for each."""
    results = []
    for i in range(n_texts):
        prompt = PROMPTS[i % len(PROMPTS)]
        _log(f"  generating watermarked text {i+1}/{n_texts} ...")
        try:
            text = _chat_completion(base_url, prompt, n_predict, model=model)
        except Exception as e:
            _result(f"watermark_gen_{i}", FAIL, str(e))
            results.append(False)
            continue

        _log(f"  text length: {len(text)} chars")

        if tok is None:
            _result(f"watermark_ztest_{i}", SKIP,
                    "no tokenizer -- generation OK, z-test skipped")
            results.append(None)
            continue

        ids = _tokenize(tok, text)
        _log(f"  token count: {len(ids)}")

        if len(ids) < 50:
            _result(f"watermark_ztest_{i}", SKIP,
                    f"only {len(ids)} tokens, too short for reliable detection")
            results.append(None)
            continue

        zt = ztest_synthid(ids, ctx_size)
        if zt is None:
            _result(f"watermark_ztest_{i}", FAIL, "ztest returned None (0 scored tokens)")
            results.append(False)
            continue

        ok = zt["z_score"] > Z_ALPHA
        detail = (f"z={zt['z_score']:.3f} mean_g={zt['mean_g']:.4f} "
                  f"n={zt['n_scored']} threshold={Z_ALPHA}")
        _result(f"watermark_ztest_{i}", PASS if ok else FAIL, detail)
        results.append(ok)

    return results


def _test_human_text_control(tok, ctx_size):
    """Human text should score below Z_ALPHA."""
    if tok is None:
        _result("human_text_control", SKIP, "no tokenizer")
        return None

    ids = _tokenize(tok, HUMAN_TEXT)
    zt  = ztest_synthid(ids, ctx_size)
    if zt is None:
        _result("human_text_control", SKIP, "0 scored tokens")
        return None

    ok = zt["z_score"] < Z_ALPHA
    detail = (f"z={zt['z_score']:.3f} mean_g={zt['mean_g']:.4f} "
              f"n={zt['n_scored']} (expect < {Z_ALPHA})")
    _result("human_text_control", PASS if ok else FAIL, detail)
    return ok


def _test_no_watermark_generation(base_url, tok, ctx_size, n_predict, model="watermark-test"):
    """Without watermark, z-score should be below Z_ALPHA."""
    _log("  generating unwatermarked text ...")
    try:
        text = _chat_completion(base_url, PROMPTS[0], n_predict, model=model)
    except Exception as e:
        _result("no_watermark_gen", FAIL, str(e))
        return False

    if tok is None:
        _result("no_watermark_ztest", SKIP, "no tokenizer")
        return None

    ids = _tokenize(tok, text)
    if len(ids) < 50:
        _result("no_watermark_ztest", SKIP, f"only {len(ids)} tokens")
        return None

    zt = ztest_synthid(ids, ctx_size)
    if zt is None:
        _result("no_watermark_ztest", SKIP, "0 scored tokens")
        return None

    ok = zt["z_score"] < Z_ALPHA
    detail = (f"z={zt['z_score']:.3f} mean_g={zt['mean_g']:.4f} "
              f"n={zt['n_scored']} (expect < {Z_ALPHA})")
    _result("no_watermark_ztest", PASS if ok else FAIL, detail)
    return ok


def _test_env_var_boundaries(exe_dir, config, host, port):
    """Verify env var edge cases: empty/0 -> off, any other non-empty -> on.
    Only verifies that service starts normally; does not do z-test.
    """
    cases = [
        ("",  "empty string"),
        ("0", "value=0"),
        ("1", "value=1"),
    ]
    results = []
    for val, label in cases:
        _log(f"  env boundary: GENIE_WATERMARK_ENABLE={repr(val)}")
        svc = ServiceManager(exe_dir, host, port)
        try:
            svc.start(config, extra_env={"GENIE_WATERMARK_ENABLE": val})
            wait_port_open(host, port, timeout=180, process=svc.process)
            r = requests.get(f"http://{host}:{port}/v1/models", timeout=10)
            ok = r.status_code in (200, 404)
            _result(f"env_boundary_{label}", PASS if ok else FAIL, "service started OK")
            results.append(ok)
        except Exception as e:
            _result(f"env_boundary_{label}", FAIL, str(e))
            results.append(False)
        finally:
            svc.stop()
            time.sleep(3)
    return results


def _test_graceful_degradation(exe_dir, config, host, port):
    """GENIE_WATERMARK_ENABLE=1 with no DLL: service must start without crash."""
    _log("  graceful degradation: watermark enabled, DLL may or may not exist ...")
    svc = ServiceManager(exe_dir, host, port)
    ok  = False
    try:
        svc.start(config, extra_env={"GENIE_WATERMARK_ENABLE": "1"})
        wait_port_open(host, port, timeout=180, process=svc.process)
        r  = requests.get(f"http://{host}:{port}/v1/models", timeout=10)
        ok = r.status_code in (200, 404)
        _result("graceful_degradation", PASS if ok else FAIL,
                "service started without crash")
    except Exception as e:
        _result("graceful_degradation", FAIL, str(e))
    finally:
        svc.stop()
    return ok


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="SynthID z-test watermark regression for GenieAPIService",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--exe_dir", default=None,
                        help="GenieAPIService.exe directory (required for local mode)")
    parser.add_argument("--config", default=None,
                        help="GGUF model config.json path (required for local mode)")
    parser.add_argument("--remote", action="store_true",
                        help="Remote mode: connect to already-running service")
    parser.add_argument("--host", default="127.0.0.1", help="Service host")
    parser.add_argument("--port", type=int, default=8910, help="Service port")
    parser.add_argument("--tokenizer", default=None,
                        help="Path to tokenizer.json (HuggingFace tokenizers format); "
                             "if omitted, z-test steps are skipped unless --gguf_model is given")
    parser.add_argument("--gguf_model", default=None,
                        help="Path to the .gguf model file. Used as a fallback when "
                             "--tokenizer is omitted or the given path does not exist: the "
                             "tokenizer is reconstructed directly from the GGUF file's own "
                             "tokenizer.ggml.* KV metadata (see tool/extract_gguf_tokenizer.py), "
                             "so no external tokenizer.json or 'pip install gguf' is required.")
    parser.add_argument("--model", default="watermark-test",
                        help="Model name to send in the request 'model' field. Must match "
                             "the model registered in the service (e.g. gpt-oss-20b-GGUF); "
                             "the default 'watermark-test' placeholder will fail with 404 "
                             "if the service only routes to its actual registered model name.")
    parser.add_argument("--ctx_size", type=int, default=4,
                        help="Watermark context window (must match generation side)")
    parser.add_argument("--n_texts", type=int, default=3,
                        help="Number of watermarked texts to generate and test")
    parser.add_argument("--n_predict", type=int, default=512,
                        help="Max tokens per generation")
    parser.add_argument("--skip_env_boundary", action="store_true",
                        help="Skip env-var boundary tests (restarts service 3x, slow)")
    args = parser.parse_args()

    base_url = f"http://{args.host}:{args.port}"
    tok      = _try_load_tokenizer(args.tokenizer, args.gguf_model)
    failures = 0
    skips    = 0

    def _tally(r):
        nonlocal failures, skips
        if r is False:
            failures += 1
        elif r is None:
            skips += 1

    # -----------------------------------------------------------------------
    # Remote mode: service already running, assume watermark is enabled
    # -----------------------------------------------------------------------
    if args.remote:
        _log("=== Remote mode: connecting to already-running service ===")
        if not wait_port_open(args.host, args.port, timeout=10):
            _log(f"ERROR: port {args.host}:{args.port} unreachable")
            sys.exit(1)

        _tally(_test_service_startup(base_url))
        _tally(_test_human_text_control(tok, args.ctx_size))
        for r in _test_watermark_detection(base_url, tok, args.ctx_size,
                                            args.n_texts, args.n_predict,
                                            model=args.model):
            _tally(r)

    # -----------------------------------------------------------------------
    # Local mode: manage service lifecycle
    # -----------------------------------------------------------------------
    else:
        if not args.exe_dir or not args.config:
            print("ERROR: --exe_dir and --config are required in local mode")
            sys.exit(1)

        exe_dir = str(Path(args.exe_dir).resolve())
        config  = str(Path(args.config).resolve())

        # -- Test 1: watermark ENABLED (main acceptance criterion) ----------
        _log("=== Test 1: watermark ON (GENIE_WATERMARK_ENABLE=1) ===")
        svc = ServiceManager(exe_dir, args.host, args.port)
        try:
            svc.start(config, extra_env={"GENIE_WATERMARK_ENABLE": "1"})
            wait_port_open(args.host, args.port, timeout=180, process=svc.process)
            _tally(_test_service_startup(base_url))
            for r in _test_watermark_detection(base_url, tok, args.ctx_size,
                                                args.n_texts, args.n_predict,
                                                model=args.model):
                _tally(r)
        except Exception as e:
            _log(f"ERROR starting service: {e}")
            failures += 1
        finally:
            svc.stop()
        time.sleep(3)

        # -- Test 2: watermark DISABLED (control: z-score should be low) ---
        _log("=== Test 2: watermark OFF (no env var) ===")
        svc = ServiceManager(exe_dir, args.host, args.port)
        try:
            svc.start(config)
            wait_port_open(args.host, args.port, timeout=180, process=svc.process)
            _tally(_test_no_watermark_generation(base_url, tok,
                                                  args.ctx_size, args.n_predict,
                                                  model=args.model))
        except Exception as e:
            _log(f"ERROR starting service: {e}")
            failures += 1
        finally:
            svc.stop()
        time.sleep(3)

        # -- Test 3: human text control (no service needed) -----------------
        _log("=== Test 3: human text control ===")
        _tally(_test_human_text_control(tok, args.ctx_size))

        # -- Test 4: graceful degradation -----------------------------------
        _log("=== Test 4: graceful degradation (watermark enabled, DLL may be absent) ===")
        _tally(_test_graceful_degradation(exe_dir, config, args.host, args.port))
        time.sleep(3)

        # -- Test 5: env var boundaries (optional, slow) --------------------
        if not args.skip_env_boundary:
            _log("=== Test 5: env var boundaries ===")
            for r in _test_env_var_boundaries(exe_dir, config, args.host, args.port):
                _tally(r)
        else:
            _log("[--] env var boundary tests skipped (--skip_env_boundary)")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    _log(f"=== Summary: failures={failures} skips={skips} ===")
    if failures:
        _log("RESULT: FAIL")
        sys.exit(1)
    else:
        _log("RESULT: PASS" + (" (with skips -- tokenizer unavailable)" if skips else ""))
        sys.exit(0)


if __name__ == "__main__":
    main()
