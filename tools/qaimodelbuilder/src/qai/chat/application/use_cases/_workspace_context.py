# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Neutral workspace project-context (AGENTS.md / CLAUDE.md) helpers.

Both the **main agent** path (``streaming.py:_resolve_workspace_context_files``,
which publishes the result on ``extra["workspace_context_files"]`` for the
cloud :class:`RichSystemPromptBuilder`) and the **sub-agent** path
(``agent_tool.py:_iter_loop``, which inlines the blocks straight into its own
minimal system prompt) read the SAME workspace-root files with the SAME size
cap and best-effort semantics. Keeping the reader here (single source of
truth) prevents the two paths from drifting (mirrors why ``_agentic_kernel``
exists for the loop mechanics).

V2 enhancement — no V1 equivalent. ONLY cloud models receive these blocks;
the callers gate on ``model_hint`` before invoking these helpers. Cached with
mtime/size invalidation (State-Truth-First): the cache key embeds the file's
``st_mtime`` + ``st_size``, so the moment a file is edited (mtime/size change)
the next turn re-reads it from disk and the stale entry can never be served.
This keeps the rendered system prompt byte-stable across turns while the
underlying files are unchanged (so Anthropic prompt caching can hit), yet edits
still take effect immediately because the truth source remains the filesystem.
"""

from __future__ import annotations

from pathlib import Path

#: Workspace-root project-context files, in injection order (AGENTS.md first).
WORKSPACE_CONTEXT_FILENAMES: tuple[str, ...] = ("AGENTS.md", "CLAUDE.md")

#: ``extra`` key carrying the pre-resolved files as an ordered list of
#: ``(filename, content)`` tuples (main-agent path → cloud prompt builder).
WORKSPACE_CONTEXT_EXTRA_KEY: str = "workspace_context_files"

#: Per-file byte cap. Files larger than this have their *body* truncated to
#: this many bytes (a short notice is appended AFTER the cap, so the rendered
#: text may exceed the cap by that small fixed amount); the intent is to keep
#: a huge file from blowing up the context window, not a hard ceiling on the
#: final string.
#:
#: This project's AGENTS.md is ~82KB, so even the old 32KB cap already showed
#: only the front ~40% (a truncated remnant either way). Dropping to 20KB keeps
#: the front ~25% — which still carries ALL highest-priority constraints, since
#: they are concentrated at the top of AGENTS.md: the git file-replacement
#: prohibition (byte ~712), cross-platform/no-regression/work-method sections,
#: and the 核心规则 "fix-defects" iron rule (byte ~17147, inside 20KB). Only the
#: State-Truth-First iron law (byte ~23370) falls past 20KB, same as it would
#: past a 16KB cut — 20KB is chosen over 16KB precisely to keep 核心规则 in view.
#: A truncation notice tells the model the content was cut. This path is
#: CLOUD-only (local models use _build_local_system_prompt and do NOT inline
#: workspace files), so local behaviour is unaffected. Net: ~8K fewer tokens
#: sent per cloud round with no loss of the top-priority project constraints.
WORKSPACE_CONTEXT_FILE_MAX_BYTES: int = 20 * 1024

#: Content cache keyed by ``(str(path), st_mtime, st_size)``. When a file is
#: edited its mtime/size changes → a new key → a fresh read (State-Truth-First:
#: the truth source is the on-disk stat, not a process-lifetime assumption).
#: Before inserting a fresh entry we evict ONLY the stale-mtime keys for the
#: SAME path (those can never be hit again once the file changes) — we must NOT
#: clear the whole cache, or a second file read in the same turn (AGENTS.md then
#: CLAUDE.md) would evict the first file's entry and both would miss every turn.
_WORKSPACE_CONTEXT_CACHE: dict[tuple[str, float, int], str | None] = {}


def read_workspace_context_file(root: Path, filename: str) -> str | None:
    """Read one project-context file (UTF-8, size-capped); best-effort.

    Returns the (possibly truncated) text, or ``None`` when the file is
    absent, empty, undecodable, or any I/O error occurs.

    The resolved (non-empty) text is cached under an ``(path, mtime, size)``
    key so unchanged files return byte-identical content across turns without
    re-reading/re-decoding, while an edit (mtime/size change) invalidates the
    entry and forces a fresh read on the next turn. Best-effort ``None``
    outcomes (missing/empty/undecodable) are intentionally NOT cached — they
    are cheap to recompute and caching them would need an extra
    file-recreation-detection path for little gain.
    """
    try:
        path = root / filename
        if not path.is_file():
            return None
        st = path.stat()
    except (OSError, ValueError):
        return None
    cache_key = (str(path), st.st_mtime, st.st_size)
    if cache_key in _WORKSPACE_CONTEXT_CACHE:
        return _WORKSPACE_CONTEXT_CACHE[cache_key]
    try:
        raw = path.read_bytes()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    truncated = False
    if len(raw) > WORKSPACE_CONTEXT_FILE_MAX_BYTES:
        raw = raw[:WORKSPACE_CONTEXT_FILE_MAX_BYTES]
        truncated = True
    try:
        # ``errors="ignore"`` keeps a stray bad byte (e.g. from a hard
        # byte-cap cut mid-character) from dropping the whole file.
        text = raw.decode("utf-8", errors="ignore")
    except (UnicodeDecodeError, ValueError):
        return None
    text = text.strip()
    if not text:
        return None
    if truncated:
        text += (
            f"\n\n[... {filename} 内容超过 "
            f"{WORKSPACE_CONTEXT_FILE_MAX_BYTES // 1024}KB，已截断 ...]"
        )
    # Evict stale-mtime keys for THIS path only (they can never be hit again),
    # then memoise. Do NOT clear the whole cache — a sibling file cached earlier
    # in the same turn (e.g. AGENTS.md before CLAUDE.md) must survive.
    path_str = str(path)
    for stale_key in [k for k in _WORKSPACE_CONTEXT_CACHE if k[0] == path_str]:
        del _WORKSPACE_CONTEXT_CACHE[stale_key]
    _WORKSPACE_CONTEXT_CACHE[cache_key] = text
    return text


def resolve_workspace_context_files(root: Path) -> list[tuple[str, str]]:
    """Read every :data:`WORKSPACE_CONTEXT_FILENAMES` under ``root``.

    Returns an ordered list of ``(filename, content)`` for the files that
    exist and have content (best-effort; missing/empty/unreadable files are
    simply omitted). Empty list when none are present.
    """
    resolved: list[tuple[str, str]] = []
    for filename in WORKSPACE_CONTEXT_FILENAMES:
        content = read_workspace_context_file(root, filename)
        if content:
            resolved.append((filename, content))
    return resolved


def render_workspace_context_block(filename: str, content: str) -> str:
    """Render one project-context file as a system-prompt block.

    Used by the sub-agent path (the main-agent path renders via the cloud
    :class:`RichSystemPromptBuilder`, which keeps an equivalent renderer to
    avoid an adapter→application import in the reverse direction). Returns an
    empty string when there is no content.
    """
    body = (content or "").strip()
    if not body:
        return ""
    return (
        f"## 项目约定（{filename}）\n"
        f"以下是当前工作区根目录 {filename} 的内容，"
        "请在本次会话中遵循其中的项目级约定/指引：\n\n"
        f'<project_context file="{filename}">\n'
        f"{body}\n"
        "</project_context>"
    )


__all__ = [
    "WORKSPACE_CONTEXT_FILENAMES",
    "WORKSPACE_CONTEXT_EXTRA_KEY",
    "WORKSPACE_CONTEXT_FILE_MAX_BYTES",
    "read_workspace_context_file",
    "resolve_workspace_context_files",
    "render_workspace_context_block",
]
