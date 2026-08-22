# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""Protected-paths child-process hook (auto-imported as ``sitecustomize``).

This file is injected into Python child processes via ``PYTHONPATH`` by the
``exec`` tool's env builder. When a child interpreter starts, Python
automatically imports ``sitecustomize`` from the first ``PYTHONPATH`` entry,
loading this module before any pipeline / user code runs.

What it does:
  Installs lightweight hooks that DENY writes (create / modify / delete /
  truncate) to any path under a protected prefix supplied via the
  ``QAI_PROTECTED_PATHS`` environment variable (``os.pathsep``-separated). This
  is the child-process arm of ``qai.platform.protected_paths`` — it stops a
  child like the x86_64 model-builder Python from truncating the QAIRT SDK
  generator (the 2026-06-16 incident), even when FileGuard and the OS sandbox
  are both disabled.

Why a SEPARATE hook (not the FileGuard sitecustomize):
  This guard is **independent of FileGuard** and ALWAYS active whenever
  ``QAI_PROTECTED_PATHS`` is set, regardless of any security settings. It is a
  deny-list (only the protected prefixes are blocked; everything else is
  allowed), so it is safe to run unconditionally without breaking ordinary
  pipeline writes.

Hard design constraints (same as any sitecustomize):
  * NEVER raise an uncaught exception / NEVER ``sys.exit`` at import time — that
    would break EVERY Python child process.
  * stdlib-only; no ``qai.*`` import (runs in an isolated child interpreter).
  * Idempotent (guarded by an env marker).
  * A blocked write raises ``PermissionError`` from the hooked call site (this
    is how the write is actually prevented), with a clear message.
  * BEFORE raising, a structured ``[[QAI_PROTECTED_DENY]] {json}`` line is
    written to ``sys.stderr`` (best-effort, ZERO file IO) so the parent ``exec``
    handler can relay the deny into the audit funnel (P-08 #6, design A). A
    marker-write failure NEVER stops the deny — the interception semantic is
    independent of the audit side.
"""

import builtins
import os
import sys

_MARKER = "QAI_PROTECTED_PATHS_INSTALLED"


def _warn(msg: str) -> None:
    try:
        sys.stderr.write("[ProtectedPaths] " + msg + "\n")
        sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


try:
    if os.environ.get(_MARKER, "").strip() == "1":
        pass  # already installed in this process tree
    else:
        _raw = os.environ.get("QAI_PROTECTED_PATHS", "")
        _prefixes = [p for p in _raw.split(os.pathsep) if p.strip()]

        if _prefixes:
            # Pre-normalize the protected prefixes (normcase + abspath + 8.3
            # short-name + symlink/junction resolution + \\?\ strip). Mirrors
            # qai.platform.protected_paths._normalize (cannot import qai here).
            def _strip_ext(_p):
                if _p.startswith("\\\\?\\UNC\\"):
                    return "\\\\" + _p[len("\\\\?\\UNC\\"):]
                if _p.startswith("\\\\?\\") or _p.startswith("\\\\.\\"):
                    return _p[4:]
                return _p

            def _norm(_p):
                if isinstance(_p, bytes):
                    try:
                        _p = _p.decode("utf-8", "surrogateescape")
                    except Exception:  # noqa: BLE001
                        return None
                try:
                    _a = os.path.abspath(_p)
                except (OSError, ValueError):
                    return None
                if sys.platform == "win32":
                    _a = _strip_ext(_a)
                    try:
                        _a = os.path.realpath(_a)  # follows junction/symlink
                    except (OSError, ValueError):
                        pass
                    try:
                        import ctypes

                        _buf = ctypes.create_unicode_buffer(1024)
                        _r = ctypes.windll.kernel32.GetLongPathNameW(  # type: ignore[attr-defined]
                            _a, _buf, 1024
                        )
                        if _r and _r < 1024:
                            _a = _buf.value
                    except (OSError, AttributeError, ValueError):
                        pass
                    _a = _strip_ext(_a)
                return os.path.normcase(_a)

            _NORM_PREFIXES = tuple(
                _n for _n in (_norm(_p) for _p in _prefixes) if _n
            )

            def _is_blocked(_path):
                if _path is None:
                    return None
                # Narrow, explicit bypass for app-controlled restores INTO a
                # protected tree (the generator self-heal copies the backup
                # back into C:\Qualcomm). Set ``QAI_PROTECTED_PATHS_BYPASS=1``
                # only around that single copy, then clear it.
                if os.environ.get("QAI_PROTECTED_PATHS_BYPASS", "") == "1":
                    return None
                try:
                    _s = os.fspath(_path)
                except TypeError:
                    return None
                # _norm handles bytes; do NOT reject bytes paths here (os.open /
                # os.remove accept bytes — rejecting them would be a bypass).
                _n = _norm(_s)
                if _n is None:
                    return None
                for _pref in _NORM_PREFIXES:
                    if _n == _pref or _n.startswith(_pref + os.sep):
                        return _pref
                return None

            # Cross-process audit marker (design A, P-08 #6). Kept as a literal
            # here because this module is stdlib-only and CANNOT import
            # ``qai.platform.child_process_deny_audit`` (it runs in an isolated
            # child interpreter). MUST stay byte-identical to that module's
            # ``MARKER_PREFIX`` (a round-trip test guards the pair).
            _DENY_MARKER = "[[QAI_PROTECTED_DENY]]"

            def _emit_deny_marker(_path, _pref, _op):
                # Best-effort ONLY: write one structured line to stderr so the
                # parent ``exec`` handler can record a cross-process audit row.
                # ZERO file IO (avoids re-entering our own open/write hooks +
                # the audit backstop → recursion). NEVER let a marker-write
                # failure stop the deny: swallow everything.
                try:
                    import json as _json

                    _payload = _json.dumps(
                        {
                            "op": _op or "write",
                            "path": str(_path),
                            "prefix": str(_pref),
                        },
                        ensure_ascii=True,
                    )
                    sys.stderr.write("\n" + _DENY_MARKER + " " + _payload + "\n")
                    sys.stderr.flush()
                except Exception:  # noqa: BLE001
                    pass

            def _deny(_path, _pref, _op="write"):
                _emit_deny_marker(_path, _pref, _op)
                raise PermissionError(
                    "ProtectedPaths: refusing to write protected path %r "
                    "(under %r). This tree (e.g. the QAIRT SDK / C:\\Qualcomm) "
                    "must not be modified — doing so corrupts the shared "
                    "toolchain (e.g. truncating an .exe to 0 bytes, causing "
                    "[WinError 193]). Use the data/sdk backup instead."
                    % (str(_path)[:160], _pref)
                )

            # ── builtins.open (write modes) ──────────────────────────────
            _orig_open = builtins.open

            def _guarded_open(file, mode="r", *args, **kwargs):
                try:
                    _is_write = any(c in mode for c in ("w", "a", "x", "+"))
                except TypeError:
                    _is_write = False
                if _is_write:
                    _pref = _is_blocked(file)
                    if _pref:
                        _deny(file, _pref, "write")
                return _orig_open(file, mode, *args, **kwargs)

            try:
                builtins.open = _guarded_open  # type: ignore[assignment]
            except Exception as _e:  # noqa: BLE001
                _warn("open hook failed: %r" % _e)

            # ── os write ops ─────────────────────────────────────────────
            # IMPORTANT (py3.10 pathlib compat): the wrapper MUST be a callable
            # INSTANCE, not a plain ``def`` function. ``pathlib._NormalAccessor``
            # (Python <= 3.11) binds ``mkdir = os.mkdir`` (and rename/replace/
            # unlink/rmdir) as CLASS ATTRIBUTES at import time. A plain function
            # placed there is treated as a descriptor and bound as an instance
            # method, so ``self._accessor.mkdir(self, path, mode)`` injects an
            # extra ``self`` → "mkdir() takes at most 2 positional arguments
            # (3 given)". A class instance has no ``__get__``, so it is NOT
            # bound — it behaves like the original builtin. (3.12+ dropped
            # ``_NormalAccessor`` so this is harmless there too.)
            class _OsPathArgGuard:
                def __init__(self, orig, argidx, op="write"):
                    self._orig = orig
                    self._argidx = argidx
                    self._op = op

                def __call__(self, *a, **k):
                    _target = (
                        a[self._argidx]
                        if len(a) > self._argidx
                        else k.get("path", k.get("name"))
                    )
                    if _target is not None:
                        _pref = _is_blocked(_target)
                        if _pref:
                            _deny(_target, _pref, self._op)
                    return self._orig(*a, **k)

            class _OsDstGuard:
                # rename/replace: block when DESTINATION or SOURCE is protected.
                def __init__(self, orig, dstidx, op="write"):
                    self._orig = orig
                    self._dstidx = dstidx
                    self._op = op

                def __call__(self, *a, **k):
                    _dst = a[self._dstidx] if len(a) > self._dstidx else k.get("dst")
                    _src = a[0] if len(a) > 0 else k.get("src")
                    for _t in (_dst, _src):
                        if _t is not None:
                            _pref = _is_blocked(_t)
                            if _pref:
                                _deny(_t, _pref, self._op)
                    return self._orig(*a, **k)

            def _wrap_os_path_arg(_name, _argidx=0, _op="write"):
                _orig = getattr(os, _name, None)
                if _orig is None:
                    return
                try:
                    setattr(os, _name, _OsPathArgGuard(_orig, _argidx, _op))
                except Exception as _e:  # noqa: BLE001
                    _warn("os.%s hook failed: %r" % (_name, _e))

            def _wrap_os_dst(_name, _dstidx=1, _op="write"):
                _orig = getattr(os, _name, None)
                if _orig is None:
                    return
                try:
                    setattr(os, _name, _OsDstGuard(_orig, _dstidx, _op))
                except Exception as _e:  # noqa: BLE001
                    _warn("os.%s hook failed: %r" % (_name, _e))

            # delete-intent ops record ``delete``; everything else ``write``
            # (matches AuditBypassSink._op_from_event's vocabulary).
            for _n in ("remove", "unlink"):
                _wrap_os_path_arg(_n, 0, "delete")
            for _n in ("mkdir", "makedirs", "rmdir", "truncate"):
                _wrap_os_path_arg(_n, 0, "write")
            for _n in ("rename", "replace"):
                _wrap_os_dst(_n, 1, "write")

            # ── shutil write ops ─────────────────────────────────────────
            try:
                import shutil

                class _ShutilDstGuard:
                    def __init__(self, orig, dstidx, op="write"):
                        self._orig = orig
                        self._dstidx = dstidx
                        self._op = op

                    def __call__(self, *a, **k):
                        if len(a) > self._dstidx:
                            _pref = _is_blocked(a[self._dstidx])
                            if _pref:
                                _deny(a[self._dstidx], _pref, self._op)
                        return self._orig(*a, **k)

                class _ShutilSrcGuard:
                    # rmtree(path): block when the tree being removed is protected.
                    def __init__(self, orig, srcidx, op="delete"):
                        self._orig = orig
                        self._srcidx = srcidx
                        self._op = op

                    def __call__(self, *a, **k):
                        if len(a) > self._srcidx:
                            _pref = _is_blocked(a[self._srcidx])
                            if _pref:
                                _deny(a[self._srcidx], _pref, self._op)
                        return self._orig(*a, **k)

                def _wrap_shutil_dst(_name, _dstidx=1, _op="write"):
                    _orig = getattr(shutil, _name, None)
                    if _orig is None:
                        return
                    try:
                        setattr(shutil, _name, _ShutilDstGuard(_orig, _dstidx, _op))
                    except Exception as _e:  # noqa: BLE001
                        _warn("shutil.%s hook failed: %r" % (_name, _e))

                def _wrap_shutil_src(_name, _srcidx=0, _op="delete"):
                    # rmtree(path): block when the tree being removed is protected.
                    _orig = getattr(shutil, _name, None)
                    if _orig is None:
                        return
                    try:
                        setattr(shutil, _name, _ShutilSrcGuard(_orig, _srcidx, _op))
                    except Exception as _e:  # noqa: BLE001
                        _warn("shutil.%s hook failed: %r" % (_name, _e))

                for _n in ("copy", "copy2", "copyfile", "move"):
                    _wrap_shutil_dst(_n, 1, "write")
                _wrap_shutil_src("rmtree", 0, "delete")
            except Exception as _e:  # noqa: BLE001
                _warn("shutil hooks failed: %r" % _e)

            # ── PEP 578 audit hook backstop ──────────────────────────────
            # monkeypatching builtins/os/shutil misses writes that go straight
            # to the C layer or through pathlib's io.open (e.g. Path.open("w"),
            # C-extension raw open). A sys.addaudithook on the write events is
            # the parser-independent backstop (mirrors the FileGuard hook +
            # qai.platform.main_process_audit_sentinel). Per-thread re-entrancy guard avoids
            # recursion via the hook's own (non-IO) normalization.
            try:
                import threading as _threading

                _hk_local = _threading.local()
                _WR_FLAGS = (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
                )

                def _mode_is_write(_m):
                    if isinstance(_m, str):
                        return any(_c in _m for _c in ("w", "a", "x", "+"))
                    if isinstance(_m, int):
                        return bool(_m & _WR_FLAGS)
                    return False

                def _hk_check(_path, _event):
                    if getattr(_hk_local, "busy", False):
                        return
                    _hk_local.busy = True
                    try:
                        _pref = _is_blocked(_path)
                        if _pref:
                            # backstop only fires for write-intent open /
                            # os.open / os.truncate → all record ``write``.
                            _deny(_path, _pref, "write")
                    finally:
                        _hk_local.busy = False

                def _audit(_event, _args):
                    try:
                        if _event == "open":
                            if (
                                (len(_args) >= 2 and _mode_is_write(_args[1]))
                                or (len(_args) >= 3 and _mode_is_write(_args[2]))
                            ) and _args:
                                _hk_check(_args[0], _event)
                        elif _event == "os.open":
                            if len(_args) >= 2 and _mode_is_write(_args[1]):
                                _hk_check(_args[0], _event)
                        elif _event == "os.truncate":
                            if _args:
                                _hk_check(_args[0], _event)
                    except PermissionError:
                        raise
                    except Exception:  # noqa: BLE001
                        return

                sys.addaudithook(_audit)
            except Exception as _e:  # noqa: BLE001
                _warn("audit hook backstop failed: %r" % _e)

            os.environ[_MARKER] = "1"
            if os.environ.get("QAI_PROTECTED_PATHS_QUIET", "1") != "1":
                _warn(
                    "installed (%d protected prefixes)" % len(_NORM_PREFIXES)
                )
except Exception as _e:  # noqa: BLE001
    # sitecustomize must NEVER raise — degraded protection is logged, not fatal.
    _warn("install failed, child protection DISABLED: %r" % _e)
