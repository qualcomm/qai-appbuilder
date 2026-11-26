# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""User-editable settings: manifest ``settings[]``, the card, update rules, ``ctx.settings``."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from qai.platform.user_extensions import (
    ExtensionSecrets,
    ExtensionStateStore,
    ManifestError,
    SettingsValidationError,
    module_name_for,
    parse_manifest,
    settings_view,
    update_settings,
)

from .conftest import FakeSecretStore, manifest

SETTINGS = [
    {"key": "endpoint", "type": "text", "label": "Endpoint", "default": "https://a"},
    {"key": "token", "type": "secret", "label": {"en": "Token", "zh-CN": "令牌"}, "required": True},
    {"key": "limit", "type": "number", "label": "Limit", "min": 1, "max": 10, "default": 3},
    {"key": "region", "type": "select", "label": "Region", "options": ["us", "eu"]},
    {"key": "verbose", "type": "bool", "label": "Verbose", "default": False},
]


def _m(settings=SETTINGS):
    return parse_manifest(manifest(settings=settings), root=Path("/x/demo-ext"))


@pytest.fixture
def env(tmp_path: Path):
    store = FakeSecretStore()
    return (
        ExtensionStateStore(tmp_path / "extensions.json"),
        ExtensionSecrets(store, "demo-ext"),
        store,
    )


# -- manifest ------------------------------------------------------------------


def test_settings_parsed() -> None:
    by_key = {s.key: s for s in _m().settings}
    assert by_key["token"].label == "Token" and by_key["token"].required
    assert by_key["limit"].min == 1 and by_key["limit"].default == 3
    assert by_key["region"].options == ("us", "eu")


@pytest.mark.parametrize(
    ("setting", "match"),
    [
        ({"key": "Bad", "type": "text", "label": "x"}, "key"),
        ({"key": "a", "type": "color", "label": "x"}, "type"),
        ({"key": "a", "type": "text"}, "label"),
        ({"key": "a", "type": "select", "label": "x"}, "options"),
        ({"key": "a", "type": "select", "label": "x", "options": ["u", "u"]}, "distinct"),
        (
            {"key": "a", "type": "text", "label": "x", "options": ["u"]},
            "only valid for type 'select'",
        ),
        ({"key": "a", "type": "text", "label": "x", "min": 1}, "only valid for type 'number'"),
        ({"key": "a", "type": "secret", "label": "x", "default": "s"}, "not allowed for a secret"),
        ({"key": "a", "type": "number", "label": "x", "max": 2, "default": 5}, "must be <= 2"),
        ({"key": "a", "type": "bool", "label": "x", "default": "yes"}, "true or false"),
        ({"key": "a", "type": "text", "label": "x", "hint": "?"}, "unknown field"),
    ],
)
def test_bad_settings(setting: dict[str, Any], match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        _m([setting])


def test_duplicate_key() -> None:
    with pytest.raises(ManifestError, match="declared twice"):
        _m([{"key": "a", "type": "text", "label": "x"}, {"key": "a", "type": "bool", "label": "y"}])


# -- card + update ----------------------------------------------------------------


def test_view_never_returns_secret_values(env) -> None:
    state, secrets, _store = env
    secrets.set("token", "s3cret")
    view = {i["key"]: i for i in settings_view(_m(), state, secrets)}
    assert view["token"] == {
        "key": "token",
        "type": "secret",
        "label": "Token",
        "description": "",
        "required": True,
        "is_set": True,
    }
    assert "s3cret" not in repr(view)
    assert view["endpoint"]["value"] == "https://a" and view["endpoint"]["default"] == "https://a"
    assert view["limit"]["min"] == 1 and view["region"]["options"] == ["us", "eu"]


def test_update_stores_plain_values_and_secrets_separately(env) -> None:
    state, secrets, store = env
    update_settings(_m(), state, secrets, {"token": "s3cret", "limit": 7, "region": "eu"})
    assert state.settings("demo-ext") == {"limit": 7, "region": "eu"}
    assert store.get("ext.demo-ext", "token") == "s3cret"
    assert "s3cret" not in state.path.read_text("utf-8")


def test_update_is_partial_and_null_clears(env) -> None:
    state, secrets, _ = env
    update_settings(_m(), state, secrets, {"token": "t", "limit": 7, "region": "eu"})
    update_settings(_m(), state, secrets, {"limit": None})  # token not resent: kept
    assert state.settings("demo-ext") == {"region": "eu"}
    assert secrets.get("token") == "t"


def test_update_is_all_or_nothing_and_reports_every_bad_field(env) -> None:
    state, secrets, _ = env
    with pytest.raises(SettingsValidationError) as exc:
        update_settings(
            _m(),
            state,
            secrets,
            {"limit": 99, "region": "mars", "verbose": "yes", "nope": 1, "endpoint": "ok"},
        )
    assert set(exc.value.errors) == {"limit", "region", "verbose", "nope", "token"}
    assert exc.value.errors["token"] == "is required"
    assert state.settings("demo-ext") == {}  # nothing written


def test_clearing_a_required_secret_is_refused(env) -> None:
    state, secrets, _ = env
    update_settings(_m(), state, secrets, {"token": "t"})
    with pytest.raises(SettingsValidationError, match="token: is required"):
        update_settings(_m(), state, secrets, {"token": None})
    assert secrets.get("token") == "t"


# -- ctx.settings ------------------------------------------------------------------


def test_ctx_settings_are_live_and_exclude_secrets(make_ext, new_host, tmp_path) -> None:
    state = ExtensionStateStore(tmp_path / "s.json")
    code = "SAVED = {}\ndef activate(ctx):\n    SAVED['ctx'] = ctx\n"
    make_ext("demo-ext", code=code, settings=SETTINGS)
    host = new_host(stored_settings=state.settings)
    (status,) = host.load_all()
    assert status.state == "loaded", status.error
    ctx = sys.modules[module_name_for("demo-ext")].SAVED["ctx"]

    assert dict(ctx.settings) == {
        "endpoint": "https://a",
        "limit": 3,
        "region": None,
        "verbose": False,
    }
    state.set_settings("demo-ext", {"limit": 9})
    assert ctx.settings["limit"] == 9  # no reload needed
    with pytest.raises(TypeError):
        ctx.settings["limit"] = 1  # type: ignore[index]
    assert "token" not in ctx.settings


def test_host_manifest_works_for_a_disabled_extension(make_ext, new_host) -> None:
    make_ext("demo-ext", manifest_data=manifest("demo-ext", settings=SETTINGS))
    host = new_host(is_disabled=lambda _n: True)
    host.load_all()
    assert [s.key for s in host.manifest("demo-ext").settings][:2] == ["endpoint", "token"]
