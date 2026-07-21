# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``Artifact`` value object — describes a single file produced by a Run.

Artifacts are produced by adapters (the runner / output writer) and
attached to the owning :class:`Run` aggregate via
:meth:`Run.attach_artifact`. The domain layer never opens or writes the
file itself — adapters use the ``ArtifactStorePort`` to materialise the
bytes, then construct an :class:`Artifact` describing the result.

Path discipline (HANDOFF §4.7):

* The :attr:`path` field must be a *relative* logical path expressed as
  a forward-slash-joined string (``"audio/2026-05-29/run-XXX/out.wav"``).
* Adapters resolve it against ``qai.platform.config.DataPaths`` to
  obtain a concrete filesystem path. The domain VO refuses absolute
  paths, ``..`` segments and Windows drive prefixes so we never leak
  legacy hardcoded ``data/...`` literals into use cases or tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from qai.app_builder.domain.value_objects import Hash256

__all__ = ["ArtifactKind", "Artifact"]


class ArtifactKind(str, Enum):
    """Coarse classification of an artifact for routing/display.

    The legacy backend distinguishes audio / image / text outputs in
    several places (see ``05-data-config.md`` line 105 and
    ``backend/tools/_appbuilder_run.py``); we keep the same four buckets
    plus a ``BINARY`` catch-all.
    """

    AUDIO = "audio"
    IMAGE = "image"
    TEXT = "text"
    BINARY = "binary"


def _validate_relative_path(path: str) -> None:
    if not isinstance(path, str):
        raise ValueError(
            f"Artifact.path must be str, got {type(path).__name__}"
        )
    if not path:
        raise ValueError("Artifact.path must not be empty")
    if path.startswith("/") or path.startswith("\\"):
        raise ValueError(
            f"Artifact.path must be relative, got {path!r}"
        )
    # Reject Windows drive prefixes (e.g. "C:") and UNC roots.
    if len(path) >= 2 and path[1] == ":":
        raise ValueError(
            f"Artifact.path must not start with a drive letter, got {path!r}"
        )
    if "\\" in path:
        raise ValueError(
            "Artifact.path must use forward slashes, "
            f"got backslash in {path!r}"
        )
    parts = path.split("/")
    for part in parts:
        if part in ("", ".", ".."):
            raise ValueError(
                "Artifact.path must not contain empty / '.' / '..' segments, "
                f"got {path!r}"
            )
    # Refuse any reference to legacy raw data root (HANDOFF §4.7).
    if parts[0] in {"data", "config"}:
        raise ValueError(
            "Artifact.path must not start with legacy 'data/' or 'config/' "
            f"root, got {path!r}"
        )


@dataclass(frozen=True, slots=True, kw_only=True)
class Artifact:
    """A file produced by a :class:`Run`.

    * :attr:`path` — relative logical path; see module docstring.
    * :attr:`size_bytes` — non-negative byte count (0 is allowed for
      empty placeholder files, e.g. a touched marker).
    * :attr:`kind` — :class:`ArtifactKind` classification.
    * :attr:`checksum` — optional SHA-256 of the bytes (``None`` if the
      adapter chose not to compute it; we don't make it mandatory
      because some streaming runners can't afford to checksum).
    """

    path: str
    size_bytes: int
    kind: ArtifactKind
    checksum: Hash256 | None = None

    def __post_init__(self) -> None:
        _validate_relative_path(self.path)
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool):
            raise ValueError(
                "Artifact.size_bytes must be an int, "
                f"got {type(self.size_bytes).__name__}"
            )
        if self.size_bytes < 0:
            raise ValueError(
                f"Artifact.size_bytes must be >= 0, got {self.size_bytes}"
            )
        if not isinstance(self.kind, ArtifactKind):
            raise ValueError(
                "Artifact.kind must be an ArtifactKind, "
                f"got {type(self.kind).__name__}"
            )
        if self.checksum is not None and not isinstance(self.checksum, Hash256):
            raise ValueError(
                "Artifact.checksum must be a Hash256 or None, "
                f"got {type(self.checksum).__name__}"
            )
