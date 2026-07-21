# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Shell-execution tool handler (``exec``).

When a :class:`ProcessRunnerPort` is injected, argv is routed through it
so behaviour matches the chat agentic loop's routed exec path. Post
Phase 3 cleanup (2026-07-01) this runner is the plain
:class:`SubprocessProcessRunner` — the historical AppContainer launcher
wrap was deleted, so the routed path executes directly on the host.
When no runner is injected the handler falls back to the legacy raw
``asyncio.create_subprocess_exec`` path (byte-for-byte prior behaviour).
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import platform
import re
import sys
import tempfile
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from qai.ai_coding.application.ports import FileGuardPort
from qai.ai_coding.infrastructure.tools.errors import ToolError, ToolGuardDenied
from qai.ai_coding.infrastructure.tools.handlers._multiline_rewrite import (
    cleanup_temp_scripts,
    rewrite_multiline_to_argv,
)
from qai.ai_coding.infrastructure.tools.handlers._protected_command_guard import (
    protected_command_sentinel,
)
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    EXEC_MAX_OUTPUT_BYTES,
    _ok,
    default_cwd,
    strip_ansi_escapes,
)
from qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics import (
    command_has_explicit_redirect,
    format_exit_diagnostics,
)
from qai.platform.child_process_deny_audit import (
    notify_child_protected_deny,
    parse_and_strip_deny_markers,
)
from qai.platform.process import (
    best_effort_tree_kill,
    no_window_creationflags,
    terminate_process_tree,
)
from qai.platform.process.ports import (
    ProcessExecutionRequest,
    ProcessRunnerPort,
    ProcessStartedFrame,
    ProcessStderrFrame,
    ProcessStdoutFrame,
    ProcessTerminatedFrame,
)

# ---------------------------------------------------------------------------
# D2-B: native FileGuard denial probe type alias.
#
# The exec handler lives in ``qai.ai_coding`` and the import-linter
# ``context-isolation`` contract FORBIDS it from importing
# ``qai.security.application.ports.AuditQueryPort`` or
# ``qai.security.domain.native_guard_denial_message`` directly (only
# ``qai.** -> qai.platform.**`` is allowed to cross contexts).
#
# The composition root (``apps/api/_ai_coding_di.py``) pre-composes those two
# security-context APIs into a single stdlib-typed async callable and passes it
# in via :func:`tool_exec`'s ``native_denial_probe`` argument. The callable
# takes ``(root_pid, since)`` and MUST return a ready-to-append note string
# (``""`` when no matching DENY rows exist, or a ``"\\n\\n"``-prefixed
# diagnostic block otherwise — matching :func:`build_native_guard_denial_note`'s
# contract). It MUST NOT raise (never break the tool result on audit failure).
#
# Fail-open (AGENTS.md §5 铁律5): when the caller does not inject a probe, or
# when the probe returns ``""``, the handler behaves exactly as it did before
# D2-B (the D1 keyword-hint from ``exec_diagnostics`` still applies). Both
# hints coexist when both are relevant.
NativeGuardDenialProbe = Callable[[int, datetime], Awaitable[str]]

# ---------------------------------------------------------------------------
# Shell auto-detection (7-M4) — V1 parity: ``_exec._detect_shell_type``.
#
# 6 heuristics promote a command to PowerShell so that Unix-style cmdlets
# (Get-*, Set-*, ...), PS operators (-eq / -match / |% / |?), $-variables and
# Out-File pipelines are dispatched to powershell.exe rather than being
# mis-routed to cmd.exe (which would mangle 30+ common cmdlets).
# ---------------------------------------------------------------------------

# Background tree-kill/reap tasks kept alive until done (asyncio only holds a
# WEAK ref to a bare ``ensure_future`` task, so without this set a fire-and-
# forget reap could be GC'd mid-flight). On a user cancel we hard-kill the tree
# SYNCHRONOUSLY (fast, so the child can never be orphaned) and hand the OS-level
# reap (``proc.wait()``, which on Windows can block up to the 5s reap timeout)
# to one of these background tasks — so the cancelling caller (and the tool
# card UI) is NOT blocked waiting for the reap. See the CancelledError branch.
_BACKGROUND_REAP_TASKS: set[asyncio.Task[None]] = set()


def _reap_in_background(proc: "asyncio.subprocess.Process") -> None:
    """Fire-and-forget the OS reap of an already-hard-killed process tree.

    The tree is already dead (``best_effort_tree_kill`` ran synchronously before
    this call), so this only awaits ``proc.wait()`` to release the OS handle /
    close the transport — work the canceller must not wait on. Kept in a module
    set so the task is not GC'd; self-removes on completion. Never raises.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return  # no loop → nothing to schedule (best-effort)

    async def _reap() -> None:
        with contextlib.suppress(Exception):
            await terminate_process_tree(proc)

    task = loop.create_task(_reap())
    _BACKGROUND_REAP_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_REAP_TASKS.discard)


# ---------------------------------------------------------------------------

# Verb-Noun cmdlet pattern (covers 30+ common cmdlets via the verb set).
_PS_CMDLET_PATTERN = re.compile(
    r"\b(?:Get|Set|New|Remove|Import|Export|Invoke|Start|Stop|Test|Write|Read|"
    r"Out|Select|Where|ForEach|Sort|Group|Measure|Compare|ConvertTo|"
    r"ConvertFrom|Add|Clear|Copy|Move|Rename|Update|Wait|Enter|Exit|Push|Pop)"
    r"-[A-Z]\w+",
    re.IGNORECASE,
)
_PS_OPERATOR_PATTERN = re.compile(
    r"\s-(?:eq|ne|gt|lt|ge|le|like|notlike|match|notmatch|contains|in|notin)\s"
)
_PS_PIPE_SHORTCUT_PATTERN = re.compile(r"\|\s*[%?]\s")
_PS_OUTFILE_PATTERN = re.compile(r"\|\s*Out-File\b", re.IGNORECASE)


def _detect_shell_type(command: str) -> str:
    """Auto-detect whether *command* uses PowerShell or cmd.exe syntax.

    Returns ``"powershell"`` when any of the 6 heuristics match, else
    ``"cmd"``.  V1 parity: ``backend/tools/_exec.py:_detect_shell_type``.
    """
    cmd = command.strip()
    # 1. Starts with the call operator ``& `` — definitive PowerShell.
    if re.match(r"^&\s", cmd):
        return "powershell"
    # 2. Verb-Noun cmdlet pattern (Get-ChildItem, Test-Path, ...).
    if _PS_CMDLET_PATTERN.search(cmd):
        return "powershell"
    # 3. $-variable syntax ($env:VAR, $x) but not cmd-style %VAR%.
    if re.search(r"\$\w+", cmd) and not re.search(r"%\w+%", cmd):
        return "powershell"
    # 4. PowerShell comparison / logic operators.
    if _PS_OPERATOR_PATTERN.search(cmd):
        return "powershell"
    # 5. Pipeline shortcuts |% / |? (ForEach-Object / Where-Object aliases).
    if _PS_PIPE_SHORTCUT_PATTERN.search(cmd):
        return "powershell"
    # 6. Out-File pipelines.
    if _PS_OUTFILE_PATTERN.search(cmd):
        return "powershell"
    return "cmd"


# ---------------------------------------------------------------------------
# PowerShell alias-removal prelude (7-M3) — V1 parity:
# ``_exec._POWERSHELL_ALIAS_REMOVAL_PRELUDE`` / ``_wrap_powershell_command``.
#
# PowerShell resolves Alias > Function > Cmdlet > Application(PATH).  ``ls
# -la`` resolves ``ls`` to the ``Get-ChildItem`` alias, so the real
# ``ls.exe`` on PATH (git\usr\bin) never runs and Unix short flags fail.  We
# prepend a ``Remove-Item Alias:`` prelude so PowerShell falls through to the
# Application stage and the real Unix .exe tools win.
# ---------------------------------------------------------------------------

_POWERSHELL_ALIAS_REMOVAL_PRELUDE = (
    "Remove-Item -Force -ErrorAction SilentlyContinue "
    "Alias:ls,Alias:cat,Alias:cp,Alias:mv,Alias:rm,Alias:ps,Alias:pwd,"
    "Alias:echo,Alias:where,Alias:kill,Alias:wget,Alias:curl,Alias:tee,"
    "Alias:type,Alias:sort,Alias:diff,Alias:history,Alias:man,"
    "Alias:select,Alias:compare,Alias:cpi,Alias:mi,Alias:ri;"
)

# Silence the PowerShell progress stream. When powershell.exe is spawned with
# its stderr redirected to a pipe (as the exec tool does), PowerShell serialises
# EVERY non-stdout stream — including the "Preparing modules for first use"
# progress records — into CLIXML and writes it to stderr, producing the
# ``#< CLIXML <Objs ...>`` noise the model sees even for a fully successful
# command. Setting ``$ProgressPreference='SilentlyContinue'`` suppresses the
# progress stream at the source, so a clean command yields an EMPTY stderr
# (verified: 616 bytes of CLIXML -> 0 bytes). Real errors (Write-Error /
# exceptions) are unaffected — they still surface on stderr. ``-OutputFormat
# Text`` does NOT help: it only controls the stdout OBJECT stream, not stderr
# serialisation (verified empirically).
_POWERSHELL_PROGRESS_SILENCE_PRELUDE = "$ProgressPreference='SilentlyContinue';"


def _wrap_powershell_command(command: str) -> str:
    """Prefix a PowerShell command with the noise-suppression preludes.

    Two preludes are prepended (in order):

    1. ``$ProgressPreference='SilentlyContinue';`` — suppress the progress
       stream so a successful command produces no CLIXML stderr noise
       (see :data:`_POWERSHELL_PROGRESS_SILENCE_PRELUDE`). Applied
       UNCONDITIONALLY.
    2. The alias-removal prelude so Unix tools on PATH win over PowerShell
       aliases — SKIPPED when the command already starts by removing
       aliases (rare).

    V1 parity: ``_exec._wrap_powershell_command`` (plus the 2026-07-13
    progress-silence fix for the ``#< CLIXML`` stderr noise).
    """
    cmd_stripped = command.lstrip()
    if (
        cmd_stripped.startswith("Remove-Item")
        and "Alias:" in cmd_stripped[:200]
    ):
        # Alias prelude already present in the user command; still prepend the
        # progress-silence prelude (it never conflicts with alias removal).
        return _POWERSHELL_PROGRESS_SILENCE_PRELUDE + " " + command
    return (
        _POWERSHELL_PROGRESS_SILENCE_PRELUDE
        + " "
        + _POWERSHELL_ALIAS_REMOVAL_PRELUDE
        + " "
        + command
    )


def _resolve_shell(command: str, shell: str) -> str:
    """Resolve ``"auto"`` to a concrete shell name for *command*.

    On Windows ``auto`` runs the 6-rule detector (7-M4); on POSIX it
    becomes ``"sh"``.  An explicit shell is returned unchanged.
    """
    if shell != "auto":
        return shell
    if sys.platform == "win32":
        return _detect_shell_type(command)
    return "sh"


# ---------------------------------------------------------------------------
# PowerShell -File materialisation (2026-07-20) — root-cause CLIXML fix.
#
# ``powershell.exe -EncodedCommand ... 2>pipe`` unconditionally activates the
# "minishell" host on stderr, which serialises EVERY non-stdout stream
# (Write-Host / Write-Error / Progress / Information records) into CLIXML —
# ``#< CLIXML\n<Objs ...>...</Objs>`` blobs whose Tags-section leaks ``<S>PSHOST</S>``
# and other internal element values into any regex-based cleanup. This is
# built into powershell.exe's ``-EncodedCommand`` code path; ``-OutputFormat
# Text`` does not disable it, ``$ProgressPreference='SilentlyContinue'`` only
# suppresses the progress records (not error/info/host records), and the
# ``_strip_powershell_clixml`` regex fights an ever-growing set of XML
# element shapes.
#
# ``powershell.exe -File script.ps1 2>pipe`` uses the host-native text
# formatter on stderr — plain readable text, no CLIXML wrapper. Verified
# empirically (see the ``test_powershell_file_mode_stderr_no_clixml``
# regression). This removes the noise at source and makes
# ``_strip_powershell_clixml`` a defence-in-depth pass rather than a
# load-bearing hack.
# ---------------------------------------------------------------------------


def _write_powershell_script_temp(wrapped_command: str) -> Path:
    r"""Materialise a wrapped PowerShell command into a temporary ``.ps1``.

    Returns the absolute path to the temp file. The caller MUST append the
    returned path to a ``tmp_paths`` list so ``cleanup_temp_scripts``
    unlinks it after the child exits (best-effort, in a ``finally``).

    Encoding — UTF-8 with BOM
    -------------------------
    PowerShell 5.1 defaults to ANSI/GBK when reading a ``.ps1`` file that
    lacks an encoding marker, so a wrapped command containing non-ASCII
    (Chinese, emoji, accented chars) would silently corrupt at parse time.
    Prepending the UTF-8 BOM (``\xef\xbb\xbf``) tells PS to decode as
    UTF-8 unconditionally — the same pattern used by
    ``_multiline_rewrite._write_multiline_bat`` for its ``.bat`` payloads
    (which additionally sets ``chcp 65001`` for its child's stdout).

    Location
    --------
    Written under ``%TEMP%\QAIModelBuilder\default\qai_ps_<rand>.ps1``
    (isolated from user data, cleanable as a group) when that directory is
    reachable; otherwise the system default ``%TEMP%`` root — same policy
    as :func:`_multiline_rewrite._materialisation_temp_dir`.

    Errors
    ------
    Any write failure is re-raised after best-effort deletion of the
    partially-written file, so the caller sees the real disk/permission
    error rather than a downstream "script not found".
    """
    # Match the .bat materialisation policy — QAIModelBuilder\default under
    # %TEMP%. Falling back to None lets tempfile use the system default.
    tmp_dir_root = (
        Path(os.environ.get("TEMP", tempfile.gettempdir()))
        / "QAIModelBuilder"
        / "default"
    )
    try:
        tmp_dir_root.mkdir(parents=True, exist_ok=True)
        tmp_dir: str | None = str(tmp_dir_root)
    except OSError:
        tmp_dir = None

    # UTF-8 BOM prefix (0xEF 0xBB 0xBF) — mandatory so PS 5.1 doesn't fall
    # back to ANSI/GBK on non-ASCII payloads.
    payload = "\ufeff" + wrapped_command
    if not payload.endswith("\n"):
        payload += "\n"

    fd, path_str = tempfile.mkstemp(
        suffix=".ps1", prefix="qai_ps_", dir=tmp_dir, text=False
    )
    tmp_path = Path(path_str)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload.encode("utf-8"))
    except Exception:
        # Best-effort cleanup on write failure — re-raise so the caller
        # sees the real error (disk full, permission denied, etc.).
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        raise
    return tmp_path


def _pe_machine(path: Path) -> int | None:
    """Return the PE ``Machine`` value of a Windows executable, or ``None``.

    Reads the COFF header machine field (offset ``e_lfanew + 4``). Used to
    verify a PortableGit ``sh.exe`` / ``bash.exe`` (and its real MSYS backend)
    matches the CURRENT process architecture BEFORE spawning it — a mismatched
    binary (e.g. an x86-64 backend spawned from an ARM64-native parent, with
    x64 emulation disabled) fails to load with ``0xC000007B``
    (STATUS_INVALID_IMAGE_FORMAT), not a clean error. Any read failure returns
    ``None``.

    PE machine constants: ``0x8664`` = x86-64, ``0xAA64`` = ARM64,
    ``0x014C`` = x86-32.
    """
    try:
        with path.open("rb") as fh:
            if fh.read(2) != b"MZ":
                return None
            fh.seek(0x3C)
            e_lfanew = int.from_bytes(fh.read(4), "little")
            fh.seek(e_lfanew)
            if fh.read(4) != b"PE\x00\x00":
                return None
            return int.from_bytes(fh.read(2), "little")
    except OSError:
        return None


def _current_pe_machine() -> int | None:
    """PE ``Machine`` value matching the current Python process architecture.

    Uses ``platform.machine()`` (the running interpreter's arch) so we only
    accept a PortableGit shell the OS can actually run in THIS process. Returns
    ``None`` for an unrecognised arch (then no PE-arch check is applied).
    """
    machine = platform.machine().upper()
    if machine in ("AMD64", "X86_64", "EM64T"):
        return 0x8664
    if machine in ("ARM64", "AARCH64"):
        return 0xAA64
    if machine in ("X86", "I386", "I686"):
        return 0x014C
    return None


def _resolve_portable_git_shell(shell_name: str) -> str | None:
    r"""Resolve the FULL path to PortableGit's ``sh.exe`` / ``bash.exe``, or None.

    State-Truth-First (AGENTS.md 铁律4): Windows has no ``sh`` on PATH, and a
    bare ``argv[0]="sh"`` cannot be spawned by ``create_subprocess_exec``. We
    resolve the ABSOLUTE path from the same structural anchor ``_build_exec_env``
    uses (``%LOCALAPPDATA%\QAIModelBuilder\git``) — never a hard-coded per-user
    path.

    Architecture selection (2026-07-12): PortableGit ships the shell under both
    ``bin\`` and ``usr\bin\``, and on a Windows-on-Snapdragon install these can
    differ (``bin\sh.exe`` = ARM64 native, ``usr\bin\sh.exe`` = x86-64). We
    prefer the candidate whose PE machine MATCHES the current process (so the
    front-end we spawn is native and directly loadable). We do NOT try to
    predict whether the MSYS backend it may fork into can run — that depends on
    the host's runtime x86 emulation, which is not knowable from the file
    headers; if it genuinely cannot run, the child surfaces a normal non-zero
    exit rather than us pre-emptively disabling a working shell.

    Returns the arch-matching front-end path; if none matches the process arch,
    the first existing candidate; ``None`` when PortableGit is absent /
    ``LOCALAPPDATA`` is empty (caller raises a friendly ToolError).
    """
    if shell_name not in ("sh", "bash"):
        return None
    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if not local_app_data:
        return None
    git_root = Path(local_app_data) / "QAIModelBuilder" / "git"
    exe = f"{shell_name}.exe"
    # ``bin\`` first: on WoS it holds the ARM64-native front-end; ``usr\bin\``
    # is the fallback (and the x86-64 build on WoS).
    candidates = [git_root / "bin" / exe, git_root / "usr" / "bin" / exe]
    existing = [c for c in candidates if c.is_file()]
    if not existing:
        return None
    want = _current_pe_machine()
    chosen: str | None = None
    if want is not None:
        # Prefer the front-end whose arch matches this process (native, no
        # cross-arch load of argv[0] itself).
        for candidate in existing:
            if _pe_machine(candidate) == want:
                chosen = str(candidate)
                break
    if chosen is None:
        chosen = str(existing[0])
    # TEMP DIAGNOSTIC (2026-07-12): capture daemon-side resolution truth.
    try:
        import platform as _plat
        _diag = (
            f"resolve({shell_name}) LOCALAPPDATA={local_app_data!r} "
            f"machine={_plat.machine()!r} want=0x{want or 0:X} "
            f"existing={[(str(c), '0x%X' % (_pe_machine(c) or 0)) for c in existing]} "
            f"chosen={chosen!r}"
        )
        with open(
            Path(os.environ.get("TEMP", "C:/Windows/Temp")) / "qai_sh_debug.log",
            "a", encoding="utf-8",
        ) as _fh:
            import datetime as _dt
            _fh.write(f"[{_dt.datetime.now().isoformat()}] pid={os.getpid()} {_diag}\n")
    except Exception:
        pass
    return chosen


def _select_shell(
    command: str,
    shell: str,
    tmp_paths_out: list[Path] | None = None,
) -> tuple[list[str], bool]:
    """Return (argv, use_shell_wrap) for the chosen interpreter.

    On Windows we pick between cmd.exe and powershell.exe via the 6-rule
    auto-detector (7-M4); on POSIX we fall back to ``/bin/sh``.  PowerShell
    commands are wrapped with the alias-removal prelude (7-M3) and invoked
    with ``-ExecutionPolicy Bypass`` (7-L4) so restricted-policy machines do
    not refuse to run them.

    PowerShell payloads are materialised to a temporary UTF-8-BOM ``.ps1``
    file and invoked via ``-File <path>`` (2026-07-20 root-cause CLIXML
    fix — see :func:`_write_powershell_script_temp`). ``-File`` uses the
    host-native stderr text formatter, whereas ``-EncodedCommand`` +
    piped stderr triggers CLIXML minishell serialisation whose Tags-section
    ``<S>PSHOST</S>`` element (and other internal values) leak into
    regex-based cleanup and corrupt the visible output. The temp ``.ps1``
    path is embedded in the returned argv right after the ``-File`` token,
    AND — when *tmp_paths_out* is provided — appended to that list so the
    caller can unlink it via
    :func:`_multiline_rewrite.cleanup_temp_scripts` after the child exits.
    Callers that cannot pass *tmp_paths_out* can recover the path from the
    argv (``argv[argv.index("-File") + 1]``) instead.

    ``sh`` / ``bash`` on Windows are resolved to a USABLE PortableGit shell
    (arch-matching front-end AND backend, see :func:`_resolve_portable_git_shell`);
    when no runnable shell exists (PortableGit missing, or its MSYS backend is a
    non-matching architecture such as x86-64 on an ARM64 host without x64
    emulation) a friendly :class:`ToolError` is raised — steering the model to
    ``cmd`` / ``powershell`` — rather than spawning a child that crashes with
    ``0xC000007B``.
    """
    shell = _resolve_shell(command, shell)

    if shell == "cmd":
        return ["cmd", "/c", command], False
    if shell == "powershell":
        wrapped = _wrap_powershell_command(command)
        script_path = _write_powershell_script_temp(wrapped)
        if tmp_paths_out is not None:
            tmp_paths_out.append(script_path)
        return [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script_path),
        ], False
    if shell in ("sh", "bash"):
        # POSIX: rely on PATH resolution (``/bin/sh`` / ``/bin/bash``).
        if sys.platform != "win32":
            return [shell, "-c", command], False
        # Windows: resolve the PortableGit shell's absolute path (arch-matching
        # front-end). A bare ``argv[0]="sh"`` cannot be spawned by
        # create_subprocess_exec, so a missing PortableGit must fail with a
        # clear hint rather than a spawn crash.
        resolved = _resolve_portable_git_shell(shell)
        if resolved is None:
            raise ToolError(
                f"exec: shell='{shell}' 在本机不可用（未找到 PortableGit "
                f"{shell}.exe）。请运行 Setup.bat 安装 PortableGit 到 "
                "%LOCALAPPDATA%\\QAIModelBuilder\\git，或改用 shell='cmd' / "
                "shell='powershell' 执行该命令。"
            )
        return [resolved, "-c", command], False
    raise ToolError(f"exec: unknown shell {shell!r}")


def _build_exec_env(*, guard_token: str | None = None, allow_x86: bool = False) -> dict[str, str]:
    """Build the subprocess environment for the exec tool.

    V1 parity: ``backend/tools/_security.py::_build_exec_env`` (414-447) — the
    PATH / venv ``Scripts`` / PortableGit / ``PYTHONUNBUFFERED`` slice.  This is
    the ai_coding-context twin of ``qai.tools.infrastructure.exec_env.
    build_exec_env`` (the chat streaming path's copy): the
    ``context-isolation`` import-linter contract forbids ``qai.ai_coding.*``
    from importing ``qai.tools.*``, so the small, V1-pinned env builder is
    duplicated rather than coupling the two contexts.  Both mirror the same V1
    source line-for-line.

    Cross-platform (AGENTS.md): a missing / empty ``LOCALAPPDATA`` gracefully
    skips the PortableGit injection (no crash, no Windows-only import).
    """
    import os
    from pathlib import Path

    env = os.environ.copy()
    venv_scripts = str(Path(sys.executable).parent)
    env["PATH"] = venv_scripts + os.pathsep + env.get("PATH", "")
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONUNBUFFERED"] = "1"

    # ALWAYS-ON child-process protected-path guard (independent of FileGuard /
    # OS sandbox, both of which ship disabled). Put the protected-paths child
    # hook dir on PYTHONPATH so the child interpreter auto-imports it as
    # ``sitecustomize`` and denies writes into the Qualcomm / QAIRT SDK tree
    # before any pipeline code runs (e.g. the x86_64 model-builder Python can no
    # longer truncate the generator exe — the 2026-06-16 incident). The hook
    # reads ``QAI_PROTECTED_PATHS``; ``protected_paths.env_value()`` always
    # includes the non-removable built-ins plus any user-configured prefixes.
    try:
        from qai.platform import child_process_audit_sentinel, protected_paths

        hook_dir = str(Path(child_process_audit_sentinel.__file__).resolve().parent)
        env["PYTHONPATH"] = hook_dir
        env["QAI_PROTECTED_PATHS"] = protected_paths.env_value()
    except Exception:  # noqa: BLE001 — never let env wiring break exec
        pass

    local_app_data = os.environ.get("LOCALAPPDATA", "")
    if local_app_data:
        git_root = Path(local_app_data) / "QAIModelBuilder" / "git"
        # System32 interposed BEFORE usr\bin so the 8 PortableGit coreutils that
        # collide by name with semantically-incompatible Windows built-ins
        # (find / sort / timeout / tar / whoami / hostname / expand / reset)
        # resolve to the Windows built-in in cmd pipelines, while the ~240
        # Unix-only tools (grep / sed / awk / ls / cat …) still resolve to
        # usr\bin. Fixes the 2026-06-21 ``... | find /c "x"`` runaway where the
        # GNU find treated ``/c`` as the C: drive root and scanned the whole
        # disk. Kept identical to the chat-streaming twin
        # (``qai.tools.infrastructure.exec_env.build_exec_env``). Per AGENTS.md
        # 🟡🟡 this corrects a latent V1 defect (V1 prepended usr\bin ahead of
        # everything) rather than carrying it forward.
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        git_bin_candidates = [
            git_root / "cmd",
            git_root / "bin",
            system32,
            git_root / "usr" / "bin",
            git_root / "clangarm64" / "bin",
        ]
        # Only interpose System32 + git dirs when PortableGit is actually
        # present; without it there is nothing to shadow, so leave the inherited
        # PATH untouched (System32 is already on it).
        if (git_root / "cmd").is_dir() or (git_root / "usr" / "bin").is_dir():
            git_prefix_parts = [str(p) for p in git_bin_candidates if p.is_dir()]
            if git_prefix_parts:
                env["PATH"] = (
                    os.pathsep.join(git_prefix_parts) + os.pathsep + env["PATH"]
                )
    # FileGuard guard-token marker (2026-07-06 guard-only reversal). Set on
    # the child env copy only — never the host os.environ — so the native
    # guard64.dll guards this exec subtree. ``None`` injects nothing → child
    # bypassed (safe non-guarding default).
    if guard_token:
        env["QAI_FILEGUARD_GUARD_TOKEN"] = guard_token
    # x86 process escape hatch: when the user enables "Allow 32-bit processes"
    # in Security settings, propagate QAI_GUARD_ALLOW_X86=1 so the native
    # guard64 HookedCreateProcessW does not terminate x86 children.
    if allow_x86:
        env["QAI_GUARD_ALLOW_X86"] = "1"
    return env


def _resolve_timeout(timeout_raw: Any) -> float:
    """Resolve the ``timeout`` argument to seconds (V1/v0.5 parity).

    Returns ``0.0`` (== NO timeout — every downstream gate uses ``timeout > 0``
    to decide whether to arm :func:`asyncio.wait_for`) when ``timeout`` is
    omitted (``None``), zero, negative, or unparseable. Only a finite positive
    value caps the run. Mirrors V1 ``_exec.py:795-797`` (``timeout = None if
    (_t is None or _t == 0) else _t``) and v0.5 ``tool_executor.py:231-233``.
    """
    if timeout_raw is None:
        return 0.0
    try:
        value = float(timeout_raw)
    except (TypeError, ValueError):
        return 0.0
    return value if value > 0 else 0.0


def _ensure_cwd_exists(cwd_str: str) -> str:
    """Make sure the subprocess working directory really exists.

    State-Truth-First (AGENTS.md 铁律1): a non-existent ``cwd`` makes the OS
    reject process creation (Windows ``[WinError 267]``), so a missing default
    workspace dir (e.g. ``C:/WoS_AI`` on a fresh machine) breaks *every* exec.
    Try to create it (it is an app-owned workspace location, safe to
    materialise). If creation fails for any reason, fall back to a directory
    that is guaranteed to exist so commands can still run, rather than failing
    the whole tool.
    """
    if not cwd_str:
        return cwd_str
    try:
        if os.path.isdir(cwd_str):
            return cwd_str
        os.makedirs(cwd_str, exist_ok=True)
        return cwd_str
    except OSError:
        # Could not create the requested dir (permissions, invalid drive, …).
        # Fall back to a directory we know exists so exec still works; never
        # let a missing workspace dir take down command execution entirely.
        for fallback in (os.path.expanduser("~"), os.getcwd()):
            try:
                if fallback and os.path.isdir(fallback):
                    return fallback
            except OSError:
                continue
        return cwd_str


async def tool_exec(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
    process_runner: ProcessRunnerPort | None = None,
    guard_token_provider: "Callable[[], str | None] | None" = None,
    ask_pending_probe: "Callable[[int], bool] | None" = None,
    native_denial_probe: NativeGuardDenialProbe | None = None,
    allow_x86: bool = False,
) -> dict[str, Any]:
    command = args.get("command") or ""
    if not isinstance(command, str) or not command.strip():
        raise ToolError("exec: 'command' argument is required")
    shell = (args.get("shell") or "auto").lower()
    timeout_raw = args.get("timeout")
    # V1/v0.5 parity (AGENTS.md 🟢): omitting ``timeout`` means NO timeout —
    # the legacy ``_exec.py:795-797`` / v0.5 ``tool_executor.py:231-233`` set
    # ``timeout=None`` (无限等待) when the arg is absent or 0. The V2-only
    # default of 120s silently KILLED long but legitimate commands (compile,
    # ``pip``/``npm install``, big file ops) whenever the model did not pass an
    # explicit timeout — a behaviour regression. Restore "omit/0 = no timeout";
    # only an explicit positive value caps the run.
    timeout = _resolve_timeout(timeout_raw)
    cwd_str = args.get("cwd") or None
    if cwd_str is not None and not isinstance(cwd_str, str):
        raise ToolError("exec: 'cwd' must be a string when provided")
    # Default the working dir to the per-request workspace base (active
    # session workspace → global configured workspace) instead of
    # inheriting the daemon process CWD (== repo root). Keeps artifacts /
    # temp files inside the workspace, not the application install dir.
    if cwd_str is None:
        cwd_str = default_cwd()

    # State-Truth-First (AGENTS.md 铁律1): the working directory we hand to the
    # subprocess must REALLY exist. The default workspace base (e.g.
    # ``C:/WoS_AI``) does not exist on a freshly-installed machine until a
    # workspace is initialised, so spawning there fails for EVERY command with
    # ``[WinError 267] The directory name is invalid`` — turning a missing
    # directory into a total exec outage. Materialise the directory here (the
    # workspace base is a safe, app-owned location) so the first command no
    # longer depends on some earlier step having created it.
    cwd_str = _ensure_cwd_exists(cwd_str)

    # ALWAYS-ON protected-path guard (independent of FileGuard, which ships
    # disabled): block a command whose obvious write target lands in the
    # Qualcomm / QAIRT SDK tree (``echo x > C:\Qualcomm\...`` / copy / del /
    # Out-File / -Destination …). The subprocess audit hook injected by
    # ``_build_exec_env`` is the deeper backstop for anything this misses.
    protected_reason = protected_command_sentinel(command)
    if protected_reason:
        raise ToolGuardDenied(
            message=protected_reason,
            error_code="ai_coding.tool.protected_path_write_denied",
        )

    await file_guard.enforce_exec(
        command=command,
        cwd=cwd_str,
        caller="ai_coding.tool.exec",
    )

    resolved_shell = _resolve_shell(command, shell)
    # Multi-line command support on the tokenised-argv exec path. A multi-line
    # cmd body cannot be carried as a single ``["cmd","/c",command]`` element
    # (``list2cmdline`` + cmd.exe double-parse drops trailing lines / mangles
    # quotes). ZERO-PARSE fix: materialise the whole command verbatim into a
    # ``.bat`` and run ``["cmd","/c","<tmp>.bat"]`` — the command CONTENT is
    # never parsed (no fragile python -c extraction), cmd.exe applies its own
    # per-line parsing. Returns ``(None, [])`` for single-line / powershell /
    # sh, which then fall back to ``_select_shell``. Temp files are unlinked in
    # the ``finally``. (A multi-line ``python -c`` inside the .bat still fails
    # with a clear error for the model to self-correct — see the rewrite
    # module's design note.)
    rewritten_argv, tmp_paths = rewrite_multiline_to_argv(
        command, resolved_shell
    )
    # Resolve the FileGuard guard-token per invocation (State-Truth-First:
    # the native guard starts lazily, so a snapshot could be stale). ``None``
    # when the guard is disabled / not started → no marker injected → the
    # spawned exec child is bypassed (allow-all), safe non-guarding default.
    guard_token: str | None = None
    if guard_token_provider is not None:
        try:
            guard_token = guard_token_provider()
        except Exception:  # noqa: BLE001 — token lookup must never break exec
            guard_token = None
    try:
        # ``_select_shell`` is inside the try so a ToolError it raises (e.g.
        # sh/bash unavailable) still runs the finally that unlinks any temp
        # scripts already materialised by the multi-line rewrite above.
        # Passing ``tmp_paths_out=tmp_paths`` lets the PowerShell ``-File``
        # branch (2026-07-20 CLIXML fix) register its temp ``.ps1`` for the
        # same finally-cleanup path used by the multi-line ``.bat``.
        if rewritten_argv is not None:
            argv = rewritten_argv
        else:
            argv, _ = _select_shell(
                command, resolved_shell, tmp_paths_out=tmp_paths
            )
        return await _dispatch_exec(
            argv=argv,
            command=command,
            shell=resolved_shell,
            cwd_str=cwd_str,
            timeout=timeout,
            process_runner=process_runner,
            guard_token=guard_token,
            ask_pending_probe=ask_pending_probe,
            native_denial_probe=native_denial_probe,
            allow_x86=allow_x86,
        )
    finally:
        # Unlink the materialised temp script(s) (best-effort, no-op when empty)
        # AFTER the child has fully run, on every exit path (return / raise /
        # cancel) so a rewritten multi-line body never leaks a temp file.
        cleanup_temp_scripts(tmp_paths)


async def _dispatch_exec(
    *,
    argv: list[str],
    command: str,
    shell: str,
    cwd_str: str | None,
    timeout: float,
    process_runner: ProcessRunnerPort | None,
    guard_token: str | None = None,
    ask_pending_probe: "Callable[[int], bool] | None" = None,
    native_denial_probe: NativeGuardDenialProbe | None = None,
    allow_x86: bool = False,
) -> dict[str, Any]:
    # Route through the injected runner when present.  Post Phase 3
    # cleanup the runner is the plain :class:`SubprocessProcessRunner`;
    # both branches (routed vs raw) execute directly on the host, but
    # the routed path keeps the platform-layer OOM cap + frame plumbing
    # active (see :func:`_run_via_process_runner` below).
    if process_runner is not None:
        return await _run_via_process_runner(
            process_runner,
            argv=argv,
            command=command,
            shell=shell,
            cwd=cwd_str,
            timeout=timeout,
            guard_token=guard_token,
            ask_pending_probe=ask_pending_probe,
            native_denial_probe=native_denial_probe,
            allow_x86=allow_x86,
        )

    # Sample the wall-clock BEFORE spawn so the post-run audit query can filter
    # to denies triggered by THIS subprocess (D2-B). ``timezone.utc`` matches
    # what ``AuditEntry.occurred_at`` is stored with — a naive datetime would
    # be interpreted as local time by the SQL adapter and miss recent rows.
    spawn_started_at = datetime.now(tz=timezone.utc)

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd_str,
            env=_build_exec_env(guard_token=guard_token, allow_x86=allow_x86),
            # Windows: don't flash a console window for the child (no-op on
            # POSIX). stdout/stderr are still captured via the pipes above.
            creationflags=no_window_creationflags(),
        )
    except FileNotFoundError as e:
        raise ToolError(f"exec: shell binary not found: {e}") from e
    except OSError as e:
        # x86 denial: when native guard64 refuses a 32-bit child,
        # CreateProcessW returns FALSE with ERROR_ACCESS_DENIED (WinError 5).
        # Detect this specific case and give the model a precise, actionable
        # message instead of a generic "spawn failed".
        if getattr(e, "winerror", None) == 5 and argv and argv[0]:
            machine = _pe_machine(Path(argv[0]))
            if machine == 0x014C:  # IMAGE_FILE_MACHINE_I386
                raise ToolError(
                    f"exec: refused to spawn '{Path(argv[0]).name}' — it is a "
                    "32-bit (x86) executable.\n\n"
                    "The Agent security environment (guard64.dll) cannot monitor "
                    "32-bit processes. Running one would bypass all file-access "
                    "security policies, so it was blocked pre-spawn.\n\n"
                    "Action: use the 64-bit (x86-64 or ARM64) version of this "
                    "program. If you must use a 32-bit tool, ask the user to "
                    "enable 'Allow 32-bit processes' in Settings → Security."
                ) from e
        raise ToolError(f"exec: subprocess spawn failed: {e}") from e

    timed_out = False
    output_capped = False
    # LOOP-DRIVEN timeout (orphan-safe fix): the previous
    # ``asyncio.wait_for(_communicate_capped(...), timeout)`` armed a
    # COROUTINE-driven timer — it only fires while the awaiting coroutine
    # chain is actively stepped by the event loop. When an upstream Stop
    # cascade set the abort event but did NOT cancel the task running this
    # exec (``StopChatUseCase`` is a pure flag-setter — see
    # ``streaming.py`` ``StopChatUseCase.execute``), the loop stopped
    # driving this coroutine, so BOTH the ``wait_for`` deadline AND the
    # child's ``stream.read()`` froze together and the subprocess ran on
    # unbounded (the reported "timeout:30 ran to 129 s, Stop did nothing"
    # bug). We instead arm the deadline as a ``loop.call_later`` callback:
    # a loop-scheduled timer fires independently of whether THIS coroutine
    # is being awaited, so a starved/orphaned exec is still force-killed at
    # its timeout. Mirrors the runner branch's ``_on_deadline``
    # (``subprocess_runner.py:220-233``) but tree-kills (PowerShell/python
    # may have spawned grandchildren) instead of a bare ``proc.kill()``.
    loop = asyncio.get_running_loop()
    deadline_handle: asyncio.TimerHandle | None = None
    kill_task: asyncio.Task[None] | None = None

    def _on_deadline() -> None:
        # Runs on the event loop at the deadline REGARDLESS of whether the
        # exec coroutine is currently being awaited. Mark timed-out and
        # schedule the async tree-kill+reap as its OWN task (shielded so a
        # later cancel of the exec task cannot interrupt the reap and orphan
        # the child). Killing the tree makes both child pipes hit EOF, so
        # ``_communicate_capped``'s drain returns and the ``await`` below
        # unblocks naturally.
        nonlocal timed_out, kill_task, deadline_handle
        if proc.returncode is not None:
            return  # already exited between the last read and the deadline
        # 2026-07-08 — do NOT count time spent BLOCKED on a native FileGuard
        # authorization dialog against the command's execution timeout. When
        # the exec child (e.g. powershell) is suspended by the native hook
        # waiting for the user to approve a file access, the wall-clock timer
        # is still running; without this check the user could be mid-decision
        # when the 30s deadline force-kills the child. We probe the AUTHORITY
        # (the pending-permission registry, via the injected probe — State-
        # Truth-First: we don't guess "we're waiting", we ask whether a native
        # ASK is genuinely pending on this child process tree). If so, we
        # RE-ARM the deadline for another slice instead of killing — so the
        # timeout effectively pauses for the duration of the authorization
        # wait. Orphan-safety is preserved: a genuinely hung/runaway child
        # (no pending ASK) still gets tree-killed on time.
        if ask_pending_probe is not None and proc.pid is not None:
            try:
                blocked_on_ask = bool(ask_pending_probe(proc.pid))
            except Exception:  # noqa: BLE001 — probe failure → do NOT stall kill
                blocked_on_ask = False
            if blocked_on_ask:
                # Re-arm for another slice; re-check at the next deadline.
                deadline_handle = loop.call_later(timeout, _on_deadline)
                return
        timed_out = True
        kill_task = asyncio.ensure_future(
            asyncio.shield(terminate_process_tree(proc))
        )

    try:
        if timeout > 0:
            deadline_handle = loop.call_later(timeout, _on_deadline)
        stdout, stderr, output_capped = await _communicate_capped(
            proc, EXEC_MAX_OUTPUT_BYTES
        )
    except asyncio.CancelledError:
        # Upstream "Stop" / single-tool cancel that DID cancel this task: the
        # child (and any subtree it spawned) must not be left running.
        #
        # RESPONSIVENESS FIX (单工具停止慢 ~5.7s): previously we
        # ``await asyncio.shield(terminate_process_tree(proc))`` here, which runs
        # the FULL kill+reap ladder — and on Windows the reap (``proc.wait()``
        # after ``taskkill /F /T``) can block up to the 5s reap timeout because
        # ProactorEventLoop's exit detection lags an external kill. That 5s was
        # spent BEFORE re-raising, so the tool card / cancelling caller sat on
        # "executing" for ~5s after the user clicked stop.
        #
        # The tree is force-killed SYNCHRONOUSLY here (fast, so the child can
        # NEVER be orphaned), then the OS-level reap (releasing the handle /
        # closing the transport) is handed to a background task so we can
        # re-raise the cancel IMMEDIATELY — the caller (and UI) no longer waits
        # on the reap. This matches the takeover path's synchronous
        # ``best_effort_tree_kill`` (which also does not await the reap).
        with contextlib.suppress(ProcessLookupError, OSError):
            best_effort_tree_kill(proc)
        _reap_in_background(proc)
        raise
    finally:
        # Disarm the deadline on the happy path so a completed exec does not
        # leave a pending timer (no leak); ``TimerHandle.cancel`` is a no-op
        # if it already fired.
        if deadline_handle is not None:
            deadline_handle.cancel()
        # If the deadline fired it launched ``kill_task``; await it so the
        # tree-kill+reap actually COMPLETES before we read ``returncode`` /
        # return (the child is truly dead, not merely signalled). Shielded
        # at creation, so awaiting it here is safe even under a later cancel.
        if kill_task is not None:
            try:
                await kill_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                # The kill was shielded; swallow only this reap's own noise
                # (a nested cancel / kill error) — never mask the real
                # control flow (a happy return or the re-raised outer cancel).
                pass

    exit_code = proc.returncode if proc.returncode is not None else -1

    # S2: skip the truncation advice when the user explicitly redirected
    # stdout/stderr to a file (empty/short pipe output is expected, not a
    # truncation).  V1 parity (_exec.py:346-364 _command_has_explicit_redirect).
    suppress_advice = command_has_explicit_redirect(command)
    out_text, out_trunc = _decode(stdout or b"", suppress_advice=suppress_advice)
    err_text, err_trunc = _decode(stderr or b"", suppress_advice=suppress_advice)
    # PowerShell serialises its stderr streams into CLIXML when stderr is a
    # pipe; unwrap it to readable text (no-op for non-powershell / non-CLIXML).
    if shell == "powershell":
        err_text = _strip_powershell_clixml(err_text)
    # P-08 #6: pull out + strip any child-process protected-deny markers and
    # audit them BEFORE any return path (timeout / capped / normal all get the
    # cleaned stderr; the marker never reaches the user / model).
    err_text = _extract_child_protected_denies(err_text)

    if timed_out:
        return _timeout_result(
            command, shell, out_text, err_text, exit_code, timeout
        )

    if output_capped:
        # OOM guard fired: the child flooded stdout/stderr past
        # EXEC_MAX_OUTPUT_BYTES and was killed. Keep the head we read, but tell
        # the model the output was cut off so it does not treat it as complete.
        err_text = _output_cap_note(err_text)

    # V1 parity (_exec.py:946-958): non-zero exit diagnostic hint. This raw
    # path ran directly on the host (no injected runner), so attribute
    # Access-Denied to Windows ACL / FileGuard (``sandboxed=False``).
    diagnostics = format_exit_diagnostics(
        exit_code,
        out_text,
        err_text,
        sandboxed=False,
        command=command,
    )

    # D2-B — native FileGuard denial diagnostics. When the subprocess exited
    # non-zero AND a probe is wired, ask whether the native guard64.dll hook
    # denied one or more file syscalls issued by ``proc`` (or one of its
    # descendants) since ``spawn_started_at``. The probe returns a ready-to-
    # append note string (``""`` when audit found nothing / audit failed).
    # Coexists with the D1 keyword-hint above — the D1 hint is a heuristic
    # ("Access Denied wording spotted"), the D2 note is a precise assertion
    # ("audit shows N denials from this pid subtree"). Both add signal.
    diagnostics = await _maybe_append_native_denial_note(
        diagnostics,
        exit_code=exit_code,
        pid=proc.pid,
        spawn_started_at=spawn_started_at,
        probe=native_denial_probe,
    )

    return _ok(
        f"exec exited {exit_code}",
        command=command,
        shell=shell,
        stdout=out_text,
        stderr=err_text,
        exit_code=exit_code,
        truncated=out_trunc or err_trunc or output_capped,
        timed_out=False,
        exit_diagnostics=diagnostics,
    )


async def _communicate_capped(
    proc: asyncio.subprocess.Process,
    max_bytes: int,
) -> tuple[bytes, bytes, bool]:
    """Read ``proc`` stdout/stderr concurrently, capped at ``max_bytes`` total.

    Memory-safety guard (NOT a timeout): a runaway command can flood its pipes
    with gigabytes of output, which a plain ``proc.communicate()`` would read
    fully into memory before any downstream truncation runs -- an OOM risk.
    This reader stops once the combined bytes read exceed ``max_bytes``, KILLS
    the child, and returns ``(stdout_head, stderr_head, capped=True)``. Under
    the cap it behaves like ``communicate()`` (returns the full output,
    ``capped=False``). The "omit = no timeout" V1 parity is unaffected -- a
    slow but quiet command still runs to completion.
    """
    chunks: dict[str, list[bytes]] = {"out": [], "err": []}
    total = 0
    capped = False

    async def _drain(stream: asyncio.StreamReader | None, key: str) -> None:
        nonlocal total, capped
        if stream is None:
            return
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            chunks[key].append(chunk)
            total += len(chunk)
            if total > max_bytes:
                capped = True
                # Kill NOW so the child stops producing and BOTH pipes hit EOF
                # -- otherwise the sibling drain (and a still-running child
                # blocked writing to this now-unread pipe) would deadlock. Tree
                # kill so a flooding grandchild is stopped too, not just the
                # direct shell child.
                best_effort_tree_kill(proc)
                return

    await asyncio.gather(
        _drain(proc.stdout, "out"),
        _drain(proc.stderr, "err"),
    )
    # Reap the child so it does not linger as a zombie / leave the transport
    # open (best-effort; the pipes are drained or the proc was killed above).
    try:
        await proc.wait()
    except Exception:  # noqa: BLE001
        pass
    return b"".join(chunks["out"]), b"".join(chunks["err"]), capped


def _decode(buf: bytes, *, suppress_advice: bool = False) -> tuple[str, bool]:
    """Decode + ANSI-strip exec output IN FULL (no inline truncation).

    V1 parity (AGENTS.md 🟢): the legacy ``_exec.py`` returned the COMPLETE
    output and relied on ``tool_result_storage.store_and_preview`` to persist
    the full body to disk + show the model a small head+tail preview with a
    ``read(path=...)`` retrieval hint. The oversized-output handling in V2
    lives in the SAME place — the registry routes ``stdout`` / ``stderr``
    through the injected :class:`ToolResultStorePort`
    (registry.py ``_apply_result_store``), which persists the full body and
    renders the preview.

    So this function must hand back the FULL decoded text: a prior V2
    ``buf[:200KB]`` head+tail cut HERE would truncate the body BEFORE the
    store ever saw it, defeating the落盘/``read`` recovery (the model could
    never retrieve the elided middle). We only decode + strip ANSI now; the
    store owns truncation/preview/persistence.

    ``suppress_advice`` is retained for signature compatibility with the two
    call sites but is now a no-op (the store, not this function, decides
    whether to emit a truncation note; an explicit file redirect simply
    produces short output that stays under the store threshold). The second
    tuple element (``truncated``) is always ``False`` here — the store sets the
    authoritative ``truncated`` flag on the persisted preview.

    7-M7: ANSI/VT100 escape sequences are stripped before the output reaches
    the model (many CLI tools emit colour codes even when piped; literal escape
    bytes confuse the model and waste tokens).
    """
    _ = suppress_advice  # retained for call-site symmetry; store owns the note
    text = strip_ansi_escapes(buf.decode("utf-8", errors="replace"))
    return text, False


# ---------------------------------------------------------------------------
# PowerShell CLIXML stderr un-wrapping.
# ---------------------------------------------------------------------------
# When powershell.exe runs with stderr redirected to a pipe, PowerShell does
# NOT write plain text to stderr — it serialises every non-stdout stream
# (error / warning / verbose / progress) into CLIXML, an XML object format
# whose first line is ``#< CLIXML``. The progress noise is silenced at the
# source by ``$ProgressPreference='SilentlyContinue'`` (see
# _POWERSHELL_PROGRESS_SILENCE_PRELUDE), so a SUCCESSFUL command yields an
# empty stderr. But when the command genuinely errors, the error text is still
# CLIXML-wrapped:
#
#   #< CLIXML
#   <Objs ...><S S="Error">line 1&#x000D;&#x000A;</S><S S="Error">line 2...</S></Objs>
#
# This helper unwraps that into plain text so the model sees a readable error
# instead of raw XML. It is intentionally dependency-free (regex + stdlib
# unescape) rather than a full CLIXML deserialiser: we only need the human
# error/warning text, not the object graph.
_CLIXML_HEADER = "#< CLIXML"
# Match the inner text of each <S S="...">...</S> stream-content node.
#
# The ``S="..."`` attribute is REQUIRED (not optional) — it names the source
# stream kind (Error / Warning / Verbose / Debug / Information / progress).
# Making the attribute optional would also match plain ``<S>...</S>`` elements
# used elsewhere in the CLIXML object graph — notably the ``Tags`` list of an
# ``InformationRecord`` for a ``Write-Host`` line, which serialises as
# ``<LST><S>PSHOST</S></LST>`` (``PSHOST`` here is a Tag value, not stream
# content). See regression test ``test_strip_clixml_does_not_extract_tags_S_element``.
_CLIXML_S_NODE_RE = re.compile(
    r'<S\s+S="[^"]*">(.*?)</S>', re.DOTALL
)
# PowerShell encodes control chars as ``_xHHHH_`` (e.g. _x000D_ = CR,
# _x000A_ = LF). Decode those back to real characters.
_CLIXML_XCHAR_RE = re.compile(r"_x([0-9A-Fa-f]{4})_")
# Precise CLIXML blob boundary is ``<Objs ...>...</Objs>``. We locate the
# opening ``<Objs`` after the ``#< CLIXML`` header and its matching closing
# ``</Objs>`` so that only that XML span is stripped — anything before the
# header and anything after ``</Objs>`` (which under the streaming exec path
# is REAL user stdout that PowerShell interleaved with the CLIXML stderr
# line-by-line) is preserved verbatim.
_CLIXML_OBJS_OPEN_RE = re.compile(r"<Objs\b")
_CLIXML_OBJS_CLOSE = "</Objs>"


def _strip_powershell_clixml(text: str) -> str:
    """Unwrap PowerShell CLIXML stream noise into readable plain text.

    Returns *text* unchanged when it is not CLIXML (no ``#< CLIXML`` header),
    so it is safe to call on any stderr / mixed output.

    The input may be either:

    * A pure CLIXML blob (classic case — stderr only), or
    * A **mixed** buffer where stdout and stderr have been merged line-by-line
      into a single string. This happens in the streaming exec path
      (``stream_exec()`` merges both streams into ``full_output`` by arrival
      order). PowerShell 5.1 ``Write-Host`` emits its information records as
      CLIXML on stderr while a concurrent ``Get-ChildItem`` (or any other
      cmdlet) writes plain text to stdout — the two streams end up
      interleaved:

          hello from stdout
          #< CLIXML
          <Objs ...>...</Objs>
          world from stdout after clixml

    The OLD implementation naively grabbed ``text[start:]`` from the CLIXML
    header to end-of-string and treated it all as XML, silently swallowing
    every plain-stdout line that happened to appear AFTER ``</Objs>``. This
    routinely turned 500-byte directory listings into 6 bytes.

    The FIX bounds the CLIXML span precisely to ``<Objs ...> ... </Objs>``
    and iterates in case (rare but possible) the buffer contains multiple
    CLIXML segments interleaved with plain text. For each segment: the plain
    text before it is preserved as-is, the ``<S S="...">...</S>`` stream
    nodes inside are extracted / decoded, and iteration continues from just
    after ``</Objs>`` so any following plain stdout is kept.

    Called for ``shell="powershell"`` stderr AND for the merged streaming
    buffer; both paths route through here so the handler stays idempotent.
    """
    if _CLIXML_HEADER not in text:
        return text
    import html as _html

    def _decode_node(raw: str) -> str:
        # 1) XML entities (&lt; &amp; &#x000D; ...); 2) _xHHHH_ escapes.
        s = _html.unescape(raw)
        s = _CLIXML_XCHAR_RE.sub(
            lambda m: chr(int(m.group(1), 16)), s
        )
        return s

    out_parts: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        header_idx = text.find(_CLIXML_HEADER, pos)
        if header_idx < 0:
            # No more CLIXML segments — remainder is all plain text.
            out_parts.append(text[pos:])
            break
        # Preserve plain text before the header verbatim.
        out_parts.append(text[pos:header_idx])
        # Locate the ``<Objs>`` opening after the header. If absent (residual
        # / truncated header without a body), preserve legacy behaviour: drop
        # from the header onward within this segment (nothing meaningful to
        # extract) and stop scanning — there is no ``</Objs>`` to bound on.
        objs_open_match = _CLIXML_OBJS_OPEN_RE.search(text, header_idx)
        if objs_open_match is None:
            # Legacy behaviour: header w/o <Objs> ⇒ everything from the header
            # to EOF is CLIXML noise we cannot parse; drop it.
            break
        objs_start = objs_open_match.start()
        # Anything between the header line and ``<Objs>`` (whitespace/newline)
        # is CLIXML framing — discard it.
        # Find the matching ``</Objs>`` after ``<Objs``. If missing (malformed
        # / truncated), fall back to dropping the remainder like the legacy
        # implementation did.
        objs_close_idx = text.find(_CLIXML_OBJS_CLOSE, objs_start)
        if objs_close_idx < 0:
            break
        objs_end = objs_close_idx + len(_CLIXML_OBJS_CLOSE)
        # Extract stream-content nodes strictly inside the ``<Objs>...</Objs>``
        # span (never past ``</Objs>`` — that's user stdout territory).
        clixml_body = text[objs_start:objs_end]
        nodes = _CLIXML_S_NODE_RE.findall(clixml_body)
        if nodes:
            out_parts.append("".join(_decode_node(nd) for nd in nodes))
        # Continue after ``</Objs>``. A trailing newline that PowerShell
        # emitted after the CLIXML line is preserved as part of the next
        # ``text[pos:...]`` slice.
        pos = objs_end
    return "".join(out_parts).strip()


def _extract_child_protected_denies(err_text: str) -> str:
    """Parse + STRIP child-process protected-deny markers, then audit each.

    P-08 #6 (design A): the child-process audit sentinel writes a
    ``[[QAI_PROTECTED_DENY]] {json}`` line to its stderr before raising
    ``PermissionError`` on a protected-path write (it cannot reach the parent's
    audit funnel from its isolated interpreter). Here — the parent that captured
    that stderr — we:

      1. extract every marker line and REMOVE it from ``err_text`` (so the
         internal protocol never leaks to the user / model), and
      2. dispatch each recovered deny to the injected audit callback (wired by
         the apps layer to :meth:`AuditBypassSink.enqueue`; inert / no-op when
         unwired).

    Called ONCE per exec, immediately after decoding stderr and BEFORE any
    return path (timeout / output-capped / normal), so EVERY path returns the
    cleaned text. Pure wrt control flow — never raises, and returns ``err_text``
    unchanged when it contains no markers. Layering: this reaches the audit sink
    ONLY through ``qai.platform.child_process_deny_audit`` (the shared kernel),
    never importing ``apps`` — import-linter FORBIDS ``qai.ai_coding -> apps``.
    """
    clean, denies = parse_and_strip_deny_markers(err_text)
    if denies:
        notify_child_protected_deny(denies)
    return clean


def _timeout_result(
    command: str,
    shell: str,
    out_text: str,
    err_text: str,
    exit_code: int,
    timeout: float,
) -> dict[str, Any]:
    return {
        "ok": False,
        "error_code": "ai_coding.tool.exec_timeout",
        "message": (
            f"exec: command timed out after {timeout:.0f}s and was killed"
        ),
        "command": command,
        "shell": shell,
        "stdout": out_text,
        "stderr": err_text,
        "exit_code": exit_code,
        "truncated": False,
        "timed_out": True,
    }


async def _run_via_process_runner(
    runner: ProcessRunnerPort,
    *,
    argv: list[str],
    command: str,
    shell: str,
    cwd: str | None,
    timeout: float,
    guard_token: str | None = None,
    ask_pending_probe: "Callable[[int], bool] | None" = None,
    native_denial_probe: NativeGuardDenialProbe | None = None,
    allow_x86: bool = False,
) -> dict[str, Any]:
    """Drain a :class:`ProcessRunnerPort` stream into the exec result dict.

    Collects stdout / stderr frames and the terminal exit status. The FULL
    decoded output is returned (only ANSI-stripped via :func:`_decode`);
    oversized-output persistence + head/tail preview is handled downstream by
    the registry's :class:`ToolResultStorePort` (V1 parity).

    OOM guard: the request carries ``output_byte_cap=EXEC_MAX_OUTPUT_BYTES`` so
    the runner (the plain :class:`SubprocessProcessRunner` post Phase 3
    cleanup; previously also wrapped by ``SandboxedProcessRunner``) trims +
    KILLS a flooding child at the platform layer and reports it via the
    terminating frame's ``status.truncated``. This is the SAME memory-safety
    cap the raw ``_dispatch_exec`` path applies via :func:`_communicate_capped`
    — without it the ``out_buf``/``err_buf`` here would grow unbounded (the real
    runtime ALWAYS injects a runner, so this path — not the raw fallback — is
    the one that must enforce the cap).
    """
    request = ProcessExecutionRequest(
        argv=tuple(argv),
        cwd=cwd,
        env=_build_exec_env(guard_token=guard_token, allow_x86=allow_x86),
        timeout_s=timeout if timeout > 0 else None,
        output_byte_cap=EXEC_MAX_OUTPUT_BYTES,
        ask_pending_probe=ask_pending_probe,
    )
    # TEMP DIAGNOSTIC (2026-07-12): capture the EXACT argv + PATH the daemon
    # hands to the runner, so a spawn crash (0xC000007B) can be diagnosed.
    try:
        _env = _build_exec_env(guard_token=guard_token, allow_x86=allow_x86)
        with open(
            Path(os.environ.get("TEMP", "C:/Windows/Temp")) / "qai_sh_debug.log",
            "a", encoding="utf-8",
        ) as _fh:
            import datetime as _dt
            _fh.write(
                f"[{_dt.datetime.now().isoformat()}] pid={os.getpid()} "
                f"RUNNER argv={list(argv)} cwd={cwd!r} "
                f"runner={type(runner).__name__} "
                f"PATH_head={_env.get('PATH','')[:400]!r}\n"
            )
    except Exception:
        pass
    # D2-B: sample wall-clock BEFORE the runner spawns the child so the
    # post-run audit query can filter to denies triggered by THIS subprocess.
    spawn_started_at = datetime.now(tz=timezone.utc)
    out_buf = bytearray()
    err_buf = bytearray()
    exit_code = -1
    timed_out = False
    output_capped = False
    # D2-B: capture the child pid from the STARTED frame so we can scope the
    # audit query to this subprocess's pid tree. ``None`` until the runner
    # emits a ``ProcessStartedFrame``; stays ``None`` if the runner never
    # emits one (e.g. spawn failure) — the probe guard below handles that.
    child_pid: int | None = None
    suppress_advice = command_has_explicit_redirect(command)
    try:
        async for frame in runner.run(request):
            if isinstance(frame, ProcessStartedFrame):
                child_pid = frame.pid
            elif isinstance(frame, ProcessStdoutFrame):
                out_buf.extend(frame.data)
            elif isinstance(frame, ProcessStderrFrame):
                err_buf.extend(frame.data)
            elif isinstance(frame, ProcessTerminatedFrame):
                exit_code = frame.status.exit_code
                timed_out = frame.status.timed_out
                # The runner trimmed+killed the child when its combined output
                # exceeded ``output_byte_cap`` (the OOM guard).
                output_capped = bool(getattr(frame.status, "truncated", False))
    except FileNotFoundError as e:
        raise ToolError(f"exec: shell binary not found: {e}") from e
    except OSError as e:
        # x86 denial: when native guard64 refuses a 32-bit child,
        # CreateProcessW returns FALSE with ERROR_ACCESS_DENIED (WinError 5).
        # Detect this specific case and give the model a precise, actionable
        # message instead of a generic "spawn failed".
        if getattr(e, "winerror", None) == 5 and argv and argv[0]:
            machine = _pe_machine(Path(argv[0]))
            if machine == 0x014C:  # IMAGE_FILE_MACHINE_I386
                raise ToolError(
                    f"exec: refused to spawn '{Path(argv[0]).name}' — it is a "
                    "32-bit (x86) executable.\n\n"
                    "The Agent security environment (guard64.dll) cannot monitor "
                    "32-bit processes. Running one would bypass all file-access "
                    "security policies, so it was blocked pre-spawn.\n\n"
                    "Action: use the 64-bit (x86-64 or ARM64) version of this "
                    "program. If you must use a 32-bit tool, ask the user to "
                    "enable 'Allow 32-bit processes' in Settings → Security."
                ) from e
        raise ToolError(f"exec: subprocess spawn failed: {e}") from e

    # The platform runner surfaces ``exit_code=None`` when it killed the child
    # (timeout / output-cap), since the OS returncode is then just noise. Map it
    # to ``-1`` so the result has an int code (parity with the raw path's
    # ``proc.returncode ... else -1``) and the message never reads "exited None".
    if exit_code is None:
        exit_code = -1

    out_text, out_trunc = _decode(bytes(out_buf), suppress_advice=suppress_advice)
    err_text, err_trunc = _decode(bytes(err_buf), suppress_advice=suppress_advice)
    # PowerShell serialises its stderr streams into CLIXML when stderr is a
    # pipe; unwrap it to readable text (no-op for non-powershell / non-CLIXML).
    if shell == "powershell":
        err_text = _strip_powershell_clixml(err_text)
    # P-08 #6: strip + audit child-process protected-deny markers before any
    # return path (the routed runner is the production exec path). See the raw
    # ``_dispatch_exec`` twin above.
    err_text = _extract_child_protected_denies(err_text)

    if timed_out:
        return _timeout_result(
            command, shell, out_text, err_text, exit_code, timeout
        )

    if output_capped:
        err_text = _output_cap_note(err_text)

    # V1 parity: non-zero exit diagnostic. This path ran through the
    # injected ``ProcessRunnerPort``; the ``sandboxed=True`` flag is the
    # diagnostic-attribution hint preserved from the pre Phase 3 cleanup
    # days (it used to mean "ran inside the AppContainer launcher" — now
    # both branches execute directly, but the flag keeps diagnostics
    # output stable for consumers).
    diagnostics = format_exit_diagnostics(
        exit_code,
        out_text,
        err_text,
        sandboxed=True,
        command=command,
    )

    # D2-B — native FileGuard denial diagnostics (runner path). Mirrors the
    # raw ``_dispatch_exec`` path above; uses ``child_pid`` recovered from the
    # ``ProcessStartedFrame`` emitted by the runner.
    diagnostics = await _maybe_append_native_denial_note(
        diagnostics,
        exit_code=exit_code,
        pid=child_pid,
        spawn_started_at=spawn_started_at,
        probe=native_denial_probe,
    )

    return _ok(
        f"exec exited {exit_code}",
        command=command,
        shell=shell,
        stdout=out_text,
        stderr=err_text,
        exit_code=exit_code,
        truncated=out_trunc or err_trunc or output_capped,
        timed_out=False,
        exit_diagnostics=diagnostics,
    )


def _output_cap_note(err_text: str) -> str:
    """Prepend the EXEC_MAX_OUTPUT_BYTES truncation note to ``err_text``.

    Shared by the raw (:func:`_dispatch_exec`) and runner
    (:func:`_run_via_process_runner`) paths so both surface an identical
    "output was capped, narrow the command" hint to the model.
    """
    note = (
        f"[output truncated] exec output exceeded "
        f"{EXEC_MAX_OUTPUT_BYTES // (1024 * 1024)}MB and the command was "
        f"stopped. The text above is the HEAD of a much larger output -- "
        f"narrow the command (e.g. filter with a pattern, target a specific "
        f"path, or page the output) instead of dumping everything."
    )
    return f"{note}\n{err_text}" if err_text else note


async def _maybe_append_native_denial_note(
    diagnostics: str,
    *,
    exit_code: int,
    pid: int | None,
    spawn_started_at: datetime,
    probe: NativeGuardDenialProbe | None,
) -> str:
    """Append a native FileGuard denial note to ``diagnostics`` when warranted.

    D2-B helper shared by the raw (:func:`_dispatch_exec`) and runner
    (:func:`_run_via_process_runner`) paths.

    Returns ``diagnostics`` unchanged when any of the following hold:

    * ``exit_code == 0`` — the command succeeded; no denial is relevant.
    * ``pid is None`` — the subprocess pid is unknown; cannot scope the query.
    * ``probe is None`` — no audit query port was injected (fail-open).
    * The probe returns ``""`` — audit found no matching DENY rows.
    * The probe raises — swallowed (AGENTS.md §5 铁律5: fail-open).

    The probe is expected to return a string that either is ``""`` (no
    denials) or starts with ``"\\n\\n"`` (the
    :func:`build_native_guard_denial_note` contract), so a plain
    ``diagnostics + note`` concatenation is always safe.
    """
    if exit_code == 0 or pid is None or probe is None:
        return diagnostics
    try:
        note = await probe(pid, spawn_started_at)
    except Exception:  # noqa: BLE001 — never let audit break exec result
        return diagnostics
    return diagnostics + note
