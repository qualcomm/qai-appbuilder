# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Outbound ``User-Agent`` for QGenie's dual-bucket quota metering.

QGenie bills every request to one of two INDEPENDENT daily allowances and picks
which one purely from the outbound ``User-Agent``: a UA containing
``claude-cli`` lands in its ``UI`` (IDE/CLI) bucket, anything else in ``Api``
(API/SDK). Verified live over 19 A/B request pairs against ``/v1/usage/cost``
counters — host, endpoint, auth scheme and the ``anthropic-*`` headers make no
difference.

That makes this header the ONLY lever that moves a request between the two
allowances, so it has exactly ONE home. It lives here rather than in the
streaming adapter because the streaming path is not the only caller: title
generation, prompt enhancement and smart approval each build their own headers
and post to the SAME resolved endpoint. Left without a UA they all silently
land in ``Api``, so the gauge would claim ``IDE/CLI`` was in use while a share
of the account's spend accrued to the other bucket — and the user's switch
decision would rest on incomplete numbers.

Lives in the platform shared kernel because the callers span bounded contexts
(``qai.chat`` for streaming / title / enhance, ``qai.security`` for smart
approval) and the import-linter contract only permits ``qai.** ->
qai.platform.**`` — a helper in ``qai.chat`` could not be reached from
``qai.security`` without breaking context isolation.

The host gate lives here too, so the module is self-contained: it answers "is
this endpoint metered, and if so which bucket" without reaching back into any
context.
"""

from __future__ import annotations

from typing import Final
from urllib.parse import urlsplit

__all__ = [
    "DEFAULT_TRAFFIC_CLASS",
    "PRODUCT_UA",
    "TRAFFIC_CLASS_VALUES",
    "apply_qgenie_user_agent",
    "configured_traffic_class",
    "is_qgenie_host",
    "quota_user_agent",
    "with_quota_user_agent",
]

#: Product token identifying this app in outbound ``User-Agent`` headers.
#:
#: Deliberately version-free: ``pyproject`` carries a placeholder version, and a
#: wrong version in a UA is worse than none.
PRODUCT_UA: Final[str] = "QAIModelBuilder"

#: Marker QGenie greps for to bill a request to its ``UI`` (IDE/CLI) bucket.
#:
#: Externally imposed, not a name we chose, and the exact spelling is
#: load-bearing: a bare ``claude`` does not match, and ``claude_code`` with an
#: underscore does not match. Appended AFTER the product token so upstream logs
#: still show which app called — substring matching lets both coexist, so this
#: earns the bucket without impersonating Claude Code outright.
_IDE_UA: Final[str] = f"{PRODUCT_UA} claude-cli/2.0.14"

#: The two wire values QGenie's ``RateLimitType`` schema uses.
TRAFFIC_CLASS_VALUES: Final[tuple[str, str]] = ("Api", "UI")


def quota_user_agent(traffic_class: str | None) -> str | None:
    """Map a traffic class onto the UA that lands a request in it.

    ``None`` (and any unrecognised value) means "send no ``User-Agent``",
    preserving the pre-existing behaviour for every provider that does not meter
    by traffic class. Never guesses a bucket from a malformed value: billing the
    wrong side silently is worse than not overriding at all.
    """
    if traffic_class == "UI":
        return _IDE_UA
    if traffic_class == "Api":
        return PRODUCT_UA
    return None


def with_quota_user_agent(
    headers: dict[str, str], traffic_class: str | None
) -> dict[str, str]:
    """Return ``headers`` plus the UA for ``traffic_class``, if any.

    Mutates and returns the given dict so callers can keep their existing
    "build a dict then post it" shape. A no-op when there is no class, which is
    what keeps the non-QGenie paths unchanged.
    """
    user_agent = quota_user_agent(traffic_class)
    if user_agent is not None:
        headers["User-Agent"] = user_agent
    return headers


def apply_qgenie_user_agent(
    headers: dict[str, str],
    base_url: str | None,
    model_params: dict[str, object] | None,
) -> dict[str, str]:
    """Set the metering UA on ``headers`` when this is a QGenie endpoint.

    The one-call form for the side channels (title generation, prompt
    enhancement, smart approval): they each build a small header dict and post
    to whatever endpoint the model resolver handed them, so they need the host
    gate, the stored preference and the header write in a single step.

    Reads the bucket from the resolved model's own catalog entry
    (``params.traffic_class``) — the same per-model channel the streaming path
    uses — so a side-channel call is billed to the side the user chose for that
    model. Falls back to the default bucket on a QGenie host with no stored
    preference, matching the streaming path exactly; a title request must not
    land in a different bucket than the conversation it titles.

    A no-op (headers untouched) for every non-QGenie endpoint, which keeps the
    outbound headers of qai-service, a user's own gateway and local models
    byte-for-byte unchanged.
    """
    if not base_url or not is_qgenie_host(base_url):
        return headers
    return with_quota_user_agent(
        headers, configured_traffic_class(model_params) or DEFAULT_TRAFFIC_CLASS
    )


#: Domain every QGenie gateway lives under.
_QGENIE_DOMAIN: Final[str] = ".qualcomm.com"

#: First-label prefixes identifying a QGenie gateway inside that domain.
#: All three known hosts (``qgenie-chat`` / ``qgenie-api`` / ``qpilot-api``)
#: were verified to behave identically.
_QGENIE_HOST_PREFIXES: Final[tuple[str, str]] = ("qgenie", "qpilot")

#: Bucket used when no preference is stored.
#:
#: The IDE/CLI side measured more generous on the account this was developed
#: against (some models several times the daily tokens, others equal), so
#: starting there makes hitting an exhaustion prompt less likely. Allowances are
#: per-USER, so this is a heuristic default, not a guarantee.
DEFAULT_TRAFFIC_CLASS: Final[str] = "UI"


def is_qgenie_host(base_url: str) -> bool:
    """Whether ``base_url``'s host is a QGenie gateway.

    Parses the host instead of pattern-matching the raw URL. A substring or
    unanchored-regex test over the whole string accepts an attacker- or
    proxy-controlled host that merely CONTAINS the gateway name —
    ``https://fake-qpilot-api.qualcomm.com.evil.com`` and
    ``https://evil.com/?x=qgenie.qualcomm.com`` both slip through such a test,
    which would send our product token plus the ``claude-cli`` marker to a third
    party. Anchoring on the parsed host's trailing domain and its FIRST label
    removes that whole class.
    """
    try:
        host = urlsplit(base_url).hostname
    except ValueError:
        return False
    if not host:
        return False
    host = host.lower()
    if not host.endswith(_QGENIE_DOMAIN):
        return False
    first_label = host.split(".", 1)[0]
    return any(
        first_label == prefix or first_label.startswith(f"{prefix}-")
        for prefix in _QGENIE_HOST_PREFIXES
    )


def configured_traffic_class(
    model_params: dict[str, object] | None,
) -> str | None:
    """Read the persisted bucket preference off a model's catalog params.

    Shape: ``{"traffic_class": "Api" | "UI"}`` inside the model entry's
    ``params``. Rides the existing per-model params channel rather than a new
    settings key, so the choice survives a restart through the provider config
    the user already edits.

    Anything unrecognised yields ``None`` (caller falls back to the default)
    rather than a guess, so a hand-edited config cannot silently bill the wrong
    side.
    """
    if not isinstance(model_params, dict):
        return None
    raw = model_params.get("traffic_class")
    return raw if raw in TRAFFIC_CLASS_VALUES else None
