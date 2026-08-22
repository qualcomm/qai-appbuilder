# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Filesystem-backed :class:`AudioUploadPort` (PR-045).

Persists user-uploaded audio bytes under
``DataPaths.upload_dir("audio", on_date=<today>)`` and returns an
:class:`Artifact` describing the resulting file.

Unlike runner-produced artifacts (which live under
``data/blobs/app_builder/<run_id>/``), uploads have a date-partitioned
top-level so multiple runs can reference the same recording without
collision and the directory tree stays manageable for retention sweeps.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from qai.platform.crypto.hashes import Hash256
from qai.platform.errors import PersistenceError
from qai.platform.io_validator import assert_safe_filename
from qai.platform.time import Clock

from qai.app_builder.domain.artifact import Artifact, ArtifactKind

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.config import DataPaths

__all__ = ["FileSystemAudioUpload"]

_log = logging.getLogger(__name__)


class FileSystemAudioUpload:
    """Filesystem implementation of :class:`AudioUploadPort`.

    Filename collisions are resolved by prefixing the timestamp from the
    injected :class:`Clock`; the original filename is preserved as the
    suffix so users see something recognisable in the artifact path.
    """

    __slots__ = ("_data_paths", "_clock")

    def __init__(
        self, *, data_paths: "DataPaths", clock: Clock
    ) -> None:
        self._data_paths = data_paths
        self._clock = clock

    async def save(
        self,
        *,
        filename: str,
        data: bytes,
        content_type: str,
    ) -> Artifact:
        # Re-validate (use cases already do this; the adapter is
        # defensive in case it gets called from a different path).
        assert_safe_filename(filename)
        del content_type  # not needed at this layer; metadata kept in audit
        now = self._clock.now()
        # Always use UTC date for the partition to avoid TZ skew.
        on_date = now.astimezone(timezone.utc).date()
        upload_root = self._data_paths.upload_dir("audio", on_date=on_date)
        try:
            self._data_paths.ensure(upload_root)
        except (OSError, ValueError) as exc:
            raise PersistenceError(
                "app_builder.audio_upload.mkdir_failed",
                f"failed to create upload directory: {exc}",
                operation="audio_upload.save",
                cause=exc,
            ) from exc

        ts_prefix = _format_timestamp(now)
        target_name = f"{ts_prefix}_{filename}"
        target = upload_root / target_name

        try:
            target.write_bytes(data)
        except OSError as exc:
            raise PersistenceError(
                "app_builder.audio_upload.write_failed",
                f"failed to write upload {target_name!r}: {exc}",
                operation="audio_upload.save",
                cause=exc,
            ) from exc

        _log.info("voice_audio_upload: path=%s size=%d", target, len(data))

        digest = hashlib.sha256(data).hexdigest()
        # The artifact's relative_path is the user-visible logical key:
        # uploads/audio/<yyyy-mm-dd>/<ts>_<filename>. This matches what
        # callers will reference when they pass it as a Run input.
        relative_path = (
            f"uploads/audio/{on_date.isoformat()}/{target_name}"
        )
        return Artifact(
            path=relative_path,
            size_bytes=len(data),
            kind=ArtifactKind.AUDIO,
            checksum=Hash256(value=digest),
        )


def _format_timestamp(now: datetime) -> str:
    """Compact UTC timestamp: ``YYYYMMDDTHHMMSSZ``."""
    return now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
