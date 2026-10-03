# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure QAIRT SDK version-directory discovery.

The installed SDK root contains one child directory per release. Only a single
three- or four-part numeric version is a valid candidate (for example
``2.49.40.260810`` or ``2.48.40.260702``). Text prefixes/suffixes and ranges are
not SDK version directories and must never surface in selectors.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from qai.platform.qairt_switch.versioning import QairtVersion


def resolve_qairt_installation(
    env_file: Path | None,
    *,
    fallback_root: Path,
) -> tuple[Path, str | None]:
    """Resolve the QAIRT install parent and configured active version.

    Accepts both the current installer field (``qairt_root``) and the legacy
    setup field (``qairt_sdk_root``). Missing/malformed config returns the
    supplied fallback root with no active version.
    """

    if env_file is None:
        return fallback_root, None
    try:
        payload = json.loads(env_file.read_text(encoding="utf-8"))
        raw = payload.get("qairt_root") or payload.get("qairt_sdk_root")
        if not isinstance(raw, str) or not raw:
            return fallback_root, None
        configured = Path(os.path.expandvars(os.path.expanduser(raw)))
    except (OSError, ValueError, TypeError):
        return fallback_root, None
    return configured.parent, configured.name


def discover_qairt_versions(install_root: Path) -> list[str]:
    """Return strict QAIRT SDK version directory names, newest first.

    Missing, unreadable, or non-directory roots return an empty list. Sorting
    is numeric per dot-separated component, never lexicographic (``2.10``
    sorts above ``2.9``), by delegating both strict parsing and comparison to
    :class:`QairtVersion`.
    """

    try:
        children = [child for child in install_root.iterdir() if child.is_dir()]
    except (FileNotFoundError, NotADirectoryError, PermissionError, OSError):
        return []

    parsed: list[tuple[QairtVersion, str]] = []
    for child in children:
        try:
            parsed.append((QairtVersion.parse(child.name), child.name))
        except ValueError:
            continue

    parsed.sort(key=lambda pair: pair[0], reverse=True)
    return [name for _version, name in parsed]


__all__ = ["discover_qairt_versions", "resolve_qairt_installation"]
