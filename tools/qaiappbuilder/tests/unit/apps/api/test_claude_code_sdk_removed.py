# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""The ``claude_agent_sdk`` Claude Code backend is gone; old configs must still load.

Before the removal ``tools.cc_backend`` defaulted to ``sdk`` and users could have
stored ``cc_backend = "sdk"``, ``cc_cli_path`` and ``cc_enable_file_checkpointing``
in ``user_config.toml`` or in a session config. None of that may start failing to
load, and the provider must always be the HTTP adapter.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from apps.api._ai_coding_di import _build_cc_provider
from qai.ai_coding.application.use_cases.health_status import (
    HealthStatusResult,
    HealthStatusUseCase,
)
from qai.ai_coding.domain import CodingSessionConfig
from qai.ai_coding.infrastructure import providers
from qai.ai_coding.infrastructure.providers import ClaudeCodeProvider
from qai.platform.config.settings import ToolsSettings


def test_legacy_sdk_value_is_accepted_and_normalised_to_http() -> None:
    cfg = CodingSessionConfig(
        cc_backend="sdk", cli_path="C:/x/claude.exe", enable_file_checkpointing=True
    )
    assert cfg.cc_backend == "http"
    # the other two legacy fields are still readable (they just do nothing)
    assert cfg.cli_path == "C:/x/claude.exe"
    assert cfg.enable_file_checkpointing is True


def test_unknown_backend_is_still_rejected() -> None:
    with pytest.raises(ValueError):
        CodingSessionConfig(cc_backend="bogus")


def test_default_backend_is_http() -> None:
    assert CodingSessionConfig().cc_backend == "http"
    assert ToolsSettings().cc_backend == "http"


def test_old_user_config_keys_still_load() -> None:
    tools = ToolsSettings(
        cc_backend="sdk", cc_cli_path="C:/x/claude.exe", cc_enable_file_checkpointing=True
    )
    assert (tools.cc_backend, tools.cc_cli_path) == ("sdk", "C:/x/claude.exe")
    assert tools.cc_enable_file_checkpointing is True


@pytest.mark.parametrize("backend", ["sdk", "http", ""])
def test_provider_is_always_the_http_adapter(backend: str) -> None:
    container = SimpleNamespace(
        settings=SimpleNamespace(tools=SimpleNamespace(cc_backend=backend, cc_cli_path="")),
        secret_store=SimpleNamespace(),
    )
    provider = _build_cc_provider(container, config_reader=lambda: {})  # type: ignore[arg-type]
    assert isinstance(provider, ClaudeCodeProvider)


def test_sdk_provider_and_cli_locator_are_gone() -> None:
    for name in ("ClaudeCodeSdkProvider", "claude_sdk_available", "locate_claude_cli"):
        assert not hasattr(providers, name)


def test_health_reports_no_sdk_but_keeps_the_wire_fields() -> None:
    assert HealthStatusResult.__dataclass_fields__["sdk_available"].default is False
    assert HealthStatusResult.__dataclass_fields__["sdk_version"].default == ""
    assert "sdk_probe" not in HealthStatusUseCase.__init__.__code__.co_varnames
