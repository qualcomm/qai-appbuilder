# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Install a ``.pth`` hook into a venv that routes Python bytecode caches
out of the source tree into ``<repo_root>/data/caches/pycache``.

Why a ``.pth`` (not an env var / not a launch script)
-----------------------------------------------------
``PYTHONPYCACHEPREFIX`` only reaches processes launched THROUGH the wrapper
scripts (``Start.bat`` / ``Console.bat`` / ``Setup.bat``). Anything that runs
the venv's ``python.exe`` directly — an IDE, a bare ``python foo.py`` /
``pytest`` in a plain terminal, or an automation agent — bypasses those
scripts and drops ``__pycache__`` next to the sources again.

CPython's ``site.py`` executes any ``import ...`` line found in a ``.pth``
file under ``site-packages`` at interpreter startup, for EVERY process that
uses the venv, with no env var and no wrapper required. So a one-line
``.pth`` that sets ``sys.pycache_prefix`` is the only mechanism that covers
the "ran Python directly" case as well.

We deliberately do NOT name the hook ``sitecustomize.py`` — that name is a
reserved external hook already owned by the native FileGuard child-process
audit sentinel (``src/qai/platform/child_process_audit_sentinel/
sitecustomize.py``); reusing it would collide. ``usercustomize.py`` is also
avoided because it is skipped when the user-site is disabled (common in
venvs). A ``.pth`` has neither constraint.

Runtime path resolution (belt + suspenders, State-Truth-First)
--------------------------------------------------------------
The generated ``.pth`` resolves the pycache root at interpreter startup, in
priority order:

1. ``PYTHONPYCACHEPREFIX`` already set (a wrapper script won) → do nothing,
   never override the stronger, explicit signal.
2. ``QAI_REPO_ROOT`` env var, when set → ``<that>/data/caches/pycache``.
3. The repo root baked in at install time (this script writes the resolved
   absolute path into the ``.pth``) → ``<baked>/data/caches/pycache``.

All of it is wrapped so a resolution failure is a silent no-op: the hook
must NEVER be able to crash an interpreter that merely imported ``site``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_PTH_NAME = "_qai_pycache_prefix.pth"

# Structural markers that identify the QAIModelBuilder repo root (AGENTS.md §5
# 铁律4 — locate by structure, never a fixed "up N levels" assumption).
_REPO_MARKERS = ("Setup.bat", "src/qai", "apps")


def find_repo_root(start: Path | None = None) -> Path:
    """Walk upward from *start* (default: this file) to the repo root.

    A directory qualifies when every marker in ``_REPO_MARKERS`` exists under
    it. Falls back to three-levels-up of this script's location
    (``scripts/setup/`` → repo root) if no marker match is found.
    """
    here = (start or Path(__file__).resolve()).resolve()
    for cand in (here, *here.parents):
        if all((cand / m).exists() for m in _REPO_MARKERS):
            return cand
    # Fallback: scripts/setup/install_pycache_pth.py → repo root is parents[2].
    return Path(__file__).resolve().parents[2]


def _pth_content(repo_root: Path) -> str:
    """Full ``.pth`` file text (a single executable line + trailing newline).

    ``.pth`` executable lines cannot contain a real newline, so the guard is
    expressed as a one-liner ``try: ... \nexcept`` is NOT possible on one
    line — instead we use a compound-statement-free guard via a helper that
    swallows errors. We keep it to a single ``import``-prefixed line.
    """
    baked = repo_root.as_posix()
    # One physical line. ``exec`` of a tiny program lets us use a real
    # try/except without embedding a newline in the .pth line itself.
    prog = (
        "import os,sys\n"
        "try:\n"
        "    _r=os.environ.get('QAI_REPO_ROOT') or r'" + baked + "'\n"
        "    if not os.environ.get('PYTHONPYCACHEPREFIX'):\n"
        "        sys.pycache_prefix=os.path.join(_r,'data','caches','pycache')\n"
        "except Exception:\n"
        "    pass\n"
    )
    line = "import sys; exec(compile(" + repr(prog) + ", '<qai-pycache-pth>', 'exec'))"
    return line + "\n"


def _site_packages(venv: Path) -> Path | None:
    """Return the venv's site-packages dir, or ``None`` if not found."""
    # Windows layout: <venv>/Lib/site-packages
    win = venv / "Lib" / "site-packages"
    if win.is_dir():
        return win
    # POSIX layout: <venv>/lib/pythonX.Y/site-packages
    lib = venv / "lib"
    if lib.is_dir():
        for child in sorted(lib.glob("python*")):
            sp = child / "site-packages"
            if sp.is_dir():
                return sp
    return None


def install(venv: Path, repo_root: Path) -> bool:
    """Write ``_qai_pycache_prefix.pth`` into *venv*'s site-packages.

    Returns ``True`` on success. Best-effort + idempotent: re-running
    overwrites with identical content.
    """
    sp = _site_packages(venv)
    if sp is None:
        print(f"  [WARN] site-packages not found under venv: {venv}")
        return False
    target = sp / _PTH_NAME
    content = _pth_content(repo_root)
    try:
        existing = target.read_text(encoding="utf-8") if target.exists() else None
        if existing == content:
            print(f"  [OK]   pycache .pth already current: {target}")
            return True
        target.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"  [WARN] could not write pycache .pth ({exc}): {target}")
        return False
    print(f"  [OK]   pycache redirect installed: {target}")
    print(f"         → {(repo_root / 'data' / 'caches' / 'pycache').as_posix()}")
    return True


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Install the bytecode-cache redirect .pth into a venv.",
    )
    p.add_argument(
        "--venv",
        required=True,
        help="Path to the venv root (the dir that contains Scripts/ or bin/).",
    )
    p.add_argument(
        "--repo-root",
        default=None,
        help="Repo root to bake into the .pth. Auto-detected when omitted.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    venv = Path(args.venv).expanduser().resolve()
    repo_root = (
        Path(args.repo_root).expanduser().resolve()
        if args.repo_root
        else find_repo_root()
    )
    ok = install(venv, repo_root)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
