# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""factory/config/file_guard_paths.json - the Windows signature-catalog stores are write-denied silently.

Converting a model loads the QAIRT SDK's signed DLLs / .pyd modules; Windows' signature lookup may then open
C:\\Windows\\System32\\CatRoot / catroot2 with write intent. The OS ACL already refuses that write for a non-elevated
child, so an ASK prompt there could never be meaningfully granted and stalled the child until the callback timeout.
These tests read the REAL config through the REAL loader and apply the same union rule as check_permission
(any matching deny wins; otherwise every requested op must be allowed; otherwise ASK).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[4]

pytestmark = pytest.mark.skipif(sys.platform != "win32" or not os.environ.get("SystemRoot"), reason="Windows paths")

R, W, X = 1, 2, 4


@pytest.fixture(scope="module")
def rows(monkeypatch_module):
    import apps.api._workspace_resolver as wr

    monkeypatch_module.setattr(wr, "_repo_root", lambda c: ROOT)
    return wr.resolve_file_guard_masked_paths(None)


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


def decide(rows, path: str, want: int) -> str:
    from qai.security.application.use_cases.check_permission import _path_under_any_prefix

    allow = deny = 0
    for prefix, a, d in rows:
        if _path_under_any_prefix(path, (prefix,)):
            allow |= a
            deny |= d
    if want & deny:
        return "DENY"
    return "ALLOW" if (want & allow) == want else "ASK"


SYS = os.environ.get("SystemRoot", r"C:\Windows")


@pytest.mark.parametrize("sub", [r"System32\CatRoot", r"System32\catroot2"])
def test_catalog_stores_are_read_execute_allow_write_delete_deny(rows, sub):
    entry = [r for r in rows if r[0].casefold() == str(Path(SYS, sub).resolve()).casefold()]
    assert len(entry) == 1, f"{sub} must have exactly one entry"
    _, allow, deny = entry[0]
    assert allow == R | X, "read/execute restate the %SystemRoot% parent's allow"
    assert deny == W | 8, "write (and the delete that follows it) is hard-denied"


@pytest.mark.parametrize(
    "path,want,expected",
    [
        (r"System32\CatRoot\{F750E6C3-38EE-11D1-85E5-00C04FC295EE}\a.cat", W, "DENY"),
        (r"System32\CatRoot\{F750E6C3-38EE-11D1-85E5-00C04FC295EE}\a.cat", R, "ALLOW"),
        (r"System32\catroot2\edb.log", W, "DENY"),
        (r"System32\catroot2\{127D0A1D-4EF2-11D1-8608-00C04FC295EE}\catdb", R, "ALLOW"),
        (r"System32\CATROOT\a.cat", W, "DENY"),  # case-insensitive
        # must NOT widen: other System32 writes still prompt, prefix stops at the component boundary
        (r"System32\CatRootX\a", W, "ASK"),
        (r"System32\Tasks\new_task", W, "ASK"),
        (r"System32\drivers\etc\hosts", W, "ASK"),
        (r"System32\kernel32.dll", X, "ALLOW"),
        # neighbours unchanged
        (r"Temp\scratch.txt", W, "ALLOW"),
        (r"INF\cpu.PNF", W, "DENY"),
    ],
)
def test_decisions(rows, path, want, expected):
    assert decide(rows, str(Path(SYS, path)), want) == expected
