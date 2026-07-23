"""Unit tests for ``apps.cli._pager`` (cli-render-redesign plan, Step 2).

Drives :func:`apps.cli._pager.show_pager` headlessly via ``prompt_toolkit``'s
own test primitives (``create_pipe_input`` + ``DummyOutput`` inside
``create_app_session``, the same pattern ``prompt_toolkit``'s own test suite
uses) — no real terminal needed. The running ``Application`` is observed via
``AppSession.app`` (set by ``Application.run_async`` for its duration),
which is the same object the outer test and the pager's own asyncio task
share, regardless of which task set it.
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from apps.cli._pager import show_pager


async def _wait_until(predicate, *, attempts: int = 50) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0)


async def test_scroll_content_and_quit_with_q():
    text = "\n".join(f"line{i}" for i in range(200))
    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()) as session:
            task = asyncio.ensure_future(show_pager(text, title="t"))
            await _wait_until(lambda: session.app is not None)
            assert session.app is not None

            pipe_input.send_text("q")
            await asyncio.wait_for(task, timeout=5)


async def test_search_moves_cursor_to_match():
    text = "\n".join(f"line{i}" for i in range(200))
    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()) as session:
            task = asyncio.ensure_future(show_pager(text, title="t"))
            await _wait_until(lambda: session.app is not None)
            app = session.app

            pipe_input.send_text("/line150\r")
            await _wait_until(lambda: app.layout.current_buffer.cursor_position != 0)

            buf = app.layout.current_buffer
            pos = buf.cursor_position
            assert buf.document.text[pos : pos + 7] == "line150"

            pipe_input.send_text("q")
            await asyncio.wait_for(task, timeout=5)


async def test_search_with_no_match_does_not_crash():
    text = "\n".join(f"line{i}" for i in range(200))
    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()) as session:
            task = asyncio.ensure_future(show_pager(text, title="t"))
            await _wait_until(lambda: session.app is not None)
            app = session.app
            buf = app.layout.current_buffer
            before = buf.cursor_position

            pipe_input.send_text("/no-such-substring-xyz\r")
            await asyncio.sleep(0)
            for _ in range(20):
                await asyncio.sleep(0)

            # No match: cursor stays put, app keeps running (no crash).
            assert buf.cursor_position == before
            assert not task.done()

            pipe_input.send_text("q")
            await asyncio.wait_for(task, timeout=5)


async def test_empty_text_shows_placeholder_and_quits_with_escape():
    with create_pipe_input() as pipe_input:
        with create_app_session(input=pipe_input, output=DummyOutput()) as session:
            task = asyncio.ensure_future(show_pager("", title="empty"))
            await _wait_until(lambda: session.app is not None)
            app = session.app
            buf = app.layout.current_buffer
            assert buf.document.text == "（无内容）"

            pipe_input.send_text("\x1b")  # Esc
            await asyncio.wait_for(task, timeout=5)
