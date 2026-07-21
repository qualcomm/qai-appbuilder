# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai service`` subcommands — control the local GenieAPIService daemon.

Desktop App Plan §2.1.1 group B. Thin wrappers over use cases in
:mod:`qai.model_runtime.application.use_cases`; no business logic lives
here (the CLI is an adapter sibling to the HTTP routes that share the
same use cases).

Daemon-affecting commands & in-process Container (P2 deferral)
--------------------------------------------------------------
Per Desktop App Plan §2.6, lifecycle commands such as ``service start`` /
``stop`` / ``load-model`` ideally route through ``127.0.0.1:<port>/api/...``
to the running API server because the *running daemon* owns the
``InferenceService`` adapter holding the live subprocess handle. The D2
implementation lands the **in-process** path first: every handler builds
a fresh :class:`Container` via :func:`apps.cli._runtime.cli_container`
and calls the use case directly. This means:

* ``qai service status`` reports an accurate "stopped" view when no
  daemon is running anywhere — it consults the same status logic the
  HTTP route uses.
* ``qai service start`` from a separate CLI process spawns its **own**
  GenieAPIService child; this is correct from a single-process
  standpoint but does NOT change the running ``apps.api`` server's
  in-memory adapter state. Operators wanting to drive the running
  server's daemon should currently use the HTTP API directly. A P2
  follow-up will add a probe step: if the API server is reachable on
  ``127.0.0.1:<port>``, route the call to it; otherwise fall back to
  in-process.

Output convention
-----------------
* Business output → ``stdout`` as JSON (``ensure_ascii=False``, indent
  2) so ``qai service status | jq .pid`` works.
* Diagnostic / error output → ``stderr`` (handled by
  :mod:`apps.cli.__main__` for unexpected exceptions; this module only
  writes to stderr for the explicit ``invalid JSON`` case in
  ``service config set``).
* Exit codes: ``0`` success, ``1`` business error (raised exception,
  caught by the dispatcher), ``2`` usage error (argparse / malformed
  JSON arg).

The ``[<instance-id>]`` argument shown in the plan is reserved for
future multi-daemon support; the current ``model_runtime`` BC manages a
single daemon and the use cases do not accept an instance selector, so
the parsers accept the argument as a positional-optional but ignore it
when present (with a one-line stderr note so a future operator hint
isn't silently swallowed).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
from pathlib import Path
from typing import Any

from apps.api.di import Container
from apps.cli._runtime import run_use_case

__all__ = [
    "register",
    "cmd_service_status",
    "cmd_service_probe",
    "cmd_service_start",
    "cmd_service_stop",
    "cmd_service_load_model",
    "cmd_service_models",
    "cmd_service_logs",
    "cmd_service_logs_clear",
    "cmd_service_path",
    "cmd_service_config_get",
    "cmd_service_config_set",
]


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``qai service`` subtree to the top-level parser."""

    service = subparsers.add_parser(
        "service",
        help="control the local GenieAPIService daemon (model_runtime)",
        description=(
            "Read or change the state of the local inference daemon "
            "(GenieAPIService). All commands operate against the in-process "
            "Container in D2; a future P2 will probe the running API server "
            "and route daemon-affecting calls over HTTP when one is up."
        ),
    )
    service_sub = service.add_subparsers(
        dest="service_command", required=True, metavar="<subcommand>"
    )

    # status / probe / start / stop accept an optional positional
    # ``instance-id`` reserved for future multi-daemon support.
    status_p = service_sub.add_parser(
        "status",
        help="print pid / uptime / loaded model / port / memory + path warnings",
    )
    status_p.add_argument(
        "instance_id",
        nargs="?",
        default=None,
        help="reserved for future multi-daemon support; ignored today",
    )
    status_p.set_defaults(handler=cmd_service_status)

    probe_p = service_sub.add_parser(
        "probe",
        help="quick reachability check; prints reachable + loaded model",
    )
    probe_p.add_argument("instance_id", nargs="?", default=None)
    probe_p.add_argument(
        "--host",
        default=None,
        help="probe an arbitrary host (V1 'Test connection' button parity)",
    )
    probe_p.add_argument(
        "--port", type=int, default=None, help="probe an arbitrary port"
    )
    probe_p.set_defaults(handler=cmd_service_probe)

    start_p = service_sub.add_parser(
        "start", help="start the local GenieAPIService daemon"
    )
    start_p.add_argument("instance_id", nargs="?", default=None)
    start_p.add_argument(
        "--model",
        dest="model_name",
        default=None,
        help="load this model immediately on start",
    )
    start_p.add_argument(
        "--port", type=int, default=None, help="bind to this port"
    )
    start_p.add_argument(
        "--loglevel",
        type=int,
        default=None,
        help="GenieAPIService -d log level (1..5)",
    )
    start_p.set_defaults(handler=cmd_service_start)

    stop_p = service_sub.add_parser(
        "stop", help="stop the running GenieAPIService daemon"
    )
    stop_p.add_argument("instance_id", nargs="?", default=None)
    stop_p.set_defaults(handler=cmd_service_stop)

    load_p = service_sub.add_parser(
        "load-model", help="load/switch a model in the running daemon"
    )
    load_p.add_argument("model_name", help="model directory name (V1 parity)")
    load_p.set_defaults(handler=cmd_service_load_model)

    models_p = service_sub.add_parser(
        "models", help="list locally-available models on disk"
    )
    models_p.add_argument(
        "--models-root",
        dest="models_root",
        default=None,
        help="override the models-root scan path (default: forge.config)",
    )
    models_p.set_defaults(handler=cmd_service_models)

    logs_p = service_sub.add_parser(
        "logs", help="print recent daemon log lines (one per line, plain text)"
    )
    logs_p.add_argument(
        "--lines",
        type=int,
        default=None,
        help="cap the number of trailing lines (default: full retained buffer)",
    )
    logs_p.set_defaults(handler=cmd_service_logs)

    logs_clear_p = service_sub.add_parser(
        "logs-clear", help="clear the daemon log buffer (V1 parity)"
    )
    logs_clear_p.set_defaults(handler=cmd_service_logs_clear)

    path_p = service_sub.add_parser(
        "path",
        help="print the resolved GenieAPIService install directory",
    )
    path_p.set_defaults(handler=cmd_service_path)

    # service config get / set
    config_p = service_sub.add_parser(
        "config",
        help="read/write GenieAPIService service_config.json",
    )
    config_sub = config_p.add_subparsers(
        dest="service_config_command", required=True, metavar="<subcommand>"
    )
    cfg_get = config_sub.add_parser(
        "get", help="print the merged service_config.json (api_key masked)"
    )
    cfg_get.set_defaults(handler=cmd_service_config_get)
    cfg_set = config_sub.add_parser(
        "set",
        help=(
            "deep-merge the given JSON object into service_config.json; "
            "api_key fields are routed to the SecretStore"
        ),
    )
    cfg_set.add_argument(
        "value",
        help=(
            "JSON object to deep-merge into service_config.json (top-level "
            "must be an object)"
        ),
    )
    cfg_set.set_defaults(handler=cmd_service_config_set)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emit_json(value: Any) -> None:
    """Pretty-print ``value`` to stdout as JSON.

    ``default=str`` covers the few datetime / Path objects that surface
    from the model_runtime layer; ``ensure_ascii=False`` keeps Chinese
    metadata (e.g. ``model_name`` of a CN model directory) un-escaped so
    operators see a readable wire form.
    """

    sys.stdout.write(
        json.dumps(value, ensure_ascii=False, indent=2, default=str)
    )
    sys.stdout.write("\n")
    sys.stdout.flush()


def _runtime_kwargs(args: argparse.Namespace) -> dict[str, Path | None]:
    """Extract the standard ``--repo-root`` / ``--config`` overrides."""
    return {
        "repo_root": getattr(args, "repo_root", None),
        "config_file": getattr(args, "config_file", None),
    }


def _warn_instance_ignored(args: argparse.Namespace) -> None:
    """Emit a one-liner if the operator passed ``instance_id``.

    The ``model_runtime`` BC manages a single daemon today; the argument
    is parsed so the surface stays stable for a future multi-daemon
    refactor, but ignoring it silently would mask operator intent.
    """
    instance_id = getattr(args, "instance_id", None)
    if instance_id:
        sys.stderr.write(
            f"qai service: ignoring instance-id={instance_id!r} "
            "(single-daemon BC; reserved for future multi-daemon support)\n"
        )


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def cmd_service_status(args: argparse.Namespace) -> int:
    """``qai service status [<instance-id>]``."""

    _warn_instance_ignored(args)

    async def _go(c: Container) -> dict[str, Any]:
        return await c.model_runtime.get_status_use_case.execute()

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_service_probe(args: argparse.Namespace) -> int:
    """``qai service probe [<instance-id>] [--host <h>] [--port <p>]``."""

    _warn_instance_ignored(args)

    host: str | None = args.host
    port: int | None = args.port

    async def _go(c: Container) -> dict[str, Any]:
        return await c.model_runtime.probe_service_use_case.execute(
            host=host, port=port
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_service_start(args: argparse.Namespace) -> int:
    """``qai service start [<instance-id>] [--model <m>] [--port <p>] [--loglevel N]``.

    Limitation (P2 follow-up): in-process start spawns a daemon owned by
    THIS short-lived CLI process; the use case returns ``{"status":
    "starting"}`` immediately but the child will be reaped when the CLI
    exits because no supervisor remains. Operators driving a running
    ``apps.api`` server should use its HTTP route until the daemon-HTTP
    routing lands.
    """

    _warn_instance_ignored(args)
    model_name: str | None = args.model_name
    port: int | None = args.port
    loglevel: int | None = args.loglevel

    async def _go(c: Container) -> dict[str, str]:
        return await c.model_runtime.start_service_use_case.execute(
            model_name=model_name, port=port, loglevel=loglevel
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_service_stop(args: argparse.Namespace) -> int:
    """``qai service stop [<instance-id>]``."""

    _warn_instance_ignored(args)

    async def _go(c: Container) -> dict[str, str]:
        return await c.model_runtime.stop_service_use_case.execute()

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_service_load_model(args: argparse.Namespace) -> int:
    """``qai service load-model <name>``."""

    name: str = args.model_name

    async def _go(c: Container) -> dict[str, str]:
        return await c.model_runtime.load_model_use_case.execute(name)

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_service_models(args: argparse.Namespace) -> int:
    """``qai service models [--models-root <path>]``."""

    models_root: str | None = args.models_root

    async def _go(c: Container) -> Any:
        items = await c.model_runtime.list_models_use_case.execute(
            models_root=models_root
        )
        # ``ModelInfo`` is a dataclass; lift to plain dicts for JSON.
        return [
            dataclasses.asdict(m) if dataclasses.is_dataclass(m) else m
            for m in items
        ]

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_service_logs(args: argparse.Namespace) -> int:
    """``qai service logs [--lines N]`` — print one log line per stdout row.

    Logs are intentionally NOT JSON-wrapped here (V1 parity: each line is
    already a structured ``[lvl] msg`` text record). ``--lines`` caps the
    tail; absent it prints the whole retained buffer.
    """

    lines_cap: int | None = args.lines

    async def _go(c: Container) -> list[str]:
        return await c.model_runtime.get_logs_use_case.execute()

    lines = run_use_case(_go, **_runtime_kwargs(args))
    if lines_cap is not None and lines_cap >= 0:
        lines = lines[-lines_cap:]
    for line in lines:
        sys.stdout.write(line.rstrip("\r\n"))
        sys.stdout.write("\n")
    sys.stdout.flush()
    return 0


def cmd_service_logs_clear(args: argparse.Namespace) -> int:
    """``qai service logs-clear``."""

    async def _go(c: Container) -> dict[str, object]:
        return await c.model_runtime.clear_logs_use_case.execute()

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_service_path(args: argparse.Namespace) -> int:
    """``qai service path`` — print install directory only (do NOT open it).

    The use case is named ``OpenServiceDirUseCase`` for V1 parity, but
    only resolves the path. The HTTP route additionally calls
    ``os.startfile`` on the operator's behalf; the CLI should never
    spawn an explorer window from a one-shot command — print the path so
    the operator can pipe it to ``explorer.exe`` / ``code`` / ``cd`` as
    they choose.
    """

    async def _go(c: Container) -> str:
        return await c.model_runtime.open_service_dir_use_case.execute()

    path = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json({"install_dir": path})
    return 0


def cmd_service_config_get(args: argparse.Namespace) -> int:
    """``qai service config get`` — return ``{"config": ..., "meta": ...}``.

    Surface error: when the platform ports needed for the service-config
    repository are absent (very rare — only minimal test containers),
    the use case is ``None`` on the namespace; raise a clear error so
    the dispatcher exits 1 with an actionable message instead of an
    AttributeError trace.
    """

    async def _go(c: Container) -> dict[str, Any]:
        uc = c.model_runtime.get_service_config_use_case
        if uc is None:
            raise RuntimeError(
                "service-config repository is not wired on this container "
                "(missing data_paths or secret_store); cannot read "
                "service_config.json"
            )
        return await uc.execute()

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_service_config_set(args: argparse.Namespace) -> int:
    """``qai service config set <json>`` — deep-merge into service_config.json.

    JSON must be a top-level object; api_key fields inside cloud_model /
    enterprise_cloud_model are stripped + persisted to the SecretStore
    by the use case (A3.3 credentials rule).
    """

    try:
        parsed = json.loads(args.value)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"invalid JSON: {exc}\n")
        return 2
    if not isinstance(parsed, dict):
        sys.stderr.write(
            "invalid JSON: top-level value must be a JSON object "
            f"(got {type(parsed).__name__})\n"
        )
        return 2

    async def _go(c: Container) -> dict[str, Any]:
        uc = c.model_runtime.save_service_config_use_case
        if uc is None:
            raise RuntimeError(
                "service-config repository is not wired on this container "
                "(missing data_paths or secret_store); cannot write "
                "service_config.json"
            )
        return await uc.execute(parsed)

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0
