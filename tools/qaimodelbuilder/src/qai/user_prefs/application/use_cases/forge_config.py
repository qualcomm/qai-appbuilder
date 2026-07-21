# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``LoadForgeConfigUseCase`` — forge-config read with default injection (R5).

The legacy ``GET /api/forge-config`` handler did ~120 lines of work in
the route layer:

* load the ``forge.config`` KV document;
* inject a set of V1-parity factory defaults via ``setdefault``
  (security bind_host / allow_exec_tool, the ``auto_title`` agent-loop
  flag, service_launch.show_prompt_in_ui, toolbar-module enabled flags);
* DERIVE the chat-input CC/OC pill ``enabled`` flags by reading the
  separate ``ai_coding.config`` / ``ai_coding.oc.config`` KV documents
  and overwriting ``ai_coding.{cc,oc}.enabled`` so the pill and the
  Settings toggle can never drift.

All of that is application policy (defaulting + cross-document
derivation), so it belongs here, not in ``interfaces/``. The route now
just calls :meth:`execute` and wraps the result.

Cross-document note (AGENTS.md §3.2)
------------------------------------
The pill derivation reads the ``ai_coding.config`` / ``ai_coding.oc.config``
KV documents. These live in the **same** ``kv_user_prefs`` table and are
read here purely by their string key through this BC's own
:class:`LoadDocumentUseCase` — there is **no import of the ``ai_coding``
bounded context**. We only consume the opaque ``{"enabled": bool}`` shape
of those documents, treating them as untyped KV rows, so context
isolation is preserved.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from qai.user_prefs.application.use_cases.load_document import (
    LoadDocumentUseCase,
)

__all__ = ["LoadForgeConfigUseCase"]

#: KV keys of the coding-config documents the pill flags derive from.
#: Read by string key only — NOT a cross-context import (see module docstring).
_CC_CONFIG_KEY = "ai_coding.config"
_OC_CONFIG_KEY = "ai_coding.oc.config"

#: Toolbar-module first-run enabled defaults.
#:
#: Aligns with V1 ``forge_config.json`` defaults (see ``factory/_source/
#: forge_config.json`` ``_comment_toolbar_modules``: "默认仅显示 模型构建器/
#: 应用构建器/编程，翻译与 PPT 关闭") and the front-end default in
#: ``frontend/src/composables/useForgeConfig.ts`` ``DEFAULT_TOOLBAR_MODULES``
#: (translate/ppt ``enabled: false``).
#:
#: Previously translate/ppt defaulted to True here, which DRIFTED from
#: the factory source + front-end + the user-visible JSON ``_comment``,
#: causing those two buttons to appear in the chat-input toolbar on
#: every fresh install even though the documented behaviour was "hidden
#: by default; user opts in via Settings → App Config → Toolbar Modules".
#: Fixed back to False so the three sources of truth agree again.
_TOOLBAR_DEFAULT_ENABLED = {
    "model_builder": True,
    "model_hub": True,
    "app_builder": True,
    "code": True,
    "translate": False,
    "ppt": False,
}


def _ensure_dict(parent: dict[str, Any], key: str) -> dict[str, Any]:
    """Return ``parent[key]`` as a dict, replacing a non-dict in place.

    Mirrors the legacy ``setdefault`` + ``isinstance`` guard pattern: if
    ``key`` is absent it is created as ``{}``; if it holds a non-dict
    value it is overwritten with ``{}`` (so a hand-edited / corrupted row
    cannot crash the defaults injection). Returns the live nested dict.
    """
    value = parent.setdefault(key, {})
    if not isinstance(value, dict):
        value = {}
        parent[key] = value
    return value


@dataclass(slots=True, frozen=True)
class LoadForgeConfigUseCase:
    """Load ``forge.config`` and inject V1-parity defaults + pill flags.

    ``bind_host`` is the secure loopback default for the internal
    forge-config service. It is injected at DI time from the
    allow-listed ``qai.platform.config.LOOPBACK_HOST`` constant
    (always ``127.0.0.1``, V1 parity) — deliberately NOT from
    ``container.settings.server.host`` (which may be ``0.0.0.0``), so the
    internal service is never exposed on all interfaces while the
    ``check_no_magic_host_port`` guard stays clean (no literal here).
    """

    load_document_use_case: LoadDocumentUseCase
    forge_config_key: str
    bind_host: str
    # internal-only edition gate (default False = external/safe). When True the
    # toolbar-defaults injection adds the ``pro`` (Model Builder Pro / 增强)
    # toolbar module so the chat composer shows the「Pro」mode button. On
    # external editions this stays False, so ``pro`` is never injected and the
    # button never appears — mirroring the model-dropdown 查询服务 group's
    #端点级 edition gate (interfaces/http/routes/model_catalog.py: list_query_
    # services returns [] when not is_internal). The frontend carries NO edition
    # judgement and NO ``pro`` fallback in DEFAULT_TOOLBAR_MODULES — the button
    # is purely backend-data-driven, so an external build simply never renders
    # it. (mb-pro-integration-plan.md §6 / §7 layer ①.)
    is_internal: bool = False
    # Which GoMaster link is wired (edition config ``gomaster_mode``): "external"
    # (default, one-click optimize task — NOT chat), "agent" (conversational),
    # or "both". Surfaced to the frontend on the ``gomaster`` toolbar module so
    # the composer knows NOT to route ``query::gomaster`` in external mode (it is
    # not a chat) and the chat empty-state can show the GoMaster intro. Only
    # meaningful when ``is_internal`` (the gomaster module is injected then).
    gomaster_mode: str = "external"

    async def execute(self) -> dict[str, Any]:
        doc = await self.load_document_use_case.execute(self.forge_config_key)
        self._inject_scalar_defaults(doc)
        await self._derive_ai_coding_pills(doc)
        self._inject_toolbar_defaults(doc)
        return doc

    # ── default-injection helpers ──────────────────────────────────────────

    def _inject_scalar_defaults(self, doc: dict[str, Any]) -> None:
        """security.* / agent-loop / service_launch defaults (V1 parity)."""
        # security.* defensive defaults (front-end relies on these keys).
        security = _ensure_dict(doc, "security")
        security.setdefault("bind_host", self.bind_host)
        security.setdefault("allow_exec_tool", True)

        # Agent-loop default (V1 parity) — flat top-level key.
        #
        # Only ``auto_title`` is injected: it gates a user-PERCEIVED behaviour
        # (auto-generating the conversation title after the first turn), so a
        # default belongs here for the front-end / a future title-gate to read.
        #
        # The three other V1 ``agent.*`` loop knobs that used to be injected
        # here — ``experience_extraction`` / ``auto_compress`` /
        # ``max_iterations`` — were DEAD defaults: written by this method but
        # read by NObody (verified: no front-end Settings toggle and no backend
        # reader consume them; the chat use case in V2 does not gate on them).
        # Injecting an unread default violates single-source-of-truth (a config
        # value the system claims to honour but silently ignores —
        # "配置自相矛盾"), so they are no longer injected:
        #   * ``max_iterations`` — V2 unifies the agentic tool loop in the
        #     backend (``chat`` use case) with a 200-round + graceful-END cap
        #     plus a no-progress circuit breaker; that is the终态 mechanism and
        #     no per-config tool-round knob gates it.  The legacy v0.5
        #     微信/飞书 通道 had its own backend-read ``channels.max_tool_rounds``
        #     (0 = UNLIMITED default; positive N injected a "give your final
        #     answer" prompt at round N) — that knob is NOT carried into V2:
        #     the no-progress breaker + 200-round graceful cap cover the
        #     "防工具死循环 + 收敛" need without照搬 v0.5's forced-conclusion
        #     prompt.  The dead ``channels.max_tool_rounds`` /
        #     ``channels.tool_result_max_chars`` fields (exposed but never read
        #     at runtime) were removed from ``factory/_source/forge_config.json``
        #     and the front-end AppConfigPanel to end the "配置自相矛盾".
        #     (单结果工具输出截断 is handled by the chat domain's
        #     ``AdaptiveToolResultTruncator`` / ``TOOL_RESULT_HARD_CAP_CHARS``,
        #     not by a forge-config knob.)
        #   * ``auto_compress`` — V2 compression is threshold-gated and always
        #     beneficial; a user toggle to DISABLE it only invites
        #     prompt_too_long rejections (no positive user value).
        #   * ``experience_extraction`` — a fire-and-forget background sink with
        #     no user-perceived effect to toggle.
        doc.setdefault("auto_title", True)

        # service_launch defaults (V1 forge_config.json parity).
        service_launch = _ensure_dict(doc, "service_launch")
        service_launch.setdefault("show_prompt_in_ui", True)
        # V1 parity (forge_config.json:48 ``service_launch.prompt_debug``): a
        # hidden operator flag that, when true, dumps the FULL messages list
        # actually sent to the model into the backend log (structured
        # ``chat.prompt_debug`` event in V2). Default False — it is verbose +
        # for debugging the prompt source only; V1 keeps it off by default and
        # did not expose a UI toggle.
        service_launch.setdefault("prompt_debug", False)

    async def _derive_ai_coding_pills(self, doc: dict[str, Any]) -> None:
        """Derive CC/OC pill ``enabled`` flags from the coding-config docs.

        SINGLE SOURCE OF TRUTH (V1 parity): the pill flag is read from the
        real ``ai_coding.config`` / ``ai_coding.oc.config`` documents and
        OVERWRITTEN onto ``ai_coding.{cc,oc}.enabled`` so the pill and the
        Settings toggle can never drift.
        """
        cc_enabled = await self._read_coding_enabled(_CC_CONFIG_KEY)
        oc_enabled = await self._read_coding_enabled(_OC_CONFIG_KEY)
        ai_coding = _ensure_dict(doc, "ai_coding")
        _ensure_dict(ai_coding, "cc")["enabled"] = cc_enabled
        _ensure_dict(ai_coding, "oc")["enabled"] = oc_enabled

    async def _read_coding_enabled(self, key: str) -> bool:
        """Read ``enabled`` (default **False**) from a coding-config KV doc.

        Default is False so a fresh install (empty KV doc) does NOT show the
        CC/OC pills in chat while the Settings toggle reads "Disabled" — the
        pill and the toggle must agree (V1 parity: pill shows ⇔ enabled==true).
        Previously this defaulted to True, which made the pills appear even
        though the toggle was off (the front-end toggle defaults to false),
        i.e. "toggle off but pill shown". CC/OC are opt-in: don't expose the
        mode entry until the user has actually enabled the SDK.
        """
        try:
            cfg = await self.load_document_use_case.execute(key)
        except Exception:  # noqa: BLE001
            return False
        if isinstance(cfg, dict):
            return bool(cfg.get("enabled", False))
        return False

    def _inject_toolbar_defaults(self, doc: dict[str, Any]) -> None:
        """Toolbar-module enabled defaults under ``ui.toolbar_modules``.

        V1 user-perceived parity; ``setdefault`` keeps any genuine user
        opt-out. NOT a top-level key.
        """
        ui = _ensure_dict(doc, "ui")
        toolbar_modules = _ensure_dict(ui, "toolbar_modules")
        for mode, default in _TOOLBAR_DEFAULT_ENABLED.items():
            _ensure_dict(toolbar_modules, mode).setdefault("enabled", default)
        # internal-only「Pro / 增强」mode button (Model Builder Pro remote GPU
        # Agent). Injected ONLY on internal editions so external builds never
        # surface it (the frontend has no ``pro`` fallback default, so without
        # this backend injection the button does not render — same pattern as
        # the edition-gated 查询服务 dropdown group). Unlike the 5 modes above
        # (front-end DEFAULT_TOOLBAR_MODULES fills order/mode/i18n/icon), the
        # frontend has NO ``pro`` entry, so the backend ships the full module
        # descriptor here for the toolbar to render it.
        #
        # Order 40 slots pro directly after app_builder (10) / model_hub (20)
        # / model_builder (30) so advanced model-workflow modes stay grouped,
        # then general-purpose modes (code=60 / translate=70 / ppt=80) follow.
        # The 10-unit gap leaves room for future modules to slot in.
        if self.is_internal:
            pro = _ensure_dict(toolbar_modules, "pro")
            pro.setdefault("enabled", True)
            # Legacy-order migration (2026-07-20 rearrange): pro's previous
            # default was 30; the toolbar was reordered (app_builder→10,
            # model_hub→20, model_builder→30) so pro moves to 40. Installs
            # whose forge_config carries the previous default get a one-time
            # bump; a deliberately-customised value won't be exactly 30 in
            # normal usage. Mirrors the frontend LEGACY_ORDER_MIGRATION.
            if pro.get("order") == 30:
                pro["order"] = 40
            pro.setdefault("order", 40)
            pro.setdefault("mode", "pro")
            pro.setdefault("i18n", "index.proMode")
            pro.setdefault("icon", "pro")
            # internal-only「GoMaster」mode button — same edition-gated
            # injection pattern as the ``pro`` module. The frontend has no
            # ``gomaster`` fallback default, so the button does not render on
            # external builds (which lack this backend injection). The full
            # module descriptor is shipped here (order/mode/i18n/icon) since
            # the frontend's DEFAULT_TOOLBAR_MODULES has no entry for it.
            # Order 50 places GoMaster right after Pro (40), still ahead of
            # the general-purpose modes (code=60, translate=70, ppt=80).
            gomaster = _ensure_dict(toolbar_modules, "gomaster")
            gomaster.setdefault("enabled", True)
            # Legacy-order migration (2026-07-20 rearrange): mirrors the
            # pro=30→40 migration above. gomaster's previous default was 40;
            # now 50 so it stays paired with pro.
            if gomaster.get("order") == 40:
                gomaster["order"] = 50
            gomaster.setdefault("order", 50)
            gomaster.setdefault("mode", "gomaster")
            gomaster.setdefault("i18n", "index.gomasterMode")
            gomaster.setdefault("icon", "gomaster")
            # Which GoMaster link is active. The frontend reads this to (a) NOT
            # route ``query::gomaster`` on chat send in "external" mode (external
            # is a one-click optimize task, not a conversation → routing it would
            # lock the session on an unbound hint), and (b) show the GoMaster
            # intro empty-state. "external" (default) / "agent" / "both".
            gomaster.setdefault("gomaster_mode", self.gomaster_mode)
