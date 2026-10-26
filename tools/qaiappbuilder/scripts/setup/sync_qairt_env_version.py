# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Bring an existing qairt_env.json in line with the QAIRT version in qairt_release.json.

Usage:
    python sync_qairt_env_version.py <qairt_env.json> <qairt_release.json>

qairt_env.json is generated on the user's machine and survives an upgrade install, so it
can still point at the SDK of the previous release. Runtime code only reads qairt_env.json,
and the "is the converter environment ready" check only looks at whether its paths exist,
so a stale file would keep the old SDK in use and never ask for the new one.

When the version recorded in qairt_env.json differs from qairt_release.json, the SDK
fields are rewritten to the release version (sdk root, version, download url). If the new
SDK is not installed yet, the readiness check then reports it missing and the user is asked
to install it. Every other key (venvs, arch, ...) is kept. The old SDK is left on disk.

Version sources compared (each only when present): ``qairt_version``, ``_version`` and the
SDK root folder name when it looks like a version (a custom ``--sdk-root`` such as
``D:\\sdk\\qairt`` is not treated as a mismatch on its own). Any difference -> rewrite.

Idempotent: no write when everything already matches. Never fails the caller: problems are
reported as [WARN] and exit code 0, except bad usage (exit 1).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(\.\d+)?$")
_ROOT_KEYS = ("qairt_sdk_root", "qairt_root")
_VERSION_KEYS = ("qairt_version", "_version")


def _load_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def recorded_versions(cfg: dict) -> dict[str, str]:
    """Versions qairt_env.json claims, keyed by where they came from."""
    found: dict[str, str] = {}
    for key in _VERSION_KEYS:
        val = cfg.get(key)
        if isinstance(val, str) and val.strip():
            found[key] = val.strip()
    for key in _ROOT_KEYS:
        val = cfg.get(key)
        if isinstance(val, str) and val.strip():
            name = Path(val.strip().replace("\\", "/").rstrip("/")).name
            if _VERSION_RE.match(name):
                found[key] = name
    return found


def sync(cfg: dict, release: dict) -> list[str]:
    """Rewrite *cfg* in place when it disagrees with *release*. Returns the mismatches found."""
    expected = str(release["qairt_version"])
    mismatches = [f"{k}={v}" for k, v in recorded_versions(cfg).items() if v != expected]
    if not mismatches:
        return []

    sdk_root = str(release["qairt_sdk_root_template"]).format(version=expected)
    cfg["qairt_sdk_root"] = sdk_root
    if "qairt_root" in cfg:  # written by the QAIRT switch / install_qairt.py; keep both in step
        cfg["qairt_root"] = sdk_root
    cfg["_version"] = expected
    if "qairt_version" in cfg:
        cfg["qairt_version"] = expected
    url_t = release.get("qairt_download_url_template")
    if url_t:
        cfg["qairt_download_url"] = str(url_t).format(version=expected)
    # The new SDK was not extracted by the app (yet); install_builder_env.py sets this again
    # when it installs it. Leaving True would make the uninstaller treat the new path as ours.
    if "qairt_sdk_installed_by_app" in cfg:
        cfg["qairt_sdk_installed_by_app"] = False
    return mismatches


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("Usage: sync_qairt_env_version.py <qairt_env.json> <qairt_release.json>", file=sys.stderr)
        return 1
    cfg_path, rel_path = Path(argv[1]), Path(argv[2])

    if not cfg_path.is_file():
        return 0  # nothing generated yet; the --builder step will write it from the release
    release = _load_json(rel_path)
    if not release or not isinstance(release.get("qairt_version"), str) \
            or not isinstance(release.get("qairt_sdk_root_template"), str):
        print(f"[WARN] {rel_path} unreadable or incomplete; qairt_env.json version not checked.")
        return 0
    cfg = _load_json(cfg_path)
    if cfg is None:
        print(f"[WARN] {cfg_path} is not valid JSON; leaving it (the readiness check reports it).")
        return 0

    expected = release["qairt_version"]
    mismatches = sync(cfg, release)
    if not mismatches:
        print(f"[SKIP] qairt_env.json already on QAIRT {expected}.")
        return 0

    tmp = cfg_path.with_name(cfg_path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        os.replace(tmp, cfg_path)
    except OSError as exc:
        print(f"[WARN] could not update {cfg_path}: {exc}")
        tmp.unlink(missing_ok=True)
        return 0
    print(f"[OK]   qairt_env.json updated to QAIRT {expected} (was: {', '.join(mismatches)}); "
          f"qairt_sdk_root -> {cfg['qairt_sdk_root']}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
