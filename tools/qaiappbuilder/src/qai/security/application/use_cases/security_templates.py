# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apply built-in security policy template use case (R7 cohesion fix).

Previously the built-in ``demo`` / ``development`` / ``strict`` rule sets
and their ``PolicyRule`` construction lived inline in
``interfaces/http/routes/security.py`` (``_template_rules`` +
``apply_security_template``). That meant the route layer owned domain-rule
construction — a Clean-Architecture leak flagged by
``scripts/ci/check_route_thinness.py``.

This module moves the template *data* + the domain-entity construction +
the "look up template id → apply via UpdatePolicyUseCase" orchestration
into the application layer. The route handler now only translates the
result into its wire DTO.

V1 parity: template ids and rule types are byte-for-byte identical to
the inline definitions they replaced. The 2026-07 rewrite tightened
each template's rule set (removing the ``*`` catch-all deny that
silently suppressed the ASK dialog); ``rules_count`` advertised by
``GET /api/security/templates`` is now ``1 / 2 / 0`` (demo /
development / strict) and continues to equal ``len(rules)`` here.
"""

from __future__ import annotations

from qai.security.domain.entities import Policy, PolicyRule
from qai.security.domain.value_objects import (
    PathPattern,
    PolicyAction,
    PolicyOp,
    PolicyScope,
)

from .update_policy import UpdatePolicyUseCase

__all__ = [
    "ApplySecurityTemplateUseCase",
    "template_catalog",
    "template_rules",
]


# ---------------------------------------------------------------------------
# Built-in policy templates (V1 parity: demo / development / strict).
# Template ids match V1 config/policy_templates/{id}.json filenames.
# Each template id maps to a concrete rule set that
# ``POST /api/security/templates`` writes through UpdatePolicyUseCase.
#
# These are the shipped, out-of-the-box templates (their out-of-box source
# also lives at ``factory/_source/policy_templates/{id}.json``). V1 read the
# same fixed set from ``config/policy_templates/`` at runtime and shipped no
# template-authoring UI, so the catalog is effectively a built-in set in both
# versions; V2 keeps it as a single in-code source of truth so the catalog
# advertised by ``GET /templates`` can never drift from the rule sets
# ``POST /templates`` actually applies.
# ---------------------------------------------------------------------------


# Catalog metadata for the built-in templates, in display order. The
# ``rules_count`` is intentionally NOT stored here — it is derived from
# ``template_rules()`` so the count advertised by ``GET /templates`` always
# equals what ``POST /templates`` applies (no drift).
_TEMPLATE_META: tuple[tuple[str, str, str], ...] = (
    (
        "demo",
        "demo",
        "Read-only mode with no dynamic authorization popups. AI can only "
        "read files. Best for demos and presentations.",
    ),
    (
        "development",
        "development",
        "Project files can be read without confirmation. Write and execute "
        "operations still require approval. Good for active development.",
    ),
    (
        "strict",
        "strict",
        "All file and command operations require explicit user approval. "
        "Best for high-security environments.",
    ),
)


def template_catalog() -> list[dict[str, object]]:
    """Return the built-in template catalog for ``GET /templates``.

    Each entry's ``rules_count`` is derived from :func:`template_rules`
    (the single source of truth that ``POST /templates`` applies), so the
    advertised catalog can never drift from the rule sets actually written.
    """
    catalog: list[dict[str, object]] = []
    for template_id, name, description in _TEMPLATE_META:
        rules = template_rules(template_id) or ()
        catalog.append(
            {
                "id": template_id,
                "name": name,
                "description": description,
                "rules_count": len(rules),
            }
        )
    return catalog


def template_rules(template_id: str) -> tuple[PolicyRule, ...] | None:
    """Return the rule set for a built-in template, or ``None`` if unknown.

    Design principle (2026-07 rewrite) — **no ``*`` catch-all deny**.
    Before this rewrite each template terminated with a ``*`` deny rule
    which turned every out-of-tree access into a HARD ``rule_id``-hit DENY
    (``would_ask=False``), silently suppressing the ASK dialog. That drove
    the "since 07-25 弹窗突然不工作了" regression: applying any template
    disabled the ``dynamic_authorization`` prompt path for everything that
    wasn't inside ``${PROJECT_ROOT}``.

    The three templates now shape access via **allow** rules for the
    surfaces they explicitly trust, and let anything else fall through to
    the standard cascade (workspace → skill capability → auto-approve →
    grants → miss). When ``dynamic_authorization`` is on (default), a
    miss pops the authorization dialog exactly as V1 did. When it's off,
    a miss is a fail-closed DENY — but that DENY is now the operator's
    explicit "always block silently" choice, not an unintended template
    side effect.

    Ordering of severity (matches display order in Overview):

    * **demo** — read-only over the project root. Nothing writable
      pre-declared; every write / execute anywhere prompts. Intended for
      quick demos where the AI reads code but shouldn't touch it.
    * **development** — project root readable AND writable, plus
      ``${TEMP}`` writable for scratch output. Anything else prompts.
      The default recommendation for active project work.
    * **strict** — no rules at all. Everything routes through the
      workspace / skill / auto-approve cascade, and misses prompt.
      This is the "ask me everything I haven't explicitly authorised"
      posture; deliberately empty because the ambient guards
      (workspace_allow / skill_capability / auto_approve /
      ${'{'}file_guard_paths.json{'}'}) already cover the common
      day-to-day operations for a strict setup — the template's job is
      to NOT layer additional blanket allow rules on top.
    """
    # "demo" — read-only project root; write/exec falls through to ASK.
    if template_id == "demo":
        return (
            PolicyRule(
                rule_id="tpl-demo-allow-project-read",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="${PROJECT_ROOT}/*"),
                action=PolicyAction("allow"),
                op=PolicyOp("read"),
                description=(
                    "Allow READING project root without prompting "
                    "(demo template). Write / execute anywhere still "
                    "prompts."
                ),
            ),
        )
    # "development" — project readable+writable, temp writable, everything
    # else prompts. Intended default for AI-assisted development.
    if template_id == "development":
        return (
            PolicyRule(
                rule_id="tpl-dev-allow-project",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="${PROJECT_ROOT}/*"),
                action=PolicyAction("allow"),
                op=PolicyOp("write"),
                description=(
                    "Allow read+write inside the project root without "
                    "prompting (development template). ``PolicyOp.WRITE`` "
                    "covers both read and write per the V1 4-list "
                    "taxonomy (write implies read)."
                ),
            ),
            PolicyRule(
                rule_id="tpl-dev-allow-temp",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="${TEMP}/*"),
                action=PolicyAction("allow"),
                op=PolicyOp("write"),
                description=(
                    "Allow read+write inside the OS temp directory "
                    "(development template)."
                ),
            ),
        )
    # "strict" — no template rules. The ambient allow cascade (workspace
    # subtree, skill capabilities, auto-approve, file_guard_paths) still
    # covers day-to-day surfaces; anything else routes through
    # ``dynamic_authorization`` → dialog. Empty tuple is intentional:
    # this is a real, valid template state (a "reset to prompts-only"
    # posture), NOT the same as "template not found" (which returns
    # ``None`` at line ~end below).
    if template_id == "strict":
        return ()
    return None


class ApplySecurityTemplateUseCase:
    """Apply a built-in policy template by id.

    Resolves the template id to its rule set and writes it through the
    locked :class:`UpdatePolicyUseCase` — the rules CRUD contract is
    reused, not bypassed, so a template apply triggers the same version
    bump + reboot-signal path as any manual rule edit.

    Returns ``None`` when the template id is unknown so the route layer
    can raise its own :class:`NotFoundError` (keeping HTTP concerns out
    of the application layer).
    """

    def __init__(
        self,
        *,
        update_policy_use_case: UpdatePolicyUseCase,
    ) -> None:
        self._update_policy = update_policy_use_case

    async def execute(self, *, template_id: str) -> Policy | None:
        """Apply the template, or return ``None`` for an unknown id."""
        rules = template_rules(template_id)
        if rules is None:
            return None
        return await self._update_policy.execute(
            new_rules=rules,
            reboot_reason=f"applied template {template_id}",
        )
