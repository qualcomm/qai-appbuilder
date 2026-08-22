# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Exec exit-diagnostics — V1-faithful hints for failing commands.

Ported verbatim (text + logic) from V1
``backend/tools/_exec.py::_format_exit_diagnostics`` /
``_format_silent_failure_hint`` and
``backend/security/access_error_helper.py::build_sandbox_access_denied_hint``.

Problem this solves: when a command exits non-zero but produces little or
no output, the model only sees ``[exit code: N]`` and has no idea what
went wrong, so it flails.  V1 inspected the (stdout, stderr, exit_code,
command) tuple and appended a short, targeted hint block.  V2 had dropped
this; this module restores it (V1 parity, no regression).

The result is appended (by the chat tool-result renderer) AFTER the
``[exit code: N]`` marker, exactly like V1's
``output_parts.append(_diag)``.

Access-denied attribution: why only THIS path is vague
------------------------------------------------------
``Access is denied`` reads identically whether FileGuard, a Windows ACL, a
read-only attribute, or a file lock caused it. Four tool paths hit that
wording, and each knows a different amount:

* ``edit`` / ``write`` — probes ``os.access(path, os.W_OK)`` BEFORE writing
  (``_safe_commit._reject_unsafe_target``), so it names the real cause
  ("refusing to overwrite read-only file") and needs no hint at all.
* ``run_code`` — a denial the user's own code caught and printed lands in
  STDOUT. Content, not an error channel; never scanned (see the gate in
  ``apps/api/_chat_tool_result_render.py``).
* ``background_process`` — the manager's audit probe queries the audit log
  by pid tree, so a real FileGuard block is CONFIRMED and gets the
  authoritative ``[FileGuard]`` note.
* ``exec`` subprocess stderr — THIS module. All we have is text from
  someone else's process: no pre-probe (we do not know which files it will
  touch), and a later ``os.access`` would answer about a different moment
  and a different identity. Hence :func:`build_access_denied_ambiguous_hint`
  names both causes, ordered by likelihood, instead of guessing.

So the vagueness is missing evidence, not sloppy wording — do not "unify"
the four paths onto one message. Callers that DO know the source must pass
``attribution="confirmed_fileguard"``.
"""

from __future__ import annotations

import re
from typing import Literal

__all__ = [
    "ACCESS_DENIED_SIGNALS",
    "HintAttribution",
    "append_fileguard_hint_if_denial",
    "build_access_denied_ambiguous_hint",
    "build_fileguard_denial_hint",
    "command_has_explicit_redirect",
    "format_exit_diagnostics",
    "text_has_access_denied_signal",
]


# ---------------------------------------------------------------------------
# Explicit-redirect detection (V1 _exec.py:323-364 _REDIRECT_RE).
# ---------------------------------------------------------------------------
# Matches user-supplied stdout/stderr redirection so the silent-failure hint
# is skipped (empty output is expected when the user redirected it away).
# ``2>&1`` is deliberately NOT treated as a file redirect (it merges stderr
# into stdout; the output still comes back to the tool).
_REDIRECT_RE = re.compile(
    r"""
    (?:
        \s>>?\s*[^\s&|<>]+      # `>file` / `>> file` (stdout → file)
      | \s2>>?\s*[^\s&|<>]+     # `2> file` / `2>> file` (stderr → file)
      | \s&>\s*[^\s&|<>]+       # `&> file` (PowerShell / bash merge → file)
      | \|\s*tee\b              # `| tee file`
      | \|\s*Out-File\b         # PowerShell `| Out-File`
      | \|\s*Set-Content\b      # PowerShell `| Set-Content`
      | \|\s*Add-Content\b      # PowerShell `| Add-Content`
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def command_has_explicit_redirect(command: str) -> bool:
    """Return True when *command* explicitly redirects stdout/stderr to a file.

    V1 parity (``_exec.py:346-364``).  Used to skip the silent-failure hint:
    empty output is expected (not a failure) when the user redirected it.
    """
    if not command:
        return False
    return bool(_REDIRECT_RE.search(" " + command + " "))


# ---------------------------------------------------------------------------
# Access-denied signal detection — SINGLE SOURCE OF TRUTH (2026-07-23).
# ---------------------------------------------------------------------------
# Every textual wording that means "a file/exec operation was denied" (Win32,
# POSIX/msys, PowerShell, Python, Chinese, Win32 error code). Both this module's
# exit-diagnostics Case-1 check AND the chat renderer's universal FileGuard-hint
# injector (``apps.api._chat_tool_result_render._maybe_append_fileguard_hint``)
# consume this ONE list, so a new denial wording is added in exactly one place
# and can never drift between the two detectors (the P1-8 double-maintenance
# defect). All entries are lowercase; callers match against a ``.lower()``-ed
# haystack (CJK entries are case-insensitive by nature).
ACCESS_DENIED_SIGNALS: tuple[str, ...] = (
    "access is denied",       # Win32 (cmd `type`, most Win32 APIs)
    "access denied",          # Win32 short form
    "is denied",              # PowerShell "Access to the path ... is denied"
    "permission denied",      # POSIX / msys (mv, rm, bash, ln, chmod ...)
    "operation not permitted",  # POSIX EPERM
    "unauthorizedaccessexception",  # .NET / PowerShell
    "permissionerror",        # Python
    "errno 13",               # Python EACCES
    "error 5",                # Win32 ERROR_ACCESS_DENIED numeric
    "error code 5",           # Win32 numeric (alt phrasing)
    "fileguard denied",       # our in-process ToolGuardDenied message wording
    "denied by the security policy",  # exec-broker deny suffix
    "拒绝访问",                # Chinese (Windows localized / Python)
)


def text_has_access_denied_signal(text: str) -> bool:
    """Return ``True`` iff *text* carries any access-denied wording.

    Single detector shared by exit-diagnostics and the universal FileGuard
    hint injector (see :data:`ACCESS_DENIED_SIGNALS`). Case-insensitive.
    """
    if not text:
        return False
    low = text.lower()
    return any(sig in low for sig in ACCESS_DENIED_SIGNALS)


def build_fileguard_denial_hint() -> str:
    """Concise FileGuard-denial guidance appended to any tool output that
    carries an access-denied signal (2026-07-23 V2, per user directive).

    Focused rewrite of the earlier :func:`_build_access_denied_hint` for the
    "give the model an instruction" use case (as opposed to that helper's
    developer-facing 5-cause diagnostic checklist). Single subject, no
    alternative interpretations, no cause enumeration:

    * names the source (``[FileGuard]`` — this application's security policy);
    * asserts it is an enforced boundary, not a transient error;
    * enumerates the common bypass attempts and says they fail identically;
    * gives the ONE correct action (stop; ask the user to authorize).

    The leading ``[FileGuard]`` token is also the idempotency marker used by
    :func:`append_fileguard_hint_if_denial` (matches the ``[FileGuard]``
    prefix emitted by
    :func:`qai.security.domain.native_guard_denial_message.build_native_guard_denial_note`
    too, so the two never double-append).
    """
    return (
        "\n\n[FileGuard] Denied by this application's security policy. "
        "This is an enforced boundary, not a transient error — retrying "
        "with another tool, a different path form, symlinks, or a copy "
        "will fail identically. Stop this operation; if access is truly "
        "required, ask the user to authorize the path in Security → Allow "
        "Lists."
    )


def build_access_denied_ambiguous_hint() -> str:
    """Neutral access-denied guidance when the source of the denial CANNOT
    be confirmed as this application's FileGuard (2026-07-23 X-tier).

    Purely-textual callers — i.e. code that only saw an ``Access is denied``
    / ``Permission denied`` / ``拒绝访问`` / ``Errno 13`` / ``error 5`` phrase
    in a subprocess's stdout+stderr — cannot distinguish a FileGuard block
    from a native Windows ACL / file-lock / read-only-attribute denial.
    Emitting :func:`build_fileguard_denial_hint` on such text would falsely
    attribute a system-level ACL denial to FileGuard and hand the model
    misleading guidance (the user hit this exact false positive on files
    Windows silently repurposed for kernel dumps).

    This neutral variant:

    * does NOT claim ``[FileGuard]`` — the source is unknown;
    * enumerates both plausible causes with the correct action for each;
    * still tells the model to STOP retrying and surface the error to the
      user — safe under either interpretation.

    Wording order (2026-08-09): the OS-level cause is stated FIRST and the
    FileGuard cause second, with a concrete discriminator (this app's own
    denials carry a bracketed FileGuard marker). The previous text led with
    FileGuard and spent more words on it, which biases a reader towards the
    app-policy reading even when the evidence points at the OS — an agent
    reviewing a plain read-only-attribute failure flagged exactly that skew.
    The OS case
    is also the more likely one in practice: a real FileGuard block on a path
    this app itself gated normally arrives through the CONFIRMED channel
    (``ToolGuardDenied`` → :func:`build_fileguard_denial_hint`, or the
    manager's audit probe writing an authoritative note), so a denial that
    only shows up as free text is more often a plain ACL / lock / read-only
    refusal. Both actions remain "stop and surface", so the reorder cannot
    change what the model DOES — only what it tells the user the cause is.

    The leading ``[hint]`` prefix is intentional so
    :func:`append_fileguard_hint_if_denial`'s idempotency (which already
    treats any ``[hint]`` block as "guidance already present") suppresses
    duplicates regardless of which variant fired first.
    """
    return (
        "\n\n[hint] Command reported an access-denied error, and its SOURCE "
        "is not identified. Most often this is an OS-level refusal — a "
        "read-only file attribute, a Windows ACL, or another process holding "
        "the file locked — which is outside this application's control; "
        "report it to the user with the failing path rather than retrying. "
        "Less often it is this application's own security policy: those "
        "denials announce themselves with a bracketed FileGuard marker in "
        "the output, so absent such a marker do NOT assume one. If the path "
        "does look security-gated, the user can authorize it in Security → "
        "Allow Lists. Either way, retrying with other tools, path forms, or "
        "copies will not help. Stop this operation and surface the error to "
        "the user."
    )


#: Attribution vocabulary for :func:`append_fileguard_hint_if_denial`.
#: ``"confirmed_fileguard"`` — the caller KNOWS the denial came from
#: FileGuard (it caught ``ToolGuardDenied``, or is the FileGuard bridge
#: itself raising) → append the strong ``[FileGuard]`` V2 guidance.
#: ``"textual_only"`` — the caller only scanned free-text output for a
#: denial keyword (an exec subprocess's stderr, an exit-diagnostics
#: hit) and CANNOT rule out a Windows ACL / file-lock / read-only
#: attribute denial → append the neutral ``[hint]`` variant that names
#: both plausible causes without falsely attributing the denial.
HintAttribution = Literal["confirmed_fileguard", "textual_only"]


def append_fileguard_hint_if_denial(
    text: str,
    *,
    attribution: HintAttribution = "textual_only",
) -> str:
    """Append an access-denied hint to *text* iff it carries a denial signal.

    Single-source-of-truth helper (2026-07-23) consumed by every FileGuard-
    denial surface. The variant appended depends on ``attribution``:

    * ``"confirmed_fileguard"`` — the caller KNOWS the denial came from
      FileGuard (it caught ``ToolGuardDenied`` from the guard bridge, or is
      the guard bridge itself raising it). Appends the strong V2 guidance
      :func:`build_fileguard_denial_hint` — ``[FileGuard]`` prefix,
      "enforced boundary", "retry won't help", "ask user to authorize". This
      is safe because the source is verified.
    * ``"textual_only"`` (DEFAULT) — the caller only scanned free-text output
      (a subprocess's stderr, an exit-diagnostics text match) and CANNOT
      distinguish a FileGuard block from a native Windows ACL / file-lock /
      read-only-attribute denial. Appends the neutral
      :func:`build_access_denied_ambiguous_hint` — ``[hint]`` prefix,
      names BOTH plausible causes, still tells the model to stop retrying.
      This avoids the false-attribution failure mode where a system-level
      Windows ACL denial got labelled ``[FileGuard]`` and the model was told
      to "ask the user to authorize" a path FileGuard was never gating.

    Called from:

    * :mod:`apps.api._chat_tool_result_render._maybe_append_fileguard_hint`
      (thin wrapper — its own default is ``"textual_only"``, callers
      override with ``"confirmed_fileguard"`` when they know);
    * :mod:`qai.ai_coding.infrastructure.tools.registry` ``ToolGuardDenied``
      catches (in-process file / exec tools — confirmed);
    * :mod:`apps.api._chat_appbuilder_tool_bridge` ``_InputRejected`` returns
      (confirmed);
    * :mod:`apps.api.di` streaming exec ``ToolGuardDenied`` catch (confirmed)
      and post-composition textual scan (textual_only).

    Living in :mod:`qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics`
    keeps every caller inside the ``context-isolation`` import-linter
    contract (:mod:`qai.ai_coding` cannot import :mod:`apps.api`; both may
    import from ai_coding).

    Idempotent — skipped when guidance is already present. Markers (2026-07-23
    F1 cleanup):

    * ``[fileguard]`` — the confirmed-variant's own prefix AND the native
      audit-probe note prefix (:func:`build_native_guard_denial_note`
      emits ``[FileGuard] This subprocess was blocked ...``); either
      present means "the model already knows this was a FileGuard denial",
      so both variants defer to it (the audit note is more specific).
    * Any ``[hint]`` block — the neutral ambiguous hint AND every
      :func:`format_exit_diagnostics` fallback hint use this prefix. Its
      presence means "guidance is already there — defer". This is
      intentionally broad: several exit-diagnostics fallbacks (silent-
      failure, silent-with-command-path) list ``"Permission denied"`` as
      one possible cause in their prose. Without deferring, that prose
      would satisfy :func:`text_has_access_denied_signal` and cause a
      duplicate ambiguous hint to be appended — the model would see two
      overlapping hints where one (the more specific one) already covers
      the case. Deferring to any pre-existing ``[hint]`` block is correct:
      if there's a real denial it lands as its own case; if it's only
      prose-mentioned as a cause, the pre-existing hint already covers it.

    Empty / non-denial input is returned untouched.
    """
    if not text or not text_has_access_denied_signal(text):
        return text
    low = text.lower()
    if (
        "[fileguard]" in low
        or "[hint]" in low
    ):
        return text
    if attribution == "confirmed_fileguard":
        return text + build_fileguard_denial_hint()
    return text + build_access_denied_ambiguous_hint()


# ---------------------------------------------------------------------------
# Access-Denied hint — X-tier attribution split (2026-07-23).
#
# Only caller is :func:`format_exit_diagnostics` Case-1, which fires purely
# on TEXTUAL match against ACCESS_DENIED_SIGNALS in a subprocess's
# stdout/stderr. Textual match CANNOT distinguish a FileGuard block from a
# Windows ACL / file-lock / read-only-attribute denial — the wording is
# identical across all three sources. So Case-1 emits the NEUTRAL
# :func:`build_access_denied_ambiguous_hint`, which names both plausible
# causes with the correct action for each, and never falsely attributes a
# system-level denial to FileGuard (the exact false-positive the user hit
# on ``C:\p*.txt`` files Windows had silently repurposed for kernel dumps).
#
# Callers that KNOW the denial came from FileGuard (they caught a
# ``ToolGuardDenied`` from the FileGuard bridge, or are the bridge itself
# raising it) call :func:`build_fileguard_denial_hint` directly, or
# ``append_fileguard_hint_if_denial(..., attribution="confirmed_fileguard")``.
#
# ``sandboxed`` is kept for signature stability with existing call sites and
# tests (the sandbox execution chain was removed on 2026-07-01, so the flag
# has had no behavioural effect for a while).
# ---------------------------------------------------------------------------
def _build_access_denied_hint(*, sandboxed: bool) -> str:
    _ = sandboxed  # parameter retained for signature stability; unused.
    return build_access_denied_ambiguous_hint()


# ---------------------------------------------------------------------------
# Silent-failure hint (V1 _exec.py:555-678).
# ---------------------------------------------------------------------------
_MSYS2_TOOLS = frozenset({
    "bash", "sh", "grep", "sed", "awk", "tail", "head", "cat", "wc",
    "find", "sort", "uniq", "tr", "tee", "xargs", "ls", "cp", "mv",
    "rm", "mkdir", "touch", "chmod", "diff", "patch", "curl", "wget",
})

_PS_CMDLETS = frozenset({
    "get-content", "set-content", "test-path", "get-childitem",
    "get-item", "new-item", "remove-item", "copy-item", "move-item",
    "invoke-expression", "invoke-webrequest", "select-string",
    "write-host", "write-output", "start-process",
})


def _format_silent_failure_hint(
    exit_code: int, command: str, sandboxed: bool
) -> str:
    """Targeted hint for commands that exit non-zero with no output.

    V1 parity (``_exec.py:555-678``): analyse the command pattern to give a
    focused suggestion instead of a generic catch-all.
    """
    cmd_lower = command.lower().strip()

    # ── Pattern: Python script execution ──────────────────────────────
    is_python = bool(
        re.search(r'python(?:3|\.exe)?\b|\.py[\s"\']*(?:$|\s|2>&1)', cmd_lower)
    )
    if is_python:
        if re.search(r'python[^"]*\s+-c\s', cmd_lower):
            hint = (
                "Detected `python -c` inline script. Possible causes:\n"
                "  • Nested quotes truncated by the shell (cmd.exe cannot handle complex quote nesting) — rewrite as a temp .py file\n"
                "  • Script syntax error but stderr was buffered and not emitted\n"
                "Suggestion: use the write tool to save the script to a .py file, then run it with exec."
            )
        else:
            script_match = re.search(
                r'(?:python(?:3|\.exe)?["\s]+)([^\s"]+\.py)', cmd_lower
            )
            script_path = script_match.group(1) if script_match else ""
            hint = (
                "Python script exited with no output. Possible causes:\n"
                "  • Python stdout fully buffered (print is not flushed in pipe mode) — buffer lost on abnormal exit\n"
                "  • Script failed at import time (missing package, DLL load failure, etc.)\n"
                + (
                    f"  • Script file missing or wrong path: {script_path}\n"
                    if script_path
                    else ""
                )
                + "  • FileGuard audit hook blocked file access (silent PermissionError)\n"
                "Suggestion: add `-u` (e.g. `python -u script.py`) for unbuffered output; "
                "or add `import sys; sys.stdout.reconfigure(line_buffering=True)` at the top of the script."
            )
        return f"\n[hint] {hint}"

    # ── Pattern: Unix/msys2 tools (even outside sandbox) ──────────────
    first_word = re.split(r"[\s|&;]+", cmd_lower.lstrip('"'))[0]
    first_word = first_word.rstrip(".exe").split("\\")[-1].split("/")[-1]
    if first_word in _MSYS2_TOOLS:
        # Historical note: an earlier "sandboxed" branch described
        # AppContainer-specific rejection of msys2 tools; that sandbox
        # was removed 2026-07-01.  Kept a single neutral hint — PortableGit
        # / msys2 setup issues manifest identically regardless of the
        # legacy sandbox flag.
        hint = (
            f"Detected Unix tool `{first_word}`. Possible causes:\n"
            "  • PortableGit not installed or not on PATH\n"
            "  • Tool failed to start (msys2 runtime init error)\n"
            f"Suggestion: use native Windows commands (e.g. findstr for grep, type for cat) "
            "or the read/glob/grep tools."
        )
        return f"\n[hint] {hint}"

    # ── Pattern: PowerShell cmdlets / variables ───────────────────────
    has_ps_cmdlet = any(
        re.search(rf"(?:^|[\s|;]){re.escape(c)}(?:$|[\s|;])", cmd_lower)
        for c in _PS_CMDLETS
    )
    has_ps_var = bool(re.search(r"(?:^|\s)\$[a-zA-Z_]", command))
    if has_ps_cmdlet or has_ps_var:
        hint = (
            "PowerShell command exited with no output. Possible causes:\n"
            "  • Current shell is cmd.exe but the command uses PowerShell syntax — set shell='powershell'\n"
            "  • Cmdlet returned silently when a condition failed (e.g. Test-Path returns $false without error)\n"
            "  • A command after `|` failed but emitted no error\n"
            "Suggestion: confirm the shell type matches; or append `; Write-Host \"DONE: $LASTEXITCODE\"`."
        )
        return f"\n[hint] {hint}"

    # ── Pattern: Commands with paths that might not exist ─────────────
    path_match = re.search(r'"([A-Za-z]:[\\\/][^"]+)"', command)
    if not path_match:
        path_match = re.search(r"([A-Za-z]:[\\\/]\S+)", command)
    if path_match:
        mentioned_path = path_match.group(1)
        hint = (
            f"Command references path `{mentioned_path[:60]}` and exited with no output. Possible causes:\n"
            "  • Target file/directory does not exist\n"
            "  • Permission denied but stderr not emitted (cmd.exe sometimes hides the error)\n"
            "  • Wrong executable path (.exe not found)\n"
            "Suggestion: verify with `if exist \"<path>\" (echo EXISTS) else (echo NOT FOUND)`; "
            "or use the glob tool to check whether the file exists."
        )
        return f"\n[hint] {hint}"

    # ── Fallback: truly unknown ───────────────────────────────────────
    return (
        f"\n[hint] Command exited with exit code {exit_code} and produced no output.\n"
        "Possible causes:\n"
        "  • Command syntax error or shell mismatch (cmd vs PowerShell)\n"
        "  • Permission denied but no stderr emitted\n"
        "  • Command is expected to be silent (e.g. `exit 1`); if so, echo status explicitly\n"
        "Suggestion: wrap with `echo START & <command> & echo END=%ERRORLEVEL%` (cmd) or "
        "`Write-Host` to trace the execution path."
    )


#: cmd.exe wordings emitted when it tries to run a Python source line as a
#: shell command. ``PRN`` is the giveaway that a bare ``print(...)`` line was
#: read as the legacy DOS printer device.
_CMD_SHRED_SIGNALS: tuple[str, ...] = (
    "is not recognized as an internal or external command",
    "unable to initialize device prn",
    "was unexpected at this time",
)

#: Python keywords/builtins that start a STATEMENT line. cmd.exe quotes the
#: offending token, so a shredded script yields ``'import' is not recognized``.
#: Matching the quoted token (rather than any occurrence) keeps this from
#: firing on a command that merely mentions the word.
_PY_STATEMENT_TOKENS: tuple[str, ...] = (
    "import", "from", "try:", "except", "else:", "elif", "def", "class",
    "for", "while", "with", "return", "print", "if",
)


def _is_shredded_inline_script(*, command: str, combined: str) -> bool:
    """True iff a multi-line inline ``-c`` script was split by the shell.

    Deliberately conjunctive — ALL of the following must hold, so an unrelated
    "command not found" never picks up Python-script advice:

    1. the command actually invokes an inline script (``python -c`` / ``py -c``);
    2. the command spans multiple non-blank lines (the only shape the ``.bat``
       materialisation shreds — a single-line ``-c`` works fine);
    3. the output carries a cmd.exe "cannot run this line" wording;
    4. that wording names a PYTHON statement token, i.e. cmd.exe was trying to
       execute script source rather than a genuinely missing executable.

    Condition 4 is what separates "my script got shredded" from "the tool I
    wanted is not installed": the latter quotes a program name, not ``import``.
    """
    if not command:
        return False
    cmd_lower = command.lower()
    # (1) an inline -c invocation
    if not re.search(r"\b(?:python\d?|py)(?:\.exe)?\b[^\n]*\s-c\b", cmd_lower):
        return False
    # (2) multi-line (what the .bat materialisation splits)
    if len([ln for ln in command.split("\n") if ln.strip()]) < 2:
        return False
    # (3) cmd.exe refused to run a line
    if not any(sig in combined for sig in _CMD_SHRED_SIGNALS):
        return False
    # (4) ...and the refused token is Python source, not a missing program
    return any(f"'{tok}'" in combined for tok in _PY_STATEMENT_TOKENS) or (
        "unable to initialize device prn" in combined
    )


def _is_quote_stripped_inline_script(*, command: str, combined: str) -> bool:
    """True iff a single-line inline ``-c`` lost its outer quotes to the shell.

    Signature: Python reports an unterminated string literal at LINE 1 while the
    command was an inline ``-c`` invocation. The tell-tale is the echoed source
    fragment starting with a stray double quote (``"import``), because the shell
    handed Python the first whitespace-delimited token only.

    Conjunctive for the same reason as :func:`_is_shredded_inline_script`: a
    genuine unterminated-string bug in a ``.py`` FILE must not collect
    shell-quoting advice, so an inline ``-c`` invocation is required.
    """
    if not command:
        return False
    if not re.search(
        r"\b(?:python\d?|py)(?:\.exe)?\b[^\n]*\s-c\b", command.lower()
    ):
        return False
    return (
        "unterminated string literal" in combined
        or "eol while scanning string literal" in combined
    ) and '"<string>"' in combined


def format_exit_diagnostics(
    exit_code: int,
    stdout: str,
    stderr: str,
    *,
    sandboxed: bool = False,
    command: str = "",
) -> str:
    """Build a diagnostic suffix for non-zero exec exits (V1 _exec.py:410-552).

    Returns a string to append AFTER the ``[exit code: N]`` marker, or ``""``
    when no diagnostic applies (exit_code == 0, the command produced
    meaningful output, or the user explicitly redirected output).
    """
    if exit_code == 0:
        return ""

    stdout = stdout or ""
    stderr = stderr or ""
    combined_raw = stdout + "\n" + stderr
    combined = combined_raw.lower()

    # ── Case 1: Access Denied (English / Chinese / Win32 / POSIX) ─────
    # POSIX/msys tools (mv, rm, bash redirects, ln, chmod, chown, etc.) emit
    # "Permission denied" / "Operation not permitted" (EPERM) rather than the
    # Win32 "Access is denied" wording — same underlying denial class, must
    # produce the same hint. The detection list is centralized in
    # :data:`ACCESS_DENIED_SIGNALS` so the chat renderer's universal FileGuard
    # hint injector uses the SAME wordings (P1-8 single-source-of-truth).
    #
    # False-positive fix (2026-08-03): this Case scans ``stderr`` ONLY, not
    # ``combined_raw``. ``stdout`` is the command's CONTENT channel — a command
    # that merely PRINTS a denial phrase (``type security_notes.md``,
    # ``grep -r 'permission denied' logs/``) would otherwise be diagnosed as
    # access-denied and get the hint, misleading the model into aborting a
    # working command. A real denial always surfaces on stderr (the OS / shell
    # writes it there); the later Cases keep using ``combined`` because their
    # signals (path-not-found prose, empty-output detection) legitimately look
    # at both streams.
    if text_has_access_denied_signal(stderr):
        return _build_access_denied_hint(sandboxed=sandboxed)

    # ── Case 2: PowerShell "cannot find drive" / "does not exist" ─────
    if (
        "cannot find drive" in combined
        or "cannot find path" in combined
        or "does not exist" in combined
    ):
        return (
            "\n[hint] Path resolution failed (PowerShell reports path / drive not found).\n"
            "Possible causes:\n"
            "  1. The target path really does not exist (typo, not yet created)\n"
            "  2. PowerShell's PSDrive context does not include the drive letter\n"
            "  3. Sandbox or FileGuard hid the path (access check fails before existence check)\n"
            "Suggestion: use shell='cmd' with `if exist <path> ...`, "
            "or python -c \"import os; print(os.path.exists(r'<path>'))\", "
            "or the glob / read tools."
        )

    # ── Case 3: msys2 / Cygwin tools failing to initialise ─────────────
    # Historical note: this branch used to attribute msys2 failures to the
    # AppContainer sandbox (which blocked \\BaseNamedObjects\\msys-2.0-...
    # global-name creation).  The AppContainer sandbox was removed
    # 2026-07-01; msys2 initialisation failures today are almost always
    # environment issues (PortableGit not installed / not on PATH /
    # incompatible ARM64 vs x64 runtime), so the hint is retargeted.
    if (
        "ntcreatedirectoryobject" in combined
        or "msys-2.0" in combined
        or "bug (fork bomb)" in combined
        or (exit_code in (-1073741502, -1073741515) and sandboxed)
    ):
        return (
            "\n[hint] msys2/Cygwin tool failed to initialise. Possible causes:\n"
            "  • PortableGit not installed or not on PATH (Setup.bat installs PortableGit "
            "and configures PATH; an external shell may not inherit it)\n"
            "  • Tool/runtime architecture mismatch (e.g. loading x64 msys2 libs on ARM64)\n"
            "  • System-level Job Object / global named-object limits (enterprise EDR / group policy)\n"
            "Affected PortableGit tools: bash, tail, head, grep, sed, awk, wc, "
            "cat, ls, cp, mv, rm, find, sort, uniq, tr, tee, xargs, etc.\n"
            "Alternatives:\n"
            "  • Text tail (tail -N): Python `subprocess.run(..., capture_output=True).stdout.splitlines()[-N:]` "
            "or PowerShell `Get-Content ... | Select-Object -Last N`\n"
            "  • Text search (grep): Python `re.search` or PowerShell `Select-String`\n"
            "  • Line count (wc -l): Python `len(text.splitlines())` or `findstr /R /N \"^\" file | find /C \":\"`\n"
            "  • File ops (cp/mv/rm): cmd copy/move/del or Python shutil/os\n"
            "  • Pipelines: write the whole thing as a Python script (use write to create a .py file, then exec it)\n"
        )

    # ── Case 3b: a multi-line ``python -c`` shredded by cmd.exe ────────
    # The exec tool materialises a multi-line cmd command VERBATIM, line by
    # line, into a temp ``.bat`` (``_multiline_rewrite``: ZERO-PARSE — the
    # command content is never rewritten, by design). For a multi-line
    # ``python -c "<script>"`` that means the opening quote is left unclosed on
    # its own line, so cmd.exe hands Python an empty program and then executes
    # every remaining SCRIPT line as if it were a shell command:
    #
    #     'import' is not recognized as an internal or external command
    #     Unable to initialize device PRN      <- cmd read `print(...)` as PRN
    #     The syntax of the command is incorrect.
    #
    # The correct guidance already exists in ``_format_silent_failure_hint``,
    # but that only runs for a non-zero exit with NO output (Case 5 below).
    # This failure is the opposite — it floods stderr — so the model used to
    # get a screenful of ``is not recognized`` and no indication of the actual
    # cause, leaving it to guess (usually re-issuing a similar broken command).
    # Detect the signature and name the fix. This does NOT change execution or
    # attempt to rewrite the command; it only explains a failure we already
    # accept by design.
    if _is_shredded_inline_script(command=command, combined=combined):
        return (
            "\n[hint] A multi-line `python -c \"<script>\"` was split by "
            "cmd.exe, so the script never ran.\n"
            "Cause: a multi-line command is materialised line-by-line into a "
            ".bat, which leaves the `-c` opening quote unclosed — Python "
            "receives an empty program and cmd.exe then tries to execute each "
            "SCRIPT line as a shell command (hence `'import' is not "
            "recognized`; `print(...)` is even mistaken for the legacy PRN "
            "printer device).\n"
            "Suggestion (in order of reliability):\n"
            "  1. Use the write tool to save the script to a .py file, then "
            "`exec` `python <file>.py`. This is the ROBUST pattern for anything "
            "multi-line — no shell quoting is involved at all.\n"
            "  2. Or pass shell='powershell', which carries a multi-line "
            "payload natively (cmd cannot).\n"
            "  3. Only if the script is TRULY tiny: a single-line `python -c`. "
            "Beware — under shell='cmd' the outer quotes are lost as soon as "
            "the script contains a space (you get `SyntaxError: unterminated "
            "string literal`), and `;` cannot express blocks such as "
            "try/except anyway."
        )

    # ── Case 3c: inline ``-c`` script whose outer quotes the shell ate ──
    # The sibling of 3b for a SINGLE-line ``-c``. Under ``shell='cmd'`` the
    # outer double quotes are lost as soon as the script contains a space, so
    # Python receives the fragment ``"import`` and dies with an unterminated
    # string literal pointing at line 1 — a confusing message, because the
    # script itself is syntactically fine. Verified: ``python -c "print(1+1)"``
    # (no space) succeeds, ``python -c "import os; print(1)"`` fails under cmd
    # and succeeds under powershell.
    if _is_quote_stripped_inline_script(command=command, combined=combined):
        return (
            "\n[hint] The inline `-c` script's outer quotes were consumed by "
            "the shell, so Python received a fragment (not your script).\n"
            "Cause: under shell='cmd' the outer double quotes of "
            "`-c \"<script>\"` are lost once the script contains a space — the "
            "`SyntaxError` points at line 1 even though your script is fine.\n"
            "Suggestion (in order of reliability):\n"
            "  1. Use the write tool to save the script to a .py file, then "
            "`exec` `python <file>.py` — no shell quoting involved.\n"
            "  2. Pass shell='powershell', which preserves the quoted "
            "argument."
        )

    # ── Case 4: Python UnicodeEncodeError on Windows cmd ──────────────
    has_unicode_err = (
        "unicodeencodeerror" in combined
        or ("charmap" in combined and "codec can't encode" in combined)
    )
    has_traceback = "traceback" in combined and (
        'file "' in combined or "line " in combined
    )
    if has_unicode_err and has_traceback:
        return (
            "\n[hint] Python stdout encoding error (UnicodeEncodeError / charmap codec).\n"
            "Cause: Windows cmd stdout defaults to cp1252/GBK, so Python print crashes on CJK/emoji.\n"
            "Suggestion (pick one):\n"
            "  • Add at the top of the script: `import sys; sys.stdout.reconfigure(encoding=\"utf-8\", errors=\"replace\")`\n"
            "  • Set env var `set PYTHONIOENCODING=utf-8` before running\n"
            "  • Python 3.7+: enable UTF-8 mode via the `PYTHONUTF8=1` env var"
        )

    # ── Case 5: Non-zero exit + effectively empty output ──────────────
    if not stdout.strip() and not stderr.strip():
        if command_has_explicit_redirect(command):
            return ""
        return _format_silent_failure_hint(exit_code, command, sandboxed)

    return ""
