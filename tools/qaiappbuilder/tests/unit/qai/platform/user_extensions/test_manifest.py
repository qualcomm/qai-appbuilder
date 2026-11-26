# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``extension.json`` validation (ModelBuilder api_version 2 subset)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from qai.platform.user_extensions import ManifestError, load_manifest, parse_manifest

from .conftest import tool_decl


def _base(name: str = "demo-ext", **over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": name,
        "version": "1.0.0",
        "api_version": 2,
        "description": "demo",
    }
    data.update(over)
    return data


def _parse(data: dict[str, Any], dirname: str = "demo-ext"):
    return parse_manifest(data, root=Path("/x") / dirname)


def test_minimal_manifest_without_code() -> None:
    m = _parse(_base())
    assert m.name == "demo-ext"
    assert m.python_package is None
    assert m.tools == ()
    assert m.warnings == ()


def test_locale_description_prefers_en() -> None:
    m = _parse(_base(description={"zh-CN": "中文", "en": "English"}))
    assert m.description == "English"


def test_tools_parsed() -> None:
    m = _parse(
        _base(
            python_package="qai_ext_demo_ext",
            contributes={"tools": [tool_decl("demo_ext_echo", mutating=False)]},
        )
    )
    (decl,) = m.tools
    assert decl.name == "demo_ext_echo"
    assert decl.group == "read"
    assert decl.result_category == "query"
    assert decl.output_kind == "text"
    assert decl.mutating is False


@pytest.mark.parametrize("missing", ["name", "version", "api_version", "description"])
def test_required_fields(missing: str) -> None:
    data = _base()
    del data[missing]
    with pytest.raises(ManifestError, match=missing):
        _parse(data)


@pytest.mark.parametrize("bad", ["Demo", "demo_ext", "-demo", "demo--x", "1demo", "a" * 65])
def test_bad_extension_name(bad: str) -> None:
    with pytest.raises(ManifestError, match="name"):
        _parse(_base(bad), dirname=bad)


def test_name_must_equal_directory() -> None:
    with pytest.raises(ManifestError, match="directory name"):
        _parse(_base("demo-ext"), dirname="other")


def test_unsupported_api_version() -> None:
    with pytest.raises(ManifestError, match="api_version 1"):
        _parse(_base(api_version=1))


def test_python_package_must_follow_name() -> None:
    with pytest.raises(ManifestError, match="qai_ext_demo_ext"):
        _parse(_base(python_package="qai_ext_other"))


def test_tools_require_python_package() -> None:
    with pytest.raises(ManifestError, match="requires python_package"):
        _parse(_base(contributes={"tools": [tool_decl("demo_echo")]}))


@pytest.mark.parametrize(
    "bad",
    ["Echo", "demo__echo", "mcp__x__y", "demo-echo", "_demo", "demo.echo", "d:echo", "a" * 65],
)
def test_bad_tool_name(bad: str) -> None:
    data = _base(python_package="qai_ext_demo_ext", contributes={"tools": [tool_decl(bad)]})
    with pytest.raises(ManifestError, match=r"tools\[0\]\.name"):
        _parse(data)


def test_tool_declared_twice() -> None:
    data = _base(
        python_package="qai_ext_demo_ext",
        contributes={"tools": [tool_decl("demo_echo"), tool_decl("demo_echo")]},
    )
    with pytest.raises(ManifestError, match="declared twice"):
        _parse(data)


@pytest.mark.parametrize(
    ("key", "value"),
    [("group", "write"), ("result_category", "blob"), ("output_kind", "html"), ("mutating", "yes")],
)
def test_bad_tool_attribute(key: str, value: Any) -> None:
    data = _base(
        python_package="qai_ext_demo_ext",
        contributes={"tools": [tool_decl("demo_echo", **{key: value})]},
    )
    with pytest.raises(ManifestError, match=key):
        _parse(data)


def test_null_group_allowed() -> None:
    data = _base(
        python_package="qai_ext_demo_ext",
        contributes={"tools": [tool_decl("demo_echo", group=None)]},
    )
    assert _parse(data).tools[0].group is None


def test_unsupported_parts_warn_not_fail() -> None:
    data = _base(
        python_package="qai_ext_demo_ext",
        frontend="frontend/index.ts",
        contributes={
            "modes": [{"id": "demo-ext"}],
            "tools": [tool_decl("demo_echo", host_local=True, tier="SERVICE_GATED")],
        },
    )
    m = _parse(data)
    text = "\n".join(m.warnings)
    assert "'frontend'" in text
    assert "contributes.modes" in text
    assert "host_local" in text and "tier" in text
    assert m.tools[0].unsupported == ("host_local", "tier")


def test_load_manifest_reads_file_with_bom(tmp_path: Path) -> None:
    ext = tmp_path / "demo-ext"
    ext.mkdir()
    (ext / "extension.json").write_text(
        '{"name":"demo-ext","version":"1","api_version":2,"description":"d"}',
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


def test_modelbuilder_template_manifest_parses() -> None:
    """The QAIModelBuilder ``_template`` manifest (verbatim shape) is accepted."""
    data = {
        "name": "template-ext",
        "version": "1.0.0",
        "api_version": 2,
        "description": {"en": "Copyable extension template", "zh-CN": "模板"},
        "editions": ["internal", "external"],
        "python_package": "qai_ext_template_ext",
        "frontend": "frontend/index.ts",
        "guard_tokens": ["template-ext", "template_ext", "TemplateExt"],
        "lifecycle": {"shutdown_timeout_s": 5},
        "contributes": {
            "modes": [{"id": "template-ext", "toolbar_key": "template_ext"}],
            "tools": [
                {
                    "name": "template_ext_echo",
                    "group": "read",
                    "result_category": "query",
                    "output_kind": "text",
                    "read_only_ok": True,
                    "mutating": False,
                    "tier": "SERVICE_GATED",
                }
            ],
            "settings": {"config_sections": ["template_ext_cfg"]},
        },
    }
    m = _parse(data, dirname="template-ext")
    assert m.tool_names == frozenset({"template_ext_echo"})
    assert any("contributes.settings" in w for w in m.warnings)
