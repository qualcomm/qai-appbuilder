# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Port protocols for the desktop-control capability.

Two Protocols split the async supervisor (crosses the subprocess
boundary) from the synchronous in-worker desktop session (drives Win32
directly). The tool handler depends ONLY on
:class:`DesktopControllerPort`; it never imports Win32 or subprocess
internals, keeping the cross-context edge clean.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .types import Action, Capabilities, Capture

__all__ = [
    "DesktopControllerPort",
    "DesktopSessionPort",
]


@runtime_checkable
class DesktopControllerPort(Protocol):
    """Async controller backing the ``computer`` tool.

    Implemented by the subprocess supervisor: ``execute`` crosses the
    worker process boundary (hence async) and runs batches serially;
    ``capabilities`` is cached after the worker reports ``ready``.
    """

    @property
    def capabilities(self) -> Capabilities | None:
        """Probed capabilities, or ``None`` before the first execute."""
        ...

    async def execute(self, actions: list[Action]) -> Capture:
        """Run an ordered batch of actions; return a fresh screenshot."""
        ...

    async def close(self) -> None:
        """Close the controller and release the worker (idempotent)."""
        ...


@runtime_checkable
class DesktopSessionPort(Protocol):
    """Synchronous desktop session used inside the worker subprocess.

    Aggregates the Win32 primitives (capture + input) and runs each call
    synchronously on the worker's dedicated thread.
    """

    @property
    def capabilities(self) -> Capabilities:
        """Probed capabilities of this session."""
        ...

    def capture(self) -> Capture:
        """Take one screenshot; spans every captured monitor.

        The returned :class:`Capture` reports one ``Display`` per captured
        monitor with its virtual-desktop rect, so a caller can map an image
        pixel back to a screen coordinate on any screen.
        """
        ...

    def execute(self, actions: list[Action]) -> Capture:
        """Run a batch of actions; return the post-batch screenshot."""
        ...

    def close(self) -> None:
        """Release any held resources."""
        ...
