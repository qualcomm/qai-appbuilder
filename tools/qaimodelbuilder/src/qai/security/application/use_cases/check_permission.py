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

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

from qai.platform.ids import IdGenerator
from qai.platform.time import Clock

from qai.security.domain.entities import AuditEntry
from qai.security.domain.errors import ChannelPolicyNotFoundError
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

    The GLOBAL / workspace allow *prefixes* are already resolved to ABSOLUTE
    paths (``resolve_global_allow_paths`` / the session-workspace resolver do
    ``Path(...).resolve()``). The resource identifier reaching the use case,
    however, may be RELATIVE (e.g. a bare ``data/config/x.json`` when a tool
    ran with no explicit workspace binding) or otherwise un-normalised — and a
    relative target can never component-prefix-match an absolute prefix. So we
    normalise BOTH sides identically: absolutise the target against the CWD
    (the same base ``prefix.resolve()`` uses) and collapse ``.``/``..``/
    separators via :meth:`Path.resolve`.

    Never raises — a resolution fault (odd/too-long path, transient FS error)
    degrades to the ORIGINAL string so matching still proceeds on the raw
    value rather than crashing the permission check.
    """
    try:
        return str(Path(path).resolve())
    except Exception:  # noqa: BLE001 — degrade to the raw path (never crash)
        return path


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
            "Callable[[], tuple[tuple[str, int], ...]] | None"
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
        # policy DENY short-circuit. ``None`` (S0-S7 callers / tests) keeps
        # pre-feature behaviour byte-for-byte (no read-only allow surface).
        self._read_only_allow_provider = read_only_allow_provider
        # Op-masked base-environment whitelist — 0-arg provider returning
        # ``((path, mask), ...)`` from factory/config/file_guard_paths.json
        # (READ=1, WRITE=2, EXECUTE=4, DELETE=8). A path resource under a rule
        # prefix short-circuits ALLOW iff EVERY requested op bit is set in that
        # rule's mask (so "read+execute" is allowed but "write" on the same
        # prefix is NOT — it falls through to policy/grant/ASK). Mirrors the
        # native op-masked whitelist from the SAME source (State-Truth-First).
        # ``protected_write_paths`` (black) still wins. ``None`` (S0-S7 callers /
        # tests) keeps pre-feature behaviour byte-for-byte.
        self._op_mask_allow_provider = op_mask_allow_provider

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
        # excluded); ``protected_write_paths`` (black) still wins via the
        # policy DENY short-circuit for any non-read op.
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

        # Op-masked base-environment whitelist — factory/config/
        # file_guard_paths.json (READ=1, WRITE=2, EXECUTE=4, DELETE=8). An
        # op-mask rule is AUTHORITATIVE for its subtree: a path under a rule
        # prefix is ALLOWED iff every requested op bit is covered by that rule's
        # mask, and otherwise hard-DENIED (e.g. C:\Qualcomm mask=R|X allows read
        # / execute but DENIES write — matching the native op-masked whitelist
        # from the SAME source). Runs after the read-only check, before
        # workspace/policy. ``protected_write_paths`` (black) is checked earlier
        # and still wins.
        if (
            decision is None
            and resource.kind == "path"
            and self._op_mask_allow_provider is not None
        ):
            want = 0
            if requested_mask.read:
                want |= 1 << 0
            if requested_mask.write:
                want |= 1 << 1
            if requested_mask.execute:
                want |= 1 << 2
            if requested_mask.delete:
                want |= (1 << 1) | (1 << 3)  # delete is a write-class op
            if want != 0:
                for prefix, mask in self._op_mask_allow_provider():
                    if not _path_under_any_prefix(
                        resource.identifier, (prefix,)
                    ):
                        continue
                    # path is under this op-mask rule's subtree → authoritative.
                    if (want & ~int(mask)) == 0:
                        decision = PolicyAction.ALLOW  # all requested ops covered
                    else:
                        decision = PolicyAction.DENY  # an op not in mask → deny
                    break

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
                    )
                    if (
                        evaluated is not None
                        and evaluated[0] is PolicyAction.DENY
                    ):
                        decision, matched_rule_id = evaluated

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
                    if grant.covers(requested_mask):
                        matched_grant_id = grant.grant_id
                        decision = PolicyAction.ALLOW
                        break

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
            await self._audit.append(
                AuditEntry(
                    audit_id=audit_id,
                    occurred_at=self._clock.now(),
                    subject=subject,
                    resource=resource,
                    decision=decision,
                    rule_id=matched_rule_id,
                    correlation_id=correlation_id,
                    note="",
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
        # a real ALLOW is never touched. This single override makes both the
        # Python FileGuard and the native OS hook honour audit_only, since
        # both consume ``CheckPermissionResult.decision``.
        audit_only_override = False
        if decision is PolicyAction.DENY and self._is_audit_only():
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
