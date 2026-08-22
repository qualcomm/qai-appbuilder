# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``RosterTemplate`` aggregate for the chat bounded context.

A :class:`RosterTemplate` is a *named, reusable bundle of discussion role
definitions* — a "team" such as ``Architect`` / ``Developer`` / ``Tester`` —
that a user can preview and import into any conversation.  It exists so a
multi-agent discussion roster need not be rebuilt from scratch every time
(PURE V2 enhancement; V1 has no multi-agent discussion).

Relationship to :class:`~qai.chat.domain.participant.Participant`
----------------------------------------------------------------
A ``Participant`` is strictly conversation-scoped (one row per speaker, bound
to a ``conversation_id``).  A ``RosterTemplate`` is the orthogonal,
*conversation-independent* library entry: it holds the role *definitions*
(display name + model + persona + per-role config) WITHOUT any conversation
binding.  "Applying" a template to a conversation instantiates one
``kind=named_agent`` ``Participant`` per :class:`RosterTemplateMember`.

Mutability model
----------------
The aggregate is mutable (``name`` / ``description`` / ``members`` may change
via mutators that bump ``updated_at``); members are an immutable tuple of
frozen value objects.  Like :class:`Participant`, mutators emit no platform
side-effects (domain stays pure).  ``is_builtin`` templates are factory-seeded
presets and are treated as read-only by the application layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from qai.chat.domain.ids import RosterTemplateId
from qai.platform.time import ensure_aware_utc

_MAX_NAME_LENGTH: int = 256
_MAX_DESCRIPTION_LENGTH: int = 2000
_MAX_MEMBER_DISPLAY_NAME_LENGTH: int = 256
_MAX_MEMBERS: int = 50


def _validate_member_config(config: dict[str, Any] | None) -> dict[str, Any] | None:
    """Validate + defensively copy a per-member config blob.

    Recognises the same keys as ``Participant.config`` (``allowed_tools`` /
    ``color``) so a member can be instantiated into a participant 1:1.
    """
    if config is None:
        return None
    if not isinstance(config, dict):
        raise TypeError("RosterTemplateMember.config must be a dict or None")
    allowed = config.get("allowed_tools")
    if allowed is not None and (
        not isinstance(allowed, (list, tuple))
        or not all(isinstance(tool, str) for tool in allowed)
    ):
        raise TypeError(
            "RosterTemplateMember.config['allowed_tools'] must be a list of "
            "str when present",
        )
    color = config.get("color")
    if color is not None and (
        isinstance(color, bool) or not isinstance(color, (str, int))
    ):
        raise TypeError(
            "RosterTemplateMember.config['color'] must be a str, int "
            "(palette index) or None",
        )
    copied: dict[str, Any] = dict(config)
    if allowed is not None:
        copied["allowed_tools"] = list(allowed)
    return copied


@dataclass(frozen=True, slots=True, kw_only=True)
class RosterTemplateMember:
    """One role definition inside a :class:`RosterTemplate`.

    Carries exactly the fields needed to instantiate a ``kind=named_agent``
    :class:`~qai.chat.domain.participant.Participant` when the template is
    applied to a conversation.  Immutable value object (no identity of its own;
    members are addressed positionally within their template).
    """

    display_name: str = ""
    model_id: str | None = None
    persona: str | None = None
    config: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.display_name, str):
            raise TypeError("RosterTemplateMember.display_name must be a str")
        if len(self.display_name) > _MAX_MEMBER_DISPLAY_NAME_LENGTH:
            raise ValueError(
                "RosterTemplateMember.display_name must be "
                f"<= {_MAX_MEMBER_DISPLAY_NAME_LENGTH} chars",
            )
        if self.model_id is not None and not isinstance(self.model_id, str):
            raise TypeError("RosterTemplateMember.model_id must be a str or None")
        if self.persona is not None and not isinstance(self.persona, str):
            raise TypeError("RosterTemplateMember.persona must be a str or None")
        # ``object.__setattr__`` because the dataclass is frozen; we replace the
        # config with a validated defensive copy so callers cannot mutate the
        # shared dict through the value object.
        object.__setattr__(self, "config", _validate_member_config(self.config))


@dataclass(slots=True, kw_only=True)
class RosterTemplate:
    """A named, reusable bundle of discussion role definitions.

    Construction goes through :meth:`create` to set ``created_at`` /
    ``updated_at`` consistently; loading from persistence uses the regular
    constructor.
    """

    id: RosterTemplateId
    name: str = ""
    description: str = ""
    members: tuple[RosterTemplateMember, ...] = field(default_factory=tuple)
    is_builtin: bool = False
    #: Optional default collaboration mode (``chat_mode_template.id``) this team
    #: binds to.  When set, applying the team to a conversation also selects this
    #: mode (sets ``meta["discussion"]["selected_mode_id"]``); ``None`` leaves the
    #: conversation's current mode untouched.  Tail-appended optional field
    #: (§3.1 additive); a team answers "谁参与", the bound mode answers "怎么协作".
    default_mode_id: str | None = None
    #: When this template was cloned from another (e.g. a built-in preset), the
    #: SOURCE template's id; ``None`` for originals. Tail-appended optional field
    #: (§3.1 additive). "Editing a built-in preset" = cloning a user copy then
    #: editing it; ``reset`` restores the copy's business fields from
    #: ``cloned_from_id`` in place (the copy keeps its own id / created_at).
    cloned_from_id: str | None = None
    #: Optional per-locale i18n maps for built-in presets (migration 056).
    #: ``name_i18n`` / ``description_i18n`` are ``{"en": "...", ...}`` maps;
    #: ``members_i18n`` is ``{"en": [{"display_name","persona","config"},...],
    #: "zh-CN": [...], "zh-TW": [...]}`` — the WHOLE localised members array per
    #: locale, positionally aligned with :attr:`members`. ``None`` for custom
    #: (``is_builtin=0``) templates and pre-056 rows, which then always render
    #: their canonical single-language fields as the fallback (AGENTS.md §8
    #: forward-compatibility). Tail-appended optional fields (§3.1 additive).
    name_i18n: dict[str, str] | None = None
    description_i18n: dict[str, str] | None = None
    members_i18n: dict[str, list[dict[str, Any]]] | None = None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, RosterTemplateId):
            raise TypeError(
                "RosterTemplate.id must be RosterTemplateId, got "
                f"{type(self.id).__name__}",
            )
        if not isinstance(self.name, str):
            raise TypeError("RosterTemplate.name must be a str")
        if len(self.name) > _MAX_NAME_LENGTH:
            raise ValueError(
                f"RosterTemplate.name must be <= {_MAX_NAME_LENGTH} chars",
            )
        if not isinstance(self.description, str):
            raise TypeError("RosterTemplate.description must be a str")
        if len(self.description) > _MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                "RosterTemplate.description must be "
                f"<= {_MAX_DESCRIPTION_LENGTH} chars",
            )
        self.members = _coerce_members(self.members)
        if not isinstance(self.is_builtin, bool):
            raise TypeError("RosterTemplate.is_builtin must be a bool")
        if self.default_mode_id is not None:
            if not isinstance(self.default_mode_id, str):
                raise TypeError(
                    "RosterTemplate.default_mode_id must be a str or None",
                )
            # Normalise empty/whitespace to None so "unset" has a single
            # representation (matches NULL in persistence / no mode binding).
            if not self.default_mode_id.strip():
                self.default_mode_id = None
        if self.cloned_from_id is not None and not isinstance(
            self.cloned_from_id, str
        ):
            raise TypeError("RosterTemplate.cloned_from_id must be a str or None")
        ensure_aware_utc(self.created_at)
        ensure_aware_utc(self.updated_at)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        template_id: RosterTemplateId,
        now: datetime,
        name: str = "",
        description: str = "",
        members: tuple[RosterTemplateMember, ...] | list[RosterTemplateMember] = (),
        is_builtin: bool = False,
        default_mode_id: str | None = None,
        cloned_from_id: str | None = None,
    ) -> RosterTemplate:
        """Construct a brand-new roster template."""
        ts = ensure_aware_utc(now)
        return cls(
            id=template_id,
            name=name,
            description=description,
            members=tuple(members),
            is_builtin=is_builtin,
            default_mode_id=default_mode_id,
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

    def set_members(
        self,
        members: tuple[RosterTemplateMember, ...] | list[RosterTemplateMember],
        *,
        now: datetime,
    ) -> None:
        """Replace the full member list and bump ``updated_at``."""
        self.members = _coerce_members(members)
        self.updated_at = ensure_aware_utc(now)

    def set_default_mode_id(
        self,
        default_mode_id: str | None,
        *,
        now: datetime,
    ) -> None:
        """Set/clear the bound default collaboration mode; bump ``updated_at``.

        Empty/whitespace normalises to ``None`` (no binding), matching NULL in
        persistence.
        """
        if default_mode_id is not None and not isinstance(default_mode_id, str):
            raise TypeError("default_mode_id must be a str or None")
        normalised = (
            default_mode_id.strip()
            if isinstance(default_mode_id, str) and default_mode_id.strip()
            else None
        )
        self.default_mode_id = normalised
        self.updated_at = ensure_aware_utc(now)


def _coerce_members(
    members: tuple[RosterTemplateMember, ...] | list[RosterTemplateMember],
) -> tuple[RosterTemplateMember, ...]:
    coerced = tuple(members)
    if len(coerced) > _MAX_MEMBERS:
        raise ValueError(
            f"RosterTemplate may not exceed {_MAX_MEMBERS} members",
        )
    for member in coerced:
        if not isinstance(member, RosterTemplateMember):
            raise TypeError(
                "RosterTemplate.members must be RosterTemplateMember "
                f"instances, got {type(member).__name__}",
            )
    return coerced


__all__ = [
    "RosterTemplate",
    "RosterTemplateMember",
]
