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
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    CODE_EXTENSIONS,
    _format_truncation_notice,
    _ok,
    expand_skill_placeholders,
    get_tool_output_thresholds,
    get_workspace_base,
    is_under_tool_result_store_root,
    make_line_truncated_suffix,
    resolve_under_workspace,
)
from qai.platform import protected_paths
from qai.platform.scheduling.path_locks import PathLockManager


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
        return await asyncio.to_thread(_read_file_slice, path_str, offset, limit)

    await file_guard.enforce_project_access(path=path_str, operation="read")
    await file_guard.enforce_read(path=path_str, caller="ai_coding.tool.read")

    return await asyncio.to_thread(_read_file_slice, path_str, offset, limit)


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
    it stops reading. To preserve the exact ``total_lines`` for files that fit
    (the common case — small files never hit a byte cap), the loop keeps
    COUNTING lines past the in-prompt window (without storing them) until EOF or
    the byte budget; a file large enough to trip the byte cap stops early, so
    ``total_lines`` then reflects "lines read so far" (the file is too big to
    enumerate fully without the OOM we are guarding against).
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

    start_idx = max(0, offset - 1)
    # The maximum number of lines we are willing to MATERIALISE in the window
    # (user ``limit`` is the tighter of the two; the line cap always applies).
    window_cap = max_lines if limit is None else min(limit, max_lines)

    selected: list[str] = []  # the materialised window (kept-EOL lines)
    total_lines = 0  # lines SEEN so far (exact unless the byte budget stops us)
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
                    total_lines += 1
                    idx = total_lines - 1  # 0-based index of this line
                    if idx < start_idx:
                        continue  # before the window — count only
                    if len(selected) >= window_cap:
                        # Window full. Record WHY (user limit vs line cap) but
                        # keep counting lines for an exact total_lines.
                        if limit is not None and window_cap == limit:
                            user_limit_applied = True
                        else:
                            line_cap_hit = True
                        continue

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
                        total_lines -= 1  # this line was not counted as seen
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

    if start_idx >= total_lines and not selected:
        return _ok(
            f"(file has {total_lines} lines; offset {offset} is beyond "
            f"end of file)",
            content="",
            total_lines=total_lines,
            start_line=offset,
            end_line=offset,
            truncated=False,
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

    # 7-L2: SKILL.md files use path placeholders so bundled asset / sibling
    # sub-SKILL references resolve when the file is read on demand (not via the
    # system-prompt injection path). ``${SKILL_DIR}`` → the skill's own dir;
    # ``${APP_ROOT}`` → the install/repo root (bound per-request at the DI
    # ToolPort boundary). Unavailable placeholders are left verbatim.
    if path.name == "SKILL.md" and "${" in body:
        body = expand_skill_placeholders(body, skill_dir=str(path.parent))

    notice = (
        _format_truncation_notice(
            start_line=start_line,
            end_line=end_line,
            total_lines=total_lines,
            reason=truncated_reason,
        )
        if truncated_reason
        else ""
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

    return _ok(
        f"read {path_str} ({end_line - start_line + 1} lines)",
        content=content,
        total_lines=total_lines,
        start_line=start_line,
        end_line=end_line,
        truncated=bool(
            truncated_reason and not truncated_reason.startswith("user limit")
        ),
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

    await file_guard.enforce_project_access(path=path_str, operation="read")
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

    async def _guarded_write() -> dict[str, Any]:
        _enforce_not_protected(path_str)
        await file_guard.enforce_project_access(path=path_str, operation="write")
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
                    line_ending = detect_line_ending(raw)
                    original_norm = normalize_newlines(raw)
                    new_norm = normalize_newlines(content)
                    safe_commit_text(
                        path=path,
                        new_text=new_norm,
                        original_text=original_norm,
                        line_ending=line_ending,
                        workspace_root=_workspace_root_for(path),
                        tool="write",
                        edits=0,
                        meta={"mode": "overwrite"},
                        restore_line_ending=restore_line_ending,
                    )
                    expected_bytes = restore_line_ending(
                        new_norm, line_ending
                    ).encode("utf-8")
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

    async def _guarded_edit() -> dict[str, Any]:
        _enforce_not_protected(path_str)
        await file_guard.enforce_project_access(path=path_str, operation="edit")
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
                raw = path.read_bytes().decode("utf-8")
            except UnicodeDecodeError as e:
                raise ToolError(
                    f"edit: file is not valid UTF-8: {e}"
                ) from e
            except Exception as e:  # noqa: BLE001
                raise ToolError(f"edit: cannot read file: {e}") from e

            # Match against newline-normalised content so a model emitting "\n"
            # edits against a CRLF file still matches; restore the file's own
            # line ending on write so the file's EOL convention is preserved.
            line_ending = detect_line_ending(raw)
            content = normalize_newlines(raw)

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
                old_norm = normalize_newlines(old_text)
                new_norm = normalize_newlines(new_text)
                if old_norm == new_norm:
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
                    original_text=normalize_newlines(raw),
                    line_ending=line_ending,
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
            return _ok(
                f"Successfully applied {applied} edit(s) to {path_str}",
                path=path_str,
                edits_applied=applied,
            )

        return await asyncio.to_thread(_do_edit)

    # PARALLEL-TOOL-1: serialise concurrent edits to the SAME file (per-path
    # lock); different files still run in parallel. No lock wired → unchanged.
    if path_lock is not None:
        async with path_lock.lock(path_str):
            return await _guarded_edit()
    return await _guarded_edit()
