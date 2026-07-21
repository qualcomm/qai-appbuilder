# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai skill`` subcommands — manage the on-disk skill registry + policy.

Desktop App Plan §2.1.1 group J1. Thin wrappers over use cases in
:mod:`qai.user_prefs.application.use_cases.skills`. Two related families
ride on the same ``user_prefs`` namespace:

* **Policy** (forge.config ``skills.{mode,overrides,last_reload}``):
  ``policy`` / ``policy-mode`` / ``toggle`` / ``reload``.
* **Business registry** (live filesystem scan + per-skill 4-state mode):
  ``list`` / ``mode``.

Both families live in the same BC because the per-skill mode merge reads
``forge.config skills.overrides`` to decorate the discovered ``SkillInfo``
records (V1 parity).

Note on ``cli_container(load_skill_caps=True)``
-----------------------------------------------
Earlier drafts of the Desktop App Plan §2.5 sketched a ``load_skill_caps``
runtime flag for ``cli_container()`` to register skill capabilities into
the security policy on demand. That flag has not been implemented in
:mod:`apps.cli._runtime`, and it is **not needed** for the J1 commands:
:class:`ListSkillsUseCase` / :class:`SetSkillModeUseCase` / etc. consume
:class:`qai.platform.skills.SkillDiscovery`, which scans the on-disk
``skills/`` tree without touching the security capability registry. The
flag remains a future opt-in for skill-system commands that genuinely
need security wiring; today's J1 surface is policy-storage + filesystem
listing, neither of which requires it.

Output convention (same as ``qai config``):
* JSON to stdout (``ensure_ascii=False``, indent 2);
* errors on stderr (the dispatcher handles unhandled exceptions);
* business errors → exit 1 (raised), usage errors → exit 2.
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
    "cmd_skill_list",
    "cmd_skill_policy",
    "cmd_skill_policy_mode",
    "cmd_skill_toggle",
    "cmd_skill_mode",
    "cmd_skill_reload",
]


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``qai skill`` subtree to the top-level dispatcher."""

    skill = subparsers.add_parser(
        "skill",
        help="manage the on-disk skill registry and per-skill policy",
        description=(
            "Inspect or change the user's skill preferences (policy "
            "mode, per-skill overrides) and the live skill registry "
            "scanned from <repo_root>/skills."
        ),
    )
    skill_sub = skill.add_subparsers(
        dest="skill_command", required=True, metavar="<subcommand>"
    )

    list_p = skill_sub.add_parser(
        "list",
        help="print the v1-shaped skill list (live filesystem scan + mode merge)",
    )
    list_p.set_defaults(handler=cmd_skill_list)

    policy_p = skill_sub.add_parser(
        "policy",
        help="print aggregated skill policy state (mode / overrides / last_reload)",
    )
    policy_p.set_defaults(handler=cmd_skill_policy)

    policy_mode_p = skill_sub.add_parser(
        "policy-mode",
        help="set the global skill policy mode (typically 'on' / 'off' / 'auto')",
    )
    policy_mode_p.add_argument(
        "mode",
        help=(
            "policy mode value to persist; the use case accepts an opaque "
            "string (the WebUI uses 'on' / 'off' / 'auto' today)"
        ),
    )
    policy_mode_p.set_defaults(handler=cmd_skill_policy_mode)

    toggle_p = skill_sub.add_parser(
        "toggle",
        help="set the per-skill 'enabled' override (bool)",
    )
    toggle_p.add_argument("name", help="skill_id (matches discovered skill)")
    toggle_p.add_argument(
        "value",
        choices=("on", "off"),
        help="'on' → enabled=True, 'off' → enabled=False",
    )
    toggle_p.set_defaults(handler=cmd_skill_toggle)

    mode_p = skill_sub.add_parser(
        "mode",
        help="set the per-skill 4-state run mode (off / cloud / local / both)",
    )
    mode_p.add_argument("name", help="skill_id (matches discovered skill)")
    mode_p.add_argument(
        "value",
        help=(
            "target mode: off / cloud / local / both. local & both require "
            "the skill to be NPU-optimised; the use case raises a 400-class "
            "error otherwise."
        ),
    )
    mode_p.set_defaults(handler=cmd_skill_mode)

    reload_p = skill_sub.add_parser(
        "reload",
        help="bump skills.last_reload (V1 parity); does NOT re-scan the FS",
    )
    reload_p.set_defaults(handler=cmd_skill_reload)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emit_json(value: Any) -> None:
    """Pretty-print ``value`` to stdout as JSON (CN-friendly)."""

    sys.stdout.write(
        json.dumps(value, ensure_ascii=False, indent=2, default=str)
    )
    sys.stdout.write("\n")
    sys.stdout.flush()


def _runtime_kwargs(args: argparse.Namespace) -> dict[str, Path | None]:
    return {
        "repo_root": getattr(args, "repo_root", None),
        "config_file": getattr(args, "config_file", None),
    }


# ---------------------------------------------------------------------------
# handlers
# ---------------------------------------------------------------------------


def cmd_skill_list(args: argparse.Namespace) -> int:
    """``qai skill list`` — live FS scan + per-skill mode merge."""

    async def _go(c: Container) -> dict[str, Any]:
        return await c.user_prefs.list_skills_use_case.execute()

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_skill_policy(args: argparse.Namespace) -> int:
    """``qai skill policy`` — aggregate state from ``forge.config``."""

    async def _go(c: Container) -> dict[str, Any]:
        return await c.user_prefs.get_skill_policy_use_case.execute()

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_skill_policy_mode(args: argparse.Namespace) -> int:
    """``qai skill policy-mode <mode>``."""

    mode: str = args.mode

    async def _go(c: Container) -> dict[str, Any]:
        return await c.user_prefs.set_skill_policy_mode_use_case.execute(mode)

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_skill_toggle(args: argparse.Namespace) -> int:
    """``qai skill toggle <name> <on|off>``.

    The use case takes a ``bool`` (``enabled``); we map the choices to
    keep the CLI surface explicit (``on`` / ``off`` reads better than
    ``true`` / ``false`` in shell scripts and is unambiguous about the
    parsing contract).
    """

    name: str = args.name
    enabled: bool = args.value == "on"

    async def _go(c: Container) -> dict[str, Any]:
        return await c.user_prefs.toggle_skill_use_case.execute(
            name, enabled
        )

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_skill_mode(args: argparse.Namespace) -> int:
    """``qai skill mode <name> <off|cloud|local|both>``.

    Surface NPU-mismatch errors as exit 1 with a clear stderr message
    instead of leaking the SkillModeNotAllowedError traceback. The
    dispatcher's default handler would still produce a usable error,
    but matching V1's 400 semantics with a one-line message is friendlier
    for shell pipelines.
    """

    name: str = args.name
    mode: str = args.value

    # Lazy-import the domain exceptions so ``qai --help`` doesn't pay the
    # cost of importing the user_prefs application layer.
    from qai.user_prefs.application.use_cases.skills import (  # noqa: PLC0415
        SkillModeNotAllowedError,
        SkillNotFoundError,
    )

    async def _go(c: Container) -> dict[str, Any]:
        return await c.user_prefs.set_skill_mode_use_case.execute(name, mode)

    try:
        result = run_use_case(_go, **_runtime_kwargs(args))
    except SkillNotFoundError as exc:
        sys.stderr.write(f"unknown skill: {exc.skill_id}\n")
        return 1
    except SkillModeNotAllowedError as exc:
        sys.stderr.write(
            f"skill {exc.skill_id!r}: mode {exc.mode!r} requires NPU "
            f"optimisation but this skill is not NPU-optimised\n"
        )
        return 1

    _emit_json(result)
    return 0


def cmd_skill_reload(args: argparse.Namespace) -> int:
    """``qai skill reload`` — bump ``skills.last_reload`` (V1 parity)."""

    async def _go(c: Container) -> dict[str, Any]:
        return await c.user_prefs.reload_skills_use_case.execute()

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0
