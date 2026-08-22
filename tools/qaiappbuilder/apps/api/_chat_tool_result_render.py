# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Project ai_coding tool-result dicts into V1-style plain text.

Why this module exists
----------------------
The V2 ai_coding tools return **structured dicts** (built by
``handlers/_shared.py:_ok``) — e.g. ``glob`` returns
``{"ok": True, "message": "2 file(s) matched", "files": [...],
"truncated": False}``.  That dict is great for structured consumers,
but the chat agentic loop must feed the tool result back to the LLM as
the ``role:tool`` message ``content`` — and **V1 always fed the model a
plain-text string**, not a Python dict repr.

Before this module, ``streaming.py`` did ``str(raw_result)`` which
produced a Python ``repr`` like
``{'ok': True, 'message': '2 file(s) matched', 'files': ['a.py'], ...}``.
The model (and the chat UI tool card, which renders the same string)
saw single-quoted Python syntax with ``True``/``False`` literals
instead of the clean output V1 produced.  That is both a fidelity
regression vs V1 and a likely cause of the model getting confused /
ending the agentic turn early.

This renderer reproduces V1's per-tool return *text* from the V2 dict
(V1 sources):

* ``glob`` : ``<path>\n<path>\n(N file(s) matched)``
* ``grep`` : ``<output>\n\n(N match(es) in M file(s))``
* ``read`` : the file body (already in ``content``)
* ``write`` : ``Successfully wrote N bytes to PATH`` (``message``)
* ``edit`` : ``Successfully applied N edit(s) ...`` (``message``)
* ``apply_patch`` : ``<head>\n  M  path (k hunks)\n...``
* ``exec`` : ``<stdout>\n[stderr]\n...\n[exit code: N]``
* ``web_fetch`` : the extracted body (already in ``content``)

Architecture
------------
This lives in ``apps/api`` (the only layer allowed to compose two
contexts — see ``_chat_tool_bridge``).  It encodes knowledge of the
ai_coding result-dict *shape*, which is exactly the kind of
cross-context glue ``apps/api`` exists for; neither ``qai.chat`` nor
``qai.ai_coding`` learns about the other.  The renderer is **field
shape driven** (it inspects keys like ``output`` / ``stdout`` /
``files``) rather than tool-name driven, so a new tool that follows the
same ``_ok`` conventions renders sensibly without a new branch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing-only import
    from qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics import (
        HintAttribution,
    )

__all__ = ["render_tool_result_text", "render_tool_result_text_with_hints"]


# web_search snippet caps (see ``_render_web_search``):
# * source links (hit has a url) → a compact teaser; the model can ``web_fetch``
#   the url for the full text.
# * answer / passage hits (hit has NO url) → the snippet IS the payload (e.g. a
#   RAG service's generated answer), kept near-whole so it is not cut off.
_SOURCE_SNIPPET_MAX = 300
_ANSWER_SNIPPET_MAX = 4000


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else str(value)


def _render_error(result: dict[str, Any]) -> str:
    """Render an ``ok=False`` dict as a ``[tool_error]``-prefixed string.

    V1 surfaced tool failures as plain strings beginning with
    ``[tool_error]`` / ``[guardrail_blocked]``;
    the chat UI's ``isToolErrorOutput`` (frameHandlers.ts:52-56) keys off
    those prefixes to flag the card as failed.  Preserve them: if the
    message already carries a sentinel prefix, pass it through; otherwise
    prepend ``[tool_error]``.
    """
    message = _as_str(result.get("message") or result.get("error_message") or "")
    if message.startswith("[tool_error]") or message.startswith("[guardrail_blocked]"):
        return message
    code = result.get("error_code")
    if message:
        return f"[tool_error] {message}"
    if code:
        return f"[tool_error] {code}"
    return "[tool_error] tool failed"


def _render_exec(result: dict[str, Any]) -> str:
    """Reproduce V1 text assembly from the dict.

    ``<stdout.rstrip()>`` + ``\\n[stderr]\\n<stderr.rstrip()>`` (when
    stderr non-empty) + ``\\n[exit code: N]`` (when exit_code != 0).
    Empty → ``(no output)``.  ANSI stripping + truncation were already
    applied inside the V2 exec handler (``exec.py:_decode``), so the
    ``stdout`` / ``stderr`` here are clean.
    """
    stdout = _as_str(result.get("stdout", ""))
    stderr = _as_str(result.get("stderr", ""))
    exit_code = result.get("exit_code")
    timed_out = bool(result.get("timed_out"))

    parts: list[str] = []
    if stdout:
        parts.append(stdout.rstrip())
    if stderr:
        parts.append(f"[stderr]\n{stderr.rstrip()}")
    if isinstance(exit_code, int) and exit_code != 0:
        parts.append(f"[exit code: {exit_code}]")
        # V1 parity: append the targeted diagnostic hint
        # AFTER the ``[exit code: N]`` marker so the model can act on an
        # otherwise opaque non-zero exit. The handler computed it already.
        diag = _as_str(result.get("exit_diagnostics", ""))
        if diag:
            parts.append(diag.lstrip("\n"))
    body = "\n".join(parts) if parts else "(no output)"

    if timed_out:
        # exec timeout returns ok=False with a message; surface it so the
        # model knows the command was killed (V1 raised ToolError →
        # "[tool_error] exec: command timed out ...").
        msg = _as_str(result.get("message", "")) or "exec: command timed out"
        return f"[tool_error] {msg}\n{body}" if parts else f"[tool_error] {msg}"
    return body


def _render_patch(result: dict[str, Any]) -> str:
    """Reproduce V1 head + per-file detail lines.

    V2's ``message`` only carries the *head* line; V1 also appended one
    ``  {marker}  {path}{suffix}`` line per file.  Rebuild the detail
    lines from the ``files`` list so the model sees the same breakdown
    V1 produced (parity, no information loss).
    """
    head = _as_str(result.get("message", ""))
    files = result.get("files")
    if not isinstance(files, list) or not files:
        return head

    marker_for = {"add": "A", "update": "M", "delete": "D"}
    lines: list[str] = []
    for fp in files:
        if not isinstance(fp, dict):
            continue
        kind = str(fp.get("kind", ""))
        marker = marker_for.get(kind, "?")
        path = _as_str(fp.get("path", ""))
        suffix = ""
        if kind == "update":
            hunks = fp.get("applied_hunks", 0)
            try:
                hunks_int = int(hunks)
            except (TypeError, ValueError):
                hunks_int = 0
            suffix = f" ({hunks_int} hunk{'s' if hunks_int != 1 else ''}"
            if fp.get("fuzzy"):
                suffix += ", fuzzy"
            suffix += ")"
        lines.append(f"  {marker}  {path}{suffix}")

    if not lines:
        return head
    return head + "\n" + "\n".join(lines)


def _render_glob(result: dict[str, Any]) -> str:
    """Reproduce V1: ``<path>\\n...\\n(message)``.

    ``files`` is a flat list of path strings.  When empty, V1 returned
    just the ``(no files matched ...)`` message (already in ``message``).
    """
    files = result.get("files")
    message = _as_str(result.get("message", ""))
    if not isinstance(files, list) or not files:
        return message
    body = "\n".join(_as_str(p) for p in files)
    return f"{body}\n({message})" if message else body


def _render_web_search(result: dict[str, Any]) -> str:
    """Render a ``web_search`` result list as a compact numbered text block.

    Each hit becomes ``N. <title> [score]\\n   <url>\\n   <snippet>``. A hit
    WITH a url is a source link (snippet kept compact at
    ``_SOURCE_SNIPPET_MAX`` chars — the model can ``web_fetch`` the url for the
    full text). A hit WITHOUT a url is a self-contained answer / passage (e.g.
    a RAG service's generated answer) whose snippet IS the payload the model
    needs, so it is kept up to ``_ANSWER_SNIPPET_MAX`` chars rather than
    truncated to a teaser — otherwise the answer would be cut off and the model
    would have nothing useful to read. The leading ``message`` summary is kept
    as the header.
    """
    head = _as_str(result.get("message", ""))
    results = result.get("results")
    if not isinstance(results, list) or not results:
        return head or "(no results)"

    lines: list[str] = [head] if head else []
    for i, hit in enumerate(results, start=1):
        if not isinstance(hit, dict):
            continue
        title = _as_str(hit.get("title", "")).strip() or "(untitled)"
        score = hit.get("score")
        score_tag = ""
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            score_tag = f" [score={score:.4f}]"
        lines.append(f"{i}. {title}{score_tag}")
        url = _as_str(hit.get("url", "")).strip()
        if url:
            lines.append(f"   {url}")
        snippet = _as_str(hit.get("snippet", "")).strip()
        if snippet:
            # A url-less hit is an answer/passage → keep it (nearly) whole; a
            # url-bearing hit is a source link → keep a compact teaser (the
            # model can web_fetch the url for more).
            limit = _SOURCE_SNIPPET_MAX if url else _ANSWER_SNIPPET_MAX
            if len(snippet) > limit:
                snippet = snippet[:limit] + "…"
            lines.append(f"   {snippet}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# background_process
# ---------------------------------------------------------------------------
#
# Result shapes returned by
# :func:`qai.platform.background_process.tool_handlers.handle_background_process`
# (consult that module for the authoritative contract):
#
# * ``list``           → ``{"ok": True, "processes": [Info-dict, ...]}``
# * ``start``          → ``{"ok": True, "process":   Info-dict}``
# * ``status``         → ``{"ok": True, "process":   Info-dict}``
# * ``stop``           → ``{"ok": True, "process":   Info-dict, "terminal_kind": "stopped"}``
# * ``restart``        → ``{"ok": True, "process":   Info-dict, "terminal_kind": "restarted"}``
# * ``logs``           → ``{"ok": True, "id": "bgp...", "output": "<text>"}``
# * ``wait``           → ``{"ok": True, "settled": bool, "process": Info-dict,
#                          "output": "<text>", "exit_diagnostics": "<text>"}``
#                        (``output`` + ``exit_diagnostics`` only when
#                         ``settled=True``; the retained child output is folded
#                         into the same result so the LLM does not need a
#                         follow-up ``logs`` round-trip.)
# * any ``ok=False``   → ``{"ok": False, "error_code": ..., "message": ...}``
#
# Where ``Info-dict`` is :func:`info_to_dict` output — the JSON-serialised
# :class:`~qai.platform.background_process.ports.Info` (``id`` / ``pid`` /
# ``command`` / ``status`` / ``ports`` / ``description`` / ``cwd`` / ...).
# Failures are projected by :func:`_render_error` — only the ok-path is
# handled here.


def _render_bgp_info(info: dict[str, Any]) -> str:
    """Render one ``Info`` dict (one tracked process) as a single line.

    Keeps the fields the model is most likely to act on: id / status
    (with ``ready`` flag when the manager reports it ready) / pid /
    ports / description / command. Long commands are tail-truncated to
    keep the line readable in the chat card.
    """
    pid = info.get("pid")
    pid_part = f"pid={pid}" if isinstance(pid, int) else "pid=-"
    status = _as_str(info.get("status", "?"))
    # Only annotate ``(ready)`` while the process is actually running. The
    # ``ready`` flag is orthogonal to ``status`` and the manager keeps it set
    # on a clean exit (``_exited`` preserves ``ready and exit_code == 0``), so
    # an unconditional ``(ready)`` produced misleading text like
    # ``ready (ready)`` / ``exited (ready)`` (the reported duplicate-``(ready)``
    # bug). Gating on ``status == "running"`` mirrors the frontend badge rule
    # in ``BackgroundProcessCard.vue`` (``statusKind``: ``running && ready``).
    if info.get("ready") and status == "running":
        status = f"{status} (ready)"
    ports = info.get("ports") or []
    ports_part = ""
    if isinstance(ports, list) and ports:
        ports_part = f" ports={','.join(str(p) for p in ports)}"
    desc = _as_str(info.get("description") or "").strip()
    desc_part = f" — {desc}" if desc else ""
    command = _as_str(info.get("command", "")).strip()
    # Tail-truncate command to ~80 chars so the line stays readable.
    if len(command) > 80:
        command = command[:79] + "…"
    rendered = (
        f"  {info.get('id', '?')}  [{status}]  {pid_part}{ports_part}"
        f"{desc_part}\n      $ {command}"
    )
    # D2-D: FileGuard denial diagnostic — populated by the manager's
    # ``_on_exit`` when the subprocess died non-zero AND the native denial
    # probe returned a non-empty note. ``build_native_guard_denial_note``
    # (D2-A domain module) already prefixes the note with ``"\n\n"``, so a
    # plain concatenation keeps a single blank line between the command
    # line and the diagnostic block. Empty string (default) is a no-op.
    diag = _as_str(info.get("exit_diagnostics", ""))
    if diag.strip():
        rendered += diag
    return rendered


def _render_background_process(result: dict[str, Any]) -> str | None:
    """Render a ``background_process`` ok-result dict.

    Returns ``None`` when the shape does not match any of the tool's
    known successful payloads (``processes`` / ``process`` / logs), so the
    caller can fall through to the generic ``message``-based renderer.
    """
    # ``list`` action.
    processes = result.get("processes")
    if isinstance(processes, list):
        if not processes:
            return "(no background processes)"
        lines = [f"{len(processes)} background process(es):"]
        for info in processes:
            if isinstance(info, dict):
                lines.append(_render_bgp_info(info))
        return "\n".join(lines)

    # ``wait`` action — distinguished by the ``settled`` key it carries in
    # addition to ``process``. This branch MUST come before the plain
    # ``process`` branch below, because both share the ``process`` field:
    # without this discriminator ``wait`` would be absorbed by the
    # start/status/stop/restart branch, silently dropping the retained
    # child ``output`` + ``exit_diagnostics`` — the LLM would see only the
    # one-line Info summary and conclude the process had no output, while
    # the UI (which reads Logs directly over SSE / HTTP) still shows the
    # complete output. That mismatch is the "wait says output empty even
    # though the card shows 45 lines" bug this branch fixes.
    if "settled" in result:
        process = result.get("process")
        head = _render_bgp_info(process) if isinstance(process, dict) else ""
        settled = bool(result.get("settled"))
        if not settled:
            # Still running when the wait budget elapsed. Explicit sentinel
            # so the LLM does not read the head line's ``[running]`` as a
            # final result. Wording matches the tool description (see
            # ``tool_schemas.py``: "``settled: false`` means only that the
            # budget elapsed — re-issue to keep waiting").
            marker = "(settled: false — budget elapsed; re-issue wait to keep blocking)"
            return f"{head}\n{marker}" if head else marker
        output = _as_str(result.get("output", ""))
        diag = _as_str(result.get("exit_diagnostics", "")).strip()
        # Body mirrors the ``logs`` action's ``[<id> logs]`` framing so the
        # LLM sees the retained output in a familiar shape.
        proc_id = ""
        if isinstance(process, dict):
            proc_id = _as_str(process.get("id", ""))
        logs_header = f"[{proc_id} logs]" if proc_id else "[logs]"
        logs_body = (
            f"{logs_header}\n{output}" if output else f"{logs_header} (no output)"
        )
        body = f"{head}\n{logs_body}" if head else logs_body
        if diag:
            body = f"{body}\n{diag}"
        return body

    # ``start`` / ``status`` / ``stop`` / ``restart`` action.
    process = result.get("process")
    if isinstance(process, dict):
        return _render_bgp_info(process)

    # ``logs`` action — ``id`` starts with ``bgp`` (validated by the
    # ``Info.__post_init__`` in the platform module). Use that prefix to
    # discriminate from the ``grep`` ``output`` branch (which has no ``id``).
    process_id = result.get("id")
    if isinstance(process_id, str) and process_id.startswith("bgp"):
        output = _as_str(result.get("output", ""))
        # 2026-08-03 — append the FileGuard-authoritative denial note when the
        # manager's audit probe attributed DENY rows to this subprocess tree
        # (``Info.exit_diagnostics``, forwarded by ``tool_handlers`` on the
        # logs result). Mirrors how ``_render_exec`` surfaces the same field:
        # without it the LLM saw only the child's raw ``permission denied``
        # line and retried with alternate paths / tools against an enforced
        # boundary. The note already carries its own ``[FileGuard]`` prefix
        # (``build_native_guard_denial_note``), so it is appended verbatim.
        diag = _as_str(result.get("exit_diagnostics", "")).strip()
        body = (
            f"[{process_id} logs]\n{output}"
            if output
            else f"[{process_id} logs] (no output)"
        )
        if diag:
            return f"{body}\n{diag}"
        return body

    return None


def render_tool_result_text(result: Any) -> str:
    """Project an ai_coding tool result into V1-style plain text.

    Non-dict results (a tool that already returned a string, or a bare
    value) are coerced with ``str`` unchanged — this matches the legacy
    ``str(raw_result)`` fallback and keeps string-returning tools intact.

    Dict results are rendered by inspecting their **field shape** (not
    the tool name) so the projection survives new tools that follow the
    same ``_ok`` conventions:

    * ``ok=False``                 → ``[tool_error] <message>``
    * ``content`` present          → the content verbatim, else ``message``
      (read / web_fetch; the ``message`` fallback surfaces ``read`` boundary
      notes like "offset beyond end of file" when ``content`` is empty)
    * ``output`` present           → ``<output>\\n<message>`` (grep)
    * ``stdout``/``exit_code``      → V1 exec text (stdout/[stderr]/[exit code])
    * ``files`` of dicts           → patch head + per-file detail
    * ``files`` of strings         → glob path list + ``(message)``
    * otherwise                    → ``message`` (write / edit / generic)
    """
    if not isinstance(result, dict):
        if result is None:
            return ""
        return _as_str(result)

    if result.get("ok") is False:
        # exec timeout carries stdout/stderr too — render those alongside.
        if result.get("timed_out") or "stdout" in result:
            return _render_exec(result)
        return _render_error(result)

    # read / web_fetch: the model wants the file/page body verbatim.
    if "content" in result:
        content = _as_str(result.get("content", ""))
        if content:
            return content
        # Empty content but an informative ``message`` → surface the message
        # (R-4b). ``read`` returns ``ok=True`` with empty ``content`` and a
        # diagnostic ``message`` for boundary cases the model MUST see — e.g.
        # ``offset`` beyond EOF (``"(file has N lines; offset X is beyond end
        # of file)"``) or an empty file. Returning the empty content alone fed
        # the model a blank tool result (the chat card showed "(no output)")
        # and dropped that diagnostic, so the model could not tell an
        # out-of-range read apart from a genuinely empty file. Falling back to
        # the message preserves the body-verbatim contract whenever content is
        # present (the common, non-empty case is unchanged — including a
        # web_fetch page whose extracted body is always non-empty here) while
        # no longer discarding the boundary note when there is no body.
        message = _as_str(result.get("message", ""))
        if message:
            return message
        return content

    # background_process — match by shape (``processes`` / ``process`` /
    # bgp-prefixed ``id`` + ``output``). MUST come before the grep ``output``
    # branch below, since ``logs`` returns ``{"id": "bgp...", "output": "..."}``
    # whose ``output`` key would otherwise be claimed by grep.
    bgp = _render_background_process(result)
    if bgp is not None:
        return bgp

    # web_search: a ``results`` list of {title,url,snippet,score} dicts →
    # a compact numbered list the model can read + follow up with ``web_fetch``.
    if "results" in result and isinstance(result.get("results"), list):
        return _render_web_search(result)

    # grep: ``output`` is the pre-rendered multi-line match text; append
    # the count summary message (V1 ``result + summary``).
    if "output" in result:
        output = _as_str(result.get("output", ""))
        message = _as_str(result.get("message", ""))
        if output and message:
            return f"{output}\n{message}"
        return output or message

    # exec: stdout/stderr/exit_code split fields → V1 assembled text.
    if "stdout" in result or "exit_code" in result:
        return _render_exec(result)

    # patch vs glob both use ``files``; discriminate by element type.
    files = result.get("files")
    if isinstance(files, list) and files:
        if isinstance(files[0], dict):
            return _render_patch(result)
        return _render_glob(result)
    if isinstance(files, list):  # empty list → glob "no files matched"
        return _render_glob(result)

    # write / edit / generic: ``message`` is the full V1 string.
    raw_message = result.get("message")
    if isinstance(raw_message, str):
        return raw_message
    # Last-resort: never feed a Python dict repr to the model.
    return str(raw_message) if raw_message is not None else ""


# ---------------------------------------------------------------------------
# Universal FileGuard denial hint injection (2026-07-13; 2026-07-23 V2 rewrite)
# ---------------------------------------------------------------------------
# Regardless of which tool produced the output, regardless of exit code, and
# regardless of how the command was structured — if the rendered text contains
# an access-denied signal (see
# ``exec_diagnostics.ACCESS_DENIED_SIGNALS`` for the single-source detection
# list), append the concise FileGuard denial hint so the model always knows:
#   (a) this is an enforced security boundary, not a transient error;
#   (b) retrying with alternate tools / path forms / symlinks / copies fails
#       identically — no bypass exists;
#   (c) the correct action is to stop and ask the user to authorize the path
#       in Security → Allow Lists.
#
# Placed at the render layer (not inside individual tool handlers) so it covers
# every tool uniformly: exec, background_process logs, write, edit, and any
# future tool that surfaces file-access errors. The single-source helper
# :func:`qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics.append_fileguard_hint_if_denial`
# owns both the detection list and the emitted guidance so this render layer,
# the in-process registry ``ToolGuardDenied`` catches, the appbuilder input
# rejection path, and the direct FileGuard denials in ``_file_guard_bridge``
# all emit the SAME text — the model never sees two different wordings for
# the same class of denial.
#
# SCOPE (2026-08-03 false-positive fix): the textual scan is applied ONLY to
# FAILURE results. It used to run on EVERY rendered result regardless of
# outcome ("purely textual — no tool-specific logic"), which meant a tool's
# successfully-returned CONTENT was scanned too: reading any file whose prose
# contains ``is denied`` / ``access denied`` / ``permission denied`` /
# ``拒绝访问`` (security specs, error-handling guides, log files, this
# project's own FileGuard docs) got the denial hint appended even though
# ``filesec.decision`` had logged ``decision=ALLOW``. See
# :func:`render_tool_result_text_with_hints` for the failure gate.


def _maybe_append_fileguard_hint(
    text: str,
    *,
    attribution: HintAttribution = "textual_only",
) -> str:
    """Append an access-denied hint if ``text`` contains a denial signal.

    Thin apps-layer wrapper (2026-07-23) around the SINGLE-SOURCE helper
    :func:`qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics
    .append_fileguard_hint_if_denial`. Retained for the many existing
    call sites in ``apps.api`` (di.py streaming exec, _chat_appbuilder_tool_bridge,
    render_tool_result_text_with_hints) so their import site stays stable.

    ``attribution`` (default ``"textual_only"``) selects the hint variant —
    see the shared helper's docstring. Callers that KNOW the denial came
    from FileGuard (they caught ``ToolGuardDenied``) MUST pass
    ``attribution="confirmed_fileguard"`` to get the strong ``[FileGuard]``
    guidance; callers that only scanned free-text output use the default
    ``"textual_only"`` (neutral ``[hint]`` variant) so a Windows ACL denial
    is never mislabelled as a FileGuard block.
    """
    from qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics import (
        append_fileguard_hint_if_denial,
    )
    return append_fileguard_hint_if_denial(text, attribution=attribution)


#: Result-dict keys that are ALWAYS an ERROR CHANNEL (a tool's own diagnosis
#: of a failure) as opposed to CONTENT the tool was asked to fetch. The
#: textual access-denied scan is applied to these fields ONLY.
#:
#: ``stderr`` + ``exit_diagnostics`` are the exec / background-process error
#: channels: a child killed by the native FileGuard writes "Access is denied."
#: there while the ENVELOPE still reports ``ok=True`` (exec returns ``_ok(...)``
#: for ANY exit code — see ``handlers/exec.py`` ``return _ok(f"exec exited
#: {exit_code}", ...)``), so gating on ``ok is False`` alone would MISS exactly
#: the case the hint exists for.
#:
#: ``message`` is DELIBERATELY EXCLUDED here: ``_shared._ok(message, **fields)``
#: makes it the generic SUCCESS SUMMARY for every handler, and several tools
#: fold CONTENT into it —
#:   * ``search.py`` grep/glob echo the user's pattern:
#:     ``_ok(f"(no matches found for pattern: {pattern!r})", ...)`` → searching
#:     for the literal string ``access denied`` would trip the hint;
#:   * ``browser.py`` puts the extracted page text / aria snapshot INTO
#:     ``message`` (``_ok(text or "(no text extracted)", ...)``);
#:   * ``read_write.py`` returns ``_ok("(file has N lines; offset X is beyond
#:     end of file)", content="")`` and the renderer surfaces ``message`` as the
#:     body when ``content`` is empty.
#: It is added to the scan set ONLY when ``ok is False`` (a real error
#: envelope) — see :func:`render_tool_result_text_with_hints`.
_ERROR_CHANNEL_KEYS: tuple[str, ...] = (
    "stderr",
    "exit_diagnostics",
)

#: Additional error-channel keys that apply ONLY to a failure envelope
#: (``ok is False``). ``message`` is the generic SUCCESS SUMMARY otherwise
#: (see the note above); ``error`` / ``error_code`` are only meaningful on a
#: failure and a couple of handlers reuse those names for non-error payloads,
#: so they are gated the same way rather than scanned unconditionally.
_FAILURE_ONLY_ERROR_KEYS: tuple[str, ...] = ("message", "error", "error_code")


def render_tool_result_text_with_hints(result: Any) -> str:
    """``render_tool_result_text`` + FileGuard denial hint, scanned on the
    ERROR CHANNEL only.

    Drop-in replacement for ``render_tool_result_text`` at call sites that
    feed output to the LLM. Keeps the two concerns separate: shape-based
    rendering (``render_tool_result_text``) and security-hint injection
    (``_maybe_append_fileguard_hint``).

    False-positive fix (2026-08-03) — WHY this is not a plain
    ``render + scan(rendered_text)``: the injector is a PURELY TEXTUAL scan
    for ``ACCESS_DENIED_SIGNALS`` (``"is denied"``, ``"access denied"``,
    ``"permission denied"``, ``"拒绝访问"``, ...). Scanning the FULL rendered
    text means the CONTENT a tool legitimately returned is scanned too, so
    reading any document that merely *mentions* a denial phrase (a security
    spec, an error-handling guide, a log file, or this project's own FileGuard
    docs) got the hint wrongly appended — telling the model "this operation was
    blocked, stop and ask the user to authorize" when nothing was blocked
    (observed: a 50 KB patent-IDF markdown whose prose contains ``is denied``,
    for which ``filesec.decision`` logged ``decision=ALLOW``).

    Gating on ``ok is False`` instead would introduce the MIRROR defect (a
    false NEGATIVE): ``exec`` returns ``_ok(...)`` for every exit code, so a
    child killed by the native FileGuard (``stderr="Access is denied."``,
    ``exit_code=1``, ``ok=True``) would silently lose the hint — precisely the
    case the hint was written for.

    So the scan input is the ERROR CHANNEL, not the outcome flag and not the
    whole body:

    * ``dict`` → concatenate the ``_ERROR_CHANNEL_KEYS`` values (``stderr`` /
      ``exit_diagnostics`` / ``message`` / ``error`` / ``error_code``) and scan
      THAT. Content keys (``content`` / ``stdout`` / ``files`` / ``matches`` /
      ``results``) are never scanned. Note ``stdout`` is deliberately EXCLUDED:
      it is the command's content channel, and a program that merely prints the
      phrase must not trip the hint (its stderr would carry a real denial).
    * ``str`` starting with ``[tool_error]`` → scan (a pre-rendered failure —
      the whole string IS the error channel).
    * anything else → no scan, returned verbatim.

    When the error channel does carry a denial signal, the hint is appended to
    the FULL rendered text (so the model sees it at the end of the tool output,
    unchanged placement).

    Call sites that KNOW a denial happened bypass this gate entirely — they
    call :func:`_maybe_append_fileguard_hint` directly with
    ``attribution="confirmed_fileguard"`` (``_chat_appbuilder_tool_bridge``,
    ``di.py``). The exec / streaming paths in ``di.py`` likewise scan their own
    consolidated stdout+stderr directly and are unaffected.
    """
    from qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics import (
        text_has_access_denied_signal,
    )

    rendered = render_tool_result_text(result)

    scan_target: str | None = None
    if isinstance(result, dict):
        keys = _ERROR_CHANNEL_KEYS
        if result.get("ok") is False:
            # A real error envelope — ``message`` now genuinely holds the
            # error text (``_render_error`` also prefixes ``[tool_error]``).
            keys = keys + _FAILURE_ONLY_ERROR_KEYS
        chunks = [
            _as_str(result.get(key, "")) for key in keys if result.get(key)
        ]
        scan_target = "\n".join(chunks) if chunks else None
    elif isinstance(result, str) and result.lstrip().startswith("[tool_error]"):
        scan_target = result

    if not scan_target or not text_has_access_denied_signal(scan_target):
        return rendered

    # The error channel DID carry a denial signal. Do NOT route through
    # ``_maybe_append_fileguard_hint`` here: that helper re-scans the text it
    # is given, and ``rendered`` may legitimately NOT contain the phrase (an
    # exec whose stderr held "Access is denied." but whose rendered body is
    # just "started" / "(no output)"), which would silently drop the hint —
    # the very false-negative this function exists to avoid. Append the
    # neutral variant directly.
    from qai.ai_coding.infrastructure.tools.handlers.exec_diagnostics import (
        build_access_denied_ambiguous_hint,
    )

    # Idempotency: defer only when guidance is already present IN THE ERROR
    # CHANNEL we just scanned — NOT anywhere in ``rendered``. Scanning the
    # whole rendered body would let a file whose CONTENT happens to contain a
    # literal ``[hint]`` / ``[FileGuard]`` token suppress a REAL stderr denial
    # (a false negative introduced by the naive check). The markers we defer
    # to are exactly the ones the upstream producers emit into the error
    # channel: ``format_exit_diagnostics``' ``[hint]`` fallbacks and
    # ``build_native_guard_denial_note``'s ``[FileGuard]`` audit note, both of
    # which land in ``exit_diagnostics`` / ``stderr``.
    low_channel = scan_target.lower()
    if "[fileguard]" in low_channel or "[hint]" in low_channel:
        return rendered
    return rendered + build_access_denied_ambiguous_hint()
