# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""File-search tool handlers (``glob`` / ``grep``)."""

from __future__ import annotations

import asyncio
import base64
import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from qai.ai_coding.application.ports import FileGuardPort, ToolResultStorePort
from qai.ai_coding.infrastructure.tools.errors import ToolError
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    DEFAULT_SKIP_DIR_NAMES,
    GREP_MAX_SCAN_BYTES,
    WALK_MAX_ENTRIES,
    WalkBudget,
    _check_recursive_root_guard,
    _expand_braces,
    _ok,
    _walk_filtered,
    default_cwd,
    get_project_skip_dirs,
    get_tool_output_thresholds,
    make_glob_advice,
    make_grep_advice,
    make_line_truncated_suffix,
    resolve_under_workspace,
)

# ---------------------------------------------------------------------------
# glob
# ---------------------------------------------------------------------------


def _maybe_store_full_result(
    full_text: str,
    *,
    tool_name: str,
    store: ToolResultStorePort | None,
    force: bool = False,
) -> str | None:
    """Persist ``full_text`` via the store when oversized; return stored path.

    退化 #11 (subtask 3): glob/grep cap their in-prompt output (500 files /
    50 KB) to protect the context window, but that discards the rest with no
    retrieval path — the same gap ``exec`` had before the store was wired.
    When a :class:`ToolResultStorePort` is injected we persist the COMPLETE
    rendered result (every matched path / every match line) and hand back the
    saved path so the wire advice can point the model at
    ``read(path=<stored_path>)`` for the full set. Returns ``None`` when no
    store is wired or the full result is below the store threshold (small
    results need no retrieval path — behaviour unchanged).

    ``force=True`` persists regardless of the byte threshold — used when the
    decision to give the model a retrieval path is driven by a NON-byte
    signal (grep capping at a match *count* whose total bytes still sit below
    the store threshold). Without ``force`` such a result would discard the
    elided matches with no way to ``read`` them back.
    """
    if store is None or not full_text:
        return None
    preview = store.store(
        full_text, tool_name=tool_name, context_hint="full_results", force=force
    )
    return preview.stored_path if preview.stored else None


def _sort_by_mtime_desc(paths: list[Path]) -> list[Path]:
    """Return ``paths`` ordered newest-modified-first (ties broken by path).

    Runs blocking ``stat`` calls, so callers invoke it via
    ``asyncio.to_thread``. A path whose ``stat`` fails (race / permission)
    sorts as oldest (mtime 0) rather than dropping out, so a transient stat
    error never silently loses a match. A stable secondary key (the path
    string) keeps the order deterministic for equal mtimes.
    """
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0

    decorated = [(_mtime(p), str(p), p) for p in paths]
    decorated.sort(key=lambda t: (-t[0], t[1]))
    return [t[2] for t in decorated]


# ---------------------------------------------------------------------------
# ripgrep subprocess lifecycle (shared by the glob + grep ripgrep tracks)
#
# Running ``rg`` as a streamed async subprocess (rather than a blocking
# ``subprocess.run``) is what lets the search tools (a) stop consuming output
# the instant they have collected enough / hit a budget — so a pathologically
# deep tree no longer makes the tool wait for ``rg`` to finish enumerating the
# whole drive — and (b) react to an upstream cancel (user "Stop") immediately
# instead of blocking a worker thread for the full timeout.
#
# The other half of robustness is process CLEANUP: ``rg`` is multi-threaded and
# may spawn helpers, so killing only its direct PID can leave workers behind.
# :func:`_terminate_process_tree` kills the WHOLE tree (Windows ``taskkill /T``;
# POSIX process-group ``killpg``) with a SIGTERM→SIGKILL escalation ladder, and
# every run path (success, budget-stop, timeout, error, cancel) routes through
# it via ``try/finally`` so no ``rg`` is left orphaned.
# ---------------------------------------------------------------------------

# Hard wall-clock ceiling for a single ``rg`` invocation. A change to take
# effect only after a restart; generous enough that a normal project listing /
# search never trips it, but a pathological hang is bounded. On timeout the
# process tree is killed and the track returns ``None`` so the caller falls
# back to the pure-Python implementation.
_RG_TIMEOUT_SECONDS = 120.0
# Grace period after a graceful terminate before escalating to a force kill.
_RG_FORCE_KILL_AFTER_SECONDS = 3.0


async def _terminate_process_tree(
    proc: asyncio.subprocess.Process,
) -> None:
    """Kill ``proc`` AND its child process tree, escalating if it lingers.

    ``rg`` is multi-threaded and may spawn helper processes; killing only the
    direct PID can leave workers behind as orphans. This terminates the whole
    tree:

    * **Windows** — ``taskkill /PID <pid> /T /F``: ``/T`` walks the child tree,
      ``/F`` forces. ``taskkill`` is a built-in (no new dependency).
    * **POSIX** — ``os.killpg(os.getpgid(pid), SIGTERM)`` to signal the entire
      process group (the process is spawned with ``start_new_session=True`` so
      it leads its own group), then — if it has not exited within the grace
      period — ``os.killpg(..., SIGKILL)``.

    Best-effort and never raises: a process that already exited (``ProcessLookup
    Error`` / no such group) is treated as done. Safe to call more than once.
    """
    if proc.returncode is not None:
        return

    if sys.platform == "win32":
        await _terminate_tree_windows(proc)
    else:
        await _terminate_tree_posix(proc)


async def _terminate_tree_windows(
    proc: asyncio.subprocess.Process,
) -> None:
    """Windows process-tree kill via ``taskkill /T /F`` (force, whole tree)."""
    # taskkill /T kills the child tree; /F forces. Run it off the event loop so
    # we never block, and ignore failures (process may have just exited). This
    # is the force kill — there is no softer Windows tree signal, so the
    # SIGTERM→SIGKILL ladder collapses to a single step.
    def _taskkill() -> None:
        try:
            subprocess.run(  # fixed argv, no shell
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            pass

    await asyncio.to_thread(_taskkill)
    # Reap so the transport closes and no zombie lingers.
    try:
        await asyncio.wait_for(proc.wait(), timeout=_RG_FORCE_KILL_AFTER_SECONDS)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


async def _terminate_tree_posix(
    proc: asyncio.subprocess.Process,
) -> None:
    """POSIX process-group kill (SIGTERM, then SIGKILL after the grace period).

    The process is spawned with ``start_new_session=True`` so it leads its own
    group; signalling the group reaches ``rg`` + any helper it forked. Falls
    back to signalling just the process if the group lookup fails.
    """
    import signal as _signal

    def _killpg(sig: int) -> None:
        try:
            # POSIX-only APIs; this function is only reached on non-win32
            # (the caller branches on sys.platform). The win32-targeted type
            # checker does not know os.killpg / os.getpgid exist here.
            os.killpg(os.getpgid(proc.pid), sig)  # type: ignore[attr-defined,unused-ignore]
        except (ProcessLookupError, PermissionError, OSError):
            # Fall back to signalling just the process if the group lookup
            # failed (e.g. it was never made a session leader).
            try:
                proc.send_signal(sig)
            except (ProcessLookupError, OSError):
                pass

    _killpg(_signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), timeout=_RG_FORCE_KILL_AFTER_SECONDS)
        return
    except asyncio.TimeoutError:
        pass
    except ProcessLookupError:
        return
    # Still alive after the grace period — escalate to SIGKILL.
    _killpg(_signal.SIGKILL)  # type: ignore[attr-defined,unused-ignore]
    try:
        await asyncio.wait_for(proc.wait(), timeout=_RG_FORCE_KILL_AFTER_SECONDS)
    except (asyncio.TimeoutError, ProcessLookupError):
        pass


async def _terminate_process_tree_shielded(
    proc: asyncio.subprocess.Process,
) -> None:
    """Kill ``proc``'s tree, completing even if the awaiting task is cancelled.

    The teardown runs from the ``finally`` block of an ``async`` helper. A
    plain ``await _terminate_process_tree(proc)`` there is interrupted if a
    SECOND cancel is delivered while the ``finally`` is awaiting (e.g. the user
    hits Stop twice, or the framework re-cancels an already-cancelling task) —
    asyncio raises ``CancelledError`` straight through the in-flight kill and
    ``rg`` is left orphaned. (A single cancel / a :func:`asyncio.wait_for`
    timeout does NOT re-interrupt the ``finally`` await, so those paths are
    already safe — this guards the double-cancel edge.)

    We therefore run the kill as a SHIELDED child task: an outer cancel raises
    in *this* awaiter but the shielded kill keeps running. We then await the
    kill task to completion (so ``rg`` is provably dead before we return /
    re-raise) and re-raise the cancel to preserve cancellation semantics.
    """
    kill_task = asyncio.ensure_future(_terminate_process_tree(proc))
    try:
        await asyncio.shield(kill_task)
    except asyncio.CancelledError:
        # Outer cancel reached the shield; let the shielded kill finish in the
        # background so the process tree is still reaped, then propagate.
        try:
            await kill_task
        except asyncio.CancelledError:
            pass
        raise


async def _spawn_rg(
    cmd: list[str], *, cwd: str
) -> asyncio.subprocess.Process | None:
    """Spawn ``rg`` as a streamed async subprocess; ``None`` if unspawnable.

    On POSIX the child is started in a new session (``start_new_session=True``)
    so it leads its own process group and :func:`_terminate_process_tree` can
    signal the whole group. On Windows that flag does not apply; the tree is
    cleaned via ``taskkill /T`` instead. Returning ``None`` (rg missing / spawn
    failure) lets the caller fall back to the pure-Python track.
    """
    kwargs: dict[str, Any] = {
        "cwd": cwd,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "stdin": asyncio.subprocess.DEVNULL,
    }
    if sys.platform != "win32":
        # New session → the child is its own process-group leader, so a cancel
        # / timeout can kill the entire group (rg + any helpers) and not leave
        # orphans (AGENTS.md cross-platform: sys.platform-guarded, POSIX-only).
        kwargs["start_new_session"] = True
    try:
        return await asyncio.create_subprocess_exec(*cmd, **kwargs)
    except (FileNotFoundError, OSError, ValueError):
        return None


async def _glob_with_ripgrep(
    root: Path,
    pattern: str,
    extra_skip: frozenset[str],
) -> tuple[list[Path], bool] | None:
    """Enumerate files under ``root`` matching ``pattern`` via ripgrep.

    The ripgrep-backed twin of :func:`_glob_collect_matches`. Returns the SAME
    ``(matches, walk_truncated)`` shape so the caller can use either backend
    interchangeably and the whole downstream pipeline (policy filter / mtime
    sort / preview cap / oversized-result persistence) is reused unchanged.

    Returns ``None`` when ``rg`` is unavailable or errored so the caller falls
    back to the pure-Python walk — symmetric with :func:`_grep_with_ripgrep`.

    Uses ``rg --files`` (list files, not search content) with one
    ``--glob <pattern>`` per brace-expanded alternative (multiple ``--glob``
    are OR-combined by ripgrep), and one ``--glob !<name>`` per skip directory
    (same exclusion construction as :func:`_grep_with_ripgrep`). ripgrep's
    ``--files`` honours ``.gitignore`` by default (we do NOT pass
    ``--no-ignore``) — a capability the pure-Python walk lacks.

    Robustness (matches the pure-Python walk's guarantees): the output is read
    as a STREAM and the read stops as soon as :data:`WALK_MAX_ENTRIES` paths
    have been collected — at which point ``rg`` is killed and the result is
    flagged ``walk_truncated=True`` (the SAME soft-cap signal the Python walk
    raises, so the downstream INCOMPLETE message + persistence are correct).
    This means a pathologically deep / huge tree never makes the tool wait for
    ``rg`` to finish enumerating it. An upstream cancel (the awaiting task is
    cancelled) and the :data:`_RG_TIMEOUT_SECONDS` wall-clock ceiling both kill
    the whole ``rg`` process tree (no orphans) via
    :func:`_terminate_process_tree`; a timeout / spawn error returns ``None`` so
    the caller falls back to the pure-Python walk.
    """
    if not _rg_available():
        return None

    cmd: list[str] = ["rg", "--files"]
    # Brace expansion done up-front so ``{a,b}.py`` becomes two ``--glob``
    # alternatives (ripgrep treats repeated ``--glob`` as OR), matching the
    # pure-Python walk which also expands braces via ``_expand_braces``.
    expanded = _expand_braces(pattern)
    for pat in expanded:
        cmd.extend(["--glob", pat])
    # Depth alignment with the pure-Python walk's NON-``**`` branch: that branch
    # uses ``Path.glob(pat)``, whose ``*`` does NOT cross directory separators,
    # so a pattern with N path segments only matches files exactly N levels
    # below ``root`` (``*.py`` => 1 level / root only; ``src/*.ts`` => 2
    # levels). ripgrep's ``--glob`` is, by contrast, full-tree recursive
    # (``--glob *.py`` matches ``a.py`` AND ``sub/b.py``), which would make the
    # two backends return DIFFERENT sets for non-``**`` patterns. We therefore
    # cap ripgrep's recursion with ``--max-depth = max path-segment count`` of
    # the (brace-expanded) pattern so the rg track matches the same single-/
    # fixed-depth scope. A ``**`` pattern means "recurse fully" and is left
    # uncapped (ripgrep's default), matching the pure-Python ``**`` walk.
    if "**" not in pattern:
        max_depth = max(
            (pat.replace("\\", "/").strip("/").count("/") + 1) for pat in expanded
        )
        cmd.extend(["--max-depth", str(max_depth)])
    # Exclude well-known heavyweight directories + user-configured skips
    # (identical construction to the grep ripgrep path).
    for skip in sorted(set(DEFAULT_SKIP_DIR_NAMES) | set(extra_skip)):
        cmd.extend(["--glob", f"!{skip}"])

    proc = await _spawn_rg(cmd, cwd=str(root))
    if proc is None:
        return None

    matches: list[Path] = []
    seen: set[Path] = set()
    walk_truncated = False
    assert proc.stdout is not None
    try:

        async def _collect() -> None:
            nonlocal walk_truncated
            while True:
                try:
                    raw = await proc.stdout.readline()  # type: ignore[union-attr]
                except (ValueError, asyncio.LimitOverrunError):
                    # A single ``rg --files`` line longer than the stream
                    # reader's buffer limit (a pathological path, or non-line
                    # binary noise on stdout). Rather than crash, drain the
                    # offending bytes without a newline boundary so the loop
                    # keeps making progress; if even that fails the stream is
                    # unusable and we stop (the finally kills rg, the caller
                    # falls back to the Python walk).
                    try:
                        raw = await proc.stdout.read(65536)  # type: ignore[union-attr]
                    except (ValueError, OSError):
                        break
                    if not raw:
                        break
                    continue
                if not raw:
                    break
                rel = raw.decode("utf-8", errors="replace").strip()
                if not rel:
                    continue
                # Normalise to an absolute, slash-agnostic Path identical in
                # shape to the pure-Python walk's output. ripgrep emits paths
                # relative to its ``cwd`` (== ``root``); an already-absolute
                # line is taken as-is.
                candidate = Path(rel)
                full = candidate if candidate.is_absolute() else (root / candidate)
                try:
                    resolved = full.resolve()
                except OSError:
                    resolved = full
                if resolved in seen:
                    continue
                seen.add(resolved)
                matches.append(resolved)
                # Take-enough-then-stop: a recursive listing must not read an
                # unbounded number of entries. At the same soft cap the Python
                # walk uses, stop consuming + kill rg + flag the (still useful)
                # partial result INCOMPLETE so the downstream message and
                # persistence stay correct.
                if len(matches) >= WALK_MAX_ENTRIES:
                    walk_truncated = True
                    return

        await asyncio.wait_for(_collect(), timeout=_RG_TIMEOUT_SECONDS)

        if not walk_truncated:
            # Drained naturally — confirm rg exited cleanly. rg --files codes:
            # 0 = files listed, 1 = nothing matched (normal), >1 = error.
            try:
                code = await asyncio.wait_for(
                    proc.wait(), timeout=_RG_FORCE_KILL_AFTER_SECONDS
                )
            except asyncio.TimeoutError:
                return None
            if code not in (0, 1):
                return None
    except asyncio.TimeoutError:
        # Wall-clock ceiling hit — fall back to the pure-Python walk.
        return None
    except asyncio.CancelledError:
        # Upstream "Stop": ensure rg is killed (finally) before propagating.
        raise
    finally:
        # Shielded so a double-cancel cannot interrupt the kill and orphan rg.
        await _terminate_process_tree_shielded(proc)

    matches.sort()
    return matches, walk_truncated


async def tool_glob(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
    tool_result_store: ToolResultStorePort | None = None,
) -> dict[str, Any]:
    pattern = args.get("pattern") or ""
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("glob: 'pattern' argument is required")
    cwd_str = args.get("cwd") or ""
    if cwd_str and not isinstance(cwd_str, str):
        raise ToolError("glob: 'cwd' must be a string when provided")

    # Auto-split absolute patterns: "C:/foo/bar/*.py" → cwd="C:/foo/bar", pattern="*.py"
    # Uses pure string analysis — NO filesystem calls (no Path.is_dir()) so
    # the native file-guard is never triggered for intermediate directories
    # that are outside the workspace. The old is_dir() loop caused spurious
    # authorization dialogs for every directory level above the target.
    if not cwd_str:
        norm = pattern.replace("\\", "/")
        is_abs = (
            (len(norm) >= 3 and norm[1] == ":" and norm[2] == "/")
            or norm.startswith("/")
        )
        if is_abs:
            parts = norm.split("/")
            # Find the last segment that contains no wildcard — everything
            # before the first wildcard segment is the cwd.
            dir_parts: list[str] = []
            pat_parts: list[str] = []
            found_wildcard = False
            for p in parts:
                if found_wildcard or any(c in p for c in "*?["):
                    found_wildcard = True
                    pat_parts.append(p)
                else:
                    dir_parts.append(p)
            if pat_parts:
                # Normal case: pattern has wildcards — split cleanly.
                cwd_str = "/".join(dir_parts)
                pattern = "/".join(pat_parts)
            else:
                # No wildcards at all. Two sub-cases:
                # (a) Last segment is an existing DIRECTORY (e.g. "C:/WoS_AI"):
                #     user wants to list files inside it → cwd=that dir, pattern="*".
                #     Path.is_dir() uses GetFileAttributesW, NOT NtCreateFile,
                #     so it does NOT trigger the native file-guard hook.
                # (b) Last segment looks like a FILE (e.g. "C:/foo/bar/file.jpg"):
                #     cwd=parent dir, pattern=filename.
                if len(dir_parts) > 1:
                    candidate = Path("/".join(dir_parts))
                    if candidate.is_dir():
                        # Case (a): it's a directory — list its contents
                        cwd_str = "/".join(dir_parts)
                        pattern = "*"
                    else:
                        # Case (b): treat last segment as filename
                        cwd_str = "/".join(dir_parts[:-1])
                        pattern = dir_parts[-1]
                # else: single-segment absolute path — leave cwd_str empty

    root = Path(cwd_str) if cwd_str else Path(default_cwd() or Path.cwd())

    # Existence check BEFORE permission enforcement: Path.exists() uses
    # GetFileAttributesW (not NtCreateFile/NtOpenFile) so it does NOT
    # trigger the native hook. This avoids showing an authorization dialog
    # for a path that does not exist — there is nothing to authorize access
    # to. Only paths that actually exist proceed to the permission gate.
    if not root.exists():
        raise ToolError(f"glob: directory not found: {cwd_str}")
    if not root.is_dir():
        raise ToolError(f"glob: not a directory: {cwd_str}")
    _check_recursive_root_guard(root, pattern, "glob")

    # Enforce file-guard access only for paths that exist.
    await file_guard.enforce_project_access(path=str(root), operation="read")
    await file_guard.enforce_read(path=str(root), caller="ai_coding.tool.glob")

    # Dual-track collection (parity with grep): try the ripgrep backend first
    # (fast on large trees + honours .gitignore), fall back to the pure-Python
    # walk when ``rg`` is unavailable or errors. Only the FILE-COLLECTION step
    # differs between tracks — everything downstream (policy filter / mtime
    # sort / preview cap / oversized-result persistence / WALK-guard render) is
    # shared, so both tracks keep every existing glob capability.
    extra_skip = get_project_skip_dirs()
    backend = "python"
    # Streamed async rg run: it stops reading once it has collected enough
    # (WALK_MAX_ENTRIES) so a deep/huge tree never blocks, and a cancel/timeout
    # kills the whole rg process tree (no orphans) from inside the helper's
    # try/finally — so a plain ``await`` here is fully cancellation-safe.
    rg_result = await _glob_with_ripgrep(root, pattern, extra_skip)
    if rg_result is not None:
        matches, walk_truncated = rg_result
        backend = "ripgrep"
    else:
        matches, walk_truncated = await _run_walk_cancellable(
            _glob_collect_matches, root, pattern
        )
    matches, filtered_count = await _glob_filter_by_policy(
        matches, root=root, file_guard=file_guard
    )
    # Order matches newest-modified-first so that, when the list overflows the
    # in-prompt cap, the files RETAINED are the most recently changed ones
    # (most relevant to an agent that just edited / generated files). The full
    # list is still persisted, so older entries remain one ``read`` away.
    matches = await asyncio.to_thread(_sort_by_mtime_desc, matches)
    return _render_glob(
        matches,
        filtered_count,
        pattern=pattern,
        tool_result_store=tool_result_store,
        walk_truncated=walk_truncated,
        backend=backend,
    )


async def _run_walk_cancellable(
    fn: Callable[..., Any],
    *args: Any,
) -> Any:
    """Run a blocking walk ``fn(*args, stop_event)`` off-thread, cancellably.

    ``os.walk`` inside ``asyncio.to_thread`` is NOT forcibly killable, so a
    plain ``await asyncio.to_thread(...)`` cannot be cancelled once the walk is
    grinding through a huge tree. We instead pass a :class:`threading.Event` as
    the LAST positional arg (``fn`` must accept a trailing ``stop_event``); the
    walk polls it via :class:`WalkBudget`. When the awaiting task is cancelled
    (the upstream "Stop" path) we SET the event so the walk breaks
    cooperatively on its next entry check, then re-raise ``CancelledError`` to
    preserve cancellation semantics. The hard ``WALK_MAX_ENTRIES`` budget still
    bounds the walk even when no cancel arrives.
    """
    stop_event = threading.Event()
    task = asyncio.ensure_future(
        asyncio.to_thread(fn, *args, stop_event)
    )
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        stop_event.set()  # ask the worker thread to break out of the walk
        raise


def _glob_collect_matches(
    root: Path, pattern: str, stop_event: threading.Event | None = None
) -> tuple[list[Path], bool]:
    """Walk ``root`` and return ``(sorted matches, walk_truncated)``.

    ``stop_event`` (when supplied) lets an upstream cancel break a recursive
    ``**`` walk cooperatively (raises). The walk is also soft-bounded by
    :data:`WALK_MAX_ENTRIES` via :class:`WalkBudget`; when that cap is hit the
    walk stops EARLY and the second tuple element is ``True`` so the caller can
    flag the (still useful) partial results as INCOMPLETE.
    """
    patterns = _expand_braces(pattern)
    # 7-L3: merge user-configured project_skip_dirs with the defaults so
    # recursive ``**`` walks skip user-declared heavyweight dirs too.
    extra_skip = get_project_skip_dirs()
    matches: list[Path] = []
    if "**" not in pattern:
        seen: set[Path] = set()
        for pat in patterns:
            try:
                for p in root.glob(pat):
                    if p not in seen:
                        seen.add(p)
                        matches.append(p)
            except (OSError, ValueError) as e:
                raise ToolError(f"glob: invalid pattern: {e}") from e
        matches.sort()
        return matches, False
    seen2: set[Path] = set()
    budget = WalkBudget(stop_event=stop_event)
    for dirpath, _dirs, filenames in _walk_filtered(root, extra_skip, budget):
        for fname in filenames:
            candidate = dirpath / fname
            if candidate in seen2:
                continue
            try:
                rel = candidate.relative_to(root).as_posix()
            except ValueError:
                rel = str(candidate)
            for pat in patterns:
                if fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(
                    fname, pat.split("/")[-1]
                ):
                    seen2.add(candidate)
                    matches.append(candidate)
                    break
    matches.sort()
    return matches, budget.exceeded


async def _glob_filter_by_policy(
    matches: list[Path],
    *,
    root: Path,
    file_guard: FileGuardPort,
) -> tuple[list[Path], int]:
    """Drop matches the read allowlist excludes (V1 ``_glob.py:275-336``).

    Only filters when the search ``root`` is in the STATIC allowlist — a
    dynamically (ASK) authorised directory implies a tree-wide grant, so
    per-file filtering is SKIPPED to avoid over-filtering.  The two probe
    methods are optional (§3.1 tail-append); when the wired guard omits
    them (light stubs / NoopFileGuard) filtering is skipped (returns the
    matches unchanged + ``0``).
    """
    is_read_allowed = getattr(file_guard, "is_read_allowed", None)
    is_statically_allowed = getattr(file_guard, "is_statically_allowed", None)
    if not (
        matches
        and callable(is_read_allowed)
        and callable(is_statically_allowed)
        and await is_statically_allowed(path=str(root))
    ):
        return matches, 0
    kept: list[Path] = []
    filtered_count = 0
    for p in matches:
        if await is_read_allowed(path=str(p)):
            kept.append(p)
        else:
            filtered_count += 1
    return kept, filtered_count


def _render_glob(
    matches: list[Path],
    filtered_count: int,
    *,
    pattern: str,
    tool_result_store: ToolResultStorePort | None = None,
    walk_truncated: bool = False,
    backend: str = "python",
) -> dict[str, Any]:
    """Truncate + render the glob result dict with the policy-filter note.

    ``walk_truncated`` is ``True`` when the recursive walk hit the
    :data:`WALK_MAX_ENTRIES` soft cap and stopped early. The gathered matches
    are then a PARTIAL, non-exhaustive sample: we still return + persist them
    (useful for the model) but flag the result as INCOMPLETE and tell the model
    to narrow the scope for the complete set, instead of discarding everything.

    ``backend`` is tail-appended (§3.1) to the result dict as ``"ripgrep"`` /
    ``"python"`` so callers / tests can see which collection track produced the
    list (parity with the grep result's ``backend`` field).
    """
    result = _render_glob_inner(
        matches,
        filtered_count,
        pattern=pattern,
        tool_result_store=tool_result_store,
        walk_truncated=walk_truncated,
    )
    # §3.1 tail-append: record which collection backend produced the list.
    result["backend"] = backend
    return result


def _render_glob_inner(
    matches: list[Path],
    filtered_count: int,
    *,
    pattern: str,
    tool_result_store: ToolResultStorePort | None = None,
    walk_truncated: bool = False,
) -> dict[str, Any]:
    """Build the glob result dict (backend-agnostic; see :func:`_render_glob`)."""
    total_matched = len(matches)
    max_results = get_tool_output_thresholds().glob_max_results
    scan_capped = total_matched > max_results

    # Persist the gathered list (one path per line) BEFORE the preview cut so
    # the model can ``read`` the full set when the visible sample is not enough.
    # On a soft-cap (INCOMPLETE) walk we persist the partial list
    # too -- "what was scanned so far" is still useful and the model can read
    # it -- so persistence is requested whenever the preview is cut OR the walk
    # was truncated.
    stored_path: str | None = None
    if scan_capped or walk_truncated:
        full_text = "\n".join(str(p) for p in matches)
        stored_path = _maybe_store_full_result(
            full_text, tool_name="glob", store=tool_result_store
        )

    truncated = scan_capped or walk_truncated
    shown = matches[:max_results] if scan_capped else matches

    files = [str(p) for p in shown]
    if not files:
        if walk_truncated:
            # Cap hit before any file matched (e.g. a huge directory-only tree).
            msg = (
                f"(no files matched pattern: {pattern} yet, but the directory "
                f"is too large: traversal stopped early after ~"
                f"{WALK_MAX_ENTRIES:,} entries WITHOUT finishing. This is "
                f"INCOMPLETE -- narrow the scope (search a specific "
                f"sub-directory or use a tighter pattern like `src/**/*.py`).)"
            )
            return _ok(msg, files=files, truncated=True, incomplete=True)
        if filtered_count > 0:
            msg = (
                f"(no files matched pattern: {pattern}; "
                f"{filtered_count} files filtered by policy)"
            )
        else:
            # Files-only semantics (V1/v0.5 parity): a directory tree that
            # contains only subdirectories (no files) legitimately matches
            # nothing. Spell this out so the model does not conclude the
            # directory is missing / the tool is broken -- and points it at
            # the right tool for a structural listing.
            msg = (
                f"(no files matched pattern: {pattern}. glob lists FILES "
                f"only -- empty or directory-only subtrees yield no matches; "
                f"use an exec listing such as `dir`/`ls` to see directories.)"
            )
        return _ok(msg, files=files, truncated=truncated)

    if walk_truncated:
        # INCOMPLETE partial scan: count is "scanned so far", NOT the total.
        msg = (
            f"INCOMPLETE: directory too large -- traversal stopped early "
            f"after ~{WALK_MAX_ENTRIES:,} entries. Matched {total_matched} "
            f"file(s) SO FAR (a PARTIAL, non-exhaustive sample"
        )
        msg += (
            f"; showing the first {max_results}" if scan_capped else ""
        )
        msg += "). More files were NOT scanned -- do NOT treat this as the "
        msg += "complete set.\n"
        if stored_path is not None:
            msg += (
                f"[partial results] The {total_matched} paths scanned so far "
                f"were saved -- call read(path={stored_path}) to see them. "
            )
        msg += (
            "For the COMPLETE set, narrow the scope: search a specific "
            "sub-directory or use a tighter pattern (e.g. `src/**/*.py` "
            "instead of `**/*`)."
        )
        if filtered_count > 0:
            msg += f" ({filtered_count} files filtered by policy.)"
        result = _ok(msg, files=files, truncated=True, incomplete=True)
        if stored_path is not None:
            result["stored_path"] = stored_path
        return result

    msg = f"{len(files)} file(s) matched"
    if scan_capped:
        msg += f"; results truncated at {max_results}\n"
        if stored_path is not None:
            # Retrieval path beats "re-run with a tighter pattern" when the
            # full list is one ``read`` away.
            msg += (
                f"[truncation note] Glob matched {total_matched} files "
                f"(showing the {max_results} most-recently-modified). The "
                f"COMPLETE list was saved -- call read(path={stored_path}) to "
                f"see all {total_matched} paths."
            )
        else:
            msg += make_glob_advice(max_results)
    if filtered_count > 0:
        msg += f"; {filtered_count} files filtered by policy"
    result = _ok(msg, files=files, truncated=truncated)
    if stored_path is not None:
        # §3.1 tail-append: surface the retrieval path as a structured field
        # so callers / the UI can deep-link the full result.
        result["stored_path"] = stored_path
    return result


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


def _rg_available() -> bool:
    """Return True when a ``rg`` (ripgrep) executable is on PATH."""
    return shutil.which("rg") is not None


async def _grep_with_ripgrep(
    *,
    pattern: str,
    target: Path,
    include_glob: str,
    ignore_case: bool,
    context_lines: int,
    extra_skip: frozenset[str],
    tool_result_store: ToolResultStorePort | None = None,
) -> dict[str, Any] | None:
    """Run ripgrep (``rg --json``) and parse its output into the grep shape.

    Returns the result dict on success, or ``None`` when ``rg`` is
    unavailable / errored so the caller can fall back to the pure-Python
    implementation.

    Parsing: ``--json`` emits one JSON object per stdout line, each tagged with
    a ``type`` (``begin`` / ``match`` / ``context`` / ``end`` / ``summary``).
    For a ``match`` / ``context`` record we read ``data.path`` (``.text`` when
    utf-8, ``.bytes`` base64 otherwise), ``data.line_number`` and the full line
    ``data.lines.text`` as DISTINCT fields — so the path, line number and text
    are exact even when any of them contains a ``:`` / ``-`` separator, which
    the previous ``path:line:text`` text re-split could not handle. Only
    ``match`` records increment ``match_count`` (``context`` lines do not, the
    same as the old ``-`` separator convention). The human-readable ``output``
    text is then REBUILT from those exact fields (``path:line:text`` for a
    match, ``path-line-text`` for context, files separated by ``--``) so every
    downstream consumer (policy filter / persistence / preview cap / UI) keeps
    its existing plain-text contract while the data behind it is no longer
    derived from a fragile separator guess.

    Robustness: ``rg`` runs as a STREAMED async subprocess. Output is read
    incrementally and the read stops once :data:`GREP_MAX_SCAN_BYTES` of stdout
    has been collected — at which point ``rg`` is killed and the result is
    flagged truncated (the COMPLETE-so-far body still flows through the
    existing oversized-output persistence below). This bounds memory + time on
    a huge match set instead of waiting for ``rg`` to finish. An upstream
    cancel and the :data:`_RG_TIMEOUT_SECONDS` ceiling both kill the whole
    ``rg`` process tree (no orphans) via :func:`_terminate_process_tree`; a
    timeout / spawn error returns ``None`` so the caller falls back to Python.
    """
    if not _rg_available():
        return None

    # ``--json`` makes ripgrep emit one self-describing JSON object per line
    # (``begin`` / ``match`` / ``context`` / ``end`` / ``summary``) carrying the
    # path, line number and full line text as DISTINCT fields. We parse those
    # fields directly instead of re-splitting a ``path:line:text`` text line on
    # the first ``:`` / ``-`` — which mis-parses whenever a path or the matched
    # text itself contains a separator char (Windows ``C:\...`` drive letters,
    # ``key: value`` lines, hyphenated filenames). ``--json`` already implies
    # line numbers + no heading + no colour, so those flags are dropped.
    cmd: list[str] = ["rg", "--json"]
    if ignore_case:
        cmd.append("--ignore-case")
    if context_lines > 0:
        cmd.append(f"--context={context_lines}")
    if include_glob:
        cmd.extend(["--glob", include_glob])
    # Exclude well-known heavyweight directories + user-configured skips.
    for skip in sorted(set(DEFAULT_SKIP_DIR_NAMES) | set(extra_skip)):
        cmd.extend(["--glob", f"!{skip}"])
    cmd.append(pattern)
    cmd.append(str(target))

    proc = await _spawn_rg(cmd, cwd=str(target.parent if target.is_file() else target))
    if proc is None:
        return None

    chunks: list[bytes] = []
    collected = 0
    scan_capped = False
    return_code: int | None = None
    assert proc.stdout is not None
    try:

        async def _collect() -> None:
            nonlocal collected, scan_capped
            while True:
                chunk = await proc.stdout.read(65536)  # type: ignore[union-attr]
                if not chunk:
                    break
                chunks.append(chunk)
                collected += len(chunk)
                # Hard streaming ceiling: stop reading + kill rg once we have
                # captured this much output, so a pathological match flood does
                # not grow unbounded in memory / make us wait for rg to finish.
                # The body collected so far still feeds the existing persistence
                # path below (so the model can ``read`` it back).
                if collected >= GREP_MAX_SCAN_BYTES:
                    scan_capped = True
                    return

        await asyncio.wait_for(_collect(), timeout=_RG_TIMEOUT_SECONDS)

        if not scan_capped:
            try:
                return_code = await asyncio.wait_for(
                    proc.wait(), timeout=_RG_FORCE_KILL_AFTER_SECONDS
                )
            except asyncio.TimeoutError:
                return None
    except asyncio.TimeoutError:
        return None
    except asyncio.CancelledError:
        raise
    finally:
        # Shielded so a double-cancel cannot interrupt the kill and orphan rg.
        await _terminate_process_tree_shielded(proc)

    stdout_text = b"".join(chunks).decode("utf-8", errors="replace")
    # rg returncode: 0 = matches, 1 = no matches (normal), >1 = error. When we
    # stopped early on the streaming cap we never read the exit code; treat the
    # partial output as usable (it is real matches) rather than failing.
    if not stdout_text and return_code is not None and return_code not in (0, 1):
        return None

    # Parse the ``--json`` records into an exact structured intermediate
    # (path / line / text / is_match per emitted line), then REBUILD the
    # human-readable ``output`` text from those fields. A trailing JSON line may
    # be partial when the streaming cap chopped stdout mid-line; that line
    # simply fails to parse and is skipped (the matches before it are intact).
    records = _parse_rg_json_records(stdout_text)
    rebuilt = _rebuild_rg_output(records)

    max_output_bytes = get_tool_output_thresholds().grep_max_output_bytes
    cap_kb = max_output_bytes // 1024
    encoded = rebuilt.encode("utf-8")
    # ``scan_capped`` (rg killed at the streaming ceiling) is folded into the
    # truncated signal so the result is correctly flagged + persisted even in
    # the (degenerate) case where the streaming cap is below the preview cap.
    truncated = scan_capped or len(encoded) > max_output_bytes
    # Persist the COMPLETE REBUILT text BEFORE the byte-cap preview cut so the
    # model can ``read`` the full match set as readable ``path:line:text`` —
    # NOT the raw JSON stdout (which would be unreadable when read back). The
    # slice below only shapes the in-prompt preview.
    stored_path: str | None = None
    if truncated:
        stored_path = _maybe_store_full_result(
            rebuilt, tool_name="grep", store=tool_result_store
        )
    output = rebuilt
    if truncated:
        output = encoded[:max_output_bytes].decode(
            "utf-8", errors="replace"
        )

    # Structured matches list (parity with the Python path). Built straight
    # from the parsed records — only ``match`` records count, context records
    # are scaffolding. When the preview was byte-capped the visible ``output``
    # is a prefix of ``rebuilt``; the structured matches still reflect the full
    # parsed set (the same as the old behaviour, where matches were parsed from
    # the capped text but the persisted body held the rest).
    matches_list: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    match_count = 0
    for rec in records:
        seen_files.add(rec["path"])
        if rec["is_match"]:
            match_count += 1
            matches_list.append(
                {"path": rec["path"], "line": rec["line"], "text": rec["text"]}
            )

    file_count = len(seen_files)
    out = output.rstrip()
    if not out:
        return _ok(
            f"(no matches found for pattern: {pattern!r})",
            matches=[],
            file_count=0,
            match_count=0,
            truncated=False,
            output="",
            backend="ripgrep",
        )

    summary = f"({match_count} match(es) in {file_count} file(s)"
    if truncated:
        summary += f"; output truncated at {cap_kb}KB"
    summary += ")"
    if truncated:
        if stored_path is not None:
            summary += (
                f"\n[truncation note] Grep output exceeded {cap_kb}KB (showing "
                f"the first {cap_kb}KB). The COMPLETE match output was saved — "
                f"call read(path={stored_path}) to see all matches."
            )
        else:
            summary += "\n" + make_grep_advice(cap_kb=cap_kb)
    summary += _grep_backend_marker("ripgrep")

    result = _ok(
        summary,
        matches=matches_list,
        file_count=file_count,
        match_count=match_count,
        truncated=truncated,
        output=out,
        backend="ripgrep",
    )
    if stored_path is not None:
        # §3.1 tail-append: surface the retrieval path for the full match set.
        result["stored_path"] = stored_path
    # Private hand-off to :func:`_filter_rg_result`: the FULL parsed records +
    # the byte-cap so the policy filter can re-filter on exact structured
    # fields (no fragile text re-split) and re-cap. This key is internal and is
    # always stripped before the dict reaches any downstream consumer / wire.
    result["_rg_records"] = records
    result["_rg_byte_cap"] = max_output_bytes
    return result


def _decode_rg_path(path_obj: Any) -> str | None:
    """Extract the file path from a ``--json`` record's ``data.path`` object.

    ripgrep emits the path as ``{"text": "<utf-8 path>"}`` normally, or
    ``{"bytes": "<base64>"}`` when the path is not valid utf-8. We prefer
    ``text``; for ``bytes`` we base64-decode and utf-8-decode with
    ``errors="replace"`` so a non-utf-8 path degrades to a readable
    approximation rather than dropping the record. Returns ``None`` when
    neither field is present (malformed / partial line).
    """
    if not isinstance(path_obj, dict):
        return None
    text = path_obj.get("text")
    if isinstance(text, str):
        return text
    raw = path_obj.get("bytes")
    if isinstance(raw, str):
        try:
            return base64.b64decode(raw).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return None
    return None


def _rg_line_text(data: dict[str, Any]) -> str:
    """Return a record's full line text with the trailing newline stripped.

    ripgrep's ``data.lines.text`` carries the line's terminator (``\\n`` or
    ``\\r\\n`` on Windows); we strip exactly that trailing EOL so the rebuilt
    ``path:line:text`` matches the prior single-line shape. Embedded newlines
    (multi-line ``lines`` blocks) are left untouched.
    """
    lines = data.get("lines")
    text = lines.get("text") if isinstance(lines, dict) else None
    if not isinstance(text, str):
        # Non-utf-8 line content arrives as ``lines.bytes`` (base64). Decode it
        # the same way as a path so the text is still readable.
        raw = lines.get("bytes") if isinstance(lines, dict) else None
        if isinstance(raw, str):
            try:
                text = base64.b64decode(raw).decode("utf-8", errors="replace")
            except (ValueError, TypeError):
                text = ""
        else:
            text = ""
    # Strip one trailing CRLF / LF (not all whitespace — leading indentation
    # and trailing spaces inside the line are meaningful and preserved).
    if text.endswith("\r\n"):
        return text[:-2]
    if text.endswith("\n"):
        return text[:-1]
    return text


def _parse_rg_json_records(stdout_text: str) -> list[dict[str, Any]]:
    """Parse ``rg --json`` stdout into ordered match/context records.

    Each returned record is ``{"path", "line", "text", "is_match"}`` for a
    ``match`` (``is_match=True``) or ``context`` (``is_match=False``) JSON
    object, in ripgrep's emission order. ``begin`` / ``end`` / ``summary``
    records and any line that fails to parse (a partial trailing line left by
    the streaming cap, or unexpected noise) are skipped — never raising — so a
    truncated stream still yields every complete record before the cut.
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
        rec_type = obj.get("type")
        if rec_type not in ("match", "context"):
            continue
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        path = _decode_rg_path(data.get("path"))
        line_no = data.get("line_number")
        if path is None or not isinstance(line_no, int):
            continue
        records.append(
            {
                "path": path,
                "line": line_no,
                "text": _rg_line_text(data),
                "is_match": rec_type == "match",
            }
        )
    return records


def _rebuild_rg_output(records: list[dict[str, Any]]) -> str:
    """Rebuild the readable ``output`` text from parsed ``--json`` records.

    Reproduces ripgrep's classic text shape from the EXACT structured fields:
    a match line is ``path:line:text``; a context line is ``path-line-text``;
    consecutive records for DIFFERENT files are separated by a ``--`` line
    (matching ripgrep's between-file separator) so the downstream
    plain-text contract is preserved while no longer being a fragile re-split
    of an ambiguous text stream.
    """
    parts: list[str] = []
    prev_path: str | None = None
    for rec in records:
        path = rec["path"]
        if prev_path is not None and path != prev_path:
            parts.append("--")
        sep = ":" if rec["is_match"] else "-"
        parts.append(f"{path}{sep}{rec['line']}{sep}{rec['text']}")
        prev_path = path
    return "\n".join(parts)


async def _filter_rg_result(
    rg_result: dict[str, Any],
    *,
    pattern: str,
    path_allowed: Any,
) -> dict[str, Any]:
    """Drop ripgrep matches whose file the read allowlist excludes.

    V1 parity: ``backend/tools/_grep.py:122-178``. Filtering operates on the
    EXACT structured records parsed from ``rg --json`` (handed off privately by
    :func:`_grep_with_ripgrep` under ``_rg_records``), NOT a re-split of the
    rendered ``output`` text — so a denied path containing a ``:`` / ``-``
    (e.g. a Windows ``C:\\...`` path) is matched precisely. Records whose
    ``path`` :func:`path_allowed` rejects are dropped (both match + its context
    lines); the kept records are re-rendered to ``output`` and re-capped, and a
    ``"; N matches filtered by policy"`` summary suffix is appended. When
    nothing is filtered the original result passes through unchanged (with the
    private hand-off keys stripped).
    """
    records = rg_result.get("_rg_records")
    # Always strip the private hand-off keys so they never reach the wire,
    # whichever branch we return through.
    byte_cap = rg_result.pop("_rg_byte_cap", None)
    rg_result.pop("_rg_records", None)

    if not isinstance(records, list) or not records:
        return rg_result

    kept_records: list[dict[str, Any]] = []
    filtered_match_count = 0
    for rec in records:
        if await path_allowed(rec["path"]):
            kept_records.append(rec)
        elif rec["is_match"]:
            # Only count dropped MATCH records as "filtered" (a dropped context
            # line is incidental to its match being filtered).
            filtered_match_count += 1

    if filtered_match_count == 0:
        return rg_result

    matches_list: list[dict[str, Any]] = []
    seen_files: set[str] = set()
    match_count = 0
    for rec in kept_records:
        seen_files.add(rec["path"])
        if rec["is_match"]:
            match_count += 1
            matches_list.append(
                {"path": rec["path"], "line": rec["line"], "text": rec["text"]}
            )

    rebuilt = _rebuild_rg_output(kept_records)
    # Re-apply the SAME byte cap to the filtered text so the preview honours the
    # output threshold exactly as the unfiltered result did.
    cap_kb = get_tool_output_thresholds().grep_max_output_bytes // 1024
    truncated = bool(rg_result.get("truncated", False))
    if isinstance(byte_cap, int) and byte_cap > 0:
        encoded = rebuilt.encode("utf-8")
        if len(encoded) > byte_cap:
            rebuilt = encoded[:byte_cap].decode("utf-8", errors="replace")
            truncated = True
    out = rebuilt.rstrip()
    file_count = len(seen_files)

    if not out:
        return _ok(
            f"(no matches found for pattern: {pattern!r}; "
            f"{filtered_match_count} matches filtered by policy)",
            matches=[],
            file_count=0,
            match_count=0,
            truncated=False,
            output="",
            backend="ripgrep",
        )

    summary = f"({match_count} match(es) in {file_count} file(s)"
    if truncated:
        summary += f"; output truncated at {cap_kb}KB"
    summary += f"; {filtered_match_count} matches filtered by policy)"
    stored_path = rg_result.get("stored_path")
    if truncated:
        if stored_path:
            summary += (
                f"\n[truncation note] Grep output exceeded {cap_kb}KB (showing "
                f"the first {cap_kb}KB). The COMPLETE match output was saved — "
                f"call read(path={stored_path}) to see all matches."
            )
        else:
            summary += "\n" + make_grep_advice(cap_kb=cap_kb)
    summary += _grep_backend_marker("ripgrep")

    filtered = _ok(
        summary,
        matches=matches_list,
        file_count=file_count,
        match_count=match_count,
        truncated=truncated,
        output=out,
        backend="ripgrep",
    )
    if stored_path:
        # §3.1 tail-append: preserve the retrieval path through the filter.
        filtered["stored_path"] = stored_path
    return filtered


def _grep_backend_marker(backend: str) -> str:
    """Return a ``\\n[backend: ripgrep|python]`` suffix for the grep summary.

    G-9 (observability): grep runs on one of two tracks — the ripgrep
    subprocess (``backend="ripgrep"``) or the pure-Python fallback
    (``backend="python"``). The track was only on the structured ``backend``
    field, invisible in the user/model-facing summary text, so a reader could
    not tell from the receipt which path actually ran (hampering triage). This
    surfaces the value of the EXISTING ``backend`` field in the text — it does
    NOT add or rename any result-dict field. Returns ``""`` for an unknown /
    empty backend so the marker is only shown when meaningful.
    """
    if backend not in ("ripgrep", "python"):
        return ""
    return f"\n[backend: {backend}]"


def _render_grep_matches_text(matches: list[dict[str, Any]]) -> str:
    """Render a flat ``matches`` list to grouped ``path:`` / ``  Line N:`` text.

    Shared by the in-prompt preview re-render and the force-store full body so
    the persisted file the model ``read``s back is the SAME readable shape as
    the in-prompt sample (just complete). Groups consecutive matches by file
    with a blank-line separator between files, matching the preview layout.
    """
    rendered: list[str] = []
    current = ""
    for m in matches:
        path_part = str(m.get("path", ""))
        if path_part != current:
            if current:
                rendered.append("")
            current = path_part
            rendered.append(f"{path_part}:")
        rendered.append(f"  Line {m.get('line')}: {m.get('text', '')}")
    return "\n".join(rendered).rstrip()


def _apply_grep_inprompt_caps(
    result: dict[str, Any],
    *,
    pattern: str,
    store: ToolResultStorePort | None = None,
) -> dict[str, Any]:
    """Apply the in-prompt match-count cap, per-line clip and mtime ordering.

    Runs AFTER the backend (ripgrep / pure-Python) produced its result dict
    and AFTER any policy filtering. Both backends emit the same shape
    (``matches`` list of ``{path,line,text}`` + a rendered ``output`` text),
    so one pass handles both. Behaviour:

    * orders the structured matches newest-modified-file first so that, when
      the list overflows :attr:`ToolOutputThresholds.grep_max_matches`, the
      retained matches come from the most recently changed files;
    * caps the visible matches at ``grep_max_matches`` (the COMPLETE output is
      still persisted — either by the backend when it crossed the BYTE cap, or
      HERE via a forced store when the COUNT cap fired below the byte
      threshold — so the model always gets a ``read`` retrieval path);
    * clips any single matched line longer than ``grep_max_line_length`` with
      an ellipsis marker so one pathological line cannot blow the budget;
    * re-renders ``output`` grouped by file ONLY when something was actually
      capped/clipped; otherwise the original ``output`` (which may carry
      context lines / store footers) is left exactly as the backend produced
      it.

    The count-cap force-store closes the G-8 gap: the backend only persisted
    when the output crossed the 16 KB store threshold, so 200 SHORT matches
    (count-capped at 100, but only ~5 KB total) lost the elided 100 with no
    retrieval path. When ``count_capped`` fires and no ``stored_path`` exists
    yet, we render the COMPLETE (uncapped, unclipped) match set and force the
    store to persist it regardless of byte size, then surface its path in the
    truncation note — exactly the retrieval path ``exec`` already gives.

    Runs blocking ``stat`` calls for the mtime ordering, so the caller invokes
    it via ``asyncio.to_thread``. Never raises: a stat failure sorts that file
    as oldest. ``ok=False`` envelopes and empty-match results pass through.
    """
    if not result.get("ok"):
        return result
    matches = result.get("matches")
    if not isinstance(matches, list) or not matches:
        return result

    thresholds = get_tool_output_thresholds()
    max_matches = thresholds.grep_max_matches
    max_line = thresholds.grep_max_line_length
    total_matches = len(matches)

    # mtime ordering (newest file first). Cache one stat per distinct path.
    mtime_cache: dict[str, float] = {}

    def _mtime(path_str: str) -> float:
        cached = mtime_cache.get(path_str)
        if cached is not None:
            return cached
        try:
            value = Path(path_str).stat().st_mtime
        except OSError:
            value = 0.0
        mtime_cache[path_str] = value
        return value

    ordered = sorted(
        matches,
        key=lambda m: (
            -_mtime(str(m.get("path", ""))),
            str(m.get("path", "")),
            int(m.get("line", 0) or 0),
        ),
    )

    count_capped = total_matches > max_matches
    shown = ordered[:max_matches] if count_capped else ordered

    # Per-line clip of the visible matches.
    line_clipped = False
    capped_matches: list[dict[str, Any]] = []
    for m in shown:
        text = str(m.get("text", ""))
        if len(text) > max_line:
            text = text[:max_line] + make_line_truncated_suffix(
                kept_chars=max_line, original_chars=len(text)
            )
            line_clipped = True
        capped_matches.append(
            {"path": m.get("path"), "line": m.get("line"), "text": text}
        )

    # Nothing to do — leave the backend's richer output (context lines, store
    # footer) untouched so we never DEGRADE a result that already fit.
    if not count_capped and not line_clipped:
        return result

    # Re-render grouped output from the (capped, clipped) match rows.
    out = _render_grep_matches_text(capped_matches)

    file_count = len({str(m.get("path", "")) for m in capped_matches})
    stored_path = result.get("stored_path")
    backend = result.get("backend", "")
    was_byte_truncated = bool(result.get("truncated", False))

    # G-8: the count cap fired but the backend never persisted (its byte cap
    # was not crossed — e.g. 200 short matches totalling ~5 KB < the 16 KB
    # store threshold). Force-persist the COMPLETE (uncapped, unclipped) match
    # set so the model still gets a ``read(path=...)`` retrieval path for the
    # elided rows, exactly as the byte-cap track already does. Rendered from
    # the FULL ``ordered`` list (not ``capped_matches``) so every match is
    # recoverable. Force bypasses the store byte threshold (the decision here
    # is count-driven, not byte-driven).
    if count_capped and not stored_path and store is not None:
        full_text = _render_grep_matches_text(ordered)
        stored_path = _maybe_store_full_result(
            full_text, tool_name="grep", store=store, force=True
        )

    summary = f"({len(capped_matches)} match(es) in {file_count} file(s)"
    if count_capped:
        summary += f"; showing the {max_matches} newest-file matches"
    summary += ")"
    if count_capped:
        if stored_path:
            summary += (
                f"\n[truncation note] Grep found {total_matches} matches "
                f"(showing the {max_matches} from the most-recently-modified "
                f"files). The COMPLETE match output was saved -- call "
                f"read(path={stored_path}) to see all {total_matches} matches."
            )
        else:
            summary += "\n" + make_grep_advice(
                cap_kb=thresholds.grep_max_output_bytes // 1024
            )
    summary += _grep_backend_marker(str(backend))

    new_result = _ok(
        summary,
        matches=capped_matches,
        file_count=file_count,
        match_count=len(capped_matches),
        truncated=was_byte_truncated or count_capped,
        output=out,
        backend=backend,
    )
    if result.get("incomplete"):
        new_result["incomplete"] = True
    if stored_path:
        new_result["stored_path"] = stored_path
    return new_result


async def tool_grep(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
    tool_result_store: ToolResultStorePort | None = None,
) -> dict[str, Any]:
    pattern = args.get("pattern") or ""
    if not isinstance(pattern, str) or not pattern:
        raise ToolError("grep: 'pattern' argument is required")
    path_str = args.get("path") or ""
    if path_str:
        target = Path(resolve_under_workspace(path_str))
    else:
        target = Path(default_cwd() or Path.cwd())
    include_glob = args.get("include") or ""
    ignore_case = bool(args.get("ignoreCase", False))
    context_lines = int(args.get("contextLines") or 0)

    def _check_target() -> None:
        if not target.exists():
            raise ToolError(f"grep: path not found: {path_str}")
        if target.is_dir():
            _check_recursive_root_guard(target, "**", "grep")

    await asyncio.to_thread(_check_target)
    await file_guard.enforce_read(path=str(target), caller="ai_coding.tool.grep")

    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error as e:
        raise ToolError(f"grep: invalid regex pattern: {e}") from e

    # 7-L3: merge user-configured project_skip_dirs with the defaults.
    extra_skip = get_project_skip_dirs()

    # FileGuard per-file read probe (V1 ``backend/tools/_grep.py:122-178``
    # ripgrep path + ``:206-233`` Python fallback parity).  ``grep`` filters
    # individual matches/files the read allowlist excludes — unlike ``glob``
    # it does NOT gate on the root being statically allowed (V1 grep filters
    # unconditionally when a PolicyCenter is wired).  Fail-open per-file.
    path_allowed = _make_path_allowed(file_guard)

    # 7-M1: try the ripgrep backend first (10-100x faster on large trees);
    # fall back to the pure-Python implementation when ``rg`` is unavailable
    # or errors.  Directory targets only — single-file greps go straight to
    # the Python path (rg offers no win and the parsing differs).
    if target.is_dir():
        rg_result = await _grep_with_ripgrep(
            pattern=pattern,
            target=target,
            include_glob=include_glob,
            ignore_case=ignore_case,
            context_lines=context_lines,
            extra_skip=extra_skip,
            tool_result_store=tool_result_store,
        )
        if rg_result is not None:
            filtered = await _filter_rg_result(
                rg_result, pattern=pattern, path_allowed=path_allowed
            )
            return await asyncio.to_thread(
                _apply_grep_inprompt_caps,
                filtered,
                pattern=pattern,
                store=tool_result_store,
            )

    # ── Pure-Python fallback: collect candidate files, then filter them
    # through the read allowlist BEFORE scanning (V1 ``_grep.py:206-233``).
    candidate_files, walk_truncated = await _run_walk_cancellable(
        _grep_collect_files, target, extra_skip, include_glob
    )
    candidate_files, filtered_count = await _grep_filter_files(
        candidate_files,
        target=target,
        file_guard=file_guard,
        path_allowed=path_allowed,
    )
    scanned = await asyncio.to_thread(
        _grep_scan_files,
        candidate_files,
        compiled=compiled,
        pattern=pattern,
        context_lines=context_lines,
        filtered_count=filtered_count,
        tool_result_store=tool_result_store,
        walk_truncated=walk_truncated,
    )
    return await asyncio.to_thread(
        _apply_grep_inprompt_caps, scanned, pattern=pattern, store=tool_result_store
    )


def _make_path_allowed(file_guard: FileGuardPort) -> Any:
    """Build a cached, fail-open ``async (path) -> bool`` read probe.

    The optional :meth:`FileGuardPort.is_read_allowed` (§3.1 tail-append)
    is probed via ``getattr``; when absent every path is allowed (graceful
    skip for light stubs / NoopFileGuard).  A per-path cache avoids
    re-probing the same file on the line-by-line ripgrep filter.  Fail-open
    on probe error mirrors V1 ``_grep.py:151`` (``allowed=True`` on except).
    """
    is_read_allowed = getattr(file_guard, "is_read_allowed", None)
    cache: dict[str, bool] = {}

    async def _path_allowed(path_part: str) -> bool:
        if not callable(is_read_allowed):
            return True
        cached = cache.get(path_part)
        if cached is not None:
            return cached
        try:
            allowed = bool(await is_read_allowed(path=path_part))
        except Exception:  # noqa: BLE001 — fail-open per-file (V1 parity)
            allowed = True
        cache[path_part] = allowed
        return allowed

    return _path_allowed


def _grep_collect_files(
    target: Path,
    extra_skip: frozenset[str],
    include_glob: str,
    stop_event: threading.Event | None = None,
) -> tuple[list[Path], bool]:
    """Return ``(sorted candidate files, walk_truncated)`` for a Python grep.

    Bounded + cooperatively cancellable via :class:`WalkBudget` (same guard as
    glob: a recursive grep must not hang on a pathologically large tree). When
    the :data:`WALK_MAX_ENTRIES` soft cap is hit the walk stops early and the
    second element is ``True`` so the caller flags the scan as INCOMPLETE
    instead of presenting a partial result as exhaustive.
    """
    if target.is_file():
        return [target], False
    raw: list[Path] = []
    budget = WalkBudget(stop_event=stop_event)
    for dirpath, _dirs, filenames in _walk_filtered(target, extra_skip, budget):
        for fname in filenames:
            raw.append(dirpath / fname)
    raw.sort()
    if include_glob:
        raw = [f for f in raw if fnmatch.fnmatch(f.name, include_glob)]
    return raw, budget.exceeded


async def _grep_filter_files(
    files: list[Path],
    *,
    target: Path,
    file_guard: FileGuardPort,
    path_allowed: Any,
) -> tuple[list[Path], int]:
    """Drop files the read allowlist excludes BEFORE scanning.

    V1 ``_grep.py:213`` only filters directory targets in the fallback
    path (a single-file target was already authorised by ``enforce_read``);
    when the guard omits the probe filtering is skipped.
    """
    if not callable(getattr(file_guard, "is_read_allowed", None)):
        return files, 0
    if target.is_file():
        return files, 0
    kept: list[Path] = []
    filtered_count = 0
    for fp in files:
        if await path_allowed(str(fp)):
            kept.append(fp)
        else:
            filtered_count += 1
    return kept, filtered_count


def _grep_scan_files(
    files: list[Path],
    *,
    compiled: re.Pattern[str],
    pattern: str,
    context_lines: int,
    filtered_count: int,
    tool_result_store: ToolResultStorePort | None = None,
    walk_truncated: bool = False,
) -> dict[str, Any]:
    """Scan ``files`` for ``compiled`` and render the grep result dict.

    ``walk_truncated`` is ``True`` when the candidate-file walk hit the
    :data:`WALK_MAX_ENTRIES` soft cap and stopped early; the scan is then over a
    PARTIAL file set, so the result is flagged INCOMPLETE with a note to narrow
    the scope (the matches found are still returned -- they are useful).
    """
    matches_list: list[dict[str, Any]] = []
    output_parts: list[str] = []
    # When a store is wired we keep collecting the FULL rendered output past
    # the byte preview cap so it can be persisted + ``read`` back. Only
    # allocated when a store is present so the no-store path keeps its prior
    # bounded-memory behaviour.
    full_parts: list[str] | None = [] if tool_result_store is not None else None
    max_output_bytes = get_tool_output_thresholds().grep_max_output_bytes
    cap_kb = max_output_bytes // 1024
    total_bytes = 0
    match_count = 0
    file_count = 0
    truncated = False
    scan_bytes = 0
    scan_capped = False

    for fp in files:
        if scan_bytes > GREP_MAX_SCAN_BYTES:
            # Content-scan OOM/time guard: we have read more file content than
            # the cap allows. Stop scanning and flag the result INCOMPLETE
            # (grep reads every candidate file, unlike glob -- a huge file set
            # can be slow even when few lines match).
            scan_capped = True
            break
        try:
            text = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            continue
        scan_bytes += len(text)
        file_lines = text.splitlines()
        matched_indices = {
            idx for idx, line in enumerate(file_lines) if compiled.search(line)
        }
        if not matched_indices:
            continue
        file_count += 1

        chunk_lines, chunk_matches = _grep_render_file_chunk(
            fp, file_lines, matched_indices, context_lines
        )
        match_count += len(chunk_matches)
        matches_list.extend(chunk_matches)

        chunk = "\n".join(chunk_lines) + "\n"
        if full_parts is not None:
            full_parts.append(chunk)
        chunk_bytes = len(chunk.encode("utf-8"))
        if total_bytes + chunk_bytes > max_output_bytes:
            truncated = True
            if full_parts is None:
                # No store: stop early (prior behaviour). With a store we keep
                # scanning to capture the full body for persistence.
                break
            continue
        if not truncated:
            output_parts.append(chunk)
            total_bytes += chunk_bytes

    # Either cap makes the result INCOMPLETE/non-exhaustive, but the REASON
    # differs (too many directory entries vs. too much file content read), so
    # the model gets an accurate explanation rather than a misleading "entries"
    # message when it was actually the content-scan byte cap.
    incomplete = walk_truncated or scan_capped
    reason = _grep_incomplete_reason(
        walk_truncated=walk_truncated, scan_capped=scan_capped
    )

    if not output_parts:
        if incomplete:
            msg = (
                f"(INCOMPLETE: {reason} No match was found in the PARTIAL set "
                f"scanned so far for pattern: {pattern!r}. This is NOT "
                f"exhaustive -- narrow the scope (grep a specific sub-directory "
                f"or pass a tighter `include` glob) for a complete search.)"
            )
            return _ok(
                msg,
                matches=[],
                file_count=0,
                match_count=0,
                truncated=True,
                incomplete=True,
                output="",
                backend="python",
            )
        if filtered_count > 0:
            msg = (
                f"(no matches found for pattern: {pattern!r}; "
                f"{filtered_count} files filtered by policy)"
            )
        else:
            msg = f"(no matches found for pattern: {pattern!r})"
        return _ok(
            msg,
            matches=[],
            file_count=0,
            match_count=0,
            truncated=False,
            output="",
            backend="python",
        )

    stored_path: str | None = None
    if truncated and full_parts is not None:
        full_text = "\n".join(full_parts).rstrip()
        stored_path = _maybe_store_full_result(
            full_text, tool_name="grep", store=tool_result_store
        )

    out = "\n".join(output_parts).rstrip()
    summary = f"({match_count} match(es) in {file_count} file(s)"
    if truncated:
        summary += f"; output truncated at {cap_kb}KB"
    if filtered_count > 0:
        summary += f"; {filtered_count} files filtered by policy"
    summary += ")"
    if incomplete:
        summary = (
            f"INCOMPLETE: {reason} The matches below are a PARTIAL, "
            f"non-exhaustive sample (more files were NOT scanned -- do NOT "
            f"treat this as the complete set; grep a specific sub-directory or "
            f"pass a tighter `include` for the full search).\n" + summary
        )
    if truncated:
        if stored_path is not None:
            summary += (
                f"\n[truncation note] Grep output exceeded {cap_kb}KB (showing "
                f"the first {cap_kb}KB). The COMPLETE match output was saved -- "
                f"call read(path={stored_path}) to see all matches."
            )
        else:
            summary += "\n" + make_grep_advice(cap_kb=cap_kb)
    summary += _grep_backend_marker("python")

    result = _ok(
        summary,
        matches=matches_list,
        file_count=file_count,
        match_count=match_count,
        truncated=truncated or incomplete,
        output=out,
        backend="python",
    )
    if incomplete:
        result["incomplete"] = True
    if stored_path is not None:
        # §3.1 tail-append: surface the retrieval path for the full match set.
        result["stored_path"] = stored_path
    return result


def _grep_incomplete_reason(*, walk_truncated: bool, scan_capped: bool) -> str:
    """Explain WHY a Python-fallback grep stopped early (accurate per cause).

    The two caps are distinct: ``walk_truncated`` = the candidate-file *walk*
    hit :data:`WALK_MAX_ENTRIES` (too many directory entries); ``scan_capped`` =
    the *content scan* read past :data:`GREP_MAX_SCAN_BYTES` (too much file
    content). Reporting the right reason avoids the earlier bug where a
    content-byte cap was mislabelled as an "entries" cap.
    """
    if walk_truncated:
        return (
            f"directory too large -- the candidate-file walk stopped early "
            f"after ~{WALK_MAX_ENTRIES:,} entries WITHOUT finishing."
        )
    # scan_capped
    return (
        f"too much file content to scan -- stopped after reading "
        f"~{GREP_MAX_SCAN_BYTES // (1024 * 1024)}MB of file content WITHOUT "
        f"finishing."
    )


def _grep_render_file_chunk(
    fp: Path,
    file_lines: list[str],
    matched_indices: set[int],
    context_lines: int,
) -> tuple[list[str], list[dict[str, Any]]]:
    """Render one file's match block (with context) + its structured matches."""
    context_indices: set[int] = set()
    for idx in matched_indices:
        lo = max(0, idx - context_lines)
        hi = min(len(file_lines), idx + context_lines + 1)
        context_indices.update(range(lo, hi))

    chunk_lines: list[str] = []
    chunk_matches: list[dict[str, Any]] = []
    prev = -2
    for idx in sorted(context_indices):
        if idx - prev > 1 and prev >= 0:
            chunk_lines.append("--")
        prefix = ">" if idx in matched_indices else " "
        chunk_lines.append(f"{prefix} {fp}:{idx + 1}:{file_lines[idx]}")
        if idx in matched_indices:
            chunk_matches.append(
                {"path": str(fp), "line": idx + 1, "text": file_lines[idx]}
            )
        prev = idx
    return chunk_lines, chunk_matches
