# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Locale-aware heuristic rule pack for the discussion Intent Router (§21.3.1).

This module is the SINGLE, CENTRAL home for the short-phrase dictionaries the
:mod:`~qai.chat.application.use_cases.discussion_intent` router consults to
classify a user message into one of the five public discussion intents
(``social`` / ``ack`` / ``follow_up`` / ``directed_follow_up`` / ``deep_task``)
plus an internal subtype.  Per AGENTS.md §3.8.1 ("is it a tunable config value
or a part of the code?") these phrase lists are, for the MVP, **part of the
code**: they are NOT user-configurable, do NOT live in the DB, and are NOT
exposed in any UI.  So they are plain Python constants under ``src/`` — there is
deliberately **no YAML loader** (a YAML rule pack only earns its keep when
runtime override is a real requirement; introducing it now would be premature
infrastructure and a reverse-misuse of the factory/data config split).

Design contract (§21.3.1):

1. **Central, locale-organised** — one dict per signal category, keyed by
   ``"en"`` / ``"zh-CN"`` / ``"zh-TW"``; each locale carries an ``exact`` set
   (whole-message match after normalisation) and a ``contains`` set (substring
   match).  Never sprinkle ``if message in [...]`` inside ``execute()``.
2. **Signal only, never the sole judge** — a dictionary hit is ONE input.  The
   router combines it with message length, ``?`` presence, ``@mention``
   presence, task-verb presence, ``discussion_state`` and the ``awaiting_user``
   flag (e.g. "继续" matches CONTINUE_REQUEST, but "继续集成这个 SDK 的步骤是
   什么？" carries a real question and must win as ``follow_up``).
3. **Normalise before matching** — trim, strip trailing CJK / ASCII punctuation,
   casefold; so ``OK`` / ``ok`` / ``好的。`` / ``继续？`` all hit (the router
   still uses ``discussion_state`` to disambiguate command vs question).
4. **Deterministic, no external tokeniser** — topic-overlap uses character
   n-gram / keyword Jaccard (§21.14#7); the "short message" threshold and the
   task-verb universe are constants here so every branch is testable.

> **Second-phase override priority** (when this becomes configurable):
> user custom > workspace > built-in locale > fallback ``en``.  Not built now.
"""

from __future__ import annotations

import re

__all__ = [
    "SUPPORTED_LOCALES",
    "FALLBACK_LOCALE",
    "SHORT_MESSAGE_MAX_CHARS",
    "TOPIC_OVERLAP_THRESHOLD",
    "TOPIC_NGRAM_SIZE",
    "ACK_PASSIVE_TERMS",
    "CONTINUE_REQUEST_TERMS",
    "THANKS_OR_CLOSING_TERMS",
    "SOCIAL_GREETING_TERMS",
    "TASK_VERB_TERMS",
    "FOLLOW_UP_HINT_TERMS",
    "IMPLEMENT_VERB_TERMS",
    "QUESTION_WORD_TERMS",
    "TRAILING_PUNCTUATION",
    "normalize_message",
    "iter_locale_terms",
    "matches_exact",
    "contains_term",
    "contains_any",
    "keyword_set",
    "topic_overlap_ratio",
    "FOCUS_STOPWORD_TERMS",
    "extract_focus_terms",
]


# ---------------------------------------------------------------------------
# Locale registry + numeric thresholds (deterministic, §21.14#7)
# ---------------------------------------------------------------------------
#: The locales the rule pack ships phrase lists for.  Matching always tries the
#: caller-provided locale first, then falls back to scanning EVERY locale so a
#: zh-CN user typing an English "ok" still resolves (the router is language-
#: tolerant; the rule pack is the union of signals, never gated by UI language).
SUPPORTED_LOCALES: tuple[str, ...] = ("en", "zh-CN", "zh-TW")
#: Fallback locale used when an unknown locale tag is supplied.
FALLBACK_LOCALE = "en"

#: A message is "short" (a candidate for social/ack) when its normalised length
#: is at or below this many characters.  Intentionally generous enough to cover
#: "thanks a lot!" / "好的，辛苦了" but well under a real question/task.
SHORT_MESSAGE_MAX_CHARS = 16

#: Jaccard similarity (0..1) over keyword sets at/above which two messages are
#: treated as "about the same topic" (drives follow_up continuity, §21.14#7-①).
TOPIC_OVERLAP_THRESHOLD = 0.18
#: Character n-gram size used for CJK-friendly, tokeniser-free topic overlap.
TOPIC_NGRAM_SIZE = 2


# ---------------------------------------------------------------------------
# Punctuation normalisation
# ---------------------------------------------------------------------------
#: Trailing punctuation stripped before dictionary matching.  Kept as a single
#: source of truth so the router and tests normalise identically.
TRAILING_PUNCTUATION = "。！？～.!?,，、；;: \t\r\n"

#: Question marks (ASCII + full-width) — presence is a strong follow_up signal.
_QUESTION_MARKS = ("?", "？")


# ---------------------------------------------------------------------------
# Phrase dictionaries — each: {locale: {"exact": {...}, "contains": {...}}}
# All right-hand-side phrases are stored NORMALISED (casefolded, no trailing
# punctuation) so matching compares like-with-like.
# ---------------------------------------------------------------------------

#: Passive acknowledgements — "I heard you / fine / got it".  These DO NOT ask
#: the discussion to continue (that is CONTINUE_REQUEST_TERMS, kept separate per
#: §21.3 so "继续" never collapses into a passive ack).
ACK_PASSIVE_TERMS: dict[str, dict[str, frozenset[str]]] = {
    "en": {
        "exact": frozenset(
            {
                "ok",
                "okay",
                "k",
                "got it",
                "understood",
                "noted",
                "sure",
                "fine",
                "alright",
                "right",
                "i see",
                "makes sense",
                "sounds good",
                "cool",
            }
        ),
        "contains": frozenset(),
    },
    "zh-CN": {
        "exact": frozenset(
            {
                "好",
                "好的",
                "行",
                "嗯",
                "嗯嗯",
                "收到",
                "明白",
                "明白了",
                "知道了",
                "懂了",
                "了解",
                "可以",
                "没问题",
                "好吧",
            }
        ),
        "contains": frozenset(),
    },
    "zh-TW": {
        "exact": frozenset(
            {
                "好",
                "好的",
                "行",
                "嗯",
                "嗯嗯",
                "收到",
                "明白",
                "明白了",
                "知道了",
                "懂了",
                "瞭解",
                "了解",
                "可以",
                "沒問題",
                "好吧",
            }
        ),
        "contains": frozenset(),
    },
}

#: Continuation requests — the user wants the discussion to GO ON / expand.
#: Kept distinct from passive ack (§21.3, §21.11): combined with an active /
#: awaiting_user state these route to a SCOPED continuation, not a stop.
CONTINUE_REQUEST_TERMS: dict[str, dict[str, frozenset[str]]] = {
    "en": {
        "exact": frozenset(
            {
                "continue",
                "go on",
                "carry on",
                "keep going",
                "more",
                "tell me more",
                "go ahead",
                "and then",
                "what else",
            }
        ),
        "contains": frozenset(
            {
                "go on",
                "keep going",
                "tell me more",
                "say more",
                "expand on",
                "elaborate",
            }
        ),
    },
    "zh-CN": {
        "exact": frozenset(
            {
                "继续",
                "接着说",
                "接着",
                "go on",
                "然后呢",
                "还有呢",
                "再说说",
                "展开说说",
                "说下去",
            }
        ),
        "contains": frozenset(
            {
                "继续说",
                "接着说",
                "再说说",
                "展开说",
                "说下去",
                "然后呢",
                "还有呢",
            }
        ),
    },
    "zh-TW": {
        "exact": frozenset(
            {
                "繼續",
                "接著說",
                "接著",
                "go on",
                "然後呢",
                "還有呢",
                "再說說",
                "展開說說",
                "說下去",
            }
        ),
        "contains": frozenset(
            {
                "繼續說",
                "接著說",
                "再說說",
                "展開說",
                "說下去",
                "然後呢",
                "還有呢",
            }
        ),
    },
}

#: Thanks / closing — gratitude or "let's wrap up".  Routes to wrapup_mode and
#: (from awaiting_user) transitions the discussion to ``closed`` (§21.4, §21.6).
THANKS_OR_CLOSING_TERMS: dict[str, dict[str, frozenset[str]]] = {
    "en": {
        "exact": frozenset(
            {
                "thanks",
                "thank you",
                "thx",
                "ty",
                "cheers",
                "great thanks",
                "thanks a lot",
                "thank you so much",
                "that's all",
                "thats all",
                "we're done",
                "were done",
                "all done",
                "let's stop here",
                "lets stop here",
                "that's enough",
                "thats enough",
                "good work",
                "well done",
                "nice work",
            }
        ),
        "contains": frozenset(
            {
                "thank you",
                "thanks",
                "that's all",
                "thats all",
                "we are done",
                "let's stop",
                "lets stop",
                "good work",
                "well done",
            }
        ),
    },
    "zh-CN": {
        "exact": frozenset(
            {
                "谢谢",
                "谢了",
                "多谢",
                "感谢",
                "辛苦了",
                "辛苦",
                "先这样",
                "就这样",
                "先到这",
                "先到这里",
                "到此为止",
                "结束吧",
                "可以了",
                "够了",
                "干得好",
                "做得好",
            }
        ),
        "contains": frozenset(
            {
                "谢谢",
                "感谢",
                "辛苦了",
                "先这样",
                "就这样",
                "先到这",
                "到此为止",
                "干得好",
                "做得好",
            }
        ),
    },
    "zh-TW": {
        "exact": frozenset(
            {
                "謝謝",
                "謝了",
                "多謝",
                "感謝",
                "辛苦了",
                "辛苦",
                "先這樣",
                "就這樣",
                "先到這",
                "先到這裡",
                "到此為止",
                "結束吧",
                "可以了",
                "夠了",
                "幹得好",
                "做得好",
            }
        ),
        "contains": frozenset(
            {
                "謝謝",
                "感謝",
                "辛苦了",
                "先這樣",
                "就這樣",
                "先到這",
                "到此為止",
                "幹得好",
                "做得好",
            }
        ),
    },
}

#: Pure social greetings / openers — "hi / hello / 你好".  Routes to social_mode
#: and (unlike thanks) leaves the discussion state unchanged (§21.4, §21.6).
SOCIAL_GREETING_TERMS: dict[str, dict[str, frozenset[str]]] = {
    "en": {
        "exact": frozenset(
            {
                "hi",
                "hello",
                "hey",
                "yo",
                "hiya",
                "good morning",
                "good afternoon",
                "good evening",
                "morning",
                "evening",
                "how are you",
                "how's it going",
                "hows it going",
                "what's up",
                "whats up",
                "sup",
            }
        ),
        "contains": frozenset(),
    },
    "zh-CN": {
        "exact": frozenset(
            {
                "你好",
                "你们好",
                "您好",
                "您们好",
                "大家好",
                "各位好",
                "嗨",
                "哈喽",
                "早",
                "早上好",
                "下午好",
                "晚上好",
                "在吗",
                "在不在",
                "最近怎么样",
            }
        ),
        "contains": frozenset(),
    },
    "zh-TW": {
        "exact": frozenset(
            {
                "你好",
                "你們好",
                "您好",
                "您們好",
                "大家好",
                "各位好",
                "嗨",
                "哈囉",
                "早",
                "早安",
                "午安",
                "晚安",
                "在嗎",
                "在不在",
                "最近怎麼樣",
            }
        ),
        "contains": frozenset(),
    },
}

#: Task verbs — strong "do a substantive analysis" signal.  Presence promotes a
#: message towards ``deep_task`` (or ``directed_deep_task`` with an @mention),
#: §21.4 / §21.14#7-③.  Stored normalised; matched as substrings (CJK verbs are
#: not whitespace-delimited).
TASK_VERB_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {
            "analyze",
            "analyse",
            "evaluate",
            "assess",
            "design",
            "implement",
            "build",
            "refactor",
            "redesign",
            "rearchitect",
            "compare",
            "review",
            "investigate",
            "diagnose",
            "plan",
            "draft",
            "propose",
            "summarize",
            "summarise",
            "optimize",
            "optimise",
            "debug",
            "benchmark",
            "estimate",
        }
    ),
    "zh-CN": frozenset(
        {
            "分析",
            "评估",
            "完整评估",
            "评价",
            "设计",
            "重新设计",
            "实现",
            "实施",
            "构建",
            "搭建",
            "重构",
            "比较",
            "对比",
            "评审",
            "审查",
            "调研",
            "调查",
            "诊断",
            "排查",
            "规划",
            "拟定",
            "起草",
            "提出方案",
            "总结",
            "优化",
            "调试",
            "测算",
            "估算",
            "梳理",
        }
    ),
    "zh-TW": frozenset(
        {
            "分析",
            "評估",
            "完整評估",
            "評價",
            "設計",
            "重新設計",
            "實現",
            "實施",
            "構建",
            "搭建",
            "重構",
            "比較",
            "對比",
            "評審",
            "審查",
            "調研",
            "調查",
            "診斷",
            "排查",
            "規劃",
            "擬定",
            "起草",
            "提出方案",
            "總結",
            "優化",
            "調試",
            "測算",
            "估算",
            "梳理",
        }
    ),
}

#: Implement verbs (DISC-1 §22.5/§22.7) — a deliberately NARROW subset of
#: :data:`TASK_VERB_TERMS` carrying ONLY a "do it / land the change / write the
#: code" connotation.  Discussion-flavoured task verbs (analyze / evaluate /
#: review / investigate / 分析 / 评估 / 调研 / 设计) are EXCLUDED so "@dev 分析一下"
#: stays a ``directed_deep_task`` discussion and never trips the implement route.
#: An ``@mention`` + an IMPLEMENT verb in a NON-question message marks
#: ``subtype="implement"`` / ``route_kind="directed_implement"`` (DISC-1 step1
#: router; the public ``deep_task`` intent is UNCHANGED).  Stored normalised;
#: matched as substrings (CJK verbs are not whitespace-delimited).
IMPLEMENT_VERB_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {
            "implement",
            "build",
            "write the code",
            "make it",
            "code it",
            "create the",
            "modify the code",
            "fix the",
            "refactor the",
        }
    ),
    "zh-CN": frozenset(
        {
            "实现",
            "实施",
            "写代码",
            "写出来",
            "做出来",
            "改代码",
            "修复",
            "落地",
            "把它实现",
        }
    ),
    "zh-TW": frozenset(
        {
            "實現",
            "實施",
            "寫程式",
            "做出來",
            "改程式碼",
            "修復",
        }
    ),
}

#: Lighter follow-up hints — "expand / detail / why / what about".  Weaker than
#: a task verb; nudges a short, on-topic message towards ``follow_up`` / scoped
#: (not full discussion).  §21.3 L2.
FOLLOW_UP_HINT_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {
            "expand",
            "detail",
            "more detail",
            "why",
            "how about",
            "what about",
            "what if",
            "explain",
            "clarify",
            "which",
            "and the",
        }
    ),
    "zh-CN": frozenset(
        {
            "展开",
            "详细",
            "详细说",
            "为什么",
            "为何",
            "怎么",
            "如何",
            "那这个",
            "那这",
            "这个呢",
            "解释",
            "说明",
            "具体",
            "哪个",
        }
    ),
    "zh-TW": frozenset(
        {
            "展開",
            "詳細",
            "詳細說",
            "為什麼",
            "為何",
            "怎麼",
            "如何",
            "那這個",
            "那這",
            "這個呢",
            "解釋",
            "說明",
            "具體",
            "哪個",
        }
    ),
}

#: Interrogative words — a question signal even without a ``?`` (e.g. "为什么这样
#: 设计").  Used in the grey-zone tie-breaker (§21.3 L3).
QUESTION_WORD_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {
            "what",
            "why",
            "how",
            "when",
            "where",
            "which",
            "who",
            "can you",
            "could you",
            "should we",
        }
    ),
    "zh-CN": frozenset(
        {
            "什么",
            "为什么",
            "为何",
            "怎么",
            "怎样",
            "如何",
            "哪",
            "哪个",
            "哪些",
            "是否",
            "能不能",
            "可否",
            "吗",
            "呢",
        }
    ),
    "zh-TW": frozenset(
        {
            "什麼",
            "為什麼",
            "為何",
            "怎麼",
            "怎樣",
            "如何",
            "哪",
            "哪個",
            "哪些",
            "是否",
            "能不能",
            "可否",
            "嗎",
            "呢",
        }
    ),
}


# ---------------------------------------------------------------------------
# Normalisation + matching helpers (pure, deterministic — §21.3.1#4)
# ---------------------------------------------------------------------------
def normalize_message(text: str) -> str:
    """Return ``text`` trimmed, trailing-punctuation-stripped, and casefolded.

    The single normalisation used everywhere a phrase is compared so the rule
    pack and the router agree byte-for-byte.  Leading/trailing whitespace and a
    run of trailing CJK/ASCII punctuation are removed; the result is casefolded
    (Unicode-aware lower-casing) so ``OK`` / ``ok`` and ``好的。`` / ``好的``
    collapse to the same key.  A non-``str`` input yields ``""``.
    """
    if not isinstance(text, str):
        return ""
    stripped = text.strip()
    # Strip a run of trailing punctuation/whitespace (keeps INTERNAL ``?`` so
    # the router can still detect a question via ``has_question_mark``).
    stripped = stripped.rstrip(TRAILING_PUNCTUATION)
    return stripped.casefold()


def _resolve_locale(locale: str | None) -> str:
    """Return a supported locale tag, falling back to ``en`` for unknowns."""
    if isinstance(locale, str) and locale in SUPPORTED_LOCALES:
        return locale
    return FALLBACK_LOCALE


def iter_locale_terms(
    table: dict[str, dict[str, frozenset[str]]] | dict[str, frozenset[str]],
    locale: str | None,
) -> tuple[str, ...]:
    """Locale registry helper used by tests; not part of the matching hot path."""
    resolved = _resolve_locale(locale)
    ordered: list[str] = [resolved]
    ordered.extend(loc for loc in SUPPORTED_LOCALES if loc != resolved)
    out: list[str] = []
    for loc in ordered:
        entry = table.get(loc)
        if isinstance(entry, dict):
            for group in entry.values():
                out.extend(sorted(group))
        elif isinstance(entry, (set, frozenset)):
            out.extend(sorted(entry))
    return tuple(out)


def matches_exact(
    normalized: str,
    table: dict[str, dict[str, frozenset[str]]],
    locale: str | None = None,
) -> bool:
    """Return ``True`` when ``normalized`` exactly equals a phrase in ``table``.

    Tries the resolved locale first, then EVERY other supported locale (so a
    cross-language short reply still resolves).  ``normalized`` MUST already be
    :func:`normalize_message`-d by the caller.
    """
    if not normalized:
        return False
    resolved = _resolve_locale(locale)
    for loc in (resolved, *(l for l in SUPPORTED_LOCALES if l != resolved)):
        entry = table.get(loc)
        if entry and normalized in entry.get("exact", frozenset()):
            return True
    return False


def contains_term(
    normalized: str,
    table: dict[str, dict[str, frozenset[str]]],
    locale: str | None = None,
) -> bool:
    """Return ``True`` when ``normalized`` CONTAINS a ``contains``-group phrase."""
    if not normalized:
        return False
    resolved = _resolve_locale(locale)
    for loc in (resolved, *(l for l in SUPPORTED_LOCALES if l != resolved)):
        entry = table.get(loc)
        if not entry:
            continue
        for phrase in entry.get("contains", frozenset()):
            if phrase and phrase in normalized:
                return True
    return False


def contains_any(
    normalized: str,
    table: dict[str, frozenset[str]],
    locale: str | None = None,
) -> bool:
    """Return ``True`` when ``normalized`` contains any flat-set term in ``table``.

    For the flat ``{locale: frozenset[str]}`` tables (task verbs, follow-up
    hints, question words).  Substring match (CJK terms are not whitespace-
    delimited).  Scans the resolved locale first then the others.
    """
    if not normalized:
        return False
    resolved = _resolve_locale(locale)
    for loc in (resolved, *(l for l in SUPPORTED_LOCALES if l != resolved)):
        for term in table.get(loc, frozenset()):
            if term and term in normalized:
                return True
    return False


# ---------------------------------------------------------------------------
# Topic-overlap (tokeniser-free, deterministic — §21.14#7-①)
# ---------------------------------------------------------------------------
_WORD_RE = re.compile(r"[a-z0-9]+")
# CJK unified ideograph range — used to harvest single chars + bigrams so we
# never depend on a Chinese tokeniser (jieba etc.).
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


def keyword_set(text: str, *, ngram: int = TOPIC_NGRAM_SIZE) -> frozenset[str]:
    """Return a deterministic keyword/charactergram set for topic overlap.

    Combines two tokeniser-free signals:

    * **Latin/digit words** — ``[a-z0-9]+`` runs from the casefolded text;
    * **CJK character n-grams** — every length-``ngram`` window over the run of
      CJK ideographs (plus single chars when the run is shorter than ``ngram``).

    This keeps overlap detection language-agnostic and dependency-free
    (§21.14#7).  Punctuation / whitespace are ignored.  Returns an empty set
    for non-text / empty input.
    """
    if not isinstance(text, str) or not text:
        return frozenset()
    lowered = text.casefold()
    out: set[str] = set(_WORD_RE.findall(lowered))
    # Harvest contiguous CJK runs, then slide an n-gram window over each.
    cjk_run: list[str] = []

    def _flush() -> None:
        if not cjk_run:
            return
        run = "".join(cjk_run)
        if len(run) < ngram:
            out.add(run)
        else:
            for i in range(len(run) - ngram + 1):
                out.add(run[i : i + ngram])
        cjk_run.clear()

    for ch in lowered:
        if _CJK_RE.match(ch):
            cjk_run.append(ch)
        else:
            _flush()
    _flush()
    return frozenset(out)


def topic_overlap_ratio(text_a: str, text_b: str) -> float:
    """Return the Jaccard similarity (0..1) of two messages' keyword sets.

    ``|A ∩ B| / |A ∪ B|`` over :func:`keyword_set` outputs.  ``0.0`` when either
    set is empty.  Deterministic and tokeniser-free so it is unit-testable and
    Linux/Windows-portable (§21.14#7).
    """
    set_a = keyword_set(text_a)
    set_b = keyword_set(text_b)
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    if inter == 0:
        return 0.0
    union = len(set_a | set_b)
    return inter / union if union else 0.0


# ---------------------------------------------------------------------------
# Focus-term extraction (DISC-2 §22A.6 P3-b — light follow-up focus hints)
# ---------------------------------------------------------------------------
#: Deterministic, tokeniser-free stop-words stripped before a follow-up's focus
#: term survives.  These are the function/pointer/particle words a scoped
#: follow-up carries that DO NOT name the topic the user wants the roles to
#: concentrate on ("那 / 这个 / 呢 / 的" / "the / about / what / how").  Kept
#: minimal and conservative (§22A.6 P3-b: "比主题树简单得多") — a word not in this
#: set is treated as a real content term.  The question/continue/follow-hint
#: phrase sets above are ALSO consulted at runtime (so we never duplicate "为什么"
#: / "继续" / "what about" here); this set only adds the pointer/particle words
#: those tables do not carry.  Stored NORMALISED (casefolded).
FOCUS_STOPWORD_TERMS: dict[str, frozenset[str]] = {
    "en": frozenset(
        {
            "the",
            "a",
            "an",
            "about",
            "of",
            "for",
            "on",
            "in",
            "to",
            "and",
            "or",
            "this",
            "that",
            "these",
            "those",
            "it",
            "its",
            "then",
            "so",
            "please",
            "could",
            "would",
            "should",
            "do",
            "does",
            "is",
            "are",
            "be",
            "us",
            "we",
            "you",
            "me",
            "i",
        }
    ),
    "zh-CN": frozenset(
        {
            "那",
            "这",
            "那个",
            "这个",
            "那些",
            "这些",
            "那么",
            "这么",
            "它",
            "的",
            "了",
            "呢",
            "吗",
            "吧",
            "啊",
            "呀",
            "嘛",
            "方面",
            "问题",
            "这边",
            "那边",
            "请",
            "一下",
            "我们",
            "你们",
            "他们",
        }
    ),
    "zh-TW": frozenset(
        {
            "那",
            "這",
            "那個",
            "這個",
            "那些",
            "這些",
            "那麼",
            "這麼",
            "它",
            "的",
            "了",
            "呢",
            "嗎",
            "吧",
            "啊",
            "呀",
            "嘛",
            "方面",
            "問題",
            "這邊",
            "那邊",
            "請",
            "一下",
            "我們",
            "你們",
            "他們",
        }
    ),
}

#: Single CJK ideograph run + Latin word harvester for focus extraction.  Splits
#: a normalised message into ordered tokens: each maximal Latin/digit run is one
#: token; each maximal CJK ideograph run is one token (we keep the WHOLE run,
#: e.g. "安全方面" stays together so "方面" can be peeled off as a stop-word).
_FOCUS_TOKEN_RE = re.compile(r"[a-z0-9]+|[\u4e00-\u9fff]+")
#: ASCII / full-width interrogative leading particles peeled off a CJK run head
#: ("那安全呢" → strip "那" → "安全"; the trailing "呢" was already stripped by
#: ``normalize_message``'s trailing-punctuation pass only for punctuation, so the
#: CJK stop-word peel below handles particle suffixes/prefixes).
_CJK_LEADING_PEEL = ("那", "這", "这", "那個", "那个", "這個", "这个")


def _peel_cjk_stopwords(run: str, stop: frozenset[str]) -> str:
    """Peel known leading/trailing stop-words off a single CJK run.

    Deterministic, tokeniser-free: repeatedly strips a stop-word from the head
    or tail of ``run`` (longest match first) so "那安全方面呢" → "安全" without a
    Chinese segmenter.  ``呢``/``吗`` were left attached when not trailing
    punctuation, so peeling them here keeps the extractor robust.
    """
    if not run:
        return run
    ordered_stops = sorted(stop, key=len, reverse=True)
    changed = True
    while changed and run:
        changed = False
        for w in ordered_stops:
            if not w:
                continue
            if run.startswith(w) and len(run) > len(w):
                run = run[len(w) :]
                changed = True
                break
            if run.endswith(w) and len(run) > len(w):
                run = run[: -len(w)]
                changed = True
                break
    return run


def extract_focus_terms(
    message: str,
    *,
    locale: str | None = None,
    max_terms: int = 3,
) -> tuple[str, ...]:
    """Extract up to ``max_terms`` deterministic focus keywords from a follow-up.

    DISC-2 §22A.6 P3-b ("light follow-up focus hints").  Given a scoped
    follow-up message ("那安全方面呢？" / "what about the cost?") return the
    content term(s) the user wants the roles to concentrate on (``("安全",)`` /
    ``("cost",)``).  Pure, deterministic, tokeniser-free (§21.14#7) — the same
    inputs always yield the same tuple, so it is trivially unit-testable and
    Linux/Windows-portable.

    Algorithm (conservative, "宁少不滥"):

    1. :func:`normalize_message` (trim, strip trailing punctuation, casefold).
    2. Split into ordered tokens via :data:`_FOCUS_TOKEN_RE` (one token per
       maximal Latin/digit run and per maximal CJK run).
    3. Drop tokens that are pure function/pointer/particle words
       (:data:`FOCUS_STOPWORD_TERMS`) or interrogative / continue / follow-hint
       cues (reusing :data:`QUESTION_WORD_TERMS` / :data:`CONTINUE_REQUEST_TERMS`
       / :data:`FOLLOW_UP_HINT_TERMS` so we never duplicate "为什么" / "继续" /
       "what about").  For CJK runs first peel leading/trailing stop-words
       (:func:`_peel_cjk_stopwords`) so "那安全方面" → "安全".
    4. Return the first ``max_terms`` surviving tokens in order, de-duplicated.

    Returns an EMPTY tuple when nothing content-bearing survives (a bare
    "这个呢？" / pure question words) — the caller then injects NO focus hint, so
    the framing is byte-for-byte unchanged (zero-regression).
    """
    if not isinstance(message, str):
        return ()
    if not isinstance(max_terms, int) or max_terms < 1:
        return ()
    normalized = normalize_message(message)
    if not normalized:
        return ()

    resolved = _resolve_locale(locale)
    scan_locales = (resolved, *(l for l in SUPPORTED_LOCALES if l != resolved))

    # Union the stop / cue sets across the scan locales (language-tolerant — a
    # zh user may type an English follow-up and vice versa).
    stop_terms: set[str] = set()
    cue_terms: set[str] = set()
    for loc in scan_locales:
        stop_terms |= FOCUS_STOPWORD_TERMS.get(loc, frozenset())
        cue_terms |= QUESTION_WORD_TERMS.get(loc, frozenset())
        cue_terms |= FOLLOW_UP_HINT_TERMS.get(loc, frozenset())
        entry = CONTINUE_REQUEST_TERMS.get(loc)
        if entry:
            cue_terms |= entry.get("exact", frozenset())
            cue_terms |= entry.get("contains", frozenset())
    stop_frozen = frozenset(stop_terms)

    out: list[str] = []
    seen: set[str] = set()
    for raw_token in _FOCUS_TOKEN_RE.findall(normalized):
        token = raw_token
        # CJK run: peel pointer/particle stop-words off head & tail first.
        if _CJK_RE.match(token):
            token = _peel_cjk_stopwords(token, stop_frozen)
        if not token:
            continue
        # A token that IS a stop-word / interrogative / continue / follow-hint
        # cue (after peeling) carries no topic — skip it.
        if token in stop_frozen or token in cue_terms:
            continue
        # A CJK token that, after peeling, still equals a cue/stop (e.g. only
        # "为什么" remained) is dropped above.  A surviving Latin stop also.
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= max_terms:
            break
    return tuple(out)
