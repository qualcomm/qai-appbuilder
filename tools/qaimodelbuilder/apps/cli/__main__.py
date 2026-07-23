"""``apps.cli.__main__`` — top-level ``qai`` argparse dispatcher (D1 skeleton).

Desktop App Plan §1.2 / §2.1 / §2.5 / §2.6. The existing
``[project.scripts] qai = "apps.cli.__main__:main"`` entry in ``pyproject.toml``
pointed at this module since before the file existed; D1 lands the file (no
console-script churn) so ``pip install -e .`` followed by ``qai --help``
finally works end-to-end.

Scope of D1 (intentional)
-------------------------
A single PoC command group — ``qai config {get,set}`` — exercises the full
runtime path (``Container.build`` → DB start → migrate → use case → tear
down) without committing the whole §2.1.1 11-group surface up front. The
dispatcher is structured so adding the rest in D2 is a one-line ``register``
import per group; see the placeholder block below.

Exit code conventions (POSIX)
-----------------------------
* 0  — success
* 1  — business / runtime error (use case raised, DB locked, etc.); the
       traceback prints to stderr.
* 2  — usage error (unknown command, missing argument, malformed JSON).
       argparse already uses 2 for its own errors so this is consistent.
* 130 — interrupted by SIGINT (matches the shell convention 128+SIGINT=130).
"""

from __future__ import annotations

import argparse
import contextlib
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from apps.cli.commands import config as _config_cmds

# D2 command groups — imported lazily inside :func:`build_parser` so a
# missing or in-development module doesn't break ``qai --help`` for the
# rest of the surface.  Each module exposes a top-level ``register(sub)``
# that adds its own subparsers; see ``apps/cli/commands/config.py`` for
# the canonical shape.
_D2_GROUPS = (
    "service",          # B: qai serve / api / service ... (model_runtime)
    "install",          # C: install-qairt / install-pack-deps / uninstall / compile-factory
    "pack",             # D: qai pack list/show/import/deps-*/cache/...
    "run",              # E: qai run list/show/delete/cancel/artifacts/export/worker
    "conv",             # F1: qai conv list/show/rename/delete/compact/...
    "code",             # F2: qai code session/config/creds/oc/skill/checkpoint/health
    # G (model_catalog): cloud-provider config moved to `qai config provider`;
    # on-device LLM management intentionally unsupported in the CLI
    # (cli-interactive-design §4bis). The `qai model` group was removed.
    "policy",           # H: qai policy / perm / security / audit
    "channel",          # I: qai channel <verb> + qai channel wechat/feishu
    "skill",            # J1: qai skill list/policy/toggle/mode/reload
    "dep",              # J2: qai dep pending/approve/reject + exec profiles
    "service_release",  # K: qai service-release versions/models/install/aria2c/...
    "build",            # L1: qai build (Model Builder agentic session, REPL)
    "app",              # L2: qai app <pack> (App Builder inference, one-shot + REPL)
)

__all__ = ["main", "build_parser"]

#: The subparsers ``metavar`` (also used verbatim in the hand-rolled usage
#: error :func:`main` raises for the no-command + non-TTY case, so the two
#: never drift apart — see ``main``'s ``args.command is None`` branch).
_COMMAND_METAVAR = "<command>"


# ---------------------------------------------------------------------------
# Placeholders for D2 (Desktop App Plan §2.1.1):
#
#   B. service / process    — qai serve / qai api / qai service ...
#   C. install / deploy     — qai install-qairt / install-pack-deps /
#                             uninstall / compile-factory
#   D. Pack management      — qai pack list/show/import/deps-*/cache/...
#   E. run history          — qai run list/show/delete/cancel/artifacts/export
#   F. conversation / code  — qai conv * / qai code session/config/...
#   G. model catalog        — cloud providers via `qai config provider`;
#                             on-device LLM mgmt unsupported in CLI (§4bis)
#   H. security           — qai policy / perm / security / audit
#   I. channels             — qai channel <verb> + qai channel wechat/feishu
#                             subgroups (V1 ↔ V2 full inventory; see §2.1.1.I)
#   J. UX helpers           — qai shell-completion / qai version
#   K. dev / diag           — qai diag / qai support-bundle
#
# Each will land as ``from apps.cli.commands import <group> as _<group>``
# above + ``_<group>.register(subparsers)`` inside :func:`build_parser`.
# Keeping the registration colocated with each group's handlers (see
# ``apps/cli/commands/config.py:register``) means a new command tree only
# touches its own file plus a one-line wire-up here.
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Construct the top-level ``qai`` parser.

    Split out from :func:`main` so tests can introspect the parser (e.g.
    assert ``--help`` mentions ``config``) without invoking sys.exit.
    """

    parser = argparse.ArgumentParser(
        prog="qai",
        description=(
            "QAIModelBuilder unified CLI — one entry point for every "
            "operator-facing command (config, model packs, channels, "
            "security, run history, etc.). See ``qai <group> --help`` "
            "for per-group documentation."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=None,
        metavar="<path>",
        help=(
            "override the repository root (the directory containing "
            "src/ and apps/). Defaults to auto-detection from this "
            "module's location, matching apps.api. Used by tests and by "
            "operators running against an alternate checkout."
        ),
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        dest="config_file",
        metavar="<server.toml>",
        help=(
            "explicit path to a server.toml; overrides --repo-root for "
            "settings discovery. Same flag the API server accepts."
        ),
    )

    subparsers = parser.add_subparsers(
        dest="command",
        required=False,
        metavar=_COMMAND_METAVAR,
        title="commands",
    )

    # D1: ``config`` group is the canonical reference; always present.
    _config_cmds.register(subparsers)

    # D2: discover and register the rest.  Each group is loaded
    # independently so an in-development module can fail to import
    # without breaking the whole CLI surface — its subcommand simply
    # won't appear in ``qai --help`` until it lands.  Failures print
    # a single warning to stderr (visible only in --help / error
    # paths, not in normal command runs because subparsers don't run
    # this code on a successful dispatch).
    import importlib
    for _group in _D2_GROUPS:
        try:
            _mod = importlib.import_module(f"apps.cli.commands.{_group}")
        except ImportError:
            # Group not yet implemented; skip silently. We could log a
            # warning, but during D2 incremental landing we expect
            # several groups to be missing at any given moment and
            # warning noise would obscure real issues.
            continue
        _register = getattr(_mod, "register", None)
        if _register is None:
            sys.stderr.write(
                f"qai: command group {_group!r} loaded but has no register(); "
                f"this is a bug in apps/cli/commands/{_group}.py\n"
            )
            continue
        try:
            _register(subparsers)
        except Exception as exc:  # noqa: BLE001 — surface bug, don't kill CLI
            sys.stderr.write(
                f"qai: failed to register {_group!r}: {type(exc).__name__}: {exc}\n"
            )

    return parser


def _force_utf8_streams() -> None:
    """Ensure ``sys.stdout`` / ``sys.stderr`` encode UTF-8 (cross-platform).

    On Windows, when ``qai`` output is redirected to a pipe / file / captured
    by ``subprocess`` and ``PYTHONIOENCODING`` is unset, CPython picks the
    legacy ANSI code page (cp1252 / GBK) for the stream's codec. Any
    non-ASCII glyph in the help / banner text (em-dash, curly quotes, the
    Chinese strings several command ``help=`` lines carry) then raises
    ``UnicodeEncodeError: 'charmap' codec can't encode ...`` and aborts the
    process with a non-zero exit — e.g. ``qai --help | more`` or any CI
    runner capturing stdout would crash.

    Reconfiguring the streams to UTF-8 (with ``errors="replace"`` as a final
    safety net) fixes this without depending on the console code page. This
    is cross-platform neutral: POSIX streams are already UTF-8 so the call is
    a harmless no-op, and the ``hasattr`` guard keeps it safe on any exotic
    stream object that does not expose :meth:`io.TextIOWrapper.reconfigure`
    (e.g. a captured ``StringIO`` under pytest).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            # Stream already detached / not reconfigurable: leave as-is.
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def _stdin_is_tty() -> bool:
    """Defensive ``sys.stdin.isatty()`` (see ``_render.py``'s ``_stream_is_tty``)."""
    try:
        return bool(sys.stdin.isatty())
    except Exception:  # noqa: BLE001 — closed / fake stream
        return False


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m apps.cli`` / ``qai`` entry point.

    Returns the process exit code (caller does ``raise SystemExit(main())``).
    """

    _force_utf8_streams()

    parser = build_parser()

    # ``parse_args`` exits the process with code 2 on usage errors. That is
    # the documented behaviour of argparse and the right thing for a CLI
    # (matches ``ls --no-such-flag`` etc.), so we do NOT trap SystemExit:
    # the operator gets the same diagnostic argparse already prints.
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # argparse uses int code; default to 2 if it set None.
        return int(exc.code) if exc.code is not None else 2

    handler = getattr(args, "handler", None)
    if handler is None:
        if args.command is None:
            # The top-level subparsers are now ``required=False`` (rather
            # than letting argparse itself raise the usage error) so this
            # branch can first check whether stdin is an interactive TTY —
            # if so, ``qai`` with no subcommand drops into the default chat
            # REPL (Phase 2 §Step 4) instead of a usage error.
            if _stdin_is_tty():
                from apps.cli.commands.chat import cmd_chat  # noqa: PLC0415

                handler = cmd_chat
            else:
                # Non-TTY (pipe / CI) with no subcommand: reproduce the
                # exact usage-error text + exit code argparse itself used
                # to raise for the same input when the subparsers were
                # ``required=True`` (see ``_COMMAND_METAVAR``).
                try:
                    parser.error(
                        f"the following arguments are required: {_COMMAND_METAVAR}"
                    )
                except SystemExit as exc:
                    return int(exc.code) if exc.code is not None else 2
        else:
            # Should not happen — every leaf subparser must call
            # ``set_defaults(handler=...)``. Guard explicitly so we surface a
            # clear message instead of an AttributeError trace if a future
            # group forgets the wiring.
            sys.stderr.write(
                f"qai: no handler registered for command {args.command!r}\n"
            )
            return 2

    try:
        return int(handler(args))
    except KeyboardInterrupt:
        # Match the shell convention ``128 + SIGINT (2) = 130``. The CLI
        # supervisor (qai-serve) uses the same value when the user mashes
        # Ctrl+C during a long-running command.
        sys.stderr.write("\nqai: interrupted\n")
        return 130
    except SystemExit as exc:
        # A handler may legitimately ``raise SystemExit(N)``; honour the
        # code rather than coercing to 1.
        return int(exc.code) if isinstance(exc.code, int) else 1
    except Exception as exc:  # noqa: BLE001 — top-level boundary
        # Use-case / DB / IO error. Print the traceback to stderr (so it
        # never pollutes a piped stdout) and exit 1. Future polish can add
        # a ``--quiet`` flag that prints just ``str(exc)``; for D1 the full
        # trace makes operator triage straightforward.
        sys.stderr.write(f"qai: {type(exc).__name__}: {exc}\n")
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover — process boundary
    raise SystemExit(main())
