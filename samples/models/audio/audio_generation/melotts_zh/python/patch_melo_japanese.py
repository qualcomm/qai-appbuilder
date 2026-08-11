# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
patch_melo_japanese.py — apply NOTES.md Step 6 to melo/text/japanese.py.

MeloTTS' japanese.py hard-fails to import on Windows-on-Snapdragon because
MeCab (python-mecab / unidic-lite) has no ARM64 wheel and the Japanese BERT
tokenizer is gated. This makes the module import-safe with MeCab / tokenizer
absent — the ZH TTS path never uses them.

Three edits (idempotent — safe to run more than once):
  1. `import MeCab` failure  -> set MeCab = None instead of raising
  2. `_TAGGER = MeCab.Tagger()` -> guard on MeCab is not None
  3. `tokenizer = AutoTokenizer.from_pretrained(model_id)` -> try/except -> None

Called automatically by setup_env.bat; can also be run standalone:
    python patch_melo_japanese.py
"""
import os
import sys

try:
    import melo
except ImportError:
    print("[ERROR] 'melo' (melotts) is not installed yet. Install it first "
          "(setup_env.bat does this before calling this script).")
    sys.exit(1)

JP = os.path.join(os.path.dirname(melo.__file__), "text", "japanese.py")

EDITS = [
    # (description, old, new)
    (
        "import MeCab guard",
        'try:\n    import MeCab\nexcept ImportError as e:\n'
        '    raise ImportError("Japanese requires mecab-python3 and unidic-lite.") from e',
        'try:\n    import MeCab\nexcept Exception:\n    MeCab = None',
    ),
    (
        "_TAGGER guard",
        '_TAGGER = MeCab.Tagger()',
        '_TAGGER = MeCab.Tagger() if MeCab is not None else None',
    ),
    (
        "AutoTokenizer guard",
        'model_id = \'tohoku-nlp/bert-base-japanese-v3\'\n'
        'tokenizer = AutoTokenizer.from_pretrained(model_id)',
        'model_id = \'tohoku-nlp/bert-base-japanese-v3\'\n'
        'try:\n    tokenizer = AutoTokenizer.from_pretrained(model_id)\n'
        'except Exception:\n    tokenizer = None',
    ),
]


def main():
    src = open(JP, encoding="utf-8").read()
    changed = False
    for desc, old, new in EDITS:
        if new in src:
            print(f"[SKIP] {desc}: already patched.")
            continue
        if old in src:
            src = src.replace(old, new, 1)
            changed = True
            print(f"[OK]   {desc}: patched.")
        else:
            print(f"[WARN] {desc}: neither original nor patched text found "
                  f"(melotts version drift?). Check {JP} manually.")
    if changed:
        open(JP, "w", encoding="utf-8").write(src)
        print(f"[DONE] Wrote {JP}")
    else:
        print("[DONE] No changes needed.")


if __name__ == "__main__":
    main()
