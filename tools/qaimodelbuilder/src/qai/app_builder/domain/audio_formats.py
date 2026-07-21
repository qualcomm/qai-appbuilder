# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Supported audio container/codec extensions (PR-306 domain extraction).

Promoted from ``features/app-builder/shared/audio_formats.py`` to a
proper domain constant so the validator + decoder + manifest loader
can share one source of truth without depending on the legacy
``features/`` runtime path.

Decode coverage (mirrors ``audio_io.read_audio`` semantics):
    * wav / flac / ogg / aiff -> direct via soundfile
    * webm / mp3 / m4a / mp4  -> transcode via ffmpeg.exe -> temp wav -> soundfile

Used by :class:`PackInputSchema.formats` validators (PR-303) and by
the manifest registry to verify that every audio Pack's
``inputSchema.constraints.formats`` is a subset of this set.

Kept dependency-free (no numpy / soundfile / ffmpeg imports) so the
FastAPI backend can load it without dragging in runner-only deps.

The legacy ``features/app-builder/shared/audio_formats.py`` file is
preserved verbatim as a runtime alias until the I2 cutover lane drops
the ``features/`` tree (PR-1104). New code MUST import from here:

    from qai.app_builder.domain.audio_formats import SUPPORTED_AUDIO_FORMATS
"""

from __future__ import annotations

__all__ = ["SUPPORTED_AUDIO_FORMATS", "is_supported_audio_format"]


SUPPORTED_AUDIO_FORMATS: tuple[str, ...] = (
    "wav",
    "flac",
    "ogg",
    "aiff",
    "webm",
    "mp3",
    "m4a",
    "mp4",
)
"""Whitelist of audio file extensions the App Builder pipeline supports.

Lower-case, no leading dot. Extending this list MUST be paired with
the corresponding decoder support in ``shared/audio_io.py``
(post-PR-306 path: ``factory/app_builder/shared/audio_io.py``).
"""


def is_supported_audio_format(extension: str) -> bool:
    """Return ``True`` iff ``extension`` is in :data:`SUPPORTED_AUDIO_FORMATS`.

    Tolerant lookup: leading dot is stripped, comparison is
    case-insensitive. Returns ``False`` for empty / non-string input.
    """
    if not isinstance(extension, str) or not extension:
        return False
    cleaned = extension.lstrip(".").lower()
    return cleaned in SUPPORTED_AUDIO_FORMATS
