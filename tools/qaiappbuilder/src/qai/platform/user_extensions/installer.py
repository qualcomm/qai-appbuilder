# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Installing an extension from a zip archive or a local folder (design doc §7.2).

Two steps, so the user can confirm what the extension brings before any of
its code runs:

1. :meth:`ExtensionInstaller.stage_zip` / :meth:`stage_folder` copy the
   extension into ``<scan root>/.staging/<token>/<name>/`` (the scanner skips
   ``.``-prefixed directories), validate its manifest and return a
   :class:`StagedExtension` -- the capability summary. Nothing is executed.
2. :meth:`ExtensionInstaller.commit` moves the staged copy to
   ``<scan root>/<name>/`` (replacing an installed one only when asked);
   the caller then loads it. :meth:`discard` throws a staged copy away.

Archives: the manifest may sit at the archive root or inside one top-level
directory. Rejected: absolute paths, ``..``, drive letters, symlinks, more
than :data:`MAX_FILES` entries or :data:`MAX_TOTAL_BYTES` uncompressed (the
real byte count is enforced while extracting, not just the declared size).

Stdlib only.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import stat
import time
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import IO, Any, Final

from .manifest import MANIFEST_FILENAME, ExtensionManifest, ManifestError, load_manifest

__all__ = [
    "MAX_FILES",
    "MAX_TOTAL_BYTES",
    "ExtensionInstaller",
    "InstallError",
    "StagedExtension",
]

MAX_FILES: Final[int] = 5000
MAX_TOTAL_BYTES: Final[int] = 200 * 1024 * 1024
STAGING_DIRNAME: Final[str] = ".staging"
_TRASH_DIRNAME: Final[str] = ".trash"
#: Staged copies older than this are removed on the next staging call.
STAGING_TTL_S: Final[float] = 3600.0
_TOKEN_LEN: Final[int] = 16


class InstallError(ValueError):
    """The archive / folder cannot be installed. ``code`` is machine-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class StagedExtension:
    """A validated, not yet installed extension: what it would add."""

    token: str
    manifest: ExtensionManifest
    #: Skill ids found under the skills directory (``<id>/SKILL.md``).
    skills: tuple[str, ...] = ()
    #: Version of the installed extension of the same name (``None``: new).
    installed_version: str | None = None
    #: Whether the installed one could be read at all.
    installed_valid: bool = True
    files: int = 0
    bytes: int = 0
    warnings: list[str] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        """Wire form for the confirmation dialog."""
        m = self.manifest
        return {
            "token": self.token,
            "name": m.name,
            "version": m.version,
            "description": m.description,
            "runs_code": m.entry is not None,
            "tools": [{"name": t.name, "group": t.group} for t in m.tools],
            "skills": list(self.skills),
            "prompts": [p.when for p in m.prompts],
            "settings": [{"key": s.key, "type": s.type, "label": s.label} for s in m.settings],
            "mcp_servers": [
                {
                    "name": f"{m.name}-{s.name}",
                    "transport": s.transport,
                    "command": s.command,
                    "url": s.url,
                }
                for s in m.mcp_servers
            ],
            "installed_version": self.installed_version,
            "replaces": self.installed_version is not None or not self.installed_valid,
            "files": self.files,
            "bytes": self.bytes,
            "warnings": list(self.warnings),
        }


class ExtensionInstaller:
    """Stages and commits extension installs under ``scan_root``."""

    def __init__(self, scan_root: Path, *, clock: Callable[[], float] = time.time) -> None:
        self._root = scan_root
        self._clock = clock

    @property
    def staging_root(self) -> Path:
        return self._root / STAGING_DIRNAME

    # -- stage ---------------------------------------------------------------

    def stage_zip(self, archive: IO[bytes] | Path) -> StagedExtension:
        """Extract ``archive`` into a fresh staging directory and validate it."""
        token, work = self._new_work_dir()
        raw = work / "raw"
        raw.mkdir()
        try:
            try:
                zf = zipfile.ZipFile(archive)
            except (zipfile.BadZipFile, OSError) as exc:
                raise InstallError("extensions.bad_archive", f"not a zip archive: {exc}") from None
            with zf:
                try:
                    files, total = _safe_extract(zf, raw)
                except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError) as exc:
                    # Corrupt member, forged sizes (CRC mismatch), unsupported
                    # compression / encryption.
                    raise InstallError(
                        "extensions.bad_archive", f"the archive is damaged or unsupported: {exc}"
                    ) from None
            return self._finish(token, work, _find_root(raw), files=files, total=total)
        except BaseException:
            _rmtree(work)
            raise

    def stage_folder(self, folder: Path) -> StagedExtension:
        """Copy a local extension folder into a fresh staging directory."""
        src = Path(folder)
        if not src.is_dir():
            raise InstallError("extensions.not_a_folder", f"{src} is not a folder")
        if not (src / MANIFEST_FILENAME).is_file():
            raise InstallError("extensions.no_manifest", f"{MANIFEST_FILENAME} not found in {src}")
        try:
            if src.resolve() == self._root.resolve() or src.resolve().is_relative_to(
                self._root.resolve()
            ):
                raise InstallError(
                    "extensions.already_in_place",
                    "this folder is already inside the extensions directory; use reload",
                )
        except OSError:
            pass
        token, work = self._new_work_dir()
        try:
            files, total = _copy_tree(src, work / "raw")
            return self._finish(token, work, work / "raw", files=files, total=total)
        except BaseException:
            _rmtree(work)
            raise

    def staged(self, token: str) -> StagedExtension:
        """Re-validate a staged copy (the confirmation step)."""
        work = self._work_dir(token)
        candidates = [p for p in work.iterdir() if p.is_dir() and p.name != "raw"]
        if len(candidates) != 1:
            raise InstallError("extensions.unknown_token", "this install was not staged")
        return self._describe(token, candidates[0])

    def discard(self, token: str) -> bool:
        work = self.staging_root / token
        if not _valid_token(token) or not work.is_dir():
            return False
        _rmtree(work)
        return True

    # -- commit --------------------------------------------------------------

    def commit(self, token: str, *, replace: bool = False) -> ExtensionManifest:
        """Move the staged copy into place. The caller unloaded any installed
        extension of the same name first, and loads the new one afterwards.

        ``extensions.exists`` when one is installed and ``replace`` is false.
        On failure the installed copy is restored.
        """
        staged = self.staged(token)
        name = staged.manifest.name
        target = self._root / name
        if (target.exists() or target.is_symlink()) and not replace:
            raise InstallError(
                "extensions.exists", f"extension {name!r} is already installed (replace it?)"
            )
        trash: Path | None = None
        if target.exists() or target.is_symlink():
            trash = self._root / _TRASH_DIRNAME / f"{name}-{token}"
            trash.parent.mkdir(parents=True, exist_ok=True)
            _retry(lambda: os.replace(target, trash))
        try:
            _retry(lambda: os.replace(staged.manifest.root, target))
        except BaseException:
            if trash is not None:
                os.replace(trash, target)
            raise
        _rmtree(self.staging_root / token)
        if trash is not None:
            _rmtree(trash)
        return load_manifest(target)

    # -- internals -----------------------------------------------------------

    def _finish(
        self, token: str, work: Path, root: Path, *, files: int, total: int
    ) -> StagedExtension:
        name = _manifest_name(root)
        final = work / name
        os.replace(root, final)
        _rmtree(work / "raw")
        staged = self._describe(token, final)
        return StagedExtension(
            token=staged.token,
            manifest=staged.manifest,
            skills=staged.skills,
            installed_version=staged.installed_version,
            installed_valid=staged.installed_valid,
            files=files,
            bytes=total,
            warnings=staged.warnings,
        )

    def _describe(self, token: str, ext_dir: Path) -> StagedExtension:
        try:
            manifest = load_manifest(ext_dir)
        except ManifestError as exc:
            raise InstallError("extensions.invalid_manifest", str(exc)) from None
        warnings: list[str] = []
        skills_dir = ext_dir / manifest.skills.dir
        skills = tuple(
            sorted(
                p.name
                for p in (skills_dir.iterdir() if skills_dir.is_dir() else ())
                if (p / "SKILL.md").is_file()
            )
        )
        if manifest.entry is not None and not (ext_dir / manifest.entry).exists():
            warnings.append(f"entry {manifest.entry!r} does not exist; loading will fail")
        installed = self._root / manifest.name
        installed_version: str | None = None
        installed_valid = True
        if installed.exists():
            try:
                installed_version = load_manifest(installed).version
            except ManifestError:
                installed_valid = False
        return StagedExtension(
            token=token,
            manifest=manifest,
            skills=skills,
            installed_version=installed_version,
            installed_valid=installed_valid,
            warnings=warnings,
        )

    def _new_work_dir(self) -> tuple[str, Path]:
        self._sweep()
        token = secrets.token_hex(_TOKEN_LEN // 2)
        work = self.staging_root / token
        work.mkdir(parents=True)
        return token, work

    def _work_dir(self, token: str) -> Path:
        work = self.staging_root / token
        if not _valid_token(token) or not work.is_dir():
            raise InstallError(
                "extensions.unknown_token", "this install was not staged (or expired)"
            )
        return work

    def _sweep(self) -> None:
        """Remove staged copies older than :data:`STAGING_TTL_S` and leftovers."""
        now = self._clock()
        for parent in (self.staging_root, self._root / _TRASH_DIRNAME):
            if not parent.is_dir():
                continue
            for entry in parent.iterdir():
                try:
                    old = now - entry.stat().st_mtime > STAGING_TTL_S
                except OSError:
                    continue
                if old or parent.name == _TRASH_DIRNAME:
                    _rmtree(entry, attempts=1, missing_ok=True)


# -- archive helpers ----------------------------------------------------------


def _safe_extract(zf: zipfile.ZipFile, dest: Path) -> tuple[int, int]:
    infos = zf.infolist()
    if len(infos) > MAX_FILES:
        raise InstallError("extensions.too_many_files", f"more than {MAX_FILES} entries")
    if sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
        raise InstallError("extensions.too_large", f"more than {MAX_TOTAL_BYTES} bytes unpacked")
    files = total = 0
    dest_resolved = dest.resolve()
    for info in infos:
        rel = _safe_member(info.filename)
        if rel is None:
            continue  # directory entry or ignorable metadata
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise InstallError("extensions.unsafe_archive", f"{info.filename!r} is a symbolic link")
        target = dest.joinpath(*rel.parts)
        if not target.resolve().is_relative_to(dest_resolved):
            raise InstallError(
                "extensions.unsafe_archive", f"{info.filename!r} escapes the archive"
            )
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, target.open("wb") as out:
            while chunk := src.read(1 << 16):
                total += len(chunk)
                if total > MAX_TOTAL_BYTES:
                    raise InstallError(
                        "extensions.too_large", f"more than {MAX_TOTAL_BYTES} bytes unpacked"
                    )
                out.write(chunk)
        files += 1
    return files, total


def _safe_member(name: str) -> PurePosixPath | None:
    """The member's relative path, ``None`` to skip it; raises if unsafe."""
    normalized = name.replace("\\", "/")
    if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":"):
        raise InstallError("extensions.unsafe_archive", f"{name!r} is an absolute path")
    parts = [p for p in normalized.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise InstallError("extensions.unsafe_archive", f"{name!r} escapes the archive")
    if not parts or parts[0] == "__MACOSX" or parts[-1] == ".DS_Store":
        return None
    return PurePosixPath(*parts)


def _find_root(raw: Path) -> Path:
    """The directory holding the manifest: ``raw`` or its single subdirectory."""
    if (raw / MANIFEST_FILENAME).is_file():
        return raw
    entries = list(raw.iterdir())
    if len(entries) == 1 and entries[0].is_dir() and (entries[0] / MANIFEST_FILENAME).is_file():
        return entries[0]
    raise InstallError(
        "extensions.no_manifest",
        f"{MANIFEST_FILENAME} not found at the archive root or in its single top-level folder",
    )


def _manifest_name(root: Path) -> str:
    """The manifest ``name`` (validated fully later by :func:`load_manifest`)."""
    try:
        raw = json.loads((root / MANIFEST_FILENAME).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise InstallError(
            "extensions.invalid_manifest", f"cannot read the manifest: {exc}"
        ) from None
    name = raw.get("name") if isinstance(raw, dict) else None
    if not isinstance(name, str) or not name or name.startswith(".") or "/" in name or "\\" in name:
        raise InstallError("extensions.invalid_manifest", f"manifest name {name!r} is not valid")
    if name in ("raw", STAGING_DIRNAME, _TRASH_DIRNAME) or name.startswith("_"):
        raise InstallError("extensions.invalid_manifest", f"manifest name {name!r} is reserved")
    return name


def _copy_tree(src: Path, dest: Path) -> tuple[int, int]:
    """Copy ``src`` (no symlinks followed; skips ``__pycache__`` / ``.git``)."""
    files = total = 0
    src_resolved = src.resolve()
    for dirpath, dirnames, filenames in os.walk(src):
        here = Path(dirpath)
        dirnames[:] = [
            d for d in dirnames if d not in ("__pycache__", ".git") and not (here / d).is_symlink()
        ]
        rel = here.relative_to(src)
        (dest / rel).mkdir(parents=True, exist_ok=True)
        for fname in filenames:
            path = here / fname
            if path.is_symlink() or not path.resolve().is_relative_to(src_resolved):
                raise InstallError("extensions.unsafe_folder", f"{path} is a link; copy it in")
            files += 1
            total += path.stat().st_size
            if files > MAX_FILES:
                raise InstallError("extensions.too_many_files", f"more than {MAX_FILES} files")
            if total > MAX_TOTAL_BYTES:
                raise InstallError("extensions.too_large", f"more than {MAX_TOTAL_BYTES} bytes")
            shutil.copy2(path, dest / rel / fname)
    return files, total


def _valid_token(token: str) -> bool:
    return len(token) == _TOKEN_LEN and all(c in "0123456789abcdef" for c in token)


def _retry(op: Callable[[], object], attempts: int = 20) -> None:
    for attempt in range(attempts):
        try:
            op()
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.25)
        else:
            return


def _rmtree(path: Path, *, attempts: int = 20, missing_ok: bool = True) -> None:
    for attempt in range(attempts):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            if missing_ok:
                return
            raise
        except PermissionError:
            if attempt == attempts - 1:
                return  # best-effort: swept again next time
            time.sleep(0.25)
        else:
            return
