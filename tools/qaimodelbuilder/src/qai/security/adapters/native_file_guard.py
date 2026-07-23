# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Native ``guard64.dll`` adapter implementing :class:`NativeFileGuardPort`.

2026-07-04 native-hook integration — PR-2.

This adapter owns the ctypes lifecycle of the compiled Detours-based
OS-level file hook shipped in ``vendor/bin/<arch>/guard64.dll``. It
wraps the in-package :class:`qai.security.adapters.native_hook.Guard`
ctypes wrapper (a verbatim copy of the native source wrapper, so the
production package never imports the ``native/file-guard/`` source tree
at runtime) and narrows its surface to the infra-free
:class:`NativeFileGuardPort` Protocol the DI / lifespan / grant-sync
layers consume.

Two concrete implementations are exported:

* :class:`NativeFileGuard` — the real adapter. Loads + installs the DLL
  on :meth:`start`, registers a V2 filter callback (the real ASK bridge
  built by ``apps.api._native_hook_bridge.build_native_hook_filter`` is
  wired in via ``start_native_guard``; the built-in :meth:`_default_filter`
  is only reached in hand-crafted test containers that never call
  ``start_native_guard``), and forwards rule mutations to the DLL.
* :class:`DisabledNativeFileGuard` — a zero-side-effect no-op used when
  ``native_file_guard_enabled`` is ``False`` (the default). It never
  loads the DLL, :attr:`is_active` is always ``False``, and every method
  is a silent no-op. This is what guarantees the PR-2 criterion
  "hook off ⇒ zero side-effects".

DLL resolution (:func:`resolve_dll_path`):

* an explicit ``dll_path`` (from ``Settings.native_file_guard_dll_path``)
  wins;
* otherwise ``<repo_root>/vendor/bin/<arch>/guard64.dll`` where ``arch``
  is ``arm64`` / ``x64`` picked from the current *process* architecture
  (an x64 python.exe under WoA x64-emulation resolves ``x64``; a native
  ARM64 python resolves ``arm64``). The ARM64X hybrid DLL is
  deliberately NOT used (x64-emu Init hang — see the vendor evaluation).
"""

from __future__ import annotations

import os
import platform
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from qai.platform.logging import get_logger
from qai.security.adapters.native_hook import (
    FilterEventV2,
    Guard,
    GuardLoadError,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.security.application.ports import NativeFileGuardPort

__all__ = [
    "NativeFileGuard",
    "DisabledNativeFileGuard",
    "resolve_dll_path",
    "current_guard_arch",
]

_log = get_logger(__name__)

#: V2 filter callback the DLL invokes on the native pipe thread. Returns
#: ``True`` to ALLOW the file event, ``False`` to DENY it.
FilterCallable = Callable[[FilterEventV2], bool]


def current_guard_arch() -> str:
    """Return ``"arm64"`` / ``"x64"`` for the current *process* arch.

    The guard DLL is loaded into (and injected from) *this* Python
    process, so the DLL bitness MUST match the process arch, not the
    host CPU. On Windows-on-ARM an x64 ``python.exe`` runs under x64
    emulation: the process is x64 even though the CPU is ARM64, and it
    can only load an x64 DLL (an arm64 DLL fails with ``WinError 193``).

    ``platform.machine()`` is the wrong source here: under WoA x64
    emulation it reports the *OS* CPU (``ARM64``), which would pick the
    arm64 DLL for an x64 process and make the guard silently fail to
    load. Windows exposes the true *process* arch via
    ``PROCESSOR_ARCHITECTURE`` (``AMD64`` for an emulated x64 process,
    ``ARM64`` for a native arm64 process), so we key off that. Only
    ``ARM64`` maps to ``arm64``; everything else (``AMD64`` / ``x86`` /
    unset) maps to ``x64`` — the only other artefact we ship.
    """
    proc = (os.environ.get("PROCESSOR_ARCHITECTURE") or "").lower()
    if proc:
        return "arm64" if proc == "arm64" else "x64"
    # Fallback (PROCESSOR_ARCHITECTURE unset — not expected on Windows):
    # platform.machine() at least distinguishes a native arm64 process.
    machine = (platform.machine() or "").lower()
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return "x64"


def resolve_dll_path(
    *,
    repo_root: Path,
    dll_path: Path | None = None,
    arch: str | None = None,
) -> Path:
    """Resolve the guard64.dll path (explicit override wins).

    Parameters
    ----------
    repo_root:
        Repo root used to locate the bundled ``vendor/bin`` artefacts.
    dll_path:
        Explicit override (``Settings.native_file_guard_dll_path``).
        When provided it is returned verbatim (resolved absolute).
    arch:
        Force an architecture (``"arm64"`` / ``"x64"``). Defaults to
        :func:`current_guard_arch`.
    """
    if dll_path is not None:
        return Path(dll_path).expanduser().resolve()
    resolved_arch = (arch or current_guard_arch()).lower()
    return (
        Path(repo_root) / "vendor" / "bin" / resolved_arch / "guard64.dll"
    ).resolve()


class NativeFileGuard:
    """Real :class:`NativeFileGuardPort` adapter (ctypes-backed).

    The adapter is constructed at DI build time but does NOT load the
    DLL until :meth:`start` — mirroring :class:`AuditHookAdapter`'s lazy
    install, so importing ``apps.api`` (tests / tooling) never hooks the
    interpreter. Lifespan flips :meth:`start` on when
    ``native_file_guard_enabled`` is True.
    """

    def __init__(
        self,
        *,
        dll_path: Path,
        fail_closed: bool = True,
        callback_timeout_ms: int = 60000,
        filter_callback: FilterCallable | None = None,
    ) -> None:
        self._dll_path = Path(dll_path)
        self._fail_closed = bool(fail_closed)
        self._callback_timeout_ms = int(callback_timeout_ms)
        # Filter callback (V2 ASK bridge). Production wiring is completed
        # AFTER construction by ``apps.api._native_hook_rules.start_native_guard``
        # via :meth:`set_filter_callback`, which passes the real ASK bridge
        # built by ``apps.api._native_hook_bridge.build_native_hook_filter``.
        # That bridge routes UNDECIDED native events through
        # :class:`CheckPermissionUseCase` so policy rules (allow-list,
        # deny-list, session grants, write_deny, ...) and the ASK dialog
        # actually decide the outcome.
        #
        # :meth:`_default_filter` below is a strict fail-closed bridge kept
        # ONLY for hand-crafted test containers that never call
        # ``start_native_guard`` -- production paths never reach it.
        #
        # (2026-07-13 note: an older comment here claimed "PR-3 injects the
        # real bridge here", implying the bridge was pending. That was
        # stale by several PRs -- the real bridge landed in PR-4 alongside
        # ``build_native_hook_filter``. Updated to avoid misleading future
        # diagnostic work; see docs/40-security/BUG-8-fix-...md §14.1.)
        self._filter_callback = filter_callback
        self._guard: Guard | None = None
        self._started = False
        # Phase 1 T2: host-lifetime random trust token (32-char hex). Populated
        # on start() when the DLL SetTrustedInfraToken export is available;
        # cleared on stop(). Injected as QAI_FILEGUARD_TRUST_TOKEN into the env
        # of host-spawned subprocesses so the DLL can classify them as
        # TrustedInfra and skip the ASK pipeline on undetermined paths.
        self._trusted_infra_token: str | None = None

    # -- configuration -------------------------------------------------
    def set_filter_callback(self, filter_callback: FilterCallable) -> None:
        """Wire the V2 filter callback (asyncio ASK bridge).

        Production wiring: ``apps.api._native_hook_rules.start_native_guard``
        calls this with the real ASK bridge returned by
        ``apps.api._native_hook_bridge.build_native_hook_filter``, which
        routes UNDECIDED native events through :class:`CheckPermissionUseCase`
        so policy rules (allow-list, deny-list, write_deny, session grants)
        and the ASK dialog actually decide the outcome.

        Must be called BEFORE :meth:`start`; a call after the hook is
        active is ignored (the DLL already holds the first callback ref).
        """
        if self._started:
            _log.warning(
                "native_file_guard.set_filter_after_start_ignored"
            )
            return
        self._filter_callback = filter_callback

    def _default_filter(self, _evt: FilterEventV2) -> bool:
        # No bridge wired: fail_closed => deny, else allow.
        return not self._fail_closed

    # -- NativeFileGuardPort -------------------------------------------
    @property
    def is_active(self) -> bool:
        return bool(self._started and self._guard is not None
                    and self._guard.is_inited)

    def start(self) -> bool:
        if self._started:
            return self.is_active
        if not self._dll_path.is_file():
            _log.error(
                "native_file_guard.dll_missing", path=str(self._dll_path)
            )
            return False
        try:
            guard = Guard(str(self._dll_path))
        except GuardLoadError:
            _log.error(
                "native_file_guard.load_failed",
                path=str(self._dll_path),
                exc_info=True,
            )
            return False
        fn = self._filter_callback or self._default_filter
        try:
            # FULL-mode Init: the host attaches the CreateProcessW hook (which
            # propagates guard64.dll into every child/grandchild it spawns) +
            # the file Nt* hooks + runs the shared pipe server answering the
            # V2 filter. Children auto-enter full mode via DllMain and their
            # file ops call back to THIS host's pipe for the ASK decision —
            # so LLM-spawned subprocess file ops (exec / background_process /
            # sticky worker …) are guarded. The host would ALSO hook its own
            # file I/O, so immediately after Init we exempt the host's real
            # process image (see below) — otherwise the API's own reads/writes
            # would each round-trip the ASK filter. (The deadlock previously
            # seen was NOT from full mode but from marshalling the callback
            # onto the API's main asyncio loop; the ASK bridge now uses a
            # dedicated loop thread.)
            # Phase 2 note (2026-07-06): ``callback_timeout_ms`` is
            # preserved on the InitV2 ABI for diagnostics + back-compat,
            # but the DLL no longer uses it for pipe waits (see plan §2
            # N9: INFINITE wait until the user acts or the DLL tears
            # down). Setting this value only affects logged diagnostics
            # on the native side. The Python bridge similarly passes
            # ``timeout=None`` to :class:`PermissionWaitRegistry.wait`
            # (see ``apps.api._native_hook_bridge._ask_user``).
            ok = guard.init_v2(
                fn,
                fail_closed=self._fail_closed,
                callback_timeout_ms=self._callback_timeout_ms,
            )
        except Exception:  # noqa: BLE001 — Init failure must not crash boot
            _log.error("native_file_guard.init_failed", exc_info=True)
            try:
                guard.destroy()
            except Exception:  # noqa: BLE001
                pass
            return False
        if not ok:
            _log.error("native_file_guard.init_returned_false")
            return False
        self._guard = guard
        self._started = True
        # P-03 (2026-07-08) upgrade compatibility — purge Phase-0.5 legacy
        # persisted rules from the DLL's ADS BEFORE seeding. On a machine
        # upgraded from Phase 0.5 the persisted rule stream can still carry
        # (a) ExemptSelf python.exe process-exception entries (Phase 1 host
        # bypass now uses g_is_host, so ANY python.exe exemption is stale and
        # a security hole — venv children share the base image and get wrongly
        # exempted) and (b) a C:\Qualcomm FULL-white entry that short-circuits
        # ahead of the op-mask R+X rule (silently allowing WRITE to a protected
        # tree). Both also propagate into every P6 child via CheckRuleFile().
        # This purge is idempotent (empty ADS / fresh machine → no-ops) and
        # only removes provably-stale entries; the subsequent seed rebuilds the
        # correct op-mask C:\Qualcomm rule. Best-effort: never crash boot.
        self._purge_legacy_persisted_rules(guard)
        # Phase 1: register a host-lifetime random trust token so spawned
        # child processes (StickyWorker, one-shot runner, MCP servers, etc.)
        # can be classified as TrustedInfra by the DLL and skip the ASK
        # pipeline on undetermined paths (see sticky_worker/host.py which
        # injects the token as QAI_FILEGUARD_TRUST_TOKEN in the child env).
        try:
            self._trusted_infra_token = secrets.token_hex(16)
            if not guard.set_trusted_infra_token(self._trusted_infra_token):
                _log.warning("native_file_guard.set_trust_token_failed")
                self._trusted_infra_token = None
        except Exception:  # noqa: BLE001
            _log.warning(
                "native_file_guard.set_trust_token_error", exc_info=True
            )
            self._trusted_infra_token = None
        # Exempt the HOST process from its OWN file hooks. ExemptSelf() lets the
        # DLL resolve its own real image path (GetModuleFileNameW(NULL)) and add
        # it to the process-exception list — robust vs passing sys.executable,
        # which on a Windows venv (esp. uv-managed) is the venv launcher path
        # while the loaded image is the base interpreter, so an exe-prefix match
        # would silently fail. With self exempt, the host's own I/O bypasses the
        # filter while spawned children stay guarded.
        # Phase 1 P1 fix: host bypass now uses the instance-scoped g_is_host flag
        # set by InitV2 inside the DLL, NOT the image-path exception list. The
        # legacy exempt_self used GetModuleFileNameW(NULL) which resolves to the
        # BASE cpython image; venv-spawned Python subprocesses shared the same
        # base image and were WRONGLY exempted, defeating child-process guarding
        # (P1 security hole). We deliberately no longer call it here.
        _log.debug(
            "native_file_guard.exempt_self_skipped",
            reason="phase1_uses_g_is_host_flag_via_init_v2",
        )
        _log.info(
            "native_file_guard.started",
            path=str(self._dll_path),
            fail_closed=self._fail_closed,
            timeout_ms=self._callback_timeout_ms,
        )
        return True

    def _purge_legacy_persisted_rules(self, guard: object) -> None:
        """Remove Phase-0.5 legacy persisted rules from the DLL's ADS (P-03).

        Idempotent, best-effort, called AFTER init_v2 (ADS loaded into memory)
        and BEFORE seeding. Removes only provably-stale entries:

        * process exceptions whose path ends in ``python.exe`` — Phase 1 host
          bypass uses the ``g_is_host`` flag, so no python.exe process
          exemption should exist; a leftover one wrongly exempts venv children
          (they share the base cpython image) and defeats child guarding.
        * a ``C:\\Qualcomm`` FULL-white entry — seeding only ever writes
          C:\\Qualcomm as an op-mask (R+X) rule; a leftover full-white entry
          short-circuits ahead of the op-mask and silently allows WRITE.

        Both removals use the persistent (session_only=False) form so the
        change lands in the ADS and every P6 child's CheckRuleFile() sees the
        cleaned rules. On a fresh machine the lists are empty → no-ops. Any
        API/parse error is swallowed (must never crash boot).
        """
        # (a) stale python.exe process exceptions
        try:
            excs = guard.list_process_exceptions()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — API/parse hiccup → skip this leg
            excs = []
        for exc in excs or []:
            try:
                if str(exc).replace("/", "\\").lower().endswith("python.exe"):
                    guard.remove_process_exception(exc)  # type: ignore[attr-defined]
                    _log.info(
                        "native_file_guard.purged_legacy_process_exception",
                        entry=str(exc),
                    )
            except Exception:  # noqa: BLE001 — per-entry best-effort
                _log.debug(
                    "native_file_guard.purge_process_exception_failed",
                    entry=str(exc),
                    exc_info=True,
                )
        # (b) stale C:\Qualcomm full-white entry (op-mask seed rebuilds R+X)
        try:
            whites = guard.list_white_rules_parsed()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            whites = []
        for w in whites or []:
            try:
                norm = str(w).replace("/", "\\").rstrip("\\").casefold()
                if norm == r"c:\qualcomm":
                    guard.delete_white_rules(w, session_only=False)  # type: ignore[attr-defined]
                    _log.info(
                        "native_file_guard.purged_legacy_full_white",
                        entry=str(w),
                    )
            except Exception:  # noqa: BLE001 — per-entry best-effort
                _log.debug(
                    "native_file_guard.purge_full_white_failed",
                    entry=str(w),
                    exc_info=True,
                )

    def stop(self) -> None:
        if self._guard is not None:
            try:
                self._guard.destroy()
            except Exception:  # noqa: BLE001 — teardown must not raise
                _log.warning("native_file_guard.stop_failed", exc_info=True)
        self._guard = None
        self._started = False
        self._trusted_infra_token = None

    def get_trusted_infra_token(self) -> str | None:
        """Return the host-lifetime random trust token, or None if disabled.

        Phase 1 T2: host-spawned subprocess launchers (StickyWorker, one-shot
        runner, MCP servers) call this to fetch the token and inject it as
        ``QAI_FILEGUARD_TRUST_TOKEN`` in the child env. The native DLL then
        classifies the child as TrustedInfra (env-presence signal in Phase 1;
        Phase 3 upgrades to pid registry + value comparison).
        """
        return self._trusted_infra_token

    def add_deny_rule(self, path: str, *, session_only: bool = True) -> bool:
        return self._mutate("add_rules", path, session_only)

    def remove_deny_rule(
        self, path: str, *, session_only: bool = True
    ) -> bool:
        return self._mutate("delete_rules", path, session_only)

    def add_allow_rule(self, path: str, *, session_only: bool = True) -> bool:
        return self._mutate("add_white_rules", path, session_only)

    def add_read_only_allow_rule(
        self, path: str, *, session_only: bool = True
    ) -> bool:
        """Add an OP-AWARE read-only allow (white_ro) prefix.

        Read is allowed (skips the callback); write / delete / execute still
        fall through to the callback (-> ASK). Degrades to ``False`` when the
        loaded DLL predates the ``AddReadOnlyWhiteRules`` export (the wrapper
        method returns ``False`` for an absent symbol).
        """
        return self._mutate("add_read_only_white_rules", path, session_only)

    def add_op_mask_allow_rule(
        self, path: str, mask: int, *, session_only: bool = True
    ) -> bool:
        """Add an OP-MASKED allow (white_ops) prefix.

        ``mask`` is a bitfield (READ=1, WRITE=2, EXECUTE=4, DELETE=8): an op
        whose bit is set is allowed (skips the callback); an op whose bit is
        unset falls through to the callback (-> ASK). This is the general
        op-aware primitive (e.g. read+execute but not write). Degrades to
        ``False`` when the loaded DLL predates the ``AddOpMaskWhiteRules``
        export.
        """
        if self._guard is None or not self._started:
            return False
        try:
            fn = getattr(self._guard, "add_op_mask_white_rules", None)
            if fn is None:
                return False
            return bool(fn(path, int(mask), session_only=session_only))
        except Exception:  # noqa: BLE001 — mutation must never crash the caller
            _log.warning(
                "native_file_guard.add_op_mask_rule_failed",
                path=path,
                exc_info=True,
            )
            return False

    def remove_allow_rule(
        self, path: str, *, session_only: bool = True
    ) -> bool:
        return self._mutate("delete_white_rules", path, session_only)

    def add_process_exception(self, exe_path: str) -> bool:
        if self._guard is None or not self._started:
            return False
        try:
            return bool(self._guard.add_process_exception(exe_path))
        except Exception:  # noqa: BLE001 — best-effort mutation
            _log.warning(
                "native_file_guard.add_process_exception_failed",
                exc_info=True,
            )
            return False

    def diagnostics(self) -> dict[str, Any]:
        if self._guard is None or not self._started:
            return {}
        try:
            return dict(self._guard.get_diagnostics())
        except Exception:  # noqa: BLE001
            return {}

    # -- internal ------------------------------------------------------
    def _mutate(self, method: str, path: str, session_only: bool) -> bool:
        """Best-effort rule mutation; no-op (False) when inactive."""
        if self._guard is None or not self._started:
            return False
        if not path:
            return False
        try:
            fn = getattr(self._guard, method)
            return bool(fn(path, session_only=session_only))
        except Exception:  # noqa: BLE001 — a mutation must never raise into
            # a grant/revoke flow
            _log.warning(
                "native_file_guard.mutate_failed",
                method=method,
                path=path,
                exc_info=True,
            )
            return False


class DisabledNativeFileGuard:
    """Zero-side-effect no-op :class:`NativeFileGuardPort`.

    Wired when ``native_file_guard_enabled`` is ``False`` (default). It
    never loads the DLL; every method is a silent no-op returning the
    "nothing applied" value. Guarantees the PR-2 criterion that a
    disabled hook has no effect on the process.
    """

    @property
    def is_active(self) -> bool:
        return False

    def start(self) -> bool:
        return False

    def stop(self) -> None:
        return None

    def add_deny_rule(self, path: str, *, session_only: bool = True) -> bool:
        return False

    def remove_deny_rule(
        self, path: str, *, session_only: bool = True
    ) -> bool:
        return False

    def add_allow_rule(self, path: str, *, session_only: bool = True) -> bool:
        return False

    def add_read_only_allow_rule(
        self, path: str, *, session_only: bool = True
    ) -> bool:
        return False

    def add_op_mask_allow_rule(
        self, path: str, mask: int, *, session_only: bool = True
    ) -> bool:
        return False

    def remove_allow_rule(
        self, path: str, *, session_only: bool = True
    ) -> bool:
        return False

    def add_process_exception(self, exe_path: str) -> bool:
        return False

    def get_trusted_infra_token(self) -> str | None:
        return None

    def diagnostics(self) -> dict[str, Any]:
        return {}


def _assert_port() -> "NativeFileGuardPort":  # pragma: no cover - typing aid
    # Static reassurance that both concrete classes satisfy the port.
    guard: NativeFileGuardPort = DisabledNativeFileGuard()
    return guard
