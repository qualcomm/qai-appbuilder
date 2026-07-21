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
"""

from __future__ import annotations

import re

__all__ = [
    "command_has_explicit_redirect",
    "format_exit_diagnostics",
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
# Access-Denied hint (V1 security/access_error_helper.py:55-107).
#
# Note: the ``sandboxed`` parameter is kept for signature stability (call
# sites still pass it); it is unused.  Access denials today come from this
# app's in-process security audit hook (a PEP-578 audit hook enforcing
# Protected Paths / FileGuard that raises ``PermissionError``) or from a
# raw Windows NTFS permission denial — not from any OS sandbox (the legacy
# sandbox execution chain was removed on 2026-07-01; see
# ``docs/85-tasks/windows-acl-sandbox-cleanup-2026-07-01.md``).
# ---------------------------------------------------------------------------
def _build_access_denied_hint(*, sandboxed: bool) -> str:
    _ = sandboxed  # parameter retained for signature stability; not used
    return (
        "\n\n[hint] Command returned \"Access Denied\" / \"Permission denied\". "
        "Most likely one of:\n"
        "  1. Path not in an allow list "
        "(Security → Allow Lists: read_allow / write_allow / exec_allow_cwd).\n"
        "  2. Protected Paths blocked writing to a protected dir "
        "(e.g. C:\\Qualcomm and other SDK/toolchain paths).\n"
        "  3. Blocked by native FileGuard hook (guard64.dll intercepted the syscall)\n"
        "     — this is an ENFORCED security policy denial, NOT a filesystem ACL /\n"
        "     read-only / share-violation issue.\n"
        "  4. Raw Windows/POSIX file permission denial (missing rights / read-only\n"
        "     attribute / system file / file locked by another process) — unrelated\n"
        "     to this app's guards.\n"
        "  5. A 32-bit (x86) program was blocked: guard64.dll cannot monitor\n"
        "     32-bit processes — they are refused pre-spawn to prevent security\n"
        "     bypass. Use the 64-bit version, or ask the user to enable\n"
        "     'Allow 32-bit processes' in Settings → Security.\n"
        "\n"
        "If cause 1/2/3 (FileGuard family), DO NOT attempt to bypass:\n"
        "  - retrying with sudo/admin, alternate tools (Copy-Item, robocopy, xcopy,\n"
        "    cmd copy, mv, cp), reformatted paths (short 8.3 name, symlinks, UNC\n"
        "    prefix), or splitting the command will all be blocked identically;\n"
        "  - if access is truly required, ask the user to authorize the target in\n"
        "    Settings → Security → Allow Lists instead of retrying.\n"
        "Suggestion: prefer the read / write / edit tools over direct file access "
        "via exec — they report which specific guard raised the denial and why."
    )


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
    combined = (stdout + "\n" + stderr).lower()
    combined_raw = stdout + "\n" + stderr

    # ── Case 1: Access Denied (English / Chinese / Win32 / POSIX) ─────
    # POSIX/msys tools (mv, rm, bash redirects, ln, chmod, chown, etc.)
    # emit "Permission denied" / "Operation not permitted" (EPERM) rather
    # than the Win32 "Access is denied" wording — same underlying denial
    # class, must produce the same hint.
    if (
        "access is denied" in combined
        or "access denied" in combined
        or "拒绝访问" in combined_raw
        or "error 5" in combined
        or "error code 5" in combined
        or "permission denied" in combined
        or "operation not permitted" in combined
    ):
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
