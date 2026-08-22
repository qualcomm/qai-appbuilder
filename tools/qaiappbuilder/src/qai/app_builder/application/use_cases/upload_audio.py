# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``UploadAudioUseCase`` — accept a user audio upload.

Returns the resulting :class:`Artifact` (caller will typically include
its relative path in the next ``/api/appbuilder/run`` request as part
of the ``inputs`` payload).

Format gate (V1 parity)
-----------------------
V1 rejected unsupported audio extensions at the upload boundary with
HTTP 415 (``backend/app_builder/api_routes.py:794`` —
``ext not in _ALLOWED_AUDIO_EXTS``). The V2 use case enforces the same
gate against the domain whitelist
:data:`qai.app_builder.domain.audio_formats.SUPPORTED_AUDIO_FORMATS`
(the single source of truth shared with the decoder) so we never persist
a blob the pipeline cannot decode. The route layer maps the raised
:class:`ValueError` onto its validation error envelope.
"""

from __future__ import annotations

from pathlib import PurePosixPath

from qai.app_builder.application.ports import AudioUploadPort
from qai.app_builder.domain.artifact import Artifact
from qai.app_builder.domain.audio_formats import (
    SUPPORTED_AUDIO_FORMATS,
    is_supported_audio_format,
)
from qai.platform.io_validator import (
    assert_max_length,
    assert_non_empty,
    assert_safe_filename,
)

__all__ = ["UploadAudioUseCase"]

# Reasonable cap to keep memory usage predictable; matches legacy 100 MB
# limit found in ``backend/main.py`` upload handlers.
_MAX_UPLOAD_BYTES: int = 100 * 1024 * 1024


class UploadAudioUseCase:
    """Validate and persist a user-uploaded audio file."""

    def __init__(self, *, uploads: AudioUploadPort) -> None:
        self._uploads = uploads

    async def execute(
        self,
        *,
        filename: str,
        data: bytes,
        content_type: str,
    ) -> Artifact:
        assert_non_empty(filename, name="filename")
        assert_max_length(filename, max_length=255, name="filename")
        assert_safe_filename(filename)
        # V1 parity: reject unsupported audio extensions at the upload
        # boundary (V1 raised HTTP 415). The whitelist is the domain's
        # single source of truth shared with the decoder.
        suffix = PurePosixPath(filename).suffix
        if not is_supported_audio_format(suffix):
            raise ValueError(
                f"unsupported audio extension {suffix!r}; "
                f"allowed: {sorted(SUPPORTED_AUDIO_FORMATS)}"
            )
        assert_non_empty(content_type, name="content_type")
        if not isinstance(data, (bytes, bytearray)):
            raise ValueError(
                f"data must be bytes, got {type(data).__name__}"
            )
        if not data:
            raise ValueError("data must be non-empty")
        if len(data) > _MAX_UPLOAD_BYTES:
            raise ValueError(
                f"data exceeds {_MAX_UPLOAD_BYTES} bytes "
                f"(got {len(data)})"
            )
        return await self._uploads.save(
            filename=filename,
            data=bytes(data),
            content_type=content_type,
        )
