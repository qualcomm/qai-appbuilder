# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai dep`` and ``qai exec`` subcommands — broker-side approval / profiles.

Desktop App Plan §2.1.1 group J2. Two BCs are exposed here because they
share a conceptual frame ("which commands the sandbox is allowed to run")
and because keeping ``qai exec profiles`` next to the dep-broker queue
matches V1's "Security" panel layout in the WebUI.

* ``qai dep pending`` / ``approve`` / ``reject`` — :mod:`qai.dependency_approval`.
* ``qai exec profiles`` — :mod:`qai.command_policy`.

Single-module, two-group registration
-------------------------------------
``apps.cli.__main__._D2_GROUPS`` only auto-imports ``dep``; ``exec`` is
not in the list. Rather than carve a near-empty ``exec.py`` for the one
``exec profiles`` command, this module's :func:`register` attaches BOTH
top-level commands to the dispatcher. This is the documented escape
hatch for cousin BCs that share a UI surface (Desktop App Plan §2.1.1
allows a group module to register additional sibling top-levels when
the surface is small).

Output convention (consistent with the other D2 groups):
* JSON to stdout (``ensure_ascii=False``, indent 2, ``default=str`` for
  the few datetime fields on ``PendingRequest``);
* errors on stderr; business → exit 1, usage → exit 2.
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
    "cmd_dep_pending",
    "cmd_dep_approve",
    "cmd_dep_reject",
    "cmd_exec_profiles",
]


# ---------------------------------------------------------------------------
# argparse registration
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach ``qai dep`` AND ``qai exec`` to the top-level dispatcher.

    ``__main__._D2_GROUPS`` only auto-loads ``dep``; we attach ``exec``
    from here to keep the related surface in one file. See module
    docstring for rationale.
    """

    # ---- qai dep -----------------------------------------------------
    dep = subparsers.add_parser(
        "dep",
        help="manage the dep_broker pip-install approval queue",
        description=(
            "List or resolve dependency-install requests buffered by the "
            "in-memory dep_broker. Useful for triaging interactive pip "
            "approvals from the sandbox without opening the WebUI."
        ),
    )
    dep_sub = dep.add_subparsers(
        dest="dep_command", required=True, metavar="<subcommand>"
    )

    pending_p = dep_sub.add_parser(
        "pending",
        help="print all PENDING dep-install requests",
    )
    pending_p.set_defaults(handler=cmd_dep_pending)

    approve_p = dep_sub.add_parser(
        "approve",
        help="approve a pending request by id",
    )
    approve_p.add_argument(
        "request_id", help="ULID/UUID of the pending request"
    )
    approve_p.set_defaults(handler=cmd_dep_approve)

    reject_p = dep_sub.add_parser(
        "reject",
        help="reject a pending request by id",
    )
    reject_p.add_argument(
        "request_id", help="ULID/UUID of the pending request"
    )
    reject_p.set_defaults(handler=cmd_dep_reject)

    # ---- qai exec ----------------------------------------------------
    exec_p = subparsers.add_parser(
        "exec",
        help="inspect exec_broker profiles (command-execution approval)",
        description=(
            "Inspect the static exec profiles loaded from "
            "factory/config/exec_profiles/*.toml and the broker's "
            "enabled state. Read-only today (V2 D2 surface)."
        ),
    )
    exec_sub = exec_p.add_subparsers(
        dest="exec_command", required=True, metavar="<subcommand>"
    )

    profiles_p = exec_sub.add_parser(
        "profiles",
        help="print loaded exec profiles + enabled flag",
    )
    profiles_p.set_defaults(handler=cmd_exec_profiles)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emit_json(value: Any) -> None:
    """Pretty-print ``value`` to stdout as JSON.

    ``default=str`` covers the ``datetime`` and ``RequestStatus`` enum
    fields on :class:`qai.dependency_approval.domain.PendingRequest`.
    """

    sys.stdout.write(
        json.dumps(value, ensure_ascii=False, indent=2, default=str)
    )
    sys.stdout.write("\n")
    sys.stdout.flush()


def _to_dict(obj: Any) -> Any:
    """Best-effort dataclass → dict conversion for JSON serialisation.

    ``PendingRequest`` and ``CommandProfile`` are slotted dataclasses;
    :func:`dataclasses.asdict` recurses into nested dataclasses and
    converts collections, which is exactly what JSON needs. Falls back
    to the value verbatim when the object is not a dataclass instance.
    """
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return dataclasses.asdict(obj)
    return obj


def _runtime_kwargs(args: argparse.Namespace) -> dict[str, Path | None]:
    return {
        "repo_root": getattr(args, "repo_root", None),
        "config_file": getattr(args, "config_file", None),
    }


# ---------------------------------------------------------------------------
# qai dep handlers
# ---------------------------------------------------------------------------


def cmd_dep_pending(args: argparse.Namespace) -> int:
    """``qai dep pending`` — list all PENDING requests as a JSON array."""

    async def _go(c: Container) -> list[Any]:
        items = await c.dependency_approval.get_pending_requests_use_case.execute()
        return [_to_dict(item) for item in items]

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0


def cmd_dep_approve(args: argparse.Namespace) -> int:
    """``qai dep approve <id>``.

    Returns ``{"resolved": true|false, "id": ..., "decision": "approve"}``.
    ``resolved=false`` means the id was not in the pending queue (already
    decided / never existed); the use case treats that as non-fatal, but
    from a script's perspective it's still "the operation didn't take
    effect", so we exit 1 to make it observable in pipelines (``qai dep
    approve $id && echo ok`` is now meaningful). The JSON body still
    prints to stdout so callers can introspect.
    """

    request_id: str = args.request_id

    async def _go(c: Container) -> dict[str, Any]:
        ok = await c.dependency_approval.resolve_request_use_case.execute(
            request_id, "approve"
        )
        return {"resolved": ok, "id": request_id, "decision": "approve"}

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0 if result.get("resolved") else 1


def cmd_dep_reject(args: argparse.Namespace) -> int:
    """``qai dep reject <id>`` — same shape as ``approve``.

    Same exit-code policy: ``resolved=false`` → exit 1 so callers can
    detect "the request id wasn't pending".
    """

    request_id: str = args.request_id

    async def _go(c: Container) -> dict[str, Any]:
        ok = await c.dependency_approval.resolve_request_use_case.execute(
            request_id, "reject"
        )
        return {"resolved": ok, "id": request_id, "decision": "reject"}

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0 if result.get("resolved") else 1


# ---------------------------------------------------------------------------
# qai exec handlers
# ---------------------------------------------------------------------------


def cmd_exec_profiles(args: argparse.Namespace) -> int:
    """``qai exec profiles``.

    Returns ``{"enabled": <bool>, "profiles": [<CommandProfile>...]}``.
    ``enabled`` mirrors the broker's master switch (D11 default: OFF).
    """

    async def _go(c: Container) -> dict[str, Any]:
        result = await c.command_policy.get_exec_profiles_use_case.execute()
        # ``GetExecProfilesResult`` is itself a dataclass; flatten so the
        # CLI emits ``{"enabled": ..., "profiles": [...]}`` rather than
        # nesting under the result class name.
        return {
            "enabled": result.enabled,
            "profiles": [_to_dict(p) for p in result.profiles],
        }

    result = run_use_case(_go, **_runtime_kwargs(args))
    _emit_json(result)
    return 0
