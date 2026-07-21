# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Shared wire-format DTOs + conversion helpers for ai_coding routes.

Extracted verbatim from the former single-file ``ai_coding.py`` during
the S* architectural split (pure refactor, zero behaviour change).  All
class names / field shapes are preserved byte-for-byte so the OpenAPI
schema SHA does not drift.

DTOs / helpers used by ONLY one sub-module (session-extension's
``AbortResponse`` group, oc_service's ``OcService*`` group) live in their
respective modules; everything shared by ``_provider`` /
``_sessions`` / ``_config`` lives here.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field

from qai.ai_coding.domain import (
    CodingSession,
    CodingSessionConfig,
    CodingStreamFrame,
    HookConfig,
    HookEvent,
    McpServerConfig,
    OutputFormat,
    PermissionRequest,
    Skill,
)


# ---------------------------------------------------------------------------
# Wire-format DTOs
# ---------------------------------------------------------------------------


class SpawnSessionRequest(BaseModel):
    """Body of ``POST /api/{cc|oc}/sessions``."""

    workspace: str = Field(..., min_length=1, max_length=4096)
    initial_prompt: str | None = Field(default=None, min_length=1, max_length=262_144)
    title: str | None = Field(default=None, max_length=256)
    # F-3 (PR-107 SDK-12 enhancements): optional per-session config so a
    # client can ferry mcp_servers / hooks / fallback_model / output_format
    # / setting_sources / plugins / max_budget_usd / task_budget /
    # user / extra_args / session_env at spawn time.  Append-only per
    # §3.1 — historical clients that omit ``config`` keep the PR-046 shape.
    config: dict[str, Any] | None = Field(default=None)


def _build_session_config(
    raw: dict[str, Any] | None,
) -> CodingSessionConfig | None:
    """Map a spawn-request ``config`` dict to a :class:`CodingSessionConfig`.

    Unknown keys are ignored; malformed values are rejected by the
    domain ``__post_init__`` validators (surfaced as a 4xx by the unified
    error handler).  Returns ``None`` when ``raw`` is empty so the
    historical "spawn without extras" path stays byte-identical.
    """
    if not raw:
        return None

    kwargs: dict[str, Any] = {}

    mcp = raw.get("mcp_servers")
    if isinstance(mcp, list):
        servers: list[McpServerConfig] = []
        for item in mcp:
            if not isinstance(item, dict):
                continue
            servers.append(
                McpServerConfig(
                    name=str(item.get("name", "")),
                    url=str(item.get("url", "")),
                    transport=str(item.get("transport", "stdio")),
                    auth={
                        str(k): str(v)
                        for k, v in (item.get("auth") or {}).items()
                    },
                )
            )
        kwargs["mcp_servers"] = tuple(servers)

    hooks = raw.get("hooks")
    if isinstance(hooks, list):
        hook_cfgs: list[HookConfig] = []
        for item in hooks:
            if not isinstance(item, dict):
                continue
            event_raw = str(item.get("event", ""))
            try:
                event = HookEvent(event_raw)
            except ValueError:
                continue
            hook_cfgs.append(
                HookConfig(
                    event=event,
                    command=str(item.get("command", "")),
                    timeout_s=float(item.get("timeout_s", 30.0)),
                )
            )
        kwargs["hooks"] = tuple(hook_cfgs)

    out_fmt = raw.get("output_format")
    if isinstance(out_fmt, str):
        try:
            kwargs["output_format"] = OutputFormat(out_fmt)
        except ValueError:
            pass

    if isinstance(raw.get("setting_sources"), list):
        kwargs["setting_sources"] = tuple(
            str(s) for s in raw["setting_sources"]
        )
    if isinstance(raw.get("plugins"), list):
        kwargs["plugins"] = tuple(str(s) for s in raw["plugins"])
    if isinstance(raw.get("extra_args"), dict):
        kwargs["extra_args"] = {
            str(k): str(v) for k, v in raw["extra_args"].items()
        }
    if isinstance(raw.get("session_env"), dict):
        kwargs["session_env"] = {
            str(k): str(v) for k, v in raw["session_env"].items()
        }
    if raw.get("max_budget_usd") is not None:
        kwargs["max_budget_usd"] = float(raw["max_budget_usd"])
    if raw.get("task_budget") is not None:
        kwargs["task_budget"] = int(raw["task_budget"])
    if isinstance(raw.get("fallback_model"), str):
        kwargs["fallback_model"] = raw["fallback_model"]
    if isinstance(raw.get("user"), str):
        kwargs["user"] = raw["user"]
    if isinstance(raw.get("effort"), str):
        kwargs["effort"] = raw["effort"]
    # F-23 (PR-I3): per-session skill filter — list of skill names the
    # harness is allowed to advertise this turn.  Empty / missing
    # means "no filter".
    if isinstance(raw.get("skills"), list):
        kwargs["skills"] = tuple(str(s) for s in raw["skills"])
    # CC SDK file checkpoint/rewind backend (V1 ``session_manager.py``):
    # per-session opt-in to the ``claude_agent_sdk`` CLI subprocess adapter
    # (real on-disk checkpoint/rewind).  Default kept at the V1 defaults
    # (``http`` / off) so historical spawns are unchanged.
    if isinstance(raw.get("cc_backend"), str):
        kwargs["cc_backend"] = raw["cc_backend"]
    if isinstance(raw.get("cli_path"), str):
        kwargs["cli_path"] = raw["cli_path"]
    if raw.get("enable_file_checkpointing") is not None:
        kwargs["enable_file_checkpointing"] = bool(
            raw["enable_file_checkpointing"]
        )

    if not kwargs:
        return None
    return CodingSessionConfig(**kwargs)


class CodingSessionResponse(BaseModel):
    """Wire shape of a :class:`CodingSession`."""

    session_id: str
    provider: str
    workspace: str
    status: str
    title: str | None
    created_at: str
    terminated_at: str | None = None
    termination_reason: str | None = None
    last_stream_sequence: int
    # Append-only per §3.1 (V1 session-list parity for panel badges).
    # These surface real domain data already present on the aggregate:
    #   - effort: per-session thinking-depth override (config.effort)
    #   - claude_session_id: upstream provider conversation id (fork gate)
    #   - turn_count: message count (approximation of completed turns)
    effort: str | None = None
    claude_session_id: str | None = None
    turn_count: int = 0
    # Append-only per §3.1 (V1 panel 🔧 tool-calls badge). Real domain data:
    # the number of recorded tool invocations on the aggregate. Other V1
    # badge inputs (source / channel-notify ids / per-turn token usage) are
    # NOT surfaced here because V2's domain has no real data for them yet
    # (they require the channel concept / a live SDK turn) — fabricating
    # them would violate the project's "no fake data" rule.
    total_tool_calls: int = 0
    # Append-only per §3.1 (V1 dual-channel notify 🔔 badge). Real domain
    # data: the bound WeChat user / Feishu open-id persisted on the
    # aggregate (None = no binding). Backs the legacy
    # POST /sessions/{id}/wechat_notify + .../feishu_notify routes.
    wechat_notify_user_id: str | None = None
    feishu_notify_user_id: str | None = None
    # Append-only per §3.1 (V1 per-turn duration parity).
    # Wall-clock seconds of the most-recent CC/OC streaming turn,
    # rounded to 1 decimal place. ``None`` until the first completed
    # turn. Mirrors the legacy ``done`` SSE frame's ``duration_s``
    # (v1 ``backend/ai_coding/session_manager.py:2138-2140`` /
    # 2401-2416), surfaced via REST so the UI can display "本次会话耗时
    # X.X s" after a panel reload without replaying the stream.
    last_duration_s: float | None = None


class SessionListResponse(BaseModel):
    """Body of ``GET /api/{cc|oc}/sessions[/history/all]``."""

    sessions: list[CodingSessionResponse]


class TerminateSessionResponse(BaseModel):
    """Body of ``DELETE /api/{cc|oc}/sessions/{id}``."""

    session_id: str
    status: str


class InvokeToolRequest(BaseModel):
    """Body of ``POST /api/{cc|oc}/sessions/{id}/tools/invoke``."""

    tool_name: str = Field(..., min_length=1, max_length=128)
    args: dict[str, Any] = Field(default_factory=dict)


class ToolInvocationResponse(BaseModel):
    """Wire shape of a completed/failed tool invocation."""

    invocation_id: str
    tool_name: str
    status: str
    duration_ms: int | None = None
    result: dict[str, Any] | None = None
    error_code: str | None = None


class RequestPermissionBody(BaseModel):
    """Body of ``POST /api/{cc|oc}/sessions/{id}/permissions``."""

    tool_name: str = Field(..., min_length=1, max_length=128)
    args: dict[str, Any] = Field(default_factory=dict)


class PermissionRequestResponse(BaseModel):
    """Wire shape of a :class:`PermissionRequest`."""

    # OpenAPI ``$ref`` key stability (pure-refactor pin): this short name
    # collides with ``security.PermissionRequestResponse``.  Pydantic
    # disambiguates the JSON-schema ``$ref`` key by the model's
    # ``__module__``; pin it to the package module so the disambiguated
    # key matches the pre-split single-file ``ai_coding`` module.  The
    # package ``__init__`` re-exports ``Any`` so deferred forward-ref
    # resolution against ``sys.modules[__module__]`` still works.
    __module__ = "interfaces.http.routes.ai_coding"

    request_id: str
    tool_name: str
    args: dict[str, Any]
    decision: str
    requested_at: str
    decided_at: str | None = None


class DecidePermissionBody(BaseModel):
    """Body of ``POST /api/{cc|oc}/permissions/{req_id}/decide``.

    ``decision`` must be ``"approved"`` or ``"rejected"``; ``"pending"`` is
    rejected by the use case.
    """

    session_id: str = Field(..., min_length=1, max_length=128)
    decision: Literal["approved", "rejected"]
    # PR-095 / S9 H-13: optional operator edits forwarded to the
    # upstream ``can_use_tool`` callback.  Appended at the END so
    # existing clients that omit them keep working unchanged.
    updated_input: dict[str, Any] | None = None
    updated_permissions: list[dict[str, Any]] | None = None


class SkillRequest(BaseModel):
    """Body of ``POST /api/{cc|oc}/skills``."""

    name: str = Field(..., min_length=1, max_length=128)
    description: str = Field(..., min_length=1, max_length=4096)
    spec: dict[str, Any] = Field(default_factory=dict)


class SkillResponse(BaseModel):
    """Wire shape of a single :class:`Skill`."""

    name: str
    description: str
    spec: dict[str, Any]


class SkillListResponse(BaseModel):
    """Body of ``GET /api/{cc|oc}/skills``."""

    skills: list[SkillResponse]


class HealthResponse(BaseModel):
    """Body of ``GET /api/{cc|oc}/health``.

    The provider field reports which provider this prefix targets;
    ``available`` is true iff the underlying ``CodingProviderPort``
    advertises that provider via ``available_providers()``.

    PR-105 folds the legacy ``GET /api/oc/providers`` (and the
    parallel CC concept) into this response: ``providers`` lists the
    full set of provider metadata (id / name / per-provider
    available flag) and ``models`` lists the available model entries
    for adapters that advertise a model catalog.  Older clients
    looking only at the ``provider`` / ``available`` /
    ``available_providers`` fields continue to work unchanged.
    """

    # OpenAPI ``$ref`` key stability (pure-refactor pin): this short name
    # collides with ``system.HealthResponse``.  See the
    # ``PermissionRequestResponse`` note — pin ``__module__`` to the
    # package module so the disambiguated schema key is unchanged.
    __module__ = "interfaces.http.routes.ai_coding"

    provider: str
    available: bool
    available_providers: list[str]
    # PR-105: folded /providers + /models response (additive).
    providers: list[dict[str, Any]] = Field(default_factory=list)
    models: list[dict[str, Any]] = Field(default_factory=list)
    # U-5: legacy V1 footer parity (additive; older clients ignore).
    sdk_available: bool = False
    sdk_version: str = ""
    auth_configured: bool = False
    auth_source: str = "none"
    active_sessions: int = 0
    total_sessions: int = 0
    # C1: V1 ``/api/cc/models`` model-source badge parity (additive).
    # ``models_source`` is the 4-state string (upstream / cache /
    # fallback-no-key / fallback-error); the rest mirror V1's
    # ``/api/cc/models`` envelope (credential-stripped base_url + its
    # source + the fallback reason + cache age).  Empty/None when the
    # provider doesn't advertise the catalog probe (e.g. OpenCode).
    models_source: str = ""
    models_base_url: str = ""
    models_base_url_source: str = ""
    models_error: str = ""
    models_cached_age: float | None = None


# ---------------------------------------------------------------------------
# PR-104a: deferred legacy CC route DTOs
# ---------------------------------------------------------------------------


class GetSessionEnvelope(BaseModel):
    """Body of ``GET /api/cc/sessions/{session_id}``.

    Wraps :class:`CodingSessionResponse` in the legacy
    ``{"session": {...}}`` envelope.
    """

    session: CodingSessionResponse


class SendMessageRequest(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/messages`` (PR-104a).

    Mirrors the legacy ``CCMessageRequest`` shape but field names
    follow the new domain (``content`` text, optional ``client_request_id``).
    Image attachments (legacy ``image_b64``/``image_mime``,
    ``session_manager.py:1907-1929``) are forwarded through the use case
    to the provider's multimodal content block (HTTP: Anthropic
    base64 image source; SDK: streaming-input image part).  Both default
    to ``None`` so the historical text-only path is unchanged.
    """

    message: str = Field(..., min_length=1, max_length=262_144)
    client_request_id: str | None = Field(
        default=None, max_length=128
    )
    # C-2 (V1 multimodal parity): optional inline image attachment.  The
    # base64 payload excludes the ``data:<mime>;base64,`` prefix (V1
    # ``CCMessageRequest`` contract).  Both must be present for the image
    # to be forwarded; a lone value is ignored (text-only turn).
    image_b64: str | None = Field(default=None, max_length=20_000_000)
    image_mime: str | None = Field(default=None, max_length=128)


class SendMessageResponse(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/messages`` (PR-104a)."""

    message_id: str
    user_msg_id: str
    stream_url: str


class HistoryMessageEnvelope(BaseModel):
    """Single entry in the ``message_history`` list returned by GET /history.

    Legacy clients expect ``id`` / ``role`` / ``content`` /
    ``timestamp`` keys.  The new domain stores only the text on the
    aggregate so the route layer manufactures synthetic ids and
    timestamps; assistant turns are absent until the chat-side
    history projection (PR-105) lands.
    """

    id: str
    role: str
    content: str
    timestamp: int
    source: str


class HistoryResponse(BaseModel):
    """Body of ``GET /api/cc/sessions/{id}/history``."""

    session_id: str
    message_history: list[HistoryMessageEnvelope]


class RestoreRequest(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/restore`` (optional)."""

    fork: bool = False


class RestoreResponse(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/restore``."""

    session: CodingSessionResponse
    restored: bool
    forked: bool


class RenameRequest(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/rename``."""

    name: str = Field(..., min_length=1, max_length=256)


class RenameResponse(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/rename``.

    Wire shape matches legacy: ``{"ok": true, "session_id": "...",
    "name": "..."}``.
    """

    ok: bool
    session_id: str
    name: str


class WorkingDirRequest(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/working_dir``."""

    working_dir: str = Field(..., min_length=1, max_length=4096)


class WorkingDirResponse(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/working_dir``."""

    ok: bool
    session_id: str
    working_dir: str


class SetActiveResponse(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/set_active``.

    Legacy returned channel-specific fields (``wechat_user_id``);
    those land when the channels lane (L7) plugs the cross-BC
    bridge.  For now the response only confirms the marker.
    """

    ok: bool
    session_id: str
    active: bool


class EffortRequest(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/effort``."""

    effort: str | None = None


class EffortResponse(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/effort``."""

    ok: bool
    session_id: str
    effort: str | None


class WechatNotifyRequest(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/wechat_notify``.

    Mirrors the legacy ``{"wechat_user_id": "xxx"}`` shape; an empty
    string or ``null`` clears the binding.
    """

    wechat_user_id: str | None = None


class WechatNotifyResponse(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/wechat_notify`` (V1 parity)."""

    ok: bool
    session_id: str
    wechat_notify_user_id: str | None


class FeishuNotifyRequest(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/feishu_notify``.

    The legacy route used ``feishu_user_id``; the V2 frontend
    (``useCodingSession.ts``) currently sends ``feishu_open_id``.  Both
    keys are accepted so the already-wired frontend works unchanged;
    an empty string / ``null`` clears the binding.
    """

    feishu_user_id: str | None = None
    feishu_open_id: str | None = None


class FeishuNotifyResponse(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/feishu_notify`` (V1 parity)."""

    ok: bool
    session_id: str
    feishu_notify_user_id: str | None


class HardDeleteResponse(BaseModel):
    """Body of ``DELETE /api/cc/sessions/{id}/permanent``."""

    ok: bool
    deleted: str


class InterruptResponse(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/interrupt``.

    Legacy mirror: ``{"ok": true, "interrupted": true}`` on success;
    ``{"ok": false, "reason": "..."}`` when there was nothing to
    interrupt.
    """

    ok: bool
    interrupted: bool
    reason: str | None = None


class TruncateHistoryRequest(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/truncate_history``.

    Note on the index-vs-id parity guard: see
    ``src/qai/ai_coding/application/use_cases/truncate_history.py``
    for the contract — the new API takes a 0-based ``after_index``
    while the legacy clients sent ``after_msg_id`` strings.  The
    route layer handles both for backward compatibility.
    """

    after_index: int | None = Field(default=None, ge=0)
    after_msg_id: str | None = Field(default=None, max_length=128)
    include_self: bool = False


class TruncateHistoryResponse(BaseModel):
    """Body of ``POST /api/cc/sessions/{id}/truncate_history``."""

    ok: bool
    removed: int
    remaining: int


# ---------------------------------------------------------------------------
# PR-104b: config + credentials route DTOs
# ---------------------------------------------------------------------------


class CodingConfigResponse(BaseModel):
    """Body of ``GET /api/cc/config``.

    Wraps the persisted document in the legacy ``{"config": {...}}``
    envelope.  The document is intentionally a free-form dict so the
    WebUI can ship feature toggles without forcing a domain
    migration; sensitive values are NEVER present here (they live
    in ``GET /api/cc/credentials``).
    """

    config: dict[str, Any]


class SaveCodingConfigRequest(BaseModel):
    """Body of ``POST /api/cc/config``.

    Wraps the updates in the legacy ``{"config": {...}}`` envelope.
    The route layer validates a whitelist of known keys before
    forwarding to the use case.
    """

    config: dict[str, Any] = Field(default_factory=dict)


class SaveCodingConfigResponse(BaseModel):
    """Body of ``POST /api/cc/config``."""

    ok: bool
    updated_keys: list[str]


class CredentialStatusEnvelope(BaseModel):
    """Per-variable status surfaced via ``GET /api/cc/credentials``."""

    in_store: bool
    in_env: bool
    configured: bool


class CredentialsListResponse(BaseModel):
    """Body of ``GET /api/cc/credentials``.

    Mirrors the legacy ``{"credentials": {VAR: {...}, ...}}`` shape.
    """

    credentials: dict[str, CredentialStatusEnvelope]


class SaveCredentialsRequest(BaseModel):
    """Body of ``POST /api/cc/credentials``.

    Wraps the credential bag in the legacy ``{"credentials": {...}}``
    envelope.  The use case applies the legacy value semantics:
    empty string → delete; ``"****"`` → masked, skip; other → save.
    """

    credentials: dict[str, str] = Field(default_factory=dict)


class SaveCredentialsResponse(BaseModel):
    """Body of ``POST /api/cc/credentials``."""

    ok: bool
    saved: list[str]
    deleted: list[str]
    skipped: list[str]


class DeleteCredentialResponse(BaseModel):
    """Body of ``DELETE /api/cc/credentials/{var_name}``."""

    ok: bool
    deleted: str


# ---------------------------------------------------------------------------
# DTO conversion helpers
# ---------------------------------------------------------------------------


def _session_to_response(session: CodingSession) -> CodingSessionResponse:
    return CodingSessionResponse(
        session_id=str(session.session_id),
        provider=session.provider.value,
        workspace=session.workspace.path,
        status=session.status.value,
        title=session.title,
        created_at=session.created_at.isoformat(),
        terminated_at=(
            session.terminated_at.isoformat()
            if session.terminated_at is not None
            else None
        ),
        termination_reason=session.termination_reason,
        last_stream_sequence=session.last_stream_sequence,
        effort=session.config.effort,
        claude_session_id=session.claude_session_id,
        turn_count=len(session.messages),
        total_tool_calls=len(session.tool_invocations),
        wechat_notify_user_id=session.wechat_notify_user_id,
        feishu_notify_user_id=session.feishu_notify_user_id,
        last_duration_s=session.last_duration_s,
    )


def _permission_request_to_response(req: PermissionRequest) -> PermissionRequestResponse:
    return PermissionRequestResponse(
        request_id=str(req.request_id),
        tool_name=str(req.tool_name),
        args=dict(req.args),
        decision=req.decision.value,
        requested_at=req.requested_at.isoformat(),
        decided_at=(req.decided_at.isoformat() if req.decided_at is not None else None),
    )


def _skill_to_response(skill: Skill) -> SkillResponse:
    return SkillResponse(
        name=skill.name,
        description=skill.description,
        spec=dict(skill.spec),
    )


def _stream_frame_to_sse(frame: CodingStreamFrame) -> str:
    """Serialise a :class:`CodingStreamFrame` to an SSE wire chunk.

    Output forms
    ------------
    * ``StreamFrameKind.END``    -> ``event: done\\ndata: {}\\n\\n``
    * ``StreamFrameKind.ERROR``  -> ``event: error\\ndata: <payload>\\n\\n``
    * any other kind              -> ``event: message\\ndata: <json>\\n\\n``
      where ``<json>`` is ``{"kind": "...", "sequence": N, "payload": {...}}``.
    """
    kind = frame.kind.value
    if kind == "end":
        return "event: done\ndata: {}\n\n"
    if kind == "error":
        return f"event: error\ndata: {json.dumps(frame.payload, ensure_ascii=False)}\n\n"
    body = {
        "kind": kind,
        "sequence": frame.sequence,
        "payload": frame.payload,
    }
    return f"event: message\ndata: {json.dumps(body, ensure_ascii=False)}\n\n"
