# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``extension.json`` validation (manifest_version 1)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from qai.chat.adapters.openai_protocol import sanitize_tool_call_name
from qai.platform.user_extensions import ManifestError, load_manifest, parse_manifest

from .conftest import manifest, tool_decl


def _parse(data: dict[str, Any], dirname: str = "demo-ext"):
    return parse_manifest(data, root=Path("/x") / dirname)


def test_minimal_manifest_without_code() -> None:
    m = _parse(manifest())
    assert m.name == "demo-ext"
    assert m.entry is None
    assert m.tools == ()
    assert m.skills.dir == "skills"
    assert dict(m.skills.default_modes) == {}


def test_ui_names_may_be_given_per_language_with_a_plain_fallback() -> None:
    per_lang = {"en": "System Info", "zh-CN": "本机信息", "zh-TW": "本機資訊"}
    m = _parse(
        manifest(
            entry="main.py",
            ui={
                "label": per_lang,
                "actions": [
                    {"id": "go", "label": {"en": "Go", "zh-CN": "开始"}},
                    {"id": "plain", "label": "Plain"},
                ],
            },
        )
    )
    assert m.ui is not None
    assert m.ui.label == "System Info"  # `label` stays a plain string
    assert dict(m.ui.labels) == per_lang
    go, plain = m.ui.actions
    assert go.label == "Go" and dict(go.labels) == {"en": "Go", "zh-CN": "开始"}
    assert plain.label == "Plain"
    assert dict(plain.labels) == {}  # a plain string: no per-language map
    assert dict(_parse(manifest(ui={"label": "Weather"})).ui.labels) == {}


@pytest.mark.parametrize("bad", [{}, {"en": 3}, 5, ["x"]])
def test_a_ui_label_must_be_text_or_a_language_map(bad: Any) -> None:
    with pytest.raises(ManifestError, match="label"):
        _parse(manifest(ui={"label": bad}))


def test_full_manifest() -> None:
    m = _parse(
        manifest(
            entry="src/main.py",
            tools=[
                tool_decl("demo_echo"),
                tool_decl("demo_run", "command"),
                tool_decl("demo_x", None),
            ],
            skills={"dir": "content/skills", "default_modes": {"demo-skill": "both"}},
        )
    )
    assert m.entry == "src/main.py"
    assert [(t.name, t.group) for t in m.tools] == [
        ("demo_echo", "read"),
        ("demo_run", "command"),
        ("demo_x", None),
    ]
    assert m.skills.dir == "content/skills"
    assert dict(m.skills.default_modes) == {"demo-skill": "both"}


def test_locale_description_prefers_en() -> None:
    assert _parse(manifest(description={"zh-CN": "中文", "en": "English"})).description == "English"


@pytest.mark.parametrize("missing", ["manifest_version", "name", "version", "description"])
def test_required_fields(missing: str) -> None:
    data = manifest()
    del data[missing]
    with pytest.raises(ManifestError, match=missing):
        _parse(data)


@pytest.mark.parametrize("bad", [2, 0, "1", True])
def test_unsupported_manifest_version(bad: Any) -> None:
    with pytest.raises(ManifestError, match="manifest_version"):
        _parse(manifest(manifest_version=bad))


def test_unknown_top_level_field_is_an_error() -> None:
    with pytest.raises(ManifestError, match=r"unknown field.*'tool'"):
        _parse(manifest(tool=[]))


def test_modelbuilder_manifest_is_rejected_with_a_clear_reason() -> None:
    data = {
        "name": "demo-ext",
        "version": "1",
        "api_version": 2,
        "description": "d",
        "python_package": "qai_ext_demo_ext",
    }
    with pytest.raises(ManifestError, match="unknown field"):
        _parse(data)


@pytest.mark.parametrize("bad", ["Demo", "demo_ext", "-demo", "demo--x", "1demo", "a" * 65])
def test_bad_extension_name(bad: str) -> None:
    with pytest.raises(ManifestError, match="name"):
        _parse(manifest(bad), dirname=bad)


def test_name_must_equal_directory() -> None:
    with pytest.raises(ManifestError, match="directory name"):
        _parse(manifest("demo-ext"), dirname="other")


@pytest.mark.parametrize("bad", ["../x.py", "/abs.py", "a/../b.py", "", ".hidden.py"])
def test_bad_entry_path(bad: str) -> None:
    with pytest.raises(ManifestError, match="entry"):
        _parse(manifest(entry=bad))


def test_tools_require_entry() -> None:
    with pytest.raises(ManifestError, match="tools requires entry"):
        _parse(manifest(tools=[tool_decl("demo_echo")]))


@pytest.mark.parametrize(
    "bad",
    [
        "Echo",
        "demo__echo",
        "mcp__x__y",
        "demo-echo",
        "_demo",
        "1demo",
        "demo.echo",
        "d:echo",
        "demo_",
        "a" * 65,
    ],
)
def test_bad_tool_name(bad: str) -> None:
    with pytest.raises(ManifestError, match=r"tools\[0\]\.name"):
        _parse(manifest(entry="main.py", tools=[tool_decl(bad)]))


@pytest.mark.parametrize("name", ["a", "a1", "demo_echo", "a_b_c", "x" * 64, "v2_run"])
def test_accepted_tool_names_survive_the_model_side_sanitizer(name: str) -> None:
    """A name the sanitizer would rewrite could no longer be dispatched to its handler."""
    (decl,) = _parse(manifest(entry="main.py", tools=[tool_decl(name)])).tools
    assert sanitize_tool_call_name(decl.name) == decl.name


def test_tool_declared_twice() -> None:
    with pytest.raises(ManifestError, match="declared twice"):
        _parse(manifest(entry="main.py", tools=[tool_decl("demo_echo"), tool_decl("demo_echo")]))


def test_tool_group_is_required_and_checked() -> None:
    with pytest.raises(ManifestError, match="missing required field 'group'"):
        _parse(manifest(entry="main.py", tools=[{"name": "demo_echo"}]))
    with pytest.raises(ManifestError, match="group"):
        _parse(manifest(entry="main.py", tools=[tool_decl("demo_echo", "write")]))


def test_unknown_tool_field_is_an_error() -> None:
    with pytest.raises(ManifestError, match=r"tools\[0\]: unknown field.*'mutating'"):
        _parse(
            manifest(
                entry="main.py", tools=[{"name": "demo_echo", "group": "read", "mutating": True}]
            )
        )


@pytest.mark.parametrize(
    ("skills", "match"),
    [
        ({"dir": "../outside"}, "skills.dir"),
        ({"dir": "/abs"}, "skills.dir"),
        ({"dir": "a/../b"}, "skills.dir"),
        ({"default_modes": {"x": "on"}}, "default_modes"),
        ({"default_modes": ["x"]}, "default_modes must be a JSON object"),
        ({"editions": ["external"]}, "unknown field"),
        ("skills", "skills must be a JSON object"),
    ],
)
def test_bad_skills(skills: Any, match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        _parse(manifest(skills=skills))


def test_load_manifest_reads_file_with_bom(tmp_path: Path) -> None:
    ext = tmp_path / "demo-ext"
    ext.mkdir()
    (ext / "extension.json").write_text(
        '{"manifest_version":1,"name":"demo-ext","version":"1","description":"d"}',
        encoding="utf-8-sig",
    )
    assert load_manifest(ext).name == "demo-ext"


def test_load_manifest_errors(tmp_path: Path) -> None:
    ext = tmp_path / "demo-ext"
    ext.mkdir()
    with pytest.raises(ManifestError, match="not found"):
        load_manifest(ext)
    (ext / "extension.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ManifestError, match="not valid JSON"):
        load_manifest(ext)
    (ext / "extension.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ManifestError, match="must be a JSON object"):
        load_manifest(ext)
