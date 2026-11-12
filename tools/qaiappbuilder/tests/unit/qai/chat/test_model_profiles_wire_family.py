# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Sampling params are no longer user-tunable, so the backend must get them right.

* an ALIAS of a locked reasoning model is classified by its wire id;
* family regexes do not catch unrelated ids (``park``, ``markdown``, ``marco-o1``);
* an unknown model gets no ``max_tokens`` at all (gateway default).
"""

from __future__ import annotations

import pytest

from qai.chat.domain.model_profiles import _match_family, get_model_profile
from qai.chat.infrastructure.provider_routing_stream import (
    _inject_family_sampling_defaults,
)


def test_alias_of_gpt5_is_locked_via_api_model_id() -> None:
    plain = get_model_profile("my-reasoner")
    assert plain.temperature_fixed is None  # the pre-fix behaviour: "unknown"
    aliased = get_model_profile("my-reasoner", api_model_id="gpt-5")
    assert aliased.temperature_fixed == 1.0 and aliased.top_p_fixed == 1.0


def test_injection_locks_temperature_for_an_alias() -> None:
    extra: dict = {"temperature": 0.2}
    _inject_family_sampling_defaults(
        extra, model_id="my-reasoner", is_local=False, api_model_id="gpt-5.5"
    )
    assert extra["temperature"] == 1.0


def test_unknown_wire_id_keeps_display_family() -> None:
    p = get_model_profile("deepseek-reasoner", api_model_id="some-opaque-id")
    assert p.temperature_fixed == 1.0


@pytest.mark.parametrize("model", ["park-assistant", "markdown-llm", "remark-1", "marco-o1", "llama-3-o1x"])
def test_overbroad_regexes_do_not_match_unrelated_ids(model: str) -> None:
    assert _match_family(model)["name"] == "unknown"


@pytest.mark.parametrize(
    "model,family",
    [
        ("ark-code-latest", "doubao"),
        ("doubao-seed-2.0-code", "doubao"),
        ("volcengine/foo", "doubao"),
        ("o3", "gpt_5"),
        ("o3-mini", "gpt_5"),
        ("openai/o4-mini", "gpt_5"),
        ("azure::o1", "gpt_5"),
        ("gpt-5.5", "gpt_5"),
    ],
)
def test_real_ids_still_match(model: str, family: str) -> None:
    assert _match_family(model)["name"] == family


def test_unknown_model_sends_no_max_tokens_and_no_top_p() -> None:
    extra: dict = {}
    _inject_family_sampling_defaults(extra, model_id="acme-llm-7b", is_local=False)
    assert "max_tokens" not in extra and "top_p" not in extra
    assert extra["temperature"] == 0.7


@pytest.mark.asyncio
async def test_select_target_classifies_an_alias_by_its_wire_id() -> None:
    """Wiring: ``_select_target`` hands the resolved ``api_model_id`` to the profile."""
    from types import SimpleNamespace

    from qai.chat.application.ports import LLMStreamRequest, ResolvedModel
    from qai.chat.infrastructure.provider_routing_stream import (
        ProviderRoutingLLMStream,
    )

    class _Resolver:
        async def resolve(self, hint):  # noqa: ANN001
            return ResolvedModel(
                base_url="http://x.test/v1", api_key="k",
                model_id="my-reasoner", api_model_id="gpt-5",
            )

    stream = ProviderRoutingLLMStream(
        default_stream=SimpleNamespace(), model_resolver=_Resolver(),
        ids=SimpleNamespace(),
    )
    req = LLMStreamRequest(
        conversation_id="c", tab_id="t", prompt="hi", history=(),
        model_hint="my-reasoner", extra={"temperature": 0.2},
    )
    _transport, effective = await stream._select_target(req)  # noqa: SLF001
    assert effective.extra["temperature"] == 1.0
