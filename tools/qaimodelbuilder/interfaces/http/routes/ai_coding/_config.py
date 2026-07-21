# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""CC/OC config + credentials routes (``_register_cc_config_routes``).

Extracted verbatim from the former single-file ``ai_coding.py``
(zero behaviour change).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Path

from qai.ai_coding.application.use_cases.manage_coding_config import (
    GetCodingConfigQuery,
    SaveCodingConfigCommand,
)
from qai.ai_coding.application.use_cases.manage_coding_credentials import (
    DeleteCredentialCommand,
    SaveCodingCredentialsCommand,
)
from qai.ai_coding.domain import Provider

from ._dto import (
    CodingConfigResponse,
    CredentialStatusEnvelope,
    CredentialsListResponse,
    DeleteCredentialResponse,
    SaveCodingConfigRequest,
    SaveCodingConfigResponse,
    SaveCredentialsRequest,
    SaveCredentialsResponse,
)

if TYPE_CHECKING:  # pragma: no cover
    from apps.api.di import Container


#: Whitelist of safe top-level config keys the WebUI is allowed to
#: persist via ``POST /api/cc/config``.  Mirrors the legacy
#: ``backend.ai_coding.api_routes.save_cc_config`` allow-list 1:1 so
#: existing WebUI clients continue to work.  Sensitive credential
#: variable names are intentionally absent — they go through
#: ``POST /api/cc/credentials`` and the SecretStore.
_CONFIG_KEY_WHITELIST: frozenset[str] = frozenset({
    "enabled", "permission_mode", "allowed_tools", "disallowed_tools",
    "max_turns", "session_idle_timeout_minutes",
    "pending_message_ttl_seconds",
    "message_timeout_seconds",
    "permission_approval_timeout_seconds",
    "allowed_working_dirs", "system_prompt", "cli_path",
    "model", "model_list",
    "auth_env",      # NON-sensitive auth env (e.g. ANTHROPIC_BASE_URL)
    "session_env",
    "tool_catalog",
    "add_dirs",
    "effort", "thinking",
    "enable_file_checkpointing",
    # CC SDK file checkpoint/rewind backend selector (V1 parity): "http"
    # (default) | "sdk".  Persisting via /config lets the next spawn inherit
    # the backend choice; the DI root still gracefully falls back to "http"
    # when the SDK / native CLI is unavailable.
    "cc_backend",
    "agents",
    "include_partial_messages",
    "betas",
    # PR-105: OpenCode-specific config keys (legacy
    # ``OCConfigUpdateRequest`` shape from
    # ``backend/ai_coding/opencode_api_routes.py``).  Kept on the
    # SAME whitelist so a single helper serves both providers; the
    # CC mount simply ignores OC-only keys (and vice versa).
    "base_url",
    "hostname",
    "provider_id",
    "model_id",
    "auto_start",
    "use_cloud_models",
    "username",
    # NB: ``password`` is intentionally absent — OC service auth
    # tokens / passwords are credential material and MUST go
    # through ``POST /api/oc/credentials`` per v2.7 §3.3.
    "provider_mapping",
    "permission",        # OC permission map: {"bash": "ask", "edit": "deny"}
    # F-3 (PR-107 SDK-12 enhancements): persisting these via /config lets a
    # session inherit them (parity with spawn-body config).  Credential
    # material is intentionally excluded (stays on the SecretStore per §3.3).
    "mcp_servers",
    "hooks",
    "setting_sources",
    "plugins",
    "output_format",
    "max_budget_usd",
    "fallback_model",
    "task_budget",
    "user",
    "extra_args",
    # F-23 (PR-I3): per-session ``ClaudeAgentOptions.skills`` analogue.
    # Distinct from the existing ``manage_skills`` mount-style skill
    # provisioning — ``skills`` here is a *filter* listing which skill
    # names this session is allowed to use.  Persisting via /config
    # lets the next spawn inherit the filter (parity with spawn-body
    # config).
    "skills",
})


def _register_cc_config_routes(
    router: APIRouter,
    *,
    container: "Container",
    provider: Provider = Provider.CLAUDE_CODE,
) -> None:
    """Attach the 5 config + credentials routes onto ``router``.

    All routes mirror the legacy CC wire contract 1:1 per v2.7 §3.1
    path-shape lock.  Credentials flow through the platform
    :class:`SecretStore` per v2.7 §3.3 — no sensitive value is
    persisted in the config document or any other plain-text
    location.

    PR-105 generalises this helper to also support the OC sub-router
    by binding the per-provider use-case fields and credential
    whitelist via the ``provider`` argument.  The OC mount uses
    :data:`OC_CREDENTIAL_VARS` and the ``ai_coding_oc`` SecretStore
    namespace; CC keeps :data:`CC_CREDENTIAL_VARS` and ``ai_coding``.
    """
    services = container.ai_coding

    # Per-provider use-case binding.  CC uses the original PR-104b
    # use cases; OC uses the parallel PR-105 instances on the same
    # services namespace.  The credentials use cases themselves know
    # the right whitelist (CC_CREDENTIAL_VARS / OC_CREDENTIAL_VARS)
    # via DI, so the route layer just forwards the request body.
    if provider is Provider.OPEN_CODE:
        get_config_uc = services.get_oc_coding_config_use_case
        save_config_uc = services.save_oc_coding_config_use_case
        get_credentials_uc = services.get_oc_coding_credentials_use_case
        save_credentials_uc = services.save_oc_coding_credentials_use_case
        delete_credential_uc = services.delete_oc_credential_use_case
    else:
        get_config_uc = services.get_coding_config_use_case
        save_config_uc = services.save_coding_config_use_case
        get_credentials_uc = services.get_coding_credentials_use_case
        save_credentials_uc = services.save_coding_credentials_use_case
        delete_credential_uc = services.delete_credential_use_case

    # -- GET /api/{cc|oc}/config -------------------------------------------

    @router.get("/config", response_model=CodingConfigResponse)
    async def get_coding_config() -> CodingConfigResponse:
        doc = await get_config_uc.execute(GetCodingConfigQuery())
        return CodingConfigResponse(config=doc)

    # -- POST /api/{cc|oc}/config ------------------------------------------

    @router.post(
        "/config",
        response_model=SaveCodingConfigResponse,
    )
    async def save_coding_config(
        body: SaveCodingConfigRequest,
    ) -> SaveCodingConfigResponse:
        # Whitelist filter — silently drop unknown keys so a
        # malformed client request can never smuggle a credential
        # variable into the kv_user_prefs document.
        filtered = {
            k: v for k, v in body.config.items()
            if k in _CONFIG_KEY_WHITELIST
        }
        merged = await save_config_uc.execute(
            SaveCodingConfigCommand(updates=filtered)
        )
        # Preserve the legacy ``updated_keys`` shape — list ONLY the
        # keys the request tried to update (post-filter), not the
        # full merged document.
        return SaveCodingConfigResponse(
            ok=True,
            updated_keys=sorted(filtered.keys()),
        )
        # The merged document is available to the caller via a
        # subsequent GET; deliberately not returned in the POST body
        # to keep the wire shape identical to legacy.
        del merged  # silence "unused" lint while documenting intent

    # -- PUT /api/{cc|oc}/config (PR-105: OC parity) -----------------------

    @router.put(
        "/config",
        response_model=SaveCodingConfigResponse,
    )
    async def put_coding_config(
        body: SaveCodingConfigRequest,
    ) -> SaveCodingConfigResponse:
        # PR-105: OC's legacy backend exposed PUT (not POST) for
        # config updates.  Both verbs now share the same
        # idempotent merge semantic for cross-provider parity;
        # CC clients keep using POST, OC clients keep using PUT.
        filtered = {
            k: v for k, v in body.config.items()
            if k in _CONFIG_KEY_WHITELIST
        }
        await save_config_uc.execute(
            SaveCodingConfigCommand(updates=filtered)
        )
        return SaveCodingConfigResponse(
            ok=True,
            updated_keys=sorted(filtered.keys()),
        )

    # -- GET /api/{cc|oc}/credentials --------------------------------------

    @router.get(
        "/credentials",
        response_model=CredentialsListResponse,
    )
    async def get_coding_credentials() -> CredentialsListResponse:
        result = await get_credentials_uc.execute()
        return CredentialsListResponse(
            credentials={
                s.var_name: CredentialStatusEnvelope(
                    in_store=s.in_store,
                    in_env=s.in_env,
                    configured=s.configured,
                )
                for s in result.statuses
            }
        )

    # -- POST /api/{cc|oc}/credentials -------------------------------------

    @router.post(
        "/credentials",
        response_model=SaveCredentialsResponse,
    )
    async def save_coding_credentials(
        body: SaveCredentialsRequest,
    ) -> SaveCredentialsResponse:
        # The use case applies the per-provider whitelist itself
        # (unknown keys land in ``skipped``).  We forward the body
        # verbatim so the legacy wire shape — including the
        # bookkeeping of unknown keys — is preserved.
        result = await save_credentials_uc.execute(
            SaveCodingCredentialsCommand(credentials=dict(body.credentials))
        )
        return SaveCredentialsResponse(
            ok=True,
            saved=list(result.saved),
            deleted=list(result.deleted),
            skipped=list(result.skipped),
        )

    # -- DELETE /api/{cc|oc}/credentials/{var_name} ------------------------

    @router.delete(
        "/credentials/{var_name}",
        response_model=DeleteCredentialResponse,
    )
    async def delete_coding_credential(
        var_name: str = Path(..., min_length=1, max_length=128),
    ) -> DeleteCredentialResponse:
        # The use case does its own whitelist check (raises
        # ``ValidationError`` on unknown variable names → 400 envelope).
        await delete_credential_uc.execute(
            DeleteCredentialCommand(var_name=var_name)
        )
        return DeleteCredentialResponse(ok=True, deleted=var_name)
