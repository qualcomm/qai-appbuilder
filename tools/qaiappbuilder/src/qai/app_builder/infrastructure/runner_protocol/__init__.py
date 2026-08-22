# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Runner protocol v3.1 — typed event envelopes (PR-302).

Implements the wire-level decoder for the **runner_protocol v3.1**
emitted by App Builder Pack ``runner.py`` scripts in *oneshot* mode.
The legacy SSOT lives in
``features/app-builder/shared/runner_protocol.py`` (encoder helpers
for the Pack side: ``emit / status / progress / metrics / result /
done / fail``). This module is the **decoder** (host side).

Wire format
-----------

The Pack runner is a one-shot subprocess:

* ``cwd``  = Pack root directory (``features/app-builder/models/<id>/``);
* ``stdin`` = single line JSON request (``{runId, modelId, inputs,
  params, options, packDir, repoRoot, variantId, ...}``);
* ``stdout`` = one JSON event per line — the seven event kinds:

  ===========  ========================================================
  ``status``   ``{type:"status", state:"preparing"|"running"}``
  ``progress`` ``{type:"progress", phase:"infer", pct:42}``
  ``metrics``  ``{type:"metrics", latencyMs, memoryMB, device, ...}``
  ``log``      ``{type:"log", stream:"stdout"|"stderr", line:"..."}``
  ``result``   ``{type:"result", output:{...}}`` — exactly one
  ``done``     ``{type:"done"}`` — final on success
  ``error``    ``{type:"error", code, message, ...}`` — final on failure
  ===========  ========================================================

* ``stderr`` = free-form logs (host folds them into ``log`` events).

Decoder rules
-------------

* JSON-decoded line → discriminated by ``"type"`` field:
   - known type → corresponding :class:`RunnerEvent` subclass;
   - unknown type → :class:`UnknownRunnerEvent` (preserves original
     payload so the host can still surface it to the Logs panel).
* JSON-malformed line / non-dict → :class:`StdoutLogEvent` (treated as
  a free-form stdout line).
* Empty / whitespace-only line → ``None`` (caller skips).

Termination
-----------

The decoder does not enforce protocol-level termination (host owns
that). Helpers :func:`is_terminal` / :func:`event_kind` let consumers
implement the "stream until ``done`` or ``error``" loop without
hard-coding the kind set.

Compatibility with PR-301 sticky-worker protocol
-------------------------------------------------

PR-301's :class:`BootstrapProtocol` codec covers the **bootstrap RPC**
(`op:load / op:run / op:release / ...`) used by the *long-running*
sticky worker host. PR-302 covers the *oneshot* runner_protocol used
by the per-Pack subprocess. The two protocols share the same JSON-line
framing but have disjoint event kinds — sticky bootstrap emits
``status:worker_ready / model_loaded / pong / ...`` while runner_protocol
emits ``status:preparing / status:running / progress / metrics /
result / done / error``.

PR-302 imports neither sticky_worker module — both subsystems live
side-by-side under ``infrastructure/`` and may eventually merge once
the sticky bootstrap also speaks runner_protocol v3.1 frames inside
its ``op:run`` event stream. The two-protocol layout is the current
production shape; convergence is a structural option, not a planned
migration.
"""

from __future__ import annotations

from .events import (
    DoneEvent,
    ErrorEvent,
    LogEvent,
    LogStream,
    MetricsEvent,
    ProgressEvent,
    ResultEvent,
    RunnerEvent,
    StatusEvent,
    StdoutLogEvent,
    UnknownRunnerEvent,
    decode_event,
    event_kind,
    is_terminal,
)
from .ndjson_decoder import NdjsonDecoder

__all__ = [
    "DoneEvent",
    "ErrorEvent",
    "LogEvent",
    "LogStream",
    "MetricsEvent",
    "NdjsonDecoder",
    "ProgressEvent",
    "ResultEvent",
    "RunnerEvent",
    "StatusEvent",
    "StdoutLogEvent",
    "UnknownRunnerEvent",
    "decode_event",
    "event_kind",
    "is_terminal",
]
