#=============================================================================
#
# Copyright (c) 2023, Qualcomm Innovation Center, Inc. All rights reserved.
#
# SPDX-License-Identifier: BSD-3-Clause
#
#=============================================================================
"""
Suppress a specific benign native-runtime line on the OS stdout/stderr streams.

At DSP-queue teardown (inside ``del(context)`` / ``ModelDestroy`` and at process
exit) the QNN HTP / fastRPC runtime writes this line straight to a process
*file descriptor*:

    <E> Error 0x200: failed to close queue 000001A... for priority 100

Empirically (Windows on Snapdragon) the runtime writes it to **fd 1 (stdout)**,
not stderr, and not through the QNN logger -- so neither a ``logging`` filter,
the QNN ``LogLevel`` setting, nor a stderr-only redirect can suppress it. The
only lever that works is filtering the file descriptor(s) themselves.

Design
------
We redirect fd 1 and fd 2 through pipes and run a small daemon thread per fd
that forwards every line to the *real* stream EXCEPT the benign queue-close
line. Everything else (normal program output, tracebacks, real QNN errors,
other native output) is passed through byte-for-byte unchanged.

Windows console caveat
----------------------
In a real console, CPython's ``sys.stdout`` / ``sys.stderr`` are
``_WindowsConsoleIO`` objects that call ``WriteConsole`` on fd 1/2. Once we
replace those fds with a pipe, ``WriteConsole`` on the pipe fails with
``OSError(22, 'Incorrect function')`` and Python reports ``lost sys.stderr``.
To avoid that, after swapping the fds we REBIND ``sys.stdout``/``sys.stderr``
to plain :class:`io.FileIO`-backed text streams over the same fd numbers. Plain
FileIO uses ``WriteFile`` (not ``WriteConsole``), so it works over a pipe. The
pump thread forwards the bytes to the real console handle unchanged, so on-screen
output looks exactly as before -- just without the benign line.

Install is idempotent and opt-out:
  * :func:`install` is called automatically when ``qai_appbuilder.qnncontext``
    is imported.
  * Set env ``QAI_KEEP_QUEUE_WARNING=1`` to disable filtering entirely (e.g.
    when debugging teardown, or if console output ever misbehaves).
"""

import io
import os
import re
import sys
import atexit
import threading

# File descriptors to filter. The benign line is emitted on fd 1 (stdout) on
# WoS; fd 2 (stderr) is filtered too so the same benign line is dropped
# regardless of which stream a given runtime build writes it to.
_FILTERED_FDS = ((1, "stdout"), (2, "stderr"))

# Benign teardown line to drop. Kept specific so we never hide a real error.
_DROP_PATTERN = re.compile(rb"Error 0x200: failed to close queue")

_installed = False
_lock = threading.Lock()

# Keep strong references so the replaced console stream objects are not garbage
# collected (their __del__ would flush WriteConsole onto the pipe and crash).
_kept_old_streams = []
_kept_new_streams = []


def _env_true(name: str, default: str = "0") -> bool:
    return str(os.environ.get(name, default) or default).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _stream_fd(stream):
    """Return stream.fileno() or None if it has none (e.g. StringIO)."""
    try:
        return stream.fileno()
    except Exception:
        return None


def _write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data* to *fd*, tolerating partial writes."""
    view = memoryview(data)
    while view:
        try:
            n = os.write(fd, view)
        except OSError:
            return
        view = view[n:]


def _pump(read_fd: int, real_fd: int) -> None:
    """Forward lines from *read_fd* to *real_fd*, dropping the benign line."""
    buf = b""
    while True:
        try:
            chunk = os.read(read_fd, 65536)
        except OSError:
            break
        if not chunk:  # write end closed -> drain and stop
            break
        buf += chunk
        # Emit complete lines; keep any trailing partial line buffered.
        while True:
            nl = buf.find(b"\n")
            if nl == -1:
                break
            line = buf[: nl + 1]
            buf = buf[nl + 1:]
            if not _DROP_PATTERN.search(line):
                _write_all(real_fd, line)
    if buf and not _DROP_PATTERN.search(buf):
        _write_all(real_fd, buf)


def _rebind_stream(name: str, fd: int):
    """Rebind sys.<name> to a plain FileIO text stream over *fd*.

    Only rebinds when the current stream is actually backed by this fd (a real
    console/file), so a caller that redirected sys.stdout to e.g. StringIO is
    left untouched. Returns the old stream (to keep alive) or None.
    """
    old = getattr(sys, name, None)
    if old is None or _stream_fd(old) != fd:
        return None
    enc = getattr(old, "encoding", None) or "utf-8"
    err = getattr(old, "errors", None) or "backslashreplace"
    try:
        raw = io.FileIO(fd, mode="w", closefd=False)
        new = io.TextIOWrapper(
            io.BufferedWriter(raw),
            encoding=enc,
            errors=err,
            line_buffering=True,
        )
        setattr(sys, name, new)
        _kept_new_streams.append(new)
        return old
    except Exception:
        return None


def install() -> bool:
    """Route fd 1 & fd 2 through a benign-line filter. Idempotent."""
    global _installed
    if _env_true("QAI_KEEP_QUEUE_WARNING"):
        return False
    with _lock:
        if _installed:
            return True

        # Flush any buffered Python output to the REAL streams first, while the
        # fds still point at the original console/file.
        for _fd, name in _FILTERED_FDS:
            s = getattr(sys, name, None)
            if s is not None:
                try:
                    s.flush()
                except Exception:
                    pass

        # Per-fd state: (fd, name, pump_thread, saved_real_fd).
        filters = []
        for fd, name in _FILTERED_FDS:
            try:
                real_fd = os.dup(fd)           # keep the true stream handle
                read_fd, write_fd = os.pipe()
                os.dup2(write_fd, fd)           # all writes to fd now flow to pipe
                os.close(write_fd)
            except OSError:
                # Non-standard stream (closed/redirected in an embedding): skip.
                continue
            thread = threading.Thread(
                target=_pump, args=(read_fd, real_fd),
                name=f"qai-stdio-filter-fd{fd}", daemon=True,
            )
            thread.start()
            filters.append((fd, name, thread, real_fd))

        if not filters:
            return False

        # Rebind Python's console streams to plain FileIO over the same fds so
        # Python writes go through the pipe via WriteFile (never WriteConsole on
        # the pipe, which is the OSError(22) crash). The old console objects are
        # kept alive so their destructors never fire against the pipe.
        for fd, name, _thread, _real_fd in filters:
            old = _rebind_stream(name, fd)
            if old is not None:
                _kept_old_streams.append(old)

        def _restore() -> None:
            # Called from an atexit handler, i.e. still *before* the interpreter
            # finalizes and before native C++ static destructors run. Those
            # destructors can emit the benign "failed to close queue" line AFTER
            # every Python atexit handler.
            #
            # We must NOT point the fds back at the real terminal here (a late
            # native line would then print to the restored terminal). Instead:
            #   1. Flush Python's (rebound) streams into the pipes so the live
            #      pump threads forward them (filtered) to the real stream.
            #   2. Redirect each fd to os.devnull. That also drops the last
            #      reference to each pipe's write end, giving the pump threads
            #      EOF so they drain and exit promptly.
            #   3. From now on every write to these fds -- including any late
            #      native teardown line -- goes to devnull and is discarded.
            for fd, name, _thread, _real_fd in filters:
                s = getattr(sys, name, None)
                if s is not None:
                    try:
                        s.flush()
                    except Exception:
                        pass
            for fd, _name, _thread, _real_fd in filters:
                try:
                    devnull_fd = os.open(os.devnull, os.O_WRONLY)
                    os.dup2(devnull_fd, fd)   # EOF to pipe + swallow late writes
                    os.close(devnull_fd)
                except OSError:
                    pass
            for _fd, _name, thread, _real_fd in filters:
                thread.join(timeout=2.0)

        atexit.register(_restore)
        _installed = True
        return True
