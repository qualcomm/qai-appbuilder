# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Central error-code → retry-disposition mapping (single source of truth).

This module is the ONE place that decides, from a stream ERROR frame's
stable string ``code``, how the turn should treat the failure for
**automatic** retry purposes. It lives in the domain layer because it is
pure (no I/O, no httpx, no provider knowledge) and is consumed by BOTH the
infrastructure adapter (``llm_stream.py`` — stamps ``retry_disposition``
onto the wire frame) and the application layer (``_streaming_helpers`` /
the retry loops — map a code to a :class:`RetryCategory`).

Design notes (this app is a single-user local desktop app, one active chat
turn per tab, global ``ssl_verify`` — there is deliberately NO per-provider
TLS and NO provider-adapter layer, so classification is by code, not by
provider):

* ``retry_disposition`` is a coarse, frontend-facing hint describing the
  *shape* of the wait/action the user should expect. It is additive on the
  wire (tail-only field, §3.1) and complements — never replaces — the
  existing ``retryable`` bool.
* The finer ``RetryCategory`` (application layer) is derived from the same
  codes; keeping both derivations table-driven here guarantees they never
  drift apart.

Disposition values (wire strings):

* ``"never"``          — deterministic, non-transient. No auto-retry; the
  frontend shows an actionable bubble (fix config / model / cert, etc.).
  TLS-untrusted is "never auto-retry" here even though the user CAN fix the
  trust and retry — the user action is a frontend concern; automation must
  not loop on it.
* ``"bounded_fast"``   — likely-transient connectivity fault that either
  recovers quickly or is permanent (DNS / connection-refused / host
  unreachable). A few fast attempts then terminal.
* ``"network_wait"``   — a transient network fault that may take a while to
  self-heal (connect/timeout/read). Escalating backoff, now capped by a
  wall-clock budget (no longer infinite).
* ``"bounded_server"`` — upstream 5xx: a few attempts with jitter, then
  terminal.
* ``"server_time"``    — throttling (429/503): honour the server-advised
  ``Retry-After`` with bounded attempts.
* ``"after_user_action"`` — reserved for codes the user must act on before a
  retry makes sense; currently unused for auto-retry (TLS-untrusted maps to
  ``never`` for automation — see above). Kept in the vocabulary so the
  frontend contract is stable.
"""

from __future__ import annotations

# ── wire-string disposition constants ────────────────────────────────────
DISPOSITION_NEVER = "never"
DISPOSITION_BOUNDED_FAST = "bounded_fast"
DISPOSITION_NETWORK_WAIT = "network_wait"
DISPOSITION_BOUNDED_SERVER = "bounded_server"
DISPOSITION_SERVER_TIME = "server_time"
DISPOSITION_AFTER_USER_ACTION = "after_user_action"

#: Every code the backend can emit, mapped to its auto-retry disposition.
#: This is the authoritative table; ``retry_disposition_for`` reads it and
#: the application layer derives ``RetryCategory`` from the SAME codes.
_CODE_TO_DISPOSITION: dict[str, str] = {
    # ── never (deterministic / non-transient — user must fix something) ──
    # TLS trust / cert problems (global ssl_verify; verify=True by default).
    "chat.llm.tls_cert_untrusted": DISPOSITION_NEVER,
    "chat.llm.tls_hostname_mismatch": DISPOSITION_NEVER,
    "chat.llm.tls_cert_expired": DISPOSITION_NEVER,
    "chat.llm.tls_handshake_failed": DISPOSITION_NEVER,
    # Auth / authorization / model / content / param / config.
    "chat.llm.auth_failed": DISPOSITION_NEVER,
    "chat.llm.permission_denied": DISPOSITION_NEVER,
    "chat.llm.model_unavailable": DISPOSITION_NEVER,
    "chat.llm.content_filtered": DISPOSITION_NEVER,
    "chat.llm.unsupported_param": DISPOSITION_NEVER,
    "chat.llm.provider_api_key_missing": DISPOSITION_NEVER,
    "chat.llm.invalid_base_url": DISPOSITION_NEVER,
    "chat.llm.unexpected_error": DISPOSITION_NEVER,
    "chat.llm.protocol_error": DISPOSITION_NEVER,
    # network_error is retryable=False → terminal (was double-bug: retried
    # infinitely despite retryable=False). Now deterministically terminal.
    "chat.llm.network_error": DISPOSITION_NEVER,
    # ── bounded_fast (connectivity fault: recover fast or permanent) ──
    "chat.llm.dns_error": DISPOSITION_BOUNDED_FAST,
    "chat.llm.connection_refused": DISPOSITION_BOUNDED_FAST,
    "chat.llm.host_unreachable": DISPOSITION_BOUNDED_FAST,
    # ── network_wait (transient, may take a while — budget-capped) ──
    "chat.llm.connect_error": DISPOSITION_NETWORK_WAIT,
    "chat.llm.timeout": DISPOSITION_NETWORK_WAIT,
    "chat.llm.read_error": DISPOSITION_NETWORK_WAIT,
    # ── bounded_server (upstream 5xx) ──
    "chat.llm.server_error": DISPOSITION_BOUNDED_SERVER,
    # ── server_time (throttling / rate-limit) ──
    "throttling": DISPOSITION_SERVER_TIME,
    # prompt_too_long keeps its own bounded(1)+compress behaviour; it is not
    # a "wait" disposition. Report it as ``after_user_action`` (the app
    # compresses automatically, but from the user's POV the actionable state
    # is "the prompt was too long"). Auto-retry is handled by the existing
    # PROMPT_TOO_LONG category, unchanged.
    "prompt_too_long": DISPOSITION_AFTER_USER_ACTION,
}

#: Fallback disposition for any code not in the table (e.g. legacy
#: ``chat.llm.http_error``): treat as terminal so we never accidentally
#: auto-loop on an unclassified failure.
_DEFAULT_DISPOSITION = DISPOSITION_NEVER


def retry_disposition_for(code: str | None) -> str:
    """Return the frontend-facing ``retry_disposition`` for an error code.

    Unknown / missing codes default to :data:`DISPOSITION_NEVER` (terminal)
    so automation is fail-safe: an unclassified error never triggers an
    infinite/unexpected retry loop.
    """
    if not code:
        return _DEFAULT_DISPOSITION
    return _CODE_TO_DISPOSITION.get(code, _DEFAULT_DISPOSITION)


__all__ = [
    "DISPOSITION_NEVER",
    "DISPOSITION_BOUNDED_FAST",
    "DISPOSITION_NETWORK_WAIT",
    "DISPOSITION_BOUNDED_SERVER",
    "DISPOSITION_SERVER_TIME",
    "DISPOSITION_AFTER_USER_ACTION",
    "retry_disposition_for",
]
