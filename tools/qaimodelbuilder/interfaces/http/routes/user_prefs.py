# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""HTTP routes for the ``qai.user_prefs`` bounded context (PR-601a/b/606).

Routes (PR-601a backbone):
--------------------------
* ``GET  /api/forge-config`` — return the persisted forge-config doc
* ``POST /api/forge-config`` — merge & persist updates to forge-config
* ``GET  /api/preferences``  — return persisted UI preferences (``ui.*``)
* ``POST /api/preferences``  — persist a small allow-listed UI prefs set

Routes (PR-601b additions):
---------------------------
* ``GET  /api/proxy``
* ``POST /api/proxy``
* ``GET  /api/code-personas``
* ``POST /api/code-personas/select``
* ``POST /api/code-personas/{persona_id}``
* ``DELETE /api/code-personas/{persona_id}``
* ``DELETE /api/code-personas``

The six ``GET/PUT /api/settings/{dep_broker,exec_broker,process_proxy,
file_broker,project_snapshot,file_watcher}`` KV sections were removed in the
2026-06 security-settings unification: they persisted to ``forge.config`` but
had no backend consumer ("dead settings"). The authoritative security/tools
switches now live on ``GET/PUT /api/security/runtime-config`` (see
``interfaces/http/routes/security/_runtime_config.py``).

Routes (PR-606 — skills toggle/reload/discovery):
--------------------------------------------------
* ``GET  /api/skills/policy``   — return aggregated skill policy state
* ``POST /api/skills/set_mode`` — persist skill mode preference
* ``POST /api/skills/toggle``   — persist per-skill enabled state
* ``POST /api/skills/reload``   — trigger skill discovery reload signal

Routes use one shared :class:`UserPrefsServices` namespace from the
container.  No business logic lives here — the route layer decodes
the inbound JSON, picks the namespace key, and forwards to either
``LoadDocumentUseCase`` or ``SaveDocumentUseCase``.

Keys used by PR-601a (stable; will appear in migration 007's
``kv_user_prefs`` table at runtime):

* ``forge.config``    — every value previously written to
                        ``forge_config.json`` except UI prefs +
                        per-feature settings (split out below)
* ``ui.preferences``  — small allow-listed dict of UI prefs
                        (``selected_model_id`` /
                        ``selected_model_provider`` /
                        ``selected_service_model``)

Additional keys added by PR-601b:

* ``ui.code_personas`` — code persona selection and prompt overrides

Note on the legacy ``/api/config`` endpoint
-------------------------------------------
The legacy ``/api/config`` route exposed the merged
``service_config.json`` (GenieAPIService runtime config + masked
cloud_model API keys + meta).  In the new architecture that data
belongs to the **model_runtime** BC (lane L6 PR-604, the inference
service-control face).  user_prefs deliberately does not own
``/api/config`` — keeping each BC narrow per v2.7 §3.2.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from qai.platform.errors import (
    DomainError,
    NotFoundError,
    ValidationError,
)
from qai.user_prefs.application.use_cases import (
    SkillModeNotAllowedError,
    SkillNotFoundError,
)
from qai.user_prefs.domain.code_personas import (
    DEFAULT_PERSONA_ID,
    MAX_PROMPT_LENGTH,
    CodePersonaManager,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


__all__ = ["build_router"]


# ---------------------------------------------------------------------------
# Stable KV namespace keys
# ---------------------------------------------------------------------------
#: Top-level key for the legacy ``forge_config.json`` document.  We
#: persist its contents verbatim in ``kv_user_prefs`` under this key,
#: minus the UI prefs subsection (which lives under
#: :data:`PREFS_KEY` for clean separation between "config the user
#: edits" vs "UI state remembered across sessions").
FORGE_CONFIG_KEY = "forge.config"

#: Top-level key for the small set of UI preferences exposed by
#: ``/api/preferences``.  The legacy backend stored these under
#: ``forge_config.ui.*``; we lift them into a dedicated KV row so the
#: hot-path read on every chat send (selected model id) does not have
#: to deserialise the entire forge_config doc.
PREFS_KEY = "ui.preferences"

#: Top-level key for code-persona selection and prompt overrides.
CODE_PERSONAS_KEY = "ui.code_personas"

#: The strict allow-list of fields ``POST /api/preferences`` may
#: persist.  This mirrors the legacy
#: ``backend/main.py::save_preferences`` behaviour byte-for-byte
#: (only those three keys are accepted).
_PREFS_ALLOWED_FIELDS: tuple[str, ...] = (
    "selected_model_id",
    "selected_model_provider",
    "selected_service_model",
)



# ---------------------------------------------------------------------------
# Request / response DTOs
# ---------------------------------------------------------------------------
class ForgeConfigResponse(BaseModel):
    """``GET /api/forge-config`` payload — wraps the doc in ``config``."""

    config: dict[str, Any]


class ForgeConfigSaveRequest(BaseModel):
    """``POST /api/forge-config`` body — top-level dict to merge."""

    model_config = ConfigDict(extra="allow")
    config: dict[str, Any] = Field(default_factory=dict)


class ForgeConfigSaveResponse(BaseModel):
    """``POST /api/forge-config`` payload — echoes status + merged doc."""

    status: str
    config: dict[str, Any]


class PreferencesResponse(BaseModel):
    """``GET /api/preferences`` payload — flat dict of UI prefs."""

    selected_model_id: str = ""
    selected_model_provider: str = ""
    selected_service_model: str = ""


class PreferencesSaveRequest(BaseModel):
    """``POST /api/preferences`` body — partial UI prefs.

    All three fields are ``str | None`` to mirror the legacy semantics:
    a missing / null field means "do not change this preference",
    while an explicit empty string means "clear this preference".
    """

    model_config = ConfigDict(extra="ignore")
    selected_model_id: str | None = None
    selected_model_provider: str | None = None
    selected_service_model: str | None = None


class PreferencesSaveResponse(BaseModel):
    """``POST /api/preferences`` payload — echoes status."""

    status: str


# ---------------------------------------------------------------------------
# PR-606 Skills DTOs
# ---------------------------------------------------------------------------
class SkillModeRequest(BaseModel):
    """``POST /api/skills/set_mode`` body."""

    mode: Literal["auto", "manual", "disabled"]


class SkillToggleRequest(BaseModel):
    """``POST /api/skills/toggle`` body."""

    skill_name: str
    enabled: bool


class PerSkillModeRequest(BaseModel):
    """``POST /api/skills/{skill_id}/set_mode`` body (per-skill 4-state).

    Distinct from :class:`SkillModeRequest` (the global ``auto/manual/
    disabled`` policy preference). This is the v1 per-skill run mode:
    ``off`` / ``cloud`` / ``local`` / ``both`` where ``local`` and
    ``both`` require the skill to be NPU-optimised.
    """

    mode: Literal["off", "cloud", "local", "both"]


# ---------------------------------------------------------------------------
# Chat operator-hook DTOs (read/write of ``forge_config.json`` ``chat.hooks``)
# ---------------------------------------------------------------------------
#: The 10 legal hook lifecycle events. Kept byte-identical to
#: ``qai.chat.domain.hook.HookEvent`` — deliberately RE-DECLARED here
#: rather than imported, exactly as ``qai.chat.domain.hook`` itself
#: re-declares them, because the ``context-isolation`` import-linter
#: contract forbids the user_prefs route layer from importing the
#: ``qai.chat`` bounded context. The string values are the persisted
#: contract, so the two copies must stay in lock-step.
_CHAT_HOOK_EVENTS: frozenset[str] = frozenset(
    {
        "pre_tool_call",
        "post_tool_call",
        "pre_message",
        "post_message",
        "on_error",
        "on_complete",
        "on_user_input",
        "on_session_start",
        "on_session_end",
        "on_truncate",
    }
)

#: Upper bound on a hook ``command`` length, matching
#: ``qai.chat.domain.hook._MAX_COMMAND_LENGTH`` (4096).
_CHAT_HOOK_MAX_COMMAND_LENGTH = 4096


class ChatHookEntry(BaseModel):
    """One operator-hook registration: event + shell command + timeout.

    Shape matches the ``chat.hooks`` array entries read by
    ``apps/api/_chat_di.py::_load_chat_hooks``
    (``{"event": ..., "command": ..., "timeout_s": 30}``) so the same
    document feeds both this read/write surface and the chat hook engine.
    """

    event: str
    command: str
    timeout_s: float = 30.0


class ChatHooksResponse(BaseModel):
    """``GET`` / ``PUT /api/settings/chat_hooks`` payload."""

    hooks: list[ChatHookEntry]


class ChatHooksSaveRequest(BaseModel):
    """``PUT /api/settings/chat_hooks`` body."""

    hooks: list[ChatHookEntry] = Field(default_factory=list)


class ChatHooksEnabledResponse(BaseModel):
    """``GET`` / ``PUT /api/settings/chat_hooks_enabled`` payload."""

    enabled: bool


class ChatHooksEnabledSaveRequest(BaseModel):
    """``PUT /api/settings/chat_hooks_enabled`` body."""

    enabled: bool


#: Legal keys of the ``chat.subagent_profile_models`` mapping (per-profile
#: sub-agent model override). Mirrors the profile names the chat
#: ``AgentToolHandler`` resolves (``resolve_profile`` general/explore).
_SUBAGENT_PROFILE_KEYS: frozenset[str] = frozenset({"general", "explore"})


class SubagentProfileModelsResponse(BaseModel):
    """``GET`` / ``PUT /api/settings/subagent_profile_models`` payload.

    ``models`` maps a profile name (``"general"`` / ``"explore"``) to a model
    id. Blank / missing keys are omitted (``{}`` when none configured) —
    inheriting the parent's model for that profile.
    """

    models: dict[str, str] = Field(default_factory=dict)


class SubagentProfileModelsSaveRequest(BaseModel):
    """``PUT /api/settings/subagent_profile_models`` body.

    An empty-string value clears (omits) that profile key; keys must be a
    subset of ``{"general", "explore"}``.
    """

    models: dict[str, str] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Chat operator-hook file helpers
# ---------------------------------------------------------------------------
def _forge_config_file_path(container: Container) -> Any:
    """Resolve ``<data>/config/forge_config.json`` (same path as chat DI).

    Mirrors ``apps/api/_chat_di.py::_load_chat_hooks`` /
    ``apps/api/_service_release_di.py::_resolve_forge_config_path`` so all
    three operate on one file.
    """
    return container.data_paths.root / "config" / "forge_config.json"


def _read_forge_config_file(container: Container) -> dict[str, Any]:
    """Read the on-disk forge_config doc; ``{}`` when absent/malformed."""
    import json

    path = _forge_config_file_path(container)
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _write_forge_config_file(container: Container, doc: dict[str, Any]) -> None:
    """Persist the forge_config doc as UTF-8 JSON (creates parent dir)."""
    import json

    path = _forge_config_file_path(container)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _validate_chat_hooks(
    hooks: list[ChatHookEntry],
) -> list[dict[str, Any]]:
    """Validate + normalise hook entries into JSON-ready dicts.

    Raises :class:`ValueError` (mapped to HTTP 422 by the route) on the
    first illegal entry. Validation mirrors
    ``qai.chat.domain.hook.HookConfig``: legal ``event``, non-empty
    ``command`` within the length bound, and ``timeout_s > 0``.
    """
    normalised: list[dict[str, Any]] = []
    for index, entry in enumerate(hooks):
        event = entry.event
        if event not in _CHAT_HOOK_EVENTS:
            raise ValueError(
                f"hooks[{index}]: unknown event {event!r}; "
                f"must be one of {sorted(_CHAT_HOOK_EVENTS)}"
            )
        command = entry.command
        if not command or not command.strip():
            raise ValueError(f"hooks[{index}]: command must be non-empty")
        if len(command) > _CHAT_HOOK_MAX_COMMAND_LENGTH:
            raise ValueError(
                f"hooks[{index}]: command too long "
                f"({len(command)} > {_CHAT_HOOK_MAX_COMMAND_LENGTH})"
            )
        if any(ch in command for ch in ("\x00", "\r", "\n")):
            raise ValueError(
                f"hooks[{index}]: command must not contain control characters"
            )
        timeout_s = entry.timeout_s
        if timeout_s <= 0:
            raise ValueError(f"hooks[{index}]: timeout_s must be > 0")
        normalised.append(
            {
                "event": event,
                "command": command,
                "timeout_s": float(timeout_s),
            }
        )
    return normalised


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------
def build_router(*, container: Container) -> APIRouter:
    """Build the user_prefs router bound to ``container.user_prefs``.

    The router has NO ``prefix`` / ``tags`` because this BC owns
    several disjoint legacy paths (``/api/forge-config`` /
    ``/api/preferences`` / ``/api/proxy`` / ``/api/code-personas`` /
    ``/api/settings/*``) — adding a single ``user_prefs`` prefix
    would break every legacy URL the front-end relies on (route
    contract is locked, v2.7 §3.1).
    """
    router = APIRouter(tags=["user_prefs"])
    services = container.user_prefs

    # ---------------------- /api/forge-config -----------------------------
    @router.get("/api/forge-config", response_model=ForgeConfigResponse)
    async def get_forge_config() -> ForgeConfigResponse:
        # All V1-parity default injection + cross-document CC/OC pill
        # derivation lives in the use case (Clean Architecture: the route
        # stays thin). See LoadForgeConfigUseCase for the byte-for-byte
        # parity notes that previously lived inline here.
        doc = await services.load_forge_config_use_case.execute()
        return ForgeConfigResponse(config=doc)

    @router.post(
        "/api/forge-config",
        response_model=ForgeConfigSaveResponse,
    )
    async def save_forge_config(
        req: ForgeConfigSaveRequest,
    ) -> ForgeConfigSaveResponse:
        merged = await services.save_document_use_case.execute(
            FORGE_CONFIG_KEY,
            updates=req.config,
        )
        return ForgeConfigSaveResponse(status="saved", config=merged)

    # ---------------------- /api/preferences ------------------------------
    @router.get("/api/preferences", response_model=PreferencesResponse)
    async def get_preferences() -> PreferencesResponse:
        doc = await services.load_document_use_case.execute(PREFS_KEY)
        return PreferencesResponse(
            selected_model_id=str(doc.get("selected_model_id", "") or ""),
            selected_model_provider=str(
                doc.get("selected_model_provider", "") or ""
            ),
            selected_service_model=str(
                doc.get("selected_service_model", "") or ""
            ),
        )

    @router.post(
        "/api/preferences",
        response_model=PreferencesSaveResponse,
    )
    async def save_preferences(
        req: PreferencesSaveRequest,
    ) -> PreferencesSaveResponse:
        # Filter ``None``s out: a None means "no change", we never
        # want to overwrite the persisted value with an explicit
        # ``null`` (which would happen with a naive merge).
        updates: dict[str, Any] = {}
        if req.selected_model_id is not None:
            updates["selected_model_id"] = req.selected_model_id
        if req.selected_model_provider is not None:
            updates["selected_model_provider"] = req.selected_model_provider
        if req.selected_service_model is not None:
            updates["selected_service_model"] = req.selected_service_model
        if updates:
            await services.save_document_use_case.execute(
                PREFS_KEY,
                updates=updates,
                allowed_top_level=_PREFS_ALLOWED_FIELDS,
            )
        return PreferencesSaveResponse(status="saved")

    # ===========================================================================
    # PR-601b: Proxy routes (2 routes)
    #
    # URL/username persist to forge.config network_proxy; password is handled
    # via SecretStore (AGENTS.md §3.3). The read-modify-write + masked-password
    # policy lives in Get/SaveProxyUseCase — the route just forwards the body.
    # ===========================================================================

    @router.get("/api/proxy")
    async def get_proxy() -> dict[str, Any]:
        return await services.get_proxy_use_case.execute()

    @router.post("/api/proxy")
    async def post_proxy(body: dict[str, Any]) -> dict[str, Any]:
        return await services.save_proxy_use_case.execute(body)

    # ===========================================================================
    # PR-601b: Code-personas routes (5 routes)
    # ===========================================================================

    @router.get("/api/code-personas")
    async def get_code_personas(locale: str | None = None) -> dict[str, Any]:
        doc = await services.load_document_use_case.execute(CODE_PERSONAS_KEY)
        selected, personas = CodePersonaManager.get_all_personas(doc, locale=locale)
        return {"selected": selected, "personas": personas}

    @router.post("/api/code-personas/select")
    async def select_code_persona(body: dict[str, Any]) -> dict[str, Any]:
        persona_id = str(body.get("persona_id", DEFAULT_PERSONA_ID))
        doc = await services.load_document_use_case.execute(CODE_PERSONAS_KEY)
        try:
            updated = CodePersonaManager.select_persona(doc, persona_id)
        except ValueError as exc:
            raise ValidationError(
                "user_prefs.code_personas.invalid_persona", str(exc)
            ) from exc
        await services.save_document_use_case.execute(
            CODE_PERSONAS_KEY, updates=updated
        )
        return {"status": "saved", "selected": persona_id}

    @router.post("/api/code-personas/active")
    async def set_active_persona(body: dict[str, Any]) -> dict[str, Any]:
        """Set the active persona (alias for POST /select)."""
        persona_id = str(body.get("persona_id", DEFAULT_PERSONA_ID))
        doc = await services.load_document_use_case.execute(CODE_PERSONAS_KEY)
        try:
            updated = CodePersonaManager.select_persona(doc, persona_id)
        except ValueError as exc:
            raise ValidationError(
                "user_prefs.code_personas.invalid_persona", str(exc)
            ) from exc
        await services.save_document_use_case.execute(
            CODE_PERSONAS_KEY, updates=updated
        )
        return {"status": "saved", "active": persona_id}

    @router.post("/api/code-personas/{persona_id}")
    async def override_code_persona(
        persona_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        prompt = body.get("prompt")
        groups = body.get("groups")
        doc = await services.load_document_use_case.execute(CODE_PERSONAS_KEY)
        # Apply prompt override if provided.
        if prompt is not None:
            prompt = str(prompt)
            if len(prompt) > MAX_PROMPT_LENGTH:
                raise DomainError(
                    "user_prefs.code_personas.prompt_too_long",
                    f"Prompt too long: {len(prompt)} chars "
                    f"(max {MAX_PROMPT_LENGTH})",
                )
            try:
                doc = CodePersonaManager.override_prompt(doc, persona_id, prompt)
            except ValueError as exc:
                raise ValidationError(
                    "user_prefs.code_personas.invalid_persona", str(exc)
                ) from exc
        # Apply groups override if provided.
        if groups is not None:
            if not isinstance(groups, list):
                raise ValidationError(
                    "user_prefs.code_personas.invalid_groups",
                    "groups must be an array",
                )
            try:
                doc = CodePersonaManager.override_groups(doc, persona_id, groups)
            except ValueError as exc:
                raise ValidationError(
                    "user_prefs.code_personas.invalid_groups", str(exc)
                ) from exc
        if prompt is None and groups is None:
            raise ValidationError(
                "user_prefs.code_personas.empty_body",
                "Request body must contain at least one of: prompt, groups",
            )
        await services.save_document_use_case.execute(
            CODE_PERSONAS_KEY, updates=doc
        )
        return {"status": "saved"}

    @router.delete("/api/code-personas/{persona_id}")
    async def reset_code_persona(persona_id: str) -> dict[str, Any]:
        doc = await services.load_document_use_case.execute(CODE_PERSONAS_KEY)
        try:
            updated = CodePersonaManager.reset_persona(doc, persona_id)
        except ValueError as exc:
            raise ValidationError(
                "user_prefs.code_personas.invalid_persona", str(exc)
            ) from exc
        await services.save_document_use_case.execute(
            CODE_PERSONAS_KEY, updates=updated
        )
        return {"status": "reset"}

    @router.delete("/api/code-personas")
    async def reset_all_code_personas() -> dict[str, Any]:
        doc = await services.load_document_use_case.execute(CODE_PERSONAS_KEY)
        updated = CodePersonaManager.reset_all(doc)
        await services.save_document_use_case.execute(
            CODE_PERSONAS_KEY, updates=updated
        )
        return {"status": "reset_all"}

    # ── Additional code-persona routes (W1-H §14-17) ────────────────────

    @router.get("/api/code-personas/{persona_id}")
    async def get_code_persona_detail(persona_id: str) -> dict[str, Any]:
        """Return a single persona by ID with overrides applied."""
        from qai.user_prefs.domain.code_personas import DEFAULT_PERSONAS as _DP

        if persona_id not in _DP:
            raise NotFoundError(
                "user_prefs.code_personas.not_found",
                "code_persona",
                persona_id,
                message=f"Unknown persona id: {persona_id!r}",
            )
        doc = await services.load_document_use_case.execute(CODE_PERSONAS_KEY)
        _, all_personas = CodePersonaManager.get_all_personas(doc)
        for p in all_personas:
            if p.get("id") == persona_id:
                return {"persona": p}
        # Fallback (should not happen given the check above)
        return {"persona": dict(_DP[persona_id])}

    @router.put("/api/code-personas/{persona_id}")
    async def update_code_persona(
        persona_id: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Update a persona's prompt and/or groups via PUT."""
        prompt = body.get("prompt")
        groups = body.get("groups")
        doc = await services.load_document_use_case.execute(CODE_PERSONAS_KEY)
        if prompt is not None:
            prompt = str(prompt)
            if len(prompt) > MAX_PROMPT_LENGTH:
                raise DomainError(
                    "user_prefs.code_personas.prompt_too_long",
                    f"Prompt too long: {len(prompt)} chars "
                    f"(max {MAX_PROMPT_LENGTH})",
                )
            try:
                doc = CodePersonaManager.override_prompt(doc, persona_id, prompt)
            except ValueError as exc:
                raise ValidationError(
                    "user_prefs.code_personas.invalid_persona", str(exc)
                ) from exc
        if groups is not None:
            if not isinstance(groups, list):
                raise ValidationError(
                    "user_prefs.code_personas.invalid_groups",
                    "groups must be an array",
                )
            try:
                doc = CodePersonaManager.override_groups(doc, persona_id, groups)
            except ValueError as exc:
                raise ValidationError(
                    "user_prefs.code_personas.invalid_groups", str(exc)
                ) from exc
        if prompt is None and groups is None:
            raise ValidationError(
                "user_prefs.code_personas.empty_body",
                "Request body must contain at least one of: prompt, groups",
            )
        await services.save_document_use_case.execute(
            CODE_PERSONAS_KEY, updates=doc
        )
        return {"status": "saved", "persona_id": persona_id}

    # ===========================================================================
    # PR-606: Skills toggle / reload / discovery routes (4 routes)
    #
    # Each is a read-modify-write of the ``forge.config skills`` sub-key; the
    # transaction lives in the matching use case (Clean Architecture). The
    # route just forwards the validated request fields.
    # ===========================================================================

    @router.get("/api/skills/policy")
    async def get_skill_policy() -> dict[str, Any]:
        """Return aggregated skill policy state from forge.config."""
        return await services.get_skill_policy_use_case.execute()

    @router.post("/api/skills/set_mode")
    async def set_skill_mode(req: SkillModeRequest) -> dict[str, Any]:
        """Persist skill mode preference in forge.config skills.mode."""
        return await services.set_skill_policy_mode_use_case.execute(req.mode)

    @router.post("/api/skills/toggle")
    async def toggle_skill(req: SkillToggleRequest) -> dict[str, Any]:
        """Persist per-skill enabled state in forge.config skills.overrides."""
        return await services.toggle_skill_use_case.execute(
            req.skill_name, req.enabled
        )

    @router.post("/api/skills/reload")
    async def reload_skills() -> dict[str, Any]:
        """Trigger skill discovery reload signal.

        Persists a ``skills.last_reload`` UTC timestamp to forge.config and
        returns success.
        """
        return await services.reload_skills_use_case.execute()

    # ===========================================================================
    # Skills business registry (v1 SkillManager port): directory discovery +
    # per-skill 4-state mode + icon serving. The directory scan + override
    # mode-resolve + NPU validation live in ListSkillsUseCase /
    # SetSkillModeUseCase (Clean Architecture: interfaces stays thin); the
    # route maps the domain errors to the same 404 / 400 responses.
    # ===========================================================================

    @router.get("/api/skills")
    async def list_skills() -> dict[str, Any]:
        """Return the v1-shaped skill business list.

        Scans ``<repo_root>/skills`` live on every call (no cache, v1
        reload parity) and merges each skill's persisted per-skill mode
        from ``forge.config skills.overrides``.
        """
        return await services.list_skills_use_case.execute()

    @router.post("/api/skills/{skill_id}/set_mode")
    async def set_per_skill_mode(
        skill_id: str, req: PerSkillModeRequest
    ) -> dict[str, Any]:
        """Set the per-skill 4-state run mode and persist it.

        * 404 if ``skill_id`` is not a discovered skill.
        * 400 if ``mode`` is ``local``/``both`` but the skill is not
          NPU-optimised (v1 ``set_mode`` ValueError parity).
        Persists to ``forge.config skills.overrides[skill_id].mode`` —
        tail-appending the ``mode`` sub-key alongside any existing
        ``enabled`` flag written by ``/api/skills/toggle``.
        """
        try:
            return await services.set_skill_mode_use_case.execute(
                skill_id, req.mode
            )
        except SkillNotFoundError as exc:
            raise NotFoundError(
                "user_prefs.skills.not_found",
                "skill",
                skill_id,
                message=str(exc),
            ) from exc
        except SkillModeNotAllowedError as exc:
            raise ValidationError(
                "user_prefs.skills.mode_not_allowed", str(exc)
            ) from exc

    @router.get("/api/skills/{skill_id}/icon")
    async def get_skill_icon(skill_id: str):
        """Serve a skill's icon file (``icon.{png,svg,jpg,webp}``)."""
        from fastapi.responses import FileResponse

        icon = services.skill_discovery.icon_path(skill_id)
        if icon is None:
            raise NotFoundError(
                "user_prefs.skills.icon_not_found",
                "skill_icon",
                skill_id,
                message=f"No icon for skill: {skill_id}",
            )
        return FileResponse(str(icon))

    # ===========================================================================
    # Chat operator hooks: GET/PUT /api/settings/chat_hooks (2 routes)
    #
    # Reads / writes the ``chat.hooks`` array of the on-disk
    # ``<data>/config/forge_config.json`` document — the SAME file +
    # key that ``apps/api/_chat_di.py::_load_chat_hooks`` reads at chat
    # wiring time. Persisting here therefore guarantees the front-end,
    # this read surface, and the chat hook engine all observe one source
    # of truth (rather than the ``kv_user_prefs`` ``forge.config`` row,
    # which the hook engine does not read). The file shallow-merge
    # preserves every unrelated section (V1 ``forge_config.update``
    # semantics, mirrored by ``service_release``'s writer).
    # ===========================================================================

    @router.get("/api/settings/chat_hooks", response_model=ChatHooksResponse)
    async def get_chat_hooks() -> ChatHooksResponse:
        doc = _read_forge_config_file(container)
        raw_hooks = (((doc or {}).get("chat") or {}).get("hooks")) or []
        entries: list[ChatHookEntry] = []
        for item in raw_hooks:
            if not isinstance(item, dict):
                continue
            event = item.get("event")
            command = item.get("command")
            if not isinstance(event, str) or not isinstance(command, str):
                continue
            try:
                timeout_s = float(item.get("timeout_s", 30.0))
            except (TypeError, ValueError):
                timeout_s = 30.0
            entries.append(
                ChatHookEntry(
                    event=event, command=command, timeout_s=timeout_s
                )
            )
        return ChatHooksResponse(hooks=entries)

    @router.put("/api/settings/chat_hooks", response_model=ChatHooksResponse)
    async def put_chat_hooks(req: ChatHooksSaveRequest) -> Any:
        try:
            normalised = _validate_chat_hooks(req.hooks)
        except ValueError as exc:
            raise DomainError(
                "user_prefs.chat_hooks.invalid", str(exc)
            ) from exc
        doc = _read_forge_config_file(container)
        chat_section = doc.get("chat")
        if not isinstance(chat_section, dict):
            chat_section = {}
        chat_section["hooks"] = normalised
        doc["chat"] = chat_section
        _write_forge_config_file(container, doc)
        return ChatHooksResponse(
            hooks=[ChatHookEntry(**entry) for entry in normalised]
        )

    # ===========================================================================
    # Chat hooks master enable gate: GET/PUT /api/settings/chat_hooks_enabled
    #
    # Reads / writes the ``chat.hooks_enabled`` boolean of the on-disk
    # ``<data>/config/forge_config.json`` document — the SAME file the chat
    # ``LazyReloadHookEngine`` re-reads each turn (precedence forge_config >
    # ``settings.chat.hooks_enabled``). Flipping this therefore takes effect on
    # the next turn WITHOUT a service restart. The file shallow-merge preserves
    # every unrelated section (mirrors ``put_chat_hooks``).
    # ===========================================================================

    @router.get(
        "/api/settings/chat_hooks_enabled",
        response_model=ChatHooksEnabledResponse,
    )
    async def get_chat_hooks_enabled() -> ChatHooksEnabledResponse:
        doc = _read_forge_config_file(container)
        chat_section = (doc or {}).get("chat")
        override = (
            chat_section.get("hooks_enabled")
            if isinstance(chat_section, dict)
            else None
        )
        if isinstance(override, bool):
            return ChatHooksEnabledResponse(enabled=override)
        # Absent forge_config key → fall back to the Settings field (default
        # False). Defensive getattr keeps minimal-container tests working.
        chat_settings = getattr(container.settings, "chat", None)
        return ChatHooksEnabledResponse(
            enabled=bool(getattr(chat_settings, "hooks_enabled", False))
        )

    @router.put(
        "/api/settings/chat_hooks_enabled",
        response_model=ChatHooksEnabledResponse,
    )
    async def put_chat_hooks_enabled(
        req: ChatHooksEnabledSaveRequest,
    ) -> ChatHooksEnabledResponse:
        enabled = bool(req.enabled)
        doc = _read_forge_config_file(container)
        chat_section = doc.get("chat")
        if not isinstance(chat_section, dict):
            chat_section = {}
        chat_section["hooks_enabled"] = enabled
        doc["chat"] = chat_section
        _write_forge_config_file(container, doc)
        return ChatHooksEnabledResponse(enabled=enabled)

    # ===========================================================================
    # Per-profile sub-agent model overrides:
    # GET/PUT /api/settings/subagent_profile_models
    #
    # Reads / writes the ``chat.subagent_profile_models`` mapping of the on-disk
    # ``<data>/config/forge_config.json`` document — the SAME file + key the
    # chat DI ``_build_subagent_profile_models_reader`` re-reads each sub-agent
    # run. Setting a model for "general"/"explore" routes that profile's
    # sub-agents to the chosen model on the next run WITHOUT a restart; an
    # empty-string value clears (omits) that key. The file shallow-merge
    # preserves every unrelated section (mirrors ``put_chat_hooks``).
    # ===========================================================================

    @router.get(
        "/api/settings/subagent_profile_models",
        response_model=SubagentProfileModelsResponse,
    )
    async def get_subagent_profile_models() -> SubagentProfileModelsResponse:
        doc = _read_forge_config_file(container)
        chat_section = (doc or {}).get("chat")
        raw_models = (
            chat_section.get("subagent_profile_models")
            if isinstance(chat_section, dict)
            else None
        )
        out: dict[str, str] = {}
        if isinstance(raw_models, dict):
            for key in _SUBAGENT_PROFILE_KEYS:
                value = raw_models.get(key)
                if isinstance(value, str) and value.strip():
                    out[key] = value.strip()
        return SubagentProfileModelsResponse(models=out)

    @router.put(
        "/api/settings/subagent_profile_models",
        response_model=SubagentProfileModelsResponse,
    )
    async def put_subagent_profile_models(
        req: SubagentProfileModelsSaveRequest,
    ) -> Any:
        # Validate keys ∈ {"general","explore"}.
        illegal = set(req.models) - _SUBAGENT_PROFILE_KEYS
        if illegal:
            raise DomainError(
                "user_prefs.subagent_profile_models.invalid",
                f"unknown profile key(s) {sorted(illegal)}; "
                f"must be a subset of {sorted(_SUBAGENT_PROFILE_KEYS)}",
            )
        doc = _read_forge_config_file(container)
        chat_section = doc.get("chat")
        if not isinstance(chat_section, dict):
            chat_section = {}
        existing = chat_section.get("subagent_profile_models")
        merged: dict[str, str] = (
            {
                k: v
                for k, v in existing.items()
                if k in _SUBAGENT_PROFILE_KEYS
                and isinstance(v, str)
                and v.strip()
            }
            if isinstance(existing, dict)
            else {}
        )
        # Shallow-merge: a non-blank value sets the key; an empty-string value
        # clears (omits) it.
        for key, value in req.models.items():
            if isinstance(value, str) and value.strip():
                merged[key] = value.strip()
            else:
                merged.pop(key, None)
        chat_section["subagent_profile_models"] = merged
        doc["chat"] = chat_section
        _write_forge_config_file(container, doc)
        return SubagentProfileModelsResponse(models=merged)

    return router
