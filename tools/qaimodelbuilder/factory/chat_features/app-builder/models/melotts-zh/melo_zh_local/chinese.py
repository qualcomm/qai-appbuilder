# ---------------------------------------------------------------------
# Derived from MeloTTS (https://github.com/myshell-ai/MeloTTS)
# Copyright (c) 2024 MyShell.ai
# SPDX-License-Identifier: MIT
# ---------------------------------------------------------------------
import os
import re
import sys

import cn2an
from pypinyin import lazy_pinyin, Style

from .symbols import punctuation, language_tone_start_map
from .tone_sandhi import ToneSandhi

current_file_path = os.path.dirname(__file__)
with open(
    os.path.join(current_file_path, "opencpop-strict.txt"), encoding="utf-8"
) as _f:
    pinyin_to_symbol_map = {
        line.split("\t")[0]: line.strip().split("\t")[1]
        for line in _f.readlines()
    }
del _f

import jieba.posseg as psg


rep_map = {
    "：": ",",
    "；": ",",
    "，": ",",
    "。": ".",
    "！": "!",
    "？": "?",
    "\n": ".",
    "·": ",",
    "、": ",",
    "...": "…",
    "$": ".",
    """: "'",
    """: "'",
    "'": "'",
    "'": "'",
    "（": "'",
    "）": "'",
    "(": "'",
    ")": "'",
    "《": "'",
    "》": "'",
    "【": "'",
    "】": "'",
    "[": "'",
    "]": "'",
    "—": "-",
    "～": "-",
    "~": "-",
    "「": "'",
    "」": "'",
}

tone_modifier = ToneSandhi()


def replace_punctuation(text):
    """Replace punctuation, keeping Chinese chars and English letters."""
    text = text.replace("嗯", "恩").replace("呣", "母")
    pattern = re.compile("|".join(re.escape(p) for p in rep_map.keys()))
    replaced_text = pattern.sub(lambda x: rep_map[x.group()], text)
    # Keep Chinese characters, English letters, spaces, and punctuation
    replaced_text = re.sub(
        r"[^\u4e00-\u9fa5_a-zA-Z\s" + "".join(re.escape(p) for p in punctuation) + r"]+",
        "",
        replaced_text,
    )
    replaced_text = re.sub(r"[\s]+", " ", replaced_text)
    return replaced_text


def g2p(text):
    """Main G2P entry: supports Chinese-English mixed text (v2 approach)."""
    # Escape punctuation for use in character class to avoid regex range issues
    escaped_puncs = "".join(re.escape(p) for p in punctuation)
    pattern = r"(?<=[{0}])\s*".format(escaped_puncs)
    sentences = [i for i in re.split(pattern, text) if i.strip() != ""]
    phones, tones, word2ph = _g2p_v2(sentences)
    assert sum(word2ph) == len(phones)
    phones = ["_"] + phones + ["_"]
    tones = [0] + tones + [0]
    word2ph = [1] + word2ph + [1]
    return phones, tones, word2ph


def _get_initials_finals(word):
    initials = []
    finals = []
    orig_initials = lazy_pinyin(word, neutral_tone_with_five=True, style=Style.INITIALS)
    orig_finals = lazy_pinyin(
        word, neutral_tone_with_five=True, style=Style.FINALS_TONE3
    )
    for c, v in zip(orig_initials, orig_finals):
        initials.append(c)
        finals.append(v)
    return initials, finals


def _g2p(segments):
    """Pure Chinese G2P (no English support). Used internally by _g2p_v2."""
    phones_list = []
    tones_list = []
    word2ph = []
    for seg in segments:
        # Remove English characters for pure Chinese processing
        seg = re.sub("[a-zA-Z]+", "", seg)
        seg_cut = psg.lcut(seg)
        initials = []
        finals = []
        seg_cut = tone_modifier.pre_merge_for_modify(seg_cut)
        for word, pos in seg_cut:
            if pos == "eng":
                # Skip English fragments in pure Chinese mode
                continue
            sub_initials, sub_finals = _get_initials_finals(word)
            sub_finals = tone_modifier.modified_tone(word, pos, sub_finals)
            initials.append(sub_initials)
            finals.append(sub_finals)

        initials = sum(initials, [])
        finals = sum(finals, [])

        for c, v in zip(initials, finals):
            raw_pinyin = c + v
            if c == v:
                assert c in punctuation
                phone = [c]
                tone = "0"
                word2ph.append(1)
            else:
                v_without_tone = v[:-1]
                tone = v[-1]

                pinyin = c + v_without_tone
                assert tone in "12345"

                if c:
                    # 多音节
                    v_rep_map = {
                        "uei": "ui",
                        "iou": "iu",
                        "uen": "un",
                    }
                    if v_without_tone in v_rep_map.keys():
                        pinyin = c + v_rep_map[v_without_tone]
                else:
                    # 单音节
                    pinyin_rep_map = {
                        "ing": "ying",
                        "i": "yi",
                        "in": "yin",
                        "u": "wu",
                    }
                    if pinyin in pinyin_rep_map.keys():
                        pinyin = pinyin_rep_map[pinyin]
                    else:
                        single_rep_map = {
                            "v": "yu",
                            "e": "e",
                            "i": "y",
                            "u": "w",
                        }
                        if pinyin[0] in single_rep_map.keys():
                            pinyin = single_rep_map[pinyin[0]] + pinyin[1:]

                assert pinyin in pinyin_to_symbol_map.keys(), (pinyin, seg, raw_pinyin)
                phone = pinyin_to_symbol_map[pinyin].split(" ")
                word2ph.append(len(phone))

            phones_list += phone
            tones_list += [int(tone)] * len(phone)
    return phones_list, tones_list, word2ph


def _g2p_v2(segments):
    """Mixed Chinese-English G2P (matches upstream chinese_mix.py _g2p_v2).

    Splits each segment into Chinese/English sub-segments using regex,
    routes Chinese parts to _g2p() and English parts to english.g2p().

    PERFORMANCE: english.py imports g2p_en, which loads ~50MB NLTK + LSTM
    weights and takes ~20 s to import. We avoid that cost entirely for
    pure-Chinese inputs by:
      1. Fast-path: if no ASCII letters anywhere in any segment, skip
         english import and call _g2p() directly.
      2. Otherwise: lazy-import english only when an English fragment is
         actually encountered in the per-fragment loop.
    """
    splitter = '#$&^!@'

    # Fast-path: detect any English letters across all segments. If none,
    # skip the whole mixed-language pipeline (and the heavy g2p_en import).
    has_english = any(re.search(r'[a-zA-Z]', t) for t in segments)
    if not has_english:
        return _g2p(segments)

    phones_list = []
    tones_list = []
    word2ph = []

    # Lazy-imported on first English fragment to avoid the 20s cold start.
    g2p_en = None

    for text in segments:
        assert splitter not in text
        # Split on English words (with surrounding spaces)
        text = re.sub(r'([a-zA-Z\s]+)', lambda x: f'{splitter}{x.group(1)}{splitter}', text)
        texts = text.split(splitter)
        texts = [t for t in texts if len(t) > 0]

        for text_part in texts:
            if re.match(r'[a-zA-Z\s]+', text_part):
                # English segment: use english.py g2p (lazy import)
                if g2p_en is None:
                    from .english import g2p as g2p_en  # noqa: F811
                # Tokenize using simple whitespace split for word2ph alignment
                words = text_part.strip().split()
                # Build tokenized list: each word is one "group"
                tokenized = []
                for w in words:
                    tokenized.append(w.lower())

                phones_en, tones_en, word2ph_en = g2p_en(
                    text=None, pad_start_end=False, tokenized=tokenized
                )
                # Apply EN tone offset
                tones_en = [t + language_tone_start_map['EN'] for t in tones_en]
                phones_list += phones_en
                tones_list += tones_en
                word2ph += word2ph_en
            else:
                # Chinese segment: use _g2p
                phones_zh, tones_zh, word2ph_zh = _g2p([text_part])
                phones_list += phones_zh
                tones_list += tones_zh
                word2ph += word2ph_zh

    return phones_list, tones_list, word2ph


def text_normalize(text):
    """Normalize text: convert numbers to Chinese, replace punctuation.

    Preserves English characters for mixed-language support.
    """
    numbers = re.findall(r"\d+(?:\.?\d+)?", text)
    for number in numbers:
        text = text.replace(number, cn2an.an2cn(number), 1)
    text = replace_punctuation(text)
    return text


def get_bert_feature(text, word2ph, device=None):
    raise NotImplementedError(
        "melo_zh_local does not provide BERT features. Use BERT zeros instead."
    )


if __name__ == "__main__":
    text = "啊！但是《原神》是由,米哈游自主，  [研发]的一款全.新开放世界.冒险游戏"
    text = text_normalize(text)
    print(text)
    phones, tones, word2ph = g2p(text)
    print(phones, tones, word2ph)

    # Test mixed Chinese-English
    text2 = "今天天气很好。Good morning."
    text2 = text_normalize(text2)
    print(text2)
    phones2, tones2, word2ph2 = g2p(text2)
    print(phones2, tones2, word2ph2)
