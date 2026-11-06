# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

r"""``cmd /c`` commands keep their double quotes (Windows).

``create_subprocess_exec(["cmd", "/c", 'dir "C:\Program Files"'])`` makes Python escape the
quotes as ``\"``, which cmd.exe does not understand: ``dir`` fails, and ``if exist "..."``
silently takes the ELSE branch -- an agent then concludes that an installed program is missing.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from qai.platform.process import cmd_raw_command, create_subprocess_for_argv
from qai.platform.process.ports import ProcessExecutionRequest
from qai.platform.process.subprocess_runner import SubprocessProcessRunner
from qai.tools.infrastructure.tool_exec_stream import ExecStreamFrameKind, stream_exec

win_only = pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe quoting is a Windows matter")

_SYS32 = r"C:\Windows\System32"


# ── the decision ────────────────────────────────────────────────────────────


@win_only
@pytest.mark.parametrize(
    "argv",
    [
        ["cmd", "/c", r'dir /b "C:\Program Files"'],
        ["CMD", "/C", 'echo "x"'],
        ["cmd.exe", "/c", 'echo "x"'],
        [r"C:\Windows\System32\cmd.exe", "/c", 'echo "x"'],
    ],
)
def test_a_quote_bearing_cmd_call_is_taken_over(argv: list[str]) -> None:
    assert cmd_raw_command(argv) == argv[2]


@win_only
@pytest.mark.parametrize(
    "argv",
    [
        ["cmd", "/c", r"dir /b C:\Windows"],  # no quote: nothing to protect, behaviour unchanged
        ["cmd", "/c", r"C:\Users\a b\x.bat"],  # a .bat path with a space: list2cmdline quotes it correctly
        ["cmd", "/k", 'echo "x"'],
        ["cmd", "/c", "echo", '"x"'],
        ["cmd", "/c"],
        ["powershell", "-File", "x.ps1"],
        [r"C:\Git\sh.exe", "-c", 'echo "x"'],
        ["python", "-c", 'print("x")'],
        [],
    ],
)
def test_everything_else_keeps_the_old_spawn(argv: list[str]) -> None:
    assert cmd_raw_command(argv) is None


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX only")
def test_never_applies_off_windows() -> None:
    assert cmd_raw_command(["cmd", "/c", 'echo "x"']) is None


# ── real cmd.exe ────────────────────────────────────────────────────────────


async def _run(argv: list[str]) -> tuple[int, str, str]:
    proc = await create_subprocess_for_argv(argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode or 0, out.decode("mbcs", "replace").strip(), err.decode("mbcs", "replace").strip()


#: (command, predicate on stdout) -- each one is WRONG with the old spawn.
_QUOTED = [
    ('echo "a b"', lambda o: o == '"a b"'),
    (f'dir /b "{_SYS32}\\drivers\\etc"', lambda o: "hosts" in o),
    (f'if exist "{_SYS32}" (echo yes) else (echo no)', lambda o: o == "yes"),
    (f'dir /s /b "{_SYS32}\\cmd.exe" 2>nul || echo missing', lambda o: o.lower().endswith("cmd.exe")),
    (f'dir "{_SYS32}\\cmd.exe" >nul 2>&1 && echo FOUND || echo NOT_FOUND', lambda o: o == "FOUND"),
    ('echo a & echo "b c"', lambda o: o.splitlines()[-1] == '"b c"'),
]


@win_only
@pytest.mark.parametrize("command,check", _QUOTED, ids=[c[0][:40] for c in _QUOTED])
def test_quoted_cmd_commands_run_as_typed(command: str, check) -> None:
    rc, out, err = asyncio.run(_run(["cmd", "/c", command]))
    assert check(out), (rc, out, err)


@win_only
def test_commands_without_quotes_are_unaffected() -> None:
    rc, out, _ = asyncio.run(_run(["cmd", "/c", f"dir /b {_SYS32}\\drivers\\etc"]))
    assert rc == 0 and "hosts" in out


@win_only
def test_a_bat_path_with_a_space_still_runs(tmp_path: Path) -> None:
    d = tmp_path / "a b"
    d.mkdir()
    bat = d / "x.bat"
    bat.write_text("@echo off\r\necho from-bat\r\n")
    rc, out, err = asyncio.run(_run(["cmd", "/c", str(bat)]))
    assert out == "from-bat", (rc, out, err)


@win_only
@pytest.mark.parametrize("command,check", _QUOTED[:3], ids=["echo", "dir", "if-exist"])
def test_the_chat_streaming_path_runs_quoted_commands_as_typed(command: str, check) -> None:
    async def go() -> str:
        frames, _result = await stream_exec(command, argv=["cmd", "/c", command])
        return "".join([f.data async for f in frames if f.kind is ExecStreamFrameKind.STDOUT]).strip()

    assert check(asyncio.run(go()))


@win_only
def test_the_process_runner_path_runs_quoted_commands_as_typed() -> None:
    async def go() -> str:
        runner = SubprocessProcessRunner()
        req = ProcessExecutionRequest(argv=("cmd", "/c", f'if exist "{_SYS32}" (echo yes) else (echo no)'))
        out: list[str] = []
        async for frame in runner.run(req):
            if type(frame).__name__ == "ProcessStdoutFrame":
                data = getattr(frame, "data", b"")
                out.append(data if isinstance(data, str) else data.decode("mbcs", "replace"))
        return "".join(out).strip()

    assert asyncio.run(go()) == "yes"
