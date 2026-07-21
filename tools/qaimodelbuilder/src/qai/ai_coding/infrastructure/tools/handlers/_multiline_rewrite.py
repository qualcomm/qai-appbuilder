# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Multi-line command materialisation for the ``exec`` tool.

Problem
-------
A multi-line command string cannot be carried through Windows ``cmd.exe``:

* ``create_subprocess_exec(["cmd", "/c", command])`` — Python's
  ``list2cmdline`` re-quotes the ``command`` element and cmd.exe then
  re-parses it; a multi-line body has its newlines treated as command
  separators (only the first line runs, the rest ``'import' is not
  recognized`` / ``i was unexpected at this time``).
* ``create_subprocess_shell("cmd.exe /c \"cmd1\ncmd2\"")`` — cmd.exe runs
  only ``cmd1`` and silently drops the remaining lines.

Design — ZERO-PARSE materialisation
-----------------------------------
Earlier revisions tried to *parse* the command with a regex to pull out a
``python -c "<script>"`` segment and rewrite it. That is fundamentally
unreliable: a model emits free-form command strings, and no regex (nor
``shlex``) can robustly recover a Python script embedded in an arbitrary,
possibly-malformed, cmd.exe-targeted command line (unescaped inner quotes,
Windows backslash paths, ``py -3 -c`` / ``python -u -c`` prefixes, …). It
produced *silent wrong captures* (worst kind of failure).

So we DO NOT parse the command at all. When the resolved shell is ``cmd``
and the command spans multiple (non-blank) lines, we write the command
**verbatim, line-by-line** into a temporary ``.bat`` and run
``["cmd", "/c", "<tmp>.bat"]``. cmd.exe reads the ``.bat`` line-by-line and
applies its OWN, well-defined parsing to each line — preserving ``&&`` /
pipes / redirects / ``%VAR%`` / quoting exactly as the model intended. We
never inspect or transform the command's content, so there is no parser and
no parser fragility.

Scope note (``python -c "<multi-line>"``)
-----------------------------------------
A multi-line ``python -c "<script>"`` inside a ``.bat`` still fails, because
cmd.exe splits the embedded newlines. That is *correct* and *safe*: the run
fails with a clear cmd.exe / Python error on stderr, which the model reads
and self-corrects (the robust pattern is to write the script to a ``.py``
file and run that, or keep ``-c`` single-line). We deliberately do NOT try
to auto-extract the script — a code-level "fix" there cannot be made safe
for arbitrary model output, so per project policy it is left for the model
to correct rather than shipped as an unstable heuristic.

powershell / sh are never materialised here: powershell (invoked via
``-EncodedCommand``) and ``/bin/sh -c`` both carry multi-line payloads
natively.

Each rewriter returns the materialised temp path(s) alongside the rewritten
argv / command string; the caller unlinks them after the child completes
(see :func:`cleanup_temp_scripts`).
"""

from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger("qai.ai_coding.tools.exec")

#: Minimum non-empty line count before a command is treated as multi-line.
_MIN_MULTILINE_LINES = 2

__all__ = [
    "cleanup_temp_script",
    "cleanup_temp_scripts",
    "rewrite_multiline_to_argv",
    "rewrite_multiline_to_command_str",
]


def _materialisation_temp_dir() -> str | None:
    r"""Return an isolated, cleanable temp dir for materialised scripts.

    Materialised ``.bat`` bodies are landed under
    ``%TEMP%\QAIModelBuilder\default`` (rather than the bare ``%TEMP%`` root)
    so they are isolated from user data and trivially cleanable as a group.
    Returns ``None`` if the directory cannot be created, in which case the
    caller falls back to the system default temp location.
    """
    root = (
        Path(os.environ.get("TEMP", tempfile.gettempdir()))
        / "QAIModelBuilder"
        / "default"
    )
    try:
        root.mkdir(parents=True, exist_ok=True)
        return str(root)
    except OSError:
        return None


def _is_multiline_cmd(command: str, resolved_shell: str) -> bool:
    """True iff *command* must be materialised into a ``.bat``.

    Only the ``cmd`` shell is materialised (powershell / sh carry multi-line
    payloads natively), and only when the command has at least two non-blank
    lines. This is a pure structural check — the command CONTENT is never
    parsed or interpreted.
    """
    if resolved_shell != "cmd":
        return False
    if "\n" not in command:
        return False
    non_blank = [ln for ln in command.split("\n") if ln.strip()]
    return len(non_blank) >= _MIN_MULTILINE_LINES


def _write_multiline_bat(command: str) -> Path:
    r"""Materialise *command* verbatim (line-by-line) into a temp ``.bat``.

    ZERO-PARSE: the command's non-blank lines are written unchanged. A
    ``@echo off`` + ``chcp 65001`` (UTF-8 code page) prologue is prepended so
    child stdout for non-ASCII text is UTF-8; CRLF line endings are used (what
    cmd.exe expects). cmd.exe reads the ``.bat`` line-by-line, applying its own
    parsing — we do not transform the content.
    """
    lines = [ln for ln in command.split("\n") if ln.strip()]
    bat_content = (
        "@echo off\r\nchcp 65001 >nul\r\n" + "\r\n".join(lines) + "\r\n"
    )
    tmp_dir = _materialisation_temp_dir()
    tmp = tempfile.NamedTemporaryFile(  # noqa: SIM115 — delete=False: handle is
        # closed below so cmd.exe can reopen the .bat by name for execution.
        suffix=".bat",
        delete=False,
        mode="w",
        encoding="utf-8",
        dir=tmp_dir,
        newline="",
    )
    try:
        tmp.write(bat_content)
    finally:
        tmp.close()
    tmp_path = Path(tmp.name)
    logger.info(
        "exec: materialised multi-line command to temp .bat (lines=%d, tmp=%s)",
        len(lines),
        tmp_path,
    )
    return tmp_path


def rewrite_multiline_to_argv(
    command: str, resolved_shell: str
) -> tuple[list[str] | None, list[Path]]:
    """Rewrite a multi-line ``cmd`` command into a structured ``argv``.

    For the ``create_subprocess_exec`` paths (one-shot ``tool_exec`` and the
    ai_coding streaming engine). Returns ``(["cmd", "/c", "<tmp>.bat"], [bat])``
    when the command is a multi-line cmd body, else ``(None, [])``. The ``.bat``
    path is its own argv element, so CreateProcess / cmd.exe never re-parse a
    quoted command string. The caller unlinks the temp path(s) via
    :func:`cleanup_temp_scripts` once the child has finished.
    """
    if not _is_multiline_cmd(command, resolved_shell):
        return None, []
    tmp_bat = _write_multiline_bat(command)
    return ["cmd", "/c", str(tmp_bat)], [tmp_bat]


def rewrite_multiline_to_command_str(
    command: str, resolved_shell: str
) -> tuple[str | None, list[Path]]:
    r"""Rewrite a multi-line ``cmd`` command into a shell command STRING.

    For the chat ``create_subprocess_shell`` path (``di.py``), which hands a
    single command string to the platform default shell (``cmd.exe`` on
    Windows). Returns ``('cmd.exe /c "<tmp>.bat"', [bat])`` for a multi-line
    cmd body, else ``(None, [])``. The ``.bat`` path is the only quoted token,
    so the outer shell parses it cleanly. Caller unlinks via
    :func:`cleanup_temp_scripts`.
    """
    if not _is_multiline_cmd(command, resolved_shell):
        return None, []
    tmp_bat = _write_multiline_bat(command)
    return f'cmd.exe /c "{tmp_bat}"', [tmp_bat]


def cleanup_temp_script(tmp_path: Path | None) -> None:
    """Best-effort removal of a single temp script created by a rewriter."""
    if tmp_path is None:
        return
    try:
        tmp_path.unlink()
    except OSError as exc:  # pragma: no cover - best-effort cleanup
        logger.debug("exec: failed to unlink temp script %s: %s", tmp_path, exc)


def cleanup_temp_scripts(tmp_paths: list[Path]) -> None:
    """Best-effort removal of every temp file from a rewrite."""
    for tmp_path in tmp_paths:
        cleanup_temp_script(tmp_path)
