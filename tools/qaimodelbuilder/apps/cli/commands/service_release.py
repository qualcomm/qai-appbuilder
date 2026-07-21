# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai service-release`` subcommands — V1 "Download Center" parity.

Desktop App Plan §2.1.1 group K. Thin wrappers over the
``qai.service_release`` use cases that drive the GenieAPIService version
catalog, hardware-grouped model catalog, aria2c daemon, install / delete
flows, local-status scanner, and the ``forge_config.download.*``
settings.

Module name vs command name
---------------------------
The top-level command is ``service-release`` (with a hyphen, V1 path
shape) but the Python module name must be ``service_release`` (no
hyphen, importable identifier). :mod:`apps.cli.__main__._D2_GROUPS`
imports by module name; we register the user-facing parser under the
hyphenated string. The ``dest`` and per-subcommand attribute names use
underscores so ``args.service_release_command`` is a valid Python
identifier.

What this group does NOT surface
--------------------------------
* Streaming download progress (``StreamServiceDownloadUseCase`` /
  ``StreamModelDownloadUseCase``) — those are SSE-shaped iterators
  designed for the API server. The CLI exposes ``aria2c status / start /
  stop / cancel`` so an operator can drive the daemon side-band; live
  progress is the API server's job.
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
    "cmd_versions",
    "cmd_models",
    "cmd_install_service",
    "cmd_install_model",
    "cmd_delete_service",
    "cmd_delete_downloaded",
    "cmd_delete_model",
    "cmd_status_versions",
    "cmd_status_models",
    "cmd_aria2c_status",
    "cmd_aria2c_start",
    "cmd_aria2c_stop",
    "cmd_aria2c_cancel",
    "cmd_settings_get",
    "cmd_settings_set",
]


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach ``qai service-release`` subparsers."""

    sr = subparsers.add_parser(
        "service-release",
        help="V1 Download Center parity (versions / models / aria2c / install / settings)",
        description=(
            "Manage GenieAPIService version packages and the hardware-"
            "grouped model catalog: list remote catalog entries, drive "
            "the aria2c daemon, install / delete archives, inspect local "
            "status, read / write download settings. Mirrors the V1 "
            "Download Center surface."
        ),
    )
    sr_sub = sr.add_subparsers(
        dest="service_release_command", required=True, metavar="<subcommand>"
    )

    # ── versions / models (catalog listing) ─────────────────────────
    sr_sub.add_parser(
        "versions",
        help="list remote GenieAPIService versions from the release catalog",
    ).set_defaults(handler=cmd_versions)

    sr_sub.add_parser(
        "models",
        help="list remote catalog models grouped by hardware (npu/gpu/cpu)",
    ).set_defaults(handler=cmd_models)

    # ── install (subcommand: service / model) ───────────────────────
    install_p = sr_sub.add_parser(
        "install",
        help="install a downloaded archive (service / model)",
    )
    install_sub = install_p.add_subparsers(
        dest="service_release_install_kind",
        required=True,
        metavar="<kind>",
    )
    install_service_p = install_sub.add_parser(
        "service", help="install a GenieAPIService archive into data/bin",
    )
    install_service_p.add_argument(
        "archive",
        metavar="<archive>",
        help="path to the downloaded service archive (.zip)",
    )
    install_service_p.add_argument(
        "--version",
        default="",
        metavar="<version>",
        help="explicit version label (default: derive from archive name)",
    )
    install_service_p.set_defaults(handler=cmd_install_service)

    install_model_p = install_sub.add_parser(
        "model", help="install a model archive into data/models",
    )
    install_model_p.add_argument(
        "archive",
        metavar="<archive>",
        help="path to the downloaded model archive (.zip)",
    )
    install_model_p.add_argument(
        "--model-id",
        dest="model_id",
        default="",
        metavar="<id>",
        help="explicit model id (default: derive from archive)",
    )
    install_model_p.add_argument(
        "--install-dir",
        dest="install_dir",
        default="",
        metavar="<path>",
        help="explicit install directory (default: data/models/<id>)",
    )
    install_model_p.set_defaults(handler=cmd_install_model)

    # ── delete (service / downloaded / model) ───────────────────────
    delete_p = sr_sub.add_parser(
        "delete", help="delete an installed / downloaded artifact",
    )
    delete_sub = delete_p.add_subparsers(
        dest="service_release_delete_kind",
        required=True,
        metavar="<kind>",
    )
    delete_service_p = delete_sub.add_parser(
        "service",
        help="delete an installed GenieAPIService version (under data/bin)",
    )
    delete_service_p.add_argument(
        "version",
        metavar="<version>",
        help="version label to delete (e.g. '0.5.2')",
    )
    delete_service_p.add_argument(
        "--stop",
        action="store_true",
        help=(
            "if the service for this version is running, stop it automatically "
            "before deleting (releases the Genie.dll lock; avoids WinError 5)"
        ),
    )
    delete_service_p.add_argument(
        "--yes",
        action="store_true",
        help="don't prompt when the service is running (implies --stop)",
    )
    delete_service_p.set_defaults(handler=cmd_delete_service)

    delete_downloaded_p = delete_sub.add_parser(
        "downloaded",
        help="delete a downloaded-but-not-installed service archive",
    )
    delete_downloaded_p.add_argument(
        "version",
        metavar="<version>",
        help="version label whose downloaded zip to delete",
    )
    delete_downloaded_p.set_defaults(handler=cmd_delete_downloaded)

    delete_model_p = delete_sub.add_parser(
        "model", help="delete an installed model directory",
    )
    delete_model_p.add_argument(
        "model_id",
        metavar="<id>",
        help="model id to delete (matches a directory under data/models)",
    )
    delete_model_p.add_argument(
        "--keep-zip",
        action="store_true",
        help="keep the downloaded archive (default deletes it too)",
    )
    delete_model_p.set_defaults(handler=cmd_delete_model)

    # ── status (subcommand: versions / models) ──────────────────────
    status_p = sr_sub.add_parser(
        "status", help="inspect local install state",
    )
    status_sub = status_p.add_subparsers(
        dest="service_release_status_kind",
        required=True,
        metavar="<kind>",
    )
    status_versions_p = status_sub.add_parser(
        "versions",
        help="local install/download state for each known service version",
    )
    status_versions_p.set_defaults(handler=cmd_status_versions)
    status_models_p = status_sub.add_parser(
        "models",
        help="local install state for each known catalog model",
    )
    status_models_p.set_defaults(handler=cmd_status_models)

    # ── aria2c (status / start / stop / cancel) ─────────────────────
    aria2c_p = sr_sub.add_parser(
        "aria2c",
        help="aria2c daemon control (status / start / stop / cancel)",
    )
    aria2c_sub = aria2c_p.add_subparsers(
        dest="service_release_aria2c_command",
        required=True,
        metavar="<subcommand>",
    )
    aria2c_sub.add_parser(
        "status", help="print Aria2cStatus snapshot (running / pid / exe)",
    ).set_defaults(handler=cmd_aria2c_status)
    aria2c_sub.add_parser(
        "start", help="start the aria2c daemon (idempotent)",
    ).set_defaults(handler=cmd_aria2c_start)
    aria2c_sub.add_parser(
        "stop", help="stop the aria2c daemon",
    ).set_defaults(handler=cmd_aria2c_stop)
    aria2c_cancel_p = aria2c_sub.add_parser(
        "cancel",
        help="cancel an in-flight download by task id",
    )
    aria2c_cancel_p.add_argument(
        "task_id",
        metavar="<task-id>",
        help="task id returned by the StreamServiceDownload SSE",
    )
    aria2c_cancel_p.set_defaults(handler=cmd_aria2c_cancel)

    # ── settings (get / set) ────────────────────────────────────────
    settings_p = sr_sub.add_parser(
        "settings",
        help="forge_config.download.* settings (save_dir, urls, timeouts)",
    )
    settings_sub = settings_p.add_subparsers(
        dest="service_release_settings_command",
        required=True,
        metavar="<subcommand>",
    )
    settings_sub.add_parser(
        "get", help="print current DownloadSettings JSON",
    ).set_defaults(handler=cmd_settings_get)
    settings_set_p = settings_sub.add_parser(
        "set",
        help="merge <json> into the persisted DownloadSettings (write-through)",
        description=(
            "Persist a new DownloadSettings document. The JSON object's "
            "fields are passed verbatim to ``DownloadSettings(**body)`` "
            "after defaulting any missing keys against the current "
            "stored value, so partial updates are supported."
        ),
    )
    settings_set_p.add_argument(
        "json",
        metavar="<json>",
        help="JSON object with the fields to update",
    )
    settings_set_p.set_defaults(handler=cmd_settings_set)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emit(payload: Any) -> None:
    """JSON-encode ``payload`` to stdout.

    Writes UTF-8 bytes directly to ``sys.stdout.buffer`` to bypass the
    Windows cp1252 / charmap default codec — see
    :func:`apps.cli.commands.pack._emit` for the rationale (the upstream
    release manifest commonly contains non-Latin-1 characters such as
    Chinese model descriptions and accented changelogs).
    """

    body = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    sys.stdout.buffer.write(body.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def _json_default(obj: Any) -> Any:
    """JSON fallback for service_release domain objects."""

    from datetime import datetime  # noqa: PLC0415

    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "value") and obj.__class__.__name__.endswith(
        ("Status", "Action", "State", "Kind", "Algorithm", "Format", "Hardware")
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


def _parse_json_object(raw: str) -> dict[str, Any]:
    """Parse ``raw`` and require a top-level JSON object."""

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise _CliUsageError(f"invalid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise _CliUsageError(
            "invalid JSON: top-level value must be a JSON object "
            f"(got {type(parsed).__name__})"
        )
    return parsed


class _CliUsageError(Exception):
    """Raised by helpers to signal a usage error (exit 2 + stderr message)."""


# ---------------------------------------------------------------------------
# handlers — catalog listing
# ---------------------------------------------------------------------------


def cmd_versions(args: argparse.Namespace) -> int:
    """``qai service-release versions`` handler."""

    async def _go(c: Container) -> dict[str, Any]:
        versions = (
            await c.service_release.list_service_versions_use_case.execute()
        )
        return {"items": versions}

    payload = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(payload)
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    """``qai service-release models`` handler."""

    async def _go(c: Container) -> dict[str, Any]:
        models = (
            await c.service_release.list_catalog_models_use_case.execute()
        )
        return {"items": models}

    payload = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(payload)
    return 0


# ---------------------------------------------------------------------------
# handlers — install (service / model)
# ---------------------------------------------------------------------------


def cmd_install_service(args: argparse.Namespace) -> int:
    """``qai service-release install service <archive>`` handler."""

    from qai.service_release.application.use_cases import (  # noqa: PLC0415
        InstallServiceCommand,
    )

    command = InstallServiceCommand(
        save_path=str(Path(args.archive).resolve()),
        version=args.version,
    )

    async def _go(c: Container) -> Any:
        return await c.service_release.install_service_use_case.execute(command)

    result = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(result)
    return 0


def cmd_install_model(args: argparse.Namespace) -> int:
    """``qai service-release install model <archive>`` handler."""

    from qai.service_release.application.use_cases import (  # noqa: PLC0415
        InstallModelCommand,
    )

    command = InstallModelCommand(
        save_path=str(Path(args.archive).resolve()),
        model_id=args.model_id,
        install_dir=args.install_dir,
    )

    async def _go(c: Container) -> Any:
        return await c.service_release.install_model_use_case.execute(command)

    result = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(result)
    return 0


# ---------------------------------------------------------------------------
# handlers — delete (service / downloaded / model)
# ---------------------------------------------------------------------------


def cmd_delete_service(args: argparse.Namespace) -> int:
    """``qai service-release delete service <version>`` handler.

    If the version's GenieAPIService is still running, deleting its files
    fails on Windows (loaded ``Genie.dll`` is locked → WinError 5). We probe
    first and, when running, either stop it automatically (``--stop`` /
    ``--yes``) or ask for confirmation (custom terminal prompt, §3.9.2 — no
    native ``input`` dialog).
    """
    import sys  # noqa: PLC0415

    stop_running = bool(args.stop or args.yes)

    async def _probe(c: Container) -> bool:
        return await c.service_release.delete_installed_service_use_case.is_running(
            version=args.version
        )

    running = run_use_case(
        _probe,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )

    if running and not stop_running:
        # Interactive confirm only on a TTY; non-TTY must pass --stop/--yes.
        if not sys.stdin.isatty():
            sys.stderr.write(
                f"GenieAPIService {args.version!r} 正在运行；删除会先停止它。"
                "请加 --stop（或 --yes）确认后重试。\n"
            )
            return 2
        sys.stdout.write(
            f"GenieAPIService {args.version!r} 正在运行。删除将先自动停止它。\n"
            "继续? [y/N] "
        )
        sys.stdout.flush()
        ans = sys.stdin.readline().strip().lower()
        if ans not in ("y", "yes"):
            sys.stdout.write("已取消。\n")
            return 1
        stop_running = True

    async def _go(c: Container) -> Any:
        return (
            await c.service_release.delete_installed_service_use_case.execute(
                version=args.version, stop_running=stop_running
            )
        )

    result = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(result)
    return 0


def cmd_delete_downloaded(args: argparse.Namespace) -> int:
    """``qai service-release delete downloaded <version>`` handler."""

    async def _go(c: Container) -> Any:
        return (
            await c.service_release.delete_downloaded_service_use_case.execute(
                version=args.version
            )
        )

    result = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(result)
    return 0


def cmd_delete_model(args: argparse.Namespace) -> int:
    """``qai service-release delete model <id>`` handler."""

    async def _go(c: Container) -> Any:
        return await c.service_release.delete_model_use_case.execute(
            model_id=args.model_id,
            delete_zip=not args.keep_zip,
        )

    result = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(result)
    return 0


# ---------------------------------------------------------------------------
# handlers — status
# ---------------------------------------------------------------------------


def cmd_status_versions(args: argparse.Namespace) -> int:
    """``qai service-release status versions`` handler."""

    async def _go(c: Container) -> Any:
        return (
            await c.service_release.get_versions_local_status_use_case.execute()
        )

    status = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(status)
    return 0


def cmd_status_models(args: argparse.Namespace) -> int:
    """``qai service-release status models`` handler."""

    async def _go(c: Container) -> Any:
        return (
            await c.service_release.get_models_local_status_use_case.execute()
        )

    status = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(status)
    return 0


# ---------------------------------------------------------------------------
# handlers — aria2c
# ---------------------------------------------------------------------------


def cmd_aria2c_status(args: argparse.Namespace) -> int:
    """``qai service-release aria2c status`` handler."""

    async def _go(c: Container) -> Any:
        return await c.service_release.get_aria2c_status_use_case.execute()

    status = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(status)
    return 0


def cmd_aria2c_start(args: argparse.Namespace) -> int:
    """``qai service-release aria2c start`` handler."""

    async def _go(c: Container) -> Any:
        return await c.service_release.start_aria2c_use_case.execute()

    status = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(status)
    return 0


def cmd_aria2c_stop(args: argparse.Namespace) -> int:
    """``qai service-release aria2c stop`` handler."""

    async def _go(c: Container) -> Any:
        return await c.service_release.stop_aria2c_use_case.execute()

    status = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(status)
    return 0


def cmd_aria2c_cancel(args: argparse.Namespace) -> int:
    """``qai service-release aria2c cancel <task-id>`` handler.

    Note: the ``CancelDownloadUseCase`` here is the
    ``service_release`` one (operates on aria2c task ids via the
    Aria2cManagerPort) — distinct from
    ``qai.model_catalog.application.use_cases.cancel_download``.
    """

    async def _go(c: Container) -> dict[str, Any]:
        ok = await c.service_release.cancel_download_use_case.execute(
            task_id=args.task_id
        )
        return {"task_id": args.task_id, "cancelled": ok}

    payload = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(payload)
    return 0


# ---------------------------------------------------------------------------
# handlers — settings
# ---------------------------------------------------------------------------


def cmd_settings_get(args: argparse.Namespace) -> int:
    """``qai service-release settings get`` handler."""

    async def _go(c: Container) -> Any:
        return (
            await c.service_release.get_download_settings_use_case.execute()
        )

    settings = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(settings)
    return 0


def cmd_settings_set(args: argparse.Namespace) -> int:
    """``qai service-release settings set <json>`` handler.

    Loads the current settings, merges the supplied object on top
    (shallow), then write-throughs via
    :class:`UpdateDownloadSettingsUseCase`. Partial updates are the
    common case (operator only sets ``save_dir``); a full-replace is
    just a JSON object containing every field.
    """

    from dataclasses import replace  # noqa: PLC0415

    try:
        body = _parse_json_object(args.json)
    except _CliUsageError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2

    async def _go(c: Container) -> Any:
        current = (
            await c.service_release.get_download_settings_use_case.execute()
        )
        try:
            merged = replace(current, **body)
        except TypeError as exc:
            raise _CliUsageError(
                f"invalid JSON: {exc} "
                f"(allowed fields: {', '.join(current.__dataclass_fields__)})"
            ) from exc
        return await c.service_release.update_download_settings_use_case.execute(
            merged
        )

    try:
        result = run_use_case(
            _go,
            config_file=_resolved_config_file(args),
            repo_root=_resolved_repo_root(args),
        )
    except _CliUsageError as exc:
        sys.stderr.write(f"{exc}\n")
        return 2
    _emit(result)
    return 0
