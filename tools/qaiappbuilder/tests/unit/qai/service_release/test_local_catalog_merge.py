# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Local model-catalog supplement: merge / fallback / toggle behaviour."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from qai.service_release.domain.errors import CatalogUnavailableError
from qai.service_release.infrastructure.http_catalog_source import HttpCatalogSource

_LOCAL_FILE = (
    Path(__file__).resolve().parents[4] / "factory" / "config" / "model_catalog_local.json"
)


def _entry(model_id: str, name: str) -> dict:
    return {
        "model_id": model_id,
        "name": name,
        "format": "gguf",
        "hardware": "gpu",
        "download_url": "https://example.invalid/" + model_id + ".zip",
    }


class _Settings:
    async def read(self):
        return SimpleNamespace(
            catalog_url="https://example.invalid/model_catalog.json",
            fetch_timeout_seconds=5,
            ssl_verify=True,
        )


class _Source(HttpCatalogSource):
    """Serves a canned remote body (or raises) instead of hitting the network."""

    __slots__ = ("_body", "_fail")

    def __init__(self, *, body=None, fail=False, **kw):
        super().__init__(settings=_Settings(), **kw)
        self._body = body
        self._fail = fail

    async def _fetch_text(self, url, *, timeout, ssl_verify):  # type: ignore[override]
        if self._fail:
            raise CatalogUnavailableError("network down")
        return json.dumps(self._body)


def _write_local(tmp_path: Path, entries: list[dict]) -> Path:
    p = tmp_path / "local.json"
    p.write_text(json.dumps({"models": entries}), encoding="utf-8")
    return p


def _ids(models) -> list[str]:
    return [m.model_id for m in models]


def _run(src):
    return asyncio.run(src.fetch_catalog_models())


def test_local_only_entries_are_appended_after_remote(tmp_path):
    local = _write_local(tmp_path, [_entry("local-a", "Local A")])
    src = _Source(body={"models": [_entry("remote-a", "Remote A")]}, local_catalog_path=local)
    assert _ids(_run(src)) == ["remote-a", "local-a"]


def test_same_model_id_local_wins_in_place(tmp_path):
    local = _write_local(tmp_path, [_entry("dup", "Local Dup")])
    remote = {"models": [_entry("first", "First"), _entry("dup", "Remote Dup"), _entry("last", "Last")]}
    out = _run(_Source(body=remote, local_catalog_path=local))
    assert _ids(out) == ["first", "dup", "last"]
    assert out[1].name == "Local Dup"


def test_remote_failure_falls_back_to_local(tmp_path):
    local = _write_local(tmp_path, [_entry("local-a", "Local A")])
    assert _ids(_run(_Source(fail=True, local_catalog_path=local))) == ["local-a"]


def test_remote_failure_without_local_still_raises():
    with pytest.raises(CatalogUnavailableError):
        _run(_Source(fail=True))


def test_disabled_flag_ignores_local(tmp_path):
    local = _write_local(tmp_path, [_entry("local-a", "Local A")])
    src = _Source(
        body={"models": [_entry("remote-a", "Remote A")]},
        local_catalog_path=local,
        local_catalog_enabled=lambda: False,
    )
    assert _ids(_run(src)) == ["remote-a"]


def test_missing_or_corrupt_local_file_never_breaks_remote(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    for path in (tmp_path / "absent.json", bad):
        src = _Source(body={"models": [_entry("remote-a", "Remote A")]}, local_catalog_path=path)
        assert _ids(_run(src)) == ["remote-a"]


def test_shipped_local_catalog_parses_and_has_expected_models():
    src = _Source(fail=True, local_catalog_path=_LOCAL_FILE)
    assert _ids(_run(src)) == ["qwen3.8-27b-q4_0", "gpt-oss-20b-GGUF"]
