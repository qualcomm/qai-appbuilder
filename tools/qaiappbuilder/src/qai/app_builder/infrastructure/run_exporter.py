# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Markdown run-report renderer (PR-094 §17.5 #14).

Restores the legacy ``backend/app_builder/run_exporter.py`` (188 LOC) as an
infrastructure adapter. The renderer is a pure function over a
:class:`qai.app_builder.domain.run.Run` aggregate; the use case
(:class:`qai.app_builder.application.use_cases.export_run_markdown.ExportRunMarkdownUseCase`)
loads the Run via :class:`RunRepositoryPort` and forwards the result here.

The rendered Markdown is identical in shape to the legacy export so the
existing front-end "Download report" UI keeps working without translation:

* ``# Run Report: <model_id>`` heading with run id / status / timing;
* ``## Inputs`` — bullet list, values truncated at 200 chars;
* ``## Output`` — fenced JSON block, truncated at 5_000 chars;
* ``## Artifacts`` — bullet list of relative paths + sizes (NEW in S3);
* ``## Error`` — only emitted when the run failed.

Artifact summary is NEW (legacy stored ``output`` payload directly inside
the history record; the S3 model attaches artifacts as a separate
collection on the :class:`Run` aggregate). The S9 audit (§3.3 A-14)
required parity with the legacy MD format; we surface artifacts as a
distinct section so the parity is preserved without forcing the legacy
``output`` key onto the new Run model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from qai.app_builder.domain.run import Run, RunStatus

__all__ = ["render_markdown_report", "MarkdownRunExporter"]


_MAX_INPUT_VALUE_CHARS = 200
_MAX_OUTPUT_CHARS = 5000


def _safe_dumps(obj: Any) -> str:
    try:
        return json.dumps(obj, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(obj)


def _status_emoji(status: RunStatus) -> str:
    return {
        RunStatus.COMPLETED: "✅",
        RunStatus.FAILED: "❌",
        RunStatus.CANCELLED: "⚠️",
        RunStatus.RUNNING: "⏳",
        RunStatus.STREAMING: "⏳",
        RunStatus.PENDING: "⏸",
    }.get(status, "❓")


def render_markdown_report(run: Run) -> str:
    """Render ``run`` as a Markdown report.

    Pure function — no I/O, no clock injection (the "exported at" footer
    intentionally uses ``datetime.now(timezone.utc)`` so the artifact has
    a real-world timestamp; tests that need determinism should freeze
    time at the framework boundary).
    """
    lines: list[str] = []

    # ── Header ─────────────────────────────────────────────────────
    lines.append(f"# Run Report: {run.model_id}")
    lines.append("")
    lines.append(f"- **Run ID**: `{run.id}`")
    lines.append(f"- **Model**: `{run.model_id}`")
    lines.append(f"- **Status**: {_status_emoji(run.status)} {run.status.value}")
    if run.created_at is not None:
        lines.append(f"- **Created**: {run.created_at.isoformat()}")
    if run.started_at is not None:
        lines.append(f"- **Started**: {run.started_at.isoformat()}")
    if run.finished_at is not None:
        lines.append(f"- **Finished**: {run.finished_at.isoformat()}")
    if run.started_at is not None and run.finished_at is not None:
        try:
            elapsed = (
                run.finished_at - run.started_at
            ).total_seconds()
            lines.append(f"- **Elapsed**: {elapsed:.2f}s")
        except (TypeError, ValueError):  # pragma: no cover -- defensive
            pass
    lines.append("")

    # ── Inputs ─────────────────────────────────────────────────────
    if run.inputs:
        lines.append("## Inputs")
        lines.append("")
        for k, v in run.inputs.items():
            v_str = str(v)
            if len(v_str) > _MAX_INPUT_VALUE_CHARS:
                v_str = v_str[:_MAX_INPUT_VALUE_CHARS] + "..."
            lines.append(f"- **{k}**: `{v_str}`")
        lines.append("")

    # ── Output (synthesized from artifacts/error for the new model) ──
    output_payload: dict[str, Any] = {}
    if run.error_message:
        output_payload["error"] = run.error_message
    if run.artifacts:
        output_payload["artifacts"] = [
            {"path": a.path, "size_bytes": a.size_bytes, "kind": a.kind.value}
            for a in run.artifacts
        ]
    if output_payload:
        lines.append("## Output")
        lines.append("")
        lines.append("```json")
        out_str = _safe_dumps(output_payload)
        if len(out_str) > _MAX_OUTPUT_CHARS:
            out_str = out_str[:_MAX_OUTPUT_CHARS] + "\n... (truncated)"
        lines.append(out_str)
        lines.append("```")
        lines.append("")

    # ── Artifacts (separate human-readable list) ─────────────────────
    if run.artifacts:
        lines.append("## Artifacts")
        lines.append("")
        for a in run.artifacts:
            checksum = (
                f" (sha256: `{a.checksum.value[:16]}…`)"
                if a.checksum is not None
                else ""
            )
            lines.append(
                f"- `{a.path}` — {a.size_bytes} bytes — kind: "
                f"`{a.kind.value}`{checksum}"
            )
        lines.append("")

    # ── Error ──────────────────────────────────────────────────────
    if run.status == RunStatus.FAILED and run.error_message:
        lines.append("## Error")
        lines.append("")
        lines.append(f"- {run.error_message}")
        lines.append("")
    elif run.status == RunStatus.CANCELLED and run.error_message:
        lines.append("## Cancellation Reason")
        lines.append("")
        lines.append(f"- {run.error_message}")
        lines.append("")

    # ── Footer ─────────────────────────────────────────────────────
    lines.append("---")
    lines.append(
        f"_Exported at {datetime.now(timezone.utc).isoformat()}_"
    )
    return "\n".join(lines)


class MarkdownRunExporter:
    """Concrete adapter implementing the application-layer
    :class:`qai.app_builder.application.ports.RunMarkdownRendererPort`.

    Wraps the module-level :func:`render_markdown_report` pure function
    in a class so the production DI (``apps/api/_app_builder_di.py``)
    can inject it into :class:`ExportRunMarkdownUseCase` without the
    application layer importing this infrastructure module — which the
    ``layered-app_builder`` import-linter contract forbids.
    """

    __slots__ = ()

    def render(self, run: Run) -> str:
        """Return the Markdown report for ``run``.

        Delegates to :func:`render_markdown_report` so the rendering
        logic stays in one place; subclasses / alternate adapters may
        override this method without touching the pure function.
        """
        return render_markdown_report(run)
