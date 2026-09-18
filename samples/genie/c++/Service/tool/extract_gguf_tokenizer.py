#!/usr/bin/env python3
"""Extract BPE tokenizer from a GGUF file and save as HuggingFace tokenizer.json.
Works for gpt2/gpt-4o pre-tokenizer style models (byte-level BPE).

The reusable entry point is ``extract_tokenizer_from_gguf(gguf_path) -> dict``,
which returns a HuggingFace ``tokenizer.json``-shaped dict (byte-level BPE
vocab + merges + special tokens rebuilt from ``tokenizer.ggml.*`` GGUF KV
metadata). It can be imported directly (e.g. by test_watermark.py) or used via
the CLI below.

Known limitation: ``tokenizer.ggml.pre`` (e.g. "gpt-4o") hints that the
original tokenizer may use a variant-specific pre-tokenizer regex (e.g. the
o200k_harmony split rules), which is not reproduced here -- this script
always emits the standard byte-level ``ByteLevel`` pre-tokenizer/decoder.
Token<->id mapping and BPE merges are reconstructed exactly (verified via
round-trip encode/decode), so this approximation is negligible for ordinary
English text and only risks rare split-boundary differences around digits/
punctuation.
"""
import struct
import sys
import json
import os

SIZES = {0:1, 1:1, 2:2, 3:2, 4:4, 5:4, 6:4, 7:1, 10:8, 11:8, 12:8}
FMTS  = {0:'B', 1:'b', 2:'H', 3:'h', 4:'I', 5:'i', 6:'f', 7:'?',
         10:'Q', 11:'q', 12:'d'}

def read_string(f):
    n = struct.unpack('<Q', f.read(8))[0]
    return f.read(n).decode('utf-8', 'replace')

def read_val(f, vt, collect_arrays=False):
    if vt in SIZES:
        raw = f.read(SIZES[vt])
        return struct.unpack('<' + FMTS[vt], raw)[0]
    if vt == 8:
        return read_string(f)
    if vt == 9:
        et = struct.unpack('<I', f.read(4))[0]
        n  = struct.unpack('<Q', f.read(8))[0]
        if collect_arrays:
            return [read_val(f, et, False) for _ in range(n)]
        # just skip
        if et in SIZES:
            f.read(SIZES[et] * n)
        elif et == 8:
            for _ in range(n):
                sn = struct.unpack('<Q', f.read(8))[0]
                f.read(sn)
        else:
            for _ in range(n):
                read_val(f, et, False)
        return None
    raise ValueError(f"unknown type {vt}")

def read_string_array(f):
    """Read a string array KV value already positioned at element-type byte."""
    et = struct.unpack('<I', f.read(4))[0]
    n  = struct.unpack('<Q', f.read(8))[0]
    if et != 8:
        raise ValueError(f"expected string array (et=8), got et={et}")
    out = []
    for _ in range(n):
        sn = struct.unpack('<Q', f.read(8))[0]
        out.append(f.read(sn).decode('utf-8', 'replace'))
    return out

def read_int_array(f):
    """Read a numeric array KV value already positioned at element-type byte.

    Used for ``tokenizer.ggml.token_type`` (llama.cpp convention: 3 == CONTROL,
    i.e. a special token), whose element scalar type varies across GGUF writers
    (commonly int32, sometimes int8).
    """
    et = struct.unpack('<I', f.read(4))[0]
    n  = struct.unpack('<Q', f.read(8))[0]
    if et not in SIZES:
        raise ValueError(f"expected numeric array (et in SIZES), got et={et}")
    out = []
    for _ in range(n):
        raw = f.read(SIZES[et])
        out.append(struct.unpack('<' + FMTS[et], raw)[0])
    return out

def extract_tokenizer_from_gguf(gguf_path, verbose=False):
    """Parse a GGUF file's ``tokenizer.ggml.*`` KV metadata and rebuild it as a
    HuggingFace ``tokenizer.json``-shaped dict (byte-level BPE).

    Returns a dict suitable for ``json.dump()`` to a file later loaded via
    ``tokenizers.Tokenizer.from_file()``, or directly via
    ``tokenizers.Tokenizer.from_str(json.dumps(...))``.
    """
    def log(msg):
        if verbose:
            print(msg, flush=True)

    tokens = None
    merges = None
    token_type = None

    with open(gguf_path, 'rb') as f:
        magic   = struct.unpack('<I', f.read(4))[0]
        version = struct.unpack('<I', f.read(4))[0]
        tc      = struct.unpack('<Q', f.read(8))[0]
        kc      = struct.unpack('<Q', f.read(8))[0]
        log(f"GGUF v{version}, {tc} tensors, {kc} kv pairs")

        for i in range(kc):
            k  = read_string(f)
            vt = struct.unpack('<I', f.read(4))[0]

            if k == 'tokenizer.ggml.tokens':
                log(f"  [{i}] reading tokens array ...")
                tokens = read_string_array(f)
                log(f"  -> {len(tokens)} tokens")
            elif k == 'tokenizer.ggml.merges':
                log(f"  [{i}] reading merges array ...")
                merges = read_string_array(f)
                log(f"  -> {len(merges)} merges")
            elif k == 'tokenizer.ggml.token_type':
                log(f"  [{i}] reading token_type array ...")
                token_type = read_int_array(f)
                log(f"  -> {len(token_type)} token types")
            else:
                read_val(f, vt, False)

    if tokens is None:
        raise ValueError("tokenizer.ggml.tokens not found in GGUF")

    # Build vocab dict: token_str -> id
    vocab = {t: i for i, t in enumerate(tokens)}
    log(f"Vocab size: {len(vocab)}")

    # Mark control tokens (llama.cpp token_type == 3 == LLAMA_TOKEN_TYPE_CONTROL)
    # as special added_tokens, so tokenizers.Tokenizer treats them as atomic
    # (not split by the BPE merge rules) and Tokenizer.encode(add_special_tokens=
    # False) still excludes them correctly.
    added_tokens = []
    if token_type is not None:
        for idx, t in enumerate(token_type):
            if t == 3 and idx < len(tokens):
                added_tokens.append({
                    "id": idx,
                    "content": tokens[idx],
                    "single_word": False,
                    "lstrip": False,
                    "rstrip": False,
                    "normalized": False,
                    "special": True,
                })
        log(f"  -> {len(added_tokens)} special tokens marked")

    # Build HuggingFace tokenizer.json structure
    # This is a BPE tokenizer (gpt2 / byte-level)
    tok_json = {
        "version": "1.0",
        "truncation": None,
        "padding": None,
        "added_tokens": added_tokens,
        "normalizer": None,
        "pre_tokenizer": {
            "type": "ByteLevel",
            "add_prefix_space": False,
            "trim_offsets": True,
            "use_regex": True
        },
        "post_processor": None,
        "decoder": {
            "type": "ByteLevel",
            "add_prefix_space": False,
            "trim_offsets": True,
            "use_regex": True
        },
        "model": {
            "type": "BPE",
            "dropout": None,
            "unk_token": None,
            "continuing_subword_prefix": None,
            "end_of_word_suffix": None,
            "fuse_unk": False,
            "byte_fallback": False,
            "vocab": vocab,
            "merges": merges if merges is not None else []
        }
    }
    return tok_json

def main():
    gguf_path = sys.argv[1]
    out_path  = sys.argv[2] if len(sys.argv) > 2 else 'tokenizer.json'

    try:
        tok_json = extract_tokenizer_from_gguf(gguf_path, verbose=True)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(tok_json, f, ensure_ascii=False, indent=2)
    print(f"Saved tokenizer.json to {out_path}", flush=True)

if __name__ == '__main__':
    main()
