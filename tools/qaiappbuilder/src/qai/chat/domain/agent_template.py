# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``AgentTemplate`` aggregate for the chat bounded context.

An :class:`AgentTemplate` is a *named, reusable definition of a SINGLE
discussion role* — an "agent" such as ``资深架构师`` / ``全栈开发`` /
``严谨测试`` — that a user can preview and import into any conversation (or pull
into a team).  It is the smallest reusable unit in the three-tier template
system (design §27: single role → team → mode), orthogonal to the team-level
:class:`~qai.chat.domain.roster_template.RosterTemplate` and the
mode-level ``ModeTemplate``.

Relationship to :class:`~qai.chat.domain.participant.Participant`
----------------------------------------------------------------
A ``Participant`` is strictly conversation-scoped (one row per speaker, bound
to a ``conversation_id``).  An ``AgentTemplate`` is the orthogonal,
*conversation-independent* library entry: it holds one role *definition*
(display name + model + persona + config) WITHOUT any conversation binding.
"Applying" a template to a conversation instantiates exactly one
``kind=named_agent`` ``Participant``.

Mutability model
----------------
The aggregate is mutable (``name`` / ``description`` / ``display_name`` /
``model_id`` / ``persona`` / ``config`` may change via mutators that bump
``updated_at``).  Like :class:`Participant`, mutators emit no platform
side-effects (domain stays pure).  ``is_builtin`` templates are factory-seeded
presets and are treated as read-only by the application layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from qai.chat.domain.ids import AgentTemplateId
from qai.platform.time import ensure_aware_utc

_MAX_NAME_LENGTH: int = 256
_MAX_DESCRIPTION_LENGTH: int = 2000
_MAX_DISPLAY_NAME_LENGTH: int = 256


def _validate_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate + defensively copy a config blob.

    Recognises the same keys as ``Participant.config`` (``allowed_tools`` /
    ``color``) so the agent can be instantiated into a participant 1:1.
    """
    if config is None:
        return None
    if not isinstance(config, dict):
        raise TypeError("AgentTemplate.config must be a dict or None")
    allowed = config.get("allowed_tools")
    if allowed is not None and (
        not isinstance(allowed, (list, tuple))
        or not all(isinstance(tool, str) for tool in allowed)
    ):
        raise TypeError(
            "AgentTemplate.config['allowed_tools'] must be a list of str "
            "when present",
        )
    color = config.get("color")
    if color is not None and (
        isinstance(color, bool) or not isinstance(color, (str, int))
    ):
        raise TypeError(
            "AgentTemplate.config['color'] must be a str, int (palette index) "
            "or None",
        )
    copied: dict[str, Any] = dict(config)
    if allowed is not None:
        copied["allowed_tools"] = list(allowed)
    return copied


@dataclass(slots=True, kw_only=True)
class AgentTemplate:
    """A named, reusable definition of a single discussion role.

    Construction goes through :meth:`create` to set ``created_at`` /
    ``updated_at`` consistently; loading from persistence uses the regular
    constructor.
    """

    id: AgentTemplateId
    name: str = ""
    description: str = ""
    display_name: str = ""
    model_id: str | None = None
    persona: str | None = None
    config: dict[str, Any] | None = None
    is_builtin: bool = False
    #: When this template was cloned from another (e.g. a built-in preset), the
    #: SOURCE template's id; ``None`` for originals. Tail-appended optional field
    #: (§3.1 additive). "Editing a built-in preset" is modelled as cloning a
    #: user copy (``is_builtin=False`` + ``cloned_from_id=preset.id``) and then
    #: editing the copy — the original stays read-only and untouched. ``reset``
    #: restores a copy's business fields from ``cloned_from_id`` in place.
    cloned_from_id: str | None = None
    #: Optional per-locale i18n maps for built-in presets (migration 056).
    #: Each is ``{"en": "...", "zh-CN": "...", "zh-TW": "..."}`` loaded from the
    #: matching ``*_i18n_json`` column; ``None`` for custom (``is_builtin=0``)
    #: templates and pre-056 rows — which then always render their canonical
    #: single-language field (name / description / display_name / persona) as the
    #: fallback (AGENTS.md §8 forward-compatibility). Tail-appended optional
    #: fields (§3.1 additive) so old constructors / old data are unaffected.
    name_i18n: dict[str, str] | None = None
    description_i18n: dict[str, str] | None = None
    display_name_i18n: dict[str, str] | None = None
    persona_i18n: dict[str, str] | None = None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, AgentTemplateId):
            raise TypeError(
                "AgentTemplate.id must be AgentTemplateId, got "
                f"{type(self.id).__name__}",
            )
        if not isinstance(self.name, str):
            raise TypeError("AgentTemplate.name must be a str")
        if len(self.name) > _MAX_NAME_LENGTH:
            raise ValueError(
                f"AgentTemplate.name must be <= {_MAX_NAME_LENGTH} chars",
            )
        if not isinstance(self.description, str):
            raise TypeError("AgentTemplate.description must be a str")
        if len(self.description) > _MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                "AgentTemplate.description must be "
                f"<= {_MAX_DESCRIPTION_LENGTH} chars",
            )
        if not isinstance(self.display_name, str):
            raise TypeError("AgentTemplate.display_name must be a str")
        if len(self.display_name) > _MAX_DISPLAY_NAME_LENGTH:
            raise ValueError(
                "AgentTemplate.display_name must be "
                f"<= {_MAX_DISPLAY_NAME_LENGTH} chars",
            )
        if self.model_id is not None and not isinstance(self.model_id, str):
            raise TypeError("AgentTemplate.model_id must be a str or None")
        if self.persona is not None and not isinstance(self.persona, str):
            raise TypeError("AgentTemplate.persona must be a str or None")
        self.config = _validate_config(self.config)
        if not isinstance(self.is_builtin, bool):
            raise TypeError("AgentTemplate.is_builtin must be a bool")
        if self.cloned_from_id is not None and not isinstance(
            self.cloned_from_id, str
        ):
            raise TypeError("AgentTemplate.cloned_from_id must be a str or None")
        ensure_aware_utc(self.created_at)
        ensure_aware_utc(self.updated_at)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        template_id: AgentTemplateId,
        now: datetime,
        name: str = "",
        description: str = "",
        display_name: str = "",
        model_id: str | None = None,
        persona: str | None = None,
        config: dict[str, Any] | None = None,
        is_builtin: bool = False,
        cloned_from_id: str | None = None,
    ) -> AgentTemplate:
        """Construct a brand-new agent template."""
        ts = ensure_aware_utc(now)
        return cls(
            id=template_id,
            name=name,
            description=description,
            display_name=display_name,
            model_id=model_id,
            persona=persona,
            config=config,
            is_builtin=is_builtin,
            cloned_from_id=cloned_from_id,
            created_at=ts,
            updated_at=ts,
        )

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------
    def rename(self, name: str, *, now: datetime) -> None:
        """Set a new template name and bump ``updated_at``."""
        if not isinstance(name, str):
            raise TypeError("name must be a str")
        if len(name) > _MAX_NAME_LENGTH:
            raise ValueError(f"name must be <= {_MAX_NAME_LENGTH} chars")
        self.name = name
        self.updated_at = ensure_aware_utc(now)

    def set_description(self, description: str, *, now: datetime) -> None:
        """Replace the description and bump ``updated_at``."""
        if not isinstance(description, str):
            raise TypeError("description must be a str")
        if len(description) > _MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                f"description must be <= {_MAX_DESCRIPTION_LENGTH} chars",
            )
        self.description = description
        self.updated_at = ensure_aware_utc(now)

    def set_role(
        self,
        *,
        now: datetime,
        display_name: str,
        model_id: str | None,
        persona: str | None,
        config: dict[str, Any] | None,
    ) -> None:
        """Replace the role definition (display_name / model / persona / config).

        Bumps ``updated_at``.  Validation mirrors ``__post_init__``.
        """
        if not isinstance(display_name, str):
            raise TypeError("display_name must be a str")
        if len(display_name) > _MAX_DISPLAY_NAME_LENGTH:
            raise ValueError(
                f"display_name must be <= {_MAX_DISPLAY_NAME_LENGTH} chars",
            )
        if model_id is not None and not isinstance(model_id, str):
            raise TypeError("model_id must be a str or None")
        if persona is not None and not isinstance(persona, str):
            raise TypeError("persona must be a str or None")
        self.display_name = display_name
        self.model_id = model_id
        self.persona = persona
        self.config = _validate_config(config)
        self.updated_at = ensure_aware_utc(now)


__all__ = [
    "AgentTemplate",
]
