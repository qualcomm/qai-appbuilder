# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Tests for the "refuse to run elevated" guard in ``apps.cli.serve.main``.

``is_admin`` is monkeypatched on the ``serve`` module so these exercise the
wiring and the ``QAI_ALLOW_ELEVATED`` opt-out, not the OS probe (which has
its own tests).
"""

from __future__ import annotations

import pytest

from apps.cli import serve


class _ReachedParser(Exception):
    """Raised by the fake parser to prove ``main`` got past the guard."""


def _stop_at_parser(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> None:
        raise _ReachedParser

    monkeypatch.setattr(serve, "_build_parser", _boom)


def test_refuses_when_admin_and_env_unset(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(serve, "is_admin", lambda: True)
    monkeypatch.delenv(serve.ALLOW_ELEVATED_ENV, raising=False)
    _stop_at_parser(monkeypatch)

    rc = serve.main([])

    # A dedicated code (the desktop shell keys off it), NOT the reboot code 75
    # (an outer wrapper would retry forever) and not the generic 1.
    assert rc == serve.ELEVATED_REFUSED_EXIT_CODE == 78
    assert rc != serve.REBOOT_EXIT_CODE
    err = capsys.readouterr().err
    assert "Refusing to start" in err


def test_refusal_message_explains_both_ways_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(serve, "is_admin", lambda: True)
    monkeypatch.delenv(serve.ALLOW_ELEVATED_ENV, raising=False)

    assert serve.main([]) == serve.ELEVATED_REFUSED_EXIT_CODE

    err = capsys.readouterr().err
    # Way 1: use a non-administrator Windows account.
    assert "non-administrator" in err
    # Way 2: the env var, with a copy-pasteable command for each shell.
    assert f"{serve.ALLOW_ELEVATED_ENV}=1" in err
    assert f"set {serve.ALLOW_ELEVATED_ENV}=1" in err
    assert f"$env:{serve.ALLOW_ELEVATED_ENV}" in err


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "Yes", "on", " 1 "])
def test_env_opt_out_lets_admin_through_with_warning(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    value: str,
) -> None:
    monkeypatch.setattr(serve, "is_admin", lambda: True)
    monkeypatch.setenv(serve.ALLOW_ELEVATED_ENV, value)
    _stop_at_parser(monkeypatch)

    # Reaching the parser means the guard did not return early.
    with pytest.raises(_ReachedParser):
        serve.main([])

    err = capsys.readouterr().err
    assert "WARNING" in err
    assert serve.ALLOW_ELEVATED_ENV in err
    assert "Refusing to start" not in err


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "2", "maybe"])
def test_env_values_that_are_not_truthy_keep_the_guard_on(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    monkeypatch.setattr(serve, "is_admin", lambda: True)
    monkeypatch.setenv(serve.ALLOW_ELEVATED_ENV, value)
    _stop_at_parser(monkeypatch)

    assert serve.main([]) == serve.ELEVATED_REFUSED_EXIT_CODE


def test_non_admin_proceeds_and_is_silent_even_if_env_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(serve, "is_admin", lambda: False)
    monkeypatch.setenv(serve.ALLOW_ELEVATED_ENV, "1")
    _stop_at_parser(monkeypatch)

    with pytest.raises(_ReachedParser):
        serve.main([])

    # No elevation warning for a normal user, even with the variable set.
    assert "WARNING" not in capsys.readouterr().err
