# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Chat-side ``skill`` tool lookup adapter.

Bridges the ``qai.platform.skills.SkillDiscovery`` filesystem scanner into the
``{skill_id: skill_md_path}`` mapping consumed by
:class:`qai.chat.adapters.harness_tools.SkillToolHandler`.

This module lives in ``apps/api/`` for the same reason as
``_chat_skill_catalog_provider``: it joins the platform-level skill scanner
with the chat tool surface without ``qai.chat`` importing the discovery
machinery directly. ``SkillToolHandler`` only consumes the injected
``{skill_id: path}`` mapping (a plain dict) — it never touches
``qai.platform.skills`` — so it stays trivially testable with a fake mapping.

Lazy resolution: ``container.user_prefs`` is wired *after* ``chat`` in
``apps/api/di.py``, so the ``SkillDiscovery`` is resolved via a zero-arg factory
on every call (the lookup callable is invoked per tool call, not at DI time).

The lookup intentionally returns ALL discovered skills' SKILL.md paths (it does
NOT apply the ``forge.config skills.overrides`` cloud/local mode filter that the
system-prompt catalog provider applies). Rationale: the ``skill`` tool is an
explicit, model-driven pull of a named skill the model already saw advertised;
gating the *tool's* lookup on the prompt-catalog visibility filter would be a
second, duplicate policy surface. The catalog provider (what the model is told
about) remains the single place visibility is decided; this lookup just resolves
a name the model chose to load. Failures degrade to an empty mapping so the
``skill`` tool returns a stable "no skills available" result rather than
crashing the turn.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, Callable

_log = logging.getLogger("qai.chat.skill_tool")

__all__ = ["build_skill_tool_lookup"]


def build_skill_tool_lookup(
    skill_discovery_factory: Callable[[], Any],
) -> Callable[[], Mapping[str, str]]:
    """Return a zero-arg ``skill_lookup`` for :class:`SkillToolHandler`.

    Parameters
    ----------
    skill_discovery_factory:
        Zero-arg callable returning the ``SkillDiscovery`` instance, e.g.
        ``lambda: container.user_prefs.skill_discovery``. Resolved lazily on
        every call so it is safe to build this before ``user_prefs`` is wired.

    Returns
    -------
    A zero-arg callable returning ``{skill_id: skill_md_path}`` for every
    discovered skill that has a resolvable SKILL.md path. Any failure
    (user_prefs not wired yet, scan error) degrades to an empty mapping.
    """

    def _lookup() -> Mapping[str, str]:
        try:
            discovery = skill_discovery_factory()
        except Exception as exc:  # noqa: BLE001 — best-effort; never break turn
            _log.debug("chat.skill_tool.discovery_unavailable: %s", exc)
            return {}
        try:
            skills = discovery.scan()
        except Exception as exc:  # noqa: BLE001 — best-effort
            _log.warning("chat.skill_tool.scan_failed: %s", exc)
            return {}

        mapping: dict[str, str] = {}
        for skill in skills:
            skill_id = getattr(skill, "skill_id", "")
            skill_path = getattr(skill, "skill_path", "")
            if skill_id and skill_path:
                mapping[skill_id] = skill_path
        return mapping

    return _lookup
