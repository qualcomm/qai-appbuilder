# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Extension prompt fragments: manifest ``prompts[]`` + host loading."""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from apps.api._user_extensions_di import prompt_addenda_for
from qai.platform.user_extensions import PROMPT_MAX_CHARS, ManifestError, parse_manifest

from .conftest import manifest


def _parse(**fields: Any):
    return parse_manifest(manifest(**fields), root=Path("/x/demo-ext"))


def _ext(make_ext, name: str, **fields: Any) -> Path:
    return make_ext(name, manifest_data=manifest(name, **fields))


# -- manifest ------------------------------------------------------------------


def test_prompts_parsed() -> None:
    m = _parse(prompts=[{"when": "always", "text": "A"}, {"when": "active", "file": "p/active.md"}])
    assert [(p.when, p.text, p.file) for p in m.prompts] == [
        ("always", "A", None),
        ("active", None, "p/active.md"),
    ]


@pytest.mark.parametrize(
    ("prompts", "match"),
    [
        ("x", "prompts must be an array"),
        ([{"text": "A"}], "missing required field 'when'"),
        ([{"when": "sometimes", "text": "A"}], "'always' or 'active'"),
        ([{"when": "always"}], "exactly one of"),
        ([{"when": "always", "text": "A", "file": "a.md"}], "exactly one of"),
        ([{"when": "always", "text": "  "}], "non-empty"),
        ([{"when": "always", "text": "x" * (PROMPT_MAX_CHARS + 1)}], "longer than"),
        ([{"when": "always", "file": "../a.md"}], r"prompts\[0\]\.file"),
        ([{"when": "always", "text": "A", "order": 1}], "unknown field"),
    ],
)
def test_bad_prompts(prompts: Any, match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        _parse(prompts=prompts)


# -- host ------------------------------------------------------------------------


def test_fragments_load_and_render(make_ext, new_host) -> None:
    ext = _ext(
        make_ext,
        "b-ext",
        prompts=[{"when": "always", "text": "B always"}, {"when": "active", "file": "active.md"}],
    )
    (ext / "active.md").write_text("\ufeffB active\n", encoding="utf-8")  # BOM + newline trimmed
    _ext(make_ext, "a-ext", prompts=[{"when": "always", "text": "A always"}])
    host = new_host()
    statuses = {s.name: s for s in host.load_all()}
    assert statuses["b-ext"].prompts == {"always": 1, "active": 1}

    container = SimpleNamespace(user_extensions=SimpleNamespace(host=host))
    plain = prompt_addenda_for(container, frozenset())
    assert plain.system_suffix == (
        '<extension_instructions extension="a-ext">\nA always\n</extension_instructions>\n\n'
        '<extension_instructions extension="b-ext">\nB always\n</extension_instructions>'
    )
    assert plain.context == ""
    selected = prompt_addenda_for(container, frozenset({"b-ext", "no-such"}))
    assert selected.system_suffix == plain.system_suffix  # byte-stable across selections
    assert selected.context == (
        '<extension_instructions extension="b-ext">\nB active\n</extension_instructions>'
    )


def test_fragments_follow_unload_and_reload(make_ext, new_host) -> None:
    ext = _ext(make_ext, "demo-ext", prompts=[{"when": "always", "file": "p.md"}])
    (ext / "p.md").write_text("v1", encoding="utf-8")
    host = new_host()
    host.load_all()
    assert [f.text for f in host.prompt_fragments("always")] == ["v1"]
    (ext / "p.md").write_text("v2", encoding="utf-8")
    host.reload("demo-ext")
    assert [f.text for f in host.prompt_fragments("always")] == ["v2"]
    host.unload("demo-ext")
    assert host.prompt_fragments("always") == ()


@pytest.mark.parametrize(
    ("setup", "match"),
    [
        ("missing", "not found"),
        ("too_long", "longer than"),
        ("not_utf8", "cannot be read"),
    ],
)
def test_bad_prompt_file_fails_the_extension(make_ext, new_host, setup: str, match: str) -> None:
    ext = _ext(make_ext, "demo-ext", prompts=[{"when": "always", "file": "p.md"}])
    if setup == "too_long":
        (ext / "p.md").write_text("x" * (PROMPT_MAX_CHARS + 1), encoding="utf-8")
    elif setup == "not_utf8":
        (ext / "p.md").write_bytes(b"\xff\xfe\xfa")
    host = new_host()
    (status,) = host.load_all()
    assert status.state == "failed" and match in status.error
    assert host.prompt_fragments("always") == ()


def test_prompt_file_escaping_through_a_link_fails(make_ext, new_host, tmp_path: Path) -> None:
    ext = _ext(make_ext, "demo-ext", prompts=[{"when": "always", "file": "inner/p.md"}])
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "p.md").write_text("secret", encoding="utf-8")
    try:
        if os.name == "nt":
            importlib.import_module("_winapi").CreateJunction(str(outside), str(ext / "inner"))
        else:
            (ext / "inner").symlink_to(outside, target_is_directory=True)
    except (OSError, AttributeError):
        pytest.skip("directory links not supported here")
    (status,) = new_host().load_all()
    assert status.state == "failed" and "outside the extension" in status.error


def test_a_disabled_extension_contributes_no_prompt(make_ext, new_host) -> None:
    _ext(make_ext, "demo-ext", prompts=[{"when": "always", "text": "A"}])
    host = new_host(is_disabled=lambda _n: True)
    host.load_all()
    assert host.prompt_fragments("always") == ()


def test_no_host_means_no_addenda() -> None:
    assert prompt_addenda_for(SimpleNamespace(), frozenset({"x"})).system_suffix == ""
