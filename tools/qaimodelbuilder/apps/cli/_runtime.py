# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``apps.cli._runtime`` — shared runtime context for one-shot CLI commands.

Desktop App Plan §1.2 / §2.5 / §2.6 — single composition root for short-lived
``qai <verb>`` invocations. The reboot supervisor (``apps.cli.serve``) and the
long-lived API server (``apps.api.main``) have their own lifespans tuned for
server-side concerns (daemon pool, voice warm-up, PEP 578 audit hook, policy
watcher, GenieAPIService, aria2c, channel inbound consumers). Those are
**deliberately not** activated here:

* CLI invocations live for milliseconds-to-seconds; spinning up daemons /
  watching files / warming the NPU would dominate runtime and leak
  background tasks past the command exit.
* CLI is read-mostly for D1 (``qai config get/set``); use cases that *do*
  need server-only resources will surface a clear error rather than have
  the runtime silently start a sandbox daemon for a one-shot.

What it DOES do (the minimum every use case below the use_cases/ layer
requires):

1. Load ``Settings`` with the same operator-persisted overrides the API
   server would see (parity with ``apps.api.main._load_settings_with_persisted_overrides``).
2. Build the DI :class:`Container` (synchronous, no FastAPI dependency).
3. Open the SQLite database + run any pending schema migrations (matches
   ``apps.api.lifespan`` startup steps 2 and 3).
4. Yield the container to the caller.
5. On exit, close the EventBus and database. Errors during teardown are
   swallowed so a use-case error is never masked by a shutdown error.

Logging defaults to ``stderr`` so CLI business output on ``stdout`` stays
clean for shell pipes / scripts.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import TypeVar

from qai.platform.config import Settings, load_settings
from qai.platform.logging import configure_logging, get_logger
from qai.platform.persistence import migrate

from apps.api._runtime_config_store import load_runtime_config_overrides
from apps.api.di import Container

__all__ = ["cli_container", "run_use_case"]

_log = get_logger("apps.cli._runtime")

_T = TypeVar("_T")


def _detect_repo_root() -> Path:
    """Find the repository root from this file's location.

    ``apps/cli/_runtime.py`` is two levels below the repo root, mirroring
    :func:`apps.api.main._detect_repo_root`.
    """
    return Path(__file__).resolve().parents[2]


def _load_settings_with_persisted_overrides(
    repo_root: Path,
    *,
    config_file: Path | None = None,
) -> Settings:
    """CLI-side parity of :func:`apps.api.main._load_settings_with_persisted_overrides`.

    Inlined (rather than imported) so a one-shot ``qai config get`` does not
    transitively import FastAPI / uvicorn / every router module just to read
    one preference key. The behaviour is byte-for-byte identical: load
    defaults, discover ``data.data_dir``, layer the persisted typed-security
    overrides on top.

    **CLI-only fix (CLI-ISO-1, 2026-06-10)**: when ``data.data_dir`` is still
    the relative default ``Path("data")`` *and* no explicit ``config_file``
    was given, anchor it to ``repo_root / "data"`` so a ``--repo-root <tmp>``
    invocation actually isolates from production data. The server (which is
    started from the repo CWD by ``Start.bat`` / ``qai-serve``) doesn't hit
    this because its CWD already matches; the CLI does because it can be
    launched from anywhere with arbitrary ``--repo-root``.

    Tests and ``--config`` flag callers may inject an explicit ``config_file``;
    when ``None`` the loader picks ``<repo_root>/config/server.toml`` if
    present, otherwise pure defaults.
    """

    base = load_settings(config_file=config_file, repo_root=repo_root)

    # CLI-ISO-1: anchor relative ``data_dir`` to ``repo_root`` for CLI calls.
    # We detect "still default" by checking it's a non-absolute Path("data");
    # any operator-set absolute path or non-default relative path is left alone.
    overrides: dict[str, dict[str, str]] = {}
    if config_file is None and not base.data.data_dir.is_absolute() and str(base.data.data_dir) == "data":
        overrides["data"] = {"data_dir": str(repo_root / "data")}
        # Re-load with the anchored data_dir so subsequent ``data_paths()``
        # calls produce absolute paths.
        base = load_settings(config_file=config_file, repo_root=repo_root, overrides=overrides)

    persisted = load_runtime_config_overrides(base.data.data_dir)
    if not persisted:
        return base
    if overrides:
        # Merge: data anchor wins for ``data_dir``, persisted typed-security
        # overrides win for everything else.
        merged = dict(persisted)
        merged.setdefault("data", {})["data_dir"] = str(repo_root / "data")
        return load_settings(config_file=config_file, repo_root=repo_root, overrides=merged)
    return load_settings(config_file=config_file, repo_root=repo_root, overrides=persisted)


def _resolve_migrations_dir(repo_root: Path) -> Path:
    """Resolve the SQL migrations directory.

    Mirrors :func:`apps.api.lifespan._default_migrations_dir`: prefer the
    workspace path under ``<repo_root>/src/qai/platform/persistence/migrations_sql/``
    for dev-checkout runs; fall back to the directory bundled with the
    installed ``qai.platform.persistence`` package so an installed wheel
    (where the workspace path doesn't exist) still finds migrations.
    """

    candidate = (
        repo_root
        / "src"
        / "qai"
        / "platform"
        / "persistence"
        / "migrations_sql"
    )
    if candidate.exists():
        return candidate
    import qai.platform.persistence as _persistence

    return Path(_persistence.__file__).resolve().parent / "migrations_sql"


@contextlib.asynccontextmanager
async def cli_container(
    *,
    config_file: Path | None = None,
    repo_root: Path | None = None,
) -> AsyncIterator[Container]:
    """Build, start, yield, then tear down a :class:`Container` for one CLI call.

    Parameters
    ----------
    config_file:
        Optional explicit ``server.toml`` path. ``None`` lets ``load_settings``
        pick the default under ``repo_root``.
    repo_root:
        Repository root (the directory containing ``src/`` / ``apps/``).
        ``None`` auto-detects from this module's location, matching the API
        server's behaviour.

    Yields
    ------
    Container
        Fully-built container with database started and migrations applied.
        Use cases reachable via ``c.<context>.<use_case>`` are safe to call.

    Notes
    -----
    Server-only resources are intentionally NOT started: daemon pool, voice
    warm-up, PEP 578 audit hook, policy hot-reload watcher, GenieAPIService,
    aria2c daemon, WeChat QR login, channel inbound consumers. A CLI use
    case that genuinely depends on one of those should be invoked against a
    running server (over HTTP) rather than via this short-lived runtime.

    Logging is initialised on the first call and routed to stderr so CLI
    stdout stays a clean business-output channel for shell composition.
    """

    resolved_root = (repo_root or _detect_repo_root()).resolve()
    settings = _load_settings_with_persisted_overrides(
        resolved_root, config_file=config_file
    )

    # Route logs to stderr so stdout remains a clean machine-readable
    # surface for ``qai config get foo | jq ...``. ``fmt="console"`` gives
    # the operator readable lines (the API server uses ``auto`` which picks
    # JSON in non-TTY contexts, but for CLI the operator IS the audience).
    configure_logging(
        level=settings.logging.level,  # type: ignore[arg-type]
        fmt="console",
        stream=sys.stderr,
    )

    container = Container.build(settings=settings, repo_root=resolved_root)
    migrations_dir = _resolve_migrations_dir(resolved_root)

    await container.database.start()
    applied = await migrate(container.database, migrations_dir=migrations_dir)
    if applied:
        _log.info("cli.migrations_applied", ids=applied)

    try:
        yield container
    finally:
        # Swallow teardown errors so a use-case error is never masked.
        # The CLI process is exiting anyway; OS will reclaim resources.
        try:
            await container.events.close()
        except Exception:  # noqa: BLE001 — teardown best-effort
            _log.warning("cli.events_close_failed", exc_info=True)
        try:
            await container.database.close()
        except Exception:  # noqa: BLE001 — teardown best-effort
            _log.warning("cli.database_close_failed", exc_info=True)


def run_use_case(
    coro_factory: Callable[[Container], Awaitable[_T]],
    *,
    config_file: Path | None = None,
    repo_root: Path | None = None,
) -> _T:
    """Synchronous bridge for argparse command handlers.

    ``coro_factory`` receives the live :class:`Container` and returns the
    awaitable to drive. Wraps :func:`cli_container` in :func:`asyncio.run`
    so each CLI handler can stay declarative::

        def cmd_config_get(args) -> int:
            doc = run_use_case(
                lambda c: c.user_prefs.load_document_use_case.execute(args.key)
            )
            print(json.dumps(doc, ensure_ascii=False, indent=2))
            return 0
    """

    async def _runner() -> _T:
        async with cli_container(
            config_file=config_file, repo_root=repo_root
        ) as c:
            return await coro_factory(c)

    return asyncio.run(_runner())
