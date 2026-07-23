# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
App Builder WebUI — generic startup dependency check  (copy as-is)
==================================================================

HOW TO USE
----------
1. Copy this file verbatim to  <app_id>/backend/ensure_deps.py  (no edits
   needed for the common case).
2. The launch scripts (run.bat / run.ps1 / run.sh) call it BEFORE uvicorn:
       python -m backend.ensure_deps
   If it exits non-zero, the launcher stops and prints the reason instead of
   starting a server that would only die later with an obscure ImportError.

What it does (idempotent, standard-Python dependency management)
----------------------------------------------------------------
The packaged app may be unzipped on ANOTHER machine where we can only assume a
Python interpreter exists — the QAI ModelBuilder shared venv, and even
``qai_appbuilder`` itself, may be absent. So at startup we:

  1. Read  <app>/requirements.txt  AND every bundled  pack/<id>/requirements.txt
     (each model — built-in OR user-converted — ships its own deps), and merge
     them (later files do not override an earlier pin for the same distribution).
     This means a model's dependencies are provisioned even if the app author
     did not manually copy them into the top-level requirements.txt.
  2. For each requirement, check whether it is importable in THIS interpreter
     (``importlib.util.find_spec``), mapping the pip *distribution* name to its
     *import* name (numpy→numpy, opencv-python→cv2, pillow→PIL, …; overridable
     inline in requirements.txt via ``# import: <name>``).
  3. pip-install only the MISSING ones — preferring a bundled local wheel under
     ``<app>/vendor/whl/`` (offline / ARM64 wheels) and falling back to the
     configured pip index. This keeps the check fast when everything is already
     present (the dev-host case) and self-heals a bare target machine.
  4. Run each bundled pack's optional ``pack/<id>/predeploy.py`` hook for
     NON-pip runtime data (NLTK corpora, tokenizer caches, dictionary warm-ups)
     that ``pip install`` cannot provide. See the melotts-zh pack for an example.
  5. Write a sentinel (``<app>/.deps_ok``) keyed on the interpreter + a hash of
     requirements.txt so a warm start skips straight through in milliseconds.

Design notes
------------
* No third-party imports here — this module must run on a stock interpreter
  BEFORE any dependency is guaranteed installed.
* ``--force`` (or deleting ``.deps_ok``) re-runs the full check.
* A pip failure is FATAL (non-zero exit): better a clear "could not install X"
  at launch than a mysterious traceback on the first request.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# <app>/backend/ensure_deps.py -> <app>
_APP_ROOT = Path(__file__).resolve().parent.parent
_REQ_FILE = _APP_ROOT / "requirements.txt"
_VENDOR_WHL = _APP_ROOT / "vendor" / "whl"
_PACK_ROOT = _APP_ROOT / "pack"
_SENTINEL = _APP_ROOT / ".deps_ok"

# pip *distribution* name  ->  *import* (module) name, for the cases where they
# differ. Extend as new packs need it; unknown names default to a best-effort
# normalisation (dashes -> underscores). Inline ``# import: <name>`` in
# requirements.txt always wins over this table.
_IMPORT_NAME = {
    "opencv-python": "cv2",
    "opencv-python-headless": "cv2",
    "pillow": "PIL",
    "pyyaml": "yaml",
    "openai-whisper": "whisper",
    "more-itertools": "more_itertools",
    "scikit-learn": "sklearn",
    "protobuf": "google.protobuf",
    "qai-appbuilder": "qai_appbuilder",
}


def _log(msg: str) -> None:
    print(f"[ensure_deps] {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"[ensure_deps][WARN] {msg}", file=sys.stderr, flush=True)


def _fatal(msg: str) -> int:
    print(f"[ensure_deps][FATAL] {msg}", file=sys.stderr, flush=True)
    return 1


# ── requirements.txt parsing ───────────────────────────────────────────────

def _requirements_files() -> list[Path]:
    """Every requirements.txt to honour, app-level first then each pack's.

    Order matters for the dedupe in :func:`_merge_requirements`: the app-level
    file wins over a pack file for the same distribution (an app author can
    pin/override), and built-in vs user-converted packs are treated identically
    — both live under ``pack/<id>/`` in the packaged zip.
    """
    files: list[Path] = []
    if _REQ_FILE.is_file():
        files.append(_REQ_FILE)
    if _PACK_ROOT.is_dir():
        files.extend(sorted(_PACK_ROOT.glob("*/requirements.txt")))
    return files


def _merge_requirements(files: list[Path]) -> tuple[list[dict], str]:
    """Parse + merge several requirements files, deduping by distribution.

    Returns ``(entries, combined_text)``. The first occurrence of a
    distribution wins (files are passed app-first), so a pack cannot silently
    downgrade a pin the app author chose. ``combined_text`` feeds the sentinel
    fingerprint so adding a model (new pack requirements.txt) re-triggers a
    check.
    """
    entries: list[dict] = []
    seen: set[str] = set()
    chunks: list[str] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _warn(f"could not read {f}: {exc}")
            continue
        chunks.append(f"# --- {f.name} @ {f.parent.name} ---\n{text}")
        for entry in _parse_requirements(text):
            key = entry["dist"].lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append(entry)
    return entries, "\n".join(chunks)


def _parse_requirements(text: str) -> list[dict]:
    """Parse requirements.txt into ``[{spec, dist, import_name}]`` entries.

    Ignores blank / comment / option (``-r``, ``--find-links`` …) lines. Honors
    an inline ``# import: <name>`` override for the module name. Strips version
    specifiers / extras / environment markers to recover the distribution name.
    """
    out: list[dict] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue

        import_override = None
        # Split off a trailing comment; capture an ``import:`` hint if present.
        if "#" in line:
            code, comment = line.split("#", 1)
            line = code.strip()
            comment = comment.strip()
            if comment.lower().startswith("import:"):
                import_override = comment.split(":", 1)[1].strip()
            if not line:
                continue

        # Distribution name = leading run of name chars before any
        # version/extra/marker separator.
        dist = line
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", "[", ";", " ", "@"):
            idx = dist.find(sep)
            if idx != -1:
                dist = dist[:idx]
        dist = dist.strip()
        if not dist:
            continue

        key = dist.lower()
        import_name = (
            import_override
            or _IMPORT_NAME.get(key)
            or dist.replace("-", "_")
        )
        out.append({"spec": line, "dist": dist, "import_name": import_name})
    return out


def _is_importable(import_name: str) -> bool:
    """True if ``import_name`` (possibly dotted) resolves in this interpreter."""
    try:
        return importlib.util.find_spec(import_name) is not None
    except (ImportError, ValueError, ModuleNotFoundError):
        # A parent package that is itself missing raises ModuleNotFoundError;
        # treat that as "not importable" so we attempt an install.
        return False


# ── installation ────────────────────────────────────────────────────────────

def _find_local_wheel(dist: str) -> Path | None:
    """Return a bundled wheel for ``dist`` under vendor/whl/, if any.

    Wheel filenames normalise the distribution name with underscores and are
    case-insensitive (PEP 427), e.g. ``qai_appbuilder-2.46.0-...whl`` for the
    ``qai-appbuilder`` distribution.
    """
    if not _VENDOR_WHL.is_dir():
        return None
    norm = dist.replace("-", "_").lower()
    for whl in sorted(_VENDOR_WHL.glob("*.whl")):
        if whl.name.replace("-", "_").lower().startswith(norm + "_"):
            return whl
    return None


def _pip_install(target: str, *, from_wheel: bool) -> bool:
    """Run ``pip install`` for one target (a wheel path or a requirement spec)."""
    cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check"]
    if from_wheel:
        # A local wheel install should not go back to the index for the wheel
        # itself, but may still need its (already-present) deps resolved.
        cmd.append(target)
    else:
        cmd.append(target)
    _log(f"pip install {target}")
    try:
        proc = subprocess.run(cmd, check=False)
    except OSError as exc:
        _warn(f"could not launch pip for {target!r}: {exc}")
        return False
    return proc.returncode == 0


def _install_missing(missing: list[dict]) -> list[str]:
    """Install each missing entry; return the list that still failed."""
    failed: list[str] = []
    for entry in missing:
        dist, spec = entry["dist"], entry["spec"]
        wheel = _find_local_wheel(dist)
        ok = False
        if wheel is not None:
            _log(f"{dist}: found bundled wheel {wheel.name}")
            ok = _pip_install(str(wheel), from_wheel=True)
        if not ok:
            ok = _pip_install(spec, from_wheel=False)
        if not ok:
            failed.append(dist)
    return failed


# ── per-pack non-pip hooks ────────────────────────────────────────────────

def _run_predeploy_hooks() -> None:
    """Invoke each bundled ``pack/<id>/predeploy.py`` (best-effort).

    These handle runtime data that ``pip`` cannot: NLTK corpora, tokenizer /
    G2P caches, dictionary warm-ups. Each hook is idempotent and owns its own
    sentinel; a failure is a warning, not fatal, because the app may still run
    (just slower / with a degraded path). Run in a subprocess so a hook crash
    cannot take down this checker.
    """
    if not _PACK_ROOT.is_dir():
        return
    for hook in sorted(_PACK_ROOT.glob("*/predeploy.py")):
        _log(f"running pack predeploy hook: {hook.relative_to(_APP_ROOT).as_posix()}")
        try:
            proc = subprocess.run(
                [sys.executable, str(hook)],
                check=False,
                cwd=str(hook.parent),
            )
            if proc.returncode != 0:
                _warn(
                    f"predeploy hook {hook.name} exited {proc.returncode} "
                    f"(app may run with a degraded / slower path)"
                )
        except OSError as exc:
            _warn(f"could not run predeploy hook {hook}: {exc}")


# ── sentinel ──────────────────────────────────────────────────────────────

def _fingerprint(combined_text: str) -> str:
    h = hashlib.sha256()
    h.update(sys.executable.encode("utf-8", "replace"))
    h.update(b"\0")
    h.update(combined_text.encode("utf-8", "replace"))
    return h.hexdigest()


def _sentinel_matches(fingerprint: str) -> bool:
    if not _SENTINEL.is_file():
        return False
    try:
        data = json.loads(_SENTINEL.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return data.get("fingerprint") == fingerprint


def _write_sentinel(fingerprint: str) -> None:
    try:
        _SENTINEL.write_text(
            json.dumps(
                {
                    "fingerprint": fingerprint,
                    "python": sys.executable,
                    "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        _warn(f"could not write sentinel {_SENTINEL}: {exc}")


# ── entry point ─────────────────────────────────────────────────────────────

def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    force = "--force" in argv

    _log(f"app root  = {_APP_ROOT}")
    _log(f"python    = {sys.executable}")

    req_files = _requirements_files()
    if not req_files:
        _log("no requirements.txt (app or pack) found — nothing to check.")
        _run_predeploy_hooks()
        return 0
    _log(
        "requirements sources: "
        + ", ".join(f.relative_to(_APP_ROOT).as_posix() for f in req_files)
    )

    entries, combined_text = _merge_requirements(req_files)
    fingerprint = _fingerprint(combined_text)

    if not force and _sentinel_matches(fingerprint):
        _log("dependencies already verified (sentinel hit) — skipping.")
        return 0

    if not entries:
        _log("requirements declare no packages — nothing to install.")
        _run_predeploy_hooks()
        _write_sentinel(fingerprint)
        return 0

    missing = [e for e in entries if not _is_importable(e["import_name"])]
    if missing:
        names = ", ".join(e["dist"] for e in missing)
        _log(f"missing: {names}")
        failed = _install_missing(missing)
        if failed:
            return _fatal(
                "could not install: "
                + ", ".join(failed)
                + ". Install them manually into this Python environment "
                f"({sys.executable}) — e.g. `pip install "
                + " ".join(failed)
                + "` — or drop matching wheels into vendor/whl/, then re-run."
            )
        _log("all missing dependencies installed.")
    else:
        _log(f"all {len(entries)} declared dependencies already present.")

    # Non-pip runtime data (NLTK, caches, warm-ups) — after pip deps exist.
    _run_predeploy_hooks()

    _write_sentinel(fingerprint)
    _log("dependency check complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
