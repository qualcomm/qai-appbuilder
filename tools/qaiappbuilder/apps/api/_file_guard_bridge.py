# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Cross-context FileGuard bridge (S-1 / D11 — apps/api wiring root).

Wires the ai_coding :class:`FileGuardPort` to the *real* PolicyCenter
(``qai.security``) + the dep / exec brokers, restoring the V1
``_enforce_*`` family without
``qai.ai_coding`` ever importing ``qai.security`` directly (the
``context-isolation`` import-linter contract forbids that). The bridge
lives in ``apps/api/`` — the one layer allowed to depend on multiple
bounded contexts — exactly like ``_permission_bridge.py`` /
``_skill_registry_bridge.py``.

V1 enforcement anchors
----------------------
* ``_enforce_read`` (read).
* ``_enforce_write`` (write).
* ``_enforce_exec`` — the
  **three gates in order**: ① Dep Broker, ② Exec Broker
  profile, ③ PolicyCenter exec decision.
  dep / exec broker failures are best-effort (swallowed → allow);
  PolicyCenter failure is fail-closed (deny).
* project-access — project toggle.

Master switch (D11)
-------------------
V1 ``access_policy.default.json`` ships ``enabled=false`` (FileGuard is
opt-in). The bridge mirrors that: when
``settings.security.file_guard_enabled`` is ``False`` (the default) all
four methods are pass-through (``return None``) — exactly the V1
``FILEGUARD_DISABLED`` open-box behaviour. ``allow_exec_tool`` defaults
to ``True`` (V1 ``forge_config_manager.py:341``); when ``False`` the
``enforce_exec`` gate hard-denies before consulting any broker.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, NamedTuple

from qai.ai_coding.application.ports import FileGuardPort
from qai.ai_coding.infrastructure.tools.errors import ToolGuardDenied
from qai.ai_coding.infrastructure.tools.file_guard import NoopFileGuard
from qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics import (
    build_fileguard_denial_hint,
)
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Callable

    from qai.dependency_approval.application.ports import DepBrokerPort
    from qai.command_policy.application.ports import ExecBrokerPort
    from qai.command_policy.domain import ClassifyReason
    from qai.security.application.permission_wait import PermissionWaitRegistry
    from qai.security.domain.value_objects import PolicyAction

    from .di import Container

__all__ = ["FileGuardFacade", "build_file_guard"]

_log = get_logger(__name__)

# ---------------------------------------------------------------------------
# SEC true-scoping — ASK request_id → conversation id registry (PART D)
# ---------------------------------------------------------------------------
# The security ``PermissionRequest`` entity is field-locked (no slot for the
# originating conversation id), but a session-scoped ASK grant must be keyed
# to the conversation that triggered it. The FileGuard bridge stashes the
# conversation id (from the ``get_conversation_scope()`` contextvar) keyed by
# the minted ``request_id`` when it creates the ASK; the approve HTTP route
# reads it back as ``scope_conversation_id`` for ``ApprovePermissionUseCase``.
#
# The registry now lives in its own apps-layer module
# (:mod:`apps.api._ask_conversation_registry`) and is *injected* into the
# FileGuard bridge instead of reached as a hidden module-global. The
# process-wide default singleton is re-exported here as
# ``ASK_CONVERSATION_REGISTRY`` for the approve route + tests that import it by
# that path (back-compat), and it is the bridge constructor's default.
from ._ask_conversation_registry import (  # noqa: E402
    ASK_CONVERSATION_REGISTRY,
    AskConversationRegistry,
)

# FileGuard ASK blocks the synchronous tool call until the user decides.
#
# 2026-07-07 fix (Bug 2 / P-10 in-process leg): previously this ceilinged at
# 60s (V1 ``PolicyCenter.ask_user`` default) and, on timeout, resolved to a
# fabricated "user rejected" DENY. In practice a single ``uv pip install``
# fans out into DOZENS of native PATH-search popups; by the time the user
# reaches the exec dialog, >60s has elapsed and the command was already
# auto-denied — the classic "I clicked Allow but it still denied" bug. P-10
# fixed this on the NATIVE leg (``_native_hook_bridge`` waits ``timeout=None``)
# but the in-process exec/path leg here was left at 60s. We now align: wait
# INDEFINITELY for the user (``None``), so "user walked away" = still pending,
# never an auto-deny. Service teardown still breaks the wait fail-closed via
# the registry (same as native). Operators may still set a finite ceiling via
# ``build_file_guard(ask_timeout_sec=...)`` if they truly want one.
_ASK_TIMEOUT_SEC: float | None = None


class _AskOutcome(NamedTuple):
    """Result of an ASK dialog wait: whether allowed, and if it timed out.

    ``timed_out`` lets callers distinguish a genuine user REJECT (``allow
    =False, timed_out=False``) from a wait that expired without a user
    response (``allow=False, timed_out=True``). This is what fixes Bug 4:
    a timeout must NOT be reported to the model as "the user rejected this,
    do not retry" — it should say "confirmation timed out, ask the user to
    click Allow, then retry the SAME command". With ``_ASK_TIMEOUT_SEC=None``
    (infinite wait) ``timed_out`` is normally False, but the field is kept
    for any finite-ceiling deployment and for the terminal expired state.
    """

    allow: bool
    timed_out: bool = False


def _build_exec_error(command: str, reason: str) -> str:
    """Map a FileGuard exec-deny ``reason`` code to user-facing guidance.

    7-M9 — V1 parity. The message
    names the exact Security UI panel the operator must visit to authorise
    the command, instead of a generic "denied".

    The reason-code vocabulary + message catalog live in the security domain
    (:mod:`qai.security.domain.exec_deny_reason`); this apps-layer helper
    owns the shell-parsing (extracting the executable token), delegates the
    per-reason wording to the domain, then appends the SINGLE-SOURCE V2
    FileGuard denial hint (2026-07-23) so every FileGuard-denied exec
    message ends with the SAME strong "enforced boundary / retry-won't-work
    / ask the user to authorise" clause — matching the concise hint every
    other FileGuard denial path emits. The domain catalog keeps its specific
    per-reason guidance (which Allow List entry to add, which capability to
    grant); V2 provides the model-facing stop-and-authorise instruction.
    """
    from qai.security.domain import exec_deny_message

    cmd_short = command[:80]
    try:
        exe_token = cmd_short.split()[0] if cmd_short else cmd_short
        exe_display = Path(exe_token).name or exe_token
    except Exception:  # noqa: BLE001
        exe_display = cmd_short.split()[0] if cmd_short else cmd_short

    domain_msg = exec_deny_message(
        reason, command_display=cmd_short, exe_display=exe_display
    )
    # ``build_fileguard_denial_hint`` prefixes ``\n\n`` for the "append to
    # arbitrary text" case — that's exactly what we want here.
    return domain_msg + build_fileguard_denial_hint()


# ---------------------------------------------------------------------------
# Exec lib-storm read-only-tree derivation (module-private helpers)
# ---------------------------------------------------------------------------
# When a guarded child process is a toolchain driver (e.g. a compiler / linker
# like ``C:\Programs\llvm-mingw-ucrt-aarch64\bin\clang.exe``) it concurrently
# reads HUNDREDS of header / lib files out of its own install tree the instant
# it starts. The native ``guard64.dll`` intercepts every one of those reads; if
# the toolchain directory is not whitelisted each read becomes its own ASK
# popup, flooding the operator. Worse, the reads **burst at process startup** —
# they all fire BEFORE the operator can approve even the first one, so any
# "approve, then it applies" grant is already too late for the storm.
#
# The mitigation (see ``plans/fileguard-exec-lib-storm-fix.md``): at the moment
# an exec command is ALLOWED (``enforce_exec`` runs BEFORE the child spawns and
# sees the un-rewritten command string), derive the executable's install root
# and inject it as a READ-ONLY prefix into the native whitelist. The rule lands
# before the child's read storm happens — those reads then match the
# ``white_ro`` prefix in the DLL's local decision and are allowed WITHOUT any
# callback / popup. Only READ is bypassed for that tree; write / delete /
# execute still go through ASK, and the blacklist / protected_write_paths still
# hard-deny.
#
# Why the *grandparent* of the exe: for ``...\llvm-mingw-ucrt-aarch64\bin\
# clang.exe`` the exe's directory is ``...\bin`` and its parent
# ``...\llvm-mingw-ucrt-aarch64`` is the toolchain root, whose subtree (``bin``
# / ``lib`` / ``include`` / ...) covers the reads. Injecting the single
# grandparent prefix therefore covers BOTH the exe dir AND its sibling dirs in
# one rule — the agreed "exe dir + one level up" scope.
#
# Why the guardrails: a read-only prefix is far weaker than a full whitelist
# (it bypasses ONLY reads), but injecting a shallow root (``C:\``,
# ``C:\Windows``, ``C:\Users``, ...) would still silently allow reads across an
# enormous, sensitive tree. So we reject a candidate root that is too shallow:
# it must sit at least two components below its drive anchor, and it must not be
# one of a small set of well-known shallow system roots (nor any bare drive
# root). A rejected candidate simply falls back to the normal per-file ASK path
# — no injection, no weakening.
#
# These helpers are PURE: they never touch the filesystem (no ``resolve()`` —
# the API process may not share the child's filesystem view, and resolve() could
# hit I/O) and never raise. They only normalize / inspect the path string
# handed in.

#: Well-known shallow roots that must never be injected as a read-only prefix,
#: stored as casefolded normalized Windows path strings for an exact-match test
#: against the (casefolded) candidate root. The bare drive-root case (``C:\``,
#: ``D:\`` ...) is handled generically by the anchor-only check in
#: :func:`_derive_exec_readonly_root`, but ``C:\`` is kept here too for
#: explicitness.
_SHALLOW_ROOT_BLACKLIST: frozenset[str] = frozenset(
    p.casefold()
    for p in (
        "C:\\",
        "C:\\Windows",
        "C:\\Program Files",
        "C:\\Program Files (x86)",
        "C:\\Users",
        "C:\\ProgramData",
    )
)


def _exec_root_depth(path: PureWindowsPath) -> int:
    """Return the number of path components BELOW the drive anchor.

    ``C:\\A\\B`` → 2 (``[A, B]``); ``C:\\A`` → 1; ``C:\\`` → 0. Used by the
    minimum-depth guardrail so a candidate root that sits directly under a
    drive root (or is the drive root itself) is rejected.
    """
    # ``parts`` includes the anchor (e.g. ``C:\\``) as the first element;
    # everything after it is a real directory component.
    return max(0, len(path.parts) - 1)


def _derive_exec_readonly_root(exe_path: str) -> str | None:
    """Derive the read-only install-tree root for an executable.

    Parameters
    ----------
    exe_path:
        The absolute path of the executable about to run. May be ``""``,
        ``None``, relative, or junk.

    Returns
    -------
    str | None
        The candidate read-only root — the PARENT of the exe's directory
        (``Path(exe_path).parent.parent``) — as a normalized absolute Windows
        path string, or ``None`` when the input is not a usable absolute path
        or when the candidate fails a guardrail (too shallow / a blacklisted
        system root / any drive root). ``None`` means "do not inject; fall back
        to normal per-file ASK".

    Never raises — any parsing error yields ``None``.
    """
    try:
        if not exe_path:
            return None

        exe = PureWindowsPath(exe_path)

        # Only absolute paths can be safely scoped; a relative / bare name
        # gives no trustworthy install root. ``is_absolute`` on a
        # PureWindowsPath requires both a drive and a root anchor.
        if not exe.is_absolute():
            return None

        # Grandparent of the exe = exe dir + one level up (the toolchain
        # root whose subtree covers the read storm).
        candidate = exe.parent.parent

        # Guardrail 1 — minimum depth: reject C:\\A (1 component) and C:\\
        # (0 components); require at least 2 components below the anchor.
        if _exec_root_depth(candidate) < 2:
            return None

        # Guardrail 2 — any bare drive root (anchor-only path, i.e. a path
        # whose parent is itself). Defensive: depth < 2 already covers this,
        # but the generic anchor check documents the intent and guards
        # against future component-counting drift.
        if candidate == candidate.parent:
            return None

        candidate_str = str(candidate)

        # Guardrail 3 — explicit shallow blacklist (case-insensitive exact
        # match against the normalized candidate).
        if candidate_str.casefold() in _SHALLOW_ROOT_BLACKLIST:
            return None

        return candidate_str
    except Exception:  # noqa: BLE001 — never raise; a bad path just skips injection
        return None


def _path_under_any_static_prefix(
    path: str, prefixes: tuple[str, ...]
) -> bool:
    """Return ``True`` when ``path`` equals or is nested under any prefix.

    Case-insensitive, separator-agnostic path-COMPONENT prefix match — the
    SAME semantics ``qai.security.application.use_cases.check_permission.
    _path_under_any_prefix`` uses for the GLOBAL / workspace / read-only
    allow short-circuits (State-Truth-First: both FileGuard layers must
    read the SAME source with the SAME matching rule so a stale casefold
    or a mixed-separator target can never diverge). Kept LOCAL to the
    bridge module so ``qai.ai_coding`` never imports ``qai.security``
    (the context-isolation contract also applies here — the check use
    case's helper is a private symbol of that context).

    Empty ``path`` / empty ``prefixes`` → ``False``. Never raises.
    """
    if not path or not prefixes:
        return False
    target_parts = tuple(
        part.casefold()
        for part in path.replace("\\", "/").split("/")
        if part
    )
    # ``..`` in the RAW target is a traversal escape; refuse to match so
    # a ``prefix/../elsewhere`` sneak-path is never treated as a match.
    if ".." in target_parts:
        return False
    for prefix in prefixes:
        prefix_parts = tuple(
            part.casefold()
            for part in prefix.replace("\\", "/").split("/")
            if part
        )
        if not prefix_parts:
            continue
        if len(target_parts) < len(prefix_parts):
            continue
        if target_parts[: len(prefix_parts)] == prefix_parts:
            return True
    return False


class FileGuardFacade:
    """FileGuardPort adapter backed by PolicyCenter + dep/exec brokers.

    Implements all four :class:`FileGuardPort` methods. When
    ``file_guard_enabled`` is ``False`` every method short-circuits to
    ``return None`` (V1 ``enabled=false`` open-box parity). When enabled,
    file ops consult the security :class:`CheckPermissionUseCase` and
    ``enforce_exec`` runs the V1 three-gate chain.
    """

    def __init__(
        self,
        *,
        file_guard_enabled: bool,
        allow_exec_tool: bool,
        check_permission_use_case: object | None = None,
        dep_broker: "DepBrokerPort | None" = None,
        exec_broker: "ExecBrokerPort | None" = None,
        project_root: str = "",
        emergency_audit_path: "Path | None" = None,
        dep_handled_externally: bool = False,
        request_permission_use_case: object | None = None,
        wait_registry: "PermissionWaitRegistry | None" = None,
        ask_timeout_sec: float | None = _ASK_TIMEOUT_SEC,
        enabled_provider: "Callable[[], bool] | None" = None,
        boot_id_provider: "Callable[[], str] | None" = None,
        ask_conversation_registry: "AskConversationRegistry | None" = None,
        native_guard_active_provider: "Callable[[], bool] | None" = None,
        native_guard: object | None = None,
        static_allow_provider: "Callable[[], tuple[str, ...]] | None" = None,
    ) -> None:
        # Master-switch (file_guard_enabled) baked value + optional LIVE
        # provider. When ``enabled_provider`` is wired the ``_enabled``
        # property reads it on every guard call, so flipping the unified
        # FileGuard master switch takes effect WITHOUT a process restart
        # (mirrors ``enabled_provider``). ``None`` provider keeps the
        # baked constructor value byte-for-byte for S0-S7 callers / tests.
        self._enabled_baked = file_guard_enabled
        self._enabled_provider = enabled_provider
        self._allow_exec_tool = allow_exec_tool
        self._check = check_permission_use_case
        self._dep_broker = dep_broker
        self._exec_broker = exec_broker
        self._project_root = project_root
        self._emergency_audit_path = emergency_audit_path
        # P-17 §6.3 — unified JSONL schema via the shared factory (was a
        # hand-rolled json.dumps block). No-op sink when the path is None.
        from ._jsonl_audit_sink import make_jsonl_audit_sink

        self._emergency_jsonl = make_jsonl_audit_sink(
            emergency_audit_path, source="emergency"
        )
        # P0 ASK restore — when both collaborators are wired, a policy miss
        # that ``CheckPermissionUseCase`` flags as ``would_ask`` pops the
        # authorization dialog and blocks for the user's decision (V1
        # ``PolicyCenter.ask_user``) instead of failing closed to DENY.
        self._request_permission = request_permission_use_case
        self._wait_registry = wait_registry
        self._ask_timeout_sec = ask_timeout_sec
        # 2026-07-08 — 0-arg callable returning whether the native OS-level
        # file guard (guard64.dll) is ACTIVE. Used to weaken the redundant
        # command-level Gate ③ ASK for exec: when the native guard is active,
        # an exec command's actual FILE operations are judged per-path by the
        # native path allow-list at execution time (mkdir under the workspace
        # white-listed C:\WoS_AI → allowed, no popup), so the command-level
        # ASK — which cannot parse the command and therefore prompts for EVERY
        # exec — is redundant for file effects and is skipped. Gate ② exec_broker
        # STILL guards non-file risks (network / registry / dangerous programs)
        # that the file layer cannot cover. When the native guard is NOT active
        # (provider returns False / None) the file-layer backstop is gone, so we
        # FALL BACK to the original command-level Gate ③ ASK (no "both layers
        # off" window). ``None`` provider = conservative (treat as inactive →
        # keep original behaviour) for S0-S7 callers / tests.
        self._native_guard_active_provider = native_guard_active_provider
        # When the pure-software FileBroker is active (default ON) it owns the
        # dep-install approval loop (``_file_broker_bridge`` exec guard), which
        # always runs BEFORE this FileGuard gate. Setting this avoids a SECOND
        # approval prompt for the same command here (V1 had a single Gate①).
        self._dep_handled_externally = dep_handled_externally
        # SEC — 0-arg callable returning this backend process's boot id
        # (minted once at startup in lifespan). Used as the ``scope_key`` for
        # ``process``-scoped grants so they stop matching after a restart
        # (a new process = a new boot id). ``None`` → "" → process grants are
        # never matched (fail-safe), which is correct for test containers.
        self._boot_id_provider = boot_id_provider
        # SEC true-scoping (PART D) — the ASK request_id → conversation id
        # coordination registry, INJECTED (no hidden module-global). Defaults
        # to the process-wide shared singleton so the approve route reads back
        # the same instance; tests may pass their own for isolation.
        self._ask_registry = (
            ask_conversation_registry
            if ask_conversation_registry is not None
            else ASK_CONVERSATION_REGISTRY
        )
        # exec lib-storm mitigation — the native guard port whose
        # ``add_read_only_allow_rule(path, session_only=False)`` injects an
        # allowed executable's install-tree as a read-only prefix. This runs
        # in-process from ``enforce_exec`` (BEFORE the child is spawned, with
        # the un-rewritten command string in hand), so the read-only rule
        # lands in the DLL's whitelist ahead of the toolchain driver's startup
        # read storm — those reads then match the ``white_ro`` prefix locally
        # and never fan out into per-file ASK popups. ``None`` disables it
        # (tests / no-guard containers).
        self._native_guard = native_guard
        # Idempotency cache of already-injected roots (casefolded). ``enforce_
        # exec`` may run concurrently for different commands, but ``set.add``
        # is atomic and a duplicate ``add_read_only_allow_rule`` call is
        # harmless (the DLL de-dupes prefixes), so re-injecting the same root
        # from a race is a benign no-op — no lock needed.
        self._injected_exec_roots: set[str] = set()
        # M-Py-3 (2026-07-28) — 0-arg provider returning the STATIC allow
        # prefixes (``resolve_global_allow_paths`` + ``resolve_read_only_
        # allow_paths`` — the two op-agnostic / read-only STATIC allow lists).
        # ``is_statically_allowed`` consults ONLY this provider (never the
        # PolicyCenter decision), so an ASK-tree (dynamic authorisation)
        # allow no longer masquerades as a STATIC allow — restoring the
        # V1 ``explain_read`` semantics ``glob`` depends on to SKIP per-file
        # filtering inside a user-approved tree. ``None`` = no provider
        # wired → fail-open True (V1 ``allowed = True`` on any evaluation
        # gap) so hand-rolled test containers keep pre-feature behaviour.
        self._static_allow_provider = static_allow_provider
        # DIAG (2026-07-28) — one-shot startup snapshot of the ASK-bridge
        # wiring so the log immediately shows whether an implicit-deny would
        # be able to pop the authorization dialog. Emitted once per bridge
        # construction; look for `filesec.bridge_wired` in the service log.
        _log.info(
            "filesec.bridge_wired",
            extra={
                "file_guard_enabled_baked": bool(file_guard_enabled),
                "has_enabled_provider": self._enabled_provider is not None,
                "has_check_permission": self._check is not None,
                "has_request_permission": self._request_permission is not None,
                "has_wait_registry": self._wait_registry is not None,
                "ask_timeout_sec": self._ask_timeout_sec,
                "has_dep_broker": self._dep_broker is not None,
                "has_exec_broker": self._exec_broker is not None,
                "has_native_guard": self._native_guard is not None,
            },
        )

    @property
    def _enabled(self) -> bool:
        """Live master-switch state (file_guard_enabled).

        Reads ``enabled_provider`` on every access when wired, so a unified
        FileGuard master-switch flip is instant (no restart). Falls back to
        the baked constructor value when no provider is set, and fails
        SAFE — a provider error keeps the guard ENABLED (never silently
        opens the box on a transient read error).
        """
        if self._enabled_provider is None:
            return self._enabled_baked
        try:
            return bool(self._enabled_provider())
        except Exception:  # noqa: BLE001 — provider error → stay enabled (safe)
            return True

    # ------------------------------------------------------------------
    # Emergency audit (7-M9 — fail-closed JSONL fallback)
    # ------------------------------------------------------------------
    def _emergency_audit(
        self,
        *,
        op: str,
        path: str,
        caller: str,
        reason: str,
    ) -> None:
        """Append a single ``decision=deny / source=emergency`` JSONL line.

        7-M9 — V1 parity. When
        PolicyCenter is unavailable or its evaluation raises, the request is
        denied (fail-closed); this writes the audit trail of *what* was
        denied during the outage so the "PolicyCenter down" window is not a
        blind spot. Best-effort: a missing path / write failure is swallowed
        — the caller is already on a DENY path and must still raise.

        P-17 §6.3 — delegates to the shared JSONL sink so the emergency and
        FileBroker landing sites emit one canonical schema (this method keeps
        its sync, best-effort, never-raises contract for the fail-closed
        callers — unchanged behaviour, just no longer a hand-rolled block).
        """
        self._emergency_jsonl(
            {
                "op": op,
                "path": path,
                "decision": "deny",
                "caller": caller,
                "source": "emergency",
                "reason": reason,
                "mode": "fail_closed",
            }
        )

    # ------------------------------------------------------------------
    # File-touching gates
    # ------------------------------------------------------------------
    async def enforce_read(self, *, path: str, caller: str) -> None:
        if not self._enabled:
            return
        await self._enforce_path(
            path=path,
            read=True,
            write=False,
            error_code="ai_coding.tool.read_denied",
            caller=caller,
            op="read",
        )

    async def enforce_write(self, *, path: str, caller: str) -> None:
        if not self._enabled:
            return
        await self._enforce_path(
            path=path,
            read=False,
            write=True,
            error_code="ai_coding.tool.write_denied",
            caller=caller,
            op="write",
        )

    async def enforce_delete(self, *, path: str, caller: str) -> None:
        """Delete-specific write gate — audit distinguishes delete from write.

        The security *decision* stays write-based (delete is a mutating
        op, and V1 gated it under ``write_allow``): we still request
        ``write=True`` so an operator who granted "write" gets the same
        allow result as before. What changes is the *audit* trail:
        ``op="delete"`` + the ``AceMask.delete`` bit are threaded onto
        the ``CheckPermissionUseCase.execute`` call so the audit query
        can tell a delete apart from an in-place write on the same path
        (SEC-ENHANCE-AUDITUX-1). No behaviour change.
        """
        if not self._enabled:
            return
        await self._enforce_path(
            path=path,
            read=False,
            write=True,
            delete=True,
            error_code="ai_coding.tool.write_denied",
            caller=caller,
            op="delete",
        )

    # ------------------------------------------------------------------
    # Exec gate — V1 three-gate chain (dep → exec → PolicyCenter)
    # ------------------------------------------------------------------
    async def enforce_exec(
        self, *, command: str, cwd: str | None, caller: str
    ) -> None:
        """V1 three-gate exec chain — thin orchestration over three gates.

        The gate *ordering* and their independence from the ``file_guard``
        master switch are the load-bearing behaviour (see each gate method):

        * Gate ② (exec-broker profile) runs FIRST, INDEPENDENT of the
          ``file_guard_enabled`` toggle (M-2 regression fix), and may fully
          decide the command (ASK-approved / grant-covered → ``return``).
        * The ``file_guard_enabled`` + ``allow_exec_tool`` master gates.
        * Gate ① (dep-broker approval), Gate ③ (PolicyCenter decision).

        Extracting the body into ``_gate_*`` helpers keeps this method a
        readable sequence and gives each gate a single responsibility; the
        cross-context orchestration honestly stays in the apps layer (the
        one layer allowed to touch exec_broker + dep_broker + security).
        """
        # ── Gate ② Exec Broker profile (independent of file_guard switch) ──
        if await self._gate_exec_broker(command=command, caller=caller):
            # The exec-broker ASK path fully decided this command (approved or
            # covered by a still-valid grant); return so we do NOT fall through
            # to Gate ③ PolicyCenter which — for an ``once`` scope that stored
            # no grant — would pop a SECOND dialog when file_guard is ON.
            self._maybe_inject_exec_readonly_root(command)
            return

        if not self._enabled:
            return

        # allow_exec_tool master toggle (V1 forge_config_manager.py:341).
        if not self._allow_exec_tool:
            raise ToolGuardDenied(
                message="The exec tool is disabled (allow_exec_tool=false).",
                error_code="ai_coding.tool.exec_denied",
            )

        # ── Gate ① Dep Broker (V1) ───────────────
        await self._gate_dep_broker(command=command, caller=caller)

        # ── Gate ② Exec Broker profile — already enforced at the top of
        # enforce_exec (M-2: independent of the file_guard switch). ───────

        # ── Gate ③ PolicyCenter exec decision (V1) ─
        # 2026-07-08 — weaken the redundant command-level ASK. Gate ② already
        # let this command through as NON-dangerous (a dangerous command would
        # have DENIED / ASKed and returned above). Its file effects are judged
        # per real path by the NATIVE guard's path allow-list when it runs
        # (mkdir under white-listed C:\WoS_AI → allowed, no popup). The
        # command-level Gate ③ cannot parse the command, so it can only ASK for
        # EVERY exec — a redundant prompt on top of the reliable file-layer
        # check. So: when the native guard is active, SKIP the command-level
        # exec ASK and let the file layer decide per-path. When it is NOT active
        # the file-layer backstop is gone → fall back to the original Gate ③
        # ASK (never leave both layers off). Non-file risks (network / registry
        # / arbitrary program exec) remain covered by Gate ② exec_broker, which
        # ran above regardless of this branch.
        if self._native_guard_active():
            self._maybe_inject_exec_readonly_root(command)
            return
        await self._enforce_path(
            path=command,
            read=False,
            write=False,
            execute=True,
            resource_kind="exec",
            error_code="ai_coding.tool.exec_denied",
            caller=caller,
            op="exec",
            exec_command=command,
        )
        # ``_enforce_path`` returned without raising ⇒ the command is allowed;
        # inject the executable's read-only install tree before the child
        # spawns (mirrors the two early-return allow points above).
        self._maybe_inject_exec_readonly_root(command)

    def _native_guard_active(self) -> bool:
        """Return whether the native OS file guard is active (best-effort).

        Reads the injected ``native_guard_active_provider``. Any error or a
        missing provider → ``False`` (conservative: treat native as inactive
        so the command-level Gate ③ ASK is kept, never dropped on uncertainty).
        """
        provider = self._native_guard_active_provider
        if provider is None:
            return False
        try:
            return bool(provider())
        except Exception:  # noqa: BLE001 — uncertainty → conservative (inactive)
            return False

    # ------------------------------------------------------------------
    # Exec lib-storm read-only tree injection (in-process, pre-spawn)
    # ------------------------------------------------------------------
    def _maybe_inject_exec_readonly_root(self, command: str) -> None:
        """Inject an allowed command's executable install-tree as read-only.

        Called at every ALLOW return point of :meth:`enforce_exec` — which
        runs synchronously in-process BEFORE the child is spawned and sees
        the un-rewritten command string. A toolchain driver (compiler /
        linker) reads hundreds of header / lib files the instant it starts;
        pre-seeding the DLL's ``white_ro`` prefix with the executable's
        install root here lets those reads match the read-only prefix in the
        DLL's local decision — no per-file ASK storm. Only READ is bypassed
        for the tree; write / delete / execute still ASK and the blacklist
        still hard-denies.

        Fully best-effort — any failure is swallowed (``_log.warning``) and
        NEVER re-raised, so it can never affect the exec ALLOW. Idempotent via
        :attr:`_injected_exec_roots`. A guard that returns ``False`` (DLL
        predates the read-only export / mutation failed) is NOT cached, so a
        later working call can retry the same root.
        """
        try:
            if self._native_guard is None:
                return
            # Parse the leading token (the executable) out of the command
            # string. Skip leading whitespace; honour a quoted first token
            # (paths with spaces), else read up to the first whitespace.
            i = 0
            n = len(command)
            while i < n and command[i].isspace():
                i += 1
            if i >= n:
                return
            if command[i] == '"':
                i += 1
                start = i
                while i < n and command[i] != '"':
                    i += 1
                exe_token = command[start:i]
            else:
                start = i
                while i < n and not command[i].isspace():
                    i += 1
                exe_token = command[start:i]
            if not exe_token:
                return

            # Resolve to an absolute exe path. An already-absolute token is
            # used verbatim; a bare / relative name is resolved via PATH
            # (best-effort) so a ``clang`` on PATH still yields its install
            # tree. An unresolved token is passed through to the derive helper,
            # which returns None for a non-absolute path (no injection).
            if PureWindowsPath(exe_token).is_absolute():
                resolved = exe_token
            else:
                import shutil

                resolved = shutil.which(exe_token) or exe_token

            root = _derive_exec_readonly_root(resolved)
            if root is None:
                return
            key = root.casefold()
            if key in self._injected_exec_roots:
                return
            ok = self._native_guard.add_read_only_allow_rule(  # type: ignore[attr-defined]
                root, session_only=False
            )
            if ok:
                self._injected_exec_roots.add(key)
                _log.info(
                    "native_guard.exec_readonly_root_injected",
                    root=root,
                    command=command[:120],
                )
            else:
                # DLL predates the read-only export or the mutation failed —
                # do NOT cache, so a later working call can retry this root.
                _log.debug(
                    "native_guard.exec_readonly_root_inject_noop",
                    root=root,
                )
        except Exception:  # noqa: BLE001 — never affect the exec ALLOW
            _log.warning(
                "native_guard.exec_readonly_root_inject_error",
                exc_info=True,
            )

    # ------------------------------------------------------------------
    # Exec gates (extracted from enforce_exec — one responsibility each)
    # ------------------------------------------------------------------
    async def _gate_exec_broker(self, *, command: str, caller: str) -> bool:
        """Gate ② — exec-broker profile classification (ALLOW/ASK/DENY).

        M-2 fix: in V1 the exec-broker profile
        gate is governed ONLY by the broker's own ``.enabled`` flag — it is
        NOT short-circuited by the ``file_guard`` master toggle (default OFF).
        Running it here, BEFORE the ``file_guard_enabled`` gate, restores that:
        a user who enabled exec_broker but left file_guard OFF still gets the
        constraint enforced. When the broker is disabled ``evaluate`` returns
        ALLOW, so this is a safe no-op. (dep_broker is NOT run here: it has an
        independent, default-ON path via PatternFileScreen and re-running it
        pre-gate would risk a double approval prompt.)

        Returns ``True`` when the ASK path fully decided the command (approved
        or covered by a grant) so the caller must ``return`` without consulting
        Gate ③. Returns ``False`` on ALLOW (caller proceeds to later gates).
        Raises :class:`ToolGuardDenied` on DENY or an ASK the user rejected.

        A broker *classification* failure must NOT block exec (V1 swallows) —
        but a failure in the ASK/grant machinery AFTER a command was classified
        dangerous is fail-closed (DENY), isolated in ``_exec_broker_ask``.
        """
        if self._exec_broker is None:
            return False
        try:
            # Three-way classification (2026-07-06 guard-rail redesign):
            # ALLOW → proceed; ASK → user decides; DENY → hard block + reason.
            from qai.command_policy.domain import ExecAction

            action, reason, _profile = self._exec_broker.evaluate(
                command, project_root=self._project_root
            )

            if action is ExecAction.DENY:
                # ``reason`` is a structured ``ClassifyReason`` (code + args +
                # English fallback text). This message is fed to the LLM (not
                # the user dialog), so it is neutral English keyed off the
                # stable ``code`` — never hardcoded Chinese. The io-escape code
                # gets path-oriented guidance; everything else gets the generic
                # "don't try to bypass" guidance.
                if reason.code == "deny_io_output_escape":
                    # io_constraints path violation — the command itself is not
                    # dangerous; the path is simply outside the allowed scope.
                    suffix = (
                        "The referenced path is outside the security policy's "
                        "allowed directories. Use an absolute path, or make "
                        "sure the target path is inside the workspace / project "
                        "directory."
                    )
                else:
                    # Dangerous argument / hard-deny rule — the command itself
                    # is the problem. Use the single-source concise FileGuard
                    # denial hint (2026-07-23 V2) so the model sees the SAME
                    # subject-focused guidance every FileGuard denial path
                    # emits: enforced boundary, retry-won't-work, ask the user
                    # to authorize. ``build_fileguard_denial_hint`` prefixes
                    # ``\n\n`` for the append use case; here we're joining onto
                    # ``reason.text`` with a single ``\n``, so strip the
                    # leading newlines to avoid a triple gap.
                    suffix = build_fileguard_denial_hint().lstrip("\n")
                raise ToolGuardDenied(
                    message=f"{reason.text}\n{suffix}",
                    error_code="ai_coding.tool.exec_denied",
                )
            if action is ExecAction.ASK:
                return await self._exec_broker_ask(
                    command=command, caller=caller, reason=reason
                )
            # ALLOW → fall through to the later gates.
            return False
        except ToolGuardDenied:
            raise
        except Exception as _cls_exc:  # noqa: BLE001
            # A broker CLASSIFICATION failure must not block exec (V1 swallows
            # → allow). Record it (Phase 2 step 3: no longer a silent
            # ``except: pass``) so the "broker misbehaved" window is auditable.
            self._emergency_audit(
                op="exec",
                path=command,
                caller=caller,
                reason=f"command_policy_classify_error:{_cls_exc}",
            )
            return False

    async def _exec_broker_ask(
        self, *, command: str, caller: str, reason: "ClassifyReason"
    ) -> bool:
        """Handle an exec-broker ASK: grant-reuse → dialog → decision.

        Returns ``True`` when the command is authorised (a still-valid grant
        already covers it, or the user approved the dialog). Raises
        :class:`ToolGuardDenied` when the user rejects / the dialog is not
        wired / the ASK machinery errors.

        SECURITY (fail-closed): once a command is CLASSIFIED dangerous (ASK),
        any error in the ask/grant machinery DENIES — it never falls back to
        the broker-classification swallow in ``_gate_exec_broker`` (which is
        only for a *classification* failure). This is isolated here so the two
        failure modes stay distinct.
        """
        from qai.security.domain.value_objects import (
            AceMask,
            Resource,
            Subject,
        )

        try:
            # Grant-scope reuse (2026-07-06): honour a still-valid session /
            # process / permanent grant for this exact command silently (no
            # repeat prompt). Mirrors FileGuard's path-grant behaviour; the
            # grant was stored by ApprovePermissionUseCase with path=command.
            if await self._exec_grant_allows(command):
                return True
            # Reuse the FileGuard permission-dialog infra so the user decides.
            # When the ASK collaborators are not wired, fail-closed to DENY.
            if (
                self._request_permission is not None
                and self._wait_registry is not None
            ):
                outcome = await self._ask_user(
                    subject=Subject(kind="system", identifier="ai_coding.tool"),
                    resource=Resource(kind="exec", identifier=command),
                    requested_mask=AceMask(execute=True),
                    op="exec",
                    path=command,
                    caller=caller,
                    reason=reason.text,
                    reason_code=reason.code,
                    reason_args=reason.args,
                )
            else:
                outcome = _AskOutcome(allow=False, timed_out=False)
        except ToolGuardDenied:
            raise
        except Exception as _ask_exc:  # noqa: BLE001
            # fail-closed: a dangerous command whose ASK plumbing errored is
            # DENIED, not silently run.
            self._emergency_audit(
                op="exec",
                path=command,
                caller=caller,
                reason=f"exec_ask_error:{_ask_exc}",
            )
            raise ToolGuardDenied(
                message=(
                    f"{reason.text}\nThe authorization flow for this command "
                    "errored, so it was denied by security policy "
                    "(fail-closed). Retry later, or use a safe approach "
                    "without high-risk arguments."
                ),
                error_code="ai_coding.tool.exec_denied",
            ) from _ask_exc
        if not outcome.allow:
            if outcome.timed_out:
                # Bug 4 fix: a TIMEOUT is NOT a user rejection. Tell the model
                # to have the user click Allow and retry the SAME command —
                # do NOT let it conclude the command is forbidden and go
                # mutate/rebrand it (the "switch to pypdf / add --native-tls"
                # blind-retry loop the user observed).
                raise ToolGuardDenied(
                    message=(
                        "This command needs the user to authorize it in the "
                        "security confirmation dialog, but no user response "
                        f"arrived within the wait window (timeout). Reason: "
                        f"{reason.text}\n"
                        "Ask the user to click 'Allow' in the popped-up "
                        "security dialog, then retry the EXACT same command. "
                        "Do not switch libraries, mutate arguments, or split "
                        "the command — none of that bypasses authorization; it "
                        "only creates more pending confirmations."
                    ),
                    error_code="ai_coding.tool.exec_ask_timeout",
                )
            raise ToolGuardDenied(
                message=(
                    f"The user rejected this command's execution request. "
                    f"Reason: {reason.text}\n"
                    "Do not retry or mutate to bypass; use a safe approach "
                    "without high-risk arguments, or ask the user before "
                    "continuing."
                ),
                error_code="ai_coding.tool.exec_denied",
            )
        # ASK approved (or covered by a grant): the exec gate decided.
        return True

    async def _gate_dep_broker(self, *, command: str, caller: str) -> None:
        """Gate ① — dep-install approval (V1).

        Runs the full V1 approval loop (enqueue → notify → block until the
        operator approves / rejects / the timeout elapses) via ``check_and_wait``
        when available, falling back to the non-blocking ``check`` probe for
        brokers that predate the closed loop. Skipped entirely when the
        pure-software FileBroker already owns the approval (default ON) — it
        runs first, so re-doing it here would double-prompt.

        Best-effort: a broker failure must not block exec (V1 swallows → allow);
        Phase 2 step 3 records the swallowed error instead of silently passing.
        """
        if self._dep_broker is None or self._dep_handled_externally:
            return
        try:
            if self._dep_broker.is_dep_install_command(command):
                _caw = getattr(self._dep_broker, "check_and_wait", None)
                if _caw is not None:
                    should_block, reason = await _caw(command)
                else:
                    should_block, reason = self._dep_broker.check(command)
                if should_block:
                    raise ToolGuardDenied(
                        message=f"Dep Broker blocked this install command: {reason}",
                        error_code="ai_coding.tool.exec_denied",
                    )
        except ToolGuardDenied:
            raise
        except Exception as _dep_exc:  # noqa: BLE001
            # dep broker failure should not block exec (V1 swallows → allow);
            # record it (Phase 2 step 3: no longer a silent ``except: pass``).
            self._emergency_audit(
                op="exec",
                path=command,
                caller=caller,
                reason=f"dependency_approval_error:{_dep_exc}",
            )


    # ------------------------------------------------------------------
    # Internal: PolicyCenter probe (fail-closed)
    # ------------------------------------------------------------------
    async def _enforce_path(
        self,
        *,
        path: str,
        read: bool,
        write: bool,
        execute: bool = False,
        delete: bool = False,
        resource_kind: str = "path",
        error_code: str,
        caller: str = "ai_coding.tool",
        op: str = "path",
        exec_command: str | None = None,
    ) -> None:
        """Consult the security CheckPermissionUseCase; deny on miss.

        ALLOW → return (pass). DENY / any other decision → raise
        ``ToolGuardDenied``. When the use case is missing the bridge
        fails closed (raise) only when ``self._enabled`` — but callers
        of this method already gate on ``self._enabled``. A security
        evaluation exception is fail-closed (raise).

        7-M9 — fail-closed paths (no PolicyCenter / evaluation error) also
        write an emergency-audit JSONL row; exec denials produce a
        reason-classified message via :func:`_build_exec_error`.
        """
        if self._check is None:
            # No PolicyCenter wired but FileGuard is ON → fail closed.
            self._emergency_audit(
                op=op, path=path, caller=caller, reason="policy_center_unavailable"
            )
            raise ToolGuardDenied(
                message=(
                    _build_exec_error(exec_command, "policy_center_unavailable")
                    if exec_command is not None
                    else (
                        "Security policy center unavailable; treated as denied "
                        "(fail-closed)." + build_fileguard_denial_hint()
                    )
                ),
                error_code=error_code,
            )

        # lazy-import the security VOs only on the ON path so the bridge
        # never imports qai.security at module load (keeps coupling thin).
        from qai.security.domain.value_objects import (
            AceMask,
            PolicyAction,
            Resource,
            Subject,
        )

        # SEC — thread the CURRENT scope context so ``session``/``process``
        # grants match only where they should: the top-level conversation id
        # (from the per-request contextvar bound at the ToolPort boundary) and
        # this backend process's boot id. Missing context → only permanent
        # grants apply (fail-safe).
        try:
            from qai.ai_coding.infrastructure.tools.handlers import (
                get_conversation_scope,
            )

            conversation_id = get_conversation_scope() or ""
        except Exception:  # noqa: BLE001 — never break the check on plumbing
            conversation_id = ""
        boot_id = self._boot_id_provider() if self._boot_id_provider else ""

        try:
            result = await self._check.execute(  # type: ignore[attr-defined]
                subject=Subject(kind="system", identifier="ai_coding.tool"),
                resource=Resource(kind=resource_kind, identifier=path),
                requested_mask=AceMask(
                    read=read, write=write, execute=execute, delete=delete
                ),
                op=op,
                scope_conversation_id=conversation_id,
                scope_boot_id=boot_id,
            )
        except Exception as exc:  # fail-closed on evaluation error
            self._emergency_audit(
                op=op, path=path, caller=caller, reason=f"evaluation_error:{exc}"
            )
            raise ToolGuardDenied(
                message=(
                    _build_exec_error(exec_command, "policy_center_unavailable")
                    if exec_command is not None
                    else (
                        f"Security policy evaluation error; treated as denied "
                        f"(fail-closed): {exc}" + build_fileguard_denial_hint()
                    )
                ),
                error_code=error_code,
            ) from exc

        # DIAG (2026-07-28) — post-decision snapshot. Emitted for EVERY
        # ``_enforce_path`` outcome (ALLOW returns immediately below, so an
        # ALLOW leaves exactly one row; a DENY-path emits this row and then
        # either enters ``_ask_user`` or raises ``ToolGuardDenied``). Grep
        # ``filesec.decision`` in the service log to see, per attempt, which
        # branch fired and why — in particular ``would_ask=False`` reveals a
        # ``dynamic_authorization`` off / channel-blocked miss (no dialog),
        # while ``would_ask=True`` + missing collaborators reveals a wiring
        # gap (also no dialog).
        _decision_obj = getattr(result, "decision", None)
        _log.info(
            "filesec.decision",
            extra={
                "op": op,
                "path": path,
                "caller": caller,
                "read": bool(read),
                "write": bool(write),
                "execute": bool(execute),
                "delete": bool(delete),
                "decision": getattr(_decision_obj, "name", str(_decision_obj)),
                "would_ask": bool(getattr(result, "would_ask", False)),
                "ask_block_reason": getattr(result, "ask_block_reason", None),
                "matched_rule_id": getattr(result, "matched_rule_id", None),
                "matched_grant_id": getattr(result, "matched_grant_id", None),
                "audit_only_override": bool(
                    getattr(result, "audit_only_override", False)
                ),
                "has_request_permission": self._request_permission is not None,
                "has_wait_registry": self._wait_registry is not None,
                "is_exec_command": exec_command is not None,
                # scope_conversation_id: the per-request contextvar bound at
                # the ToolPort boundary. Empty ("") means "no conversation
                # context" — which disables the workspace-subtree allow
                # short-circuit (``check_permission.py`` line ~688) and so
                # matters when a write on a workspace-subtree path
                # unexpectedly falls through to policy DENY: an empty value
                # here is the smoking gun.
                "scope_conversation_id": conversation_id,
            },
        )
        if result.decision is PolicyAction.ALLOW:
            return
        # P0 ASK restore — a would-have-asked miss (dynamic authorization on,
        # not a hard deny-rule hit, interactive channel) pops the dialog and
        # blocks for the user's decision instead of failing closed (V1
        # ``Decision.ASK`` → ``PolicyCenter.ask_user``). When the ASK
        # collaborators are not wired (or the use case did not flag
        # ``would_ask``) we keep the original fail-closed DENY.
        if (
            getattr(result, "would_ask", False)
            and self._request_permission is not None
            and self._wait_registry is not None
        ):
            outcome = await self._ask_user(
                subject=Subject(kind="system", identifier="ai_coding.tool"),
                resource=Resource(kind=resource_kind, identifier=path),
                requested_mask=AceMask(
                    read=read, write=write, execute=execute, delete=delete
                ),
                op=op,
                path=path,
                caller=caller,
            )
            if outcome.allow:
                return
            # Bug 4 fix: a confirmation TIMEOUT is not a policy denial — tell
            # the caller/model to have the user click Allow and retry the same
            # operation, rather than treating the path as a hard-blocked
            # boundary and giving up / trying to bypass it.
            if outcome.timed_out:
                raise ToolGuardDenied(
                    message=(
                        f"Operation {op} on {path} needs the user to authorize "
                        f"it in the security confirmation dialog, but no user "
                        f"response arrived within the wait window (timeout).\n"
                        "Ask the user to click 'Allow' in the popped-up "
                        "security dialog, then retry the EXACT same operation. "
                        "Do not switch tools, mutate the path, or split the "
                        "request — none of that bypasses authorization; it "
                        "only creates more pending confirmations."
                    ),
                    error_code="ai_coding.tool.ask_timeout"
                    if exec_command is None
                    else error_code,
                )
        # DENY (explicit deny rule, ASK rejected, or ASK not wired). exec gets
        # a reason-classified message.
        if exec_command is not None:
            # 7-M9 — the PolicyCenter ``CheckPermissionResult`` exposes no
            # granular exec reason code (it has ``ask_block_reason`` for the
            # channel/rate-limit cases, never an exec-gate code), so a plain
            # PolicyCenter DENY maps to the generic exec guidance (Allow Lists
            # / Skill Capabilities) — the most actionable default. We pass the
            # explicit domain code instead of the historical dead
            # ``getattr(result, "reason", "")`` read (which always yielded "").
            from qai.security.domain import ExecDenyReason

            raise ToolGuardDenied(
                message=_build_exec_error(
                    exec_command, ExecDenyReason.POLICY_CENTER_DENY.value
                ),
                error_code=error_code,
            )
        # Path deny — use the single-source concise FileGuard denial hint
        # (2026-07-23 V2). Same subject-focused guidance every FileGuard
        # denial path emits (enforced boundary, retry-won't-work, ask the
        # user to authorize); strip leading newlines because
        # ``build_fileguard_denial_hint`` is designed for the "append to
        # arbitrary text" use case (prefixes ``\n\n``) whereas this message
        # IS the whole body.
        raise ToolGuardDenied(
            message=build_fileguard_denial_hint().lstrip("\n"),
            error_code=error_code,
        )

    async def _exec_grant_allows(self, command: str) -> bool:
        """Return ``True`` iff a still-valid grant already allows ``command``.

        Consults :class:`CheckPermissionUseCase` for an ``exec``-kind
        resource whose identifier is the command string. When the user
        previously approved this exact command under a session / process /
        permanent scope, ``ApprovePermissionUseCase`` stored a grant with
        ``path == command``; ``check_permission`` (whose kind gate was
        widened to ``path`` / ``exec``) then returns ``ALLOW`` on a scope +
        exact-path + mask match — so we skip the dialog. Any error or a
        non-ALLOW decision → ``False`` (fall through to the normal ASK
        dialog); never raises (a grant-lookup hiccup must not break the
        exec gate).

        Note: exec commands carry no matching allow-*rule*, so a non-grant
        ALLOW cannot occur here — an ``ALLOW`` from ``check_permission`` for
        an exec resource is necessarily grant-driven, which is exactly what
        we want to honour silently.
        """
        if self._check is None:
            return False
        # lazy-import the security VOs (module keeps qai.security coupling thin).
        from qai.security.domain.value_objects import (
            AceMask,
            PolicyAction,
            Resource,
            Subject,
        )

        try:
            from qai.ai_coding.infrastructure.tools.handlers import (
                get_conversation_scope,
            )

            conversation_id = get_conversation_scope() or ""
        except Exception:  # noqa: BLE001 — never break the gate on plumbing
            conversation_id = ""
        boot_id = self._boot_id_provider() if self._boot_id_provider else ""
        try:
            result = await self._check.execute(  # type: ignore[attr-defined]
                subject=Subject(kind="system", identifier="ai_coding.tool"),
                resource=Resource(kind="exec", identifier=command),
                requested_mask=AceMask(execute=True),
                op="exec",
                scope_conversation_id=conversation_id,
                scope_boot_id=boot_id,
            )
        except Exception:  # noqa: BLE001 — grant lookup must not block exec
            return False
        return getattr(result, "decision", None) is PolicyAction.ALLOW

    async def _ask_user(
        self,
        *,
        subject: object,
        resource: object,
        requested_mask: object,
        op: str,
        path: str,
        caller: str,
        reason: str = "",
        reason_code: str = "",
        reason_args: "dict[str, str] | None" = None,
    ) -> _AskOutcome:
        """Pop the authorization dialog and block for the user's decision.

        V1 ``PolicyCenter.ask_user``: creates a
        PENDING request (→ SSE ``permission_request`` event via the
        :class:`PermissionRequestedEvent` published by the request use case),
        registers a waiter, then blocks (``_ask_timeout_sec``; default
        ``None`` = infinite, 2026-07-07 Bug 2 fix) until the front-end
        ``approve`` / ``reject`` route resolves it. Returns an
        :class:`_AskOutcome` — ``allow`` (ALLOW vs DENY/reject/timeout) plus
        ``timed_out`` so callers can tell a real reject from an expired wait
        (Bug 4: never report a timeout to the model as "user rejected").

        ``reason`` (optional) is a human-readable explanation of *why* the
        command needs confirmation (e.g. "带有高风险参数 --force"); it is
        forwarded to the request use case so the dialog can show it. Passed
        best-effort — a use case that does not accept ``reason`` still works.

        Race-safety (State-Truth-First): the waiter is registered BEFORE the
        request is created so an instant smart-approval auto-resolution (or a
        very fast operator click) can never slip through the gap between
        "request created / event published" and "waiter registered".
        """
        # Pre-allocate the waiter id is impossible (the request id is minted
        # inside the use case), so we create the request first then register.
        # The use case publishes the SSE event AND only runs smart-approval
        # AFTER publishing; smart-approval resolves through the SAME registry
        # we register on below, and ``resolve`` on a not-yet-registered id is
        # a silent no-op that leaves the request PENDING — the operator can
        # still resolve it via the route. To close even that small window we
        # register immediately after obtaining the id and re-check pending.
        try:
            _rp_kwargs: dict = dict(
                subject=subject,
                resource=resource,
                requested_mask=requested_mask,
            )
            if reason or reason_code:
                # Forward the reason (+ structured code/args for i18n) only if
                # the use case accepts each param, so a use case predating any
                # of them is unaffected (guarded per-param).
                try:
                    import inspect

                    _params = inspect.signature(
                        self._request_permission.execute  # type: ignore[attr-defined]
                    ).parameters
                    if reason and "reason" in _params:
                        _rp_kwargs["reason"] = reason
                    if reason_code and "reason_code" in _params:
                        _rp_kwargs["reason_code"] = reason_code
                    if reason_code and "reason_args" in _params:
                        _rp_kwargs["reason_args"] = dict(reason_args or {})
                except (TypeError, ValueError):
                    pass
            request = await self._request_permission.execute(  # type: ignore[attr-defined]
                **_rp_kwargs
            )
        except Exception as exc:  # noqa: BLE001 — plumbing error, not a user decision
            # Fail-CLOSED for authorisation (allow=False) BUT surface this as
            # ``timed_out=True`` (M-Py-2, 2026-07-28): a plumbing exception
            # while minting the ASK request means the user never saw a dialog,
            # so the DENY outcome is NOT a genuine user reject. Reporting it
            # as ``timed_out=False`` mapped to "user rejected — do not retry"
            # in the tool-layer message, driving the model down the "被拒了
            # 别重试 / switch library" branch even though the user was never
            # asked. Aligning with the wait-timeout branch (Bug 4) tells the
            # caller/model: "the system did not get a decision — please have
            # the user click Allow and retry the SAME command / path". The
            # emergency_audit trail below is unchanged so the plumbing fault
            # is still recorded.
            self._emergency_audit(
                op=op, path=path, caller=caller, reason=f"ask_request_error:{exc}"
            )
            return _AskOutcome(allow=False, timed_out=True)

        request_id = request.request_id.value
        # (探针 ask_created 已挪到 _conv 定义之后，见下方。)
        # SEC true-scoping (PART D) — remember which conversation this ASK
        # belongs to, keyed by the minted request_id, so the approve route can
        # scope a session grant to it. Captured from the same contextvar the
        # decision path already read (``get_conversation_scope()``); best-effort
        # so a plumbing hiccup never breaks the ASK.
        try:
            from qai.ai_coding.infrastructure.tools.handlers import (
                get_conversation_scope,
            )

            _conv = get_conversation_scope() or ""
        except Exception:  # noqa: BLE001 — capture is best-effort
            _conv = ""
        if _conv:
            self._ask_registry.remember(request_id, _conv)
        # DIAG (2026-07-28) — the request use case published the SSE
        # ``permission_request`` event just now; grep ``filesec.ask_created``
        # in the log to confirm the dialog was actually enqueued for the
        # front-end (and see whether smart-approval auto-resolved it).
        _log.info(
            "filesec.ask_created",
            extra={
                "request_id": request_id,
                "op": op,
                "path": path,
                "caller": caller,
                "initial_state": getattr(
                    getattr(request, "state", None), "value", None
                ),
                "conversation_id": _conv,
            },
        )
        # If the request was already auto-resolved by smart-approval inside
        # the use case, honour that terminal state without blocking.
        state = getattr(request, "state", None)
        state_value = getattr(state, "value", state)
        if state_value == "approved":
            return _AskOutcome(allow=True, timed_out=False)
        if state_value in ("rejected", "cancelled", "expired"):
            # Terminal non-approve → no session grant will be created, so drop
            # the registry entry now (only the approve route consumes it).
            self._ask_registry.take(request_id)
            # ``expired`` is a timeout-class terminal state; ``rejected`` /
            # ``cancelled`` are genuine non-timeout denies.
            return _AskOutcome(
                allow=False, timed_out=(state_value == "expired")
            )

        # Still PENDING — register the waiter and block for the decision.
        resolution = await self._wait_registry.wait(  # type: ignore[union-attr]
            request_id, timeout=self._ask_timeout_sec
        )
        # DIAG (2026-07-28) — the wait ended. ``timed_out=True`` = the user
        # never responded within ``_ask_timeout_sec``; otherwise ``allow``
        # is the operator's approve/reject verdict.
        _log.info(
            "filesec.ask_resolved",
            extra={
                "request_id": request_id,
                "op": op,
                "path": path,
                "caller": caller,
                "allow": bool(getattr(resolution, "allow", False)),
                "timed_out": bool(getattr(resolution, "timed_out", False)),
            },
        )
        if resolution.timed_out:
            self._emergency_audit(
                op=op, path=path, caller=caller, reason="ask_timeout"
            )
        # Clean up the conversation-scope registry entry unless this resolved
        # to an ALLOW that the approve route will consume. On timeout / DENY
        # the approve route never runs ``take()``, so drop it here to avoid a
        # slow leak of stale entries (the 512-cap is only a backstop).
        if not resolution.allow:
            self._ask_registry.take(request_id)
        return _AskOutcome(
            allow=bool(resolution.allow),
            timed_out=bool(getattr(resolution, "timed_out", False)),
        )

    # ------------------------------------------------------------------
    # Per-file read probes (non-raising) — restore V1 glob/grep per-file
    # / per-line FileGuard filtering (退化 #10). Both are fail-open and
    # treat the master switch OFF as ALLOW (V1 ``enabled=false → ALLOW``).
    # ------------------------------------------------------------------
    async def _read_decision(self, path: str) -> "PolicyAction | None":
        """Evaluate the read decision for ``path`` (non-raising probe).

        Returns the :class:`PolicyAction` from the PolicyCenter, or
        ``None`` when no use case is wired / evaluation errors (callers
        treat ``None`` as fail-open ALLOW, per V1).
        """
        if self._check is None:
            return None
        from qai.security.domain.value_objects import (
            AceMask,
            Resource,
            Subject,
        )

        try:
            result = await self._check.execute(  # type: ignore[attr-defined]
                subject=Subject(kind="system", identifier="ai_coding.tool"),
                resource=Resource(kind="path", identifier=path),
                requested_mask=AceMask(read=True, write=False, execute=False),
            )
        except Exception:  # noqa: BLE001 — fail-open per V1
            return None
        decision: PolicyAction = result.decision
        return decision

    async def is_read_allowed(self, *, path: str) -> bool:
        # V1 ``enabled=false → ALLOW`` (policy.py:877/893).
        if not self._enabled:
            return True
        from qai.security.domain.value_objects import PolicyAction

        decision = await self._read_decision(path)
        if decision is None:
            return True  # fail-open (V1 ``allowed = True``)
        # The V2 PolicyCenter decision is binary (ALLOW / DENY) — there is
        # no synchronous ASK state on this path — so only an explicit DENY
        # drops a file from glob/grep results (V1 ``check_read != ALLOW``).
        return decision is PolicyAction.ALLOW

    async def is_statically_allowed(self, *, path: str) -> bool:
        """See :meth:`FileGuardPort.is_statically_allowed`.

        (M-Py-3, 2026-07-28) consults ONLY the STATIC allowlist provider
        (``resolve_global_allow_paths`` ∪ ``resolve_read_only_allow_paths``),
        NOT the PolicyCenter decision. This restores the V1 ``explain_read``
        semantics ``glob``/``grep`` depend on to distinguish:

        * STATIC allow (root in the whitelist) → True → run per-file
          :meth:`is_read_allowed` filtering inside the tree (a whitelisted
          root may still contain individually excluded sub-paths);
        * ASK / dynamic authorisation (root was granted whole-tree by the
          user) → False → SKIP per-file filtering (the user already
          authorised the whole subtree — over-filtering it would hide
          files the user explicitly approved).

        The previous implementation shared ``_read_decision`` with
        :meth:`is_read_allowed`, collapsing STATIC and ASK-grant into the
        same ALLOW verdict — an ASK-tree then wrongly enabled per-file
        filtering. This method is now decoupled: it never invokes the
        PolicyCenter (an ASK-tree grant emits ALLOW there, indistinguishable
        from STATIC).
        """
        # V1 ``enabled=false → ALLOW`` — no static allowlist gating when
        # the master switch is OFF, so per-file filtering is skipped.
        if not self._enabled:
            return True
        # No provider wired (hand-rolled test container) → fail-open True
        # so the pre-M-Py-3 "no gating" behaviour is preserved byte-for-byte
        # for callers that never opted into the new provider. Production
        # ``build_file_guard`` always wires one.
        if self._static_allow_provider is None:
            return True
        try:
            prefixes = tuple(self._static_allow_provider())
        except Exception:  # noqa: BLE001 — provider fault → fail-open True
            return True
        # A provider that returned nothing counts as "STATIC allowlist is
        # empty this call" — a genuine no-match, NOT a fault. Return False
        # so callers correctly SKIP per-file filtering (an empty static
        # allowlist means every root reaching this probe was granted
        # dynamically).
        return _path_under_any_static_prefix(path, prefixes)


def build_file_guard(container: "Container") -> FileGuardPort:
    """Compose the production :class:`FileGuardPort` from the container.

    Reads the master switches from ``container.settings.security`` and
    fronts ``container.security.check_permission_use_case`` +
    ``container.dependency_approval.broker`` + ``container.command_policy.broker``.
    Any missing namespace (hand-rolled test containers) degrades to
    :class:`NoopFileGuard` so non-security wiring keeps working.
    """
    settings = getattr(container, "settings", None)
    security_settings = getattr(settings, "security", None) if settings else None
    if security_settings is None:
        return NoopFileGuard()

    file_guard_enabled = bool(
        getattr(security_settings, "file_guard_enabled", False)
    )
    allow_exec_tool = bool(getattr(security_settings, "allow_exec_tool", True))

    security = getattr(container, "security", None)
    check_permission = (
        getattr(security, "check_permission_use_case", None)
        if security is not None
        else None
    )
    # P0 ASK restore — the request-permission use case (creates PENDING +
    # publishes the SSE ``permission_request`` event) + the process-wide
    # async wait registry (woken by approve / reject routes). Both come from
    # the security namespace via this apps-layer bridge so ``qai.ai_coding``
    # never imports ``qai.security`` (context-isolation contract).
    request_permission = (
        getattr(security, "request_permission_use_case", None)
        if security is not None
        else None
    )
    wait_registry = (
        getattr(security, "permission_wait_registry", None)
        if security is not None
        else None
    )
    dep_ns = getattr(container, "dependency_approval", None)
    dep_broker = getattr(dep_ns, "broker", None) if dep_ns is not None else None

    exec_ns = getattr(container, "command_policy", None)
    exec_broker = (
        getattr(exec_ns, "broker", None) if exec_ns is not None else None
    )

    project_root = str(getattr(container, "repo_root", "") or "")

    # 7-M9 — emergency-audit JSONL path under the data dir's security
    # folder (alongside ``active_policy.json``). Best-effort: resolved from
    # the container's data paths when available, else None (audit disabled).
    emergency_audit_path: "Path | None" = None
    try:
        _data_paths = getattr(container, "data_paths", None)
        _root = getattr(_data_paths, "root", None) if _data_paths else None
        if _root is not None:
            emergency_audit_path = (
                Path(_root) / "security" / "emergency_audit.jsonl"
            )
    except Exception:  # noqa: BLE001
        emergency_audit_path = None

    # The pure-software FileBroker (default ON) owns the dep-install approval
    # loop and runs before this gate, so tell FileGuard to skip its own Gate①
    # dep approval when FileBroker is active — avoids a double prompt.
    tools_settings = getattr(settings, "tools", None) if settings else None
    file_broker_active = bool(
        getattr(tools_settings, "file_broker_enabled", True)
    )

    # 2026-07-08 — live provider reporting whether the native OS file guard
    # (guard64.dll) is ACTIVE, read from the SAME instance DI built
    # (``container.security.native_file_guard.is_active`` — State-Truth-First:
    # the real runtime state, not just the ``native_file_guard_enabled``
    # setting, so a DLL that failed to load reads as inactive). Used to skip
    # the redundant command-level exec ASK only when the file-layer backstop
    # is genuinely in place. Missing namespace / error → False (conservative:
    # keep the original command-level ASK).
    def _native_guard_active_provider() -> bool:
        nfg = getattr(security, "native_file_guard", None) if security else None
        if nfg is None:
            return False
        try:
            # State-Truth-First (2026-07-08 security audit fix — leak 4):
            # "active" for the purpose of SKIPPING the command-level exec ASK
            # must mean the native layer will ACTUALLY guard this exec's child
            # process — not merely that the host DLL init'd. The native hook
            # only guards children carrying a non-empty QAI_FILEGUARD_GUARD_TOKEN
            # (inverted guard model). If the trust token failed to register
            # (``set_trusted_infra_token`` failed → token is None) the exec
            # child is NOT injected/guarded, so skipping Gate ③ would leave its
            # file ops with NO backstop (native bypasses an untokened child).
            # Require BOTH: DLL active AND a usable guard token. Missing token →
            # return False → keep the Gate ③ ASK (no silent bypass window).
            if not bool(getattr(nfg, "is_active", False)):
                return False
            _tok = getattr(nfg, "get_trusted_infra_token", None)
            if _tok is None:
                return False
            return bool(_tok())
        except Exception:  # noqa: BLE001 — uncertainty → conservative (inactive)
            return False

    # M-Py-3 (2026-07-28) — live provider assembling the STATIC allow
    # prefixes so ``is_statically_allowed`` can decide "STATIC vs ASK-tree
    # grant" WITHOUT invoking the PolicyCenter (whose binary ALLOW verdict
    # cannot distinguish the two). Reads the SAME sources the native
    # guard64.dll allow-list seed and the Python global-allow short-circuit
    # use (``resolve_global_allow_paths`` + ``resolve_read_only_allow_paths``)
    # so all three layers stay in sync (State-Truth-First). Lazy import so a
    # bare ``FileGuardFacade`` test callsite that never touches the resolver
    # module is unaffected; each provider fault degrades to an empty tuple,
    # which ``is_statically_allowed`` correctly reports as "no static match"
    # (fail-open on the wire-level fault is handled inside the method).
    def _static_allow_provider() -> tuple[str, ...]:
        try:
            from ._workspace_resolver import (
                resolve_global_allow_paths,
                resolve_read_only_allow_paths,
            )
        except Exception:  # noqa: BLE001 — resolver import failure
            return ()
        collected: list[str] = []
        for resolver in (resolve_global_allow_paths, resolve_read_only_allow_paths):
            try:
                collected.extend(resolver(container))  # type: ignore[arg-type]
            except Exception:  # noqa: BLE001 — per-resolver fault
                continue
        # De-dupe while preserving order (each resolver already de-dupes its
        # own output; a duplicate across the two lists is a benign no-op for
        # prefix-match but bloats the loop, so drop it here too).
        seen: set[str] = set()
        deduped: list[str] = []
        for entry in collected:
            key = entry.replace("\\", "/").casefold()
            if key in seen:
                continue
            seen.add(key)
            deduped.append(entry)
        return tuple(deduped)

    return FileGuardFacade(
        file_guard_enabled=file_guard_enabled,
        allow_exec_tool=allow_exec_tool,
        check_permission_use_case=check_permission,
        dep_broker=dep_broker,
        exec_broker=exec_broker,
        project_root=project_root,
        emergency_audit_path=emergency_audit_path,
        dep_handled_externally=file_broker_active,
        request_permission_use_case=request_permission,
        wait_registry=wait_registry,
        # Unified FileGuard master switch — live provider reading the current
        # ``file_guard_enabled`` off the settings object on every guard call,
        # so the master-switch route (PUT /api/security/runtime-config) can
        # flip it WITHOUT a restart (it mutates settings.security.
        # file_guard_enabled live + no longer forces a reboot). Falls back to
        # the baked value for hand-rolled test containers with no settings.
        enabled_provider=(
            (lambda: bool(getattr(security_settings, "file_guard_enabled", False)))
            if security_settings is not None
            else None
        ),
        # SEC true-scoping — a live provider returning THIS backend process's
        # boot id (minted once at startup in ``lifespan.py`` and stashed as
        # ``container.boot_id``). Read on every ``_enforce_path`` call so a
        # ``process``-scoped grant matches only within this process; after a
        # restart the container is rebuilt with a fresh boot id so stale
        # process grants stop matching. Missing attribute → "" → process
        # grants never match (fail-safe), matching hand-rolled test containers.
        boot_id_provider=lambda: getattr(container, "boot_id", ""),
        # 2026-07-08 — native-guard-active live provider (see FileGuardFacade
        # __init__): lets enforce_exec skip the redundant command-level Gate ③
        # ASK when the native path-allow-list backstop is genuinely active.
        native_guard_active_provider=(
            _native_guard_active_provider if security is not None else None
        ),
        # exec lib-storm mitigation — the native guard port whose
        # ``add_read_only_allow_rule`` pre-seeds an allowed executable's
        # install-tree as a read-only prefix so a toolchain driver's startup
        # read storm matches the white_ro prefix in the DLL instead of
        # flooding ASK popups. Injected in-process from ``enforce_exec`` before
        # the child spawns (the only layer that sees the un-rewritten command
        # string ahead of the read storm). ``None`` disables it.
        native_guard=(
            getattr(security, "native_file_guard", None)
            if security is not None
            else None
        ),
        # M-Py-3 (2026-07-28) — STATIC allowlist provider (see
        # ``_static_allow_provider`` above) so ``is_statically_allowed``
        # can distinguish STATIC vs ASK-tree grant WITHOUT invoking the
        # PolicyCenter (which cannot tell them apart on the binary decision).
        static_allow_provider=_static_allow_provider,
    )
