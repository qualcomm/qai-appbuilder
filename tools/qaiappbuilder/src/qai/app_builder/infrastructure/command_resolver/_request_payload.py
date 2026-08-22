# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Helpers that serialise the runner request envelope (batch E).

The 4 built-in App Builder Pack runners
(``factory/chat_features/app-builder/models/<pack>/runner.py``) consume their
inputs through ``runner_protocol.read_request()`` — a single line of
JSON read from stdin. Without that envelope the runner immediately
raises ``RuntimeError("no request received on stdin (and no argv[1]
file)")`` and the run never produces frames.

This module is the platform-agnostic side of the contract: it builds
the JSON payload (matching the field names the runners actually read:
``repoRoot`` / ``packDir`` / ``inputs`` / optional ``params`` /
optional ``variant`` / optional ``runId``) and returns the UTF-8 encoded line so the
:class:`qai.platform.process.SubprocessProcessRunner` can write it
into the child's stdin via ``ProcessExecutionRequest.stdin_data``.

The format is intentionally minimal — runner-specific fields land in
``inputs`` / ``params`` verbatim so adding a new pack does not require
wire changes here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

__all__ = ["build_runner_request_payload"]


def build_runner_request_payload(
    *,
    repo_root: Path,
    pack_dir: Path,
    inputs: Mapping[str, Any] | None,
    params: Mapping[str, Any] | None = None,
    variant: str | None = None,
    run_id: str | None = None,
) -> bytes:
    """Build the JSON request bytes consumed by a Pack ``runner.py``.

    Schema mirrors ``factory/chat_features/app-builder/shared/runner_protocol.py``
    ``read_request()``:

    * ``repoRoot``: absolute path string of the repository root (the
      runner uses it to anchor relative ``inputs.image`` / weight
      lookups under ``<repo>/models/<pack>/``).
    * ``packDir``: absolute path string of
      ``<repo>/factory/chat_features/app-builder/models/<pack>/`` (where the
      runner's ``weights/`` and ``assets/`` siblings live).
    * ``inputs``: opaque mapping forwarded verbatim. Each runner reads
      its own keys (``inputs.image`` for ppocrv4, ``inputs.audio`` for
      whisper-base / zipformer-zh, ``inputs.text`` for melotts-zh).
    * Optional ``params`` and ``variant`` ride alongside.
    * Optional ``runId`` lets runners write per-run output artifacts instead
      of falling back to pack-level fixed filenames.

    The line **must** be a single line terminated with ``\\n`` because
    ``read_request`` consumes the request via ``sys.stdin.readline()``.

    Returns the UTF-8 encoded bytes ready for
    :attr:`qai.platform.process.ProcessExecutionRequest.stdin_data`.
    """
    if not isinstance(repo_root, Path):
        raise TypeError(
            f"repo_root must be Path, got {type(repo_root).__name__}"
        )
    if not isinstance(pack_dir, Path):
        raise TypeError(
            f"pack_dir must be Path, got {type(pack_dir).__name__}"
        )

    payload: dict[str, Any] = {
        "repoRoot": str(repo_root),
        "packDir": str(pack_dir),
        "inputs": dict(inputs) if inputs else {},
    }
    if params:
        payload["params"] = dict(params)
    if variant is not None:
        if not isinstance(variant, str):
            raise TypeError(
                f"variant must be str or None, got {type(variant).__name__}"
            )
        if variant:
            payload["variant"] = variant
    if run_id is not None:
        if not isinstance(run_id, str):
            raise TypeError(
                f"run_id must be str or None, got {type(run_id).__name__}"
            )
        if run_id:
            payload["runId"] = run_id

    # ``ensure_ascii=False`` keeps CJK / Unicode payloads compact and
    # human-readable on the wire; ``read_request`` calls
    # ``json.loads`` which decodes UTF-8 fine. The ``\n`` terminator
    # is mandatory — ``read_request`` uses ``readline`` and would
    # otherwise block forever waiting for an EOL after our EOF.
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
