# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai policy`` / ``perm`` / ``security`` / ``audit`` -- security ops CLI.

Desktop App Plan §2.1.1 group H. Four sibling top-level command groups all
registered by this module's :func:`register`:

* ``qai policy``   -- show / set / apply-template / skill-cap subcommands
* ``qai perm``     -- list / approve / reject / cancel / check
* ``qai security`` -- grant / revoke / settings get
* ``qai audit``    -- query

De-sandbox refactor (2026-07-04) + sandbox→path rename (2026-07)
----------------------------------------------------------------
The OS-isolation sandbox chain was removed 2026-07-01 and replaced by
FileGuard, so the user-facing command verb ``sandbox`` was renamed to
``security`` (``qai security grant`` / ``qai security revoke`` /
``qai security settings get``). The legacy ``sandbox`` verb alias has been
removed (zero-compat, PROJECT-RULES §2). The grant / revoke operations are
backed by the renamed ``PathGrant`` aggregate (``security_path_grant``
table, create/revoke_path_grant use cases); the earlier ``Sandbox*`` names
were dropped since the entries are FileGuard's path authorization store,
not an OS sandbox. Pure rename, no behaviour change.

Thin wrappers over the use cases / ports surfaced by
:class:`apps.api._security_di.SecurityServices`; no business logic lives
here. The CLI is an adapter sibling to the HTTP routes, sharing the same
in-process :class:`Container` (built via
:func:`apps.cli._runtime.cli_container`).

Skill-capability subcommands (``qai policy skill-cap …``)
---------------------------------------------------------
The H-group skill *capability* registry (PR-504, ``security`` context)
is intentionally distinct from the J1 ``qai skill list/policy/toggle/…``
group (``user_prefs`` context). To avoid colliding with J1's top-level
``qai skill``, the H-group capability registry is nested under the
``policy`` group as ``qai policy skill-cap {discover,policy,register,
unregister}``.

Output / exit conventions
-------------------------
* Business output → ``stdout`` as JSON (indent 2, ``ensure_ascii=False``)
  so ``qai perm list | jq`` / ``qai audit query | jq`` work.
* Diagnostic / error output → ``stderr`` (the dispatcher prints the
  traceback for unexpected exceptions; this module writes to stderr only
  for explicit ``invalid JSON`` cases and for the reboot warning emitted
  by ``qai policy set``).
* Exit codes: 0 success, 1 business error (raised by use case), 2 usage
  error (argparse / malformed JSON / missing ``--yes`` confirmation).

§3.1 contract notes
-------------------
* CLI runs in-process and never touches the HTTP route layer, so CSRF /
  cookies / bind-host concerns do not apply here. We do NOT invent any
  new secret field names -- long-term credentials still belong in
  :class:`qai.platform.persistence.secrets.SecretStore`; nothing here
  bypasses that.
* ``qai policy set`` and ``qai policy apply-template`` go through
  :class:`UpdatePolicyUseCase`, which raises the reboot signal
  (``REBOOT_EXIT_CODE = 75``) when the new rule set differs from the
  current one. The CLI surfaces this with a ``--yes`` confirmation flag
  and a stderr warning so an operator running a script doesn't trigger
  a server reboot by accident.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from apps.api.di import Container
from apps.cli._runtime import run_use_case

__all__ = [
    "register",
    # policy
    "cmd_policy_show",
    "cmd_policy_set",
    "cmd_policy_apply_template",
    # perm
    "cmd_perm_list",
    "cmd_perm_approve",
    "cmd_perm_reject",
    "cmd_perm_cancel",
    "cmd_perm_check",
    # security (formerly "sandbox"; grant/revoke/settings-get)
    "cmd_security_grant",
    "cmd_security_revoke",
    "cmd_security_settings_get",
    # audit
    "cmd_audit_query",
    # skill-cap (nested under policy)
    "cmd_skillcap_discover",
    "cmd_skillcap_policy",
    "cmd_skillcap_register",
    "cmd_skillcap_unregister",
]


# ---------------------------------------------------------------------------
# argparse registration -- five sibling top-level groups
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach all four H-group top-level commands to the ``qai`` dispatcher.

    Although ``apps/cli/__main__.py`` lists ``"policy"`` as the module
    name, ``register`` is free to add multiple top-level parsers -- the
    ``_D2_GROUPS`` tuple only governs *which module is loaded*, not the
    parser names it contributes. Four siblings (``policy`` / ``perm`` /
    ``security`` / ``audit``) keep operator command lines short
    and match the §2.1.1.H plan verbatim.
    """

    _register_policy(subparsers)
    _register_perm(subparsers)
    _register_security(subparsers)
    _register_audit(subparsers)


# ---------------------------------------------------------------------------
# helpers -- JSON I/O, runtime kwargs
# ---------------------------------------------------------------------------


def _emit_json(value: Any) -> None:
    """Pretty-print ``value`` to stdout as JSON (indent 2, no ASCII escape).

    ``default=str`` covers ``datetime`` / ``Path`` / dataclass-derived
    fields so handlers don't have to spell out a serialiser per response
    shape.

    Writes UTF-8 bytes directly via ``sys.stdout.buffer`` so non-ASCII
    payloads (e.g. V1 preset labels like "Python 解释器 (uv)" surfacing from
    ``qai policy show``) don't blow up on a Windows PowerShell console
    (default cp1252). Subprocess callers that capture with
    ``encoding="utf-8"`` see the same bytes either way; an interactive
    operator on PowerShell now also gets readable Chinese instead of a
    UnicodeEncodeError. ``json.dumps(ensure_ascii=False, ...)`` above is
    what makes the bytes UTF-8 in the first place.
    """

    payload = json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n"
    encoded = payload.encode("utf-8")
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is None:
        # Fallback for unusual sys.stdout replacements (e.g. some test
        # stubs that aren't a real TextIOWrapper); they usually accept
        # str writes anyway.
        sys.stdout.write(payload)
    else:
        buffer.write(encoded)
    sys.stdout.flush()


def _runtime_kwargs(args: argparse.Namespace) -> dict[str, Path | None]:
    """Extract the standard ``--repo-root`` / ``--config`` overrides."""

    return {
        "repo_root": getattr(args, "repo_root", None),
        "config_file": getattr(args, "config_file", None),
    }


def _parse_json_arg(raw: str, *, name: str) -> Any:
    """Parse ``raw`` as JSON; on failure write ``invalid JSON`` to stderr.

    Returns the parsed value on success; calls :func:`sys.exit(2)` on
    failure so handlers can treat the return as guaranteed non-error
    (matches the ``qai config set`` convention from D1).
    """

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"invalid JSON in {name}: {exc}\n")
        raise SystemExit(2) from None


def _read_json_file(path: Path) -> Any:
    """Read ``path`` as UTF-8 JSON; exit 2 on parse / IO failure."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"cannot read {path}: {exc}\n")
        raise SystemExit(2) from None
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"invalid JSON in {path}: {exc}\n")
        raise SystemExit(2) from None


def _confirm_reboot(args: argparse.Namespace, action: str) -> None:
    """Bail out unless ``--yes`` was passed for a reboot-triggering action.

    ``action`` is the human description (e.g. ``"qai policy set"``).
    Writes a short reminder to stderr -- V1 parity with the
    ``REBOOT_EXIT_CODE = 75`` contract from ``refactor-plan.md §8.11``:
    the running API server will exit 75 when the supervised reboot is
    requested, prompting :mod:`apps.cli.serve` to restart the child.
    """

    if getattr(args, "yes", False):
        return
    sys.stderr.write(
        f"{action}: this will trigger a server reboot "
        f"(REBOOT_EXIT_CODE=75). Re-run with --yes to confirm.\n"
    )
    raise SystemExit(2)


# ---------------------------------------------------------------------------
# Policy entity ↔ JSON projection
# ---------------------------------------------------------------------------


def _policy_rule_to_dict(rule: Any) -> dict[str, Any]:
    """Project a :class:`PolicyRule` to a JSON-friendly dict.

    Matches the V1 wire shape (rule_id / scope / pattern / action /
    description / op) so ``qai policy show`` round-trips through
    ``qai policy set --file …`` without field churn.
    """

    return {
        "rule_id": rule.rule_id,
        "scope": rule.scope.value,
        "pattern": rule.pattern.pattern,
        "case_sensitive": rule.pattern.case_sensitive,
        "match_kind": rule.pattern.match_kind.value,
        "action": rule.action.value,
        "description": rule.description,
        "op": rule.op.value,
    }


def _policy_to_dict(policy: Any) -> dict[str, Any]:
    """Serialise a :class:`Policy` aggregate (version + rules)."""

    return {
        "version": policy.version,
        "updated_at": policy.updated_at.isoformat(),
        "rules": [_policy_rule_to_dict(r) for r in policy.rules],
    }


def _build_rule(raw: Any) -> Any:
    """Parse one rule dict into a :class:`PolicyRule`.

    Imports happen inside the function so ``apps/cli/commands/policy.py``
    stays cheap to import for ``qai --help``: the security domain types
    only get pulled in when an operator actually invokes
    ``qai policy set`` / ``apply-template``.

    Accepts the field set produced by :func:`_policy_rule_to_dict`. The
    ``case_sensitive`` / ``match_kind`` keys are optional (mirror
    :class:`PathPattern` defaults).
    """

    from qai.security.domain.entities import PolicyRule
    from qai.security.domain.value_objects import (
        PathPattern,
        PolicyAction,
        PolicyMatchKind,
        PolicyOp,
        PolicyScope,
    )

    if not isinstance(raw, dict):
        sys.stderr.write(
            f"invalid rule entry: must be a JSON object, got "
            f"{type(raw).__name__}\n"
        )
        raise SystemExit(2)
    try:
        pattern = PathPattern(
            pattern=str(raw["pattern"]),
            case_sensitive=bool(raw.get("case_sensitive", False)),
            match_kind=PolicyMatchKind(raw.get("match_kind", "glob")),
        )
        return PolicyRule(
            rule_id=str(raw["rule_id"]),
            scope=PolicyScope(raw["scope"]),
            pattern=pattern,
            action=PolicyAction(raw["action"]),
            description=str(raw.get("description", "")),
            op=PolicyOp(raw.get("op", "any")),
        )
    except KeyError as exc:
        sys.stderr.write(f"missing required rule field: {exc}\n")
        raise SystemExit(2) from None
    except (ValueError, TypeError) as exc:
        sys.stderr.write(f"invalid rule field: {exc}\n")
        raise SystemExit(2) from None


# ---------------------------------------------------------------------------
# qai policy show / set / apply-template / skill-cap …
# ---------------------------------------------------------------------------


def _register_policy(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "policy",
        help="show / set / apply built-in policy templates + skill-cap registry",
        description=(
            "Inspect or replace the active security Policy, apply a "
            "built-in template (demo / development / strict), or manage "
            "the skill-capability registry (nested under ``skill-cap``)."
        ),
    )
    sub = p.add_subparsers(
        dest="policy_command", required=True, metavar="<subcommand>"
    )

    # show
    show_p = sub.add_parser(
        "show", help="print the active Policy as JSON (rules + version)"
    )
    show_p.set_defaults(handler=cmd_policy_show)

    # set --file
    set_p = sub.add_parser(
        "set",
        help=(
            "replace the active Policy with the rule list from <file> "
            "(triggers a server reboot)"
        ),
        description=(
            "Read a JSON file containing a top-level array of rule objects "
            "(or {rules: [...]}), construct PolicyRule entities, and pass "
            "them through UpdatePolicyUseCase. Any change vs the existing "
            "rule set raises the reboot signal (REBOOT_EXIT_CODE=75); the "
            "supervised API server will restart with the new rules. Pass "
            "--yes to confirm; otherwise the command exits 2 without "
            "writing."
        ),
    )
    set_p.add_argument(
        "--file",
        required=True,
        type=Path,
        metavar="<rules.json>",
        help="path to a JSON file with the new rule set",
    )
    set_p.add_argument(
        "--yes",
        action="store_true",
        help="confirm that triggering a server reboot is acceptable",
    )
    set_p.add_argument(
        "--reason",
        default="policy changed via qai policy set",
        help="audit reason recorded with the reboot signal",
    )
    set_p.set_defaults(handler=cmd_policy_set)

    # apply-template <id>
    tpl_p = sub.add_parser(
        "apply-template",
        help=(
            "apply a built-in template (demo / development / strict); "
            "triggers a reboot when the rule set changes"
        ),
    )
    tpl_p.add_argument(
        "template_id",
        choices=("demo", "development", "strict"),
        help="built-in template id",
    )
    tpl_p.add_argument(
        "--yes",
        action="store_true",
        help="confirm that triggering a server reboot is acceptable",
    )
    tpl_p.set_defaults(handler=cmd_policy_apply_template)

    # skill-cap nested group
    skillcap_p = sub.add_parser(
        "skill-cap",
        help=(
            "skill-capability registry (security context; distinct from "
            "the J1 ``qai skill`` group)"
        ),
        description=(
            "Manage the in-memory SkillCapability registry that lets "
            "skills declare their intended IO surface up front. NOT to be "
            "confused with the user-facing skill toggle / policy commands "
            "in J1 (``qai skill list/policy/toggle/...``); those manage "
            "user_prefs-side configuration of the same skills."
        ),
    )
    skillcap_sub = skillcap_p.add_subparsers(
        dest="skillcap_command", required=True, metavar="<subcommand>"
    )

    discover = skillcap_sub.add_parser(
        "discover",
        help="aggregate registered + override + filesystem-scanned skills",
    )
    discover.set_defaults(handler=cmd_skillcap_discover)

    policy_q = skillcap_sub.add_parser(
        "policy",
        help="print the effective per-skill policy (override on defaults)",
    )
    policy_q.add_argument("name", help="skill name")
    policy_q.set_defaults(handler=cmd_skillcap_policy)

    register_q = skillcap_sub.add_parser(
        "register",
        help=(
            "register a SkillCapability from an inline JSON object "
            "(injection scanner runs against ``skill_body`` if present)"
        ),
        description=(
            "JSON shape: {skill_name, capability: {capability_name, "
            "read_paths, write_paths, exec_paths, trusted_binaries, "
            "description, sha256_pins}, skill_body?}. ``capability_name`` "
            "defaults to ``skill_name`` when omitted; the four path lists "
            "default to []."
        ),
    )
    register_q.add_argument(
        "json",
        help="JSON object describing the skill + capability",
    )
    register_q.set_defaults(handler=cmd_skillcap_register)

    unregister_q = skillcap_sub.add_parser(
        "unregister", help="remove a skill capability from the registry"
    )
    unregister_q.add_argument("name", help="skill name")
    unregister_q.set_defaults(handler=cmd_skillcap_unregister)


def cmd_policy_show(args: argparse.Namespace) -> int:
    """``qai policy show`` -- print the active Policy as JSON."""

    async def _go(c: Container) -> dict[str, Any]:
        policy = await c.security.policy_repository.load()
        return _policy_to_dict(policy)

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_policy_set(args: argparse.Namespace) -> int:
    """``qai policy set --file <rules.json>``."""

    _confirm_reboot(args, "qai policy set")
    raw = _read_json_file(args.file)
    rules_raw = raw["rules"] if isinstance(raw, dict) and "rules" in raw else raw
    if not isinstance(rules_raw, list):
        sys.stderr.write(
            "rules payload must be a JSON array (or {rules: [...]})\n"
        )
        return 2
    new_rules = tuple(_build_rule(r) for r in rules_raw)

    async def _go(c: Container) -> dict[str, Any]:
        policy = await c.security.update_policy_use_case.execute(
            new_rules=new_rules, reboot_reason=args.reason
        )
        return _policy_to_dict(policy)

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_policy_apply_template(args: argparse.Namespace) -> int:
    """``qai policy apply-template <id>``."""

    _confirm_reboot(args, "qai policy apply-template")

    async def _go(c: Container) -> dict[str, Any] | None:
        policy = await c.security.apply_security_template_use_case.execute(
            template_id=args.template_id
        )
        return _policy_to_dict(policy) if policy is not None else None

    result = run_use_case(_go, **_runtime_kwargs(args))
    if result is None:
        sys.stderr.write(f"unknown template id: {args.template_id!r}\n")
        return 1
    _emit_json(result)
    return 0


# ---------------------------------------------------------------------------
# qai perm list / approve / reject / cancel / check
# ---------------------------------------------------------------------------


def _permission_request_to_dict(req: Any) -> dict[str, Any]:
    """Project a :class:`PermissionRequest` to a JSON-friendly dict."""

    return {
        "request_id": req.request_id.value,
        "subject": {
            "kind": req.subject.kind,
            "identifier": req.subject.identifier,
        },
        "resource": {
            "kind": req.resource.kind,
            "identifier": req.resource.identifier,
        },
        "requested_mask": {
            "read": req.requested_mask.read,
            "write": req.requested_mask.write,
            "execute": req.requested_mask.execute,
            "delete": req.requested_mask.delete,
        },
        "state": req.state.value,
        "created_at": req.created_at.isoformat(),
        "resolved_at": (
            req.resolved_at.isoformat() if req.resolved_at else None
        ),
        "resolution_reason": req.resolution_reason,
    }


def _register_perm(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "perm",
        help="permission requests: list / approve / reject / cancel / check",
    )
    sub = p.add_subparsers(
        dest="perm_command", required=True, metavar="<subcommand>"
    )

    sub.add_parser(
        "list", help="print all currently PENDING permission requests"
    ).set_defaults(handler=cmd_perm_list)

    approve_p = sub.add_parser(
        "approve", help="approve a pending permission request"
    )
    approve_p.add_argument("request_id", help="opaque permission request id")
    approve_p.add_argument(
        "--reason", default="", help="audit reason recorded with the decision"
    )
    approve_p.add_argument(
        "--decided-by",
        default=None,
        help="optional subject identifier (e.g. ``user:alice``)",
    )
    approve_p.set_defaults(handler=cmd_perm_approve)

    reject_p = sub.add_parser("reject", help="reject a pending request")
    reject_p.add_argument("request_id", help="opaque permission request id")
    reject_p.add_argument("--reason", default="")
    reject_p.add_argument("--decided-by", default=None)
    reject_p.set_defaults(handler=cmd_perm_reject)

    cancel_p = sub.add_parser(
        "cancel", help="cancel a pending request (subject withdraws)"
    )
    cancel_p.add_argument("request_id", help="opaque permission request id")
    cancel_p.add_argument("--cancelled-by", default=None)
    cancel_p.set_defaults(handler=cmd_perm_cancel)

    check_p = sub.add_parser(
        "check",
        help=(
            "synchronously evaluate a check; useful for scripting / debug"
        ),
        description=(
            "Pass a JSON object with subject, resource, and requested_mask "
            "fields, e.g. {\"subject\":{\"kind\":\"user\",\"identifier\":"
            "\"alice\"},\"resource\":{\"kind\":\"path\",\"identifier\":"
            "\"C:/tmp\"},\"requested_mask\":{\"read\":true}}. The use case "
            "writes an audit row whether or not access is granted."
        ),
    )
    check_p.add_argument(
        "payload",
        help="JSON object with subject / resource / requested_mask",
    )
    check_p.set_defaults(handler=cmd_perm_check)


def _build_subject(raw: Any, *, name: str = "subject") -> Any:
    from qai.security.domain.value_objects import Subject

    if not isinstance(raw, dict):
        sys.stderr.write(f"{name} must be a JSON object\n")
        raise SystemExit(2)
    try:
        return Subject(
            kind=str(raw["kind"]), identifier=str(raw["identifier"])
        )
    except (KeyError, ValueError, TypeError) as exc:
        sys.stderr.write(f"invalid {name}: {exc}\n")
        raise SystemExit(2) from None


def _build_subject_or_none(raw: str | None) -> Any:
    """Helper for optional ``--decided-by`` / ``--cancelled-by`` flags.

    Accepts ``"kind:identifier"`` shorthand (e.g. ``"user:alice"``) or
    ``None``. Anything else without a colon defaults to ``kind=user``.
    """

    if not raw:
        return None
    from qai.security.domain.value_objects import Subject

    if ":" in raw:
        kind, identifier = raw.split(":", 1)
    else:
        kind, identifier = "user", raw
    try:
        return Subject(kind=kind, identifier=identifier)
    except (ValueError, TypeError) as exc:
        sys.stderr.write(f"invalid subject {raw!r}: {exc}\n")
        raise SystemExit(2) from None


def _build_resource(raw: Any) -> Any:
    from qai.security.domain.value_objects import Resource

    if not isinstance(raw, dict):
        sys.stderr.write("resource must be a JSON object\n")
        raise SystemExit(2)
    try:
        return Resource(
            kind=str(raw["kind"]), identifier=str(raw["identifier"])
        )
    except (KeyError, ValueError, TypeError) as exc:
        sys.stderr.write(f"invalid resource: {exc}\n")
        raise SystemExit(2) from None


def _build_mask(raw: Any) -> Any:
    from qai.security.domain.value_objects import AceMask

    if not isinstance(raw, dict):
        sys.stderr.write("requested_mask must be a JSON object\n")
        raise SystemExit(2)
    try:
        return AceMask(
            read=bool(raw.get("read", False)),
            write=bool(raw.get("write", False)),
            execute=bool(raw.get("execute", False)),
            delete=bool(raw.get("delete", False)),
        )
    except (ValueError, TypeError) as exc:
        sys.stderr.write(f"invalid requested_mask: {exc}\n")
        raise SystemExit(2) from None


def cmd_perm_list(args: argparse.Namespace) -> int:
    """``qai perm list`` -- print pending permission requests."""

    async def _go(c: Container) -> list[dict[str, Any]]:
        rows = await c.security.permission_request_repository.list_pending()
        return [_permission_request_to_dict(r) for r in rows]

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_perm_approve(args: argparse.Namespace) -> int:
    """``qai perm approve <id>``."""

    from qai.security.domain.value_objects import RequestId

    decided_by = _build_subject_or_none(args.decided_by)
    request_id = RequestId(value=args.request_id)

    async def _go(c: Container) -> dict[str, Any]:
        req = await c.security.approve_permission_use_case.execute(
            request_id=request_id,
            decided_by=decided_by,
            reason=args.reason,
        )
        return _permission_request_to_dict(req)

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_perm_reject(args: argparse.Namespace) -> int:
    """``qai perm reject <id>``."""

    from qai.security.domain.value_objects import RequestId

    decided_by = _build_subject_or_none(args.decided_by)
    request_id = RequestId(value=args.request_id)

    async def _go(c: Container) -> dict[str, Any]:
        req = await c.security.reject_permission_use_case.execute(
            request_id=request_id,
            decided_by=decided_by,
            reason=args.reason,
        )
        return _permission_request_to_dict(req)

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_perm_cancel(args: argparse.Namespace) -> int:
    """``qai perm cancel <id>``."""

    from qai.security.domain.value_objects import RequestId

    cancelled_by = _build_subject_or_none(args.cancelled_by)
    request_id = RequestId(value=args.request_id)

    async def _go(c: Container) -> dict[str, Any]:
        req = await c.security.cancel_permission_request_use_case.execute(
            request_id=request_id,
            cancelled_by=cancelled_by,
        )
        return _permission_request_to_dict(req)

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_perm_check(args: argparse.Namespace) -> int:
    """``qai perm check <json>`` -- synchronous policy + grant evaluation.

    The use case writes an audit row regardless of the decision, so this
    is an effect-bearing read (matches V1 ``check_permission`` semantics).
    """

    payload = _parse_json_arg(args.payload, name="payload")
    if not isinstance(payload, dict):
        sys.stderr.write("payload must be a JSON object\n")
        return 2

    subject = _build_subject(payload.get("subject"), name="subject")
    resource = _build_resource(payload.get("resource"))
    mask = _build_mask(payload.get("requested_mask"))
    correlation_id = payload.get("correlation_id")
    if correlation_id is not None and not isinstance(correlation_id, str):
        correlation_id = str(correlation_id)

    async def _go(c: Container) -> dict[str, Any]:
        result = await c.security.check_permission_use_case.execute(
            subject=subject,
            resource=resource,
            requested_mask=mask,
            correlation_id=correlation_id,
        )
        return {
            "decision": result.decision.value,
            "matched_rule_id": result.matched_rule_id,
            "matched_grant_id": result.matched_grant_id,
            "audit_id": result.audit_id,
            "ask_block_reason": result.ask_block_reason,
        }

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


# ---------------------------------------------------------------------------
# qai security grant / revoke / settings get   (formerly "qai sandbox …")
# ---------------------------------------------------------------------------


def _register_security(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "security",
        help=(
            "security grants + runtime settings (single-process; HTTP API "
            "remains the primary surface for live-server tweaks). "
            "These commands persist grants and the runtime-state KV "
            "bucket (consumed by audit + future security adapters). "
            "OS-level process isolation was removed 2026-07-01 (replaced "
            "by FileGuard) — the Protected Paths / FileBroker software "
            "layers guard the actual IO."
        ),
    )
    sub = p.add_subparsers(
        dest="security_command", required=True, metavar="<subcommand>"
    )

    grant_p = sub.add_parser(
        "grant",
        help="create a PathGrant from an inline JSON payload",
        description=(
            "JSON shape: {subject:{kind,identifier}, path, mask:{read,"
            "write,execute,delete}, source: 'user'|'auto'|'preset', "
            "expires_at?: iso8601}."
        ),
    )
    grant_p.add_argument("payload", help="JSON object describing the grant")
    grant_p.set_defaults(handler=cmd_security_grant)

    revoke_p = sub.add_parser("revoke", help="revoke a PathGrant by id")
    revoke_p.add_argument("grant_id")
    revoke_p.add_argument("--revoked-by", default=None)
    revoke_p.set_defaults(handler=cmd_security_revoke)

    settings_p = sub.add_parser(
        "settings", help="read security runtime settings"
    )
    settings_sub = settings_p.add_subparsers(
        dest="security_settings_command",
        required=True,
        metavar="<subcommand>",
    )
    settings_sub.add_parser(
        "get", help="print the live SecurityRuntimeStateService snapshot"
    ).set_defaults(handler=cmd_security_settings_get)
    # De-sandbox refactor (2026-07-04) — the ``settings set`` subcommand was
    # removed alongside the orphaned SaveSandboxSettingsUseCase (the
    # OS-isolation sandbox chain was removed 2026-07-01, replaced by
    # FileGuard). Operator-facing security runtime toggles are edited via the
    # ``/api/security/*`` HTTP routes, which write the live runtime-state
    # buckets directly. ``settings get`` (read-only snapshot) stays.


def cmd_security_grant(args: argparse.Namespace) -> int:
    """``qai security grant <json>``."""

    from qai.security.domain.value_objects import GrantSource

    payload = _parse_json_arg(args.payload, name="payload")
    if not isinstance(payload, dict):
        sys.stderr.write("payload must be a JSON object\n")
        return 2
    subject = _build_subject(payload.get("subject"))
    mask = _build_mask(payload.get("mask"))
    path = payload.get("path")
    if not isinstance(path, str) or not path:
        sys.stderr.write("path must be a non-empty string\n")
        return 2
    try:
        source = GrantSource(payload.get("source", "user"))
    except (ValueError, TypeError) as exc:
        sys.stderr.write(f"invalid source: {exc}\n")
        return 2
    expires_at_raw = payload.get("expires_at")
    expires_at: datetime | None = None
    if expires_at_raw:
        try:
            expires_at = datetime.fromisoformat(str(expires_at_raw))
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except ValueError as exc:
            sys.stderr.write(f"invalid expires_at: {exc}\n")
            return 2

    async def _go(c: Container) -> dict[str, Any]:
        grant = await c.security.create_path_grant_use_case.execute(
            subject=subject,
            path=path,
            mask=mask,
            source=source,
            expires_at=expires_at,
        )
        return {
            "grant_id": grant.grant_id,
            "subject": {
                "kind": grant.subject.kind,
                "identifier": grant.subject.identifier,
            },
            "path": grant.path,
            "mask": {
                "read": grant.mask.read,
                "write": grant.mask.write,
                "execute": grant.mask.execute,
                "delete": grant.mask.delete,
            },
            "source": grant.source.value,
            "created_at": grant.created_at.isoformat(),
            "expires_at": (
                grant.expires_at.isoformat() if grant.expires_at else None
            ),
        }

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_security_revoke(args: argparse.Namespace) -> int:
    """``qai security revoke <grant_id>``."""

    revoked_by = _build_subject_or_none(args.revoked_by)

    async def _go(c: Container) -> dict[str, Any]:
        await c.security.revoke_path_grant_use_case.execute(
            grant_id=args.grant_id, revoked_by=revoked_by
        )
        return {"grant_id": args.grant_id, "revoked": True}

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_security_settings_get(args: argparse.Namespace) -> int:
    """``qai security settings get`` -- live runtime-state snapshot."""

    async def _go(c: Container) -> dict[str, Any]:
        snap = c.security.security_runtime_state.snapshot()
        return {
            "enabled": snap.enabled,
            "mode": snap.mode,
            "dynamic_authorization": snap.dynamic_authorization,
            "settings": snap.settings,
        }

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


# ---------------------------------------------------------------------------
# qai audit query
# ---------------------------------------------------------------------------


def _audit_entry_to_dict(entry: Any) -> dict[str, Any]:
    return {
        "audit_id": entry.audit_id,
        "occurred_at": entry.occurred_at.isoformat(),
        "subject": {
            "kind": entry.subject.kind,
            "identifier": entry.subject.identifier,
        },
        "resource": {
            "kind": entry.resource.kind,
            "identifier": entry.resource.identifier,
        },
        "decision": entry.decision.value,
        "rule_id": entry.rule_id,
        "correlation_id": entry.correlation_id,
        "note": entry.note,
        "channel": entry.channel,
    }


def _register_audit(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "audit", help="query the security audit log"
    )
    sub = p.add_subparsers(
        dest="audit_command", required=True, metavar="<subcommand>"
    )
    q = sub.add_parser(
        "query",
        help=(
            "print the most recent audit entries (newest first); audit "
            "logs can grow large -- pipe through ``head`` / ``jq`` to "
            "control output"
        ),
    )
    q.add_argument(
        "--limit",
        type=int,
        default=100,
        help=(
            "maximum number of rows; 0 returns the empty list (sentinel). "
            "Default: 100. There is no built-in upper bound -- large "
            "values may be slow."
        ),
    )
    # ``--filter`` is reserved for future structured filters; today the
    # AuditQueryPort only exposes ``recent(limit=...)``. We accept the
    # flag (stored on args) but warn if used so operators know it's
    # currently a no-op rather than silently dropping the constraint.
    q.add_argument(
        "--filter",
        action="append",
        default=[],
        metavar="<key=value>",
        help=(
            "reserved for future server-side filters (key=value); not "
            "yet wired to the AuditQueryPort -- pipe through ``jq`` for now"
        ),
    )
    q.set_defaults(handler=cmd_audit_query)


def cmd_audit_query(args: argparse.Namespace) -> int:
    """``qai audit query [--limit N] [--filter k=v]``."""

    if args.limit < 0:
        sys.stderr.write("--limit must be >= 0\n")
        return 2
    if args.filter:
        # Surface the no-op so operators don't think a filter was applied.
        sys.stderr.write(
            "qai audit query: --filter is not yet wired to the "
            "AuditQueryPort; ignoring "
            f"{args.filter!r}. Use ``| jq`` for now.\n"
        )

    async def _go(c: Container) -> list[dict[str, Any]]:
        entries = await c.security.audit_query.recent(limit=args.limit)
        return [_audit_entry_to_dict(e) for e in entries]

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


# ---------------------------------------------------------------------------
# qai policy skill-cap discover / policy / register / unregister
# ---------------------------------------------------------------------------


def cmd_skillcap_discover(args: argparse.Namespace) -> int:
    """``qai policy skill-cap discover``."""

    async def _go(c: Container) -> dict[str, Any]:
        result = await c.security.skill_discovery_use_case.execute()
        return {"skills": result.skills, "by_name": result.by_name}

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_skillcap_policy(args: argparse.Namespace) -> int:
    """``qai policy skill-cap policy <name>``."""

    async def _go(c: Container) -> dict[str, Any] | None:
        uc = c.security.get_skill_policy_use_case
        if not await uc.is_known(args.name):
            return None
        view = await uc.execute(skill_name=args.name)
        return {
            "skill_name": view.skill_name,
            "capability_name": view.capability_name,
            "read_paths": view.read_paths,
            "write_paths": view.write_paths,
            "exec_paths": view.exec_paths,
            "trusted_binaries": view.trusted_binaries,
            "description": view.description,
            "raw_read": view.raw_read,
            "raw_write": view.raw_write,
            "raw_trusted_binaries": view.raw_trusted_binaries,
            "has_policy": view.has_policy,
            "active": view.active,
            "source": view.source,
        }

    out = run_use_case(_go, **_runtime_kwargs(args))
    if out is None:
        sys.stderr.write(f"unknown skill: {args.name!r}\n")
        return 1
    _emit_json(out)
    return 0


def cmd_skillcap_register(args: argparse.Namespace) -> int:
    """``qai policy skill-cap register <json>``."""

    from qai.security.domain.skill_capability import SkillCapability

    raw = _parse_json_arg(args.json, name="payload")
    if not isinstance(raw, dict):
        sys.stderr.write("payload must be a JSON object\n")
        return 2
    skill_name = raw.get("skill_name")
    if not isinstance(skill_name, str) or not skill_name:
        sys.stderr.write("skill_name must be a non-empty string\n")
        return 2
    cap_raw = raw.get("capability") or {}
    if not isinstance(cap_raw, dict):
        sys.stderr.write("capability must be a JSON object\n")
        return 2
    try:
        capability = SkillCapability(
            capability_name=str(cap_raw.get("capability_name") or skill_name),
            read_paths=tuple(cap_raw.get("read_paths") or ()),
            write_paths=tuple(cap_raw.get("write_paths") or ()),
            exec_paths=tuple(cap_raw.get("exec_paths") or ()),
            trusted_binaries=tuple(cap_raw.get("trusted_binaries") or ()),
            description=str(cap_raw.get("description") or ""),
            sha256_pins=tuple(
                tuple(p) for p in (cap_raw.get("sha256_pins") or ())
            ),
        )
    except (ValueError, TypeError) as exc:
        sys.stderr.write(f"invalid capability: {exc}\n")
        return 2
    skill_body = str(raw.get("skill_body") or "")

    async def _go(c: Container) -> dict[str, Any]:
        result = await c.security.register_skill_capability_use_case.execute(
            skill_name=skill_name,
            capability=capability,
            skill_body=skill_body,
        )
        return {
            "skill_name": result.skill_name,
            "audit_id": result.audit_id,
            "scanner_warnings": list(result.scanner_warnings),
        }

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0


def cmd_skillcap_unregister(args: argparse.Namespace) -> int:
    """``qai policy skill-cap unregister <name>``."""

    async def _go(c: Container) -> dict[str, Any]:
        audit_id = (
            await c.security.unregister_skill_capability_use_case.execute(
                skill_name=args.name
            )
        )
        return {"skill_name": args.name, "audit_id": audit_id}

    _emit_json(run_use_case(_go, **_runtime_kwargs(args)))
    return 0

