# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Which token counter the chat layer ends up with: tiktoken, pure Python, or ``len//2``.

Windows ARM64 has no tiktoken wheel, so the pure-Python encoder must take over there with
no behaviour change except speed. The hot path (``_get_encoder``) must stay OFFLINE.
"""

from __future__ import annotations

import base64
import hashlib
import sys
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from qai.chat.application import _token_estimate_helpers as h
from qai.platform.bpe import CL100K_BASE, EncodingSpec, PureBpeEncoding, tiktoken_cache_key


def _vocab_bytes() -> bytes:
    """A tiny valid ``.tiktoken`` file: the 256 single bytes plus a few merges."""
    toks = [bytes([i]) for i in range(256)] + [b"th", b"he", b"the", b" t", b" the", b"in", b"ing"]
    return b"".join(base64.b64encode(t) + b" " + str(i).encode() + b"\n" for i, t in enumerate(toks))


def _spec(url: str, payload: bytes) -> EncodingSpec:
    return EncodingSpec(
        name="cl100k_base",
        url=url,
        sha256=hashlib.sha256(payload).hexdigest(),
        pat_str=CL100K_BASE.pat_str,
        special_tokens=CL100K_BASE.special_tokens,
    )


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated cache dir, a synthetic vocabulary spec, and tiktoken made unimportable."""
    payload = _vocab_bytes()
    spec = _spec("http://127.0.0.1:1/never-used.tiktoken", payload)
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(tmp_path))
    monkeypatch.setitem(h._ENCODING_SPECS, "cl100k_base", spec)
    monkeypatch.setitem(sys.modules, "tiktoken", None)  # `import tiktoken` -> ImportError
    h._get_encoder.cache_clear()
    yield SimpleEnv(tmp_path, spec, payload, monkeypatch)
    h._get_encoder.cache_clear()


class SimpleEnv:
    def __init__(self, cache_dir: Path, spec: EncodingSpec, payload: bytes, mp: pytest.MonkeyPatch):
        self.cache_dir, self.spec, self.payload, self.mp = cache_dir, spec, payload, mp

    def install_vocab(self) -> None:
        (self.cache_dir / tiktoken_cache_key(self.spec.url)).write_bytes(self.payload)
        h._get_encoder.cache_clear()


def test_no_vocabulary_means_len2_and_never_touches_the_network(env: SimpleEnv) -> None:
    def boom(*_a: object, **_k: object) -> None:
        raise AssertionError("the hot path must not use the network")

    env.mp.setattr(urllib.request, "urlopen", boom)
    assert h._get_encoder("cl100k_base") is None
    assert h.precise_text_tokens("x" * 5000, "any-model") is None
    assert h.token_backend_name() == "len2"


def test_without_tiktoken_the_pure_python_encoder_is_used(env: SimpleEnv) -> None:
    env.install_vocab()
    enc = h._get_encoder("cl100k_base")
    assert isinstance(enc, PureBpeEncoding)
    assert h.token_backend_name() == "pure"
    text = "the thing in the string " * 100
    assert h.precise_text_tokens(text, None) == len(enc.encode_ordinary(text)) > 0


def test_special_looking_text_is_counted_not_rejected(env: SimpleEnv) -> None:
    env.install_vocab()
    n = h.precise_text_tokens("hello <|endoftext|> world", None)
    assert isinstance(n, int) and n > 0  # real tiktoken would have raised -> len//2 fallback


def test_a_corrupt_vocabulary_is_ignored(env: SimpleEnv) -> None:
    (env.cache_dir / tiktoken_cache_key(env.spec.url)).write_bytes(b"not the vocabulary")
    h._get_encoder.cache_clear()
    assert h._get_encoder("cl100k_base") is None


def test_a_missing_regex_package_degrades_to_len2_instead_of_breaking(env: SimpleEnv) -> None:
    env.install_vocab()
    env.mp.setitem(sys.modules, "regex", None)  # `import regex` -> ImportError
    h._get_encoder.cache_clear()
    assert h._get_encoder("cl100k_base") is None
    assert h.precise_text_tokens("hello world " * 400, None) is None


def test_unknown_encoding_is_len2(env: SimpleEnv) -> None:
    assert h._get_encoder("o200k_base") is None


# ------------------------------------------------------------------ warm-up (the only place that downloads)
class _Server:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.hits = 0
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                outer.hits += 1
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/cl100k_base.tiktoken"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def test_warm_up_downloads_once_then_switches_from_len2_to_pure(env: SimpleEnv) -> None:
    srv = _Server(env.payload)
    try:
        env.mp.setitem(h._ENCODING_SPECS, "cl100k_base", _spec(srv.url, env.payload))
        h._get_encoder.cache_clear()
        assert h.token_backend_name() == "len2"  # cached "no vocabulary" ...
        assert h.warm_token_backend(timeout=5.0) == "pure"  # ... is dropped once the file arrives
        assert srv.hits == 1
        assert h.warm_token_backend(timeout=5.0) == "pure"
        assert srv.hits == 1  # already cached and verified: no second download
    finally:
        srv.close()


def test_warm_up_with_a_failing_download_stays_on_len2(env: SimpleEnv) -> None:
    srv = _Server(b"wrong bytes")
    try:
        env.mp.setitem(h._ENCODING_SPECS, "cl100k_base", _spec(srv.url, env.payload))
        h._get_encoder.cache_clear()
        assert h.warm_token_backend(timeout=5.0) == "len2"
        assert list(env.cache_dir.iterdir()) == []
    finally:
        srv.close()


def test_warm_up_never_raises(env: SimpleEnv) -> None:
    env.mp.setattr(h, "_vocab_cache_dir", lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert h.warm_token_backend() == "len2"
