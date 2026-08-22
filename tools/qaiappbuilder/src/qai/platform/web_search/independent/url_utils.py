# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""URL utilities shared by engines and the aggregator.

Two jobs:

* :func:`dedup_key` — a canonical key so the aggregator recognises the same
  page returned by different engines (host case-folded and ``www.``-stripped,
  path trailing-slash-stripped, query preserved and sorted, fragment dropped).
* :func:`unwrap_redirect` — several engines wrap result links in their own
  redirect endpoint (``?uddg=`` / ``&u=a1<b64>`` / ``RU=<pct>``); this recovers
  the underlying target. Callers pass the raw href; a URL that is not a known
  wrapper is returned unchanged.
"""

from __future__ import annotations

import base64
import binascii
from urllib.parse import parse_qs, parse_qsl, unquote, urlencode, urlsplit

__all__ = ["dedup_key", "is_http_url", "unwrap_redirect"]


def is_http_url(url: str) -> bool:
    """True when ``url`` is an absolute http(s) URL with a host."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def dedup_key(url: str) -> str:
    """Return a canonical dedup key for ``url``.

    Same page from different engines collapses to one key: host lower-cased and
    ``www.`` stripped, path with any trailing slash removed, query params sorted
    (order-independent), fragment dropped. Non-parseable input returns the input
    lower-cased as a last-resort key.
    """
    try:
        parts = urlsplit(url)
    except ValueError:
        return url.lower()
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/")
    query = urlencode(sorted(parse_qsl(parts.query, keep_blank_values=True)))
    key = f"{host}{path}"
    if query:
        key = f"{key}?{query}"
    return key


def _decode_b64url_maybe(value: str) -> str | None:
    """Decode a base64url token to UTF-8 text, or ``None`` if it is not one."""
    pad = "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(value + pad)
    except (binascii.Error, ValueError):
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


def unwrap_redirect(href: str) -> str:
    """Recover the target URL from a known engine redirect wrapper.

    Handles the common wrappers observed in practice:

    * ``//duckduckgo.com/l/?uddg=<pct-encoded-url>`` — percent-decode ``uddg``.
    * ``…/ck/a?…&u=a1<base64url>`` — strip the ``a1`` prefix, base64url-decode.
    * ``…/RU=<pct-encoded>/RK=…`` — percent-decode the ``RU`` segment.

    A URL that is not a recognised wrapper (or whose payload does not decode to
    an http(s) URL) is returned unchanged.
    """
    try:
        parts = urlsplit(href if "//" in href[:8] else f"https:{href}"
                         if href.startswith("//") else href)
    except ValueError:
        return href

    qs = parse_qs(parts.query, keep_blank_values=True)

    # DuckDuckGo: ?uddg=<pct-encoded url>
    uddg = qs.get("uddg", [None])[0]
    if uddg:
        target = unquote(uddg)
        if is_http_url(target):
            return target

    # Bing: &u=a1<base64url>
    u = qs.get("u", [None])[0]
    if u and u.startswith("a1"):
        decoded = _decode_b64url_maybe(u[2:])
        if decoded and is_http_url(decoded):
            return decoded

    # Yahoo: /RU=<pct-encoded>/RK=...
    path = parts.path
    marker = "/RU="
    if marker in path:
        segment = path.split(marker, 1)[1].split("/", 1)[0]
        target = unquote(segment)
        if is_http_url(target):
            return target

    return href
