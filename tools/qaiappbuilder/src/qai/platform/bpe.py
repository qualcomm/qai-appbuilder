# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure-Python byte-pair-encoding tokenizer, token-for-token compatible with tiktoken.

Why this exists
---------------
``tiktoken``'s encoder is a Rust extension and PyPI publishes no ``win_arm64``
wheel for it (the sdist needs a Rust toolchain). Without it the app could only
estimate tokens as ``len // 2``. This module re-implements the same algorithm on
top of the ``regex`` package (which does have a Windows ARM64 wheel): split the text
with tiktoken's regular expression, then repeatedly merge the adjacent byte pair
with the lowest rank.

It is a drop-in for the part of ``tiktoken.Encoding`` this project uses:
``encode`` / ``encode_ordinary`` / ``encode_single_token`` / ``decode`` /
``n_vocab`` / ``special_tokens_set`` plus a cheap :meth:`count`. The token ids are
identical to tiktoken's (checked against tiktoken on synthetic and real
``cl100k_base`` text, see ``tests/unit/platform/test_bpe.py``); it is only slower
(roughly 2-3x on ordinary text).

Provenance and licence
----------------------
The algorithm follows ``tiktoken/_educational.py`` and the regular expression /
special-token table follows ``tiktoken_ext/openai_public.py`` from
https://github.com/openai/tiktoken (MIT licence, Copyright (c) 2022 OpenAI, Shantanu
Jain). The vocabulary file itself is NOT shipped here: :func:`fetch_vocab` downloads
it from OpenAI's official URL and verifies its SHA-256, and tiktoken's own cache
naming (``sha1(url)``) is used so both implementations share one cache directory.

This file is kept byte-identical in ``factory/chat_features/app-builder/shared/
pure_bpe.py`` (the App Builder packs run in their own process and cannot import
``qai``); a unit test enforces that.

Stdlib + ``regex`` only, so it can be imported by anything without layering concerns.
"""

from __future__ import annotations

import base64
import hashlib
import heapq
import logging
import os
import tempfile
import urllib.request
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "CL100K_BASE",
    "EncodingSpec",
    "PureBpeEncoding",
    "cached_vocab",
    "fetch_vocab",
    "load_ranks",
    "tiktoken_cache_key",
]

_log = logging.getLogger(__name__)


def _regex():  # noqa: ANN202
    """Import ``regex`` lazily so merely importing this module never fails without it."""
    import regex  # noqa: PLC0415

    return regex

#: Pieces up to this many bytes use the simple O(n^2) merge; longer ones use a heap.
_SIMPLE_MERGE_MAX_BYTES = 32
#: Bound on the per-instance piece cache (memory guard; ordinary text repeats a lot).
_WORD_CACHE_MAX = 50_000
#: Hard cap on a vocabulary download (cl100k_base is ~1.7 MB, o200k_base ~3.6 MB).
_VOCAB_MAX_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class EncodingSpec:
    """Where an OpenAI vocabulary lives and how to interpret it."""

    name: str
    url: str
    sha256: str
    pat_str: str
    special_tokens: Mapping[str, int]


#: Mirrors ``tiktoken_ext.openai_public.cl100k_base`` (tiktoken 0.12.0).
CL100K_BASE = EncodingSpec(
    name="cl100k_base",
    url="https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken",
    sha256="223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
    pat_str=(
        r"""'(?i:[sdmt]|ll|ve|re)|[^\r\n\p{L}\p{N}]?+\p{L}++|\p{N}{1,3}+"""
        r"""| ?[^\s\p{L}\p{N}]++[\r\n]*+|\s++$|\s*[\r\n]|\s+(?!\S)|\s"""
    ),
    special_tokens={
        "<|endoftext|>": 100257,
        "<|fim_prefix|>": 100258,
        "<|fim_middle|>": 100259,
        "<|fim_suffix|>": 100260,
        "<|endofprompt|>": 100276,
    },
)


def tiktoken_cache_key(url: str) -> str:
    """The file name tiktoken uses for ``url`` inside ``TIKTOKEN_CACHE_DIR``."""
    # tiktoken names its cache file sha1(url), so we must match it; not a security use.
    return hashlib.sha1(url.encode(), usedforsecurity=False).hexdigest()  # noqa: S324  # nosemgrep


def load_ranks(path: str | os.PathLike[str]) -> dict[bytes, int]:
    """Parse a ``.tiktoken`` file (``<base64 token> <rank>`` per line)."""
    ranks: dict[bytes, int] = {}
    for line in Path(path).read_bytes().splitlines():
        if not line:
            continue
        token, rank = line.split()
        ranks[base64.b64decode(token)] = int(rank)
    return ranks


def _merge_pieces(ranks: Mapping[bytes, int], data: bytes) -> list[bytes]:
    """Byte-pair merge of one regex piece; returns the resulting byte tokens."""
    n = len(data)
    if n < 2:
        return [data]
    if n <= _SIMPLE_MERGE_MAX_BYTES:
        parts = [bytes([x]) for x in data]
        while len(parts) > 1:
            best_i = -1
            best_rank: int | None = None
            for i in range(len(parts) - 1):
                r = ranks.get(parts[i] + parts[i + 1])
                if r is not None and (best_rank is None or r < best_rank):
                    best_rank, best_i = r, i
            if best_rank is None:
                break
            parts[best_i : best_i + 2] = [parts[best_i] + parts[best_i + 1]]
        return parts

    # Long piece (e.g. a long unpunctuated CJK run): doubly linked list over the byte
    # positions plus a lazy-deletion min-heap keyed by (rank, position) -> O(n log n).
    toks: list[bytes | None] = [bytes([x]) for x in data]
    nxt = list(range(1, n + 1))
    prv = list(range(-1, n - 1))
    gen = [0] * n
    heap: list[tuple[int, int, int, int]] = []

    def push(i: int) -> None:
        j = nxt[i]
        if j < n:
            r = ranks.get(toks[i] + toks[j])  # type: ignore[operator]
            if r is not None:
                heapq.heappush(heap, (r, i, gen[i], gen[j]))

    for i in range(n - 1):
        push(i)
    while heap:
        _rank, i, gi, gj = heapq.heappop(heap)
        j = nxt[i]
        if toks[i] is None or j >= n or toks[j] is None or gen[i] != gi or gen[j] != gj:
            continue
        toks[i] = toks[i] + toks[j]  # type: ignore[operator]
        gen[i] += 1
        toks[j] = None
        nj = nxt[j]
        nxt[i] = nj
        if nj < n:
            prv[nj] = i
        if prv[i] >= 0:
            push(prv[i])
        push(i)
    return [t for t in toks if t is not None]


def _sanitise(text: str) -> str:
    """Replace lone surrogates the way tiktoken does instead of raising."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = text.encode("utf-16", "surrogatepass").decode("utf-16", "replace")
    return text


class PureBpeEncoding:
    """A tiktoken-compatible encoding built from ``mergeable_ranks`` (pure Python)."""

    def __init__(
        self,
        name: str,
        pat_str: str,
        mergeable_ranks: Mapping[bytes, int],
        special_tokens: Mapping[str, int] | None = None,
    ) -> None:
        self.name = name
        self._ranks = mergeable_ranks
        self._special: dict[str, int] = dict(special_tokens or {})
        rx = _regex()
        self._pat = rx.compile(pat_str)
        self._cache: dict[str, tuple[int, ...]] = {}
        self._decoder: dict[int, bytes] | None = None
        values = [*mergeable_ranks.values(), *self._special.values()]
        self.max_token_value = max(values) if values else 0
        if self._special:
            self._special_pat = rx.compile(
                "|".join(rx.escape(s) for s in sorted(self._special, key=len, reverse=True))
            )
        else:
            self._special_pat = None

    # ---- vocabulary facts -------------------------------------------------------
    @property
    def n_vocab(self) -> int:
        return self.max_token_value + 1

    @property
    def special_tokens_set(self) -> set[str]:
        return set(self._special)

    # ---- encoding ---------------------------------------------------------------
    def _piece_ids(self, piece: str) -> tuple[int, ...]:
        hit = self._cache.get(piece)
        if hit is not None:
            return hit
        data = piece.encode("utf-8")
        whole = self._ranks.get(data)
        if whole is not None:
            ids: tuple[int, ...] = (whole,)
        else:
            ids = tuple(self._ranks[p] for p in _merge_pieces(self._ranks, data))
        if len(self._cache) < _WORD_CACHE_MAX:
            self._cache[piece] = ids
        return ids

    def encode_ordinary(self, text: str) -> list[int]:
        """Encode ``text`` treating special-token strings as ordinary text."""
        out: list[int] = []
        for piece in self._pat.findall(_sanitise(text)):
            out.extend(self._piece_ids(piece))
        return out

    def count(self, text: str) -> int:
        """Number of ordinary tokens in ``text`` (same as ``len(encode_ordinary(text))``)."""
        total = 0
        for piece in self._pat.findall(_sanitise(text)):
            total += len(self._piece_ids(piece))
        return total

    def encode(
        self,
        text: str,
        *,
        allowed_special: Collection[str] | str = frozenset(),
        disallowed_special: Collection[str] | str = (),
    ) -> list[int]:
        """tiktoken-style ``encode``.

        Unlike tiktoken (whose ``disallowed_special`` defaults to ``"all"`` and raises on
        text such as ``<|endoftext|>``) the default here is ``()``: such text is encoded
        as ordinary text. That is what token *counting* wants and never raises.
        """
        allowed = set(self._special) if allowed_special == "all" else set(allowed_special)
        if disallowed_special == "all":
            disallowed = set(self._special) - allowed
        else:
            disallowed = set(disallowed_special) - allowed
        text = _sanitise(text)
        if disallowed and self._special_pat is not None:
            for m in self._special_pat.finditer(text):
                if m.group(0) in disallowed:
                    raise ValueError(
                        f"Encountered text corresponding to disallowed special token {m.group(0)!r}."
                    )
        if not allowed or self._special_pat is None:
            return self.encode_ordinary(text)
        rx = _regex()
        pat = rx.compile("|".join(rx.escape(s) for s in sorted(allowed, key=len, reverse=True)))
        out: list[int] = []
        pos = 0
        for m in pat.finditer(text):
            out.extend(self.encode_ordinary(text[pos : m.start()]))
            out.append(self._special[m.group(0)])
            pos = m.end()
        out.extend(self.encode_ordinary(text[pos:]))
        return out

    def encode_single_token(self, text_or_bytes: str | bytes) -> int:
        """The id of exactly one token (a special-token string or a vocabulary entry)."""
        if isinstance(text_or_bytes, str):
            if text_or_bytes in self._special:
                return self._special[text_or_bytes]
            text_or_bytes = text_or_bytes.encode("utf-8")
        try:
            return self._ranks[text_or_bytes]
        except KeyError:
            raise KeyError(text_or_bytes) from None

    # ---- decoding ---------------------------------------------------------------
    def _build_decoder(self) -> dict[int, bytes]:
        if self._decoder is None:
            decoder = {rank: tok for tok, rank in self._ranks.items()}
            decoder.update({rank: s.encode("utf-8") for s, rank in self._special.items()})
            self._decoder = decoder
        return self._decoder

    def decode_bytes(self, tokens: Collection[int]) -> bytes:
        decoder = self._build_decoder()
        return b"".join(decoder[t] for t in tokens)

    def decode(self, tokens: Collection[int], errors: str = "replace") -> str:
        return self.decode_bytes(tokens).decode("utf-8", errors=errors)


def cached_vocab(spec: EncodingSpec, cache_dir: str | os.PathLike[str]) -> Path | None:
    """The vocabulary file in ``cache_dir`` if it is present AND its SHA-256 matches.

    Pure local I/O (no network), cheap enough to call once per process.
    """
    path = Path(cache_dir) / tiktoken_cache_key(spec.url)
    try:
        if path.is_file() and _sha256_file(path) == spec.sha256:
            return path
    except OSError:
        pass
    return None


def fetch_vocab(
    spec: EncodingSpec,
    cache_dir: str | os.PathLike[str],
    *,
    timeout: float = 20.0,
) -> Path | None:
    """Make sure ``spec``'s vocabulary file is in ``cache_dir``; return its path or ``None``.

    * An existing file is reused only when its SHA-256 matches.
    * Otherwise it is downloaded from ``spec.url`` (the official source), verified, and
      moved into place atomically, so a half-written or tampered file is never visible.
    * Never raises: any failure is logged and returns ``None`` (callers then fall back
      to a coarse estimate). This performs blocking network I/O - call it from a worker
      thread / startup warm-up, never from a request hot path.
    """
    dest_dir = Path(cache_dir)
    dest = dest_dir / tiktoken_cache_key(spec.url)
    existing = cached_vocab(spec, dest_dir)
    if existing is not None:
        return existing
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        fd, tmp_name = tempfile.mkstemp(dir=dest_dir, prefix=".bpe-", suffix=".part")
        try:
            with os.fdopen(fd, "wb") as out, urllib.request.urlopen(  # noqa: S310  # nosemgrep
                spec.url, timeout=timeout
            ) as resp:
                while True:
                    chunk = resp.read(1 << 16)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > _VOCAB_MAX_BYTES:
                        raise ValueError("vocabulary download exceeds the size cap")
                    digest.update(chunk)
                    out.write(chunk)
            if digest.hexdigest() != spec.sha256:
                raise ValueError("vocabulary SHA-256 mismatch")
            os.replace(tmp_name, dest)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
        return dest
    except Exception as exc:  # noqa: BLE001 - best effort, never raises
        _log.warning("bpe.vocab_fetch_failed name=%s error=%r", spec.name, exc)
        return None


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
