# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""The whisper-base pack must tokenize identically with or without tiktoken.

tiktoken has no Windows ARM64 wheel, so on ARM64 the runner uses ``shared/pure_bpe.py``.
This loads the real runner code (``_load_tokenizer`` / ``_load_tokenizer_mode``) against a
synthetic ``gpt2.tiktoken`` / ``multilingual.tiktoken`` and compares both backends.
"""

from __future__ import annotations

import base64
import importlib.util
import sys
from pathlib import Path

import pytest

_APP = Path(__file__).resolve().parents[3] / "factory" / "chat_features" / "app-builder"


def _vocab() -> bytes:
    toks = [bytes([i]) for i in range(256)] + [b"th", b"he", b"the", b" t", b" the", b"in", b"ing", b" a"]
    return b"".join(base64.b64encode(t) + b" " + str(i).encode() + b"\n" for i, t in enumerate(toks))


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.syspath_prepend(str(_APP / "shared"))
    name = "whisper_base_runner_under_test"
    spec = importlib.util.spec_from_file_location(name, _APP / "models" / "whisper-base" / "runner.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    monkeypatch.setitem(sys.modules, name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _pack(tmp_path: Path, *, multilingual: bool) -> Path:
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / ("multilingual.tiktoken" if multilingual else "gpt2.tiktoken")).write_bytes(_vocab())
    return tmp_path


def _facts(tok) -> dict:
    return {
        "vocab_size": tok.vocab_size,
        "multilingual": tok.is_multilingual,
        "sot_sequence": tok.sot_sequence,
        "eot": tok.eot,
        "no_timestamps": tok.no_timestamps,
        "timestamp_begin": tok.timestamp_begin,
        "specials": dict(tok.special_tokens),
        "enc": {t: tok.encode(t) for t in (" -", " '", "-", "'", "...", " the thing in", "ing")},
        "dec": tok.decode([0, 1, 256, 258, 259, 261, tok.eot + 5]),
    }


@pytest.mark.parametrize("multilingual", [False, True], ids=["base.en", "multilingual"])
def test_pure_backend_matches_tiktoken(runner, tmp_path: Path, monkeypatch, multilingual: bool) -> None:
    pytest.importorskip("tiktoken")
    pack = _pack(tmp_path, multilingual=multilingual)
    real = _facts(runner._load_tokenizer(pack))
    monkeypatch.setitem(sys.modules, "tiktoken", None)  # `import tiktoken` -> ImportError
    assert runner._encoding_class().__name__ == "PureBpeEncoding"
    pure = _facts(runner._load_tokenizer(pack))
    assert pure == real


def test_forced_mode_loader_also_works_without_tiktoken(runner, tmp_path: Path, monkeypatch) -> None:
    pack = _pack(tmp_path, multilingual=True)
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    tok = runner._load_tokenizer_mode(pack, multilingual=True)
    assert tok.is_multilingual and tok.sot_sequence[0] == tok.sot
    assert tok.decode(tok.encode(" the thing")) == " the thing"


def test_a_missing_regex_gives_a_clear_error(runner, monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "tiktoken", None)
    monkeypatch.setitem(sys.modules, "pure_bpe", None)
    with pytest.raises(Exception, match="regex"):
        runner._encoding_class()
