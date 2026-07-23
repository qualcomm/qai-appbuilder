# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
install_app_builder_weights.py — App Builder Pack weight bootstrapper.

Why this script exists
======================

App Builder Packs (``factory/chat_features/app-builder/models/<id>/``) are *source-only*
artifacts in the repo. The actual QNN context-binary weights live outside the
Pack tree:

    Real file:    <repo>/models/<modelId>/<weight>.bin
    Pack symlink: <repo>/factory/chat_features/app-builder/models/<modelId>/weights/<weight>.bin

The Pack's ``manifest.json`` declares the canonical weight location via
``assets.installPath`` — this is the *single source of truth* the backend
registry (``backend/app_builder/registry.py``) reads at startup to compute
each Pack's run-readiness ``status`` (``ready`` / ``missing-weights`` /
``error``). Frontend ModelCards render the same status badge.

This script provides the two pre-flight steps a developer needs *before* the
first ``Run`` button click:

* ``check`` — Walk every Pack manifest, look up the file at
  ``<repo>/<assets.installPath>``, and report which ones are missing.
  Exits with code 1 if any are missing (CI-friendly).

* ``link``  — For every present real weight, create a symlink inside the
  Pack at ``weights/<basename>``. If symlink creation fails (typically
  Windows non-Administrator without Developer Mode), fall back to a copy
  and emit a clear hint.

We do **not** download anything here. Real OTA / aria2c-based weight
downloading is scheduled for v1.4 (see Roadmap N in the design doc); for
the v1.0 MVP, weights are placed manually under ``models/<modelId>/`` by
the developer (or by ``Setup.bat`` in a future iteration). The
``check`` command prints the most useful pointer we have today —
``assets.weightsUrl`` from the manifest if set, otherwise a hint to the
existing model_catalog flow used by model-builder.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# ─── Constants ────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PACK_ROOT = REPO_ROOT / "factory" / "chat_features" / "app-builder" / "models"


# ─── Tiny printer helpers (match setup_qairt_env.py style) ────────────────────

def _ok(msg: str) -> None:
    print(f"  [OK]   {msg}")


def _warn(msg: str) -> None:
    print(f"  [WARN] {msg}")


def _err(msg: str) -> None:
    print(f"  [ERR]  {msg}")


def _info(msg: str) -> None:
    print(f"  [INFO] {msg}")


# ─── Pack discovery ───────────────────────────────────────────────────────────

def _iter_pack_manifests() -> List[Tuple[str, Path, dict]]:
    """Return [(modelId, manifest_path, manifest_dict), ...] for every Pack.

    Skips:
      - dirs starting with ``_`` (e.g. ``_template``)
      - dirs with no ``manifest.json``
      - manifests that fail to parse (logs a WARN and continues)
    """
    out: List[Tuple[str, Path, dict]] = []
    if not PACK_ROOT.exists():
        return out
    for d in sorted(PACK_ROOT.iterdir()):
        if not d.is_dir():
            continue
        if d.name.startswith("_") or d.name.startswith("."):
            continue
        m = d / "manifest.json"
        if not m.exists():
            continue
        try:
            data = json.loads(m.read_text(encoding="utf-8"))
        except Exception as e:  # noqa: BLE001
            _warn(f"failed to parse {m}: {e}")
            continue
        out.append((d.name, m, data))
    return out


def _resolve_install_path(model_id: str, manifest: dict) -> Optional[Path]:
    """Translate manifest.assets.installPath into an absolute path.

    Manifest convention: ``installPath`` is a *repo-relative* path such as
    ``models/realesrgan-x4/realesr-general-x4v3.bin``. Returns ``None`` if
    the manifest does not declare one.
    """
    assets = manifest.get("assets") or {}
    install_path = assets.get("installPath")
    if not install_path:
        return None
    return (REPO_ROOT / install_path).resolve()


def _weights_url(manifest: dict) -> str:
    return ((manifest.get("assets") or {}).get("weightsUrl") or "").strip()


# ─── Subcommand: check ────────────────────────────────────────────────────────

def cmd_check() -> int:
    """Report which Pack weights are missing. Exit 1 if any missing."""
    print("=" * 70)
    print("  App Builder Pack — Weight Presence Check")
    print("=" * 70)

    packs = _iter_pack_manifests()
    if not packs:
        _warn(f"no Pack manifests found under {PACK_ROOT}")
        return 0

    missing: List[Dict] = []
    present: List[str] = []

    for model_id, manifest_path, manifest in packs:
        target = _resolve_install_path(model_id, manifest)
        if target is None:
            _warn(f"{model_id}: manifest has no assets.installPath — skipping")
            continue
        if target.exists():
            try:
                size_mb = target.stat().st_size / (1024 * 1024)
                _ok(f"{model_id}: {target.relative_to(REPO_ROOT)} ({size_mb:.1f} MB)")
            except OSError:
                _ok(f"{model_id}: {target}")
            present.append(model_id)
        else:
            missing.append({
                "modelId": model_id,
                "expected": target,
                "weightsUrl": _weights_url(manifest),
            })

    if missing:
        print()
        print(f"  Missing weights for {len(missing)} Pack(s):")
        for m in missing:
            rel = m["expected"]
            try:
                rel = m["expected"].relative_to(REPO_ROOT)
            except ValueError:
                pass
            print(f"    - {m['modelId']}")
            print(f"        expected: {rel}")
            if m["weightsUrl"]:
                print(f"        download: {m['weightsUrl']}")
            else:
                print(
                    "        download: (no weightsUrl in manifest; obtain via "
                    "model_catalog or place the .bin manually)"
                )
        print()
        _info(
            f"Place the missing files under <repo>/models/<modelId>/, then re-run "
            f"`python scripts/setup/install_app_builder_weights.py link` to create "
            f"the in-Pack weights/ symlinks."
        )
        return 1

    print()
    _ok(f"All {len(present)} Pack(s) have their weights present.")
    return 0


# ─── Subcommand: link ─────────────────────────────────────────────────────────

def _create_symlink(real: Path, link: Path) -> Tuple[bool, str]:
    """Try to create a file symlink. Return (ok, mode_used).

    ``mode_used`` is one of:
      - ``"symlink"``  — succeeded as a true OS symlink
      - ``"copy"``     — fell back to file copy (Windows non-admin / no dev mode)
      - ``"failed"``   — neither symlink nor copy worked
    """
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(real, link)
        return True, "symlink"
    except OSError as e:
        # Windows: error 1314 = "A required privilege is not held by the client"
        # i.e. non-admin without Developer Mode. Fall back to a regular copy.
        try:
            shutil.copy2(real, link)
            return True, "copy"
        except Exception as e2:  # noqa: BLE001
            return False, f"failed: symlink={e!r}, copy={e2!r}"


def _is_link_consistent(link: Path, real: Path) -> Tuple[bool, str]:
    """Decide whether an existing entry at ``link`` already points to ``real``.

    Returns (consistent, kind). ``kind`` is informational:
      - ``symlink-ok``   / ``symlink-mismatch``
      - ``regular-file`` (not a symlink — likely a copy from a previous run)
      - ``directory``    / ``other``
    """
    if link.is_symlink():
        try:
            target = Path(os.readlink(link))
            if not target.is_absolute():
                target = (link.parent / target).resolve()
            else:
                target = target.resolve()
            return (target == real.resolve()), (
                "symlink-ok" if target == real.resolve() else "symlink-mismatch"
            )
        except OSError as e:
            return False, f"symlink-unreadable ({e})"
    if link.is_dir():
        return False, "directory"
    if link.exists():
        # A regular file (not a symlink). Treat as consistent if size & mtime
        # match — common when the previous run fell back to copy mode.
        try:
            real_st = real.stat()
            link_st = link.stat()
            if real_st.st_size == link_st.st_size:
                return True, "regular-file (likely copy)"
            return False, "regular-file (size differs)"
        except OSError:
            return False, "regular-file (stat failed)"
    return False, "missing"


def cmd_link() -> int:
    """Create weights/<basename> symlinks inside each Pack with present real file."""
    print("=" * 70)
    print("  App Builder Pack — In-Pack weights/ Symlink Bootstrap")
    print("=" * 70)

    packs = _iter_pack_manifests()
    if not packs:
        _warn(f"no Pack manifests found under {PACK_ROOT}")
        return 0

    linked = 0
    skipped_existing = 0
    skipped_missing_real = 0
    fallback_copies = 0
    failed = 0

    for model_id, manifest_path, manifest in packs:
        real = _resolve_install_path(model_id, manifest)
        if real is None:
            _warn(f"{model_id}: manifest has no assets.installPath — skipping")
            continue
        if not real.exists():
            skipped_missing_real += 1
            try:
                rel = real.relative_to(REPO_ROOT)
            except ValueError:
                rel = real
            _info(f"{model_id}: real weight not yet placed at {rel} — skipping")
            continue

        pack_weights_dir = manifest_path.parent / "weights"
        link = pack_weights_dir / real.name

        if link.is_symlink() or link.exists():
            consistent, kind = _is_link_consistent(link, real)
            if consistent:
                skipped_existing += 1
                _ok(f"{model_id}: weights/{real.name} already linked ({kind})")
            else:
                _warn(
                    f"{model_id}: weights/{real.name} exists but is inconsistent "
                    f"({kind}); leaving in place — remove it manually if you want "
                    f"to re-link."
                )
            continue

        ok, mode = _create_symlink(real, link)
        if not ok:
            failed += 1
            _err(f"{model_id}: failed to materialize weights/{real.name}: {mode}")
            continue
        if mode == "symlink":
            linked += 1
            _ok(f"{model_id}: weights/{real.name} → symlink → {real}")
        elif mode == "copy":
            fallback_copies += 1
            _warn(
                f"{model_id}: weights/{real.name} created as a COPY (symlink "
                f"creation requires Administrator or Windows Developer Mode). "
                f"This works for runtime but doubles disk usage."
            )

    print()
    summary = (
        f"linked={linked}, copy-fallback={fallback_copies}, "
        f"already-present={skipped_existing}, real-missing={skipped_missing_real}, "
        f"failed={failed}"
    )
    if linked == 0 and fallback_copies == 0 and skipped_missing_real == len(packs):
        _info(
            "No real weights are present yet under <repo>/models/<modelId>/ — "
            "place the .bin files there first, then re-run this command."
        )
    _info(f"Summary: {summary}")
    # `link` always returns 0 — failures here are non-fatal and reported above.
    # The `check` subcommand is the gate for CI.
    return 0


# ─── CLI ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect / bootstrap App Builder Pack weight files.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="Verify each Pack's manifest.assets.installPath exists. Exits 1 if any missing.")
    sub.add_parser("link", help="Create weights/<basename> symlinks inside each Pack with present real file.")

    args = parser.parse_args(argv)

    if args.cmd == "check":
        return cmd_check()
    if args.cmd == "link":
        return cmd_link()
    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
