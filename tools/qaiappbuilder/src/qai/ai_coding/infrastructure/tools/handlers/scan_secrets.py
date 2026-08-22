# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
r"""``scan_secrets`` tool handler — read-only credential / PII self-check.

Why this exists next to ``scripts/ci/check_no_secrets.py``
----------------------------------------------------------
The repo already gates commits on ``scripts/ci/check_no_secrets.py`` through
the ``pre-commit`` hook. That gate fires LAST — after the model has finished
writing, staged everything and asked for a commit — which is the most
expensive moment to learn that a fixture line looks like a live token. This
tool exposes the SAME detection rules as an in-loop diagnostic so the model
can check the two files it just touched, immediately, and fix them before the
gate ever runs.

The two entry points deliberately share RULES, not CODE:

* the CI script is not importable — it lives under ``scripts/`` (not a
  package) and its module body has CLI side effects (``sys.stdout.reconfigure``,
  repo-root discovery, ``argparse``). Importing it from a tool handler would
  drag process-global reconfiguration into the daemon.
* the script MUST stay standalone: the git hook runs it with a bare
  ``python scripts/ci/check_no_secrets.py``, with no ``src/`` on ``sys.path``.
  Making it import from ``qai.*`` would break the hook in exactly the
  situation it is meant to protect (a clean checkout, no venv activated).

So the pattern tables below are an independent transcription of the script's
``SECRET_PATTERNS`` / ``PII_PATTERNS`` / ``ALLOW_LIST``. When a rule changes,
change it in BOTH places — they are two entry points onto one rule set, and
:mod:`tests.unit.qai.ai_coding.tools.test_scan_secrets` pins the shapes that
matter (counts, severities, redaction) on this side.

This tool never blocks anything. It reports ``ok=True`` with structured
findings and lets the model decide; the pre-commit hook remains the only
gate.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from qai.ai_coding.application.ports import FileGuardPort, ToolResultStorePort
from qai.ai_coding.infrastructure.tools.errors import ToolError
from qai.ai_coding.infrastructure.tools.handlers._shared import (
    WalkBudget,
    _ok,
    _walk_filtered,
    default_cwd,
    get_project_skip_dirs,
    resolve_under_workspace,
)

# ---------------------------------------------------------------------------
# HARD rules — a match here is a probable live credential.
# Mirrors ``SECRET_PATTERNS`` in scripts/ci/check_no_secrets.py.
# ---------------------------------------------------------------------------
SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "GitHub token (ghp_/gho_/ghs_/github_pat_)",
        re.compile(
            r"""(?<![A-Za-z0-9_])(gh[pous]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{80,})""",
            re.IGNORECASE,
        ),
    ),
    (
        "AWS Access Key ID",
        re.compile(r"""(?<![A-Z0-9])(A(?:KIA|BIA|CCA|SIA)[0-9A-Z]{16})(?![A-Z0-9])"""),
    ),
    # Stripe SECRET/restricted keys. ``sk_live_`` / ``rk_live_`` bill real
    # money and cannot be rotated silently, so a leak is immediately costly;
    # the ``_test_`` variants are deliberately included too, because a repo
    # that commits test keys is a repo that will commit a live one.
    (
        "Stripe secret key (sk_/rk_)",
        re.compile(
            r"""(?<![A-Za-z0-9_])((?:sk|rk)_(?:live|test)_[A-Za-z0-9]{16,})"""
        ),
    ),
    # Only values >= 20 chars: shorter strings are almost always placeholders.
    (
        "Hardcoded token/key/secret assignment",
        re.compile(
            r"""(?i)(?:token|api_key|apikey|secret|password|passwd|access_key)\s*=\s*['"]([A-Za-z0-9+/=_\-]{20,})['"]"""
        ),
    ),
    (
        "PEM private key",
        re.compile(r"""-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----"""),
    ),
    (
        "Slack token",
        re.compile(r"""xox[baprs]-[0-9A-Za-z\-]{10,}"""),
    ),
    (
        "Bearer token in code",
        re.compile(
            r"""(?i)(?:Authorization|Bearer)\s*[=:]\s*['"]?(?:Bearer\s+)?([A-Za-z0-9+/=_\-\.]{30,})['"]?"""
        ),
    ),
    # ---- Cloud model / AI-platform API keys ----
    (
        "OpenAI API key",
        re.compile(r"""(?<![A-Za-z0-9])(sk-[A-Za-z0-9]{20}T3BlbkFJ[A-Za-z0-9]{20})"""),
    ),
    (
        "Anthropic API key",
        re.compile(r"""(?<![A-Za-z0-9])(sk-ant-[A-Za-z0-9\-_]{80,})"""),
    ),
    (
        "Google/Gemini API key",
        re.compile(r"""(?<![A-Za-z0-9_\-])(AIza[0-9A-Za-z\-_]{35})(?![0-9A-Za-z\-_])"""),
    ),
    (
        "Hugging Face token",
        re.compile(r"""(?<![A-Za-z0-9])(hf_[A-Za-z0-9]{34,})"""),
    ),
    (
        "Groq API key",
        re.compile(r"""(?<![A-Za-z0-9])(gsk_[A-Za-z0-9]{40,})"""),
    ),
    (
        "Replicate API token",
        re.compile(r"""(?<![A-Za-z0-9])(r8_[A-Za-z0-9]{35,})"""),
    ),
    # ---- Cloud credentials / connection material ----
    (
        "GCP service-account key material",
        re.compile(r'''"(?:private_key|type)"\s*:\s*"(?:-----BEGIN[^"]+|service_account)'''),
    ),
    (
        "JSON Web Token (JWT)",
        re.compile(
            r"""(?<![A-Za-z0-9_\-])(eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,})"""
        ),
    ),
    (
        "DB/broker connection string with password",
        re.compile(
            r"""(?i)\b(?:postgres(?:ql)?|mysql|mongodb(?:\+srv)?|redis|amqps?)://[^:@\s/]+:[^@\s/]{3,}@"""
        ),
    ),
    (
        "Azure connection string (AccountKey)",
        re.compile(r"""(?i)AccountKey\s*=\s*[A-Za-z0-9+/=]{40,}"""),
    ),
    (
        "Feishu/Lark bot webhook",
        re.compile(
            r"""https://open\.(?:feishu\.cn|larksuite\.com)/open-apis/bot/v2/hook/[A-Za-z0-9\-]{20,}"""
        ),
    ),
    (
        "WeChat-Work bot webhook key",
        re.compile(
            r"""https://qyapi\.weixin\.qq\.com/cgi-bin/webhook/send\?key=[A-Za-z0-9\-]{20,}"""
        ),
    ),
    (
        "Feishu/Lark app secret",
        re.compile(
            r"""(?i)(?:lark|feishu)[_-]?(?:app[_-]?)?secret\s*[=:]\s*['"]([A-Za-z0-9]{20,})['"]"""
        ),
    ),
]

# ---------------------------------------------------------------------------
# PII rules — informational only. These regexes are false-positive prone, so
# they are reported at severity ``"pii"`` and NEVER promoted to ``"secret"``
# (the CI script keeps them out of its exit code for the same reason).
# ---------------------------------------------------------------------------
PII_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "Email address (PII)",
        re.compile(r"""(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"""),
    ),
    (
        "CN mobile number (PII)",
        re.compile(r"""(?<!\d)(1[3-9]\d{9})(?!\d)"""),
    ),
    (
        "CN national ID (PII)",
        re.compile(r"""(?<!\d)(\d{17}[0-9Xx])(?!\d)"""),
    ),
    (
        "Bank card number (PII)",
        re.compile(
            r"""(?i)(?:bank[\s_-]*card|card[\s_-]*(?:no|number|num)|银行卡|卡号)\D{0,10}(\d{13,19})(?!\d)"""
        ),
    ),
]


@dataclass(frozen=True, slots=True)
class AllowEntry:
    """One allow-list rule: all three non-empty fields must match to suppress.

    An empty field matches anything, so an entry with only ``file_suffix``
    exempts a whole file kind, and one with only ``value_fragment`` exempts a
    placeholder shape everywhere.
    """

    file_suffix: str = ""
    pattern_fragment: str = ""
    value_fragment: str = ""
    reason: str = ""


# Mirrors ``ALLOW_LIST`` in scripts/ci/check_no_secrets.py.
ALLOW_LIST: list[AllowEntry] = [
    AllowEntry(
        file_suffix=".example",
        reason="Example files may contain placeholder values",
    ),
    AllowEntry(
        file_suffix=".md",
        pattern_fragment="Hardcoded token",
        reason="Markdown docs may show token format examples",
    ),
    AllowEntry(
        value_fragment="fake",
        reason="Value contains 'fake' — likely a test placeholder",
    ),
    AllowEntry(
        value_fragment="test",
        reason="Value contains 'test' — likely a test placeholder",
    ),
    AllowEntry(
        value_fragment="placeholder",
        reason="Value contains 'placeholder' — intentional stub",
    ),
    AllowEntry(
        value_fragment="example",
        reason="Value contains 'example' — intentional stub",
    ),
    AllowEntry(
        value_fragment="REPLACE_ME",
        reason="Value is a template placeholder",
    ),
    AllowEntry(
        value_fragment="YOUR_",
        reason="Value is a template placeholder (YOUR_…)",
    ),
    AllowEntry(
        file_suffix="check_no_secrets.py",
        reason="The CI scanner script carries these regexes as literals",
    ),
    AllowEntry(
        file_suffix="scan_secrets.py",
        reason="This handler carries the same regexes as literals",
    ),
    AllowEntry(
        file_suffix=".md",
        pattern_fragment="GitHub token",
        reason="Markdown files may document token formats",
    ),
    AllowEntry(
        file_suffix=".md",
        reason="Markdown docs may show illustrative credential formats",
    ),
    AllowEntry(
        file_suffix="pnpm-lock.yaml",
        reason="Lock file integrity hashes are not secrets",
    ),
    AllowEntry(
        file_suffix="package-lock.json",
        reason="Lock file integrity hashes are not secrets",
    ),
    AllowEntry(
        file_suffix="uv.lock",
        reason="Lock file integrity hashes are not secrets",
    ),
    AllowEntry(
        value_fragment="qai-channels-default-verifier",
        reason="Public dev default signing value, not a live credential",
    ),
    AllowEntry(
        pattern_fragment="Bearer token",
        value_fragment="_DEFAULT_",
        reason="RHS is a constant identifier reference, not a secret value",
    ),
]

#: Extensions worth scanning when a DIRECTORY is walked. An explicitly named
#: FILE is always scanned regardless of extension — the model asked for that
#: exact path, so second-guessing it would silently return "clean".
SCAN_EXTENSIONS: frozenset[str] = frozenset({
    ".py", ".js", ".ts", ".jsx", ".tsx", ".vue",
    ".json", ".yaml", ".yml", ".toml",
    ".sh", ".bash", ".zsh", ".bat", ".cmd", ".ps1",
    ".env", ".cfg", ".ini", ".conf",
    ".txt",
})

#: Directory names never walked. Superset of the CI script's ``SKIP_DIRS``:
#: generated / vendored trees produce nothing but noise, and agent-state
#: directories multiply every finding by the number of worktrees.
SKIP_DIRS: frozenset[str] = frozenset({
    ".git", ".venv", "venv", "node_modules", ".pytest_cache",
    "__pycache__", ".mypy_cache", ".ruff_cache", "dist", "build",
    ".build", "vendor", "models", "data", "bin", ".edit_trash",
    ".playwright-mcp", ".import_linter_cache", "samples",
    ".kilo", ".roo", ".claude", ".opencode", ".agent",
})

#: Max findings rendered in-prompt. Beyond this the full list is persisted via
#: the result store and the message points at ``read(path=...)``.
MAX_INPROMPT_FINDINGS = 100

#: Max files a single directory walk will read. A ``paths``-scoped call never
#: comes near it; a workspace-wide default scan on a huge tree stops in
#: bounded time and says so instead of stalling the turn.
MAX_FILES_SCANNED = 4000


@dataclass(frozen=True, slots=True)
class _Finding:
    path: str
    line: int
    rule: str
    severity: str  # "secret" | "pii"
    matched_value: str
    line_text: str


def _redact(value: str, *, severity: str) -> str:
    """Return the reportable form of a matched value.

    Secrets keep 6 leading characters, PII keeps 3 — enough to locate the hit
    on the line, never enough to reconstruct the credential. This is the whole
    reason the handler carries ``matched_value`` internally and emits only
    ``redacted_value``: a tool result travels into the model context and the
    transcript, so a plaintext key here would leak the very thing the scan is
    meant to protect.

    A value no longer than its own keep-length is replaced ENTIRELY: slicing
    alone would hand back the whole match (``value[:6]`` of a 5-char match is
    that match), so a short credential would be reported verbatim by the tool
    whose purpose is to keep it out of the transcript. The CI scanner guards
    the same way; matching it keeps one redaction contract across both entry
    points.
    """
    keep = 6 if severity == "secret" else 3
    if len(value) <= keep:
        return "***"
    return value[:keep] + "***"


def _is_allowed(finding: _Finding) -> AllowEntry | None:
    """Return the allow-list entry suppressing ``finding``, else ``None``."""
    for entry in ALLOW_LIST:
        if entry.file_suffix and not finding.path.endswith(entry.file_suffix):
            continue
        if (
            entry.pattern_fragment
            and entry.pattern_fragment.lower() not in finding.rule.lower()
        ):
            continue
        if (
            entry.value_fragment
            and entry.value_fragment.lower() not in finding.matched_value.lower()
        ):
            continue
        return entry
    return None


def _scan_text(
    text: str, *, path: str, include_pii: bool
) -> list[_Finding]:
    """Apply every rule to ``text`` line by line."""
    findings: list[_Finding] = []
    rule_sets: list[tuple[list[tuple[str, re.Pattern[str]]], str]] = [
        (SECRET_PATTERNS, "secret")
    ]
    if include_pii:
        rule_sets.append((PII_PATTERNS, "pii"))

    for line_no, line in enumerate(text.splitlines(), start=1):
        for patterns, severity in rule_sets:
            for name, pattern in patterns:
                match = pattern.search(line)
                if match is None:
                    continue
                value = (
                    match.group(1)
                    if match.lastindex and match.lastindex >= 1
                    else match.group(0)
                )
                findings.append(
                    _Finding(
                        path=path,
                        line=line_no,
                        rule=name,
                        severity=severity,
                        matched_value=value,
                        line_text=line.rstrip(),
                    )
                )
    return findings


def _scan_file(path: Path, *, include_pii: bool) -> list[_Finding]:
    """Scan one file; an unreadable file yields nothing rather than raising.

    A binary or permission-denied file inside a walked directory must not
    abort the whole scan — the point of the tool is the findings it CAN
    produce.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _scan_text(text, path=str(path), include_pii=include_pii)


def _collect_files(target: Path) -> tuple[list[Path], bool]:
    """Return ``(files, truncated)`` for one resolved target.

    A file target is taken verbatim. A directory target is walked with the
    skip-dir set and filtered to :data:`SCAN_EXTENSIONS`, bounded by
    :data:`MAX_FILES_SCANNED`.
    """
    if target.is_file():
        return [target], False

    extra_skip = SKIP_DIRS | get_project_skip_dirs()
    files: list[Path] = []
    budget = WalkBudget()
    for dirpath, _dirnames, filenames in _walk_filtered(
        target, frozenset(extra_skip), budget
    ):
        for name in filenames:
            candidate = dirpath / name
            if candidate.suffix.lower() in SCAN_EXTENSIONS:
                files.append(candidate)
                if len(files) >= MAX_FILES_SCANNED:
                    return files, True
    return files, budget.exceeded


def _scan_targets(
    targets: list[Path], *, include_pii: bool
) -> tuple[list[_Finding], list[tuple[_Finding, AllowEntry]], int, bool]:
    """Scan every target, splitting reported findings from suppressed ones.

    Returns ``(reported, suppressed, files_scanned, walk_truncated)``. Runs
    entirely in a worker thread (blocking filesystem IO), so it takes no
    async dependency.
    """
    reported: list[_Finding] = []
    suppressed: list[tuple[_Finding, AllowEntry]] = []
    seen: set[Path] = set()
    walk_truncated = False

    for target in targets:
        files, truncated = _collect_files(target)
        walk_truncated = walk_truncated or truncated
        for file_path in files:
            # Overlapping targets ("src; src/qai") must not double-report.
            if file_path in seen:
                continue
            seen.add(file_path)
            for finding in _scan_file(file_path, include_pii=include_pii):
                entry = _is_allowed(finding)
                if entry is None:
                    reported.append(finding)
                else:
                    suppressed.append((finding, entry))

    return reported, suppressed, len(seen), walk_truncated


def _as_dict(finding: _Finding) -> dict[str, Any]:
    """Project a finding onto the wire shape — redacted value, no plaintext."""
    return {
        "path": finding.path,
        "line": finding.line,
        "rule": finding.rule,
        "severity": finding.severity,
        "redacted_value": _redact(
            finding.matched_value, severity=finding.severity
        ),
    }


def _render(findings: list[_Finding]) -> str:
    """Render findings as one ``path:line  [severity] rule → value`` per line.

    The line TEXT is deliberately omitted: it is the one place a plaintext
    credential would slip back into the output.
    """
    return "\n".join(
        f"{f.path}:{f.line}  [{f.severity}] {f.rule} → "
        f"{_redact(f.matched_value, severity=f.severity)}"
        for f in findings
    )


async def tool_scan_secrets(
    args: dict[str, Any],
    *,
    file_guard: FileGuardPort,
    tool_result_store: ToolResultStorePort | None = None,
) -> dict[str, Any]:
    """Scan ``paths`` for credential / PII patterns and report structurally."""
    raw_paths = args.get("paths")
    if raw_paths is not None and not isinstance(raw_paths, str):
        raise ToolError("scan_secrets: 'paths' must be a string")
    include_pii = args.get("include_pii", True)
    if not isinstance(include_pii, bool):
        raise ToolError("scan_secrets: 'include_pii' must be a boolean")
    verbose = bool(args.get("verbose", False))

    # Same multi-root convention as ``grep``: ';' separates targets so one
    # call can cover exactly the files just edited.
    entries = [
        part.strip()
        for part in (raw_paths or "").split(";")
        if part.strip()
    ]
    if not entries:
        entries = [str(default_cwd() or Path.cwd())]

    targets: list[Path] = []
    missing: list[str] = []
    for entry in entries:
        resolved = Path(resolve_under_workspace(entry))
        if await asyncio.to_thread(resolved.exists):
            targets.append(resolved)
        else:
            # One bad entry must not discard the good ones — with several
            # targets that would hide real findings behind a typo.
            missing.append(entry)

    if not targets:
        return _ok(
            "scan_secrets: nothing scanned — no target path exists "
            f"({', '.join(missing)}). This is a self-check tool; the "
            "pre-commit hook remains the independent final gate.",
            findings=[],
            finding_count=0,
            secret_count=0,
            pii_count=0,
            allowlisted_count=0,
            files_scanned=0,
            missing_paths=missing,
            truncated=False,
        )

    for target in targets:
        await file_guard.enforce_read(
            path=str(target), caller="ai_coding.tool.scan_secrets"
        )

    from qai.platform.tool_progress import emit_progress

    emit_progress(
        f"scan_secrets “{'; '.join(entries)[:60]}”…", "match"
    )

    reported, suppressed, files_scanned, walk_truncated = await asyncio.to_thread(
        _scan_targets, targets, include_pii=include_pii
    )

    # Secrets first so the actionable hits are never the ones elided by the cap.
    reported.sort(key=lambda f: (f.severity != "secret", f.path, f.line))
    secret_count = sum(1 for f in reported if f.severity == "secret")
    pii_count = len(reported) - secret_count

    visible = reported[:MAX_INPROMPT_FINDINGS]
    count_capped = len(reported) > len(visible)

    stored_path: str | None = None
    if count_capped and tool_result_store is not None:
        # ``force`` because the driver is a finding COUNT, not a byte size:
        # without it a 120-finding result whose text sits under the store
        # threshold would drop the elided hits with no retrieval path.
        try:
            preview = tool_result_store.store(
                _render(reported),
                tool_name="scan_secrets",
                context_hint="full_findings",
                force=True,
            )
        except Exception:  # noqa: BLE001 — persistence is best-effort
            preview = None
        if preview is not None and preview.stored:
            stored_path = preview.stored_path

    message = _build_message(
        secret_count=secret_count,
        pii_count=pii_count,
        files_scanned=files_scanned,
        scope=entries,
        include_pii=include_pii,
        allowlisted=len(suppressed),
        shown=len(visible),
        total=len(reported),
        stored_path=stored_path,
        missing=missing,
        walk_truncated=walk_truncated,
    )

    result = _ok(
        message,
        findings=[_as_dict(f) for f in visible],
        finding_count=len(reported),
        secret_count=secret_count,
        pii_count=pii_count,
        allowlisted_count=len(suppressed),
        files_scanned=files_scanned,
        output=_render(visible),
        truncated=count_capped or walk_truncated,
    )
    if missing:
        result["missing_paths"] = missing
    if stored_path is not None:
        result["stored_path"] = stored_path
    if verbose and suppressed:
        # Detail only on request: the count alone already tells the model that
        # something was exempted, and the full list is usually long and dull.
        result["allowlisted"] = [
            {
                "path": f.path,
                "line": f.line,
                "rule": f.rule,
                "severity": f.severity,
                "redacted_value": _redact(f.matched_value, severity=f.severity),
                "allow_reason": entry.reason,
            }
            for f, entry in suppressed[:MAX_INPROMPT_FINDINGS]
        ]
    return result


def _build_message(
    *,
    secret_count: int,
    pii_count: int,
    files_scanned: int,
    scope: list[str],
    include_pii: bool,
    allowlisted: int,
    shown: int,
    total: int,
    stored_path: str | None,
    missing: list[str],
    walk_truncated: bool,
) -> str:
    """One-line verdict plus every caveat the model needs to act correctly."""
    scope_text = "; ".join(scope)
    if total == 0:
        head = (
            f"scan_secrets: clean — no credential"
            f"{' or PII' if include_pii else ''} pattern matched across "
            f"{files_scanned} file(s) in {scope_text}"
        )
    else:
        parts: list[str] = []
        if secret_count:
            parts.append(f"{secret_count} probable secret(s)")
        if pii_count:
            parts.append(f"{pii_count} PII hit(s) (informational)")
        head = (
            f"scan_secrets: {', '.join(parts)} across {files_scanned} "
            f"file(s) in {scope_text}"
        )

    notes: list[str] = []
    if allowlisted:
        notes.append(
            f"{allowlisted} match(es) suppressed by the allow-list "
            "(placeholder / docs / lock-file rules); pass verbose=true to see "
            "them"
        )
    if not include_pii:
        notes.append("PII rules disabled for this call (include_pii=false)")
    if shown < total:
        note = f"showing the first {shown} of {total} findings"
        if stored_path:
            note += f"; full list: read(path='{stored_path}')"
        else:
            note += "; narrow 'paths' to see the rest"
        notes.append(note)
    if missing:
        notes.append(f"path(s) not found and skipped: {', '.join(missing)}")
    if walk_truncated:
        notes.append(
            f"the directory walk stopped at its {MAX_FILES_SCANNED}-file "
            "budget, so this is a PARTIAL scan — pass narrower 'paths' for a "
            "complete answer"
        )
    notes.append(
        "this is a SELF-CHECK: it blocks nothing, and the pre-commit hook "
        "(scripts/ci/check_no_secrets.py) is the independent final gate"
    )
    return head + " — " + "; ".join(notes)
