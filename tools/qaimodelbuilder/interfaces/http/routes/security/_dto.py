# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Security routes — wire DTOs + module-level helpers (split from security.py).

Pure-move extraction (zero behaviour change). All Pydantic DTOs, the
domain⇄DTO mappers, the persistent-ACL / skill-policy / overview-state
helpers and the SSE serialisation helpers live here so the per-resource
``_register_*`` route modules can import exactly what they use.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, Field

from qai.platform.errors import ValidationError
from qai.security.application.use_cases import (
    skill_discovery as _skill_discovery,
)
from qai.security.domain.entities import (
    AuditEntry,
    PathGrant,
    PermissionRequest,
    Policy,
    PolicyRule,
)
from qai.security.domain.value_objects import (
    AceMask,
    PathPattern,
    PolicyAction,
    PolicyMatchKind,
    PolicyOp,
    PolicyScope,
    RequestId,
    Resource,
    Subject,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


# P-11 (backend) — the native FileGuard subject identity. Every intercepted
# OS file event from an LLM-spawned subprocess is attributed to
# Subject(kind="system", identifier="native.file_guard") by the native-hook
# bridge (see apps/api/_native_hook_bridge.py:69 ``_SUBJECT_IDENTIFIER``).
# There is no shared security-domain constant for this string (the domain
# stays unaware of the apps-layer bridge), so we mirror the literal here and
# in ``qai.security.domain.events.PermissionRequestedEvent.to_dict`` — both
# derive the SAME ``is_native_subprocess`` flag from the subject so the SSE
# frame and the ``/permission/pending`` re-hydration agree.
_NATIVE_SUBJECT_KIND = "system"
_NATIVE_SUBJECT_IDENTIFIER = "native.file_guard"


def _is_native_subprocess(subject: Subject) -> bool:
    """Return ``True`` iff ``subject`` is the native FileGuard identity."""
    return (
        subject.kind == _NATIVE_SUBJECT_KIND
        and subject.identifier == _NATIVE_SUBJECT_IDENTIFIER
    )




# ---------------------------------------------------------------------------
# Wire-format DTOs — Pydantic v2 BaseModel
# ---------------------------------------------------------------------------


class _SubjectDTO(BaseModel):
    """Wire shape for :class:`qai.security.domain.value_objects.Subject`."""

    kind: Literal["user", "preset", "system"]
    identifier: str = Field(..., min_length=1, max_length=512)


class _ResourceDTO(BaseModel):
    """Wire shape for :class:`qai.security.domain.value_objects.Resource`."""

    kind: Literal["path", "skill", "network", "exec", "dep"]
    identifier: str = Field(..., min_length=1, max_length=4096)


class _AceMaskDTO(BaseModel):
    """Wire shape for :class:`qai.security.domain.value_objects.AceMask`."""

    read: bool = False
    write: bool = False
    execute: bool = False
    delete: bool = False


class _PolicyRuleDTO(BaseModel):
    """Wire shape for a :class:`qai.security.domain.entities.PolicyRule`."""

    rule_id: str = Field(..., min_length=1, max_length=128)
    scope: Literal["user", "preset", "path"]
    pattern: str = Field(..., min_length=1, max_length=4096)
    case_sensitive: bool = False
    action: Literal["allow", "deny"]
    description: str = ""
    # ── tail-appended operation dimension (v2.7 §3.1 additive) ────────
    # Restores the V1 4-list taxonomy (read_allow / write_allow /
    # exec_allow_cwd / exec_deny_patterns) as an explicit per-rule value.
    # Defaults to "any" so callers that submitted only the original
    # fields keep working byte-for-byte (the rule then matches on path
    # glob regardless of operation, exactly as before).
    op: Literal["read", "write", "exec", "exec_deny", "any"] = "any"


class PolicyResponse(BaseModel):
    """``GET /api/security/policy`` payload.

    The ``rules`` array is the **locked** rules-based CRUD contract
    (v2.7 §3.1): ``rule_id`` / ``scope`` / ``pattern`` / ``action``
    field names and the array shape MUST NOT change — the Allow-Lists
    tab depends on them.

    The four operational toggles below are **tail-appended** (v2.7 §3.1
    additive surface) so the Overview tab can render the FileGuard
    status card (master switch / run mode / dynamic authorization / IM
    channel dialog gate) without a second round-trip. They are backed by
    the in-process :class:`SecurityRuntimeStateService` (``enabled`` /
    ``dynamic_authorization``) plus a ``policy_overview`` settings bucket
    (``run_mode`` / ``no_ui_channels``), NOT by the rules-based Policy
    aggregate — keeping the domain Policy pure while matching V1's flat
    policy-object wire shape (``policy.enabled`` / ``policy.mode`` /
    ``policy.dynamic_authorization`` / ``policy.no_ui_channels``).
    """

    version: int
    updated_at: str
    rules: list[_PolicyRuleDTO]
    # ── tail-appended operational toggles (v2.7 §3.1 additive) ────────
    enabled: bool = True
    mode: Literal["enforce", "audit_only"] = "enforce"
    dynamic_authorization: bool = True
    no_ui_channels: list[str] = Field(default_factory=list)
    # ── reboot indicator (additive — absent on GET, present on PUT) ────
    needs_reboot: bool = False


class PolicyVersionResponse(BaseModel):
    """``GET /api/security/policy/version`` payload."""

    version: int


class UpdatePolicyRequest(BaseModel):
    """``PUT /api/security/policy`` body.

    ``rules`` + ``reboot_reason`` are the original locked contract. The
    four operational toggles are tail-appended and OPTIONAL (all
    defaulted) so legacy callers that submit only ``rules`` keep working
    byte-for-byte; when present they are persisted to the runtime-state /
    ``policy_overview`` bucket (the rules CRUD path is untouched).
    """

    rules: list[_PolicyRuleDTO]
    reboot_reason: str = "policy changed"
    # ── tail-appended operational toggles (v2.7 §3.1 additive) ────────
    enabled: bool | None = None
    mode: Literal["enforce", "audit_only"] | None = None
    dynamic_authorization: bool | None = None
    no_ui_channels: list[str] | None = None


class CheckPermissionRequest(BaseModel):
    """``POST /api/security/permission/check`` body."""

    subject: _SubjectDTO
    resource: _ResourceDTO
    requested_mask: _AceMaskDTO
    correlation_id: str | None = None


class CheckPermissionResponse(BaseModel):
    """``POST /api/security/permission/check`` payload."""

    decision: Literal["allow", "deny"]
    matched_rule_id: str | None
    matched_grant_id: str | None
    audit_id: str


class RequestPermissionRequest(BaseModel):
    """``POST /api/security/permission/request`` body."""

    subject: _SubjectDTO
    resource: _ResourceDTO
    requested_mask: _AceMaskDTO


class _PermissionRequestDTO(BaseModel):
    """Common envelope used by request / approve / reject responses."""

    request_id: str
    subject: _SubjectDTO
    resource: _ResourceDTO
    requested_mask: _AceMaskDTO
    state: Literal["pending", "approved", "rejected", "expired", "cancelled"]
    created_at: str
    resolved_at: str | None
    resolution_reason: str
    # P-11 (backend) — native-subprocess discriminator. TAIL-appended per
    # §3.1 with a safe default so existing callers / persisted shapes stay
    # byte-compatible. Mirrors the SSE ``permission_request`` frame's
    # ``is_native_subprocess`` flag so a reconnect re-hydration via
    # ``GET /permission/pending`` preserves the flag (front-end grays out the
    # native-invalid "session" scope button). Derived from the request's
    # subject in :func:`_request_to_dto`.
    is_native_subprocess: bool = False


class PermissionRequestResponse(BaseModel):
    """Wire shape for a single PermissionRequest."""

    request: _PermissionRequestDTO


class PendingPermissionRequestsResponse(BaseModel):
    """``GET /api/security/permission/pending`` payload."""

    requests: list[_PermissionRequestDTO]


class ApprovePermissionRequest(BaseModel):
    """``POST /api/security/permission/{request_id}/approve`` body."""

    decided_by: _SubjectDTO | None = None
    reason: str = ""
    # P0 ASK restore — grant scope (V1 ``resolve_permission`` grant
    # vocabulary, ``policy.py:1670``): ``once`` (this call only, no persisted
    # grant) / ``session`` / ``process`` / ``permanent`` (persist a
    # PathGrant so the next same-path access skips the prompt). Optional,
    # tail-appended (v2.7 §3.1) — defaults to ``once`` so existing callers
    # that omit it keep the single-use behaviour.
    grant: str = "once"
    # P-11B — grant range: ``file`` (default, the single approved file, exact
    # match) or ``directory`` (authorize the parent directory the user
    # explicitly chose in the dialog; the backend derives the parent dir from
    # the file path and stores it with is_directory=True so sibling files
    # under it stop re-prompting). 2026-07-08 — ``program`` (exec resources
    # only): permanently allow the whole PROGRAM — the backend stores the
    # normalized command binary token (e.g. ``powershell``) with
    # is_program=True so any future command with the same binary is
    # auto-allowed (the user is asked once per program, not per command).
    # Tail-appended (§3.1) — defaults to ``file`` so existing callers keep
    # single-file semantics.
    grant_range: str = "file"


class RejectPermissionRequest(BaseModel):
    """``POST /api/security/permission/{request_id}/reject`` body."""

    decided_by: _SubjectDTO | None = None
    reason: str = ""


class CancelPermissionRequestBody(BaseModel):
    """``DELETE /api/security/permission/{request_id}`` body (optional).

    The HTTP ``DELETE`` method has no body in most clients; we accept an
    optional :attr:`cancelled_by` so administrative cancellations
    performed on behalf of a subject can be attributed correctly in
    audit trails. Frontends invoking the route from a user gesture
    typically send an empty body.
    """

    cancelled_by: _SubjectDTO | None = None


class CancelPendingPermissionBody(BaseModel):
    """``POST /api/security/permission/cancel`` body (Phase 2).

    Cancels ONE / MANY / ALL in-flight ASK popups from the operator side
    (the user clicked "cancel" on the dialog, or a shutdown hook chose to
    wipe the queue). Exactly ONE of :attr:`request_id`, :attr:`pid`, or
    :attr:`cancel_all` must be truthy — the route validates and 400s on
    an ambiguous request (empty / more-than-one).

    Semantics
    ---------
    * ``request_id`` — wake the future for this specific ASK as DENY;
      mark the durable row (if persistence is on) as ``user_cancelled``.
    * ``pid`` — enumerate every unresolved request_id for the given pid
      (via :class:`PermissionPendingStorePort.list_by_pid`) and DENY
      each. Useful when a specific subprocess should be silenced without
      the operator having to click through N popups.
    * ``cancel_all`` — enumerate every pending request_id in the
      in-memory registry (via :meth:`PermissionWaitRegistry.list_pending`)
      and DENY each. The "nuke the queue" panic button.
    """

    request_id: str | None = None
    pid: int | None = None
    cancel_all: bool = False


class CancelPendingPermissionResponse(BaseModel):
    """``POST /api/security/permission/cancel`` payload (Phase 2).

    ``cancelled`` is the list of request_ids that were actually woken —
    ids that were already resolved / unknown are silently skipped, so the
    caller can compare its input against this list to detect no-ops.
    """

    cancelled: list[str] = Field(default_factory=list)


class CreateGrantRequest(BaseModel):
    """``POST /api/security/path-grants`` body."""

    subject: _SubjectDTO
    path: str = Field(..., min_length=1, max_length=4096)
    mask: _AceMaskDTO
    source: Literal["user", "auto", "preset"] = "user"
    expires_at: str | None = None


class _PathGrantDTO(BaseModel):
    """Wire shape for a single :class:`PathGrant`."""

    grant_id: str
    subject: _SubjectDTO
    path: str
    mask: _AceMaskDTO
    source: Literal["user", "auto", "preset"]
    created_at: str
    expires_at: str | None


class GrantResponse(BaseModel):
    """``POST /api/security/path-grants`` payload."""

    grant: _PathGrantDTO


class GrantsListResponse(BaseModel):
    """``GET /api/security/path-grants`` payload."""

    grants: list[_PathGrantDTO]


class _AuditEntryDTO(BaseModel):
    """Wire shape for an :class:`AuditEntry`."""

    audit_id: str
    occurred_at: str
    subject: _SubjectDTO
    resource: _ResourceDTO
    decision: Literal["allow", "deny"]
    rule_id: str | None
    correlation_id: str | None
    note: str
    #: Origin channel (V1 audit-filter parity); ``None`` for system actions.
    channel: str | None = None
    #: Operation kind (read / write / delete / exec); ``""`` for legacy rows.
    #: Lets the Audit view distinguish a delete from a write. Tail-appended.
    op: str = ""
    #: Native sub-process attribution (guard64.dll events): triggering
    #: process image path / command line / pid / parent pid. Empty / ``None``
    #: for in-process tool events. Tail-appended (v2.7 §3.1 additive).
    process_path: str = ""
    command_line: str = ""
    actor_pid: int | None = None
    actor_parent_pid: int | None = None


class AuditRecentResponse(BaseModel):
    """``GET /api/security/audit/recent`` payload."""

    entries: list[_AuditEntryDTO]


class SecurityHealthResponse(BaseModel):
    """``GET /api/security/health`` payload (Overview header pill/banner).

    Restores V1 ``/api/security/health`` parity
    (``SecurityConfigPanel.js:239-249, 922-958``). The frontend polls
    this every 30 s and renders a 4-state pill plus a down/test_mode
    banner. Status semantics mirror V1:

    * ``ok``        — PolicyCenter readable, fail-closed in effect.
    * ``down``      — policy could not be loaded; all tool calls denied.
    * ``test_mode`` — sandbox globally disabled (``enabled=false``);
      analogous to V1's ``FILEGUARD_DISABLED=1`` bypass.
    * ``degraded``  — the software-layer FileGuard is enabled AND the
      native ``guard64.dll`` sub-process hook was requested
      (``native_enabled=true``) but is NOT actually active
      (``native_active=false``): the in-process tool write guard still
      works, but LLM-spawned sub-process file writes are unguarded. This
      surfaces "Python on but DLL failed to load" honestly on the
      Overview card instead of showing "healthy" (🔴 State-Truth-First).
    * ``unknown``   — transient startup state (never emitted here; the
      frontend uses it before the first poll resolves).

    The three ``native_*`` fields are **tail-appended** (v2.7 §3.1
    additive) so legacy callers keep working byte-for-byte; they report
    the real state of the OS-level sub-process hook:

    * ``native_enabled``     — the ``native_file_guard_enabled`` setting
      (operator intent: was the native hook requested?).
    * ``native_active``      — the LIVE probe
      (``NativeFileGuardPort.is_active``): DLL loaded AND hook installed.
      A ``native_enabled && !native_active`` mismatch is the ``degraded``
      trigger.
    * ``native_diagnostics`` — the DLL's internal counter/config snapshot
      (``.diagnostics()``); empty dict when the hook is inactive / the
      DLL predates the ``GetDiagnostics`` export.
    """

    status: Literal["ok", "down", "test_mode", "degraded", "unknown"]
    enabled: bool
    mode: Literal["enforce", "audit_only"]
    test_mode: bool
    # ── tail-appended native sub-process hook state (v2.7 §3.1) ────────
    native_enabled: bool = False
    native_active: bool = False
    native_diagnostics: dict[str, int] = Field(default_factory=dict)


class ApplyTemplateRequest(BaseModel):
    """``POST /api/security/templates`` body (apply a built-in template).

    Mirrors V1 ``applyPolicyTemplate`` (``SecurityConfigPanel.js:794-811``)
    which posts ``{ template: <id> }``. Applying a template replaces the
    Policy's rule set with the template's rules via the locked
    :class:`UpdatePolicyUseCase` (rules CRUD contract unchanged).
    """

    template: str = Field(..., min_length=1, max_length=64)


class ApplyTemplateResponse(BaseModel):
    """``POST /api/security/templates`` payload — the new Policy."""

    template: str
    policy: PolicyResponse


# ---------------------------------------------------------------------------
# Phase 3 (2026-07-01) — sandbox HTTP DTOs removed
# ---------------------------------------------------------------------------
# The ``SandboxStatusResponse`` / ``SandboxToggleRequest`` /
# ``SandboxSettingsResponse`` / ``SandboxSettingsUpdateRequest`` /
# ``SandboxTestRequest`` / ``SandboxTestMatchedRule`` /
# ``SandboxTestResponse`` / ``SandboxStatsResponse`` /
# ``SandboxBatchCommand`` / ``SandboxBatchRequest`` /
# ``SandboxBatchResultRow`` / ``SandboxBatchResponse`` /
# ``SandboxExecuteRequest`` DTOs (plus the ``_resolve_origin_channel`` /
# ``_sse_event`` / ``_serialise_sandbox_frame`` helpers) were lifted
# alongside the ``_sandbox.py`` route registrar. They consumed the
# AppContainer/LPAC launcher chain (``SandboxedProcessRunner`` /
# ``SandboxPolicyBuilder`` / ``DaemonManager`` / ``launcher_resolver``)
# which has been deleted as part of the Windows sandbox cleanup. The
# ``PathGrant`` DTOs (``_PathGrantDTO`` / ``GrantResponse`` /
# ``GrantsListResponse`` / ``_grant_to_dto``) are NOT sandbox-execution
# concepts — they describe persistent ACL grants and stay attached to
# the surviving ``/api/security/path-grants`` routes.


class AutoApproveConfigRequest(BaseModel):
    enabled: bool
    trusted_paths: list[str] = Field(default_factory=list)


class AutoApproveConfigResponse(BaseModel):
    enabled: bool
    trusted_paths: list[str]


# ── V1-aligned full auto-approve config (tool toggles + command lists) ────
# These power the new top-level GET/PUT /auto_approve endpoints (different
# path from the locked /auto_approve/config above; nothing on /config is
# touched). Stored under a separate `auto_approve_tool` settings bucket so
# enabled/trusted_paths in the legacy `/config` bucket stays isolated.
class AutoApproveToolToggles(BaseModel):
    read: bool = False
    write: bool = False
    exec: bool = False
    glob: bool = False
    grep: bool = False


class CommandListConfig(BaseModel):
    enabled: bool = False
    prefixes: list[str] = Field(default_factory=list)


class AutoApproveFullRequest(BaseModel):
    auto_approve: AutoApproveToolToggles = Field(default_factory=AutoApproveToolToggles)
    command_whitelist: CommandListConfig = Field(default_factory=CommandListConfig)
    # V1: command blacklist defaults to ENABLED (safety default).
    command_blacklist: CommandListConfig = Field(
        default_factory=lambda: CommandListConfig(enabled=True, prefixes=[])
    )


class AutoApproveFullResponse(BaseModel):
    auto_approve: AutoApproveToolToggles
    command_whitelist: CommandListConfig
    command_blacklist: CommandListConfig


# ── Path-pattern config (read/write allow-pattern glob allowlists) ──────────
# `read_allow_patterns` / `write_allow_patterns` drive the auto-approve
# path-pattern allowlist (`RuntimeStateAutoApproveAdapter`). On PUT they are
# partial-update (None = preserve existing bucket value).
class PatternConfig(BaseModel):
    enabled: bool = False
    patterns: list[str] = Field(default_factory=list)


class PathPatternsRequest(BaseModel):
    # Optional → partial update (None = preserve existing bucket value).
    read_allow_patterns: PatternConfig | None = None
    write_allow_patterns: PatternConfig | None = None


class PathPatternsResponse(BaseModel):
    read_allow_patterns: PatternConfig = Field(default_factory=PatternConfig)
    write_allow_patterns: PatternConfig = Field(default_factory=PatternConfig)


# ── Dangerous-command custom patterns (P-10) ─────────────────────────────
# The security domain owns an immutable built-in dangerous-command floor
# (``BUILTIN_DANGEROUS_COMMAND_PATTERNS`` — 9 non-removable regexes) plus a
# UNION-ONLY runtime override layer (operator-supplied ``extra`` patterns that
# can only ADD coverage, never delete a floor entry — red line §9.2.4). These
# DTOs surface that split: GET returns the read-only ``builtin`` floor plus the
# editable ``extra`` list; PUT accepts only ``extra`` (there is deliberately NO
# field that can write / delete the floor). The extra patterns are baked into
# the FileBroker guard closure at ``build_file_broker`` time, so a PUT needs a
# reboot to take effect (same nature as ``file_broker_enabled``); the response
# carries ``needs_reboot=True`` to drive the frontend reboot banner.
class DangerousCommandPatternsResponse(BaseModel):
    """``GET /api/security/dangerous-command-patterns`` payload.

    ``builtin`` is the read-only immutable floor (regex source strings,
    projected from ``BUILTIN_DANGEROUS_COMMAND_PATTERNS``). ``extra`` is the
    operator-editable union-only override list.
    """

    builtin: list[str] = Field(default_factory=list)
    extra: list[str] = Field(default_factory=list)


class DangerousCommandPatternsRequest(BaseModel):
    """``PUT /api/security/dangerous-command-patterns`` body.

    Only ``extra`` is writable — there is NO field to modify or delete the
    built-in floor (union-only guarantee, red line §9.2.4).
    """

    extra: list[str] = Field(default_factory=list)


class DangerousCommandPatternsUpdateResponse(DangerousCommandPatternsResponse):
    """PUT response: the merged view + reboot signal + rejected patterns.

    ``needs_reboot`` is always True on a successful write because the extra
    patterns are baked into the FileBroker guard closure at build time.
    ``invalid`` lists any submitted regex strings that failed to compile (they
    are dropped, never persisted — a bad operator regex can never open the box
    nor 500 the endpoint).
    """

    needs_reboot: bool = False
    invalid: list[str] = Field(default_factory=list)


class ProjectAccessRequest(BaseModel):
    enabled: bool
    # Optional → partial update.
    path: str | None = None


class ProjectAccessResponse(BaseModel):
    enabled: bool
    path: str = ""


class SkillPolicyResponse(BaseModel):
    skill_name: str
    capability_name: str
    read_paths: list[str]
    write_paths: list[str]
    exec_paths: list[str]
    trusted_binaries: list[str]
    description: str
    # V1-aligned tail-appended fields (v2.7 §3.1).
    # The Security/Skill panel surfaces user-overridden raw lists
    # separately from the effective lists so operators can edit only
    # their overrides without losing the capability defaults.
    raw_read: list[str] = Field(default_factory=list)
    raw_write: list[str] = Field(default_factory=list)
    raw_trusted_binaries: list[str] = Field(default_factory=list)
    has_policy: bool = False
    active: bool = True
    source: str = "skills"  # "features" | "skills"


class SkillPolicyUpdateRequest(BaseModel):
    """Body for ``PUT /api/security/skill_policy/{skill_name}``.

    Mirrors the legacy ``SecurityConfigPanel.js:saveSkillPolicy`` payload —
    user-overridden ``read`` / ``write`` / ``trusted_binaries`` lists
    persist as a per-skill override on top of the capability defaults.
    """

    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)
    trusted_binaries: list[str] = Field(default_factory=list)


class _ChannelQuotaDTO(BaseModel):
    window_seconds: int = Field(..., gt=0)
    max_asks: int = Field(..., gt=0)


class ChannelPolicyUpdateRequest(BaseModel):
    requires_ui: bool = True
    description: str = ""
    quota: _ChannelQuotaDTO | None = None


class ChannelPolicyResponse(BaseModel):
    name: str
    requires_ui: bool
    description: str
    quota: dict[str, int] | None


class ChannelsListResponse(BaseModel):
    channels: list[dict[str, Any]]


class SkillsListResponse(BaseModel):
    skills: list[dict[str, Any]]


class SkillRegisterRequest(BaseModel):
    skill_name: str = Field(..., min_length=1, max_length=256)
    capability_name: str = Field(..., min_length=1, max_length=256)
    read_paths: list[str] = Field(default_factory=list)
    write_paths: list[str] = Field(default_factory=list)
    exec_paths: list[str] = Field(default_factory=list)
    trusted_binaries: list[str] = Field(default_factory=list)
    description: str = ""
    skill_body: str = ""


class SkillRegisterResponse(BaseModel):
    skill_name: str
    audit_id: str
    scanner_warnings: list[str]


# ---------------------------------------------------------------------------
# Domain ⇄ DTO mappers (private helpers)
# ---------------------------------------------------------------------------


def _subject_from_dto(dto: _SubjectDTO) -> Subject:
    return Subject(kind=dto.kind, identifier=dto.identifier)


def _subject_to_dto(s: Subject) -> _SubjectDTO:
    return _SubjectDTO(kind=s.kind, identifier=s.identifier)  # type: ignore[arg-type]


def _resource_from_dto(dto: _ResourceDTO) -> Resource:
    return Resource(kind=dto.kind, identifier=dto.identifier)


def _resource_to_dto(r: Resource) -> _ResourceDTO:
    return _ResourceDTO(kind=r.kind, identifier=r.identifier)  # type: ignore[arg-type]


def _mask_from_dto(dto: _AceMaskDTO) -> AceMask:
    return AceMask(
        read=dto.read,
        write=dto.write,
        execute=dto.execute,
        delete=dto.delete,
    )


def _mask_to_dto(m: AceMask) -> _AceMaskDTO:
    return _AceMaskDTO(
        read=m.read,
        write=m.write,
        execute=m.execute,
        delete=m.delete,
    )


def _rule_from_dto(dto: _PolicyRuleDTO) -> PolicyRule:
    op = PolicyOp(dto.op)
    # exec_deny rules are regexes (V1 exec_deny_patterns); every other op
    # is glob. Deriving match_kind from op keeps the wire shape minimal.
    match_kind = (
        PolicyMatchKind.REGEX
        if op is PolicyOp.EXEC_DENY
        else PolicyMatchKind.GLOB
    )
    return PolicyRule(
        rule_id=dto.rule_id,
        scope=PolicyScope(dto.scope),
        pattern=PathPattern(
            pattern=dto.pattern,
            case_sensitive=dto.case_sensitive,
            match_kind=match_kind,
        ),
        action=PolicyAction(dto.action),
        description=dto.description,
        op=op,
    )


def _rule_to_dto(rule: PolicyRule) -> _PolicyRuleDTO:
    return _PolicyRuleDTO(
        rule_id=rule.rule_id,
        scope=rule.scope.value,  # type: ignore[arg-type]
        pattern=rule.pattern.pattern,
        case_sensitive=rule.pattern.case_sensitive,
        action=rule.action.value,  # type: ignore[arg-type]
        description=rule.description,
        op=rule.op.value,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Overview operational toggles — backed by SecurityRuntimeStateService.
#
# These four fields tail-append onto PolicyResponse (v2.7 §3.1) so the
# Overview tab can render the FileGuard status card. They live OUTSIDE
# the rules-based Policy aggregate (which stays pure): ``enabled`` and
# ``dynamic_authorization`` come from the runtime-state service that
# already drives ``/sandbox/status``; ``run_mode`` (enforce|audit_only)
# and ``no_ui_channels`` live in a dedicated ``policy_overview`` settings
# bucket. V1's ``mode`` is enforce|audit_only — orthogonal to the
# sandbox runtime mode (enforcing|permissive|disabled), so we keep them
# in separate keys to avoid conflation.
# ---------------------------------------------------------------------------

_POLICY_OVERVIEW_KEY = "policy_overview"
_DEFAULT_NO_UI_CHANNELS: tuple[str, ...] = ("wechat", "feishu")


def _read_overview_state(container: "Container") -> tuple[
    bool, str, bool, list[str]
]:
    """Return ``(enabled, run_mode, dynamic_authorization, no_ui_channels)``.

    All four operational toggles live in the ``policy_overview`` settings
    bucket so they are independent of the sandbox-launcher ``enabled``
    flag (which gates AppContainer isolation, a different concern). This
    matches V1 semantics where the FileGuard *master switch* defaults ON
    regardless of whether the OS-level sandbox launcher is present.

    Defaults mirror V1: master switch ON, ``enforce`` run mode, dynamic
    authorization ON, both IM channels gated off the WebUI dialog
    (``no_ui_channels`` = wechat + feishu). ``dynamic_authorization``
    falls back to the sandbox runtime snapshot when no override has been
    persisted, preserving consistency with ``/sandbox/status``.
    """
    snap = container.security.security_runtime_state.snapshot()
    bucket = (
        container.security.security_runtime_state.get_settings(
            _POLICY_OVERVIEW_KEY
        )
        or {}
    )
    enabled = bucket.get("enabled")
    if not isinstance(enabled, bool):
        enabled = True
    run_mode = str(bucket.get("run_mode", "enforce"))
    if run_mode not in ("enforce", "audit_only"):
        run_mode = "enforce"
    no_ui = bucket.get("no_ui_channels")
    if not isinstance(no_ui, list):
        no_ui = list(_DEFAULT_NO_UI_CHANNELS)
    # ``dynamic_authorization`` defaults to the runtime snapshot but an
    # operator override persisted via PUT (bucket key) takes precedence,
    # since the runtime service exposes the flag read-only.
    dyn_auth = bucket.get("dynamic_authorization")
    if not isinstance(dyn_auth, bool):
        dyn_auth = snap.dynamic_authorization
    return enabled, run_mode, dyn_auth, list(no_ui)


def _persist_overview_state(
    container: "Container",
    *,
    enabled: bool | None,
    mode: str | None,
    dynamic_authorization: bool | None,
    no_ui_channels: list[str] | None,
) -> None:
    """Persist any present Overview toggle (partial update; None = skip).

    All four toggles live in the ``policy_overview`` bucket. None-valued
    fields are skipped so a legacy rules-only PUT leaves the toggles
    untouched (byte-for-byte pre-existing behaviour).
    """
    state = container.security.security_runtime_state
    current = state.get_settings(_POLICY_OVERVIEW_KEY) or {}
    bucket: dict[str, Any] = dict(current)
    if enabled is not None:
        bucket["enabled"] = enabled
    if mode is not None:
        bucket["run_mode"] = mode
    if dynamic_authorization is not None:
        bucket["dynamic_authorization"] = dynamic_authorization
    if no_ui_channels is not None:
        bucket["no_ui_channels"] = list(no_ui_channels)
    if bucket != current:
        state.update_settings(_POLICY_OVERVIEW_KEY, bucket)


def _policy_to_response(
    p: Policy,
    *,
    container: "Container | None" = None,
    needs_reboot: bool = False,
) -> PolicyResponse:
    if container is None:
        # Defaults keep the response shape valid for callers that don't
        # need the overview toggles (none in production; defensive).
        enabled, run_mode, dyn_auth, no_ui = (
            True,
            "enforce",
            True,
            list(_DEFAULT_NO_UI_CHANNELS),
        )
    else:
        enabled, run_mode, dyn_auth, no_ui = _read_overview_state(container)
    return PolicyResponse(
        version=p.version,
        updated_at=p.updated_at.isoformat(),
        rules=[_rule_to_dto(r) for r in p.rules],
        enabled=enabled,
        mode=run_mode,  # type: ignore[arg-type]
        dynamic_authorization=dyn_auth,
        no_ui_channels=no_ui,
        needs_reboot=needs_reboot,
    )


# ---------------------------------------------------------------------------
# Built-in policy templates (V1 parity: demo / development / strict).
# Template ids match V1 config/policy_templates/{id}.json filenames.
# The rule-set data + construction + apply orchestration now live in
# ``qai.security.application.use_cases.security_templates`` (R7 cohesion
# fix); the ``GET /api/security/templates`` catalogue ``rules_count``
# (2 / 4 / 3) below mirrors the rule counts there.
# ---------------------------------------------------------------------------


def _request_to_dto(r: PermissionRequest) -> _PermissionRequestDTO:
    return _PermissionRequestDTO(
        request_id=r.request_id.value,
        subject=_subject_to_dto(r.subject),
        resource=_resource_to_dto(r.resource),
        requested_mask=_mask_to_dto(r.requested_mask),
        state=r.state.value,  # type: ignore[arg-type]
        created_at=r.created_at.isoformat(),
        resolved_at=r.resolved_at.isoformat() if r.resolved_at else None,
        resolution_reason=r.resolution_reason,
        is_native_subprocess=_is_native_subprocess(r.subject),
    )


def _grant_to_dto(g: PathGrant) -> _PathGrantDTO:
    return _PathGrantDTO(
        grant_id=g.grant_id,
        subject=_subject_to_dto(g.subject),
        path=g.path,
        mask=_mask_to_dto(g.mask),
        source=g.source.value,  # type: ignore[arg-type]
        created_at=g.created_at.isoformat(),
        expires_at=g.expires_at.isoformat() if g.expires_at else None,
    )


def _audit_to_dto(a: AuditEntry) -> _AuditEntryDTO:
    return _AuditEntryDTO(
        audit_id=a.audit_id,
        occurred_at=a.occurred_at.isoformat(),
        subject=_subject_to_dto(a.subject),
        resource=_resource_to_dto(a.resource),
        decision=a.decision.value,  # type: ignore[arg-type]
        rule_id=a.rule_id,
        correlation_id=a.correlation_id,
        note=a.note,
        channel=a.channel,
        op=a.op,
        process_path=a.process_path,
        command_line=a.command_line,
        actor_pid=a.actor_pid,
        actor_parent_pid=a.actor_parent_pid,
    )


# ---------------------------------------------------------------------------
# Skill policy helpers (R9 cohesion fix).
# ---------------------------------------------------------------------------
#
# The skill classification / override-bucket I/O / merge-dedup /
# three-way discovery aggregation now live in
# ``qai.security.application.use_cases.skill_discovery``. The route module
# re-uses those rather than re-defining its own copies; the only thing
# left here is the projection of the application-layer view onto the wire
# DTO and the thin bucket-read/write delegations the read/write handlers
# call.


def _read_skill_policy_overrides(
    container: "Container",
) -> dict[str, dict[str, list[str]]]:
    """Delegate to the application layer's override-bucket reader."""
    return _skill_discovery.read_skill_policy_overrides(
        container.security.security_runtime_state
    )


def _skill_policy_view_to_dto(
    view: "_skill_discovery.SkillPolicyView",
) -> "SkillPolicyResponse":
    """Project an application-layer :class:`SkillPolicyView` onto the DTO."""
    return SkillPolicyResponse(
        skill_name=view.skill_name,
        capability_name=view.capability_name,
        read_paths=view.read_paths,
        write_paths=view.write_paths,
        exec_paths=view.exec_paths,
        trusted_binaries=view.trusted_binaries,
        description=view.description,
        raw_read=view.raw_read,
        raw_write=view.raw_write,
        raw_trusted_binaries=view.raw_trusted_binaries,
        has_policy=view.has_policy,
        active=view.active,
        source=view.source,
    )

def _parse_iso_or_validation_error(
    raw: str, *, field_name: str
) -> datetime:
    """Parse ``raw`` as an ISO-8601 datetime, raising :class:`ValidationError`.

    Pydantic's ``datetime`` field type accepts more shapes than we want
    (e.g. unix timestamps); using a string + this helper keeps the wire
    contract narrow and the error message uniform across endpoints.
    """
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "security.invalid_datetime",
            f"{field_name} is not a valid ISO-8601 datetime",
            field_errors={field_name: [str(exc)]},
        ) from exc
    if parsed.tzinfo is None:
        raise ValidationError(
            "security.naive_datetime",
            f"{field_name} must include a timezone offset",
            field_errors={field_name: ["missing timezone"]},
        )
    return parsed


def _make_request_id(value: str) -> RequestId:
    """Wrap a path-param string in :class:`RequestId`, raising :class:`ValidationError`."""
    try:
        return RequestId(value=value)
    except ValueError as exc:
        raise ValidationError(
            "security.invalid_request_id",
            f"request_id is invalid: {exc}",
            field_errors={"request_id": [str(exc)]},
        ) from exc


# ---------------------------------------------------------------------------
# Schema-name stability (split from security.py)
# ---------------------------------------------------------------------------
# Security master-switch (mode) DTOs — 3c switch-tree §6.4 backend.
#
# These back the NEW ``PUT /api/security/mode`` endpoint. ``mode`` here is
# the master-switch SEMANTIC control (enforcing | permissive | disabled),
# which is ORTHOGONAL to — and MUST NOT be confused with — the locked
# ``PolicyResponse.mode`` / ``UpdatePolicyRequest.mode`` field whose value
# domain stays (enforce | audit_only). The master switch is folded into an
# effective run-mode by ``SecurityRuntimeStateService.effective_run_mode``
# and consumed through the single existing ``audit_only`` override path.
# ---------------------------------------------------------------------------


class SetSecurityModeRequest(BaseModel):
    """``PUT /api/security/mode`` body.

    ``mode`` is the security master switch. ``Literal`` validation rejects
    any out-of-domain value with a 422 automatically.
    """

    mode: Literal["enforcing", "permissive", "disabled"]


class SecurityModeResponse(BaseModel):
    """``PUT /api/security/mode`` payload.

    Surfaces the master-switch ``mode``, its derived ``enabled`` master
    flag (single truth source = ``policy_overview`` bucket) and the
    ``effective_run_mode`` the decision core will honour, so the Overview
    tab can render the master switch plus its derived state without a
    second round-trip.
    """

    mode: Literal["enforcing", "permissive", "disabled"]
    enabled: bool
    effective_run_mode: Literal["enforce", "audit_only"]


# ---------------------------------------------------------------------------
# Pydantic/FastAPI derive a model's OpenAPI $defs key from __name__,
# falling back to the fully module-qualified name on cross-module name
# collisions (e.g. this context's PermissionRequestResponse collides with
# interfaces.http.routes.ai_coding._dto.PermissionRequestResponse). Before
# this module was split out of security.py every DTO reported
# __module__ == "interfaces.http.routes.security"; pin it back to that so
# the emitted schema names -- and therefore the OpenAPI snapshot SHA --
# stay byte-for-byte identical to the pre-split monolith.
_SECURITY_ROUTE_MODULE = "interfaces.http.routes.security"
_security_dtos = [
    _obj
    for _obj in globals().values()
    if (
        isinstance(_obj, type)
        and issubclass(_obj, BaseModel)
        and _obj.__module__ == __name__
    )
]
# Pass 1: retarget every local DTO's reported module to the package path.
for _obj in _security_dtos:
    _obj.__module__ = _SECURITY_ROUTE_MODULE
# Pass 2: rebuild so the updated __module__ (carried in the frozen
# pydantic-core ref) is reflected in the OpenAPI $defs key. Done as a
# second pass so nested DTO references resolve against the already-
# retargeted child models.
for _obj in _security_dtos:
    _obj.model_rebuild(force=True)
del _obj, _security_dtos
