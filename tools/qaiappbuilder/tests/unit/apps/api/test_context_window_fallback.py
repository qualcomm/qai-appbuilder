# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""A configured cloud model without ``context_length`` falls back to the family table."""

from __future__ import annotations

import pytest

from apps.api._model_resolver_bridge import ModelCatalogProviderLookupBridge
from qai.chat.domain.errors import MissingModelContextLengthError
from qai.chat.domain.model_profiles import get_context_limit


class _Registry:
    def __init__(self, models: list[dict]) -> None:
        self._rows = [{"provider_id": "p", "config": {"base_url": "u", "models": models}}]

    async def list_provider_configs(self):
        return self._rows


def _bridge(models: list[dict]) -> ModelCatalogProviderLookupBridge:
    return ModelCatalogProviderLookupBridge(provider_registry=_Registry(models))


@pytest.mark.asyncio
async def test_explicit_value_wins() -> None:
    b = _bridge([{"model_id": "deepseek-chat", "context_length": 4096}])
    assert await b.context_window("deepseek-chat", "p") == 4096


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [{}, {"context_length": None}, {"context_length": 0}])
async def test_missing_value_uses_family_table(missing: dict) -> None:
    b = _bridge([{"model_id": "deepseek-chat", **missing}])
    assert await b.context_window("deepseek-chat", "p") == get_context_limit("deepseek-chat")


@pytest.mark.asyncio
async def test_unconfigured_model_still_raises() -> None:
    b = _bridge([{"model_id": "other", "context_length": 8192}])
    with pytest.raises(MissingModelContextLengthError):
        await b.context_window("deepseek-chat", "p")


@pytest.mark.parametrize(
    "model,expected",
    [
        ("deepseek-chat", 128_000),
        ("deepseek-reasoner", 128_000),
        ("qwen-plus", 131_072),
        ("qwen-max", 32_768),
        ("qwen3-235b-a22b", 131_072),
        ("glm-4.6", 200_000),
        ("glm-4-plus", 128_000),
    ],
)
def test_preset_families_have_sane_context(model: str, expected: int) -> None:
    assert get_context_limit(model) == expected
