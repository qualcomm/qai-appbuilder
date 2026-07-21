# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Default :class:`FileBrokerPort` implementations.

* :class:`NoopFileBroker` — pass-through; the default adapter.
* :class:`PatternFileScreen` — a **pure-software safety layer** that runs
  around file/exec tool calls independently of the FileGuard / PolicyCenter
  stack (which owns the heavier, OS-level AppContainer / Win32-ACL isolation
  in :mod:`file_guard`). It provides, with **zero OS-isolation dependency**:

  - pre-call ``always_exclude`` glob path rejection (``.git/**`` / ``.env`` …);
  - pre-call **dangerous write-directory** rejection for write/edit tools
    (injected ``write_dir_guard``; backed by the security domain's
    ``dangerous_paths.is_blocked_for_write`` via the apps-layer bridge);
  - pre-call **dangerous exec command** rejection for the ``exec`` tool
    (injected ``exec_command_guard``; backed by the dep/exec broker pure
    predicates + a dangerous-command regex via the bridge);
  - post-call ``glob`` / ``grep`` ``always_exclude`` result filtering
    (drop ``.env`` / ``.git/**`` … entries the LLM would otherwise see) plus
    result truncation (``max_entries``) to keep the LLM context from being
    blown up by a huge file list.

  Cross-context purity is preserved: this module imports nothing from
  ``qai.security`` / ``qai.dependency_approval`` / ``qai.command_policy``; the guards are
  plain injected callables wired by ``apps/api/_file_broker_bridge.py`` (the
  one layer allowed to depend on multiple bounded contexts).

This layer is independent of ``settings.security.file_guard_enabled``: it is
gated by its own ``settings.tools.file_broker_enabled`` (default ON), so the
basic pattern/command hygiene + truncation keeps working even while the
OS-isolation FileGuard stack is disabled.
"""

from __future__ import annotations

import fnmatch
import inspect
import os
import re
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from qai.ai_coding.infrastructure.tools.errors import ToolGuardDenied

__all__ = ["NoopFileBroker", "PatternFileScreen"]


_FILE_TOOL_PATH_KEYS: dict[str, tuple[str, ...]] = {
    "read": ("path",),
    "write": ("path",),
    "edit": ("path",),
    "glob": ("cwd",),
    "grep": ("path",),
    "apply_patch": (),
}

#: Tools whose path argument writes to disk (subject to the dangerous
#: write-directory guard).
_WRITE_TOOLS = frozenset({"write", "edit"})

_TRUNCATABLE_TOOLS = frozenset({"glob", "grep"})

#: Optional audit callback: ``(op, path, reason) -> None``. Best-effort.
AuditSink = Callable[[str, str, str], None]
#: Returns ``True`` when ``path`` must be denied write access.
WriteDirGuard = Callable[[str], bool]
#: Returns a deny-reason string when ``command`` must be blocked, else ``None``.
#: The guard MAY be async (return an awaitable) — the dep-broker approval path
#: blocks until the operator approves / rejects / the timeout elapses, so it
#: returns ``Awaitable[str | None]``; the static dangerous-pattern path stays
#: synchronous. :meth:`PatternFileScreen.pre_call` awaits an awaitable result.
ExecCommandGuard = Callable[[str], "str | None | Awaitable[str | None]"]


class NoopFileBroker:
    """No filtering, no truncation, no redirection."""

    async def pre_call(
        self, *, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        return args

    async def post_call(
        self, *, tool_name: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        return result


class PatternFileScreen:
    """Pure-software pre/post safety layer for file & exec tools.

    Args:
        always_exclude: list of fnmatch patterns relative to ``project_root``;
            any tool path matching one triggers :class:`ToolGuardDenied`
            (``error_code="ai_coding.tool.path_excluded"``).
        max_entries: cap for ``glob`` / ``grep`` ``files`` / ``matches``
            lists; 0 disables truncation.
        project_root: absolute path used to compute the relative path checked
            against ``always_exclude``. When ``None`` we just check the
            basename (legacy fallback behaviour).
        write_dir_guard: optional callable ``(path) -> bool`` returning
            ``True`` when a write/edit target must be rejected (dangerous
            system / profile directory). Injected by the apps-layer bridge so
            this module stays cross-context-pure. ``None`` disables the check.
        exec_command_guard: optional callable ``(command) -> str | None``
            returning a deny-reason when an ``exec`` command must be blocked
            (dangerous install args / denied pattern), else ``None``. ``None``
            disables the check.
        audit_sink: optional ``(op, path, reason) -> None`` callback invoked
            (best-effort) whenever a pre-call rejection fires, so the
            "what was blocked while FileGuard is off" trail is preserved.
    """

    __slots__ = (
        "_always_exclude",
        "_max_entries",
        "_project_root",
        "_write_dir_guard",
        "_exec_command_guard",
        "_audit_sink",
    )

    def __init__(
        self,
        *,
        always_exclude: list[str] | None = None,
        max_entries: int = 10_000,
        project_root: Path | None = None,
        write_dir_guard: WriteDirGuard | None = None,
        exec_command_guard: ExecCommandGuard | None = None,
        audit_sink: AuditSink | None = None,
    ) -> None:
        self._always_exclude: tuple[str, ...] = tuple(
            always_exclude
            if always_exclude is not None
            else (
                # AGENTS.md 🟡🟡 (发现 V1 缺陷必须修复，绝不将错就错) +
                # 🟢 (对齐前先评估合理性). The TRUTH about V1 (verified against
                # ``QAIModelBuilder_v1_pure/backend/tools/file_broker.py:56-58``):
                # V1's default always_exclude DID include ``*.log``, BUT V1 only
                # applied always_exclude to REJECT an INPUT path (pre_call step 1,
                # e.g. ``read(path="x.log")``) — its result step only did
                # max_entries truncation and NEVER stripped ``*.log`` from glob /
                # grep RESULT lists. V2 instead applied always_exclude to the
                # glob/grep result LISTS (``_filter_excluded_results``), which
                # silently hid real, often-targeted files (a user globbing for
                # ``openclaw-*.log`` got fewer matches + a mismatched count).
                # That combination — hiding ``.log`` results AND wanting to
                # ``read`` them — is a net-harmful default, so ``*.log`` is
                # dropped entirely here (input + result). ``.git`` /
                # ``node_modules`` / ``__pycache__`` / ``*.pyc`` / ``.env`` remain
                # (genuine noise / secrets — keep excluding, as V1 did).
                ".git/**",
                "node_modules/**",
                "__pycache__/**",
                "*.pyc",
                ".env",
            )
        )
        self._max_entries: int = max(0, int(max_entries))
        self._project_root: Path | None = project_root
        self._write_dir_guard = write_dir_guard
        self._exec_command_guard = exec_command_guard
        self._audit_sink = audit_sink

    async def pre_call(
        self, *, tool_name: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        # ── exec: dangerous-command guard (dep/exec broker + regex) ──────
        if tool_name == "exec" and self._exec_command_guard is not None:
            command = args.get("command")
            if isinstance(command, str) and command.strip():
                reason = self._exec_command_guard(command)
                # The dep-broker approval path is async (it blocks until the
                # operator approves / rejects / the timeout elapses); the
                # static dangerous-pattern path is sync. Await an awaitable.
                if inspect.isawaitable(reason):
                    reason = await reason
                if reason:
                    self._audit("exec", command, reason)
                    raise ToolGuardDenied(
                        message=f"FileBroker: command blocked by security policy: {reason}",
                        error_code="ai_coding.tool.exec_denied",
                    )

        # ── path tools: always_exclude + dangerous write-dir ─────────────
        keys = _FILE_TOOL_PATH_KEYS.get(tool_name, ())
        is_write = tool_name in _WRITE_TOOLS
        for key in keys:
            value = args.get(key)
            if not isinstance(value, str) or not value:
                continue
            if self._is_excluded(value):
                self._audit(tool_name, value, "path_excluded")
                raise ToolGuardDenied(
                    message=f"FileBroker: path blocked by exclude policy: {value}",
                    error_code="ai_coding.tool.path_excluded",
                )
            if (
                is_write
                and self._write_dir_guard is not None
                and self._write_dir_guard(value)
            ):
                self._audit(tool_name, value, "dangerous_write_dir")
                raise ToolGuardDenied(
                    message=(
                        f"FileBroker: write denied to protected system/user directory: {value}"
                    ),
                    error_code="ai_coding.tool.write_denied",
                )
        return args

    async def post_call(
        self, *, tool_name: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        if tool_name not in _TRUNCATABLE_TOOLS:
            return result

        # ── always_exclude result filtering ─────────────────────────────
        # ``pre_call`` only rejects an excluded *input* path; a directory
        # ``glob`` / ``grep`` would otherwise still list / return excluded
        # entries (e.g. enumerate ``.env`` into ``files`` or return ``.env``
        # match lines into ``matches`` / ``output``), leaking secret-bearing
        # files to the model. Drop any result entry whose path matches
        # ``always_exclude`` (reusing the same predicate as ``pre_call``).
        result = self._filter_excluded_results(result)

        if self._max_entries == 0:
            return result
        # Truncate canonical list field names; tools below emit ``files``
        # for glob and ``matches`` for grep.
        for field in ("files", "matches"):
            value = result.get(field)
            if isinstance(value, list) and len(value) > self._max_entries:
                truncated = list(value[: self._max_entries])
                result = {**result, field: truncated, "truncated": True}
        return result

    def _filter_excluded_results(
        self, result: dict[str, Any]
    ) -> dict[str, Any]:
        """Drop ``always_exclude``-matching entries from glob/grep results.

        - ``files``: list of path strings (glob) — drop excluded paths, then
          recompute the ``message`` ``"<N> file(s) matched"`` count so the
          human-readable summary stays consistent with the (now shorter)
          ``files`` list (no "lists 5 but says 6 matched" skew).
        - ``matches``: list of ``{"path", "line", "text"}`` dicts (grep) —
          drop entries whose ``path`` is excluded, then recompute
          ``match_count`` / ``file_count`` and prune matching lines from the
          human-readable ``output`` string so no excluded content leaks.
        """
        files = result.get("files")
        if isinstance(files, list):
            kept_files = [
                f
                for f in files
                if not (isinstance(f, str) and self._is_excluded(f))
            ]
            if len(kept_files) != len(files):
                result = {**result, "files": kept_files}
                # Keep the glob summary count consistent with the filtered
                # list. ``_render_glob`` (search.py) builds the message as
                # ``"<N> file(s) matched"`` BEFORE this post-filter runs, so a
                # dropped path would otherwise leave a stale higher count
                # ("lists 5 paths but says 6 matched"). Rewrite the leading
                # count to ``len(kept_files)``, preserving any trailing
                # suffix (e.g. "; results truncated at ...").
                result = self._recount_glob_message(result, len(kept_files))

        matches = result.get("matches")
        if isinstance(matches, list):
            kept_matches = [
                m
                for m in matches
                if not (
                    isinstance(m, dict)
                    and isinstance(m.get("path"), str)
                    and self._is_excluded(m["path"])
                )
            ]
            if len(kept_matches) != len(matches):
                kept_paths = {
                    m["path"]
                    for m in kept_matches
                    if isinstance(m, dict) and isinstance(m.get("path"), str)
                }
                new_result = {
                    **result,
                    "matches": kept_matches,
                    "match_count": len(kept_matches),
                    "file_count": len(kept_paths),
                }
                output = result.get("output")
                if isinstance(output, str) and output:
                    new_result["output"] = self._prune_output_lines(
                        output, kept_paths
                    )
                result = new_result

        return result

    # Leading ``"<N> file(s) matched"`` count produced by ``_render_glob``
    # (search.py). Captured so the post-filter can rewrite ONLY the count,
    # preserving any trailing suffix ("; results truncated at ...", etc.).
    _GLOB_COUNT_RE = re.compile(r"^\d+ file\(s\) matched")

    def _recount_glob_message(
        self, result: dict[str, Any], new_count: int
    ) -> dict[str, Any]:
        """Rewrite a glob ``message`` count to ``new_count`` after filtering.

        ``_render_glob`` builds the summary ``"<N> file(s) matched[; ...]"``
        BEFORE this broker post-filter drops ``always_exclude`` paths, so a
        dropped file would leave a stale higher count inconsistent with the
        shorter ``files`` list (the reported "lists 5 but says 6 matched"
        skew). Replace only the leading count token, leaving any trailing
        suffix intact. No-op when the message does not start with the
        expected count token (e.g. the "no files matched" branch) — the
        ``files`` list is already authoritative there.
        """
        message = result.get("message")
        if not isinstance(message, str):
            return result
        new_message, n = self._GLOB_COUNT_RE.subn(
            f"{new_count} file(s) matched", message, count=1
        )
        if n == 0 or new_message == message:
            return result
        return {**result, "message": new_message}

    def _prune_output_lines(
        self, output: str, kept_paths: set[str]
    ) -> str:
        """Keep only ``output`` lines referencing a non-excluded path.

        Grep ``output`` lines look like ``> <path>:<line>:<text>`` (or a
        leading space for context lines); the ``--`` separators are dropped
        alongside removed chunks. A line is kept when one of ``kept_paths``
        appears as its path segment, so excluded files' content never leaks.
        """
        if not kept_paths:
            return ""
        kept_lines: list[str] = []
        for line in output.splitlines():
            if line == "--":
                continue
            if any(f"{p}:" in line for p in kept_paths):
                kept_lines.append(line)
        return "\n".join(kept_lines).rstrip()

    def _audit(self, op: str, path: str, reason: str) -> None:
        if self._audit_sink is None:
            return
        try:
            self._audit_sink(op, path, reason)
        except Exception:  # noqa: BLE001 — audit must never break a tool call
            pass

    def _is_excluded(self, path: str) -> bool:
        rel = path
        if self._project_root is not None:
            try:
                rel = os.path.relpath(path, self._project_root).replace(
                    "\\", "/"
                )
            except (ValueError, TypeError):
                rel = path
        else:
            rel = path.replace("\\", "/")
        basename = os.path.basename(path)
        for pattern in self._always_exclude:
            if fnmatch.fnmatch(rel, pattern):
                return True
            if fnmatch.fnmatch(basename, pattern):
                return True
        return False
