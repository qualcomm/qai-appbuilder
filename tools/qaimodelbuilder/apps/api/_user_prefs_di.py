# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``user_prefs`` bounded context (PR-601a/b).

S7.5 lane L6 introduces this BC from scratch: PR-601a ships the
backbone (``LoadDocumentUseCase`` + ``SaveDocumentUseCase`` over a
:class:`KvUserPrefsRepository`); PR-601b adds namespace-specific
endpoints (proxy, code-personas, settings/*) on top of the same two
use cases plus a :class:`SecretStore` for proxy credential storage.

Field-name lock (v2.7 §3.1)
---------------------------
Once :class:`UserPrefsServices` is wired into ``Container.user_prefs``
its existing field names are part of the public namespace contract:
they may only be **tail-appended** by future PRs, never renamed or
removed.  PR-601a creates the namespace clean, so any field present
in this file from this PR onward is locked.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from qai.platform.config import LOOPBACK_HOST
from qai.platform.skills import SkillDiscovery
from qai.user_prefs.adapters import KvUserPrefsRepository
from qai.user_prefs.application.ports import UserPrefsRepositoryPort
from qai.user_prefs.application.use_cases import (
    GetProxyUseCase,
    GetSkillPolicyUseCase,
    ListSkillsUseCase,
    LoadDocumentUseCase,
    LoadForgeConfigUseCase,
    ReloadSkillsUseCase,
    SaveDocumentUseCase,
    SaveProxyUseCase,
    SetSkillModeUseCase,
    SetSkillPolicyModeUseCase,
    ToggleSkillUseCase,
)

if TYPE_CHECKING:  # pragma: no cover
    from qai.platform.persistence.secrets import SecretStore

    from .di import Container


__all__ = [
    "FORGE_CONFIG_KEY",
    "UserPrefsServices",
    "build_user_prefs_services",
]


# ---------------------------------------------------------------------------
# Stable KV namespace key for the forge-config document (shared with the
# route layer's contract; defined here so the use cases can be wired with it
# at construction time).
# ---------------------------------------------------------------------------
FORGE_CONFIG_KEY = "forge.config"

# SecretStore namespace for the proxy password (AGENTS.md §3.3).
_PROXY_SECRET_SERVICE = "qai.network.proxy"  # noqa: S105 — keyring SERVICE name, not a secret
_PROXY_SECRET_KEY = "proxy_password"  # noqa: S105 — keyring KEY name, not a secret value
_PROXY_MASK = "****"


def _resolve_gomaster_mode(container: object) -> str:
    """Resolve the GoMaster link mode from edition config ("external" default).

    Internal-only + edition-excluded: the ``get_query_services`` import is local
    and guarded so a stripped external tree never crashes, and returns the safe
    "external" default when not internal / not configured.
    """
    settings = getattr(container, "settings", None)
    if settings is None or not getattr(settings, "is_internal", False):
        return "external"
    try:
        from qai.platform.edition import get_query_services

        fields = get_query_services().get("gomaster") or {}
        mode = str(fields.get("gomaster_mode", "external")).lower()
        return mode if mode in ("external", "agent", "both") else "external"
    except Exception:  # pragma: no cover — excluded on external
        return "external"


@dataclass(slots=True)
class UserPrefsServices:
    """Application services / ports for the ``user_prefs`` namespace.

    Fields are positional-arguments-equivalent in the constructor;
    Container.user_prefs is built once at startup.  Routes consume
    only the use cases — the bare ``repository`` is kept on the
    namespace so debug/inspection routes that need a non-merged dump
    (e.g. ``GET /api/user_prefs/_debug``) can read it directly
    without re-instantiating an adapter.

    PR-601b appends ``secret_store`` for proxy password storage.
    """

    repository: UserPrefsRepositoryPort
    load_document_use_case: LoadDocumentUseCase
    save_document_use_case: SaveDocumentUseCase
    secret_store: SecretStore
    # Tail-appended (skills business registry): scans ``<repo_root>/skills``
    # and ``<repo_root>/factory/chat_features`` for SKILL.md / skill.json
    # metadata + NPU detection so the ``GET /api/skills`` route can return
    # the v1-shaped skill list and serve per-skill icons. Mode persistence
    # stays in this BC's ``forge.config`` document; discovery is read-only
    # filesystem access.
    skill_discovery: SkillDiscovery
    # Tail-appended (R4/R5/R6 route-thinning): use cases that own the
    # read-modify-write transactions + default injection + cross-document
    # pill derivation + skill scan/mode-resolve/NPU validation previously
    # inlined in the route layer (Clean Architecture: interfaces stays thin).
    load_forge_config_use_case: LoadForgeConfigUseCase
    get_proxy_use_case: GetProxyUseCase
    save_proxy_use_case: SaveProxyUseCase
    get_skill_policy_use_case: GetSkillPolicyUseCase
    set_skill_policy_mode_use_case: SetSkillPolicyModeUseCase
    toggle_skill_use_case: ToggleSkillUseCase
    reload_skills_use_case: ReloadSkillsUseCase
    list_skills_use_case: ListSkillsUseCase
    set_skill_mode_use_case: SetSkillModeUseCase


def build_user_prefs_services(container: Container) -> UserPrefsServices:
    """Wire the user_prefs namespace against the container's database.

    Uses ``container.database`` for KV persistence and
    ``container.secret_store`` for proxy password credential storage
    (per AGENTS.md §3.3).
    """
    repo = KvUserPrefsRepository(db=container.database)
    load_uc = LoadDocumentUseCase(repository=repo)
    save_uc = SaveDocumentUseCase(repository=repo)
    skill_discovery = SkillDiscovery(_resolve_skills_dirs(container))
    # ``security.bind_host`` is the loopback bind default for an INTERNAL
    # forge-config service — it must stay a secure loopback (V1 parity:
    # always ``127.0.0.1``) and must NOT follow the public ``server.host``,
    # which may be ``0.0.0.0`` (binding to all interfaces). We inject the
    # allow-listed ``LOOPBACK_HOST`` platform constant (defined in
    # settings.py — the only file the magic-host guard exempts) so the
    # secure default is preserved without a hard-coded literal here.
    bind_host = LOOPBACK_HOST

    # Closure factories fold away the boilerplate that every use case
    # repeats: ``load_document_use_case`` (+ ``forge_config_key``) for the
    # load-only family, and additionally ``save_document_use_case`` for the
    # read-modify-write family.  Each factory forwards any extra keyword
    # arguments verbatim, so the constructed graph is byte-for-byte the same
    # as the previous fully-inlined wiring (architecture cleanup only — zero
    # behaviour change).
    def _ld(factory, **extra):  # type: ignore[no-untyped-def]
        return factory(
            load_document_use_case=load_uc,
            forge_config_key=FORGE_CONFIG_KEY,
            **extra,
        )

    def _lds(factory, **extra):  # type: ignore[no-untyped-def]
        return factory(
            load_document_use_case=load_uc,
            save_document_use_case=save_uc,
            forge_config_key=FORGE_CONFIG_KEY,
            **extra,
        )

    return UserPrefsServices(
        repository=repo,
        load_document_use_case=load_uc,
        save_document_use_case=save_uc,
        secret_store=container.secret_store,
        skill_discovery=skill_discovery,
        load_forge_config_use_case=_ld(
            LoadForgeConfigUseCase,
            bind_host=bind_host,
            # internal-only edition gate for the「Pro / 增强」toolbar module
            # (mb-pro-integration-plan.md §6/§7 layer ①). When the build is
            # internal, the use case injects the ``pro`` toolbar module so the
            # button renders; external builds leave it absent.
            is_internal=bool(
                getattr(getattr(container, "settings", None), "is_internal", False)
            ),
            # Which GoMaster link is wired (edition config; "external" default).
            # Surfaced on the gomaster toolbar module so the composer skips the
            # ``query::gomaster`` chat route in external mode (one-click optimize,
            # not a conversation) and the empty-state shows the GoMaster intro.
            gomaster_mode=_resolve_gomaster_mode(container),
        ),
        get_proxy_use_case=_ld(
            GetProxyUseCase,
            secret_store=container.secret_store,
            secret_service=_PROXY_SECRET_SERVICE,
            secret_key=_PROXY_SECRET_KEY,
            mask=_PROXY_MASK,
        ),
        save_proxy_use_case=_lds(
            SaveProxyUseCase,
            secret_store=container.secret_store,
            secret_service=_PROXY_SECRET_SERVICE,
            secret_key=_PROXY_SECRET_KEY,
            mask=_PROXY_MASK,
        ),
        get_skill_policy_use_case=_ld(GetSkillPolicyUseCase),
        set_skill_policy_mode_use_case=_lds(SetSkillPolicyModeUseCase),
        toggle_skill_use_case=_lds(
            ToggleSkillUseCase, skill_discovery=skill_discovery
        ),
        reload_skills_use_case=_lds(ReloadSkillsUseCase),
        list_skills_use_case=_ld(
            ListSkillsUseCase, skill_discovery=skill_discovery
        ),
        set_skill_mode_use_case=_lds(
            SetSkillModeUseCase, skill_discovery=skill_discovery
        ),
    )


def _resolve_skills_dirs(container: Container) -> list[Path]:
    """Resolve all skill directories to scan.

    Returns a list of directories in priority order:
    1. ``<repo_root>/skills``              — user-installed skills (highest priority)
    2. ``<repo_root>/factory/chat_features`` — built-in chat-feature skill packs

    ``factory/chat_features/app-builder`` is now a standard subdirectory of
    ``factory/chat_features`` and is discovered by :class:`SkillDiscovery`
    (which scans immediate subdirs) alongside model-builder, model-hub, etc.
    Its internal ``_template/`` and ``models/<id>/`` subdirs are NOT surfaced
    because ``SkillDiscovery`` only iterates first-level children.

    Production wiring reads ``container.repo_root`` (the repository root
    resolved in :func:`apps.api.main.create_app`). The defensive
    ``getattr`` covers hand-rolled test containers that build a
    stripped-down namespace without ``repo_root`` (e.g. the user_prefs
    integration conftest); those fall back to the real repo root derived
    from this module's location (``apps/api/_user_prefs_di.py`` is two
    levels below the repo root).
    """
    repo_root = getattr(container, "repo_root", None)
    if not isinstance(repo_root, Path):
        repo_root = Path(__file__).resolve().parents[2]
    return [
        repo_root / "skills",
        repo_root / "factory" / "chat_features",
    ]
