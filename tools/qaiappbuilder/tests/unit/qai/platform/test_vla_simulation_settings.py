# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

from pathlib import Path

import pytest

from qai.platform.config.settings import load_settings

pytestmark = pytest.mark.unit


def test_external_edition_requires_explicit_vla_opt_in(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    (tmp_path / "build_info.json").write_text(
        '{"edition":"external"}', encoding="utf-8"
    )
    monkeypatch.delenv("QAI_VLA_SIMULATION_ENABLED", raising=False)

    external = load_settings(repo_root=tmp_path)
    assert external.edition == "external"
    assert not external.is_internal
    assert not external.is_vla_simulation_enabled

    monkeypatch.setenv("QAI_VLA_SIMULATION_ENABLED", "true")
    vla_local = load_settings(repo_root=tmp_path)
    assert vla_local.edition == "external"
    assert not vla_local.is_internal
    assert vla_local.is_vla_simulation_enabled
