#!/usr/bin/env python
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
melotts-zh pack predeploy hook — NON-pip runtime data (idempotent)
==================================================================

This is the per-pack L3 customization hook invoked by the app's startup
dependency check (``backend/ensure_deps.py``) AFTER the pip dependencies in
``requirements.txt`` are installed. It provisions the runtime data that
``pip install`` cannot, so the first inference on a fresh machine never blocks
on a network download or a cold dictionary build:

  1. Download the NLTK corpora g2p_en needs (cmudict + the perceptron tagger)
     into a vendored ``vendor/nltk_data/``.
  2. Warm up jieba.posseg so its tag-dictionary cache is built once.
  3. Warm up g2p_en.G2p() and pickle its state to ``vendor/g2p_data/g2p_cache.pkl``
     (~200 ms restore vs ~14 s cold import on the English G2P path).
  4. Write a sentinel ``vendor/nltk_data/.predeploy_ok`` so a warm start skips.

This is a self-contained, in-pack copy of the logic in
``scripts/setup/predeploy_tts_runtime.py`` (which runs at dev-host install
time). Shipping it INSIDE the pack means the packaged app carries its own
provisioning step and works on a machine that never ran the dev-host setup.

**Vendor location is resolved by ancestor-walk, not a fixed ``parents[N]``**,
so it lands correctly in BOTH layouts:
  * dev repo:   ``<repo>/vendor/``            (pack at ``<repo>/factory/chat_features/app-builder/models/melotts-zh``)
  * packaged:   ``<app>/vendor/``             (pack at ``<app>/pack/melotts-zh``)

Exit code is 0 on success and on best-effort partial completion (a missing
optional corpus is a warning); it is non-zero only on a hard failure the app
truly cannot run without. ``ensure_deps.py`` treats a non-zero exit as a
warning (the app may still run a degraded path), so this hook never blocks a
launch on its own.
"""
from __future__ import annotations

import os
import ssl
import sys
import time
import traceback
from pathlib import Path

# Make stdout/stderr UTF-8 capable so the end-to-end check can print Chinese
# even under a cp936/cp1252 console code page.
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass

_PACK_DIR = Path(__file__).resolve().parent


def _resolve_vendor_dir() -> Path:
    """Locate the ``vendor/`` dir by walking up from this pack file.

    Works in both the dev repo (``<repo>/vendor``) and the packaged app
    (``<app>/vendor``) because it searches ancestors for an existing
    ``vendor/`` dir. When none exists yet (first run on a fresh machine), it
    creates ``vendor/`` beside the app/repo root — the FIRST ancestor that
    looks like a project root (contains ``backend/`` for a packaged app, or
    ``factory/`` for the dev repo). Falls back to the pack's grandparent.
    """
    here = _PACK_DIR
    # 1) Reuse an existing vendor/ anywhere up the tree.
    for parent in here.parents:
        candidate = parent / "vendor"
        if candidate.is_dir():
            return candidate
    # 2) No vendor/ yet — anchor it at the nearest recognisable project root.
    for parent in here.parents:
        if (parent / "backend").is_dir() or (parent / "factory").is_dir():
            return parent / "vendor"
    # 3) Last resort: two levels up (packaged: <app>/pack/<id> -> <app>).
    try:
        return here.parents[1] / "vendor"
    except IndexError:
        return here / "vendor"


_VENDOR_DIR = _resolve_vendor_dir()
NLTK_DIR = _VENDOR_DIR / "nltk_data"
G2P_CACHE = _VENDOR_DIR / "g2p_data" / "g2p_cache.pkl"
SENTINEL = NLTK_DIR / ".predeploy_ok"

# NLTK packages g2p_en needs. Both tagger names are attempted for cross-version
# compatibility (NLTK <3.9 vs 3.9+ renamed ``*_eng``); cmudict is the dict.
_NLTK_PACKAGES = (
    "averaged_perceptron_tagger",
    "averaged_perceptron_tagger_eng",
    "cmudict",
)


def _step(msg: str) -> None:
    print(f"[predeploy:melotts-zh] {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"[predeploy:melotts-zh][WARN] {msg}", file=sys.stderr, flush=True)


def _install_ssl_workaround() -> None:
    """Let NLTK's urllib downloader talk to GitHub through a corporate proxy
    that injects a self-signed root CA. Process-scoped; setup-time only."""
    ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore[attr-defined]


def _download_nltk() -> None:
    _step(f"NLTK data dir: {NLTK_DIR}")
    NLTK_DIR.mkdir(parents=True, exist_ok=True)
    try:
        import nltk  # type: ignore[import-not-found]
    except ImportError as e:
        raise SystemExit(
            f"[predeploy:melotts-zh][FATAL] nltk not installed: {e}. "
            "It should have been installed from requirements.txt before this hook."
        )
    if str(NLTK_DIR) not in nltk.data.path:
        nltk.data.path.insert(0, str(NLTK_DIR))
    for pkg in _NLTK_PACKAGES:
        _step(f"downloading NLTK package: {pkg}")
        ok = False
        try:
            ok = bool(
                nltk.download(
                    pkg, download_dir=str(NLTK_DIR), quiet=False, raise_on_error=False
                )
            )
        except Exception as e:  # noqa: BLE001
            _warn(f"NLTK download {pkg!r} raised: {e!r} — continuing")
        if not ok:
            _warn(f"NLTK package {pkg!r} unavailable for this nltk version — continuing")


def _warmup_jieba() -> None:
    _step("warming up jieba.posseg ...")
    try:
        import jieba.posseg as psg  # type: ignore[import-not-found]
    except ImportError as e:
        _warn(f"jieba not importable ({e}); skipping warm-up")
        return
    list(psg.lcut("测试 jieba 词性标注"))
    _step("jieba.posseg cache ready.")


def _warmup_g2p_en() -> None:
    _step("warming up g2p_en.G2p() ...")
    try:
        from g2p_en import G2p  # type: ignore[import-not-found]
    except ImportError as e:
        _warn(f"g2p_en not importable ({e}); skipping G2p cache build")
        return
    g = G2p()
    _ = g("hello world")
    _step("g2p_en ready.")

    _step("building G2p pickle cache ...")
    try:
        import pickle
        cache_data = {
            "cmu": g.cmu,
            "graphemes": g.graphemes,
            "phonemes": g.phonemes,
            "g2idx": g.g2idx,
            "idx2g": g.idx2g,
            "p2idx": g.p2idx,
            "idx2p": g.idx2p,
            "homograph2features": g.homograph2features,
            "enc_emb": g.enc_emb,
            "enc_w_ih": g.enc_w_ih,
            "enc_w_hh": g.enc_w_hh,
            "enc_b_ih": g.enc_b_ih,
            "enc_b_hh": g.enc_b_hh,
            "dec_emb": g.dec_emb,
            "dec_w_ih": g.dec_w_ih,
            "dec_w_hh": g.dec_w_hh,
            "dec_b_ih": g.dec_b_ih,
            "dec_b_hh": g.dec_b_hh,
            "fc_w": g.fc_w,
            "fc_b": g.fc_b,
        }
        G2P_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with open(G2P_CACHE, "wb") as f:
            pickle.dump(cache_data, f, protocol=pickle.HIGHEST_PROTOCOL)
        _step(f"G2p cache written: {G2P_CACHE} ({G2P_CACHE.stat().st_size // 1024} KB)")
    except Exception as e:  # noqa: BLE001
        _warn(f"failed to build G2p cache: {e!r} (inference still works, just slower)")


def _verify_end_to_end() -> None:
    _step("end-to-end verification: clean_text('你好world', 'ZH') ...")
    pack_path = str(_PACK_DIR)
    if pack_path not in sys.path:
        sys.path.append(pack_path)
    try:
        from melo_zh_local import clean_text  # type: ignore[import-not-found]
    except Exception as e:  # noqa: BLE001
        _warn(f"could not import melo_zh_local: {e!r}; end-to-end check skipped")
        return
    try:
        out = clean_text("你好world", "ZH")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _warn(f"clean_text('你好world','ZH') failed: {e!r} — pre-warm steps still ran")
        return
    if isinstance(out, tuple) and len(out) == 4:
        norm_text, phones, tones, word2ph = out
        _step(
            f"clean_text OK: norm='{norm_text}' phones={len(phones)} "
            f"tones={len(tones)} word2ph={len(word2ph)}"
        )
    else:
        _warn(f"clean_text returned unexpected shape: {type(out).__name__}")


def _write_sentinel() -> None:
    NLTK_DIR.mkdir(parents=True, exist_ok=True)
    SENTINEL.write_text(
        f"ok at {time.strftime('%Y-%m-%d %H:%M:%S')}\npython={sys.executable}\n",
        encoding="utf-8",
    )
    _step(f"sentinel written: {SENTINEL}")


def main() -> int:
    _step(f"pack dir   = {_PACK_DIR}")
    _step(f"vendor dir = {_VENDOR_DIR}")
    _step(f"python     = {sys.executable}")

    # Warm-start skip: if the sentinel exists and the caches are in place,
    # there is nothing to do.
    if SENTINEL.is_file() and G2P_CACHE.is_file():
        _step("runtime data already provisioned (sentinel hit) — skipping.")
        return 0

    os.environ.setdefault("NLTK_DATA", str(NLTK_DIR))
    _install_ssl_workaround()

    try:
        _download_nltk()
        _warmup_jieba()
        _warmup_g2p_en()
        _verify_end_to_end()
        _write_sentinel()
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        _warn(f"unexpected error: {e!r}")
        return 1

    _step("TTS runtime pre-deployment complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
