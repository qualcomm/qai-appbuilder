# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``make_truncation_notice`` must describe an OUTPUT-cap truncation.

``finish_reason=length`` means the completion hit the per-call
``max_tokens`` ceiling — it is NOT an input context overflow (that
surfaces as a provider ``prompt_too_long`` 400 and is handled by the
separate context-overflow recovery path).  The old notice nonetheless
told the user to run ``/compact``, which only shrinks the INPUT history
and therefore cannot fix an output-cap truncation — users compacted
repeatedly with no effect.  These tests pin the corrected wording.
"""

from __future__ import annotations

from qai.chat.infrastructure.notice_text import (
    make_content_filter_notice,
    make_truncation_notice,
)


def test_truncation_notice_zh_clarifies_output_cap() -> None:
    text = make_truncation_notice()
    assert "生成被截断" in text
    # Names the output cap explicitly and offers the actionable fixes.
    assert "输出" in text
    assert "max_tokens" in text
    # The old unconditional /compact-only advice is gone.
    assert "请使用 `/compact` 压缩历史记录后重试" not in text


def test_truncation_notice_zh_names_truncated_tool() -> None:
    text = make_truncation_notice("grep")
    assert "`grep`" in text


def test_truncation_notice_english() -> None:
    text = make_truncation_notice(language="en")
    assert "Generation truncated" in text
    assert "max_tokens" in text
    assert "Use `/compact` to compress conversation history and retry" not in text


def test_content_filter_notice_unchanged() -> None:
    text = make_content_filter_notice()
    assert "内容被过滤" in text
    # Content-filter recovery is rephrasing, never history compaction.
    assert "/compact" not in text
