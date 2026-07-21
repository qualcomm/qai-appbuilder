# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Cross-context tool bridge: wire App Builder inference into chat.

Registers the two conditional App Builder tools — ``appbuilder_run``
(single inference) and ``appbuilder_batch_run`` (batch, NPU-serialized) —
onto the chat-side :class:`RegistryBackedToolInvocation` registry so an
LLM-emitted ``tool_call`` in **app-builder** mode resolves to a REAL
on-device inference instead of the ``appbuilder_not_wired`` stub
(``qai.ai_coding.infrastructure.tools.handlers.appbuilder``).

V1 parity
---------
This restores the two LLM Agent Pipeline tools the V1 backend shipped
(``backend/tools/_appbuilder_run.py`` + ``backend/tools/_appbuilder_batch_run.py``)
that were lost in the V2 cutover (audit ``D6 §6.11`` CRITICAL GAP /
``§6.12`` HIGH GAP).  The schemas are byte-for-byte the V1 rich schemas;
the handlers reproduce V1 behaviour (path PolicyCenter gate, per-``outputSchema``
formatting, batch loop with ``stopOnError`` + ``_MAX_BATCH_SIZE`` + total
timeout cap).

Why a separate apps/api bridge
------------------------------
``qai.chat`` and ``qai.ai_coding`` must NOT import ``qai.app_builder``
(``context-isolation`` import-linter contract).  ``apps/api/`` is the only
layer permitted to compose two contexts, so this bridge owns the join — it
wraps the app_builder :class:`RunAppUseCase` (already wired in
``apps/api/_app_builder_di.py``) and the ai_coding :class:`FileGuardPort`
(the SAME PolicyCenter-backed gate the file tools use) into chat-shaped
``async (ToolInvocationRequest) -> str`` handlers, following the exact
pattern of :mod:`apps.api._chat_tool_bridge`.

Architecture (reuse > rebuild, AGENTS.md 判据 1)
-----------------------------------------------
* **No domain change.**  ``RunAppUseCase.execute(model_id, inputs)`` already
  carries per-call ``variant`` / ``params`` through the established
  ``inputs["variant_id"]`` / ``inputs["params"]`` packing convention (see
  ``run_app._extract_variant_id`` / ``_extract_params`` +
  ``command_resolver.registry._split_run_inputs``).  The bridge packs the
  tool's ``variantId`` / ``params`` arguments into that same shape, so the
  full V1 capability works WITHOUT extending the ``Run`` aggregate, the
  ``RunnerPort``, or any runner adapter.
* **No duplicated persistence.**  ``RunAppUseCase`` already persists each run
  via ``RunRepositoryPort`` and drives the ``Run`` state machine, so the
  bridge does NOT reproduce V1's ``_persist_run_to_history`` (that V1 helper
  existed only because the V1 tool bypassed the HTTP run path).
* **NPU serialization** is owned by the runner / sticky-worker inside
  app_builder (V1's ``_npu_lock``); the batch handler awaits each item
  sequentially so it never fans out concurrent NPU work.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from qai.chat.adapters import RegistryBackedToolInvocation
from qai.chat.application.ports import ToolInvocationRequest
from qai.platform.logging import get_logger

__all__ = [
    "APPBUILDER_RUN_SCHEMA",
    "APPBUILDER_BATCH_RUN_SCHEMA",
    "register_appbuilder_tools_into_chat",
]

_log = get_logger(__name__)

# Per-item inference timeout (s) — parity with V1
# ``_appbuilder_batch_run._PER_ITEM_TIMEOUT_S`` and the runner's own
# 300s wall-clock cap.
_PER_ITEM_TIMEOUT_S = 300.0
# Total batch timeout ceiling (s) = 30 min (V1 ``_MAX_TOTAL_TIMEOUT_S``).
_MAX_TOTAL_TIMEOUT_S = 30 * 60.0
# Max items per batch call (V1 ``_MAX_BATCH_SIZE``).
_MAX_BATCH_SIZE = 20

# File extensions that mark an inputs value as a path (V1 parity:
# ``_appbuilder_run._looks_like_path``). A value that "looks like a path"
# is routed through the FileGuard PolicyCenter gate before it reaches the
# runner; everything else (e.g. a TTS ``text`` string) passes through
# verbatim.
_PATH_EXTENSIONS = frozenset(
    {
        "png", "jpg", "jpeg", "webp", "gif", "bmp", "tiff",
        "wav", "mp3", "flac", "webm", "ogg", "m4a",
        "txt", "json", "csv", "md", "bin",
    }
)


# ---------------------------------------------------------------------------
# Schemas (V1 parity — the rich descriptions from
# ``backend/tools/_appbuilder_run.py`` / ``_appbuilder_batch_run.py``).
# ---------------------------------------------------------------------------

APPBUILDER_RUN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "appbuilder_run",
        "description": (
            "Run an App Builder Model Pack for on-device AI inference "
            "(NPU/CPU via QNN). Supports image super-resolution, OCR, ASR, "
            "TTS, image classification, etc. Input/output types depend on "
            "the model's schema. All inference runs locally on the user's "
            "device — no data is uploaded. NPU runs are serialized (one at a "
            "time). Use this tool when the user asks to process images, "
            "audio, or text with on-device AI models listed in the system "
            "prompt."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "modelId": {
                    "type": "string",
                    "description": (
                        "The model Pack ID to run. Must be one of the "
                        "available models listed in the system prompt. "
                        "Examples: 'real-esrgan-x4plus', 'ppocrv4', "
                        "'inception-v3', 'whisper-base', 'zipformer-zh', "
                        "'melotts-zh'."
                    ),
                },
                "inputs": {
                    "type": "object",
                    "description": (
                        "Input data object. Keys depend on the model's "
                        "inputSchema.kind:\n"
                        "- Image models: {\"image\": \"<path>\"}\n"
                        "- Audio models: {\"audio\": \"<path>\"}\n"
                        "- Text models: {\"text\": \"<content_string>\"}\n"
                        "Paths can be relative (e.g. 'data/uploads/images/"
                        "xxx.png') or absolute (e.g. 'C:/photos/x.jpg')."
                    ),
                    "additionalProperties": True,
                },
                "params": {
                    "type": "object",
                    "description": (
                        "Optional model parameters. If omitted, model "
                        "defaults are used. Common examples:\n"
                        "- SR: {\"scale\": 4, \"tile_size\": 256}\n"
                        "- OCR: {\"language\": \"auto\"}\n"
                        "- ASR: {\"language\": \"auto\", \"task\": "
                        "\"transcribe\"}\n"
                        "- TTS: {\"voice\": \"female-1\", \"speed\": 1.0}\n"
                        "See the model param list in the system prompt for "
                        "valid options."
                    ),
                    "additionalProperties": True,
                },
                "variantId": {
                    "type": "string",
                    "description": (
                        "Optional. For multi-variant Packs, select a specific "
                        "quantization precision (e.g. 'fp16', 'int8', "
                        "'w8a16'). If omitted, the Pack's default variant is "
                        "used."
                    ),
                },
            },
            "required": ["modelId", "inputs"],
        },
    },
}


APPBUILDER_BATCH_RUN_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "appbuilder_batch_run",
        "description": (
            "Run an App Builder Model Pack on multiple inputs in a single "
            "call. Internally serializes on NPU (one inference at a time). "
            "Use this instead of calling appbuilder_run repeatedly when "
            "processing multiple files (\u2264 20) with the same model — it "
            "saves tool-call round-trips and tokens. Returns aggregated, "
            "per-item results."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "modelId": {
                    "type": "string",
                    "description": (
                        "The model Pack ID to run. Must be one of the "
                        "available models listed in the system prompt. All "
                        "items in the batch share the same model."
                    ),
                },
                "batch": {
                    "type": "array",
                    "description": (
                        "Array of inputs to process. Each item carries its "
                        "own `inputs` dict (and optional `params`). Maximum "
                        "20 items."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "inputs": {
                                "type": "object",
                                "description": (
                                    "Per-item input data. Same shape as "
                                    "`appbuilder_run.inputs` — keys depend on "
                                    "model.inputSchema.kind (image/audio/"
                                    "text)."
                                ),
                                "additionalProperties": True,
                            },
                            "params": {
                                "type": "object",
                                "description": (
                                    "Optional per-item params. If omitted, "
                                    "model defaults are used. Same shape as "
                                    "`appbuilder_run.params`."
                                ),
                                "additionalProperties": True,
                            },
                        },
                        "required": ["inputs"],
                    },
                    "maxItems": _MAX_BATCH_SIZE,
                },
                "variantId": {
                    "type": "string",
                    "description": (
                        "Optional. For multi-variant Packs, select a specific "
                        "quantization precision (e.g. 'fp16', 'int8'). Applied "
                        "to every item in the batch."
                    ),
                },
                "stopOnError": {
                    "type": "boolean",
                    "description": (
                        "If true, stop the batch on the first failed item. "
                        "Default false (continue and aggregate per-item "
                        "errors)."
                    ),
                    "default": False,
                },
            },
            "required": ["modelId", "batch"],
        },
    },
}


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_appbuilder_tools_into_chat(
    *,
    tools: RegistryBackedToolInvocation,
    run_app_use_case: Any,
    app_model_lookup: Callable[[], Awaitable[Mapping[str, Any]]] | None = None,
    file_guard: Any | None = None,
) -> tuple[str, ...]:
    """Register ``appbuilder_run`` / ``appbuilder_batch_run`` on the chat registry.

    Idempotent overwrite (``RegistryBackedToolInvocation.register``): this
    SUPERSEDES the ai_coding ``appbuilder_run`` stub previously registered by
    :func:`apps.api._chat_tool_bridge.register_ai_coding_tools_into_chat`, and
    adds the batch tool the stub set never had.

    Parameters
    ----------
    tools:
        The chat-side tool registry.
    run_app_use_case:
        ``container.app_builder.run_app_use_case`` — the app_builder use case
        that runs one inference and yields ``RunFrame`` chunks.
    app_model_lookup:
        Optional zero-arg async callable returning a ``{model_id: title}``
        mapping of the currently-available (enabled) app models, used only to
        produce a friendly "available models" hint on an unknown ``modelId``.
        ``None`` degrades the hint to "(see the system prompt)".
    file_guard:
        Optional ai_coding :class:`FileGuardPort`. When wired, inputs values
        that look like a path are routed through ``enforce_project_access`` +
        ``enforce_read`` (the SAME PolicyCenter gate the file tools use), so an
        out-of-policy path is rejected / prompts the user exactly like ``read``.
        ``None`` skips the gate (paths pass through verbatim — matches a
        deployment with security handled out-of-band).

    Returns
    -------
    tuple[str, ...]
        The names registered (sorted). Empty only if ``tools`` is not the
        registry-backed adapter or ``run_app_use_case`` is falsy.
    """
    if not isinstance(tools, RegistryBackedToolInvocation):
        return ()
    if run_app_use_case is None:
        return ()

    handler = _AppBuilderToolHandler(
        run_app_use_case=run_app_use_case,
        app_model_lookup=app_model_lookup,
        file_guard=file_guard,
    )
    registered: list[str] = []
    try:
        tools.register(
            "appbuilder_run",
            handler.execute_run,
            schema=APPBUILDER_RUN_SCHEMA,
        )
        registered.append("appbuilder_run")
        tools.register(
            "appbuilder_batch_run",
            handler.execute_batch,
            schema=APPBUILDER_BATCH_RUN_SCHEMA,
        )
        registered.append("appbuilder_batch_run")
    except Exception:  # noqa: BLE001 — never block chat startup
        _log.warning("chat.appbuilder_tools.register_failed", exc_info=True)
    return tuple(sorted(registered))


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class _AppBuilderToolHandler:
    """Chat-shaped handlers wrapping the app_builder :class:`RunAppUseCase`."""

    __slots__ = ("_run_app", "_app_model_lookup", "_file_guard")

    def __init__(
        self,
        *,
        run_app_use_case: Any,
        app_model_lookup: Callable[[], Awaitable[Mapping[str, Any]]] | None,
        file_guard: Any | None,
    ) -> None:
        self._run_app = run_app_use_case
        self._app_model_lookup = app_model_lookup
        self._file_guard = file_guard

    # ----- appbuilder_run -------------------------------------------------

    async def execute_run(self, request: ToolInvocationRequest) -> str:
        args = request.arguments or {}
        model_id = str(args.get("modelId") or "").strip()
        inputs = args.get("inputs")
        params = args.get("params") or {}
        variant_id = args.get("variantId")

        if not model_id:
            return "[tool_error] modelId is required."
        if not isinstance(inputs, dict) or not inputs:
            return (
                "[tool_error] inputs is required and must be a non-empty "
                "object (e.g. {\"image\": \"data/uploads/...\"})."
            )
        if params and not isinstance(params, dict):
            return "[tool_error] params must be an object if provided."

        try:
            validated_inputs = await self._validate_inputs(inputs)
        except _InputRejected as exc:
            return f"[tool_error] {exc}"

        return await self._run_single(
            model_id=model_id,
            inputs=validated_inputs,
            params=params if isinstance(params, dict) else {},
            variant_id=variant_id if isinstance(variant_id, str) else None,
        )

    # ----- appbuilder_batch_run -------------------------------------------

    async def execute_batch(self, request: ToolInvocationRequest) -> str:
        args = request.arguments or {}
        model_id = str(args.get("modelId") or "").strip()
        batch = args.get("batch")
        variant_id = args.get("variantId")
        stop_on_error = bool(args.get("stopOnError", False))

        if not model_id:
            return "[tool_error] modelId is required."
        if not isinstance(batch, list) or not batch:
            return "[tool_error] batch must be a non-empty array."
        if len(batch) > _MAX_BATCH_SIZE:
            return (
                f"[tool_error] batch size {len(batch)} exceeds max "
                f"{_MAX_BATCH_SIZE}. Please split into multiple calls."
            )

        # Validate every item up front (V1 parity: reject the whole call on a
        # malformed / out-of-policy item rather than half-running it).
        validated_batch: list[dict[str, Any]] = []
        for i, item in enumerate(batch):
            if not isinstance(item, dict):
                return f"[tool_error] batch[{i}] must be an object."
            item_inputs = item.get("inputs")
            if not isinstance(item_inputs, dict) or not item_inputs:
                return (
                    f"[tool_error] batch[{i}].inputs is required and must be "
                    "a non-empty object."
                )
            item_params = item.get("params") or {}
            if not isinstance(item_params, dict):
                return (
                    f"[tool_error] batch[{i}].params must be an object if "
                    "provided."
                )
            try:
                validated_inputs = await self._validate_inputs(item_inputs)
            except _InputRejected as exc:
                return f"[tool_error] batch[{i}].inputs rejected: {exc}"
            validated_batch.append(
                {"inputs": validated_inputs, "params": item_params}
            )

        normalized_variant = (
            variant_id if isinstance(variant_id, str) else None
        )

        # Total wall-clock ceiling (V1 ``_MAX_TOTAL_TIMEOUT_S`` cap).
        timeout_total = min(
            _PER_ITEM_TIMEOUT_S * len(validated_batch),
            _MAX_TOTAL_TIMEOUT_S,
        )
        try:
            return await asyncio.wait_for(
                self._run_batch(
                    model_id=model_id,
                    batch=validated_batch,
                    variant_id=normalized_variant,
                    stop_on_error=stop_on_error,
                ),
                timeout=timeout_total,
            )
        except (asyncio.TimeoutError, TimeoutError):
            _log.warning(
                "chat.appbuilder_batch_run.timeout",
                model_id=model_id,
                items=len(validated_batch),
            )
            return (
                f"[tool_error] Batch inference timed out for model "
                f"'{model_id}' (exceeded {int(timeout_total)}s)."
            )

    # ----- internals ------------------------------------------------------

    async def _run_batch(
        self,
        *,
        model_id: str,
        batch: list[dict[str, Any]],
        variant_id: str | None,
        stop_on_error: bool,
    ) -> str:
        results: list[tuple[int, dict[str, Any], str]] = []
        success_count = 0
        error_count = 0
        aborted_early = False
        start = time.monotonic()

        for i, item in enumerate(batch):
            single = await self._run_single(
                model_id=model_id,
                inputs=item["inputs"],
                params=item["params"],
                variant_id=variant_id,
            )
            failed = single.startswith("[appbuilder_run error]") or (
                single.startswith("[tool_error]")
            )
            if failed:
                error_count += 1
            else:
                success_count += 1
            results.append((i, item["inputs"], single))
            if failed and stop_on_error:
                aborted_early = True
                break

        elapsed_s = time.monotonic() - start
        processed = len(results)
        skipped = len(batch) - processed

        lines = [
            "[appbuilder_batch_run result]",
            f"model: `{model_id}`",
            (
                f"batch size: {len(batch)} | succeeded: {success_count} | "
                f"failed: {error_count}"
                + (f" | skipped: {skipped}" if skipped else "")
            ),
            f"total time: {elapsed_s:.1f}s",
        ]
        if aborted_early:
            lines.append(
                f"note: stopOnError=true — stopped after item "
                f"{results[-1][0] + 1} failed; {skipped} item(s) not run."
            )
        lines.append("")

        for idx, item_inputs, result_text in results:
            label = _short_label(item_inputs, idx)
            lines.append(f"--- item {idx + 1} ({label}) ---")
            lines.append(result_text)
            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    async def _run_single(
        self,
        *,
        model_id: str,
        inputs: dict[str, Any],
        params: dict[str, Any],
        variant_id: str | None,
    ) -> str:
        """Drive one ``RunAppUseCase`` run and project frames to text.

        Reuses the established ``Run.inputs`` packing convention so per-call
        ``variant`` / ``params`` reach the runner WITHOUT any domain change
        (see ``run_app._extract_variant_id`` / ``_extract_params`` +
        ``command_resolver.registry._split_run_inputs``).
        """
        try:
            from qai.app_builder.domain.value_objects import AppModelId
        except Exception:  # noqa: BLE001 — app_builder unavailable
            return (
                "[tool_error] App Builder is not available in this build; "
                "cannot run on-device inference."
            )

        try:
            app_model_id = AppModelId(value=model_id)
        except ValueError:
            return (
                f"[tool_error] Invalid modelId '{model_id}'. Use one of the "
                f"models listed in the system prompt."
            )

        # Pack variant_id / params into Run.inputs per the use-case convention.
        packed_inputs: dict[str, Any] = dict(inputs)
        if params:
            packed_inputs["params"] = dict(params)
        if variant_id:
            packed_inputs["variant_id"] = variant_id

        events: list[dict[str, Any]] = []
        start = time.monotonic()
        iterator = None
        try:
            iterator = self._run_app.execute(
                model_id=app_model_id, inputs=packed_inputs
            )
            async for frame in iterator:
                payload = getattr(frame, "payload", None)
                if isinstance(payload, dict):
                    events.append(payload)
        except Exception as exc:  # noqa: BLE001 — surface a stable error
            code = _domain_error_code(exc)
            if code == "not_found":
                return await self._unknown_model_message(model_id)
            if code == "disabled":
                return (
                    f"[tool_error] Model '{model_id}' is disabled and cannot "
                    "be run."
                )
            _log.warning(
                "chat.appbuilder_run.failed",
                model_id=model_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return f"[tool_error] Inference failed: {exc}"
        finally:
            # ``RunAppUseCase.execute`` documents that callers MUST iterate to
            # completion OR close the iterator; otherwise the Run is left in
            # ``RUNNING``. ``aclose()`` lets the use case's own
            # ``except CancelledError`` / ``finally`` reconcile the run state
            # on an early exit (exception mid-stream, or an outer
            # ``CancelledError`` when the chat turn is stopped). Closing an
            # already-exhausted generator is a harmless no-op.
            aclose = getattr(iterator, "aclose", None)
            if callable(aclose):
                try:
                    await aclose()
                except Exception:  # noqa: BLE001 — cleanup must never raise out
                    pass

        elapsed_ms = int((time.monotonic() - start) * 1000)
        return _format_run_result(model_id, events, elapsed_ms)

    async def _validate_inputs(
        self, inputs: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Route path-like inputs values through the FileGuard gate (V1 parity).

        Non-path values (e.g. a TTS ``text`` string) pass through verbatim.
        Raises :class:`_InputRejected` when the FileGuard denies a path.
        """
        guard = self._file_guard
        out: dict[str, Any] = {}
        for key, value in inputs.items():
            if (
                guard is not None
                and isinstance(value, str)
                and _looks_like_path(value)
            ):
                try:
                    await guard.enforce_project_access(
                        path=value, operation="read"
                    )
                    await guard.enforce_read(
                        path=value,
                        caller="chat.appbuilder_run",
                    )
                except Exception as exc:  # noqa: BLE001 — ToolGuardDenied etc.
                    raise _InputRejected(
                        f"input path rejected: {value} ({exc})"
                    ) from exc
            out[key] = value
        return out

    async def _unknown_model_message(self, model_id: str) -> str:
        available = "(see the system prompt)"
        if self._app_model_lookup is not None:
            try:
                mapping = await self._app_model_lookup()
                ids = sorted(mapping.keys())
                if ids:
                    available = ", ".join(ids)
                else:
                    available = "(none installed)"
            except Exception:  # noqa: BLE001 — best-effort hint only
                pass
        return (
            f"[tool_error] Model '{model_id}' not found or not enabled.\n"
            f"Available models: {available}"
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


class _InputRejected(Exception):
    """Raised internally when a FileGuard denies an inputs path."""


def _looks_like_path(value: str) -> bool:
    """V1 parity ``_appbuilder_run._looks_like_path``."""
    if not value or len(value) > 1024:
        return False
    if "/" in value or "\\" in value:
        return True
    if "." in value and value.rsplit(".", 1)[-1].lower() in _PATH_EXTENSIONS:
        return True
    return False


def _domain_error_code(exc: BaseException) -> str:
    """Classify an app_builder domain error by class name (no import needed).

    Avoids importing ``qai.app_builder.domain.errors`` at module import time
    (keeps the bridge import-light); matches on the well-known class names the
    use case raises: ``AppModelNotFoundError`` / ``AppModelDisabledError``.
    """
    name = type(exc).__name__
    if name == "AppModelNotFoundError":
        return "not_found"
    if name == "AppModelDisabledError":
        return "disabled"
    return "other"


def _short_label(item_inputs: Mapping[str, Any], idx: int) -> str:
    """Pick a short identifying label from an item's inputs (V1 parity)."""
    for k, v in item_inputs.items():
        if isinstance(v, str) and len(v) < 80:
            return f"{k}={v}"
    return f"item_{idx}"


def _format_run_result(
    model_id: str, events: list[dict[str, Any]], elapsed_ms: int
) -> str:
    """Project a run's frames into V1-style LLM-readable text.

    Frame ``payload`` shape is the runner-protocol event dict surfaced
    verbatim by ``RunAppUseCase`` (``payload["event"]`` discriminator):

    * ``error``  → ``{event:"error", code, message}``
    * ``result`` → ``{event:"result", output:{...}}`` (exactly one per run)
    * ``metrics``→ ``{event:"metrics", latencyMs, device, memoryMB}``
    """
    error_ev = next(
        (e for e in events if e.get("event") == "error"), None
    )
    result_ev = next(
        (e for e in events if e.get("event") == "result"), None
    )
    metrics_ev = next(
        (e for e in events if e.get("event") == "metrics"), None
    )

    if error_ev is not None:
        code = error_ev.get("code", "UNKNOWN")
        msg = error_ev.get("message", "Unknown error")
        return (
            "[appbuilder_run error]\n"
            f"model: {model_id}\n"
            f"error code: {code}\n"
            f"detail: {msg}\n"
            f"elapsed: {elapsed_ms}ms"
        )

    lines = [
        "[appbuilder_run result]",
        f"model: `{model_id}`",
        "status: success",
    ]
    metrics_parts = [f"elapsed {elapsed_ms}ms"]
    if isinstance(metrics_ev, dict):
        latency = metrics_ev.get("latencyMs")
        device = metrics_ev.get("device")
        memory = metrics_ev.get("memoryMB")
        if isinstance(latency, (int, float)) and not isinstance(latency, bool):
            metrics_parts[0] = (
                f"inference latency {latency}ms (total {elapsed_ms}ms)"
            )
        if isinstance(device, str) and device:
            metrics_parts.append(f"device {device}")
        if isinstance(memory, (int, float)) and not isinstance(memory, bool):
            metrics_parts.append(f"memory {memory}MB")
    lines.append(" | ".join(metrics_parts))
    lines.append("")

    if isinstance(result_ev, dict):
        output = result_ev.get("output", {})
        lines.append(_format_output(output))
    else:
        lines.append("(no result output)")
    return "\n".join(lines)


def _format_output(output: Any) -> str:
    """Format a result ``output`` mapping by its kind (V1 parity).

    Mirrors ``backend/tools/_appbuilder_run._format_output``: detect the
    output kind from well-known keys (image_path / audio_path / lines /
    segments / predictions) and render LLM-readable text; otherwise dump
    JSON (truncated). An output path is surfaced so the model can feed it
    into a follow-up ``appbuilder_run`` call (multi-step pipeline).
    """
    if not output:
        return "(empty output)"
    if not isinstance(output, dict):
        text = json.dumps(output, ensure_ascii=False, default=str)
        if len(text) > 1000:
            text = text[:1000] + "\n... (truncated)"
        return f"(non-standard output):\n{text}"

    parts: list[str] = []

    if "image_path" in output:
        path_str = str(output["image_path"]).replace("\\", "/")
        parts.append(f"output image: {path_str}")
        parts.append(
            f"  (reuse this path as the next appbuilder_run's "
            f"inputs.image, e.g. inputs={{\"image\": \"{path_str}\"}})"
        )
        if "in_size" in output and "out_size" in output:
            in_s = "x".join(str(x) for x in output["in_size"])
            out_s = "x".join(str(x) for x in output["out_size"])
            parts.append(f"size: {in_s} -> {out_s}")
        if "scale" in output:
            parts.append(f"scale: {output['scale']}x")
        return "\n".join(parts)

    if "audio_path" in output:
        path_str = str(output["audio_path"]).replace("\\", "/")
        parts.append(f"output audio: {path_str}")
        parts.append(
            f"  (reuse this path as the next appbuilder_run's "
            f"inputs.audio, e.g. inputs={{\"audio\": \"{path_str}\"}})"
        )
        if "duration_s" in output:
            parts.append(f"duration: {output['duration_s']}s")
        if "sample_rate" in output:
            parts.append(f"sample rate: {output['sample_rate']}Hz")
        return "\n".join(parts)

    if isinstance(output.get("lines"), list):
        ocr_lines = output["lines"]
        parts.append(f"recognized: {len(ocr_lines)} line(s) of text")
        full_text = output.get("fullText")
        if isinstance(full_text, str):
            if len(full_text) > 500:
                parts.append(f"full text (first 500 chars):\n{full_text[:500]}…")
            else:
                parts.append(f"full text:\n{full_text}")
        else:
            for i, line in enumerate(ocr_lines[:10]):
                if isinstance(line, dict):
                    text = line.get("text", "")
                    conf = line.get("conf", 0)
                    try:
                        parts.append(f"  [{i + 1}] {text} (conf={float(conf):.2f})")
                    except (TypeError, ValueError):
                        parts.append(f"  [{i + 1}] {text}")
            if len(ocr_lines) > 10:
                parts.append(f"  ... {len(ocr_lines) - 10} more line(s)")
        if "lang_detected" in output:
            parts.append(f"detected language: {output['lang_detected']}")
        return "\n".join(parts)

    if isinstance(output.get("segments"), list):
        segs = output["segments"]
        parts.append(f"transcription: {len(segs)} segment(s)")
        if "language" in output:
            parts.append(f"language: {output['language']}")
        full_text = output.get("fullText")
        if isinstance(full_text, str):
            if len(full_text) > 500:
                parts.append(f"full text (first 500 chars):\n{full_text[:500]}…")
            else:
                parts.append(f"full text:\n{full_text}")
        else:
            for seg in segs[:10]:
                if isinstance(seg, dict):
                    s = seg.get("start", 0)
                    e = seg.get("end", 0)
                    text = seg.get("text", "")
                    try:
                        parts.append(f"  [{float(s):.1f}s-{float(e):.1f}s] {text}")
                    except (TypeError, ValueError):
                        parts.append(f"  {text}")
            if len(segs) > 10:
                parts.append(f"  ... {len(segs) - 10} more segment(s)")
        return "\n".join(parts)

    if isinstance(output.get("predictions"), list):
        parts.append("classification (top-k):")
        for i, pred in enumerate(output["predictions"][:10]):
            if isinstance(pred, dict):
                label = pred.get("label", pred.get("class", "?"))
                score = pred.get("score", pred.get("confidence", 0))
                try:
                    parts.append(f"  {i + 1}. {label} ({float(score):.4f})")
                except (TypeError, ValueError):
                    parts.append(f"  {i + 1}. {label}")
        return "\n".join(parts)

    text = json.dumps(output, ensure_ascii=False, indent=2)
    if len(text) > 1000:
        text = text[:1000] + "\n... (truncated)"
    parts.append(f"output (JSON):\n{text}")
    return "\n".join(parts)
