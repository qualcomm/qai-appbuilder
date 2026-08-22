# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""File snapshot tags shared by ``read`` and ``edit``.

Why this exists
---------------
``read`` and ``edit`` are two separate tool calls. Between them the file can
change under the agent's feet — a sibling agent editing the same module, the
user saving in an editor, a formatter/codegen step, a git operation. The
7-layer fuzzy matcher in :mod:`._edit_match` then happily lands ``oldText`` on
whatever the file says NOW: the edit "succeeds" while silently overwriting the
other writer's work, and nothing in the result tells the model that the file it
reasoned about no longer exists.

A snapshot tag closes that window WITHOUT a stateful lock: ``read`` hands back
a short digest of the file's exact bytes, and ``edit`` optionally re-derives it
and refuses to write when it differs. Optimistic concurrency control, one
opaque string wide.

Design notes
------------
* **Raw bytes, never decoded text.** The digest covers the file's bytes as they
  are on disk, so it is stable for CRLF and LF files alike (two consecutive
  reads of the same file always agree) and for any encoding / non-ASCII
  content. Decoding first would be wrong twice over: universal-newline
  translation makes a CRLF file hash like its LF twin (an EOL-only rewrite
  would go undetected), and ``errors="replace"`` would collapse distinct
  invalid byte sequences onto the same tag.
* **Whole file, never the slice.** ``read`` returns a window; the tag must
  describe the FILE. Hashing the returned slice would give a different tag per
  ``offset``/``limit``/``ranges`` request and make the tag meaningless as an
  identity for the file.
* **Truncated digest.** Only the first :data:`_TAG_HEX` hex characters of the
  sha256 are exposed: 48 bits is far more than enough to catch an accidental
  concurrent edit, it is not invertible, and the full digest (a
  content-identifying value that the model has no use for) never leaves this
  module.
* **Bounded cost.** Files up to :data:`SNAPSHOT_MAX_BYTES` are digested with a
  chunked sha256 (O(1) memory, ~1 GB/s), and ``edit`` reuses the bytes it has
  already read instead of touching the disk a second time.

Weak (degraded) tags — READ THIS BEFORE TRUSTING A TAG
------------------------------------------------------
Above :data:`SNAPSHOT_MAX_BYTES` a full digest stops being cheap, so the tag
degrades to ``weak-<hex>`` derived from ``(size, mtime_ns)``. The degradation is
NEVER silent: the ``weak-`` prefix is part of the value the model sees, and both
tool schemas spell out what it means.

Weak-tag semantics, precisely:

* A CHANGED weak tag still proves the file changed (no false alarms).
* An UNCHANGED weak tag does NOT prove the file is untouched: an in-place
  rewrite that keeps the byte count and lands inside the filesystem's mtime
  granularity is invisible to it (false negative). On a huge file that is a
  narrow window, but it is real — a weak tag is a cheap guard, not a proof.
* Tag kinds never compare equal across the threshold: if a file crosses
  :data:`SNAPSHOT_MAX_BYTES` between ``read`` and ``edit``, its size changed by
  definition, so the mismatch is a true positive.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from qai.ai_coding.infrastructure.tools.errors import ToolError
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    get_conversation_scope,
)

#: Hex characters of the sha256 exposed as the tag. 12 hex = 48 bits: a
#: collision between two *accidental* concurrent versions of one file is not a
#: practical concern, and the full digest is never published.
_TAG_HEX = 12

#: Read size in the chunked digest pass — one page-friendly block, so memory
#: stays O(1) no matter how large the file is.
_CHUNK_BYTES = 256 * 1024

#: Above this size the strong digest is replaced by a ``weak-`` (size+mtime)
#: tag. 8 MiB of sha256 costs single-digit milliseconds; past that the hash
#: would start to dominate the cost of a windowed read of a file that big.
#: Raising this trades read latency for detection strength.
SNAPSHOT_MAX_BYTES = 8 * 1024 * 1024

#: Prefix marking a degraded tag. Part of the public value on purpose.
WEAK_PREFIX = "weak-"


def is_weak(tag: str) -> bool:
    """True when *tag* is a degraded (size+mtime) snapshot, not a digest."""
    return tag.startswith(WEAK_PREFIX)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:_TAG_HEX]


def _weak_tag(path: Path) -> str | None:
    """``weak-<hex>`` over ``(size, mtime_ns)``; ``None`` if ``stat`` fails."""
    try:
        st = path.stat()
    except OSError:
        return None
    stamp = f"{st.st_size}:{st.st_mtime_ns}".encode("utf-8")
    return WEAK_PREFIX + _digest(stamp)


def compute_snapshot(path: Path, *, data: bytes | None = None) -> str | None:
    """Return the snapshot tag for *path*, or ``None`` when it cannot be taken.

    Pass ``data`` when the caller already holds the file's COMPLETE raw bytes
    (``edit`` does): the digest is taken from them, so the file is not read
    twice. ``data`` must be the whole file — a slice would produce a tag that
    no other reader can reproduce.

    Fail-open by construction: a vanished / unreadable file yields ``None`` so
    a snapshot never turns into a second failure mode for a read that
    otherwise worked. Verification treats a missing tag as "cannot confirm"
    and is handled by :func:`verify_snapshot`.
    """
    if data is not None:
        if len(data) <= SNAPSHOT_MAX_BYTES:
            return _digest(data)
        return _weak_tag(path)

    try:
        size = path.stat().st_size
    except OSError:
        return None
    if size > SNAPSHOT_MAX_BYTES:
        return _weak_tag(path)

    hasher = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(_CHUNK_BYTES)
                if not chunk:
                    break
                hasher.update(chunk)
    except OSError:
        return None
    return hasher.hexdigest()[:_TAG_HEX]


# ---------------------------------------------------------------------------
# Conversation-bound store — the tag the model NEVER has to carry
# ---------------------------------------------------------------------------
# Returning a tag and asking the model to pass it back makes the race guard
# OPT-IN, and an opt-in guard is not a guard: measured across four test rounds,
# ``read`` printed the tag in its message and the model still called ``edit``
# without it, so nothing was ever checked. Recording what each reader saw —
# keyed by conversation and canonical path — lets ``edit`` verify with no
# cooperation from the model at all.
#
# Keyed by conversation so two chats editing the same file cannot invalidate
# each other, and so the entries age out with the conversation instead of
# growing for the life of the process.

#: ``{conversation_id: {canonical_path: tag}}``.
_seen: dict[str, dict[str, str]] = {}

#: Per-conversation ceiling. A long session touches many files; without a cap
#: this map is an unbounded leak. Oldest entries drop first (dicts preserve
#: insertion order), and a dropped entry degrades to "unknown" — never to a
#: false match.
_MAX_TRACKED_FILES = 512


def canonical_key(path: Path) -> str:
    """Stable identity for *path*, case-folded on case-insensitive filesystems.

    ``C:\\A\\b.py`` and ``c:\\a\\B.PY`` are ONE file on Windows; keying by the
    raw string would let a differently-cased read hide a stale edit.
    """
    try:
        resolved = str(path.resolve())
    except OSError:
        resolved = str(path)
    return os.path.normcase(os.path.normpath(resolved))


def record_seen(path: Path, tag: str | None) -> None:
    """Remember the tag a reader observed for *path*. Never raises."""
    if not tag:
        return
    scope = get_conversation_scope()
    if not scope:
        # No conversation bound (a bare unit test, a scheduled job): tracking
        # would have no owner and no lifetime, so stay out of the way.
        return
    bucket = _seen.setdefault(scope, {})
    key = canonical_key(path)
    bucket.pop(key, None)  # re-insert so the newest key is last
    bucket[key] = tag
    while len(bucket) > _MAX_TRACKED_FILES:
        bucket.pop(next(iter(bucket)))


def get_seen(path: Path) -> str | None:
    """The tag last observed for *path* in this conversation, if any."""
    scope = get_conversation_scope()
    if not scope:
        return None
    return _seen.get(scope, {}).get(canonical_key(path))


def discard_conversation(conversation_id: str) -> None:
    """Release everything tracked for a finished conversation."""
    _seen.pop(conversation_id, None)


def verify_snapshot(
    path: Path,
    expected: str,
    *,
    data: bytes | None = None,
    tool: str = "edit",
    from_caller: bool = True,
) -> None:
    """Raise :class:`ToolError` unless *path* still matches *expected*.

    Called BEFORE any matching or writing, so a stale tag aborts the whole
    operation with the file untouched.

    The message names the actual situation ("someone else changed this file,
    read it again") rather than a generic validation failure: the model's only
    correct recovery is to re-read and rebuild its ``oldText`` against the new
    content, and a vague error sends it retrying the same doomed edit instead.
    """
    if not isinstance(expected, str) or not expected.strip():
        raise ToolError(
            f"{tool}: 'expect_snapshot' must be the non-empty snapshot string "
            f"returned by a previous read of this file (omit it entirely to "
            f"skip the staleness check)."
        )
    expected = expected.strip()
    current = compute_snapshot(path, data=data)
    if current is None:
        raise ToolError(
            f"{tool}: cannot verify 'expect_snapshot' for {path} — the file "
            f"could not be read to re-derive its snapshot. Refusing to write "
            f"a file whose current state is unknown; read the file again."
        )
    if current == expected:
        return

    weak_note = ""
    if is_weak(expected) or is_weak(current):
        weak_note = (
            " (this file is large enough that snapshots are the degraded "
            "size+mtime kind — see the 'snapshot' field docs)"
        )
    # The expectation can come from the caller OR from this conversation's own
    # read history. Saying "you passed expect_snapshot=..." in the second case
    # sends the model hunting for an argument it never sent, so the wording
    # follows the actual source.
    origin = (
        f"but you passed expect_snapshot={expected!r}"
        if from_caller
        else f"but this conversation last read it as {expected!r}"
    )
    raise ToolError(
        f"{tool}: file has been modified by someone else since you read it — "
        f"{path} now has snapshot {current!r}, {origin}{weak_note}. NOTHING "
        f"was written. Your copy of this file is stale, so an edit based on it "
        f"could silently overwrite the other writer's changes. Read the file "
        f"again, rebuild your oldText from the CURRENT content, and retry."
    )
