# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

import pytest

from qai.user_prefs.application.use_cases.forge_config import LoadForgeConfigUseCase

pytestmark = pytest.mark.unit


def _use_case(
    *, is_internal: bool, vla_simulation_enabled: bool = False
) -> LoadForgeConfigUseCase:
    return LoadForgeConfigUseCase(
        load_document_use_case=None,  # type: ignore[arg-type]
        forge_config_key="forge.config",
        bind_host="127.0.0.1",
        is_internal=is_internal,
        vla_simulation_enabled=vla_simulation_enabled,
    )


def test_vla_simulation_toolbar_module_is_internal_only() -> None:
    internal_doc: dict[str, object] = {}
    external_doc: dict[str, object] = {}

    _use_case(is_internal=True)._inject_toolbar_defaults(internal_doc)  # type: ignore[arg-type]
    _use_case(is_internal=False)._inject_toolbar_defaults(external_doc)  # type: ignore[arg-type]

    internal_modules = internal_doc["ui"]["toolbar_modules"]  # type: ignore[index]
    assert internal_modules["vla_simulation"] == {
        "enabled": True,
        "order": 55,
        "mode": "vla-simulation",
        "i18n": "index.vlaSimulationMode",
        "icon": "vla_simulation",
    }
    external_modules = external_doc["ui"]["toolbar_modules"]  # type: ignore[index]
    assert "vla_simulation" not in external_modules


def test_vla_only_opt_in_does_not_enable_other_internal_modes() -> None:
    doc: dict[str, object] = {}

    _use_case(
        is_internal=False,
        vla_simulation_enabled=True,
    )._inject_toolbar_defaults(doc)  # type: ignore[arg-type]

    modules = doc["ui"]["toolbar_modules"]  # type: ignore[index]
    assert "pro" not in modules
    assert "gomaster" not in modules
    assert modules["vla_simulation"]["mode"] == "vla-simulation"


def test_vla_simulation_toolbar_user_settings_are_preserved() -> None:
    doc: dict[str, object] = {
        "ui": {
            "toolbar_modules": {
                "vla_simulation": {"enabled": False, "order": 75},
            },
        },
    }

    _use_case(is_internal=True)._inject_toolbar_defaults(doc)  # type: ignore[arg-type]

    modules = doc["ui"]["toolbar_modules"]  # type: ignore[index]
    assert modules["vla_simulation"] == {
        "enabled": False,
        "order": 75,
        "mode": "vla-simulation",
        "i18n": "index.vlaSimulationMode",
        "icon": "vla_simulation",
    }
