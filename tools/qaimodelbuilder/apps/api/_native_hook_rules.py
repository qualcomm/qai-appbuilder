# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Native FileGuard rule hot-sync (apps/api wiring root) — PR-4.

Seeds and live-syncs the native ``guard64.dll`` deny / allow prefix rules
from the security state, so the OS-level hook enforces the same
protected-path / workspace / granted-path boundaries the in-process
Python layer already honours:

Layering — native allow-list is PROCESS-SCOPED; true session isolation
lives in Python (SEC true-scoping, confirmed by user)
--------------------------------------------------------------------
The native ``guard64.dll`` allow-list is a **process-global** prefix
whitelist: the DLL has NO notion of a conversation / session — it only
sees OS file events from LLM-spawned sub-processes, which carry a
``process_path`` / ``pid`` but never a conversation id. Therefore:

* **Native layer = process granularity.** A ``session`` or ``process``
  grant, once created, pushes its path onto the native allow-list for the
  REMAINDER OF THIS PROCESS's lifetime (it cannot be scoped narrower at the
  DLL level). This is intentional and correct: a sub-process a session
  spawned needs to write the granted path while THIS backend process runs.
* **True session isolation is enforced ONLY in the Python layer** —
  :class:`CheckPermissionUseCase` filters grants via
  ``PathGrant.matches_scope(boot_id=..., conversation_id=...)``, so an
  in-process tool call in conversation *A* never matches a session grant
  minted for conversation *B*, even though both paths sit on the shared
  native allow-list. The native layer is a coarser, process-lifetime
  backstop; the fine-grained per-conversation gate is Python's job.

startup seeding (:func:`seed_native_guard_rules`):
    * ``Settings.security.protected_write_paths`` → DLL **deny** list
      (blacklist — a write under these prefixes is rejected).
    * the resolved workspace root → DLL **allow** list (whitelist — the
      model-build workspace bypasses the filter so normal project IO is
      never prompted).
    * every durable (**permanent** / non-expiring, non-expired)
      :class:`PathGrant` path → DLL **allow** list, restoring the
      "permanent grant means never ask again for this path" contract
      across restarts.

    NOTE — startup seed is deliberately **permanent-only**: at boot the only
    grants that meaningfully belong to THIS process are the permanent ones.
    A ``session`` / ``process`` grant persisted by a PRIOR process run is
    stale (its ``scope_key`` = an old conversation id / old boot id that the
    Python matcher will never match against the fresh boot id / a
    not-yet-existing conversation), so seeding it into the native allow-list
    would only widen the process-global whitelist for a grant that can never
    legitimately fire in Python. We skip them at startup and let the runtime
    subscriber (below) push freshly-approved session/process grants for the
    CURRENT process instead.

runtime hot-sync (:func:`subscribe_native_guard_grant_sync`):
    * ``PathGrantCreatedEvent`` → ``add_allow_rule(path)`` — a fresh
      grant of ANY scope (session / process / permanent) immediately
      whitelists its path in the live hook. This is the deliberate
      process-granularity layering: a session grant approved NOW is for a
      conversation that exists in THIS process, so its sub-processes must be
      able to write the path for the rest of this process's life; the Python
      layer still enforces that only that conversation's in-process tool
      calls match the grant.
    * ``PathGrantRevokedEvent`` → ``remove_allow_rule(path)`` — a
      revoked grant drops its whitelist entry so the next access
      re-prompts.

All mutations are best-effort: the :class:`NativeFileGuardPort`
contract makes every mutator a silent no-op (returns ``False``) when the
guard is inactive / disabled, so this module is safe to invoke
unconditionally — it simply does nothing when the hook is off.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from qai.security.application.ports import NativeFileGuardPort

__all__ = [
    "seed_native_guard_rules",
    "subscribe_native_guard_grant_sync",
    "start_native_guard",
    "stop_native_guard",
]

_log = get_logger(__name__)


async def seed_native_guard_rules(container: object) -> None:
    """Seed the native guard deny/allow lists from security state.

    No-op when the guard is inactive (the port mutators short-circuit).
    Never raises — a seeding fault is logged and swallowed so a rule
    hiccup cannot wedge startup.
    """
    security = getattr(container, "security", None)
    guard: "NativeFileGuardPort | None" = (
        getattr(security, "native_file_guard", None)
        if security is not None
        else None
    )
    if guard is None or not getattr(guard, "is_active", False):
        return

    settings = getattr(container, "settings", None)
    security_settings = getattr(settings, "security", None) if settings else None

    # 1) protected_write_paths → deny (blacklist), session_only so they are
    #    not persisted into the DLL's DPAPI store (they are re-seeded every
    #    startup from Settings — the durable source of truth). Includes the
    #    built-in, non-removable protected prefixes (``BUILTIN_PROTECTED_
    #    PREFIXES`` — currently ``C:\Qualcomm``) plus the operator
    #    ``protected_write_paths`` extras.
    #
    #    IMPORTANT — op-mask interaction: the native black list is an OP-AGNOSTIC
    #    TOTAL deny (checked before every white list; denies read+write+execute).
    #    A path that the base-environment op-mask config declares with a
    #    read/execute allow (e.g. ``C:\Qualcomm`` = read+execute, NO write) must
    #    therefore NOT be seeded as black — otherwise its READS would be wrongly
    #    hard-denied. For such a path the op-mask expresses the intent precisely:
    #    read/execute are allowed natively; a WRITE is NOT in the mask, so it
    #    falls through to the callback → Python check_permission (the op-mask
    #    provider does not cover write ⇒ default DENY) AND the always-on
    #    in-process ``protected_paths`` guard blocks in-process writes. So write
    #    protection is preserved without black hard-denying reads. We thus SKIP
    #    seeding a black prefix that is covered by an op-mask config entry.
    try:
        from qai.platform.protected_paths import BUILTIN_PROTECTED_PREFIXES

        builtin_protected = tuple(BUILTIN_PROTECTED_PREFIXES)
    except Exception:  # noqa: BLE001 — degrade to just the settings extras
        builtin_protected = ()
    protected = tuple(
        getattr(security_settings, "protected_write_paths", ()) or ()
    )
    # Paths the op-mask config governs (read/exec allow etc.) — these are NOT
    # black-listed (see rationale above); their write protection comes from the
    # op-mask fall-through + protected_paths guard.
    op_mask_keys = {
        str(p).replace("/", "\\").casefold()
        for p, _m in _resolve_file_guard_masked_paths(container)
    }
    # De-duplicate (case-insensitive) while preserving order: builtins first so
    # they are never dropped by an operator duplicate.
    _seen_deny: set[str] = set()
    deny_prefixes: list[str] = []
    for path in (*builtin_protected, *protected):
        if not path:
            continue
        key = str(path).replace("/", "\\").casefold()
        if key in _seen_deny:
            continue
        _seen_deny.add(key)
        if key in op_mask_keys:
            # governed by the op-mask config (read/exec allowed) — skip black.
            continue
        deny_prefixes.append(str(path))
    deny_count = 0
    for path in deny_prefixes:
        if guard.add_deny_rule(path, session_only=True):
            deny_count += 1

    # 2) workspace root → allow (whitelist). Resolved the same way the rest
    #    of lifespan resolves it (forge_config override → settings → default).
    allow_count = 0
    workspace_root = _resolve_workspace_root(container)
    if workspace_root and guard.add_allow_rule(
        workspace_root, session_only=True
    ):
        allow_count += 1

    # 2b) three-state GLOBAL allow prefixes (data/models roots + operator
    #     ``global_allow_paths``) → allow (whitelist). session_only=False:
    #     these are process-lifetime, session-independent whitelists (unlike
    #     the workspace root, which is re-seeded each startup and conceptually
    #     tied to the current run). Same single source of truth the Python
    #     ``CheckPermissionUseCase`` prefix ALLOW short-circuit reads
    #     (``resolve_global_allow_paths``), so both FileGuard layers stay in
    #     sync (State-Truth-First). Native allow rules are prefix + op-agnostic
    #     → one rule covers the subtree's read/write/execute.
    global_allow_count = 0
    for path in _resolve_global_allow_paths(container):
        if path and guard.add_allow_rule(str(path), session_only=False):
            global_allow_count += 1

    # 2c) op-aware READ-ONLY allow prefixes (business dirs + system read
    #     surface) → read-only allow (white_ro). session_only=False: these are
    #     process-lifetime, session-independent. Read is allowed; write / edit /
    #     delete / execute still route through the callback (-> ASK) — the
    #     native decision pipeline only skips the callback for read events on a
    #     ro-white prefix. Same single source of truth the Python
    #     ``CheckPermissionUseCase`` read-only ALLOW short-circuit reads
    #     (``resolve_read_only_allow_paths``), so both FileGuard layers stay in
    #     sync (State-Truth-First). Never degrades to a plain (op-agnostic)
    #     allow: a DLL predating the read-only export makes
    #     ``add_read_only_allow_rule`` a no-op (returns False), so the paths are
    #     simply left to ASK rather than being over-permitted (read + write).
    read_only_allow_count = 0
    for path in _resolve_read_only_allow_paths(container):
        if path and guard.add_read_only_allow_rule(str(path), session_only=False):
            read_only_allow_count += 1

    # 2d) TEMP read/write is now handled in the native layer (guard.cpp
    #     ``IsTempPath``) which matches C:\Users\*\AppData\Local\Temp and
    #     C:\Windows\Temp for any user account without depending on the API
    #     server process's %TEMP% environment variable. The Python-layer seed
    #     (resolve_temp_rw_paths) has been removed — it only covered the
    #     current user's Temp and missed child processes running as other users
    #     (e.g. Administrator). Native rule is authoritative; no seed needed.

    # 2e) FileGuard base-environment config (factory/config/file_guard_paths.json)
    #     → OP-MASKED allow (white_ops). Each entry is (path, mask) where mask
    #     bits are READ=1/WRITE=2/EXECUTE=4/DELETE=8. An op whose bit is set is
    #     allowed (skip callback); an op whose bit is unset falls through to the
    #     callback (-> ASK) — NOT force-allowed, NOT hard-denied (the black list
    #     is the only hard-deny; protected_write_paths still wins for writes).
    #     This is what expresses "read + execute but not write" (C:\Qualcomm)
    #     precisely. Same source the Python permission check reads
    #     (``resolve_file_guard_masked_paths``). Degrades to a no-op on a DLL
    #     predating the op-mask export (paths left to ASK, never over-permitted).
    op_mask_count = 0
    for path, mask in _resolve_file_guard_masked_paths(container):
        if path and guard.add_op_mask_allow_rule(
            str(path), int(mask), session_only=False
        ):
            op_mask_count += 1

    # 2e-bis) 2026-07-08 — per-program fixed-artifact path allowlist
    #     (factory/config/program_path_allowlist.json). Silences native ASK
    #     for a program's own runtime writes (e.g. powershell → PSReadLine
    #     history / module cache / profile dirs) that are unrelated to the
    #     user's command. Same op-mask mechanism as 2e; the loader enforces
    #     program-owned-subdir safety (never a bare user root). Degrades to a
    #     no-op on a DLL predating the op-mask export.
    program_allow_count = 0
    for path, mask in _resolve_program_allowlist_paths(container):
        if path and guard.add_op_mask_allow_rule(
            str(path), int(mask), session_only=False
        ):
            program_allow_count += 1

    # 2f) Phase 1 T5: system EXECUTE allow (op-masked, READ+EXECUTE, no WRITE).
    #     Lets host-spawned subprocesses run cmd.exe / PowerShell / system
    #     tools without ASK, while writes to system dirs remain gated. Solves
    #     the startup ``cmd.exe /c ver`` ASK regression the read-only whitelist
    #     could not cover (read-only skips only READ, not EXECUTE). Degrades
    #     to a no-op on a DLL predating the op-mask export (paths left to ASK,
    #     never over-permitted).
    system_exec_count = 0
    for path, mask in _resolve_system_exec_allow_paths(container):
        if path and guard.add_op_mask_allow_rule(
            str(path), int(mask), session_only=False
        ):
            system_exec_count += 1

    # 2g) Phase 1: runtime EXECUTE allow (interpreter dirs, op-agnostic) —
    #     already resolved by resolve_runtime_exec_allow_paths but previously
    #     not seeded onto the native FULL white list. The host re-executes its
    #     own interpreter to spawn workers; with the native guard ON that spawn
    #     triggers an EXECUTE event which the read-only whitelist cannot allow.
    #     Seeding here closes the gap (see resolve_runtime_exec_allow_paths
    #     docstring for the full rationale; ``resolve_global_allow_paths`` also
    #     folds these in for the Python global-allow short-circuit — this is
    #     the matching native seed).
    runtime_exec_count = 0
    for path in _resolve_runtime_exec_allow_paths(container):
        if path and guard.add_allow_rule(str(path), session_only=False):
            runtime_exec_count += 1

    # 3) durable (permanent / non-expiring) grants → allow (whitelist).
    grant_count = await _seed_permanent_grants(container, guard)

    _log.info(
        "native_file_guard.rules_seeded",
        deny=deny_count,
        allow=allow_count,
        global_allow=global_allow_count,
        read_only_allow=read_only_allow_count,
        op_mask=op_mask_count,
        program_allow=program_allow_count,
        system_exec=system_exec_count,
        runtime_exec=runtime_exec_count,
        grants=grant_count,
    )


def _resolve_workspace_root(container: object) -> str:
    """Resolve the workspace root string (best-effort, empty on failure)."""
    try:
        from apps.api._workspace_resolver import resolve_workspace_root

        root = resolve_workspace_root(container)
        return str(root) if root else ""
    except Exception:  # noqa: BLE001 — degrade to "no workspace allow rule"
        # fall back to repo_root when the resolver is unavailable
        repo_root = getattr(container, "repo_root", None)
        return str(repo_root) if repo_root else ""


def _resolve_global_allow_paths(container: object) -> tuple[str, ...]:
    """Resolve the global-allow prefixes (best-effort, empty on failure).

    Delegates to the shared :func:`resolve_global_allow_paths` so the native
    seed and the Python ``CheckPermissionUseCase`` short-circuit read the
    SAME source (the runtime data root + its sibling models root + the
    ``%LOCALAPPDATA%`` data/models pair + operator ``global_allow_paths``;
    the workspace root is session-scoped and seeded separately above).
    """
    try:
        from apps.api._workspace_resolver import resolve_global_allow_paths

        return resolve_global_allow_paths(container)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — degrade to "no global allow rules"
        return ()


def _resolve_read_only_allow_paths(container: object) -> tuple[str, ...]:
    """Resolve the op-aware read-only allow prefixes (best-effort, empty on fail).

    Delegates to the shared :func:`resolve_read_only_allow_paths` so the native
    read-only seed and the Python ``CheckPermissionUseCase`` read-only ALLOW
    short-circuit read the SAME source (the three business dirs + the system
    read surface + operator ``read_only_allow_paths`` / ``system_read_allow_paths``).
    """
    try:
        from apps.api._workspace_resolver import resolve_read_only_allow_paths

        return resolve_read_only_allow_paths(container)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — degrade to "no read-only allow rules"
        return ()


def _resolve_runtime_exec_allow_paths(container: object) -> tuple[str, ...]:
    """Resolve the Python-runtime EXECUTE allow prefixes (best-effort, empty).

    Delegates to :func:`resolve_runtime_exec_allow_paths` so the interpreter
    dirs the host's own worker spawns need to EXECUTE (sys.executable dir,
    sys.prefix, sys.base_prefix) are seeded onto the native FULL white list.
    Historically resolved but not wired into the native seed — that gap
    contributed to the startup ASK storm.
    """
    try:
        from apps.api._workspace_resolver import (
            resolve_runtime_exec_allow_paths,
        )

        return resolve_runtime_exec_allow_paths(container)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — degrade to "no runtime exec rules"
        return ()


def _resolve_system_exec_allow_paths(
    container: object,
) -> "tuple[tuple[str, int], ...]":
    """Resolve the system EXECUTE op-mask allow tuples (best-effort, empty).

    Delegates to :func:`resolve_system_exec_allow_paths` so host-spawned
    subprocesses can run system tools (cmd.exe / PowerShell / system utils)
    under an op-masked READ+EXECUTE allow (writes still gated). Solves the
    startup ``cmd.exe /c ver`` ASK regression the read-only whitelist could
    not cover (read-only skips only READ, not EXECUTE).
    """
    try:
        from apps.api._workspace_resolver import (
            resolve_system_exec_allow_paths,
        )

        return resolve_system_exec_allow_paths(container)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — degrade to "no system exec rules"
        return ()


def _resolve_file_guard_masked_paths(
    container: object,
) -> "tuple[tuple[str, int], ...]":
    """Resolve the op-masked base-environment paths (best-effort, empty on fail).

    Delegates to the shared :func:`resolve_file_guard_masked_paths` so the
    native op-masked seed and the Python permission check read the SAME source
    (``factory/config/file_guard_paths.json``).
    """
    try:
        from apps.api._workspace_resolver import (
            resolve_file_guard_masked_paths,
        )

        return resolve_file_guard_masked_paths(container)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — degrade to "no op-masked rules"
        return ()


def _resolve_program_allowlist_paths(
    container: object,
) -> "tuple[tuple[str, int], ...]":
    """Resolve per-program fixed-artifact allowlist paths (best-effort, empty on fail).

    Delegates to :func:`resolve_program_allowlist_paths` so the native seed
    reads ``factory/config/program_path_allowlist.json`` — silencing a
    program's own runtime writes (e.g. powershell PSReadLine history).
    """
    try:
        from apps.api._workspace_resolver import (
            resolve_program_allowlist_paths,
        )

        return resolve_program_allowlist_paths(container)  # type: ignore[arg-type]
    except Exception:  # noqa: BLE001 — degrade to "no program allowlist rules"
        return ()


async def _seed_permanent_grants(
    container: object, guard: "NativeFileGuardPort"
) -> int:
    """Whitelist every durable (non-expiring, non-expired) grant path."""
    security = getattr(container, "security", None)
    repo = (
        getattr(security, "path_grant_repository", None)
        if security is not None
        else None
    )
    list_all = getattr(repo, "list_all", None)
    if list_all is None:
        return 0
    try:
        grants = await list_all()
    except Exception:  # noqa: BLE001 — a repo error must not wedge startup
        _log.warning("native_file_guard.grant_seed_list_failed", exc_info=True)
        return 0

    now = _now(container)
    seeded = 0
    seen: set[str] = set()
    for grant in grants:
        # Startup seed is permanent-only (see module docstring — Layering):
        # a durable grant == non-expiring (``expires_at is None``). Session /
        # process grants left over from a PRIOR process run are stale — their
        # ``scope_key`` (old conversation id / old boot id) can never match
        # the fresh boot id or a not-yet-existing conversation in the Python
        # matcher, so seeding them would only widen the process-global native
        # whitelist for a grant that can never legitimately fire. The runtime
        # ``PathGrantCreatedEvent`` subscriber pushes freshly-approved
        # session/process grants for the CURRENT process instead.
        if getattr(grant, "expires_at", None) is not None:
            continue
        if now is not None and _is_expired(grant, now):
            continue
        path = getattr(grant, "path", "") or ""
        if not path or path in seen:
            continue
        seen.add(path)
        if guard.add_allow_rule(path, session_only=False):
            seeded += 1
    return seeded


def _now(container: object):
    clock = getattr(container, "clock", None)
    if clock is None:
        return None
    try:
        return clock.now()
    except Exception:  # noqa: BLE001
        return None


def _is_expired(grant: object, now) -> bool:
    try:
        return bool(grant.is_expired(now=now))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return False


async def subscribe_native_guard_grant_sync(container: object):
    """Subscribe grant create / revoke → native allow-rule add / remove.

    Returns a list of subscription handles (each with an async
    ``unsubscribe``) so lifespan can drain them on shutdown, or an empty
    list when the guard is inactive / the event bus is unavailable.
    """
    security = getattr(container, "security", None)
    guard: "NativeFileGuardPort | None" = (
        getattr(security, "native_file_guard", None)
        if security is not None
        else None
    )
    events = getattr(container, "events", None)
    if guard is None or events is None or not getattr(guard, "is_active", False):
        return []

    from qai.security.domain.events import (
        PathGrantCreatedEvent,
        PathGrantRevokedEvent,
    )

    async def _on_created(envelope) -> None:  # type: ignore[no-untyped-def]
        # SEC true-scoping layering: push the path for a grant of ANY scope
        # (session / process / permanent). The native allow-list is
        # process-global (no conversation concept), so a session/process grant
        # created NOW belongs to THIS running process and its sub-processes
        # must be able to write the path for the rest of the process's life.
        # True per-conversation isolation is still enforced in the Python
        # layer (CheckPermissionUseCase.matches_scope); the native layer is
        # the coarser process-lifetime backstop. Deliberately NO scope filter
        # here — every created grant whitelists its path.
        event = getattr(envelope, "event", envelope)
        path = getattr(event, "path", "") or ""
        if path:
            guard.add_allow_rule(str(path), session_only=False)

    async def _on_revoked(envelope) -> None:  # type: ignore[no-untyped-def]
        event = getattr(envelope, "event", envelope)
        path = getattr(event, "path", "") or ""
        if path:
            guard.remove_allow_rule(str(path), session_only=False)

    subs = []
    try:
        subs.append(
            await events.subscribe(PathGrantCreatedEvent, _on_created)
        )
        subs.append(
            await events.subscribe(PathGrantRevokedEvent, _on_revoked)
        )
        _log.info("native_file_guard.grant_sync_subscribed")
    except Exception:  # noqa: BLE001
        _log.warning("native_file_guard.grant_sync_subscribe_failed", exc_info=True)
    return subs


async def start_native_guard(container: object) -> list:
    """Start the native guard64.dll hook + wire the ASK bridge + seed rules.

    Shared by lifespan (boot) and the unified FileGuard master-switch route
    (live toggle). Idempotent-safe: a second call on an already-started
    guard re-runs seeding harmlessly (the DLL Init is idempotent). Returns
    the grant create/revoke subscription handles the caller must drain on
    stop (empty list when the guard could not start / no event bus).

    Never raises — a fault is logged and an empty list returned so a start
    hiccup can neither wedge boot nor fail the toggle request.
    """
    security = getattr(container, "security", None)
    guard: "NativeFileGuardPort | None" = (
        getattr(security, "native_file_guard", None)
        if security is not None
        else None
    )
    if guard is None:
        return []
    try:
        # Wire the native-thread → asyncio ASK bridge as the V2 filter BEFORE
        # start. build_native_hook_filter starts the bridge's OWN dedicated
        # loop thread (NOT the API main loop), so the native filter never
        # marshals onto / blocks the main loop. A container with no security
        # use case yields (None, None) → the guard keeps its built-in
        # fail-closed default filter.
        from apps.api._native_hook_bridge import build_native_hook_filter

        bridge, filt = build_native_hook_filter(container)
        if bridge is not None and filt is not None:
            set_filter = getattr(guard, "set_filter_callback", None)
            if set_filter is not None:
                set_filter(filt)
            # Stash the bridge on the guard so stop can close its loop thread.
            try:
                guard._ask_bridge = bridge  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
        if not guard.start():
            _log.warning("native_file_guard.start_failed")
            return []
        _log.info("native_file_guard.started")
        await seed_native_guard_rules(container)
        subs = await subscribe_native_guard_grant_sync(container)
        # Stash the subscription handles on the guard so a later
        # stop_native_guard (or lifespan shutdown) can drain them without the
        # caller threading the list around (live toggle re-entrancy).
        try:
            guard._grant_subs = subs  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return subs
    except Exception:  # noqa: BLE001
        _log.warning("native_file_guard.start_error", exc_info=True)
        return []


async def stop_native_guard(container: object, subs: list | None = None) -> None:
    """Stop the native guard + drain its grant-sync subscriptions.

    Shared by lifespan shutdown and the master-switch live toggle. Drains
    the subscriptions FIRST so no handler fires against a stopping guard,
    then calls ``stop()`` (idempotent). Never raises. When ``subs`` is None
    the handles stashed on the guard by :func:`start_native_guard` are used.
    """
    security = getattr(container, "security", None)
    guard = (
        getattr(security, "native_file_guard", None)
        if security is not None
        else None
    )
    if subs is None and guard is not None:
        subs = getattr(guard, "_grant_subs", None)
    for sub in subs or []:
        try:
            await sub.unsubscribe()
        except Exception:  # noqa: BLE001
            _log.warning("native_file_guard.grant_sync_drain_failed", exc_info=True)
    if guard is not None:
        try:
            guard._grant_subs = []  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        try:
            guard.stop()
            _log.info("native_file_guard.stopped")
        except Exception:  # noqa: BLE001
            _log.warning("native_file_guard.stop_failed", exc_info=True)
        # Close the ASK bridge's dedicated loop thread (if wired).
        bridge = getattr(guard, "_ask_bridge", None)
        if bridge is not None:
            try:
                bridge.close()
            except Exception:  # noqa: BLE001
                _log.warning(
                    "native_file_guard.ask_bridge_close_failed", exc_info=True
                )
            try:
                guard._ask_bridge = None  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                pass
