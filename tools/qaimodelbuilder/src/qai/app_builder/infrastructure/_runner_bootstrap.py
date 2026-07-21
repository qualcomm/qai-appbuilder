# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Runner bootstrap — installs faulthandler & breadcrumb logging BEFORE
loading the actual model runner.

Why this file exists
====================
Some native crashes (e.g. Windows 0xC0000005 access violation inside a
qai_appbuilder / numpy / Pillow C extension) occur during ``import`` of the
runner.py before the runner has a chance to install ``faulthandler`` itself.
The end result observed by the orchestrator is::

    runner exited with code 3221225477 before emitting 'done'
    (stderr was empty)

By spawning this bootstrap *first* and only then handing off to the real
runner via ``runpy.run_path``, the Python-level fault handler is armed
before any user-side import runs, so a SIGSEGV / 0xC0000005 will produce a
real native + Python traceback on stderr instead of vanishing.

Stdout protection
=================
Native libraries (qai_appbuilder, QNN HTP runtime, etc.) may write to
stdout via C printf / std::cout. These stray bytes would corrupt the
JSON event protocol that runner_protocol uses on stdout.

This bootstrap applies fd-level stdout protection BEFORE any user code runs:

  1. ``os.dup(1)`` saves the *real* stdout fd (the pipe the orchestrator reads).
  2. ``os.dup2(2, 1)`` redirects process-level fd 1 -> stderr, so any native
     library writing to fd 1 (printf/cout) goes to stderr instead.
  3. A Python file object wrapping the saved fd becomes the new ``sys.stdout``.
  4. ``builtins.print`` is redirected to stderr for extra safety.
  5. ``runner_protocol.emit()`` uses ``sys.stdout.write()`` which now writes
     to the saved event fd -- the only path to the orchestrator's pipe.

This means: **Pack runners do NOT need to implement any stdout protection
themselves.** They just ``from runner_protocol import emit, result, done``
and everything works.

Breadcrumb trace
================
We write a "breadcrumb" line to the file pointed at by env var
``QAI_RUNNER_TRACE_LOG`` (one per stage). If the child process dies between
breadcrumbs, the orchestrator can read the file and tell the user the last
stage that was reached.

Contract
========
- One-shot mode: ``argv[1]``: absolute path to the real runner.py to execute.
- Persistent mode: ``argv[1]`` = ``--persistent`` (no runner path; runner is
  specified in the ``load`` command's ``runnerPath`` field).
- ``QAI_RUNNER_TRACE_LOG`` (optional): absolute path of breadcrumb file.
- stdout is protected: the orchestrator pipe is exclusively used for JSON events.
- stderr is passed through unmodified (log collection by orchestrator).
- Exit code mirrors the child runner's exit code (or 1 on bootstrap error).

Path layout (v2)
================
::

    factory/app_builder/
        shared/              <- runner_protocol.py, audio_io.py, etc.
        models/<model_id>/
            runner.py        <- does ``from runner_protocol import emit``

The bootstrap adds ``shared/`` and the runner's own directory to ``sys.path``
before executing the runner so that ``from runner_protocol import ...`` works.
"""
from __future__ import annotations

import builtins
import faulthandler
import os
import runpy
import sys
import time
from dataclasses import dataclass
from typing import Any, Optional

_TRACE_FILE = os.environ.get("QAI_RUNNER_TRACE_LOG") or ""


def _breadcrumb(stage: str, extra: str = "") -> None:
    """Append one timestamped line to the breadcrumb log; never raises."""
    if not _TRACE_FILE:
        return
    try:
        line = f"{time.strftime('%H:%M:%S')} {stage}"
        if extra:
            line += f" | {extra}"
        line += "\n"
        with open(_TRACE_FILE, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
            try:
                os.fsync(fh.fileno())
            except OSError:
                pass
    except OSError:
        pass


def _protect_stdout() -> None:
    """Redirect process-level stdout so only runner_protocol can write events.

    After this function:
      - sys.stdout -> a file object wrapping the REAL stdout pipe (event stream)
      - fd 1 -> points to stderr (so native printf/cout goes to stderr)
      - builtins.print -> writes to stderr

    runner_protocol.emit() uses sys.stdout.write(), which goes to the real pipe.
    """
    try:
        # 1. Save the real stdout fd (the pipe to the orchestrator).
        event_fd = os.dup(sys.stdout.fileno())

        # 2. Redirect fd 1 to stderr fd. After this, any C/C++ code doing
        #    write(1, ...) or printf(...) will output to stderr instead.
        os.dup2(sys.stderr.fileno(), sys.stdout.fileno())

        # 3. Create a Python file object for the saved event fd.
        event_stream = os.fdopen(event_fd, "w", encoding="utf-8", errors="replace")

        # 4. Replace sys.stdout so that runner_protocol.emit() writes to
        #    the event pipe.
        sys.stdout = event_stream

        # 5. Redirect builtins.print to stderr.
        _original_print = builtins.print

        def _print_to_stderr(*args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("file", sys.stderr)
            _original_print(*args, **kwargs)

        builtins.print = _print_to_stderr

    except (OSError, AttributeError) as e:
        _breadcrumb("stdout_protect_fallback", repr(e))
        _original_print = builtins.print

        def _print_to_stderr_fallback(*args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("file", sys.stderr)
            _original_print(*args, **kwargs)

        builtins.print = _print_to_stderr_fallback


def _install_logging() -> None:
    """Configure root logging handler so runner log calls surface to stderr.

    The orchestrator captures the runner's stderr stream and forwards every
    line to the model panel's "Logs" tab. Without a configured root handler,
    logging calls below WARNING are silently dropped.
    """
    try:
        import logging

        level_name = (os.environ.get("QAI_RUNNER_LOG_LEVEL") or "INFO").upper()
        level = getattr(logging, level_name, logging.INFO)
        if not isinstance(level, int):
            level = logging.INFO

        root = logging.getLogger()
        if not root.handlers:
            handler = logging.StreamHandler(stream=sys.stderr)
            handler.setFormatter(
                logging.Formatter("%(levelname)s [%(name)s] %(message)s")
            )
            root.addHandler(handler)
        if root.level == logging.WARNING or root.level == 0:
            root.setLevel(level)
    except Exception as _exc:  # noqa: BLE001
        _breadcrumb("logging_install_failed", repr(_exc)[:200])


def _resolve_shared_dir(runner_path: str) -> Optional[str]:
    """Find the shared/ directory relative to a runner script.

    Layout: ``<pack_root>/models/<id>/runner.py``
    shared:  ``<pack_root>/shared/``

    Returns the absolute path to shared/ if found, else None.
    """
    pack_dir = os.path.dirname(os.path.abspath(runner_path))
    # Try: pack_dir/../../shared (runner in models/<id>/)
    candidate = os.path.normpath(os.path.join(pack_dir, "..", "..", "shared"))
    if os.path.isdir(candidate):
        return candidate
    # Try: pack_dir/../shared (runner directly in models/)
    candidate = os.path.normpath(os.path.join(pack_dir, "..", "shared"))
    if os.path.isdir(candidate):
        return candidate
    return None


def _ensure_paths_for_runner(runner_path: str) -> None:
    """Add the runner's directory and shared/ to sys.path.

    This is the critical fix: without this, ``from runner_protocol import emit``
    fails with ModuleNotFoundError because shared/ is not on the import path.
    """
    pack_dir = os.path.dirname(os.path.abspath(runner_path))
    if pack_dir not in sys.path:
        sys.path.insert(0, pack_dir)

    shared_dir = _resolve_shared_dir(runner_path)
    if shared_dir and shared_dir not in sys.path:
        sys.path.insert(0, shared_dir)
        _breadcrumb("shared_dir_added", shared_dir)


# ---------------------------------------------------------------------------
# Persistent worker mode
# ---------------------------------------------------------------------------

def _persistent_main() -> int:
    """Persistent worker mode: shared process RPC loop on stdin/stdout.

    The worker process is started ONCE without any runner module pre-loaded.
    The runner module is dynamically imported when the first ``load`` command
    arrives (which includes a ``runnerPath`` field). On model switch the old
    runner's ``release_model()`` is called, the old module is unloaded, and
    the new runner module is imported -- all without restarting the process.

    Protocol:
      - stdout: {"type":"status","state":"worker_ready"} on startup
      - stdin reads JSON commands line by line:
        - {"op":"load", "runnerPath":"...", ...} -> imports runner, calls
          mod.load_model(cmd), responds model_loaded
        - {"op":"run", ...}  -> calls mod.run_inference(ctx, cmd)
        - {"op":"cancel","runId":"..."} -> sets cancel event
        - {"op":"ping"}      -> responds with pong
        - {"op":"release","modelId":"..."} -> releases one model
        - {"op":"shutdown"}  -> releases all, exits
      - stdout: NDJSON events (same protocol as one-shot mode)
    """
    import importlib.util
    import json
    import queue
    import threading
    import time as _time

    _breadcrumb("persistent_mode_start", "shared_worker")

    # Quiet shutdown on Ctrl+C / console close. The parent (StickyWorkerHost)
    # runs this worker inside a console process group, so a normal server
    # shutdown (Ctrl+C or window close) delivers CTRL_C_EVENT / CTRL_BREAK_EVENT
    # here too. Without a handler the interpreter dies via CONTROL_C_EXIT and the
    # armed faulthandler dumps every thread's stack + the loaded C-extension list
    # to stderr — pure noise on an intended shutdown. Install a handler that
    # exits immediately and cleanly instead. This does NOT weaken crash
    # diagnostics: faulthandler stays armed for genuine fatal faults (segfault /
    # abort in native QNN/HTP DLLs), which are what it exists to capture.
    import signal as _signal

    def _quiet_shutdown(_signum, _frame):  # noqa: ANN001
        _breadcrumb("persistent_signal_shutdown", str(_signum))
        os._exit(0)

    for _sig_name in ("SIGINT", "SIGBREAK", "SIGTERM"):
        _sig = getattr(_signal, _sig_name, None)
        if _sig is not None:
            try:
                _signal.signal(_sig, _quiet_shutdown)
            except (ValueError, OSError, RuntimeError):
                # Not all signals are settable on every platform / thread.
                pass

    # Stdin reader thread
    cmd_queue: queue.Queue = queue.Queue()
    stdin_eof = threading.Event()

    def _stdin_reader() -> None:
        try:
            for raw_line in sys.stdin:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    parsed = json.loads(raw_line)
                except json.JSONDecodeError as e:
                    sys.stderr.write(f"[bootstrap] invalid JSON on stdin: {e}\n")
                    continue
                cmd_queue.put(parsed)
        except (EOFError, OSError):
            pass
        finally:
            stdin_eof.set()
            cmd_queue.put(None)

    reader_t = threading.Thread(target=_stdin_reader, name="stdin-reader", daemon=True)
    reader_t.start()

    # Signal ready immediately — pre-import warmup runs in background thread
    # so it does not block host from completing startup.
    sys.stdout.write('{"type":"status","state":"worker_ready"}\n')
    sys.stdout.flush()
    _breadcrumb("persistent_worker_ready")

    # Pre-import common libraries in a background thread to warm the cache.
    # This does NOT block the main loop — the worker can accept commands
    # immediately; first run may be slightly slower if warmup hasn't finished.
    def _background_warmup() -> None:
        _preimport_libs = [
            "numpy", "numpy.core", "numpy.linalg",
            "json", "pathlib", "threading", "time",
            "re", "os", "sys", "struct", "wave",
            "collections", "functools", "itertools",
        ]
        for _lib in _preimport_libs:
            try:
                __import__(_lib)
            except (ImportError, OSError):
                pass

        _preimport_heavy = [
            "nltk",
            "jieba",
            "jieba.posseg",
            "pypinyin",
            "pypinyin.core",
            "PIL", "PIL.Image",
        ]
        for _lib in _preimport_heavy:
            try:
                __import__(_lib)
            except (ImportError, OSError):
                pass
        _breadcrumb("background_warmup_done")

    warmup_t = threading.Thread(
        target=_background_warmup, name="lib-warmup", daemon=True,
    )
    warmup_t.start()

    # Multi-model support
    _multimodel_env = (os.environ.get("QAI_STICKY_MULTIMODEL") or "").strip().lower()
    multimodel_enabled = _multimodel_env not in ("0", "false", "no", "off")
    _breadcrumb("persistent_multimodel", "on" if multimodel_enabled else "off")

    @dataclass
    class _LoadedModel:
        model_id: str
        mod: Any
        model_ctx: Any
        runner_path: str
        pack_dir: str
        sys_module_key: str
        last_used_at: float

    loaded_models: dict[str, _LoadedModel] = {}
    active_model_id: Optional[str] = None
    cancel_events: dict[str, threading.Event] = {}
    stdout_lock = threading.Lock()

    def _emit(msg: str) -> None:
        with stdout_lock:
            sys.stdout.write(msg + "\n")
            sys.stdout.flush()

    def _safe_module_key(model_id: str) -> str:
        slug = "".join(c if (c.isalnum() or c == "_") else "_" for c in (model_id or "default"))
        return f"pack_runner_{slug}"

    def _release_one(entry: _LoadedModel, *, drop_sys_path: bool = True) -> None:
        nonlocal active_model_id
        if entry.mod is not None and entry.model_ctx is not None:
            try:
                entry.mod.release_model(entry.model_ctx)
            except Exception as e:
                sys.stderr.write(
                    f"[bootstrap] release_model error for {entry.model_id!r}: {e}\n"
                )
        sys.modules.pop(entry.sys_module_key, None)
        if drop_sys_path and entry.pack_dir and entry.pack_dir in sys.path:
            still_in_use = any(
                e.pack_dir == entry.pack_dir
                for mid, e in loaded_models.items()
                if mid != entry.model_id
            )
            if not still_in_use:
                try:
                    sys.path.remove(entry.pack_dir)
                except ValueError:
                    pass
        if active_model_id == entry.model_id:
            active_model_id = None

        # Reset QNN configuration flag when no models remain loaded,
        # so that the next load re-initializes the HTP runtime.
        remaining = {mid for mid in loaded_models if mid != entry.model_id}
        if not remaining:
            try:
                from qnn_helper import reset_qnn_configured  # type: ignore[import]
                reset_qnn_configured()
                _breadcrumb("qnn_configured_reset", "no models remain")
            except (ImportError, AttributeError):
                pass

    def _load_runner_module_into(model_id: str, runner_path: str) -> Optional[_LoadedModel]:
        """Dynamically import a runner module and register it."""
        nonlocal active_model_id
        sys_key = _safe_module_key(model_id)

        existing = loaded_models.get(model_id)
        if existing is not None and existing.runner_path == runner_path:
            return existing

        if existing is not None and existing.runner_path != runner_path:
            _release_one(existing)
            loaded_models.pop(model_id, None)

        spec = importlib.util.spec_from_file_location(sys_key, runner_path)
        if spec is None or spec.loader is None:
            sys.stderr.write(f"[bootstrap] cannot load runner module: {runner_path}\n")
            return None

        new_mod = importlib.util.module_from_spec(spec)

        pack_dir = os.path.dirname(os.path.abspath(runner_path))
        if pack_dir not in sys.path:
            sys.path.insert(0, pack_dir)

        # Add shared/ to sys.path so runner can import runner_protocol etc.
        #
        # IMPORTANT (runner_protocol shadowing fix): force the Pack ``shared/``
        # dir to the FRONT of sys.path even if it is already present further
        # down. The host process may run with ``PYTHONPATH=src;.`` which makes
        # the host-side ``qai.app_builder.infrastructure`` dir reachable; if
        # that dir (carrying a *different* ``runner_protocol`` package without
        # ``emit`` / ``read_request``) precedes ``shared/`` in sys.path, the
        # runner's ``from runner_protocol import emit`` resolves to the wrong
        # module and the load fails. Moving ``shared/`` to index 0
        # unconditionally guarantees the Pack helper wins.
        shared_dir = _resolve_shared_dir(runner_path)
        if shared_dir:
            try:
                sys.path.remove(shared_dir)
            except ValueError:
                pass
            sys.path.insert(0, shared_dir)
            _breadcrumb("shared_dir_added", shared_dir)
            # If a stale top-level ``runner_protocol`` from a different
            # location was already imported/cached, drop it so the next
            # import re-resolves against the Pack ``shared/`` now at index 0.
            _cached_rp = sys.modules.get("runner_protocol")
            if _cached_rp is not None:
                _cached_file = getattr(_cached_rp, "__file__", "") or ""
                if os.path.normpath(os.path.dirname(_cached_file)) != os.path.normpath(shared_dir):
                    sys.modules.pop("runner_protocol", None)

        try:
            sys.modules[sys_key] = new_mod
            spec.loader.exec_module(new_mod)
        except Exception as e:
            sys.stderr.write(
                f"[bootstrap] failed to load runner module ({model_id}): {e}\n"
            )
            _breadcrumb("persistent_load_failed", repr(e)[:200])
            sys.modules.pop(sys_key, None)
            return None

        for fn_name in ("load_model", "run_inference", "release_model"):
            if not hasattr(new_mod, fn_name):
                sys.stderr.write(
                    f"[bootstrap] runner module missing '{fn_name}' "
                    f"(required for --persistent mode): {runner_path}\n"
                )
                sys.modules.pop(sys_key, None)
                return None

        entry = _LoadedModel(
            model_id=model_id,
            mod=new_mod,
            model_ctx=None,
            runner_path=runner_path,
            pack_dir=pack_dir,
            sys_module_key=sys_key,
            last_used_at=_time.time(),
        )
        loaded_models[model_id] = entry
        _breadcrumb("persistent_module_loaded", f"{model_id} <- {runner_path}")
        return entry

    # Per-run wall-clock timeout (hard cap at bootstrap level)
    _MAX_RUN_WALL_S = float(os.environ.get("QAI_RUN_TIMEOUT_S") or "600")

    def _run_with_timeout(
        entry: _LoadedModel,
        run_cmd: dict,
        cancel_ev_: threading.Event,
        run_id_: str,
        max_wall_s: float = _MAX_RUN_WALL_S,
    ) -> None:
        """Run inference in a daemon thread with wall-clock timeout."""
        exc_holder: list = []

        def _target() -> None:
            try:
                entry.mod.run_inference(entry.model_ctx, run_cmd)
            except Exception as e:  # noqa: BLE001
                exc_holder.append(e)

        t = threading.Thread(target=_target, name=f"run-{run_id_}", daemon=True)
        t.start()
        t.join(timeout=max_wall_s)

        if t.is_alive():
            # Run exceeded hard cap. Set cancel event and give grace period.
            _breadcrumb("run_timeout_fired", f"run_id={run_id_} wall={max_wall_s:.0f}s")
            if cancel_ev_ is not None:
                cancel_ev_.set()
            t.join(timeout=5.0)
            if t.is_alive():
                raise RuntimeError(
                    f"run_inference exceeded {max_wall_s:.0f}s hard cap and did not "
                    f"respond to cancel within 5s. Worker may need recycling."
                )

        if exc_holder:
            raise exc_holder[0]

    # Main command loop
    try:
        while True:
            cmd = cmd_queue.get()
            if cmd is None:
                break

            op = cmd.get("op")

            if op == "load":
                model_id = cmd.get("modelId") or "default"
                _breadcrumb("persistent_op_load", model_id)
                _emit('{"type":"status","state":"loading"}')

                runner_path = cmd.get("runnerPath")
                if not runner_path:
                    _emit(json.dumps({
                        "type": "error", "code": "LOAD_FAILED",
                        "message": "load command missing 'runnerPath' field",
                        "modelId": model_id,
                    }, ensure_ascii=False))
                    continue

                if not os.path.isfile(runner_path):
                    _emit(json.dumps({
                        "type": "error", "code": "LOAD_FAILED",
                        "message": f"runner script not found: {runner_path}",
                        "modelId": model_id,
                    }, ensure_ascii=False))
                    continue

                if not multimodel_enabled:
                    for other_id in list(loaded_models.keys()):
                        if other_id != model_id:
                            _release_one(loaded_models[other_id])
                            loaded_models.pop(other_id, None)

                existing = loaded_models.get(model_id)
                if (existing is not None
                        and existing.runner_path == runner_path
                        and existing.model_ctx is not None):
                    existing.last_used_at = _time.time()
                    active_model_id = model_id
                    _emit(json.dumps({
                        "type": "status", "state": "model_loaded",
                        "modelId": model_id, "cached": True,
                    }, ensure_ascii=False))
                    continue

                entry = _load_runner_module_into(model_id, runner_path)
                if entry is None:
                    _emit(json.dumps({
                        "type": "error", "code": "LOAD_FAILED",
                        "message": f"failed to import runner module: {runner_path}",
                        "modelId": model_id,
                    }, ensure_ascii=False))
                    continue

                try:
                    entry.model_ctx = entry.mod.load_model(cmd)
                    entry.last_used_at = _time.time()
                    active_model_id = model_id
                    _emit(json.dumps({
                        "type": "status", "state": "model_loaded",
                        "modelId": model_id,
                    }, ensure_ascii=False))
                    _breadcrumb("persistent_model_loaded", model_id)
                except Exception as e:
                    _emit(json.dumps({
                        "type": "error", "code": "LOAD_FAILED",
                        "message": str(e), "modelId": model_id,
                    }, ensure_ascii=False))
                    _breadcrumb("persistent_load_error", repr(e)[:200])
                    entry.model_ctx = None
                    _release_one(entry)
                    loaded_models.pop(model_id, None)

            elif op == "run":
                run_id = cmd.get("runId", "unknown")
                _breadcrumb("persistent_op_run", run_id)

                target_model_id = cmd.get("modelId") or active_model_id
                _breadcrumb("persistent_op_run_start", f"{run_id} model={target_model_id}")
                target_entry = loaded_models.get(target_model_id) if target_model_id else None

                if target_entry is None or target_entry.model_ctx is None:
                    _emit(json.dumps({
                        "type": "error", "code": "NO_MODEL_LOADED",
                        "message": (
                            f"run command for modelId={target_model_id!r} but no "
                            f"matching model is loaded "
                            f"(loaded: {sorted(loaded_models.keys())})"
                        ),
                        "runId": run_id, "modelId": target_model_id,
                    }, ensure_ascii=False))
                    continue

                active_model_id = target_entry.model_id
                target_entry.last_used_at = _time.time()

                cancel_ev = threading.Event()
                cancel_events[run_id] = cancel_ev
                cmd["_cancel_event"] = cancel_ev

                run_done_flag = threading.Event()
                deferred_cmds: list = []

                def _drain_during_run() -> None:
                    while not run_done_flag.is_set():
                        try:
                            bg_cmd = cmd_queue.get(timeout=0.5)
                        except queue.Empty:
                            continue
                        if bg_cmd is None:
                            cmd_queue.put(None)
                            break
                        bg_op = bg_cmd.get("op")
                        if bg_op == "cancel":
                            crid = bg_cmd.get("runId")
                            ev = cancel_events.get(crid) if crid else None
                            if ev:
                                ev.set()
                            _emit(json.dumps({
                                "type": "status", "state": "cancel_ack", "runId": crid,
                            }))
                        elif bg_op == "ping":
                            _emit(json.dumps({"type": "pong", "ts": _time.time()}))
                        elif bg_op == "shutdown":
                            cancel_ev.set()
                            deferred_cmds.append(bg_cmd)
                            break
                        else:
                            deferred_cmds.append(bg_cmd)

                drain_t = threading.Thread(
                    target=_drain_during_run, name="cmd-drain", daemon=True
                )
                drain_t.start()

                try:
                    _run_with_timeout(target_entry, cmd, cancel_ev, run_id)
                except Exception as e:
                    code = getattr(e, "code", None) or "INFER_ERROR"
                    _emit(json.dumps({
                        "type": "error", "code": code,
                        "message": str(e), "runId": run_id,
                        "modelId": target_entry.model_id,
                    }, ensure_ascii=False))
                    _breadcrumb("persistent_run_error", repr(e)[:100])
                else:
                    _emit(json.dumps({
                        "type": "done", "runId": run_id,
                        "modelId": target_entry.model_id,
                    }, ensure_ascii=False))
                finally:
                    cancel_events.pop(run_id, None)
                    run_done_flag.set()
                    drain_t.join(timeout=2.0)
                    target_entry.last_used_at = _time.time()

                _should_exit = False
                for pending in deferred_cmds:
                    p_op = pending.get("op")
                    if p_op == "shutdown":
                        _breadcrumb("persistent_op_shutdown")
                        for mid in list(loaded_models.keys()):
                            _release_one(loaded_models[mid], drop_sys_path=False)
                            loaded_models.pop(mid, None)
                        active_model_id = None
                        _emit('{"type":"status","state":"shutting_down"}')
                        _should_exit = True
                        break
                    else:
                        # Re-queue deferred commands (run, load, release, etc.)
                        # so they are processed in the next iteration.
                        cmd_queue.put(pending)
                if _should_exit:
                    break

            elif op == "release":
                target_id = cmd.get("modelId")
                _breadcrumb("persistent_op_release", target_id or "?")
                if not target_id:
                    _emit(json.dumps({
                        "type": "error", "code": "RELEASE_FAILED",
                        "message": "release command missing 'modelId' field",
                    }, ensure_ascii=False))
                    continue
                entry = loaded_models.pop(target_id, None)
                if entry is None:
                    _emit(json.dumps({
                        "type": "status", "state": "model_released",
                        "modelId": target_id, "noop": True,
                    }, ensure_ascii=False))
                    continue
                _release_one(entry)
                _emit(json.dumps({
                    "type": "status", "state": "model_released",
                    "modelId": target_id,
                }, ensure_ascii=False))

            elif op == "list_loaded":
                snapshot = [
                    {
                        "modelId": mid,
                        "lastUsedAt": entry.last_used_at,
                        "ageS": max(0.0, _time.time() - entry.last_used_at),
                        "active": (mid == active_model_id),
                    }
                    for mid, entry in loaded_models.items()
                ]
                _emit(json.dumps({
                    "type": "status", "state": "loaded_models",
                    "models": snapshot, "multimodel": multimodel_enabled,
                }, ensure_ascii=False))

            elif op == "cancel":
                cancel_run_id = cmd.get("runId")
                _breadcrumb("persistent_op_cancel", cancel_run_id or "?")
                ev = cancel_events.get(cancel_run_id) if cancel_run_id else None
                if ev:
                    ev.set()
                _emit(json.dumps({
                    "type": "status", "state": "cancel_ack", "runId": cancel_run_id,
                }))

            elif op == "ping":
                _emit(json.dumps({"type": "pong", "ts": _time.time()}))

            elif op == "shutdown":
                _breadcrumb("persistent_op_shutdown")
                for mid in list(loaded_models.keys()):
                    _release_one(loaded_models[mid], drop_sys_path=False)
                    loaded_models.pop(mid, None)
                active_model_id = None
                _emit('{"type":"status","state":"shutting_down"}')
                break

            else:
                sys.stderr.write(f"[bootstrap] unknown op: {op!r}\n")

    except (EOFError, KeyboardInterrupt):
        _breadcrumb("persistent_stdin_closed")
    except Exception as e:
        _breadcrumb("persistent_loop_error", repr(e)[:200])
        sys.stderr.write(f"[bootstrap] RPC loop error: {e}\n")
        return 1

    _breadcrumb("persistent_exit_clean")
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    """Bootstrap entry point. Arms faulthandler, protects stdout, then
    hands off to either persistent mode or one-shot runpy execution."""
    # 1) Arm faulthandler before any user code runs.
    try:
        faulthandler.enable(file=sys.stderr, all_threads=True)
    except (RuntimeError, OSError) as e:
        _breadcrumb("bootstrap_faulthandler_failed", repr(e))

    _breadcrumb(
        "bootstrap_start",
        f"py={sys.version.split()[0]} arch={sys.platform} pid={os.getpid()}",
    )

    if len(sys.argv) < 2:
        sys.stderr.write(
            "[bootstrap] missing runner script path or --persistent flag\n"
        )
        return 2

    # 2) Protect stdout BEFORE loading ANY user code.
    _protect_stdout()
    _breadcrumb("stdout_protected")

    # 2b) Install default logging handler.
    _install_logging()
    _breadcrumb("logging_installed")

    # Persistent worker mode
    if sys.argv[1] == "--persistent":
        return _persistent_main()

    # One-shot mode: argv[1] is the runner script path
    runner_path = sys.argv[1]
    if not os.path.isfile(runner_path):
        sys.stderr.write(f"[bootstrap] runner not found: {runner_path}\n")
        return 2

    # 3) Add runner dir + shared/ to sys.path so imports work.
    _ensure_paths_for_runner(runner_path)
    _breadcrumb("about_to_runpy", runner_path)

    # 4) Hand off control via runpy.
    sys.argv = [runner_path] + sys.argv[2:]

    try:
        runpy.run_path(runner_path, run_name="__main__")
    except SystemExit as se:
        rc = se.code if isinstance(se.code, int) else (0 if se.code is None else 1)
        _breadcrumb("runner_exited", f"rc={rc}")
        return rc
    except BaseException as e:  # noqa: BLE001
        _breadcrumb("runner_uncaught", repr(e)[:200])
        raise

    _breadcrumb("runner_returned_normally")
    return 0


if __name__ == "__main__":
    sys.exit(main())
