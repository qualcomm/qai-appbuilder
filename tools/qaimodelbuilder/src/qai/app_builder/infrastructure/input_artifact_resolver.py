# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Resolve logical upload/artifact input paths to absolute physical paths.

Why this exists
===============
The App Builder file-input components upload a file and then pass the
returned **logical** path back as the run input:

* audio upload (``POST /upload/audio``) returns
  :attr:`Artifact.path` = ``uploads/audio/<date>/<file>`` (a logical key,
  see :mod:`qai.app_builder.infrastructure.audio_upload`);
* the physical file lives under the data blob root,
  ``<data>/blobs/uploads/audio/<date>/<file>``.

The Pack runners (``zipformer-zh`` / ``whisper-base`` / ``ppocrv4`` …)
resolve a relative ``inputs.audio`` / ``inputs.image`` against
``repoRoot`` / ``packDir`` / ``cwd`` only (see e.g.
``factory/chat_features/app-builder/models/zipformer-zh/runner.py:_resolve_input_audio``).
None of those bases is the data blob root, so a logical ``uploads/…`` key
never resolves and the run fails with ``INVALID_INPUT: input audio not
found``.

V1 parity
---------
V1 avoided this because its ``/upload/audio`` returned ``rel_path`` =
``data/uploads/audio/…`` (relative to ``repo_root``) and the V1 runner
resolved ``repo_root / "data/uploads/…"`` successfully
(``QAIModelBuilder_v1_pure/backend/app_builder/api_routes.py:825-827`` +
the V1 runner's identical ``_resolve_input_audio``). V2 stores under
``data/blobs/`` and returns a ``data/blobs``-relative logical key, so the
``repo_root`` anchor no longer reaches the file.

Rather than change the §3.1-locked ``artifact.path`` wire field, we
resolve the logical key to an **absolute** physical path *before* the run
input reaches the runner. An absolute path satisfies every runner's
``Path(raw).is_absolute()`` fast-path, so the fix is runner-agnostic and
also covers ModelBuilder-imported packs.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["FILE_INPUT_KEYS", "resolve_input_artifact_paths"]

#: Input keys whose string value is a path to a user-uploaded file the
#: runner must open. Kept explicit (rather than "any string that looks
#: like a path") so opaque text inputs (e.g. melotts ``inputs.text``) are
#: never accidentally rewritten.
FILE_INPUT_KEYS: tuple[str, ...] = ("audio", "image", "video", "file")


def resolve_input_artifact_paths(
    inputs: Mapping[str, Any],
    *,
    blobs_dir: Path | None,
) -> dict[str, Any]:
    """Return a copy of ``inputs`` with logical upload paths made absolute.

    For each key in :data:`FILE_INPUT_KEYS` whose value is a *relative*
    string path that resolves to an existing file under ``blobs_dir``, the
    value is replaced with that file's absolute path. All other entries
    (and already-absolute / non-existent / non-string values) are passed
    through unchanged so behaviour is a strict superset of "verbatim".

    ``blobs_dir is None`` (test containers without a data root) is a no-op.
    """
    if not inputs:
        return dict(inputs or {})
    out: dict[str, Any] = dict(inputs)
    if blobs_dir is None:
        return out
    blob_root = Path(blobs_dir)
    for key in FILE_INPUT_KEYS:
        raw = out.get(key)
        if not isinstance(raw, str) or not raw:
            continue
        p = Path(raw)
        if p.is_absolute():
            # Already a concrete path — leave the runner to validate it.
            continue
        # Logical blob key (e.g. ``uploads/audio/<date>/<file>``).
        candidate = (blob_root / raw).resolve()
        if candidate.is_file():
            out[key] = str(candidate)
    return out
