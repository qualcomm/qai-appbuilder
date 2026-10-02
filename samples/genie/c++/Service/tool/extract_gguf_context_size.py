#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import struct
import sys
import os
import json

GGUF_MAGIC = 0x46554747

GGUF_TYPE_STRING = 8
GGUF_TYPE_ARRAY = 9

SIZES = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
FMTS = {0: 'B', 1: 'b', 2: 'H', 3: 'h', 4: 'I', 5: 'i', 6: 'f', 7: '?',
        10: 'Q', 11: 'q', 12: 'd'}


def read_string(f):
    n = struct.unpack('<Q', f.read(8))[0]
    return f.read(n).decode('utf-8', 'replace')


def skip_array(f):
    et = struct.unpack('<I', f.read(4))[0]
    n = struct.unpack('<Q', f.read(8))[0]
    if et in SIZES:
        f.read(SIZES[et] * n)
    elif et == GGUF_TYPE_STRING:
        for _ in range(n):
            sn = struct.unpack('<Q', f.read(8))[0]
            f.read(sn)
    elif et == GGUF_TYPE_ARRAY:
        for _ in range(n):
            skip_array(f)
    else:
        raise ValueError(f"unknown array element type {et}")


def read_scalar(f, vt):
    if vt in SIZES:
        raw = f.read(SIZES[vt])
        return struct.unpack('<' + FMTS[vt], raw)[0]
    if vt == GGUF_TYPE_STRING:
        return read_string(f)
    raise ValueError(f"unknown scalar type {vt}")


def _safe_print(msg):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or 'ascii'
        print(msg.encode(encoding, 'backslashreplace').decode(encoding), flush=True)


def read_metadata_scalars(f, kv_count, verbose=False):
    def log(msg):
        if verbose:
            _safe_print(msg)

    metadata = {}
    for i in range(kv_count):
        key = read_string(f)
        vt = struct.unpack('<I', f.read(4))[0]
        if vt == GGUF_TYPE_ARRAY:
            skip_array(f)
            continue
        value = read_scalar(f, vt)
        metadata[key] = value
        log(f"  [{i}] {key} = {value}")
    return metadata


def extract_context_length_from_gguf(gguf_path, verbose=False):
    def log(msg):
        if verbose:
            _safe_print(msg)

    with open(gguf_path, 'rb') as f:
        magic = struct.unpack('<I', f.read(4))[0]
        if magic != GGUF_MAGIC:
            raise ValueError(
                f"not a GGUF file (magic={magic:#010x}, expected {GGUF_MAGIC:#010x})"
            )
        version = struct.unpack('<I', f.read(4))[0]
        tensor_count = struct.unpack('<Q', f.read(8))[0]
        kv_count = struct.unpack('<Q', f.read(8))[0]
        log(f"GGUF v{version}, {tensor_count} tensors, {kv_count} kv pairs")

        metadata = read_metadata_scalars(f, kv_count, verbose)

    architecture = metadata.get('general.architecture')
    if not architecture:
        raise ValueError("general.architecture not found in GGUF metadata")

    ctx_key = f"{architecture}.context_length"
    context_length = metadata.get(ctx_key)
    if context_length is None:
        raise ValueError(
            f"'{ctx_key}' not found in GGUF metadata (architecture='{architecture}')"
        )

    return architecture, int(context_length)


def merge_context_size_into_config(config_path, context_size):
    if os.path.exists(config_path) and os.path.getsize(config_path) > 0:
        with open(config_path, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    else:
        cfg = {}
    cfg['context_size'] = context_size
    with open(config_path, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
        f.write('\n')


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <gguf_path> [config_json_path]", file=sys.stderr)
        sys.exit(1)

    gguf_path = sys.argv[1]
    config_path = sys.argv[2] if len(sys.argv) > 2 else None

    try:
        architecture, context_length = extract_context_length_from_gguf(gguf_path, verbose=True)
    except (ValueError, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"architecture={architecture} context_length={context_length}", flush=True)

    if config_path:
        merge_context_size_into_config(config_path, context_length)
        print(f"Wrote context_size={context_length} to {config_path}", flush=True)


if __name__ == '__main__':
    main()
