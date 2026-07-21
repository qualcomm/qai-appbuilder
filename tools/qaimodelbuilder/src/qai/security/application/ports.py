# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Abstract ports (Protocol-based) for the security application layer.

These describe the interface the use cases need from the outside world;
adapters in S4 PR-040+ provide concrete implementations (aiosqlite, REST
clients, etc.).

All ports are :class:`typing.Protocol` so adapters can be plain duck-typed
classes without inheritance — this keeps the dependency direction strictly
from adapters to the application package.

Repository ports return / accept domain aggregates (``Policy``,
``PathGrant``, ``PermissionRequest``); they do **not** leak storage
details (rows, aiosqlite cursors, …).
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from qai.security.domain.entities import (
    AuditEntry,
    ChannelPolicy,
    PermissionRequest,
    Policy,
    PathGrant,
)
from qai.security.domain.skill_capability import SkillCapability
from qai.security.domain.value_objects import (
    AceMask,
    Channel,
    RequestId,
    Resource,
    Subject,
)

__all__ = [
    "AclTrackingRecorderPort",
    "AskRateLimiterPort",
    "AuditHookPort",
    "AuditQueryPort",
    "AuditSinkPort",
    "AutoApprovePort",
    "ChannelPolicyRepositoryPort",
    "NativeFileGuardPort",
    "PathGrantRepositoryPort",
    "PermissionBroadcastPort",
    "PermissionPendingStorePort",
    "PermissionRequestRepositoryPort",
    "PolicyRepositoryPort",
    "RebootSignalPort",
    "RuntimeStatePersistencePort",
    "SkillCapabilityRegistryPort",
    "SmartApprovalDecision",
    "SmartApprovalPort",
]


# ---------------------------------------------------------------------------
# Repositories
# ---------------------------------------------------------------------------
@runtime_checkable
class PolicyRepositoryPort(Protocol):
    """Persistence for the singleton :class:`Policy` aggregate.

    Implementations must enforce a single Policy instance per deployment;
    :meth:`save` overwrites rather than appends. The :meth:`load` operation
    must be cheap enough to call on the hot path of permission checks.
    """

    async def load(self) -> Policy:
        """Return the current Policy.

        Implementations should return ``Policy.empty(now=...)`` (with an
        appropriate timestamp) when no policy has ever been saved, rather
        than raising ``NotFoundError``.
        """
        ...

    async def save(self, policy: Policy) -> None:
        """Persist ``policy``, replacing the previous version atomically."""
        ...


@runtime_checkable
class PathGrantRepositoryPort(Protocol):
    """CRUD storage for persistent ACL entries (``PathGrant``).

    The legacy system serialised these in ``config/persistent_acl.json``
    (89.7 KB, 7800+ entries — see inventory §2.2). PR-026 will migrate
    them 1:1 into a SQLite table with the same logical shape.
    """

    async def get(self, grant_id: str) -> PathGrant | None:
        """Return the grant with this id, or ``None`` if absent."""
        ...

    async def list_for_subject(
        self, subject: Subject
    ) -> list[PathGrant]:
        """Return all grants belonging to ``subject``.

        The list may be empty; ordering is implementation-defined but
        should be stable across consecutive calls when no writes happen.
        """
        ...

    async def list_for_path(self, path: str) -> list[PathGrant]:
        """Return all grants whose ``path`` field equals ``path``."""
        ...

    async def list_all(self) -> list[PathGrant]:
        """Return every persisted grant (PR-4 — startup seeding).

        Ordering is implementation-defined but should be stable when no
        writes happen. Used to seed the native FileGuard persistent
        whitelist from durable (non-expiring / ``permanent``) grants at
        API startup; callers filter expiry themselves.
        """
        ...

    async def save(self, grant: PathGrant) -> None:
        """Insert or update ``grant``, keyed by ``grant_id``."""
        ...

    async def delete(self, grant_id: str) -> None:
        """Remove the grant with this id.

        Should silently no-op if the grant does not exist; callers that
        care about not-found semantics must check via :meth:`get` first.
        """
        ...


@runtime_checkable
class AclTrackingRecorderPort(Protocol):
    """Append-only audit trail of grant lifecycle events (U-013 / 6-H2).

    Replaces the legacy ``data/persistent_acl_tracking.txt`` file
    (``backend/security/persistent_acl.py:_add_tracking_entry`` /
    ``_remove_tracking_entry``) with the indexed ``security_acl_tracking``
    table (``qai-db-schema.md`` §1.4). The V1 file held one line per
    *active* grant keyed by a ``PR``/``PF``/``PM``/``MF`` prefix encoding
    ``access_type`` × ``recursive``; V2 stores that semantic context on
    the referenced ``security_path_grant`` row (mask + path) and keeps
    this table as a pure lifecycle log keyed by ``event_type``:

    * ``add``    — emitted when a new grant is created
      (:class:`CreatePathGrantUseCase`);
    * ``revoke`` — emitted when an operator/admin revokes a grant
      (:class:`RevokePathGrantUseCase`);
    * ``remove`` — reserved for non-revocation removals (e.g. expiry
      sweeps) so the two causes stay distinguishable in audit.

    Implementations MUST be append-only — there is no update/delete.
    Recording MUST be best-effort safe to call inside a grant
    transaction; a tracking failure must not roll back the grant write
    (matching V1 where the ``.txt`` update was wrapped in a
    ``try/except OSError`` that only logged).
    """

    async def record(
        self,
        *,
        grant_id: str,
        event_type: str,
        occurred_at: datetime,
        note: str = "",
    ) -> None:
        """Append one lifecycle event for ``grant_id``.

        ``event_type`` MUST be one of ``"add"`` / ``"remove"`` /
        ``"revoke"`` (mirrors the table ``CHECK`` constraint). ``note``
        is an optional free-text annotation (truncated to 1024 chars by
        the adapter to satisfy the schema length check).
        """
        ...


@runtime_checkable
class PermissionRequestRepositoryPort(Protocol):
    """Storage for in-flight and resolved PermissionRequest aggregates."""

    async def get(self, request_id: RequestId) -> PermissionRequest | None:
        """Return the request with this id, or ``None`` if absent."""
        ...

    async def list_pending(self) -> list[PermissionRequest]:
        """Return all currently PENDING requests."""
        ...

    async def save(self, request: PermissionRequest) -> None:
        """Insert or update ``request``, keyed by ``request_id``."""
        ...


# ---------------------------------------------------------------------------
# Audit / smart approval / reboot signal
# ---------------------------------------------------------------------------
@runtime_checkable
class AuditSinkPort(Protocol):
    """Append-only sink for :class:`AuditEntry` records.

    Implementations must be append-only — there is no ``update`` or
    ``delete`` operation, by contract. **Read-side affordances live on
    the companion** :class:`AuditQueryPort` so the write surface stays
    minimal and CQRS-style isolated (see ``api-contract.md`` §7.6,
    issue (a) decision A — PR-040).
    """

    async def append(self, entry: AuditEntry) -> None:
        """Persist ``entry``."""
        ...


@runtime_checkable
class AuditQueryPort(Protocol):
    """Read-side port for :class:`AuditEntry` records (PR-040 / issue a).

    Split out of :class:`AuditSinkPort` so that:

    * the write surface stays append-only by contract;
    * the query surface can grow filters / pagination independently
      without polluting the writer adapter;
    * adapters can apply different concurrency / consistency strategies
      to writes vs reads (e.g. WAL reader vs writer connection pools).
    """

    async def recent(self, *, limit: int) -> list[AuditEntry]:
        """Return the most recent audit entries, newest first.

        ``limit`` must be a non-negative int; ``limit=0`` is allowed and
        returns an empty list. Implementations should leverage the
        ``occurred_at DESC`` index on ``security_audit_entry``.
        """
        ...

    async def query_native_denies_by_pid_tree(
        self,
        *,
        root_pid: int,
        since: datetime,
        max_depth: int = 3,
        limit: int = 50,
    ) -> tuple[AuditEntry, ...]:
        """Return native FileGuard DENY entries triggered by ``root_pid`` or
        any of its (transitive) descendants since ``since``.

        Only entries with ``subject.kind == "system"`` AND
        ``subject.identifier == "native.file_guard"`` AND
        ``decision == PolicyAction.DENY`` are returned. Descendant discovery
        walks up to ``max_depth`` levels via ``actor_parent_pid`` (Python-side
        iteration over a windowed candidate set) - deeper trees are truncated
        (AGENTS.md tradeoff: 3 covers ~95%+ of LLM shell patterns like
        ``bash -> python -> mv``).

        ``max_depth`` is the maximum hop distance between ``root_pid`` and a
        matched denial's ``actor_pid``. ``max_depth = 1`` matches only direct
        children (``bash(root) -> mv(denied)``); ``max_depth = 3`` extends to
        great-grandchildren (``bash -> python -> bash -> mv``), covering the
        canonical LLM shell-nesting pattern.

        Ordering: entries within a single call are ordered by
        ``occurred_at ASC`` (earliest-first) so the caller can render
        "N operations denied in execution order".

        ``limit`` caps the result count (defensive against runaway processes
        that trigger hundreds of denies); the
        ``ix_security_audit_entry_subject_time`` index is used to bound the
        window scan.

        Never raises - a DB glitch returns an empty tuple so the caller
        (exec / bgp handler) never breaks its own tool result on audit
        query failure.
        """
        ...


class SmartApprovalDecision(str, Enum):
    """Tri-state decision returned by :class:`SmartApprovalPort`.

    Modelled as a ``(str, Enum)`` so values round-trip through JSON /
    SQLite without bespoke encoders. PR-026 §10.2 chose this shape over
    the original sentinel class to align with the other 9 enums in the
    domain (mypy strict infers ``Literal`` correctly here).

    The three states are intentionally named after the actor's intent
    rather than the resulting state of the request:

    * ``APPROVE`` — the heuristic recommends granting access.
    * ``REJECT`` — the heuristic recommends denying access.
    * ``UNDECIDED`` — the heuristic abstains; the use case must defer
      to a human reviewer (the request stays PENDING).
    """

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    UNDECIDED = "UNDECIDED"

    @property
    def label(self) -> str:
        """Backward-compat alias for the legacy sentinel ``label`` API.

        Pre-PR-026 the class exposed ``decision.label``; downstream
        adapters / tests may still depend on this attribute, so we
        keep it as a thin shim returning :attr:`Enum.value`.
        """

        return self.value


@runtime_checkable
class SmartApprovalPort(Protocol):
    """Optional extension point for automatic approval heuristics.

    Adapters may return ``UNDECIDED`` to defer to a human reviewer; the
    use case treats that as "remain pending". A null implementation that
    always returns ``UNDECIDED`` is acceptable in development / tests.
    """

    async def evaluate(
        self,
        *,
        subject: Subject,
        resource: Resource,
        requested_mask: AceMask,
    ) -> SmartApprovalDecision:
        """Return the smart-approval decision for this candidate."""
        ...


@runtime_checkable
class AutoApprovePort(Protocol):
    """Tri-state auto-approve / command-list pre-check (U-005 / 5-H4).

    Restores the V1 ``PolicyCenter.is_auto_approved`` decision that runs
    *before* the FileGuard policy rules
    (``backend/security/policy.py:659-700``): operations that the
    operator has explicitly auto-approved (per-op booleans + trusted
    paths) short-circuit to ALLOW, and the exec command whitelist /
    blacklist gate the auto-approval of ``exec``.

    The V2 runtime-toggleable state lives in
    :class:`qai.security.application.security_runtime_state.SecurityRuntimeStateService`
    (the ``auto_approve`` bucket: ``{enabled, trusted_paths,
    command_whitelist, command_blacklist}``). This port is the thin
    application surface :class:`CheckPermissionUseCase` consults at the
    top of ``execute`` before walking the policy rules.

    Tri-state return contract (mirrors the three V1 outcomes):

    * ``True``  — auto-approved → the use case short-circuits to ALLOW.
    * ``False`` — explicitly denied by the command blacklist → the use
      case short-circuits to DENY (V1 ``_command_passes_lists`` returns
      ``False`` on a blacklist hit, blacklist taking priority).
    * ``None``  — no opinion → the use case proceeds to the normal
      :meth:`Policy.evaluate_request` + grant cascade (pre-U-005
      behaviour byte-for-byte).
    """

    def is_auto_approved(
        self,
        *,
        resource: Resource,
        requested_mask: AceMask,
    ) -> bool | None:
        """Return the tri-state auto-approve decision (see class docstring).

        Implementations MUST be cheap (hot path, called on every
        permission check) and side-effect-free.
        """
        ...


@runtime_checkable
class RuntimeStatePersistencePort(Protocol):
    """Persist + restore the operator-tunable security runtime-state buckets.

    Background
    ----------
    The legacy build kept the security/auto-approve/path-pattern flags in
    ``data/sandbox_state.json`` so an operator flip survived a restart.
    The refactor initially moved these into a purely in-memory
    :class:`SecurityRuntimeStateService` (recovered to defaults on each
    boot). Per the 2026-06 security-settings unification (decision 2A)
    user-flipped runtime buckets MUST again survive a restart, but the
    security *context* is not allowed to touch the filesystem / DB
    directly (Clean Architecture; domain-purity + context-isolation).

    This port lets the apps layer inject a persistence sink that writes
    the runtime buckets into the shared ``forge_config`` document and
    reads them back at DI build time. Implementations are synchronous and
    best-effort: a persistence failure MUST NOT crash a runtime toggle
    (the in-memory state stays authoritative for the live process).
    """

    def load(self) -> dict[str, Any]:
        """Return the persisted runtime-state buckets (``{}`` when absent).

        The returned mapping is keyed by bucket name (``auto_approve`` /
        ``path_patterns`` / ``project_access`` / ``skill_policies``)
        plus the top-level scalars (``enabled`` /
        ``mode`` / ``dynamic_authorization``). Unknown / malformed values
        are the caller's responsibility to coerce.
        """
        ...

    def save(self, state: dict[str, Any]) -> None:
        """Persist the full runtime-state snapshot (best-effort).

        ``state`` is the serialisable dict produced by
        :class:`SecurityRuntimeStateService`. Implementations overwrite the
        runtime-state region of the backing document atomically enough for
        a single-process daemon; failures are swallowed (logged) so a UI
        toggle never 500s on a transient disk error.
        """
        ...


@runtime_checkable
class RebootSignalPort(Protocol):
    """Emits the REBOOT_EXIT_CODE = 75 signal.

    Implementations must NOT call ``sys.exit`` directly — the use case
    decides *when* to schedule a reboot; the port records the *intent*
    and lets the supervising process actually perform the exit.
    See refactor-plan §8.11 for the contract.
    """

    async def request_reboot(self, *, reason: str) -> None:
        """Record that a reboot is required and schedule it.

        Implementations may coalesce multiple calls within a short window
        so that bulk policy updates don't trigger N restarts.
        """
        ...


# ---------------------------------------------------------------------------
# Channel policy + ask-rate limiter (PR-501)
# ---------------------------------------------------------------------------
@runtime_checkable
class ChannelPolicyRepositoryPort(Protocol):
    """Persistence for :class:`ChannelPolicy` rows keyed by channel name.

    Replaces the inline ``PolicyCenter._no_ui_channels`` set
    (``backend/security/policy.py:377-484``) with an explicit aggregate
    that operators can read / update via ``GET /api/security/channels``
    in PR-504.

    ``install`` seeds one row per
    :attr:`qai.security.domain.value_objects.Channel._ALLOWED_NAMES`
    member; the repository never synthesises defaults at read time so
    ``get`` returning ``None`` is a real "not configured" signal that
    the use case must surface as
    :class:`qai.security.domain.errors.ChannelPolicyNotFoundError`.
    """

    async def get(self, channel_name: str) -> ChannelPolicy | None:
        """Return the policy for ``channel_name`` or ``None`` if absent."""
        ...

    async def list_all(self) -> list[ChannelPolicy]:
        """Return every configured channel policy.

        Order is implementation-defined but should be stable across
        reads when no writes happen in between.
        """
        ...

    async def save(self, policy: ChannelPolicy) -> None:
        """Insert or update ``policy`` keyed by ``policy.name``."""
        ...


@runtime_checkable
class AskRateLimiterPort(Protocol):
    """Tracks ASK counts inside an
    :class:`qai.security.domain.value_objects.AskQuotaWindow`.

    Adapters are free to back the counter with an in-process
    ``collections.deque`` (single-worker default) or an external store
    (Redis) — the contract only fixes the operations the use case
    relies on.

    The :meth:`check_and_record` method is the hot-path call: it
    atomically asks "is this channel/subject still under quota?" and,
    on a yes, records the ASK so the next call sees the updated count.
    A no answer does NOT record the ASK (avoids feedback loops where
    repeated requests keep extending the window).
    """

    async def check_and_record(
        self,
        *,
        channel: Channel,
        subject: Subject,
        window_seconds: int,
        max_asks: int,
        now: datetime,
    ) -> bool:
        """Return ``True`` iff the ASK is allowed under the quota.

        On ``True`` the ASK is recorded against
        ``(channel.name, subject)``; on ``False`` no state is mutated.
        """
        ...


@runtime_checkable
class PermissionBroadcastPort(Protocol):
    """Outbound broadcast surface for permission FSM events (PR-501).

    Replaces the legacy ``PolicyCenter._broadcast_callback`` indirection
    (``backend/security/policy.py:1202-1270``) where main.py registered
    a closure pumping ``ask_user`` payloads onto SSE. In the new
    architecture the use case publishes a typed event via this port
    and the SSE / WS bridge in ``apps.api.*`` translates it for the
    wire.

    The port is intentionally minimal — the bridge owns transport
    concerns (SSE frame format, retry, fan-out across tabs) — so the
    domain layer stays unaware of FastAPI.
    """

    async def publish_permission_request(
        self,
        request: PermissionRequest,
        *,
        channel: Channel | None,
    ) -> None:
        """Notify subscribers of a new pending request."""
        ...

    async def publish_ask_blocked(
        self,
        *,
        channel: Channel,
        subject: Subject,
        resource: Resource,
        reason: str,
    ) -> None:
        """Notify subscribers that an ASK was short-circuited.

        ``reason`` is one of ``"no_ui_channel"`` / ``"rate_limited"``
        / ``"channel_policy_missing"`` (extensible).
        """
        ...


# ---------------------------------------------------------------------------
# Audit hook (PR-502)
# ---------------------------------------------------------------------------
@runtime_checkable
class AuditHookPort(Protocol):
    """Process-level on/off switch for the PEP 578 audit hook.

    The actual hook implementation lives in
    :mod:`qai.security.infrastructure.audit_hook` (sync, hot-path
    sensitive, must not import any port). This port is the thin
    application-layer surface that
    ``apps.api.lifespan`` / ``interfaces.http.routes.security`` use to
    request install / uninstall without reaching into infrastructure
    directly.

    Implementations are expected to be **idempotent**: repeated
    :meth:`install` calls return the same underlying hook handle, and
    :meth:`uninstall` is a no-op when the hook is already disabled.
    The :attr:`installed` property reflects the *effective* state —
    it is ``True`` only when the hook is registered AND not soft-
    disabled.
    """

    def install(self) -> None:
        """Install (or refresh) the singleton audit hook."""
        ...

    def uninstall(self) -> None:
        """Soft-disable the audit hook (idempotent)."""
        ...

    @property
    def installed(self) -> bool:
        """``True`` while the hook is actively making decisions."""
        ...


# ---------------------------------------------------------------------------
# Skill capability registry — PR-504
# ---------------------------------------------------------------------------
@runtime_checkable
class SkillCapabilityRegistryPort(Protocol):
    """Persistence + lookup for active :class:`SkillCapability` declarations.

    PR-504 ships an in-memory adapter keyed by ``(skill_name)``; the
    port shape is deliberately compatible with a SQLite-backed adapter
    should one ever be required, but the canonical source of truth is
    boot-time registration from skill bundles, so the in-memory
    registry is the production fit. Callers MUST treat
    :meth:`list_active` as a snapshot — concurrent register / unregister
    operations may race; the registry returns whatever set is current
    at call time.
    """

    async def register(
        self,
        skill_name: str,
        capability: SkillCapability,
        *,
        scanner_warnings: tuple[str, ...] = (),
    ) -> None:
        """Register or replace ``capability`` for ``skill_name``.

        ``scanner_warnings`` carries non-fatal threat names the
        ``RegisterSkillCapabilityUseCase`` chose to admit (e.g.
        ``"html_comment_injection"`` is medium-severity and may be
        merely logged); the registry persists them alongside the
        capability so operators can list them via the UI.
        """
        ...

    async def unregister(self, skill_name: str) -> None:
        """Remove the capability for ``skill_name``; idempotent."""
        ...

    async def list_active(self) -> list[SkillCapability]:
        """Return all currently registered capabilities."""
        ...

    async def get(self, skill_name: str) -> SkillCapability | None:
        """Return the capability registered for ``skill_name`` or ``None``."""
        ...

    async def find_trusted_binary_for(
        self, exe_path: str
    ) -> SkillCapability | None:
        """Return the first active capability whose ``trusted_binaries``
        glob matches ``exe_path`` (U-003c / 6-H12).

        Restores V1's global ``skill_policy.is_trusted_binary`` query
        (``backend/security/skill_policy.py:485-529``): the legacy
        function walked every registered skill's ``trusted_binaries``
        globs and returned ``True`` on the first match. V2 returns the
        *owning* capability instead of a bare boolean so callers can
        surface which skill authorised the binary (audit / UI); a
        ``None`` return is the "no skill trusts this binary" signal.

        Matching mirrors :meth:`SkillCapability.covers_exec` (fnmatch
        glob, case-folded, slash-normalised). Iteration order across
        active skills is implementation-defined; callers must treat the
        result as "some skill that trusts it" rather than a stable pick.
        """
        ...

    async def list_all_trusted_binaries(self) -> list[str]:
        """Return the de-duplicated union of every active capability's
        ``trusted_binaries`` patterns (U-003c / 6-H12).

        Aggregates across all currently-registered skills so callers
        (e.g. the sandbox launcher policy builder) can enumerate the
        full trusted-binary allowlist without holding a reference to
        each capability. Order is first-seen-stable; the empty list
        means no active skill declared any trusted binary.
        """
        ...


# ---------------------------------------------------------------------------
# Native FileGuard hook — 2026-07-04 native-hook integration
# ---------------------------------------------------------------------------
@runtime_checkable
class NativeFileGuardPort(Protocol):
    """Application-layer surface for the native ``guard64.dll`` OS hook.

    The concrete adapter
    (:mod:`qai.security.adapters.native_file_guard`) owns the ctypes
    lifecycle of the compiled ``vendor/bin/<arch>/guard64.dll`` (Detours
    hook of ``ntdll`` file APIs). This port is the thin, infra-free
    contract that ``apps.api`` DI / lifespan and the security use cases
    (grant / revoke rule hot-sync) consume, so no application code ever
    reaches into ctypes / infrastructure directly.

    Design mirrors the other process-level ports (``AuditHookPort``):

    * The hook is a **process singleton** — a second :meth:`start` on an
      already-started guard is a no-op that keeps the first callback.
    * All rule mutators are **idempotent** and **best-effort**: a
      mutation on a not-started guard, or a disabled adapter, is a
      silent no-op returning ``False`` (nothing was applied) rather than
      raising, so a callers's grant / revoke flow never fails because
      the native hook is off.
    * When ``native_file_guard_enabled`` is ``False`` the DI layer wires
      a **disabled no-op** implementation whose :attr:`is_active` is
      ``False`` and whose every method is a zero-side-effect no-op —
      guaranteeing "hook off ⇒ zero effect" (PR-2 criterion).

    Rule model (matches ``guard.py``):

    * deny rules (blacklist) — :meth:`add_deny_rule` /
      :meth:`remove_deny_rule`. A path prefix under a deny rule is
      rejected outright.
    * allow rules (whitelist) — :meth:`add_allow_rule` /
      :meth:`remove_allow_rule`. A path prefix under an allow rule
      bypasses the filter callback entirely (ALLOW).
    * process exceptions — :meth:`add_process_exception`. An exe whose
      path matches is exempted from all hooks (highest priority).

    Precedence (DLL side): deny > allow > filter-callback; a process
    exception short-circuits above all of them.
    """

    @property
    def is_active(self) -> bool:
        """``True`` while the DLL is loaded AND the hook is installed."""
        ...

    def start(self) -> bool:
        """Load + install the hook; register the filter callback.

        Returns ``True`` when the hook is (or already was) active.
        Idempotent. A disabled no-op adapter returns ``False`` without
        loading anything.
        """
        ...

    def stop(self) -> None:
        """Uninstall the hook + unload the DLL (idempotent)."""
        ...

    def add_deny_rule(self, path: str, *, session_only: bool = True) -> bool:
        """Add a blacklist prefix rule (matching path ⇒ DENY)."""
        ...

    def remove_deny_rule(
        self, path: str, *, session_only: bool = True
    ) -> bool:
        """Remove a blacklist prefix rule (idempotent)."""
        ...

    def add_allow_rule(self, path: str, *, session_only: bool = True) -> bool:
        """Add a whitelist prefix rule (matching path ⇒ bypass filter)."""
        ...

    def add_read_only_allow_rule(
        self, path: str, *, session_only: bool = True
    ) -> bool:
        """Add an OP-AWARE read-only whitelist prefix rule.

        Read events matching the prefix bypass the filter; write / delete /
        execute events still fall through to the filter (⇒ ASK). No-op
        (``False``) when the loaded DLL predates the read-only whitelist
        export.
        """
        ...

    def add_op_mask_allow_rule(
        self, path: str, mask: int, *, session_only: bool = True
    ) -> bool:
        """Add an OP-MASKED whitelist prefix rule.

        ``mask`` is a bitfield (READ=1, WRITE=2, EXECUTE=4, DELETE=8). An event
        whose op bit is set in ``mask`` and whose path matches the prefix
        bypasses the filter; an event whose op bit is unset falls through to the
        filter (⇒ ASK). No-op (``False``) when the loaded DLL predates the
        op-masked whitelist export.
        """
        ...

    def remove_allow_rule(
        self, path: str, *, session_only: bool = True
    ) -> bool:
        """Remove a whitelist prefix rule (idempotent)."""
        ...

    def add_process_exception(self, exe_path: str) -> bool:
        """Exempt a process (by exe-path prefix) from all hooks."""
        ...

    def get_trusted_infra_token(self) -> str | None:
        """Return the host-lifetime random trust token, or None if not started
        or disabled. Used by process spawners to inject QAI_FILEGUARD_TRUST_TOKEN
        into child env, allowing the native DLL to classify children as
        TrustedInfra (skip ASK on undetermined paths)."""
        ...

    def diagnostics(self) -> dict[str, Any]:
        """Return the DLL's internal counters / config snapshot.

        Empty dict when the hook is inactive / the DLL predates the
        ``GetDiagnostics`` export.
        """
        ...


# ---------------------------------------------------------------------------
# Phase 2 (2026-07-06) — durable pending-permission store
# ---------------------------------------------------------------------------
@runtime_checkable
class PermissionPendingStorePort(Protocol):
    """Durable mirror of the in-memory ASK ``PermissionWaitRegistry``.

    The Phase 2 (concurrent-popup + no-timeout wait) design persists every
    IN-FLIGHT ASK to the ``security_pending_permission`` table (migration
    052) so that:

    * a service restart with unanswered ASKs can rehydrate the UI's
      "pending" list on next boot and mark rows whose ``boot_id`` != the
      current boot as ORPHANED (their DLL pipe is gone with the previous
      process; the operator dismisses them);
    * :class:`PendingCleanupService` (10s scan interval) can look up
      unresolved rows by ``pid`` and resolve them as ``subprocess_gone``
      when the process is dead;
    * the ``/api/security/permission/cancel`` route (by ``pid`` or
      ``cancel_all``) can enumerate outstanding request-ids without
      walking the domain :class:`PermissionRequest` repository.

    This port is INTENTIONALLY narrow — it mirrors the operational registry
    (native event context: pid / process_path / command_line / event
    bitfield / boot_id / created_at / resolved_at / resolution), NOT the
    domain aggregate (subject / resource / mask / state). The two are
    joined on ``request_id`` when the UI needs the full picture; the
    domain state machine (pending → approved / rejected / cancelled /
    expired) stays authoritative on :class:`PermissionRequestRepositoryPort`.

    Adapters MUST be best-effort and non-fatal: a persistence failure
    MUST NOT abort a fresh ASK (the in-memory registry stays authoritative
    for the live process). A ``NullPermissionPendingStore`` adapter
    (adapters/pending_permission_store.py) provides a zero-side-effect
    no-op used when ``SecuritySettings.permission_pending_persist`` is
    False (test / in-memory deployments).
    """

    async def insert_pending(
        self,
        *,
        request_id: str,
        pid: int,
        process_path: str,
        command_line: str,
        path: str,
        event: int,
        boot_id: str,
        created_at: datetime,
        actor_parent_pid: int | None = None,
    ) -> None:
        """Insert a fresh pending row. Idempotent on ``request_id``.

        ``event`` is the native ``Event`` bitfield (1=READ, 2=WRITE,
        4=EXECUTE, 8=DELETE). ``boot_id`` is this backend process's boot
        id (minted once in lifespan). A duplicate insert (same
        ``request_id``) is a silent no-op — a retried native callback
        for the same ASK never spawns two rows.
        """
        ...

    async def mark_resolved(
        self,
        *,
        request_id: str,
        resolved_at: datetime,
        resolution: str,
    ) -> None:
        """Mark ``request_id`` resolved.

        ``resolution`` MUST be one of ``"allow"`` / ``"deny"`` /
        ``"user_cancelled"`` / ``"subprocess_gone"`` / ``"shutdown"``
        (matches the schema CHECK). Unknown values MAY be coerced by the
        adapter to ``"deny"`` (fail-safe). Idempotent — a second call
        for the same id is a silent no-op (only the first resolution is
        honoured).
        """
        ...

    async def list_unresolved(self) -> list[dict[str, Any]]:
        """Return every row whose ``resolved_at`` is NULL.

        Each dict carries the row shape used by the UI:
        ``{request_id, pid, process_path, command_line, path, event,
        boot_id, created_at (datetime), actor_parent_pid}``. Order is
        ``created_at ASC`` (oldest first — the UI shows the pending
        queue in FIFO order). Empty list on error / missing table
        (fail-safe).
        """
        ...

    async def list_by_pid(self, pid: int) -> list[str]:
        """Return unresolved ``request_id``s for ``pid``.

        Used by the ``/api/security/permission/cancel`` route
        (``{"pid": N}`` body) and by :class:`PendingCleanupService` when
        it detects a dead pid. Empty list when no rows match.
        """
        ...

    async def find_dedupe(
        self, *, pid: int, path: str, event: int
    ) -> str | None:
        """Best-effort cross-restart dedupe lookup.

        Returns the ``request_id`` of an UNRESOLVED row matching the
        ``(pid, path, event)`` triple, or ``None`` when no such row
        exists.         The in-memory ``PermissionWaitRegistry.register_or_dedupe``
        is the authoritative dedupe path in the live process; this
        method exists for post-restart rehydrate flows (Phase 2.5) where
        the in-memory index is empty but the row from the previous boot
        is still on disk.
        """
        ...

    async def resolve_orphaned_boots(self, current_boot_id: str) -> int:
        """Resolve stale-boot unresolved rows as ``shutdown`` (P-09).

        On startup, every unresolved row whose ``boot_id`` differs from
        ``current_boot_id`` belongs to a PREVIOUS process whose native
        DLL pipe thread is already dead — the waiter can never be woken.
        This marks those rows resolved with resolution ``"shutdown"``
        (matching native FailDecision shutdown semantics and the schema
        CHECK — there is deliberately NO ``"orphaned"`` resolution value).
        Rows for the CURRENT boot are left untouched. Returns the number
        of rows resolved. Best-effort / non-fatal: any error returns ``0``
        without raising (never aborts startup). TAIL-appended per §3.1
        with no positional default needed (single required arg).
        """
        ...
