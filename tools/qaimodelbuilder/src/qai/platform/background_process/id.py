# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Background-process ID generation + schema validation.

A background-process ID has the form ``"bgp_" + <26-char ULID>`` for a
total length of **30 characters**. The ``"bgp_"`` prefix gives the ID a
visually distinct namespace (vs. plain ULIDs / UUIDs used elsewhere in
the platform) so log lines, sidebar entries, and tool arguments are
self-identifying.

Design references:

* ``docs/90-refactor/background-process-design.md`` section 4 ("ID
  Format"): canonical shape is ``bgp_<26-char ulid>``, total 30 chars,
  schema validation MUST be lenient.
* The runtime generates IDs as ``"bgp_" + ulid()``.
* The LLM-tool schema uses ``startsWith("bgp")`` (NOT
  ``"bgp_"``). This is deliberate -- the integration test suite
  passes synthetic IDs like ``"bgp01"`` through the tool layer, and the
  schema MUST accept them. We keep this lenient behaviour in
  :func:`is_bgp_id` so cross-implementation parity is preserved.
* :func:`qai.platform.ids.new_ulid`: the underlying 26-char Crockford
  base32 generator (ULID, naturally time-sortable). Re-used verbatim --
  we add only the namespace prefix, never re-implement entropy.

Why the validator is lenient (not strict ULID check):

The strict variant ``startswith("bgp_") and is_valid_ulid(s[4:])`` would
break the parity test-double pattern AND make the validator more
expensive on the hot path (the ID flows through every tool-call
arguments boundary). The cheap ``isinstance + startswith("bgp")`` check
is exactly the wire contract; production IDs from :func:`new_bgp_id`
are well-formed by construction and need no extra verification.

Aligned with the same lenient check already used in
:class:`qai.platform.background_process.ports.Info.__post_init__`
(``ports.py:198``) -- both validators agree on the wire format.
"""

from __future__ import annotations

from qai.platform.ids import new_ulid

__all__ = [
    "BGP_ID_PREFIX",
    "is_bgp_id",
    "new_bgp_id",
]


BGP_ID_PREFIX: str = "bgp_"
"""Namespace prefix used by :func:`new_bgp_id`.

Note the trailing underscore is part of the *generation* prefix but NOT
the *validation* prefix -- see module docstring for the rationale (the
schema is ``startsWith("bgp")``, no underscore).
"""


def new_bgp_id() -> str:
    """Return a fresh background-process ID.

    Shape: ``"bgp_" + new_ulid()`` -> 30 characters total
    (``4`` prefix + ``26`` Crockford base32 ULID body). The ULID body
    is naturally time-sortable (lexicographic order == creation order),
    so a sequence of IDs from this function sorts the same way as the
    underlying processes were spawned -- useful for stable sidebar
    ordering without an extra ``time.started`` lookup.

    Returns:
        A new ID string. Never empty; never reused (entropy from
        :func:`secrets.token_bytes` via :func:`qai.platform.ids.new_ulid`).
    """
    return BGP_ID_PREFIX + new_ulid()


def is_bgp_id(value: object) -> bool:
    """Return ``True`` iff ``value`` is a syntactically acceptable BGP ID.

    Lenient check -- mirrors the wire schema
    ``startsWith("bgp")``. Accepts any ``str`` starting
    with ``"bgp"`` (no underscore required, no length / charset check).

    Rationale: cross-implementation parity. The test suite feeds
    synthetic IDs like ``"bgp01"`` through the same code paths and the
    schema MUST accept them; a stricter Python-side check would diverge
    from the documented contract.

    Examples:

    >>> is_bgp_id("bgp_01J9X0YK8VTGM5R3DZ4EHWQP12")  # canonical 30-char
    True
    >>> is_bgp_id("bgp01")  # synthetic test fixture shape
    True
    >>> is_bgp_id("bgp")  # bare prefix
    True
    >>> is_bgp_id("")
    False
    >>> is_bgp_id("foo")
    False
    >>> is_bgp_id(None)
    False
    >>> is_bgp_id(123)
    False
    """
    return isinstance(value, str) and value.startswith("bgp")
