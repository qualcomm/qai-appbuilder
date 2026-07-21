# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Apps-layer cross-context bridge: chat → user_prefs code-persona (R12).

The chat bounded context resolves a selected *code persona* (id →
working-role prompt + display name) so the system-prompt builder can
inject it as the active role for ``tool_mode == "code"`` turns.  The
persona records + per-persona overrides live in the **user_prefs**
bounded context (the ``ui.code_personas`` prefs document + the pure
:class:`qai.user_prefs.domain.code_personas.CodePersonaManager`
helper).

Per the import-linter ``context-isolation`` contract ``qai.chat.*``
may NEVER import ``qai.user_prefs.*``.  Before R12 the resolution
happened inline in the SSE / WS route layer
(``interfaces/http/routes/chat/_sse.py:_resolve_code_persona_into_extra``)
which reached directly into ``qai.user_prefs.domain.code_personas`` —
a cross-context import that, while physically in the interface layer,
leaked a user_prefs domain dependency into the chat request path and
duplicated the resolution logic across two route modules.

R12 lifts the resolution behind
:class:`qai.chat.application.ports.CodePersonaResolverPort` and wires
this bridge at the ``apps/api`` composition root — the one layer that
legitimately sees both contexts.  :class:`StreamChatUseCase` consumes
the port (injected via ``apps/api/_chat_di``); the chat context no
longer imports user_prefs anywhere.

Mirrors legacy ``chat_handler.code_persona_manager`` behaviour: when a
selected persona id names a known persona, its (override-applied)
prompt + display name are returned so the chat system-prompt builder
(``RichSystemPromptBuilder._build_feature_prompt``) injects the persona
as the active working role.  The "cloud models only" rule (legacy
app.js L1810-1811) is enforced on the frontend, which omits the persona
id for local models; the backend resolves whatever id reaches it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from qai.chat.application.ports import ResolvedCodePersona
from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

logger = get_logger(__name__)

__all__ = ["CodePersonaResolverBridge"]

# Top-level prefs key for code-persona selection / overrides.  Mirrors
# ``interfaces/http/routes/user_prefs.py:CODE_PERSONAS_KEY``.
_CODE_PERSONAS_KEY = "ui.code_personas"


class CodePersonaResolverBridge:
    """Resolve a code persona id against the user_prefs prefs document.

    Implements :class:`qai.chat.application.ports.CodePersonaResolverPort`.
    Constructed in ``apps/api/_chat_di`` with the :class:`Container` so it
    can lazily reach ``container.user_prefs.load_document_use_case`` (the
    user_prefs context is wired *after* chat in the DI order, so the
    lookup must be deferred to call time) and the pure
    ``CodePersonaManager`` domain helper.

    All failures are swallowed (best-effort, never break the stream) —
    a missing user_prefs context, a missing document, or a lookup error
    all surface as ``None`` so the caller leaves the system prompt
    unchanged.
    """

    __slots__ = ("_container",)

    def __init__(self, *, container: "Container") -> None:
        self._container = container

    async def resolve(
        self, persona_id: str, locale: str | None = None
    ) -> ResolvedCodePersona | None:
        persona_id = (persona_id or "").strip()
        if not persona_id:
            return None
        user_prefs = getattr(self._container, "user_prefs", None)
        if user_prefs is None:
            return None
        load_doc_uc = getattr(user_prefs, "load_document_use_case", None)
        if load_doc_uc is None:
            return None
        try:
            # Lazy cross-context import — legitimate at the apps
            # composition root (context-isolation targets ``qai.<ctx>``
            # source files, not ``apps.api``).
            from qai.user_prefs.domain.code_personas import (
                CodePersonaManager,
                DEFAULT_PERSONAS,
            )

            doc = await load_doc_uc.execute(_CODE_PERSONAS_KEY)
            _selected, personas = CodePersonaManager.get_all_personas(doc)
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat.persona_resolution_failed", error=str(exc))
            return None
        for persona in personas:
            if persona.get("id") != persona_id:
                continue
            # Determine the effective prompt:
            # 1. If user has customized (is_customized=True), use their override directly.
            # 2. Otherwise, pick from the multilingual prompts dict by locale.
            prompt: str | None = None
            is_customized = persona.get("is_customized", False)
            if is_customized:
                prompt = persona.get("prompt")
            else:
                # `prompts` is stripped from get_all_personas output (API
                # payload optimization); read directly from DEFAULT_PERSONAS.
                builtin = DEFAULT_PERSONAS.get(persona_id, {})
                prompts = builtin.get("prompts")
                if isinstance(prompts, dict) and locale:
                    norm = _normalize_locale(locale)
                    prompt = prompts.get(norm) or prompts.get("zh-CN")
                if not prompt:
                    prompt = persona.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                return None
            name = persona.get("name")
            groups = persona.get("groups")
            return ResolvedCodePersona(
                prompt=prompt,
                name=name if isinstance(name, str) and name.strip() else None,
                groups=tuple(groups) if isinstance(groups, list) else None,
            )
        return None


def _normalize_locale(locale: str) -> str:
    """Normalize a locale string to one of 'en', 'zh-CN', 'zh-TW'."""
    locale = (locale or "").strip().lower()
    if locale.startswith("en"):
        return "en"
    if locale in ("zh-tw", "zh_tw", "zh-hant"):
        return "zh-TW"
    return "zh-CN"
