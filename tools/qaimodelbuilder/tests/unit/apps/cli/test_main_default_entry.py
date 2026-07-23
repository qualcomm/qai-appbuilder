"""Unit tests for ``apps.cli.__main__``'s no-subcommand dispatch branch
(delivery plan Phase 2 §Step 4).

Covers: (a) an interactive-TTY invocation with no subcommand routes to the
new default chat entry point (``apps.cli.commands.chat.cmd_chat``) instead of
argparse's usage error; (b) a non-TTY invocation with no subcommand
reproduces the exact usage-error text + exit code argparse itself raised
before this change (the top-level subparsers moved from ``required=True`` to
``required=False``); (c) every existing command group still resolves to a
valid subcommand (``required=False`` did not break subcommand dispatch).
"""

from __future__ import annotations

import argparse

import pytest

from apps.cli import __main__ as main_mod


def test_no_command_on_tty_routes_to_chat_handler(monkeypatch) -> None:
    monkeypatch.setattr(main_mod, "_stdin_is_tty", lambda: True)

    calls: list[argparse.Namespace] = []

    def _fake_cmd_chat(args: argparse.Namespace) -> int:
        calls.append(args)
        return 0

    import apps.cli.commands.chat as chat_mod

    monkeypatch.setattr(chat_mod, "cmd_chat", _fake_cmd_chat)

    rc = main_mod.main([])

    assert rc == 0
    assert len(calls) == 1
    assert calls[0].command is None


def test_no_command_on_non_tty_reproduces_usage_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main_mod, "_stdin_is_tty", lambda: False)

    rc = main_mod.main([])

    assert rc == 2
    err = capsys.readouterr().err
    assert "usage: qai" in err
    assert (
        f"qai: error: the following arguments are required: "
        f"{main_mod._COMMAND_METAVAR}"
        in err
    )


def test_required_false_still_raises_usage_error_for_no_command() -> None:
    """Structural check: the top-level subparsers action is not required,
    yet a bare invocation on a non-TTY stream must still exit 2 (matching
    the pre-change ``required=True`` behaviour byte-for-byte, per the two
    tests above) — this test just pins the ``required`` flag itself so a
    future edit can't silently flip it back without failing here too.
    """
    parser = main_mod.build_parser()
    subparsers_action = next(
        action
        for action in parser._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)
    )
    assert subparsers_action.required is False


def _registered_subcommands() -> tuple[str, ...]:
    """The actual top-level subcommand names argparse resolves.

    A ``_D2_GROUPS`` module name is NOT always the subcommand name it
    registers (e.g. the ``install`` module registers ``install-qairt`` /
    ``install-pack-deps`` / ``uninstall`` / ``compile-factory``, never a bare
    ``install``; ``service_release`` registers the hyphenated
    ``service-release``) — so this reads the real choices off the built
    parser instead of guessing from the module list.
    """
    parser = main_mod.build_parser()
    subparsers_action = next(
        action
        for action in parser._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)
    )
    return tuple(subparsers_action.choices.keys())


@pytest.mark.parametrize("group", _registered_subcommands())
def test_existing_command_groups_still_resolve(group: str, capsys) -> None:
    """Smoke test: the top-level parser still resolves ``group`` correctly.

    Some groups need further mandatory sub-arguments (e.g. ``qai run``
    alone is a usage error) — that is unrelated to this change and not what
    this test checks. It only asserts ``group`` is never rejected as an
    *unknown* top-level subcommand (``required=False`` did not break
    subcommand resolution for any pre-existing command group): either
    ``args.command`` resolves to ``group`` directly, or a ``SystemExit`` is
    raised for a reason OTHER than "invalid choice".
    """
    parser = main_mod.build_parser()
    try:
        args, _unknown = parser.parse_known_args([group])
    except SystemExit:
        assert "invalid choice" not in capsys.readouterr().err
        return
    assert args.command == group
