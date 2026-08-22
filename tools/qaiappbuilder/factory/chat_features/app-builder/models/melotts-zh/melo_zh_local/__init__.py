# ---------------------------------------------------------------------
# Derived from MeloTTS (https://github.com/myshell-ai/MeloTTS)
# Copyright (c) 2024 MyShell.ai
# SPDX-License-Identifier: MIT
# ---------------------------------------------------------------------
"""Local minimal subset of melo.text for Chinese-only TTS preprocessing.

This package re-implements the parts of melo.text that are needed by
``melotts_zh_standalone.py`` to produce phones / tones / language ids.

IMPORTANT — alignment with melo runtime:
1. melo's TTS("ZH") sets self.language = "ZH_MIX_EN" internally
   (see melo/api.py:60). The NPU model was trained with this language
   id, NOT plain "ZH".
2. The symbol_to_id mapping comes from the TTS checkpoint, not from the
   default symbols.py enumeration. We load it from a pre-exported JSON
   ("melo_symbol_to_id.json") to guarantee byte-exact match.
"""

import json
import os
from .symbols import (
    symbols,
    language_id_map,
    language_tone_start_map,
    num_tones,
    num_languages,
)

# Load melo's real symbol_to_id (exported from a live TTS object). The
# default enumeration order from symbols.py does NOT match the model's
# expected mapping — using the wrong mapping produces nonsense output.
_PKG_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SYMBOLS_JSON = os.path.join(_PKG_DIR, "melo_symbol_to_id.json")
if os.path.exists(_SYMBOLS_JSON):
    with open(_SYMBOLS_JSON, "r", encoding="utf-8") as _f:
        _symbol_to_id = json.load(_f)
else:
    # Fallback: default enumeration (likely INCORRECT for this model)
    _symbol_to_id = {s: i for i, s in enumerate(symbols)}


def cleaned_text_to_sequence(cleaned_text, tones, language, symbol_to_id=None):
    """Convert cleaned phones/tones to integer ids matching melo runtime.

    Args:
        cleaned_text: list[str] of phone symbols
        tones:        list[int] tone ids per phone (0..5 for Chinese)
        language:     "ZH" (will be auto-promoted to "ZH_MIX_EN" since the
                      NPU model was trained with that language id, matching
                      melo's TTS("ZH") -> internal "ZH_MIX_EN" rewrite)
        symbol_to_id: optional override mapping
    Returns:
        (phones_ids, tones_with_offset, lang_ids) all list[int]
    """
    symbol_to_id_map = symbol_to_id if symbol_to_id else _symbol_to_id
    phones = [symbol_to_id_map[s] for s in cleaned_text]

    # Auto-promote ZH -> ZH_MIX_EN to match melo runtime behavior.
    effective_lang = "ZH_MIX_EN" if language == "ZH" else language
    tone_start = language_tone_start_map[effective_lang]
    tones_out = [t + tone_start for t in tones]
    lang_id = language_id_map[effective_lang]
    lang_ids = [lang_id for _ in phones]
    return phones, tones_out, lang_ids


def clean_text(text: str, language: str):
    """Run text_normalize + g2p for the given language. Only ZH supported here."""
    if language != "ZH":
        raise NotImplementedError(
            f"melo_zh_local only supports ZH; got {language!r}"
        )
    from . import chinese as zh
    norm_text = zh.text_normalize(text)
    phones, tones, word2ph = zh.g2p(norm_text)
    return norm_text, phones, tones, word2ph
