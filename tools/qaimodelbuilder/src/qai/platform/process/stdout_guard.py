# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Process-level stdout protection + faulthandler arming for native-loading
one-shot child processes.

Why this module exists
======================
Native libraries (``qai_appbuilder`` / the QNN HTP runtime, numpy / Pillow C
extensions, etc.) write to the process ``fd 1`` / ``fd 2`` via C ``printf`` /
``std::cout``. When such a library is loaded **in-process** by a long-running
service, those stray bytes leak into the service's stdout / stderr (and any log
file the launcher redirects fd 2 into). They also corrupt any structured
protocol (JSON events) the parent expects on the child's stdout pipe.

This helper is the single cross-context implementation of the fd-level guard
that the App Builder ``_runner_bootstrap.py`` first introduced
(``_runner_bootstrap._protect_stdout`` + the ``faulthandler.enable`` call in
``main()``). It is extracted here (``qai.platform.process``, the process
shared kernel) so any one-shot child that loads native code can reuse the
exact same isolation without copy-pasting, and without a cross-context import
(``qai.model_builder`` -> ``qai.app_builder`` is forbidden by the
import-linter ``context-isolation`` contract).

Contract (identical to ``_runner_bootstrap``)
=============================================
After :func:`protect_stdout` returns:

* ``sys.stdout`` -> a Python file object wrapping the REAL saved stdout fd
  (the pipe the parent reads). This is the ONLY path that reaches the parent's
  stdout pipe, so a child writes its structured JSON envelope via
  ``sys.stdout.write(...)``.
* process-level ``fd 1`` -> points at ``fd 2`` (stderr), so any C/C++ code
  doing ``write(1, ...)`` / ``printf(...)`` goes to stderr instead of the
  event pipe.
* ``builtins.print`` -> writes to stderr for extra safety.

:func:`arm_faulthandler` enables :mod:`faulthandler` on stderr so a native
segfault (Windows ``0xC0000005`` / POSIX ``SIGSEGV``) produces a real
traceback on stderr instead of the child vanishing with an empty stderr.

Ordering rule (critical)
========================
Both functions MUST be called **before importing any native library**. Once a
native DLL is loaded its stdout writes cannot be recaptured. See
``_runner_bootstrap.main()`` (arm faulthandler -> protect stdout -> only then
load user code) for the reference ordering.

This module imports only the standard library so it is always safe to import
first, before ``qai_appbuilder`` or any heavy dependency.
"""

from __future__ import annotations

import builtins
import faulthandler
import sys
from typing import Any

__all__ = ["arm_faulthandler", "protect_stdout"]


def arm_faulthandler() -> None:
    """Enable :mod:`faulthandler` on stderr (all threads). Never raises.

    Call this first thing in a one-shot child so a native fatal fault
    (segfault / abort in a QNN / HTP DLL) surfaces a traceback on stderr
    rather than an empty-stderr silent exit.
    """
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except (RuntimeError, OSError, ValueError):
        # faulthandler may be unavailable on exotic embeds; a missing crash
        # traceback must never abort the child before it does real work.
        pass


def protect_stdout() -> Any:
    """Redirect process-level stdout so only structured writes reach the pipe.

    Returns the file object bound to the saved real-stdout fd (also installed
    as ``sys.stdout``) so a caller can hold / flush it explicitly if it wants;
    most callers simply use ``sys.stdout`` afterwards.

    Mirrors ``_runner_bootstrap._protect_stdout`` byte-for-byte in behaviour:

      1. ``os.dup(1)`` saves the REAL stdout fd (the pipe to the parent).
      2. ``os.dup2(2, 1)`` redirects process-level fd 1 -> stderr, so any
         native ``printf`` / ``cout`` goes to stderr instead of the event pipe.
      3. a Python file object wrapping the saved fd becomes ``sys.stdout``.
      4. ``builtins.print`` is redirected to stderr for extra safety.

    Never raises: on any OS error it degrades to only redirecting
    ``builtins.print`` to stderr (the same fallback as ``_runner_bootstrap``),
    so the child can still run.
    """
    import os

    try:
        # 1. Save the real stdout fd (the pipe to the parent / orchestrator).
        event_fd = os.dup(sys.stdout.fileno())

        # 2. Redirect fd 1 to stderr fd. After this, any C/C++ code doing
        #    write(1, ...) or printf(...) outputs to stderr instead.
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

        # 3. Create a Python file object for the saved event fd.
        event_stream = os.fdopen(event_fd, "w", encoding="utf-8", errors="replace")

        # 4. Replace sys.stdout so structured writes go to the event pipe.
        sys.stdout = event_stream

        # 5. Redirect builtins.print to stderr.
        _original_print = builtins.print

        def _print_to_stderr(*args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("file", sys.stderr)
            _original_print(*args, **kwargs)

        builtins.print = _print_to_stderr
        return event_stream

    except (OSError, AttributeError):
        _original_print = builtins.print

        def _print_to_stderr_fallback(*args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("file", sys.stderr)
            _original_print(*args, **kwargs)

        builtins.print = _print_to_stderr_fallback
        return sys.stdout
