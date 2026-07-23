# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""
audio_formats
=============

Single source of truth for the audio container/codec extensions the App Builder
audio decoder can handle. Kept dependency-free (no numpy / soundfile / ffmpeg
imports) so the FastAPI backend can load it via importlib without dragging in
runner-only deps.

Decode coverage (see audio_io.read_audio):
    * wav / flac / ogg / aiff -> direct via soundfile
    * webm / mp3 / m4a / mp4  -> transcode via ffmpeg.exe -> temp wav -> soundfile

audio_io.py re-exports SUPPORTED_AUDIO_FORMATS so existing runners that
``from audio_io import SUPPORTED_AUDIO_FORMATS`` keep working.
backend/app_builder/registry.py validates each audio Pack's
``inputSchema.constraints.formats`` against this list at manifest load time
(plan section S.7).
"""

from __future__ import annotations

SUPPORTED_AUDIO_FORMATS: tuple[str, ...] = (
    "wav", "flac", "ogg", "aiff", "webm", "mp3", "m4a", "mp4",
)
