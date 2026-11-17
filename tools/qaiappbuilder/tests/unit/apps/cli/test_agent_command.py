from __future__ import annotations

import argparse
import io
import json

import pytest

from apps.cli.__main__ import main
from apps.cli.commands.agent import _positive_float, build_extra, read_prompt


@pytest.mark.unit
def test_read_prompt_accepts_stdin_when_requested() -> None:
    args = argparse.Namespace(prompt=None, prompt_file=None, read_stdin=True)

    assert read_prompt(args, stdin=io.StringIO("hello from stdin")) == "hello from stdin"


@pytest.mark.unit
def test_read_prompt_rejects_multiple_sources() -> None:
    args = argparse.Namespace(prompt="hello", prompt_file=None, read_stdin=True)

    with pytest.raises(ValueError, match="exactly one prompt source"):
        read_prompt(args, stdin=io.StringIO("ignored"))


@pytest.mark.unit
def test_build_extra_merges_structured_agent_options() -> None:
    payload = build_extra(
        extra_json=json.dumps({"temperature": 0.2}),
        system_prompt="Return JSON only.",
        tool_mode="code",
        tool_params=['model_paths=["model.onnx"]', "batch=true", "label=demo"],
    )

    assert payload == {
        "temperature": 0.2,
        "system_prompt": "Return JSON only.",
        "tool_mode": "code",
        "tool_params": {
            "model_paths": ["model.onnx"],
            "batch": True,
            "label": "demo",
        },
    }


@pytest.mark.unit
def test_positive_float_rejects_invalid_timeout_values() -> None:
    assert _positive_float("1.5") == 1.5
    for raw in ("0", "-1", "nan", "inf"):
        with pytest.raises(argparse.ArgumentTypeError):
            _positive_float(raw)


@pytest.mark.unit
def test_build_extra_rejects_model_builder_mode() -> None:
    with pytest.raises(ValueError, match="agent-only"):
        build_extra(tool_mode="model-build")

    with pytest.raises(ValueError, match="agent-only"):
        build_extra(extra_json=json.dumps({"tool_mode": "model_builder"}))


@pytest.mark.unit
def test_agent_run_without_model_fails_with_clear_message(capsys) -> None:
    code = main(["agent", "run", "hello"])

    captured = capsys.readouterr()
    assert code == 2
    assert "no model specified" in captured.err
    assert "--model" in captured.err


@pytest.mark.unit
def test_agent_run_blank_model_is_rejected(capsys) -> None:
    code = main(["agent", "run", "--model", "   ", "hello"])

    assert code == 2
    assert "no model specified" in capsys.readouterr().err
