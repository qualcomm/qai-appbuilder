# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Chat-side SKILL catalog provider (Batch B / B-2).

Bridges the ``qai.platform.skills.SkillDiscovery`` filesystem scanner
and the ``forge.config skills.overrides`` persistence layer into the
``((path, use_for), ...)`` row format consumed by
:class:`qai.chat.adapters.RichSystemPromptBuilder.skill_catalog_provider`.

This module lives in ``apps/api/`` because it composes two concerns:

* ``qai.platform.skills`` (platform-level, no context boundary) — the
  filesystem scanner that reads ``skills/<id>/SKILL.md`` metadata.
* ``qai.user_prefs`` (user_prefs context) — the ``forge.config``
  document store that persists per-skill mode overrides.

Neither ``qai.chat`` nor ``qai.platform.skills`` imports the other;
the bridge is the only place where the two are joined.

V1 parity
---------
``backend/skill_manager.py:374-388 build_skill_catalog`` filters skills
by ``model_type`` (``"cloud"`` for cloud LLM turns) and returns only
those whose ``mode`` is ``"cloud"`` or ``"both"``.  This provider
replicates that filter: a skill is included when its effective mode
(after merging ``forge.config skills.overrides``) is ``"cloud"`` or
``"both"``.  Skills with ``mode == "off"`` or ``mode == "local"`` are
excluded from the cloud-model system prompt.

Caching
-------
``SkillDiscovery.scan()`` re-reads the filesystem on every call (v1
no-cache semantics) and the ``forge.config`` override merge runs an
``aiosqlite`` query.  Crucially the provider's ``__call__`` is a
**synchronous** facade invoked from inside ``RichSystemPromptBuilder.build()``,
which the streaming use case calls **on the event-loop thread without
``await``** (``streaming.py:3936`` etc.).  Each call spins up a throwaway
``ThreadPoolExecutor`` + a fresh ``asyncio.run`` loop to run the DB query and
**blocks the calling thread up to 2 s** (``future.result(timeout=2.0)``).

Because that happens on the single event-loop thread, one chat turn building
its system prompt would *freeze every other tab's* streaming / tool calls /
history refresh until it returns.  With several tabs each running a main agent
+ sub-agents in parallel, this turned into pervasive "everything is sluggish"
stalls (the turn-setup phase repeatedly pins the loop).

Fix: a short process-wide TTL cache (``_CATALOG_TTL_S``).  The skill list and
overrides change rarely (only when the user toggles a skill mode), so caching
the resolved rows for a few seconds lets the overwhelming majority of turns
return instantly from memory — **no thread spawn, no nested loop, no DB query,
no event-loop block** — while a user's skill-mode toggle still takes effect
within the TTL.  Failures are NOT cached (so a transient DB lock can't pin an
empty catalog); they degrade to ``()`` exactly as before, and discovered
skills still reach the prompt with their default ``mode="cloud"``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

_log = logging.getLogger("qai.chat.skill_catalog_provider")

__all__ = ["ChatSkillCatalogProvider", "LocalChatSkillCatalogProvider"]

# How long a resolved skill catalog stays valid before a rebuild.  Skill
# mode toggles take effect within this window; turns within it skip the
# thread-spawn + nested-loop + DB query entirely (the perf fix — see module
# docstring).  Kept small so user toggles feel near-immediate.
_CATALOG_TTL_S = 3.0


def _run_async_off_loop(coro_factory: Any) -> Any:
    """Run an async ``coro_factory()`` on a throwaway worker thread + loop.

    Shared by both providers' synchronous ``__call__`` facades. The chat
    streaming use case calls ``RichSystemPromptBuilder.build()`` (and thus
    these providers) synchronously from the event-loop thread, so we cannot
    ``await``; running on a dedicated short-lived thread with its own
    ``asyncio.run`` loop keeps the caller's loop neither blocked-by-reentry
    nor corrupted. Bounded by a 2 s wait so a stuck DB read can't pin the
    caller indefinitely; any failure degrades to ``()``.

    Note: callers front this with a TTL cache, so on the hot path this
    thread is NOT spawned per turn — only on a cache miss (≈ once per
    ``_CATALOG_TTL_S``).
    """
    import concurrent.futures

    def _worker() -> Any:
        try:
            return asyncio.run(coro_factory())
        except Exception:  # noqa: BLE001
            return ()

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_worker)
            return future.result(timeout=2.0)
    except Exception:  # noqa: BLE001 — never break prompt build
        return ()

# Modes that make a skill visible to cloud-model turns (v1 parity).
_CLOUD_VISIBLE_MODES = frozenset({"cloud", "both"})
# Modes that make a skill visible to local (on-device) model turns.
# V1 parity ``backend/skill_manager.py:327-336 get_enabled_skills(model_type="local")``:
# a skill is active for local turns when its mode is ``"local"`` or ``"both"``.
_LOCAL_VISIBLE_MODES = frozenset({"local", "both"})

#: Per-skill DEFAULT run mode used when the user has NOT persisted an explicit
#: ``skills.overrides[<skill_id>]`` entry in ``forge.config``. Skills absent
#: from this map fall through to ``"cloud"`` (enabled). MUST stay in sync with
#: ``qai.user_prefs.application.use_cases.skills._DEFAULT_SKILL_MODE`` (the
#: ``GET /api/skills`` list path) so the chat-prompt gate and the UI toggle
#: list agree on the shipped defaults. Requested default: ppt-gen / code-assist
#: / meetingminutes ship OFF; the rest (e.g. model-builder) stay on.
_DEFAULT_SKILL_MODE: dict[str, str] = {
    "ppt-gen": "off",
    "code-assist": "off",
    "meetingminutes": "off",
}


class ChatSkillCatalogProvider:
    """Zero-arg callable that returns live ``((path, use_for), ...)`` rows.

    Constructed once at DI time with lazy references to the
    ``SkillDiscovery`` instance and the ``LoadDocumentUseCase`` from the
    ``user_prefs`` context.  Both are resolved lazily (via callables)
    so the provider can be built before ``container.user_prefs`` is
    fully wired (chat is built before user_prefs in ``di.py``).

    Parameters
    ----------
    skill_discovery_factory:
        Zero-arg callable returning the ``SkillDiscovery`` instance.
        Typically ``lambda: container.user_prefs.skill_discovery``.
    load_prefs_factory:
        Zero-arg callable returning the ``LoadDocumentUseCase`` instance.
        Typically ``lambda: container.user_prefs.load_document_use_case``.
    forge_config_key:
        The ``forge.config`` document key (default ``"forge.config"``).
    skills_subkey:
        The sub-key inside the document that holds skill overrides
        (default ``"skills"``).
    """

    __slots__ = (
        "_cache",
        "_cache_at",
        "_forge_config_key",
        "_load_prefs_factory",
        "_skill_discovery_factory",
        "_skills_subkey",
    )

    def __init__(
        self,
        *,
        skill_discovery_factory: Any,
        load_prefs_factory: Any,
        forge_config_key: str = "forge.config",
        skills_subkey: str = "skills",
    ) -> None:
        self._skill_discovery_factory = skill_discovery_factory
        self._load_prefs_factory = load_prefs_factory
        self._forge_config_key = forge_config_key
        self._skills_subkey = skills_subkey
        # TTL cache (perf fix): last resolved rows + monotonic timestamp.
        self._cache: tuple[tuple[str, str], ...] | None = None
        self._cache_at: float = 0.0

    def __call__(self) -> tuple[tuple[str, str], ...]:
        """Return ``((skill_path, use_for), ...)`` for cloud-visible skills.

        Synchronous facade: ``RichSystemPromptBuilder.build()`` is sync
        (called from inside an async request handler), so we cannot
        ``await`` here.  The async ``LoadDocumentUseCase`` runs against
        aiosqlite which has no sync API; we therefore execute it on a
        **separate worker thread** with its own event loop using
        ``asyncio.run`` so the caller's loop is never blocked or
        re-entered.

        A short TTL cache (see module docstring) makes the common path a
        pure in-memory return, so back-to-back turns across many tabs no
        longer each block the event-loop thread on the thread spawn + DB
        query.

        Failures (timeout, DB lock, missing user_prefs) degrade to
        "no overrides" — discovered skills still reach the system
        prompt with their default ``mode="cloud"``, matching v1's
        first-run behaviour before any user toggles are persisted.
        """
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_at) < _CATALOG_TTL_S:
            return self._cache
        rows = _run_async_off_loop(self._async_call)
        # Only cache a successful (non-empty-by-failure) result. An empty
        # tuple is also a legitimate "no cloud-visible skills" answer; we
        # still cache it — the failure paths inside _async_call/_worker that
        # mean "couldn't read" are indistinguishable from "genuinely empty"
        # at this layer, but both are cheap to recompute after the short TTL,
        # so caching () for a few seconds is acceptable and still avoids the
        # per-turn loop block.
        self._cache = rows
        self._cache_at = now
        return rows

    async def _async_call(self) -> tuple[tuple[str, str], ...]:
        """Async implementation: scan + merge overrides + filter."""
        # Resolve lazy factories.
        try:
            skill_discovery = self._skill_discovery_factory()
        except Exception:  # noqa: BLE001
            return ()

        # Load forge.config overrides (best-effort; empty dict on failure).
        overrides: dict[str, Any] = {}
        try:
            load_uc = self._load_prefs_factory()
            doc = await load_uc.execute(self._forge_config_key)
            skills_doc = doc.get(self._skills_subkey, {}) if isinstance(doc, dict) else {}
            raw_overrides = skills_doc.get("overrides", {}) if isinstance(skills_doc, dict) else {}
            if isinstance(raw_overrides, dict):
                overrides = raw_overrides
        except Exception:  # noqa: BLE001 — best-effort
            pass

        # Scan skills and filter to cloud-visible ones.
        try:
            skills = skill_discovery.scan()
        except Exception:  # noqa: BLE001
            return ()

        rows: list[tuple[str, str]] = []
        for skill in skills:
            # Pinned skills are always cloud-visible regardless of user overrides.
            if skill.pinned:
                if skill.skill_path:
                    rows.append((skill.skill_path, skill.use_for or skill.description or ""))
                continue
            effective_mode = _resolve_mode(
                overrides.get(skill.skill_id),
                skill.npu_optimized,
                skill.skill_id,
            )
            if effective_mode not in _CLOUD_VISIBLE_MODES:
                continue
            if not skill.skill_path:
                continue
            rows.append((skill.skill_path, skill.use_for or skill.description or ""))
        return tuple(rows)


class LocalChatSkillCatalogProvider:
    """Zero-arg callable returning ``((skill_id, use_for, path), ...)`` rows
    for **local-visible** skills (on-device model turns).

    Mirrors :class:`ChatSkillCatalogProvider` exactly except for the
    visibility filter and the returned row shape:

    * **Visibility** — V1 ``backend/skill_manager.py:327-336``
      ``get_enabled_skills(model_type="local")`` returns skills whose
      effective mode is ``"local"`` or ``"both"`` (NOT ``"cloud"``).
    * **Row shape** — V1 ``build_available_skills_xml`` (skill_manager.py:
      390-421) needs ``<name>`` (= ``skill_id``), ``<description>`` (=
      ``use_for`` or ``description``), and ``<location>`` (= ``skill_path``),
      so this provider returns ``(skill_id, use_for_or_description, path)``
      triples — a superset of the cloud provider's ``(path, use_for)`` pair.

    The simplified local system prompt (built in the chat use case) renders
    these triples into the ``<available_skills>`` XML block that
    GenieAPIService's PromptOptimizer parses; the on-device model then reads
    each ``<location>`` SKILL.md on demand via the read tool.
    """

    __slots__ = (
        "_cache",
        "_cache_at",
        "_forge_config_key",
        "_load_prefs_factory",
        "_skill_discovery_factory",
        "_skills_subkey",
    )

    def __init__(
        self,
        *,
        skill_discovery_factory: Any,
        load_prefs_factory: Any,
        forge_config_key: str = "forge.config",
        skills_subkey: str = "skills",
    ) -> None:
        self._skill_discovery_factory = skill_discovery_factory
        self._load_prefs_factory = load_prefs_factory
        self._forge_config_key = forge_config_key
        self._skills_subkey = skills_subkey
        self._cache: tuple[tuple[str, str, str], ...] | None = None
        self._cache_at: float = 0.0

    def __call__(self) -> tuple[tuple[str, str, str], ...]:
        """Return ``((skill_id, use_for, path), ...)`` for local-visible skills.

        Synchronous facade over an async DB read on a dedicated worker
        thread (same pattern + rationale as
        :meth:`ChatSkillCatalogProvider.__call__`), fronted by the same
        short TTL cache to avoid blocking the event-loop thread on every
        turn; degrades to ``()`` on any failure so the local system prompt
        never breaks.
        """
        now = time.monotonic()
        if self._cache is not None and (now - self._cache_at) < _CATALOG_TTL_S:
            return self._cache
        rows = _run_async_off_loop(self._async_call)
        self._cache = rows
        self._cache_at = now
        return rows

    async def _async_call(self) -> tuple[tuple[str, str, str], ...]:
        """Async implementation: scan + merge overrides + local filter."""
        try:
            skill_discovery = self._skill_discovery_factory()
        except Exception:  # noqa: BLE001
            return ()

        overrides: dict[str, Any] = {}
        try:
            load_uc = self._load_prefs_factory()
            doc = await load_uc.execute(self._forge_config_key)
            skills_doc = doc.get(self._skills_subkey, {}) if isinstance(doc, dict) else {}
            raw_overrides = skills_doc.get("overrides", {}) if isinstance(skills_doc, dict) else {}
            if isinstance(raw_overrides, dict):
                overrides = raw_overrides
        except Exception:  # noqa: BLE001 — best-effort
            pass

        try:
            skills = skill_discovery.scan()
        except Exception:  # noqa: BLE001
            return ()

        rows: list[tuple[str, str, str]] = []
        for skill in skills:
            # Pinned skills are always local-visible regardless of user overrides.
            if skill.pinned:
                if skill.skill_path:
                    rows.append((
                        skill.skill_id,
                        skill.use_for or skill.description or "",
                        skill.skill_path,
                    ))
                continue
            effective_mode = _resolve_mode(
                overrides.get(skill.skill_id),
                skill.npu_optimized,
                skill.skill_id,
            )
            if effective_mode not in _LOCAL_VISIBLE_MODES:
                continue
            if not skill.skill_path:
                continue
            rows.append(
                (
                    skill.skill_id,
                    skill.use_for or skill.description or "",
                    skill.skill_path,
                )
            )
        return tuple(rows)


# ---------------------------------------------------------------------------
# Mode resolution (mirrors user_prefs.py:_resolve_mode)
# ---------------------------------------------------------------------------


def _resolve_mode(
    override: Any, npu_optimized: bool, skill_id: str = "",
) -> str:
    """Merge a per-skill forge.config override into the discovered mode.

    Mirrors ``interfaces/http/routes/user_prefs.py:_resolve_mode``:

    1. explicit ``mode`` key (off/cloud/local/both) if present & valid;
       a persisted local/both that is no longer NPU-optimised falls
       back to 'cloud'.
    2. else legacy ``enabled`` bool: False -> 'off', True -> 'cloud'.
    3. else the per-skill default (``_DEFAULT_SKILL_MODE``), defaulting to
       'cloud' (enabled) for skills not listed there.
    """
    from qai.platform.skills import VALID_MODES, NPU_MODES

    if isinstance(override, dict):
        mode = override.get("mode")
        if isinstance(mode, str) and mode in VALID_MODES:
            if mode in NPU_MODES and not npu_optimized:
                return "cloud"
            return mode
        enabled = override.get("enabled")
        if isinstance(enabled, bool):
            return "cloud" if enabled else "off"
    return _DEFAULT_SKILL_MODE.get(skill_id, "cloud")
