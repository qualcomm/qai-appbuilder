# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Dataset archive extraction (zip / tar) with zip-slip protection.

V1 parity (``backend/main.py`` L6160-6196): a dataset upload that is a
``.zip`` / ``.tar(.gz/.bz2/.xz)`` archive is **extracted** into the
dataset directory rather than stored as an opaque blob, so the user sees
the individual dataset files (and the chat / model-builder flows can read
them). Single (non-archive) files are stored as-is.

This module is pure stdlib (``zipfile`` / ``tarfile``) so it stays in the
platform shared kernel without pulling in any framework dependency. It
performs the extraction **in memory** and returns the member name + bytes
for each safe entry; the caller (``UploadDatasetUseCase``) persists each
member through the :class:`UploadStorePort`, reusing the store's index /
list / delete machinery instead of inventing a parallel on-disk layout.

Security — zip-slip / tar path traversal
-----------------------------------------
Every archive member is validated before extraction (V1
``main.py`` L6172-6173 / L6180-6181 rejected ``..`` and absolute paths):

* absolute paths (``/etc/passwd`` / ``C:\\...``) are rejected;
* any member whose normalised path escapes the extraction root (``..``
  traversal) is rejected;
* symlink / device / hardlink tar members are rejected (they can point
  outside the root even with a benign name);
* directory entries are skipped (the store recreates structure from the
  member name).

Rejection raises :class:`DatasetExtractionError` (mapped to HTTP 400 by
the route, matching the V1 ``status_code=400`` contract).
"""

from __future__ import annotations

import io
import posixpath
import tarfile
import zipfile

from qai.platform.uploads.errors import UploadPolicyError

# Archive extensions that trigger extraction (V1 parity: zip + tar family).
_ZIP_SUFFIXES: tuple[str, ...] = (".zip",)
_TAR_SUFFIXES: tuple[str, ...] = (
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)


class DatasetExtractionError(UploadPolicyError):
    """Raised when a dataset archive is malformed or contains an unsafe path.

    Maps to HTTP 400 (V1 ``main.py`` L6173/L6184 raised 400 for illegal
    paths / corrupt archives).
    """


def _lower(filename: str) -> str:
    return (filename or "").lower()


def is_zip_filename(filename: str) -> bool:
    """Return ``True`` when *filename* names a ``.zip`` archive."""
    return _lower(filename).endswith(_ZIP_SUFFIXES)


def is_tar_filename(filename: str) -> bool:
    """Return ``True`` when *filename* names a tar-family archive."""
    return _lower(filename).endswith(_TAR_SUFFIXES)


def is_archive_filename(filename: str) -> bool:
    """Return ``True`` when *filename* is a zip/tar archive to be extracted."""
    return is_zip_filename(filename) or is_tar_filename(filename)


def _is_unsafe_member_name(name: str) -> bool:
    """Return ``True`` when *name* would escape the extraction root.

    Mirrors the V1 guard (reject ``..`` / leading ``/``) but normalises
    first so disguised traversals (``a/../../b``) are also caught.
    """
    if not name:
        return True
    # Normalise backslashes (zip entries may use either separator).
    candidate = name.replace("\\", "/")
    # Absolute path (POSIX ``/...`` or Windows drive ``C:/...``).
    if candidate.startswith("/") or (
        len(candidate) >= 2 and candidate[1] == ":"
    ):
        return True
    normalised = posixpath.normpath(candidate)
    # ``normpath`` collapses ``a/../b`` → ``b`` but leaves an escaping
    # ``../x`` as ``../x`` (or ``..``). Any leading ``..`` escapes the root.
    if normalised == ".." or normalised.startswith("../"):
        return True
    if normalised.startswith("/"):  # absolute after normalisation
        return True
    return False


def extract_archive(
    *, content: bytes, filename: str
) -> list[tuple[str, bytes]]:
    """Extract *content* (a zip/tar archive) into ``(name, bytes)`` members.

    Returns one ``(relative_path, file_bytes)`` tuple per regular file in
    the archive (directories are skipped — the store rebuilds structure
    from the member path). Raises :class:`DatasetExtractionError` for a
    corrupt archive or any member with an unsafe (traversal / absolute /
    symlink) path.

    Extraction is performed entirely in memory; nothing is written to
    disk here — persistence is the caller's responsibility (so the
    store's index / list / delete stay the single source of truth).
    """
    if is_zip_filename(filename):
        return _extract_zip(content)
    if is_tar_filename(filename):
        return _extract_tar(content)
    raise DatasetExtractionError(
        f"'{filename}' is not a supported dataset archive (.zip / .tar*)."
    )


def _extract_zip(content: bytes) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            for info in zf.infolist():
                name = info.filename
                if _is_unsafe_member_name(name):
                    raise DatasetExtractionError(
                        f"压缩包含非法路径：{name}"
                    )
                if info.is_dir():
                    continue
                members.append((name, zf.read(info)))
    except zipfile.BadZipFile as exc:
        raise DatasetExtractionError(f"解压失败：{exc}") from exc
    return members


def _extract_tar(content: bytes) -> list[tuple[str, bytes]]:
    members: list[tuple[str, bytes]] = []
    try:
        with tarfile.open(fileobj=io.BytesIO(content), mode="r:*") as tf:
            for member in tf.getmembers():
                name = member.name
                if _is_unsafe_member_name(name):
                    raise DatasetExtractionError(
                        f"压缩包含非法路径：{name}"
                    )
                # Reject symlinks / hardlinks / devices — even a benign
                # name can redirect a link target outside the root.
                if not (member.isfile() or member.isdir()):
                    raise DatasetExtractionError(
                        f"压缩包含不支持的条目类型：{name}"
                    )
                if member.isdir():
                    continue
                extracted = tf.extractfile(member)
                data = extracted.read() if extracted is not None else b""
                members.append((name, data))
    except tarfile.TarError as exc:
        raise DatasetExtractionError(f"解压失败：{exc}") from exc
    return members


__all__ = [
    "DatasetExtractionError",
    "extract_archive",
    "is_archive_filename",
    "is_tar_filename",
    "is_zip_filename",
]
