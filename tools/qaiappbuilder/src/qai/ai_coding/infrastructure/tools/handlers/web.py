# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""URL-fetch tool handler (``web_fetch``).

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

from qai.ai_coding.infrastructure.tools.errors import ToolError
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    WEB_FETCH_DEFAULT_MAX_CHARS,
    _ok,
    make_web_fetch_advice,
)
from qai.platform.config.settings import LOOPBACK_HOST_NAME
from qai.platform.logging import get_logger

_log = get_logger(__name__)

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


class _WebFetchConfig:
    __slots__ = ("global_proxy", "ssl_verify")

    def __init__(self) -> None:
        self.ssl_verify: bool = True
        self.global_proxy: str | None = None


_CONFIG = _WebFetchConfig()


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


class _FetchRefusedError(Exception):
    """Internal: the plain HTTP fetch was refused.

    Carries the HTTP ``status`` (``None`` for a transport-level failure) so
    :func:`tool_web_fetch` can decide whether a headless retry is warranted.
    Never escapes this module — it is converted to a ``ToolError`` once the
    browser fallback has also been ruled out.
    """

    def __init__(self, status: int | None, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _parse_fetch_args(args: dict[str, Any]) -> tuple[str, str, int, float]:
    """Validate and normalise the tool arguments.

    Split out of :func:`tool_web_fetch` so the handler body stays readable as
    "fetch → extract → gate → return" rather than burying that flow under
    argument coercion.

    Returns ``(url, extract_mode, max_chars, timeout)``.
    """
    url = args.get("url") or ""
    if not isinstance(url, str) or not url:
        raise ToolError("web_fetch: 'url' argument is required")
    if not url.startswith(("http://", "https://")):
        raise ToolError("web_fetch: 'url' must start with http:// or https://")
    extract_mode = (args.get("extractMode") or "markdown").lower()
    if extract_mode not in ("markdown", "text"):
        extract_mode = "markdown"
    max_chars_raw = args.get("maxChars")
    max_chars = (
        int(max_chars_raw)
        if max_chars_raw is not None
        else WEB_FETCH_DEFAULT_MAX_CHARS
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
        timeout = (
            _DEFAULT_TIMEOUT_SECONDS
            if requested <= 0
            else min(requested, _MAX_TIMEOUT_SECONDS)
        )
    return url, extract_mode, max_chars, timeout


async def tool_web_fetch(
    args: dict[str, Any],
) -> dict[str, Any]:
    # ``file_guard`` used to be a parameter for signature parity with the
    # file-touching tool handlers; ``web_fetch`` performs no filesystem access
    # and the harness enforces URL policy at its own layer, so the param was
    # always dropped (``_ = file_guard``). Removed (L-Py-5, 2026-07-28) — the
    # tool wiring no longer forwards it.
    url, extract_mode, max_chars, timeout = _parse_fetch_args(args)

    # M10: emit a keep-alive so the UI's tool card shows "fetching …"
    # instead of a silent freeze during the network round-trip + PDF/DOCX/
    # XLSX conversion (multi-second on large docs).
    from qai.platform.tool_progress import emit_progress
    _short_url = url if len(url) <= 60 else url[:57] + "…"
    emit_progress(f"fetching {_short_url}…", "network")

    try:
        html_text, content_type = await _fetch_and_decode(url, timeout=timeout)
    except _FetchRefusedError as refused:
        # A bot wall refused the plain request (measured: CSDN 521, Zhihu 403)
        # or cut the connection outright. The reader service fetches from its own
        # infrastructure, so it gets past walls keyed to this egress.
        html_text, content_type = await _reader_retry(url, refused, timeout=timeout)

    def _looks_like_html(text: str, ctype: str) -> bool:
        return (
            "<html" in text[:2000].lower()
            or "<!doctype" in text[:200].lower()
            or "text/html" in ctype.lower()
        )

    def _extract(text: str, ctype: str) -> str:
        if not _looks_like_html(text, ctype):
            # Plain text / markdown / JSON / XML — return as-is.
            return text
        return _html_to_readable(text, extract_mode)

    content = await asyncio.to_thread(_extract, html_text, content_type)

    # An HTML 200 can still carry a JS gate or a bare navigation shell; both
    # extract to something that *looks* like content. Escalate on the text, not
    # the status — handing the model a "please enable JavaScript" page as the
    # answer is the failure this gate exists to prevent.
    #
    # Scoped to HTML on purpose. A short NON-HTML body is usually exactly right
    # (a JSON status, a plain-text token, a tiny .txt file); judging it by prose
    # heuristics would send perfectly good responses through a needless retry.
    #
    # The local text is also kept as the floor: the fetch itself succeeded, so a
    # reader that is unreachable or blocked must not turn a mediocre result into
    # no result at all.
    if _looks_like_html(html_text, content_type) and _is_low_quality(content):
        try:
            retry_text, retry_type = await _reader_retry(url, None, timeout=timeout)
            retry_content = await asyncio.to_thread(_extract, retry_text, retry_type)
        except ToolError as exc:
            _log.info(
                "web_fetch.quality_retry_failed",
                url=url[:200],
                reason=str(exc)[:160],
            )
        else:
            if not _is_low_quality(retry_content):
                content, content_type = retry_content, retry_type

    # Report the page's TRUE size, not just the cap. Saying only "truncated at
    # 20000 chars" left the model unable to tell whether it held 99% of the page
    # or 10% of it — the same defect as ``read``'s old "of total <lines that
    # fit>" and ``grep``/``glob``'s capped counts. ``total_chars`` is free (we
    # already have the whole string in hand), so it is always stated.
    #
    # The footer MUST go in ``content``: the chat render layer
    # (``_chat_tool_result_render.render_tool_result_text``) returns ``content``
    # verbatim whenever it is non-empty and never reads ``message``, so a size
    # placed only in ``message`` is invisible to the model. A live session caught
    # exactly this — a complete fetch printed nothing, and the model had to
    # reason "no truncation warning, therefore complete", which it correctly
    # called out as inferring from silence ("absence of evidence is not evidence
    # of absence"). Completeness is now stated POSITIVELY, matching what
    # ``read`` / ``grep`` / ``skill`` already do.
    total_chars = len(content)
    truncated = total_chars > max_chars
    if truncated:
        omitted = total_chars - max_chars
        content = (
            content[:max_chars]
            + f"\n\n...[content truncated: showing the first {max_chars} of "
            f"{total_chars} chars ({omitted} omitted) — this is NOT the whole "
            f"page] "
            + make_web_fetch_advice(max_chars)
        )
    else:
        content = (
            content
            + f"\n\n...[content complete: whole page, {total_chars} chars]"
        )

    shown = min(total_chars, max_chars)
    return _ok(
        f"web_fetch ok ({shown} of {total_chars} chars, {extract_mode} mode)",
        url=url,
        content=content,
        content_type=content_type,
        truncated=truncated,
        total_chars=total_chars,
        extract_mode=extract_mode,
    )


#: Statuses worth retrying through the reader service. A bot wall refuses the
#: request itself, which a different fetcher can get past; a 404 means the page
#: is genuinely gone, so retrying only adds latency.
#:
#: Measured against live targets: CSDN answers 521, Zhihu 403.
_READER_RETRY_STATUSES: frozenset[int] = frozenset(
    {401, 403, 405, 429, 500, 502, 503, 520, 521, 522, 526}
)

#: Reader service used as the last resort. It renders the page on its own
#: infrastructure and answers with markdown, so this stays one ordinary HTTP
#: request — no browser process and no extra dependency here.
_READER_ENDPOINT = "https://r.jina.ai/"

#: Ceiling on one reader-service call. It renders remotely, so it is slower than
#: a plain fetch, but must not be able to hold the caller's turn open.
_READER_TIMEOUT_SECONDS = 45.0

#: Below this, extracted text is treated as "nothing usable arrived".
_MIN_USABLE_CHARS = 100

#: A body this small that also names a JS requirement is a gate, not an article.
_JS_GATE_MAX_CHARS = 1024

#: Phrases a JS-gated shell uses to explain itself.
_JS_GATE_MARKERS: tuple[str, ...] = (
    "enable javascript",
    "javascript required",
    "turn on javascript",
    "please enable javascript",
    "browser not supported",
    "请开启 javascript",
    "请启用 javascript",
)

#: A page whose lines are overwhelmingly short is chrome (menus, link lists),
#: not prose. Tuned on the reference implementation's measured threshold.
_NAV_SHELL_MIN_LINES = 10
_NAV_SHELL_SHORT_LINE_CHARS = 40
_NAV_SHELL_SHORT_LINE_RATIO = 0.7


def _is_low_quality(content: str) -> bool:
    """Whether extracted text is a shell rather than the page's actual content.

    Two failure modes that both arrive as a perfectly healthy HTTP 200, so only
    the text itself reveals them:

    * a **JS gate** — a short body whose only message is "enable JavaScript";
    * a **navigation shell** — the header/menu/link furniture rendered without
      the article, which reads as many very short lines and no prose.

    Returning either to the model is worse than failing: it looks like an answer.
    """
    stripped = content.strip()
    if len(stripped) <= _MIN_USABLE_CHARS:
        return True
    lowered = stripped.lower()
    if len(stripped) < _JS_GATE_MAX_CHARS and any(
        marker in lowered for marker in _JS_GATE_MARKERS
    ):
        return True
    lines = [line for line in stripped.splitlines() if line.strip()]
    if len(lines) > _NAV_SHELL_MIN_LINES:
        short = sum(
            1 for line in lines if len(line.strip()) < _NAV_SHELL_SHORT_LINE_CHARS
        )
        if short / len(lines) > _NAV_SHELL_SHORT_LINE_RATIO:
            return True
    return False

#: Host NAMES never sent to the reader service. It is a third party, so an
#: internal address must never leave this process: the URL itself (and any
#: credential in its query string) would be disclosed for no benefit, since the
#: service cannot reach a private address anyway.
#:
#: Literal IPs are NOT listed here — :func:`_is_public_url` judges those with
#: :mod:`ipaddress`, which covers every private / loopback / link-local range
#: without a hand-maintained prefix list.
_PRIVATE_HOST_MARKERS: tuple[str, ...] = (
    LOOPBACK_HOST_NAME,
    ".local",
    ".internal",
    ".corp",
    ".intranet",
    "qualcomm.com",
)


def _is_public_url(url: str) -> bool:
    """Whether ``url`` may be handed to the third-party reader service.

    A literal IP is judged by :mod:`ipaddress` (which knows every private,
    loopback, link-local and reserved range); a hostname is matched against
    :data:`_PRIVATE_HOST_MARKERS`. Anything unparseable is treated as private —
    failing closed keeps an odd URL from leaking to a third party.
    """
    import ipaddress  # noqa: PLC0415 — stdlib, kept local to this one check
    from urllib.parse import urlsplit  # noqa: PLC0415

    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Not a literal IP — judge the name.
        return not any(marker in host for marker in _PRIVATE_HOST_MARKERS)
    return address.is_global


async def _reader_retry(
    url: str,
    refused: _FetchRefusedError | None,
    *,
    timeout: float,
) -> tuple[str, str]:
    """Re-fetch ``url`` through the reader service after a local failure.

    ``refused`` is the plain-HTTP refusal when there was one, or ``None`` when
    the fetch succeeded but its content did not survive the quality gate.

    Returns ``(markdown, content_type)``. Raises :class:`ToolError` naming the
    real obstacle: a caller that only sees "HTTP error" cannot tell a bot wall
    from a dead link from a private address, and each needs a different response.
    """
    detail = refused.detail if refused is not None else "content was unusable"
    status = refused.status if refused is not None else None

    if status is not None and status not in _READER_RETRY_STATUSES:
        raise ToolError(f"web_fetch: HTTP error: {detail}")
    if not _is_public_url(url):
        raise ToolError(
            f"web_fetch: HTTP error: {detail} (a private / internal address is "
            "never sent to the external reader service)"
        )

    from qai.platform.tool_progress import emit_progress  # noqa: PLC0415

    emit_progress("blocked — retrying via reader service…", "network")
    try:
        import httpx  # noqa: PLC0415 — optional-dep import guard
    except ImportError as e:  # pragma: no cover — httpx is checked earlier too
        raise ToolError(f"web_fetch: httpx package is required: {e}") from e

    client_kwargs: dict[str, Any] = {
        "follow_redirects": True,
        "timeout": min(timeout, _READER_TIMEOUT_SECONDS),
        "verify": get_ssl_verify(),
        "trust_env": True,
        # NO browser User-Agent here. Measured: the reader answers 403 to a
        # request carrying a Chrome UA (it reads that as scraper traffic) and
        # 200 to the same request without one. This is the opposite of what the
        # origin sites want, which is why it must not reuse _CHROME_UA.
        "headers": {"Accept": "text/markdown"},
    }
    proxy = get_global_proxy()
    if proxy:
        client_kwargs["proxy"] = proxy

    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.get(f"{_READER_ENDPOINT}{url}")
            response.raise_for_status()
            text = response.text
    except Exception as exc:
        raise ToolError(
            f"web_fetch: HTTP error: {detail} (reader-service retry also "
            f"failed: {exc})"
        ) from exc

    if not text.strip():
        raise ToolError(
            f"web_fetch: HTTP error: {detail} (the reader service returned an "
            "empty document; this site may block automated readers entirely)"
        )
    _log.info(
        "web_fetch.reader_fallback_used",
        url=url[:200],
        original=detail[:120],
        chars=len(text),
    )
    # Already markdown — declared as such so the caller skips HTML extraction.
    return text, "text/markdown"


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
        raise ToolError(f"web_fetch: httpx package is required: {e}") from e

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
                    raise ToolError(  # noqa: TRY301 — re-raised as-is below
                        "web_fetch: response too large "
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
    except ToolError:
        # Raised by our own size guard inside the block above — it is a final
        # verdict, not a refusal a browser could get past. Must be re-raised
        # before the HTTPError arm, which would otherwise reclassify it.
        raise
    except httpx.HTTPError as e:
        # Preserve the HTTP status when the failure carries one, so the caller
        # can tell a recoverable bot wall from a dead link. A status-less
        # failure is a transport error (connection reset / TLS) — some bot walls
        # cut the connection instead of answering, so that escalates too.
        #
        # ``response`` is read defensively rather than branching on
        # ``httpx.HTTPStatusError``: only the subset of httpx this module
        # actually uses is guaranteed present (the unit tests inject a minimal
        # fake module), and an attribute probe needs no extra API surface.
        response = getattr(e, "response", None)
        status = getattr(response, "status_code", None)
        raise _FetchRefusedError(
            status if isinstance(status, int) else None, str(e)
        ) from e

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
