# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Unit tests for ``ProcessBackedInferenceService`` V1-parity CLI builder.

Covers PENDING-WORK §1 P1-3 — V2 spawn must invoke GenieAPIService.exe with
V1's exact short-option CLI: ``-c <models_root>/<model_name>/config.json
-l -n -1 -p <port> -d <loglevel>`` (V1 ``backend/main.py:5251-5267``
``_build_service_args`` is the truth source).

Tests stub out the actual ``Popen`` call (``_do_start``) and capture the
``args`` list the adapter would have spawned, asserting:

- argv shape matches V1 byte-for-byte (positional order, short flags only)
- ``loglevel`` resolution honours arg > provider > V1 default 3
- ``models_root`` resolution honours provider > static field > install_dir/models
- ``_sync_service_config_model`` writes the V1 wire format
  (first enabled NPU slot + ``default_model``; ensure_ascii=False; indent=4)
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from qai.model_runtime.infrastructure.process_service import (
    ProcessBackedInferenceService,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _ArgvCapture:
    """Drop-in replacement for ``_do_start`` that records argv."""

    def __init__(self) -> None:
        self.args: list[str] | None = None
        self.port: int | None = None
        self.model_name: str | None = None

    def __call__(
        self, args: list[str], port: int, model_name: str | None
    ) -> None:
        self.args = list(args)
        self.port = port
        self.model_name = model_name


def _make_adapter(
    *,
    install_dir: str = "C:/svc",
    default_port: int = 8000,
    models_root: str = "",
    models_root_provider: Any = None,
    loglevel_provider: Any = None,
) -> tuple[ProcessBackedInferenceService, _ArgvCapture]:
    """Build an adapter with ``_do_start`` stubbed out for argv capture."""
    svc = ProcessBackedInferenceService(
        install_dir=install_dir,
        default_port=default_port,
        models_root=models_root,
        models_root_provider=models_root_provider,
        loglevel_provider=loglevel_provider,
    )
    capture = _ArgvCapture()
    # Replace the blocking spawn step with a recorder. Also disable the
    # service_config.json sync side-effect so tests don't need a real file
    # — sync is exercised separately below.
    svc._do_start = capture  # type: ignore[assignment]
    svc._sync_service_config_model = lambda model_name: None  # type: ignore[assignment]
    return svc, capture


# ---------------------------------------------------------------------------
# argv shape — V1 _build_service_args parity
# ---------------------------------------------------------------------------


class TestBuildV1Cli:
    @pytest.mark.asyncio
    async def test_start_with_model_builds_v1_cli(self) -> None:
        svc, cap = _make_adapter(
            install_dir="C:/svc", models_root="C:/models"
        )
        await svc.start(model_name="qwen3-8b", port=8910, loglevel=3)
        # V1 order: -c -l -n -1 -p <port> -d <loglevel>
        expected_cfg = str(Path("C:/models") / "qwen3-8b" / "config.json")
        assert cap.args == [
            "-c",
            expected_cfg,
            "-l",
            "-n",
            "-1",
            "-p",
            "8910",
            "-d",
            "3",
        ]
        assert cap.port == 8910
        assert cap.model_name == "qwen3-8b"

    @pytest.mark.asyncio
    async def test_start_without_model_skips_config(self) -> None:
        svc, cap = _make_adapter(install_dir="C:/svc")
        await svc.start(port=9001, loglevel=4)
        # No -c when model_name is None; -l -n -1 -p -d still present.
        assert cap.args == [
            "-l",
            "-n",
            "-1",
            "-p",
            "9001",
            "-d",
            "4",
        ]
        assert "-c" not in (cap.args or [])

    @pytest.mark.asyncio
    async def test_start_uses_default_port_when_omitted(self) -> None:
        svc, cap = _make_adapter(install_dir="C:/svc", default_port=8000)
        await svc.start(loglevel=3)
        assert cap.args is not None
        assert "-p" in cap.args
        assert cap.args[cap.args.index("-p") + 1] == "8000"

    @pytest.mark.asyncio
    async def test_start_prefers_genie_config_json_when_only_that_exists(
        self, tmp_path: Path
    ) -> None:
        model_dir = tmp_path / "qwen3-vl-8b"
        model_dir.mkdir()
        (model_dir / "genie_config.json").write_text("{}", encoding="utf-8")
        svc, cap = _make_adapter(install_dir="C:/svc", models_root=str(tmp_path))
        await svc.start(model_name="qwen3-vl-8b", port=8910, loglevel=3)
        assert cap.args is not None
        cfg_idx = cap.args.index("-c")
        assert cap.args[cfg_idx + 1] == str(model_dir / "genie_config.json")

    @pytest.mark.asyncio
    async def test_start_prefers_genie_config_json_over_config_json(
        self, tmp_path: Path
    ) -> None:
        model_dir = tmp_path / "qwen3-8b"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}", encoding="utf-8")
        (model_dir / "genie_config.json").write_text("{}", encoding="utf-8")
        svc, cap = _make_adapter(install_dir="C:/svc", models_root=str(tmp_path))
        await svc.start(model_name="qwen3-8b", port=8910, loglevel=3)
        assert cap.args is not None
        cfg_idx = cap.args.index("-c")
        assert cap.args[cfg_idx + 1] == str(model_dir / "genie_config.json")


# ---------------------------------------------------------------------------
# list_models scan: genie_config.json / config.json dual naming (real dirs)
# ---------------------------------------------------------------------------


class TestListModelsGenieConfigScan:
    @pytest.mark.asyncio
    async def test_scan_finds_genie_config_json_only_model(
        self, tmp_path: Path
    ) -> None:
        model_dir = tmp_path / "qwen3-vl-8b"
        model_dir.mkdir()
        (model_dir / "genie_config.json").write_text(
            '{"dialog": {"context": {"size": 4096}}}', encoding="utf-8"
        )
        (model_dir / "weights.bin").write_bytes(b"\x00")
        (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
        svc = ProcessBackedInferenceService(install_dir="C:/svc")
        models = await svc.list_models(models_root=str(tmp_path))
        assert [m.name for m in models] == ["qwen3-vl-8b"]
        assert models[0].config_path == str(model_dir / "genie_config.json")

    @pytest.mark.asyncio
    async def test_scan_does_not_duplicate_model_with_both_files(
        self, tmp_path: Path
    ) -> None:
        model_dir = tmp_path / "qwen3-8b"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}", encoding="utf-8")
        (model_dir / "genie_config.json").write_text("{}", encoding="utf-8")
        (model_dir / "weights.bin").write_bytes(b"\x00")
        (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
        svc = ProcessBackedInferenceService(install_dir="C:/svc")
        models = await svc.list_models(models_root=str(tmp_path))
        assert [m.name for m in models] == ["qwen3-8b"]
        assert models[0].config_path == str(model_dir / "genie_config.json")

    @pytest.mark.asyncio
    async def test_scan_still_finds_config_json_only_model(
        self, tmp_path: Path
    ) -> None:
        model_dir = tmp_path / "qwen3-8b"
        model_dir.mkdir()
        (model_dir / "config.json").write_text("{}", encoding="utf-8")
        (model_dir / "weights.bin").write_bytes(b"\x00")
        (model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")
        svc = ProcessBackedInferenceService(install_dir="C:/svc")
        models = await svc.list_models(models_root=str(tmp_path))
        assert [m.name for m in models] == ["qwen3-8b"]
        assert models[0].config_path == str(model_dir / "config.json")


# ---------------------------------------------------------------------------
# loglevel precedence: arg > provider > V1 default 3
# ---------------------------------------------------------------------------


class TestLoglevelPrecedence:
    @pytest.mark.asyncio
    async def test_loglevel_default_is_three_when_none_supplied(self) -> None:
        svc, cap = _make_adapter(install_dir="C:/svc")
        await svc.start(port=8000)  # no loglevel, no provider
        assert cap.args is not None
        assert cap.args[cap.args.index("-d") + 1] == "3"

    @pytest.mark.asyncio
    async def test_loglevel_provider_used_when_arg_missing(self) -> None:
        async def _provider() -> int:
            return 5

        svc, cap = _make_adapter(
            install_dir="C:/svc", loglevel_provider=_provider
        )
        await svc.start(port=8000)
        assert cap.args is not None
        assert cap.args[cap.args.index("-d") + 1] == "5"

    @pytest.mark.asyncio
    async def test_loglevel_arg_overrides_provider(self) -> None:
        async def _provider() -> int:
            return 5

        svc, cap = _make_adapter(
            install_dir="C:/svc", loglevel_provider=_provider
        )
        await svc.start(port=8000, loglevel=2)
        assert cap.args is not None
        assert cap.args[cap.args.index("-d") + 1] == "2"

    @pytest.mark.asyncio
    async def test_loglevel_provider_failure_falls_back_to_default(
        self,
    ) -> None:
        async def _broken_provider() -> int:
            raise RuntimeError("forge.config unreadable")

        svc, cap = _make_adapter(
            install_dir="C:/svc", loglevel_provider=_broken_provider
        )
        await svc.start(port=8000)
        assert cap.args is not None
        assert cap.args[cap.args.index("-d") + 1] == "3"

    @pytest.mark.asyncio
    async def test_loglevel_sync_provider_supported(self) -> None:
        # Tests / minimal containers may pass a plain (non-async) callable.
        svc, cap = _make_adapter(
            install_dir="C:/svc", loglevel_provider=lambda: 4
        )
        await svc.start(port=8000)
        assert cap.args is not None
        assert cap.args[cap.args.index("-d") + 1] == "4"


# ---------------------------------------------------------------------------
# models_root resolution: provider > static field > install_dir/models
# ---------------------------------------------------------------------------


class TestModelsRootResolution:
    @pytest.mark.asyncio
    async def test_models_root_provider_used_when_present(self) -> None:
        async def _provider() -> str:
            return "C:/custom/models"

        svc, cap = _make_adapter(
            install_dir="C:/svc",
            models_root="C:/static/models",  # ignored when provider returns
            models_root_provider=_provider,
        )
        await svc.start(model_name="m1", port=8000, loglevel=3)
        assert cap.args is not None
        cfg_idx = cap.args.index("-c")
        assert cap.args[cfg_idx + 1] == str(
            Path("C:/custom/models") / "m1" / "config.json"
        )

    @pytest.mark.asyncio
    async def test_models_root_falls_back_to_static_field(self) -> None:
        # No provider → static models_root field.
        svc, cap = _make_adapter(
            install_dir="C:/svc", models_root="C:/static/models"
        )
        await svc.start(model_name="m1", port=8000, loglevel=3)
        assert cap.args is not None
        cfg_idx = cap.args.index("-c")
        assert cap.args[cfg_idx + 1] == str(
            Path("C:/static/models") / "m1" / "config.json"
        )

    @pytest.mark.asyncio
    async def test_models_root_falls_back_to_install_dir_models(self) -> None:
        # Neither provider nor static field → install_dir/models.
        svc, cap = _make_adapter(install_dir="C:/svc")
        await svc.start(model_name="m1", port=8000, loglevel=3)
        assert cap.args is not None
        cfg_idx = cap.args.index("-c")
        assert cap.args[cfg_idx + 1] == str(
            Path("C:/svc") / "models" / "m1" / "config.json"
        )

    @pytest.mark.asyncio
    async def test_models_root_provider_returns_empty_falls_back(
        self,
    ) -> None:
        async def _empty_provider() -> str:
            return ""

        svc, cap = _make_adapter(
            install_dir="C:/svc",
            models_root="C:/static/models",
            models_root_provider=_empty_provider,
        )
        await svc.start(model_name="m1", port=8000, loglevel=3)
        assert cap.args is not None
        cfg_idx = cap.args.index("-c")
        assert cap.args[cfg_idx + 1] == str(
            Path("C:/static/models") / "m1" / "config.json"
        )


# ---------------------------------------------------------------------------
# _sync_service_config_model — V1 main.py:5214-5248 parity
# ---------------------------------------------------------------------------


class TestSyncServiceConfigModel:
    def test_writes_first_enabled_npu_slot_and_default_model(
        self, tmp_path: Path
    ) -> None:
        cfg_path = tmp_path / "service_config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "default_model": "old-model",
                    "models": [
                        {
                            "enabled": True,
                            "backend": "qnn",
                            "name": "old-model",
                            "path": "old-model",
                        },
                        {
                            "enabled": True,
                            "backend": "cpu",
                            "name": "ignore-me",
                            "path": "ignore-me",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        svc = ProcessBackedInferenceService(install_dir=str(tmp_path))
        svc._sync_service_config_model("new-model")

        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["default_model"] == "new-model"
        assert data["models"][0]["name"] == "new-model"
        assert data["models"][0]["path"] == "new-model"  # V1: path == name
        # Non-NPU slot must be untouched.
        assert data["models"][1]["name"] == "ignore-me"

    def test_picks_first_enabled_with_empty_backend(
        self, tmp_path: Path
    ) -> None:
        # V1: backend in {"qnn", ""} both qualify as the NPU slot.
        cfg_path = tmp_path / "service_config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "default_model": "old",
                    "models": [
                        {
                            "enabled": False,
                            "backend": "qnn",
                            "name": "disabled",
                            "path": "disabled",
                        },
                        {
                            "enabled": True,
                            "backend": "",
                            "name": "old",
                            "path": "old",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        svc = ProcessBackedInferenceService(install_dir=str(tmp_path))
        svc._sync_service_config_model("fresh")

        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["models"][0]["name"] == "disabled"  # untouched
        assert data["models"][1]["name"] == "fresh"
        assert data["default_model"] == "fresh"

    def test_handles_missing_file_silently(self, tmp_path: Path) -> None:
        # No service_config.json on disk → no-op, no exception.
        svc = ProcessBackedInferenceService(install_dir=str(tmp_path))
        svc._sync_service_config_model("any-model")  # must not raise
        assert not (tmp_path / "service_config.json").exists()

    def test_handles_missing_install_dir_silently(self) -> None:
        svc = ProcessBackedInferenceService(install_dir="")
        svc._sync_service_config_model("any-model")  # must not raise

    def test_handles_empty_model_name_silently(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "service_config.json"
        original = json.dumps({"default_model": "keep"})
        cfg_path.write_text(original, encoding="utf-8")

        svc = ProcessBackedInferenceService(install_dir=str(tmp_path))
        svc._sync_service_config_model("")  # must not modify anything

        assert cfg_path.read_text(encoding="utf-8") == original

    def test_skips_write_when_already_in_sync(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "service_config.json"
        original = json.dumps(
            {
                "default_model": "current",
                "models": [
                    {
                        "enabled": True,
                        "backend": "qnn",
                        "name": "current",
                        "path": "current",
                    }
                ],
            }
        )
        cfg_path.write_text(original, encoding="utf-8")
        mtime_before = cfg_path.stat().st_mtime_ns

        svc = ProcessBackedInferenceService(install_dir=str(tmp_path))
        svc._sync_service_config_model("current")

        # Already in sync → no rewrite (V1 ``changed`` flag short-circuits).
        assert cfg_path.stat().st_mtime_ns == mtime_before

    def test_handles_corrupt_json_non_fatally(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "service_config.json"
        cfg_path.write_text("{ this is not json", encoding="utf-8")

        svc = ProcessBackedInferenceService(install_dir=str(tmp_path))
        svc._sync_service_config_model("anything")  # must not raise

        # Corrupt input must not be rewritten — V1 reads-only on parse error.
        assert cfg_path.read_text(encoding="utf-8") == "{ this is not json"


# ---------------------------------------------------------------------------
# Sync integration: start() invokes _sync_service_config_model
# ---------------------------------------------------------------------------


class TestStartTriggersSync:
    @pytest.mark.asyncio
    async def test_start_with_model_calls_sync(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "service_config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "default_model": "old",
                    "models": [
                        {
                            "enabled": True,
                            "backend": "qnn",
                            "name": "old",
                            "path": "old",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        svc = ProcessBackedInferenceService(install_dir=str(tmp_path))
        capture = _ArgvCapture()
        svc._do_start = capture  # type: ignore[assignment]

        await svc.start(model_name="brand-new", port=8000, loglevel=3)

        data = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert data["default_model"] == "brand-new"
        assert data["models"][0]["name"] == "brand-new"

    @pytest.mark.asyncio
    async def test_start_without_model_skips_sync(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "service_config.json"
        original = json.dumps(
            {
                "default_model": "untouched",
                "models": [
                    {
                        "enabled": True,
                        "backend": "qnn",
                        "name": "untouched",
                        "path": "untouched",
                    }
                ],
            }
        )
        cfg_path.write_text(original, encoding="utf-8")

        svc = ProcessBackedInferenceService(install_dir=str(tmp_path))
        capture = _ArgvCapture()
        svc._do_start = capture  # type: ignore[assignment]

        await svc.start(port=8000, loglevel=3)  # no model_name

        assert cfg_path.read_text(encoding="utf-8") == original


# ---------------------------------------------------------------------------
# load_model starts the daemon when stopped (audit A1, V1 parity)
# ---------------------------------------------------------------------------


class TestLoadModelStartsServiceWhenStopped:
    """V1 `_do_load_local_model` (main.py:5300-5400): /api/service/load-model
    STARTS the GenieAPIService with the requested model when it isn't running
    (the daemon loads the model from its -c config at boot), instead of
    erroring. This is the chat dropdown / `/model` auto-load path (#4)."""

    @pytest.mark.asyncio
    async def test_load_model_when_stopped_starts_daemon_with_model(self) -> None:
        svc, capture = _make_adapter(install_dir="C:/svc", default_port=8910)
        # Service starts STOPPED (no _do_start called yet).
        await svc.load_model("qwen3-8b-8380")
        # Daemon was spawned (V1 starts the service) — not a 500.
        assert capture.args is not None
        assert capture.model_name == "qwen3-8b-8380"
        # argv pins the model's config via -c (V1 -c <model>/config.json).
        joined = " ".join(capture.args)
        assert "qwen3-8b-8380" in joined
        assert "-c" in capture.args

    @pytest.mark.asyncio
    async def test_load_model_when_stopped_does_not_raise(self) -> None:
        svc, _ = _make_adapter()
        # Must not raise RuntimeError("Service is not running") anymore.
        await svc.load_model("m")  # no exception == pass


    @pytest.mark.asyncio
    async def test_load_model_same_model_is_noop_when_running(self) -> None:
        """Fast path: if the daemon is already running the requested model,
        load_model must NOT stop/restart it (no-op)."""
        svc, capture = _make_adapter(install_dir="C:/svc", default_port=8910)
        # Simulate a running daemon holding 'qwen3-8b'.
        from qai.model_runtime.domain.enums import ServiceState

        svc._state = ServiceState.RUNNING
        svc._port = 8910
        svc._loaded_model = "qwen3-8b"
        # Track stop calls.
        stop_called: list[bool] = []
        original_stop = svc.stop

        async def _record_stop() -> None:
            stop_called.append(True)
            await original_stop()

        svc.stop = _record_stop  # type: ignore[assignment]
        await svc.load_model("qwen3-8b")
        assert stop_called == [], "stop() must NOT be called when model unchanged"
        assert capture.args is None, "_do_start must NOT be called for same model"

    @pytest.mark.asyncio
    async def test_load_model_different_model_stops_then_starts(self) -> None:
        """V1 parity: switching to a different model when the daemon is already
        running must stop the current daemon first, then start a new one with
        the requested model.  The old /load_model HTTP POST (which returned
        404 Not Found) must NOT be attempted."""
        svc, capture = _make_adapter(install_dir="C:/svc", default_port=8910)
        from qai.model_runtime.domain.enums import ServiceState

        svc._state = ServiceState.RUNNING
        svc._port = 8910
        svc._loaded_model = "llama3-8b"
        stop_called: list[bool] = []

        async def _fake_stop() -> None:
            stop_called.append(True)
            # Simulate the daemon going down so start() proceeds.
            svc._state = ServiceState.STOPPED
            svc._port = None

        svc.stop = _fake_stop  # type: ignore[assignment]
        await svc.load_model("qwen3-8b")
        assert stop_called == [True], "stop() must be called before switching model"
        assert capture.args is not None, "_do_start must be called with the new model"
        assert capture.model_name == "qwen3-8b"
        joined = " ".join(capture.args)
        assert "qwen3-8b" in joined


# ---------------------------------------------------------------------------
# V1-parity status/start semantics (corrects an earlier wrong direction):
# the Service panel's status is SINGLE-SOURCE = the Popen handle. It does NOT
# HTTP-probe /v1/models (that probe is V1's Chat-model-list concern, a
# separate link). start() always spawns (never "adopts" a port) so it always
# owns stdout → has logs / pid / uptime. exe_path is resolved live every call
# (installed-ness is a disk fact, independent of running). See the V1↔V2
# Service deep-dive (2026-06-10) and AGENTS.md "真实状态优先".
# ---------------------------------------------------------------------------
class TestV1ParityStatusAndStart:
    @pytest.mark.asyncio
    async def test_status_running_is_pure_handle_no_http_probe(self) -> None:
        """status() must NOT call _probe_address — running is handle-only.

        Mixing an HTTP probe in caused Running↔Stopped flicker during the
        daemon's model-load window. We assert the probe is never consulted by
        making it raise if called.
        """
        svc, _ = _make_adapter(default_port=9999)

        async def _boom(host: str, port: int) -> dict[str, Any]:
            raise AssertionError(
                "status() must not HTTP-probe — running is the Popen handle only"
            )

        svc._probe_address = _boom  # type: ignore[assignment]
        # No spawn → no handle → stopped, deterministically (no probe flicker).
        st = await svc.status()
        assert st["running"] is False
        assert st["pid"] is None

    @pytest.mark.asyncio
    async def test_status_resolves_exe_path_live_regardless_of_running(
        self,
    ) -> None:
        """exe_path ("installed?") is a disk fact, resolved live every call —
        never '' just because the service is stopped (would falsely show
        "not installed"). V1 parity: _build_service_exe_path is unconditional.
        """
        svc, _ = _make_adapter(default_port=9999)

        async def _fake_live() -> str:
            return "C:/svc/GenieAPIService.exe"

        svc._resolve_exe_path_live = _fake_live  # type: ignore[assignment]
        st = await svc.status()  # stopped, but binary on disk
        assert st["running"] is False
        assert st["exe_path"] == "C:/svc/GenieAPIService.exe"

    @pytest.mark.asyncio
    async def test_start_spawns_when_port_free(self) -> None:
        """When the port is FREE, start() always goes through _do_start
        (spawn) so it owns the child's stdout PIPE → logs/pid/uptime work.
        It must NOT "adopt" a port-only daemon (that path produced no logs).
        """
        svc, capture = _make_adapter(default_port=9999)
        # Port free → single-instance guard passes.
        svc._is_port_in_use = lambda port, host="127.0.0.1": False  # type: ignore[assignment]
        # The HTTP /v1/models probe must NOT be consulted by start at all.
        async def _boom(host: str, port: int) -> dict[str, Any]:
            raise AssertionError("start() must not HTTP-probe / adopt — always spawn")

        svc._probe_address = _boom  # type: ignore[assignment]
        await svc.start(model_name="qwen3-8b-8380", port=9999)
        assert capture.args is not None  # _do_start (spawn) WAS called
        assert capture.port == 9999

    @pytest.mark.asyncio
    async def test_start_refuses_when_port_in_use(self) -> None:
        """Single-instance guard: if the target port is already occupied,
        start() must NOT spawn a competing daemon — it raises
        ServicePortInUseError so the UI can show a friendly message.
        """
        from qai.model_runtime.domain.errors import ServicePortInUseError

        svc, capture = _make_adapter(default_port=9999)
        # Port occupied → guard trips.
        svc._is_port_in_use = lambda port, host="127.0.0.1": True  # type: ignore[assignment]
        with pytest.raises(ServicePortInUseError) as ei:
            await svc.start(model_name="qwen3-8b-8380", port=9999)
        assert ei.value.port == 9999
        # Must NOT have spawned anything.
        assert capture.args is None

