# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai run`` subcommands — App Builder run history + worker status.

Desktop App Plan §2.1.1 group E. Thin wrappers over the
``qai.app_builder`` use cases that read the run history aggregate, plus
``qai run worker status`` for the sticky-worker pool snapshot.

Worker command placement
------------------------
The plan §2.1.1 lists ``qai worker status`` as a top-level verb, but
:func:`apps.cli.__main__.build_parser` enumerates command groups in
``_D2_GROUPS`` and group registration runs **inside** the ``run`` group's
own subparser scope — there is no documented affordance to register a
sibling top-level verb from a non-``__main__`` module without modifying
``__main__.py`` (forbidden by this PR's file-domain). The pragmatic
landing is ``qai run worker status``: same use case, same wire shape,
operator types one extra word. A future PR that splits worker into its
own ``_D2_GROUPS`` entry can deprecate this nesting without changing the
underlying use-case wiring.

Behavioural contract
--------------------
* Every read subcommand emits one JSON object on stdout (uniform with
  :mod:`apps.cli.commands.config` / :mod:`apps.cli.commands.pack`).
* ``qai run list`` defaults to the use case's full snapshot
  (``limit=200``); ``--model <id>`` switches to the per-model repository
  read (``RunRepositoryPort.list_by_model``) so an operator can scope a
  history dump to a single Pack without paginating through unrelated
  runs. ``--limit N`` caps either path.
* ``qai run delete`` calls :class:`DeleteRunHistoryUseCase`. Unlike
  ``qai pack delete`` we do not gate on ``--yes``: a single run row
  is reversible by re-running the model, so the same script-friendly
  default ``qai conv delete`` will use applies here too.
* ``qai run cancel`` accepts an optional ``--reason`` echoed into the
  ``RunCancelledEvent`` payload so audits can attribute the cancellation.
* ``qai run export`` writes Markdown to stdout by default; ``--out
  <path>`` writes to a file (text mode, UTF-8) and prints a one-line
  status object. Exporting to file is the V1 operator workflow.
* ``qai run feedback`` requires ``--rating <1-5>`` to satisfy the
  :class:`SubmitFeedbackCommand` validation; ``--note`` is optional.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from apps.api.di import Container
from apps.cli._runtime import run_use_case

__all__ = [
    "register",
    "cmd_run_list",
    "cmd_run_show",
    "cmd_run_delete",
    "cmd_run_cancel",
    "cmd_run_artifacts",
    "cmd_run_export",
    "cmd_run_feedback",
    "cmd_run_bench",
    "cmd_run_worker_status",
]


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach ``qai run`` subparsers."""

    run = subparsers.add_parser(
        "run",
        help="App Builder run history (list / show / delete / cancel / artifacts / export / feedback / bench / worker)",
        description=(
            "Inspect and operate on the App Builder run history aggregate. "
            "All subcommands read or mutate ``app_builder_run_*`` rows; "
            "the long-running streaming endpoints (``POST /runs`` etc.) "
            "are intentionally not surfaced here — those require the API "
            "server (see ``qai api``)."
        ),
    )
    run_sub = run.add_subparsers(
        dest="run_command", required=True, metavar="<subcommand>"
    )

    # ── list ────────────────────────────────────────────────────────
    list_p = run_sub.add_parser(
        "list",
        help="list runs (newest first); optional --model filter, --limit cap",
    )
    list_p.add_argument(
        "--model",
        dest="model_id",
        default=None,
        metavar="<id>",
        help="filter to runs of this Pack id only (defaults to all)",
    )
    list_p.add_argument(
        "--limit",
        type=int,
        default=200,
        metavar="<n>",
        help="maximum number of runs to return (default 200; must be > 0)",
    )
    list_p.set_defaults(handler=cmd_run_list)

    # ── show / delete / cancel ──────────────────────────────────────
    show_p = run_sub.add_parser(
        "show", help="print the Run aggregate (status / artifacts / inputs) for <run-id>",
    )
    show_p.add_argument("run_id", help="ULID returned by ``qai run list``")
    show_p.set_defaults(handler=cmd_run_show)

    delete_p = run_sub.add_parser(
        "delete", help="delete the run history row for <run-id>",
    )
    delete_p.add_argument("run_id", help="ULID of the run to delete")
    delete_p.set_defaults(handler=cmd_run_delete)

    cancel_p = run_sub.add_parser(
        "cancel",
        help="mark a still-running Run as CANCELLED (publishes RunCancelledEvent)",
    )
    cancel_p.add_argument("run_id", help="ULID of the run to cancel")
    cancel_p.add_argument(
        "--reason",
        default=None,
        metavar="<text>",
        help="optional human-readable reason echoed into the audit event",
    )
    cancel_p.set_defaults(handler=cmd_run_cancel)

    # ── artifacts / export ──────────────────────────────────────────
    art_p = run_sub.add_parser(
        "artifacts", help="list artifact descriptors (path / size / kind / checksum) for <run-id>",
    )
    art_p.add_argument("run_id", help="ULID of the run to inspect")
    art_p.set_defaults(handler=cmd_run_artifacts)

    export_p = run_sub.add_parser(
        "export",
        help="render <run-id> as Markdown (stdout, or to <path> with --out)",
    )
    export_p.add_argument("run_id", help="ULID of the run to export")
    export_p.add_argument(
        "--out",
        type=Path,
        default=None,
        metavar="<path.md>",
        help="write Markdown to this file (UTF-8); without --out prints to stdout",
    )
    export_p.set_defaults(handler=cmd_run_export)

    # ── feedback ────────────────────────────────────────────────────
    feedback_p = run_sub.add_parser(
        "feedback",
        help="submit a 1-5 star rating + optional note against <run-id>",
    )
    feedback_p.add_argument("run_id", help="ULID of the run being rated")
    feedback_p.add_argument(
        "--rating",
        type=int,
        required=True,
        choices=[1, 2, 3, 4, 5],
        help="Likert 1..5 (5 = 👍, 1 = 👎); validated against the use case",
    )
    feedback_p.add_argument(
        "--note",
        default="",
        metavar="<text>",
        help="optional free-form note (≤4000 chars per use case validation)",
    )
    feedback_p.set_defaults(handler=cmd_run_feedback)

    # ── bench ───────────────────────────────────────────────────────
    bench_p = run_sub.add_parser(
        "bench", help="print the BenchmarkRecord for <benchmark-id>",
    )
    bench_p.add_argument(
        "benchmark_id",
        help="benchmark id returned by ``POST /api/app-builder/benchmark``",
    )
    bench_p.set_defaults(handler=cmd_run_bench)

    # ── worker (nested) ─────────────────────────────────────────────
    worker_p = run_sub.add_parser(
        "worker",
        help="sticky-worker pool inspection",
        description=(
            "Sticky-worker pool snapshot. Lives under ``run`` because the "
            "argparse subparser registry doesn't let us add a sibling "
            "top-level verb from this module — see module docstring."
        ),
    )
    worker_sub = worker_p.add_subparsers(
        dest="run_worker_command", required=True, metavar="<subcommand>"
    )
    worker_status_p = worker_sub.add_parser(
        "status",
        help="print the WorkerPoolStatus snapshot (sticky workers, idle/busy)",
    )
    worker_status_p.set_defaults(handler=cmd_run_worker_status)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emit(payload: Any) -> None:
    """JSON-encode ``payload`` to stdout (uniform with the rest of the CLI).

    Writes UTF-8 bytes directly to ``sys.stdout.buffer`` to bypass the
    Windows cp1252 / charmap default codec — see
    :func:`apps.cli.commands.pack._emit` for the rationale.
    """

    body = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    sys.stdout.buffer.write(body.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def _json_default(obj: Any) -> Any:
    """Best-effort JSON fallback (mirrors :func:`apps.cli.commands.pack._json_default`)."""

    from datetime import datetime  # noqa: PLC0415

    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "value") and obj.__class__.__name__.endswith(
        ("Status", "Action", "State", "Kind", "Algorithm")
    ):
        return obj.value
    if hasattr(obj, "__dataclass_fields__"):
        return {
            name: getattr(obj, name)
            for name in obj.__dataclass_fields__  # type: ignore[attr-defined]
            if hasattr(obj, name)
        }
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def _resolved_repo_root(args: argparse.Namespace) -> Path | None:
    return getattr(args, "repo_root", None)


def _resolved_config_file(args: argparse.Namespace) -> Path | None:
    return getattr(args, "config_file", None)


def _run_to_dict(run_obj: Any) -> dict[str, Any]:
    """Mirror of :func:`interfaces.http.routes.app_builder._dto._run_to_dto`.

    Re-implemented locally so the CLI doesn't import fastapi-bound DTOs.
    Wire shape stays identical to the HTTP route response so a script
    that consumes either surface can use the same ``jq`` query.
    """

    return {
        "id": str(run_obj.id),
        "model_id": str(run_obj.model_id),
        "status": run_obj.status.value,
        "created_at": run_obj.created_at.isoformat(),
        "started_at": run_obj.started_at.isoformat() if run_obj.started_at else None,
        "finished_at": (
            run_obj.finished_at.isoformat() if run_obj.finished_at else None
        ),
        "inputs": dict(run_obj.inputs),
        "artifacts": [
            {
                "path": a.path,
                "size_bytes": a.size_bytes,
                "kind": a.kind.value,
                "checksum": a.checksum.value if a.checksum is not None else None,
            }
            for a in run_obj.artifacts
        ],
        "error_message": run_obj.error_message,
        "error_code": getattr(run_obj, "error_code", None),
    }


def _artifact_to_dict(artifact: Any) -> dict[str, Any]:
    """Map a single :class:`Artifact` to its wire shape."""

    return {
        "path": artifact.path,
        "size_bytes": artifact.size_bytes,
        "kind": artifact.kind.value,
        "checksum": artifact.checksum.value if artifact.checksum is not None else None,
    }


# ---------------------------------------------------------------------------
# handlers — list / show / delete / cancel
# ---------------------------------------------------------------------------


def cmd_run_list(args: argparse.Namespace) -> int:
    """``qai run list [--model <id>] [--limit N]`` handler."""

    if args.limit <= 0:
        sys.stderr.write("qai run list: --limit must be > 0\n")
        return 2

    async def _go(c: Container) -> dict[str, Any]:
        if args.model_id is not None:
            # Per-model fast path: avoid the use case's O(models) fan-out
            # when we already know the scope.
            from qai.app_builder.domain.value_objects import (  # noqa: PLC0415
                AppModelId,
            )

            runs = await c.app_builder.run_repository.list_by_model(
                AppModelId(value=args.model_id),
                limit=args.limit,
            )
        else:
            uc = c.app_builder.list_runs_use_case
            if uc is None:
                raise RuntimeError(
                    "ListRunsUseCase is not wired (stripped-down container?)"
                )
            runs = await uc.execute(limit=args.limit, offset=0)
        return {"items": [_run_to_dict(r) for r in runs]}

    try:
        payload = run_use_case(
            _go,
            config_file=_resolved_config_file(args),
            repo_root=_resolved_repo_root(args),
        )
    except ValueError as exc:
        sys.stderr.write(f"invalid argument: {exc}\n")
        return 2
    _emit(payload)
    return 0


def cmd_run_show(args: argparse.Namespace) -> int:
    """``qai run show <run-id>`` handler."""

    from qai.app_builder.domain.value_objects import RunId  # noqa: PLC0415

    async def _go(c: Container) -> dict[str, Any]:
        run_obj = await c.app_builder.get_run_use_case.execute(
            run_id=RunId(value=args.run_id),
        )
        return _run_to_dict(run_obj)

    try:
        payload = run_use_case(
            _go,
            config_file=_resolved_config_file(args),
            repo_root=_resolved_repo_root(args),
        )
    except ValueError as exc:
        sys.stderr.write(f"invalid run id: {exc}\n")
        return 2
    _emit(payload)
    return 0


def cmd_run_delete(args: argparse.Namespace) -> int:
    """``qai run delete <run-id>`` handler."""

    from qai.app_builder.domain.value_objects import RunId  # noqa: PLC0415

    async def _go(c: Container) -> None:
        uc = c.app_builder.delete_run_history_use_case
        if uc is None:
            raise RuntimeError(
                "DeleteRunHistoryUseCase is not wired (stripped-down container?)"
            )
        await uc.execute(RunId(value=args.run_id))
        return None

    try:
        run_use_case(
            _go,
            config_file=_resolved_config_file(args),
            repo_root=_resolved_repo_root(args),
        )
    except ValueError as exc:
        sys.stderr.write(f"invalid run id: {exc}\n")
        return 2
    _emit({"deleted": args.run_id})
    return 0


def cmd_run_cancel(args: argparse.Namespace) -> int:
    """``qai run cancel <run-id> [--reason <text>]`` handler."""

    from qai.app_builder.domain.value_objects import RunId  # noqa: PLC0415

    async def _go(c: Container) -> None:
        await c.app_builder.cancel_run_use_case.execute(
            run_id=RunId(value=args.run_id),
            reason=args.reason,
        )
        return None

    try:
        run_use_case(
            _go,
            config_file=_resolved_config_file(args),
            repo_root=_resolved_repo_root(args),
        )
    except ValueError as exc:
        sys.stderr.write(f"invalid run id: {exc}\n")
        return 2
    _emit({"cancelled": args.run_id, "reason": args.reason})
    return 0


# ---------------------------------------------------------------------------
# handlers — artifacts / export
# ---------------------------------------------------------------------------


def cmd_run_artifacts(args: argparse.Namespace) -> int:
    """``qai run artifacts <run-id>`` handler."""

    from qai.app_builder.domain.value_objects import RunId  # noqa: PLC0415

    async def _go(c: Container) -> dict[str, Any]:
        artifacts = await c.app_builder.list_run_artifacts_use_case.execute(
            run_id=RunId(value=args.run_id),
        )
        return {"items": [_artifact_to_dict(a) for a in artifacts]}

    try:
        payload = run_use_case(
            _go,
            config_file=_resolved_config_file(args),
            repo_root=_resolved_repo_root(args),
        )
    except ValueError as exc:
        sys.stderr.write(f"invalid run id: {exc}\n")
        return 2
    _emit(payload)
    return 0


def cmd_run_export(args: argparse.Namespace) -> int:
    """``qai run export <run-id> [--out <path.md>]`` handler.

    Default: print the Markdown content to stdout (no JSON wrapper —
    Markdown is itself the payload, and shell ``>`` redirection is the
    operator-friendly way to capture it).

    ``--out``: write the Markdown to ``<path>`` (UTF-8, newline-LF) and
    emit a status JSON object so a downstream pipe can confirm the
    write committed.
    """

    from qai.app_builder.domain.value_objects import RunId  # noqa: PLC0415

    async def _go(c: Container) -> str:
        uc = c.app_builder.export_run_markdown_use_case
        if uc is None:
            raise RuntimeError(
                "ExportRunMarkdownUseCase is not wired (stripped-down container?)"
            )
        return await uc.execute(run_id=RunId(value=args.run_id))

    try:
        markdown = run_use_case(
            _go,
            config_file=_resolved_config_file(args),
            repo_root=_resolved_repo_root(args),
        )
    except ValueError as exc:
        sys.stderr.write(f"invalid run id: {exc}\n")
        return 2

    if args.out is not None:
        # ``newline="\n"`` keeps the on-disk Markdown LF-only on Windows
        # so the file round-trips unchanged through git / cross-platform
        # editors. Write atomically via a sibling tempfile would be
        # nicer but is overkill for an explicit CLI write.
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown, encoding="utf-8", newline="\n")
        _emit({"written": str(args.out), "bytes": len(markdown.encode("utf-8"))})
        return 0

    sys.stdout.buffer.write(markdown.encode("utf-8"))
    if not markdown.endswith("\n"):
        sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()
    return 0


# ---------------------------------------------------------------------------
# handlers — feedback / bench
# ---------------------------------------------------------------------------


def cmd_run_feedback(args: argparse.Namespace) -> int:
    """``qai run feedback <run-id> --rating <1-5> [--note <text>]`` handler."""

    from qai.app_builder.application.use_cases.submit_feedback import (  # noqa: PLC0415
        SubmitFeedbackCommand,
    )
    from qai.app_builder.domain.value_objects import RunId  # noqa: PLC0415

    async def _go(c: Container) -> dict[str, Any]:
        uc = c.app_builder.submit_feedback_use_case
        if uc is None:
            raise RuntimeError(
                "SubmitFeedbackUseCase is not wired (stripped-down container?)"
            )
        feedback = await uc.execute(
            SubmitFeedbackCommand(
                run_id=RunId(value=args.run_id),
                rating=args.rating,
                text=args.note or "",
            )
        )
        return {
            "id": feedback.id,
            "run_id": str(feedback.run_id),
            "rating": feedback.rating,
            "text": feedback.text,
            "created_at": feedback.created_at.isoformat(),
        }

    try:
        payload = run_use_case(
            _go,
            config_file=_resolved_config_file(args),
            repo_root=_resolved_repo_root(args),
        )
    except ValueError as exc:
        sys.stderr.write(f"invalid argument: {exc}\n")
        return 2
    _emit(payload)
    return 0


def cmd_run_bench(args: argparse.Namespace) -> int:
    """``qai run bench <benchmark-id>`` handler."""

    async def _go(c: Container) -> Any:
        uc = c.app_builder.get_benchmark_use_case
        if uc is None:
            raise RuntimeError(
                "GetBenchmarkUseCase is not wired (stripped-down container?)"
            )
        return await uc.execute(args.benchmark_id)

    record = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(record)
    return 0


# ---------------------------------------------------------------------------
# handlers — worker
# ---------------------------------------------------------------------------


def cmd_run_worker_status(args: argparse.Namespace) -> int:
    """``qai run worker status`` handler.

    Wraps :class:`GetWorkerStatusUseCase`. When no daemon / sticky-worker
    host is running (the CLI never starts one — see
    :mod:`apps.cli._runtime` notes) the use case returns the static
    fallback adapter's snapshot; the exit code is still 0 because
    "no worker host" is a valid state, not an error.
    """

    async def _go(c: Container) -> Any:
        return await c.app_builder.get_worker_status_use_case.execute()

    status = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(status)
    return 0
