# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""File read / write / edit tool handlers (``read`` / ``write`` / ``edit``)."""

from __future__ import annotations

import asyncio
import io
import os
from pathlib import Path
from typing import Any

from qai.ai_coding.application.ports import FileGuardPort
from qai.ai_coding.infrastructure.tools._safe_commit import (
    SafeWriteError,
    atomic_write_bytes,
    safe_commit_text,
    verify_after_write,
)
from qai.ai_coding.infrastructure.tools.errors import ToolError, ToolGuardDenied
from qai.ai_coding.infrastructure.tools.handlers._edit_match import (
    EditMatchError,
    detect_line_ending,
    normalize_newlines,
    replace_block,
    restore_line_ending,
)
from qai.ai_coding.infrastructure.tools.handlers._file_snapshot import (
    compute_snapshot,
    get_seen,
    record_seen,
    verify_snapshot,
)
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    CODE_EXTENSIONS,
    _format_complete_notice,
    _format_truncation_notice,
    _ok,
    count_file_lines,
    expand_skill_placeholders,
    format_bytes,
    get_tool_output_thresholds,
    get_workspace_base,
    is_under_tool_result_store_root,
    make_line_truncated_suffix,
    resolve_under_workspace,
)
from qai.ai_coding.infrastructure.tools.handlers._source_outline import (
    build_outline,
    split_source_lines,
    supports_outline,
)
from qai.platform import protected_paths
from qai.platform.scheduling.path_locks import PathLockManager

#: Files larger than this are never outlined: parsing cost stops paying off
#: and a plain windowed read is the right tool.
_OUTLINE_MAX_BYTES = 4 * 1024 * 1024


def _enforce_not_protected(path_str: str) -> None:
    """ALWAYS-ON guard: deny writes into a protected path tree.

    Independent of FileGuard (which ships disabled): the QAIRT SDK / Qualcomm
    toolchain tree must never be modified by the agent, even with every
    optional security module off. Raises :class:`ToolGuardDenied` (surfaced to
    the model as a tool error) when ``path_str`` is under a protected prefix.
    """
    matched = protected_paths.is_write_blocked(path_str)
    if matched:
        raise ToolGuardDenied(
            message=protected_paths.deny_message(path_str, matched),
            error_code="ai_coding.tool.protected_path_write_denied",
        )


def _workspace_root_for(path: Path) -> Path:
    """Best-effort workspace root used to lay out the edit-trash tree.

    Prefers the per-request workspace base (the active session workspace, e.g.
    ``C:\\WoS_AI``); falls back to the file's own parent. This is passed to
    :func:`safe_commit_text` / :func:`backup_to_trash`, where
    :func:`resolve_trash_root` places the trash at ``<workspace>/.edit_trash``
    — co-located with the edited file, requiring NO ``src/``/``apps/``/``data/``
    marker. The safe-commit layer degrades gracefully (and falls back to a
    per-user dir, only skipping as a true last resort), so an imperfect root
    here never breaks the write.
    """
    base = get_workspace_base()
    if base:
        try:
            return Path(base)
        except (ValueError, OSError):  # pragma: no cover — defensive
            pass
    return path.parent


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


async def tool_read(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
) -> dict[str, Any]:
    path_str = args.get("path") or ""
    if not isinstance(path_str, str) or not path_str:
        raise ToolError("read: 'path' argument is required")
    path_str = resolve_under_workspace(path_str)

    offset = int(args.get("offset") or 1)
    limit_raw = args.get("limit")
    limit = int(limit_raw) if limit_raw is not None else None
    ranges = _parse_ranges(args.get("ranges"))
    # ``outline`` is a three-state switch: True forces it, False forbids it,
    # absent (None) lets the heuristic decide (no explicit window → outline).
    outline_raw = args.get("outline")
    outline_pref = bool(outline_raw) if outline_raw is not None else None

    # 退化 #11 (subtask 2): files the oversized-output store persisted under
    # ``data/tool_results/`` are SYSTEM-OWNED retrieval targets — the process
    # wrote them itself and explicitly told the model to ``read`` them back.
    # Bypass the FileGuard read gate for them (V1 ``get_stored_result`` read
    # STORAGE_DIR directly, never through the allowlist) so the saved file is
    # always recoverable even when the operator turned FileGuard ON without
    # allow-listing the application data dir. The trusted store root(s) are
    # registered by the apps/api wiring root via ``set_tool_result_store_roots``;
    # the set is empty by default so non-store reads are unaffected.
    if is_under_tool_result_store_root(path_str):
        return await asyncio.to_thread(
            _read_dispatch, path_str, offset, limit, ranges, outline_pref
        )

    # M10: heartbeat progress on large-file reads.  Cheap probe on file
    # size — files under 1 MB emit nothing; larger files publish a "reading
    # …" preview through the per-turn emitter (no-op outside a chat turn).
    try:
        _size = Path(path_str).stat().st_size
    except Exception:  # noqa: BLE001 — never break the read
        _size = 0
    if _size >= 1_048_576:
        from qai.platform.tool_progress import emit_progress
        _mb = _size / 1_048_576
        emit_progress(f"reading large file ({_mb:.1f} MB)…", "bytes")

    await file_guard.enforce_read(path=path_str, caller="ai_coding.tool.read")

    return await asyncio.to_thread(
        _read_dispatch, path_str, offset, limit, ranges, outline_pref
    )


def _parse_ranges(raw: Any) -> list[tuple[int, int]] | None:
    """Normalise the ``ranges`` argument into ordered 1-indexed line spans.

    Accepts ``"5-16,960-973"`` (string) or ``["5-16", "960-973"]`` (list).
    A bare ``"42"`` is the single line 42; ``"80-"`` runs to end of file
    (represented as an open end via ``-1``). Overlapping / unordered spans
    are merged and sorted so the reader walks the file exactly once.
    """
    if raw is None:
        return None
    if isinstance(raw, str):
        tokens = [tok.strip() for tok in raw.split(",")]
    elif isinstance(raw, list | tuple):
        tokens = [str(tok).strip() for tok in raw]
    else:
        raise ToolError("read: 'ranges' must be a string or a list of strings")

    spans: list[tuple[int, int]] = []
    for token in tokens:
        if not token:
            continue
        if "-" in token:
            head, _, tail = token.partition("-")
            try:
                start = int(head)
            except ValueError as exc:
                raise ToolError(
                    f"read: invalid range {token!r} (expected N-M, N- or N)"
                ) from exc
            if tail.strip() == "":
                end = -1  # open-ended: to end of file
            else:
                try:
                    end = int(tail)
                except ValueError as exc:
                    raise ToolError(
                        f"read: invalid range {token!r} (expected N-M, N- or N)"
                    ) from exc
        else:
            try:
                start = end = int(token)
            except ValueError as exc:
                raise ToolError(
                    f"read: invalid range {token!r} (expected N-M, N- or N)"
                ) from exc
        if start < 1:
            raise ToolError(f"read: range start must be >= 1 (got {start})")
        if end != -1 and end < start:
            raise ToolError(
                f"read: range end {end} is before its start {start}"
            )
        spans.append((start, end))

    if not spans:
        return None
    spans.sort(key=lambda s: (s[0], -1 if s[1] == -1 else s[1]))
    merged: list[tuple[int, int]] = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if last_end == -1:
            break  # an open span swallows everything after it
        if start <= last_end + 1:
            merged[-1] = (last_start, -1 if end == -1 else max(last_end, end))
        else:
            merged.append((start, end))
    return merged


def _read_dispatch(
    path_str: str,
    offset: int,
    limit: int | None,
    ranges: list[tuple[int, int]] | None,
    outline_pref: bool | None,
) -> dict[str, Any]:
    """Route a read to the ranges / outline / plain-slice reader.

    Precedence is deliberate:

    1. ``ranges`` — an explicit multi-span request always wins.
    2. Outline — only when the caller asked for NO window (default offset,
       no limit) or set ``outline=True``. A caller who named a window wants
       those lines, not a summary.
    3. Plain slice — the previous behaviour, unchanged.

    Persisted tool results are NEVER outlined unless explicitly asked. The
    process wrote them itself and told the model to read them back for the
    COMPLETE output; silently returning a summary of the recovery file would
    break that promise (the store names files ``.txt`` today, so this is a
    guard against a future rename rather than a live bug).
    """
    if ranges is not None:
        return _read_line_ranges(path_str, ranges)

    wants_window = offset > 1 or limit is not None
    is_recovery_read = is_under_tool_result_store_root(path_str)
    use_outline = outline_pref is True or (
        outline_pref is None and not wants_window and not is_recovery_read
    )
    if use_outline:
        outlined = _try_outline(path_str, forced=outline_pref is True)
        if outlined is not None:
            return outlined
    return _read_file_slice(path_str, offset, limit)


def _snapshot_hint(snapshot: str | None) -> str:
    """Model-VISIBLE note carrying the snapshot tag, for the MESSAGE field.

    The tag is also returned as a structured ``snapshot`` field, but a model
    only ever sees the rendered text. Left out of it, the value is
    undiscoverable: nothing tells the model that ``edit`` accepts
    ``expect_snapshot``, so the concurrency guard sits unused and a stale edit
    silently overwrites whoever wrote in between — the exact race the tag was
    added to close.

    It goes in ``message``, NOT in ``content``. ``content`` is the file's text
    under a byte ceiling: appending there both corrupted line-oriented parsing
    (callers split ``content`` and read a line number off each row) and pushed
    a large outline PAST ``read_max_bytes`` — reintroducing the unbounded-output
    defect that ceiling exists to prevent.
    """
    if not snapshot:
        return ""
    return (
        f" [snapshot: {snapshot}] — pass this back as edit's "
        "'expect_snapshot' so the write is REFUSED if someone else changes "
        "this file first."
    )


def _try_outline(path_str: str, *, forced: bool) -> dict[str, Any] | None:
    """Build a declaration outline for *path_str*, or ``None`` to fall back.

    Fail-open by construction: an unreadable / unparseable / too-small /
    unsupported file returns ``None`` so the caller does a normal read. An
    outline is an optimisation, never a precondition.

    The rendered outline honours the SAME line / byte ceilings as a plain
    read. It is not automatically small: a declaration-dense 14 000-line
    module outlines to ~280 KB, over 5x ``read_max_bytes``, and because the
    result carries a ``truncated`` key the generic second-layer truncator
    deliberately keeps its hands off (the tool is declaring it owns its own
    size). Unbounded, the outline would therefore inject several times more
    text than the plain read it replaces — the opposite of the context saving
    it exists for.
    """
    path = Path(path_str)
    if not supports_outline(path.suffix):
        return None
    try:
        if path.stat().st_size > _OUTLINE_MAX_BYTES:
            return None
        # Raw bytes, decoded + newline-normalised BY HAND rather than via
        # ``read_text``: identical result (universal-newline translation is
        # exactly ``\r\n``/``\r`` → ``\n``) but it leaves us holding the
        # file's real bytes, so the snapshot tag is derived from THEM instead
        # of costing a second full pass over the file.
        raw_bytes = path.read_bytes()
    except OSError:
        return None
    source = normalize_newlines(raw_bytes.decode("utf-8", errors="replace"))

    built = build_outline(source, path.suffix)
    if built is None:
        return None
    outline_text, elided = built
    total_lines = len(split_source_lines(source))
    snapshot = compute_snapshot(path, data=raw_bytes)
    record_seen(path, snapshot)

    thresholds = get_tool_output_thresholds()
    rows = outline_text.splitlines()
    kept: list[str] = []
    body_bytes = 0
    capped = False
    for row in rows:
        row_bytes = len(row.encode("utf-8")) + 1
        if (
            len(kept) >= thresholds.read_max_lines
            or body_bytes + row_bytes > thresholds.read_max_bytes
        ):
            capped = True
            break
        kept.append(row)
        body_bytes += row_bytes
    if capped:
        outline_text = "\n".join(kept)

    forced_note = "" if not forced else " (outline=true)"
    summary = (
        f"Structural outline{forced_note}: {total_lines} line(s) total, "
        f"{elided} line(s) inside declaration bodies elided. Declarations "
        f"and module-level code are shown with their real line numbers. "
        f"To see an elided body, call read again with "
        f"ranges=\"<start>-<end>\" for ONLY the range(s) you need "
        f"(ranges accepts several spans, e.g. \"5-16,960-973\"); pass "
        f"outline=false to read the whole file instead. Do NOT guess the "
        f"content of an elided range."
    )
    if capped:
        # The outline itself overflowed, so it is a PREFIX of the declaration
        # list — say so, or the model reads "outline" as "every declaration in
        # this file" and concludes the rest do not exist.
        last_shown = kept[-1].split(":", 1)[0].strip() if kept else "0"
        summary += (
            f" NOTE: this outline is itself TRUNCATED at the "
            f"{thresholds.read_max_lines}-line / "
            f"{thresholds.read_max_bytes // 1024}KB ceiling — it covers only "
            f"up to line {last_shown} of {total_lines} and is NOT the complete "
            f"declaration list. Use ranges=\"{last_shown}-\" (or grep) for the "
            f"rest."
        )
    return _ok(
        summary + _snapshot_hint(snapshot),
        content=outline_text,
        total_lines=total_lines,
        outline=True,
        elided_lines=elided,
        truncated=capped,
        snapshot=snapshot,
    )


def _read_line_ranges(
    path_str: str, ranges: list[tuple[int, int]]
) -> dict[str, Any]:
    """Read several disjoint line spans in ONE pass over the file.

    Spans arrive merged + sorted (see :func:`_parse_ranges`), so the file is
    streamed once and each requested line is emitted with its real line
    number. The combined body honours the same byte/line ceilings as a plain
    read so a pathological request cannot flood the context window.

    ``total_lines`` is the file's REAL line count, resolved by the same
    bounded newline-count pass a plain read uses. It must not be "the last
    line number this loop happened to walk past": the loop stops as soon as
    the last requested span is behind it, so on ``ranges="5-16"`` of a
    20 000-line file that bookkeeping reported ``total_lines=16`` — i.e. "you
    have the whole file" — the exact class of undetectable lie already fixed
    in :func:`_read_file_slice`.
    """
    path = Path(path_str)
    if not path.exists():
        raise ToolError(f"read: file not found: {path_str}")
    if path.is_dir():
        raise ToolError(
            f"read: path is a directory (use 'list'): {path_str}"
        )

    thresholds = get_tool_output_thresholds()
    max_lines = thresholds.read_max_lines
    max_bytes = thresholds.read_max_bytes

    open_ended = any(end == -1 for _, end in ranges)
    last_needed = -1 if open_ended else max(end for _, end in ranges)

    total_lines, total_lines_exact = count_file_lines(
        path, scan_max_bytes=thresholds.read_count_scan_max_bytes
    )
    # Snapshot of the WHOLE file (chunked, O(1) memory) — this reader emits
    # only the requested spans, so hashing what it returns would hand out a
    # different tag per ``ranges`` request for one unchanged file.
    snapshot = compute_snapshot(path)
    record_seen(path, snapshot)

    chunks: list[str] = []
    emitted = 0
    body_bytes = 0
    capped = False
    walked = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line_no, raw_line in enumerate(handle, start=1):
                walked = line_no
                if not capped and _line_in_ranges(line_no, ranges):
                    text = raw_line.rstrip("\n").rstrip("\r")
                    rendered = f"{line_no:>6}: {text}\n"
                    encoded_len = len(rendered.encode("utf-8"))
                    if (
                        emitted >= max_lines
                        or body_bytes + encoded_len > max_bytes
                    ):
                        capped = True
                    else:
                        chunks.append(rendered)
                        emitted += 1
                        body_bytes += encoded_len
                # Stop early once nothing further can be emitted: either every
                # requested span is behind us, or the output is capped and the
                # remaining spans could only add lines we would drop anyway.
                if capped or (last_needed != -1 and line_no >= last_needed):
                    break
    except OSError as exc:
        raise ToolError(f"read: cannot read {path_str}: {exc}") from exc

    spans_text = ",".join(
        f"{start}-{'EOF' if end == -1 else end}" for start, end in ranges
    )
    total_part = (
        f"{total_lines}" if total_lines_exact else f"at least {total_lines}"
    )
    if emitted == 0:
        return _ok(
            f"(no lines matched ranges {spans_text}; the file has "
            f"{total_part} line(s), {walked} scanned)",
            content="",
            total_lines=total_lines,
            total_lines_exact=total_lines_exact,
            ranges=spans_text,
            truncated=False,
            snapshot=snapshot,
        )
    summary = (
        f"{emitted} line(s) from range(s) {spans_text} "
        f"(file has {total_part} line(s))"
    )
    if capped:
        summary += (
            f"; output capped at {max_lines} lines / "
            f"{max_bytes // 1024}KB — narrow the ranges for the rest"
        )
    return _ok(
        summary + _snapshot_hint(snapshot),
        content="".join(chunks),
        total_lines=total_lines,
        total_lines_exact=total_lines_exact,
        ranges=spans_text,
        truncated=capped,
        snapshot=snapshot,
    )


def _line_in_ranges(line_no: int, ranges: list[tuple[int, int]]) -> bool:
    """True when *line_no* falls inside any (merged, sorted) span."""
    for start, end in ranges:
        if line_no < start:
            return False  # sorted: no later span can contain it
        if end == -1 or line_no <= end:
            return True
    return False


def _read_file_slice(
    path_str: str, offset: int, limit: int | None
) -> dict[str, Any]:
    """Read a (possibly truncated) line slice of ``path_str``.

    Shared by the guarded read path and the tool-result-store retrieval
    bypass so both produce identical truncation / numbering semantics. Runs
    blocking filesystem I/O, so callers invoke it via ``asyncio.to_thread``.

    Streams the file LINE BY LINE (never reads the whole file into memory) so a
    multi-GB log / data file does not OOM the process when the model only wants
    the first ``READ_MAX_LINES`` / ``READ_MAX_BYTES``. The loop keeps only the
    selected window in memory and a byte budget; once the byte budget is reached
    it stops materialising.

    Reported scale signals (BOTH are given on every read)
    ----------------------------------------------------
    * ``file_size`` — one ``stat()`` (~0.06 ms), ALWAYS exact. The cheapest and
      most reliable "how big is this" signal, and it stays trustworthy even when
      the line total is only a bound.
    * ``total_lines`` / ``total_lines_exact`` — the file's real line count,
      obtained by a SEPARATE raw newline-count pass bounded by
      ``read_count_scan_max_bytes`` (50 MB). Counting newlines over raw blocks is
      O(1) memory and ~2x cheaper than decoding (measured: 1 MB ~2 ms, 50 MB
      ~64 ms), so an exact total is effectively free for real source / config /
      log files. Past the budget the count is a LOWER BOUND and
      ``total_lines_exact`` is ``False``.

    The earlier implementation reported "lines seen before the output cap fired"
    AS the total — a lie the model could not detect (a 20 000-line file read at
    the 50KB cap came back "showed lines 1-512 of total 512", i.e. "you have the
    whole file"), so it stopped reading and reasoned over 2.5% of the content.
    The justification was that scanning to EOF is the unbounded read this loop
    exists to prevent; that conflated STORING lines (the real OOM risk) with
    COUNTING them (cheap). Hence the bounded counting pass above.
    """
    path = Path(path_str)
    if not path.exists():
        raise ToolError(f"read: file not found: {path_str}")
    if not path.is_file():
        raise ToolError(f"read: not a file: {path_str}")

    # Live, runtime-configurable in-prompt caps (fall back to the module
    # defaults when the wiring root has not installed config).
    thresholds = get_tool_output_thresholds()
    max_lines = thresholds.read_max_lines
    max_bytes = thresholds.read_max_bytes
    max_line_length = thresholds.read_max_line_length

    # Scale signals, resolved BEFORE the window read so they describe the whole
    # file rather than the slice. ``stat`` is ~0.06 ms and always exact; the
    # count is a bounded raw newline pass (see ``count_file_lines``).
    try:
        file_size: int | None = path.stat().st_size
    except OSError:
        file_size = None
    total_lines, total_lines_exact = count_file_lines(
        path, scan_max_bytes=thresholds.read_count_scan_max_bytes
    )
    # Snapshot tag: identity of the FILE, not of the window below. Taken here
    # (before the window read) so every exit path — including the
    # offset-past-EOF early return — carries it, and computed over the whole
    # file so reading the same file at different offset/limit yields the SAME
    # tag. Chunked, so a multi-GB file is still O(1) memory; past
    # ``SNAPSHOT_MAX_BYTES`` it degrades to a ``weak-`` size+mtime tag (see
    # ``_file_snapshot``).
    snapshot = compute_snapshot(path)
    record_seen(path, snapshot)

    start_idx = max(0, offset - 1)
    # The maximum number of lines we are willing to MATERIALISE in the window
    # (user ``limit`` is the tighter of the two; the line cap always applies).
    window_cap = max_lines if limit is None else min(limit, max_lines)

    selected: list[str] = []  # the materialised window (kept-EOL lines)
    seen_lines = 0  # lines the window pass walked past (for offset bookkeeping)
    body_bytes = 0  # encoded bytes of the materialised body so far
    line_clipped = False
    user_limit_applied = False
    line_cap_hit = False
    byte_cap_hit = False
    # The byte budget bounds the in-memory body; once the materialised body
    # would exceed it we stop reading entirely (the OOM guard). We add a small
    # headroom so a single final line that crosses the boundary is still
    # emitted whole before the cut, matching the prior "cut on the last full
    # line" behaviour.
    byte_budget = max_bytes

    try:
        # Binary stream + incremental UTF-8 decode (errors="replace") so the
        # file is never fully buffered. Universal-newline translation (the
        # default ``newline=None``) normalises ``\r\n`` / ``\r`` to ``\n`` on
        # read — byte-for-byte identical to the prior ``path.read_text()`` +
        # ``splitlines(keepends=True)`` so downstream content / numbering is
        # unchanged, just streamed instead of fully buffered.
        with open(path, "rb") as raw_fh:
            stream = io.TextIOWrapper(
                raw_fh, encoding="utf-8", errors="replace"
            )
            try:
                for raw_line in stream:
                    seen_lines += 1
                    idx = seen_lines - 1  # 0-based index of this line
                    if idx < start_idx:
                        continue  # before the window — skip, do not materialise
                    if len(selected) >= window_cap:
                        # Window full. Record WHY (user limit vs line cap) and
                        # stop: the authoritative total already came from the
                        # bounded counting pass, so there is nothing to gain by
                        # walking the rest of the file here.
                        if limit is not None and window_cap == limit:
                            user_limit_applied = True
                        else:
                            line_cap_hit = True
                        break

                    # Per-line clip (CONTENT excluding the EOL); preserve the
                    # original line ending so downstream numbering / byte math
                    # is unaffected. Universal newlines normalised everything to
                    # ``\n`` (or no EOL on the final unterminated line).
                    if raw_line.endswith("\n"):
                        content_part, eol = raw_line[:-1], "\n"
                    else:
                        content_part, eol = raw_line, ""
                    if len(content_part) > max_line_length:
                        original_len = len(content_part)
                        content_part = content_part[:max_line_length] + (
                            make_line_truncated_suffix(
                                kept_chars=max_line_length,
                                original_chars=original_len,
                            )
                        )
                        line_clipped = True
                    emitted = content_part + eol

                    size = len(emitted.encode("utf-8"))
                    if selected and body_bytes + size > byte_budget:
                        # Emitting this line would overflow the byte budget.
                        # Stop here (do NOT store it): the materialised body is
                        # bounded and we never read the rest of the file.
                        byte_cap_hit = True
                        seen_lines -= 1  # this line was not emitted
                        break
                    selected.append(emitted)
                    body_bytes += size
            finally:
                # Detach so closing the wrapper does not also close raw_fh twice;
                # the ``with`` closes raw_fh.
                stream.detach()
    except ToolError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"read: cannot read file: {e}") from e

    if start_idx >= seen_lines and not selected:
        return _ok(
            f"(file has {total_lines} lines; offset {offset} is beyond "
            f"end of file)",
            content="",
            total_lines=total_lines,
            total_lines_exact=total_lines_exact,
            file_size=file_size,
            start_line=offset,
            end_line=offset,
            truncated=False,
            snapshot=snapshot,
        )

    truncated_reason: str | None = None
    if line_cap_hit:
        truncated_reason = f"line cap {max_lines}"
    elif user_limit_applied:
        truncated_reason = f"user limit {limit}"
    if byte_cap_hit:
        truncated_reason = f"byte cap {max_bytes // 1024}KB"
    if truncated_reason is None and line_clipped:
        truncated_reason = f"line length cap {max_line_length}"

    start_line = start_idx + 1
    end_line = start_idx + len(selected)
    body = "".join(selected)

    # A LOWER BOUND must never contradict what we just showed. The counting
    # pass can legitimately report fewer lines than the window materialised —
    # it stops at its own budget (and a budget of 0 disables it entirely) — so
    # "100 of at least 0" was reachable. Lift the bound to at least ``end_line``
    # so the two numbers stay consistent. Only ever applied to a BOUND; an exact
    # count is authoritative and left untouched.
    if not total_lines_exact and total_lines < end_line:
        total_lines = end_line

    # 7-L2: SKILL.md files use path placeholders so bundled asset / sibling
    # sub-SKILL references resolve when the file is read on demand (not via the
    # system-prompt injection path). ``${SKILL_DIR}`` → the skill's own dir;
    # ``${APP_ROOT}`` → the install/repo root (bound per-request at the DI
    # ToolPort boundary). Unavailable placeholders are left verbatim.
    if path.name == "SKILL.md" and "${" in body:
        body = expand_skill_placeholders(body, skill_dir=str(path.parent))

    # Scale footer. Emitted on EVERY read, truncated or not.
    #
    # It must live in ``content``, not in ``message``: the chat render layer
    # (``apps/api/_chat_tool_result_render.render_tool_result_text``) returns
    # ``content`` VERBATIM whenever it is non-empty and never looks at
    # ``message`` — so anything put only in ``message`` is invisible to the
    # model on a normal read. A live session confirmed this: a complete
    # ``pyproject.toml`` read reported nothing, and the model concluded "read
    # completeness is signalled only by the ABSENCE of a truncation notice",
    # i.e. it was inferring completeness from silence. Silence is a weak signal
    # (indistinguishable from a tool that simply forgot to tell you), so we
    # state completeness positively instead.
    if truncated_reason:
        notice = _format_truncation_notice(
            start_line=start_line,
            end_line=end_line,
            total_lines=total_lines,
            reason=truncated_reason,
            total_is_exact=total_lines_exact,
            file_size=file_size,
        )
    else:
        notice = _format_complete_notice(
            start_line=start_line,
            end_line=end_line,
            total_lines=total_lines,
            total_is_exact=total_lines_exact,
            file_size=file_size,
        )

    if path.suffix.lower() in CODE_EXTENSIONS:
        numbered_lines = []
        for i, line in enumerate(
            body.splitlines(keepends=True), start=start_idx + 1
        ):
            numbered_lines.append(f"{i}\t{line}")
        content = "".join(numbered_lines) + notice
    else:
        content = body + notice

    # Scale signals go in the MESSAGE too, so they are present on every read —
    # including the untruncated ones, which carry no trailing notice. The model
    # then always knows the file's true size / line count, not just the size of
    # the slice it happened to receive.
    shown = end_line - start_line + 1
    total_part = (
        f"{total_lines} total" if total_lines_exact else f"at least {total_lines} total"
    )
    size_part = (
        f", {format_bytes(file_size)}" if file_size is not None else ""
    )
    return _ok(
        f"read {path_str} ({shown} of {total_part} lines{size_part})"
        + _snapshot_hint(snapshot),
        content=content,
        total_lines=total_lines,
        total_lines_exact=total_lines_exact,
        file_size=file_size,
        start_line=start_line,
        end_line=end_line,
        truncated=bool(
            truncated_reason and not truncated_reason.startswith("user limit")
        ),
        snapshot=snapshot,
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


async def tool_list(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
) -> dict[str, Any]:
    """List the entries directly inside a directory (single level, paginated).

    Complements ``glob`` (which returns FILES only): ``list`` shows files AND
    sub-directories (dirs suffixed with ``/``), sorted alphabetically, so the
    model can see a directory's structure — including empty sub-directories
    glob never reports. Pagination reuses the same ``offset`` / ``limit``
    semantics as ``read`` (1-indexed offset, default page size =
    ``read_max_lines``); when more entries remain the response says how many
    and which ``offset`` to pass next. NOT recursive.
    """
    path_str = args.get("path") or ""
    if not isinstance(path_str, str) or not path_str:
        raise ToolError("list: 'path' argument is required")
    path_str = resolve_under_workspace(path_str)

    offset = int(args.get("offset") or 1)
    limit_raw = args.get("limit")
    limit = int(limit_raw) if limit_raw is not None else None

    await file_guard.enforce_read(path=path_str, caller="ai_coding.tool.list")

    return await asyncio.to_thread(_list_dir_slice, path_str, offset, limit)


def _list_dir_slice(
    path_str: str, offset: int, limit: int | None
) -> dict[str, Any]:
    """Enumerate one directory level and return a (paginated) entry slice.

    Sub-directory names are suffixed ``/``; entries are sorted alphabetically
    (case-insensitively). Runs blocking filesystem I/O, so callers invoke it
    via ``asyncio.to_thread``. The default page size is the configured
    ``read_max_lines`` so a directory with an enormous number of entries cannot
    flood the context window; the response tells the model the next ``offset``.
    """
    path = Path(path_str)
    if not path.exists():
        raise ToolError(f"list: directory not found: {path_str}")
    if not path.is_dir():
        raise ToolError(f"list: not a directory: {path_str}")

    entries: list[str] = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    is_dir = False
                entries.append(entry.name + "/" if is_dir else entry.name)
    except OSError as e:
        raise ToolError(f"list: cannot read directory: {e}") from e
    entries.sort(key=lambda name: name.lower())

    total_entries = len(entries)
    default_limit = get_tool_output_thresholds().read_max_lines
    page = limit if limit is not None and limit > 0 else default_limit
    start_idx = max(0, offset - 1)
    sliced = entries[start_idx : start_idx + page]
    end_idx = start_idx + len(sliced)
    truncated = end_idx < total_entries

    body = "\n".join(sliced)
    if truncated:
        next_off = end_idx + 1
        notice = (
            f"\n\n(Showing {len(sliced)} of {total_entries} entries "
            f"[{start_idx + 1}-{end_idx}]; call list again with "
            f"offset={next_off} to continue.)"
        )
    elif start_idx > 0:
        notice = (
            f"\n\n(Showing entries {start_idx + 1}-{end_idx} of "
            f"{total_entries}.)"
        )
    else:
        notice = f"\n\n({total_entries} entries)"

    return _ok(
        f"list {path_str} ({len(sliced)} entries)",
        content=body + notice,
        entries=sliced,
        total_entries=total_entries,
        start_entry=start_idx + 1,
        end_entry=end_idx,
        truncated=truncated,
    )


# ---------------------------------------------------------------------------
# write
# ---------------------------------------------------------------------------


async def tool_write(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
    path_lock: PathLockManager | None = None,
) -> dict[str, Any]:
    path_str = args.get("path") or ""
    if not isinstance(path_str, str) or not path_str:
        raise ToolError("write: 'path' argument is required")
    path_str = resolve_under_workspace(path_str)
    content = args.get("content")
    if not isinstance(content, str):
        raise ToolError("write: 'content' argument is required and must be string")

    # M10: emit a keep-alive when writing a large file (verify_after_write
    # + backup can take seconds on big content).
    if len(content) >= 1_048_576:  # >= 1 MB
        from qai.platform.tool_progress import emit_progress
        emit_progress(
            f"writing large file ({len(content) / 1_048_576:.1f} MB)…",
            "bytes",
        )

    async def _guarded_write() -> dict[str, Any]:
        _enforce_not_protected(path_str)
        await file_guard.enforce_write(path=path_str, caller="ai_coding.tool.write")

        def _do_write() -> dict[str, Any]:
            path = Path(path_str)
            existed = path.exists()
            # ``write`` is a WHOLE-FILE overwrite (or create): there is NO
            # original→delta conservation relation, so NO conservation guard
            # and NO large-change block here (those belong to ``edit`` /
            # ``apply_patch``). Safety comes from atomic write + read-back
            # verify, plus a trash backup of the ORIGINAL when overwriting.
            try:
                if existed:
                    # Overwrite path: detect/restore the file's own EOL and
                    # route through the full backup + atomic + verify pipeline.
                    # Read RAW bytes (no newline translation) so the true EOL
                    # is detected and preserved on the verbatim atomic write.
                    try:
                        raw = path.read_bytes().decode("utf-8")
                    except UnicodeDecodeError as e:
                        raise ToolError(
                            f"write: existing file is not valid UTF-8: {e}"
                        ) from e
                    except Exception as e:  # noqa: BLE001
                        raise ToolError(
                            f"write: cannot read existing file: {e}"
                        ) from e
                    # An overwrite legitimately re-flows the whole body to the
                    # file's existing convention (that is what ``write`` means),
                    # so the NEW text is restored to ``line_ending``. The
                    # ORIGINAL must NOT be: passing a normalised copy made
                    # ``safe_commit_text`` back up bytes the file never had, so
                    # a CRLF file's .edit_trash entry could not restore it.
                    line_ending = detect_line_ending(raw)
                    new_norm = normalize_newlines(content)
                    expected_bytes = restore_line_ending(
                        new_norm, line_ending
                    ).encode("utf-8")
                    safe_commit_text(
                        path=path,
                        new_text=expected_bytes.decode("utf-8"),
                        original_text=raw,
                        line_ending="\n",
                        workspace_root=_workspace_root_for(path),
                        tool="write",
                        edits=0,
                        meta={"mode": "overwrite"},
                        restore_line_ending=restore_line_ending,
                    )
                else:
                    # New file: atomic write + verify only (nothing to back
                    # up, no conservation relation). Preserve the content
                    # verbatim (no EOL normalisation for a brand-new file).
                    expected_bytes = content.encode("utf-8")
                    atomic_write_bytes(path, expected_bytes)
                    verify_after_write(path, expected_bytes)
            except SafeWriteError as e:
                raise ToolError(f"write: {e}") from e
            except ToolError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"write: cannot write file: {e}") from e
            size = len(expected_bytes)
            # Our own write is not a foreign change: advance the tracked tag so
            # a following ``edit`` in this conversation is not rejected against
            # a file only we just wrote.
            record_seen(path, compute_snapshot(path))
            return _ok(
                f"Successfully wrote {size} bytes to {path_str}",
                path=path_str,
                bytes_written=size,
            )

        return await asyncio.to_thread(_do_write)

    # PARALLEL-TOOL-1: serialise concurrent writes to the SAME file (per-path
    # lock); different files still run in parallel. No lock wired → unchanged.
    if path_lock is not None:
        async with path_lock.lock(path_str):
            return await _guarded_write()
    return await _guarded_write()


# ---------------------------------------------------------------------------
# edit
# ---------------------------------------------------------------------------


async def tool_edit(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
    path_lock: PathLockManager | None = None,
) -> dict[str, Any]:
    path_str = args.get("path") or ""
    if not isinstance(path_str, str) or not path_str:
        raise ToolError("edit: 'path' argument is required")
    path_str = resolve_under_workspace(path_str)
    edits = args.get("edits")
    if not isinstance(edits, list) or not edits:
        raise ToolError("edit: 'edits' list is required and must not be empty")
    # OPTIONAL optimistic-concurrency anchor. Absent (the default) → every
    # code path below behaves exactly as it always has; present → the file
    # must still be byte-identical to what the ``read`` that produced this tag
    # saw, or nothing is written. See ``_file_snapshot``.
    expect_snapshot = args.get("expect_snapshot")

    # M10: emit a keep-alive when applying many edits at once (the multi-edit
    # match+rewrite pipeline can take seconds when the edit list is large).
    if len(edits) >= 20:
        from qai.platform.tool_progress import emit_progress
        emit_progress(f"applying {len(edits)} edits to file…", "match")

    async def _guarded_edit() -> dict[str, Any]:
        _enforce_not_protected(path_str)
        await file_guard.enforce_write(path=path_str, caller="ai_coding.tool.edit")

        def _do_edit() -> dict[str, Any]:
            path = Path(path_str)
            if not path.exists():
                raise ToolError(f"edit: file not found: {path_str}")
            try:
                # Read RAW bytes and decode WITHOUT newline translation so
                # ``detect_line_ending`` sees the file's true EOL. (The prior
                # ``read_text`` translated CRLF→LF on read, then relied on
                # ``write_text`` re-translating LF→os.linesep on write — which
                # accidentally preserved CRLF on Windows but would CORRUPT a
                # genuine LF file into CRLF on Windows. Reading the real bytes
                # makes EOL preservation correct + platform-neutral, paired
                # with the verbatim atomic byte write.)
                raw_bytes = path.read_bytes()
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"edit: cannot read file: {e}") from e

            # Staleness gate FIRST: before any matching, any rewriting and
            # any disk write, so a rejected edit leaves the file untouched.
            # Re-uses the bytes just read — no second full pass over the file.
            #
            # The expectation comes from the CONVERSATION'S OWN read history
            # when the caller did not supply one. Requiring the model to pass
            # the tag back made the guard opt-in, and it was simply never
            # opted into: across four measured test rounds ``read`` printed the
            # tag and ``edit`` was still called without it, so no check ran and
            # a concurrent writer's work was silently overwritable. Whoever
            # read the file in this conversation already told us what they saw;
            # holding them to it needs no cooperation.
            #
            # An explicit ``expect_snapshot`` still wins, so a caller that
            # tracks tags itself keeps full control.
            expectation = expect_snapshot
            from_caller = expectation is not None
            if expectation is None:
                expectation = get_seen(path)
            if expectation is not None:
                verify_snapshot(
                    path,
                    expectation,
                    data=raw_bytes,
                    tool="edit",
                    from_caller=from_caller,
                )

            try:
                raw = raw_bytes.decode("utf-8")
            except UnicodeDecodeError as e:
                raise ToolError(
                    f"edit: file is not valid UTF-8: {e}"
                ) from e

            # Match against the RAW text and commit it byte-exactly. The
            # previous normalise-everything / restore-everything round-trip
            # reflowed files that mix endings: ``detect_line_ending`` calls a
            # file CRLF as soon as ONE ``\r\n`` occurs, so restoring rewrote
            # every lone ``\n`` too and a one-token rename came back with the
            # whole file's endings changed (measured: 2 CRLF in, 4 CRLF out).
            # It also handed ``safe_commit_text`` an "original" that did not
            # match the file, so the .edit_trash backup could not undo it.
            #
            # Normalising is not needed for MATCHING: ``replace_block``'s
            # ``line_trim`` layer already lines a model's ``\n`` oldText up
            # against CRLF content (measured). Only the INSERTED text needs
            # converting, so it adopts the file's convention instead of
            # smuggling the model's endings in — done per edit below.
            line_ending = detect_line_ending(raw)
            content = raw

            applied = 0
            for i, edit in enumerate(edits):
                if not isinstance(edit, dict):
                    raise ToolError(f"edit: edits[{i}] must be a dict")
                old_text = edit.get("oldText", "")
                new_text = edit.get("newText", "")
                replace_all = bool(edit.get("replaceAll", False))
                if not isinstance(old_text, str) or not old_text:
                    raise ToolError(f"edit: edits[{i}].oldText is empty")
                if not isinstance(new_text, str):
                    raise ToolError(f"edit: edits[{i}].newText must be string")
                # BOTH sides adopt the file's ending. Converting only the new
                # text is not enough: a model's "\n" oldText then misses the
                # byte-faithful ``exact`` layer and lands on ``line_trim``,
                # which reassembles the region line by line and appends its OWN
                # separator — re-emitting the replacement with "\n" and undoing
                # the conversion (measured: 'Q\r\n' came back as 'Q\n').
                # Matching on the file's own ending keeps ``exact`` in play,
                # and ``line_trim`` still covers indentation drift.
                old_norm = restore_line_ending(
                    normalize_newlines(old_text), line_ending
                )
                new_norm = restore_line_ending(
                    normalize_newlines(new_text), line_ending
                )
                # Compared NORMALISED on both sides: both now carry the file's
                # ending, so an unchanged edit must not be judged different by
                # a "\r" alone.
                if normalize_newlines(old_text) == normalize_newlines(new_text):
                    raise ToolError(
                        f"edit: edits[{i}].oldText and newText are identical "
                        "(no change)."
                    )
                len_before = len(content)
                try:
                    result = replace_block(
                        content, old_norm, new_norm, replace_all=replace_all
                    )
                except EditMatchError as e:
                    raise ToolError(f"edit: edits[{i}].{e.message}") from e

                # CONSERVATION GUARD (pure arithmetic, pre-write — the cheapest
                # and most precise defence; catches the 2162→26 truncation
                # before any disk write). A deterministic oldText→newText
                # replacement removes ``result.consumed`` chars of original
                # content and inserts ``len(new) * replacements`` chars, so the
                # resulting length is EXACTLY known. If the actual length does
                # not match, the replacement mislanded / truncated → abort the
                # whole batch (all-or-nothing) WITHOUT writing.
                expected_len = (
                    len_before
                    - result.consumed
                    + len(new_norm) * result.replacements
                )
                if len(result.content) != expected_len:
                    raise ToolError(
                        f"edit: edits[{i}] produced an unexpected content size "
                        f"(got {len(result.content)} chars, conservation "
                        f"predicted {expected_len}: before={len_before}, "
                        f"consumed={result.consumed}, "
                        f"inserted={len(new_norm)}x{result.replacements} via "
                        f"strategy {result.strategy!r}). The replacement did "
                        "not land deterministically; refusing to write to "
                        "avoid corrupting the file."
                    )
                content = result.content
                applied += 1

            try:
                safe_commit_text(
                    path=path,
                    new_text=content,
                    original_text=raw,
                    # ``"\n"`` makes ``restore_line_ending`` a no-op, so what
                    # was matched is what lands: ``content`` already holds the
                    # file's own endings and the backup is the TRUE original.
                    line_ending="\n",
                    workspace_root=_workspace_root_for(path),
                    tool="edit",
                    edits=applied,
                    meta={"mode": "edit"},
                    restore_line_ending=restore_line_ending,
                )
            except SafeWriteError as e:
                raise ToolError(f"edit: {e}") from e
            except ToolError:
                raise
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"edit: cannot write file: {e}") from e
            # Hand back the POST-write tag so a follow-up edit can chain
            # straight on without a re-read (the file we just wrote is still
            # in the OS cache, so this pass is nearly free).
            #
            # The tracked expectation is ADVANCED to it, not dropped: our own
            # successful write is not a foreign change, and leaving the old tag
            # behind would make the very next edit in this conversation fail
            # against a file only we had touched.
            fresh = compute_snapshot(path)
            record_seen(path, fresh)
            return _ok(
                f"Successfully applied {applied} edit(s) to {path_str}",
                path=path_str,
                edits_applied=applied,
                snapshot=fresh,
            )

        return await asyncio.to_thread(_do_edit)

    # PARALLEL-TOOL-1: serialise concurrent edits to the SAME file (per-path
    # lock); different files still run in parallel. No lock wired → unchanged.
    if path_lock is not None:
        async with path_lock.lock(path_str):
            return await _guarded_edit()
    return await _guarded_edit()
