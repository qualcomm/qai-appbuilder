# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""P2c — real per-model context window resolver (apps/api composition bridge).

WHY THIS EXISTS
===============
:func:`qai.chat.domain.model_profiles.get_context_limit` guesses a model's
context window by regex-matching its NAME against a family table. That is the
only thing a pure domain function can do, and it is fine for the well-known
cloud families it was written for. It is actively wrong for a locally
registered model, because the name tells you nothing about how the user
actually configured it:

    Glymur_QWEN3.8   real window 32768 (configured in the model directory)
                     get_context_limit() -> 131072, because the name
                     contains "qwen3"

A 4x over-estimate does not fail loudly. It fails by making the compaction
trigger unreachable: the main loop compacts at ``0.80 x window``, which the
guess puts at 104857 tokens — a threshold a 32768-token window can never
reach. So the prompt grew until it filled the REAL window, and the final tool
call was silently truncated by ``finish_reason=length`` (incomplete arguments,
execution cancelled). Note this is NOT the same failure as a provider
rejection: there is no 400 to recover from, the request returns 200 with
truncated content, so the context-overflow recovery path is never even
consulted.

WHAT IT RESOLVES, IN ORDER
==========================
1. **The local model runtime** (``qai.model_runtime``) — a scan of the models
   directory, where ``context_length`` is resolved per model from the real
   artefact: the GGUF metadata / MNN + ``config.json``
   (``context_size`` / ``context_length`` / ``max_position_embeddings``, via
   ``model_runtime.domain.context_source_chain``). This is the authoritative
   value for an on-device model and the one the model dropdown's ctx badge
   shows.
2. **The cloud models catalog** (``qai.model_catalog``) — the configured
   ``context_length`` for a registered endpoint. This is the same source
   ``GET /api/chat/context`` uses for the composer badge, so the badge and the
   compaction trigger agree by construction.
3. **Miss** -> ``None``, and the caller falls back to ``get_context_limit``.
   Returning ``None`` (rather than a guess) is deliberate: the fallback lives
   at the call site, so a resolver miss is byte-for-byte the old behaviour.

A model reached through a "cloud" provider route can still be a LOCAL
artefact (Glymur is exactly that: a loopback ``base_url``, registered as a
provider endpoint). That is why both sources are consulted for every lookup
regardless of how the model is routed, and why the local runtime is consulted
FIRST — for such a model the runtime knows the real artefact window while the
catalog may carry only a copied-in or absent number.

LAYERING
========
``qai.chat`` must not import ``qai.model_catalog`` or ``qai.model_runtime``.
This module lives in the ``apps/api`` composition root, which is the one place
allowed to see every context, and is injected into
:class:`~qai.chat.application.use_cases.streaming.StreamChatUseCase` as a
plain ``async (str | None) -> int | None`` callable. The use case therefore
depends on a function type, not on either context.

CACHING
=======
Both sources are I/O (a directory scan; a config read). The compaction trigger
runs on the hot send path, several times per turn, so every lookup is served
from a small TTL cache (:data:`_CACHE_TTL_SECONDS`). A user editing a model's
configured window sees it take effect within the TTL without a restart, which
is the same freshness contract the rest of the forge-config readers offer.
Failures are cached as negatives too — a broken catalog must not turn into a
directory scan per compaction check.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from qai.platform.logging import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Awaitable, Callable

    from .di import Container

__all__ = ["make_context_window_resolver"]

_log = get_logger(__name__)

#: How long a resolved (or missed) window is reused before re-reading the
#: sources. Long enough that a multi-round turn does ~one lookup, short enough
#: that a Settings edit lands without a restart.
_CACHE_TTL_SECONDS: float = 30.0

#: Upper sanity bound on a configured window. A nonsense value (a byte count
#: pasted into a token field, a negative sentinel) must not be trusted over
#: the family-table guess — we would rather compact late than never compact
#: again. 4M tokens is comfortably above any real 2026 model.
_MAX_PLAUSIBLE_CONTEXT_LENGTH: int = 4_000_000

#: Lower sanity bound. Below this the "window" cannot hold a system prompt plus
#: one tool result, so it is far more likely a unit mix-up (e.g. thousands of
#: tokens recorded as "32" for 32K) than a real configuration.
_MIN_PLAUSIBLE_CONTEXT_LENGTH: int = 1024


def _normalize_model_key(model_hint: str | None) -> str:
    """Canonical lookup key for *model_hint*.

    Strips the ``local::`` routing prefix (a routing detail, not part of the
    model's registered name) and lowercases, because the two catalogs are
    populated from user-typed directory / config names whose casing does not
    reliably match the hint.
    """
    return (model_hint or "").removeprefix("local::").strip().casefold()


def _usable_length(raw: Any) -> int | None:
    """Return *raw* as a plausible token count, or ``None``.

    Rejects non-ints (including ``bool``, which ``isinstance(x, int)`` would
    otherwise accept), zero / negative sentinels, and values outside the
    plausibility band. A rejected value degrades to the caller's fallback
    rather than propagating a bad window into the compaction maths.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    if not (_MIN_PLAUSIBLE_CONTEXT_LENGTH <= raw <= _MAX_PLAUSIBLE_CONTEXT_LENGTH):
        return None
    return raw


async def _from_local_runtime(
    container: Container, key: str
) -> int | None:
    """Real window from the on-device model scan, or ``None``.

    Matches on the model's directory name, which is what a ``local::`` hint
    carries and what a locally-registered provider endpoint is conventionally
    named after. ``models_root=None`` uses the adapter's default
    ``<data>/models`` root — the same call
    ``GET /api/service/models`` makes.
    """
    runtime = getattr(container, "model_runtime", None)
    list_uc = getattr(runtime, "list_models_use_case", None)
    if list_uc is None:
        return None
    try:
        models = await list_uc.execute(models_root=None)
    except Exception as exc:  # noqa: BLE001 — a scan failure must not break chat
        _log.debug("chat.context_window.local_scan_failed", error=str(exc))
        return None
    for m in models or ():
        name = getattr(m, "name", None)
        if not isinstance(name, str) or name.strip().casefold() != key:
            continue
        return _usable_length(getattr(m, "context_length", None))
    return None


async def _from_cloud_catalog(
    container: Container, key: str
) -> int | None:
    """Configured window from the cloud-models catalog, or ``None``.

    Mirrors the ``/api/chat/context`` badge resolver: scan catalog entries for
    a matching ``model_id`` and take the first usable ``context_length``. No
    provider disambiguation here — the compaction trigger only ever knows the
    ``model_hint``, and two same-named endpoints with DIFFERENT windows would
    make any choice a guess. Taking the first usable value keeps this aligned
    with the badge's own unfiltered (legacy) behaviour, so badge and trigger
    never disagree.
    """
    catalog = getattr(container, "model_catalog", None)
    list_uc = getattr(catalog, "list_cloud_models_use_case", None)
    if list_uc is None:
        return None
    try:
        entries = await list_uc.execute()
    except Exception as exc:  # noqa: BLE001 — catalog read is best-effort
        _log.debug("chat.context_window.catalog_read_failed", error=str(exc))
        return None
    for entry in entries or ():
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("model_id")
        if not isinstance(model_id, str) or model_id.strip().casefold() != key:
            continue
        usable = _usable_length(entry.get("context_length"))
        if usable is not None:
            return usable
    return None


def make_context_window_resolver(
    container: Container,
) -> Callable[[str | None], Awaitable[int | None]]:
    """Build the async ``model_hint -> real context window`` resolver.

    Returns ``None`` for any model neither source knows (or whose configured
    value is implausible), which the caller reads as "no better information
    than the family table" and falls back to ``get_context_limit``.

    The returned callable never raises: a resolution failure is logged at debug
    and reported as a miss. Compaction is a best-effort safety mechanism and
    must not be able to fail a turn.
    """
    # key -> (resolved_or_None, monotonic_expiry)
    cache: dict[str, tuple[int | None, float]] = {}

    async def _resolve(model_hint: str | None) -> int | None:
        key = _normalize_model_key(model_hint)
        if not key:
            return None
        now = time.monotonic()
        hit = cache.get(key)
        if hit is not None and hit[1] > now:
            return hit[0]

        resolved: int | None = None
        try:
            # Local runtime first — see the module docstring: a loopback-routed
            # model is physically local, and the artefact's own metadata beats
            # whatever was copied into a catalog entry.
            resolved = await _from_local_runtime(container, key)
            if resolved is None:
                resolved = await _from_cloud_catalog(container, key)
        except Exception as exc:  # noqa: BLE001 — never break the send path
            _log.debug(
                "chat.context_window.resolve_failed",
                model_hint=model_hint,
                error=str(exc),
            )
            resolved = None

        cache[key] = (resolved, now + _CACHE_TTL_SECONDS)
        if resolved is not None:
            _log.info(
                "chat.context_window.resolved",
                model_hint=model_hint,
                context_length=resolved,
            )
        return resolved

    return _resolve
