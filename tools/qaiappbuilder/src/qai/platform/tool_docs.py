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
* **Pure text.** No IO, no side effects, no runtime env lookups —
  description text is baked at import time and is safe to splice into
  any OpenAI function-calling schema dict. The one callable here
  (:func:`subtitle_param_description`) is a pure string builder for the
  same reason: it is evaluated once, at schema-definition time.
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
    "subtitle_param_description",
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
# The aliases are the LLM-facing surface; each tool's resolver
# translates them to a concrete executable path (see
# ``exec._resolve_portable_git_shell`` / ``manager._resolve_shell_alias``).
# The enum ordering is deliberate (auto first = default, then increasing
# specificity) so tool-list UIs render the safest choice at the top.
# ``bash`` is accepted as a synonym of ``sh`` (2026-08-09): both resolvers
# already map either spelling to PortableGit's bash, and models reach for
# ``bash`` far more readily than ``sh`` when writing heredocs.

SHELL_ALIAS_ENUM: tuple[str, ...] = ("auto", "cmd", "powershell", "sh", "bash")

SHELL_ALIAS_DESCRIPTION: str = (
    "Shell interpreter: 'auto' (default, auto-detect based on syntax / "
    "platform default), 'cmd' (force cmd.exe), 'powershell' (force "
    "PowerShell 5.1 — powershell.exe), or 'sh' / 'bash' (force PortableGit "
    "bash — required for POSIX tools like ls, grep, cat, mv, rm, for shell "
    "scripts with a #!/bin/bash shebang, AND for any MULTI-LINE POSIX "
    "payload such as a heredoc (`<<'EOF' … EOF`) or a herestring (`<<<`). "
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
    "- Multi-line payloads keep their real newlines, so a heredoc runs as "
    "one script; quote the delimiter (<<'EOF') to stop the shell expanding\n"
    "  $VAR inside the body. Use this for a multi-line script in a language\n"
    "  this shell can launch (node, another shell). For PYTHON, prefer the\n"
    "  run_code tool — it keeps state between calls and needs no nested\n"
    "  quoting; `python -c` / `python - <<EOF` starts a fresh interpreter\n"
    "  every time and discards everything it built.\n"
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
    "sed/awk), 'write' to create files (not echo >), 'run_code' to run "
    "Python (not python -c / a heredoc — a fresh interpreter each time "
    "keeps nothing, while 'run_code' carries state between calls). Use "
    "the shell tools above for what genuinely needs a shell (git, "
    "builds, running scripts)."
)


# ---------------------------------------------------------------------------
# ``description`` PARAMETER wording — the 7th fragment (2026-08-09)
# ---------------------------------------------------------------------------
#
# The six fragments above are all TOOL-level prose. None of them covered a
# PARAMETER-level ``description``, so every tool that wants a card subtitle
# wrote its own wording and they drifted: ``exec`` said the four things a
# model needs to hear (who reads it, what shape, three worked examples, an
# explicit nudge) and reliably got a subtitle back, while
# ``background_process`` said "Short label shown in the sidebar" — six words,
# no example, no nudge — and reliably got nothing. That silence is not
# cosmetic: concurrent background results are merged into one message and
# separated only by ``── Job <id> (<label>) ──``, so an empty label leaves
# the model unable to tell which result belongs to which job.
#
# Hence a template rather than a constant. The skeleton (who reads it →
# expected shape → examples → encouragement) is what must not drift; the
# examples MUST differ per tool, because a one-shot command and a
# long-running service are named differently ("Run unit tests" vs "Dev
# server for web UI") and a model shown the wrong genre of example copies
# the genre. ``surface`` and ``encourage`` are holes for the same reason:
# the process sidebar only exists for the long-running tool, and a service
# you can leave running for an hour deserves a stronger nudge than a
# one-line ``echo``.
#
# The default arguments reproduce ``exec``'s 2026-07 wording VERBATIM — it
# is the measured-good baseline, so the extraction must be a no-op for it.

def subtitle_param_description(
    *,
    examples: tuple[str, ...],
    surface: str = "the tool-card subtitle in the chat",
    encourage: str = "anything beyond a trivial one-liner",
) -> str:
    """Build the wording for a tool's ``description`` parameter.

    Args:
        examples: Two or more action-first phrases in this tool's own
            idiom, rendered as the ``e.g. '...', '...'`` list. Required
            and non-empty: an example-less nudge is exactly the wording
            that failed to produce labels.
        surface: Where the value is rendered, as a noun phrase slotted
            into "shown as ``<surface>``". Defaults to ``exec``'s chat
            tool card.
        encourage: What the value is strongly encouraged FOR, slotted
            into "strongly encouraged for ``<encourage>``".

    Raises:
        ValueError: ``examples`` is empty.
    """
    if not examples:
        raise ValueError(
            "subtitle_param_description requires at least one example — "
            "an example-less description is the wording that measurably "
            "fails to elicit a label"
        )
    rendered = ", ".join(f"'{example}'" for example in examples)
    return (
        f"A brief phrase naming what this call is for — shown as "
        f"{surface} so the user can follow the assistant's work at a "
        f"glance without expanding each command. Aim for an action-first "
        f"phrase (a few words is plenty), e.g. {rendered}. Optional but "
        f"strongly encouraged for {encourage}."
    )


# ---------------------------------------------------------------------------
# Background-delivery contract — the 8th fragment (2026-08-09)
# ---------------------------------------------------------------------------
#
# Any tool whose work can outlive its own call owes the model the SAME three
# facts, and they must not drift between tools: the main agent and a
# sub-agent hitting the identical situation cannot be told different things.
#
#   1. the result arrives on its own — so there is nothing to collect;
#   2. a user message arriving first does NOT mean the work failed — this is
#      the misread that makes a model re-run a task that is still running, or
#      report a failure that never happened;
#   3. concurrent results are merged into one message and separated by id —
#      so attribution is by id, never by position.
#
# Stated positively rather than as a prohibition: telling a model "results
# auto-deliver, you never need to poll" removes the REASON to poll, whereas
# "do not poll" leaves it guessing how the result will otherwise appear —
# which is how a single hand-off became minutes of repeated waiting.
BACKGROUND_DELIVERY_CONTRACT_SECTION: str = (
    "# When this outlives the call\n"
    "This tool's work can continue after the call returns (you sent a new "
    "message mid-run, or it was handed to the background-process manager). "
    "When that happens:\n"
    "- Its result is delivered to you AUTOMATICALLY once it finishes. You "
    "never need to poll for it, and you do not have to keep the conversation "
    "waiting on it.\n"
    "- Seeing a new user message BEFORE that result does NOT mean the work "
    "failed or was cancelled — it is still running. Do not re-run it and do "
    "not tell the user it failed; handle the new message and the result will "
    "still arrive.\n"
    "- When several finish together their results arrive in one message, each "
    "under its own id header — attribute each result by that id, never by "
    "position."
)
