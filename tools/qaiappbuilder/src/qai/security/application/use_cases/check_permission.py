# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Permission check use case (``POST /api/security/check`` heat-path).

Combines policy evaluation with persistent grants:

1. Walk the (cached) :class:`Policy` rules in declaration order. The
   first match wins; an explicit ``DENY`` short-circuits with deny.
2. If no rule matches, look up :class:`PathGrant` records for the
   subject; an unexpired grant covering the requested mask allows the
   action.
3. Otherwise the action is denied.

Every decision is recorded as an :class:`AuditEntry` through
:class:`AuditSinkPort`.

PR-501 — channel-aware ASK fallback / ask-rate quota
----------------------------------------------------

The use case accepts an optional :class:`Channel` and (when provided)
the :class:`ChannelPolicyRepositoryPort` + :class:`AskRateLimiterPort`
+ :class:`PermissionBroadcastPort` collaborators so the legacy
``PolicyCenter`` per-channel behaviour can run end-to-end without
breaking the existing single-arg signature:

* If a deny rule matches the request short-circuits to DENY (channel
  policy is not consulted — explicit deny always wins, matching the
  legacy ``_effective_decision`` precedence).
* Otherwise, on a miss-path that would have been *implicitly DENY*
  (no rule matched, no grant covers), the use case consults the
  :class:`ChannelPolicy`. When ``channel.requires_ui`` is ``False``
  the decision stays DENY but a ``PermissionAskBlockedEvent`` is
  emitted with ``reason="no_ui_channel"`` so the caller can surface
  a meaningful UX. When the channel has a quota and it is exceeded,
  same outcome with ``reason="rate_limited"``.

The legacy semantics are preserved: a request from ``wechat`` for an
out-of-policy path still results in a DENY audit row plus a clear
``ask_blocked`` event for the operator dashboard.

The ``channel`` parameter defaults to ``None`` so all S0-S7 callers
keep working unchanged; the new collaborators are also optional.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from qai.platform import protected_paths
from qai.platform.ids import IdGenerator
from qai.platform.time import Clock

from qai.security.domain.entities import AuditEntry
from qai.security.domain.errors import ChannelPolicyNotFoundError
from qai.security.domain.skill_capability import SkillCapability
from qai.security.domain.value_objects import (
    AceMask,
    Channel,
    PolicyAction,
    Resource,
    Subject,
)

from ..ports import (
    AskRateLimiterPort,
    AuditSinkPort,
    AutoApprovePort,
    ChannelPolicyRepositoryPort,
    PathGrantRepositoryPort,
    PermissionBroadcastPort,
    PolicyRepositoryPort,
)

__all__ = ["CheckPermissionResult", "CheckPermissionUseCase"]


def _path_parts(path: str) -> tuple[str, ...]:
    """Split a path into case-folded components (``/`` and ``\\``).

    Mirrors ``RuntimeStateAutoApproveAdapter._path_parts`` so global-allow
    prefix matching uses the SAME Windows-friendly, case-insensitive
    path-component semantics as the trusted-path check.
    """
    normalised = path.replace("\\", "/")
    return tuple(part.casefold() for part in normalised.split("/") if part)


def _absolutise(path: str) -> str:
    """Best-effort absolutise ``path`` for prefix comparison.

    Routes through :func:`qai.security.adapters.path_normalizer.normalize_windows_path`
    (Group C, M-Py-4) so the prefix compare, ``protected_paths._normalize``
    and ``handlers/_shared.is_under_tool_result_store_root`` all reach the
    SAME canonical form — a 8.3 short-name / extended-length / bytes / non-
    existent-leaf path can no longer diverge across these three call sites.

    Never raises: the helper degrades to the raw (normcased) input on any
    resolution fault so a matching call still proceeds on SOMETHING rather
    than crashing the permission check.
    """
    from qai.platform.path_normalize import normalize_windows_path

    normalised = normalize_windows_path(path)
    return normalised if normalised else path


def _path_under_any_prefix(path: str, prefixes: tuple[str, ...]) -> bool:
    """Return ``True`` when ``path`` equals or is nested under a prefix.

    Case-insensitive, separator-agnostic path-COMPONENT prefix match (so
    ``C:/WoS_AI/models`` matches ``C:/WoS_AI/models/foo/bar.bin`` but NOT
    ``C:/WoS_AI/models_backup``). Empty ``path`` / empty ``prefixes`` →
    ``False``.

    Both sides are normalised to the SAME absolute form before the component
    compare (the target via :func:`_absolutise`, the prefixes already resolved
    by their producers) so a RELATIVE / ``.``-containing / mixed-separator
    target still matches an absolute allow prefix — this keeps the in-process
    Python decision in lock-step with the paths the tool layer actually passes
    (an un-normalised target was the source of the "data dir still prompts"
    miss).

    Defence-in-depth: a target containing a ``..`` component is REJECTED
    outright (``return False``) — checked on the RAW target BEFORE
    absolutisation (``resolve`` would silently collapse ``..`` and could let a
    crafted ``allow_root/../../secret`` slip through by sharing leading
    components). A normal, resolved path never contains ``..``.
    """
    if not path or not prefixes:
        return False
    # ``..`` guard on the RAW target first (resolve() would collapse it).
    if ".." in _path_parts(path):
        return False
    target_parts = _path_parts(_absolutise(path))
    if not target_parts:
        return False
    for prefix in prefixes:
        prefix_parts = _path_parts(prefix)
        if not prefix_parts:
            continue
        if len(target_parts) < len(prefix_parts):
            continue
        if target_parts[: len(prefix_parts)] == prefix_parts:
            return True
    return False


# Op-mask bit layout — MUST match the native guard (rule.h EventToMaskBit)
# and the config loader (_workspace_resolver._MASK_*): READ=1<<0, WRITE=1<<1,
# EXECUTE=1<<2, DELETE=1<<3.
_MASK_READ = 1 << 0
_MASK_WRITE = 1 << 1
_MASK_EXECUTE = 1 << 2
_MASK_DELETE = 1 << 3


def _requested_op_mask_bits(requested_mask: AceMask) -> int:
    """Translate a :class:`AceMask` into op-mask bits for the tri-state check.

    Single source of truth shared by the hard-deny short-circuit and the
    op-mask ALLOW segment so the two never drift.

    Delete is a write-class operation and sets BOTH the DELETE and the WRITE
    bit: a rule that hard-denies WRITE (e.g. ``C:\\Qualcomm`` deny=W|D) must
    also deny a DELETE even if it did not spell out the delete bit, and an
    entry that ALLOWs delete already carries the write bit too. This is the
    intentionally-STRICTER Python side of the MEDIUM-2 delete-bit alignment
    (the native ``EventToMaskBit`` maps a delete event to the DELETE bit only);
    folding WRITE in here keeps the in-process guard fail-safe — it can only
    ever be at least as restrictive as native, never looser.
    """
    want = 0
    if requested_mask.read:
        want |= _MASK_READ
    if requested_mask.write:
        want |= _MASK_WRITE
    if requested_mask.execute:
        want |= _MASK_EXECUTE
    if requested_mask.delete:
        want |= _MASK_WRITE | _MASK_DELETE  # delete is a write-class op
    return want


# P-11 (backend) — the native FileGuard subject identity. The native-hook
# bridge (apps/api/_native_hook_bridge.py:69 ``_SUBJECT_IDENTIFIER``)
# attributes every intercepted OS file event from an LLM-spawned subprocess
# to Subject(kind="system", identifier="native.file_guard"). The security
# domain must stay unaware of the apps-layer bridge, so the literal is
# mirrored here (documented) rather than imported cross-context.
_NATIVE_SUBJECT_KIND = "system"
_NATIVE_SUBJECT_IDENTIFIER = "native.file_guard"


def _is_native_subprocess_subject(subject: Subject) -> bool:
    """Return ``True`` iff ``subject`` is the native FileGuard identity (P-11)."""
    return (
        subject.kind == _NATIVE_SUBJECT_KIND
        and subject.identifier == _NATIVE_SUBJECT_IDENTIFIER
    )


def _grant_path_ancestor_of(grant_path: str, resource_path: str) -> bool:
    """Return ``True`` when ``grant_path`` is an ancestor DIRECTORY of
    ``resource_path`` (a real path-boundary prefix), else ``False`` (P-11).

    Used ONLY for native-subprocess requests: after a user grants
    process/permanent scope on a *directory*, subsequent native accesses to
    sibling files under that directory match without re-asking. The check is
    robust — it normalises separators and requires a path-component boundary
    so ``C:\\foo`` matches ``C:\\foo\\bar`` but NOT ``C:\\foobar``. An exact
    match is handled by the caller (this returns ``False`` on equality so the
    caller's ``==`` branch owns that case). Never raises.
    """
    if not grant_path or not resource_path:
        return False
    # Normalise both sides to a single separator for a boundary compare. We
    # do NOT resolve()/absolutise here — the native bridge passes an already-
    # absolute OS path (``FilterEventV2.file_path``) and the grant path is
    # whatever approve stored verbatim; we only need a lexical, case-insensitive
    # (Windows) path-boundary prefix test.
    g = grant_path.replace("\\", "/").rstrip("/")
    r = resource_path.replace("\\", "/")
    if not g:
        return False
    g_cmp = g.casefold()
    r_cmp = r.casefold()
    # A real path-boundary prefix: the resource must start with the grant dir
    # FOLLOWED BY a separator (so "c:/foo" matches "c:/foo/bar" but not
    # "c:/foobar"). Equality is intentionally excluded (caller owns exact).
    return r_cmp.startswith(g_cmp + "/")


def _exec_binary_token(command: str) -> str:
    """Extract + normalize the binary token from an exec command string.

    Mirrors ``qai.command_policy.domain.extract_binary`` (take the quoted
    path or the first whitespace token) but is kept HERE as a small string
    helper so ``qai.security`` does not import ``qai.command_policy`` (the
    context-isolation contract; same mirroring approach as the native subject
    literals in this file). Single source of truth for the extraction rule
    lives in ``command_policy.extract_binary`` — keep the two in sync.

    Normalization: forward-slash the path, take the basename, lowercase, and
    drop a trailing ``.exe`` — so ``powershell``, ``C:\\...\\powershell.exe``
    and ``PowerShell.EXE`` all collapse to the same comparable token
    ``powershell``. This is what a ``is_program`` grant stores and matches on,
    so "permanently allow this program" holds regardless of how the LLM spells
    the invocation (bare name vs full path vs case).
    """
    cmd = (command or "").strip()
    if not cmd:
        return ""
    if cmd.startswith('"'):
        end_quote = cmd.find('"', 1)
        raw = cmd[1:end_quote] if end_quote > 0 else cmd
    else:
        parts = cmd.split()
        raw = parts[0] if parts else ""
    base = raw.replace("\\", "/").rsplit("/", 1)[-1].lower()
    for _ext in (".exe", ".cmd", ".bat", ".com"):
        if base.endswith(_ext):
            base = base[: -len(_ext)]
            break
    return base


def _looks_like_path_search_probe(path: str) -> bool:
    """Heuristic: is ``path`` a PATH-search / exe-lookup PROBE, not a real
    file access? (Bug 1 — 2026-07-07 dialog-storm mitigation, Python leg.)

    When the OS runs a command (e.g. ``uv pip install pdfplumber``) it walks
    every ``PATH`` directory looking for the executable, and ``cmd.exe`` even
    passes the whole command line + a ``.*`` wildcard as the name to find.
    The native hook intercepts each of those lookups, producing dozens of
    read events against non-existent targets like
    ``C:\\Programs\\LLVM\\bin\\uv pip install pdfplumber.*`` — one popup per
    PATH dir. Those are harmless read-only probes of things that DON'T EXIST.

    The authoritative "target does not exist + read-only" judgement lives in
    the native layer (guard.cpp, see the DLL-side fix). This Python-side
    heuristic is the no-reboot stopgap: it recognises the tell-tale shape of
    such a probe path — a trailing ``.*`` wildcard, or a final path segment
    that contains spaces AND reads like a command line (multiple whitespace-
    separated tokens). Real Windows file paths essentially never have a final
    component that is a multi-token command string. Deliberately narrow so it
    can only ALLOW (never deny) and only for such probe-shaped paths.
    """
    if not path:
        return False
    p = path.replace("\\", "/").rstrip("/")
    # trailing wildcard from cmd.exe PATH search
    if p.endswith(".*"):
        return True
    last = p.rsplit("/", 1)[-1] if "/" in p else p
    # final segment that is a multi-token command string (has interior spaces
    # AND ≥3 whitespace tokens, e.g. "uv pip install pdfplumber") — not a
    # plausible real filename.
    if " " in last and len(last.split()) >= 3:
        return True
    return False


@dataclass(frozen=True, slots=True, kw_only=True)
class CheckPermissionResult:
    """Outcome of :class:`CheckPermissionUseCase.execute`."""

    decision: PolicyAction
    matched_rule_id: str | None
    matched_grant_id: str | None
    audit_id: str
    # PR-501 — populated only when ``channel`` was supplied AND the
    # decision flipped because of a channel rule (no-UI / rate-limit).
    # ``None`` for everyone else, including S0-S7 callers that never
    # pass a channel.
    ask_block_reason: str | None = None
    # P0 ASK restore — ``True`` when this DENY was an *implicit* deny on a
    # would-have-asked path (no rule matched, no grant covers, no explicit
    # deny rule) AND the deployment's ``dynamic_authorization`` toggle is
    # on AND the request was NOT short-circuited by a headless-channel /
    # rate-limit block. This mirrors V1 ``Decision.ASK``: the policy itself
    # says "deny by default", but a synchronous interactive caller (普通聊天
    # 工具路径 / ``FileGuardFacade``) should pop the authorization
    # dialog and block for the user's decision instead of failing closed.
    # ``decision`` stays :attr:`PolicyAction.DENY` so every S0-S7 / channel
    # caller that ignores this flag keeps its byte-for-byte behaviour; only
    # the FileGuard ASK bridge consults it. ``False`` for an *explicit* deny
    # rule hit (hard DENY — never pops a dialog, V1 deny_patterns parity).
    would_ask: bool = False
    # audit_only run-mode — ``True`` when the policy's real decision was a
    # block (DENY / would-ask) but the deployment run-mode is ``audit_only``
    # so the use case overrode ``decision`` to ALLOW *after* auditing the
    # true outcome. The audit row records the real (block) decision for
    # observability; ``decision`` is ALLOW so neither the Python FileGuard
    # nor the native OS hook actually blocks the operation. ``False`` in
    # enforce mode and whenever the real decision was already ALLOW.
    audit_only_override: bool = False


class CheckPermissionUseCase:
    """Resolve a permission check for one ``(subject, resource, mask)``."""

    def __init__(
        self,
        *,
        policy_repository: PolicyRepositoryPort,
        grant_repository: PathGrantRepositoryPort,
        audit_sink: AuditSinkPort,
        clock: Clock,
        ids: IdGenerator,
        channel_policy_repository: ChannelPolicyRepositoryPort | None = None,
        ask_rate_limiter: AskRateLimiterPort | None = None,
        permission_broadcast: PermissionBroadcastPort | None = None,
        auto_approve: AutoApprovePort | None = None,
        dynamic_authorization: "Callable[[], bool] | None" = None,
        run_mode_provider: "Callable[[], str] | None" = None,
        global_allow_provider: "Callable[[], tuple[str, ...]] | None" = None,
        workspace_allow_provider: (
            "Callable[[str], Awaitable[tuple[str, ...]]] | None"
        ) = None,
        read_only_allow_provider: "Callable[[], tuple[str, ...]] | None" = None,
        op_mask_allow_provider: (
            "Callable[[], tuple[tuple[str, int, int], ...]] | None"
        ) = None,
        policy_env_provider: (
            "Callable[[], Mapping[str, str]] | None"
        ) = None,
        skill_allow_provider: (
            "Callable[[], Awaitable[tuple[SkillCapability, ...]]] | None"
        ) = None,
    ) -> None:
        self._policies = policy_repository
        self._grants = grant_repository
        self._audit = audit_sink
        self._clock = clock
        self._ids = ids
        self._channel_policies = channel_policy_repository
        self._rate_limiter = ask_rate_limiter
        self._broadcast = permission_broadcast
        self._auto_approve = auto_approve
        self._dynamic_authorization = dynamic_authorization
        # audit_only run-mode — 0-arg provider returning "enforce" (default)
        # or "audit_only". Read live per-call from the policy_overview bucket
        # so a mode flip takes effect without a restart. ``None`` (S0-S7
        # callers / tests) keeps enforce semantics byte-for-byte.
        self._run_mode_provider = run_mode_provider
        # Three-state whitelist — 0-arg provider returning the GLOBAL allow
        # prefixes (four data/models roots + operator ``global_allow_paths``).
        # A path resource under any of these prefixes short-circuits ALLOW
        # (op-agnostic: read/write/execute) BEFORE the policy / grant / ASK
        # cascade — mirroring the native guard64.dll allow (white) prefix
        # list from the SAME source (State-Truth-First). The exec-deny hard
        # gate is still re-checked so a protected-path deny is never bypassed.
        # ``None`` (S0-S7 callers / tests) keeps pre-feature behaviour
        # byte-for-byte (no global allow surface).
        self._global_allow_provider = global_allow_provider
        # Three-state whitelist — SESSION-SCOPED workspace subtree coverage.
        # An async ``(conversation_id) -> tuple[str, ...]`` provider returning
        # the allow prefix(es) for the CURRENT collaboration session's working
        # directory (its subtree). A path under any returned prefix is ALLOWED
        # (op-agnostic: read/write/execute) — but ONLY for that conversation
        # (session isolation): the provider is keyed by ``scope_conversation_id``
        # and resolves that conversation's own workspace, so conversation *A*'s
        # subtree never authorises conversation *B*. This complements the
        # EXACT-path workspace session grant (which covers only the workspace
        # directory itself) by extending r/w/x to the whole subtree, mirroring
        # what the process-global native allow prefix already does for
        # sub-processes — but scoped to the session in the Python tool layer.
        # ``None`` (S0-S7 callers / tests / no conversation context) keeps
        # pre-feature behaviour byte-for-byte.
        self._workspace_allow_provider = workspace_allow_provider
        # Op-aware READ-ONLY whitelist — 0-arg provider returning the read-only
        # allow prefixes (business dirs + system read surface + operator
        # ``read_only_allow_paths`` / ``system_read_allow_paths``). A path
        # resource under any of these prefixes short-circuits ALLOW ONLY when
        # the request is READ-ONLY (read set, and no write / execute / delete)
        # — mirroring the native guard64.dll op-aware read-only whitelist from
        # the SAME source (State-Truth-First). Non-read requests fall through
        # to the policy / grant / ASK cascade (never silently allowed, never a
        # hard deny). ``protected_write_paths`` (black) still wins via the
        # hard-deny short-circuit hoisted to the TOP of ``check`` (which runs
        # BEFORE this provider is ever consulted). ``None`` (S0-S7 callers /
        # tests) keeps
        # pre-feature behaviour byte-for-byte (no read-only allow surface).
        self._read_only_allow_provider = read_only_allow_provider
        # Op-masked base-environment whitelist (THREE-STATE) — 0-arg provider
        # returning ``((path, allow_mask, deny_mask), ...)`` from
        # factory/config/file_guard_paths.json (READ=1, WRITE=2, EXECUTE=4,
        # DELETE=8). A path resource under a rule prefix is evaluated per op:
        # any requested op bit in ``deny_mask`` → DENY; else EVERY requested op
        # bit covered by ``allow_mask`` → ALLOW; else (some op in neither mask)
        # → FALL THROUGH to policy/grant/ASK (never a silent deny — this is the
        # fix for the old subtree-authoritative flaw). So C:\Qualcomm (allow=R|X,
        # deny=W|D) allows read/execute, DENIES write; %ProgramFiles% (allow=R|X,
        # deny=0) allows read/execute, ASKs on write. Mirrors the native op-mask
        # whitelist from the SAME source (State-Truth-First), which likewise
        # evaluates DENY before ALLOW. ``None`` (S0-S7 callers / tests) keeps
        # pre-feature behaviour byte-for-byte.
        self._op_mask_allow_provider = op_mask_allow_provider
        # Domain-agnostic ``${VAR}`` bindings forwarded to
        # :meth:`Policy.evaluate_request` (and the exec-deny gate) so
        # built-in / operator-authored patterns like ``${PROJECT_ROOT}/*``
        # (shipped by the ``strict`` / ``development`` / ``demo`` policy
        # templates in :mod:`security_templates`) are expanded to real
        # absolute paths before matching. Without this the placeholders
        # stayed literal at evaluation time and the "allow project root"
        # rules never fired — the catch-all deny then took over and hit
        # every write as a hard-DENY *rule match* (``would_ask=False``),
        # suppressing the ASK dialog. State-Truth-First: the provider is
        # 0-arg and resolved live per-call so an operator-triggered
        # workspace / repo relocation takes effect without re-wiring DI.
        # ``None`` (S0-S7 callers / tests that don't ship placeholder
        # patterns) keeps pre-fix behaviour byte-for-byte — the domain
        # then falls back to its ``env=None`` path.
        self._policy_env_provider = policy_env_provider
        # Skill-capability-driven ALLOW short-circuit. 0-arg provider
        # returning a snapshot of the currently-active :class:`SkillCapability`
        # tuple — every skill whose ``skills.overrides[<id>].mode`` is
        # anything other than ``"off"``. The apps layer resolves this from
        # the ``SkillCapabilityRegistryPort`` + the ``user_prefs`` skill
        # mode table on every check so an operator toggle takes effect
        # without re-wiring DI. See the branch inside :meth:`execute` for
        # ordering: hard-deny → workspace-allow → SKILL-ALLOW → auto-approve
        # → policy → grants → ASK. Placing skill AFTER workspace means a
        # per-session workspace grant still wins (session isolation stays
        # authoritative), and placing it BEFORE auto-approve / policy means
        # a skill's declared path never regresses into a policy DENY —
        # matching the "SkillCapability is a static contract the skill
        # declared up front, honoured by the security center" semantics
        # from :mod:`qai.security.domain.skill_capability`. ``None``
        # provider (S0-S7 callers / tests) keeps pre-feature behaviour
        # byte-for-byte (no skill short-circuit).
        self._skill_allow_provider = skill_allow_provider

    async def execute(
        self,
        *,
        subject: Subject,
        resource: Resource,
        requested_mask: AceMask,
        correlation_id: str | None = None,
        channel: Channel | None = None,
        op: str = "",
        process_path: str = "",
        command_line: str = "",
        actor_pid: int | None = None,
        actor_parent_pid: int | None = None,
        scope_conversation_id: str = "",
        scope_boot_id: str = "",
    ) -> CheckPermissionResult:
        """Evaluate a permission request and record an audit fact.

        The trailing metadata kwargs are OPTIONAL audit-enrichment only —
        they never influence the decision (which is derived solely from
        ``requested_mask`` + policy/grant state). They are recorded verbatim
        on the :class:`AuditEntry` so the audit trail can distinguish a
        delete from a write (``op``) and attribute a native sub-process file
        event to its triggering process (``process_path`` / ``command_line``
        / ``actor_pid`` / ``actor_parent_pid``). All default to empty so every
        existing caller keeps working byte-for-byte.
        """
        if requested_mask.is_empty():
            raise ValueError("requested_mask must have at least one bit set")

        policy = await self._policies.load()
        matched_rule_id: str | None = None
        decision: PolicyAction | None = None
        # Resolve ``${VAR}`` bindings for policy-rule pattern expansion
        # (see ``_policy_env_provider`` in ``__init__``). Live per-call so
        # a workspace / repo relocation takes effect without re-wiring DI.
        # Any provider fault degrades to ``None`` — the domain then falls
        # back to its ``env=None`` path (patterns matched verbatim), which
        # is the pre-fix behaviour. Assembled ONCE per ``execute`` call so
        # the four downstream ``evaluate_request`` call sites share the
        # same snapshot (a resolver flip mid-check would violate the
        # "one decision, one env" invariant we test in the domain suite).
        policy_env: "Mapping[str, str] | None" = None
        if self._policy_env_provider is not None:
            try:
                policy_env = self._policy_env_provider()
            except Exception:  # noqa: BLE001 — provider fault → env=None
                policy_env = None

        # ==================================================================
        # HARD-DENY SHORT-CIRCUIT (checked FIRST — before every ALLOW branch)
        # ------------------------------------------------------------------
        # Invariant (spec §3): "hard protection (deny_mask + protected_write_
        # paths black list) can be overridden by NO grant". The ALLOW
        # short-circuits below (global-allow :449, read-only :484, probe :509,
        # op-mask allow :536, workspace :576) each set ``decision = ALLOW`` and
        # skip the rest of the chain — so if a hard-deny were only evaluated
        # LATER (as the op-mask deny at :559 used to be), a path that is BOTH
        # under a global-allow / workspace prefix AND under a deny rule would be
        # wrongly ALLOWED (review finding HIGH-1). We therefore hoist BOTH
        # hard-protection channels to the very top, structurally mirroring the
        # native guard's "black list first" order (guard.cpp MatchesAnyRule /
        # MatchesWhiteOpsDeny) instead of relying on prefixes never overlapping.
        #
        # (a) ``protected_write_paths`` black list (qai.platform.protected_paths,
        #     e.g. C:\Qualcomm) — a WRITE or DELETE anywhere under a protected
        #     tree is HARD-DENIED. This is the in-process twin of the native
        #     deny rule seeded from the same list (_native_hook_rules.py:113);
        #     until now it existed only in comments on the Python side (LOW-3),
        #     so op-mask deny_mask was the ONLY Python defence — the very defence
        #     HIGH-1 bypassed. Read/execute are unaffected (reads of an SDK are
        #     fine; the guard only blocks mutation).
        # (b) op-mask ``deny_mask`` — any requested op in a matched rule's
        #     deny_mask → DENY (e.g. WRITE under C:\Qualcomm, deny=W|D). Same
        #     bit computation as the op-mask segment below; evaluated here so a
        #     grant / global-allow / workspace prefix can never unlock it.
        hard_deny_from_protected_paths = False
        if resource.kind == "path" and (
            requested_mask.write or requested_mask.delete
        ):
            matched_prefix = protected_paths.is_write_blocked(resource.identifier)
            if matched_prefix is not None:
                decision = PolicyAction.DENY
                # C-Sec-2: remember this DENY came from the hard-deny floor
                # (``protected_paths`` — native guard64's in-process twin).
                # The ``audit_only`` override below MUST leave this decision
                # alone: the native OS hook cannot be flipped by Python
                # config, so overriding here would desync the two layers
                # (Python says ALLOW, native still blocks) and violate the
                # "hard floor is unbypassable" invariant.
                hard_deny_from_protected_paths = True

        if (
            decision is None
            and resource.kind == "path"
            and self._op_mask_allow_provider is not None
        ):
            want = _requested_op_mask_bits(requested_mask)
            if want != 0:
                # 2026-08-09 — scan EVERY matching prefix, deny wins.
                # This mirrors the native guard exactly: ``rule.h``
                # ``MatchesWhiteOpsRule`` walks the whole rule table and
                # ``continue``s past rules whose selected mask lacks the op,
                # so ANY matching deny denies. The old code took the FIRST
                # matching prefix and ``break``ed, which made both layers
                # disagree AND made nesting order-dependent: a nested entry
                # listed after its parent was dead config (``%SystemRoot%\INF``
                # write=deny never fired because ``%SystemRoot%`` matched
                # first), while a nested READ-ONLY entry listed before its
                # parent shadowed the parent's wider allow (``%APPDATA%``
                # read=allow hid ``%USERPROFILE%`` rwx=allow, downgrading
                # writes there to an ASK the native layer never asked for).
                # Union semantics remove the ordering question entirely.
                for _prefix, _allow_mask, _deny_mask in (
                    self._op_mask_allow_provider()
                ):
                    if not _path_under_any_prefix(
                        resource.identifier, (_prefix,)
                    ):
                        continue
                    if (want & int(_deny_mask)) != 0:
                        decision = PolicyAction.DENY
                        # 2026-08-09 — an op-mask ``deny`` is a HARD floor too,
                        # so flag it exactly like the ``protected_paths`` DENY
                        # above and keep the ``audit_only`` override off it.
                        #
                        # ``file_guard_paths.json`` defines this state as
                        # "HARD-DENIED (no prompt, no override — a grant cannot
                        # unlock it)", and the native guard enforces it in
                        # ``LocalDecide`` with NO run-mode input at all
                        # (verified: zero ``audit_only`` / ``run_mode`` matches
                        # under ``native/``). Softening it here therefore could
                        # not soften the native layer with it: an in-process
                        # ``write`` would succeed while the same write from a
                        # guarded CHILD still failed with "Permission denied" —
                        # one path, two answers, and a log that records ALLOW
                        # for an operation the user watched fail.
                        #
                        # ``audit_only`` means "log-but-allow ordinary policy /
                        # grant denials", not "disable hard protection", so the
                        # two hard floors now behave identically under it. This
                        # became reachable in practice once ``%SystemRoot%\INF``
                        # and ``C:\$Recycle.Bin`` were given write=deny.
                        hard_deny_from_protected_paths = True
                        break
        # ==================================================================

        # Three-state whitelist — GLOBAL allow prefixes (four data/models
        # roots + operator ``global_allow_paths``). A path resource under any
        # of these prefixes is ALWAYS ALLOWED (op-agnostic: read/write/execute,
        # subtree-covering) for ANY session, WITHOUT prompting — the inverse
        # of ``protected_write_paths``. This mirrors the native guard64.dll
        # allow (white) prefix list, reading the SAME source
        # (``resolve_global_allow_paths`` via the injected provider) so both
        # FileGuard layers stay in sync (State-Truth-First). Checked FIRST so
        # it short-circuits before the auto-approve / policy / grant / ASK
        # cascade. Exec still re-runs the ``op=exec_deny`` hard-deny gate
        # below (a protected-path deny can never be bypassed by a global
        # allow — same invariant the auto-approve ALLOW branch enforces).
        if (
            decision is None
            and resource.kind == "path"
            and self._global_allow_provider is not None
            and _path_under_any_prefix(
                resource.identifier, self._global_allow_provider()
            )
        ):
            decision = PolicyAction.ALLOW
            if requested_mask.execute:
                evaluated = policy.evaluate_request(
                    resource.identifier,
                    read=requested_mask.read,
                    write=requested_mask.write,
                    execute=True,
                    env=policy_env,
                )
                if (
                    evaluated is not None
                    and evaluated[0] is PolicyAction.DENY
                ):
                    decision, matched_rule_id = evaluated

        # Op-aware READ-ONLY whitelist — business dirs + system read surface.
        # A path resource under any read-only prefix is ALLOWED WITHOUT
        # prompting ONLY when the request is READ-ONLY (read requested, and no
        # write / execute / delete): reads of the huge system / business read
        # surface never prompt, but any write / edit / delete / execute still
        # falls through to the policy / grant / ASK cascade (never silently
        # allowed, never a hard deny). This mirrors the native guard64.dll
        # op-aware read-only whitelist, reading the SAME source
        # (``resolve_read_only_allow_paths`` via the injected provider) so both
        # FileGuard layers stay in sync (State-Truth-First). Because the match
        # is gated on read-only, no exec-deny re-check is needed (execute is
        # excluded); ``protected_write_paths`` (black) still wins for any
        # write/delete via the hard-deny short-circuit at the TOP of ``check``.
        if (
            decision is None
            and resource.kind == "path"
            and self._read_only_allow_provider is not None
            and requested_mask.read
            and not requested_mask.write
            and not requested_mask.execute
            and not requested_mask.delete
            and _path_under_any_prefix(
                resource.identifier, self._read_only_allow_provider()
            )
        ):
            decision = PolicyAction.ALLOW

        # PATH-search / exe-lookup probe — read-only ALLOW without prompting
        # (Bug 1 dialog-storm stopgap, 2026-07-07). When a command runs, the OS
        # walks every PATH dir hunting the exe; the native hook turns each into
        # a read event against a non-existent, command-shaped path
        # (``…\uv pip install pdfplumber.*``). Those are harmless read-only
        # probes and must not each pop a dialog. Gated hard on READ-ONLY (no
        # write/exec/delete) AND the probe-shaped-path heuristic, so it can only
        # ALLOW such lookups — a real file read/write is unaffected (a normal
        # path fails the heuristic and falls through to the cascade). The native
        # layer (guard.cpp) has the authoritative "target-not-found + read-only"
        # fix; this is the no-reboot Python-side mitigation.
        if (
            decision is None
            and resource.kind == "path"
            and requested_mask.read
            and not requested_mask.write
            and not requested_mask.execute
            and not requested_mask.delete
            and _looks_like_path_search_probe(resource.identifier)
        ):
            decision = PolicyAction.ALLOW

        # Op-masked base-environment whitelist (THREE-STATE) — factory/config/
        # file_guard_paths.json (READ=1, WRITE=2, EXECUTE=4, DELETE=8). For a
        # path under a rule prefix, evaluated per op (DENY wins, mirroring the
        # native decision order):
        #   * any requested op bit in the rule's deny_mask → DENY (e.g. a WRITE
        #     under C:\Qualcomm, deny=W|D — a hard protection no grant unlocks,
        #     enforced here BEFORE the grant cascade below).
        #   * else every requested op bit covered by allow_mask → ALLOW (e.g.
        #     read/execute under C:\Qualcomm or %ProgramFiles%).
        #   * else (some requested op in NEITHER mask) → leave decision None so
        #     the request FALLS THROUGH to policy / grant / ASK (e.g. a WRITE
        #     under %ProgramFiles%, allow=R|X deny=0 → the user is ASKed, and a
        #     prior session/permanent grant is honoured). This is the fix for the
        #     old flaw where an unlisted op was silently hard-denied here.
        # 2026-08-09 — UNION semantics across every matching prefix, mirroring
        # the native guard (``rule.h`` ``MatchesWhiteOpsRule`` walks the whole
        # table; the call sites test DENY first, then ALLOW). Deny bits win over
        # allow bits, and an op in NEITHER union falls through to
        # policy/grant/ASK — never a silent deny.
        #
        # This replaced "only the FIRST matching prefix is consulted", which
        # made the outcome depend on provider order and desynced the two
        # layers. Both failure directions were real: a nested DENY listed after
        # its parent never fired (``%SystemRoot%\INF`` write=deny lost to
        # ``%SystemRoot%``), and a nested READ-ONLY entry listed before its
        # parent shadowed the parent's wider grant (``%APPDATA%`` read=allow hid
        # ``%USERPROFILE%`` rwx=allow, turning package-manager writes into ASK
        # storms the native layer never raised). Unioning makes overlapping
        # entries compose the obvious way and removes ordering from the
        # contract altogether.
        if (
            decision is None
            and resource.kind == "path"
            and self._op_mask_allow_provider is not None
        ):
            want = _requested_op_mask_bits(requested_mask)
            if want != 0:
                allow_union = 0
                deny_union = 0
                matched = False
                for prefix, allow_mask, deny_mask in (
                    self._op_mask_allow_provider()
                ):
                    if not _path_under_any_prefix(
                        resource.identifier, (prefix,)
                    ):
                        continue
                    matched = True
                    allow_union |= int(allow_mask)
                    deny_union |= int(deny_mask)
                if matched:
                    # Deny half is ALSO enforced by the hard-deny pre-pass at
                    # the top of the chain; repeated here so this segment is
                    # correct in isolation and ALLOW never fires on a
                    # partially-denied request.
                    if (want & deny_union) != 0:
                        decision = PolicyAction.DENY
                        # 2026-08-09 — mirror the hard-floor flag here as well.
                        # REACHABLE, not defensive padding: the op-mask provider
                        # is documented "resolved live per-call"
                        # (``_security_di.py``), i.e. it re-reads
                        # ``file_guard_paths.json`` on EVERY invocation — and
                        # this chain calls it TWICE, once in the deny pre-pass
                        # near the top and once here. An operator saving the
                        # config (or a hot reload) between the two calls yields
                        # a first read WITHOUT the deny bit and a second read
                        # WITH it: the pre-pass leaves ``decision`` None, this
                        # segment is entered, and this line is the ONLY thing
                        # that keeps ``audit_only`` off the hard deny.
                        # Reproduced with a provider returning
                        # ``deny=0`` then ``deny=W``: 2 provider calls,
                        # ``decision=deny``, ``audit_only_override=False``.
                        #
                        # It also keeps the segment correct in isolation, which
                        # the deny check above is deliberately written to be —
                        # if anyone later narrows the pre-pass, this path
                        # becomes the normal one, and a missing flag would let
                        # ``audit_only`` soften a hard deny while the native
                        # guard, which has no run-mode input, still refused it.
                        hard_deny_from_protected_paths = True
                    elif (want & ~allow_union) == 0:
                        decision = PolicyAction.ALLOW
                    # else: some op in neither union → leave None (fall through
                    # to policy / grant / ASK — NOT a silent deny).

        # Three-state whitelist — SESSION-SCOPED workspace subtree ALLOW. When
        # the request carries a conversation scope AND that conversation has a
        # working directory, a path under the workspace subtree is ALLOWED
        # (op-agnostic) WITHOUT prompting — but ONLY for THAT conversation
        # (session isolation): the provider resolves the workspace of THIS
        # ``scope_conversation_id``, so it can never widen another session's
        # surface. Runs after the global-allow check (both are ALLOW
        # short-circuits before policy/grant/ASK); exec still re-checks the
        # hard exec-deny gate so a protected-path deny is never bypassed.
        if (
            decision is None
            and resource.kind == "path"
            and self._workspace_allow_provider is not None
            and scope_conversation_id
        ):
            try:
                ws_prefixes = await self._workspace_allow_provider(
                    scope_conversation_id
                )
            except Exception:  # noqa: BLE001 — provider fault → no ws allow
                ws_prefixes = ()
            if _path_under_any_prefix(resource.identifier, ws_prefixes):
                decision = PolicyAction.ALLOW
                if requested_mask.execute:
                    evaluated = policy.evaluate_request(
                        resource.identifier,
                        read=requested_mask.read,
                        write=requested_mask.write,
                        execute=True,
                        env=policy_env,
                    )
                    if (
                        evaluated is not None
                        and evaluated[0] is PolicyAction.DENY
                    ):
                        decision, matched_rule_id = evaluated

        # Skill-capability ALLOW short-circuit — a skill's ``skill.policy.json``
        # declaration is a static up-front contract the security center honours
        # for the ops the skill declared. Runs AFTER workspace / global / read-
        # only / op-mask allow (so a session-scoped grant stays authoritative)
        # but BEFORE auto_approve / policy / grant / ASK (so a policy DENY
        # never overrides a legitimately-declared skill path — the skill
        # author's declaration is the intent). Op semantics:
        #   * request has ``read`` bit → any active capability whose
        #     ``covers_read(path)`` matches short-circuits ALLOW
        #   * request has ``write`` or ``delete`` bit → ``covers_write(path)``
        #     (delete is a write-class op per :func:`_requested_op_mask_bits`)
        #   * request has ``execute`` bit → ``covers_exec(path)``
        # ALL declared bits must covered for the decision to fire — a
        # combined read+write request against a read-only skill declaration
        # falls through (mirrors the op-mask ALLOW's "every requested op bit
        # covered" invariant). Any provider fault degrades to "no coverage"
        # so a hiccup can never widen the surface. The exec-deny hard gate
        # is re-checked (mirrors the workspace-allow branch above) so an
        # ``op=EXEC_DENY`` rule can still veto a skill-authorised exec.
        if (
            decision is None
            and resource.kind == "path"
            and self._skill_allow_provider is not None
        ):
            try:
                _skills = await self._skill_allow_provider()
            except Exception:  # noqa: BLE001 — provider fault → no coverage
                _skills = ()
            for _cap in _skills or ():
                covered = True
                if requested_mask.read and not _cap.covers_read(
                    resource.identifier
                ):
                    covered = False
                if covered and (
                    requested_mask.write or requested_mask.delete
                ) and not _cap.covers_write(resource.identifier):
                    covered = False
                if covered and requested_mask.execute and not _cap.covers_exec(
                    resource.identifier
                ):
                    covered = False
                if covered:
                    decision = PolicyAction.ALLOW
                    if requested_mask.execute:
                        evaluated = policy.evaluate_request(
                            resource.identifier,
                            read=requested_mask.read,
                            write=requested_mask.write,
                            execute=True,
                            env=policy_env,
                        )
                        if (
                            evaluated is not None
                            and evaluated[0] is PolicyAction.DENY
                        ):
                            decision, matched_rule_id = evaluated
                    break

        # U-005 / 5-H4 — auto-approve / command-list pre-check (V1
        # ``PolicyCenter.is_auto_approved`` ran *before* the FileGuard
        # policy rules). Tri-state:
        #   * True  -> short-circuit ALLOW (operator auto-approved this
        #              op / path, or exec passed the command whitelist).
        #   * False -> short-circuit DENY (exec command blacklist hit;
        #              blacklist has priority in V1 ``_command_passes_lists``).
        #   * None  -> no opinion; fall through to the normal
        #              ``Policy.evaluate_request`` + grant cascade
        #              (pre-U-005 behaviour byte-for-byte).
        # ``matched_rule_id`` stays ``None`` for an auto-approve decision
        # since no policy rule was the cause; the audit row still records
        # the resulting ALLOW/DENY.
        if decision is None and self._auto_approve is not None:
            auto = self._auto_approve.is_auto_approved(
                resource=resource,
                requested_mask=requested_mask,
            )
            if auto is True:
                decision = PolicyAction.ALLOW
                # V1 parity (``backend/tools/_security.py:301-306``): an
                # exec auto-approve must STILL be vetoed by an
                # ``op=EXEC_DENY`` hard-deny rule — "hard deny cannot be
                # bypassed". The auto-approve ALLOW short-circuits the
                # normal ``evaluate_request`` below, so for execute
                # requests we re-run the exec-deny gate here and let a
                # matching deny rule override the auto-approve ALLOW.
                if requested_mask.execute:
                    evaluated = policy.evaluate_request(
                        resource.identifier,
                        read=requested_mask.read,
                        write=requested_mask.write,
                        execute=True,
                        env=policy_env,
                    )
                    if (
                        evaluated is not None
                        and evaluated[0] is PolicyAction.DENY
                    ):
                        decision, matched_rule_id = evaluated
            elif auto is False:
                decision = PolicyAction.DENY

        # Operation-aware evaluation (V1 4-list parity): the rule's ``op``
        # dimension must be relevant to the requested mask, and any
        # ``op=exec_deny`` regex match on an execute request short-circuits
        # to DENY first. Rules persisted before the ``op`` field existed
        # load as ``op=any`` and still match on path glob regardless of
        # operation, so this is byte-for-byte backward compatible.
        if decision is None:
            evaluated = policy.evaluate_request(
                resource.identifier,
                read=requested_mask.read,
                write=requested_mask.write,
                execute=requested_mask.execute,
                env=policy_env,
            )
            if evaluated is not None:
                decision, matched_rule_id = evaluated

        matched_grant_id: str | None = None
        if decision is None or decision is PolicyAction.ALLOW:
            # If no rule matched OR a rule allowed: still verify a grant
            # exists when no rule matched at all, so we have a clean
            # default-deny posture for paths.
            if decision is None and resource.kind in ("path", "exec"):
                grants = await self._grants.list_for_subject(subject)
                now = self._clock.now()
                # P-11 (backend) — native-subprocess granularity. In-process
                # (6-tool) requests keep EXACT-path matching (no regression);
                # native-subprocess requests (Subject system/native.file_guard,
                # per _native_hook_bridge.py) additionally match when the grant
                # path is an ANCESTOR DIRECTORY of the requested file. This lets
                # a directory-scoped process/permanent grant cover sibling files
                # so each new file under a granted uv/tool dir does NOT re-ask.
                # NOTE: this only widens WHICH stored grants match — it does not
                # change what approve_permission stores (still the exact file
                # path); a single-file grant therefore never matches siblings.
                # See FIX 3 report on the approve-side coupling.
                native_request = _is_native_subprocess_subject(subject)
                # Constraint G — deny-effect grants (a REMEMBERED rejection)
                # win over allow-effect grants (a remembered approval) for the
                # same (subject, path, mask, scope). We therefore cannot break
                # on the first matching allow-grant: a deny-grant later in the
                # list must still take precedence. Record the first matching
                # allow-grant but keep scanning; a matching deny-grant
                # short-circuits to DENY immediately.
                matched_allow_grant_id: str | None = None
                for grant in grants:
                    if grant.is_expired(now=now):
                        continue
                    # SEC — true session/process scoping: a grant only
                    # applies in the context it was scoped to. permanent →
                    # always; process → only this process's boot id; session
                    # → only this collaboration session's top-level
                    # conversation id (shared by main agent + sub-agents /
                    # participants). Missing context matches permanent only.
                    if not grant.matches_scope(
                        boot_id=scope_boot_id,
                        conversation_id=scope_conversation_id,
                    ):
                        continue
                    path_matches = grant.path == resource.identifier
                    if (
                        not path_matches
                        and grant.is_program
                        and resource.kind == "exec"
                    ):
                        # Program-grant match (permanently-allow-this-program):
                        # the grant stores a normalized binary token (e.g.
                        # ``powershell``) and matches ANY exec command whose
                        # extracted binary equals it — so the user is not asked
                        # again for a different powershell invocation. Only for
                        # kind="exec" (a program grant is meaningless for a path
                        # resource). Uses the same extraction+normalization the
                        # approve path used to store it (single source of truth
                        # documented on ``_exec_binary_token``).
                        path_matches = (
                            _exec_binary_token(resource.identifier) == grant.path
                        )
                    if not path_matches and (native_request or grant.is_directory):
                        # Directory-prefix match. Two cases reach here:
                        #  * native-subprocess request (P-11, implicit — a
                        #    native grant historically covers sibling files);
                        #  * ANY subject when the grant was EXPLICITLY made a
                        #    directory grant by the user (P-11B, is_directory
                        #    =True). The user saw & chose "authorize the whole
                        #    directory" in the dialog, so widening to
                        #    in-process / exec subjects here is their explicit
                        #    consent, not an implicit privilege escalation.
                        # ``_grant_path_ancestor_of`` enforces a real path
                        # boundary (C:\foo matches C:\foo\bar, NOT C:\foobar).
                        path_matches = _grant_path_ancestor_of(
                            grant.path, resource.identifier
                        )
                    if not path_matches:
                        continue
                    if not grant.covers(requested_mask):
                        continue
                    if grant.effect == "deny":
                        # A remembered rejection for this (path, mask, scope).
                        # Constraint G: this wins over any allow-grant, so
                        # short-circuit to DENY immediately regardless of
                        # whether an allow-grant was already seen.
                        matched_grant_id = grant.grant_id
                        decision = PolicyAction.DENY
                        break
                    # Allow-effect: remember the first match but keep scanning
                    # in case a deny-grant appears later in the list.
                    if matched_allow_grant_id is None:
                        matched_allow_grant_id = grant.grant_id
                if decision is None and matched_allow_grant_id is not None:
                    matched_grant_id = matched_allow_grant_id
                    decision = PolicyAction.ALLOW

        ask_block_reason: str | None = None
        would_ask = False
        if decision is None:
            # Implicit-deny / would-have-asked path — consult the
            # channel policy (PR-501) when one was supplied. The
            # outcome is still DENY in either branch (no async UI in
            # this use case); the only observable side-effect is the
            # ``PermissionAskBlockedEvent`` on the broadcast port and
            # the populated :attr:`ask_block_reason` field on the
            # result.
            if channel is not None:
                ask_block_reason = await self._consult_channel_policy(
                    channel=channel,
                    subject=subject,
                    resource=resource,
                )
            # P0 ASK restore — when this is an implicit deny that was NOT
            # short-circuited by a headless-channel / rate-limit block AND
            # the deployment's ``dynamic_authorization`` toggle is on, flag
            # it as a would-ask so the FileGuard ASK bridge pops the
            # authorization dialog (V1 ``Decision.ASK`` parity). An explicit
            # deny-rule hit never reaches here (it short-circuits via
            # ``evaluate_request``), so a hard DENY never sets this flag.
            if ask_block_reason is None and self._should_ask():
                would_ask = True
            decision = PolicyAction.DENY

        audit_id = self._ids.new_id()
        # Bug 3 (2026-07-07) — audit clarity for would-ask rows. When
        # ``would_ask`` is set, ``decision`` is a PROVISIONAL default-deny: the
        # real outcome is decided later by the user via the ASK dialog
        # (approve/reject route), which this synchronous use case cannot see.
        # PolicyAction has only ALLOW/DENY (no ASK/PENDING) and we do not want
        # P-17 (2026-07-09) — deferred audit for ASK outcomes. When would_ask
        # is True the real decision (allow/deny) is not yet known — it will be
        # determined by the user via the approval dialog. Writing a provisional
        # ``deny/ask_pending`` row here produced a misleading permanent DENY
        # entry in the audit log even when the user later approved the request.
        # Fix: skip the audit row entirely for would_ask; the approve/reject use
        # cases now each write the definitive row once the user has decided.
        # Deterministic outcomes (hard ALLOW or hard DENY, no dialog) are still
        # written here immediately as before.
        if not would_ask:
            # L-Py-4 + L-Py-6 + L-Sec-6: stamp the policy-version snapshot AND
            # the normalised resource path into the audit note so a reviewer
            # can (a) tie a decision to the exact ruleset that was live, and
            # (b) see the CANONICAL path we actually matched against (an 8.3
            # short-name / extended-prefix / relative-target request would
            # otherwise leave the raw agent-supplied string alone). ``policy``
            # is the SAME snapshot the whole cascade uses (fetched once at the
            # top of ``execute``) so the version can never drift mid-check.
            note_parts: list[str] = [f"policy_version={policy.version}"]
            if resource.kind == "path" and resource.identifier:
                resource_norm = _absolutise(resource.identifier)
                note_parts.append(f"resource_norm={resource_norm}")
            await self._audit.append(
                AuditEntry(
                    audit_id=audit_id,
                    occurred_at=self._clock.now(),
                    subject=subject,
                    resource=resource,
                    decision=decision,
                    rule_id=matched_rule_id,
                    correlation_id=correlation_id,
                    note=" ".join(note_parts),
                    channel=channel.name if channel is not None else None,
                    op=op,
                    process_path=process_path,
                    command_line=command_line,
                    actor_pid=actor_pid,
                    actor_parent_pid=actor_parent_pid,
                )
            )

        # audit_only run-mode — the audit row above recorded the policy's
        # REAL decision; now, if the deployment run-mode is ``audit_only``
        # and that real decision was a block (DENY, incl. would-ask misses),
        # override the returned decision to ALLOW so nothing is actually
        # blocked ("log but allow"). enforce mode (default) is unchanged, and
        # a real ALLOW is never touched.
        #
        # SCOPE — the override is DISABLED for the HARD FLOORS, tracked by
        # ``hard_deny_from_protected_paths``. Two channels set that flag:
        #
        #   * ``protected_paths.is_write_blocked`` (C-Sec-2, the original);
        #   * an op-mask ``deny_mask`` hit (2026-08-09) — the state
        #     ``file_guard_paths.json`` documents as "HARD-DENIED (no prompt,
        #     no override — a grant cannot unlock it)".
        #
        # Both are enforced by ``guard.cpp`` ``LocalDecide`` as well, and that
        # function takes NO run-mode input (verified: zero ``audit_only`` /
        # ``run_mode`` matches under ``native/``). So softening either one here
        # could not soften the native side with it — an in-process write would
        # succeed while the same write from a guarded CHILD still failed, one
        # path giving two answers, with the audit row claiming ALLOW for an
        # operation the user watched fail.
        #
        # ``audit_only`` therefore means "log-but-allow ordinary policy / grant
        # denials", NOT "disable hard protection". Everything outside the two
        # floors is still fully softened, which is what the UI promises
        # ("不阻止任何操作 — 仅记录违规").
        audit_only_override = False
        if (
            decision is PolicyAction.DENY
            and not hard_deny_from_protected_paths
            and self._is_audit_only()
        ):
            decision = PolicyAction.ALLOW
            would_ask = False
            audit_only_override = True

        return CheckPermissionResult(
            decision=decision,
            matched_rule_id=matched_rule_id,
            matched_grant_id=matched_grant_id,
            audit_id=audit_id,
            ask_block_reason=ask_block_reason,
            would_ask=would_ask,
            audit_only_override=audit_only_override,
        )

    def _is_audit_only(self) -> bool:
        """Return ``True`` when the live run-mode is ``audit_only``.

        Reads the ``run_mode_provider`` (the policy_overview ``run_mode``
        bucket) on every call so a mode flip is instant. ``None`` provider
        or any provider error → ``False`` (enforce — fail-closed to the
        blocking behaviour, never silently opens).
        """
        if self._run_mode_provider is None:
            return False
        try:
            return str(self._run_mode_provider()) == "audit_only"
        except Exception:  # noqa: BLE001 — provider error → enforce (safe)
            return False


    def _should_ask(self) -> bool:
        """Return ``True`` when an implicit deny should pop the ASK dialog.

        Mirrors V1's ``dynamic_authorization`` (``access_policy``, default
        ``True``): when dynamic authorization is enabled a miss is an ASK
        (interactive callers prompt the user); when disabled a miss is a
        hard DENY (the original V2 fail-closed behaviour). ``None`` provider
        (S0-S7 callers / tests that don't wire it) keeps the old
        always-DENY posture so nothing regresses.
        """
        if self._dynamic_authorization is None:
            return False
        try:
            return bool(self._dynamic_authorization())
        except Exception:  # noqa: BLE001 — provider error → fail-closed (no ASK)
            return False

    async def _consult_channel_policy(
        self,
        *,
        channel: Channel,
        subject: Subject,
        resource: Resource,
    ) -> str | None:
        """Return the ``reason`` string when ASK is blocked, else ``None``.

        Pre-flight wiring guard: if the caller passed a ``channel`` but
        the use case wasn't constructed with the channel collaborators,
        treat that as ``"channel_policy_missing"`` so the operator
        immediately notices the misconfiguration.
        """
        if self._channel_policies is None:
            reason = "channel_policy_missing"
            await self._notify_blocked(
                channel=channel,
                subject=subject,
                resource=resource,
                reason=reason,
            )
            return reason

        cp = await self._channel_policies.get(channel.name)
        if cp is None:
            raise ChannelPolicyNotFoundError(channel.name)

        if not cp.requires_ui:
            reason = "no_ui_channel"
            await self._notify_blocked(
                channel=channel,
                subject=subject,
                resource=resource,
                reason=reason,
            )
            return reason

        if cp.quota is not None and self._rate_limiter is not None:
            allowed = await self._rate_limiter.check_and_record(
                channel=channel,
                subject=subject,
                window_seconds=cp.quota.window_seconds,
                max_asks=cp.quota.max_asks,
                now=self._clock.now(),
            )
            if not allowed:
                reason = "rate_limited"
                await self._notify_blocked(
                    channel=channel,
                    subject=subject,
                    resource=resource,
                    reason=reason,
                )
                return reason

        return None

    async def _notify_blocked(
        self,
        *,
        channel: Channel,
        subject: Subject,
        resource: Resource,
        reason: str,
    ) -> None:
        if self._broadcast is None:
            return
        await self._broadcast.publish_ask_blocked(
            channel=channel,
            subject=subject,
            resource=resource,
            reason=reason,
        )
