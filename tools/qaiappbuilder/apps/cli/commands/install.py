# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai install-*`` / ``qai uninstall`` / ``qai compile-factory`` — thin wrappers.

Desktop App Plan §2.1.1 group C. Each subcommand is a one-line bridge to
the existing console-script entry point in ``scripts/``. The CLI is the
single ``qai <verb>`` surface (Setup.bat / Start.bat / Uninstall.bat
already shell out to it); the actual install logic stays in the
``scripts/`` modules so V1 → V2 parity does not require a second
implementation.

Why no Container here
---------------------
Unlike the other D2 groups, install / uninstall / compile-factory do not
go through a use case in ``src/qai/<ctx>/application/use_cases/``: they
are bootstrap operations that *create* the data tree, install Python
venvs, or compile factory seeds — actions that pre-date the running
``Container``. Each script's ``main(argv)`` already owns its argparse
surface (with all its flags and exit codes); the CLI's job is purely to
forward ``sys.argv`` after the ``qai`` group prefix. Adding a layer of
re-parsing here would risk drifting from the script's own help text.

Forwarding pattern
------------------
Each registered subparser uses ``argparse.REMAINDER`` to capture every
token after the verb verbatim. The handler then calls
``scripts.<...>:main(rest)`` and returns its int exit code. This means:

* ``qai install-qairt --check`` → ``install_qairt.main(["--check"])``;
* ``qai install-qairt --help`` → forwarded too, so the script's full
  help text wins (the ``qai install-qairt`` help only summarises which
  flags the underlying script accepts).

This is intentional: the V1 ``.bat`` files invoke the same scripts; V2
keeps the contract identical so an operator's muscle memory transfers.

Cross-platform note (AGENTS.md §0 forward compatibility): QAIRT install
is Windows-only by design (the SDK is Windows-only). The wrappers do
not add any platform guard — the underlying script's first action is to
detect the platform and exit 0 / log a no-op on non-Windows, so a
future Linux CI lane that runs ``qai install-qairt --check`` will not
crash.
"""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover — import shape, not runtime
    from collections.abc import Sequence

__all__ = [
    "register",
    "cmd_install_qairt",
    "cmd_install_pack_deps",
    "cmd_uninstall",
    "cmd_compile_factory",
]


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach ``qai install-qairt`` / ``install-pack-deps`` / ``uninstall`` /
    ``compile-factory`` to the top-level dispatcher.

    Each is a top-level command (not a subgroup) so the user types
    ``qai install-qairt --check`` rather than ``qai install qairt
    --check`` — matches the Desktop App Plan §2.1.1.C verbatim.
    """

    install_qairt = subparsers.add_parser(
        "install-qairt",
        help="install the QAIRT SDK + x64-py310 venv (delegates to scripts.init.install_qairt)",
        description=(
            "Install the QAIRT SDK and the x86_64 Python 3.10 venv used "
            "by model conversion. All flags are forwarded verbatim to "
            "scripts/init/install_qairt.py — see ``qai install-qairt --help`` "
            "(which prints that script's own help) for the full surface."
        ),
        # ``add_help=False`` so ``qai install-qairt --help`` is forwarded to
        # the inner script rather than intercepted here. argparse normally
        # eats ``-h`` / ``--help``; turning it off lets us delegate.
        add_help=False,
        # ``prefix_chars='\x00'`` disables argparse's option-detection in
        # this subparser: every token (including ``--check``) becomes a
        # positional for the catch-all ``rest`` argument below. Without
        # this, argparse rejects ``--check`` because ``nargs=REMAINDER``
        # only captures non-option tokens (a long-standing argparse
        # limitation; see Python issue 17050). The NUL byte is a sentinel
        # no real CLI flag can use.
        prefix_chars="\x00",
    )
    install_qairt.add_argument(
        "rest", nargs="*", help=argparse.SUPPRESS
    )
    install_qairt.set_defaults(handler=cmd_install_qairt)

    install_pack_deps = subparsers.add_parser(
        "install-pack-deps",
        help="install requirements for all chat-feature packs (delegates to scripts.setup.install_app_builder_deps)",
        description=(
            "Install Python requirements aggregated from every Pack under "
            "``factory/chat_features``. No flags today — kept as positional "
            "catch-all so the script can grow flags later without churning "
            "the CLI."
        ),
        add_help=False,
        prefix_chars="\x00",
    )
    install_pack_deps.add_argument(
        "rest", nargs="*", help=argparse.SUPPRESS
    )
    install_pack_deps.set_defaults(handler=cmd_install_pack_deps)

    uninstall = subparsers.add_parser(
        "uninstall",
        help="uninstall QAIModelBuilder (delegates to scripts.init.uninstall)",
        description=(
            "Remove the install tree under %LOCALAPPDATA%\\QAIModelBuilder. "
            "All flags forwarded verbatim — see ``qai uninstall --help`` "
            "for the full surface (``-y`` / ``--dry-run`` / ``--clean-uv`` "
            "etc.)."
        ),
        add_help=False,
        prefix_chars="\x00",
    )
    uninstall.add_argument(
        "rest", nargs="*", help=argparse.SUPPRESS
    )
    uninstall.set_defaults(handler=cmd_uninstall)

    compile_factory = subparsers.add_parser(
        "compile-factory",
        help="compile factory_source/* into factory/* seeds (delegates to scripts.build.compile_factory)",
        description=(
            "Build the on-disk ``factory/`` tree from ``factory/_source/``. "
            "All flags forwarded — see ``qai compile-factory --help`` for "
            "``--apply`` / ``--verify`` / ``--dry-run`` / ``--include`` / "
            "``--exclude`` / ``--timestamp`` etc."
        ),
        add_help=False,
        prefix_chars="\x00",
    )
    compile_factory.add_argument(
        "rest", nargs="*", help=argparse.SUPPRESS
    )
    compile_factory.set_defaults(handler=cmd_compile_factory)

    # ── qai api — direct uvicorn (debug / development only) ──────────
    api_p = subparsers.add_parser(
        "api",
        help="start FastAPI server directly via uvicorn (debug; no reboot supervisor)",
        description=(
            "Start the FastAPI server directly with uvicorn — no reboot "
            "supervisor. Intended for development / debugger attach. "
            "For production use ``qai serve`` (or ``qai-serve``) which "
            "wraps this with the reboot-75 supervisor. "
            "All flags forwarded to ``apps.api.main:main`` — see "
            "``qai api --help`` for ``--host`` / ``--port`` / ``--reload`` / "
            "``--config`` etc."
        ),
        add_help=False,
        prefix_chars="\x00",
    )
    api_p.add_argument("rest", nargs="*", help=argparse.SUPPRESS)
    api_p.set_defaults(handler=cmd_api)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _passthrough(args: argparse.Namespace) -> "Sequence[str]":
    """Return the REMAINDER list captured by argparse.

    ``args.rest`` is always a (possibly empty) ``list[str]`` because the
    parser always sees the ``rest`` argument; we accept the empty list
    as "no flags" rather than coerce to ``None``.
    """
    rest = getattr(args, "rest", None) or []
    # Defensive copy: the underlying scripts may mutate argv in-place.
    return list(rest)


def _delegate(target: Callable[..., int | None], args: argparse.Namespace) -> int:
    """Call ``target(passthrough)`` and coerce ``None`` → ``0``.

    Every install script's ``main(argv)`` returns ``int`` on success/
    failure or occasionally ``None`` (Python's implicit return) when the
    operation is a no-op. Treat ``None`` as success — the script would
    have raised SystemExit on a real error.
    """
    rc = target(_passthrough(args))
    return int(rc) if rc is not None else 0


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def cmd_install_qairt(args: argparse.Namespace) -> int:
    """``qai install-qairt [...]``.

    Imported lazily so ``qai --help`` (which runs ``register()`` for
    every group) does not pay the cost of pulling in
    ``scripts.init.install_qairt`` (which transitively imports aria2c
    helpers, archive utilities, etc.). Lazy import keeps ``qai --help``
    snappy.
    """
    from scripts.init import install_qairt  # noqa: PLC0415 — lazy

    return _delegate(install_qairt.main, args)


def cmd_install_pack_deps(args: argparse.Namespace) -> int:
    """``qai install-pack-deps``."""
    from scripts.setup import install_app_builder_deps  # noqa: PLC0415

    return _delegate(install_app_builder_deps.main, args)


def cmd_uninstall(args: argparse.Namespace) -> int:
    """``qai uninstall [...]``."""
    from scripts.init import uninstall as uninstall_mod  # noqa: PLC0415

    return _delegate(uninstall_mod.main, args)


def cmd_compile_factory(args: argparse.Namespace) -> int:
    """``qai compile-factory [...]``."""
    from scripts.build import compile_factory  # noqa: PLC0415

    return _delegate(compile_factory.main, args)


def cmd_api(args: argparse.Namespace) -> int:
    """``qai api [...]`` — start FastAPI server directly via uvicorn.

    No reboot supervisor — for production use ``qai serve`` instead, which
    wraps ``apps.api.main:main`` in the reboot-75 supervisor (see
    ``apps.cli.serve``). This subcommand exists so ``Setup.bat`` /
    ``Start.bat`` aren't the only paths to reach the bare uvicorn entry,
    matching ``desktop-app-plan §2.1.1.B``.
    """
    from apps.api import main as api_main  # noqa: PLC0415

    return _delegate(api_main.main, args)
