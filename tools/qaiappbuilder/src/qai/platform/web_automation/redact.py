# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""URL credential redaction for browser-tool outputs.

Strips a ``user:pass@`` userinfo component from any URL before it is surfaced
in a tool result / log line, so navigating to an authenticated URL never leaks
the credential back to the model or the log file. Non-URL strings pass through
unchanged.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

__all__ = ["redact_url_credentials"]


def redact_url_credentials(url: str) -> str:
    """Return ``url`` with any ``user:pass@`` userinfo replaced by ``***``.

    Best-effort: a value that does not parse as a URL with a netloc is returned
    verbatim. Only the userinfo is touched; scheme/host/port/path/query/fragment
    are preserved.
    """
    if not isinstance(url, str) or "@" not in url:
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url
    if not parts.netloc or "@" not in parts.netloc:
        return url
    userinfo, _, hostport = parts.netloc.rpartition("@")
    if not userinfo:
        return url
    redacted_netloc = f"***@{hostport}"
    return urlunsplit(
        (parts.scheme, redacted_netloc, parts.path, parts.query, parts.fragment)
    )
