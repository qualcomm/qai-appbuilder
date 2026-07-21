# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Bootstrap RPC protocol — JSON-line stdin/stdout codec.

The sticky-worker bootstrap subprocess speaks a tiny line-oriented JSON
protocol (one message per line, UTF-8 encoded). Both sides — the
asyncio :class:`StickyWorkerHost` and the bootstrap entry point — use
this module so the wire format is defined in exactly one place.

Wire format
-----------

Each line is a JSON object with a discriminating key:

* **Op** (host → bootstrap): ``{"op": <op_name>, ...}``.
  Op names: ``load`` / ``run`` / ``cancel`` / ``release`` /
  ``ping`` / ``shutdown`` / ``list_loaded``.
* **Status** (bootstrap → host): ``{"type": "status", "state": <state_name>, ...}``.
  Examples: ``worker_ready``, ``loading``, ``model_loaded``,
  ``model_released``, ``cancel_ack``, ``shutting_down``, ``loaded_models``.
* **Pong** (bootstrap → host): ``{"type": "pong"}``.
* **Run event** (bootstrap → host): ``{"type": <event_type>, "runId": ..., ...}``.
  ``event_type`` is one of ``status / progress / metrics / result /
  log / error / done`` per runner_protocol v3.1 (full payload schema
  in PR-302).
* **Error** (bootstrap → host, fatal): ``{"type": "error", "code": ..., "message": ..., "runId"?: ...}``.

Versioning
----------

This module is the single source of truth for the protocol. When a
backwards-incompatible change is needed, the version constant
:data:`PROTOCOL_VERSION` MUST be bumped and both sides MUST advertise
it in the ``worker_ready`` status frame so the host can fail fast on
mismatch.

Frame model
-----------

The :class:`ProtocolFrame` dataclass is a thin envelope around the raw
JSON payload — we keep the payload as a plain ``dict`` so PR-302 can
specialise it into runner_protocol-shaped subtypes without a churn in
the host.

Robustness
----------

The codec is **defensive**: malformed lines (non-JSON / non-dict /
missing discriminator) are converted into ``ProtocolFrame``s of kind
``"unknown"`` so the host can mirror them into the per-run logs panel
rather than crash. A truly fatal protocol violation (e.g. host can't
even decode the line as UTF-8) raises :class:`ProtocolError`.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Final, Literal, Mapping

__all__ = [
    "PROTOCOL_VERSION",
    "BootstrapProtocol",
    "ProtocolError",
    "ProtocolFrame",
    "FrameKind",
]

logger = logging.getLogger(__name__)

PROTOCOL_VERSION: Final[str] = "3.1.0"
"""Bootstrap RPC protocol version.

Incompatible changes (semver-major bump, e.g. ``3.x → 4.0``) require
PR-301-style end-to-end migration of host + bootstrap simultaneously.
"""


FrameKind = Literal[
    "status",
    "pong",
    "result",
    "progress",
    "metrics",
    "log",
    "error",
    "done",
    "loaded_models",
    "unknown",
]


class ProtocolError(RuntimeError):
    """Unrecoverable protocol violation (line cannot be UTF-8 decoded, etc.).

    Routine malformed JSON is *not* a :class:`ProtocolError` — those
    surface as ``ProtocolFrame(kind='unknown')`` so the worker can keep
    running. We only raise this when there's no sensible way to keep
    the channel alive.
    """


@dataclass(frozen=True, slots=True, kw_only=True)
class ProtocolFrame:
    """One decoded line from the bootstrap subprocess.

    Fields:

    * :attr:`kind` — discriminator (see :data:`FrameKind`).
      ``"unknown"`` is used for malformed / unrecognised lines so the
      host can surface them in the Logs panel.
    * :attr:`payload` — verbatim JSON object that came over the wire
      (frozen-shape via the dataclass; not deep-copied).
    * :attr:`raw` — the original line text, preserved for diagnostics.
    """

    kind: FrameKind
    payload: Mapping[str, Any] = field(default_factory=dict)
    raw: str = ""

    def __post_init__(self) -> None:
        if self.kind not in (
            "status",
            "pong",
            "result",
            "progress",
            "metrics",
            "log",
            "error",
            "done",
            "loaded_models",
            "unknown",
        ):
            raise ValueError(
                f"ProtocolFrame.kind must be a FrameKind, got {self.kind!r}"
            )
        if not isinstance(self.payload, Mapping):
            raise ValueError("ProtocolFrame.payload must be a Mapping")
        if not isinstance(self.raw, str):
            raise ValueError("ProtocolFrame.raw must be str")

    @property
    def state(self) -> str | None:
        """Convenience accessor for ``state`` field on status-shaped frames.

        Both ``"status"`` and the disambiguated ``"loaded_models"`` kind
        carry a ``state`` field on the wire (the latter being a
        ``status`` frame with ``state="loaded_models"``); the accessor
        returns it for both so consumers can route uniformly.
        """
        if self.kind not in ("status", "loaded_models"):
            return None
        value = self.payload.get("state")
        return value if isinstance(value, str) else None

    @property
    def run_id(self) -> str | None:
        """Convenience accessor for run-scoped frames' ``runId`` field."""
        value = self.payload.get("runId")
        return value if isinstance(value, str) else None

    @property
    def model_id(self) -> str | None:
        """Convenience accessor for the ``modelId`` field where present."""
        value = self.payload.get("modelId")
        return value if isinstance(value, str) else None


class BootstrapProtocol:
    """Stateless codec for one-line JSON frames.

    Both sides instantiate (or directly call the static methods on)
    this class. Methods are:

    * :meth:`encode_op` — host-side: build the bytes to write to the
      bootstrap's stdin (newline-terminated UTF-8).
    * :meth:`decode_line` — host-side: parse a single line read from
      the bootstrap's stdout into a :class:`ProtocolFrame`.

    The class exposes no I/O; the caller owns the streams (so the
    host can use asyncio while a unit test can use a synchronous
    ``BytesIO``).
    """

    @staticmethod
    def encode_op(payload: Mapping[str, Any]) -> bytes:
        """Serialise an op payload into a newline-terminated UTF-8 line.

        Raises :class:`ValueError` if ``payload`` does not have an
        ``"op"`` key (catches host bugs early).
        """
        if not isinstance(payload, Mapping):
            raise ValueError("op payload must be a Mapping")
        op = payload.get("op")
        if not isinstance(op, str) or not op.strip():
            raise ValueError(
                "op payload must contain a non-empty 'op' string"
            )
        # ensure_ascii=False keeps Chinese model titles readable in
        # diagnostic dumps; UTF-8 framing handles non-ASCII fine.
        line = json.dumps(dict(payload), ensure_ascii=False) + "\n"
        return line.encode("utf-8")

    @staticmethod
    def decode_line(raw: str) -> ProtocolFrame:
        """Parse a single line from the bootstrap's stdout.

        ``raw`` is the line as produced by :class:`asyncio.StreamReader`
        — bytes already decoded with ``errors="replace"`` and the
        trailing newline stripped. Empty / whitespace-only lines yield
        ``ProtocolFrame(kind="unknown")`` (no exception).
        """
        if not isinstance(raw, str):
            raise ProtocolError(
                f"decode_line requires str, got {type(raw).__name__}"
            )
        text = raw.strip()
        if not text:
            return ProtocolFrame(kind="unknown", raw=raw)
        try:
            obj: Any = json.loads(text)
        except json.JSONDecodeError:
            return ProtocolFrame(kind="unknown", raw=raw)
        if not isinstance(obj, dict):
            return ProtocolFrame(kind="unknown", raw=raw)
        kind = _classify(obj)
        return ProtocolFrame(kind=kind, payload=obj, raw=raw)

    @staticmethod
    def encode_status(state: str, **fields: Any) -> bytes:
        """Bootstrap-side helper: build a ``status`` frame line.

        Provided so the bootstrap entry point (PR-302) doesn't have to
        re-implement the framing. Returns newline-terminated UTF-8
        bytes ready to write to ``sys.stdout.buffer``.
        """
        if not isinstance(state, str) or not state.strip():
            raise ValueError("status state must be a non-empty str")
        payload: dict[str, Any] = {"type": "status", "state": state}
        for k, v in fields.items():
            payload[k] = v
        return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

    @staticmethod
    def encode_pong() -> bytes:
        """Bootstrap-side helper: build the canonical pong reply."""
        return b'{"type": "pong"}\n'


def _classify(obj: dict[str, Any]) -> FrameKind:
    """Map a parsed JSON object onto a :class:`FrameKind`.

    Order of checks is deliberate: ``status`` must beat the generic
    ``type`` switch so a frame like ``{"type":"status","state":"loaded_models",...}``
    routes to the dedicated ``loaded_models`` kind.
    """
    raw_type = obj.get("type")
    if not isinstance(raw_type, str):
        return "unknown"
    if raw_type == "status":
        if obj.get("state") == "loaded_models":
            return "loaded_models"
        return "status"
    if raw_type in (
        "pong",
        "result",
        "progress",
        "metrics",
        "log",
        "error",
        "done",
    ):
        return raw_type  # type: ignore[return-value]
    return "unknown"
