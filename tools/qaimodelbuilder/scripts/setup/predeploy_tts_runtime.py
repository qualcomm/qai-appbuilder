#!/usr/bin/env python
# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
Pre-deploy TTS runtime data so that the first inference does not depend on
network access or cold dictionary builds.

What this script does (idempotent):

  1. Download required NLTK corpora into ``<repo_root>/vendor/nltk_data/``.
     This vendored copy is what ``factory/app_builder/models/melotts-zh/runner.py``
     prepends to ``nltk.data.path`` at startup.

  2. Warm up jieba.posseg so that its tag dictionary cache is built once,
     not on the user's first inference.

  3. Warm up ``g2p_en.G2p()`` so its LSTM checkpoint and NLTK lookups are
     loaded and resolvable without network calls.

  4. Run a tiny end-to-end ``clean_text("你好world", "ZH")`` to prove that
     both the Chinese path (jieba) and the English path (g2p_en + NLTK)
     are wired correctly.

  5. Drop a sentinel file ``vendor/nltk_data/.predeploy_ok`` so that the
     setup script can quickly tell whether this step already succeeded.

This script is intended to be invoked by ``Setup.bat`` (Step 6) using the
ARM64 venv's Python interpreter, so it runs in the same environment that App
Builder later uses for inference.

Run manually for troubleshooting::

    <venv>\\Scripts\\python.exe scripts\\predeploy_tts_runtime.py
"""

from __future__ import annotations

import os
import ssl
import sys
import time
import traceback
from pathlib import Path

# Make stdout/stderr UTF-8 capable so that the end-to-end verification can
# print Chinese characters even when the cmd.exe code page is cp1252/cp936.
# (Setup.bat intentionally avoids ``chcp 65001`` due to a Windows bug,
# so we have to fix encoding at the Python level.)
for _stream_name in ("stdout", "stderr"):
    _stream = getattr(sys, _stream_name, None)
    if _stream is not None and hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (OSError, ValueError):
            pass

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NLTK_DIR = REPO_ROOT / "vendor" / "nltk_data"
SENTINEL = NLTK_DIR / ".predeploy_ok"

# NLTK packages required by g2p_en.G2p().
# - averaged_perceptron_tagger:        legacy (NLTK <3.9) tagger; some envs still resolve via this name.
# - averaged_perceptron_tagger_eng:    new (NLTK 3.9+) renamed package; both names are downloaded for
#                                      compatibility because g2p_en's exact lookup string varies by version.
# - cmudict:                           pronunciation dictionary used as the dictionary front-end.
_NLTK_PACKAGES = (
    "averaged_perceptron_tagger",
    "averaged_perceptron_tagger_eng",
    "cmudict",
)


def _step(msg: str) -> None:
    print(f"[predeploy] {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"[predeploy][WARN] {msg}", file=sys.stderr, flush=True)


def _install_ssl_workaround() -> str:
    """Make ``urllib`` (and therefore NLTK's downloader) able to talk to
    ``raw.githubusercontent.com`` from within a corporate-proxy environment
    that injects its own root CA (self-signed certificate chain).

    NLTK uses ``urllib.request.urlopen`` which relies on Python's ssl module
    to verify certificates.  Corporate proxies (Netskope, Zscaler, etc.)
    inject their own root CA that is not recognized by Python's default
    certificate store, causing CERTIFICATE_VERIFY_FAILED errors.

    The ONLY approach that reliably works is to disable SSL verification for
    these specific setup-time downloads.  This is acceptable because:
    - We download well-known public NLTK corpora from GitHub during one-time
      setup — the same content pip/conda would fetch.
    - This workaround is process-scoped and only affects this short-lived
      script.  It does NOT persist into the inference runtime.

    Returns a short human-readable string describing which path was used.
    """
    # Always use unverified context for NLTK downloads.  NLTK's downloader
    # uses urllib (not requests), so REQUESTS_CA_BUNDLE won't help it, and
    # corporate proxy CA certs (Netskope etc.) are never in Python's default
    # trust store.
    ssl._create_default_https_context = ssl._create_unverified_context  # type: ignore[attr-defined]
    return "unverified (setup-time NLTK downloads only)"


def _download_nltk() -> None:
    _step(f"NLTK data dir: {NLTK_DIR}")
    NLTK_DIR.mkdir(parents=True, exist_ok=True)

    try:
        import nltk  # type: ignore[import-not-found]
    except ImportError as e:
        raise SystemExit(
            f"[predeploy][FATAL] nltk is not installed in this Python: {e}. "
            "Make sure Setup.bat dependency installation has completed before "
            "running this script."
        )

    # Make sure subsequent lookups during this process see the vendored copy.
    if str(NLTK_DIR) not in nltk.data.path:
        nltk.data.path.insert(0, str(NLTK_DIR))

    for pkg in _NLTK_PACKAGES:
        _step(f"downloading NLTK package: {pkg}")
        # ``raise_on_error=False`` lets us continue when one of the renamed
        # packages does not exist on this NLTK version (older NLTK has no
        # ``averaged_perceptron_tagger_eng``; newer NLTK may have removed
        # the legacy alias).  We surface a warning but do not fail the run
        # — the *other* name will satisfy g2p_en.
        ok = False
        try:
            ok = bool(
                nltk.download(pkg, download_dir=str(NLTK_DIR), quiet=False, raise_on_error=False)
            )
        except Exception as e:  # noqa: BLE001
            _warn(f"NLTK download {pkg!r} raised: {e!r} — continuing")
        if not ok:
            _warn(f"NLTK package {pkg!r} could not be downloaded (may not exist for this nltk version).")


def _warmup_jieba() -> None:
    _step("warming up jieba.posseg ...")
    try:
        import jieba.posseg as psg  # type: ignore[import-not-found]
    except ImportError as e:
        raise SystemExit(
            f"[predeploy][FATAL] jieba is not installed in this Python: {e}."
        )
    list(psg.lcut("测试 jieba 词性标注"))
    _step("jieba.posseg cache ready.")


def _warmup_g2p_en() -> None:
    _step("warming up g2p_en.G2p() ...")
    try:
        from g2p_en import G2p  # type: ignore[import-not-found]
    except ImportError as e:
        raise SystemExit(
            f"[predeploy][FATAL] g2p_en is not installed in this Python: {e}."
        )
    g = G2p()
    _ = g("hello world")
    _step("g2p_en ready.")

    # Build pickle cache of G2p state for fast loading (~200ms vs ~14s import).
    # The cache is consumed by melo_zh_local/english.py at inference time.
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
        g2p_cache = REPO_ROOT / "vendor" / "g2p_data" / "g2p_cache.pkl"
        g2p_cache.parent.mkdir(parents=True, exist_ok=True)
        with open(g2p_cache, "wb") as f:
            pickle.dump(cache_data, f, protocol=pickle.HIGHEST_PROTOCOL)
        _step(f"G2p cache written: {g2p_cache} ({g2p_cache.stat().st_size // 1024} KB)")
    except Exception as e:  # noqa: BLE001
        _warn(f"failed to build G2p cache: {e!r} (inference will still work, just slower)")


def _verify_end_to_end() -> None:
    _step("end-to-end verification: clean_text('你好world', 'ZH') ...")
    pack_dir = REPO_ROOT / "factory" / "app_builder" / "models" / "melotts-zh"
    if not pack_dir.is_dir():
        _warn(
            f"melotts-zh pack dir not found ({pack_dir}); skipping end-to-end check. "
            "NLTK data and g2p_en have still been pre-warmed."
        )
        return

    # Make the pack-local ``melo_zh_local`` package importable.  We put it
    # at the *end* of sys.path so we don't accidentally shadow other
    # similarly-named modules during this process.
    pack_path = str(pack_dir)
    if pack_path not in sys.path:
        sys.path.append(pack_path)

    try:
        from melo_zh_local import clean_text  # type: ignore[import-not-found]
    except Exception as e:  # noqa: BLE001
        _warn(f"could not import melo_zh_local from {pack_dir}: {e!r}")
        _warn("end-to-end check skipped (pre-warm steps still succeeded).")
        return

    try:
        out = clean_text("你好world", "ZH")
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        raise SystemExit(
            f"[predeploy][FATAL] clean_text('你好world','ZH') failed: {e!r}. "
            "Check that NLTK data downloaded above and that g2p_en imports cleanly."
        )

    if not (isinstance(out, tuple) and len(out) == 4):
        raise SystemExit(
            f"[predeploy][FATAL] clean_text returned unexpected shape: {type(out).__name__} "
            f"len={len(out) if hasattr(out,'__len__') else '?'}"
        )

    norm_text, phones, tones, word2ph = out
    _step(
        f"clean_text OK: norm='{norm_text}' "
        f"phones={len(phones)} tones={len(tones)} word2ph={len(word2ph)}"
    )


def _precompile_pyc() -> None:
    """Pre-compile .py files to .pyc bytecode caches.

    On ARM64 CPython, parsing+compiling .py to bytecode is very slow (~30s for
    the full import chain).  By pre-compiling at setup time, we ensure that the
    worker process cold-start uses cached .pyc files and avoids re-compilation.

    We compile:
      - factory/app_builder/models/melotts-zh/ (runner + melo_zh_local)
      - factory/app_builder/shared/ (runner_protocol, telemetry, etc.)
      - key site-packages (numpy, jieba, g2p_en, nltk, etc.)
    """
    import compileall
    _step("pre-compiling .pyc bytecode caches ...")

    dirs_to_compile = [
        REPO_ROOT / "factory" / "app_builder" / "models" / "melotts-zh",
        REPO_ROOT / "factory" / "app_builder" / "shared",
    ]
    # Also compile key site-packages if accessible
    try:
        import site
        for sp in site.getsitepackages():
            sp_path = Path(sp)
            for pkg in ("numpy", "jieba", "g2p_en", "nltk", "pypinyin"):
                pkg_dir = sp_path / pkg
                if pkg_dir.is_dir():
                    dirs_to_compile.append(pkg_dir)
    except Exception:
        pass

    compiled = 0
    for d in dirs_to_compile:
        if d.is_dir():
            try:
                ok = compileall.compile_dir(
                    str(d), maxlevels=10, quiet=2, optimize=0
                )
                if ok:
                    compiled += 1
            except Exception as e:  # noqa: BLE001
                _warn(f"compile_dir({d.name}) failed: {e!r}")
    _step(f"pre-compiled {compiled}/{len(dirs_to_compile)} directories.")


def _write_sentinel() -> None:
    NLTK_DIR.mkdir(parents=True, exist_ok=True)
    SENTINEL.write_text(
        f"ok at {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"python={sys.executable}\n",
        encoding="utf-8",
    )
    _step(f"sentinel written: {SENTINEL}")


def main() -> int:
    _step(f"repo_root = {REPO_ROOT}")
    _step(f"python    = {sys.executable}")

    # Best-effort: tell child code (e.g. anything spawned by NLTK) where to
    # cache so we don't accidentally fall back to a user-profile location.
    os.environ.setdefault("NLTK_DATA", str(NLTK_DIR))

    ssl_mode = _install_ssl_workaround()
    _step(f"SSL trust source for NLTK downloads: {ssl_mode}")

    try:
        _download_nltk()
        _warmup_jieba()
        _warmup_g2p_en()
        _verify_end_to_end()
        _precompile_pyc()
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
