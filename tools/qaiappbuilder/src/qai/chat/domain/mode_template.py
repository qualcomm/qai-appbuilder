# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``ModeTemplate`` aggregate for the chat bounded context.

A :class:`ModeTemplate` is a *named collaboration mode* — "怎么协作": ``讨论`` /
``评审`` / ``辩论`` / ``实施`` / custom — the third tier of the three-tier
template system (design §26 / §27).  A team (``RosterTemplate``) answers "谁参与";
a mode answers "怎么协作".  They are orthogonal: the same team can run any mode;
the same mode applies to any team (PURE V2 enhancement; V1 has no multi-agent
discussion).

The mode (design §26.1, V1 subset)
----------------------------------
``identity``     — name / description / is_builtin
``framing``      — the prose expressing HOW to collaborate (tone + goal +
                   boundary).  It NEVER carries real permission (§26.3 / §26.8).
``tool_policy``  — per-tool ``allow`` / ``deny`` policy; the SINGLE truth for
                   "is this tool usable in this mode" (State-Truth-First: real
                   permission is in the core, not in framing prose).
``flow_policy``  — speaker_strategy / max_rounds / judge_enabled /
                   allow_mode_switch (collaboration preferences).

V1 scope note (no sandbox / no confirmation gate)
-------------------------------------------------
The discussion runtime executes tools through the ordinary
``ToolInvocationPort`` — it has NO execution-time confirmation channel and NO
subprocess sandbox.  Carrying ``require_confirmation`` / ``allow_in_sandbox_only``
/ sandbox / confirm-before-* fields would be DEAD, MISLEADING state (the framing
would promise a confirmation that never happens), which violates
State-Truth-First.  So V1 deliberately models tool policy as a clean ``allow`` /
``deny`` only.  A future version that adds an execution-time confirmation /
sandbox gate to the discussion path can reintroduce the richer states then.

Mutability model
----------------
The aggregate is mutable (each policy may be replaced via a mutator that bumps
``updated_at``); policies are immutable frozen value objects.  ``is_builtin``
templates are factory-seeded presets, treated as read-only by the application
layer.  Domain stays pure (no platform side-effects).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from qai.chat.domain.ids import ModeTemplateId
from qai.platform.time import ensure_aware_utc

_MAX_NAME_LENGTH: int = 256
_MAX_DESCRIPTION_LENGTH: int = 2000
_MAX_FRAMING_LENGTH: int = 8000
_MAX_TOOLS: int = 200

#: meeting-room hard-constraint bounds (task §7.3 / decisions 3+9). Soft-only:
#: these are advertised to the speaker LLM as a prose constraint (decision 4 —
#: NO streaming truncation / NO ``asyncio.wait_for``); the bounds keep the value
#: meaningful (too small = useless, too large = no constraint).
_MIN_MAX_CHARS_PER_TURN: int = 50
_MAX_MAX_CHARS_PER_TURN: int = 5000
_MIN_MAX_SECONDS_PER_TURN: int = 5
_MAX_MAX_SECONDS_PER_TURN: int = 600


class ToolPolicy(str, Enum):
    """Per-tool policy for a mode (design §26.5, V1 ``allow`` / ``deny``).

    A mode either advertises a tool to the speaker's LLM (``allow``) or removes
    it entirely (``deny``).  This is the SINGLE real gate on tool advertisement
    in the discussion path (State-Truth-First) — framing prose carries no
    permission.  (Execution-time confirmation / sandbox states are out of V1
    scope, see the module docstring.)
    """

    DENY = "deny"
    ALLOW = "allow"


def _coerce_tool_policy(raw: object) -> ToolPolicy:
    if isinstance(raw, ToolPolicy):
        return raw
    if isinstance(raw, str):
        try:
            return ToolPolicy(raw)
        except ValueError as exc:
            raise ValueError(
                f"unknown tool policy {raw!r}; must be one of "
                f"{[p.value for p in ToolPolicy]}",
            ) from exc
    raise TypeError("tool policy must be a ToolPolicy or its str value")


@dataclass(frozen=True, slots=True, kw_only=True)
class ModeToolPolicy:
    """Per-tool ``allow`` / ``deny`` policy + a default for unlisted tools."""

    #: tool-name → ToolPolicy.
    tools: dict[str, ToolPolicy] = field(default_factory=dict)
    #: policy for tools NOT explicitly listed (a role may have allowed_tools the
    #: mode does not mention).  Default ``allow`` keeps modes permissive unless
    #: they explicitly clamp down (built-in 实施 mode sets this to ``deny``).
    default: ToolPolicy = ToolPolicy.ALLOW

    def __post_init__(self) -> None:
        if not isinstance(self.tools, dict):
            raise TypeError("ModeToolPolicy.tools must be a dict")
        if len(self.tools) > _MAX_TOOLS:
            raise ValueError(f"ModeToolPolicy may not exceed {_MAX_TOOLS} tools")
        coerced: dict[str, ToolPolicy] = {}
        for name, pol in self.tools.items():
            if not isinstance(name, str):
                raise TypeError("ModeToolPolicy tool name must be a str")
            coerced[name] = _coerce_tool_policy(pol)
        object.__setattr__(self, "tools", coerced)
        object.__setattr__(self, "default", _coerce_tool_policy(self.default))

    def policy_for(self, tool: str) -> ToolPolicy:
        """Return the policy for ``tool`` (explicit entry, else the default)."""
        return self.tools.get(tool, self.default)

    def is_advertised(self, tool: str) -> bool:
        """Whether ``tool`` may be advertised to the LLM (``allow``, not deny)."""
        return self.policy_for(tool) is ToolPolicy.ALLOW

    def to_dict(self) -> dict[str, Any]:
        return {
            "default": self.default.value,
            "tools": {k: v.value for k, v in self.tools.items()},
        }

    @classmethod
    def from_dict(cls, raw: object) -> ModeToolPolicy:
        if not isinstance(raw, dict):
            return cls()
        tools_raw = raw.get("tools")
        tools: dict[str, ToolPolicy] = {}
        if isinstance(tools_raw, dict):
            for name, pol in tools_raw.items():
                if not isinstance(name, str):
                    continue
                try:
                    tools[name] = _coerce_tool_policy(pol)
                except (TypeError, ValueError):
                    continue
        default_raw = raw.get("default", ToolPolicy.ALLOW.value)
        try:
            default = _coerce_tool_policy(default_raw)
        except (TypeError, ValueError):
            default = ToolPolicy.ALLOW
        return cls(tools=tools, default=default)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModeFlowPolicy:
    """Speaker / round / judge preferences for a mode (§26.1 flow_policy)."""

    speaker_strategy: str = "manager"
    max_rounds: int = 8
    judge_enabled: bool = True
    allow_mode_switch: bool = True
    system_model_id: str | None = None

    def __post_init__(self) -> None:
        if self.speaker_strategy not in ("manager", "round_robin"):
            raise ValueError(
                "ModeFlowPolicy.speaker_strategy must be 'manager' or "
                "'round_robin'",
            )
        if not isinstance(self.max_rounds, int) or isinstance(
            self.max_rounds, bool
        ):
            raise TypeError("ModeFlowPolicy.max_rounds must be an int")
        if not 1 <= self.max_rounds <= 1000:
            raise ValueError("ModeFlowPolicy.max_rounds must be in [1, 1000]")
        for name in ("judge_enabled", "allow_mode_switch"):
            if not isinstance(getattr(self, name), bool):
                raise TypeError(f"ModeFlowPolicy.{name} must be a bool")
        if self.system_model_id is not None and not isinstance(
            self.system_model_id, str
        ):
            raise TypeError(
                "ModeFlowPolicy.system_model_id must be a str or None"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "speaker_strategy": self.speaker_strategy,
            "max_rounds": self.max_rounds,
            "judge_enabled": self.judge_enabled,
            "allow_mode_switch": self.allow_mode_switch,
            "system_model_id": self.system_model_id,
        }

    @classmethod
    def from_dict(cls, raw: object) -> ModeFlowPolicy:
        if not isinstance(raw, dict):
            return cls()
        kwargs: dict[str, Any] = {}
        strat = raw.get("speaker_strategy")
        if strat in ("manager", "round_robin"):
            kwargs["speaker_strategy"] = strat
        mr = raw.get("max_rounds")
        if isinstance(mr, int) and not isinstance(mr, bool) and 1 <= mr <= 1000:
            kwargs["max_rounds"] = mr
        for name in ("judge_enabled", "allow_mode_switch"):
            v = raw.get(name)
            if isinstance(v, bool):
                kwargs[name] = v
        smid = raw.get("system_model_id")
        if isinstance(smid, str):
            kwargs["system_model_id"] = smid
        return cls(**kwargs)


@dataclass(frozen=True, slots=True, kw_only=True)
class ModeHardConstraints:
    """Meeting-room soft constraints injected into the speaker prompt (§26.8).

    Two INDEPENDENT optional caps (decisions 3 + 9):

    * ``max_chars_per_turn`` — per-turn length cap (chars; Chinese counted by
      character, English by whitespace-delimited word — decision 5);
    * ``max_seconds_per_turn`` — per-round time cap (seconds).

    ``None`` on either field = that constraint is NOT enabled (both ``None`` =
    no constraint at all → nothing appended to the prompt).  These are **soft**
    constraints (decision 4): the orchestrator appends a prose instruction to
    the speaker's persona — it does NOT truncate the stream or
    ``asyncio.wait_for`` the turn.  Bounds keep the value meaningful (§7.3).
    """

    max_chars_per_turn: int | None = None
    max_seconds_per_turn: int | None = None

    def __post_init__(self) -> None:
        self._validate_bound(
            "max_chars_per_turn",
            self.max_chars_per_turn,
            _MIN_MAX_CHARS_PER_TURN,
            _MAX_MAX_CHARS_PER_TURN,
        )
        self._validate_bound(
            "max_seconds_per_turn",
            self.max_seconds_per_turn,
            _MIN_MAX_SECONDS_PER_TURN,
            _MAX_MAX_SECONDS_PER_TURN,
        )

    @staticmethod
    def _validate_bound(
        name: str, value: int | None, lo: int, hi: int
    ) -> None:
        if value is None:
            return
        if not isinstance(value, int) or isinstance(value, bool):
            raise TypeError(f"ModeHardConstraints.{name} must be an int or None")
        if not lo <= value <= hi:
            raise ValueError(
                f"ModeHardConstraints.{name} must be in [{lo}, {hi}]",
            )

    @property
    def is_empty(self) -> bool:
        """``True`` when NEITHER constraint is enabled (nothing to inject)."""
        return self.max_chars_per_turn is None and self.max_seconds_per_turn is None

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_chars_per_turn": self.max_chars_per_turn,
            "max_seconds_per_turn": self.max_seconds_per_turn,
        }

    @classmethod
    def from_dict(cls, raw: object) -> ModeHardConstraints:
        if not isinstance(raw, dict):
            return cls()

        def _coerce(key: str, lo: int, hi: int) -> int | None:
            # Mirror ModeFlowPolicy.from_dict: deserialisation NEVER raises —
            # bool / non-int / OUT-OF-RANGE values are all coerced to None
            # ("constraint not enabled"). This keeps the read path robust against
            # legacy / hand-edited DB rows (State-Truth-First): a stale
            # out-of-range value reads back as "no constraint" instead of
            # crashing _row_to_template. Construction (__post_init__) still
            # validates so the write path rejects nonsense; the front-end input
            # already clamps to [lo, hi] so the normal UX never hits this.
            v = raw.get(key)
            if isinstance(v, bool) or not isinstance(v, int):
                return None
            if not lo <= v <= hi:
                return None
            return v

        return cls(
            max_chars_per_turn=_coerce(
                "max_chars_per_turn",
                _MIN_MAX_CHARS_PER_TURN,
                _MAX_MAX_CHARS_PER_TURN,
            ),
            max_seconds_per_turn=_coerce(
                "max_seconds_per_turn",
                _MIN_MAX_SECONDS_PER_TURN,
                _MAX_MAX_SECONDS_PER_TURN,
            ),
        )


@dataclass(slots=True, kw_only=True)
class ModeTemplate:
    """A named collaboration mode (identity / framing / tool_policy / flow)."""

    id: ModeTemplateId
    name: str = ""
    description: str = ""
    framing: str = ""
    tool_policy: ModeToolPolicy = field(default_factory=ModeToolPolicy)
    flow_policy: ModeFlowPolicy = field(default_factory=ModeFlowPolicy)
    hard_constraints: ModeHardConstraints = field(
        default_factory=lambda: ModeHardConstraints(),
    )
    is_builtin: bool = False
    #: When this mode was cloned from another (e.g. a built-in preset), the
    #: SOURCE template's id; ``None`` for originals. Tail-appended optional field
    #: (§3.1 additive). "Editing a built-in preset" = cloning a user copy then
    #: editing it; ``reset`` restores the copy's business fields from
    #: ``cloned_from_id`` in place (the copy keeps its own id / created_at).
    cloned_from_id: str | None = None
    #: Optional per-locale i18n maps for built-in presets (migration 056). Each
    #: is ``{"en": "...", "zh-CN": "...", "zh-TW": "..."}`` loaded from the
    #: matching ``*_i18n_json`` column; ``None`` for custom (``is_builtin=0``)
    #: templates and pre-056 rows — which then always render their canonical
    #: single-language field (name / description / framing) as the fallback
    #: (AGENTS.md §8 forward-compatibility). NOTE: the built-in 讨论 mode's
    #: framing is intentionally an empty string in all three languages — an empty
    #: translation is treated as "no override" and falls back to the (also empty)
    #: framing, which is correct. Tail-appended optional fields (§3.1 additive).
    name_i18n: dict[str, str] | None = None
    description_i18n: dict[str, str] | None = None
    framing_i18n: dict[str, str] | None = None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.id, ModeTemplateId):
            raise TypeError(
                "ModeTemplate.id must be ModeTemplateId, got "
                f"{type(self.id).__name__}",
            )
        if not isinstance(self.name, str):
            raise TypeError("ModeTemplate.name must be a str")
        if len(self.name) > _MAX_NAME_LENGTH:
            raise ValueError(f"ModeTemplate.name must be <= {_MAX_NAME_LENGTH}")
        if not isinstance(self.description, str):
            raise TypeError("ModeTemplate.description must be a str")
        if len(self.description) > _MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                f"ModeTemplate.description must be <= {_MAX_DESCRIPTION_LENGTH}",
            )
        if not isinstance(self.framing, str):
            raise TypeError("ModeTemplate.framing must be a str")
        if len(self.framing) > _MAX_FRAMING_LENGTH:
            raise ValueError(
                f"ModeTemplate.framing must be <= {_MAX_FRAMING_LENGTH}",
            )
        if not isinstance(self.tool_policy, ModeToolPolicy):
            raise TypeError("ModeTemplate.tool_policy must be a ModeToolPolicy")
        if not isinstance(self.flow_policy, ModeFlowPolicy):
            raise TypeError("ModeTemplate.flow_policy must be a ModeFlowPolicy")
        if not isinstance(self.hard_constraints, ModeHardConstraints):
            raise TypeError(
                "ModeTemplate.hard_constraints must be a ModeHardConstraints",
            )
        if not isinstance(self.is_builtin, bool):
            raise TypeError("ModeTemplate.is_builtin must be a bool")
        if self.cloned_from_id is not None and not isinstance(
            self.cloned_from_id, str
        ):
            raise TypeError("ModeTemplate.cloned_from_id must be a str or None")
        ensure_aware_utc(self.created_at)
        ensure_aware_utc(self.updated_at)

    # ------------------------------------------------------------------
    # Factories
    # ------------------------------------------------------------------
    @classmethod
    def create(
        cls,
        *,
        template_id: ModeTemplateId,
        now: datetime,
        name: str = "",
        description: str = "",
        framing: str = "",
        tool_policy: ModeToolPolicy | None = None,
        flow_policy: ModeFlowPolicy | None = None,
        hard_constraints: ModeHardConstraints | None = None,
        is_builtin: bool = False,
        cloned_from_id: str | None = None,
    ) -> ModeTemplate:
        ts = ensure_aware_utc(now)
        return cls(
            id=template_id,
            name=name,
            description=description,
            framing=framing,
            tool_policy=tool_policy if tool_policy is not None else ModeToolPolicy(),
            flow_policy=flow_policy if flow_policy is not None else ModeFlowPolicy(),
            hard_constraints=(
                hard_constraints
                if hard_constraints is not None
                else ModeHardConstraints()
            ),
            is_builtin=is_builtin,
            cloned_from_id=cloned_from_id,
            created_at=ts,
            updated_at=ts,
        )

    # ------------------------------------------------------------------
    # Mutators
    # ------------------------------------------------------------------
    def rename(self, name: str, *, now: datetime) -> None:
        if not isinstance(name, str):
            raise TypeError("name must be a str")
        if len(name) > _MAX_NAME_LENGTH:
            raise ValueError(f"name must be <= {_MAX_NAME_LENGTH} chars")
        self.name = name
        self.updated_at = ensure_aware_utc(now)

    def set_description(self, description: str, *, now: datetime) -> None:
        if not isinstance(description, str):
            raise TypeError("description must be a str")
        if len(description) > _MAX_DESCRIPTION_LENGTH:
            raise ValueError(
                f"description must be <= {_MAX_DESCRIPTION_LENGTH} chars",
            )
        self.description = description
        self.updated_at = ensure_aware_utc(now)

    def set_framing(self, framing: str, *, now: datetime) -> None:
        if not isinstance(framing, str):
            raise TypeError("framing must be a str")
        if len(framing) > _MAX_FRAMING_LENGTH:
            raise ValueError(f"framing must be <= {_MAX_FRAMING_LENGTH} chars")
        self.framing = framing
        self.updated_at = ensure_aware_utc(now)

    def set_tool_policy(self, policy: ModeToolPolicy, *, now: datetime) -> None:
        if not isinstance(policy, ModeToolPolicy):
            raise TypeError("policy must be a ModeToolPolicy")
        self.tool_policy = policy
        self.updated_at = ensure_aware_utc(now)

    def set_flow_policy(self, policy: ModeFlowPolicy, *, now: datetime) -> None:
        if not isinstance(policy, ModeFlowPolicy):
            raise TypeError("policy must be a ModeFlowPolicy")
        self.flow_policy = policy
        self.updated_at = ensure_aware_utc(now)

    def set_hard_constraints(
        self, constraints: ModeHardConstraints, *, now: datetime
    ) -> None:
        if not isinstance(constraints, ModeHardConstraints):
            raise TypeError("constraints must be a ModeHardConstraints")
        self.hard_constraints = constraints
        self.updated_at = ensure_aware_utc(now)


def effective_advertised_tools(
    *,
    role_tools: set[str],
    mode: ModeTemplate | None,
    global_excluded: frozenset[str],
) -> set[str]:
    """Compute the tools advertised to a speaker's LLM (design §26.5).

    ``effective = role_tools ∩ mode_policy ∩ global_policy`` (the V1 subset of
    the full intersection).  ``mode`` ``None`` = no mode selected → only
    role ∩ global applies (current behaviour, zero regression).  This is the
    SINGLE place the V1 permission intersection lives, so it can be unit tested
    in isolation and reused by the orchestrator.
    """
    allowed = {t for t in role_tools if t not in global_excluded}
    if mode is None:
        return allowed
    return {t for t in allowed if mode.tool_policy.is_advertised(t)}


@dataclass(frozen=True, slots=True, kw_only=True)
class ModeLintIssue:
    """One conflict found by :func:`lint_mode`."""

    #: ``"warning"`` is advisory (surfaced to the user, does not block).  V1 has
    #: no ``error`` issues — the tool policy is the real gate, so framing prose
    #: can never grant a permission it lacks (nothing to hard-block).
    severity: str
    code: str
    message: str


#: Real execution tools whose presence contradicts a "do not execute" framing.
_EXECUTION_TOOLS: tuple[str, ...] = ("write", "edit", "exec")

#: Framing tokens that discourage execution (the soft-conflict trigger).
_FORBIDS_EXEC_TOKENS: tuple[str, ...] = (
    "不要执行",
    "不写代码",
    "不要写代码",
    "do not execute",
    "don't execute",
)


def lint_mode(mode: ModeTemplate) -> tuple[ModeLintIssue, ...]:
    """Detect framing ↔ tool-policy soft conflicts in a mode (design §26.8).

    Pure, no IO.  Returns advisory **warning** issues only (V1 has no hard
    blocks: the tool policy is the real gate, so the prose can never grant a
    permission it does not have).  Currently flags one soft inconsistency: the
    framing discourages execution (``不要执行命令`` / ``不写代码`` / …) yet an
    execution tool (``write`` / ``edit`` / ``exec``) is still ``allow``ed by the
    tool policy — the user should see it but may keep it (the real gate is the
    tool policy, not the prose).
    """
    issues: list[ModeLintIssue] = []
    forbids_exec = any(tok in mode.framing for tok in _FORBIDS_EXEC_TOKENS)
    if forbids_exec:
        # Report EVERY conflicting execution tool (not just the first): a framing
        # that forbids execution while ``write`` AND ``edit`` AND ``exec`` are all
        # allowed should surface all three at once, not make the user fix them one
        # at a time (each message carries the tool name so they stay distinct).
        for tool in _EXECUTION_TOOLS:
            if mode.tool_policy.is_advertised(tool):
                issues.append(
                    ModeLintIssue(
                        severity="warning",
                        code="framing_forbids_but_tool_allowed",
                        message=(
                            f"framing discourages execution but tool {tool!r} is "
                            "still allowed by the tool policy"
                        ),
                    )
                )
    return tuple(issues)


__all__ = [
    "ModeTemplate",
    "ModeToolPolicy",
    "ModeFlowPolicy",
    "ModeHardConstraints",
    "ToolPolicy",
    "ModeLintIssue",
    "effective_advertised_tools",
    "lint_mode",
]
