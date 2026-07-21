# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Live ``.bin`` I/O contract extraction + zero-tensor smoke test.

Direct port of
``features/model-builder/scripts/qai_pack_export.py:_extract_and_smoke_test_contract``.

Why this exists
---------------

The single most important quality gate in the export pipeline: a
Pack that fails this step is broken and we refuse to ship it. By
contrast a Pack that passes is *guaranteed* to load and infer in
AppBuilder (the runtime path uses the same ``qai_appbuilder`` API).

Failure modes surfaced here (any one is a hard abort):

* ``.bin`` unreadable / wrong runtime version → ``QnnContext.load``
  raises;
* native getter inconsistency (model has 2 inputs but reports 1
  dtype);
* zero-tensor inference itself crashes (op compatibility / runtime
  mismatch).

When the host environment lacks the ``qai_appbuilder`` runtime
(common on dev boxes that author Packs from remote ``.bin`` files),
we raise :class:`MissingQaiAppBuilderError` rather than silently
skipping validation — same hard-abort policy as the legacy script.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from qai.model_builder.domain import (
    MissingQaiAppBuilderError,
    SmokeTestFailedError,
)
from qai.platform.process.tree_kill import best_effort_tree_kill

__all__ = [
    "extract_and_smoke_test_contract",
    "extract_and_smoke_test_contract_subprocess",
]

# Absolute path of the one-shot child entry script that runs the probe
# out-of-process. Ships alongside this module so the parent can always locate
# it by ``__file__`` without a config lookup.
_CHILD_SCRIPT: Path = Path(__file__).resolve().parent / "_smoke_probe_child.py"

# error_code (domain ``.code``) -> exception class, so the parent can rebuild
# the exact typed exception the in-process path raised. Any unrecognised code
# maps to ``SmokeTestFailedError`` (the conservative hard-abort default, matching
# how the exporter's non-strict ``except Exception`` folds unknown failures).
_ERROR_CODE_TO_EXC: dict[str, type[Exception]] = {
    MissingQaiAppBuilderError.code: MissingQaiAppBuilderError,
    SmokeTestFailedError.code: SmokeTestFailedError,
}

# Hard wall-clock bound for the one-shot child. A smoke test is a single .bin
# load + one zero-tensor inference — normally seconds, tens of seconds on a
# cold HTP init. If the child exceeds this it has almost certainly hung (native
# deadlock / stuck HTP), and we must not let a hung child block the export
# request forever. On timeout we kill the child's process tree and surface a
# SmokeTestFailedError (AGENTS.md §5 rule 5 — abnormal-exit paths must have a
# fallback). Generous enough to never trip on a legitimately slow first-load.
_SMOKE_CHILD_TIMEOUT_SECONDS: float = 300.0


def extract_and_smoke_test_contract(
    context_bin: Path,
    *,
    shared_dir: Path | None = None,
) -> dict[str, Any]:
    """Load ``context_bin``, query its native I/O contract, run zeros once.

    The returned dict goes into ``manifest.io_contract`` and becomes
    the SSOT for runtime shape / dtype.

    The ``shared_dir`` argument lets DI override where to find
    ``qnn_helper`` + ``io_validator`` (the App Builder shared runner
    helpers). When ``None`` we fall back to the canonical layout
    inherited from the legacy script:
    ``<repo>/features/app-builder/shared/`` — but since this adapter
    runs inside the new architecture, the recommended deployment
    bundles those helpers under ``data/runtime/app_builder_shared/``
    or similar; DI passes the resolved path. When neither is
    available the import simply fails through the
    :class:`MissingQaiAppBuilderError` path.
    """
    # Late import so the export pipeline does not hard-require
    # ``qai_appbuilder`` to be installed on the host that runs the
    # API server. The import error surfaces clearly via our typed
    # domain error rather than as a generic ``ModuleNotFoundError``
    # at module top.
    if shared_dir is not None and shared_dir.is_dir():
        shared_str = str(shared_dir)
        if shared_str not in sys.path:
            sys.path.insert(0, shared_str)

    try:
        from qnn_helper import QnnContext  # type: ignore[import-not-found]
        import io_validator as _iv  # type: ignore[import-not-found]
    except ImportError as exc:
        raise MissingQaiAppBuilderError(
            "qai_appbuilder / shared modules unavailable on this host; "
            "cannot validate .bin. Run the API process inside an "
            "environment where the same qai_appbuilder is installed "
            "that App Builder would use at runtime. "
            f"Original import error: {exc}"
        ) from exc

    try:
        ctx = QnnContext.load(context_bin, runtime="Htp", log_level=1)
    except Exception as exc:  # noqa: BLE001 — qai_appbuilder may raise anything
        raise SmokeTestFailedError(
            f"failed to load context binary via qai_appbuilder: {exc} "
            f"(context_bin={context_bin}). This usually means the .bin "
            "was built against a different QAIRT runtime version, the "
            "file is corrupted, or the target backend (HTP) is not "
            "available on this host."
        ) from exc

    try:
        # The QnnContext wrapper may proxy native getters via its inner
        # ``_ctx`` attribute; otherwise call directly on the wrapper.
        native = getattr(ctx, "_ctx", None) or ctx

        contract = _iv.extract_io_contract(native, validated_at_export=False)

        # Smoke test: invoke once with zero tensors.
        try:
            zeros = _iv.zero_inputs_for_contract(contract)
            _ = ctx.run(zeros)
        except Exception as exc:  # noqa: BLE001
            raise SmokeTestFailedError(
                f"zero-tensor smoke test failed during export: {exc} "
                f"(context_bin={context_bin}). This indicates the .bin "
                "loads but cannot be invoked. The Pack was not produced. "
                "Re-run the conversion pipeline."
            ) from exc

        contract["validated_at_export"] = True
        return contract
    finally:
        try:
            ctx.close()
        except Exception:  # noqa: BLE001 — best-effort close
            pass


# ---------------------------------------------------------------------------
# Out-of-process probe (Plan B1)
# ---------------------------------------------------------------------------
# The in-process :func:`extract_and_smoke_test_contract` above still exists and
# is reused verbatim BY THE CHILD process. The function below is what the
# service process now calls instead: it spawns a one-shot child
# (``_smoke_probe_child.py``) that loads the QNN native library, so native
# ``printf`` / ``cout`` output (``Time: model_inference`` etc.) and any native
# crash stay inside the throwaway child — never the long-lived service's
# stdout / stderr / log, and never taking the service down.


def _rebuild_exception(error_code: str, message: str) -> Exception:
    """Rebuild the typed domain exception from a child envelope's error_code.

    Preserves the exact failure semantics of the in-process path: an
    ``qai_appbuilder_missing`` code becomes :class:`MissingQaiAppBuilderError`,
    everything else (including an unrecognised code) becomes
    :class:`SmokeTestFailedError` — the same hard-abort default the in-process
    ``except Exception`` branch produced.
    """
    exc_cls = _ERROR_CODE_TO_EXC.get(error_code, SmokeTestFailedError)
    return exc_cls(message)


async def extract_and_smoke_test_contract_subprocess(
    context_bin: Path,
    *,
    shared_dir: Path | None = None,
    interpreter_argv: tuple[str, ...] | None = None,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Run :func:`extract_and_smoke_test_contract` in a one-shot child process.

    Semantically equivalent to the in-process function — returns the same
    validated ``io_contract`` dict on success and raises the same typed domain
    exceptions on failure — but the QNN native library is loaded in a throwaway
    subprocess so its fd-level output and any native crash are isolated from the
    service process.

    Parameters
    ----------
    context_bin:
        Absolute path to the ``.bin`` context binary to probe.
    shared_dir:
        Optional dir bundling ``qnn_helper.py`` / ``io_validator.py``; forwarded
        to the child (and prepended to ``PYTHONPATH`` so the child can import
        them). Same meaning as the in-process ``shared_dir``.
    interpreter_argv:
        The child launch prefix, typically ``(str(python_exe),)`` where
        ``python_exe`` is the ARM64 venv interpreter resolved by
        ``select_runner_interpreter`` (so ``qai_appbuilder`` + the QNN runtime
        load). Defaults to ``(sys.executable,)`` when not injected (dev / tests).
    env:
        Full environment for the child (already merged with the QAIRT SDK env +
        ``PATH`` extras by the caller/DI, mirroring the sticky-worker spawn).
        Defaults to a copy of ``os.environ`` when not supplied.

    Raises
    ------
    MissingQaiAppBuilderError
        When the child could not import ``qai_appbuilder`` / shared modules.
    SmokeTestFailedError
        When the ``.bin`` failed to load / invoke, or the child exited without
        a valid result envelope (e.g. a native crash) — the child's stderr tail
        is attached for diagnostics.
    """
    prefix = tuple(interpreter_argv) if interpreter_argv else (sys.executable,)
    argv: list[str] = [
        *prefix,
        str(_CHILD_SCRIPT),
        str(context_bin),
    ]
    if shared_dir is not None:
        argv += ["--shared-dir", str(shared_dir)]

    child_env: dict[str, str] = dict(env) if env is not None else dict(os.environ)
    # Ensure the child can import qai.* (the pure probe + domain errors) and the
    # shared helpers. Prepend shared_dir so ``import qnn_helper`` resolves the
    # same modules the in-process path used (parity with _io_contract_probe's
    # sys.path.insert). We only ADD entries; we never drop the inherited path.
    if shared_dir is not None and shared_dir.is_dir():
        existing_pp = child_env.get("PYTHONPATH", "")
        child_env["PYTHONPATH"] = str(shared_dir) + (
            os.pathsep + existing_pp if existing_pp else ""
        )

    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_env,
        )
    except (OSError, ValueError) as exc:
        # Could not even spawn the child (bad interpreter path, etc.). Treat as
        # a smoke-test failure so the exporter's failure handling is unchanged.
        raise SmokeTestFailedError(
            f"failed to spawn smoke-test child process: {exc} "
            f"(context_bin={context_bin}, interpreter={prefix[0]})"
        ) from exc

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=_SMOKE_CHILD_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError as exc:
        # The child hung (native deadlock / stuck HTP init). Kill its whole
        # process tree so it cannot linger, then surface a hard smoke-test
        # failure — never let a hung child block the export forever.
        best_effort_tree_kill(proc)
        # Best-effort reap so we don't leave a zombie / unawaited transport.
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except Exception:  # noqa: BLE001 — reap is best-effort; already killed
            pass
        raise SmokeTestFailedError(
            f"smoke-test child timed out after "
            f"{_SMOKE_CHILD_TIMEOUT_SECONDS:.0f}s (context_bin={context_bin}). "
            "The QNN native runtime most likely hung while loading or "
            "invoking the .bin; the child was killed."
        ) from exc

    stdout_text = stdout_bytes.decode("utf-8", errors="replace")
    stderr_text = stderr_bytes.decode("utf-8", errors="replace")

    envelope = _parse_envelope(stdout_text)

    if envelope is None:
        # No well-formed envelope on stdout: the child crashed / exited abnormally
        # (e.g. native segfault) before it could report. Surface as a smoke-test
        # failure with the stderr tail (the native traceback / QNN error) so the
        # operator can diagnose — same hard-abort class as an invocation crash.
        tail = _stderr_tail(stderr_text)
        raise SmokeTestFailedError(
            f"smoke-test child produced no valid result "
            f"(exit_code={proc.returncode}, context_bin={context_bin}). "
            f"This usually means the QNN native runtime crashed while loading "
            f"or invoking the .bin. Child stderr tail:\n{tail}"
        )

    if envelope.get("ok") is True:
        contract = envelope.get("io_contract")
        if isinstance(contract, dict):
            return contract
        # Malformed success envelope — defensive: treat as a failure rather than
        # returning a non-dict contract into the manifest.
        raise SmokeTestFailedError(
            f"smoke-test child returned ok=true with a non-dict io_contract "
            f"(context_bin={context_bin})"
        )

    # ok:false — rebuild the typed exception the in-process path would have raised.
    error_code = str(envelope.get("error_code") or SmokeTestFailedError.code)
    message = str(envelope.get("message") or "smoke test failed")
    raise _rebuild_exception(error_code, message)


def _parse_envelope(stdout_text: str) -> dict[str, Any] | None:
    """Parse the child's single-line JSON result envelope from its stdout.

    The child writes exactly one JSON object line to its protected stdout. To be
    robust against a stray leading line (defence in depth — the stdout guard
    should prevent native noise here, but a fallback path may not), scan lines in
    reverse for the first that parses as a dict carrying an ``ok`` key.
    """
    for line in reversed([ln for ln in stdout_text.splitlines() if ln.strip()]):
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict) and "ok" in obj:
            return obj
    return None


def _stderr_tail(stderr_text: str, *, max_lines: int = 20) -> str:
    """Return the last ``max_lines`` non-empty stderr lines (crash diagnostics)."""
    lines = [ln for ln in stderr_text.splitlines() if ln.strip()]
    if not lines:
        return "<none captured>"
    return "\n".join(lines[-max_lines:])
