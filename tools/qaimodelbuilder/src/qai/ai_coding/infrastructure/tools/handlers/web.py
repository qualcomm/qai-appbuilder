# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""URL-fetch tool handler (``webfetch``).

V1 parity (``backend/tools/_webfetch.py``):

* HTML → markdown via the optional ``markdownify`` library, with a
  regex-based built-in fallback when it is not installed (import guard;
  keeps the dependency optional / cross-platform-neutral).
* Chrome User-Agent + Accept headers so sites that gate bots still serve
  readable HTML.
* HTML entity decoding (named + numeric) in the built-in extractor.
* 4 MB read cap on the response body.
* Proxy support via httpx ``trust_env`` (honours ``HTTP_PROXY`` /
  ``HTTPS_PROXY`` / ``NO_PROXY`` env vars).  Operator-configured global
  proxy injection (V1 ``proxy_helper``) is a config-layer concern wired by
  ``apps/api``; see :func:`set_global_proxy`.
* ``ssl_verify`` is read from the injectable installed by ``apps/api`` (V1
  read it from ``forge_config.ssl_verify``); defaults to ``True``.
"""

from __future__ import annotations

import asyncio
import html as _html
import re
from typing import Any

from qai.ai_coding.application.ports import FileGuardPort
from qai.ai_coding.infrastructure.tools.errors import ToolError
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    WEBFETCH_DEFAULT_MAX_CHARS,
    _ok,
    make_webfetch_advice,
)

# Cap the response body read so a malicious / runaway URL cannot exhaust
# memory.  V1 parity: ``resp.read(4 * 1024 * 1024)``.
_MAX_FETCH_BYTES = 4 * 1024 * 1024

# Request timeout (seconds). The default matches the prior hard-coded value; a
# caller may pass a larger ``timeout`` for a slow endpoint, but it is clamped to
# ``_MAX_TIMEOUT_SECONDS`` so a single fetch can never hang the turn unbounded.
_DEFAULT_TIMEOUT_SECONDS = 30.0
_MAX_TIMEOUT_SECONDS = 120.0

_CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "User-Agent": _CHROME_UA,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    ),
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


# ---------------------------------------------------------------------------
# Injectable config (7-L1 ssl_verify + 7-M2 proxy) — installed by apps/api so
# the handler stays config-source-agnostic (clean-arch: infrastructure does
# not import the security / config context).  Benign defaults keep the
# handler working unchanged when nothing is installed.  Held on a small
# mutable holder (rather than module-level ``global`` rebinding) so callers
# mutate attributes in place.
# ---------------------------------------------------------------------------


class _WebfetchConfig:
    __slots__ = ("global_proxy", "ssl_verify")

    def __init__(self) -> None:
        self.ssl_verify: bool = True
        self.global_proxy: str | None = None


_CONFIG = _WebfetchConfig()


def set_ssl_verify(value: bool) -> None:
    """Install the ``forge_config.ssl_verify`` setting (V1 parity)."""
    _CONFIG.ssl_verify = bool(value)


def get_ssl_verify() -> bool:
    return _CONFIG.ssl_verify


def set_global_proxy(url: str | None) -> None:
    """Install an operator-configured global proxy URL (V1 ``proxy_helper``).

    ``None`` resets to "no explicit proxy" (httpx still honours proxy env
    vars via ``trust_env=True``).
    """
    _CONFIG.global_proxy = url or None


def get_global_proxy() -> str | None:
    return _CONFIG.global_proxy


async def tool_webfetch(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
) -> dict[str, Any]:
    # ``file_guard`` is accepted for signature parity with the other tool
    # handlers; webfetch URL policy is enforced at the harness layer.
    _ = file_guard
    url = args.get("url") or ""
    if not isinstance(url, str) or not url:
        raise ToolError("webfetch: 'url' argument is required")
    if not url.startswith(("http://", "https://")):
        raise ToolError("webfetch: 'url' must start with http:// or https://")
    extract_mode = (args.get("extractMode") or "markdown").lower()
    if extract_mode not in ("markdown", "text"):
        extract_mode = "markdown"
    max_chars_raw = args.get("maxChars")
    max_chars = (
        int(max_chars_raw)
        if max_chars_raw is not None
        else WEBFETCH_DEFAULT_MAX_CHARS
    )
    # Optional caller timeout, clamped to (0, _MAX_TIMEOUT_SECONDS]. A
    # non-positive / missing value uses the default; anything above the ceiling
    # is capped so a single fetch can never hang the turn unbounded.
    timeout_raw = args.get("timeout")
    if timeout_raw is None:
        timeout = _DEFAULT_TIMEOUT_SECONDS
    else:
        try:
            requested = float(timeout_raw)
        except (TypeError, ValueError):
            requested = _DEFAULT_TIMEOUT_SECONDS
        if requested <= 0:
            timeout = _DEFAULT_TIMEOUT_SECONDS
        else:
            timeout = min(requested, _MAX_TIMEOUT_SECONDS)

    html_text, content_type = await _fetch_and_decode(url, timeout=timeout)

    is_html = (
        "<html" in html_text[:2000].lower()
        or "<!doctype" in html_text[:200].lower()
        or "text/html" in content_type.lower()
    )

    def _extract() -> str:
        if not is_html:
            # Plain text / JSON / XML — return as-is.
            return html_text
        return _html_to_readable(html_text, extract_mode)

    content = await asyncio.to_thread(_extract)

    truncated = len(content) > max_chars
    if truncated:
        content = (
            content[:max_chars]
            + f"\n\n...[content truncated at {max_chars} chars] "
            + make_webfetch_advice(max_chars)
        )

    return _ok(
        f"webfetch ok ({len(content)} chars, {extract_mode} mode)",
        url=url,
        content=content,
        content_type=content_type,
        truncated=truncated,
        extract_mode=extract_mode,
    )


async def _fetch_and_decode(
    url: str, *, timeout: float = _DEFAULT_TIMEOUT_SECONDS
) -> tuple[str, str]:
    """Fetch *url* (4 MB cap, ssl_verify + proxy honoured) and decode the
    body using the declared charset (utf-8 fallback).

    A declared ``Content-Length`` larger than the 4 MB cap is rejected up front
    (before downloading the body) so an oversized response is not pulled over
    the wire only to be discarded. ``timeout`` bounds the request.

    Returns ``(text, content_type)``.  Raises :class:`ToolError` on HTTP
    failure / oversized response / missing ``httpx``.
    """
    try:
        import httpx  # noqa: PLC0415 — optional-dep import guard (cross-platform-neutral)
    except ImportError as e:
        raise ToolError(f"webfetch: httpx package is required: {e}") from e

    # 7-L1: verify follows the installed ssl_verify setting (V1 forge_config).
    # 7-M2: trust_env honours proxy env vars; an explicit global proxy (when
    # configured by apps/api) takes precedence.
    client_kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "timeout": timeout,
        "verify": get_ssl_verify(),
        "trust_env": True,
        "headers": _DEFAULT_HEADERS,
    }
    proxy = get_global_proxy()
    if proxy:
        client_kwargs["proxy"] = proxy

    try:
        async with httpx.AsyncClient(**client_kwargs) as client, client.stream(
            "GET", url
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "")
            # Content-Length pre-check: reject an oversized response BEFORE
            # downloading its body (the streaming cap below is the hard backstop
            # for responses that omit / understate the header).
            declared = response.headers.get("content-length")
            if declared is not None:
                try:
                    declared_len = int(declared)
                except ValueError:
                    declared_len = -1
                if declared_len > _MAX_FETCH_BYTES:
                    raise ToolError(
                        "webfetch: response too large "
                        f"({declared_len} bytes; limit "
                        f"{_MAX_FETCH_BYTES} bytes)"
                    )
            # 7-M2: cap the body read at 4 MB.
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= _MAX_FETCH_BYTES:
                    break
            raw = b"".join(chunks)[:_MAX_FETCH_BYTES]
    except httpx.HTTPError as e:
        raise ToolError(f"webfetch: HTTP error: {e}") from e

    charset = "utf-8"
    if "charset=" in content_type:
        candidate = content_type.split("charset=")[-1].split(";")[0].strip()
        if candidate:
            charset = candidate
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        # Unknown charset label — fall back to utf-8.
        text = raw.decode("utf-8", errors="replace")
    return text, content_type


def _html_to_readable(html_text: str, mode: str) -> str:
    """Convert HTML to markdown or plain *text* (V1 ``_html_to_readable``).

    Both modes share the same pipeline — drop ``<script>`` / ``<style>`` /
    comments, strip the remaining tags, decode HTML entities (named +
    numeric) and collapse blank lines — so the **line / paragraph / list
    structure of the source HTML is preserved in both modes**.  The *only*
    difference is that ``markdown`` mode additionally rewrites common block
    elements (headings, lists, ``<br>`` / ``<p>`` …) into their markdown
    equivalents before the shared strip step; ``text`` mode skips that
    rewrite but still keeps the newlines already present in the source.

    For ``markdown`` mode the optional ``markdownify`` library is used when
    installed (high-quality conversion); otherwise the regex extractor below
    runs.  ``text`` mode always uses the built-in extractor.  The import
    guard keeps ``markdownify`` an optional, cross-platform-neutral
    dependency.
    """
    if mode == "markdown":
        try:
            import markdownify  # type: ignore[import-not-found]  # noqa: PLC0415 — optional-dep import guard (regex fallback below)

            md = markdownify.markdownify(
                html_text, heading_style="ATX", strip=["script", "style"]
            )
            return _collapse_blank_lines(md)
        except ImportError:
            pass  # fall through to the built-in extractor

    text = html_text
    # Drop scripts / styles / comments entirely.
    text = re.sub(
        r"<(script|style)[^>]*>.*?</\1>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)

    if mode == "markdown":
        # Common block elements → markdown equivalents.
        for level in range(1, 7):
            text = re.sub(
                rf"<h{level}[^>]*>(.*?)</h{level}>",
                rf"\n{'#' * level} \1\n",
                text,
                flags=re.DOTALL | re.IGNORECASE,
            )
        text = re.sub(
            r"<(strong|b)[^>]*>(.*?)</\1>",
            r"**\2**",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r"<(em|i)[^>]*>(.*?)</\1>",
            r"*\2*",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r"<code[^>]*>(.*?)</code>",
            r"`\1`",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r"<pre[^>]*>(.*?)</pre>",
            r"\n```\n\1\n```\n",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r'<a[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>',
            r"[\2](\1)",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(
            r"<li[^>]*>(.*?)</li>",
            r"\n- \1",
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )
        text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<p[^>]*>", "\n\n", text, flags=re.IGNORECASE)
        text = re.sub(r"</p>", "", text, flags=re.IGNORECASE)
        text = re.sub(r"<hr\s*/?>", "\n---\n", text, flags=re.IGNORECASE)
        text = re.sub(r"<blockquote[^>]*>", "\n> ", text, flags=re.IGNORECASE)

    # Strip remaining tags + decode entities (named + numeric).  No
    # ``\s+`` → single-space collapse here: that would flatten the whole
    # page onto one line and lose the paragraph / list structure (V1
    # ``_collapse_blank_lines`` only folds 3+ blank lines and rstrips each
    # line, preserving newlines).
    text = re.sub(r"<[^>]+>", "", text)
    text = _html.unescape(text)
    return _collapse_blank_lines(text)


def _collapse_blank_lines(text: str) -> str:
    """Collapse runs of 3+ blank lines into at most 2 and rstrip each line."""
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [line.rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()
