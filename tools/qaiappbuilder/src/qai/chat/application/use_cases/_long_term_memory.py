# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Long-term persona memory (``memory/*.md``) — global agent context.

The operator maintains a small set of hand-written Markdown files under the
application repo's ``memory/`` directory that form the agent's stable persona:

* ``SOUL.md``     — core values / behavioural boundaries (the "constitution").
* ``IDENTITY.md`` — name, persona, tone, emoji.
* ``USER.md``     — the human's preferences, background, habits.

These are the PERSONA layer of the memory design. Unlike the agent-editable
*experience* layer (``chat_experience`` table, surfaced on demand via the
``recall_experience`` tool), the persona files are stable and injected into
EVERY LLM call as top-priority background.

* ``SOUL.md`` and ``USER.md`` render into a single ``<long_term_memory>`` block
  appended to the assembled system prompt on all non-translate branches.
* ``IDENTITY.md`` is returned separately by :func:`resolve_persona_identity`:
  when it exists and has content it OVERRIDES the code-default identity intro;
  when absent/empty the builder keeps its default identity.

Caching mirrors :mod:`qai.chat.application.use_cases._workspace_context`: each
file is cached under an ``(path, mtime, size)`` key, so an unchanged file
returns byte-identical content across turns (upstream prompt-cache friendly)
while an edit invalidates the entry and forces a fresh read next turn — the
operator can tweak a memory file without restarting the process.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The persona file injected into the shared ``<long_term_memory>`` block, in
#: order. ``IDENTITY.md`` is NOT here — it is resolved separately as an identity
#: override (see :func:`resolve_persona_identity`).
MEMORY_BLOCK_FILENAMES: tuple[str, ...] = ("SOUL.md", "USER.md")

#: Identity file resolved on its own (override-or-default), not part of the
#: shared block.
IDENTITY_FILENAME: str = "IDENTITY.md"

#: ``extra`` keys carrying the pre-resolved persona content (use case → prompt
#: builder), mirroring ``WORKSPACE_CONTEXT_EXTRA_KEY``.
MEMORY_BLOCK_EXTRA_KEY: str = "long_term_memory_block"
PERSONA_IDENTITY_EXTRA_KEY: str = "persona_identity"

#: Per-file byte cap (persona files are hand-maintained prose, expected small).
#: A file larger than this has its body truncated to this many bytes with a
#: short notice appended, guarding the context window without a hard ceiling on
#: the final string. 16KB per file (three files → ≤48KB) keeps persona well
#: under ~2% of a 200K context.
MEMORY_FILE_MAX_BYTES: int = 16 * 1024

#: Absolute-path override for the ``memory/`` directory. When unset the
#: directory resolves to ``<repo>/memory`` so a packaged/relocated deployment
#: can point at the operator's memory folder without code changes.
MEMORY_DIR_ENV_VAR: str = "QAI_MEMORY_DIR"

#: Content cache keyed by ``(str(path), st_mtime, st_size)`` — same
#: State-Truth-First invalidation as the workspace-context cache. ``None``
#: (missing/empty/undecodable) is intentionally not cached.
_MEMORY_CACHE: dict[tuple[str, float, int], str | None] = {}


def _default_memory_dir() -> Path:
    """Return the default ``memory/`` directory (application repo root).

    This module lives at ``src/qai/chat/application/use_cases/`` so the repo
    root is ``parents[5]`` (use_cases → application → chat → qai → src → repo).
    """
    return Path(__file__).resolve().parents[5] / "memory"


def resolve_memory_dir() -> Path:
    """Resolve the memory directory, honouring the ``QAI_MEMORY_DIR`` override."""
    override = os.environ.get(MEMORY_DIR_ENV_VAR, "").strip()
    if override:
        return Path(override)
    return _default_memory_dir()


def read_memory_file(root: Path, filename: str) -> str | None:
    """Read one persona file (UTF-8, size-capped); best-effort.

    Returns the (possibly truncated) text, or ``None`` when the file is absent,
    empty, undecodable, or any I/O error occurs. Non-empty results are cached
    under an ``(path, mtime, size)`` key; ``None`` outcomes are not cached.
    """
    try:
        path = root / filename
        if not path.is_file():
            return None
        st = path.stat()
    except (OSError, ValueError):
        return None
    cache_key = (str(path), st.st_mtime, st.st_size)
    if cache_key in _MEMORY_CACHE:
        return _MEMORY_CACHE[cache_key]
    try:
        raw = path.read_bytes()
    except (OSError, ValueError):
        return None
    if not raw:
        return None
    truncated = False
    if len(raw) > MEMORY_FILE_MAX_BYTES:
        raw = raw[:MEMORY_FILE_MAX_BYTES]
        truncated = True
    try:
        # ``errors="ignore"`` keeps a stray bad byte (e.g. from a hard byte-cap
        # cut mid-character) from dropping the whole file.
        text = raw.decode("utf-8", errors="ignore")
    except (UnicodeDecodeError, ValueError):
        return None
    text = text.strip()
    if not text:
        return None
    if truncated:
        text += (
            f"\n\n[... {filename} 内容超过 "
            f"{MEMORY_FILE_MAX_BYTES // 1024}KB，已截断 ...]"
        )
    # Evict stale-mtime keys for THIS path only, then memoise (mirrors
    # _workspace_context: never clear the whole cache, or a sibling persona
    # file cached earlier in the same turn would miss).
    path_str = str(path)
    for stale_key in [k for k in _MEMORY_CACHE if k[0] == path_str]:
        del _MEMORY_CACHE[stale_key]
    _MEMORY_CACHE[cache_key] = text
    return text


def resolve_persona_identity() -> str | None:
    """Return ``IDENTITY.md`` content, or ``None`` when absent/empty.

    ``None`` signals the caller to keep the code-default identity intro; a
    non-empty string OVERRIDES it (the operator-defined agent identity).
    """
    return read_memory_file(resolve_memory_dir(), IDENTITY_FILENAME)


def render_long_term_memory_block() -> str:
    """Render SOUL.md + USER.md into one ``<long_term_memory>`` block.

    Files missing / empty / unreadable are omitted. Returns an empty string
    when NONE of the block files resolve, so callers can skip appending.
    ``IDENTITY.md`` is intentionally excluded (handled as an identity override).
    """
    root = resolve_memory_dir()
    sections: list[str] = []
    for filename in MEMORY_BLOCK_FILENAMES:
        content = read_memory_file(root, filename)
        if content:
            sections.append(f'<file name="{filename}">\n{content}\n</file>')
    if not sections:
        return ""
    body = "\n\n".join(sections)
    return (
        "## 长期记忆（Long-term Memory）\n"
        "以下是你的长期记忆与人格设定，在每一次对话与每一个子任务中都必须遵循，"
        "作为最高优先级的背景上下文：\n\n"
        "<long_term_memory>\n"
        f"{body}\n"
        "</long_term_memory>"
    )


def resolve_persona_memory() -> tuple[str, str | None]:
    """Resolve both persona artefacts in one pass.

    Returns ``(block, identity)`` where ``block`` is the rendered
    ``<long_term_memory>`` block ("" when no block files resolve) and
    ``identity`` is the IDENTITY.md override (``None`` to keep the default).
    """
    return render_long_term_memory_block(), resolve_persona_identity()


__all__ = [
    "MEMORY_BLOCK_FILENAMES",
    "IDENTITY_FILENAME",
    "MEMORY_BLOCK_EXTRA_KEY",
    "PERSONA_IDENTITY_EXTRA_KEY",
    "MEMORY_FILE_MAX_BYTES",
    "MEMORY_DIR_ENV_VAR",
    "resolve_memory_dir",
    "read_memory_file",
    "resolve_persona_identity",
    "render_long_term_memory_block",
    "resolve_persona_memory",
]
