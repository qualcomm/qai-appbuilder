# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""``ast_grep`` tool handler — read-only STRUCTURAL (AST) code search.

Why a second search tool at all? ``grep`` matches TEXT, so it cannot tell a
real call ``foo(1)`` apart from the same characters sitting inside a string
literal, a comment or a doc-string. That produces false positives on exactly
the questions a coding agent asks most ("where is this function actually
called?"), and the model then reads files that never used the symbol. This
tool delegates to the ``ast-grep`` CLI, which parses each file with
tree-sitter and matches on the syntax tree, so text-in-a-comment simply is
not a match.

Backend: the ``ast-grep`` (or, on Windows, ``sg``) EXECUTABLE, run as a
streamed async subprocess with ``--json=stream`` (one JSON object per stdout
line). There is no Python binding involved: ``ast-grep-py`` publishes no
``win_arm64`` wheel, and this project must work on arm64 Windows, so the
standalone CLI binary is the only portable option.

Discovery lives in :mod:`._ast_grep_binary` (shared with ``ast_edit``): PATH
first, then the arch-matching copy this repo bundles at
``vendor/bin/<arch>/ast-grep.exe`` — the ``guard64.dll`` layout. Nothing is
ever downloaded. When neither exists the call raises :class:`ToolError` naming
the install options — it NEVER returns an empty match list, because "no
matches" and "the search never ran" are opposite facts, and conflating them
would make the model conclude a symbol is unused.

Subprocess lifecycle is the ``_grep_with_ripgrep`` pattern reused verbatim
(see :mod:`...handlers.search`): streamed reads with a hard byte ceiling, a
wall-clock timeout, and a SHIELDED whole-process-tree kill in ``finally`` so
neither a cancel nor a double-cancel can orphan the child.

Writing / rewriting is deliberately out of scope: this handler never passes
``-r`` / ``-U`` to the CLI, so it cannot modify a file. Structural rewrites
belong to the separate ``ast_edit`` tool.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from qai.ai_coding.application.ports import FileGuardPort, ToolResultStorePort
from qai.ai_coding.infrastructure.tools.errors import ToolError
from qai.ai_coding.infrastructure.tools.handlers._ast_grep_binary import (
    INSTALL_HINT,
    missing_binary_message,
    resolve_ast_grep_binary,
)
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    GREP_MAX_SCAN_BYTES,
    _ok,
    default_cwd,
    get_tool_output_thresholds,
    resolve_under_workspace,
)

# Reused as-is from the ripgrep track — both are "spawn a search binary and be
# sure it is dead afterwards". Duplicating them would mean two copies of the
# platform-specific process-tree kill, which is precisely the code that must
# not drift.
from qai.ai_coding.infrastructure.tools.handlers.search import (
    _maybe_store_full_result,
    _spawn_rg as _spawn_search_binary,
    _split_targets,
    _terminate_process_tree_shielded,
)

#: Hard wall-clock ceiling for one ``ast-grep`` invocation. Parsing is much
#: more expensive per byte than ripgrep's regex scan, so a big tree can take a
#: while legitimately; beyond this the tree is killed and the call fails loudly.
_AST_TIMEOUT_SECONDS = 120.0

#: Grace period after a graceful terminate before escalating to a force kill.
#: Mirrors the ripgrep track so both binaries get the same treatment.
_AST_FORCE_KILL_AFTER_SECONDS = 3.0

#: Streaming ceiling on captured stdout. Same idea as
#: :data:`GREP_MAX_SCAN_BYTES` — stop reading and kill the child rather than
#: growing unbounded — but tightened, because ``--json=stream`` emits a fat
#: object (matched text + full display lines + every meta-variable capture) per
#: hit, so equivalent information costs far more bytes here than a
#: ``path:line:text`` line does.
AST_GREP_MAX_SCAN_BYTES = min(GREP_MAX_SCAN_BYTES, 32 * 1024 * 1024)

#: Max matches rendered in-prompt. Beyond this the COMPLETE rendered set is
#: persisted via the result store and the message points at ``read(path=...)``.
MAX_INPROMPT_MATCHES = 200

#: Per-match text cap for the rendered preview. A structural match can be a
#: whole class body; the preview only needs enough to recognise the hit.
MAX_MATCH_TEXT_CHARS = 300

#: ``--strictness`` values accepted by the CLI. Validated here so a typo comes
#: back as a tool error naming the legal set instead of an opaque exit code 2.
_STRICTNESS_VALUES: frozenset[str] = frozenset(
    {"cst", "smart", "ast", "relaxed", "signature"}
)


def _require_ast_grep() -> str:
    """Return the executable path or raise a ToolError that says what to do.

    This is the single most important behaviour in this module: an unavailable
    backend MUST NOT look like a successful search with zero hits.
    """
    found = resolve_ast_grep_binary()
    if found is None:
        raise ToolError(missing_binary_message("ast_grep"))
    return found


def _build_command(
    executable: str,
    *,
    pattern: str,
    lang: str | None,
    globs: list[str],
    context_lines: int,
    strictness: str | None,
    targets: list[Path],
) -> list[str]:
    """Assemble the ``ast-grep run`` argv (search only — never ``-r`` / ``-U``)."""
    cmd: list[str] = [executable, "run", "--json=stream", "-p", pattern]
    if lang:
        cmd.extend(["-l", lang])
    if strictness:
        cmd.extend(["--strictness", strictness])
    if context_lines > 0:
        cmd.extend(["-C", str(context_lines)])
    for glob in globs:
        cmd.extend(["--globs", glob])
    cmd.extend(str(t) for t in targets)
    return cmd


def _parse_ast_grep_records(stdout_text: str) -> list[dict[str, Any]]:
    """Parse ``--json=stream`` stdout into ordered, flat match records.

    One JSON object per line. A trailing line may be PARTIAL when the
    streaming cap chopped stdout mid-object; such a line simply fails to parse
    and is skipped, so every complete record before the cut survives (the same
    tolerance the ripgrep track relies on).

    Line/column numbers in the CLI's ``range`` are ZERO-based; they are
    converted to the 1-based convention every other tool here reports.
    """
    records: list[dict[str, Any]] = []
    for raw_line in stdout_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        file_path = obj.get("file")
        rng = obj.get("range")
        if not isinstance(file_path, str) or not isinstance(rng, dict):
            continue
        start = rng.get("start")
        end = rng.get("end")
        if not isinstance(start, dict) or not isinstance(end, dict):
            continue
        start_line = start.get("line")
        end_line = end.get("line")
        column = start.get("column")
        if not isinstance(start_line, int) or not isinstance(end_line, int):
            continue
        record: dict[str, Any] = {
            "path": file_path,
            "line": start_line + 1,
            "end_line": end_line + 1,
            "column": (column + 1) if isinstance(column, int) else 1,
            "text": obj.get("text") if isinstance(obj.get("text"), str) else "",
        }
        language = obj.get("language")
        if isinstance(language, str):
            record["language"] = language
        captures = _extract_captures(obj.get("metaVariables"))
        if captures:
            record["captures"] = captures
        records.append(record)
    return records


def _extract_captures(meta: Any) -> dict[str, str]:
    """Flatten ``metaVariables`` into ``{name: matched text}``.

    ``single`` holds one node per ``$NAME``; ``multi`` holds the node list a
    ``$$$NAME`` absorbed (joined back with a space, which is what the model
    wants to read); ``transformed`` holds already-computed strings. Names
    starting with ``_`` are the CLI's own unbound placeholders (``$_`` /
    ``$$$``) and carry no information for the caller, so they are dropped.
    """
    if not isinstance(meta, dict):
        return {}
    out: dict[str, str] = {}
    single = meta.get("single")
    if isinstance(single, dict):
        for name, node in single.items():
            if isinstance(node, dict) and isinstance(node.get("text"), str):
                out[str(name)] = node["text"]
    multi = meta.get("multi")
    if isinstance(multi, dict):
        for name, nodes in multi.items():
            if not isinstance(nodes, list):
                continue
            texts = [
                n["text"]
                for n in nodes
                if isinstance(n, dict) and isinstance(n.get("text"), str)
            ]
            # A ``$$$NAME`` legitimately matches ZERO nodes (``f()`` against
            # ``f($$$ARGS)``). Reporting ``ARGS: ""`` there reads like a
            # captured empty string; omitting the key says "absorbed nothing",
            # which is what actually happened.
            if texts:
                out[str(name)] = " ".join(texts)
    transformed = meta.get("transformed")
    if isinstance(transformed, dict):
        for name, value in transformed.items():
            if isinstance(value, str):
                out[str(name)] = value
    return {k: v for k, v in out.items() if not k.startswith("_")}


def _flatten(text: str) -> str:
    """Collapse a (possibly multi-line) match to one readable, capped line."""
    joined = " ".join(part.strip() for part in text.splitlines() if part.strip())
    if len(joined) > MAX_MATCH_TEXT_CHARS:
        return joined[:MAX_MATCH_TEXT_CHARS] + "…"
    return joined


def _render(records: list[dict[str, Any]]) -> str:
    """Render records as one ``path:line:col: text`` line per match.

    A multi-line match reports its span as ``path:start-end`` so the model can
    see it is a block, and any meta-variable captures are appended — those are
    usually the answer to the question that motivated the search.
    """
    lines: list[str] = []
    for rec in records:
        if rec["end_line"] != rec["line"]:
            locus = f"{rec['path']}:{rec['line']}-{rec['end_line']}:{rec['column']}"
        else:
            locus = f"{rec['path']}:{rec['line']}:{rec['column']}"
        entry = f"{locus}: {_flatten(rec['text'])}"
        captures = rec.get("captures")
        if captures:
            rendered = ", ".join(
                f"${name}={_flatten(value)}" for name, value in captures.items()
            )
            entry += f"    [{rendered}]"
        lines.append(entry)
    return "\n".join(lines)


def _validate_args(
    args: dict[str, Any],
) -> tuple[str, str | None, list[str], int, str | None]:
    """Validate the wire arguments; returns the normalised search parameters."""
    pattern = args.get("pattern")
    if not isinstance(pattern, str) or not pattern.strip():
        raise ToolError("ast_grep: 'pattern' is required and must be a string")

    lang = args.get("lang")
    if lang is not None and not isinstance(lang, str):
        raise ToolError("ast_grep: 'lang' must be a string")
    lang = lang.strip() if isinstance(lang, str) and lang.strip() else None

    raw_globs = args.get("globs")
    if raw_globs is not None and not isinstance(raw_globs, str):
        raise ToolError("ast_grep: 'globs' must be a string")
    globs = _split_targets(raw_globs or "")

    raw_context = args.get("context_lines", 0)
    if isinstance(raw_context, bool) or not isinstance(raw_context, (int, float)):
        raise ToolError("ast_grep: 'context_lines' must be a number")
    context_lines = max(0, min(int(raw_context), 20))

    strictness = args.get("strictness")
    if strictness is not None:
        if not isinstance(strictness, str):
            raise ToolError("ast_grep: 'strictness' must be a string")
        strictness = strictness.strip().lower()
        if strictness and strictness not in _STRICTNESS_VALUES:
            raise ToolError(
                f"ast_grep: unknown strictness {strictness!r}; expected one of "
                + ", ".join(sorted(_STRICTNESS_VALUES))
            )
        strictness = strictness or None

    return pattern, lang, globs, context_lines, strictness


async def _resolve_targets(
    entries: list[str],
) -> tuple[list[Path], list[str]]:
    """Resolve every entry under the workspace, splitting existing from missing.

    One typo'd entry must not discard the good ones — with several targets that
    would hide real matches behind a bad path (the ``grep`` convention).
    """
    targets: list[Path] = []
    missing: list[str] = []
    for entry in entries:
        resolved = Path(resolve_under_workspace(entry))
        if await asyncio.to_thread(resolved.exists):
            targets.append(resolved)
        else:
            missing.append(entry)
    return targets, missing


async def _run_ast_grep(cmd: list[str], *, cwd: str) -> tuple[str, str, bool]:
    """Run the CLI and return ``(stdout, stderr, scan_capped)``.

    Lifecycle copied from ``_grep_with_ripgrep``: stdout is consumed
    incrementally and the read stops once :data:`AST_GREP_MAX_SCAN_BYTES` has
    been captured (child killed, ``scan_capped=True``, partial-but-real matches
    kept). A timeout or any cancel kills the WHOLE process tree from a shielded
    ``finally``, so no orphan survives. Unlike the grep track there is no
    pure-Python fallback to degrade into, so every failure raises instead of
    returning ``None``.

    The exit code follows the ripgrep convention (0 = matches, 1 = no matches,
    >1 = error), so only ``>1`` is an error — reported with the child's stderr,
    where an unknown language explains itself. The exit code is NOT sufficient
    on its own: an unparseable PATTERN exits 0 with empty stdout and only a
    stderr warning, so stderr is inspected too (see below).
    """
    proc = await _spawn_search_binary(cmd, cwd=cwd)
    if proc is None:
        raise ToolError(
            "ast_grep: found the 'ast-grep' executable but could not start it "
            f"(spawn failed for: {' '.join(cmd[:2])}). Verify the binary runs: "
            "`ast-grep --version`. " + INSTALL_HINT
        )

    assert proc.stdout is not None
    assert proc.stderr is not None
    chunks: list[bytes] = []
    err_chunks: list[bytes] = []
    collected = 0
    scan_capped = False
    return_code: int | None = None
    timed_out = False
    try:

        async def _collect() -> None:
            nonlocal collected, scan_capped
            while True:
                chunk = await proc.stdout.read(65536)  # type: ignore[union-attr]
                if not chunk:
                    break
                chunks.append(chunk)
                collected += len(chunk)
                if collected >= AST_GREP_MAX_SCAN_BYTES:
                    scan_capped = True
                    return

        async def _collect_err() -> None:
            # Drained concurrently: ast-grep writes parse/pattern diagnostics
            # to stderr, and an unread pipe can fill and DEADLOCK the child
            # while we are still waiting on stdout.
            while True:
                chunk = await proc.stderr.read(65536)  # type: ignore[union-attr]
                if not chunk:
                    break
                if len(err_chunks) < 64:
                    err_chunks.append(chunk)

        err_task = asyncio.ensure_future(_collect_err())
        try:
            await asyncio.wait_for(_collect(), timeout=_AST_TIMEOUT_SECONDS)
            if not scan_capped:
                try:
                    return_code = await asyncio.wait_for(
                        proc.wait(), timeout=_AST_FORCE_KILL_AFTER_SECONDS
                    )
                except asyncio.TimeoutError:
                    timed_out = True
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            err_task.cancel()
            try:
                await err_task
            except (asyncio.CancelledError, OSError):
                pass
    except asyncio.CancelledError:
        raise
    finally:
        # Shielded so a double-cancel cannot interrupt the kill and orphan the
        # child (and its tree-sitter worker threads/processes).
        await _terminate_process_tree_shielded(proc)

    if timed_out:
        raise ToolError(
            f"ast_grep: search exceeded {_AST_TIMEOUT_SECONDS:.0f}s and was "
            "stopped, so the result is unknown (NOT 'no matches'). Narrow the "
            "scope: pass a specific sub-directory in 'paths', add 'globs' to "
            "restrict the files, or set 'lang' so fewer files are parsed."
        )

    stdout_text = b"".join(chunks).decode("utf-8", errors="replace")
    stderr_text = b"".join(err_chunks).decode("utf-8", errors="replace").strip()

    if return_code is not None and return_code > 1:
        detail = stderr_text or "(no diagnostic on stderr)"
        raise ToolError(
            f"ast_grep: ast-grep exited with code {return_code} and did NOT "
            f"search — {detail[:2000]}. Check the pattern (metavariables are "
            "$NAME for one node, $_ for one unnamed node, $$$NAME for zero or "
            "more) and that 'lang' names a language ast-grep supports."
        )

    # A pattern that does not parse is NOT reported through the exit code:
    # ast-grep exits 0, prints nothing to stdout, and only warns "Pattern
    # contains an ERROR node" on stderr (verified with 0.45.1: 'compute($$$'
    # → rc=0, 0 hits, warning; ')))) ' likewise). So an unbalanced-paren typo
    # is byte-for-byte indistinguishable from "this construct does not exist"
    # — the single most misleading outcome this tool could produce, since the
    # model would conclude the code is clean. Fail loudly instead.
    if "ERROR node" in stderr_text:
        raise ToolError(
            "ast_grep: the pattern did NOT parse as valid syntax, so the "
            "search result is meaningless (ast-grep reports zero matches for "
            "an unparseable pattern — that is NOT evidence the code lacks "
            f"this construct). ast-grep said: {stderr_text[:500]}. A pattern "
            "must be a COMPLETE syntax node with balanced brackets: use "
            "'foo($$$ARGS)', not 'foo($$$'. Metavariables are $NAME for one "
            "node, $_ for one unnamed node, $$$NAME for zero or more. If the "
            "snippet is only valid in a specific language, pass 'lang'."
        )

    return stdout_text, stderr_text, scan_capped


async def tool_ast_grep(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
    tool_result_store: ToolResultStorePort | None = None,
) -> dict[str, Any]:
    """Search ``paths`` for a STRUCTURAL pattern via the ``ast-grep`` CLI."""
    pattern, lang, globs, context_lines, strictness = _validate_args(args)

    raw_paths = args.get("paths")
    if raw_paths is not None and not isinstance(raw_paths, str):
        raise ToolError("ast_grep: 'paths' must be a string")

    # Availability is checked BEFORE anything else observable, so a missing
    # binary can never be mistaken for an exhausted search.
    executable = _require_ast_grep()

    entries = _split_targets(raw_paths or "")
    if not entries:
        entries = [str(default_cwd() or Path.cwd())]

    targets, missing = await _resolve_targets(entries)
    if not targets:
        raise ToolError(
            "ast_grep: no target path exists ("
            + ", ".join(missing)
            + "), so NOTHING was searched — this is NOT a 'no matches' "
            "result. Pass an existing file or directory (several may be "
            "separated with ';')."
        )

    for target in targets:
        await file_guard.enforce_read(
            path=str(target), caller="ai_coding.tool.ast_grep"
        )

    from qai.platform.tool_progress import emit_progress

    emit_progress(f"ast_grep “{pattern[:60]}”…", "match")

    cmd = _build_command(
        executable,
        pattern=pattern,
        lang=lang,
        globs=globs,
        context_lines=context_lines,
        strictness=strictness,
        targets=targets,
    )
    cwd = str(default_cwd() or Path.cwd())
    stdout_text, stderr_text, scan_capped = await _run_ast_grep(cmd, cwd=cwd)

    records = _parse_ast_grep_records(stdout_text)
    match_count = len(records)
    file_count = len({rec["path"] for rec in records})

    if not records:
        return _build_empty_result(
            pattern=pattern,
            lang=lang,
            entries=entries,
            missing=missing,
            stderr_text=stderr_text,
        )

    visible = records[:MAX_INPROMPT_MATCHES]
    count_capped = len(records) > len(visible)
    rendered_all = _render(records)
    rendered_visible = _render(visible)

    max_output_bytes = get_tool_output_thresholds().grep_max_output_bytes
    cap_kb = max_output_bytes // 1024
    encoded = rendered_visible.encode("utf-8")
    byte_capped = len(encoded) > max_output_bytes
    output = (
        encoded[:max_output_bytes].decode("utf-8", errors="replace")
        if byte_capped
        else rendered_visible
    )

    # Persist the COMPLETE rendered match set (not the raw JSON, which is
    # unreadable when read back) whenever anything was elided, so the model has
    # a real retrieval path instead of a silently shortened list. ``force``
    # because the driver may be a match COUNT whose bytes sit under the store
    # threshold.
    stored_path: str | None = None
    if count_capped or byte_capped or scan_capped:
        stored_path = _maybe_store_full_result(
            rendered_all,
            tool_name="ast_grep",
            store=tool_result_store,
            force=True,
        )

    message = _build_message(
        pattern=pattern,
        lang=lang,
        match_count=match_count,
        file_count=file_count,
        shown=len(visible),
        count_capped=count_capped,
        byte_capped=byte_capped,
        cap_kb=cap_kb,
        scan_capped=scan_capped,
        stored_path=stored_path,
        missing=missing,
    )

    result = _ok(
        message,
        matches=visible,
        match_count=match_count,
        file_count=file_count,
        pattern=pattern,
        output=output,
        truncated=count_capped or byte_capped or scan_capped,
        incomplete=scan_capped,
        backend="ast-grep",
    )
    if lang:
        result["lang"] = lang
    if missing:
        result["missing_paths"] = missing
    if stored_path is not None:
        result["stored_path"] = stored_path
    return result


def _build_empty_result(
    *,
    pattern: str,
    lang: str | None,
    entries: list[str],
    missing: list[str],
    stderr_text: str,
) -> dict[str, Any]:
    """Shape the genuine "searched, found nothing" answer.

    Only reachable after the CLI ran and exited 0/1, so zero matches here is a
    FACT about the code. The message says so explicitly, and names the two
    things that most often make a valid-looking structural pattern miss: the
    pattern being matched at the wrong granularity, and files skipped because
    their extension maps to no language ast-grep knows (or to a language other
    than the one ``lang`` restricted the search to).
    """
    scope = "; ".join(entries)
    msg = (
        f"(no structural matches for pattern {pattern!r} in {scope} — the "
        "search DID run and completed)"
    )
    notes: list[str] = []
    if lang:
        # ``-l`` is a FILTER, not a grammar override: files whose extension
        # maps to a different language are skipped entirely. That is the most
        # common cause of "my pattern is right but I get nothing".
        notes.append(
            f"'lang' was set to {lang!r}, which SKIPS every file whose "
            "extension is not that language — drop it to search each file "
            "under the language its extension implies"
        )
    else:
        notes.append(
            "no 'lang' given, so a language was inferred per file extension "
            "and files with an unrecognised extension were skipped entirely "
            "(ast-grep matches by extension; 'lang' filters, it cannot force "
            "a grammar onto an unknown extension)"
        )
    notes.append(
        "a structural pattern must be a complete syntax node: try a looser "
        "shape such as '$FUNC($$$ARGS)' (metavariables: $NAME one node, $_ "
        "one unnamed node, $$$NAME zero or more)"
    )
    if missing:
        notes.append(f"skipped non-existent path(s): {', '.join(missing)}")
    if stderr_text:
        notes.append(f"ast-grep stderr: {stderr_text[:500]}")
    result = _ok(
        msg + " — " + "; ".join(notes),
        matches=[],
        match_count=0,
        file_count=0,
        pattern=pattern,
        output="",
        truncated=False,
        incomplete=False,
        backend="ast-grep",
    )
    if lang:
        result["lang"] = lang
    if missing:
        result["missing_paths"] = missing
    return result


def _build_message(
    *,
    pattern: str,
    lang: str | None,
    match_count: int,
    file_count: int,
    shown: int,
    count_capped: bool,
    byte_capped: bool,
    cap_kb: int,
    scan_capped: bool,
    stored_path: str | None,
    missing: list[str],
) -> str:
    """Summary line plus every caveat needed to read the result correctly."""
    if scan_capped:
        # The child was KILLED at the streaming ceiling, so it never finished:
        # the counts are "what we saw before stopping", NOT totals. Saying this
        # plainly is what stops the model from treating a partial sample as an
        # exhaustive answer.
        head = (
            f"INCOMPLETE: too much output — ast-grep was stopped after "
            f"~{AST_GREP_MAX_SCAN_BYTES // (1024 * 1024)}MB WITHOUT finishing "
            f"the search. {match_count} structural match(es) in {file_count} "
            f"file(s) SO FAR (a PARTIAL, non-exhaustive sample; do NOT treat "
            f"these as totals). Narrow 'paths' or add 'globs' for a complete "
            f"search."
        )
    else:
        head = (
            f"{match_count} structural match(es) for {pattern!r} in "
            f"{file_count} file(s)"
        )
        if lang:
            head += f" [lang={lang}]"
    notes: list[str] = []
    if count_capped:
        notes.append(
            f"showing the first {shown} of {match_count} — this list is a "
            "PARTIAL view"
        )
    if byte_capped:
        notes.append(f"'output' text cut at {cap_kb}KB")
    if stored_path is not None:
        notes.append(
            f"the COMPLETE match list was saved — call read(path={stored_path}) "
            "to see all of it"
        )
    elif count_capped or byte_capped or scan_capped:
        notes.append(
            "the elided matches were NOT saved (no result store wired); "
            "re-run against a narrower 'paths' to see them"
        )
    if missing:
        notes.append(f"skipped non-existent path(s): {', '.join(missing)}")
    if not notes:
        return head + " [backend: ast-grep]"
    return head + " — " + "; ".join(notes) + " [backend: ast-grep]"
