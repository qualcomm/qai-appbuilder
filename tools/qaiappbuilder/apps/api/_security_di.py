# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``security`` bounded context.

PR-031 (S3) injected seven ``_Fake<Port>`` in-memory adapters here; PR-040
(S4) replaced all seven with real adapters:

* :class:`qai.security.adapters.SqlitePolicyRepository`
* :class:`qai.security.adapters.SqlitePathGrantRepository`
* :class:`qai.security.adapters.SqlitePermissionRequestRepository`
* :class:`qai.security.adapters.SqliteAuditSink`
* :class:`qai.security.adapters.SqliteAuditQuery` (NEW — issue (a)
  decision A: read-side companion to AuditSinkPort)
* :class:`qai.security.adapters.SettingsSmartApprovalAdapter`
* :class:`apps.api._reboot_scheduler.SecurityRebootSignalAdapter`

De-sandbox refactor (2026-07-04) — the OS-isolation sandbox was removed
(2026-07-01, replaced by FileGuard). This pass deleted the now-orphaned
security-side sandbox execution framework (``ExecuteSandboxedUseCase`` /
``SandboxConfigHolder`` / ``coerce_sandbox_config`` / ``should_sandbox`` /
``SandboxStateMachine`` / ``sandbox_routing`` / ``SaveSandboxSettingsUseCase``)
and the corresponding orphaned :class:`SecurityServices` fields
(``execute_sandboxed_use_case`` / ``sandbox_config`` /
``sandbox_config_holder`` / ``on_sandbox_config_changed`` /
``detect_bypass_risks_for`` / ``detect_extra_paths_risks_for`` /
``save_sandbox_settings_use_case`` + the ``None`` Persistent-ACL / launcher
placeholders ``persistent_acl_*`` / ``app_container_sid_provider`` /
``ace_writer`` / ``sandbox_policy_builder`` / ``sandboxed_process_runner`` /
``daemon_manager`` / ``estimate_persistent_acl_use_case`` /
``get_persistent_acl_config_use_case`` / ``add_user_path_use_case``).
The runtime-state service was renamed
``SandboxRuntimeStateService`` → :class:`SecurityRuntimeStateService`
(field ``sandbox_runtime_state`` → ``security_runtime_state``). The live
FileGuard exec path uses
``qai.ai_coding.infrastructure.tools.handlers.exec`` directly and never
consumed the deleted framework, so no user-facing behaviour changes.

Sandbox→path rename (2026-07): the persistent-ACL grant aggregate and
its wiring were renamed from ``Sandbox*`` to ``Path*`` (``PathGrant`` /
``SqlitePathGrantRepository`` / ``create/revoke_path_grant`` use
cases + ``PathGrant{Created,Revoked}Event`` + the ``security_path_grant``
table + the ``/api/security/path-grants`` routes) because the OS sandbox
was removed 2026-07-01 and these entries are FileGuard's path
authorization store. Pure rename, no behaviour change. The
``process_runner`` field (a plain
:class:`qai.platform.process.subprocess_runner.SubprocessProcessRunner`)
remains. The former ``Settings.security.sandbox_enabled`` field + its
exec-branch gate were removed 2026-07 (a no-op that performed no OS
isolation); ``sandbox_launcher_path`` / ``SandboxSettings`` remain a
separate, not-yet-renamed batch.

Two fields appeared on :class:`SecurityServices` in PR-040 and
remain:

* ``audit_query`` — :class:`AuditQueryPort` for the read side of audit
  records (was implicitly the fake AuditSink's ``recent`` method).
* ``cancel_permission_request_use_case`` — :class:`CancelPermissionRequestUseCase`
  backing ``DELETE /api/security/permission/{request_id}`` (issue d
  decision B).

Existing :class:`SecurityServices` field names are part of the public
route contract and have NOT been changed.

Import discipline
-----------------
Top-level adapters import is allowed because the ``interfaces-stays-thin``
contract uses ``allow_indirect_imports = True`` (set in PR-040): routes
reach this module transitively via ``apps.api.di``, but only DIRECT
``interfaces.http -> qai.*.adapters`` edges are forbidden — those would
mean a route is bypassing its use case.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from apps.api._global_proxy import build_ssl_verify_provider
from apps.api._runtime_config_store import ForgeRuntimeStatePersistence
from qai.platform.process import ProcessRunnerPort
from qai.platform.process.subprocess_runner import SubprocessProcessRunner
from qai.security.adapters import (
    AuditHookAdapter,
    EventBusPermissionBroadcast,
    InMemoryAskRateLimiter,
    InMemorySkillCapabilityRegistry,
    NativeFileGuard,
    NullPermissionPendingStore,
    RuntimeStateAutoApproveAdapter,
    SettingsSmartApprovalAdapter,
    SmartApprovalLLMAdapter,
    SqliteAuditQuery,
    SqliteAuditSink,
    SqliteChannelPolicyRepository,
    SqlitePathGrantRepository,
    SqlitePendingPermissionStore,
    SqlitePermissionRequestRepository,
    SqlitePolicyRepository,
    resolve_dll_path,
)
from qai.security.application.pending_cleanup import PendingCleanupService
from qai.security.application.permission_wait import PermissionWaitRegistry
from qai.security.application.ports import (
    AskRateLimiterPort,
    AuditHookPort,
    AuditQueryPort,
    AuditSinkPort,
    ChannelPolicyRepositoryPort,
    NativeFileGuardPort,
    PathGrantRepositoryPort,
    PermissionBroadcastPort,
    PermissionPendingStorePort,
    PermissionRequestRepositoryPort,
    PolicyRepositoryPort,
    RebootSignalPort as SecurityRebootSignalPort,
    SkillCapabilityRegistryPort,
    SmartApprovalPort,
)
from qai.security.application.security_audit_facade import SecurityAuditFacade
from qai.security.application.security_runtime_state import (
    SecurityRuntimeStateService,
)
from qai.security.application.use_cases.approve_permission import (
    ApprovePermissionUseCase,
)
from qai.security.application.use_cases.cancel_permission_request import (
    CancelPermissionRequestUseCase,
)
from qai.security.application.use_cases.check_permission import (
    CheckPermissionUseCase,
)
from qai.security.application.use_cases.create_path_grant import (
    CreatePathGrantUseCase,
)
from qai.security.application.use_cases.reject_permission import (
    RejectPermissionUseCase,
)
from qai.security.application.use_cases.request_permission import (
    RequestPermissionUseCase,
)
from qai.security.application.use_cases.revoke_path_grant import (
    RevokePathGrantUseCase,
)
from qai.security.application.use_cases.security_templates import (
    ApplySecurityTemplateUseCase,
)
from qai.security.application.use_cases.skill_capability import (
    RegisterSkillCapabilityUseCase,
    UnregisterSkillCapabilityUseCase,
)
from qai.security.application.use_cases.skill_discovery import (
    GetSkillPolicyUseCase,
    SkillDiscoveryUseCase,
    build_effective_capabilities,
    read_skill_policy_overrides,
)
from qai.security.application.use_cases.update_policy import (
    UpdatePolicyUseCase,
)
from qai.security.application.wait_watchdog import WaitWatchdogService
from qai.security.domain.events import PermissionResolvedEvent
from qai.security.domain.skill_capability import SkillCapability
from qai.security.domain.value_objects import RequestId
from qai.security.infrastructure.skill_injection_scanner import (
    scan as skill_injection_scan,
)

from ._reboot_scheduler import SecurityRebootSignalAdapter

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container


__all__ = [
    "SecurityServices",
    "build_security_services",
]


# PR-G1 / F-17 — default audit-log row cap for SqliteAuditSink rotation.
# Mirrors V1 10 MiB / single
# backup file rotation, expressed as a row count (~200 bytes per audit
# row × 50 000 rows ≈ 10 MiB on-disk; the single-table-with-prune
# strategy collapses V1's "current jsonl + .1.bak" into one bounded
# table). 0 / None on the constructor disables rotation, but the default
# deployment is bounded out-of-the-box.
_DEFAULT_AUDIT_MAX_ROWS = 50_000


@dataclass(slots=True)
class SecurityServices:
    """Application services / ports for the ``security`` namespace.

    Holds the 9 use cases (8 from PR-020 + 1 from PR-040) plus the
    underlying port instances so the route layer can do read-only
    inspection (e.g. ``GET /policy``) without round-tripping through a
    use case.

    Field-name compatibility note (Container field-name lock per
    PR-031 manifest §11): every field present in S3's PR-031 wiring is
    retained verbatim. PR-040 only **adds** ``audit_query`` and
    ``cancel_permission_request_use_case``. PR-501 tail-appends
    ``channel_policy_repository``, ``ask_rate_limiter`` and
    ``permission_broadcast`` for the channel-aware ASK fallback /
    rate-limit pipeline. PR-502 tail-appends ``audit_hook`` for the
    PEP 578 interpreter-level IO interceptor; existing field positions
    remain unchanged.
    """

    # repositories / sinks (raw ports, used by read endpoints)
    policy_repository: PolicyRepositoryPort
    path_grant_repository: PathGrantRepositoryPort
    permission_request_repository: PermissionRequestRepositoryPort
    audit_sink: AuditSinkPort
    audit_query: AuditQueryPort  # NEW (PR-040 / issue a)
    smart_approval: SmartApprovalPort
    reboot_signal: SecurityRebootSignalPort
    # ``process_runner`` field name is locked (v2.7 §3.1). Post the
    # 2026-07-01 sandbox cleanup its value is the plain
    # :class:`SubprocessProcessRunner`. The previous AppContainer/LPAC
    # launcher wrap (``SandboxedProcessRunner``) was deleted; callers that
    # resolve ``container.security.process_runner`` now get an
    # un-decorated subprocess runner. The field stays a
    # :class:`ProcessRunnerPort` so downstream wiring
    # (``apps/api/_chat_di.py`` / ``apps/api/_ai_coding_di.py`` etc.) is
    # unaffected.
    process_runner: ProcessRunnerPort  # NEW (PR-041) -- cross-context
    # use cases
    update_policy_use_case: UpdatePolicyUseCase
    check_permission_use_case: CheckPermissionUseCase
    request_permission_use_case: RequestPermissionUseCase
    approve_permission_use_case: ApprovePermissionUseCase
    reject_permission_use_case: RejectPermissionUseCase
    cancel_permission_request_use_case: CancelPermissionRequestUseCase  # NEW (PR-040 / issue d)
    create_path_grant_use_case: CreatePathGrantUseCase
    revoke_path_grant_use_case: RevokePathGrantUseCase
    # PR-501 — channel-aware ASK fallback + ask-rate quota
    channel_policy_repository: ChannelPolicyRepositoryPort
    ask_rate_limiter: AskRateLimiterPort
    permission_broadcast: PermissionBroadcastPort
    # PR-502 — PEP 578 sys.addaudithook interpreter-level IO interceptor.
    # Lazy: the adapter is constructed here but ``install()`` is the
    # responsibility of ``apps.api.lifespan`` (gated on
    # ``SecuritySettings.audit_hook_enabled``).
    audit_hook: AuditHookPort
    # PR-504 — skill capability + security runtime state + 18 deferred
    # routes. The registry is in-memory by default (single-worker
    # deployment); ``security_runtime_state`` carries the runtime-
    # toggleable flags (enabled/mode/auto_approve/path_patterns/...)
    # routes consume.
    skill_capability_registry: SkillCapabilityRegistryPort
    register_skill_capability_use_case: RegisterSkillCapabilityUseCase
    unregister_skill_capability_use_case: UnregisterSkillCapabilityUseCase
    security_runtime_state: SecurityRuntimeStateService
    # PR-092 §2.1 C-7 / §17.5 #8 — LLM-backed Smart Approval. Optional;
    # ``None`` when ``security.smart_approval_llm_endpoint`` is unset.
    # Wired alongside the legacy stub so the use case can prefer the
    # LLM when configured.
    smart_approval_llm: SmartApprovalPort | None
    # R7-R10 route-thinness cohesion fix — application use cases that
    # absorb business logic previously inlined in
    # ``interfaces/http/routes/security.py``. Tail-appended per v2.7 §3.1
    # (existing field positions unchanged; route path/method/response
    # shapes preserved verbatim).
    apply_security_template_use_case: ApplySecurityTemplateUseCase
    skill_discovery_use_case: SkillDiscoveryUseCase
    get_skill_policy_use_case: GetSkillPolicyUseCase
    # P0 ASK restore — process-wide async ASK suspend/resume registry shared
    # by the FileGuard ASK bridge (``apps.api._file_guard_bridge``) and the
    # approve / reject / cancel use cases. Tail-appended per v2.7 §3.1.
    permission_wait_registry: PermissionWaitRegistry
    # 2026-07-04 native-hook integration (PR-2) — the OS-level guard64.dll
    # file hook adapter. Tail-appended per §3.1 (new field, not a
    # placeholder revive). Wired to :class:`DisabledNativeFileGuard` (a
    # zero-side-effect no-op) when ``native_file_guard_enabled`` is False
    # (default), else the ctypes-backed :class:`NativeFileGuard`. Lifespan
    # is responsible for calling :meth:`start` (never at DI build time, so
    # importing apps.api in tests never hooks the interpreter). PR-3 wires
    # the asyncio ASK filter callback; PR-4 drives rule hot-sync through it.
    native_file_guard: NativeFileGuardPort
    # 2026-07-06 Phase 2 — durable pending-permission store (migration 052
    # ``security_pending_permission``). Wired to :class:`NullPermissionPendingStore`
    # (no-op) when ``permission_pending_persist`` is False (test /
    # in-memory), else the aiosqlite-backed :class:`SqlitePendingPermissionStore`.
    # Mirrors the in-memory ``PermissionWaitRegistry`` for cross-restart
    # rehydrate + subprocess-gone cleanup + cancel-by-pid enumeration.
    # Tail-appended per §3.1 (new field).
    permission_pending_store: PermissionPendingStorePort
    # 2026-07-06 Phase 2 — periodic subprocess-gone sweep service. Scans
    # every ``scan_interval_seconds`` (default 10s) and resolves any
    # pending ASK whose subprocess is dead as ``subprocess_gone``. Lifespan
    # calls :meth:`start` after DI build and :meth:`stop` on shutdown.
    # Tail-appended per §3.1 (new field).
    pending_cleanup_service: PendingCleanupService
    # L-Sec-4 (fileguard-audit-2026-07-26) — WaitWatchdogService. Periodic
    # sweep that WARNs about ASKs unresolved far longer than a human would
    # take (timeout=None Phase-2 semantic means a stuck popup never
    # auto-DENYs; this surfaces it for ops). NEVER resolves the wait.
    # Lifespan calls :meth:`start` after DI build and :meth:`stop` on
    # shutdown. Tail-appended per §3.1.
    wait_watchdog_service: WaitWatchdogService
    # 2026-07-07 Phase 3b (P-17 §6.3) — unified security-audit funnel. Fronts
    # the canonical ``audit_sink`` (#8) AND any JSONL fallback sinks (#7
    # emergency / #1 file_broker) so scattered write-only JSONL records also
    # become queryable via the same ``security_audit_entry`` surface. Optional
    # collaborator (tail-appended per §3.1); ``None`` for hand-rolled test
    # containers that never build it.
    security_audit_facade: SecurityAuditFacade | None = None


def build_security_services(container: Container) -> SecurityServices:
    """Wire ``container.security`` with real PR-040 adapters.

    Uses ``container.{database, clock, ids, settings, events,
    reboot_scheduler}`` rather than constructing fresh ``SystemClock``
    / ``UlidGenerator`` instances so tests injecting a ``FrozenClock``
    via ``Container`` see the same clock everywhere.
    """

    clock = container.clock
    ids = container.ids
    db = container.database
    events = container.events

    policy_repo = SqlitePolicyRepository(db=db, clock=clock)
    grants_repo = SqlitePathGrantRepository(db=db)
    requests_repo = SqlitePermissionRequestRepository(db=db)
    # PR-G1 / F-17 — Audit log rotation. V1 reference is the file-level
    # rotation (``_AUDIT_MAX_BYTES = 10 * 1024 * 1024``, single ``.1.bak`` backup).
    # V1 had no SQLite security-audit rotation; the documented V2-equivalent
    # cap (`security-implementation.md` appendix D TODO S-4 / GAP plan
    # F-17) is "10MB / 5 backup-equivalent" → ~50 000 rows single-table cap
    # (assuming ~200 bytes per audit row, 50MB total worst-case).
    # ``None`` would disable rotation; we pass an explicit cap so the
    # default deployment is bounded out-of-the-box. Operators can tune
    # this by future-extending ``SecuritySettings`` if needed; for now
    # the constant lives at the DI seam to keep settings stable.
    audit_sink = SqliteAuditSink(db=db, max_rows=_DEFAULT_AUDIT_MAX_ROWS)
    audit_query = SqliteAuditQuery(db=db)
    # PR-092 §2.1 C-7 / §17.5 #8 — LLM smart-approval takes precedence
    # over the stub when both endpoint + model are configured. The stub
    # remains available so callers that want a deterministic UNDECIDED
    # path can keep using it; the use case sees only the active
    # ``smart_approval`` field.
    stub_smart_approval = SettingsSmartApprovalAdapter(
        settings=container.settings.security,
    )
    llm_smart_approval: SmartApprovalPort | None = None
    if (
        container.settings.security.smart_approval_llm_endpoint
        and container.settings.security.smart_approval_llm_model
    ):
        llm_smart_approval = SmartApprovalLLMAdapter(
            settings=container.settings.security,
            # 缺口 fix — route the (previously hardcoded verify=False) approval
            # classifier through the live global Settings.ssl_verify toggle.
            ssl_verify_provider=build_ssl_verify_provider(container),
        )
    smart_approval: SmartApprovalPort = (
        llm_smart_approval if llm_smart_approval is not None
        else stub_smart_approval
    )
    reboot_signal = SecurityRebootSignalAdapter(
        scheduler=container.reboot_scheduler,
    )
    process_runner = SubprocessProcessRunner()
    # PR-501 — channel-aware ASK fallback + ask-rate quota
    channel_policy_repo = SqliteChannelPolicyRepository(db=db)
    ask_rate_limiter = InMemoryAskRateLimiter()
    permission_broadcast = EventBusPermissionBroadcast(
        events=events,
        clock=clock,
    )
    # PR-502 — PEP 578 audit hook adapter. Built lazily: the underlying
    # ``sys.addaudithook`` registration is deferred to lifespan startup
    # (gated on ``SecuritySettings.audit_hook_enabled``). The adapter's
    # default ``policy_provider`` returns an empty Policy until lifespan
    # seeds a real one via :meth:`AuditHookAdapter.set_policy_provider`,
    # which means the hook is fail-closed during any window where the
    # cache has not been warmed yet.
    #
    # PR-092 §2.2 H-17 / §17.5 #4 — wire ``audit_hook_extra_events`` so
    # the lifespan install adds ``os.scandir`` / ``os.listdir`` /
    # ``shutil.copyfile`` (or whatever the operator configured) on top
    # of the built-in event set.
    #
    # Trusted-subprocess wrapping (PR-092 §2.1 C-11): aria2c download
    # invocations, the launcher subprocess, the reboot helper and any
    # other project-internal Popen path should wrap their call site in
    # ``with trusted_subprocess(reason=...): ...`` so the hook
    # short-circuits the exec scope without touching the Policy.
    audit_hook = AuditHookAdapter(
        extra_events=tuple(
            container.settings.security.audit_hook_extra_events or ()
        ),
    )
    # PR-504 — skill capability registry + security runtime state.
    skill_capability_registry = InMemorySkillCapabilityRegistry()
    security_runtime_state = SecurityRuntimeStateService(
        # Decision 2A — persist operator flips of the runtime buckets to the
        # shared forge_config so they survive a restart (V1 sandbox_state.json
        # parity). The security context only sees the abstract
        # RuntimeStatePersistencePort; the apps-layer adapter owns the file I/O.
        persistence=ForgeRuntimeStatePersistence(
            data_root=container.data_paths.root
        ),
    )
    # U-005 / 5-H4 — auto-approve / command-list pre-check adapter
    # (V1 ``PolicyCenter.is_auto_approved`` ran before the FileGuard
    # policy rules). Reads the live ``auto_approve`` bucket on the
    # runtime-state service so operator toggles take effect without
    # re-wiring DI; injected into ``CheckPermissionUseCase`` below.
    auto_approve_adapter = RuntimeStateAutoApproveAdapter(
        runtime_state=security_runtime_state,
    )

    # P0 ASK restore — process-wide async ASK suspend/resume registry. The
    # FileGuard ASK bridge registers + awaits on it; the approve / reject /
    # cancel use cases wake it. Single instance per container so the wake
    # routes and the blocked FileGuard share the same futures.
    permission_wait_registry = PermissionWaitRegistry()

    # 2026-07-04 native-hook integration — construct the OS-level
    # guard64.dll adapter. We ALWAYS construct the real ctypes
    # :class:`NativeFileGuard` (unstarted — construction does NOT load the
    # DLL, only :meth:`start` does), so the unified FileGuard master switch
    # can dynamically start()/stop() the hook at runtime WITHOUT a restart
    # (the DLL Init/Destroy cycle is re-entrant — verified). Lifespan starts
    # it at boot only when ``native_file_guard_enabled`` is True; the
    # master-switch route toggles it live thereafter. An unstarted guard is
    # itself a zero-side-effect no-op (all mutators short-circuit until
    # started), and a missing/failed DLL surfaces at ``start`` time (logged,
    # returns False) rather than crashing DI build. The Disabled no-op is
    # retained only for the explicit "never construct the real adapter" path
    # (kept import-compatible; no longer wired by default).
    _dll = resolve_dll_path(
        repo_root=container.repo_root,
        dll_path=container.settings.security.native_file_guard_dll_path,
    )
    native_file_guard: NativeFileGuardPort = NativeFileGuard(
        dll_path=_dll,
        fail_closed=container.settings.security.native_file_guard_fail_closed,
        callback_timeout_ms=(
            container.settings.security.native_file_guard_callback_timeout_ms
        ),
    )

    # 2026-07-06 Phase 2 — durable pending-permission store + subprocess-gone
    # cleanup service. The store is aiosqlite-backed
    # (:class:`SqlitePendingPermissionStore`) when the operator opted in via
    # ``permission_pending_persist=True`` (default), else a zero-side-effect
    # no-op (test / in-memory). The cleanup service is ALWAYS wired (even
    # with a null store: the in-memory registry's list_pending is still
    # authoritative for the live process) so the periodic sweep runs
    # regardless of the persistence toggle; lifespan is responsible for
    # calling :meth:`start` after DI build and :meth:`stop` on shutdown.
    permission_pending_store: PermissionPendingStorePort
    if getattr(
        container.settings.security, "permission_pending_persist", True
    ):
        permission_pending_store = SqlitePendingPermissionStore(db=db)
    else:
        permission_pending_store = NullPermissionPendingStore()
    # Problem ② backstop-honesty — publish a UI-close ``PermissionResolvedEvent``
    # whenever the sweep resolves a stale ASK whose subprocess died silently
    # (no local user response, no exec-cancel flush, so this sweep is the only
    # thing that can close its dialog). Built as an apps-layer callback so the
    # dependency-free application service stays EventBus-free (§3.2 layering);
    # best-effort inside the service (a publish glitch never breaks the sweep).
    async def _publish_resolved(request_id: str, resolution: str) -> None:
        await events.publish(
            PermissionResolvedEvent(
                request_id=RequestId(value=request_id),
                resolution=resolution,
                occurred_at=clock.now(),
            )
        )

    pending_cleanup_service = PendingCleanupService(
        wait_registry=permission_wait_registry,
        pending_store=permission_pending_store,
        clock=clock,
        on_resolved=_publish_resolved,
        # 2026-08-09 — also close the ``PermissionRequest`` aggregate the UI's
        # pending list reads. Without it, an ASK abandoned by a short-lived
        # subprocess (onnxruntime touching ``C:\Windows\INF\cpu.PNF`` during
        # provider init is the canonical case: it never waits for the answer)
        # left a permanent ``state='pending'`` ghost row whose dialog had
        # already been closed by this very sweep.
        request_repository=requests_repo,
    )

    # L-Sec-4 — observation-only watchdog for over-long unresolved ASKs.
    # Shares the same wait_registry + durable pending store + clock as the
    # cleanup service; uses module-default cadence (5 min scan, 15 min
    # stale threshold). No counter wired here (log-only) — a metrics
    # backend can inject one later without touching this seam.
    wait_watchdog_service = WaitWatchdogService(
        wait_registry=permission_wait_registry,
        pending_store=permission_pending_store,
        clock=clock,
    )

    update_policy = UpdatePolicyUseCase(
        policy_repository=policy_repo,
        reboot_signal=reboot_signal,
        event_bus=events,
        clock=clock,
    )
    # Unified FileGuard run-mode provider — reads the live ``run_mode``
    # (enforce | audit_only) from the ``policy_overview`` runtime bucket (the
    # same key ``GET/PUT /api/security/policy`` read/write) and folds it
    # through the security master switch (``mode`` = enforcing | permissive |
    # disabled) via ``effective_run_mode``: ``permissive`` / ``disabled``
    # force ``audit_only`` (log-but-allow) regardless of the sub-switch, while
    # ``enforcing`` passes the sub-switch through. Consumed by
    # CheckPermissionUseCase so a flip takes effect instantly, without a
    # restart, through the SINGLE existing ``audit_only`` override path.
    # Defaults to "enforce" on any read miss (fail-safe).
    #
    # SCOPE (corrected 2026-08-09): this reaches the native OS hook only for
    # events the DLL cannot decide locally and forwards over the pipe — those
    # run through this use case, so the override applies. Events the DLL
    # decides ITSELF never see it: ``guard.cpp`` ``LocalDecide()`` has no
    # ``audit_only`` concept at all (verified: zero matches for
    # ``audit_only`` / ``run_mode`` under ``native/``), so a hard-deny it
    # resolves locally — its black list or an op-mask ``deny_mask`` such as
    # ``%SystemRoot%\INF`` write=deny — stays DENY regardless of run mode.
    # Net effect in ``audit_only``: an in-process tool write to such a path
    # is permitted (logged-but-allowed) while the same write from a guarded
    # CHILD process is still blocked by the DLL. That asymmetry predates this
    # note; it is recorded here so the previous "takes effect for BOTH
    # layers" claim no longer misleads.
    def _run_mode_provider() -> str:
        try:
            bucket = security_runtime_state.get_settings("policy_overview") or {}
            run_mode = str(bucket.get("run_mode", "enforce"))
            if run_mode not in ("enforce", "audit_only"):
                run_mode = "enforce"
            effective = security_runtime_state.effective_run_mode(run_mode)
            return effective if effective in ("enforce", "audit_only") else "enforce"
        except Exception:  # noqa: BLE001 — read miss → enforce (safe)
            return "enforce"

    # Three-state whitelist — 0-arg provider returning the GLOBAL allow
    # prefixes (four data/models roots + operator ``global_allow_paths``),
    # resolved live per-call from the SAME apps-layer source the native
    # guard64.dll allow-list seed uses. Any resolution fault degrades to an
    # empty tuple (no global allow surface) so a config hiccup can never
    # widen — or wedge — the permission check.
    def _global_allow_provider() -> tuple[str, ...]:
        try:
            from apps.api._workspace_resolver import resolve_global_allow_paths

            return resolve_global_allow_paths(container)
        except Exception:  # noqa: BLE001 — degrade to "no global allow"
            return ()

    # Op-aware READ-ONLY whitelist — 0-arg provider returning the read-only
    # allow prefixes (business dirs + system read surface + operator
    # ``read_only_allow_paths`` / ``system_read_allow_paths``), resolved live
    # per-call from the SAME apps-layer source the native guard64.dll read-only
    # seed uses. Any resolution fault degrades to an empty tuple (no read-only
    # surface) so a config hiccup can never widen — or wedge — the check.
    def _read_only_allow_provider() -> tuple[str, ...]:
        try:
            from apps.api._workspace_resolver import (
                resolve_read_only_allow_paths,
            )

            return resolve_read_only_allow_paths(container)
        except Exception:  # noqa: BLE001 — degrade to "no read-only allow"
            return ()

    # Op-masked base-environment whitelist (THREE-STATE) — 0-arg provider
    # returning ``((path, allow_mask, deny_mask), ...)`` from
    # factory/config/file_guard_paths.json (the SAME source the native op-masked
    # seed uses), resolved live per-call. A resolution fault degrades to an empty
    # tuple (no op-masked surface).
    def _op_mask_allow_provider() -> tuple[tuple[str, int, int], ...]:
        try:
            from apps.api._workspace_resolver import (
                resolve_file_guard_masked_paths,
            )

            return resolve_file_guard_masked_paths(container)
        except Exception:  # noqa: BLE001 — degrade to "no op-masked allow"
            return ()

    # Policy-rule ``${VAR}`` placeholder expansion — 0-arg provider returning
    # the bindings used by :meth:`Policy.evaluate_request` to expand tokens
    # like ``${PROJECT_ROOT}`` / ``${WORKSPACE}`` / ``${TEMP}`` / ``${HOME}``
    # / ``${APPDATA}`` in a rule's pattern *before* it is matched against
    # the incoming resource path.
    #
    # Why this exists
    # ---------------
    # The built-in ``strict`` / ``development`` / ``demo`` templates ship
    # rules like ``PathPattern("${PROJECT_ROOT}/*") → ALLOW`` followed by
    # ``PathPattern("*") → DENY`` (see
    # :mod:`qai.security.application.use_cases.security_templates`). Before
    # this wiring the pattern text was matched verbatim — ``${PROJECT_ROOT}``
    # never matched a real path, the "allow project root" rule never fired
    # and every write into the project tree hit the catch-all deny as a
    # *rule match* (``would_ask=False``), which suppresses the ASK dialog
    # by design (an explicit deny-rule hit is not an implicit miss).
    #
    # State-Truth-First
    # -----------------
    # The provider is 0-arg and re-resolves live per-check so an operator
    # workspace-root flip (via the runtime-config API) takes effect without
    # re-wiring DI. Single source of truth for the bindings is
    # :func:`qai.security.infrastructure.skill_capability_loader.
    # build_env_bindings` — the same helper the skill-capability loader
    # uses so a token means the same thing in both places (no drift). A
    # resolution fault degrades to an empty mapping (``{}``): unknown
    # placeholders are left verbatim by
    # :func:`qai.security.domain.path_templates.expand_placeholders`, so a
    # provider hiccup keeps the pre-fix behaviour (fail-safe: never
    # widens the allow surface on a config read error).
    def _policy_env_provider() -> dict[str, str]:
        try:
            from pathlib import Path

            from apps.api._workspace_resolver import resolve_workspace_root
            from qai.security.infrastructure.skill_capability_loader import (
                build_env_bindings,
            )

            workspace = resolve_workspace_root(container)
            return build_env_bindings(
                Path(container.repo_root),
                workspace_root=str(workspace) if workspace else None,
            )
        except Exception:  # noqa: BLE001 — degrade to "no placeholders"
            return {}

    # Skill-capability ALLOW short-circuit — an active skill's static
    # ``skill.policy.json`` declaration PLUS the operator's per-skill
    # override becomes a real ALLOW rule for the ops it declared.
    # Bridges THREE cross-context signals:
    #
    #   1. ``skill_capability_registry.list_active()`` — every skill whose
    #      on-disk ``skill.policy.json`` was loaded at boot (see
    #      ``lifespan.py`` GAP-2 skill_capability_loader.load_all). The
    #      registry is filled during startup and mutated only via the
    #      Register/Unregister use cases; ``list_active`` returns whatever
    #      is CURRENTLY registered — a live snapshot per the port contract.
    #
    #   2. ``security_runtime_state`` ``skill_policies`` bucket — the
    #      operator's per-skill ``read`` / ``write`` / ``trusted_binaries``
    #      additions written by ``PUT /api/security/skill_policy/{name}``
    #      (persisted to ``forge_config.json``, so a restart keeps them).
    #      The override is LAYERED ON TOP of the on-disk declaration —
    #      never replaces it — matching what the Security > Skill panel
    #      renders (``skill_discovery._entry_for`` merges the same way, so
    #      the UI summary and the enforced surface cannot drift).
    #      An override for a skill with NO registered capability (the
    #      operator authored a policy for a skill that ships no
    #      ``skill.policy.json``) synthesises a capability from the
    #      override alone — that is the whole point of the editor: it is
    #      how an agent skill under ``skills/`` gets a file surface.
    #
    #   3. ``forge.config skills.overrides[<name>].mode`` — the operator's
    #      per-skill 4-state mode ("off" | "cloud" | "local" | "both"; see
    #      :mod:`qai.user_prefs.application.use_cases.skills`). A skill in
    #      mode ``"off"`` is disabled and neither its declaration nor its
    #      override may unlock any path (that is the operator's intent when
    #      they turn a skill off). Any other mode (or an absent override,
    #      defaulting to ``"cloud"`` for most skills — see
    #      ``_DEFAULT_SKILL_MODE``) leaves the skill active.
    #
    # ``trusted_binaries`` feeds ``exec_paths`` on the merged capability:
    # the loader leaves ``exec_paths`` empty (V1 parity) so without this
    # fold ``covers_exec`` could never match and a declared/authorised
    # binary stayed unreachable. The exec-deny hard gate is still
    # re-checked downstream (``check_permission`` skill branch), so a
    # protected-path deny can never be bypassed this way.
    #
    # State-Truth-First: ALL THREE sides are re-read on every check call so
    # an operator edit (saving a per-skill policy, toggling a skill off, a
    # skill re-registering after a re-scan) takes effect without re-wiring
    # DI. Any fault on ANY side degrades to an empty tuple (no coverage) —
    # matching the domain provider fault contract
    # (``check_permission.py`` skill branch).

    # Document key of the shared forge-config document in the user_prefs KV
    # store. Mirrors ``interfaces.http.routes.user_prefs.FORGE_CONFIG_KEY``;
    # duplicated as a local literal rather than imported so the security DI
    # module keeps no dependency on the HTTP route layer.
    forge_config_doc_key = "forge.config"

    # Single reader for the operator's per-skill mode overrides
    # (``forge.config`` -> ``skills.overrides[<skill_id>]``).
    #
    # Truth source: ``container.user_prefs.load_document_use_case`` over
    # ``KvUserPrefsRepository`` (the ``kv_user_prefs`` SQLite table, key
    # ``forge.config``) — the SAME use case
    # ``qai.user_prefs.application.use_cases.skills.SetSkillModeUseCase``
    # writes through. Reading the ``forge_config.json`` FILE instead (as this
    # code used to) always yielded ``{}``: that file holds the runtime-config
    # and security_runtime_state buckets, never ``skills.overrides``.
    #
    # Read live per call (State-Truth-First) so a mode flip on the Skills page
    # takes effect on the next permission check with no reboot. Any fault
    # degrades to ``{}`` = "no override known", which keeps skills ENABLED —
    # matching the documented default (an absent override means "cloud").
    async def _load_skill_mode_overrides() -> dict[str, Any]:
        prefs = getattr(container, "user_prefs", None)
        load_uc = (
            getattr(prefs, "load_document_use_case", None)
            if prefs is not None
            else None
        )
        if load_uc is None:
            return {}
        try:
            doc = await load_uc.execute(forge_config_doc_key)
        except Exception:  # noqa: BLE001 — degrade to "no override known"
            return {}
        section = doc.get("skills", {}) if isinstance(doc, dict) else {}
        if not isinstance(section, dict):
            return {}
        raw = section.get("overrides", {})
        return raw if isinstance(raw, dict) else {}

    def _skill_allow_provider_disabled(
        mode_overrides: dict[str, Any],
    ) -> Callable[[str], bool]:
        """Build the per-skill "is this skill switched off?" predicate."""

        def _is_off(skill_name: str) -> bool:
            entry = mode_overrides.get(skill_name)
            if not isinstance(entry, dict):
                return False
            mode = entry.get("mode")
            if isinstance(mode, str) and mode == "off":
                return True
            return entry.get("enabled") is False

        return _is_off

    async def _skill_allow_provider() -> tuple[SkillCapability, ...]:
        try:
            caps = await skill_capability_registry.list_active()
        except Exception:  # noqa: BLE001 — registry fault → no coverage
            caps = []
        # Operator per-skill overrides (the bucket the Skill panel edits).
        try:
            path_overrides = read_skill_policy_overrides(
                security_runtime_state
            )
        except Exception:  # noqa: BLE001 — degrade to "no override known"
            path_overrides = {}
        if not caps and not path_overrides:
            return ()
        # Read the operator's per-skill mode overrides from the SAME store the
        # Skills page writes them to.
        #
        # C-Sec-mode (2026-07-31) — this used to read
        # ``<data>/config/forge_config.json`` directly. That file is NOT where
        # ``skills.overrides`` lives: ``build_user_prefs_services`` wires the
        # forge-config document to ``KvUserPrefsRepository(db=…)``, i.e. the
        # ``kv_user_prefs`` SQLite table under key ``forge.config``
        # (``apps/api/_user_prefs_di.py``). The JSON file only carries the
        # runtime-config / security_runtime_state buckets, so
        # ``skills.overrides`` read as ``{}`` forever and a skill switched
        # ``off`` on the Skills page still had its declared paths unlocked
        # here — the mode gate was silently inert.
        mode_overrides = await _load_skill_mode_overrides()

        try:
            return build_effective_capabilities(
                registered=list(caps),
                path_overrides=path_overrides,
                is_disabled=_skill_allow_provider_disabled(mode_overrides),
            )
        except Exception:  # noqa: BLE001 — resolution fault → no coverage
            return ()

    # Three-state whitelist — SESSION-SCOPED workspace subtree provider.
    # Given the CURRENT ``scope_conversation_id`` it resolves that
    # conversation's working directory and returns it as the single allow
    # prefix, so an in-process tool call under the workspace subtree is
    # ALLOWED for THAT conversation.
    #
    # Uses ``build_session_workspace_resolver`` (the WITH-FALLBACK variant):
    # if the conversation set its own workspace via the per-session UI, that
    # explicit path is used; otherwise it falls back to the GLOBAL configured
    # workspace root (``settings.workspace.model_root``, default
    # ``C:/WoS_AI``). This matches the per-session workspace UI's promise:
    # "留空则使用全局默认目录（C:\\WoS_AI）。本会话内 AI 可在此目录读写，
    # 无需重复授权。" — a conversation that does NOT set its own workspace
    # must still get the default C:\\WoS_AI subtree as the session allow,
    # otherwise every AI tool read of C:\\WoS_AI\\<file> pops an ASK
    # popup even though the UI promised no popup.
    #
    # Session isolation is preserved for the EXPLICIT case: a conversation
    # that sets its own workspace to ``D:/proj/foo`` gets ONLY that subtree
    # (not the global root), so two conversations with different explicit
    # workspaces cannot see each other's tree via the global fallback. The
    # only "sharing" is when both conversations use the DEFAULT (unset)
    # workspace — in which case they legitimately share ``C:/WoS_AI`` as
    # the tool's default workspace, matching V1 / the UI contract.
    #
    # A resolution fault degrades to no prefix (falls through to policy /
    # grant / ASK — stricter, never wider).
    try:
        from apps.api._workspace_resolver import (
            build_session_workspace_resolver,
        )

        _session_ws_resolver = build_session_workspace_resolver(
            container
        )

        async def _workspace_allow_provider(
            conversation_id: str,
        ) -> tuple[str, ...]:
            try:
                root = await _session_ws_resolver(conversation_id)
            except Exception:  # noqa: BLE001 — never break the check
                return ()
            root = (root or "").strip() if isinstance(root, str) else ""
            return (root,) if root else ()
    except Exception:  # noqa: BLE001 — resolver unavailable → no ws allow
        _workspace_allow_provider = None  # type: ignore[assignment]

    check_permission = CheckPermissionUseCase(
        policy_repository=policy_repo,
        grant_repository=grants_repo,
        audit_sink=audit_sink,
        clock=clock,
        ids=ids,
        # PR-501 — opt-in channel-aware collaborators; existing callers
        # that don't pass ``channel`` to ``execute`` get pre-PR-501
        # behaviour byte-for-byte.
        channel_policy_repository=channel_policy_repo,
        ask_rate_limiter=ask_rate_limiter,
        permission_broadcast=permission_broadcast,
        # U-005 / 5-H4 — auto-approve / command-list pre-check.
        auto_approve=auto_approve_adapter,
        # P0 ASK restore — dynamic_authorization provider drives the ASK vs
        # hard-DENY decision on a policy miss (V1 ``access_policy``). Reads
        # the live runtime-state scalar so an operator toggle takes effect
        # without re-wiring DI.
        dynamic_authorization=(
            lambda: security_runtime_state.snapshot().dynamic_authorization
        ),
        # audit_only run-mode — log-but-allow (see _run_mode_provider).
        run_mode_provider=_run_mode_provider,
        # Three-state whitelist — GLOBAL allow prefixes (four data/models
        # roots + operator ``global_allow_paths``). Reads the SAME source the
        # native guard64.dll allow-list seed uses (``resolve_global_allow_paths``),
        # resolved live per-call so a workspace-root / settings change takes
        # effect without re-wiring DI. A path under any prefix short-circuits
        # ALLOW (op-agnostic) before policy/grant/ASK; exec still re-checks the
        # hard exec-deny gate (protected-path deny can never be bypassed).
        global_allow_provider=_global_allow_provider,
        # Three-state whitelist — session-scoped workspace subtree ALLOW
        # (resolves THIS conversation's workspace; never widens another
        # session — session isolation preserved, per the user decision).
        workspace_allow_provider=_workspace_allow_provider,
        # Op-aware READ-ONLY whitelist — business dirs + system read surface.
        # Reads the SAME source the native guard64.dll read-only seed uses
        # (``resolve_read_only_allow_paths``), resolved live per-call. A path
        # under any prefix short-circuits ALLOW ONLY for read-only requests
        # (write/edit/delete/execute still fall through to policy/grant/ASK).
        read_only_allow_provider=_read_only_allow_provider,
        # Op-masked base-environment whitelist — per-op ALLOW from
        # factory/config/file_guard_paths.json, mirroring the native op-masked
        # seed from the SAME source (e.g. C:\Qualcomm read+execute, write ->
        # falls through so the black list hard-denies it).
        op_mask_allow_provider=_op_mask_allow_provider,
        # Placeholder-expansion bindings so patterns like
        # ``${PROJECT_ROOT}/*`` shipped by the built-in policy templates
        # (and by any operator-authored rule) match the real absolute
        # path at evaluation time. Without this the placeholders stayed
        # literal, the "allow project root" rules never fired, and the
        # catch-all ``*`` deny turned every write into a hard rule-DENY
        # (``would_ask=False``) — suppressing the ASK dialog. See the
        # provider docstring for the State-Truth-First / degrade-on-fault
        # contract.
        policy_env_provider=_policy_env_provider,
        # Skill-capability ALLOW short-circuit — a skill's declared paths
        # in ``skill.policy.json`` become a real ALLOW rule for its
        # declared ops (mode != "off" only). See the provider docstring
        # above for the two-signal merge (registry snapshot + mode
        # overrides) and the fail-safe fault contract.
        skill_allow_provider=_skill_allow_provider,
    )
    request_permission = RequestPermissionUseCase(
        request_repository=requests_repo,
        event_bus=events,
        clock=clock,
        ids=ids,
        smart_approval=smart_approval,
        wait_registry=permission_wait_registry,
        allow_smart_approve_auto=container.settings.security.smart_approval_llm_allow_auto_approve,
    )
    def _ttl_resolver() -> dict[str, int | None]:
        """Return the live grant-TTL mapping from Settings (L-Sec-3).

        Resolved per-call so an operator settings hot-reload takes effect
        without re-wiring the container.  ``permanent`` stays ``None``
        (non-expiring) — the operator knob is the two BOUNDED scopes.
        """
        s = container.settings.security
        return {
            "session": int(s.session_grant_ttl_seconds),
            "process": int(s.process_grant_ttl_seconds),
            "permanent": None,
        }

    create_grant = CreatePathGrantUseCase(
        grant_repository=grants_repo,
        event_bus=events,
        clock=clock,
        ids=ids,
        # M-Sec-4: fold grant-creation into the unified security-audit
        # trail so a reviewer can pair every persisted grant with the
        # exact security decision that produced it.
        audit_sink=audit_sink,
    )
    approve_permission = ApprovePermissionUseCase(
        request_repository=requests_repo,
        event_bus=events,
        clock=clock,
        wait_registry=permission_wait_registry,
        create_grant=create_grant,
        audit_sink=audit_sink,
        # L-Sec-3: live-resolved grant TTL from Settings.
        ttl_resolver=_ttl_resolver,
    )
    reject_permission = RejectPermissionUseCase(
        request_repository=requests_repo,
        event_bus=events,
        clock=clock,
        wait_registry=permission_wait_registry,
        # Constraint G (reject side): share the same CreatePathGrantUseCase so
        # a session/process/permanent rejection persists a deny-effect grant
        # (mirrors approve's create_grant wiring above).
        create_grant=create_grant,
        audit_sink=audit_sink,
        # L-Sec-3: same TTL resolver on both terminal paths.
        ttl_resolver=_ttl_resolver,
    )
    cancel_permission = CancelPermissionRequestUseCase(
        request_repository=requests_repo,
        event_bus=events,
        clock=clock,
        wait_registry=permission_wait_registry,
        # M-Sec-2: cancel path now writes a user_cancelled audit row.
        audit_sink=audit_sink,
    )
    revoke_grant = RevokePathGrantUseCase(
        grant_repository=grants_repo,
        event_bus=events,
        clock=clock,
    )
    register_skill_capability = RegisterSkillCapabilityUseCase(
        registry=skill_capability_registry,
        audit_sink=audit_sink,
        clock=clock,
        ids=ids,
        scanner=skill_injection_scan,
        event_bus=events,
    )
    unregister_skill_capability = UnregisterSkillCapabilityUseCase(
        registry=skill_capability_registry,
        audit_sink=audit_sink,
        clock=clock,
        ids=ids,
    )

    # R7-R10 — route-thinness cohesion use cases (business logic lifted
    # out of ``interfaces/http/routes/security.py``).
    apply_security_template = ApplySecurityTemplateUseCase(
        update_policy_use_case=update_policy,
    )
    # Per-skill run mode bridge — the Security > Skill panel renders the
    # 4-state mode switch from the SAME discovery payload it renders
    # capabilities from, so discovery needs each skill's mode.
    #
    # Truth source: the ``forge.config`` document's ``skills.overrides``, read
    # through ``_load_skill_mode_overrides`` (the user_prefs KV store — see
    # that helper for why reading the JSON file instead is wrong). Precedence
    # (explicit ``mode`` > legacy ``enabled`` bool > per-skill default) is
    # delegated to ``qai.user_prefs.application.use_cases.skills._resolve_mode``
    # rather than re-implemented, so the Security panel and the Skills page can
    # never disagree about a skill's mode. The security context itself stays
    # pure: it only sees an injected awaitable.
    #
    # Bulk, not per-skill: one KV read per discovery pass instead of one per
    # discovered skill. Read live (State-Truth-First) so a mode flip on the
    # Skills page shows up on the next refresh with no reboot; any fault
    # degrades to ``{}`` and the UI shows no mode selected.
    async def _skill_mode_provider(
        skill_ids: Sequence[str],
    ) -> dict[str, str]:
        try:
            from qai.user_prefs.application.use_cases.skills import (
                _resolve_mode,
            )
        except Exception:  # noqa: BLE001 — degrade to "no modes known"
            return {}

        overrides = await _load_skill_mode_overrides()
        out: dict[str, str] = {}
        for skill_id in skill_ids:
            if not isinstance(skill_id, str) or not skill_id:
                continue
            # Resolve for EVERY discovered skill, not just the overridden
            # ones: ``_resolve_mode`` falls back to the per-skill default
            # (``_DEFAULT_SKILL_MODE``, "cloud" for most), which is the same
            # value the Skills page shows. Passing only override keys would
            # leave never-configured skills with no mode and the two surfaces
            # would disagree.
            #
            # ``npu_optimized=True``: report the PERSISTED operator intent.
            # The NPU gate is enforced at WRITE time by
            # ``SetSkillModeUseCase`` (a non-NPU skill can never have
            # ``local``/``both`` persisted), so passing ``False`` here would
            # silently rewrite a stored mode into ``cloud`` for display only.
            try:
                resolved = _resolve_mode(
                    overrides.get(skill_id), True, skill_id
                )
            except (TypeError, ValueError, AttributeError):
                resolved = ""
            out[skill_id] = resolved if isinstance(resolved, str) else ""
        return out

    skill_discovery = SkillDiscoveryUseCase(
        registry=skill_capability_registry,
        runtime_state=security_runtime_state,
        repo_root=container.repo_root,
        skill_mode_provider=_skill_mode_provider,
    )
    get_skill_policy = GetSkillPolicyUseCase(
        registry=skill_capability_registry,
        runtime_state=security_runtime_state,
    )

    # Phase 3b (P-17 §6.3) — unified audit funnel over the canonical sink.
    # JSONL fallback sinks (#7/#1) are owned by the apps bridges and wired in
    # there; here we front the queryable ``security_audit_entry`` sink so the
    # funnel is available as a first-class collaborator.
    security_audit_facade = SecurityAuditFacade(
        audit_sink=audit_sink,
        clock=clock,
        ids=ids,
    )

    return SecurityServices(
        policy_repository=policy_repo,
        path_grant_repository=grants_repo,
        permission_request_repository=requests_repo,
        audit_sink=audit_sink,
        audit_query=audit_query,
        smart_approval=smart_approval,
        reboot_signal=reboot_signal,
        # Post the 2026-07-01 sandbox cleanup ``process_runner`` is the
        # plain :class:`SubprocessProcessRunner`. Callers that resolved
        # :attr:`SecurityServices.process_runner` (chat exec / ai_coding
        # exec / app_builder runner) now get an un-decorated subprocess
        # runner. Field name locked per §3.1.
        process_runner=process_runner,
        update_policy_use_case=update_policy,
        check_permission_use_case=check_permission,
        request_permission_use_case=request_permission,
        approve_permission_use_case=approve_permission,
        reject_permission_use_case=reject_permission,
        cancel_permission_request_use_case=cancel_permission,
        create_path_grant_use_case=create_grant,
        revoke_path_grant_use_case=revoke_grant,
        # PR-501 — tail-appended fields
        channel_policy_repository=channel_policy_repo,
        ask_rate_limiter=ask_rate_limiter,
        permission_broadcast=permission_broadcast,
        # PR-502 — audit hook (lazy; lifespan installs it)
        audit_hook=audit_hook,
        # PR-504 — skill capability + security runtime state.
        skill_capability_registry=skill_capability_registry,
        register_skill_capability_use_case=register_skill_capability,
        unregister_skill_capability_use_case=unregister_skill_capability,
        security_runtime_state=security_runtime_state,
        # PR-092 — LLM-backed Smart Approval (None when unconfigured).
        smart_approval_llm=llm_smart_approval,
        # R7-R10 — route-thinness cohesion use cases.
        apply_security_template_use_case=apply_security_template,
        skill_discovery_use_case=skill_discovery,
        get_skill_policy_use_case=get_skill_policy,
        # P0 ASK restore — async ASK suspend/resume registry.
        permission_wait_registry=permission_wait_registry,
        # 2026-07-04 native-hook integration (PR-2) — OS-level guard64.dll
        # adapter (disabled no-op unless native_file_guard_enabled).
        native_file_guard=native_file_guard,
        # 2026-07-06 Phase 2 — durable pending-permission store + subprocess-gone
        # cleanup service. Store is a no-op when
        # ``permission_pending_persist`` is False (test / in-memory).
        permission_pending_store=permission_pending_store,
        pending_cleanup_service=pending_cleanup_service,
        # L-Sec-4 — observation-only stale-ASK watchdog.
        wait_watchdog_service=wait_watchdog_service,
        # 2026-07-07 Phase 3b (P-17 §6.3) — unified security-audit funnel.
        security_audit_facade=security_audit_facade,
    )
