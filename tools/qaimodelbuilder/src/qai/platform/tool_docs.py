# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared LLM tool description fragments.

Both the ``exec`` (:mod:`qai.ai_coding.infrastructure.tools.handlers._shared`)
and ``background_process``
(:mod:`qai.platform.background_process.tool_schemas`) LLM tools describe the
**same execution environment** (Windows on ARM host, PortableGit Unix
toolchain, PowerShell 5.1 / bash / cmd shell options) and share the **same
shell-selection alias set** (``auto`` / ``cmd`` / ``powershell`` / ``sh``).
Historically each tool duplicated the environment section in its own JSON
schema description — 2026-07-13 refactor extracts the shared fragments here
so a change to the toolchain catalogue / shell notes / workdir guidance
propagates to both tools in a single edit.

Design constraints:

* **No runtime imports from bounded contexts.** This module lives under
  ``qai.platform`` so any bounded context (``qai.ai_coding``,
  ``qai.chat``, ...) may import it without violating the
  ``context-isolation`` import-linter contract
  (``.importlinter`` §Contract 3).
* **Pure text constants.** No IO, no side effects, no runtime env
  lookups — description text is baked at import time and is safe to
  splice into any OpenAI function-calling schema dict.
* **Tool-specific pieces stay in the tool.** Only genuinely shared
  guidance is here; each tool's unique semantics (one-shot vs
  long-running, ``timeout`` / ``ready`` params, ``action`` set) live in
  its own schema file.
"""

from __future__ import annotations

__all__ = [
    "AVAILABLE_TOOLS_SECTION",
    "SHELL_ALIAS_ENUM",
    "SHELL_ALIAS_DESCRIPTION",
    "SHELL_NOTES_SECTION",
    "WORKDIR_GUIDANCE_SECTION",
    "PREFER_DEDICATED_TOOLS_SECTION",
]


# ---------------------------------------------------------------------------
# AVAILABLE TOOLS — PortableGit Unix toolchain catalogue
# ---------------------------------------------------------------------------
#
# The tool list mirrors what Setup.bat guarantees is present under
# ``%LOCALAPPDATA%\QAIModelBuilder\git`` (PortableGit bundle). Historically
# on-device models missed the availability of ``git`` / ``bash`` / ``grep`` /
# ``sed`` and reached for ``cmd`` built-ins that behave badly (encoding,
# quoting), or hallucinated tools that are not present (``rsync`` / ``jq``).
# Keeping the catalogue verbatim in the description significantly improved
# tool-call quality; do NOT truncate it to a summary.

AVAILABLE_TOOLS_SECTION: str = (
    "AVAILABLE TOOLS: This environment includes PortableGit with Unix "
    "tools. You can use: git, bash, sh, grep, sed, awk, find, diff, "
    "patch, curl, tar, gzip, xargs, sort, uniq, wc, head, tail, cut, tr, "
    "tee, cat, ls, cp, mv, rm, mkdir, touch, chmod, ssh, scp. These are "
    "native ARM64 binaries and work without WSL. "
    "IMPORTANT: To use Unix tools (ls, grep, cat, etc.), you MUST "
    "explicitly set shell='sh' — they do NOT work under shell='cmd' or "
    "shell='auto' (which may resolve to cmd.exe)."
)


# ---------------------------------------------------------------------------
# Shell alias set — kept in sync between ``exec`` and ``background_process``
# ---------------------------------------------------------------------------
#
# The four aliases are the LLM-facing surface; each tool's resolver
# translates them to a concrete executable path (see
# ``exec._resolve_portable_git_shell`` / ``manager._resolve_shell_alias``).
# The enum ordering is deliberate (auto first = default, then increasing
# specificity) so tool-list UIs render the safest choice at the top.

SHELL_ALIAS_ENUM: tuple[str, ...] = ("auto", "cmd", "powershell", "sh")

SHELL_ALIAS_DESCRIPTION: str = (
    "Shell interpreter: 'auto' (default, auto-detect based on syntax / "
    "platform default), 'cmd' (force cmd.exe), 'powershell' (force "
    "PowerShell 5.1 — powershell.exe), or 'sh' (force PortableGit bash — "
    "required for POSIX tools like ls, grep, cat, mv, rm, and for shell "
    "scripts with a #!/bin/bash shebang). "
    "IMPORTANT: if the command already starts with 'sh -c' or 'bash -c', "
    "set shell='sh' directly — do NOT use shell='cmd' or shell='auto' to "
    "wrap it again, as that adds an unnecessary extra shell layer."
)


# ---------------------------------------------------------------------------
# Shell notes — syntax reminders per shell
# ---------------------------------------------------------------------------
#
# Kept minimal: each block is only the handful of gotchas that on-device
# models actually get wrong (chaining syntax, quote style, alias vs full
# cmdlet name, path separator under bash). More elaborate coaching lives
# in the operator-facing docs, not in every LLM tool call.

SHELL_NOTES_SECTION: str = (
    "# PowerShell 5.1 shell notes (when shell='powershell' or auto)\n"
    "- Chain dependent commands: `cmd1; if ($?) { cmd2 }`\n"
    "- Double quotes for interpolated strings (\"Hello $name\"), "
    "single quotes for verbatim strings.\n"
    "- Prefer full cmdlet names: Get-ChildItem, Set-Content, "
    "Remove-Item, New-Item (not aliases).\n"
    "- Use $(...) for subexpressions, @(...) for array expressions.\n"
    "- Call executables with spaces in path: & \"path/to/exe\" args\n"
    "- Escape special characters with the backtick character.\n\n"
    "# sh/bash shell notes (when shell='sh')\n"
    "- Use POSIX sh syntax; avoid bashisms for portability.\n"
    "- Chain commands: `cmd1 && cmd2` or `cmd1; cmd2`\n"
    "- Windows paths: use forward slashes or /c/Users/... form.\n"
    "- Do NOT use & or nohup to background processes — use the "
    "background_process tool instead."
)


# ---------------------------------------------------------------------------
# Working-directory guidance
# ---------------------------------------------------------------------------
#
# Both tools have a working-directory parameter (``exec`` uses ``cwd``,
# ``background_process`` uses ``workdir``); we phrase the guidance
# generically so the same fragment fits both. The rule follows the
# standard shell-tool convention: never ``cd`` inside the command — the
# tool parameter is the single source of truth for the child's initial
# directory, and combining it with a mid-command ``cd`` produces path
# bugs when the command is retried / resumed / logged.

WORKDIR_GUIDANCE_SECTION: str = (
    "WORKDIR: The command runs in the current working directory by "
    "default. Use the working-directory parameter (``cwd`` for exec, "
    "``workdir`` for background_process) to run in a different directory. "
    "AVOID changing directories inside the command (no 'cd foo && ...' or "
    "'Set-Location') — use the parameter instead."
)


# ---------------------------------------------------------------------------
# "Prefer dedicated tools" — steer models away from shell-based file I/O
# ---------------------------------------------------------------------------
#
# On-device models otherwise reach for ``cat`` / ``echo >`` / ``sed`` when
# a dedicated file tool exists in the same schema. The dedicated tools are
# cross-shell portable, produce structured output the tool-router can
# stream, and interact correctly with the local file-guard hook.

PREFER_DEDICATED_TOOLS_SECTION: str = (
    "PREFER the dedicated tools over shell for file work when they "
    "cover the same need: 'glob' to find files (not ls/find/dir), "
    "'grep' to search contents (not grep -r/findstr), 'read' to read "
    "files (not cat/type), 'edit'/'apply_patch' to change files (not "
    "sed/awk), 'write' to create files (not echo >). Use the shell "
    "tools above for what genuinely needs a shell (git, builds, "
    "running scripts)."
)
