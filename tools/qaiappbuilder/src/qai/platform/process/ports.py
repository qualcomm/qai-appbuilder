# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Port + value objects for cross-context process execution (PR-041).

The shapes here are deliberately tiny and free of security / app_builder
concepts so both contexts can consume them without leaking the other's
vocabulary.

Frame protocol
--------------
``ProcessRunnerPort.run`` returns an async iterator that yields
:class:`ProcessFrame` values in this order:

1. exactly one :class:`ProcessStartedFrame` (with the spawned PID);
2. zero or more :class:`ProcessStdoutFrame` / :class:`ProcessStderrFrame`
   chunks interleaved as they arrive (each frame carries one buffered
   read, never multiple lines fused together);
3. exactly one :class:`ProcessTerminatedFrame` carrying the exit status.

If the request is rejected before spawning (e.g. zero ``argv``), the
runner raises a :class:`ValueError` synchronously rather than yielding
an error frame.

If the timeout fires the runner kills the child, drains any remaining
output, then yields a :class:`ProcessTerminatedFrame` with
``timed_out=True`` and ``exit_code=None``.

The output buffer cap (``ProcessExecutionRequest.output_byte_cap``) is
counted across both stdout and stderr; once exceeded the runner kills
the child, sets ``truncated=True`` on the terminating frame, and
finishes with whatever it had buffered up to the cap.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable

__all__ = [
    "ProcessExecutionRequest",
    "ProcessExitStatus",
    "ProcessFrame",
    "ProcessFrameKind",
    "ProcessRunnerPort",
    "ProcessStartedFrame",
    "ProcessStderrFrame",
    "ProcessStdoutFrame",
    "ProcessTerminatedFrame",
]


# 16 MiB hard upper bound for ``ProcessExecutionRequest.stdin_data``.
# Sized for App Builder Pack runner JSON envelopes (typically <1 KiB)
# with comfortable headroom for batched datasets while still preventing
# a GiB-scale payload from deadlocking the read pumps.
_STDIN_DATA_MAX_BYTES: int = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Frame kind enum
# ---------------------------------------------------------------------------
class ProcessFrameKind(str, Enum):
    """Discriminator for :class:`ProcessFrame` subclasses.

    Modelled as a string-valued enum so consumers can route on
    ``frame.kind.value`` (e.g. when serialising the stream over SSE in
    PR-045) without having to import the concrete subclasses.
    """

    STARTED = "started"
    STDOUT = "stdout"
    STDERR = "stderr"
    TERMINATED = "terminated"


# ---------------------------------------------------------------------------
# Request value object
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessExecutionRequest:
    """Caller's intent for a single subprocess run.

    Frozen for hashability and so concurrent consumers cannot mutate the
    request after the runner has captured it.

    Fields
    ------
    argv:
        Non-empty tuple ``(executable, *args)``. The runner uses
        :func:`asyncio.create_subprocess_exec` (no shell) so each entry
        must already be a fully-formed argv element.
    cwd:
        Working directory for the child. ``None`` means inherit. Must be
        an existing directory on the filesystem; the runner validates
        this before spawning.
    env:
        Optional environment override. ``None`` means inherit the parent
        environment verbatim. An empty mapping means "spawn with an empty
        environment" -- callers that want to *augment* the parent env
        should construct a copy of ``os.environ`` first.
    timeout_s:
        Hard wall-clock deadline. ``None`` disables the deadline. The
        runner enforces ``timeout_s > 0`` when the value is set; ``0`` is
        rejected because it is ambiguous (immediate kill vs. no
        deadline).
    output_byte_cap:
        Combined cap across stdout + stderr. ``None`` means uncapped
        (use with care). Once exceeded the runner kills the child.
    stdin_data:
        Optional payload to write to the child's stdin once the process
        is spawned. ``None`` (the default) preserves the PR-041 behaviour
        of attaching ``DEVNULL`` so the child sees an immediate EOF on
        ``stdin``. When non-``None`` the runner attaches a PIPE, writes
        the bytes verbatim, and closes the pipe to signal EOF — this is
        how the App Builder Pack runners (whose ``runner_protocol.read_
        request`` reads a single JSON line from stdin) receive their
        request envelope. Must be ``bytes`` (not ``str`` /
        ``bytearray`` / ``memoryview``) and at most 16 MiB so an
        accidental gigabyte-sized payload does not deadlock the pump.
    """

    argv: tuple[str, ...]
    cwd: str | None = None
    env: Mapping[str, str] | None = None
    timeout_s: float | None = None
    output_byte_cap: int | None = None
    # PR-batch-E (tail-append; §3.1 only allows additive changes here):
    # ``None`` keeps the runner attaching DEVNULL to the child's stdin
    # (PR-041 behaviour, byte-for-byte equivalent for the 31 existing
    # call sites). Non-``None`` flips the runner to PIPE + write + close.
    stdin_data: bytes | None = None
    # 2026-07-08 (tail-append; additive). Optional probe the runner calls at
    # its execution deadline: given the spawned child's pid, returns True iff
    # that child (or its subtree) is currently BLOCKED on a native FileGuard
    # authorization dialog. When True the runner RE-ARMS the deadline instead
    # of killing — so time spent waiting for the user to approve a file access
    # is not counted against the command timeout. ``None`` (default) keeps the
    # original behaviour: deadline always force-kills (orphan-safe). Platform
    # layer stays context-neutral — it only receives a callable, never imports
    # security. Excluded from equality/hash (callables are not value identity).
    ask_pending_probe: Callable[[int], bool] | None = field(
        default=None, compare=False
    )

    def __post_init__(self) -> None:
        if not isinstance(self.argv, tuple):
            raise TypeError(
                f"argv must be a tuple, got {type(self.argv).__name__}"
            )
        if not self.argv:
            raise ValueError("argv must be non-empty")
        for i, item in enumerate(self.argv):
            if not isinstance(item, str):
                raise TypeError(
                    f"argv[{i}] must be str, got {type(item).__name__}"
                )
            if not item:
                raise ValueError(f"argv[{i}] must be non-empty")
        if self.timeout_s is not None:
            if not isinstance(self.timeout_s, (int, float)) or isinstance(
                self.timeout_s, bool
            ):
                raise TypeError(
                    "timeout_s must be float or None, got "
                    f"{type(self.timeout_s).__name__}"
                )
            if self.timeout_s <= 0:
                raise ValueError(
                    f"timeout_s must be > 0 when set, got {self.timeout_s!r}"
                )
        if self.output_byte_cap is not None:
            if not isinstance(self.output_byte_cap, int) or isinstance(
                self.output_byte_cap, bool
            ):
                raise TypeError(
                    "output_byte_cap must be int or None, got "
                    f"{type(self.output_byte_cap).__name__}"
                )
            if self.output_byte_cap <= 0:
                raise ValueError(
                    "output_byte_cap must be > 0 when set, got "
                    f"{self.output_byte_cap!r}"
                )
        if self.stdin_data is not None:
            # Strict ``bytes`` only: ``bytearray`` / ``memoryview`` /
            # ``str`` are deliberately rejected so call sites can't
            # accidentally feed encoded text without a final UTF-8 hop.
            if type(self.stdin_data) is not bytes:
                raise TypeError(
                    "stdin_data must be bytes or None, got "
                    f"{type(self.stdin_data).__name__}"
                )
            # 16 MiB upper bound: App Builder runner request envelopes
            # are typically <1 KiB; this guards against an accidental
            # GiB-scale payload deadlocking the pump pipeline.
            if len(self.stdin_data) > _STDIN_DATA_MAX_BYTES:
                raise ValueError(
                    "stdin_data exceeds 16 MiB cap, got "
                    f"{len(self.stdin_data)} bytes"
                )

    @classmethod
    def of(
        cls,
        *argv: str,
        cwd: str | None = None,
        env: Mapping[str, str] | None = None,
        timeout_s: float | None = None,
        output_byte_cap: int | None = None,
        stdin_data: bytes | None = None,
    ) -> "ProcessExecutionRequest":
        """Convenience builder accepting positional argv entries.

        ``ProcessExecutionRequest.of("python", "-c", "print(1)")`` is
        equivalent to ``ProcessExecutionRequest(argv=("python", "-c",
        "print(1)"))`` but reads more naturally at call sites.
        """
        return cls(
            argv=tuple(argv),
            cwd=cwd,
            env=env,
            timeout_s=timeout_s,
            output_byte_cap=output_byte_cap,
            stdin_data=stdin_data,
        )


# ---------------------------------------------------------------------------
# Exit status value object
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessExitStatus:
    """Outcome of a finished (or killed) subprocess.

    ``exit_code`` is ``None`` when the runner had to kill the child
    (timeout / output cap / explicit cancellation). On POSIX the
    conventional way to inspect a signal is via the negative exit code
    surfaced by :mod:`asyncio` -- that value is preserved verbatim here.

    The boolean flags are non-exclusive: a process can be both timed
    out and truncated if it fills the buffer cap and then misses the
    deadline anyway. Consumers should look at ``timed_out`` /
    ``truncated`` independently.
    """

    exit_code: int | None
    timed_out: bool = False
    truncated: bool = False

    def __post_init__(self) -> None:
        if self.exit_code is not None and not isinstance(
            self.exit_code, int
        ):
            raise TypeError(
                "exit_code must be int or None, got "
                f"{type(self.exit_code).__name__}"
            )
        if not isinstance(self.timed_out, bool):
            raise TypeError(
                f"timed_out must be bool, got {type(self.timed_out).__name__}"
            )
        if not isinstance(self.truncated, bool):
            raise TypeError(
                f"truncated must be bool, got {type(self.truncated).__name__}"
            )

    @property
    def succeeded(self) -> bool:
        """True iff the process exited cleanly with code 0."""
        return self.exit_code == 0 and not self.timed_out and not self.truncated


# ---------------------------------------------------------------------------
# Frame value objects
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessStartedFrame:
    """First frame emitted by :class:`ProcessRunnerPort.run`."""

    pid: int

    @property
    def kind(self) -> ProcessFrameKind:
        return ProcessFrameKind.STARTED


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessStdoutFrame:
    """Bytes read from the child's stdout."""

    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError(
                f"data must be bytes, got {type(self.data).__name__}"
            )

    @property
    def kind(self) -> ProcessFrameKind:
        return ProcessFrameKind.STDOUT


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessStderrFrame:
    """Bytes read from the child's stderr."""

    data: bytes

    def __post_init__(self) -> None:
        if not isinstance(self.data, bytes):
            raise TypeError(
                f"data must be bytes, got {type(self.data).__name__}"
            )

    @property
    def kind(self) -> ProcessFrameKind:
        return ProcessFrameKind.STDERR


@dataclass(frozen=True, slots=True, kw_only=True)
class ProcessTerminatedFrame:
    """Final frame, always present once a run reaches steady state."""

    status: ProcessExitStatus

    @property
    def kind(self) -> ProcessFrameKind:
        return ProcessFrameKind.TERMINATED


ProcessFrame = (
    ProcessStartedFrame
    | ProcessStdoutFrame
    | ProcessStderrFrame
    | ProcessTerminatedFrame
)


# ---------------------------------------------------------------------------
# Port
# ---------------------------------------------------------------------------
@runtime_checkable
class ProcessRunnerPort(Protocol):
    """Async streaming subprocess runner.

    Implementations should:

    * spawn the subprocess **without** going through a shell;
    * never raise once :meth:`run` has yielded the first frame -- always
      reflect failures by ending the iterator with a
      :class:`ProcessTerminatedFrame` carrying the appropriate flags;
    * raise :class:`ValueError` synchronously (before iteration starts)
      for invalid request shape (empty argv, missing cwd, ...).

    The runner is intentionally stateless from the caller's perspective:
    one :meth:`run` call corresponds to one subprocess. Concurrency is
    safe -- multiple concurrent calls each drive their own child.
    """

    def run(
        self, request: ProcessExecutionRequest
    ) -> AsyncIterator[ProcessFrame]:
        """Spawn ``request.argv`` and stream output frames.

        Returning an :class:`AsyncIterator` (not an
        :class:`AsyncGenerator`) keeps the type annotation independent of
        ``yield`` mechanics; concrete adapters typically implement this
        by returning a private helper coroutine generator.
        """
        ...


# Suppress unused-import warning for the ``Sequence`` re-export that is
# nominally part of the module typing surface but not directly referenced
# in the runtime types defined above.
_ = Sequence
