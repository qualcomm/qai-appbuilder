# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""FileGuard cmdline static parser.

Statically parses a Windows shell command string into one or more sub-commands
and extracts their write/read targets so that PolicyCenter can enforce file
access rules without actually running the command.

Capabilities:
  * Splits chained shell commands on ``&&``, ``||``, ``;`` and ``|``.
  * Recognizes generic redirections: ``>``, ``>>``, ``2>``, ``&>``, ``<``.
  * Recognizes ``tee`` and PowerShell ``Out-File``/``Set-Content``/``Add-Content``.
  * Knows tool-specific output flags for ``cmake``, ``cl``, ``link`` and QAIRT.
  * Best-effort tracking of read targets for trivial reading commands like
    ``type`` (Windows) so that audit hooks can see them.

This module is intentionally free of cross-imports to avoid circular imports
when the security domain loads it. It is also pure-domain: it performs no
filesystem or environment I/O. ``import os`` is retained solely for the
string-only helper ``os.path.basename`` — it never reads ``os.environ``;
callers wanting ``%VAR%`` / ``$env:`` expansion must pass an ``expand_env``
snapshot into :func:`parse_command`.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Callable, Iterable

# Signature shared by every entry in :data:`_TOOL_TARGET_DISPATCH`: a handler
# is called as ``handler(args, writes, reads, *, expand_env=...)`` — it receives
# the tool's ``args`` plus the shared ``writes`` / ``reads`` lists it appends to,
# and a keyword-only ``expand_env`` snapshot (used only by the recursive ``cmd``
# handler; ignored by the others for a uniform signature). ``Callable[..., None]``
# is used because every handler carries the keyword-only ``*, expand_env`` param,
# which the positional-only ``Callable[[...], None]`` form cannot express.
_ToolTargetHandler = Callable[..., None]

try:  # pragma: no cover - optional dependency
    import mslex  # type: ignore

    _HAS_MSLEX = True
except Exception:  # pragma: no cover
    mslex = None  # type: ignore
    _HAS_MSLEX = False


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ParsedCommand:
    """A single sub-command parsed from a shell chain."""

    raw: str
    exe: str | None
    args: list[str] = field(default_factory=list)
    write_targets: list[str] = field(default_factory=list)
    read_targets: list[str] = field(default_factory=list)


@dataclass
class ParsedShell:
    """Result of parsing a shell command string."""

    commands: list[ParsedCommand] = field(default_factory=list)
    deny_reason: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_VAR_PERCENT_RE = re.compile(r"%([A-Za-z_][A-Za-z0-9_]*)%")
_VAR_ENV_RE = re.compile(r"\$env:([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)
_VAR_DOLLAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

# 2>&1, 1>&2, >&2, &>&1 etc. -- file descriptor duplications, NOT real file paths.
_FD_DUP_RE = re.compile(r"^[0-9]?(?:&)?>&[0-9]+$")

# PowerShell pipeline writers. Match either form:
#   Out-File C:\out.txt
#   Out-File -FilePath C:\out.txt
#   Out-File "C:\out.txt"
_PS_WRITER_RE = re.compile(
    r"(?i)\b(?:Out-File|Set-Content|Add-Content)\b"
    r"(?:\s+-(?:Path|FilePath|LiteralPath))?"
    r"\s+(?P<path>\"[^\"]+\"|'[^']+'|[^\s\"'|;&]+)"
)

# PowerShell Remove-Item: matches the path argument (positional or -Path/-LiteralPath)
_PS_REMOVE_ITEM_RE = re.compile(
    r"(?i)\bRemove-Item\b"
    r"(?:\s+-(?:Path|LiteralPath))?"
    r"\s+(?P<path>\"[^\"]+\"|'[^']+'|[^\s\"'|;&]+)"
)

# PowerShell Copy-Item / Move-Item: extract -Destination value (write target)
_PS_COPY_MOVE_DEST_RE = re.compile(
    r"(?i)\b(?:Copy-Item|Move-Item)\b"
    r"[^|;&]*?"  # any args before -Destination, but not crossing pipeline/chain
    r"-Destination\s+(?P<dest>\"[^\"]+\"|'[^']+'|[^\s\"'|;&]+)"
)


def _strip_outer_quotes(s: str) -> str:
    """Strip a single matched pair of outer quotes."""
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        return s[1:-1]
    return s


def _basename_no_ext(exe: str | None) -> str:
    if not exe:
        return ""
    base = os.path.basename(exe).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def _expand_vars(text: str, env) -> str:
    """Expand %VAR%, $env:VAR, $VAR using the given mapping."""

    def _from_env(name: str) -> str:
        try:
            return env.get(name, "") or ""
        except AttributeError:
            try:
                return env[name]
            except Exception:
                return ""

    text = _VAR_PERCENT_RE.sub(lambda m: _from_env(m.group(1)), text)
    text = _VAR_ENV_RE.sub(lambda m: _from_env(m.group(1)), text)
    text = _VAR_DOLLAR_RE.sub(lambda m: _from_env(m.group(1)), text)
    return text


# ---------------------------------------------------------------------------
# Chain splitter
# ---------------------------------------------------------------------------


def _split_chain(cmd: str) -> list[str]:
    """Split a shell line on ``&&``/``||``/``;``/``|`` while honoring quotes.

    Treats ``"`` and ``'`` as quote pairs so that delimiters inside a quoted
    region are preserved literally. Single ``&`` (PowerShell call operator
    or backgrounding) is NOT a delimiter.
    """

    segments: list[str] = []
    cur: list[str] = []
    in_dq = False
    in_sq = False

    i = 0
    n = len(cmd)
    while i < n:
        c = cmd[i]

        # Quote toggling.
        if c == '"' and not in_sq:
            in_dq = not in_dq
            cur.append(c)
            i += 1
            continue
        if c == "'" and not in_dq:
            in_sq = not in_sq
            cur.append(c)
            i += 1
            continue

        if not in_dq and not in_sq:
            # Two-char operators first.
            two = cmd[i : i + 2]
            if two in ("&&", "||"):
                seg = "".join(cur).strip()
                if seg:
                    segments.append(seg)
                cur = []
                i += 2
                continue
            if c in (";", "|"):
                seg = "".join(cur).strip()
                if seg:
                    segments.append(seg)
                cur = []
                i += 1
                continue
            # Lone '&' is NOT treated as a chain (PowerShell call operator).
            cur.append(c)
            i += 1
            continue

        cur.append(c)
        i += 1

    seg = "".join(cur).strip()
    if seg:
        segments.append(seg)
    return segments


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------


def _tokenize(segment: str) -> list[str]:
    """Tokenize a single segment using mslex if available, else shlex."""
    if _HAS_MSLEX:
        try:
            return list(mslex.split(segment))  # type: ignore[arg-type]
        except ValueError:
            # mslex raises ValueError on malformed quoting / tokenization;
            # fall through to the shlex tokenizer rather than aborting.
            pass
    return shlex.split(segment, posix=False)


# ---------------------------------------------------------------------------
# Redirection extractor
# ---------------------------------------------------------------------------


def _extract_redirections(tokens: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Extract write / read paths from generic shell redirections.

    Returns ``(remaining_tokens, write_targets, read_targets)``.
    """
    remaining: list[str] = []
    writes: list[str] = []
    reads: list[str] = []

    # Order matters: longer prefixes first so '>>' wins over '>' etc.
    write_prefixes = ("&>>", "&>", "1>>", "2>>", "1>", "2>", ">>", ">")
    read_prefixes = ("<",)

    i = 0
    n = len(tokens)
    while i < n:
        tok = tokens[i]

        # File-descriptor duplications: 2>&1, 1>&2, >&2 -- skip entirely.
        if _FD_DUP_RE.match(tok):
            i += 1
            continue

        # Standalone redirection operators (path is the next token).
        if tok in ("&>", "&>>", "2>", "2>>", "1>", "1>>", ">", ">>"):
            if i + 1 < n:
                writes.append(_strip_outer_quotes(tokens[i + 1]))
                i += 2
            else:
                i += 1
            continue
        if tok == "<":
            if i + 1 < n:
                reads.append(_strip_outer_quotes(tokens[i + 1]))
                i += 2
            else:
                i += 1
            continue

        # Prefix-attached redirection: '>file', '>>file', '2>file', '&>file'.
        matched = False
        for prefix in write_prefixes:
            if tok.startswith(prefix):
                rest = tok[len(prefix) :]
                # If the rest looks like an FD ref (e.g. "&1" in "2>&1") it's
                # a duplication, not a file path. Already handled above by
                # _FD_DUP_RE for whole-token matches; defensive check here.
                if rest and not rest.startswith("&"):
                    writes.append(_strip_outer_quotes(rest))
                matched = True
                break
        if matched:
            i += 1
            continue

        for prefix in read_prefixes:
            if tok.startswith(prefix) and len(tok) > len(prefix):
                reads.append(_strip_outer_quotes(tok[len(prefix) :]))
                matched = True
                break
        if matched:
            i += 1
            continue

        remaining.append(tok)
        i += 1

    return remaining, writes, reads


# ---------------------------------------------------------------------------
# Tool-specific extractors
# ---------------------------------------------------------------------------


_CL_FILE_PREFIXES = (
    "/Fo",
    "/Fe",
    "/Fd",
    "/Fp",
    "/Fa",
    "/Fi",
    "/FI",
    "-Fo",
    "-Fe",
    "-Fd",
    "-Fp",
    "-Fa",
    "-Fi",
    "-FI",
)


def _tool_cmd(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    """cmd /c "..." or cmd /k "..." — recursively parse the inner command.

    Handles patterns like ``cmd /c "del C:\\Dump\\file.txt & echo done"``.
    """
    i = 0
    while i < len(args):
        a = args[i].lower()
        if a in ("/c", "/k") and i + 1 < len(args):
            # Everything after /c is the inner command (may be one quoted string
            # or multiple tokens that form the command).
            inner_parts = args[i + 1:]
            inner_cmd = " ".join(_strip_outer_quotes(p) for p in inner_parts)
            if inner_cmd.strip():
                # Recursive parse of the inner command. Thread the
                # caller-supplied env snapshot through so nested
                # expansion uses the same mapping (never os.environ).
                inner_parsed = parse_command(inner_cmd, expand_env=expand_env)
                for sub in inner_parsed.commands:
                    writes.extend(sub.write_targets)
                    reads.extend(sub.read_targets)
            break
        i += 1


def _tool_cmake(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-B" and i + 1 < len(args):
            writes.append(_strip_outer_quotes(args[i + 1]))
            i += 2
            continue
        if a.startswith("-B") and len(a) > 2 and not a.startswith("-B="):
            writes.append(_strip_outer_quotes(a[2:]))
            i += 1
            continue
        if a == "--build" and i + 1 < len(args):
            writes.append(_strip_outer_quotes(args[i + 1]))
            i += 2
            continue
        if a.startswith("--build="):
            writes.append(_strip_outer_quotes(a[len("--build="):]))
            i += 1
            continue
        i += 1


def _tool_cl(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    for a in args:
        for prefix in _CL_FILE_PREFIXES:
            if a.startswith(prefix) and len(a) > len(prefix):
                suffix = a[len(prefix):]
                if suffix.startswith(":"):
                    suffix = suffix[1:]
                if suffix:
                    writes.append(_strip_outer_quotes(suffix))
                break


def _tool_link(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    for a in args:
        low = a.lower()
        if low.startswith("/out:") or low.startswith("-out:"):
            writes.append(_strip_outer_quotes(a[5:]))


def _tool_tee(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    skip_flags = {"-a", "--append", "-i", "--ignore-interrupts"}
    for a in args:
        if a in skip_flags:
            continue
        if a.startswith("-"):
            continue
        writes.append(_strip_outer_quotes(a))


def _tool_type(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    # Windows type reads file(s) and writes to stdout.
    for a in args:
        if a.startswith("/") or a.startswith("-"):
            continue
        reads.append(_strip_outer_quotes(a))


def _tool_del(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    # Windows del/erase: del [/P] [/F] [/S] [/Q] [/A[:attrs]] file [file ...]
    # All non-flag positional arguments are files being deleted (write op).
    for a in args:
        low = a.lower()
        # Skip switches like /F /S /Q /A or /A:r etc.
        if low.startswith("/") or low.startswith("-"):
            continue
        writes.append(_strip_outer_quotes(a))


def _tool_move(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    # move [/Y | /-Y] source destination
    # Both src and dst end up as write targets (src is moved away).
    non_flags = [a for a in args
                 if not a.startswith("/") and not a.startswith("-")]
    for path in non_flags:
        writes.append(_strip_outer_quotes(path))


def _tool_ren(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    # ren old_name new_name  (only the source is a path; new_name is a basename)
    non_flags = [a for a in args
                 if not a.startswith("/") and not a.startswith("-")]
    if non_flags:
        writes.append(_strip_outer_quotes(non_flags[0]))


def _tool_copy(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    # copy [/A | /B] source [+source ...] [destination]
    # The last positional is the destination (write); earlier ones are reads.
    non_flags = [a for a in args
                 if not a.startswith("/") and not a.startswith("-")]
    if len(non_flags) >= 2:
        for src in non_flags[:-1]:
            reads.append(_strip_outer_quotes(src))
        writes.append(_strip_outer_quotes(non_flags[-1]))
    elif len(non_flags) == 1:
        # Single arg: implicitly copies to current dir -> conservative read
        reads.append(_strip_outer_quotes(non_flags[0]))


def _tool_xcopy(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    # xcopy   source destination [/flags]
    # robocopy source destination [files] [/flags]
    non_flags = [a for a in args
                 if not a.startswith("/") and not a.startswith("-")]
    if len(non_flags) >= 2:
        reads.append(_strip_outer_quotes(non_flags[0]))
        writes.append(_strip_outer_quotes(non_flags[1]))
    elif len(non_flags) == 1:
        reads.append(_strip_outer_quotes(non_flags[0]))


def _tool_mkdir(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    # mkdir dir [dir ...]
    for a in args:
        if a.startswith("/") or a.startswith("-"):
            continue
        writes.append(_strip_outer_quotes(a))


def _tool_rmdir(
    args: list[str], writes: list[str], reads: list[str],
    *, expand_env: dict | None,
) -> None:
    # rmdir [/S] [/Q] dir
    for a in args:
        if a.startswith("/") or a.startswith("-"):
            continue
        writes.append(_strip_outer_quotes(a))


# Exact-match dispatch table for the cmd.exe-style and MSVC-toolchain bases.
# Each handler appends its tool-specific write/read targets to the shared
# lists; this mirrors the former ``if/elif base == ...`` chain one-for-one.
# ``cmd`` is dispatched separately because it short-circuits with an early
# return (its recursion fully owns the result for that command).
_TOOL_TARGET_DISPATCH: dict[str, "_ToolTargetHandler"] = {
    "cmake": _tool_cmake,
    "cl": _tool_cl,
    "link": _tool_link,
    "tee": _tool_tee,
    "type": _tool_type,
    "del": _tool_del,
    "erase": _tool_del,
    "move": _tool_move,
    "ren": _tool_ren,
    "rename": _tool_ren,
    "copy": _tool_copy,
    "xcopy": _tool_xcopy,
    "robocopy": _tool_xcopy,
    "mkdir": _tool_mkdir,
    "md": _tool_mkdir,
    "rmdir": _tool_rmdir,
    "rd": _tool_rmdir,
}


def _extract_python_inline_targets(
    args: list[str], writes: list[str], reads: list[str],
) -> None:
    """python / python3 / pythonw / py: extract paths from a ``-c`` inline script."""
    # Find the -c flag and reconstruct the full inline script from all
    # tokens that follow it (tokenizer may have split the script).
    _inline_script: str | None = None
    _i = 0
    while _i < len(args):
        _a = args[_i]
        if _a == "-c" and _i + 1 < len(args):
            # Join all remaining tokens as the inline script body
            _inline_script = " ".join(args[_i + 1:])
            break
        if _a in ("-X", "-W", "-O", "-u", "-v", "-q") and _i + 1 < len(args):
            _i += 2  # skip flag + value
            continue
        if _a.startswith("-") and len(_a) == 2:
            _i += 1  # single-char flag without value
            continue
        _i += 1

    if not _inline_script:
        return

    # Strip outer quotes from the inline script if the tokenizer kept
    # the entire -c argument as a single quoted token.
    _script_body = _inline_script.strip()
    if (len(_script_body) >= 2
            and _script_body[0] == '"' and _script_body[-1] == '"'):
        _script_body = _script_body[1:-1]
    elif (len(_script_body) >= 2
            and _script_body[0] == "'" and _script_body[-1] == "'"):
        _script_body = _script_body[1:-1]

    # Regex to match string literals that look like absolute paths
    # inside the Python inline script body.
    _PATH_STR_RE = re.compile(
        r"""(?<!\w)(r|b|rb|br)?(?:"([^"]{3,}?)"|'([^']{3,}?)')""",
        re.IGNORECASE,
    )
    # Regex to detect write-like operations before the path argument
    _WRITE_OPS_RE = re.compile(
        r"""(?:os\.remove|os\.unlink|os\.rename|os\.replace|
                os\.makedirs|os\.mkdir|os\.symlink|os\.link|
                shutil\.move|shutil\.rmtree|shutil\.copy|shutil\.copy2|
                shutil\.copytree|shutil\.make_archive|
                \.write_text|\.write_bytes|\.unlink|\.mkdir|
                open\s*\([^)]*,\s*['"]\s*[wWaA]
            )""",
        re.IGNORECASE | re.VERBOSE,
    )

    def _looks_like_path(s: str, is_raw: bool) -> bool:
        """Return True if s looks like an absolute Windows or Unix path."""
        if not is_raw:
            s = s.replace("\\\\", "\\")
        s = s.strip()
        # Windows: C:\... or C:/...
        if len(s) >= 3 and s[1] == ":" and s[2] in ("\\/"):
            return True
        # Unix absolute
        if s.startswith("/") and len(s) > 1:
            return True
        return False

    def _normalize_path(s: str, is_raw: bool) -> str:
        """Normalize path: unescape double-backslash for non-raw strings."""
        if not is_raw:
            s = s.replace("\\\\", "\\")
        return s.strip()

    # Extract all string literals from the inline script body
    for _m in _PATH_STR_RE.finditer(_script_body):
        _prefix = (_m.group(1) or "").lower()
        _is_raw = "r" in _prefix
        _candidate = _m.group(2) or _m.group(3) or ""
        if not _looks_like_path(_candidate, _is_raw):
            continue
        _normalized = _normalize_path(_candidate, _is_raw)
        # Check context: is there a write-like operation nearby?
        _ctx_start = max(0, _m.start() - 200)
        _ctx_end = min(len(_script_body), _m.end() + 200)
        _context = _script_body[_ctx_start:_ctx_end]
        if _WRITE_OPS_RE.search(_context):
            writes.append(_normalized)
        else:
            # Conservative: treat as read target
            reads.append(_normalized)


def _extract_qairt_targets(
    args: list[str], writes: list[str], reads: list[str],
) -> None:
    """QAIRT family: qairt-converter / qairt-quantizer / qairt-* etc."""
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--output_dir" and i + 1 < len(args):
            writes.append(_strip_outer_quotes(args[i + 1]))
            i += 2
            continue
        if a.startswith("--output_dir="):
            writes.append(_strip_outer_quotes(a[len("--output_dir="):]))
            i += 1
            continue
        if a == "-o" and i + 1 < len(args):
            writes.append(_strip_outer_quotes(args[i + 1]))
            i += 2
            continue
        i += 1


def _extract_tool_targets(
    exe: str | None, args: list[str], *, expand_env: dict | None = None
) -> tuple[list[str], list[str]]:
    """Extract write/read targets based on known tool-specific argument patterns.

    Replaces the former 250-line ``if/elif`` chain with a ``dict`` dispatch
    table (:data:`_TOOL_TARGET_DISPATCH`) plus two predicate-matched handlers
    (python inline ``-c`` scripts and the QAIRT toolchain). The behaviour is
    byte-for-byte identical to the chain: ``cmd`` short-circuits with its
    recursive parse; the exact-match handler appends its targets; then the
    python and qairt probes run *independently* (they were separate ``if``
    blocks, not part of the ``elif`` chain) so a single command can be matched
    by both an exact handler and a predicate handler.
    """
    writes: list[str] = []
    reads: list[str] = []
    base = _basename_no_ext(exe)
    if not base:
        return writes, reads

    # cmd /c "..." / cmd /k "..." owns the whole result via recursion.
    if base == "cmd":
        _tool_cmd(args, writes, reads, expand_env=expand_env)
        return writes, reads

    # Exact-match tool handler (mirrors the former if/elif base == ... chain).
    handler = _TOOL_TARGET_DISPATCH.get(base)
    if handler is not None:
        handler(args, writes, reads, expand_env=expand_env)

    # python / python3 / pythonw / py: extract paths from -c inline script.
    # NOTE: independent ``if`` (not ``elif``) — preserves original semantics.
    if base in ("python", "python3", "pythonw", "py", "python3.exe", "python.exe"):
        _extract_python_inline_targets(args, writes, reads)

    # QAIRT family. Also an independent ``if`` in the original.
    if base.startswith("qairt") or "qairt" in base or base.endswith("-converter"):
        _extract_qairt_targets(args, writes, reads)

    return writes, reads


def _extract_powershell_pipeline_writes(segment_text: str) -> list[str]:
    """Find PowerShell write/delete cmdlets anywhere in the segment.

    Detected:
      - Out-File / Set-Content / Add-Content (file writers)
      - Remove-Item                          (file delete)
      - Copy-Item / Move-Item -Destination    (write target only)

    Runs as regex on the raw segment text so it works even if a cmdlet sits
    inside a quoted ``-Command`` argument.
    """
    writes: list[str] = []
    for m in _PS_WRITER_RE.finditer(segment_text):
        path = m.group("path")
        writes.append(_strip_outer_quotes(path))
    for m in _PS_REMOVE_ITEM_RE.finditer(segment_text):
        writes.append(_strip_outer_quotes(m.group("path")))
    for m in _PS_COPY_MOVE_DEST_RE.finditer(segment_text):
        writes.append(_strip_outer_quotes(m.group("dest")))
    return writes


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_command(cmd: str, *, expand_env: dict | None = None) -> ParsedShell:
    """Parse a Windows shell command into ``ParsedShell``.

    Parameters
    ----------
    cmd:
        The raw command-line string to parse.
    expand_env:
        Optional mapping for ``%VAR%`` / ``$env:`` variable expansion. When
        omitted (``None``) **no** environment expansion is performed (an
        empty mapping is used). The pure-domain layer must NOT read the
        process-global ``os.environ``; the application / adapter caller is
        responsible for snapshotting ``os.environ`` and passing it in when
        environment expansion is desired.

    Returns
    -------
    ParsedShell
        Contains the list of parsed sub-commands with their write/read targets,
        or a ``deny_reason`` if parsing failed.
    """
    if cmd is None or not str(cmd).strip():
        return ParsedShell(commands=[], deny_reason=None)

    # Domain purity: do NOT fall back to ``os.environ`` here. Callers that
    # want environment expansion must pass a snapshot explicitly.
    env = expand_env if expand_env is not None else {}

    # 1) Split on shell chain operators (respect quotes).
    segments = _split_chain(cmd)
    if not segments:
        return ParsedShell(commands=[], deny_reason=None)

    parsed: list[ParsedCommand] = []
    for seg in segments:
        seg_expanded = _expand_vars(seg, env)

        # 2) Tokenize.
        try:
            tokens = _tokenize(seg_expanded)
        except ValueError as e:
            return ParsedShell(
                commands=[
                    ParsedCommand(
                        raw=cmd,
                        exe=None,
                        args=[],
                        write_targets=[],
                        read_targets=[],
                    )
                ],
                deny_reason=f"cmdline parse error: {e}",
            )
        except Exception as e:  # pragma: no cover - defensive
            return ParsedShell(
                commands=[
                    ParsedCommand(
                        raw=cmd,
                        exe=None,
                        args=[],
                        write_targets=[],
                        read_targets=[],
                    )
                ],
                deny_reason=f"cmdline parse error: {e}",
            )

        # 3) Strip a leading PowerShell call operator ('&').
        while tokens and tokens[0] == "&":
            tokens = tokens[1:]

        # 4) Extract redirections.
        rest, redir_writes, redir_reads = _extract_redirections(tokens)

        # 5) Identify exe + args.
        exe: str | None = None
        args: list[str] = []
        if rest:
            exe = _strip_outer_quotes(rest[0])
            args = [_strip_outer_quotes(a) for a in rest[1:]]

        # 6) Tool-specific output extractors.
        tool_writes, tool_reads = _extract_tool_targets(
            exe, args, expand_env=env
        )

        # 7) PowerShell pipeline writers (regex on raw segment text).
        ps_writes = _extract_powershell_pipeline_writes(seg_expanded)

        # 8) Aggregate, preserving order, removing duplicates.
        all_writes: list[str] = []
        for p in (*redir_writes, *tool_writes, *ps_writes):
            if p and p not in all_writes:
                all_writes.append(p)

        all_reads: list[str] = []
        for p in (*redir_reads, *tool_reads):
            if p and p not in all_reads:
                all_reads.append(p)

        parsed.append(
            ParsedCommand(
                raw=seg,
                exe=exe,
                args=args,
                write_targets=all_writes,
                read_targets=all_reads,
            )
        )

    return ParsedShell(commands=parsed, deny_reason=None)
