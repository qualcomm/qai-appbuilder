# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Entities and aggregate roots for the security bounded context.

Following the style established in ``qai.platform`` and the S2 spec, each
entity is a frozen dataclass with explicit factory / state-transition
methods that return *new* instances rather than mutating in place.

Aggregates defined here:

* :class:`PolicyRule`        — one row of a Policy.
* :class:`Policy`            — aggregate root: a collection of PolicyRule
  values plus the audit metadata (version + last-updated timestamp).
* :class:`PathGrant`         — aggregate root: a single persistent ACL
  entry granting an :class:`AceMask` over a path.
* :class:`PermissionRequest` — aggregate root: an in-flight or resolved
  approval workflow item.
* :class:`AuditEntry`        — append-only record of a single security
  decision.

State transitions (e.g. :meth:`PermissionRequest.approve`) raise the
appropriate domain error from :mod:`qai.security.domain.errors` when
called from an illegal state; callers should catch and translate as
needed at the application layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime

from qai.platform.io_validator import (
    assert_max_length,
    assert_no_control_chars,
    assert_non_empty,
)

from .errors import (
    PermissionRequestAlreadyResolvedError,
    PolicyRuleConflictError,
    SecurityPolicyInvalidError,
)
from .value_objects import (
    AceMask,
    AskQuotaWindow,
    Channel,
    GrantSource,
    PathPattern,
    PolicyAction,
    PolicyOp,
    PolicyScope,
    RequestId,
    RequestState,
    Resource,
    Subject,
)

__all__ = [
    "AuditEntry",
    "ChannelPolicy",
    "PathGrant",
    "PermissionRequest",
    "Policy",
    "PolicyRule",
    "PolicyShadowWarning",
]


# ---------------------------------------------------------------------------
# PolicyRule
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyRule:
    """A single rule inside a :class:`Policy`.

    Equality is by all fields (frozen dataclass). The :attr:`rule_id` field
    is the stable identity inside a Policy; two rules with the same id but
    different content are considered conflicting and rejected during
    Policy construction.
    """

    rule_id: str
    scope: PolicyScope
    pattern: PathPattern
    action: PolicyAction
    description: str = ""
    op: PolicyOp = PolicyOp.ANY

    def __post_init__(self) -> None:
        assert_non_empty(self.rule_id, name="rule_id")
        assert_max_length(self.rule_id, max_length=128, name="rule_id")
        assert_no_control_chars(self.rule_id, name="rule_id")
        if self.description:
            assert_max_length(
                self.description, max_length=1024, name="description"
            )
            assert_no_control_chars(self.description, name="description")

    def matches(self, path: str) -> bool:
        """Return True iff this rule's pattern matches ``path``.

        Operation-agnostic — only the path/command pattern is consulted.
        Callers that also need to filter by the requested operation use
        :meth:`matches_request` (or :meth:`PolicyOp.covers_request`).
        """
        return self.pattern.matches(path)

    def matches_request(
        self, path: str, *, read: bool, write: bool, execute: bool
    ) -> bool:
        """Return True iff the rule matches both the path AND the op.

        Combines the pattern match (glob or regex per
        :attr:`PathPattern.match_kind`) with the operation filter
        (:meth:`PolicyOp.covers_request`). A rule whose ``op`` is
        ``ANY`` (the default / backward-compatible value) matches any
        operation, preserving pre-``op`` evaluation behaviour.
        """
        if not self.op.covers_request(read=read, write=write, execute=execute):
            return False
        return self.pattern.matches(path)


# ---------------------------------------------------------------------------
# Policy aggregate
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class Policy:
    """Aggregate root: an ordered, deduplicated collection of rules.

    Construction validates that:

    * rule ids are unique;
    * no two rules have the same ``(scope, pattern, action)`` triple
      under different ids (would be confusing);
    * no two rules with the same ``(scope, pattern)`` disagree on
      ``action`` (would be a contradiction).

    The :attr:`version` field is a monotonically increasing integer used
    by the application layer to decide whether a policy reload requires
    triggering the reboot signal (REBOOT_EXIT_CODE = 75 contract — see
    refactor-plan §8.11).
    """

    version: int
    updated_at: datetime
    rules: tuple[PolicyRule, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not isinstance(self.version, int) or isinstance(self.version, bool):
            raise SecurityPolicyInvalidError(
                f"version must be int, got {type(self.version).__name__}"
            )
        if self.version < 0:
            raise SecurityPolicyInvalidError(
                f"version must be >= 0, got {self.version!r}"
            )
        if not isinstance(self.rules, tuple):
            raise SecurityPolicyInvalidError(
                f"rules must be a tuple, got {type(self.rules).__name__}"
            )
        seen_ids: set[str] = set()
        seen_keys: dict[tuple[PolicyScope, str, bool, PolicyOp], PolicyAction] = {}
        for rule in self.rules:
            if not isinstance(rule, PolicyRule):
                raise SecurityPolicyInvalidError(
                    "every entry in rules must be a PolicyRule, "
                    f"got {type(rule).__name__}"
                )
            if rule.rule_id in seen_ids:
                raise SecurityPolicyInvalidError(
                    f"duplicate rule_id: {rule.rule_id!r}"
                )
            seen_ids.add(rule.rule_id)
            key = (
                rule.scope,
                rule.pattern.pattern,
                rule.pattern.case_sensitive,
                rule.op,
            )
            existing = seen_keys.get(key)
            if existing is not None and existing is not rule.action:
                raise PolicyRuleConflictError(
                    "contradictory rules for "
                    f"scope={rule.scope.value}, pattern={rule.pattern.pattern!r}, "
                    f"op={rule.op.value}",
                    details={
                        "scope": rule.scope.value,
                        "pattern": rule.pattern.pattern,
                        "op": rule.op.value,
                        "actions": sorted(
                            {existing.value, rule.action.value}
                        ),
                    },
                )
            seen_keys[key] = rule.action

    # ── factory / state transitions ─────────────────────────────────────
    @classmethod
    def empty(cls, *, now: datetime) -> Policy:
        """Return a fresh Policy with no rules at version 0."""
        return cls(version=0, updated_at=now, rules=())

    def with_rule(self, rule: PolicyRule, *, now: datetime) -> Policy:
        """Return a copy of this policy with ``rule`` appended.

        The version is bumped by 1 and ``updated_at`` is replaced.
        Validation runs through ``__post_init__`` so duplicates / conflicts
        raise immediately.
        """
        return replace(
            self,
            version=self.version + 1,
            updated_at=now,
            rules=self.rules + (rule,),
        )

    def without_rule(self, rule_id: str, *, now: datetime) -> Policy:
        """Return a copy with the rule of given id removed.

        If no rule with that id exists, the policy is returned unchanged
        but with the version still bumped — callers expecting a not-found
        signal should look it up first via :meth:`find_rule`.
        """
        new_rules = tuple(r for r in self.rules if r.rule_id != rule_id)
        return replace(
            self,
            version=self.version + 1,
            updated_at=now,
            rules=new_rules,
        )

    def find_rule(self, rule_id: str) -> PolicyRule | None:
        """Return the rule with this id or ``None`` if none."""
        for rule in self.rules:
            if rule.rule_id == rule_id:
                return rule
        return None

    def evaluate(self, path: str) -> PolicyAction | None:
        """Return the first matching rule's action (operation-agnostic).

        Rules are evaluated in declaration order; the first match wins.
        Returns ``None`` if no rule matches. This path-only variant is
        retained for callers that don't carry an operation context (and
        for shadow detection); the operation-aware
        :meth:`evaluate_request` is what the ``check_permission`` hot
        path uses.
        """
        for rule in self.rules:
            if rule.matches(path):
                return rule.action
        return None

    def evaluate_request(
        self, path: str, *, read: bool, write: bool, execute: bool
    ) -> tuple[PolicyAction, str] | None:
        """Return ``(action, rule_id)`` of the first op-relevant match.

        Two-phase, mirroring the V1 ``PolicyCenter`` precedence:

        1. **exec-deny gate first** — any ``op=EXEC_DENY`` rule whose
           (regex) pattern matches the command short-circuits to DENY
           before anything else, exactly like V1 ``exec_deny_patterns``
           being the first gate. This only applies when the request
           actually carries the execute bit.
        2. **first declaration-order match wins** — among the remaining
           rules, the first whose ``op`` is relevant to the requested
           operation (:meth:`PolicyOp.covers_request`) AND whose pattern
           matches returns its action.

        ``op=ANY`` rules participate in phase 2 for every operation, so
        a policy made entirely of pre-``op`` (``ANY``) rules behaves
        exactly as the legacy path-only ``evaluate`` did.

        Returns ``None`` if no rule is relevant — the caller then falls
        back to grants / implicit-deny.
        """
        if execute:
            for rule in self.rules:
                if rule.op is PolicyOp.EXEC_DENY and rule.pattern.matches(path):
                    return (PolicyAction.DENY, rule.rule_id)
        for rule in self.rules:
            if rule.op is PolicyOp.EXEC_DENY:
                # Already handled above (deny gate); an exec_deny rule is
                # never an allow, so skip it in the positive pass.
                continue
            if rule.matches_request(
                path, read=read, write=write, execute=execute
            ):
                return (rule.action, rule.rule_id)
        return None

    def detect_shadows(self) -> tuple[PolicyShadowWarning, ...]:
        """Detect non-fatal pattern-shadow rule conflicts (PR-501).

        ``__post_init__`` already rejects exact ``(scope, pattern,
        case_sensitive)`` collisions with opposite actions; this method
        looks one level higher and reports overlaps where one rule's
        glob pattern logically contains another's at the same scope but
        the actions disagree. The legacy ``PolicyCenter`` (see
        ``backend/security/policy.py:_command_passes_lists`` and
        ``_path_matches_patterns``) silently let the first declaration
        win, which made conflicting "allow ``foo/*``" / "deny
        ``foo/secret``" rules impossible to spot in the UI; PR-501
        surfaces them as :class:`PolicyShadowWarning` records so the
        application layer can publish them on the event bus and the
        operator can review.

        The check is intentionally O(n²) over the rule set: policy
        rule counts are bounded by the legacy ``persistent_acl.json``
        scale (≤ ~8000 entries; see PR-026 §10.2) and this method runs
        on the cold path of a policy update — never on the
        ``check_permission`` hot path.

        Two rules ``A`` and ``B`` with the *same scope* shadow each
        other when:

        * their patterns differ but one contains the other (i.e.
          ``A.pattern`` matches everything ``B.pattern`` matches OR
          vice versa), AND
        * their actions disagree (one ALLOW, one DENY).

        Containment is computed via :meth:`PathPattern.matches` against
        the *literal* of the smaller pattern: if pattern ``X.matches(Y)``
        returns True for the literal text ``Y`` after stripping glob
        meta-characters, then ``X`` covers at least the singleton
        ``{Y}`` and is treated as a shadow. This is conservative — it
        will miss some genuinely overlapping glob ranges — but it is
        precise enough to flag the common cases found in the legacy
        config files without generating false positives.
        """
        warnings: list[PolicyShadowWarning] = []
        seen_pairs: set[tuple[str, str]] = set()
        for i, rule_a in enumerate(self.rules):
            for rule_b in self.rules[i + 1 :]:
                if rule_a.scope is not rule_b.scope:
                    continue
                if rule_a.action is rule_b.action:
                    continue
                if rule_a.pattern == rule_b.pattern:
                    # Exact equality with opposite action would already
                    # have been caught by __post_init__; defensively skip
                    continue  # pragma: no cover
                shadowed_id = _detect_shadow(rule_a, rule_b)
                if shadowed_id is None:
                    continue
                pair = tuple(sorted((rule_a.rule_id, rule_b.rule_id)))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                shadower = (
                    rule_b
                    if shadowed_id == rule_a.rule_id
                    else rule_a
                )
                shadowed = (
                    rule_a
                    if shadowed_id == rule_a.rule_id
                    else rule_b
                )
                warnings.append(
                    PolicyShadowWarning(
                        shadower_rule_id=shadower.rule_id,
                        shadowed_rule_id=shadowed.rule_id,
                        scope=shadowed.scope,
                        shadower_action=shadower.action,
                        shadowed_action=shadowed.action,
                        shadower_pattern=shadower.pattern.pattern,
                        shadowed_pattern=shadowed.pattern.pattern,
                    )
                )
        return tuple(warnings)


def _detect_shadow(a: PolicyRule, b: PolicyRule) -> str | None:
    """Return the rule_id whose pattern is shadowed, or ``None``.

    Uses :meth:`PathPattern.matches` against a "concrete witness" derived
    from the other pattern: if pattern ``X`` matches the literal text of
    pattern ``Y`` (after stripping glob meta-characters), then ``X``
    semantically covers ``{Y_literal}`` and hides any opposing decision
    keyed on ``Y``. ``rule_id`` of the *narrower* (covered) rule is
    returned so the warning's ``shadower``/``shadowed`` fields read
    naturally.
    """
    a_witness = _glob_witness(a.pattern.pattern)
    b_witness = _glob_witness(b.pattern.pattern)
    a_covers_b = a.pattern.matches(b_witness) and a.pattern.pattern != b.pattern.pattern
    b_covers_a = b.pattern.matches(a_witness) and a.pattern.pattern != b.pattern.pattern
    if a_covers_b and not b_covers_a:
        return b.rule_id  # b is the narrower / shadowed rule
    if b_covers_a and not a_covers_b:
        return a.rule_id
    return None


def _glob_witness(pattern: str) -> str:
    """Return a concrete literal that lies in the language of ``pattern``.

    ``*`` and ``?`` collapse to the empty string; ``[abc]`` collapses to
    the first listed char; anything else is kept verbatim. The result
    is a representative path that the *same* pattern matches by
    construction.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        ch = pattern[i]
        if ch == "*":
            i += 1
            continue
        if ch == "?":
            out.append("a")
            i += 1
            continue
        if ch == "[":
            close = pattern.find("]", i + 1)
            if close == -1:
                out.append(ch)
                i += 1
                continue
            inner = pattern[i + 1 : close]
            if inner.startswith("!"):
                inner = inner[1:]
            out.append(inner[:1] or "a")
            i = close + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------------------
# PolicyShadowWarning — PR-501
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class PolicyShadowWarning:
    """Non-fatal report of a pattern-shadow conflict between two rules.

    Produced by :meth:`Policy.detect_shadows`; the application layer
    publishes a :class:`qai.security.domain.events.PolicyShadowDetectedEvent`
    carrying these warnings after a successful Policy save so operators
    can review the policy graph in the UI / logs.

    A "shadow" means the ``shadower`` rule's pattern semantically covers
    every path the ``shadowed`` rule's pattern would match, but their
    actions disagree. Because ``Policy.evaluate`` is first-match-wins,
    only one of the two rules ever fires — the other is dead weight at
    best and a confusing source of bugs at worst.
    """

    shadower_rule_id: str
    shadowed_rule_id: str
    scope: PolicyScope
    shadower_action: PolicyAction
    shadowed_action: PolicyAction
    shadower_pattern: str
    shadowed_pattern: str

    def __post_init__(self) -> None:
        if self.shadower_rule_id == self.shadowed_rule_id:
            raise ValueError(
                "shadower_rule_id and shadowed_rule_id must differ"
            )
        if self.shadower_action is self.shadowed_action:
            raise ValueError(
                "shadow warning requires shadower_action != shadowed_action"
            )


# ---------------------------------------------------------------------------
# PathGrant aggregate
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class PathGrant:
    """A persistent ACL entry granting an AceMask over a path.

    This is the per-path counterpart of a :class:`Policy` rule: while
    rules are global and pattern-based, grants are concrete and tied to
    a single path string (typically already resolved). One subject may
    hold many grants.

    ``expires_at`` is optional; ``None`` means the grant is non-expiring.
    The aggregate exposes :meth:`is_expired` so callers don't have to
    decide whether to compare against ``Clock.now()``.
    """

    grant_id: str
    subject: Subject
    path: str
    mask: AceMask
    source: GrantSource
    created_at: datetime
    expires_at: datetime | None = None
    #: Scope of this grant (tail-appended, v2.7 §3.1 additive). Governs
    #: *where* the grant applies, complementing ``expires_at`` (*when*):
    #:   * ``permanent`` — applies everywhere, forever (the only scope that
    #:     seeds the native FileGuard persistent whitelist at startup). This
    #:     is the DEFAULT so legacy rows / callers that omit scope keep their
    #:     original process-global, non-expiring behaviour byte-for-byte.
    #:   * ``process``   — applies only within the current backend process;
    #:     ``scope_key`` holds the process boot id. A restart mints a new
    #:     boot id, so old process grants stop matching (真 process 隔离).
    #:   * ``session``   — applies only within one collaboration session;
    #:     ``scope_key`` holds the TOP-LEVEL conversation id. The main agent
    #:     and every sub-agent / participant of that session share it (one
    #:     collaboration session = one session), and it stops matching once
    #:     the caller is in a different conversation (真 session 隔离).
    #:   * ``once``      — never persisted as a grant (approve path handles it).
    scope_kind: str = "permanent"
    #: Scope discriminator paired with ``scope_kind`` (conversation id for
    #: ``session``; process boot id for ``process``; ``""`` for ``permanent``).
    scope_key: str = ""
    #: Directory-grant flag (tail-appended, §3.1 additive). When ``True`` the
    #: grant's ``path`` is a DIRECTORY the user explicitly authorized, and a
    #: resource whose path lies UNDER that directory (path-boundary prefix)
    #: matches — not just an exact string equal. DEFAULT ``False`` keeps legacy
    #: rows / callers at single-file exact-match semantics byte-for-byte. This
    #: is the "grant the whole directory" opt-in surfaced in the permission
    #: dialog (P-11B); it lets the matcher reuse the native directory-prefix
    #: path (``_grant_path_ancestor_of``) for in-process / exec subjects too,
    #: but ONLY when the user explicitly chose directory scope (no implicit
    #: privilege widening).
    is_directory: bool = False
    #: Program-grant flag (tail-appended, §3.1 additive). When ``True`` the
    #: grant's ``path`` is a normalized command BINARY token (e.g. ``powershell``
    #: / ``powershell.exe``) the user explicitly authorized for a ``kind="exec"``
    #: resource, and ANY exec command whose extracted binary equals it matches —
    #: not just the exact command string. DEFAULT ``False`` keeps legacy rows /
    #: callers at exact command-string match byte-for-byte. This is the
    #: "permanently allow this whole program" opt-in surfaced in the permission
    #: dialog for exec commands (the "program" grant_range): the user gets asked
    #: once for e.g. powershell and can choose to stop being asked for every
    #: powershell invocation. Mutually exclusive with ``is_directory`` (a grant
    #: is either a path/dir grant or an exec-program grant, never both).
    is_program: bool = False

    def __post_init__(self) -> None:
        assert_non_empty(self.grant_id, name="grant_id")
        assert_max_length(self.grant_id, max_length=128, name="grant_id")
        assert_no_control_chars(self.grant_id, name="grant_id")
        assert_non_empty(self.path, name="path")
        assert_max_length(self.path, max_length=4096, name="path")
        assert_no_control_chars(self.path, name="path")
        if self.mask.is_empty():
            raise ValueError("mask must have at least one bit set")
        if (
            self.expires_at is not None
            and self.expires_at <= self.created_at
        ):
            raise ValueError(
                f"expires_at ({self.expires_at!r}) must be strictly "
                f"after created_at ({self.created_at!r})"
            )
        if self.scope_kind not in ("once", "session", "process", "permanent"):
            raise ValueError(
                f"scope_kind must be one of once/session/process/permanent, "
                f"got {self.scope_kind!r}"
            )
        if self.scope_key:
            assert_max_length(
                self.scope_key, max_length=128, name="scope_key"
            )
            assert_no_control_chars(self.scope_key, name="scope_key")

    def is_expired(self, *, now: datetime) -> bool:
        """Return True iff ``expires_at`` is set and lies before ``now``."""
        if self.expires_at is None:
            return False
        return now >= self.expires_at

    def matches_scope(self, *, boot_id: str, conversation_id: str) -> bool:
        """Return True iff this grant applies in the CURRENT context.

        Complements :meth:`is_expired` (which is the time dimension); this is
        the *place* dimension:

        * ``permanent`` — always applies.
        * ``process``   — applies only when ``scope_key`` equals the current
          process ``boot_id``.
        * ``session``   — applies only when ``scope_key`` equals the current
          top-level ``conversation_id`` (shared by main agent + sub-agents /
          participants of that collaboration session).
        * ``once``      — never applies as a persisted grant.

        Unknown/empty context (``boot_id``/``conversation_id`` == ``""``)
        never matches a process/session grant, so a caller that cannot supply
        scope context only ever sees ``permanent`` grants (fail-safe).
        """
        if self.scope_kind == "permanent":
            return True
        if self.scope_kind == "process":
            return bool(boot_id) and self.scope_key == boot_id
        if self.scope_kind == "session":
            return bool(conversation_id) and self.scope_key == conversation_id
        # "once" (or any unexpected value) is never an active persisted grant.
        return False

    def covers(self, mask: AceMask) -> bool:
        """Return True iff this grant's mask covers ``mask``."""
        return self.mask.covers(mask)


# ---------------------------------------------------------------------------
# PermissionRequest aggregate
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class PermissionRequest:
    """A pending or resolved approval workflow item.

    A request is created in :attr:`RequestState.PENDING` and may
    transition exactly once to :attr:`APPROVED` / :attr:`REJECTED` /
    :attr:`CANCELLED` / :attr:`EXPIRED`. The transition methods raise
    :class:`PermissionRequestAlreadyResolvedError` on repeated attempts.

    ``resolved_at`` is filled in by the transition method and stored
    alongside the new state.
    """

    request_id: RequestId
    subject: Subject
    resource: Resource
    requested_mask: AceMask
    state: RequestState
    created_at: datetime
    resolved_at: datetime | None = None
    resolution_reason: str = ""

    def __post_init__(self) -> None:
        if self.requested_mask.is_empty():
            raise ValueError("requested_mask must have at least one bit set")
        if self.resolution_reason:
            assert_max_length(
                self.resolution_reason,
                max_length=2048,
                name="resolution_reason",
            )
            assert_no_control_chars(
                self.resolution_reason, name="resolution_reason"
            )
        if self.state is RequestState.PENDING and self.resolved_at is not None:
            raise ValueError(
                "resolved_at must be None when state is PENDING"
            )
        if (
            self.state is not RequestState.PENDING
            and self.resolved_at is None
        ):
            raise ValueError(
                "resolved_at must be set when state is not PENDING"
            )

    @classmethod
    def create(
        cls,
        *,
        request_id: RequestId,
        subject: Subject,
        resource: Resource,
        requested_mask: AceMask,
        now: datetime,
    ) -> PermissionRequest:
        """Construct a fresh PENDING request."""
        return cls(
            request_id=request_id,
            subject=subject,
            resource=resource,
            requested_mask=requested_mask,
            state=RequestState.PENDING,
            created_at=now,
        )

    def _ensure_pending(self, transition: str) -> None:
        if self.state is not RequestState.PENDING:
            raise PermissionRequestAlreadyResolvedError(
                f"cannot {transition}: request is already in state "
                f"{self.state.value!r}"
            )

    def approve(
        self, *, now: datetime, reason: str = ""
    ) -> PermissionRequest:
        """Transition PENDING → APPROVED."""
        self._ensure_pending("approve")
        return replace(
            self,
            state=RequestState.APPROVED,
            resolved_at=now,
            resolution_reason=reason,
        )

    def reject(
        self, *, now: datetime, reason: str = ""
    ) -> PermissionRequest:
        """Transition PENDING → REJECTED."""
        self._ensure_pending("reject")
        return replace(
            self,
            state=RequestState.REJECTED,
            resolved_at=now,
            resolution_reason=reason,
        )

    def cancel(self, *, now: datetime) -> PermissionRequest:
        """Transition PENDING → CANCELLED (subject withdrew)."""
        self._ensure_pending("cancel")
        return replace(
            self,
            state=RequestState.CANCELLED,
            resolved_at=now,
        )

    def expire(self, *, now: datetime) -> PermissionRequest:
        """Transition PENDING → EXPIRED (TTL elapsed)."""
        self._ensure_pending("expire")
        return replace(
            self,
            state=RequestState.EXPIRED,
            resolved_at=now,
        )

    @property
    def is_pending(self) -> bool:
        return self.state is RequestState.PENDING

    @property
    def is_resolved(self) -> bool:
        return self.state is not RequestState.PENDING


# ---------------------------------------------------------------------------
# AuditEntry
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class AuditEntry:
    """Append-only record of a security decision.

    AuditEntries are *facts* — they are never edited or removed once
    persisted. The application layer constructs them and pushes them
    through :class:`qai.security.application.ports.AuditSinkPort`.
    """

    audit_id: str
    occurred_at: datetime
    subject: Subject
    resource: Resource
    decision: PolicyAction
    rule_id: str | None
    correlation_id: str | None = None
    note: str = ""
    #: Origin channel of the request that triggered this decision
    #: (V1 parity: web / wechat / feishu / cli / background). ``None`` for
    #: internal/system actions that have no originating channel. Kept as a
    #: free ``str`` (not the ``Channel`` VO) so the audit trail can also
    #: record historical/foreign channel names without a domain coupling.
    channel: str | None = None
    #: Operation kind that triggered this decision (``read`` / ``write`` /
    #: ``delete`` / ``exec``). Distinguishes a delete from a plain write in
    #: the audit trail even though both are evaluated against the write
    #: permission bit. ``""`` = unlabelled (legacy rows / callers that do not
    #: classify the op). Tail-appended (v2.7 §3.1 additive).
    op: str = ""
    #: Real image path of the process that triggered a NATIVE sub-process
    #: file event (``guard64.dll`` V2 events carry it; in-process tool events
    #: leave it ``""``). Lets the audit trail attribute a sub-process write to
    #: the concrete executable. Tail-appended.
    process_path: str = ""
    #: Command line of the triggering sub-process (native V2 events; ``""``
    #: for in-process tool events or when the DLL could not resolve it and no
    #: pid→cmdline fallback was available). Tail-appended.
    command_line: str = ""
    #: PID of the process that triggered a native sub-process event (``None``
    #: for in-process tool events). Named ``actor_pid`` to avoid any confusion
    #: with rule/audit identifiers. Tail-appended.
    actor_pid: int | None = None
    #: Parent PID of the triggering sub-process (native events; ``None``
    #: otherwise). Tail-appended.
    actor_parent_pid: int | None = None

    def __post_init__(self) -> None:
        assert_non_empty(self.audit_id, name="audit_id")
        assert_max_length(self.audit_id, max_length=128, name="audit_id")
        assert_no_control_chars(self.audit_id, name="audit_id")
        if self.rule_id is not None:
            assert_non_empty(self.rule_id, name="rule_id")
            assert_max_length(self.rule_id, max_length=128, name="rule_id")
            assert_no_control_chars(self.rule_id, name="rule_id")
        if self.correlation_id is not None:
            assert_non_empty(self.correlation_id, name="correlation_id")
            assert_max_length(
                self.correlation_id, max_length=128, name="correlation_id"
            )
            assert_no_control_chars(
                self.correlation_id, name="correlation_id"
            )
        if self.note:
            assert_max_length(self.note, max_length=2048, name="note")
            assert_no_control_chars(self.note, name="note")
        if self.channel is not None:
            assert_non_empty(self.channel, name="channel")
            assert_max_length(self.channel, max_length=64, name="channel")
            assert_no_control_chars(self.channel, name="channel")
        if self.op:
            assert_max_length(self.op, max_length=32, name="op")
            assert_no_control_chars(self.op, name="op")
        if self.process_path:
            assert_max_length(
                self.process_path, max_length=4096, name="process_path"
            )
            assert_no_control_chars(self.process_path, name="process_path")
        if self.command_line:
            # Command lines can be long; cap generously. Control chars are
            # stripped/rejected so a crafted arg can never corrupt the log.
            assert_max_length(
                self.command_line, max_length=8192, name="command_line"
            )
            assert_no_control_chars(self.command_line, name="command_line")


# ---------------------------------------------------------------------------
# ChannelPolicy — PR-501
# ---------------------------------------------------------------------------
@dataclass(frozen=True, slots=True, kw_only=True)
class ChannelPolicy:
    """Per-channel policy for ASK fallback + ask-rate quota.

    Mirrors the legacy ``PolicyCenter._no_ui_channels`` set + ask-quota
    bookkeeping (``backend/security/policy.py:377-484, 1336-1530``):

    * When :attr:`channel.requires_ui` is ``False`` the application
      layer maps any ``ASK`` decision straight to ``DENY`` so headless
      callers (chat bots / background workers) never deadlock waiting
      for a human.
    * When :attr:`quota` is set, the ``check_permission`` use case
      records each ASK against an
      :class:`qai.security.application.ports.AskRateLimiterPort` keyed
      on ``(channel.name, subject)``; once the cap is exceeded inside
      the sliding window the request is short-circuited to ``DENY``.

    The aggregate is keyed by :attr:`channel.name`, which doubles as
    its persistence row id (the
    :class:`qai.security.application.ports.ChannelPolicyRepositoryPort`
    enforces uniqueness).
    """

    channel: Channel
    quota: AskQuotaWindow | None = None
    description: str = ""

    def __post_init__(self) -> None:
        if self.description:
            assert_max_length(
                self.description, max_length=1024, name="description"
            )
            assert_no_control_chars(self.description, name="description")

    @property
    def requires_ui(self) -> bool:
        """Shortcut: ``True`` iff this channel can resolve ASK interactively."""
        return self.channel.requires_ui

    @property
    def name(self) -> str:
        """Shortcut: the channel name (also the repository key)."""
        return self.channel.name

    def with_quota(self, quota: AskQuotaWindow | None) -> ChannelPolicy:
        """Return a copy of this policy with ``quota`` replaced."""
        return replace(self, quota=quota)
