# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Per-model parameter clamping / filtering (PR-090, S9 C-5).

Implements audit item :ref:`C-5` (model param resolution) from
``docs/90-refactor/S9-final-parity-audit.md`` §2.1.

The legacy ``backend/chat_handler.py:1174-1363`` block resolved seven
sampling tunables (``temperature``, ``top_p``, ``max_tokens``,
``frequency_penalty``, ``presence_penalty``, ``stop``, ``seed``) against
a per-model **profile** so user UI controls were honoured without
breaking model families that hard-require specific values
(e.g. GPT-5 / o-series force ``temperature=1.0``; some Anthropic
models reject ``seed`` entirely).  The rewritten adapter regressed to
"forward whatever the request carried" — every request used the API
defaults and user UI controls had no effect.

This module restores the legacy semantics with two surfaces:

* :class:`ModelProfile` — frozen dataclass capturing the per-model
  constraints discovered at ``ResolvedModel`` time (supported flag,
  family-fixed value, min/max range, default).  The dataclass is
  intentionally additive; an empty profile (the no-op case) yields a
  resolver that simply forwards whatever the request supplies — which
  matches the rewritten adapter's existing behaviour byte-for-byte.
* :func:`resolve_params` — pure function that takes the resolved
  profile + the request's ``extra`` dict and returns the
  ready-for-payload sub-dict.  The seven keys above are the only ones
  the resolver is concerned with; any other ``extra`` keys flow through
  the existing ``_build_payload`` filter unchanged.

The function is **pure**: no I/O, no global state, no time, no random.
Adapter wiring at :mod:`qai.chat.infrastructure.llm_stream` calls it
once per request inside ``_build_payload``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


__all__ = [
    "ModelProfile",
    "ParamConstraint",
    "profile_from_config_params",
    "resolve_params",
    "TUNABLE_KEYS",
]


# Set of ``extra`` keys this resolver knows how to clamp / filter.  Any
# other key in ``request_extra`` is left untouched (the adapter's
# ``_build_payload`` already drops reserved keys like ``model``,
# ``messages``, ``stream``).
TUNABLE_KEYS: frozenset[str] = frozenset(
    {
        "temperature",
        "top_p",
        "max_tokens",
        "frequency_penalty",
        "presence_penalty",
        "stop",
        "seed",
    },
)


@dataclass(frozen=True, slots=True)
class ParamConstraint:
    """Per-tunable constraint slice of a :class:`ModelProfile`.

    Empty fields mean "no constraint" — leave the value alone.

    Attributes
    ----------
    supported:
        ``False`` => drop the key entirely (model rejects it).  ``None``
        means the resolver inherits the global default ("supported
        unless proven otherwise").
    fixed:
        Family-locked value that overrides any user input
        (e.g. GPT-5 ``temperature=1.0``).  When set, ``min`` / ``max``
        are ignored.
    min:
        Lower clamp bound.  ``None`` => no lower bound.
    max:
        Upper clamp bound.  ``None`` => no upper bound.
    default:
        Value injected when the request did not supply one and the
        constraint is satisfied.  ``None`` => omit the key when missing
        from the request.
    """

    supported: bool | None = None
    fixed: float | int | None = None
    min: float | int | None = None
    max: float | int | None = None
    default: float | int | list[str] | None = None


@dataclass(frozen=True, slots=True)
class ModelProfile:
    """Resolved set of per-tunable constraints for a single model id.

    Constructed from ``cloud_models.json`` ``params`` entries plus the
    family heuristics encoded by the model registry (PR-091 territory;
    PR-090 wires the dataclass with empty defaults so the resolver is
    a no-op until the registry lands).

    The class is **frozen** to make it shareable across requests.
    """

    model_id: str = ""
    context_length: int = 0
    temperature: ParamConstraint = field(default_factory=ParamConstraint)
    top_p: ParamConstraint = field(default_factory=ParamConstraint)
    max_tokens: ParamConstraint = field(default_factory=ParamConstraint)
    frequency_penalty: ParamConstraint = field(default_factory=ParamConstraint)
    presence_penalty: ParamConstraint = field(default_factory=ParamConstraint)
    stop: ParamConstraint = field(default_factory=ParamConstraint)
    seed: ParamConstraint = field(default_factory=ParamConstraint)

    def constraint(self, key: str) -> ParamConstraint:
        """Return the :class:`ParamConstraint` for *key*; empty if unknown."""
        return getattr(self, key, ParamConstraint())


# ---------------------------------------------------------------------------
# Pure resolver
# ---------------------------------------------------------------------------


def _coerce_float(value: Any) -> float | None:
    """Tolerant float coercion; returns ``None`` for unparseable values."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    """Tolerant int coercion; returns ``None`` for unparseable values."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _clamp(
    value: float,
    *,
    lower: float | int | None,
    upper: float | int | None,
) -> float:
    """Clamp ``value`` to ``[lower, upper]`` with open ends.

    A ``None`` bound means "open" (no clamp applied on that side).
    """
    if lower is not None and value < lower:
        return float(lower)
    if upper is not None and value > upper:
        return float(upper)
    return value


def _resolve_float_param(
    *,
    constraint: ParamConstraint,
    user_value: Any,
) -> float | None:
    """Resolve a single float-typed tunable.

    Order of precedence (highest first):

    1. ``supported is False`` → drop (return ``None``).
    2. ``constraint.fixed`` → return verbatim (family lock).
    3. User-supplied value → coerce & clamp.
    4. ``constraint.default`` → coerce & clamp.
    5. Else → drop (return ``None``).
    """
    if constraint.supported is False:
        return None
    if constraint.fixed is not None:
        return float(constraint.fixed)
    candidate = _coerce_float(user_value)
    if candidate is None:
        candidate = _coerce_float(constraint.default)
    if candidate is None:
        return None
    return _clamp(candidate, lower=constraint.min, upper=constraint.max)


def _resolve_int_param(
    *,
    constraint: ParamConstraint,
    user_value: Any,
    require_positive: bool = False,
) -> int | None:
    """Resolve a single int-typed tunable.  Same precedence as float."""
    if constraint.supported is False:
        return None
    if constraint.fixed is not None:
        return int(constraint.fixed)
    candidate = _coerce_int(user_value)
    if candidate is None:
        candidate = _coerce_int(constraint.default)
    if candidate is None:
        return None
    clamped = int(
        _clamp(float(candidate), lower=constraint.min, upper=constraint.max),
    )
    if require_positive and clamped <= 0:
        return None
    return clamped


def _resolve_stop_param(
    *,
    constraint: ParamConstraint,
    user_value: Any,
) -> list[str] | None:
    """Resolve the ``stop`` list (max 4 sequences per OpenAI's contract)."""
    if constraint.supported is False:
        return None
    candidate: Any = user_value if user_value is not None else constraint.default
    if not isinstance(candidate, list) or not candidate:
        return None
    sanitised = [str(s) for s in candidate if isinstance(s, (str, bytes))]
    if not sanitised:
        return None
    return sanitised[:4]


# ---------------------------------------------------------------------------
# Config → profile builder
# ---------------------------------------------------------------------------

#: Tunable keys a cloud-model-catalog ``params`` entry may constrain.  These
#: map 1:1 onto :class:`ModelProfile` fields.
_CONFIG_PARAM_KEYS: frozenset[str] = TUNABLE_KEYS


def _constraint_from_config(raw: Any) -> ParamConstraint:
    """Build a :class:`ParamConstraint` from one config ``params[key]`` dict.

    Tolerant of partial / malformed input — any unparseable field is left
    as the "no constraint" default so a typo in the config never crashes a
    chat turn (it just falls back to the family / API default for that
    field).
    """
    if not isinstance(raw, dict):
        return ParamConstraint()
    supported = raw.get("supported")
    return ParamConstraint(
        supported=supported if isinstance(supported, bool) else None,
        fixed=_coerce_float(raw.get("fixed")),
        min=_coerce_float(raw.get("min")),
        max=_coerce_float(raw.get("max")),
        default=_coerce_float(raw.get("default")),
    )


def profile_from_config_params(
    model_id: str,
    params: dict[str, Any] | None,
    *,
    context_length: int = 0,
) -> ModelProfile:
    """Build a :class:`ModelProfile` from a cloud-catalog ``params`` dict.

    ``params`` is the per-model constraint object the user configures in
    Settings → Cloud Models (``cloud_models.json`` ``models[].params``),
    e.g. ``{"temperature": {"supported": false}, "top_p": {...}}``.  Unknown
    keys (such as ``thought_signature``, which is handled separately by the
    history-flatten path) are ignored here.

    A ``None`` / empty ``params`` yields an empty profile — a no-op resolver
    that forwards whatever the request carried (preserving the existing
    family-regex behaviour applied upstream in
    ``StreamChatUseCase._apply_sampling_params``).
    """
    if not isinstance(params, dict) or not params:
        return ModelProfile(model_id=model_id, context_length=context_length)
    kwargs: dict[str, Any] = {}
    for key in _CONFIG_PARAM_KEYS:
        if key in params:
            kwargs[key] = _constraint_from_config(params[key])
    return ModelProfile(
        model_id=model_id, context_length=context_length, **kwargs
    )


def resolve_params(
    profile: ModelProfile,
    request_extra: dict[str, Any] | None,
) -> dict[str, Any]:
    """Return the sub-dict of payload keys *profile* permits.

    The returned dict is suitable for unconditional ``payload.update(...)``
    inside :meth:`HttpOpenAICompatibleLLMStream._build_payload`.  Keys
    that fail the per-tunable constraint (e.g. ``supported=False``) are
    omitted entirely so the upstream API never sees them.

    Non-tunable keys in ``request_extra`` are NOT returned here — the
    adapter's existing payload-merge loop continues to forward them.

    Parameters
    ----------
    profile:
        Per-model constraints.  An empty :class:`ModelProfile` makes
        the resolver a no-op forwarder for whatever the request carried
        (after coercion + the OpenAI ``stop[:4]`` cap).
    request_extra:
        The ``LLMStreamRequest.extra`` dict (or ``None``).  Keys outside
        :data:`TUNABLE_KEYS` are ignored by this function.
    """
    extra = request_extra or {}
    out: dict[str, Any] = {}

    temp = _resolve_float_param(
        constraint=profile.temperature,
        user_value=extra.get("temperature"),
    )
    if temp is not None:
        out["temperature"] = temp

    top_p = _resolve_float_param(
        constraint=profile.top_p,
        user_value=extra.get("top_p"),
    )
    if top_p is not None:
        out["top_p"] = top_p

    max_tokens = _resolve_int_param(
        constraint=profile.max_tokens,
        user_value=extra.get("max_tokens"),
        require_positive=True,
    )
    if max_tokens is not None:
        out["max_tokens"] = max_tokens

    freq_penalty = _resolve_float_param(
        constraint=profile.frequency_penalty,
        user_value=extra.get("frequency_penalty"),
    )
    if freq_penalty is not None:
        out["frequency_penalty"] = _clamp(freq_penalty, lower=-2.0, upper=2.0)

    pres_penalty = _resolve_float_param(
        constraint=profile.presence_penalty,
        user_value=extra.get("presence_penalty"),
    )
    if pres_penalty is not None:
        out["presence_penalty"] = _clamp(pres_penalty, lower=-2.0, upper=2.0)

    stop = _resolve_stop_param(
        constraint=profile.stop,
        user_value=extra.get("stop"),
    )
    if stop is not None:
        out["stop"] = stop

    seed = _resolve_int_param(
        constraint=profile.seed,
        user_value=extra.get("seed"),
        require_positive=True,
    )
    if seed is not None:
        out["seed"] = seed

    return out
