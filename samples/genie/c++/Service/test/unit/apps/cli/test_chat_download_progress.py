"""Unit tests for ``apps.cli.commands.chat._drain_download``'s Rich progress
bar (delivery plan Step 8): before this step a real download's ONLY visible
feedback was raw ``structlog``/``httpx`` log lines, since this helper itself
rendered nothing. Mirrors ``_render.RunFrameRenderer``'s TTY/non-TTY split
(``opts.color`` picks a live progress bar vs. periodic plain lines) so both
branches are covered without spinning up a live terminal.
"""

from __future__ import annotations

import io

from rich.console import Console

from apps.cli._render import RenderOptions
from apps.cli.commands import chat as chat_mod
from qai.service_release.domain.value_objects import DownloadProgress, DownloadStatus


async def _progress_stream(*frames: DownloadProgress):
    for frame in frames:
        yield frame


def _fake_console(monkeypatch) -> io.StringIO:
    out = io.StringIO()
    console = Console(file=out, no_color=True, force_terminal=False)
    monkeypatch.setattr(chat_mod, "_out_console", lambda _opts: console)
    return out


async def test_drain_download_without_opts_returns_final_frame_silently():
    stream = _progress_stream(
        DownloadProgress(task_id="t", status=DownloadStatus.DOWNLOADING, downloaded_bytes=1, total_bytes=10),
        DownloadProgress(task_id="t", status=DownloadStatus.DONE, downloaded_bytes=10, total_bytes=10, save_path="x.zip"),
    )
    final = await chat_mod._drain_download(stream)
    assert final is not None
    assert final.status is DownloadStatus.DONE
    assert final.save_path == "x.zip"


async def test_drain_download_with_color_opts_renders_progress_bar(monkeypatch):
    out = _fake_console(monkeypatch)
    opts = RenderOptions(color=True, emoji=False)
    stream = _progress_stream(
        DownloadProgress(task_id="t", status=DownloadStatus.DOWNLOADING, downloaded_bytes=5, total_bytes=100),
        DownloadProgress(task_id="t", status=DownloadStatus.DONE, downloaded_bytes=100, total_bytes=100, save_path="x.zip"),
    )
    final = await chat_mod._drain_download(stream, opts=opts, label="下载中")
    assert final is not None
    assert final.status is DownloadStatus.DONE
    assert "下载中" in out.getvalue()


async def test_drain_download_without_color_opts_prints_plain_lines(monkeypatch):
    out = _fake_console(monkeypatch)
    opts = RenderOptions(color=False, emoji=False)
    stream = _progress_stream(
        DownloadProgress(task_id="t", status=DownloadStatus.DOWNLOADING, downloaded_bytes=50, total_bytes=100),
        DownloadProgress(task_id="t", status=DownloadStatus.DONE, downloaded_bytes=100, total_bytes=100, save_path="x.zip"),
    )
    final = await chat_mod._drain_download(stream, opts=opts, label="下载中")
    assert final is not None
    assert final.status is DownloadStatus.DONE
    printed = out.getvalue()
    assert "下载中" in printed
    assert "50.0%" in printed


async def test_drain_download_stops_on_error_status(monkeypatch):
    out = _fake_console(monkeypatch)
    opts = RenderOptions(color=False, emoji=False)
    stream = _progress_stream(
        DownloadProgress(task_id="t", status=DownloadStatus.ERROR, error="boom"),
    )
    final = await chat_mod._drain_download(stream, opts=opts)
    assert final is not None
    assert final.status is DownloadStatus.ERROR
    assert final.error == "boom"
