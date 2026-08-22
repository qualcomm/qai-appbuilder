# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""``ast_edit`` tool handler — structural (AST) rewrite in two phases.

Backed by the ``ast-grep`` CLI, which parses each file with tree-sitter and
matches a PATTERN written in the target language's own syntax (``$NAME`` = one
node, ``$_`` = one unbound node, ``$$$NAME`` = zero or more nodes). That is the
right engine for a codemod whose textual form varies — formatting, line breaks,
argument spacing — while its STRUCTURE is fixed, and where a regex would either
miss matches or eat the wrong ones. Measured on a Python fixture containing
``legacy_call(1, 2)``, the same call split across four lines, plus the same text
inside a comment and inside a string literal: ``ast-grep -p 'legacy_call($$$A)'``
matches exactly the two real calls (including the multi-line one) and skips the
comment and the string; a text ``grep`` reports all four.

Two properties of this handler matter more than the matching.

**1. ``ast-grep -U`` is NEVER used.**
``ast-grep run -U`` rewrites files in place, bypassing this project's entire
write safety net (edit-trash backup → atomic temp-file write → read-back verify
→ rollback). So the CLI is only ever invoked WITHOUT ``-U``, purely as a
*reporter*: ``--json=stream`` makes it emit one JSON object per match carrying
``replacement`` (the rewritten text) and ``replacementOffsets`` (the exact byte
range that text replaces), while every file on disk stays untouched. This
handler then

  * parses those records into :class:`RewriteSpan` objects
    (:func:`parse_rewrite_stream`),
  * computes each file's new content IN MEMORY, applying spans back-to-front so
    no earlier edit invalidates a later offset, and rejecting OVERLAPPING spans
    instead of silently dropping one (:func:`apply_spans`),
  * re-reads every target and ABORTS if one no longer holds the scanned bytes
    (:func:`_verify_unchanged_on_disk`) — ``safe_commit_text`` trusts the
    ``original_text`` its caller passes and cannot detect this itself,
  * commits through :func:`qai.ai_coding.infrastructure.tools._safe_commit.
    safe_commit_text` — the same pipeline ``edit`` and ``write`` use.

Offsets are BYTE offsets, and this is not a detail that can be finessed:
ast-grep's ``Content for String`` implementation uses ``Underlying = u8``. On a
measured CRLF file whose first line is non-ASCII, a match reported at
``[43, 60)`` is the intended ``legacy_call(1, 2)`` when sliced out of the raw
BYTES and the meaningless ``'\ny = 3\r\n'`` when sliced out of the decoded
``str``. Spans are therefore applied to bytes, never to decoded text.

**2. Preview before commit (``apply``, default ``false``).**
A structural rewrite matches by shape, so its blast radius is much harder to
predict than a literal ``edit``: one pattern can hit dozens of files. The
default therefore only REPORTS what would change and writes nothing at all;
``apply=true`` is the explicit second step that commits.

Line endings follow the ``edit`` convention exactly: spans are applied to the
raw bytes (so CRLF offsets stay correct), then both sides are newline-normalised
and handed to ``safe_commit_text`` along with the file's detected ending, which
``restore_line_ending`` puts back on write. A CRLF file is committed as CRLF.

Multi-file atomicity: files are committed one at a time, each through the full
safety net, while the pre-write bytes of every already-committed file are kept
in memory. If a later file fails, the committed ones are restored in reverse
order via :func:`atomic_write_bytes` + :func:`verify_after_write` — the same
primitives and the same reverse-order rollback shape ``apply_patch`` uses in its
``_rollback``. A rollback that itself fails is reported, never swallowed.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qai.ai_coding.application.ports import FileGuardPort, ToolResultStorePort
from qai.ai_coding.infrastructure.tools._safe_commit import (
    SafeWriteError,
    atomic_write_bytes,
    safe_commit_text,
    verify_after_write,
)
from qai.ai_coding.infrastructure.tools.errors import ToolError, ToolGuardDenied
from qai.ai_coding.infrastructure.tools.handlers._ast_grep_binary import (
    missing_binary_message,
    resolve_ast_grep_binary,
)
from qai.ai_coding.infrastructure.tools.handlers._edit_match import (
    restore_line_ending,
)
from qai.ai_coding.infrastructure.tools.handlers._file_snapshot import (
    compute_snapshot,
    get_seen,
    record_seen,
)
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    _ok,
    default_cwd,
    get_tool_output_thresholds,
    get_workspace_base,
    logger,
    resolve_under_workspace,
)
from qai.ai_coding.infrastructure.tools.handlers.search import (
    _split_targets,
    _terminate_process_tree_shielded,
)
from qai.platform import protected_paths
from qai.platform.scheduling.path_locks import PathLockManager

__all__ = [
    "RewriteSpan",
    "FileRewrite",
    "parse_rewrite_stream",
    "apply_spans",
    "build_file_rewrite",
    "commit_rewrites",
    "tool_ast_edit",
]

#: Seconds the ``ast-grep`` dry-run may take before it is killed. A structural
#: scan is fast (the fixture probe returns in well under a second), so a
#: minute-scale ceiling only ever trips on a pathological pattern or tree.
AST_GREP_TIMEOUT_SECONDS = 120.0

#: Hard ceiling on captured ``--json=stream`` stdout. Exceeding it is an ERROR,
#: never a truncation: a half-read JSON stream would silently DROP rewrites, and
#: committing a partial codemod is far worse than committing none.
AST_GREP_MAX_STDOUT_BYTES = 32 * 1024 * 1024

#: Preview caps. The full change list is persisted through the result store when
#: either trips, so an elided change always stays reachable via ``read``.
MAX_PREVIEW_SPANS_PER_FILE = 10
MAX_PREVIEW_FILES = 50

#: Max characters of a single before/after snippet kept in the preview.
MAX_SNIPPET_CHARS = 240


# ---------------------------------------------------------------------------
# Pure data model + pure transforms.
#
# These take ast-grep's JSON text / a file's raw bytes and return new bytes.
# They spawn nothing and need no binary, so the whole risky part of this tool —
# offset arithmetic, overlap detection, CRLF and non-ASCII fidelity — is
# directly unit-testable with constructed input.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RewriteSpan:
    """One replacement: ``[start, end)`` BYTE range → ``replacement``.

    ``start`` / ``end`` index the file's RAW bytes on disk (ast-grep reports
    byte offsets). ``matched_text`` and ``line`` are carried for the preview
    only and never take part in the rewrite arithmetic.
    """

    start: int
    end: int
    replacement: str
    matched_text: str = ""
    line: int = 0


@dataclass(frozen=True, slots=True)
class FileRewrite:
    """A single file's fully-computed rewrite, pre-commit.

    Holds BOTH representations on purpose: ``original_bytes`` / ``new_bytes``
    are the byte-exact truth used for the commit decision and for rollback,
    while ``original_text`` / ``new_text`` are newline-NORMALISED and pair with
    ``line_ending`` so ``safe_commit_text`` restores the file's own ending —
    the same contract ``tool_edit`` uses.
    """

    path: Path
    original_bytes: bytes
    new_bytes: bytes
    original_text: str
    new_text: str
    line_ending: str
    spans: tuple[RewriteSpan, ...]


def _require_offset(record_json: str, container: Any, key: str) -> int:
    """Read a non-negative int ``key`` out of an offset object, or fail loudly.

    A missing / malformed offset must never be guessed or defaulted: the value
    is about to select the byte range we overwrite, so a wrong one corrupts the
    file rather than merely mis-reporting.
    """
    if not isinstance(container, dict) or key not in container:
        raise ToolError(
            "ast_edit: ast-grep emitted a match without a usable "
            f"'{key}' offset ({record_json[:300]}). Refusing to guess the "
            "range — a wrong offset would corrupt the file."
        )
    value = container[key]
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ToolError(
            f"ast_edit: ast-grep emitted a non-integer '{key}' offset "
            f"({value!r}). Refusing to guess the range."
        )
    return value


def parse_rewrite_stream(stdout: str) -> dict[str, list[RewriteSpan]]:
    """Parse ``ast-grep run --json=stream -r ...`` stdout into per-file spans.

    ``--json=stream`` emits ONE JSON object per line. With ``-r`` each object
    carries ``replacement`` (the rewritten text for that match) plus
    ``replacementOffsets`` — the authoritative ``{"start","end"}`` byte range
    the replacement covers. That range can differ from ``range.byteOffset``
    (a fixer may widen it), so ``replacementOffsets`` is REQUIRED and is never
    substituted with ``range.byteOffset``: applying a replacement to a range it
    was not computed for would overwrite the wrong bytes.

    Files are keyed by ast-grep's own ``file`` string in first-seen order (dicts
    preserve insertion order), so the report follows the order the tree was
    walked. Callers must coalesce those keys by RESOLVED path — overlapping
    targets make the same node appear more than once.

    Every failure here is loud. Unparseable JSON, a missing ``file``, or a match
    with NO ``replacement`` all raise: each would mean acting on a partially
    understood change set, and the resulting report would claim a rewrite that
    is not the one that would land.
    """
    per_file: dict[str, list[RewriteSpan]] = {}
    for line_no, line in enumerate(stdout.splitlines(), start=1):
        text = line.strip()
        if not text:
            continue
        try:
            record = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolError(
                f"ast_edit: could not parse ast-grep JSON output at line "
                f"{line_no}: {exc}. Refusing to act on a partially understood "
                "match stream; nothing was written."
            ) from exc
        if not isinstance(record, dict):
            raise ToolError(
                f"ast_edit: ast-grep JSON line {line_no} is not an object "
                f"({type(record).__name__})."
            )

        file_str = record.get("file")
        if not isinstance(file_str, str) or not file_str:
            raise ToolError(
                f"ast_edit: ast-grep JSON line {line_no} has no 'file' field."
            )

        replacement = record.get("replacement")
        if not isinstance(replacement, str):
            raise ToolError(
                "ast_edit: ast-grep reported a match with no 'replacement' in "
                f"{file_str} (output line {line_no}). The rewrite template "
                "produced nothing for that node; refusing to report a change "
                "that would not happen."
            )

        # ``replacementOffsets`` is the range the replacement actually covers.
        # There is deliberately NO fallback to ``range.byteOffset``: a fixer
        # may widen the replacement past the matched node, so the two can
        # differ, and substituting one for the other would overwrite a byte
        # range the rewrite was never computed for. Guessing here is exactly
        # what ``_require_offset`` refuses to do.
        offsets = record.get("replacementOffsets")
        start = _require_offset(text, offsets, "start")
        end = _require_offset(text, offsets, "end")
        if end < start:
            raise ToolError(
                f"ast_edit: ast-grep reported an inverted range "
                f"[{start}, {end}) for {file_str}."
            )

        node_range = record.get("range")
        line_1based = 0
        if isinstance(node_range, dict):
            start_pos = node_range.get("start")
            if isinstance(start_pos, dict) and isinstance(
                start_pos.get("line"), int
            ):
                # ast-grep positions are ZERO-based; this tool family reports
                # 1-based lines everywhere.
                line_1based = int(start_pos["line"]) + 1

        matched = record.get("text")
        per_file.setdefault(file_str, []).append(
            RewriteSpan(
                start=start,
                end=end,
                replacement=replacement,
                matched_text=matched if isinstance(matched, str) else "",
                line=line_1based,
            )
        )
    return per_file


def apply_spans(original: bytes, spans: list[RewriteSpan]) -> bytes:
    """Apply ``spans`` to ``original`` bytes and return the new content.

    Three invariants make this safe:

    #. **Back-to-front application.** Spans are sorted by ``start`` and applied
       in REVERSE, so every replacement lands at an offset no preceding
       replacement has shifted. (Front-to-back would need a running delta and
       mislands silently the moment one is forgotten.)
    #. **Overlaps are an ERROR, not a drop.** Two spans covering the same bytes
       cannot both apply, and picking one would silently discard a rewrite the
       caller was told had happened. Both ranges are named in the error so the
       pattern can be narrowed.
    #. **Out-of-range is an ERROR, not a clamp.** A span past EOF means the file
       changed after it was scanned; clamping would write a mangled file.

    Operates purely on bytes, so a non-ASCII prefix cannot shift a later offset.
    """
    if not spans:
        return original

    ordered = sorted(spans, key=lambda s: (s.start, s.end))
    for index in range(1, len(ordered)):
        prev = ordered[index - 1]
        cur = ordered[index]
        if cur.start < prev.end:
            raise ToolError(
                "ast_edit: two rewrites OVERLAP on the same bytes — "
                f"[{prev.start}, {prev.end}) and [{cur.start}, {cur.end}). "
                "Applying both is impossible and dropping one would silently "
                "lose a change, so NOTHING was written. Narrow the pattern so "
                "matches cannot nest (a pattern matching both an outer and an "
                "inner node is the usual cause)."
            )

    limit = len(original)
    for span in ordered:
        if span.end > limit:
            raise ToolError(
                f"ast_edit: rewrite range [{span.start}, {span.end}) is past "
                f"the end of the file ({limit} bytes) — the file changed after "
                "it was scanned. Nothing was written; re-run ast_edit."
            )

    out = original
    for span in reversed(ordered):
        out = (
            out[: span.start]
            + span.replacement.encode("utf-8")
            + out[span.end :]
        )
    return out


def build_file_rewrite(
    path: Path, original_bytes: bytes, spans: list[RewriteSpan]
) -> FileRewrite:
    """Compute one file's :class:`FileRewrite` from its bytes + spans.

    Spans are applied to the RAW bytes — correct for a CRLF file and for
    non-ASCII content, where a character index does not equal a byte offset.

    The result is then committed BYTE-EXACTLY: the text handed to
    ``safe_commit_text`` is the undecorated decode of those bytes and
    ``line_ending`` is ``"\\n"``, which makes its ``restore_line_ending`` a
    no-op. ``apply_spans`` has already produced the exact intended content, so
    a newline round-trip can only corrupt it — and did, in two ways:

    * A MIXED-ending file (some ``\\r\\n``, some ``\\n``) is judged CRLF by
      ``detect_line_ending`` as soon as ONE ``\\r\\n`` occurs, so restoring
      rewrote EVERY ``\\n`` and a one-identifier rename silently reflowed the
      whole file. Worse, ``safe_commit_text`` derives its ``.edit_trash``
      backup from ``original_text`` the same way, so the "original" on record
      was not the original either — the backup could not undo the damage.
    * A file delimited by lone ``\\r`` was normalised to ``\\n`` throughout,
      and a ``\\r`` INSIDE a string literal — content, not a line ending — was
      rewritten with it.

    Committing the bytes verbatim keeps every ending exactly as it was found:
    CRLF files stay CRLF because their ``\\r\\n`` sit in ``new_bytes``, not
    because a normalise/restore pass puts them back.
    """
    try:
        original_text_raw = original_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(f"ast_edit: {path} is not valid UTF-8: {exc}") from exc

    new_bytes = apply_spans(original_bytes, spans)
    try:
        new_text_raw = new_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ToolError(
            f"ast_edit: the rewrite of {path} produced invalid UTF-8: {exc}. "
            "Nothing was written."
        ) from exc

    return FileRewrite(
        path=path,
        original_bytes=original_bytes,
        new_bytes=new_bytes,
        original_text=original_text_raw,
        new_text=new_text_raw,
        line_ending="\n",
        spans=tuple(sorted(spans, key=lambda s: (s.start, s.end))),
    )


# ---------------------------------------------------------------------------
# Commit — every byte goes through the shared safety net
# ---------------------------------------------------------------------------


def _workspace_root_for_ast_edit(path: Path) -> Path:
    """Best-effort workspace root used to lay out the edit-trash tree.

    Prefers the per-request workspace base; falls back to the rewritten FILE's
    own parent (never the daemon CWD) so ``resolve_trash_root`` places the
    backup at ``<workspace>/.edit_trash`` — co-located with the file it
    protects. Same shape as ``read_write._workspace_root_for`` and
    ``patch._workspace_root_for_patch``.
    """
    base = get_workspace_base()
    if base:
        return Path(base)
    return path.parent


def _rollback(committed: list[FileRewrite]) -> list[Path]:
    """Restore already-committed files in REVERSE order.

    Returns the paths whose restore FAILED — surfaced to the caller rather than
    swallowed (State-Truth-First), because those are exactly the files an
    operator must recover from ``.edit_trash/`` by hand. Each restore writes the
    precise pre-rewrite BYTES through :func:`atomic_write_bytes` (so the
    rollback itself cannot half-write) and verifies them.
    """
    failed: list[Path] = []
    for entry in reversed(committed):
        try:
            atomic_write_bytes(entry.path, entry.original_bytes)
            verify_after_write(entry.path, entry.original_bytes)
        except Exception as exc:  # noqa: BLE001 — surface, do not swallow
            logger.error(
                "ast_edit rollback FAILED path=%s err=%s — recover the "
                "original from the workspace's .edit_trash/",
                entry.path,
                exc,
            )
            failed.append(entry.path)
    return failed


def _verify_unchanged_on_disk(rewrites: list[FileRewrite]) -> None:
    """Fail unless each span still covers the exact code ast-grep matched.

    The scan and the commit are separate passes over the disk, and the spans
    from the first are byte offsets into the content it saw. If a sibling
    agent, the user's editor or a formatter rewrites a file in between, those
    offsets address different code — and ``safe_commit_text`` cannot catch it,
    because it takes ``original_text`` from its CALLER and never compares it
    against the file. Committing would overwrite the other writer's work with
    content derived from stale offsets AND put the stale copy in
    ``.edit_trash``, so the backup could not recover what was lost either.

    Two checks, because they close DIFFERENT windows:

    #. ``matched_text`` vs the bytes each span currently covers. This is the
       load-bearing one. ``original_bytes`` is read AFTER the ast-grep child
       has already exited, so a write landing while it scanned is baked into
       it: comparing the file against ``original_bytes`` alone compares stale
       data with itself and PASSES, then commits shredded source (a 5-byte
       insertion earlier in the file turned ``legacy_call(1)`` into
       ``import os,new_call(1)ll(1)``). ``matched_text`` comes from the scan
       itself, so it is the only anchor that can detect this.
    #. Whole-file equality against ``original_bytes``, which additionally
       catches edits OUTSIDE every span — those leave the spans intact but
       still mean the committed content would revert someone else's work,
       since the new text is built from the bytes we read.

    ``apply_spans``' past-EOF check is no substitute for either: it only fires
    when the file SHRANK below a span's end. An equal-length or longer
    replacement slips past it.

    Checked for EVERY file before the first write, so a stale scan aborts the
    whole codemod instead of landing it partially.
    """
    for entry in rewrites:
        try:
            # ``_read_bytes`` already converts OSError into ToolError, so THAT
            # is what has to be caught here: an ``except OSError`` never fires
            # and the bare "cannot read" escapes without stating that nothing
            # was written — the one fact the caller needs. A target that has
            # become unreadable (deleted, replaced by a directory, locked) is
            # itself a form of "changed since the scan".
            current = _read_bytes(entry.path)
        except ToolError as exc:
            raise ToolError(
                f"ast_edit: {entry.path} could not be re-read before writing "
                f"({exc}) — it changed since ast_edit scanned it. NOTHING was "
                "written; re-run ast_edit."
            ) from exc
        for span in entry.spans:
            if not span.matched_text:
                continue
            covered = current[span.start : span.end].decode("utf-8", "replace")
            if covered != span.matched_text:
                raise ToolError(
                    f"ast_edit: in {entry.path} the match at bytes "
                    f"[{span.start}, {span.end}) no longer holds the code "
                    f"ast-grep matched there ({span.matched_text!r}; the file "
                    f"now has {covered!r}) — it was written to while ast_edit "
                    "was scanning, so applying these offsets would corrupt "
                    "the source. NOTHING was written; re-run ast_edit."
                )
        if current != entry.original_bytes:
            raise ToolError(
                f"ast_edit: {entry.path} changed on disk after ast_edit "
                "scanned it (someone else wrote to it in between), so the "
                "rewrite would revert their change. NOTHING was written — "
                "re-run ast_edit to rescan the current content."
            )


def commit_rewrites(
    rewrites: list[FileRewrite], *, pattern: str, rewrite_template: str
) -> None:
    """Commit every rewrite through ``safe_commit_text``, all-or-nothing.

    Per file the full safety net runs: symlink / special-file / read-only
    rejection, edit-trash backup of the ORIGINAL bytes, atomic temp-file write
    plus ``os.replace``, and a byte-exact read-back verify with per-file
    rollback. ``ast-grep`` never touches a file itself.

    Before the first write, :func:`_verify_unchanged_on_disk` confirms every
    target still holds the scanned bytes, so a file changed between scan and
    commit aborts the codemod instead of being clobbered from stale offsets.

    Across files, each committed file's pre-write bytes are retained so a later
    failure rolls the earlier ones back (reverse order) BEFORE the error is
    raised — a multi-file codemod therefore never lands half-applied.
    """
    _verify_unchanged_on_disk(rewrites)
    committed: list[FileRewrite] = []
    for entry in rewrites:
        try:
            safe_commit_text(
                path=entry.path,
                new_text=entry.new_text,
                original_text=entry.original_text,
                line_ending=entry.line_ending,
                workspace_root=_workspace_root_for_ast_edit(entry.path),
                tool="ast_edit",
                edits=len(entry.spans),
                meta={
                    "mode": "ast_edit",
                    "pattern": pattern,
                    "rewrite": rewrite_template,
                    "spans": len(entry.spans),
                },
                restore_line_ending=restore_line_ending,
            )
        except Exception as exc:  # noqa: BLE001 — roll back, then report
            failed = _rollback(committed)
            detail = f"{type(exc).__name__}: {exc}"
            if failed:
                names = ", ".join(str(p) for p in failed)
                raise ToolError(
                    f"ast_edit: writing {entry.path} failed ({detail}) and "
                    "rolling back the earlier files did NOT fully succeed. "
                    "Restore these manually from the workspace's "
                    f".edit_trash/: {names}"
                ) from exc
            raise ToolError(
                f"ast_edit: writing {entry.path} failed ({detail}); the "
                f"{len(committed)} file(s) already written were rolled back, "
                "so no file was left modified."
            ) from exc
        committed.append(entry)


# ---------------------------------------------------------------------------
# ast-grep invocation — dry-run ONLY, never ``-U``
# ---------------------------------------------------------------------------


# ``_split_targets`` and ``_terminate_process_tree_shielded`` are imported from
# the search track rather than re-implemented: both call sites spawn the same
# binary, and a private copy is how the two drift apart.

async def _run_ast_grep_dry(
    binary: str,
    *,
    pattern: str,
    rewrite_template: str,
    lang: str | None,
    targets: list[Path],
    cwd: str,
) -> tuple[str, str]:
    """Run ``ast-grep`` as a REPORTER; return ``(stdout, stderr)``.

    Note the argv: ``-U`` / ``--update-all`` is absent BY CONSTRUCTION. Without
    it ast-grep prints the rewrite it *would* make and leaves every file
    untouched — precisely the dry-run this handler needs, and the only mode it
    ever uses.

    Oversized stdout is an ERROR, never a truncation: a clipped JSON stream
    would drop rewrites without saying so. Note that
    :data:`AST_GREP_MAX_STDOUT_BYTES` is enforced AFTER ``communicate()`` has
    read the whole stream, so it bounds what this function will ACT ON, not
    what it buffers. That is deliberate — the alternative, incremental reads
    that stop at the ceiling, can only stop mid-object and so cannot tell a
    truncated stream from a complete one, which is precisely the failure this
    check exists to prevent. The search track can stop early because a partial
    read there costs a missing match; here it would cost a wrong rewrite.

    ``stderr`` is returned rather than only logged because ast-grep reports a
    MALFORMED pattern there while still exiting 0 with zero matches (measured:
    an unbalanced ``legacy_call($$$ARGS`` prints "Pattern contains an ERROR
    node" and exits 0). Without that text a typo'd pattern is indistinguishable
    from a genuine "this code does not occur" answer — the single most
    misleading result this tool could return — so the caller inspects it.
    """
    argv = [
        binary,
        "run",
        "--pattern",
        pattern,
        "--rewrite",
        rewrite_template,
        "--json=stream",
    ]
    if lang:
        argv.extend(["--lang", lang])
    argv.extend(str(t) for t in targets)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
    except OSError as exc:
        raise ToolError(
            f"ast_edit: could not launch ast-grep ({binary}): {exc}"
        ) from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=AST_GREP_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        await _terminate_process_tree_shielded(proc)
        raise ToolError(
            f"ast_edit: ast-grep did not finish within "
            f"{AST_GREP_TIMEOUT_SECONDS:.0f}s and was killed. No file was "
            "touched (the scan runs without --update-all). Narrow 'paths' or "
            "simplify the pattern."
        ) from exc
    except asyncio.CancelledError:
        # Shielded, and reaps the whole tree: a bare kill without an await
        # leaves the child unreaped (a zombie, plus "Event loop is closed"
        # noise at shutdown), and cancellation is exactly when the loop is
        # about to stop caring. Same helper the search track uses, so the two
        # ast-grep call sites cannot drift apart.
        await _terminate_process_tree_shielded(proc)
        raise

    if len(stdout_b) > AST_GREP_MAX_STDOUT_BYTES:
        raise ToolError(
            "ast_edit: ast-grep produced more than "
            f"{AST_GREP_MAX_STDOUT_BYTES // (1024 * 1024)}MB of match data. "
            "Nothing was written — acting on a partially read match stream "
            "would apply an incomplete codemod. Narrow 'paths' or the pattern."
        )

    stderr = stderr_b.decode("utf-8", errors="replace").strip()
    # ast-grep's ``run`` exits 0 on success and 1 when nothing matched; any
    # higher code is a real failure (unparseable pattern, unknown language,
    # unreadable path) and must not be mistaken for "no matches".
    if proc.returncode not in (0, 1):
        raise ToolError(
            f"ast_edit: ast-grep failed (exit {proc.returncode}): "
            f"{stderr or '<no stderr>'}"
        )
    return stdout_b.decode("utf-8", errors="replace"), stderr



# ---------------------------------------------------------------------------
# Preview rendering
# ---------------------------------------------------------------------------


def _snippet(text: str) -> str:
    """One-line, length-capped rendering of a matched / rewritten node.

    Whitespace is collapsed so a multi-line match (the common structural hit)
    stays one readable preview row instead of breaking up the report.
    """
    flat = " ".join(text.split())
    if len(flat) <= MAX_SNIPPET_CHARS:
        return flat
    return (
        flat[:MAX_SNIPPET_CHARS] + f"… (+{len(flat) - MAX_SNIPPET_CHARS} chars)"
    )


def _file_entry(entry: FileRewrite, *, span_limit: int) -> dict[str, Any]:
    shown = entry.spans[:span_limit]
    out: dict[str, Any] = {
        "path": str(entry.path),
        "matches": len(entry.spans),
        "bytes_before": len(entry.original_bytes),
        "bytes_after": len(entry.new_bytes),
        "changes": [
            {
                "line": span.line,
                "before": _snippet(span.matched_text),
                "after": _snippet(span.replacement),
            }
            for span in shown
        ],
    }
    if len(shown) < len(entry.spans):
        out["changes_truncated"] = len(entry.spans) - len(shown)
    return out


def _render_full(rewrites: list[FileRewrite]) -> str:
    """Plain-text rendering of EVERY change (persisted via the result store)."""
    lines: list[str] = []
    for entry in rewrites:
        lines.append(f"{entry.path}  ({len(entry.spans)} match(es))")
        for span in entry.spans:
            lines.append(f"  line {span.line}: {_snippet(span.matched_text)}")
            lines.append(f"       ->  {_snippet(span.replacement)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


def _resolve_reported_file(file_str: str, *, cwd: str) -> Path:
    """Turn ast-grep's reported ``file`` into an absolute path.

    ast-grep echoes each path as it walked it — relative to the process CWD when
    the target was relative (measured: ``data/tmp/m.py`` → ``data\\tmp\\m.py``)
    — so resolution is anchored on the SAME cwd the subprocess ran in.
    """
    candidate = Path(file_str)
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    return Path(os.path.normpath(str(candidate)))


def _is_within(path: Path, targets: list[Path]) -> bool:
    """True when ``path`` is one of ``targets`` or lives under one of them."""
    for target in targets:
        if path == target:
            return True
        try:
            path.relative_to(target)
            return True
        except ValueError:
            continue
    return False


async def tool_ast_edit(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
    tool_result_store: ToolResultStorePort | None = None,
    path_lock: PathLockManager | None = None,
) -> dict[str, Any]:
    """Structurally rewrite code: preview by default, write on ``apply=true``."""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ToolError("ast_edit: 'pattern' is required and must be a string")
    rewrite_template = args.get("rewrite")
    if not isinstance(rewrite_template, str):
        raise ToolError(
            "ast_edit: 'rewrite' is required and must be a string (pass an "
            "empty string to DELETE every matched node)"
        )
    lang = args.get("lang")
    if lang is not None and not isinstance(lang, str):
        raise ToolError("ast_edit: 'lang' must be a string")
    apply_changes = args.get("apply", False)
    if not isinstance(apply_changes, bool):
        raise ToolError("ast_edit: 'apply' must be a boolean")

    expect_matches = args.get("expect_matches")
    if expect_matches is not None:
        if isinstance(expect_matches, bool) or not isinstance(
            expect_matches, int | float
        ):
            raise ToolError("ast_edit: 'expect_matches' must be a number")
        expect_matches = int(expect_matches)

    raw_paths = args.get("paths")
    if raw_paths is not None and not isinstance(raw_paths, str):
        raise ToolError("ast_edit: 'paths' must be a string")
    entries = _split_targets(raw_paths or "")
    if not entries:
        entries = [str(default_cwd() or Path.cwd())]

    targets: list[Path] = []
    missing: list[str] = []
    for entry in entries:
        resolved = Path(resolve_under_workspace(entry))
        if await asyncio.to_thread(resolved.exists):
            targets.append(Path(os.path.normpath(str(resolved))))
        else:
            # One bad entry must not discard the good ones, but ALL-missing is
            # an error: silently scanning nothing would read as "no matches".
            missing.append(entry)
    if not targets:
        raise ToolError(
            "ast_edit: no target path exists "
            f"({', '.join(missing)}) — nothing was scanned."
        )

    # The scan READS every target. The WRITE gate is applied per changed file
    # below, once the dry-run has told us which files those actually are.
    for target in targets:
        await file_guard.enforce_read(
            path=str(target), caller="ai_coding.tool.ast_edit"
        )

    binary = await asyncio.to_thread(resolve_ast_grep_binary)
    if binary is None:
        raise ToolError(missing_binary_message("ast_edit"))

    from qai.platform.tool_progress import emit_progress

    emit_progress(
        f"ast_edit {'applying' if apply_changes else 'previewing'} "
        f"“{pattern[:48]}”…",
        "match",
    )

    cwd = str(default_cwd() or Path.cwd())
    stdout, stderr = await _run_ast_grep_dry(
        binary,
        pattern=pattern,
        rewrite_template=rewrite_template,
        lang=lang if lang else None,
        targets=targets,
        cwd=cwd,
    )
    if stderr:
        logger.debug("ast_edit: ast-grep stderr: %s", stderr)

    per_file = parse_rewrite_stream(stdout)
    if not per_file:
        # An unbalanced / unparseable pattern exits 0 with zero matches and only
        # says so on stderr, so without this branch a typo would be reported as
        # a confident "this code does not occur".
        if "ERROR node" in stderr:
            raise ToolError(
                "ast_edit: the pattern does not parse as valid "
                f"{lang or 'source'} code — ast-grep reports: "
                f"{' '.join(stderr.split())}. It therefore matched nothing, "
                "which is a PATTERN BUG, not evidence that the code is absent. "
                "Fix the pattern (balance the brackets; a fragment that cannot "
                "stand alone must be wrapped, e.g. 'class $_ { $$$BODY }') and "
                "retry."
            )
        if apply_changes and expect_matches:
            # The approved change set has VANISHED, which is drift just as much
            # as finding extra sites: reporting a cheerful zero-match here would
            # read as "already done" when in fact someone else rewrote the code
            # out from under the preview.
            raise ToolError(
                f"ast_edit: the preview found {expect_matches} match(es) but "
                "applying now finds NONE — the files changed between the "
                "preview and this call. NOTHING was written. Re-run the "
                "preview to see the current state of the code."
            )
        return _ok(
            f"ast_edit: pattern matched nothing in {'; '.join(entries)} — no "
            "file would change. The 'ast-grep' engine ran successfully and the "
            "pattern parsed, so this IS a real zero-match result. If you "
            "expected hits, check that 'lang' matches the files and that the "
            "metavariables fit ($NAME = exactly one node, $$$ARGS = zero or "
            "more nodes).",
            applied=False,
            files_changed=0,
            match_count=0,
            files=[],
            missing_paths=missing,
            truncated=False,
        )

    # Overlapping targets make ast-grep report the same match more than once:
    # ``paths="pkg; pkg/sub"`` walks ``pkg/sub/m.py`` under both, and the
    # stream carries that one node TWICE (measured: one file key, two
    # identical ``[4, 13)`` spans). Feeding both to ``apply_spans`` trips its
    # overlap guard and fails the whole codemod, and any duplicate that DID
    # get through would double the reported counts and commit the file twice.
    # So spans are deduplicated on ``(start, end, replacement)`` and buckets
    # are keyed by the RESOLVED path, which also merges the case where the
    # same file is printed under two different path strings.
    by_path: dict[Path, list[RewriteSpan]] = {}
    for file_str, spans in per_file.items():
        path = _resolve_reported_file(file_str, cwd=cwd)
        if not _is_within(path, targets):
            # Defensive: a match reported outside the requested scope would
            # become a write nobody asked for.
            raise ToolError(
                f"ast_edit: ast-grep reported a match in {path}, which is "
                f"outside the requested paths ({'; '.join(entries)}). "
                "Nothing was written."
            )
        bucket = by_path.setdefault(path, [])
        seen = {(s.start, s.end, s.replacement) for s in bucket}
        for span in spans:
            key = (span.start, span.end, span.replacement)
            if key in seen:
                continue
            seen.add(key)
            bucket.append(span)

    rewrites: list[FileRewrite] = []
    for path, spans in by_path.items():
        original_bytes = await asyncio.to_thread(_read_bytes, path)
        rewrites.append(
            await asyncio.to_thread(
                build_file_rewrite, path, original_bytes, spans
            )
        )

    # Counted from the COALESCED spans, not from ``per_file`` — a file reported
    # under two path strings would otherwise be tallied twice.
    matched_total = sum(len(spans) for spans in by_path.values())
    # A rewrite template that reproduces the matched text byte-for-byte changes
    # nothing; reporting it as a change (or committing it) would be a lie.
    rewrites = [r for r in rewrites if r.new_bytes != r.original_bytes]
    if apply_changes and expect_matches is not None and (
        matched_total != expect_matches
    ):
        # Nothing would be written on this path anyway, but reporting "no file
        # would change" after the match set moved would still tell the operator
        # their approved edit was a no-op, when the truth is the code changed.
        raise ToolError(
            f"ast_edit: the preview found {expect_matches} match(es) but "
            f"applying now finds {matched_total} — the files changed between "
            "the preview and this call. NOTHING was written. Re-run the "
            "preview."
        )
    if not rewrites:
        return _ok(
            f"ast_edit: {matched_total} node(s) matched, but the rewrite "
            "reproduces them byte-for-byte, so no file would change.",
            applied=False,
            files_changed=0,
            match_count=matched_total,
            files=[],
            missing_paths=missing,
            truncated=False,
        )

    match_count = sum(len(r.spans) for r in rewrites)

    if apply_changes:
        # Two independent drift checks, because they catch different things.
        #
        # 1. CONTENT. Every file this call is about to rewrite is compared with
        #    what this conversation last saw. ``expect_matches`` alone is blind
        #    to a change that leaves the match COUNT intact — a peer adding a
        #    comment line keeps it at 3 → 3 and sails through — which is
        #    exactly what the S7 round measured. The recorded tag notices any
        #    byte change, and needs nothing from the model.
        for entry in rewrites:
            seen = get_seen(entry.path)
            if seen is None:
                continue
            current = compute_snapshot(entry.path, data=entry.original_bytes)
            if current is not None and current != seen:
                raise ToolError(
                    f"ast_edit: {entry.path} changed since this conversation "
                    "last read it, so the rewrite would be based on content "
                    "you have not seen. NOTHING was written. Read the file "
                    "again (or re-run with apply=false) and retry."
                )

        # 2. SCOPE. An explicit ``expect_matches`` still pins the approved
        #    change-set size, which catches drift in files this conversation
        #    never read and therefore has no tag for.
        if expect_matches is not None and match_count != expect_matches:
            raise ToolError(
                f"ast_edit: the preview found {expect_matches} match(es) but "
                f"applying now would rewrite {match_count} — the files changed "
                "between the preview and this call, so the change set is no "
                "longer the one that was approved. NOTHING was written. "
                "Re-run the preview to see the current matches, then apply "
                "with the new count."
            )

    if apply_changes:
        for entry in rewrites:
            matched = protected_paths.is_write_blocked(str(entry.path))
            if matched:
                raise ToolGuardDenied(
                    message=protected_paths.deny_message(
                        str(entry.path), matched
                    ),
                    error_code="ai_coding.tool.protected_path_write_denied",
                )
            await file_guard.enforce_write(
                path=str(entry.path), caller="ai_coding.tool.ast_edit"
            )
        # PARALLEL-TOOL-1: serialise against concurrent writers of the SAME
        # files. Unlike write/edit/apply_patch the target set is not in the
        # arguments — it is discovered by the dry-run — so the lock is taken
        # HERE, around the commit only, over every file we are about to write
        # (``lock_many`` sorts canonical keys, so it cannot deadlock against
        # another multi-path op). No lock wired → unchanged behaviour.
        paths = [str(entry.path) for entry in rewrites]
        try:
            if path_lock is not None:
                async with path_lock.lock_many(paths):
                    await asyncio.to_thread(
                        commit_rewrites,
                        rewrites,
                        pattern=pattern,
                        rewrite_template=rewrite_template,
                    )
            else:
                await asyncio.to_thread(
                    commit_rewrites,
                    rewrites,
                    pattern=pattern,
                    rewrite_template=rewrite_template,
                )
        except SafeWriteError as exc:  # pragma: no cover — defensive
            raise ToolError(f"ast_edit: {exc}") from exc

        # Our own rewrite is not a foreign change: advance each file's tracked
        # tag so a following ``edit`` in this conversation is not rejected
        # against content this call just produced.
        for entry in rewrites:
            record_seen(entry.path, compute_snapshot(entry.path))

    visible = rewrites[:MAX_PREVIEW_FILES]
    files = [
        _file_entry(entry, span_limit=MAX_PREVIEW_SPANS_PER_FILE)
        for entry in visible
    ]
    truncated = len(visible) < len(rewrites) or any(
        "changes_truncated" in f for f in files
    )

    stored_path: str | None = None
    if truncated and tool_result_store is not None:
        body = _render_full(rewrites)
        try:
            preview = tool_result_store.store(
                body,
                tool_name="ast_edit",
                context_hint="full_changes",
                # The driver is a change COUNT, not a byte size: a preview with
                # hundreds of elided changes can still sit under the store's
                # byte threshold, and would then lose them with no retrieval
                # path. Force only in that case; a genuinely oversized body is
                # persisted by the store's own rule anyway.
                force=len(body) < get_tool_output_thresholds().grep_max_output_bytes,
            )
        except Exception:  # noqa: BLE001 — persistence is best-effort
            preview = None
        if preview is not None and preview.stored:
            stored_path = preview.stored_path

    if apply_changes:
        head = (
            f"ast_edit APPLIED {match_count} rewrite(s) across "
            f"{len(rewrites)} file(s). Every file was written through the "
            "standard safety net (original backed up to .edit_trash, atomic "
            "write, read-back verified); line endings preserved."
        )
    else:
        head = (
            f"ast_edit PREVIEW: {match_count} rewrite(s) would change "
            f"{len(rewrites)} file(s). NOTHING was written — no file on disk "
            "was touched. Review the changes below, then re-run the SAME call "
            f"with apply=true AND expect_matches={match_count} to commit them "
            "— that count makes the apply abort if anything changed these "
            "files in the meantime, instead of rewriting a different set of "
            "sites than the ones listed here."
        )
    if missing:
        head += f" Skipped non-existent path(s): {', '.join(missing)}."
    if stored_path is not None:
        head += f" Full change list: read(path={stored_path!r})."

    result = _ok(
        head,
        applied=apply_changes,
        files_changed=len(rewrites),
        match_count=match_count,
        files=files,
        missing_paths=missing,
        truncated=truncated,
    )
    if stored_path is not None:
        result["stored_path"] = stored_path
    return result


def _read_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ToolError(f"ast_edit: cannot read {path}: {exc}") from exc
