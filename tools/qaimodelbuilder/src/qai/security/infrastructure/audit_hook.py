# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""PEP 578 ``sys.addaudithook`` interpreter-level IO interceptor (PR-502).

This module is the new-architecture replacement for the legacy
``backend/security/audit_hook.py``. It hooks ``sys.audit`` events for a
small, well-known set of IO / subprocess operations and consults the
current security :class:`Policy` synchronously to either allow the call
through or short-circuit it by raising :class:`PermissionError`.

Why a new module
----------------

The legacy module talks to ``backend.security.policy.PolicyCenter``
directly — a class that bundles persistence, broadcast and audit IO.
In the new architecture all of that lives behind the
``qai.security.application.ports.*`` ports and the use cases own the
async I/O. The audit hook, however, runs **synchronously** in any
thread under the interpreter and must NEVER block or schedule async
work on the hot path. We therefore decouple it cleanly:

* The hook receives a **synchronous** ``policy_provider`` callable
  that returns the most recent :class:`Policy` snapshot (typically a
  cached one fed by ``UpdatePolicyUseCase`` after each save).
* The hook makes its decision purely from that snapshot — no async
  port calls inside ``sys.audit`` callbacks.
* Optional ``on_violation`` callback is invoked **before** the
  ``PermissionError`` is raised so the caller can record an audit
  entry through whatever sync surface they have (a thread-safe deque,
  a logging.Handler, a ``run_coroutine_threadsafe`` shim, …). The
  hook never awaits anything itself.

Design constraints (carried over from the legacy implementation):

1. **Hot-path safety / recursion guard**: ``threading.local`` flag
   ``_inside_hook`` short-circuits re-entry caused by the hook's own
   IO (e.g. logging) in the same thread.
2. **Baseline allowlist**: paths under the Python interpreter / site-
   packages / hardcoded extra prefixes pass straight through without
   consulting the Policy. This keeps interpreter import IO fast.
3. **Singleton install**: ``sys.addaudithook`` cannot be unregistered;
   repeated ``install()`` calls return the same handle and refresh
   internal state in place. ``uninstall()`` flips a soft-disable
   flag so the hook callback returns immediately.
4. **PermissionError is the only exception that escapes**: any other
   exception inside the hook is swallowed (we never want a buggy
   hook to crash the interpreter).

Public API
----------

* :func:`install_audit_hook` — register the hook, idempotent
* :class:`AuditHookHandle` — returned by ``install_audit_hook``;
  carries ``installed`` and ``uninstall()``
* ``Decision`` enum — what the hook reports to ``on_violation``

The :class:`AuditHookAdapter` in
``qai.security.adapters.audit_hook`` wraps these into the
:class:`qai.security.application.ports.AuditHookPort` contract.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import os
import site
import subprocess
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from qai.security.domain.entities import Policy
from qai.security.domain.value_objects import PolicyAction, PolicyScope

__all__ = [
    "AuditHookHandle",
    "Decision",
    "configure_extra_events",
    "install_audit_hook",
    "suppress_audit",
    "trusted_read_prefix",
    "trusted_subprocess",
]


_LOGGER = logging.getLogger("qai.security.audit_hook")


class Decision(str, Enum):
    """Outcome reported by the hook for a single audit event.

    The hook only ever raises ``PermissionError`` on :attr:`DENY`; the
    enum exists primarily so the optional ``on_violation`` callback can
    distinguish a deny that came from a matching rule
    (:attr:`DENY_RULE`) from a deny because no rule matched
    (:attr:`DENY_DEFAULT`). External callers should treat both as a
    plain "denied" for UX purposes.
    """

    ALLOW = "allow"
    DENY_RULE = "deny_rule"
    DENY_DEFAULT = "deny_default"


# ---------------------------------------------------------------------------
# Module-level singleton state
# ---------------------------------------------------------------------------
_INSTALL_LOCK = threading.Lock()
_LOCAL = threading.local()

# PR-092 §2.1 C-9 / §17.5 #5 — async-safe audit-bypass context.
# Legacy ``backend/security/audit_hook.py:110-196`` used
# ``threading.local`` slots for ``suppressed`` / ``trusted_reason`` /
# ``trusted_read_prefixes``. asyncio code that hops between tasks would
# leak those flags across unrelated coroutines. We migrate to
# ``contextvars.ContextVar`` so every task / thread / async generator
# carries its own copy automatically.
_SUPPRESSED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "qai_security_audit_suppressed", default=False
)
_TRUSTED_SUBPROCESS_REASON: contextvars.ContextVar[str | None] = (
    contextvars.ContextVar("qai_security_audit_trusted_subprocess", default=None)
)
_TRUSTED_READ_PREFIXES: contextvars.ContextVar[tuple[str, ...]] = (
    contextvars.ContextVar(
        "qai_security_audit_trusted_read_prefixes", default=()
    )
)

# PR-092 §2.1 C-9 — caller allow-list for the audit-bypass context
# managers. Frames whose ``co_filename`` does NOT come from one of
# these module prefixes trigger a warning (legacy behaviour was a
# fail-open warning; we keep the same semantic so legitimate platform
# code can call ``trusted_subprocess()`` without a hard failure).
_CALLER_ALLOWED_PREFIXES: tuple[str, ...] = (
    "qai/security/",
    "qai\\security\\",
    "qai/platform/process/",
    "qai\\platform\\process\\",
    "apps/api/",
    "apps\\api\\",
)
_CALLER_DISALLOWED_TOKENS: tuple[str, ...] = (
    "tool_executor",
    "_tool_exec",
    "_exec.py",
)

# Audit events the hook actively handles. The base set covers the
# CPython events the legacy ``backend/security/audit_hook.py`` already
# intercepted; PR-092 §2.2 H-17 / §17.5 #4 lets the lifespan layer
# extend the set at install time via ``Settings.security.audit_hook_extra_events``
# (typical additions: ``os.scandir``, ``os.listdir``, ``shutil.copyfile``).
_BASE_HANDLED_EVENTS: frozenset[str] = frozenset(
    {
        "open",
        "os.open",
        "os.remove",
        "os.unlink",
        "os.rename",
        "os.replace",
        "subprocess.Popen",
    }
)

# Events the hook is allowed to extend to via ``configure_extra_events``.
# Any event outside this allow-list is ignored to keep the dispatch
# table predictable across releases.
_EXTRA_ALLOWED_EVENTS: frozenset[str] = frozenset(
    {
        "os.scandir",
        "os.listdir",
        "shutil.copyfile",
    }
)

# Mutable runtime view (rebuilt by ``configure_extra_events``).
_HANDLED_EVENTS: frozenset[str] = _BASE_HANDLED_EVENTS

# os.open flag bits that imply a write-mode open.
_WRITE_FLAGS: int = (
    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
)


@dataclass(slots=True)
class _State:
    """Encapsulates the singleton hook state.

    Held as the module-level ``_STATE`` global; not part of the public
    API. Splitting the fields into a dataclass keeps :func:`reset`-style
    helpers tidy and makes the flags discoverable in pdb.
    """

    installed: bool = False
    disabled: bool = False
    policy_provider: Callable[[], Policy] | None = None
    on_violation: Callable[[Decision, str, str], None] | None = None
    baseline_prefixes: tuple[str, ...] = ()


_STATE = _State()


# ---------------------------------------------------------------------------
# Handle returned to the caller
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class AuditHookHandle:
    """Process-wide handle returned by :func:`install_audit_hook`.

    Idempotency is enforced at the module level: every successful
    :func:`install_audit_hook` returns the **same** handle instance for
    the lifetime of the interpreter, so callers can safely pass it
    around without coordinating "who owns the install".

    ``uninstall()`` flips a soft-disable flag inside the module — CPython
    does not allow removing an audit hook once registered, but the
    callback's first line short-circuits when ``_STATE.disabled`` is
    set, achieving the same observable effect.
    """

    @property
    def installed(self) -> bool:
        """``True`` while the hook is actively decision-making.

        Returns ``False`` between :func:`install_audit_hook` failures
        and after :meth:`uninstall`.
        """

        return _STATE.installed and not _STATE.disabled

    def uninstall(self) -> None:
        """Soft-disable the hook (idempotent).

        Subsequent ``sys.audit`` events are passed through without
        consulting the Policy. Callers may re-enable the same handle by
        invoking :func:`install_audit_hook` again with fresh arguments.
        """

        _STATE.disabled = True
        _LOGGER.debug("audit hook soft-disabled (installed=%s)", _STATE.installed)


# A single, shared handle keeps idempotency observable to callers — they
# can compare ``handle is install_audit_hook(...)`` after a no-op install.
_HANDLE = AuditHookHandle()


# ---------------------------------------------------------------------------
# Caller validation helper (PR-092 §2.1 C-9 / S-3 hardening)
# ---------------------------------------------------------------------------
def _validate_caller(name: str) -> None:
    """Warn (but do NOT raise) when the audit-bypass context manager is
    entered from a tool-executor frame (model-controllable code).

    The legacy implementation kept this fail-open for backwards
    compatibility — flipping it to fail-closed would break in-flight
    skill executions that legitimately spawn ``aria2c`` etc. The
    warning is sufficient for the audit trail; PR-092 will revisit
    fail-closed once the platform-side caller catalog is complete.
    """

    frame = sys._getframe(2) if hasattr(sys, "_getframe") else None
    if frame is None:
        return
    try:
        caller_file = frame.f_code.co_filename.replace("\\", "/")
        caller_func = frame.f_code.co_name
        for token in _CALLER_DISALLOWED_TOKENS:
            if token in caller_file or token == caller_func:
                _LOGGER.warning(
                    "audit_hook.%s called from disallowed context: "
                    "file=%s func=%s",
                    name,
                    caller_file,
                    caller_func,
                )
                return
        # Positive allow-list: anything outside ``qai.security.*`` /
        # ``qai.platform.process.*`` / ``apps.api.*`` produces an
        # informational debug log so operators can spot newly added
        # callers and either approve them or refactor them out.
        if not any(p in caller_file for p in _CALLER_ALLOWED_PREFIXES):
            _LOGGER.debug(
                "audit_hook.%s called from non-allowlisted module: %s",
                name,
                caller_file,
            )
    finally:
        del frame


# ---------------------------------------------------------------------------
# Public audit-bypass context managers (PR-092 §2.1 C-3..C-6 / §17.5 #5)
# ---------------------------------------------------------------------------
@contextlib.contextmanager
def suppress_audit() -> Iterator[None]:
    """Inside this context the audit hook unconditionally allows IO.

    Used by the security adapters themselves when they perform their
    own audit-log writes — without the bypass the hook would recurse
    into Policy evaluation while a deny is already in flight.
    Async-safe via :class:`contextvars.ContextVar`.
    """

    _validate_caller("suppress_audit")
    token = _SUPPRESSED.set(True)
    try:
        yield
    finally:
        _SUPPRESSED.reset(token)


@contextlib.contextmanager
def trusted_subprocess(reason: str = "trusted") -> Iterator[None]:
    """Inside this context ``subprocess.Popen`` events bypass the hook.

    Path IO (``open`` / ``os.open`` / ...) is **not** affected — only
    process spawns. Project-internal subprocess invocations (aria2c
    download helper, GenieAPIService.exe launcher, reboot helper,
    ``explorer /select``) are expected to wrap their ``Popen`` call
    in this context manager so the hook does not deny them when the
    Policy disallows model-driven exec.
    """

    _validate_caller("trusted_subprocess")
    token = _TRUSTED_SUBPROCESS_REASON.set(reason or "trusted")
    try:
        yield
    finally:
        _TRUSTED_SUBPROCESS_REASON.reset(token)


@contextlib.contextmanager
def trusted_read_prefix(prefix: str) -> Iterator[None]:
    """Inside this context, READ operations under ``prefix`` are allowed.

    Used by tool functions (glob / grep / read) after the tool layer
    has already obtained user authorisation for a directory tree.
    Without this bypass the hook would trigger redundant ASK dialogs
    for every subdirectory traversed by ``os.scandir`` / ``os.listdir``.
    Nesting is supported (LIFO stack of prefixes).
    """

    _validate_caller("trusted_read_prefix")
    if not prefix:
        yield
        return
    norm = prefix.replace("/", "\\") if sys.platform == "win32" else prefix
    key = norm.casefold().rstrip("\\").rstrip("/")
    current = _TRUSTED_READ_PREFIXES.get()
    token = _TRUSTED_READ_PREFIXES.set(current + (key,))
    try:
        yield
    finally:
        _TRUSTED_READ_PREFIXES.reset(token)


def _is_trusted_read(path_cf: str) -> bool:
    for prefix in _TRUSTED_READ_PREFIXES.get():
        if path_cf.startswith(prefix):
            return True
    return False


def configure_extra_events(extra: tuple[str, ...]) -> None:
    """Extend :data:`_HANDLED_EVENTS` with caller-supplied events.

    Only events listed in :data:`_EXTRA_ALLOWED_EVENTS` are admitted;
    unknown identifiers are silently dropped (the audit dispatcher has
    no per-event handler for them yet, so accepting them would be a
    no-op anyway). Idempotent.
    """

    global _HANDLED_EVENTS
    admitted: set[str] = set(_BASE_HANDLED_EVENTS)
    for ev in extra or ():
        if isinstance(ev, str) and ev in _EXTRA_ALLOWED_EVENTS:
            admitted.add(ev)
    _HANDLED_EVENTS = frozenset(admitted)


# ---------------------------------------------------------------------------
# Recursion guard
# ---------------------------------------------------------------------------
def _enter_hook() -> bool:
    if getattr(_LOCAL, "inside_hook", False):
        return False
    _LOCAL.inside_hook = True
    return True


def _exit_hook() -> None:
    _LOCAL.inside_hook = False


# ---------------------------------------------------------------------------
# Baseline allowlist
# ---------------------------------------------------------------------------
def _collect_baseline_prefixes(extra: tuple[str, ...]) -> tuple[str, ...]:
    """Return casefolded path prefixes that bypass the Policy.

    The interpreter pulls in hundreds of files at startup (stdlib +
    site-packages); routing all of them through ``Policy.evaluate`` is
    both unnecessary and slow. We therefore early-allow anything under
    the well-known runtime locations:

    * ``sys.prefix`` / ``sys.base_prefix`` (the Python install)
    * ``Path(sys.executable).parent`` (the launcher dir on Windows)
    * :func:`site.getsitepackages` and :func:`site.getusersitepackages`
    * any ``extra`` prefixes the caller wants treated as trusted
      (typically the project root)

    The prefixes are casefolded once and stored as a tuple; matching is
    a plain ``startswith`` check on the casefolded raw path string —
    no ``Path()`` construction on the hot path.
    """

    raw: list[str] = []
    for getter in (lambda: sys.prefix, lambda: sys.base_prefix):
        try:
            value = getter()
        except Exception:  # pragma: no cover - hardening
            continue
        if value:
            raw.append(value)
    try:
        raw.append(str(Path(sys.executable).parent))
    except Exception:  # pragma: no cover - hardening
        pass
    try:
        raw.extend(site.getsitepackages())
    except Exception:  # pragma: no cover - hardening
        pass
    try:
        user_site = site.getusersitepackages()
        if user_site:
            raw.append(user_site)
    except Exception:  # pragma: no cover - hardening
        pass

    raw.extend(extra)

    seen: set[str] = set()
    out: list[str] = []
    for entry in raw:
        if not entry:
            continue
        try:
            cleaned = str(Path(entry).absolute()).casefold()
        except Exception:
            cleaned = entry.casefold()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return tuple(out)


def _is_baseline(path: str) -> bool:
    if not path:
        return False
    cf = path.casefold()
    # Windows special device names — they are not real paths.
    basename = cf.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if basename.endswith(":"):
        basename = basename[:-1]
    if basename in _DEVICE_NAMES:
        return True
    for prefix in _STATE.baseline_prefixes:
        if cf.startswith(prefix):
            return True
    return False


_DEVICE_NAMES: frozenset[str] = frozenset(
    {
        "nul",
        "con",
        "aux",
        "prn",
        "com1",
        "com2",
        "com3",
        "com4",
        "com5",
        "com6",
        "com7",
        "com8",
        "com9",
        "lpt1",
        "lpt2",
        "lpt3",
        "lpt4",
        "lpt5",
        "lpt6",
        "lpt7",
        "lpt8",
        "lpt9",
    }
)


# ---------------------------------------------------------------------------
# Argument coercion helpers
# ---------------------------------------------------------------------------
def _coerce_path(value: object) -> str | None:
    """Best-effort conversion of an audit-event path argument to ``str``.

    Returns ``None`` when the value is an ``int`` (file descriptor —
    no path to evaluate) or otherwise unrecoverable.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return None
    if isinstance(value, bytes):
        try:
            return os.fsdecode(value)
        except Exception:
            return None
    if isinstance(value, str):
        return value
    try:
        fspath = os.fspath(value)
    except TypeError:
        try:
            return str(value)
        except Exception:
            return None
    except Exception:
        return None
    if isinstance(fspath, bytes):
        try:
            return os.fsdecode(fspath)
        except Exception:
            return None
    return fspath if isinstance(fspath, str) else None


def _open_is_write(mode_or_flags: object) -> bool:
    if isinstance(mode_or_flags, str):
        return any(c in mode_or_flags for c in ("w", "a", "+", "x"))
    if isinstance(mode_or_flags, int) and not isinstance(mode_or_flags, bool):
        return bool(mode_or_flags & _WRITE_FLAGS)
    return False


def _make_command_str(executable: object, args: object) -> str:
    """Render the ``subprocess.Popen`` (executable, args) pair as a string.

    The hook does not actually run the subprocess — this is only used to
    feed :class:`Policy.evaluate` and ``on_violation`` callbacks.
    """

    if isinstance(args, (list, tuple)) and len(args) > 0:
        try:
            parts = [_coerce_path(a) or str(a) for a in args]
            return subprocess.list2cmdline(
                [p for p in parts if p is not None]
            )
        except Exception:  # pragma: no cover - hardening
            try:
                return " ".join(str(a) for a in args)
            except Exception:
                pass
    if isinstance(args, (str, bytes)):
        coerced = _coerce_path(args)
        if coerced:
            return coerced
    coerced_exe = _coerce_path(executable)
    if coerced_exe:
        return coerced_exe
    try:
        if args is not None:
            return str(args)
        if executable is not None:
            return str(executable)
        return ""
    except Exception:  # pragma: no cover - hardening
        return ""


# ---------------------------------------------------------------------------
# Decision engine — fully synchronous
# ---------------------------------------------------------------------------
def _evaluate(policy: Policy, *, scope: PolicyScope, target: str) -> Decision:
    """Walk Policy rules at ``scope`` and report the first match.

    A matching :attr:`PolicyAction.ALLOW` rule yields :attr:`Decision.ALLOW`;
    a matching :attr:`PolicyAction.DENY` rule yields :attr:`Decision.DENY_RULE`.
    No match falls back to :attr:`Decision.DENY_DEFAULT`, mirroring the
    new-architecture default-deny posture (see ``docs/05-architecture/contexts/security.md``).
    """

    for rule in policy.rules:
        if rule.scope is not scope:
            continue
        if not rule.matches(target):
            continue
        if rule.action is PolicyAction.ALLOW:
            return Decision.ALLOW
        return Decision.DENY_RULE
    return Decision.DENY_DEFAULT


def _check_or_raise(
    *,
    op: str,
    scope: PolicyScope,
    target: str,
) -> None:
    """Consult the Policy and raise :class:`PermissionError` on deny.

    ``op`` is a short identifier (``"read"``/``"write"``/``"exec"``) used
    only in the error message and the ``on_violation`` callback — it is
    NOT consulted by the Policy (the rule's :attr:`PolicyScope` already
    captures the dimension). ``target`` is the path / command string.
    """

    # PR-092 §2.1 C-3..C-6 — honour the audit-bypass contexts. Order
    # matters: ``suppress_audit`` overrides everything; ``trusted_subprocess``
    # only affects exec scope; ``trusted_read_prefix`` only covers read ops.
    if _SUPPRESSED.get():
        return
    if op == "exec" and _TRUSTED_SUBPROCESS_REASON.get() is not None:
        return
    if op == "read" and target:
        try:
            target_cf = target.casefold()
        except Exception:  # pragma: no cover - hardening
            target_cf = ""
        if target_cf and _is_trusted_read(target_cf):
            return

    provider = _STATE.policy_provider
    if provider is None:
        # Hook is installed but policy provider has not been set yet
        # (e.g. brief window during shutdown). Fail open — the hook is
        # advisory rather than mandatory in this state.
        return

    try:
        policy = provider()
    except Exception:  # pragma: no cover - hardening
        # If the provider blew up we'd rather miss a check than abort
        # the call. Log at warning level (a swallowed exception in the
        # security audit path is operationally significant) and move on.
        _LOGGER.warning(
            "audit hook policy_provider raised; allowing event", exc_info=True
        )
        return

    decision = _evaluate(policy, scope=scope, target=target)
    if decision is Decision.ALLOW:
        return

    on_violation = _STATE.on_violation
    if on_violation is not None:
        try:
            on_violation(decision, op, target)
        except Exception:  # pragma: no cover - hardening
            _LOGGER.debug(
                "audit hook on_violation callback raised; ignoring",
                exc_info=True,
            )

    raise PermissionError(f"FileGuard denied: {op} {target!r}")


# ---------------------------------------------------------------------------
# Per-event dispatch
# ---------------------------------------------------------------------------
def _dispatch(event: str, args: tuple[Any, ...]) -> None:
    if event == "open":
        if not args:
            return
        path = _coerce_path(args[0])
        if path is None or _is_baseline(path):
            return
        mode = args[1] if len(args) >= 2 else None
        flags = args[2] if len(args) >= 3 else None
        is_write = _open_is_write(mode) or _open_is_write(flags)
        _check_or_raise(
            op="write" if is_write else "read",
            scope=PolicyScope.PATH,
            target=path,
        )
        return

    if event == "os.open":
        if not args:
            return
        path = _coerce_path(args[0])
        if path is None or _is_baseline(path):
            return
        flags = args[1] if len(args) >= 2 else None
        is_write = _open_is_write(flags)
        _check_or_raise(
            op="write" if is_write else "read",
            scope=PolicyScope.PATH,
            target=path,
        )
        return

    if event in ("os.remove", "os.unlink"):
        if not args:
            return
        path = _coerce_path(args[0])
        if path is None or _is_baseline(path):
            return
        _check_or_raise(op="write", scope=PolicyScope.PATH, target=path)
        return

    if event in ("os.rename", "os.replace"):
        if len(args) < 2:
            return
        src = _coerce_path(args[0])
        dst = _coerce_path(args[1])
        if src is not None and not _is_baseline(src):
            _check_or_raise(op="write", scope=PolicyScope.PATH, target=src)
        if dst is not None and not _is_baseline(dst):
            _check_or_raise(op="write", scope=PolicyScope.PATH, target=dst)
        return

    if event == "subprocess.Popen":
        executable = args[0] if len(args) >= 1 else None
        popen_args = args[1] if len(args) >= 2 else None
        cmd_str = _make_command_str(executable, popen_args)
        if not cmd_str:
            return
        _check_or_raise(op="exec", scope=PolicyScope.PATH, target=cmd_str)
        return

    # PR-092 §2.2 H-17 / §17.5 #4 — extended events. Only dispatched
    # when the event is in ``_HANDLED_EVENTS`` (the install-time
    # allow-list); otherwise ``_on_event`` returns before reaching us.
    if event in ("os.scandir", "os.listdir"):
        if not args:
            return
        path = _coerce_path(args[0])
        if path is None or _is_baseline(path):
            return
        _check_or_raise(op="read", scope=PolicyScope.PATH, target=path)
        return

    if event == "shutil.copyfile":
        if len(args) < 2:
            return
        src = _coerce_path(args[0])
        dst = _coerce_path(args[1])
        if src is not None and not _is_baseline(src):
            _check_or_raise(op="read", scope=PolicyScope.PATH, target=src)
        if dst is not None and not _is_baseline(dst):
            _check_or_raise(op="write", scope=PolicyScope.PATH, target=dst)
        return


def _on_event(event: str, args: tuple[Any, ...]) -> None:
    """The single callback registered with ``sys.addaudithook``.

    Behaviour contract:

    * Returns immediately when the hook is soft-disabled.
    * Returns immediately for any event outside :data:`_HANDLED_EVENTS`.
    * Recursion guard prevents the hook's own IO from re-entering.
    * ``PermissionError`` is the only exception type allowed to escape;
      everything else is swallowed and logged at debug level.
    """

    if _STATE.disabled or not _STATE.installed:
        return
    if event not in _HANDLED_EVENTS:
        return
    if not _enter_hook():
        return
    try:
        _dispatch(event, args)
    except PermissionError:
        # Must propagate — this is how the hook denies an IO call.
        raise
    except Exception:  # pragma: no cover - hardening
        _LOGGER.warning("audit hook dispatch raised; ignoring", exc_info=True)
    finally:
        _exit_hook()


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------
def install_audit_hook(
    *,
    policy_provider: Callable[[], Policy],
    on_violation: Callable[[Decision, str, str], None] | None = None,
    extra_baseline_prefixes: tuple[str, ...] = (),
    extra_events: tuple[str, ...] = (),
) -> AuditHookHandle:
    """Install (or refresh) the singleton audit hook.

    The first call registers the callback with :func:`sys.addaudithook`;
    subsequent calls only refresh the ``policy_provider`` /
    ``on_violation`` / baseline configuration and return the same
    :class:`AuditHookHandle` instance. The hook is reusable: calling
    :meth:`AuditHookHandle.uninstall` and then re-invoking
    :func:`install_audit_hook` re-enables the existing callback.

    Args:
        policy_provider: Synchronous, reentrant-safe callable returning
            the current :class:`Policy`. Called once per intercepted
            event on the hot path; implementations should serve from
            an in-memory cache rather than hitting the database.
        on_violation: Optional sync callback invoked with
            ``(decision, op, target)`` immediately before the hook
            raises :class:`PermissionError`. Useful for stamping audit
            entries through whatever surface the caller has wired up
            (a thread-safe queue, a logging.Handler, …). Exceptions
            inside the callback are swallowed.
        extra_baseline_prefixes: Optional additional path prefixes
            (typically the project root) that bypass the Policy. They
            are merged with the interpreter / site-packages defaults.

    Returns:
        The shared :class:`AuditHookHandle` instance.
    """

    with _INSTALL_LOCK:
        _STATE.policy_provider = policy_provider
        _STATE.on_violation = on_violation
        _STATE.baseline_prefixes = _collect_baseline_prefixes(
            extra_baseline_prefixes
        )
        configure_extra_events(extra_events)
        _STATE.disabled = False
        if not _STATE.installed:
            try:
                sys.addaudithook(_on_event)
            except Exception as exc:  # pragma: no cover - hardening
                _LOGGER.warning("sys.addaudithook failed: %s", exc)
                return _HANDLE
            _STATE.installed = True
            _LOGGER.info(
                "audit hook installed (%d baseline prefixes, %d events)",
                len(_STATE.baseline_prefixes),
                len(_HANDLED_EVENTS),
            )
        else:
            _LOGGER.debug(
                "audit hook refresh (%d baseline prefixes)",
                len(_STATE.baseline_prefixes),
            )
        return _HANDLE
