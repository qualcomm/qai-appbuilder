# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared runtime-package mutation lock and pip/uv error classification.

Neutral platform helper (shared kernel) used by every writer that mutates
the shared runtime venv's installed packages:
:class:`qai.app_builder.infrastructure.dep_checker.DynamicPackDepChecker`
and the QAIRT extension installer
(:mod:`qai.platform.qairt_switch.installer`). Living under ``qai.platform.*``
-- NOT under any bounded context -- lets both consumers share it without
either importing the other's context: ``qai.app_builder`` may import
``qai.platform.**`` freely (shared-kernel rule), and
``qai.platform.qairt_switch`` must never import ``qai.app_builder.*``
(layering: a bounded context must not depend on the platform depending back
on it). This module is the single place both sides meet.

Contents
--------

* :func:`classify_pip_error` -- pure stderr-tail classifier
  ``(error_kind, error_hint)``, extracted verbatim from the legacy
  ``DynamicPackDepChecker._classify_pip_error`` so its behaviour (and the
  front-end error copy it drives) is unchanged.
* :class:`PackageMutationLock` -- one DI-owned async lock so a ``pip``/``uv``
  child process spawned by the dependency checker and one spawned by the
  QAIRT extension installer can never run concurrently against the same
  venv. Two writers racing ``site-packages`` on Windows (file-in-use /
  partial overwrite) is exactly the failure this lock exists to prevent.
  Construct exactly ONE instance at the composition root and inject the
  SAME instance into every component that spawns such a subprocess.
"""

from __future__ import annotations

import asyncio
from types import TracebackType

__all__ = ["PackageMutationLock", "classify_pip_error"]


def classify_pip_error(stderr: str) -> tuple[str, str]:
    """Classify a failed pip/uv install's stderr tail into ``(kind, hint)``.

    Pattern-matches common failure signatures (TLS trust store, network
    reachability, missing ARM64 wheel, Windows file-in-use permission,
    disk space, timeout) into a small allowlisted ``error_kind`` plus a
    human-actionable ``error_hint``. Falls back to ``"unknown"`` so callers
    always get a two-tuple, never ``None``.
    """
    s = stderr.lower() if stderr else ""
    if (
        "invalid peer certificate" in s
        or "unknownissuer" in s
        or "ssl: certificate_verify_failed" in s
        or "self signed certificate" in s
        or "certificate verify failed" in s
    ):
        return (
            "tls_cert",
            "TLS certificate verification failed when contacting pypi.org. "
            "Add your corporate root CA to Python's trust store, or set "
            "REQUESTS_CA_BUNDLE / SSL_CERT_FILE.",
        )
    if (
        "failed to fetch" in s
        or "could not fetch" in s
        or "connection refused" in s
        or "name or service not known" in s
        or "temporary failure in name resolution" in s
        or "network is unreachable" in s
        or "no route to host" in s
        or "failed establishing a new connection" in s
    ):
        return (
            "network",
            "Network connection failed when contacting pypi.org. "
            "Check internet access and HTTP_PROXY / HTTPS_PROXY settings.",
        )
    if (
        "no matching distribution" in s
        or "could not find a version" in s
        or ("no version of" in s and "satisfies" in s)
    ):
        return (
            "no_match",
            "The required package version could not be found on PyPI for "
            "this Python interpreter (likely an ARM64 wheel availability "
            "issue).",
        )
    if (
        "permission denied" in s
        or "operation not permitted" in s
        or "[winerror 5]" in s
    ):
        return (
            "permission",
            "Permission denied while writing to the venv. Close any "
            "process using the venv and retry.",
        )
    if "no space left" in s or "disk full" in s or "[errno 28]" in s:
        return (
            "disk_full",
            "Disk is full. Free up space on the drive containing the "
            "ARM64 venv and retry.",
        )
    if "read timed out" in s or "timeout" in s:
        return (
            "timeout",
            "Pip request timed out. Check your network speed.",
        )
    return (
        "unknown",
        "Dependency installation failed with an unrecognized error. "
        "See the raw stderr below for details.",
    )


class PackageMutationLock:
    """Single DI-owned lock serializing pip/uv child processes against the runtime venv.

    Construct exactly once at the composition root and inject the SAME
    instance into every component that spawns a ``pip``/``uv`` subprocess
    targeting the shared runtime venv. Use as an async context manager::

        async with lock:
            ... spawn the pip/uv subprocess ...

    Wraps a plain :class:`asyncio.Lock`; the wrapper type exists so DI wiring
    has a distinct, intention-revealing type to construct and pass around
    (rather than a bare ``asyncio.Lock`` whose purpose is not self-evident
    at injection sites), and so a future caller can inspect :attr:`locked`
    without reaching into a private attribute.
    """

    __slots__ = ("_lock",)

    def __init__(self) -> None:
        self._lock = asyncio.Lock()

    @property
    def locked(self) -> bool:
        """Whether a mutation is currently in flight under this lock."""
        return self._lock.locked()

    async def __aenter__(self) -> "PackageMutationLock":
        await self._lock.acquire()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._lock.release()
