# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Context-window-size resolution chain (V1 parity).

Pure-domain helper (no I/O, no framework imports) that mirrors V1's
``backend/models_registry.py:_read_context_size_from_model_dir``
(lines 233-279) priority cascade for resolving a model's context-window
size. V1 reads two on-disk files (``config.json`` + ``prompt.json``);
the file-system access stays in the infrastructure layer
(:mod:`qai.model_runtime.infrastructure.process_service`), which parses
both JSON documents and hands the resulting ``dict`` objects to this
chain.

V1 priority order (``models_registry.py:235-239``)::

    1. config.json  dialog.context.size            (QNN/SSD authoritative)
    2. prompt.json  context_size                   (GenieAPIService ParsePromptFile)
    3. config.json  context_size / context_length / max_position_embeddings (GGUF/MNN)
    4. default 8192

The first source that yields a positive integer wins; a missing /
malformed / non-positive value falls through to the next source. When
every source abstains the chain returns the V1 default of ``8192`` so
the GGUF/MNN ctx badge is never spuriously ``0`` (V1 parity — see
``models_registry.py:279``).

Design note — why a chain of pure functions
--------------------------------------------
V1 inlined the 4-step cascade in one ~46-line function with three
nested ``try/except`` blocks reopening files. The V2 shape splits the
*resolution policy* (this pure chain, unit-testable without touching
disk) from the *I/O* (the infra scanner). Each source is a small named
function so the priority order is declarative (``_CHAIN`` tuple) and a
new source can be slotted in without touching the others — this is
strictly easier to maintain / extend than the V1 monolith while
producing byte-for-byte the same numbers.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

__all__ = [
    "DEFAULT_CONTEXT_SIZE",
    "resolve_context_length",
]

# V1 ``models_registry.py:279`` — final fallback when no source resolves.
DEFAULT_CONTEXT_SIZE = 8192


def _coerce_positive_int(value: Any) -> int | None:
    """Return ``int(value)`` when it is a positive integer, else ``None``.

    Mirrors V1's ``if ctx: return int(ctx)`` truthiness guard
    (``models_registry.py:249-250`` / ``261-262`` / ``274-275``): a
    falsy value (``0`` / ``""`` / ``None``) or one that does not coerce
    to ``int`` is treated as "this source abstains". ``bool`` is
    rejected explicitly (it is an ``int`` subclass but never a real
    context size).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return None
    return coerced if coerced > 0 else None


def _from_dialog_context_size(
    config: Mapping[str, Any] | None,
    prompt: Mapping[str, Any] | None,
) -> int | None:
    """Source 1 — ``config.json`` ``dialog.context.size`` (QNN/SSD)."""
    if not isinstance(config, Mapping):
        return None
    dialog = config.get("dialog")
    if not isinstance(dialog, Mapping):
        return None
    context = dialog.get("context")
    if not isinstance(context, Mapping):
        return None
    return _coerce_positive_int(context.get("size"))


def _from_prompt_context_size(
    config: Mapping[str, Any] | None,
    prompt: Mapping[str, Any] | None,
) -> int | None:
    """Source 2 — ``prompt.json`` ``context_size`` (ParsePromptFile)."""
    if not isinstance(prompt, Mapping):
        return None
    return _coerce_positive_int(prompt.get("context_size"))


def _from_config_top_level(
    config: Mapping[str, Any] | None,
    prompt: Mapping[str, Any] | None,
) -> int | None:
    """Source 3 — ``config.json`` top-level fields (GGUF/MNN).

    V1 order (``models_registry.py:271-273``): ``context_size`` ->
    ``context_length`` -> ``max_position_embeddings``; the first
    positive value wins.
    """
    if not isinstance(config, Mapping):
        return None
    for key in ("context_size", "context_length", "max_position_embeddings"):
        resolved = _coerce_positive_int(config.get(key))
        if resolved is not None:
            return resolved
    return None


# Declarative priority cascade (V1 ``models_registry.py:235-238`` order).
_CHAIN: tuple[
    Callable[
        [Mapping[str, Any] | None, Mapping[str, Any] | None], int | None
    ],
    ...,
] = (
    _from_dialog_context_size,
    _from_prompt_context_size,
    _from_config_top_level,
)


def resolve_context_length(
    config: Mapping[str, Any] | None,
    prompt: Mapping[str, Any] | None = None,
) -> int:
    """Resolve the context-window size from parsed model metadata.

    Walks the V1 priority chain; returns the first source's positive
    integer, or :data:`DEFAULT_CONTEXT_SIZE` (8192) when every source
    abstains (V1 parity).

    Args:
        config: Parsed ``config.json`` mapping (or ``None`` when absent /
            unreadable).
        prompt: Parsed ``prompt.json`` mapping (or ``None`` when the
            model has no ``prompt.json``).

    Returns:
        A positive context-window size; never ``0`` (V1 returns 8192
        when nothing resolves).
    """
    for source in _CHAIN:
        resolved = source(config, prompt)
        if resolved is not None:
            return resolved
    return DEFAULT_CONTEXT_SIZE
