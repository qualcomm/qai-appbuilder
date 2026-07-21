# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai pack`` subcommands — App Builder Pack management from the CLI.

Desktop App Plan §2.1.1 group D. Thin wrappers over
:mod:`qai.app_builder.application.use_cases.*` (list/show/delete/import/
manifest/deps/cache/taxonomy) plus three pass-throughs to
:mod:`scripts.build.model_builder_cli` (export / validate / workspace-init).

Behavioural contract
--------------------
* Every subcommand emits its result as a single JSON object on stdout
  (``ensure_ascii=False``, indent 2) — matches ``qai config get`` so a
  downstream pipe like ``qai pack list | jq '.items[].id'`` is well-defined.
* Listing / show / manifest / deps-status / taxonomy etc. read from the
  ``app_builder`` DB; the in-memory factory Pack manifests are seeded into
  the DB on first invocation by re-using the same helper the API server's
  lifespan hook calls
  (:func:`apps.api.lifespan._seed_app_builder_models`). This keeps the
  CLI in lock-step with the HTTP route shape: a fresh ``--repo-root``
  surfaces the four bundled factory packs identically in both surfaces.

* ``import --dry-run / --apply / --rollback`` operate on a **directory
  path or zip archive** (the importer's contract — see
  :class:`qai.app_builder.application.ports.ImportPort.dry_run`). Bare
  ``--dry-run`` followed by a path is the operator-friendly form.

* ``deps-install <id>`` re-uses the same :class:`DynamicPackDepChecker`
  that ``RunAppUseCase`` wires up on the live container, so the CLI and
  the runtime see one source of truth for "what packages must be present
  to run pack X". Output lists the packages installed (or the cached
  status when the deps are already satisfied).

* ``export``, ``validate``, ``workspace-init`` forward ``argparse.REMAINDER``
  to ``scripts.build.model_builder_cli:{pack_export_main,
  pack_validate_main, workspace_init_main}`` — same pattern as
  ``qai install-qairt`` (see :mod:`apps.cli.commands.install`). The inner
  script owns its own help text; we don't re-parse anything here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, TYPE_CHECKING

from apps.api.di import Container
from apps.cli._runtime import run_use_case

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Sequence

__all__ = [
    "register",
    "cmd_pack_list",
    "cmd_pack_show",
    "cmd_pack_delete",
    "cmd_pack_import",
    "cmd_pack_manifest",
    "cmd_pack_deps_status",
    "cmd_pack_deps_install",
    "cmd_pack_cache_status",
    "cmd_pack_cache_clear",
    "cmd_pack_taxonomy",
    "cmd_pack_export",
    "cmd_pack_validate",
    "cmd_pack_workspace_init",
]


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach ``qai pack`` subparsers to the top-level dispatcher."""

    pack = subparsers.add_parser(
        "pack",
        help="manage App Builder Packs (list / import / cache / deps / ...)",
        description=(
            "App Builder Pack management — list installed packs, inspect "
            "manifests, run import / rollback workflows, query and clear "
            "the result cache, and forward export / validate / "
            "workspace-init to the bundled scripts."
        ),
    )
    pack_sub = pack.add_subparsers(
        dest="pack_command", required=True, metavar="<subcommand>"
    )

    # ── list / show / delete ────────────────────────────────────────
    list_p = pack_sub.add_parser(
        "list", help="list every Pack registered in the local DB",
    )
    list_p.set_defaults(handler=cmd_pack_list)

    show_p = pack_sub.add_parser(
        "show", help="print the AppModelDefinition row for <id>",
    )
    show_p.add_argument("id", help="Pack id, e.g. 'whisper-base'")
    show_p.set_defaults(handler=cmd_pack_show)

    delete_p = pack_sub.add_parser(
        "delete",
        help="delete a user-imported Pack (built-ins are protected)",
    )
    delete_p.add_argument("id", help="Pack id to delete")
    delete_p.add_argument(
        "--yes",
        action="store_true",
        help="skip the interactive confirmation prompt (required in scripts)",
    )
    delete_p.set_defaults(handler=cmd_pack_delete)

    # ── import (dry-run / apply / rollback) ─────────────────────────
    import_p = pack_sub.add_parser(
        "import",
        help="run the Pack import workflow (dry-run / apply / rollback)",
        description=(
            "Three modes, exactly one required: --dry-run scans <path> "
            "(directory or zip) and prints the planned ADD/REPLACE/SKIP "
            "actions; --apply runs dry-run + commits in one shot; "
            "--rollback <commit-id> reverts a previous --apply."
        ),
    )
    mode = import_p.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        dest="dry_run_path",
        metavar="<path>",
        type=Path,
        help="scan <path> and print the planned import actions (no DB write)",
    )
    mode.add_argument(
        "--apply",
        dest="apply_path",
        metavar="<path>",
        type=Path,
        help="dry-run + commit <path> in a single invocation",
    )
    mode.add_argument(
        "--rollback",
        dest="rollback_commit_id",
        metavar="<commit-id>",
        help="rollback the named commit id (returned by --apply)",
    )
    import_p.set_defaults(handler=cmd_pack_import)

    # ── manifest ────────────────────────────────────────────────────
    manifest_p = pack_sub.add_parser(
        "manifest", help="print the resolved PackManifest JSON for <id>",
    )
    manifest_p.add_argument("id", help="Pack id, e.g. 'whisper-base'")
    manifest_p.set_defaults(handler=cmd_pack_manifest)

    # ── deps ────────────────────────────────────────────────────────
    deps_status_p = pack_sub.add_parser(
        "deps-status",
        help=(
            "print the global Pack deps status (qairt env / pack root / "
            "shared dir / sticky worker / registered count)"
        ),
    )
    # Optional <id> is accepted for parity with the V1 wire surface but
    # the use case itself returns a global snapshot — the id is currently
    # informational only. We accept and ignore it rather than reject so a
    # future per-pack status (PR-094 §17.5) can land without breaking
    # operator scripts.
    deps_status_p.add_argument(
        "id",
        nargs="?",
        default=None,
        help="optional Pack id (informational; current impl returns global status)",
    )
    deps_status_p.set_defaults(handler=cmd_pack_deps_status)

    deps_install_p = pack_sub.add_parser(
        "deps-install",
        help="install a Pack's Python dependencies into the runtime venv",
        description=(
            "Re-uses the same DynamicPackDepChecker the runtime uses, so "
            "what installs here is exactly what RunAppUseCase would "
            "install on first run. Idempotent: a satisfied pack returns "
            "ok with no pip invocation."
        ),
    )
    deps_install_p.add_argument("id", help="Pack id whose requirements to install")
    deps_install_p.set_defaults(handler=cmd_pack_deps_install)

    # ── cache ───────────────────────────────────────────────────────
    cache_p = pack_sub.add_parser(
        "cache", help="result-cache status / clear",
    )
    cache_sub = cache_p.add_subparsers(
        dest="pack_cache_command", required=True, metavar="<subcommand>"
    )
    cache_status_p = cache_sub.add_parser(
        "status", help="print blob count + total bytes under the cache dir",
    )
    cache_status_p.set_defaults(handler=cmd_pack_cache_status)
    cache_clear_p = cache_sub.add_parser(
        "clear", help="delete every blob under the cache dir",
    )
    cache_clear_p.set_defaults(handler=cmd_pack_cache_clear)

    # ── taxonomy ────────────────────────────────────────────────────
    taxonomy_p = pack_sub.add_parser(
        "taxonomy",
        help="print the (group, task) taxonomy with per-task model counts",
    )
    taxonomy_p.set_defaults(handler=cmd_pack_taxonomy)

    # ── pass-through to scripts.build.model_builder_cli ─────────────
    # ``add_help=False`` + ``prefix_chars='\x00'``: forward ``--help`` and
    # any ``--xxx`` flags through ``REMAINDER`` to the underlying script.
    # Without ``prefix_chars='\x00'`` argparse rejects option-like tokens
    # (``--help``, ``--workdir``...) before REMAINDER captures them.  The
    # NUL sentinel is a prefix no real CLI uses, so it's safe to disable
    # argparse's option detection in just these forwarding subparsers.
    # Same pattern as ``apps/cli/commands/install.py``.
    export_p = pack_sub.add_parser(
        "export",
        help="forward to scripts.build.model_builder_cli:pack_export_main",
        add_help=False,
        prefix_chars="\x00",
    )
    export_p.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    export_p.set_defaults(handler=cmd_pack_export)

    validate_p = pack_sub.add_parser(
        "validate",
        help="forward to scripts.build.model_builder_cli:pack_validate_main",
        add_help=False,
        prefix_chars="\x00",
    )
    validate_p.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    validate_p.set_defaults(handler=cmd_pack_validate)

    workspace_init_p = pack_sub.add_parser(
        "workspace-init",
        help="forward to scripts.build.model_builder_cli:workspace_init_main",
        add_help=False,
        prefix_chars="\x00",
    )
    workspace_init_p.add_argument(
        "rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS
    )
    workspace_init_p.set_defaults(handler=cmd_pack_workspace_init)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emit(payload: Any) -> None:
    """JSON-encode ``payload`` to stdout (CLI uniform output convention).

    Mirrors :func:`apps.cli.commands.config._emit_doc`: ``ensure_ascii=False``
    so Chinese / emoji titles round-trip cleanly, ``indent=2`` for human
    readability, trailing newline so shell composition works.

    Writes the encoded UTF-8 bytes directly to ``sys.stdout.buffer`` to
    bypass the default Windows cp1252 / charmap codec on
    ``sys.stdout.write`` (PowerShell's default ``CONOUT$`` ANSI codepage),
    which would otherwise raise ``UnicodeEncodeError`` whenever a remote
    catalog response or domain object contains non-Latin-1 characters
    (Chinese model names, emoji titles, accented release notes, …).
    """

    body = json.dumps(payload, ensure_ascii=False, indent=2, default=_json_default)
    sys.stdout.buffer.write(body.encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.buffer.flush()


def _json_default(obj: Any) -> Any:
    """Best-effort JSON fallback for domain dataclasses / enums / Paths.

    The use cases return frozen dataclasses / Enums / Path / tuple
    instances. We translate via ``__str__`` for VOs (which all override
    ``__str__`` to return ``.value``), ``str()`` for Paths and Enums, and
    ``vars()`` / ``__dict__`` for dataclasses. This keeps every subcommand
    handler tiny — none of them need a hand-written mapper.
    """

    if isinstance(obj, Path):
        return str(obj)
    # Enums (including ``str, Enum`` mixins) — emit the .value.
    if hasattr(obj, "value") and obj.__class__.__name__.endswith(
        ("Status", "Action", "State", "Kind", "Algorithm")
    ):
        return obj.value
    # frozen dataclasses with __slots__ expose fields via __dataclass_fields__.
    if hasattr(obj, "__dataclass_fields__"):
        out: dict[str, Any] = {}
        for name in obj.__dataclass_fields__:  # type: ignore[attr-defined]
            try:
                out[name] = getattr(obj, name)
            except AttributeError:
                continue
        return out
    if isinstance(obj, (set, frozenset)):
        return sorted(obj)
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def _model_to_dict(model: Any) -> dict[str, Any]:
    """Mirror of :func:`interfaces.http.routes.app_builder._dto._model_to_dto`.

    Re-implemented here (rather than imported) because the route DTO is a
    pydantic ``BaseModel`` that pulls in fastapi — overkill for a CLI
    one-shot. The wire shape stays identical so a pipe like
    ``qai pack list | jq '.items[].id'`` matches what the HTTP route
    surface would print.
    """

    return {
        "id": str(model.id),
        "title": model.title,
        "taxonomy": list(model.taxonomy.segments),
        "enabled": model.enabled,
        "pinned": model.pinned,
        "input_presets": [
            {"name": p.name, "payload": p.payload} for p in model.input_presets
        ],
        "required_catalog_ids": list(model.required_catalog_ids),
        "user_imported": model.user_imported,
    }


def _resolved_repo_root(args: argparse.Namespace) -> Path | None:
    """Extract ``--repo-root`` from the top-level Namespace, if set."""

    return getattr(args, "repo_root", None)


def _resolved_config_file(args: argparse.Namespace) -> Path | None:
    """Extract ``--config`` from the top-level Namespace, if set."""

    return getattr(args, "config_file", None)


async def _seed_factory_packs_if_empty(c: Container) -> None:
    """Seed the four built-in factory Packs into the DB on first call.

    The HTTP route surface relies on the lifespan hook to do this; the CLI
    doesn't run lifespan so we replay the same idempotent helper. ``--repo-
    root <empty-dir>`` (used by tests + ad-hoc operators) deliberately
    yields an empty list because the factory tree is then absent — the
    handler still returns ``{"items": []}`` without erroring.
    """

    # Lazy import so the rest of the CLI doesn't pay the lifespan import
    # cost when running ``qai pack cache clear`` or similar.
    from apps.api.lifespan import _seed_app_builder_models  # noqa: PLC0415

    await _seed_app_builder_models(c)


# ---------------------------------------------------------------------------
# handlers — list / show / delete
# ---------------------------------------------------------------------------


def cmd_pack_list(args: argparse.Namespace) -> int:
    """``qai pack list`` handler."""

    async def _go(c: Container) -> dict[str, Any]:
        await _seed_factory_packs_if_empty(c)
        models = await c.app_builder.list_app_models_use_case.execute(
            include_disabled=True,
        )
        return {"items": [_model_to_dict(m) for m in models]}

    payload = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(payload)
    return 0


def cmd_pack_show(args: argparse.Namespace) -> int:
    """``qai pack show <id>`` handler."""

    from qai.app_builder.domain.value_objects import AppModelId  # noqa: PLC0415

    async def _go(c: Container) -> dict[str, Any]:
        await _seed_factory_packs_if_empty(c)
        model = await c.app_builder.get_app_model_use_case.execute(
            model_id=AppModelId(value=args.id),
        )
        return _model_to_dict(model)

    try:
        payload = run_use_case(
            _go,
            config_file=_resolved_config_file(args),
            repo_root=_resolved_repo_root(args),
        )
    except ValueError as exc:
        sys.stderr.write(f"invalid model id: {exc}\n")
        return 2
    _emit(payload)
    return 0


def cmd_pack_delete(args: argparse.Namespace) -> int:
    """``qai pack delete <id> [--yes]`` handler.

    Refuses to run without ``--yes`` because the CLI is expected to be
    invoked from scripts; an interactive prompt would deadlock CI. The
    underlying use case still raises ``ForbiddenError`` for built-in /
    factory packs, which surfaces as a non-zero exit.
    """

    from qai.app_builder.domain.value_objects import AppModelId  # noqa: PLC0415

    if not args.yes:
        sys.stderr.write(
            "qai pack delete: refusing to delete without --yes "
            "(this command is destructive and unattended-only)\n"
        )
        return 2

    async def _go(c: Container) -> None:
        return await c.app_builder.delete_app_model_use_case.execute(
            model_id=AppModelId(value=args.id),
        )

    run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit({"deleted": args.id})
    return 0


# ---------------------------------------------------------------------------
# handlers — import (dry-run / apply / rollback)
# ---------------------------------------------------------------------------


def cmd_pack_import(args: argparse.Namespace) -> int:
    """``qai pack import {--dry-run|--apply|--rollback}`` handler.

    The mutually-exclusive group enforces "exactly one mode"; this handler
    branches on the populated attribute. ``--apply`` is a convenience
    composition (dry-run → commit) so an operator can land an import in
    a single invocation; advanced workflows can still split the steps by
    running ``--dry-run`` first and inspecting the JSON before
    ``--apply``.
    """

    if args.dry_run_path is not None:
        return _do_import_dry_run(args, args.dry_run_path)
    if args.apply_path is not None:
        return _do_import_apply(args, args.apply_path)
    if args.rollback_commit_id is not None:
        return _do_import_rollback(args, args.rollback_commit_id)
    # argparse already enforces required=True on the group; keep the
    # explicit branch as a defensive guard for future maintainers.
    sys.stderr.write(
        "qai pack import: one of --dry-run / --apply / --rollback is required\n"
    )
    return 2


def _plan_to_dict(plan: Any) -> dict[str, Any]:
    """Map an ``ImportPlan`` to a JSON-friendly dict.

    Mirror of the route DTO's ``_plan_to_dto`` shape (``items: [...]``)
    so a CLI consumer can pipe into the same ``jq`` expressions an
    HTTP-driven workflow would use.
    """

    items = []
    for item in plan.items:
        items.append(
            {
                "model_id": str(item.model_id),
                "action": item.action.value,
                "source": item.source,
                "reason": item.reason,
            }
        )
    return {"items": items}


def _do_import_dry_run(args: argparse.Namespace, path: Path) -> int:
    async def _go(c: Container) -> dict[str, Any]:
        plan = await c.app_builder.import_dry_run_use_case.execute(
            candidates=[str(path)],
        )
        return _plan_to_dict(plan)

    payload = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(payload)
    return 0


def _do_import_apply(args: argparse.Namespace, path: Path) -> int:
    async def _go(c: Container) -> dict[str, Any]:
        plan = await c.app_builder.import_dry_run_use_case.execute(
            candidates=[str(path)],
        )
        commit_id = await c.app_builder.import_commit_use_case.execute(plan=plan)
        return {"commit_id": str(commit_id), "plan": _plan_to_dict(plan)}

    payload = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(payload)
    return 0


def _do_import_rollback(args: argparse.Namespace, commit_id_raw: str) -> int:
    from qai.app_builder.domain.import_plan import CommitId  # noqa: PLC0415

    async def _go(c: Container) -> None:
        await c.app_builder.import_rollback_use_case.execute(
            commit_id=CommitId(value=commit_id_raw),
        )
        return None

    run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit({"rolled_back": commit_id_raw})
    return 0


# ---------------------------------------------------------------------------
# handlers — manifest / deps / cache / taxonomy
# ---------------------------------------------------------------------------


def cmd_pack_manifest(args: argparse.Namespace) -> int:
    """``qai pack manifest <id>`` handler."""

    from qai.app_builder.domain.value_objects import AppModelId  # noqa: PLC0415

    async def _go(c: Container) -> Any:
        await _seed_factory_packs_if_empty(c)
        uc = c.app_builder.get_pack_manifest_use_case
        if uc is None:
            raise RuntimeError(
                "GetPackManifestUseCase is not wired (stripped-down container?)"
            )
        return await uc.execute(AppModelId(value=args.id))

    manifest = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    # PackManifest is a domain dataclass; the default JSON encoder walks
    # ``__dataclass_fields__`` for us via ``_json_default``.
    _emit(manifest)
    return 0


def cmd_pack_deps_status(args: argparse.Namespace) -> int:
    """``qai pack deps-status [<id>]`` handler."""

    async def _go(c: Container) -> Any:
        uc = c.app_builder.get_deps_status_use_case
        if uc is None:
            raise RuntimeError(
                "GetDepsStatusUseCase is not wired (stripped-down container?)"
            )
        return await uc.execute()

    status = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(status)
    return 0


def cmd_pack_deps_install(args: argparse.Namespace) -> int:
    """``qai pack deps-install <id>`` handler.

    Re-uses the live :class:`DynamicPackDepChecker` instance the
    container builds for ``RunAppUseCase`` so the install path is
    byte-for-byte identical to "run the pack" (no second checker, no
    drift). When the dep_checker is disabled by settings the handler
    surfaces a clear error rather than silently no-op.
    """

    from qai.app_builder.domain.value_objects import AppModelId  # noqa: PLC0415

    async def _go(c: Container) -> dict[str, Any]:
        await _seed_factory_packs_if_empty(c)
        dep_checker = c.app_builder.dep_checker
        if dep_checker is None:
            raise RuntimeError(
                "dep_checker is disabled "
                "(set app_builder.dep_checker_enabled=true to use this command)"
            )
        # The pack root is stored on the runner registry / manifest
        # provider; resolve it via the same factory fallback the DI
        # builder uses so the directory we hand to ``ensure_installed``
        # is the one runtime-wired ``RunAppUseCase`` would pick.
        pack_root = (c.repo_root / "factory" / "app_builder" / "models").resolve()
        if not pack_root.is_dir():
            raise RuntimeError(
                f"factory pack root not found at {pack_root} "
                "(did you run `qai compile-factory --apply`?)"
            )
        # Validate the id exists in the DB before pip-installing anything.
        await c.app_builder.get_app_model_use_case.execute(
            model_id=AppModelId(value=args.id),
        )
        # ``ensure_installed`` is idempotent: returns a status dict
        # describing what was done (already satisfied / installed N
        # packages / failed). The exact return shape is the checker's
        # cache row; we surface it verbatim so an operator can grep
        # ``"satisfied": true``.
        target_dir = pack_root / args.id
        status = await dep_checker.ensure_installed(
            model_id=args.id,
            pack_dir=target_dir,
        )
        return {
            "model_id": args.id,
            "satisfied": getattr(status, "satisfied", None),
            "installing": getattr(status, "installing", None),
            "missing_packages": list(getattr(status, "missing_packages", []) or []),
            "last_error": getattr(status, "last_error", None),
        }

    payload = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(payload)
    return 0


def cmd_pack_cache_status(args: argparse.Namespace) -> int:
    """``qai pack cache status`` handler."""

    async def _go(c: Container) -> Any:
        uc = c.app_builder.get_cache_status_use_case
        if uc is None:
            raise RuntimeError(
                "GetCacheStatusUseCase is not wired (stripped-down container?)"
            )
        return await uc.execute()

    status = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(status)
    return 0


def cmd_pack_cache_clear(args: argparse.Namespace) -> int:
    """``qai pack cache clear`` handler — returns count of files deleted."""

    async def _go(c: Container) -> int:
        uc = c.app_builder.clear_cache_use_case
        if uc is None:
            raise RuntimeError(
                "ClearCacheUseCase is not wired (stripped-down container?)"
            )
        return await uc.execute()

    deleted = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit({"deleted": deleted})
    return 0


def cmd_pack_taxonomy(args: argparse.Namespace) -> int:
    """``qai pack taxonomy`` handler.

    Uses :class:`GetTaxonomyTreeUseCase` (the rich form with labels /
    icons / I/O metadata + per-task counts) rather than the flat
    :class:`GetTaxonomyUseCase` because the tree shape is what the V1
    gallery / Settings page consume — keeping the CLI in lock-step with
    the HTTP wire surface.
    """

    async def _go(c: Container) -> Any:
        await _seed_factory_packs_if_empty(c)
        uc = c.app_builder.get_taxonomy_tree_use_case
        if uc is None:
            # Fall back to the flat use case if tree isn't wired (eg.
            # stripped-down test containers).
            flat = c.app_builder.get_taxonomy_use_case
            if flat is None:
                raise RuntimeError(
                    "neither GetTaxonomyTreeUseCase nor GetTaxonomyUseCase "
                    "is wired (stripped-down container?)"
                )
            return await flat.execute()
        return await uc.execute()

    tree = run_use_case(
        _go,
        config_file=_resolved_config_file(args),
        repo_root=_resolved_repo_root(args),
    )
    _emit(tree)
    return 0


# ---------------------------------------------------------------------------
# handlers — pass-through to scripts.build.model_builder_cli
# ---------------------------------------------------------------------------


def _passthrough(args: argparse.Namespace) -> "Sequence[str]":
    """Return a defensive copy of the REMAINDER list."""

    rest = getattr(args, "rest", None) or []
    return list(rest)


def cmd_pack_export(args: argparse.Namespace) -> int:
    """``qai pack export [...]`` — forwards to ``pack_export_main``."""

    from scripts.build.model_builder_cli import pack_export_main  # noqa: PLC0415

    rc = pack_export_main(_passthrough(args))
    return int(rc) if rc is not None else 0


def cmd_pack_validate(args: argparse.Namespace) -> int:
    """``qai pack validate [...]`` — forwards to ``pack_validate_main``."""

    from scripts.build.model_builder_cli import pack_validate_main  # noqa: PLC0415

    rc = pack_validate_main(_passthrough(args))
    return int(rc) if rc is not None else 0


def cmd_pack_workspace_init(args: argparse.Namespace) -> int:
    """``qai pack workspace-init [...]`` — forwards to ``workspace_init_main``."""

    from scripts.build.model_builder_cli import workspace_init_main  # noqa: PLC0415

    rc = workspace_init_main(_passthrough(args))
    return int(rc) if rc is not None else 0
