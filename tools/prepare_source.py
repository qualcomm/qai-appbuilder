#!/usr/bin/env python3
# =============================================================================
# Copyright (c) 2026, Qualcomm Innovation Center, Inc. All rights reserved.
# SPDX-License-Identifier: BSD-3-Clause
# =============================================================================
"""
Prepare source dependencies for building QAI AppBuilder from a GitHub ZIP source.

Why this script exists
----------------------
`git clone --recursive` downloads Git submodules automatically. GitHub
"Download ZIP" does not include submodule contents, so source directories such as
`pybind/pybind11` may be empty or missing. This script downloads the source
dependencies needed by the wheel build.

Typical usage
-------------
    python prepare_source.py
    python -m build -w

By default, this script downloads only dependencies required for building the
Python wheel. Use `--all` if you also want to download all submodules listed in
.gitmodules, for example for building optional C++ samples.
"""

from __future__ import annotations

import argparse
import configparser
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent.parent
DEPS_LOCK = ROOT / "tools" / "deps.lock.json"
GITMODULES = ROOT / ".gitmodules"

# Minimal dependency required by setup.py / pybind CMake extension build.
# Keep this fallback so ZIP builds still have a useful path even if .gitmodules
# is unavailable in a generated source package.
FALLBACK_WHEEL_DEPS = [
    {
        "name": "pybind11",
        "path": "pybind/pybind11",
        "url": "https://github.com/pybind/pybind11.git",
        "commit": None,
        "required_for_wheel": True,
    },
]


@dataclass
class Dependency:
    name: str
    path: Path
    url: str
    commit: str | None = None
    required_for_wheel: bool = False


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+ " + " ".join(cmd))
    subprocess.check_call(cmd, cwd=str(cwd) if cwd else None)


def require_git() -> None:
    if shutil.which("git") is None:
        raise RuntimeError(
            "git executable not found in PATH. Please install Git first, or use "
            "`git clone --recursive` instead of GitHub Download ZIP."
        )


def load_deps_from_lock(lock_file: Path) -> list[Dependency]:
    data = json.loads(lock_file.read_text(encoding="utf-8"))
    deps = []
    for item in data.get("deps", []):
        deps.append(
            Dependency(
                name=item.get("name") or item["path"],
                path=ROOT / item["path"],
                url=item["url"],
                commit=item.get("commit"),
                required_for_wheel=bool(item.get("required_for_wheel", False)),
            )
        )
    return deps


def load_deps_from_gitmodules(gitmodules: Path) -> list[Dependency]:
    parser = configparser.ConfigParser()
    parser.read(gitmodules, encoding="utf-8")
    deps = []

    for section in parser.sections():
        if not section.startswith("submodule"):
            continue
        path = parser.get(section, "path", fallback=None)
        url = parser.get(section, "url", fallback=None)
        if not path or not url:
            continue
        name = section.split('"')[1] if '"' in section else path
        deps.append(
            Dependency(
                name=name,
                path=ROOT / path,
                url=url,
                commit=None,
                required_for_wheel=(path.replace("\\", "/") == "pybind/pybind11"),
            )
        )
    return deps


def load_dependencies() -> list[Dependency]:
    if DEPS_LOCK.exists():
        return load_deps_from_lock(DEPS_LOCK)
    if GITMODULES.exists():
        return load_deps_from_gitmodules(GITMODULES)
    return [
        Dependency(
            name=item["name"],
            path=ROOT / item["path"],
            url=item["url"],
            commit=item["commit"],
            required_for_wheel=item["required_for_wheel"],
        )
        for item in FALLBACK_WHEEL_DEPS
    ]


def select_dependencies(deps: Iterable[Dependency], include_all: bool) -> list[Dependency]:
    selected = list(deps if include_all else (d for d in deps if d.required_for_wheel))

    # If .gitmodules exists but does not mark wheel deps, still ensure pybind11 is included.
    if not selected:
        selected = [
            Dependency(
                name=item["name"],
                path=ROOT / item["path"],
                url=item["url"],
                commit=item["commit"],
                required_for_wheel=True,
            )
            for item in FALLBACK_WHEEL_DEPS
        ]

    return selected


def is_non_empty_dir(path: Path) -> bool:
    try:
        return path.is_dir() and any(path.iterdir())
    except (OSError, PermissionError):
        return False


def ensure_dependency(dep: Dependency, force: bool = False) -> None:
    rel_path = dep.path.relative_to(ROOT)

    if dep.path.exists() and force:
        print(f"[REMOVE] {rel_path}")
        shutil.rmtree(dep.path)

    if is_non_empty_dir(dep.path):
        print(f"[SKIP] {rel_path} already exists")
        if dep.commit:
            try:
                run(["git", "checkout", dep.commit], cwd=dep.path)
            except subprocess.CalledProcessError:
                print(f"[WARN] Could not checkout {dep.commit} in {rel_path}")
        return

    dep.path.parent.mkdir(parents=True, exist_ok=True)

    # Use a normal clone if a fixed commit is requested; shallow clone may not
    # contain arbitrary historical submodule commits.
    if dep.commit:
        run(["git", "clone", dep.url, str(dep.path)])
        run(["git", "checkout", dep.commit], cwd=dep.path)
    else:
        run(["git", "clone", "--depth", "1", dep.url, str(dep.path)])


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare QAI AppBuilder source dependencies.")
    parser.add_argument(
        "--all",
        action="store_true",
        help="Download all dependencies listed in .gitmodules or tools/deps.lock.json.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Remove and re-download selected dependencies if they already exist.",
    )
    args = parser.parse_args()

    require_git()
    deps = select_dependencies(load_dependencies(), include_all=args.all)

    print("Preparing source dependencies:")
    for dep in deps:
        print(f"  - {dep.name}: {dep.path.relative_to(ROOT)}")

    for dep in deps:
        ensure_dependency(dep, force=args.force)

    print("\nSource dependencies are ready.")
    print("Next step:")
    print("  python -m build -w")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
