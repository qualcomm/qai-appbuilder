# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai.platform.bpe``: a pure-Python tokenizer that must agree with tiktoken."""

from __future__ import annotations

import hashlib
import random
import threading
from collections import Counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from qai.platform.bpe import (
    CL100K_BASE,
    EncodingSpec,
    PureBpeEncoding,
    fetch_vocab,
    load_ranks,
    tiktoken_cache_key,
)

_REPO = Path(__file__).resolve().parents[4]


# --------------------------------------------------------------------- synthetic vocabulary
def _train_ranks(corpus: str, merges: int) -> dict[bytes, int]:
    """A tiny deterministic BPE trainer: 256 byte tokens, then ``merges`` merges."""
    ranks: dict[bytes, int] = {bytes([i]): i for i in range(256)}
    words = [list(bytes([b]) for b in w.encode("utf-8")) for w in corpus.split(" ") if w]
    for _ in range(merges):
        pairs: Counter[tuple[bytes, bytes]] = Counter()
        for w in words:
            pairs.update(zip(w, w[1:], strict=False))
        if not pairs:
            break
        (a, b), _count = max(pairs.items(), key=lambda kv: (kv[1], kv[0]))
        ranks[a + b] = len(ranks)
        merged = a + b
        for idx, w in enumerate(words):
            out, i = [], 0
            while i < len(w):
                if i + 1 < len(w) and w[i] == a and w[i + 1] == b:
                    out.append(merged)
                    i += 2
                else:
                    out.append(w[i])
                    i += 1
            words[idx] = out
    return ranks


_CORPUS = (
    "the quick brown fox jumps over the lazy dog and the other thing in the string "
    "def main(): return self.value + other.value  import os, sys, json "
    "今天天气很好我们去公园散步人工智能正在改变世界请帮我总结一下这份文档 "
    "日本語のテキストも大丈夫です こんにちは世界 "
) * 6
_RANKS = _train_ranks(_CORPUS, 400)
_SPECIAL = {"<|endoftext|>": len(_RANKS), "<|fim_prefix|>": len(_RANKS) + 1}

_SAMPLES = [
    "",
    "hello world",
    "The quick brown fox jumps over the lazy dog. It's a don't-care situation; we'll see.",
    "今天天气很好,我们去公园散步。人工智能正在改变世界,请帮我总结一下这份文档的要点。",
    "def f(x):\n    return x ** 2 + 1  # comment\n\n\nclass A:\n\tpass\n",
    "emoji 😀🚀✨ 👨\u200d👩\u200d👧\u200d👦 and 🇨🇳",
    "  leading and trailing   \n\n\n  \t tabs\r\nwindows\r\n",
    "1234567890 3.14159265358979 0x7fffffff 2026-11-19",
    "text <|endoftext|> and <|fim_prefix|> inside",
    "e\u0301 ñ ǅ ᾼ ٣٤٥ ½ ⅷ zero\u200bwidth",
    # one regex piece far longer than the simple-merge limit (forces the heap path)
    "今天天气很好我们去公园散步人工智能正在改变世界" * 40,
    "a" * 500,
]


def _mk_random(seed: int, n: int, lo: int, hi: int) -> str:
    rnd = random.Random(seed)
    return "".join(chr(rnd.randint(lo, hi)) for _ in range(n))


_SAMPLES += [_mk_random(1, 3000, 0x4E00, 0x9FA5), _mk_random(2, 2000, 0x20, 0x24F)]


@pytest.fixture(scope="module")
def pair():
    tiktoken = pytest.importorskip("tiktoken")
    real = tiktoken.Encoding(
        name="synthetic",
        pat_str=CL100K_BASE.pat_str,
        mergeable_ranks=_RANKS,
        special_tokens=_SPECIAL,
    )
    pure = PureBpeEncoding("synthetic", CL100K_BASE.pat_str, _RANKS, _SPECIAL)
    return real, pure


@pytest.mark.parametrize("text", _SAMPLES, ids=lambda t: f"len{len(t)}")
def test_token_ids_match_tiktoken(pair, text: str) -> None:
    real, pure = pair
    expected = real.encode(text, disallowed_special=())
    assert pure.encode(text) == expected
    assert pure.encode_ordinary(text) == real.encode_ordinary(text)
    assert pure.count(text) == len(expected)


def test_decode_round_trips_like_tiktoken(pair) -> None:
    real, pure = pair
    for text in _SAMPLES:
        ids = real.encode(text, disallowed_special=())
        assert pure.decode(ids) == real.decode(ids)
        assert pure.decode_bytes(ids) == real.decode_bytes(ids)


def test_vocabulary_facts_match_tiktoken(pair) -> None:
    real, pure = pair
    assert pure.n_vocab == real.n_vocab
    assert pure.special_tokens_set == real.special_tokens_set
    for tok in _SPECIAL:
        assert pure.encode_single_token(tok) == real.encode_single_token(tok)
    assert pure.encode_single_token(b"th") == real.encode_single_token(b"th")


def test_special_token_handling_matches_tiktoken(pair) -> None:
    real, pure = pair
    text = "a <|endoftext|> b <|fim_prefix|> c"
    assert pure.encode(text, allowed_special="all") == real.encode(text, allowed_special="all")
    assert pure.encode(text, allowed_special={"<|endoftext|>"}) == real.encode(
        text, allowed_special={"<|endoftext|>"}, disallowed_special=()
    )
    with pytest.raises(ValueError):
        pure.encode(text, disallowed_special="all")
    # the counting-friendly default never raises and treats the text as ordinary
    assert pure.encode(text) == real.encode(text, disallowed_special=())


def test_lone_surrogates_do_not_raise(pair) -> None:
    real, pure = pair
    text = "ok \ud800 broken"
    assert pure.encode(text) == real.encode(text, disallowed_special=())


def test_unknown_single_token_raises(pair) -> None:
    _real, pure = pair
    with pytest.raises(KeyError):
        pure.encode_single_token("definitely-not-a-token-\u2603")


# --------------------------------------------------------------------- the real cl100k_base vocabulary
def _real_vocab() -> Path | None:
    p = _REPO / "vendor" / "tiktoken" / tiktoken_cache_key(CL100K_BASE.url)
    return p if p.is_file() else None


@pytest.mark.skipif(_real_vocab() is None, reason="vendor/tiktoken/cl100k_base not downloaded")
def test_real_cl100k_matches_tiktoken(monkeypatch) -> None:
    tiktoken = pytest.importorskip("tiktoken")
    monkeypatch.setenv("TIKTOKEN_CACHE_DIR", str(_real_vocab().parent))  # type: ignore[union-attr]
    real = tiktoken.get_encoding("cl100k_base")
    pure = PureBpeEncoding(
        "cl100k_base", CL100K_BASE.pat_str, load_ranks(_real_vocab()), CL100K_BASE.special_tokens  # type: ignore[arg-type]
    )
    assert pure.n_vocab == real.n_vocab
    texts = [
        *_SAMPLES,
        "今天天气很好,我们去公园散步。" * 50,
        (_REPO / "src/qai/chat/application/_token_estimate_helpers.py").read_text(encoding="utf-8"),
    ]
    for text in texts:
        assert pure.encode(text) == real.encode(text, disallowed_special=()), text[:40]


# --------------------------------------------------------------------- vocabulary download
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

            def log_message(self, *args: object) -> None:  # silence
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v.tiktoken"
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _spec(url: str, payload: bytes) -> EncodingSpec:
    return EncodingSpec(
        name="t", url=url, sha256=hashlib.sha256(payload).hexdigest(), pat_str=r"\w+", special_tokens={}
    )


def test_fetch_downloads_verifies_and_names_like_tiktoken(tmp_path: Path) -> None:
    payload = b"IQ== 0\nIg== 1\n"
    srv = _Server(payload)
    try:
        spec = _spec(srv.url, payload)
        got = fetch_vocab(spec, tmp_path)
        assert got == tmp_path / tiktoken_cache_key(spec.url)
        assert got.read_bytes() == payload
        assert list(tmp_path.iterdir()) == [got]  # no temp file left behind
        # a second call reuses the verified file without touching the network
        hits = srv.hits
        assert fetch_vocab(spec, tmp_path) == got
        assert srv.hits == hits
    finally:
        srv.close()


def test_fetch_rejects_a_wrong_hash_and_leaves_nothing(tmp_path: Path) -> None:
    srv = _Server(b"tampered")
    try:
        spec = _spec(srv.url, b"the real bytes")
        assert fetch_vocab(spec, tmp_path) is None
        assert list(tmp_path.iterdir()) == []
    finally:
        srv.close()


def test_fetch_replaces_a_corrupt_cached_file(tmp_path: Path) -> None:
    payload = b"IQ== 0\n"
    srv = _Server(payload)
    try:
        spec = _spec(srv.url, payload)
        (tmp_path / tiktoken_cache_key(spec.url)).write_bytes(b"garbage")
        got = fetch_vocab(spec, tmp_path)
        assert got is not None and got.read_bytes() == payload
    finally:
        srv.close()


def test_fetch_returns_none_when_the_server_is_unreachable(tmp_path: Path) -> None:
    srv = _Server(b"x")
    url = srv.url
    srv.close()  # nothing listens any more
    assert fetch_vocab(_spec(url, b"x"), tmp_path, timeout=2.0) is None


def test_fetch_returns_none_on_http_error(tmp_path: Path) -> None:
    srv = _Server(b"nope", status=404)
    try:
        assert fetch_vocab(_spec(srv.url, b"nope"), tmp_path) is None
    finally:
        srv.close()


# --------------------------------------------------------------------- bookkeeping
def test_cl100k_spec_matches_what_tiktoken_declares() -> None:
    tiktoken = pytest.importorskip("tiktoken")
    from tiktoken_ext import openai_public  # type: ignore[import-not-found]

    src = Path(openai_public.__file__).read_text(encoding="utf-8")
    assert CL100K_BASE.url in src
    assert CL100K_BASE.sha256 in src
    assert CL100K_BASE.pat_str in src
    for name, rank in CL100K_BASE.special_tokens.items():
        assert str(rank) in src, name
    assert tiktoken_cache_key(CL100K_BASE.url) == hashlib.sha1(CL100K_BASE.url.encode()).hexdigest()


def test_app_builder_copy_is_identical() -> None:
    """The packs cannot import ``qai``; their copy must not drift from the original."""
    a = (_REPO / "src/qai/platform/bpe.py").read_bytes().replace(b"\r\n", b"\n")
    b = (_REPO / "factory/chat_features/app-builder/shared/pure_bpe.py").read_bytes().replace(b"\r\n", b"\n")
    assert a == b
