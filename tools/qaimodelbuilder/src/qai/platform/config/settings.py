# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Settings (pydantic-settings) — the single source of truth for runtime config.

Layered loading (later overrides earlier):
1. defaults defined on the model
2. ``config/server.toml`` (production deployments)
3. ``$QAI_*`` environment variables
4. explicit kwargs to ``load_settings(**overrides)`` (tests / CLI flags)

No module-level mutable singleton: ``get_settings()`` is provided as a
convenience but must be wired through DI in production. Tests should always
construct ``Settings`` explicitly.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from qai.platform.errors import ConfigurationError

from .paths import DataPaths

# Where ``config/server.toml`` is searched for, relative to the repo root.
# Apps (apps/api/main.py) typically resolve the repo root explicitly.
_DEFAULT_TOML_NAME = "server.toml"

#: Edition self-report marker written into the repo root by the release
#: packaging pipeline (edition-dual-form-design.md §2.1). Its presence + an
#: ``edition`` field is the ONLY signal that flips the runtime to ``external``;
#: a dev source tree has no such file and defaults to ``internal``. NO
#: environment variable participates in edition resolution (hard constraint ①).
_BUILD_INFO_NAME = "build_info.json"

#: The two recognised editions. ``internal`` is the safe default for the dev
#: source tree (full feature set, zero config — hard constraint ③).
_VALID_EDITIONS: frozenset[str] = frozenset({"internal", "external"})
_DEFAULT_EDITION = "internal"

#: The loopback host literal. This is the single, allow-listed definition of
#: ``127.0.0.1`` for new code (``settings.py`` is on the
#: ``check_no_magic_host_port`` allowlist). Consumers that need a SECURE
#: loopback default — independent of the (possibly ``0.0.0.0``) public
#: ``server.host`` bind — import this constant instead of hard-coding the
#: literal, so the guard stays clean while the secure default is preserved
#: (e.g. ``user_prefs`` forge-config ``security.bind_host``).
LOOPBACK_HOST = "127.0.0.1"

#: All loopback peer aliases that should be treated as "the parent process
#: itself" (used by F-9 ``POST /api/security/child_request`` to deny any
#: non-loopback caller). Single source so route/CI guard literals stay
#: confined to this allow-listed file.
LOOPBACK_HOSTS: frozenset[str] = frozenset({LOOPBACK_HOST, "::1", "localhost"})

#: Public-bind sentinels that mean "all interfaces" — children must NOT
#: dial these; resolve them to :data:`LOOPBACK_HOST` instead.
PUBLIC_BIND_SENTINELS: frozenset[str] = frozenset({"0.0.0.0", "::", ""})

class ServerSettings(BaseModel):
    """HTTP / WebSocket server bind settings."""

    host: str = Field(default="127.0.0.1")
    # Default port aligned with the Okta SSO redirect_uri registered on the
    # authorization server (http://localhost:4099/callback). ``Start.bat``
    # passes ``--port 4099`` to the supervisor so the ACTUAL bind matches
    # this default; ``routes/auth.py`` derives ``redirect_uri`` from
    # ``server.port`` so keeping the two in sync is critical — Okta rejects
    # any mismatch. Change this only if the Okta app registration is also
    # updated to accept a different loopback port.
    port: int = Field(default=4099, ge=1, le=65535)
    reboot_exit_code: int = Field(
        default=75,  # external contract — see inventory 09 §7.2
        description="Exit code that signals the supervisor to restart the API.",
    )
    docs_enabled: bool = Field(
        default=True,
        description="Whether to expose /docs and /openapi.json.",
    )

    # ---- S-2 (CORS hardening) — appended; existing fields above unchanged ----

    cors_allow_origins: tuple[str, ...] = Field(
        default=(
            "http://127.0.0.1:4099",
            "http://localhost:4099",
            "http://127.0.0.1:8989",
            "http://localhost:8989",
            "http://127.0.0.1:5173",
            "http://localhost:5173",
        ),
        description=(
            "Explicit CORS allow-list of trusted browser origins (replaces the "
            "previous permissive ``allow_origins=['*']`` which is incompatible "
            "with ``allow_credentials=True`` and leaks the CSRF cookie to any "
            "origin). Defaults cover the SSO/packaged (4099), legacy packaged "
            "(8989) and dev (5173) WebUI hosts. Operators add production "
            "origins via ``[server] cors_allow_origins`` in server.toml or "
            "``QAI_SERVER__CORS_ALLOW_ORIGINS`` in env."
        ),
    )

    # ---- S-3 (OpenAI-compat bearer auth) — appended; align D2 -----------------

    openai_api_key: str | None = Field(
        default=None,
        description=(
            "Optional bearer token guarding the OpenAI-compatible ``/v1/*`` "
            "routes. When ``None`` (the default) the routes stay open — "
            "matching V1's transparent local-proxy behaviour. When set, "
            "callers must present ``Authorization: Bearer <key>`` or receive "
            "a 403 ``openai_compat.unauthorized`` envelope."
        ),
    )

    # ---- F-2(a) (SPA dist strict mode) — appended; existing fields unchanged --

    is_production: bool = Field(
        default=False,
        description=(
            "Production-mode flag for the packaged release. When ``True`` the "
            "SPA mount runs in *strict* mode: a missing ``frontend/dist/`` "
            "bundle serves a clear 503 maintenance page at ``/`` instead of "
            "silently skipping the mount, while ``/api/*``, ``/v1/*``, "
            "``/openapi.json``, ``/docs`` and ``/ws/*`` keep returning 404 so "
            "the API surface stays usable. The default ``False`` preserves the "
            "dev behaviour (warn + skip) for un-built checkouts."
        ),
    )

    @field_validator("host")
    @classmethod
    def _host_not_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("host must not be empty")
        return v


class LoggingSettings(BaseModel):
    """Logging configuration."""

    level: str = Field(default="INFO")
    fmt: str = Field(default="auto", description="'auto', 'json', or 'console'")

    @field_validator("level")
    @classmethod
    def _valid_level(cls, v: str) -> str:
        allowed = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in allowed:
            raise ValueError(f"level must be one of {sorted(allowed)}")
        return upper

    @field_validator("fmt")
    @classmethod
    def _valid_fmt(cls, v: str) -> str:
        allowed = {"auto", "json", "console"}
        lower = v.lower()
        if lower not in allowed:
            raise ValueError(f"fmt must be one of {sorted(allowed)}")
        return lower


class DataSettings(BaseModel):
    """Filesystem layout for runtime data.

    The ``data_dir`` is the only path injected from outside; every concrete
    location is derived through ``DataPaths`` so legacy literals never leak.
    """

    data_dir: Path = Field(default=Path("data"))
    config_dir: Path = Field(default=Path("config"))
    build_dir: Path = Field(default=Path("build"))

    @field_validator("data_dir", "config_dir", "build_dir", mode="before")
    @classmethod
    def _coerce_to_path(cls, v: Any) -> Path:
        return Path(v) if not isinstance(v, Path) else v


class SandboxSettings(BaseModel):
    """Sandbox runtime configuration (S9 PR-094 §17.5 #6/#7).

    .. note::

        Phase 3 cleanup (2026-07-01) — the Windows AppContainer/LPAC
        sandbox execution chain (``SandboxedProcessRunner`` adapter,
        ``SandboxPolicyBuilder``, ``DaemonManager`` infrastructure,
        ``launcher_resolver`` infrastructure) has been deleted. The
        de-sandbox refactor (2026-07-04) then removed the remaining
        orphaned security-side sandbox execution framework (the
        execute-sandboxed use cases, the sandbox routing / state-machine
        helpers and their config VO). The fields on this class
        (``bypass_command_patterns`` / ``memory_limit_mb`` /
        ``state_machine_enabled``) plus the sibling
        ``SecuritySettings.sandbox_enabled`` /
        ``SecuritySettings.sandbox_launcher_path`` are **retained as
        field-name placeholders** per v2.7 §3.1 (field-name lock), but
        their values no longer drive OS isolation or process execution;
        any flag flip is inert with respect to how commands run (the
        live exec path is the plain
        :class:`qai.platform.process.subprocess_runner.SubprocessProcessRunner`
        gated by FileGuard).

        The fields stay because (a) test fixtures, the
        ``/api/security/runtime-config`` PUT/GET surface and the
        forge_config persistence layer all reference them; (b) any future
        file-protection / OS-isolation scheme can reuse the existing field
        names without touching the route / DB shapes.
    """

    bypass_command_patterns: tuple[re.Pattern[str], ...] = Field(
        default_factory=lambda: (
            re.compile(r"^\s*git\s+(status|diff|log|show|branch|tag|remote)\b"),
            re.compile(r"^\s*ls\b"),
            re.compile(r"^\s*pwd\s*$"),
            re.compile(r"^\s*echo\b"),
            re.compile(r"^\s*cat\s+\S+\s*$"),
            re.compile(r"^\s*head\b"),
            re.compile(r"^\s*tail\b"),
            re.compile(r"^\s*which\b"),
            re.compile(r"^\s*where\b"),
            re.compile(r"^\s*type\b"),
        ),
        description=(
            "Regex patterns for commands that skip the sandbox-mode "
            "routing check (read-only / introspection commands). "
            "Retained per v2.7 §3.1 field-name lock; the OS-isolation "
            "sandbox chain was removed 2026-07-01, so both routing "
            "branches now execute through the plain subprocess runner."
        ),
    )
    memory_limit_mb: int = Field(
        default=2048,
        ge=128,
        le=65536,
        description=(
            "Legacy per-execution memory ceiling in MB. Retained per "
            "v2.7 §3.1 field-name lock; the sandbox launcher chain "
            "that consumed this value was removed 2026-07-01."
        ),
    )
    state_machine_enabled: bool = Field(
        default=True,
        description=(
            "Legacy explicit state-machine toggle (vs. flat enabled/mode "
            "dict). Retained per v2.7 §3.1 field-name lock; the sandbox "
            "state-machine helper that consumed it was removed 2026-07-04."
        ),
    )

    model_config = {"arbitrary_types_allowed": True}


class SecuritySettings(BaseModel):
    """Security knobs (small set during S1; expanded in S2 / S4 / S9)."""

    csrf_enabled: bool = Field(default=True)
    csrf_cookie_name: str = Field(default="qai_csrf")
    csrf_header_name: str = Field(default="X-QAI-CSRF")
    csrf_token_bytes: int = Field(
        default=32,
        ge=16,
        le=128,
        description="Bytes of entropy for the CSRF token (urlsafe-encoded).",
    )
    csrf_path_allowlist: tuple[str, ...] = Field(
        default=(
            "/api/system/health",
            "/api/system/build-info",
            "/api/system/edition",
            "/openapi.json",
            "/docs",
            "/docs/oauth2-redirect",
            "/redoc",
            # S-3 (align D2): OpenAI-compat routes carry their own optional
            # bearer auth (``server.openai_api_key``) and are consumed by
            # non-browser SDK clients that cannot fetch a CSRF token; webhook
            # receivers authenticate via per-channel HMAC signature
            # verification, so CSRF (a browser-only defence) is both
            # unnecessary and would break the integration.
            "/v1/",
            "/api/wechat/webhook",
            "/api/feishu/webhook",
            # SSO login surface: /auth/login, /callback, /auth/logout are
            # GET-only and reached via top-level browser navigation (Okta
            # cross-site redirect on /callback cannot present a CSRF token),
            # /api/auth/me is a read-only GET. All safe methods so CSRF is
            # not enforced against them anyway, but listing them keeps the
            # allowlist honest about intent.
            "/auth/",
            "/callback",
            "/api/auth/me",
        ),
        description=(
            "URL path prefixes exempt from CSRF (read-only public endpoints "
            "and OpenAPI docs). Sensitive POSTs such as /api/system/reboot "
            "are intentionally NOT in this list. The OpenAI-compat ``/v1/`` "
            "surface and the HMAC-verified channel webhooks are exempt "
            "because they are not browser-driven (S-3 / align D2)."
        ),
    )
    sandbox_enabled: bool = Field(
        default=False,
        description=(
            "Runtime exec-branch selector; formerly the OS-isolation "
            "sandbox master switch. The OS-isolation layer was removed "
            "on 2026-07-01 (see docs/85-tasks/windows-acl-sandbox-cleanup-"
            "2026-07-01.md); the field name is retained per v2.7 §3.1 "
            "(field-name lock) and its value is **still consulted at DI "
            "build time** to select between two equivalent runner-routed "
            "exec branches (a change requires reboot). Both branches now "
            "execute directly through :class:`SubprocessProcessRunner`, "
            "so the flag no longer drives OS isolation — the actual IO "
            "guardrails come from the Protected Paths / FileBroker "
            "software layers + the permission-approval workflow. Not "
            "marked ``deprecated=True`` because the field is still an "
            "active configuration input; only its OS-isolation semantics "
            "are gone. Defaults OFF (user decision 2026-06-09)."
        ),
    )
    smart_approval_enabled: bool = Field(
        default=False,
        description=(
            "When False the SmartApprovalAdapter unconditionally returns "
            "UNDECIDED, deferring every request to a human reviewer."
        ),
    )

    audit_hook_enabled: bool = Field(
        default=False,
        description=(
            "Enable PEP 578 audit hook installation during lifespan startup. "
            "When True, the hook intercepts configured syscall-level events and "
            "evaluates them against the active Policy. S9 PR-092 §2.2 H-17."
        ),
    )

    # ---- S9 PR-092 fields (appended; existing fields above are not renamed) ----

    policy_hot_reload_enabled: bool = Field(
        default=False,
        description=(
            "Enable Policy file mtime watcher to trigger UpdatePolicyUseCase "
            "on user file edits. S9 PR-092 §17.5 #10. Defaults OFF (user "
            "decision 2026-06-09): with the PolicyCenter / FileGuard stack "
            "disabled by default there is no live policy to hot-reload, so the "
            "watcher would only add an idle background task. Opt in alongside "
            "``file_guard_enabled`` when the PolicyCenter stack is enabled."
        ),
    )
    policy_decision_cache_size: int = Field(
        default=2000,
        ge=0,
        le=100_000,
        description=(
            "LRU cache size for Policy.evaluate() decisions keyed by "
            "(operation, path). 0 disables the cache. S9 PR-092 §17.5 #9."
        ),
    )
    smart_approval_llm_endpoint: str | None = Field(
        default=None,
        description=(
            "HTTP endpoint for the Smart Approval LLM-based risk assessment. "
            "When set, ``SmartApprovalLLMAdapter`` performs an LLM call instead "
            "of returning UNDECIDED. S9 PR-092 §17.5 #8."
        ),
    )
    smart_approval_llm_model: str | None = Field(
        default=None,
        description=(
            "Model ID to use for the Smart Approval LLM call (e.g. "
            "'gpt-4o-mini'). Required when ``smart_approval_llm_endpoint`` "
            "is set."
        ),
    )
    audit_hook_extra_events: tuple[str, ...] = Field(
        default=("os.scandir", "os.listdir", "shutil.copyfile"),
        description=(
            "Additional CPython audit events the audit hook intercepts on top "
            "of the built-in set. S9 PR-092 §2.2 H-17."
        ),
    )
    sandbox_launcher_path: Path | None = Field(
        default=None,
        description=(
            "Legacy pointer to an OS-isolation sandbox launcher binary. "
            "The launcher chain that consumed this field was removed on "
            "2026-07-01 (the security-side sandbox execution framework "
            "that snapshotted it was deleted 2026-07-04). The field is "
            "retained "
            "per v2.7 §3.1 (field-name lock) so operator config files / "
            "test fixtures that set it continue to validate; the value "
            "is ignored by execution. Not marked ``deprecated=True`` "
            "because ``SecuritySettings`` is pydantic-validated on every "
            "settings load (including tests with ``filterwarnings=error``) "
            "and the field name remains a live schema slot."
        ),
    )

    sandbox: SandboxSettings = Field(default_factory=SandboxSettings)

    # ---- S-4 (bind-host hardening) — appended; align D3 / V1 parity ----------

    bind_host: str = Field(
        default=LOOPBACK_HOST,
        description=(
            "Public bind host for the HTTP/WS server. V1 parity "
            "(``forge_config_manager.bind_host``): only ``127.0.0.1`` "
            "(loopback, the secure default) and ``0.0.0.0`` (all interfaces, "
            "LAN-exposed) are accepted; any other value falls back to "
            "loopback. Binding ``0.0.0.0`` additionally auto-disables the "
            "OpenAPI docs surface (S-8 / align D5) to avoid exposing the API "
            "schema to the LAN."
        ),
    )

    @field_validator("bind_host")
    @classmethod
    def _valid_bind_host(cls, v: str) -> str:
        # V1 anchor: forge_config_manager.py:332-336 — accept only the two
        # canonical values, otherwise fall back to the secure loopback.
        allowed = {LOOPBACK_HOST, "0.0.0.0"}
        return v if v in allowed else LOOPBACK_HOST

    # ---- S-1 / D11 (FileGuard master switch) — appended; V1 parity ----------

    file_guard_enabled: bool = Field(
        default=True,
        description=(
            "FileGuard master switch. Ships ON (2026-07-04): the per-tool "
            "PolicyCenter enforcement + the allow/deny/ASK tri-state are "
            "active out-of-the-box so file access is guarded by default. "
            "When False the ``FileGuardFacade`` bridge "
            "(``apps/api/_file_guard_bridge.py``) is a pass-through; when "
            "True every ai_coding production tool consults PolicyCenter + "
            "the dep/exec brokers before reading / writing / executing. "
            "(Historically shipped OFF for V1 parity; flipped to ON so the "
            "path allow/deny/ASK tri-state + session-scoped grants take "
            "effect by default — the ALWAYS-ON ``protected_paths`` guard was "
            "the only default protection before.)"
        ),
    )
    allow_exec_tool: bool = Field(
        default=True,
        description=(
            "Whether the ai_coding ``exec`` tool is permitted at all. V1 "
            "parity (``forge_config_manager.py:341`` default True). When "
            "False the FileGuard ``enforce_exec`` gate hard-denies before "
            "consulting any broker."
        ),
    )
    protected_write_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "EXTRA absolute path prefixes the agent must never write to / "
            "delete / truncate, merged on top of the built-in, non-removable "
            "set (currently ``C:\\Qualcomm`` — the QAIRT SDK / Qualcomm "
            "toolchain root). Enforced by ``qai.platform.protected_paths`` at "
            "the write/edit/apply_patch tool handlers, the exec command write-"
            "target scan, AND injected into Python child processes via the "
            "``QAI_PROTECTED_PATHS`` env var + child sitecustomize hook. This "
            "guard is ALWAYS ON and is INDEPENDENT of ``file_guard_enabled`` / "
            "the native OS hook (regardless of their state — both "
            "``file_guard_enabled`` and ``native_file_guard_enabled`` ship "
            "ON): even with every optional security module off, "
            "the built-in protected tree cannot be "
            "corrupted by the model. Users may ADD prefixes here; they can "
            "never remove a built-in one. Origin: 2026-06-16 incident where a "
            "stray agent write truncated ``qnn-context-binary-generator.exe`` "
            "to 0 bytes, breaking model builds with ``[WinError 193]``."
        ),
    )
    # ------------------------------------------------------------------
    # Native FileGuard hook (2026-07-04) — tail-appended per §3.1 field
    # lock (append-only; no existing field renamed/removed). Wires the
    # compiled ``vendor/bin/<arch>/guard64.dll`` OS-level file-access
    # hook that intercepts the file events LLM-spawned *subprocesses*
    # trigger (which bypass the Python ai_coding tool layer that
    # ``file_guard_enabled`` gates). Ships ON by default (2026-07-06):
    # the DLL is loaded / hooked at startup when
    # ``native_file_guard_enabled`` is True.
    # ------------------------------------------------------------------
    native_file_guard_enabled: bool = Field(
        default=True,
        description=(
            "Master switch for the native (guard64.dll) OS-level file "
            "hook. Ships ON by default (2026-07-06): when True the API "
            "process loads the arch-appropriate "
            "``vendor/bin/<arch>/guard64.dll`` at startup and routes "
            "intercepted subprocess file events through the PolicyCenter "
            "ASK pipeline. To avoid an ASK flood from subprocesses reading "
            "the vast system read surface (System32 / the Python runtime / "
            "Program Files), those system read paths are seeded into the "
            "op-aware read-only whitelist (read allowed, write -> ASK). "
            "Unmatched paths still go to ASK (never silently allowed). "
            "Independent of ``file_guard_enabled`` (which gates the "
            "in-process Python ai_coding tool layer, not OS-level "
            "subprocess events)."
        ),
    )
    allow_x86_processes: bool = Field(
        default=False,
        description=(
            "Allow spawning 32-bit (x86) child processes from exec / "
            "background_process tools. Default OFF: guard64.dll cannot "
            "inject into 32-bit processes, so they would run completely "
            "unmonitored (bypassing all file-access security policy). "
            "Enable ONLY if you need a specific 32-bit tool and accept "
            "that it will not be subject to FileGuard protection. "
            "When enabled, sets QAI_GUARD_ALLOW_X86=1 in the child env."
        ),
    )
    native_file_guard_dll_path: Path | None = Field(
        default=None,
        description=(
            "Explicit path to guard64.dll. When None (default) the "
            "adapter resolves ``vendor/bin/<arch>/guard64.dll`` relative "
            "to the repo root, picking ``arm64`` / ``x64`` from the "
            "current process architecture. Set this only to override the "
            "bundled DLL (e.g. a locally rebuilt artefact)."
        ),
    )
    native_file_guard_fail_closed: bool = Field(
        default=True,
        description=(
            "fail-closed semantics for the native hook. When True "
            "(default) a DLL crash / callback timeout / filter exception "
            "resolves to DENY; when False such conditions allow the "
            "operation (fail-open). Kept True by default so a bridge "
            "fault cannot silently open a protected path."
        ),
    )
    native_file_guard_callback_timeout_ms: int = Field(
        default=60000,
        ge=0,
        description=(
            "Milliseconds the native hook waits for the Python filter "
            "callback (the ASK round-trip) before applying the "
            "``native_file_guard_fail_closed`` policy. Mirrors the 60s "
            "PolicyCenter ASK ceiling. 0 uses the DLL's built-in default."
        ),
    )
    # ------------------------------------------------------------------
    # Three-state whitelist (2026-07-05) — tail-appended per §3.1 field
    # lock (append-only). The INVERSE of ``protected_write_paths``: extra
    # absolute path *prefixes* that are ALWAYS ALLOWED (read/write/execute,
    # op-agnostic, subtree-covering) WITHOUT prompting — globally, i.e.
    # independent of the collaboration session. Enforced identically in
    # both FileGuard layers from this single source of truth (State-Truth-
    # First): the native guard64.dll allow (white) prefix list
    # (``add_allow_rule(path, session_only=False)``) AND the in-process
    # Python check_permission ALLOW short-circuit (a prefix match returns
    # ALLOW before the policy / grant / ASK cascade, with the exec-deny
    # hard-deny gate still re-checked so a protected-path deny can never be
    # bypassed). The apps layer seeds the four data/models roots
    # (``<workspace_root>/{data,models}`` +
    # ``%LOCALAPPDATA%\\QAIModelBuilder\\{data,models}``) into this at
    # startup; operators may ADD further global-allow prefixes here. The
    # per-session workspace directory subtree is NOT seeded here — it is
    # session-scoped (covered by the workspace session grant + a session-
    # aware prefix provider), so it never widens the global allow surface.
    global_allow_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "Absolute path prefixes that are ALWAYS ALLOWED (read / write / "
            "execute, op-agnostic, subtree-covering) for ANY session without "
            "prompting — the inverse of ``protected_write_paths``. Single "
            "source of truth shared by BOTH FileGuard layers: the native "
            "guard64.dll allow (white) prefix list and the in-process Python "
            "``CheckPermissionUseCase`` prefix ALLOW short-circuit. The "
            "exec-deny hard-deny gate is still re-checked on execute so a "
            "protected-path deny is never bypassed. The apps layer seeds the "
            "four data/models roots (``<workspace_root>/data`` + "
            "``<workspace_root>/models`` + "
            "``%LOCALAPPDATA%\\QAIModelBuilder\\data`` + "
            "``%LOCALAPPDATA%\\QAIModelBuilder\\models``) here at startup; "
            "operators may add more. The per-session workspace directory is "
            "handled separately (session-scoped) and is NOT part of this "
            "global surface."
        ),
    )
    # ------------------------------------------------------------------
    # Op-aware READ-ONLY whitelist (2026-07-06) — tail-appended per §3.1
    # field lock (append-only). Unlike ``global_allow_paths`` (which allows
    # read/write/execute, op-agnostic), these prefixes allow READ ONLY:
    # read is permitted without prompting, but write / edit / delete /
    # execute still go through ASK (never silently permitted, never a hard
    # deny). Enforced in BOTH FileGuard layers from these operator-supplied
    # extras plus the apps-layer seeded set: the native guard64.dll
    # op-aware read-only whitelist (``add_read_only_allow_rule`` -> the
    # DLL's ``AddReadOnlyWhiteRules`` export, which only skips the callback
    # for read events) AND the in-process Python ``CheckPermissionUseCase``
    # read-only ALLOW short-circuit (matches only when the request is
    # read-only). The apps layer seeds the three business dirs
    # (``<repo_root>/skills`` + ``<repo_root>/factory/chat_features`` +
    # ``<repo_root>/factory/app_builder``) into ``read_only_allow_paths``
    # and the system read surface (``%SystemRoot%``, the Python runtime
    # prefixes, ``%ProgramFiles%`` / ``%ProgramFiles(x86)%``) into
    # ``system_read_allow_paths`` at startup; the two exist as separate
    # operator knobs so the business vs system read surfaces can be tuned
    # independently. ``protected_write_paths`` (black) still wins over any
    # whitelist.
    read_only_allow_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "EXTRA absolute path prefixes allowed for READ only (write / "
            "edit / delete / execute still prompt via ASK). Business "
            "read-only surface — operators may add prefixes here; the apps "
            "layer additionally seeds ``<repo_root>/skills`` + "
            "``<repo_root>/factory/chat_features`` + "
            "``<repo_root>/factory/app_builder``. Enforced by both FileGuard "
            "layers (native op-aware read-only whitelist + Python read-only "
            "ALLOW short-circuit). ``protected_write_paths`` (black) still "
            "wins."
        ),
    )
    system_read_allow_paths: tuple[str, ...] = Field(
        default=(),
        description=(
            "EXTRA absolute path prefixes for the SYSTEM read surface, "
            "allowed for READ only (write / edit / delete / execute still "
            "prompt via ASK). Operators may add prefixes here; the apps "
            "layer additionally seeds ``%SystemRoot%``, the Python runtime "
            "prefixes (``sys.prefix`` / ``sys.base_prefix`` / the "
            "interpreter dir / site-packages) and ``%ProgramFiles%`` / "
            "``%ProgramFiles(x86)%`` so LLM-spawned subprocesses reading the "
            "system read surface do not flood the ASK pipeline. "
            "``protected_write_paths`` (black) still wins."
        ),
    )
    # ------------------------------------------------------------------
    # Phase 2 (2026-07-06) — durable pending-permission persistence.
    # Tail-appended per §3.1 field lock (append-only; no existing field
    # renamed/removed). Gates whether in-flight FileGuard ASK popups are
    # mirrored to the ``security_pending_permission`` table (migration
    # 052) so a service restart with unanswered ASKs can rehydrate the
    # UI's pending list, mark orphaned rows (boot_id != current), and
    # feed :class:`PendingCleanupService` for subprocess-gone sweeps.
    # Default True (production behaviour); False wires a no-op store so
    # tests / in-memory deployments never touch the table. The Phase 2
    # infinite-wait semantic on ``PermissionWaitRegistry.wait`` and the
    # 10s cleanup scan cadence are unaffected by this flag — they run
    # against the in-memory registry regardless.
    # ------------------------------------------------------------------
    permission_pending_persist: bool = Field(
        default=True,
        description=(
            "Phase 2 (2026-07-06): when True (default) pending FileGuard "
            "ASK requests are persisted to ``security_pending_permission`` "
            "(migration 052) and survive service restarts. When False "
            "(test / legacy / in-memory) the durable mirror is disabled "
            "via a null store; the in-memory ``PermissionWaitRegistry`` "
            "remains authoritative for the live process. The cleanup "
            "service still runs and the native ``INFINITE`` wait semantic "
            "(no auto-DENY on elapse) is unaffected either way."
        ),
    )


class ToolParallelismSettings(BaseModel):
    """Cross-agent tool concurrency budget (PARALLEL-TOOL-1, design §5).

    Bounds how many tool calls run concurrently across the whole chat turn —
    the main agent and all sub-agents share ONE budget so the
    "N sub-agents x M tools x exec subprocess" fan-out cannot storm the
    machine. A non-positive value means "unbounded" (no semaphore).
    """

    total: int = Field(
        default=8,
        ge=0,
        le=64,
        description=(
            "Max concurrent tool calls across the whole chat turn (main + "
            "sub-agents share this budget). 0 = unbounded. Conservative "
            "default (8) suits NPU inference machines + Windows subprocess "
            "spawn cost."
        ),
    )
    exec_budget: int = Field(
        default=2,
        ge=0,
        le=32,
        description=(
            "Max concurrent ``exec`` tool calls (the heaviest category — each "
            "spawns an OS subprocess). Sub-budget within ``total``. 0 = "
            "unbounded. Kept small (2) to avoid subprocess/CPU/disk storms."
        ),
    )


class ChatSettings(BaseModel):
    """Chat-context tunables (S9 PR-091)."""

    llm_base_url: str | None = Field(
        default=None,
        description=(
            "Base URL for the chat LLM endpoint (OpenAI-compatible). "
            "When None, chat falls back to offline/stub title generation."
        ),
    )
    llm_api_key: str | None = Field(
        default=None,
        description=(
            "Bearer token / API key for the chat LLM endpoint. "
            "When None, requests are sent without Authorization header."
        ),
    )
    llm_default_model: str = Field(
        default="qai-default",
        description=(
            "Default model ID used by the chat model resolver when no "
            "per-request hint is provided."
        ),
    )

    llm_stream_timeout_seconds: float = Field(
        default=120.0,
        ge=1.0,
        description=(
            "Base HTTP timeout (connect / write / pool) for the cloud chat "
            "stream, in seconds.  The SSE *read* timeout is derived from this "
            "as ``base * 5`` (default 120 → read 600s), mirroring V1 "
            "``cloud_shared.timeout_seconds`` (config_manager.py:453-454 = 120) "
            "and ``httpx.Timeout(timeout, read=timeout * 5)`` "
            "(chat_handler.py:1385).  httpx's read timeout is an *idle-gap* "
            "limit — the max time between two received SSE data chunks, NOT the "
            "whole-turn duration — so as long as the model keeps emitting tokens "
            "(or the gateway sends keep-alive lines) the timer keeps resetting "
            "and the turn never times out.  A long read window therefore means "
            "'the model may pause this long between tokens before we give up', "
            "which is exactly what lets a model spend a while organising a long "
            "tool-call argument (e.g. a big code block) without being cut off. "
            "Raise this only if a model legitimately pauses longer than ~600s "
            "mid-stream."
        ),
    )

    question_timeout_seconds: float = Field(
        default=0.0,
        ge=0.0,
        description=(
            "Hard cap (in seconds) on how long the ``question`` harness tool "
            "blocks waiting for the user to answer the in-chat dialog. "
            "Default ``0`` disables the cap entirely: the dialog stays open "
            "until the user answers or the stream is aborted (a 'stop'/abort "
            "still dismisses it cleanly via the CancelledError path), so a "
            "question is never auto-cancelled out from under the user. "
            "Set a positive value to install a hard cap — when reached the "
            "tool returns a ``[question timeout]`` result so the model can "
            "proceed and the pending dialog is cancelled; use this only when "
            "you deliberately want forgotten dialogs to auto-expire (e.g. to "
            "stop a forgotten dialog pinning an SSE/WS connection open)."
        ),
    )

    # ---- S-7 (chat action hooks execution gate) — appended; align D4 ---------

    hooks_enabled: bool = Field(
        default=False,
        description=(
            "Master execution gate for chat action hooks (the 🪝 Hooks tab / "
            "BC-135 V2 enhancement). When ``False`` (the secure default) the "
            "hook engine never spawns operator-configured subprocess commands "
            "— ``has_hook`` reports False and ``fire`` is a no-op — even when "
            "hooks are present in config. Operators must explicitly opt in via "
            "``[chat] hooks_enabled = true`` / ``QAI_CHAT__HOOKS_ENABLED`` to "
            "enable arbitrary-command execution. The Hooks configuration UI "
            "stays available regardless (config-only when disabled)."
        ),
    )

    # ---- PARALLEL-TOOL-1 (cross-agent tool concurrency budget) — appended ----

    tool_parallelism: ToolParallelismSettings = Field(
        default_factory=ToolParallelismSettings,
        description=(
            "Concurrency budget for parallel tool execution (main + "
            "sub-agents share it). See ToolParallelismSettings."
        ),
    )

    # ---- SUB-AGENT recursion depth (unified spawn path) — appended (§3.1) ----

    chat_max_spawn_depth: int = Field(
        default=8,
        ge=1,
        le=64,
        description=(
            "Hard ceiling on sub-agent recursion depth. Depth 1 = a first-level "
            "sub-agent spawned by the main agent; depth 2 = a grand sub-agent "
            "(a sub-agent's sub-agent); and so on. Once ``spawn_depth`` reaches "
            "this ceiling the ``agent`` tool call returns a diagnostic ``[error: "
            "max sub-agent nesting depth (N) reached]`` string instead of "
            "spawning another level — recursion 封顶 without a hard "
            "``allow_spawn=False`` in the code path. The per-level user opt-in "
            "toggle (``allow_spawn`` on each sub-agent) is orthogonal and still "
            "controls whether THIS run may spawn its direct children at all — "
            "in practice the LLM-visible ``agent`` tool schema does NOT expose "
            "``allow_spawn`` as an argument, so a nested spawn's grant defaults "
            "to False (pre-α parity: a grand sub-agent could never itself "
            "spawn). ``chat_max_spawn_depth`` is therefore mainly a defence-in-"
            "depth belt for any future UI that decides to expose per-level "
            "opt-in on the `agent` tool arguments. Default 8 covers realistic "
            "deep-agent workflows once such an opt-in ships; bump only if you "
            "deliberately need deeper planner/executor stacks."
        ),
    )

    # ---- MCP (Model Context Protocol) server integration — appended (§3.1) ---

    chat_mcp_enabled: bool = Field(
        default=False,
        description=(
            "Master execution gate for MCP (Model Context Protocol) servers. "
            "When ``False`` (the secure default) the chat MCP registry never "
            "spawns an external stdio subprocess nor opens an sse/http session "
            "— configured servers can still be listed / added / edited in the "
            "Settings UI, but they are not connected and their tools are not "
            "advertised to the LLM. Because an MCP stdio server is an arbitrary "
            "local subprocess, operators must explicitly opt in via ``[chat] "
            "chat_mcp_enabled = true`` / ``QAI_CHAT__CHAT_MCP_ENABLED`` to allow "
            "MCP servers to connect and contribute tools. Mirrors the "
            "``hooks_enabled`` secure-by-default gate."
        ),
    )

    # ---- Per-conversation token-budget cap (max_budget_tokens) — appended (§3.1) ---

    chat_budget_enabled: bool = Field(
        default=True,
        description=(
            "Master gate for the per-conversation TOKEN-budget cap "
            "(``max_budget_tokens`` — renamed from the CC SDK's "
            "``max_budget_usd`` since this project has no cross-provider USD "
            "pricing but does have accurate provider-measured token counts). "
            "When ``True`` (the default) the DI wires the "
            "``ConversationBackedBudgetTracker`` so a per-conversation cap "
            "(set via ``PATCH /conversations/{id}/budget``) is ENFORCED at "
            "each agentic-round boundary from provider-authoritative usage, and "
            "the running ``used_tokens`` counter accumulates the WHOLE "
            "conversation tree (main agent + all sub-/grand-sub-agents share "
            "the same ``root_conversation_id`` budget pool). When ``False`` the "
            "DI wires a no-op ``NullBudgetTracker`` so a conversation's optional "
            "``meta['budget']`` cap is NOT enforced / counted and every turn "
            "behaves byte-for-byte as before (State-Truth-First — never an "
            "estimate; local / on-device turns are never counted or blocked)."
        ),
    )

    chat_budget_raise_pct: int = Field(
        default=20,
        ge=1,
        le=1000,
        description=(
            "When a conversation reaches its per-conversation TOKEN budget cap "
            "(``max_budget_tokens``) and the user chooses 'continue' in the "
            "interactive budget-decision dialog, the cap is raised by THIS "
            "percentage (default 20 → +20%) via "
            "``BudgetTrackerPort.set_max_tokens`` and the SAME turn resumes. "
            "The dialog shows the resulting new cap so the user knows the "
            "auto-raise amount before confirming."
        ),
    )


class AppBuilderSettings(BaseModel):
    """App Builder runtime tunables (S9 PR-094)."""

    result_cache_enabled: bool = Field(
        default=True,
        description=(
            "Enable the LRU cache of recent run results. §17.5 #12. The cache "
            "is for DISPLAY only: when the user switches away from a model's "
            "App Builder view and later returns, the last result is shown from "
            "this cache. It is NOT a substitute for inference — clicking the "
            "Run/Inference button always performs a real NPU inference and "
            "never replays a cached result (user mandate 2026-06-07). The run "
            "use case therefore bypasses the cache on read and only writes to "
            "it after a real run, so subsequent view re-entry can display it. "
            "Sticky-worker model residency is a separate mechanism."
        ),
    )
    result_cache_max_entries: int = Field(
        default=64,
        ge=0,
        le=4096,
        description="Maximum number of cached run results.",
    )
    result_cache_ttl_seconds: int = Field(
        default=3600,
        ge=0,
        le=86_400,
        description="TTL (seconds) for cached run results.",
    )
    dep_checker_enabled: bool = Field(
        default=True,
        description=(
            "Enable runtime dependency checker / dynamic pack pip installer. "
            "S9 PR-094 §17.5 #11."
        ),
    )


class ChannelsSettings(BaseModel):
    """IM channels (WeChat / Feishu / WeCom) tunables (S9 PR-093)."""

    context_token_age_guard_seconds: int = Field(
        default=90,
        ge=0,
        le=3600,
        description=(
            "Maximum age (seconds) for a channel context token before "
            "real-time delivery falls back to send_to_user. §2.2 H-14."
        ),
    )


class ServiceSettings(BaseModel):
    """Inference service / launcher tunables (S9 PR-095)."""

    log_buffer_size: int = Field(
        default=6000,
        ge=0,
        le=1_000_000,
        description=(
            "Number of stdout/stderr log lines retained per service for the "
            "Service Logs SSE stream. Default 6000 aligns with V1 "
            "``ServiceManager.LOG_BUFFER_SIZE``. §3.3 A-26."
        ),
    )


class ModelCatalogSettings(BaseModel):
    """Model catalog tunables (S9 PR-097 §4bis residual cleanup).

    Promotes the previously hard-coded release manifest URL constant from
    ``apps/api/_model_catalog_di.py`` into a typed setting so operators
    can override it via TOML / env without patching DI wiring.
    """

    release_manifest_url: str = Field(
        default="https://qai.example.com/release-manifest.json",
        description=(
            "HTTPS URL for the model release manifest consumed by "
            "``HttpReleaseManifestFetcher``. Operators override via "
            "``[model_catalog] release_manifest_url`` in server.toml or "
            "``QAI_MODEL_CATALOG__RELEASE_MANIFEST_URL`` in env."
        ),
    )


class ModelRuntimeSettings(BaseModel):
    """Inference service (GenieAPIService) runtime configuration.

    Controls how ``ProcessBackedInferenceService`` locates and launches
    the local inference binary. Fields mirror the v1
    ``forge_config.json → service_launch`` section.
    """

    install_dir: str = Field(
        default="",
        description=(
            "Directory containing the inference service binary. "
            "Empty string triggers StubInferenceService fallback."
        ),
    )
    default_port: int = Field(
        default=8910,
        ge=1,
        le=65535,
        description=(
            "Default TCP port the inference binary listens on. Matches V1 "
            "``forge_config.service_launch.local_port`` default (8910); the "
            "forge.config override still takes precedence at start time."
        ),
    )
    exe_name: str = Field(
        default="GenieAPIService.exe",
        description="Filename of the inference binary within install_dir.",
    )


class ToolsSettings(BaseModel):
    """AI-coding tool-execution tunables (P2-wiring 7-L1 / 7-L3).

    Consumed by the ``apps/api`` tool-bridge wiring root, which installs
    these values into the ``qai.ai_coding`` tool handlers' module-level
    config seams (``handlers/web.py`` ``set_ssl_verify`` /
    ``handlers/_shared.py`` ``set_project_skip_dirs``) at tool-bridge build
    time. Mirrors V1 ``forge_config.version_check.ssl_verify`` (7-L1) and
    the project sandbox ``skip_dirs`` list (7-L3).
    """

    # DEPRECATED 2026-07-10: use top-level Settings.ssl_verify instead.
    # This field is no longer read by any production code path; it is kept
    # only for backward-compat with existing TOML files that set
    # [tools] ssl_verify = ... (the value is silently ignored at runtime).
    # Will be removed in a future cleanup pass.
    ssl_verify: bool = Field(
        default=False,
        description=(
            "DEPRECATED — use top-level ``ssl_verify`` instead. "
            "Verify TLS certificates for the ``webfetch`` tool. "
            "This sub-field is no longer read; the top-level "
            "``Settings.ssl_verify`` controls all outbound TLS."
        ),
    )
    global_proxy: str | None = Field(
        default=None,
        description=(
            "Optional HTTP/HTTPS proxy URL applied to tool outbound requests "
            "(``webfetch``). ``None`` disables the proxy. V1 parity with the "
            "forge_config proxy setting."
        ),
    )


    project_skip_dirs: tuple[str, ...] = Field(
        default=(),
        description=(
            "Extra directory names skipped during ``glob`` / ``grep`` project "
            "traversal, merged with the hard-coded defaults. V1 parity: "
            "project sandbox ``skip_dirs``. Names are matched case-insensitively."
        ),
    )
    file_broker_enabled: bool = Field(
        default=True,
        description=(
            "Pure-software file/exec safety layer (``PatternFileScreen``), "
            "independent of any OS-level isolation. When True (default) "
            "the ai_coding tool bridge installs ``PatternFileScreen``, "
            "which provides — with no OS-level dependency — always_exclude "
            "path rejection, dangerous write-directory rejection "
            "(write/edit), dangerous exec-command rejection (pip install "
            "-e / git+ / rm -rf …), and glob/grep result truncation, plus "
            "a best-effort audit trail. This keeps basic hygiene working "
            "even while FileGuard (the harder-to-stabilise "
            "Windows user-isolation mechanism) is disabled. Set False to fall "
            "back to ``NoopFileBroker`` (pass-through)."
        ),
    )
    file_broker_max_entries: int = Field(
        default=10000,
        ge=1,
        le=1_000_000,
        description=(
            "Maximum ``glob`` / ``grep`` result rows the ``PatternFileScreen`` "
            "returns before truncating (post-call). Baked into the broker at "
            "tool-bridge build time, so a change takes effect on the next "
            "restart. V1 parity: ``forge_config.file_broker.max_entries``."
        ),
    )
    dependency_approval_enabled: bool = Field(
        default=False,
        description=(
            "Controlled dependency-installation broker (V1 §4.4). When True, "
            "``pip``/``uv`` install commands carrying untrusted-source args "
            "(``-e`` / ``git+`` / ``--extra-index-url`` / ``--pre``) are "
            "intercepted and BLOCK until the operator approves / rejects via "
            "the WebUI (or the approval timeout auto-rejects). V1 shipped this "
            "ON (``forge_config.json`` dep_broker.enabled=true); V2 ships OFF "
            "by user decision (2026-06-13) — the operator opts in. Baked into "
            "the tool bridge at build → reboot."
        ),
    )
    dependency_approval_timeout_s: int = Field(
        default=120,
        ge=0,
        le=3600,
        description=(
            "Seconds the dep-broker blocks waiting for an approval decision "
            "before auto-rejecting (V1 ``approval_timeout_seconds`` default "
            "120). 0 waits indefinitely."
        ),
    )
    dependency_approval_deny_args: list[str] = Field(
        default_factory=lambda: ["-e", "git+", "--extra-index-url", "--pre"],
        description=(
            "Untrusted-source args the dep-broker intercepts for approval "
            "(V1 ``dep_broker.deny_args`` default "
            "``[-e, git+, --extra-index-url, --pre]``). Operator-tunable at "
            "runtime via /api/security/runtime-config (M-3). V1 parity: an "
            "explicit EMPTY list is honoured verbatim (it means 'deny "
            "nothing' — all dep-install commands pass); the default above is "
            "only used when the value is unset/absent."
        ),
    )
    command_policy_enabled: bool = Field(
        default=True,
        description=(
            "Exec-profile broker (V1 §4.4). When True, commands matching a "
            "loaded exec profile are classified ALLOW / ASK / DENY by that "
            "profile's ask_args / hard_deny_args / io_constraints. ASK pops "
            "the FileGuard permission dialog so the USER decides (dangerous-"
            "but-possibly-intended flags like ``git push --force`` / "
            "``git ... --exec`` / ``qnn ... --debug_host``); DENY hard-blocks "
            "with a corrective reason fed back to the LLM. Re-enabled ON by "
            "user decision (2026-07-06 guard-rail redesign): repositioned "
            "from a security boundary to an LLM-misoperation guard-rail with "
            "user-in-the-loop confirmation. Hot-applied to the live broker "
            "(no reboot). Operators may still opt out via "
            "/api/security/runtime-config (M-2)."
        ),
    )
    cc_backend: str = Field(
        default="sdk",
        description=(
            "Claude Code provider backend: ``sdk`` (default) drives the "
            "``claude_agent_sdk`` CLI-subprocess adapter that mirrors V1's "
            "``session_manager.py`` model — the ONLY backend with a real "
            "agentic tool loop (the CLI itself executes Read/Write/Edit/Bash "
            "and edits files on disk) plus TRUE on-disk file "
            "checkpoint/rewind (``enable_file_checkpointing`` + "
            "``ClaudeSDKClient.rewind_files``).  V1 always ran on this CLI, so "
            "``sdk`` is the V1-aligned default.  ``http`` selects the pure-HTTP "
            "Anthropic Messages adapter, which has NO tool-execution loop "
            "(it only relays tool_use frames) — kept as a graceful fallback "
            "for deployments without the ``claude_agent_sdk`` extra / a native "
            "CLI.  When ``sdk`` is requested but the SDK / native CLI is "
            "unavailable, the DI root logs a WARNING and falls back to "
            "``http`` (no crash).  Baked into the provider at DI build → "
            "reboot to change."
        ),
    )
    cc_enable_file_checkpointing: bool = Field(
        default=False,
        description=(
            "Enable Claude Code file checkpointing (SDK backend only). V1 "
            "default OFF (``session_manager.py:1725``, has I/O overhead); the "
            "operator opts in via the settings panel. When ON the SDK "
            "provider passes ``enable_file_checkpointing=True`` + "
            "``extra_args={replay-user-messages}`` so it can capture "
            "``UserMessage.uuid`` and later restore files on a rewind. No "
            "effect under the ``http`` backend."
        ),
    )
    cc_cli_path: str = Field(
        default="",
        description=(
            "Explicit path to a native ``claude`` CLI executable for the SDK "
            "backend (V1 ``ClaudeAgentOptions.cli_path``). Empty = auto-locate "
            "the native ARM64 ``claude.exe`` (avoiding the x64 bundled-CLI "
            "crash and the ``.cmd`` pipe-handshake pitfall). A ``.cmd`` shim "
            "is rewritten to the sibling ``.exe``."
        ),
    )


class WorkspaceSettings(BaseModel):
    """Model-builder workspace root configuration (single source of truth).

    The directory under which the ``model-builder`` skill places every
    model artifact (ONNX, QNN libs, context binaries, inference outputs,
    calibration data, generated scripts). Historically hard-coded as
    ``C:\\WoS_AI`` in ~7 places across the ``chat`` / ``app_builder`` /
    ``model_builder`` / ``security`` contexts; this field collapses them
    into one typed default.

    Consumed by:

    * ``security`` — the ``${WORKSPACE}`` placeholder in the sandbox
      ``read_allow`` / ``write_allow`` / ``exec_allow_cwd`` allow-lists,
      the dangerous-write blocklist, and the persistent-ACL templates,
      so changing the root keeps the sandbox permissions in lock-step
      (no manual re-grant needed).
    * ``chat`` — the ``${WORKSPACE}`` placeholder substituted into the
      ``model-builder`` ``SKILL.md`` body and the DEFAULT-mode fallback
      text when they are injected into the system prompt.
    * ``model_builder`` — the path-traversal guard root for the
      workspace reader / initializer (``_model_builder_di``).
    * frontend — surfaced via the config API so the
      ``modelWorkdir`` extractor can match the configured root.

    The WebUI-editable override lives in ``forge.config`` under
    ``workspace.model_root``; the ``apps/api`` resolver prefers that and
    falls back to this typed default. Operators may also override via
    ``[workspace] model_root`` in server.toml or
    ``QAI_WORKSPACE__MODEL_ROOT`` in env.
    """

    model_root: str = Field(
        default_factory=lambda: (
            "C:/WoS_AI"
            if __import__("sys").platform == "win32"
            else __import__("os").path.abspath("IQ_AI")
        ),
        description=(
            "Root directory for all model-builder artifacts. Forward "
            "slashes are accepted (normalised per-consumer). Empty / "
            "blank falls back to the platform default (``C:/WoS_AI`` on "
            "Windows, ``./IQ_AI`` resolved to absolute path on Linux/macOS)."
        ),
    )

    @field_validator("model_root")
    @classmethod
    def _non_blank_model_root(cls, v: str) -> str:
        # A blank / whitespace-only / literal "null" value would silently
        # collapse to a relative path (and pollute the CWD = repo root);
        # fall back to the platform-appropriate default instead.
        import os
        import sys
        cleaned = (v or "").strip()
        if not cleaned or cleaned.lower() == "null":
            return (
                "C:/WoS_AI"
                if sys.platform == "win32"
                else os.path.abspath("IQ_AI")
            )
        return cleaned


class UsageSettings(BaseModel):
    """Anonymous usage-reporting config (internal-only feature).

    Carries operator-tunable knobs for :mod:`qai.platform.usage`. The two
    upstream endpoints are deliberately NOT defaulted here: their internal
    default values live inside :mod:`qai.platform.usage` (a module physically
    excluded from external artifacts via ``manifest.toml [exclude]``), so this
    shared-kernel ``settings.py`` source — which ships under BOTH editions —
    contains NO internal-network domain literal. The external artifact's
    ``check_release.py`` sensitive-word scan therefore finds nothing here
    (edition-dual-form-design.md §5.2 / §7). When ``ceflow_url`` /
    ``redkeep_url_template`` are left empty (the default) the registration seam
    falls back to the embedded internal defaults; an operator may still pin an
    explicit endpoint via ``[usage] ...`` config. No environment variable
    participates (hard constraint ①): the V1 ``QAIFORGE_USAGE_TOOLNAME`` /
    ``QAIFORGE_USAGE_TLS_VERIFY`` env knobs are retired in favour of these
    typed config fields.

    The usage reporter is registered ONLY under the internal edition
    (``settings.is_internal`` gate in ``apps/api/lifespan.py``); the external
    edition never schedules it (runtime-gate layer of the four-layer defence)
    and the module is additionally physically excluded from external
    artifacts (``manifest.toml [exclude]``, owned by S-C).
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Master switch for anonymous usage reporting. When True (default) "
            "AND the runtime is the internal edition, the lifespan registers "
            "the 24h reporter. Set False to disable reporting even on internal "
            "builds without touching the edition. The external edition never "
            "registers the reporter regardless of this flag (is_internal gate)."
        ),
    )
    ceflow_url: str = Field(
        default="",
        description=(
            "ceflow structured-usage POST endpoint. Empty (the safe default in "
            "this shared-kernel source) means 'use the internal-edition default "
            "embedded in :mod:`qai.platform.usage` (a module physically "
            "excluded from external artifacts via ``manifest.toml [exclude]``). "
            "Operators may override via ``[usage] ceflow_url`` config. Keeping "
            "the literal OUT of this shared-kernel source ensures the external "
            "artifact's ``check_release.py`` sensitive-word scan never finds "
            "an internal-network domain in ``settings.py`` (which ships under "
            "both editions). V1 parity: ``usage_reporter.py`` ``_CEFLOW_URL``."
        ),
    )
    redkeep_url_template: str = Field(
        default="",
        description=(
            "RedKeep legacy-metrics GET URL template (placeholders "
            "``{toolname} {username} {logsize} {hours}``). Empty (default) "
            "means 'use the internal-edition default embedded in "
            ":mod:`qai.platform.usage`'. Operators may override via "
            "``[usage] redkeep_url_template`` config. Same rationale as "
            "``ceflow_url`` for keeping the literal out of this shared-kernel "
            "source. V1 parity: ``usage_reporter.py`` "
            "``_REDKEEP_URL_TEMPLATE``."
        ),
    )
    toolname: str = Field(
        default="QAI AppBuilder",
        description=(
            "Reporting tool name (V1 parity: ``_DEFAULT_TOOLNAME``). The V1 "
            "``QAIFORGE_USAGE_TOOLNAME`` env override is retired (hard "
            "constraint ①); override via ``[usage] toolname`` config instead."
        ),
    )
    function: str = Field(
        default="Chat",
        description="Static ceflow ``payload.function`` value (V1 parity).",
    )
    sub_function: str = Field(
        default="Chat",
        description="Static ceflow ``payload.Sub-function`` value (V1 parity).",
    )
    redkeep_logsize_kb: int = Field(
        default=2,
        ge=0,
        description="RedKeep ``logsizeKB`` query value (V1 parity: 2).",
    )
    redkeep_saving_hours: float = Field(
        default=0.2,
        ge=0.0,
        description="RedKeep ``mm_saving_hours`` query value (V1 parity: 0.2).",
    )
    http_timeout_seconds: float = Field(
        default=5.0,
        gt=0.0,
        description="Per-request HTTP timeout in seconds (V1 parity: 5.0).",
    )
    interval_seconds: int = Field(
        default=24 * 60 * 60,
        gt=0,
        description=(
            "Delay between two successive reports (V1 parity: 24h). Used as "
            "the BackgroundTaskManager ``interval_seconds``."
        ),
    )
    initial_delay_seconds: float = Field(
        default=2.0,
        ge=0.0,
        description=(
            "Delay before the first report so the startup burst completes "
            "first (V1 parity: ``initial_delay_seconds=2.0``)."
        ),
    )
    tls_verify: bool = Field(
        default=False,
        description=(
            "Verify TLS certificates for the usage endpoints. Defaults False "
            "(V1 parity): the internal metrics host's certificate may "
            "not be trusted by the OS store. Applies ONLY to the two usage "
            "endpoints (a dedicated httpx client), never globally."
        ),
    )


class ToolOutputSettings(BaseModel):
    """In-prompt size caps for the file-search / read / exec tool family.

    Centralises the thresholds that decide how much of a tool's result is
    shown to the model in-prompt before it is truncated. The complete result
    is still persisted to ``data/tool_results/`` and retrievable via the
    ``read`` tool, so these caps bound only the *visible* slice, never what is
    recoverable.

    The defaults mirror the tool handlers' built-in fallback values; the
    ``apps/api`` tool-bridge wiring root installs these into the handler
    module-level config seam at build time, so a change takes effect on the
    next restart. Leave a field unset to keep the handler default.

    Two distinct kinds of cap live here:

    * *Result-count* caps (``glob_max_results`` / ``grep_max_matches``) bound a
      structured list of entries — a list can be re-fetched with a tighter
      pattern, so it is not persisted-then-paged.
    * *Line / byte / length* caps (``read_*`` / ``grep_*_bytes`` /
      ``*_line_length``) bound contiguous text and pair with the on-disk
      persistence + ``read(offset=…)`` pagination contract.
    """

    read_max_lines: int = Field(
        default=2000,
        ge=1,
        description=(
            "Maximum number of lines the ``read`` tool returns in one call "
            "before the slice is truncated and a 'continue with offset=N' "
            "notice is appended. The full file stays retrievable by reading "
            "the next range."
        ),
    )
    read_max_bytes: int = Field(
        default=50 * 1024,
        ge=1024,
        description=(
            "Maximum bytes of file content the ``read`` tool returns in one "
            "call (default 51200 = 50KB). Caps a window of very long lines "
            "even when the line count is under ``read_max_lines``."
        ),
    )
    read_max_line_length: int = Field(
        default=2000,
        ge=80,
        description=(
            "Maximum characters of a SINGLE line the ``read`` tool emits "
            "before that line is clipped and tagged. Protects the context "
            "window from a few pathologically long lines (minified bundles, "
            "base64 blobs, one-line JSON)."
        ),
    )
    glob_max_results: int = Field(
        default=60,
        ge=1,
        description=(
            "Maximum number of file paths the ``glob`` tool shows in-prompt "
            "before the list is truncated. Results are sorted newest-modified "
            "first so the most recently changed files are kept; the complete "
            "list is saved to disk and retrievable via ``read``."
        ),
    )
    grep_max_matches: int = Field(
        default=100,
        ge=1,
        description=(
            "Maximum number of match lines the ``grep`` tool shows in-prompt "
            "before the result is truncated. Matches are ordered by file "
            "modification time (newest first) so the most relevant files are "
            "kept; the complete output is saved to disk and retrievable via "
            "``read``."
        ),
    )
    grep_max_line_length: int = Field(
        default=2000,
        ge=80,
        description=(
            "Maximum characters of a SINGLE matched line the ``grep`` tool "
            "emits before that line's text is clipped with an ellipsis "
            "marker (a minified / very long matching line cannot blow the "
            "context window)."
        ),
    )
    grep_max_output_bytes: int = Field(
        default=50 * 1024,
        ge=1024,
        description=(
            "Maximum bytes of rendered ``grep`` output shown in-prompt "
            "(default 51200 = 50KB). When exceeded the full output is "
            "persisted to disk and a retrieval hint is shown."
        ),
    )


class AuthSettings(BaseModel):
    """Qualcomm Okta OIDC (Authorization Code + PKCE + Loopback Redirect,
    RFC 8252) login gate for the Web UI.

    This is a **login-only** integration: the Okta ``access_token`` /
    ``refresh_token`` returned at the callback are verified once (id_token
    JWKS signature + iss/aud/exp) and then **discarded** — the HMAC-signed
    local session cookie carries the entire session state. No AI Hub / no
    OIDC token store.

    Endpoints (``/v1/authorize``, ``/v1/token``, ``/v1/keys``) are derived
    from :attr:`issuer` at request time; the ``redirect_uri`` registered
    with Okta is derived at runtime from ``server.port`` + :attr:`redirect_path`
    so operators can change the port through ``server.toml`` without any
    code change (the new port MUST also be registered on the Okta side —
    Okta enforces exact URI match).

    Master switch :attr:`enabled` defaults to ``True`` — the Okta login
    gate is an intended, always-on feature of this deployment. Set it to
    ``False`` only for local ``pnpm dev`` on 5173 (which cannot receive
    the Okta loopback callback) or in test harnesses; when ``False`` the
    middleware short-circuits, the SPA header shows no account button,
    and the flow is a no-op end-to-end.
    """

    enabled: bool = Field(
        default=True,
        description=(
            "Master switch. True (default) = every non-public path requires "
            "a valid session cookie; missing cookie → 303 to /auth/login for "
            "browser requests, 401 JSON envelope for API requests. False = "
            "don't gate any request, don't touch Okta, SPA renders as-if this "
            "feature did not exist (use for local pnpm-dev on 5173 or tests)."
        ),
    )

    # Okta OIDC client — publicly-registered identifiers, safe to commit.
    # The ``account.qualcomm.com`` authorization server is the directory
    # external Qualcomm Developer accounts use, so external partner accounts
    # can sign in — not just internal employees.
    client_id: str = Field(default="0oa14z9c6lkmQsu3K698")
    issuer: str = Field(
        default="https://account.qualcomm.com/oauth2/ausvbhs40oLZ6EsJ6697",
        description=(
            "Full authorization-server URL. Endpoints are derived by appending "
            "/v1/authorize, /v1/token, /v1/keys. Trailing slash is stripped "
            "at load time."
        ),
    )

    redirect_path: str = Field(
        default="/callback",
        description=(
            "Loopback path Okta redirects to. Combines with ``server.port`` "
            "to form the exact redirect_uri registered with Okta "
            "(http://localhost:<server.port><redirect_path>). Changing the "
            "port requires re-registering the new URI on the Okta side."
        ),
    )

    client_auth_method: str = Field(
        default="none",
        description=(
            "Token-endpoint client authentication method: 'none' = Public "
            "Client + PKCE only (the registered Native app, current default); "
            "'private_key_jwt' = sign a client_assertion with private_key_path "
            "(only if Okta re-registers the app as a confidential client)."
        ),
    )
    private_key_path: str = Field(
        default="",
        description=(
            "RSA private key path — ONLY used when client_auth_method is "
            "'private_key_jwt'. Absolute, or relative to data.data_dir. "
            "~ expanded. Empty for the default Public/Native client."
        ),
    )

    scopes: tuple[str, ...] = Field(
        default=("openid", "profile", "email", "offline_access"),
        description=(
            "OIDC scopes requested at /authorize. ``offline_access`` buys the "
            "refresh_token; harmless to request even though this login-only "
            "flow discards it."
        ),
    )

    # Local session cookie — completely separate from the Okta tokens.
    session_cookie_name: str = Field(default="qai_session")
    session_secret: str = Field(
        default="",
        description=(
            "Optional local cookie signing secret (HMAC-SHA256). Empty = "
            "generated once under ``<data.data_dir>/auth_session_secret`` "
            "on first login and reused across restarts."
        ),
    )
    session_ttl_seconds: int = Field(default=8 * 60 * 60, ge=60)
    cookie_secure: bool = Field(
        default=False,
        description=(
            "Set True when serving over HTTPS. localhost dev / packaged "
            "loopback runs on HTTP and must keep this False."
        ),
    )

    # PKCE / state entry TTL — the window between /auth/login and the
    # callback. Covers slow MFA prompts.
    state_ttl_seconds: int = Field(default=5 * 60, ge=30)
    # Token endpoint HTTP timeout.
    exchange_timeout_seconds: int = Field(default=15, ge=1)
    ssl_verify: bool = Field(
        default=False,
        description=(
            "TLS verification for the Okta token/JWKS calls. Set False "
            "behind Qualcomm's corporate CA (default); same convention as "
            "the LLM SSL knob."
        ),
    )
    allowed_email_domains: tuple[str, ...] = Field(
        default=(),
        description=(
            "Optional allow-list on the id_token's ``email`` claim. Empty = "
            "accept whatever Okta signs (any Qualcomm-authenticated user)."
        ),
    )

    @field_validator("client_auth_method")
    @classmethod
    def _valid_client_auth_method(cls, v: str) -> str:
        allowed = {"none", "private_key_jwt"}
        lower = v.lower()
        if lower not in allowed:
            raise ValueError(
                f"auth.client_auth_method must be one of {sorted(allowed)}"
            )
        return lower


class Settings(BaseSettings):
    """Top-level settings.

    Use environment variables prefixed with ``QAI_``; nested fields use
    double underscores, e.g. ``QAI_SERVER__PORT=9000``,
    ``QAI_LOGGING__LEVEL=DEBUG``.
    """

    model_config = SettingsConfigDict(
        env_prefix="QAI_",
        env_nested_delimiter="__",
        env_file=None,        # loaded explicitly by load_settings
        case_sensitive=False,
        extra="forbid",
    )

    server: ServerSettings = Field(default_factory=ServerSettings)
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    data: DataSettings = Field(default_factory=DataSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    chat: ChatSettings = Field(default_factory=ChatSettings)
    app_builder: AppBuilderSettings = Field(default_factory=AppBuilderSettings)
    channels: ChannelsSettings = Field(default_factory=ChannelsSettings)
    service: ServiceSettings = Field(default_factory=ServiceSettings)
    model_catalog: ModelCatalogSettings = Field(default_factory=ModelCatalogSettings)
    model_runtime: ModelRuntimeSettings = Field(default_factory=ModelRuntimeSettings)
    tools: ToolsSettings = Field(default_factory=ToolsSettings)
    tool_output: ToolOutputSettings = Field(default_factory=ToolOutputSettings)
    workspace: WorkspaceSettings = Field(default_factory=WorkspaceSettings)
    usage: UsageSettings = Field(default_factory=UsageSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)

    # Edition (edition dual-form design — edition-dual-form-design.md §2).
    #
    # Runtime self-report of the build form, with NO environment variable
    # participation (hard constraint ①). The value is resolved at LOAD time by
    # ``load_settings`` from ``<repo_root>/build_info.json`` (a packaged
    # ``external`` artifact self-reports its edition there); when no
    # ``build_info.json`` is present — i.e. the dev source tree you are running
    # — the field keeps its ``"internal"`` default so the developer gets the
    # full feature set out of the box (hard constraint ③).
    #
    # ``platform`` is the single source of truth: ``domain`` never reads
    # edition; ``application`` / ``adapters`` / ``interfaces`` read it through
    # the injected ``Settings`` (``container.settings.edition`` /
    # ``container.settings.is_internal``).
    edition: str = Field(
        default="internal",
        description=(
            "'internal' (dev source tree / internal release — full feature "
            "set) or 'external' (packaged external release — internal "
            "features/assets gated off). Resolved at load time from "
            "``<repo_root>/build_info.json`` with NO environment variable "
            "participation; defaults to ``internal`` when the marker file is "
            "absent (the dev source tree)."
        ),
    )

    # 2026-07-10 — unified TLS/SSL verification switch.
    # Controls ALL outbound HTTPS connections in the process: webfetch tool,
    # LLM stream, LLM title generator, MCP client, channels (Feishu / HTTP
    # proxy), Auth (Okta), and Usage reporting.
    #
    # Default is edition-derived (injected by ``load_settings`` before the
    # TOML / env / override layers so those can still override it):
    #   internal  → False  (self-signed corporate gateways are common in dev)
    #   external  → True   (packaged release must verify certificates)
    #
    # Can be overridden via env ``QAI_SSL_VERIFY=true/false`` or TOML
    # ``ssl_verify = true``.
    ssl_verify: bool = Field(
        default=False,
        description=(
            "Verify TLS/SSL certificates for outbound HTTPS connections. "
            "Controls: webfetch tool, LLM stream, LLM title generator, "
            "MCP client (HTTP + stdio cert env), channels (Feishu WS / "
            "HTTP proxy factory), Auth (Okta token/JWKS), Usage reporting. "
            "NOT yet controlling: Feishu/WeChat SDK requests (sdk_network.py "
            "monkey-patch), MCP registry source (QAI_CHAT_MCP_REGISTRY_VERIFY_TLS), "
            "service/model catalog downloads (forge version_check). "
            "Default is edition-derived: False for 'internal' (dev / "
            "self-signed corporate gateways), True for 'external' (packaged "
            "release). Override via env QAI_SSL_VERIFY or TOML ssl_verify."
        ),
    )

    @field_validator("edition")
    @classmethod
    def _valid_edition(cls, v: str) -> str:
        allowed = {"external", "internal"}
        lower = v.lower()
        if lower not in allowed:
            raise ValueError(f"edition must be one of {sorted(allowed)}")
        return lower

    # ---- convenience ----

    @property
    def is_internal(self) -> bool:
        """``True`` when running the internal (full-feature) edition.

        Convenience for ``settings.edition == "internal"`` so callers do not
        compare string literals. internal-only registration / mounting is
        gated with ``if container.settings.is_internal:`` (the runtime-gate
        layer of the four-layer internal-asset defence).
        """
        return self.edition == "internal"

    def data_paths(self) -> DataPaths:
        """Return a ``DataPaths`` rooted at ``data.data_dir``."""
        return DataPaths(self.data.data_dir)

    def get_legacy_config(
        self,
        section: str,
        key: str,
        default: Any = None,
    ) -> Any:
        """Read a value from a v1 JSON config file (fallback for unmapped fields).

        Provides access to the ~280 fields across 17 v1 config files that are
        NOT mapped to typed Settings fields (only consumed fields are mapped).

        Parameters:
            section: JSON filename without extension, e.g. ``"service_config"``,
                ``"forge_config"``, ``"model_catalog"``.
            key: dot-separated path into the JSON object, e.g.
                ``"cloud_shared.timeout_seconds"`` or
                ``"service_launch.local_port"``.
            default: value returned when the file or key is missing.

        Returns:
            The JSON value at the specified path, or ``default``.

        The config directory is resolved from ``self.data.config_dir``.
        Files are cached in-memory after first read (per Settings instance
        lifetime) to avoid repeated filesystem access.
        """
        return _legacy_config_read(
            config_dir=self.data.config_dir,
            section=section,
            key=key,
            default=default,
            cache=self._legacy_cache,
        )

    def __init__(self, **kwargs: Any) -> None:  # noqa: D107
        super().__init__(**kwargs)
        # Instance-level cache for legacy JSON config reads.
        # Using object.__setattr__ because pydantic-settings models are
        # typically frozen / have __slots__ constraints.
        object.__setattr__(self, "_legacy_cache", {})


# ----------------------------------------------------------------------
# Legacy JSON config fallback helpers
# ----------------------------------------------------------------------


def _legacy_config_read(
    *,
    config_dir: Path,
    section: str,
    key: str,
    default: Any,
    cache: dict[str, Any],
) -> Any:
    """Internal: read a dot-path key from a cached JSON config file."""
    if section not in cache:
        file_path = config_dir / f"{section}.json"
        if file_path.is_file():
            try:
                with file_path.open("r", encoding="utf-8") as fh:
                    cache[section] = json.load(fh)
            except (OSError, json.JSONDecodeError):
                cache[section] = None
        else:
            cache[section] = None

    data = cache.get(section)
    if data is None:
        return default

    # Navigate dot-separated key path
    parts = key.split(".")
    current: Any = data
    for part in parts:
        if isinstance(current, dict):
            current = current.get(part)
            if current is None:
                return default
        else:
            return default
    return current


# ----------------------------------------------------------------------
# Loading helpers
# ----------------------------------------------------------------------


def load_settings(
    *,
    config_file: Path | None = None,
    repo_root: Path | None = None,
    overrides: dict[str, Any] | None = None,
) -> Settings:
    """Build a ``Settings`` instance with priority ``overrides > env > toml > defaults``.

    Parameters:
        config_file: explicit path to a TOML file. Overrides ``repo_root``.
        repo_root: if given (and ``config_file`` is None), looks for
            ``<repo_root>/config/server.toml``. Also used to resolve the
            runtime ``edition`` from ``<repo_root>/build_info.json`` (edition
            self-report; see :func:`_resolve_build_info_edition`).
        overrides: an optional dict that wins over everything else,
            primarily for tests and CLI flags.

    ``edition`` is resolved at the very bottom of the priority chain (below
    TOML / env / overrides) from ``<repo_root>/build_info.json`` with NO
    environment variable participation; it falls back to ``"internal"`` (full
    feature set, dev source tree) when the marker is absent or invalid.

    Raises ``ConfigurationError`` on malformed TOML or invalid values.
    """
    file_data: dict[str, Any] = {}
    chosen_file = _resolve_config_file(config_file, repo_root)
    if chosen_file is not None and chosen_file.is_file():
        try:
            with chosen_file.open("rb") as fh:
                file_data = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigurationError(
                "config.toml_load_failed",
                f"Failed to load {chosen_file}: {exc}",
            ) from exc

    # Two-step build to enforce priority ``overrides > env > toml > defaults``:
    # 1. Build an env-only Settings to discover which keys env actually sets.
    # 2. Strip those keys from file_data so env wins, then merge overrides on top.
    try:
        env_only = Settings()
    except Exception as exc:  # noqa: BLE001
        raise ConfigurationError(
            "config.invalid",
            f"Invalid environment-derived settings: {exc}",
        ) from exc

    env_dump = env_only.model_dump()
    defaults = Settings.model_construct().model_dump()
    env_overrides = _diff_top_level(env_dump, defaults)

    # Merge: file_data (lowest) → env_overrides → overrides (highest)
    merged: dict[str, Any] = {}
    # Edition self-report (edition-dual-form-design.md §2.2) sits at the very
    # bottom of the priority chain: ``<repo_root>/build_info.json`` self-reports
    # the packaged ``external`` form, but an operator-supplied TOML / env /
    # explicit override still wins. When the marker is absent (dev source tree)
    # nothing is injected and the field keeps its ``"internal"`` default.
    resolved_edition = _resolve_build_info_edition(repo_root)
    if resolved_edition is not None:
        merged["edition"] = resolved_edition
    # 2026-07-10 — derive ssl_verify default from edition (same priority as
    # edition: below TOML / env / overrides so those can still override it).
    # external (packaged release) → True (verify certs); internal (dev) → False.
    # We must consider all edition sources to compute the effective edition at
    # this point: build_info (already in merged) + file_data + env_overrides +
    # overrides (caller-supplied). We peek at all of them to find the highest-
    # priority edition value, then inject ssl_verify ONLY when it has not been
    # explicitly set by any of those sources (setdefault semantics).
    _peek_edition = (
        (overrides or {}).get("edition")
        or env_overrides.get("edition")
        or file_data.get("edition")
        or merged.get("edition")
        or "internal"
    )
    merged.setdefault("ssl_verify", _peek_edition == "external")
    _deep_merge(merged, file_data)
    _deep_merge(merged, env_overrides)
    if overrides:
        _deep_merge(merged, overrides)

    try:
        # ``_env_file=None`` and ``_secrets_dir=None`` make sure pydantic-settings
        # does not re-read env on top of our explicit kwargs. Env is already
        # captured in ``env_overrides`` above.
        return Settings.model_validate(merged)
    except Exception as exc:  # noqa: BLE001 — re-raise as ConfigurationError
        raise ConfigurationError(
            "config.invalid",
            f"Settings validation failed: {exc}",
        ) from exc


def _resolve_config_file(
    config_file: Path | None,
    repo_root: Path | None,
) -> Path | None:
    if config_file is not None:
        return config_file
    if repo_root is not None:
        return repo_root / "config" / _DEFAULT_TOML_NAME
    return None


def _resolve_build_info_edition(repo_root: Path | None) -> str | None:
    """Resolve the runtime edition from ``<repo_root>/build_info.json``.

    Returns the lower-cased edition string when the marker file exists and
    carries a valid ``edition`` field, otherwise ``None`` (caller then keeps
    the ``"internal"`` default — the dev source tree / full feature set).

    Robust by design (edition-dual-form-design.md §0 ① / §2.2): a missing,
    unreadable, malformed, field-less, or out-of-range marker NEVER raises —
    edition resolution must never be able to crash application startup. No
    environment variable participates (hard constraint ①). The file is read as
    UTF-8 (AGENTS.md §3.10).
    """
    if repo_root is None:
        return None
    marker = repo_root / _BUILD_INFO_NAME
    try:
        if not marker.is_file():
            return None
        with marker.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        # Missing / unreadable / corrupt marker → graceful default.
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("edition")
    if not isinstance(raw, str):
        return None
    value = raw.strip().lower()
    if value not in _VALID_EDITIONS:
        # Unknown / illegal edition value → graceful default (internal).
        return None
    return value


def _deep_merge(dst: dict[str, Any], src: dict[str, Any]) -> None:
    """Recursively merge ``src`` into ``dst`` (in place). ``src`` wins."""
    for key, value in src.items():
        if (
            isinstance(value, dict)
            and isinstance(dst.get(key), dict)
        ):
            _deep_merge(dst[key], value)
        else:
            dst[key] = value


def _diff_top_level(
    actual: dict[str, Any],
    defaults: dict[str, Any],
) -> dict[str, Any]:
    """Return the keys/values in ``actual`` that differ from ``defaults``.

    Recurses one level deep into nested mappings so that an env override of
    e.g. ``server.port`` does not erase a TOML-supplied ``server.host``.
    """
    diff: dict[str, Any] = {}
    for key, value in actual.items():
        default_value = defaults.get(key)
        if isinstance(value, dict) and isinstance(default_value, dict):
            nested = _diff_top_level(value, default_value)
            if nested:
                diff[key] = nested
        elif value != default_value:
            diff[key] = value
    return diff


def get_settings(
    *,
    config_file: Path | None = None,
    repo_root: Path | None = None,
) -> Settings:
    """Convenience wrapper. NOT a singleton.

    Apps should call this once at startup and pass the result through DI.
    Tests should construct ``Settings`` directly with explicit values.
    """
    return load_settings(config_file=config_file, repo_root=repo_root)
