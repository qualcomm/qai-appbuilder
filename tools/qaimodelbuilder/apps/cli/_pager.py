"""``apps.cli._pager`` — full-screen scrollable/searchable text viewer.

``/show [n]`` (``commands/build.py``) opens this pager to view one folded
``tool_result`` body in full. Built entirely on ``prompt_toolkit`` (already a
project dependency); ``textual`` is now a project dependency too (see
``apps.cli._tui``, used by the new full-screen REPL shell), but this module
intentionally keeps its own ``prompt_toolkit`` implementation for the
non-TTY/fallback REPL path. Runs via ``Application.run_async()`` so it nests
inside the caller's own asyncio event loop (the REPL loop in ``_repl.py``)
instead of starting a new one.
"""

from __future__ import annotations

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.document import Document
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, ScrollablePane, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.widgets import SearchToolbar

__all__ = ["show_pager"]


async def show_pager(text: str, *, title: str = "") -> None:
    """Show *text* full-screen: scrollable (``ScrollablePane``) and
    substring-searchable (``SearchToolbar``, incremental, ``/`` to start).

    ``Esc``/``q`` exit. Empty *text* and a search with no match both render
    a clear in-place message instead of crashing.
    """

    body = text if text else "（无内容）"
    search_toolbar = SearchToolbar(
        text_if_not_searching="（/ 搜索，n/N 跳转下一个/上一个匹配，Esc/q 退出）"
    )
    buffer = Buffer(document=Document(body, 0), read_only=True)
    content = Window(
        content=BufferControl(
            buffer=buffer, search_buffer_control=search_toolbar.control
        ),
        wrap_lines=True,
    )
    header = Window(
        content=FormattedTextControl(lambda: title or "/show"),
        height=D.exact(1),
        style="reverse",
    )

    root = HSplit(
        [
            header,
            ScrollablePane(HSplit([content])),
            search_toolbar,
        ]
    )

    bindings = KeyBindings()

    @bindings.add("q")
    @bindings.add("escape")
    def _exit(event: object) -> None:
        event.app.exit()  # type: ignore[attr-defined]

    application = Application(
        layout=Layout(root, focused_element=content),
        key_bindings=bindings,
        enable_page_navigation_bindings=True,
        mouse_support=True,
        full_screen=True,
    )
    await application.run_async()
