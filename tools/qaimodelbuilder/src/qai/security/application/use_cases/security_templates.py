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

V1 parity: template ids and their rule sets are byte-for-byte identical to
the inline definitions they replace (which themselves mirror the V1
``config/policy_templates/{id}.json`` files). The ``rules_count`` advertised
by ``GET /api/security/templates`` (2 / 4 / 3) continues to equal
``len(rules)`` here.
"""

from __future__ import annotations

from qai.security.domain.entities import Policy, PolicyRule
from qai.security.domain.value_objects import (
    PathPattern,
    PolicyAction,
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

    Pure factory (no I/O); the rule sets are identical to the legacy
    inline ``_template_rules`` route helper.
    """
    # "demo" — read-only, no dynamic authorization (V1 demo.json)
    if template_id == "demo":
        return (
            PolicyRule(
                rule_id="tpl-demo-allow-project",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="${PROJECT_ROOT}/*"),
                action=PolicyAction("allow"),
                description="Allow project root (demo template)",
            ),
            PolicyRule(
                rule_id="tpl-demo-deny-all",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="*"),
                action=PolicyAction("deny"),
                description="Deny everything else (demo template)",
            ),
        )
    # "development" — allow project + temp, deny system paths (V1 development.json)
    if template_id == "development":
        return (
            PolicyRule(
                rule_id="tpl-dev-allow-project",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="${PROJECT_ROOT}/*"),
                action=PolicyAction("allow"),
                description="Allow project directory (development template)",
            ),
            PolicyRule(
                rule_id="tpl-dev-allow-temp",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="${TEMP}/*"),
                action=PolicyAction("allow"),
                description="Allow temp directory (development template)",
            ),
            PolicyRule(
                rule_id="tpl-dev-deny-system",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="C:/Windows/*"),
                action=PolicyAction("deny"),
                description="Deny system paths (development template)",
            ),
            PolicyRule(
                rule_id="tpl-dev-deny-all",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="*"),
                action=PolicyAction("deny"),
                description="Deny everything else (development template)",
            ),
        )
    # "strict" — all operations require confirmation (V1 strict.json)
    if template_id == "strict":
        return (
            PolicyRule(
                rule_id="tpl-strict-allow-project",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="${PROJECT_ROOT}/*"),
                action=PolicyAction("allow"),
                description="Allow project root (strict template)",
            ),
            PolicyRule(
                rule_id="tpl-strict-deny-system",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="C:/Windows/*"),
                action=PolicyAction("deny"),
                description="Deny system paths (strict template)",
            ),
            PolicyRule(
                rule_id="tpl-strict-deny-all",
                scope=PolicyScope("path"),
                pattern=PathPattern(pattern="*"),
                action=PolicyAction("deny"),
                description="Deny everything else (strict template)",
            ),
        )
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
