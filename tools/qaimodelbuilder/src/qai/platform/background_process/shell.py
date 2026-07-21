# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shell selection and argv assembly for background process spawning.

Handles per-shell ``acceptable()`` / ``args()`` selection with one
project-wide strengthening: **UTF-8 locale is injected in every shell
branch**, not only in PowerShell.

Why all branches need UTF-8 injection (AGENTS.md section 3.10):

- Windows ``cmd.exe`` defaults to active OEM code page (CP936 on zh-CN);
  child stdout bytes for non-ASCII text are CP936, the ring buffer
  (``buffer.clamp``) decodes them with ``utf-8 errors="replace"`` and the
  whole tail becomes U+FFFD.
- Linux/macOS may inherit ``LANG=C`` / ``LANG=en_US`` from a service
  manager environment; many CLI tools then fall back to ASCII output.
- bash/zsh interactive ``rc`` files often set ``LANG``/``LC_*`` correctly,
  but ``-l -c`` non-interactive invocations may bypass that path.

Five shell branches, dispatched on the **basename (lower-cased, suffix
stripped)** of ``shell`` so callers can pass either ``"bash"`` or
``"/usr/local/bin/bash"``:

* ``bash`` -> ``-l -c BASH_BOOTSTRAP bgp <cwd> <command>``
* ``zsh``  -> ``-l -c ZSH_BOOTSTRAP bgp <cwd> <command>``
* ``pwsh`` / ``powershell`` -> ``-NoLogo -NoProfile -NonInteractive
  -Command "<PWSH_SETUP>\n<command>"``
* ``cmd`` -> ``/c "chcp 65001 >nul && <command>"``
* ``fish`` / ``nu`` -> ``-c <command>`` (no bootstrap;
  their startup files are non-POSIX and our bootstrap script would
  not parse).
* anything else -> ``-c <command>`` fallback.

The bash / zsh bootstrap uses positional parameters (``$0 = "bgp"``,
``$1 = cwd``, ``$2 = command``) + ``eval "$2"`` to let the shell itself
handle quoting; Python does **not** ``shlex.quote`` the command. This
uses POSIX double-quoting for the relevant character set.

The PowerShell branch prepends a minimal three-line setup block that
forces ``[Console]::Input/OutputEncoding`` to UTF-8 (no BOM) and aligns
``$OutputEncoding`` for native command piping. This is a deliberate V1
simplification of a prologue-aware parser (which also handles
``using namespace`` / ``param()`` blocks that must precede other
statements); for V1 we only support commands that do not start with a
PowerShell prologue. See ``background-process-design.md`` section 6.3.

This module is **stdlib-only** so it stays cheap to import from
anywhere in the package and carries no FastAPI / DI / SQL import chain.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import PurePath

__all__ = [
    "BASH_BOOTSTRAP",
    "PWSH_SETUP",
    "ZSH_BOOTSTRAP",
    "acceptable",
    "args",
    "build_argv",
]


BASH_BOOTSTRAP: str = (
    "\n"
    "export LC_ALL=C.UTF-8 LANG=C.UTF-8 LANGUAGE=C.UTF-8 2>/dev/null || true\n"
    "shopt -s expand_aliases\n"
    "[[ -f ~/.bashrc ]] && source ~/.bashrc >/dev/null 2>&1 || true\n"
    'cd -- "$1"\n'
    'eval "$2"\n'
)
"""bash ``-l -c`` bootstrap script.

Positional parameters when invoked via
``bash -l -c <BASH_BOOTSTRAP> bgp <cwd> <command>``:

* ``$0`` -- ``"bgp"`` (shows up in ``ps`` as the pseudo-arg0; this
  project uses ``"bgp"`` here).
* ``$1`` -- working directory (``cd -- "$1"``).
* ``$2`` -- user command (``eval "$2"`` so the shell parses it).

UTF-8 export is prefixed (``2>/dev/null || true``) so the bootstrap
survives on systems where ``C.UTF-8`` is not installed -- the export
silently no-ops and the rest of the script still runs.
"""


ZSH_BOOTSTRAP: str = (
    "\n"
    "export LC_ALL=C.UTF-8 LANG=C.UTF-8 LANGUAGE=C.UTF-8 2>/dev/null || true\n"
    "[[ -f ~/.zshenv ]] && source ~/.zshenv >/dev/null 2>&1 || true\n"
    '[[ -f "${ZDOTDIR:-$HOME}/.zshrc" ]] && '
    'source "${ZDOTDIR:-$HOME}/.zshrc" >/dev/null 2>&1 || true\n'
    'cd -- "$1"\n'
    'eval "$2"\n'
)
"""zsh ``-l -c`` bootstrap script.

Same shape as :data:`BASH_BOOTSTRAP` but sources ``.zshenv`` and the
``ZDOTDIR``-aware ``.zshrc`` location. ``shopt`` is bash-specific and is
omitted; zsh alias expansion in non-interactive mode is governed by
``setopt aliases`` (default on for ``-l`` login shells).
"""


PWSH_SETUP: str = (
    "[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false);"
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false);"
    "$OutputEncoding = [Console]::OutputEncoding;"
    "$ProgressPreference='SilentlyContinue';"
)
"""PowerShell setup block: force UTF-8 (no BOM) on both directions.

Prepended verbatim before the user's command (``<PWSH_SETUP>\\n<command>``)
and passed via ``-Command``. The ``new($false)`` argument disables the
BOM so downstream byte readers (the manager's ring buffer) see clean
UTF-8 lead bytes. ``$OutputEncoding`` aligns native-command pipe
encoding with the console encoding so things like
``some-native.exe | Out-File`` keep the same charset end-to-end.

``$ProgressPreference='SilentlyContinue'`` (2026-07-13) suppresses the
PowerShell progress stream. When powershell.exe runs with its stderr
redirected to a pipe (as the manager does), PowerShell serialises the
progress records ("Preparing modules for first use") into CLIXML on
stderr, producing ``#< CLIXML <Objs ...>`` noise even for a fully
successful command. Silencing the progress stream yields a clean
(empty) stderr; real errors are unaffected.

V1 limitation: this prepend will break if the user command starts with
a PowerShell prologue (``using namespace ...``, ``param(...)``,
``[CmdletBinding()]``) that must be the first statement. A
prologue parser could handle that; we defer it until a
concrete consumer needs it. Document in tool description that commands
should not start with such constructs.
"""


def _shell_kind(shell: str) -> str:
    """Reduce a shell path to its dispatch key.

    ``"/usr/local/bin/bash"`` -> ``"bash"``.
    ``"C:\\Program Files\\PowerShell\\7\\pwsh.exe"`` -> ``"pwsh"``.
    ``"powershell.exe"`` -> ``"powershell"``.

    Lower-cased so the Windows convention of capitalised PATH entries
    (``"PowerShell.exe"``) maps to the same branch as the lower-case
    form. Uses :class:`PurePath` (pure path operations, no IO) so the
    function is platform-independent.
    """
    if not shell:
        return ""
    name = PurePath(shell).name
    # Strip a single trailing extension (``.exe`` / ``.EXE`` / ``.cmd``);
    # PurePath.stem only strips one suffix which is what we want
    # (``bash.exe`` -> ``bash``; ``pwsh.exe`` -> ``pwsh``).
    if "." in name:
        name = PurePath(name).stem
    return name.lower()


def args(shell: str, command: str, cwd: str) -> list[str]:
    """Build the argv tail (everything after ``shell``) for ``command``.

    The returned list does **not** include the shell executable itself --
    callers prepend it (or use :func:`build_argv`). Dispatch is on the
    basename of ``shell`` lower-cased without extension; see
    :func:`_shell_kind`.

    :param shell: shell executable path or basename (``"bash"``,
        ``"/usr/local/bin/zsh"``, ``"powershell.exe"``, ...).
    :param command: raw user command. Not quoted by Python -- the shell
        parses it via ``eval "$2"`` (bash/zsh) or by being embedded in
        the ``-Command`` / ``/c`` argument (pwsh/cmd).
    :param cwd: working directory. Used by bash/zsh via ``cd -- "$1"``;
        ignored for pwsh/cmd/fish/nu (their working directory is
        controlled by the spawn-level ``cwd`` parameter).
    :return: argv tail to pass to ``Popen`` after the shell path.

    Example::

        >>> args("bash", "echo hi", "/tmp")[:2]
        ['-l', '-c']
        >>> args("powershell.exe", "echo hi", "")[:3]
        ['-NoLogo', '-NoProfile', '-NonInteractive']
        >>> args("cmd.exe", "echo hi", "")
        ['/c', 'chcp 65001 >nul && echo hi']
        >>> args("fish", "echo hi", "")
        ['-c', 'echo hi']
    """
    kind = _shell_kind(shell)
    if kind == "bash":
        return ["-l", "-c", BASH_BOOTSTRAP, "bgp", cwd, command]
    if kind == "zsh":
        return ["-l", "-c", ZSH_BOOTSTRAP, "bgp", cwd, command]
    if kind in ("pwsh", "powershell"):
        return [
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"{PWSH_SETUP}\n{command}",
        ]
    if kind == "cmd":
        return ["/c", f"chcp 65001 >nul && {command}"]
    if kind in ("fish", "nu"):
        return ["-c", command]
    # Unknown shell: best-effort POSIX-style ``-c <command>``.
    return ["-c", command]


def acceptable() -> str:
    """Pick the most appropriate shell for the current platform.

    Returns an absolute path when a candidate is found on ``PATH``;
    falls back to a conventional path (``/bin/sh``, ``cmd.exe`` from
    ``%COMSPEC%``) when nothing better is available. Never returns
    empty string.

    Selection order:

    * **Windows** -- ``pwsh.exe`` (PowerShell 7+, cross-platform Core)
      > ``powershell.exe`` (Windows PowerShell 5.1, always present on
      Win10/11) > ``cmd.exe`` > ``%COMSPEC%``.
    * **macOS** -- ``$SHELL`` if it ends in ``zsh`` / ``bash`` / ``sh``
      (avoids inheriting an exotic login shell like ``fish`` that our
      bootstrap cannot parse); else ``/bin/zsh`` (macOS default since
      Catalina).
    * **Linux/other POSIX** -- ``bash`` on ``PATH`` > ``/bin/sh``
      (POSIX guaranteed).
    """
    if sys.platform == "win32":
        for candidate in ("pwsh.exe", "powershell.exe", "cmd.exe"):
            found = shutil.which(candidate)
            if found:
                return found
        return os.environ.get("COMSPEC", "cmd.exe")
    if sys.platform == "darwin":
        env_shell = os.environ.get("SHELL", "")
        if env_shell:
            base = _shell_kind(env_shell)
            if base in ("zsh", "bash", "sh"):
                return env_shell
        return "/bin/zsh"
    # Linux + other POSIX.
    return shutil.which("bash") or "/bin/sh"


def build_argv(command: str, cwd: str, shell: str | None = None) -> list[str]:
    """Convenience: ``[shell, *args(shell, command, cwd)]``.

    Picks :func:`acceptable` when ``shell`` is omitted. Use this from
    the manager's hot path (``start()``) so callers do not duplicate
    the ``shell = shell or acceptable()`` dance.

    Returns a fresh list suitable for ``subprocess.Popen`` directly.
    """
    chosen = shell or acceptable()
    return [chosen, *args(chosen, command, cwd)]
