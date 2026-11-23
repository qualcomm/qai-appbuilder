# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Support helpers: masked backend-log excerpt and the "contact support" mail."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, unquote, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from interfaces.http.routes import system as system_routes
from qai.platform import support
from qai.platform.logging.redact_log import redact_log_text

pytestmark = pytest.mark.unit


# ── redaction ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ("Authorization: Bearer abcdef0123456789xyz", "abcdef0123456789xyz"),
        ("headers={'authorization': 'Bearer abcdef0123456789'}", "abcdef0123456789"),
        ("api_key=sk-proj-AbCdEf0123456789AbCdEf", "sk-proj-AbCdEf0123456789AbCdEf"),
        ('{"client_secret": "s3cr3t-value"}', "s3cr3t-value"),
        ("refresh_token=rt.0123456789abcdef&state=1", "rt.0123456789abcdef"),
        ("password: hunter2hunter2", "hunter2hunter2"),
        ("using key sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV", "sk-ant-api03-ABCDEFGHIJKLMNOPQRSTUV"),
        (
            "id eyJhbGciOiJSUzI1NiJ9.eyJzdWIiOiIxMjM0NTYifQ.c2lnbmF0dXJlLWJ5dGVz end",
            "eyJzdWIiOiIxMjM0NTYifQ",
        ),
        ("proxy http://alice:pa55word@proxy.example:8080/x", "pa55word"),
        ("GET https://x.blob.core.windows.net/a?sv=2018&sig=AbC%2Bdef%3D&jwt=eyJ0", "AbC%2Bdef%3D"),
    ],
)
def test_credentials_are_masked(raw: str, secret: str) -> None:
    out = redact_log_text(raw)
    assert secret not in out
    assert "***" in out


@pytest.mark.parametrize(
    "line",
    [
        "chat.llm_stream.max_tokens_outgoing max_tokens=32768 model_id=qwen-plus",
        "chat.token_backend backend=pure prompt_tokens=120 completion_tokens=33",
        "auth.callback.login_ok username=jingweif email=jingweif@qti.qualcomm.com",
        "Basic configuration loaded from C:\\Users\\x\\data\\config",
    ],
)
def test_ordinary_log_lines_are_left_alone(line: str) -> None:
    assert redact_log_text(line) == line


# ── log tail ────────────────────────────────────────────────────────────────


def _write(path: Path, count: int, prefix: str) -> None:
    path.write_text("".join(f"{prefix} {i}\n" for i in range(count)), encoding="utf-8")


def test_tail_lines_reads_only_the_end(tmp_path: Path) -> None:
    log = tmp_path / "big.log"
    _write(log, 50_000, "line")
    assert support.tail_lines(log, 3, chunk=1024) == ["line 49997", "line 49998", "line 49999"]
    assert support.tail_lines(tmp_path / "missing.log", 3) == []
    assert support.tail_lines(log, 0) == []


def test_backend_log_tops_up_from_rotation_and_appends_desktop_log(tmp_path: Path) -> None:
    _write(tmp_path / "backend.log.1", 10, "old")
    _write(tmp_path / "backend.log", 3, "new")
    _write(tmp_path / "desktop-backend.log", 5, "desk")
    (tmp_path / "backend.log").write_text(
        "new 0\nnew 1 token=abc123secret\nnew 2\n", encoding="utf-8"
    )

    text = support.backend_log_text(
        tmp_path,
        lines=5,
        header=("QAI AppBuilder 9.9.9 (external)",),
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )

    lines = text.splitlines()
    assert lines[0] == "QAI AppBuilder 9.9.9 (external)"
    assert "Generated: 2026-01-02T00:00:00+00:00" in text
    body = lines[lines.index("") + 1 :]
    assert body[:5] == ["old 8", "old 9", "new 0", "new 1 token=***", "new 2"]
    assert "===== desktop-backend.log (last 5 lines) =====" in text
    assert text.rstrip().endswith("desk 4")
    assert "abc123secret" not in text


def test_backend_log_without_files_does_not_fail(tmp_path: Path) -> None:
    assert "(backend.log is empty or missing)" in support.backend_log_text(tmp_path, lines=10)


# ── contact support ─────────────────────────────────────────────────────────


def test_mailto_has_subject_and_environment_but_no_log() -> None:
    url = support.build_support_mailto(
        version="3.2.0", edition="external", system="Windows", machine="ARM64", release="11"
    )
    parts = urlsplit(url)
    assert parts.scheme == "mailto"
    assert parts.path == support.SUPPORT_EMAIL == "qaiappbuilder.support@qti.qualcomm.com"
    query = parse_qs(parts.query)
    assert query["subject"] == ["QAI AppBuilder feedback (v3.2.0)"]
    body = query["body"][0]
    assert "QAI AppBuilder 3.2.0 (external)" in body
    assert "OS: Windows 11 (ARM64)" in body
    # No raw quote / space: the Windows fallback wraps the URL in the client's own "%1" quotes.
    assert '"' not in url and " " not in url
    assert "Copy log" in unquote(url)
    assert len(url) < 1500


def test_windows_uses_the_default_client_when_there_is_one(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[str] = []
    monkeypatch.setattr(support.sys, "platform", "win32")
    monkeypatch.setattr(support, "_windows_default_handler", lambda: r"C:\mail\client.exe")
    monkeypatch.setattr(support.os, "startfile", started.append, raising=False)
    assert support.open_support_mail("mailto:x@y?subject=a") is True
    assert started == ["mailto:x@y?subject=a"]


def test_windows_falls_back_to_the_registered_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stale default ("pick an app") must not hide an installed Outlook."""
    launched: list[str] = []
    monkeypatch.setattr(support.sys, "platform", "win32")
    monkeypatch.setattr(support, "_windows_default_handler", lambda: "")
    monkeypatch.setattr(
        support,
        "_windows_registered_command",
        lambda: '"C:\\Office\\OUTLOOK.EXE" -c IPM.Note /mailto "%1"',
    )
    monkeypatch.setattr(support.subprocess, "Popen", lambda cmd, *a, **k: launched.append(cmd))
    assert support.open_support_mail("mailto:x@y?subject=a") is True
    assert launched == ['"C:\\Office\\OUTLOOK.EXE" -c IPM.Note /mailto "mailto:x@y?subject=a"']


def test_windows_without_any_client_reports_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(support.sys, "platform", "win32")
    monkeypatch.setattr(support, "_windows_default_handler", lambda: "")
    monkeypatch.setattr(support, "_windows_registered_command", lambda: "")
    assert support.mail_client_available() is False
    assert support.open_support_mail("mailto:x@y") is False


def test_a_failing_launch_reports_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(support.sys, "platform", "win32")
    monkeypatch.setattr(support, "_windows_default_handler", lambda: r"C:\mail\client.exe")

    def boom(_url: str) -> None:
        raise OSError("no association")

    monkeypatch.setattr(support.os, "startfile", boom, raising=False)
    assert support.open_support_mail("mailto:x@y") is False


# ── routes ──────────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    container = SimpleNamespace(
        repo_root=tmp_path,
        settings=SimpleNamespace(edition="external"),
        data_paths=SimpleNamespace(root=tmp_path),
    )
    app = FastAPI()
    app.include_router(
        system_routes.build_router(container=container)
    )  # router carries /api/system
    (tmp_path / "logs").mkdir()
    _write(tmp_path / "logs" / "backend.log", 30, "row")
    return TestClient(app)


def test_backend_log_route_returns_masked_plain_text(client: TestClient) -> None:
    res = client.get("/api/system/backend-log", params={"lines": 5})
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/plain")
    assert res.text.rstrip().endswith("row 29")
    assert "row 24" not in res.text
    assert client.get("/api/system/backend-log", params={"lines": 0}).status_code == 422
    assert client.get("/api/system/backend-log", params={"lines": 20001}).status_code == 422


def test_contact_support_route(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    monkeypatch.setattr(support, "open_support_mail", lambda url: seen.append(url) or False)
    res = client.post("/api/system/contact-support")
    assert res.status_code == 200
    assert res.json() == {"opened": False, "email": support.SUPPORT_EMAIL}
    assert seen and seen[0].startswith(f"mailto:{support.SUPPORT_EMAIL}?subject=")
