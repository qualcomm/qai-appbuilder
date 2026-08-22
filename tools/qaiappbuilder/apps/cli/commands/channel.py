# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai channel`` subcommands — Feishu / WeChat full command tree (D2 group I).

Desktop App Plan §2.1.1.I + §2.6 + §2.7. Thin wrappers over channels-context
use cases (``ChannelsServices`` namespace), one CLI subcommand per HTTP route
the WebUI calls. The CLI is a sibling adapter to the HTTP layer (NOT a wrapper
around HTTP); both consume the SAME application use cases via DI, so the
business behaviour is identical even though the wire shapes differ.

Surface (matches §2.1.1.I)
--------------------------
Kind-agnostic verbs::

    qai channel register --kind {feishu,wechat} --name <n>
                         [--secret-value <s>] [--meta k=v ...]
    qai channel list [--kind feishu|wechat]
    qai channel show <instance-id>
    qai channel delete <instance-id> [--yes]
    qai channel start <instance-id>
    qai channel stop <instance-id>
    qai channel status <instance-id>          # show alias

    qai channel config get <instance-id>
    qai channel config set <instance-id>
        [--auto-start | --no-auto-start] [--kv k=v ...] [--app-secret <s>]

    qai channel proxy get <instance-id>
    qai channel proxy set <instance-id>
        [--url URL] [--username U]
        [--password P | --password-stdin | --no-password]

    qai channel model get <instance-id>
    qai channel model set <instance-id> --model-id ID [--provider P]

    qai channel binding list <instance-id>
    qai channel binding set <instance-id> --conv-id C --user-id U
    qai channel binding delete <instance-id> --conv-id C

    qai channel session bind <instance-id> --user-id U
        [--internal-user-id IUID] [--coding-session-id CSID]
    qai channel session lookup <instance-id> --user-id U

    qai channel push <instance-id> --user-id U
        (--text T | --text-stdin) [--page-format "({i}/{n})"]

WeChat-only verbs::

    qai channel wechat login <instance-id> [--force]
    qai channel wechat qr <instance-id> --challenge-id ID
        [--out PATH.png | --print-ascii]
    qai channel wechat qr-status <instance-id> --challenge-id ID [--watch]
    qai channel wechat qr-issue <instance-id>
    qai channel wechat logout <instance-id>

Known limitations / explicit P2 backlog
---------------------------------------
* ``channel start`` / ``channel stop`` mutate the persisted instance state and
  the use case wires up the inbound long-poll lifecycle — but the consumer
  task tracking dict (``manage_lifecycle._INBOUND_CONSUMERS``) is process-
  local. A CLI in-process invocation cannot signal a *separately running*
  daemon to start / stop its inbound consumers; it can only drive its own
  short-lived loop. Production use therefore requires either (a) the user
  is on a host with no daemon running, or (b) a follow-up P2 enhancement
  that detects a live daemon and routes start/stop over HTTP. Both cases
  produce a correctly persisted instance row; the divergence is only in
  the in-memory consumer task. Tracked in PENDING-WORK.md.

* ``channel delete`` calls ``ChannelInstanceRepositoryPort.delete`` directly
  (no use case exists; §2.1.1.I 🟡 row). The CLI also clears the SecretStore
  records this CLI knows about (signing secret + Feishu app_secret + proxy
  password) so a delete leaves no orphan credentials. Bindings live inside
  the aggregate's metadata blob and disappear with the row; session-index
  rows are best-effort cleared via the session repo. A dedicated
  ``DeleteChannelInstanceUseCase`` that publishes a domain event + lets
  other contexts react is left as a P2 follow-up (the four CHAN-* gaps in
  PENDING-WORK.md cover related work but are explicitly out of scope here).

* ``--text-stdin`` / ``--password-stdin`` read the entire stdin to avoid
  exposing secrets in shell history. Empty stdin is treated as empty input.

* V1↔V2 gaps CHAN-1..4 (desktop-app-plan §2.7) are NOT implemented here per
  the L2 prompt — they are tracked in PENDING-WORK.md and will land when
  the underlying capability lands in the application layer.

Exit codes
----------
* 0  — success
* 1  — business / runtime error (raised by use case or repository)
* 2  — usage error (argparse / malformed input)
* 130 — SIGINT (handled by the top-level dispatcher)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from qai.platform.errors import NotFoundError

from apps.api.di import Container
from apps.cli._runtime import run_use_case

__all__ = ["register"]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _emit(payload: Any) -> None:
    """Pretty-print a JSON-serialisable payload to stdout (UTF-8, indent 2).

    Mirrors :func:`apps.cli.commands.config._emit_doc` so every CLI command
    in the codebase produces the same wire shape on stdout — operators and
    downstream tooling (``jq``) see one consistent contract.
    """
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2))
    sys.stdout.write("\n")
    sys.stdout.flush()


def _parse_kv(items: list[str], *, flag: str) -> dict[str, str]:
    """Parse a list of ``key=value`` strings into a dict.

    Used by ``--meta`` and ``--kv``. Empty list yields an empty dict.
    Malformed entries (no ``=`` separator) raise :class:`SystemExit` with
    exit code 2 — matches argparse's own usage-error convention so an
    operator typing ``--kv badkey`` gets the same diagnostic experience as
    ``--unknown-flag``.
    """
    out: dict[str, str] = {}
    for raw in items:
        if "=" not in raw:
            sys.stderr.write(
                f"qai channel: {flag} requires <key>=<value>; got {raw!r}\n"
            )
            raise SystemExit(2)
        k, v = raw.split("=", 1)
        if not k:
            sys.stderr.write(
                f"qai channel: {flag} key must be non-empty; got {raw!r}\n"
            )
            raise SystemExit(2)
        out[k] = v
    return out


def _read_stdin_str() -> str:
    """Read the full stdin and return it as a string.

    Used for ``--text-stdin`` / ``--password-stdin``. We keep the trailing
    newline stripped because shells / heredocs almost always add one and
    operators don't want a phantom blank line at the end of their messages.
    """
    data = sys.stdin.read()
    # Strip ONE trailing newline (the typical shell / heredoc artefact)
    # without disturbing intentional blank-line content.
    if data.endswith("\r\n"):
        return data[:-2]
    if data.endswith("\n"):
        return data[:-1]
    return data


def _kind_from_str(value: str) -> "ChannelKind":  # noqa: F821 - forward ref
    """Map a CLI-friendly ``feishu`` / ``wechat`` slug to :class:`ChannelKind`.

    Imported lazily so a ``qai --help`` invocation that never reaches the
    channels group doesn't pay the cost of importing the channels domain.
    """
    from qai.channels.domain import ChannelKind

    try:
        return ChannelKind(value.lower())
    except ValueError as exc:
        raise SystemExit(2) from exc


def _instance_to_dict(instance: Any) -> dict[str, Any]:
    """Serialise a :class:`ChannelInstance` to the same shape as the HTTP DTO.

    Matches :func:`interfaces.http.routes.channels._instance_to_dto` so a
    CLI ``qai channel show`` and an HTTP ``GET /api/{kind}/status`` over the
    same instance produce the same observable JSON payload.
    """
    return {
        "instance_id": instance.instance_id.value,
        "kind": instance.kind.value,
        "name": instance.name,
        "status": instance.status.value,
        "last_error": instance.last_error,
        "created_at": instance.created_at.isoformat(),
        "updated_at": instance.updated_at.isoformat(),
        "metadata": dict(instance.metadata),
    }


# ---------------------------------------------------------------------------
# kind-agnostic top-level handlers
# ---------------------------------------------------------------------------


def cmd_register(args: argparse.Namespace) -> int:
    """``qai channel register --kind ... --name ...``.

    Forwards to :class:`RegisterChannelInstanceUseCase` with the same Feishu /
    WeChat secret-namespace conventions :func:`interfaces.http.routes.channels`
    uses (Feishu pins to ``(FEISHU_APP_SECRET_SERVICE, instance_id)`` so the
    inbound transport / token cache / config writer all see the same record;
    WeChat uses the legacy ``qai.channels.signing`` namespace keyed by
    instance-id-suffix). Operator can pass ``--secret-value`` at register time
    or leave it blank and provision via ``qai channel config set --app-secret``.
    """
    from qai.channels.application.use_cases.register_channel_instance import (
        RegisterChannelInstanceCommand,
    )
    from qai.channels.domain import ChannelKind
    from apps.api._channels_di import (
        FEISHU_APP_SECRET_SERVICE,
        SIGNING_SECRET_SERVICE,
    )

    kind = _kind_from_str(args.kind)
    metadata = _parse_kv(args.meta or [], flag="--meta")
    secret_value = args.secret_value or ""

    if kind is ChannelKind.FEISHU:
        # Feishu pins the credential ref to the instance-id namespace
        # (route layer parity — see channels.py:502).  ``secret_key`` is
        # ignored by the use case when ``secret_key_use_instance_id=True``.
        cmd = RegisterChannelInstanceCommand(
            kind=kind,
            name=args.name,
            secret_service=FEISHU_APP_SECRET_SERVICE,
            secret_key="",
            secret_value=secret_value,
            metadata=tuple(metadata.items()),
            secret_key_use_instance_id=True,
        )
    else:
        # WeChat: webhook signing token namespace, key = "default" (the
        # registered route layer accepts the operator's choice; the CLI
        # picks a sane default since CLI register doesn't expose the
        # legacy 2-string secret_service / secret_key pair).
        cmd = RegisterChannelInstanceCommand(
            kind=kind,
            name=args.name,
            secret_service=SIGNING_SECRET_SERVICE,
            secret_key="default",
            secret_value=secret_value,
            metadata=tuple(metadata.items()),
        )

    async def _go(c: Container) -> dict[str, Any]:
        instance = await c.channels.register_channel_instance_use_case.execute(cmd)
        return _instance_to_dict(instance)

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    """``qai channel list [--kind ...]``.

    Reads directly through :class:`ChannelInstanceRepositoryPort.list_by_kind`
    (no use case wrapper exists for "list everything" — see §2.1.1.I 🟡).
    With no ``--kind``, returns the union of feishu + wechat instances; with
    ``--kind`` filters to that kind. Empty list returns ``[]`` (exit 0).
    """
    from qai.channels.domain import ChannelKind

    target_kinds: tuple[ChannelKind, ...]
    if args.kind:
        target_kinds = (_kind_from_str(args.kind),)
    else:
        target_kinds = (ChannelKind.FEISHU, ChannelKind.WECHAT)

    async def _go(c: Container) -> list[dict[str, Any]]:
        repo = c.channels.instance_repository
        out: list[dict[str, Any]] = []
        for kind in target_kinds:
            for instance in await repo.list_by_kind(kind):
                out.append(_instance_to_dict(instance))
        return out

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    """``qai channel show <instance-id>`` / ``qai channel status <instance-id>``.

    Composes the persisted instance row with a live ``transport.health()``
    probe — same shape as ``GET /api/{kind}/status``.  When the transport
    isn't running the report has ``status="unknown"``; the CLI surfaces it
    verbatim so an operator can tell "down vs running" without inferring.
    """
    from qai.channels.domain import (
        ChannelInstanceId,
        ChannelInstanceNotFoundError,
    )

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        repo = c.channels.instance_repository
        try:
            instance = await repo.get(iid)
        except ChannelInstanceNotFoundError as exc:
            raise SystemExit(_emit_error(str(exc))) from exc
        transport = c.channels.transport_for_kind(instance.kind)
        try:
            report = await transport.health(instance)
        except Exception as exc:  # noqa: BLE001 — health is best-effort
            return {
                "instance": _instance_to_dict(instance),
                "health": {
                    "status": "unknown",
                    "detail": f"health probe failed: {type(exc).__name__}: {exc}",
                    "checked_at": None,
                },
            }
        return {
            "instance": _instance_to_dict(instance),
            "health": {
                "status": report.status.value,
                "detail": report.detail,
                "checked_at": (
                    report.checked_at.isoformat()
                    if report.checked_at is not None
                    else None
                ),
            },
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def _emit_error(message: str) -> int:
    """Print ``qai channel: <message>`` to stderr and return exit code 1."""
    sys.stderr.write(f"qai channel: {message}\n")
    return 1


def cmd_delete(args: argparse.Namespace) -> int:
    """``qai channel delete <instance-id> [--yes]``.

    No use case exists for delete (§2.1.1.I 🟡), so this drives the repo
    port directly while ALSO clearing the SecretStore records this CLI knows
    about (signing secret + Feishu app_secret + proxy password) and best-
    effort dropping session-index rows for the same instance. Bindings live
    inside the aggregate's metadata blob so they vanish with the row.

    A ``DeleteChannelInstanceUseCase`` that emits a domain event + cascades
    to other contexts is a P2 follow-up (logged in the module docstring).
    """
    from qai.channels.domain import (
        ChannelInstanceId,
        ChannelInstanceNotFoundError,
    )
    from apps.api._channels_di import (
        FEISHU_APP_SECRET_SERVICE,
        PROXY_PASSWORD_SECRET_SERVICE,
        SIGNING_SECRET_SERVICE,
    )

    if not args.yes:
        # Refuse without explicit ``--yes``. argparse exit-2 conventions
        # apply: this is a usage-time guard rail, not a runtime error.
        sys.stderr.write(
            "qai channel delete: refusing to delete without --yes "
            f"(instance_id={args.instance_id!r}); pass --yes to confirm.\n"
        )
        return 2

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        repo = c.channels.instance_repository
        try:
            instance = await repo.get(iid)
        except ChannelInstanceNotFoundError as exc:
            raise SystemExit(_emit_error(str(exc))) from exc

        cleared_secrets: list[str] = []
        store = c.secret_store
        # Clear every namespace the route layer + register CLI write to
        # for this instance. ``delete`` raises when missing — swallow so
        # we leave no orphan no matter which subset was actually used.
        for service in (
            FEISHU_APP_SECRET_SERVICE,
            PROXY_PASSWORD_SECRET_SERVICE,
            SIGNING_SECRET_SERVICE,
        ):
            try:
                store.delete(service, iid.value)
                cleared_secrets.append(f"{service}/{iid.value}")
            except NotFoundError:
                pass
            except Exception as exc:  # noqa: BLE001 — best-effort
                sys.stderr.write(
                    f"qai channel delete: warning: failed to clear "
                    f"secret {service!r}/{iid.value!r}: "
                    f"{type(exc).__name__}: {exc}\n"
                )
        # Best-effort session-index clean-up. The session repo doesn't
        # expose "delete by instance" so we list and drop one by one.
        try:
            session_repo = c.channels.session_repository
            entries = await session_repo.list_for_instance(iid)
            for entry in entries:
                try:
                    await session_repo.delete(
                        entry.instance_id, entry.channel_user_id
                    )
                except Exception as exc:  # noqa: BLE001
                    sys.stderr.write(
                        "qai channel delete: warning: failed to drop "
                        f"session {entry.channel_user_id.value!r}: "
                        f"{type(exc).__name__}: {exc}\n"
                    )
        except Exception as exc:  # noqa: BLE001 — best-effort
            sys.stderr.write(
                "qai channel delete: warning: session cleanup failed: "
                f"{type(exc).__name__}: {exc}\n"
            )

        # Finally drop the aggregate row itself.
        await repo.delete(iid)
        return {
            "ok": True,
            "instance_id": iid.value,
            "kind": instance.kind.value,
            "cleared_secrets": cleared_secrets,
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_start(args: argparse.Namespace) -> int:
    """``qai channel start <instance-id>``.

    Drives :class:`StartChannelInstanceUseCase`. Note (per module docstring):
    in CLI in-process mode the inbound consumer task lives in *this* short-
    lived process — it terminates with the CLI exit. Production "keep the
    instance running across CLI exits" requires the daemon (P2 HTTP route).
    """
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        instance = await c.channels.start_channel_instance_use_case.execute(iid)
        return _instance_to_dict(instance)

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    """``qai channel stop <instance-id>`` — drives :class:`StopChannelInstanceUseCase`."""
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        instance = await c.channels.stop_channel_instance_use_case.execute(iid)
        return _instance_to_dict(instance)

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_acknowledge(args: argparse.Namespace) -> int:
    """``qai channel acknowledge <instance-id>``.

    Acknowledges an ``error`` state, moving the instance back to
    ``stopped`` so it can be restarted.  Raises a non-zero exit code
    if the instance is not currently in ``error`` state.
    """
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        instance = await c.channels.acknowledge_channel_error_use_case.execute(iid)
        return _instance_to_dict(instance)

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


# ---------------------------------------------------------------------------
# config / proxy / model handlers
# ---------------------------------------------------------------------------


def cmd_config_get(args: argparse.Namespace) -> int:
    """``qai channel config get <instance-id>`` — reads :class:`GetChannelSettingsUseCase`."""
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        settings = await c.channels.get_channel_settings_use_case.execute(iid)
        return {
            "auto_start": settings.auto_start,
            "kind_specific": {k: v for k, v in settings.kind_specific},
            "has_app_secret": settings.has_app_secret,
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_config_set(args: argparse.Namespace) -> int:
    """``qai channel config set <instance-id> [--auto-start] [--kv k=v] [--app-secret S]``.

    For Feishu, ``--app-secret`` is written to the SecretStore at
    ``(FEISHU_APP_SECRET_SERVICE, instance_id)`` BEFORE the use case fires
    (same convention as the HTTP route). Empty / absent ``--app-secret``
    means "preserve" (not "clear") — matches the WebUI's three-state behavior.
    For WeChat the flag is silently ignored (WeChat has no app_secret).
    """
    from qai.channels.application.use_cases.manage_settings import (
        UpdateChannelConfigCommand,
    )
    from qai.channels.domain import ChannelInstanceId, ChannelKind
    from apps.api._channels_di import FEISHU_APP_SECRET_SERVICE

    iid = ChannelInstanceId(args.instance_id)
    kvs = _parse_kv(args.kv or [], flag="--kv")
    # Three-state auto_start: --auto-start sets True, --no-auto-start sets
    # False, omitting both reads the existing setting and preserves it.
    if args.auto_start is None:
        # Will need to read the existing value to preserve.
        load_existing = True
    else:
        load_existing = False

    async def _go(c: Container) -> dict[str, Any]:
        repo = c.channels.instance_repository
        instance = await repo.get(iid)
        if instance.kind is ChannelKind.FEISHU and args.app_secret is not None:
            secret_present = bool(args.app_secret)
            if secret_present:
                c.secret_store.set(
                    FEISHU_APP_SECRET_SERVICE, iid.value, args.app_secret
                )
        else:
            secret_present = False

        if load_existing:
            auto_start = instance.get_settings().auto_start
        else:
            auto_start = bool(args.auto_start)

        cmd = UpdateChannelConfigCommand(
            instance_id=iid,
            auto_start=auto_start,
            kind_specific=kvs,
        )
        if instance.kind is ChannelKind.FEISHU:
            updated = (
                await c.channels.update_channel_config_use_case
                .execute_preserving_secret(
                    command=cmd,
                    app_secret_present=secret_present,
                )
            )
        else:
            updated = await c.channels.update_channel_config_use_case.execute(cmd)
        settings = updated.get_settings()
        return {
            "auto_start": settings.auto_start,
            "kind_specific": {k: v for k, v in settings.kind_specific},
            "has_app_secret": settings.has_app_secret,
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_proxy_get(args: argparse.Namespace) -> int:
    """``qai channel proxy get <instance-id>`` — same shape as ``GET /proxy``."""
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        settings = await c.channels.get_channel_settings_use_case.execute(iid)
        return {
            "url": settings.proxy.url,
            "username": settings.proxy.username,
            "has_password": settings.proxy.has_password,
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_proxy_set(args: argparse.Namespace) -> int:
    """``qai channel proxy set <instance-id> [--url] [--username] [--password ...]``.

    Three-state password handling — matches ``UpdateChannelProxyUseCase
    .execute_preserving_password``:

    * ``--password X`` writes X to the SecretStore (sets ``has_password=True``)
    * ``--password-stdin`` reads from stdin then same as above
    * ``--no-password`` explicitly clears the saved password
    * none of the above → preserve existing ``has_password`` flag

    Empty url/username are valid (they clear the field).
    """
    from qai.channels.domain import ChannelInstanceId
    from apps.api._channels_di import PROXY_PASSWORD_SECRET_SERVICE

    iid = ChannelInstanceId(args.instance_id)

    if args.password_stdin:
        password = _read_stdin_str()
        password_action = "set"
    elif args.no_password:
        password = ""
        password_action = "clear"
    elif args.password is not None:
        password = args.password
        password_action = "set"
    else:
        password = ""
        password_action = "preserve"

    async def _go(c: Container) -> dict[str, Any]:
        repo = c.channels.instance_repository
        instance = await repo.get(iid)
        existing = instance.get_settings()
        url = args.url if args.url is not None else existing.proxy.url
        username = (
            args.username if args.username is not None else existing.proxy.username
        )

        if password_action == "set":
            c.secret_store.set(
                PROXY_PASSWORD_SECRET_SERVICE, iid.value, password
            )
            password_present = True
        elif password_action == "clear":
            try:
                c.secret_store.delete(
                    PROXY_PASSWORD_SECRET_SERVICE, iid.value
                )
            except NotFoundError:
                pass
            password_present = False
        else:
            # preserve — pass current ``has_password`` through
            password_present = existing.proxy.has_password

        # ``execute_preserving_password`` only handles preserve vs set.  For
        # the explicit clear path we manually call ``execute`` so the flag
        # ends up False.
        if password_action == "clear":
            from qai.channels.application.use_cases.manage_settings import (
                UpdateChannelProxyCommand,
            )

            updated = await c.channels.update_channel_proxy_use_case.execute(
                UpdateChannelProxyCommand(
                    instance_id=iid,
                    url=url,
                    username=username,
                    has_password=False,
                )
            )
        else:
            updated = (
                await c.channels.update_channel_proxy_use_case
                .execute_preserving_password(
                    instance_id=iid,
                    url=url,
                    username=username,
                    password_present=password_present,
                )
            )
        proxy = updated.get_settings().proxy
        return {
            "url": proxy.url,
            "username": proxy.username,
            "has_password": proxy.has_password,
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_model_get(args: argparse.Namespace) -> int:
    """``qai channel model get <instance-id>``."""
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        settings = await c.channels.get_channel_settings_use_case.execute(iid)
        return {
            "model_id": settings.model.model_id,
            "model_provider": settings.model.model_provider,
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_model_set(args: argparse.Namespace) -> int:
    """``qai channel model set <instance-id> --model-id ... [--provider ...]``."""
    from qai.channels.application.use_cases.manage_settings import (
        UpdateChannelModelCommand,
    )
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)
    cmd = UpdateChannelModelCommand(
        instance_id=iid,
        model_id=args.model_id,
        model_provider=args.provider or "",
    )

    async def _go(c: Container) -> dict[str, Any]:
        updated = await c.channels.update_channel_model_use_case.execute(cmd)
        model = updated.get_settings().model
        return {
            "model_id": model.model_id,
            "model_provider": model.model_provider,
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    """``qai channel push <instance-id> --user-id ... (--text|--text-stdin)``.

    Out-of-band send via :class:`PushChannelMessageUseCase` (§2.1.1.I main
    send entry-point). The use case auto-splits long text at 5000 chars
    and tags each chunk with ``page_format`` when there is more than one.
    """
    from qai.channels.application.use_cases.push_message import (
        PushChannelMessageCommand,
    )
    from qai.channels.domain import ChannelInstanceId, ChannelUserId

    if args.text_stdin:
        text = _read_stdin_str()
    else:
        text = args.text or ""

    cmd = PushChannelMessageCommand(
        instance_id=ChannelInstanceId(args.instance_id),
        target=ChannelUserId(args.user_id),
        text=text,
        page_suffix_format=args.page_format,
    )

    async def _go(c: Container) -> dict[str, Any]:
        result = await c.channels.push_channel_message_use_case.execute(cmd)
        return {
            "instance_id": result.instance_id,
            "target": result.target,
            "chunk_count": result.chunk_count,
            "all_ok": result.all_ok,
            "provider_message_ids": list(result.provider_message_ids),
            "first_error": result.first_error(),
            "chunks": [
                {
                    "sequence": ch.sequence,
                    "ok": ch.ok,
                    "provider_message_id": ch.provider_message_id,
                    "error": ch.error,
                }
                for ch in result.chunks
            ],
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


# ---------------------------------------------------------------------------
# binding / session handlers
# ---------------------------------------------------------------------------


def cmd_binding_list(args: argparse.Namespace) -> int:
    """``qai channel binding list <instance-id>``."""
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        bindings = await c.channels.get_channel_bindings_use_case.execute(iid)
        return {"bindings": bindings.as_dict()}

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_binding_set(args: argparse.Namespace) -> int:
    """``qai channel binding set <instance-id> --conv-id ... --user-id ...``.

    Empty ``--user-id`` clears the binding (legacy semantics enforced by
    :class:`BindChannelConversationUseCase`); pass an explicit empty
    string ``--user-id ""`` to clear, or use ``binding delete``.
    """
    from qai.channels.application.use_cases.manage_bindings import (
        BindChannelConversationCommand,
    )
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)
    cmd = BindChannelConversationCommand(
        instance_id=iid,
        conversation_id=args.conv_id,
        channel_user_id=args.user_id,
    )

    async def _go(c: Container) -> dict[str, Any]:
        await c.channels.bind_channel_conversation_use_case.execute(cmd)
        return {
            "ok": True,
            "conversation_id": args.conv_id,
            "channel_user_id": args.user_id if args.user_id else None,
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_binding_delete(args: argparse.Namespace) -> int:
    """``qai channel binding delete <instance-id> --conv-id ...``."""
    from qai.channels.application.use_cases.manage_bindings import (
        UnbindChannelConversationCommand,
    )
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)
    cmd = UnbindChannelConversationCommand(
        instance_id=iid,
        conversation_id=args.conv_id,
    )

    async def _go(c: Container) -> dict[str, Any]:
        await c.channels.unbind_channel_conversation_use_case.execute(cmd)
        return {"ok": True, "conversation_id": args.conv_id}

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_session_bind(args: argparse.Namespace) -> int:
    """``qai channel session bind <instance-id> --user-id U [--internal-user-id ...] ...``."""
    from qai.channels.application.use_cases.session_index import (
        BindSessionIndexCommand,
    )
    from qai.channels.domain import ChannelInstanceId, ChannelUserId

    cmd = BindSessionIndexCommand(
        instance_id=ChannelInstanceId(args.instance_id),
        channel_user_id=ChannelUserId(args.user_id),
        internal_user_id=args.internal_user_id,
        coding_session_id=args.coding_session_id,
    )

    async def _go(c: Container) -> dict[str, Any]:
        entry = await c.channels.bind_session_index_use_case.execute(cmd)
        return {
            "instance_id": entry.instance_id.value,
            "channel_user_id": entry.channel_user_id.value,
            "internal_user_id": entry.internal_user_id,
            "coding_session_id": entry.coding_session_id,
            "updated_at": entry.updated_at.isoformat(),
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_session_lookup(args: argparse.Namespace) -> int:
    """``qai channel session lookup <instance-id> --user-id U`` — returns ``null`` if absent."""
    from qai.channels.domain import ChannelInstanceId, ChannelUserId

    iid = ChannelInstanceId(args.instance_id)
    cuid = ChannelUserId(args.user_id)

    async def _go(c: Container) -> dict[str, Any] | None:
        entry = await c.channels.lookup_session_index_use_case.execute(iid, cuid)
        if entry is None:
            return None
        return {
            "instance_id": entry.instance_id.value,
            "channel_user_id": entry.channel_user_id.value,
            "internal_user_id": entry.internal_user_id,
            "coding_session_id": entry.coding_session_id,
            "updated_at": entry.updated_at.isoformat(),
        }

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


# ---------------------------------------------------------------------------
# wechat-specific handlers
# ---------------------------------------------------------------------------


def cmd_wechat_login(args: argparse.Namespace) -> int:
    """``qai channel wechat login <instance-id> [--force]`` — drives the SDK login.

    Calls :meth:`WechatPersonalQrLoginAdapter.trigger_login` directly (no use
    case wrapper exists). The adapter mints a fresh challenge, builds the
    wechatbot SDK ``Bot``, and kicks off ``bot.login(force=...)`` as a
    background task — returns the ``challenge_id`` immediately so the
    operator can poll ``qai channel wechat qr-status``.

    The CLI in-process invocation builds the bot but exits before the user
    can scan; for an interactive login flow either drive the daemon over
    HTTP (P2 follow-up) or leave the CLI process alive (e.g. wrap in a
    ``--watch`` follow-up — also P2).
    """
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        repo = c.channels.instance_repository
        instance = await repo.get(iid)
        adapter = c.channels.wechat_personal_qr_login
        challenge_id = await adapter.trigger_login(instance, force=args.force)
        return {"instance_id": iid.value, "challenge_id": challenge_id}

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_wechat_qr(args: argparse.Namespace) -> int:
    """``qai channel wechat qr <instance-id> --challenge-id ID [--out path | --print-ascii]``.

    Drives :class:`RenderQrImageUseCase` to produce PNG bytes from the
    persisted ``qr_url`` the SDK reported via ``on_qr_url``. ``--out``
    writes the bytes to a file; ``--print-ascii`` renders an ASCII QR
    to stdout (requires the ``qrcode`` library — already a hard dep).
    Both flags can coexist; if neither is set the bytes are written to
    stdout (binary; pipe to ``> file.png``).
    """
    from qai.channels.domain import ChannelInstanceId, ChannelKind

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> tuple[bytes, str]:
        png = await c.channels.render_qr_image_use_case.execute(
            iid, args.challenge_id, expected_kind=ChannelKind.WECHAT
        )
        challenge = await c.channels.lookup_qr_challenge_use_case.execute(
            iid, args.challenge_id, expected_kind=ChannelKind.WECHAT
        )
        return png, challenge.qr_url or ""

    png, qr_url = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )

    wrote_anywhere = False
    if args.out is not None:
        out_path = Path(args.out)
        out_path.write_bytes(png)
        sys.stderr.write(
            f"qai channel wechat qr: wrote {len(png)} bytes to {out_path}\n"
        )
        wrote_anywhere = True

    if args.print_ascii:
        try:
            from qrcode.main import QRCode  # type: ignore[import-not-found]

            qr = QRCode(border=1)
            qr.add_data(qr_url)
            qr.make(fit=True)
            qr.print_ascii(out=sys.stdout)
            sys.stdout.flush()
        except Exception as exc:  # noqa: BLE001
            sys.stderr.write(
                f"qai channel wechat qr: ASCII render failed "
                f"({type(exc).__name__}: {exc}); url={qr_url!r}\n"
            )
            return 1
        wrote_anywhere = True

    if not wrote_anywhere:
        # Default: emit raw PNG bytes to stdout buffer (operator pipes
        # to a file). Wrapping bytes in JSON would corrupt the image.
        sys.stdout.buffer.write(png)
        sys.stdout.buffer.flush()
    return 0


def cmd_wechat_qr_status(args: argparse.Namespace) -> int:
    """``qai channel wechat qr-status <instance-id> --challenge-id ID [--watch]``.

    Single poll by default — calls :class:`ConfirmQrLoginUseCase` with
    ``confirm=False`` and prints the resulting challenge state.
    With ``--watch`` polls every 2 seconds for at most 5 minutes (the
    default challenge TTL); exits 0 once status is ``confirmed``,
    1 if it transitions to ``expired``. SIGINT exits 130 (handled by
    the top-level dispatcher).
    """
    from qai.channels.domain import ChannelInstanceId, QrLoginStatus

    iid = ChannelInstanceId(args.instance_id)
    cid = args.challenge_id

    if not args.watch:

        async def _go_once(c: Container) -> dict[str, Any]:
            challenge = await c.channels.confirm_qr_login_use_case.execute(
                iid, cid, confirm=False
            )
            return _challenge_to_dict(challenge)

        payload = run_use_case(
            _go_once,
            config_file=getattr(args, "config_file", None),
            repo_root=getattr(args, "repo_root", None),
        )
        _emit(payload)
        return 0

    # --watch: keep one cli_container open for the whole poll loop so we
    # don't pay DI / migration costs every 2 seconds.
    from apps.cli._runtime import cli_container

    async def _watch_loop() -> int:
        max_seconds = 300
        interval = 2.0
        elapsed = 0.0
        async with cli_container(
            config_file=getattr(args, "config_file", None),
            repo_root=getattr(args, "repo_root", None),
        ) as c:
            while True:
                challenge = await c.channels.confirm_qr_login_use_case.execute(
                    iid, cid, confirm=False
                )
                _emit(_challenge_to_dict(challenge))
                if challenge.status is QrLoginStatus.CONFIRMED:
                    return 0
                if challenge.status is QrLoginStatus.EXPIRED:
                    sys.stderr.write(
                        "qai channel wechat qr-status: challenge expired\n"
                    )
                    return 1
                if elapsed >= max_seconds:
                    sys.stderr.write(
                        "qai channel wechat qr-status: --watch "
                        f"timed out after {max_seconds}s\n"
                    )
                    return 1
                await asyncio.sleep(interval)
                elapsed += interval

    return asyncio.run(_watch_loop())


def _challenge_to_dict(challenge: Any) -> dict[str, Any]:
    """Serialise a :class:`QrLoginChallenge` to a stable JSON shape."""
    return {
        "challenge_id": challenge.challenge_id,
        "instance_id": challenge.instance_id_value,
        "status": challenge.status.value,
        "issued_at": challenge.issued_at.isoformat(),
        "expires_at": challenge.expires_at.isoformat(),
        "qr_url": challenge.qr_url or "",
    }


def cmd_wechat_qr_issue(args: argparse.Namespace) -> int:
    """``qai channel wechat qr-issue <instance-id>`` — pure :class:`IssueQrLoginUseCase`.

    Distinct from ``wechat login`` — this does NOT drive the wechatbot SDK,
    only mints a fresh challenge row in the repo. Useful for unit-test-style
    flows that don't have the SDK runtime available.
    """
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        challenge = await c.channels.issue_qr_login_use_case.execute(iid)
        return _challenge_to_dict(challenge)

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


def cmd_wechat_logout(args: argparse.Namespace) -> int:
    """``qai channel wechat logout <instance-id>`` — drives :class:`LogoutWechatPersonalUseCase`."""
    from qai.channels.domain import ChannelInstanceId

    iid = ChannelInstanceId(args.instance_id)

    async def _go(c: Container) -> dict[str, Any]:
        instance = await c.channels.logout_wechat_personal_use_case.execute(iid)
        return _instance_to_dict(instance)

    payload = run_use_case(
        _go,
        config_file=getattr(args, "config_file", None),
        repo_root=getattr(args, "repo_root", None),
    )
    _emit(payload)
    return 0


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def register(subparsers: argparse._SubParsersAction) -> None:
    """Attach the ``qai channel`` subparser tree (D2 group I).

    Called once by :mod:`apps.cli.__main__` at parser-build time. All
    subcommands resolve to module-level handler functions via
    ``set_defaults(handler=...)`` so the top-level dispatcher can drive
    them uniformly with the rest of the CLI surface.
    """

    channel = subparsers.add_parser(
        "channel",
        help="manage Feishu / WeChat channel instances",
        description=(
            "Register, configure, and operate Feishu / WeChat channel "
            "instances. CLI is a sibling adapter to the HTTP route layer "
            "— same use cases, same persisted state. See "
            "``qai channel <verb> --help`` for per-verb documentation."
        ),
    )
    sub = channel.add_subparsers(
        dest="channel_command", required=True, metavar="<subcommand>"
    )

    # ── register ─────────────────────────────────────────────────────
    p_register = sub.add_parser(
        "register",
        help="create a new channel instance (Feishu or WeChat)",
    )
    p_register.add_argument(
        "--kind", required=True, choices=["feishu", "wechat"]
    )
    p_register.add_argument("--name", required=True)
    p_register.add_argument(
        "--secret-value",
        default=None,
        help=(
            "initial credential value (Feishu app_secret / WeChat signing "
            "token). May be empty at register time and provisioned later "
            "via 'qai channel config set --app-secret'."
        ),
    )
    p_register.add_argument(
        "--meta",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="additional metadata entry; repeat for multiple",
    )
    p_register.set_defaults(handler=cmd_register)

    # ── list / show / status / delete / start / stop ─────────────────
    p_list = sub.add_parser("list", help="list registered channel instances")
    p_list.add_argument(
        "--kind", default=None, choices=["feishu", "wechat"], metavar="KIND"
    )
    p_list.set_defaults(handler=cmd_list)

    p_show = sub.add_parser(
        "show", help="show one instance + live transport health"
    )
    p_show.add_argument("instance_id")
    p_show.set_defaults(handler=cmd_show)

    p_status = sub.add_parser(
        "status", help="alias for 'show' (V1 ergonomics)"
    )
    p_status.add_argument("instance_id")
    p_status.set_defaults(handler=cmd_show)

    p_delete = sub.add_parser(
        "delete",
        help="delete an instance + clear its SecretStore + sessions",
    )
    p_delete.add_argument("instance_id")
    p_delete.add_argument(
        "--yes", action="store_true", help="confirm destructive action"
    )
    p_delete.set_defaults(handler=cmd_delete)

    p_start = sub.add_parser("start", help="start an instance (in-process)")
    p_start.add_argument("instance_id")
    p_start.set_defaults(handler=cmd_start)

    p_stop = sub.add_parser("stop", help="stop an instance (in-process)")
    p_stop.add_argument("instance_id")
    p_stop.set_defaults(handler=cmd_stop)

    p_acknowledge = sub.add_parser(
        "acknowledge",
        help="acknowledge error state so the instance can be restarted",
        description=(
            "Move a channel instance from 'error' back to 'stopped' so it "
            "can be restarted. The domain requires an explicit acknowledgement "
            "before any restart attempt is permitted."
        ),
    )
    p_acknowledge.add_argument("instance_id")
    p_acknowledge.set_defaults(handler=cmd_acknowledge)

    # ── push ─────────────────────────────────────────────────────────
    p_push = sub.add_parser(
        "push", help="send an out-of-band text message to a channel user"
    )
    p_push.add_argument("instance_id")
    p_push.add_argument("--user-id", dest="user_id", required=True)
    push_text_grp = p_push.add_mutually_exclusive_group(required=True)
    push_text_grp.add_argument("--text", default=None)
    push_text_grp.add_argument(
        "--text-stdin",
        dest="text_stdin",
        action="store_true",
        help="read message body from stdin (avoids leak via shell history)",
    )
    p_push.add_argument(
        "--page-format",
        dest="page_format",
        default="({i}/{n})",
        help="suffix template applied to each chunk when splitting (default '({i}/{n})')",
    )
    p_push.set_defaults(handler=cmd_push)

    # ── config get/set ───────────────────────────────────────────────
    p_config = sub.add_parser(
        "config", help="read / write per-instance ChannelSettings"
    )
    cfg_sub = p_config.add_subparsers(
        dest="config_command", required=True, metavar="<subcommand>"
    )
    cfg_get = cfg_sub.add_parser("get", help="print persisted ChannelSettings")
    cfg_get.add_argument("instance_id")
    cfg_get.set_defaults(handler=cmd_config_get)

    cfg_set = cfg_sub.add_parser(
        "set", help="update auto_start / kind_specific / app_secret"
    )
    cfg_set.add_argument("instance_id")
    auto_grp = cfg_set.add_mutually_exclusive_group()
    auto_grp.add_argument(
        "--auto-start", dest="auto_start", action="store_true", default=None
    )
    auto_grp.add_argument(
        "--no-auto-start", dest="auto_start", action="store_false"
    )
    cfg_set.add_argument(
        "--kv",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="kind_specific entry; repeat (e.g. --kv app_id=cli_xxx)",
    )
    cfg_set.add_argument(
        "--app-secret",
        dest="app_secret",
        default=None,
        help="(Feishu only) write a fresh app_secret to SecretStore",
    )
    cfg_set.set_defaults(handler=cmd_config_set)

    # ── proxy get/set ────────────────────────────────────────────────
    p_proxy = sub.add_parser(
        "proxy", help="read / write per-instance proxy config"
    )
    pxy_sub = p_proxy.add_subparsers(
        dest="proxy_command", required=True, metavar="<subcommand>"
    )
    pxy_get = pxy_sub.add_parser("get", help="print proxy config")
    pxy_get.add_argument("instance_id")
    pxy_get.set_defaults(handler=cmd_proxy_get)

    pxy_set = pxy_sub.add_parser(
        "set", help="update proxy url / username / password"
    )
    pxy_set.add_argument("instance_id")
    pxy_set.add_argument("--url", default=None)
    pxy_set.add_argument("--username", default=None)
    pwd_grp = pxy_set.add_mutually_exclusive_group()
    pwd_grp.add_argument("--password", default=None)
    pwd_grp.add_argument(
        "--password-stdin",
        dest="password_stdin",
        action="store_true",
        help="read proxy password from stdin",
    )
    pwd_grp.add_argument(
        "--no-password",
        dest="no_password",
        action="store_true",
        help="explicitly clear the saved password",
    )
    pxy_set.set_defaults(handler=cmd_proxy_set)

    # ── model get/set ────────────────────────────────────────────────
    p_model = sub.add_parser(
        "model", help="read / write per-instance default model"
    )
    mdl_sub = p_model.add_subparsers(
        dest="model_command", required=True, metavar="<subcommand>"
    )
    mdl_get = mdl_sub.add_parser("get", help="print default model")
    mdl_get.add_argument("instance_id")
    mdl_get.set_defaults(handler=cmd_model_get)

    mdl_set = mdl_sub.add_parser("set", help="set default model")
    mdl_set.add_argument("instance_id")
    mdl_set.add_argument("--model-id", dest="model_id", required=True)
    mdl_set.add_argument(
        "--provider", default=None, help="model provider id (default empty)"
    )
    mdl_set.set_defaults(handler=cmd_model_set)

    # ── binding list/set/delete ──────────────────────────────────────
    p_binding = sub.add_parser(
        "binding", help="read / write conversation - channel-user bindings"
    )
    bnd_sub = p_binding.add_subparsers(
        dest="binding_command", required=True, metavar="<subcommand>"
    )
    bnd_list = bnd_sub.add_parser("list", help="list all bindings")
    bnd_list.add_argument("instance_id")
    bnd_list.set_defaults(handler=cmd_binding_list)

    bnd_set = bnd_sub.add_parser(
        "set", help="bind / replace / clear a conversation"
    )
    bnd_set.add_argument("instance_id")
    bnd_set.add_argument("--conv-id", dest="conv_id", required=True)
    bnd_set.add_argument(
        "--user-id",
        dest="user_id",
        required=True,
        help="channel-side user id (empty string clears)",
    )
    bnd_set.set_defaults(handler=cmd_binding_set)

    bnd_del = bnd_sub.add_parser("delete", help="remove one binding")
    bnd_del.add_argument("instance_id")
    bnd_del.add_argument("--conv-id", dest="conv_id", required=True)
    bnd_del.set_defaults(handler=cmd_binding_delete)

    # ── session bind/lookup ──────────────────────────────────────────
    p_session = sub.add_parser(
        "session", help="read / write session-index entries"
    )
    ses_sub = p_session.add_subparsers(
        dest="session_command", required=True, metavar="<subcommand>"
    )
    ses_bind = ses_sub.add_parser("bind", help="upsert a session-index entry")
    ses_bind.add_argument("instance_id")
    ses_bind.add_argument("--user-id", dest="user_id", required=True)
    ses_bind.add_argument(
        "--internal-user-id", dest="internal_user_id", default=None
    )
    ses_bind.add_argument(
        "--coding-session-id", dest="coding_session_id", default=None
    )
    ses_bind.set_defaults(handler=cmd_session_bind)

    ses_lookup = ses_sub.add_parser(
        "lookup", help="look up the session-index entry for a user"
    )
    ses_lookup.add_argument("instance_id")
    ses_lookup.add_argument("--user-id", dest="user_id", required=True)
    ses_lookup.set_defaults(handler=cmd_session_lookup)

    # ── wechat <subverb> ─────────────────────────────────────────────
    p_wechat = sub.add_parser(
        "wechat",
        help="WeChat-only verbs (QR login / logout / image / status)",
    )
    we_sub = p_wechat.add_subparsers(
        dest="wechat_command", required=True, metavar="<subcommand>"
    )

    we_login = we_sub.add_parser(
        "login",
        help="trigger wechatbot SDK login; returns a challenge_id to poll",
    )
    we_login.add_argument("instance_id")
    we_login.add_argument(
        "--force",
        action="store_true",
        help="force a fresh login even if SDK reports an active session",
    )
    we_login.set_defaults(handler=cmd_wechat_login)

    we_qr = we_sub.add_parser("qr", help="render QR PNG for a challenge")
    we_qr.add_argument("instance_id")
    we_qr.add_argument("--challenge-id", dest="challenge_id", required=True)
    we_qr.add_argument(
        "--out",
        default=None,
        help="write PNG bytes to this file path",
    )
    we_qr.add_argument(
        "--print-ascii",
        dest="print_ascii",
        action="store_true",
        help="render an ASCII QR to stdout (terminal-scannable)",
    )
    we_qr.set_defaults(handler=cmd_wechat_qr)

    we_status = we_sub.add_parser(
        "qr-status", help="check / watch QR challenge state"
    )
    we_status.add_argument("instance_id")
    we_status.add_argument("--challenge-id", dest="challenge_id", required=True)
    we_status.add_argument(
        "--watch",
        action="store_true",
        help="poll until confirmed / expired (max 5 min, 2s interval)",
    )
    we_status.set_defaults(handler=cmd_wechat_qr_status)

    we_issue = we_sub.add_parser(
        "qr-issue",
        help="issue a fresh QR challenge without driving the SDK",
    )
    we_issue.add_argument("instance_id")
    we_issue.set_defaults(handler=cmd_wechat_qr_issue)

    we_logout = we_sub.add_parser(
        "logout", help="logout the SDK + stop the channel"
    )
    we_logout.add_argument("instance_id")
    we_logout.set_defaults(handler=cmd_wechat_logout)

