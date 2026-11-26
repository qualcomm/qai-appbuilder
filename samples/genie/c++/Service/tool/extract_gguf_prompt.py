#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#=============================================================================
#
# Copyright (c) 2025, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
import json
import struct
import sys
from datetime import datetime

import jinja2
from jinja2.sandbox import ImmutableSandboxedEnvironment

from extract_gguf_tokenizer import read_string, read_string_array, read_val

GGUF_MAGIC = 0x46554747
SLOT = 'string'
S, U, A, T, U2 = 'QAIPHSYS', 'QAIPHUSRONE', 'QAIPHAST', 'QAIPHTOOL', 'QAIPHUSRTWO'


def read_chat_metadata(gguf_path):
    meta = {}
    tokens = []
    with open(gguf_path, 'rb') as f:
        if struct.unpack('<I', f.read(4))[0] != GGUF_MAGIC:
            raise ValueError(f'not a GGUF file: {gguf_path}')
        f.read(12)
        kv_count = struct.unpack('<Q', f.read(8))[0]
        for _ in range(kv_count):
            k = read_string(f)
            vt = struct.unpack('<I', f.read(4))[0]
            if k == 'tokenizer.ggml.tokens':
                tokens = read_string_array(f)
            else:
                meta[k] = read_val(f, vt, False)

    template = meta.get('tokenizer.chat_template')
    if not isinstance(template, str) or not template:
        raise ValueError('tokenizer.chat_template not found in GGUF')

    def token(key):
        i = meta.get(key)
        return tokens[i] if isinstance(i, int) and 0 <= i < len(tokens) else ''

    return template, token('tokenizer.ggml.bos_token_id'), token('tokenizer.ggml.eos_token_id')


def make_renderer(template, bos_token, eos_token):
    def raise_exception(msg):
        raise jinja2.exceptions.TemplateError(msg)

    env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)
    env.globals['raise_exception'] = raise_exception
    env.globals['strftime_now'] = lambda fmt: datetime.now().strftime(fmt)
    compiled = env.from_string(template)

    def render(turns, add_generation_prompt=False):
        messages = [{'role': role, 'content': content} for role, content in turns]
        return compiled.render(messages=messages, tools=None, add_generation_prompt=add_generation_prompt,
                               bos_token=bos_token, eos_token=eos_token)

    return render


def cut(full, prefix, suffix, what):
    if not full.startswith(prefix) or not full.endswith(suffix) or len(full) < len(prefix) + len(suffix):
        raise ValueError(f'chat template is not prefix-stable around the {what} turn')
    return full[len(prefix):len(full) - len(suffix)]


def slot(segment, placeholder, what):
    if segment.count(placeholder) != 1:
        raise ValueError(f'{what} turn does not contain the message content exactly once')
    if SLOT in segment.replace(placeholder, ''):
        raise ValueError(f'{what} turn wrapper already contains the literal "{SLOT}"')
    return segment.replace(placeholder, SLOT)


def split_system_user(render):
    su = render([('system', S), ('user', U)])
    try:
        system = render([('system', S)])
    except jinja2.exceptions.TemplateError:
        user = cut(render([('system', S), ('user', U), ('user', U2)]), su, '', 'user').replace(U2, U)
        return cut(su, '', user, 'system'), user, su
    return system, cut(su, system, '', 'user'), su


def build_prompt(render):
    system, user, su = split_system_user(render)
    user2 = user.replace(U, U2)
    assistant = cut(render([('system', S), ('user', U), ('assistant', A), ('user', U2)]), su, user2, 'assistant')
    start = cut(render([('system', S), ('user', U)], True), su, '', 'generation')
    prompt = {
        'prompt_system': slot(system, S, 'system'),
        'prompt_user': slot(user, U, 'user'),
        'prompt_assistant': slot(assistant, A, 'assistant'),
    }
    try:
        suat = render([('system', S), ('user', U), ('assistant', A), ('tool', T), ('user', U2)])
        prompt['prompt_tool'] = slot(cut(suat, su + assistant, user2, 'tool'), T, 'tool')
    except (ValueError, jinja2.exceptions.TemplateError) as e:
        print(f'WARNING: tool turn not derivable ({e}); prompt_tool falls back to prompt_user', file=sys.stderr)
        prompt['prompt_tool'] = prompt['prompt_user']
    prompt['prompt_start'] = start
    return prompt


def extract_prompt_from_gguf(gguf_path):
    return build_prompt(make_renderer(*read_chat_metadata(gguf_path)))


def main():
    if len(sys.argv) < 2:
        print('usage: extract_gguf_prompt.py <model.gguf> [prompt.json]', file=sys.stderr)
        sys.exit(2)
    gguf_path = sys.argv[1]
    out_path = sys.argv[2] if len(sys.argv) > 2 else 'prompt.json'

    try:
        prompt = extract_prompt_from_gguf(gguf_path)
    except (ValueError, jinja2.exceptions.TemplateError) as e:
        print(f'ERROR: {e}', file=sys.stderr)
        sys.exit(1)

    with open(out_path, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(prompt, f, ensure_ascii=False, indent=2)
        f.write('\n')
    print(json.dumps(prompt, indent=2), flush=True)
    print(f'Saved prompt.json to {out_path}', flush=True)


if __name__ == '__main__':
    main()
