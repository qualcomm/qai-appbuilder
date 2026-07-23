"""Unit tests for ``apps.cli._session_log`` (cli-render-redesign plan, Step 3).

Covers: the session log tee actually captures everything written to
``sys.stdout``/``sys.stderr`` while active and restores the originals on
close; the shared ``cleanup_repl_session`` exit-cleanup helper runs its three
steps (stop a leftover renderer, restore the terminal cursor, close the log)
on every path, including when an exception propagates through the caller's
``finally``; and the ``qai app`` REPL turn correctly hands a leftover
``RunFrameRenderer`` reference to that cleanup when a turn raises something
other than ``KeyboardInterrupt``.
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from apps.cli import _session_log as session_log_mod
from apps.cli._render import RenderOptions, RunResult
from apps.cli._session_log import SessionLog, cleanup_repl_session
from apps.cli.commands import app as app_mod
from qai.platform.config.paths import DataPaths


def _opts() -> RenderOptions:
    return RenderOptions(color=False, emoji=False)


def test_session_log_captures_stdout_and_stderr(tmp_path) -> None:
    data_paths = DataPaths(tmp_path)
    orig_out, orig_err = sys.stdout, sys.stderr

    log = SessionLog(data_paths, "sess-1")
    try:
        sys.stdout.write("hello from stdout\n")
        sys.stderr.write("hello from stderr\n")
        sys.stdout.flush()
        sys.stderr.flush()
    finally:
        log.close()

    assert sys.stdout is orig_out
    assert sys.stderr is orig_err

    log_path = data_paths.cli_sessions_dir / "sess-1.log"
    content = log_path.read_text(encoding="utf-8")
    assert "hello from stdout" in content
    assert "hello from stderr" in content


def test_session_log_close_is_idempotent(tmp_path) -> None:
    data_paths = DataPaths(tmp_path)
    log = SessionLog(data_paths, "sess-2")
    log.close()
    log.close()  # must not raise


def test_cleanup_repl_session_restores_and_stops_renderer_and_closes_log(
    tmp_path, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        session_log_mod, "restore_terminal", lambda: calls.append("restore")
    )
    data_paths = DataPaths(tmp_path)
    log = SessionLog(data_paths, "sess-3")
    renderer = SimpleNamespace(stop=lambda: calls.append("stop"))

    cleanup_repl_session(log, active_renderer=renderer)

    assert calls == ["stop", "restore"]
    assert log._file is None  # noqa: SLF001 — asserting the close side effect


def test_cleanup_repl_session_without_renderer_still_restores_and_closes(
    tmp_path, monkeypatch
) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        session_log_mod, "restore_terminal", lambda: calls.append("restore")
    )
    log = SessionLog(DataPaths(tmp_path), "sess-4")

    cleanup_repl_session(log)

    assert calls == ["restore"]
    assert log._file is None  # noqa: SLF001


async def test_cleanup_runs_when_exception_propagates_mid_loop(
    tmp_path, monkeypatch
) -> None:
    """The exact shape of the `try: ... finally: cleanup_repl_session(...)`
    guard added to the two REPL loops: an exception raised mid-body must
    still trigger the terminal-restore + log-close cleanup on its way out.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        session_log_mod, "restore_terminal", lambda: calls.append("restore")
    )
    log = SessionLog(DataPaths(tmp_path), "sess-5")

    async def _loop_body() -> None:
        try:
            raise RuntimeError("boom mid-loop")
        finally:
            cleanup_repl_session(log)

    with pytest.raises(RuntimeError, match="boom mid-loop"):
        await _loop_body()

    assert calls == ["restore"]
    assert log._file is None  # noqa: SLF001


def _container() -> SimpleNamespace:
    return SimpleNamespace(app_builder=SimpleNamespace(get_pack_manifest_use_case=None))


async def test_repl_run_turn_clears_active_renderer_on_success(monkeypatch, capsys) -> None:
    state = app_mod._AppReplState("whisper-base")

    async def _fake_run_once(c, model_id_str, inputs, opts, *, err=None, renderer=None):
        assert renderer is state.active_renderer
        return RunResult(output={"ok": True})

    monkeypatch.setattr(app_mod, "_run_once", _fake_run_once)

    await app_mod._repl_run_turn(_container(), state, "hello", _opts())

    assert state.active_renderer is None
    assert state.last_result is not None


async def test_repl_run_turn_leaves_active_renderer_set_on_unexpected_exception(
    monkeypatch, capsys
) -> None:
    state = app_mod._AppReplState("whisper-base")

    async def _boom(c, model_id_str, inputs, opts, *, err=None, renderer=None):
        raise RuntimeError("run_once exploded")

    monkeypatch.setattr(app_mod, "_run_once", _boom)

    with pytest.raises(RuntimeError, match="run_once exploded"):
        await app_mod._repl_run_turn(_container(), state, "hello", _opts())

    assert state.active_renderer is not None


async def test_repl_run_turn_clears_active_renderer_on_keyboard_interrupt(
    monkeypatch, capsys
) -> None:
    state = app_mod._AppReplState("whisper-base")

    async def _interrupted(c, model_id_str, inputs, opts, *, err=None, renderer=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(app_mod, "_run_once", _interrupted)

    await app_mod._repl_run_turn(_container(), state, "hello", _opts())

    assert state.active_renderer is None
