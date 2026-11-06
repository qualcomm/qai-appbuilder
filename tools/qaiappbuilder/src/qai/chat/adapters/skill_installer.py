# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Install a user's skill (a folder or a ``.zip``) into the app's ``skills/`` directory.

A skill is a directory whose name is the skill id and which holds a ``SKILL.md``
(or ``skill.json``); ``SkillDiscovery`` scans ``skills/`` live, so installing is a
safe, atomic copy -- nothing is executed and nothing is pip-installed.

What this module guards against (all of it is what a hand-rolled ``exec`` + ``unzip``
gets wrong):

* zip path traversal (``../``, absolute paths, drive letters, alternate data streams,
  reserved Windows device names), symlinks, and zip bombs (declared AND actual sizes);
* a user skill silently shadowing a BUILT-IN one (``skills/`` wins over
  ``factory/chat_features/`` in discovery);
* half-installed skills: the new tree is built in a staging directory that discovery
  ignores, verified with the real discovery code, and only then moved into place; an
  upgrade (``replace``) restores the previous version if anything fails.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import sys
import time
import uuid
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from qai.chat.adapters.mcp_wheel_installer import _remove_tree
from qai.platform.logging import get_logger
from qai.platform.skills.discovery import SkillDiscovery, parse_skill_metadata

__all__ = ["SkillInstallResult", "install_skill"]

_log = get_logger(__name__)

#: Same rule as MCP server names (``domain/mcp_server.py``).
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")

MAX_TOTAL_BYTES = 50 * 1024 * 1024
MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_FILES = 2000

#: Never copied: build/VCS noise that is not part of a skill.
_SKIP_PARTS = frozenset({"__pycache__", ".git", "__MACOSX"})
_SKIP_SUFFIXES = (".pyc", ".pyo")

#: Files an agent may later RUN -- listed in the result so the user knows what they
#: are trusting.
_RUNNABLE_SUFFIXES = frozenset(
    {".py", ".ps1", ".sh", ".bat", ".cmd", ".js", ".mjs", ".vbs", ".exe", ".dll", ".msi"}
)
_MAX_LISTED_SCRIPTS = 20

_WIN_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
)


@dataclass
class SkillInstallResult:
    ok: bool
    step: str = ""
    error: str = ""
    skill_id: str = ""
    path: str = ""
    files: int = 0
    size: int = 0
    name: str = ""
    description: str = ""
    tags: tuple[str, ...] = ()
    #: Relative paths of files an agent could later run (capped).
    scripts: tuple[str, ...] = ()
    scripts_total: int = 0
    pinned: bool = False
    replaced: bool = False
    notes: list[str] = field(default_factory=list)


class _Rejected(Exception):
    """A validation failure carrying the step it belongs to."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step = step
        self.message = message


def _lp(path: Path | str) -> str:
    """Extended-length form of ``path`` on Windows (lifts the 260-char limit)."""
    s = str(path)
    if sys.platform != "win32" or s.startswith("\\\\?\\"):
        return s
    s = os.path.abspath(s)
    if s.startswith("\\\\"):  # UNC
        return "\\\\?\\UNC\\" + s[2:]
    return "\\\\?\\" + s


def _replace(src: Path, dst: Path) -> None:
    """``os.replace`` for directories, retrying the transient Windows "access denied".

    Right after files are written, a virus scanner or the search indexer may hold a handle
    inside the new tree for a moment; renaming the directory then fails with
    ``PermissionError`` (WinError 5) although nothing is wrong. A few short retries ride it out.
    """
    for attempt in range(6):
        try:
            os.replace(_lp(src), _lp(dst))
            return
        except PermissionError:
            if attempt == 5:
                raise
            time.sleep(0.15 * (attempt + 1))


def _clean_source(raw: object) -> Path:
    text = str(raw or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return Path(text).expanduser()


def _safe_member(name: str) -> tuple[str, ...]:
    """Split a zip member name into safe path parts or raise ``_Rejected``."""
    norm = name.replace("\\", "/")
    if norm.startswith("/") or re.match(r"^[A-Za-z]:", norm):
        raise _Rejected("unsafe_archive", f"absolute path in archive: {name!r}")
    parts = tuple(p for p in norm.split("/") if p not in ("", "."))
    for p in parts:
        if p == ".." or ":" in p or "\x00" in p:
            raise _Rejected("unsafe_archive", f"unsafe path in archive: {name!r}")
        if p.rstrip(". ").split(".")[0].lower() in _WIN_RESERVED:
            raise _Rejected("unsafe_archive", f"reserved device name in archive: {name!r}")
    return parts


def _skipped(parts: tuple[str, ...]) -> bool:
    return any(p in _SKIP_PARTS for p in parts) or parts[-1].lower().endswith(_SKIP_SUFFIXES)


class _Budget:
    def __init__(self) -> None:
        self.files = 0
        self.size = 0

    def add(self, size: int, label: str) -> None:
        self.files += 1
        self.size += size
        if self.files > MAX_FILES:
            raise _Rejected("too_large", f"more than {MAX_FILES} files")
        if size > MAX_FILE_BYTES:
            raise _Rejected("too_large", f"{label} is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
        if self.size > MAX_TOTAL_BYTES:
            raise _Rejected("too_large", f"more than {MAX_TOTAL_BYTES // (1024 * 1024)} MB in total")


def _stage_folder(src: Path, payload: Path, budget: _Budget) -> None:
    if not (src / "SKILL.md").is_file() and not (src / "skill.json").is_file():
        raise _Rejected("no_skill_md", f"{src} has no SKILL.md (or skill.json) at its top level")
    for dirpath, dirnames, filenames in os.walk(_lp(src), followlinks=False):
        rel_dir = Path(dirpath[len(_lp(src)):].lstrip("\\/"))
        dirnames[:] = [d for d in dirnames if d not in _SKIP_PARTS and not os.path.islink(os.path.join(dirpath, d))]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            parts = (*rel_dir.parts, fn)
            if _skipped(parts) or os.path.islink(full):
                continue
            size = os.path.getsize(full)
            budget.add(size, "/".join(parts))
            dest = payload.joinpath(*parts)
            os.makedirs(_lp(dest.parent), exist_ok=True)
            shutil.copyfile(full, _lp(dest))
    if not any(True for _ in os.scandir(_lp(payload))):
        raise _Rejected("no_skill_md", "nothing to install (the folder is empty)")


def _stage_zip(src: Path, payload: Path, budget: _Budget) -> None:
    try:
        zf = zipfile.ZipFile(_lp(src))
    except (zipfile.BadZipFile, OSError) as exc:
        raise _Rejected("read_source", f"cannot open {src.name} as a zip: {exc}") from exc
    with zf:
        members: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        for info in zf.infolist():
            parts = _safe_member(info.filename)
            if not parts:
                continue
            if (info.external_attr >> 16) & 0o170000 == stat.S_IFLNK:
                raise _Rejected("unsafe_archive", f"symbolic link in archive: {info.filename!r}")
            if info.is_dir():
                continue
            members.append((info, parts))
        if not members:
            raise _Rejected("no_skill_md", "the archive is empty")
        tops = {parts[0] for _, parts in members if parts[0] != "__MACOSX"}
        has_root_skill = any(len(p) == 1 and p[0] in ("SKILL.md", "skill.json") for _, p in members)
        # One wrapping folder ("myskill/SKILL.md") is the usual way people zip a folder.
        strip = 1 if (not has_root_skill and len(tops) == 1) else 0
        found = False
        for info, parts in members:
            rel = parts[strip:]
            if not rel or _skipped(parts):
                continue
            if len(rel) == 1 and rel[0] in ("SKILL.md", "skill.json"):
                found = True
            budget.add(info.file_size, "/".join(rel))
            dest = payload.joinpath(*rel)
            os.makedirs(_lp(dest.parent), exist_ok=True)
            written = 0
            try:
                with zf.open(info) as r, open(_lp(dest), "wb") as w:
                    while chunk := r.read(1024 * 1024):
                        written += len(chunk)
                        if written > info.file_size or written > MAX_FILE_BYTES:
                            raise _Rejected(
                                "unsafe_archive", f"{info.filename!r} is larger than the archive declares"
                            )
                        w.write(chunk)
            except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
                raise _Rejected("unsafe_archive", f"corrupt archive member {info.filename!r}: {exc}") from exc
        if not found:
            raise _Rejected("no_skill_md", "the archive has no SKILL.md (or skill.json) at its top level")


def _pick_id(explicit: str | None, payload: Path, src: Path) -> str:
    if explicit is not None and explicit.strip():
        candidate = explicit.strip()
        if not _ID_RE.match(candidate):
            raise _Rejected(
                "validate",
                f"invalid skill id {candidate!r}: use letters, digits, '_', '.', '-' "
                "and start with a letter or digit",
            )
        return candidate
    md = payload / "SKILL.md"
    meta_name = ""
    if md.is_file():
        parsed = parse_skill_metadata(md)
        meta_name = parsed[0] if parsed else ""
    for candidate in (meta_name, src.stem if src.is_file() else src.name):
        if candidate and _ID_RE.match(candidate):
            return candidate
    raise _Rejected(
        "validate",
        "cannot derive a skill id (the SKILL.md name and the source name contain characters "
        "outside letters, digits, '_', '.', '-'); pass 'name' explicitly",
    )


def _builtin_ids(builtin_root: Path | None) -> set[str]:
    if builtin_root is None or not builtin_root.is_dir():
        return set()
    return {p.name.lower() for p in builtin_root.iterdir() if p.is_dir()}


def _summarise(res: SkillInstallResult, final: Path) -> None:
    runnable: list[str] = []
    for p in sorted(final.rglob("*")):
        if p.is_file() and p.suffix.lower() in _RUNNABLE_SUFFIXES:
            runnable.append(p.relative_to(final).as_posix())
    res.scripts_total = len(runnable)
    res.scripts = tuple(runnable[:_MAX_LISTED_SCRIPTS])


def install_skill(
    source: str | Path,
    *,
    skills_root: Path,
    builtin_root: Path | None = None,
    name: str | None = None,
    replace: bool = False,
) -> SkillInstallResult:
    """Install ``source`` (a skill folder or ``.zip``) under ``skills_root``. Never raises."""
    skills_root = Path(skills_root)
    stage = skills_root / f".installing-{uuid.uuid4().hex[:10]}"
    old_backup: Path | None = None
    final: Path | None = None
    try:
        src = _clean_source(source)
        if not str(src).strip(" ."):
            raise _Rejected("read_source", "source is empty")
        is_zip = src.suffix.lower() == ".zip"
        if is_zip and not os.path.isfile(_lp(src)):
            raise _Rejected("read_source", f"zip not found: {src}")
        if not is_zip and not os.path.isdir(_lp(src)):
            raise _Rejected(
                "read_source",
                f"not a folder or .zip file: {src}" if os.path.exists(_lp(src)) else f"source not found: {src}",
            )

        payload = stage / "payload"
        os.makedirs(_lp(payload), exist_ok=True)
        budget = _Budget()
        (_stage_zip if is_zip else _stage_folder)(src, payload, budget)

        skill_id = _pick_id(name, payload, src)
        if skill_id.lower() in _builtin_ids(builtin_root):
            raise _Rejected(
                "validate",
                f"'{skill_id}' is the id of a built-in skill; a user skill with that id would "
                "silently replace it. Choose another id (pass 'name').",
            )
        final = skills_root / skill_id
        exists = os.path.exists(_lp(final))
        if exists and not replace:
            raise _Rejected(
                "exists",
                f"a skill '{skill_id}' is already installed. If this is an upgrade, call again "
                "with replace=true; otherwise choose another id (pass 'name').",
            )
        if exists and not os.path.isdir(_lp(final)):
            raise _Rejected("validate", f"{final} exists and is not a directory")

        res = SkillInstallResult(ok=False, skill_id=skill_id, path=str(final), files=budget.files, size=budget.size)
        if exists:
            old_backup = skills_root / f".old-{skill_id}-{uuid.uuid4().hex[:6]}"
            _replace(final, old_backup)
            res.replaced = True
        try:
            _replace(payload, final)
        except OSError:
            if old_backup is not None:
                _replace(old_backup, final)
                old_backup = None
            raise

        found = SkillDiscovery([skills_root]).find(skill_id)
        if found is None:
            # Roll back: the skill must be usable or not installed at all.
            _remove_tree(final)
            if old_backup is not None:
                _replace(old_backup, final)
                old_backup = None
            raise _Rejected(
                "not_discovered",
                "the skill was copied but the app's skill discovery does not see it "
                "(check that SKILL.md starts with a valid '---' front-matter block); nothing was installed",
            )
        res.name = found.name
        res.description = found.description
        res.tags = tuple(t for t in found.tags if t)
        res.pinned = found.pinned
        _summarise(res, final)
        res.ok = True
        if old_backup is not None:
            try:
                _remove_tree(old_backup)
            except OSError as exc:  # leftover backup is harmless
                res.notes.append(f"could not delete the previous version's backup {old_backup.name}: {exc}")
            old_backup = None
        _log.info("chat.skill.installed", skill=skill_id, files=res.files, replaced=res.replaced)
        return res
    except _Rejected as exc:
        return SkillInstallResult(ok=False, step=exc.step, error=exc.message)
    except Exception as exc:  # noqa: BLE001 -- contract: never raise
        _log.warning("chat.skill.install_unexpected", error=str(exc))
        if old_backup is not None and final is not None:
            try:
                if os.path.exists(_lp(final)):
                    _remove_tree(final)
                _replace(old_backup, final)
            except OSError:
                pass
        return SkillInstallResult(ok=False, step="unexpected", error=f"{type(exc).__name__}: {exc}")
    finally:
        if os.path.exists(_lp(stage)):
            try:
                _remove_tree(stage)
            except OSError:
                pass
