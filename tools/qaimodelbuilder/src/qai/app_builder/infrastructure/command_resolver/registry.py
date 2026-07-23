# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pack-runner command registry.

A registry maps :class:`AppModelDefinition` (resolved by ``model.id``
+ ``run.inputs["variant_id"]``) onto a :class:`RunnerSpec` describing
how to launch the Pack subprocess (script path / cwd / env extras /
timeout).

PR-302 ships an :class:`InMemoryRunnerCommandRegistry` that callers
populate explicitly (from DI fixtures or — once PR-303 lands — from
the manifest reader). Tests inject a registry mapping in their setup;
production injects the manifest-backed registry.

The :func:`build_command_resolver` factory wraps a registry into the
``CommandResolver`` callable expected by
:class:`qai.app_builder.infrastructure.process_runner.ProcessBackedAppRunner`.

Cross-cutting policy
--------------------

Every spawned runner has ``PYTHONUNBUFFERED=1`` and ``PYTHONIOENCODING=
utf-8`` injected by the resolver — runner_protocol v3.1 demands flushed
stdout and UTF-8 strings (matches legacy ``python_script.py:222-223``).

The shared library directory (``features/app-builder/shared/``) is
appended to ``PYTHONPATH`` when present; PR-306 will move it to the
release path and update the resolver accordingly.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol, runtime_checkable

from qai.app_builder.domain.app_model import AppModelDefinition
from qai.app_builder.domain.run import Run
from qai.app_builder.infrastructure.input_artifact_resolver import (
    resolve_input_artifact_paths,
)
from qai.app_builder.infrastructure.process_runner import CommandResolver
from qai.platform.process.ports import ProcessExecutionRequest

from ._request_payload import build_runner_request_payload
from .interpreter_resolver import PythonInterpreterResolver, SysExecutableResolver

import platform as _platform

__all__ = [
    "InMemoryRunnerCommandRegistry",
    "RunnerCommandRegistryPort",
    "RunnerSpec",
    "UnsupportedBackendError",
    "build_command_resolver",
]


class UnsupportedBackendError(RuntimeError):
    """Raised when a Pack requires a QNN delegate unavailable on this host.

    Specifically: HTP (Hexagon Tensor Processor) delegate on an x64 host
    — the Hexagon DSP hardware only exists on Snapdragon SoCs (ARM64).
    """

    def __init__(self, model_id: str, delegate: str, host_arch: str) -> None:
        self.model_id = model_id
        self.delegate = delegate
        self.host_arch = host_arch
        super().__init__(
            f"Model '{model_id}' requires the '{delegate.upper()}' backend "
            f"(Hexagon NPU), which is not available on this {host_arch} host. "
            f"HTP inference requires a Snapdragon ARM64 device with Hexagon DSP."
        )

_DEFAULT_TIMEOUT_S: float = 300.0
"""Five-minute hard wall-clock cap (parity with legacy
``_DEFAULT_TIMEOUT_MS = 300_000``)."""

_DEFAULT_OUTPUT_BYTE_CAP: int = 64 * 1024 * 1024
"""64 MiB combined stdout+stderr cap (defends against runaway runners)."""


_logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class RunnerSpec:
    """Per-Pack launch specification.

    A registry entry. PR-303's manifest reader produces these from
    ``manifest.json``; PR-302 tests build them directly.

    Fields:

    * :attr:`script_path` — absolute path to the Pack's ``runner.py``;
    * :attr:`cwd` — Pack root directory (where ``runner.py`` lives);
    * :attr:`extra_env` — additional environment variables (Pack-specific
      knobs; merged on top of the resolver's base env);
    * :attr:`extra_pythonpath` — directories to *prepend* to ``PYTHONPATH``
      (typically the shared/ helpers directory);
    * :attr:`timeout_s` — wall-clock cap; ``None`` = unlimited;
    * :attr:`output_byte_cap` — combined stdout+stderr cap; ``None`` =
      unlimited.
    * :attr:`delegate` — QNN delegate declared by the Pack manifest
      (``"htp"`` / ``"cpu"`` / ``""``). Used by the command resolver to
      reject HTP-only models on x64 hosts (no Hexagon DSP available).
    """

    script_path: Path
    cwd: Path
    extra_env: Mapping[str, str] = field(default_factory=dict)
    extra_pythonpath: tuple[Path, ...] = field(default_factory=tuple)
    timeout_s: float | None = _DEFAULT_TIMEOUT_S
    output_byte_cap: int | None = _DEFAULT_OUTPUT_BYTE_CAP
    delegate: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.script_path, Path):
            raise TypeError("script_path must be a Path")
        if not isinstance(self.cwd, Path):
            raise TypeError("cwd must be a Path")
        if not isinstance(self.extra_env, Mapping):
            raise TypeError("extra_env must be a Mapping")
        if not isinstance(self.extra_pythonpath, tuple):
            raise TypeError("extra_pythonpath must be a tuple of Paths")
        for i, p in enumerate(self.extra_pythonpath):
            if not isinstance(p, Path):
                raise TypeError(
                    f"extra_pythonpath[{i}] must be a Path, got "
                    f"{type(p).__name__}"
                )
        if self.timeout_s is not None and (
            not isinstance(self.timeout_s, (int, float))
            or isinstance(self.timeout_s, bool)
            or self.timeout_s <= 0
        ):
            raise ValueError(
                f"timeout_s must be > 0 or None, got {self.timeout_s!r}"
            )
        if self.output_byte_cap is not None and (
            not isinstance(self.output_byte_cap, int)
            or isinstance(self.output_byte_cap, bool)
            or self.output_byte_cap <= 0
        ):
            raise ValueError(
                "output_byte_cap must be > 0 or None, got "
                f"{self.output_byte_cap!r}"
            )
        if not isinstance(self.delegate, str):
            raise TypeError("delegate must be a str")


@runtime_checkable
class RunnerCommandRegistryPort(Protocol):
    """Lookup interface used by :func:`build_command_resolver`."""

    def get(
        self, model: AppModelDefinition, run: Run
    ) -> RunnerSpec | None:
        """Return the launch spec for ``model``, or ``None``.

        ``None`` means "no command bound" — the caller emits a single
        ``no_command`` informational frame and exits cleanly (matches
        PR-045 behaviour that PR-303 will eliminate by populating the
        registry from the manifest).
        """
        ...


class InMemoryRunnerCommandRegistry:
    """Dict-backed implementation of :class:`RunnerCommandRegistryPort`.

    Indexed by string ``model_id`` (``str(model.id)``). Tests and DI
    populate via :meth:`register`; PR-303's manifest reader populates
    from ``manifest.json`` rows on container startup.
    """

    __slots__ = ("_specs",)

    def __init__(
        self, specs: Mapping[str, RunnerSpec] | None = None
    ) -> None:
        self._specs: dict[str, RunnerSpec] = (
            dict(specs) if specs is not None else {}
        )

    def register(self, model_id: str, spec: RunnerSpec) -> None:
        if not isinstance(model_id, str) or not model_id.strip():
            raise ValueError("model_id must be a non-empty str")
        if not isinstance(spec, RunnerSpec):
            raise TypeError("spec must be a RunnerSpec")
        self._specs[model_id] = spec

    def deregister(self, model_id: str) -> None:
        self._specs.pop(model_id, None)

    def unregister(self, model_id: str) -> None:
        """Drop the runner spec for ``model_id`` (idempotent).

        P2 / Sub-A runtime cache invalidation on Pack deletion — satisfies
        :class:`qai.app_builder.application.ports.RunnerRegistryPort.unregister`.
        Historical spelling :meth:`deregister` is preserved for existing
        callers; :meth:`unregister` matches the Clean-Architecture port
        contract. Both are silent no-ops for unknown ids.

        State-Truth-First (§🔴 铁律 1): after this call, ``get(model, run)``
        for that ``model_id`` returns ``None`` so the resolver falls back
        to ``no_command`` rather than spawning against a stale spec whose
        ``runner.py`` no longer exists on disk.
        """
        # Log only when the id was actually present so the log line is a
        # signal of real state change (not a no-op invalidation call).
        if self._specs.pop(model_id, None) is not None:
            _logger.info(
                "app_builder.runner_registry.unregister: id=%s (runtime cache cleared)",
                model_id,
            )

    def get(
        self, model: AppModelDefinition, run: Run
    ) -> RunnerSpec | None:
        del run  # PR-302 keys solely by model.id; PR-303 may key by variant.
        return self._specs.get(str(model.id))

    def __len__(self) -> int:
        return len(self._specs)

    def __contains__(self, model_id: str) -> bool:
        return model_id in self._specs


# ---------------------------------------------------------------------------
# Resolver factory
# ---------------------------------------------------------------------------
def build_command_resolver(
    *,
    registry: RunnerCommandRegistryPort,
    interpreter: PythonInterpreterResolver | None = None,
    base_env: Mapping[str, str] | None = None,
    repo_root: Path | None = None,
    blobs_dir: Path | None = None,
    proxy_provider: Callable[[], str | None] | None = None,
) -> CommandResolver:
    """Wrap a registry + interpreter into a :class:`CommandResolver`.

    Parameters
    ----------
    registry:
        Source of :class:`RunnerSpec` objects.
    interpreter:
        Resolves which Python executable to use. Defaults to
        :class:`SysExecutableResolver` (``sys.executable``); production
        DI passes a :class:`QairtEnvJsonResolver`.
    base_env:
        Mapping merged into every spawn (after ``os.environ`` and
        before ``RunnerSpec.extra_env``). Useful for tests that want a
        clean inheritable env (defaults to ``os.environ`` copy).
    repo_root:
        Absolute path to the repository root used to populate the
        ``repoRoot`` field of the runner request envelope written to
        the child's stdin (see :func:`build_runner_request_payload`).
        Production DI passes ``container.repo_root`` so the Pack
        runners (``factory/chat_features/app-builder/models/<pack>/runner.py``)
        can resolve their weight + asset paths under
        ``<repo>/models/<pack>/`` and ``<repo>/factory/...``. When
        ``None`` no stdin envelope is attached and the resolver
        preserves PR-302's argv-only behaviour — this keeps the
        existing tests (which never wired ``repo_root``) byte-for-byte
        equivalent.

    blobs_dir:
        Optional blob root used to resolve logical upload paths to absolute
        physical paths before serialising the runner request.
    proxy_provider:
        Optional ``() -> str | None`` global-proxy provider (mechanism B;
        edition-dual-form §8 "缺口 10"). Built by the apps/api wiring root via
        ``build_global_proxy_provider(container)``. When wired, the resolver
        calls it **at every** spawn and, when it returns a non-empty URL,
        injects the standard ``HTTPS_PROXY`` / ``HTTP_PROXY`` / ``ALL_PROXY``
        (+ lowercase) environment variables into the runner subprocess env so
        the Pack runners' ``urllib`` weight downloads route through the proxy.
        ``None`` (no proxy configured) leaves the env untouched and the runner
        connects directly — the proxy is never forced (State-Truth-First).
        The env var is a parent->child *transport* of an already-resolved
        value, NOT a configuration source (hard-constraint ①: the source of
        truth is the file-backed ``ToolsSettings.global_proxy``).

    The returned callable matches the signature
    ``(Run, AppModelDefinition) -> ProcessExecutionRequest | None``
    expected by :class:`ProcessBackedAppRunner`.

    Behavior contract
    -----------------

    * ``registry.get(model, run)`` returns ``None`` ⇒ resolver returns
      ``None`` (matches PR-045 fallback);
    * spec found ⇒ build a :class:`ProcessExecutionRequest` whose
      ``argv[0]`` is the resolver's interpreter and whose ``argv[1:]``
      starts with ``"-u"`` (unbuffered IO; runner_protocol contract);
    * when ``repo_root`` is wired, the request additionally carries a
      ``stdin_data`` payload — a single line of JSON consumed by the
      Pack ``runner.py``'s ``read_request()``.
    """
    interp = interpreter or SysExecutableResolver()

    def _resolve(
        run: Run, model: AppModelDefinition
    ) -> ProcessExecutionRequest | None:
        spec = registry.get(model, run)
        if spec is None:
            return None
        # Phase D: reject HTP-only models on x64 hosts (no Hexagon DSP).
        # This pre-check gives a clear error instead of a cryptic DLL load
        # failure from the subprocess. See x64-windows-support-plan.md §D-4.
        if (
            spec.delegate.lower() == "htp"
            and _platform.machine() in ("AMD64", "x86_64")
        ):
            raise UnsupportedBackendError(
                model_id=str(model.id),
                delegate=spec.delegate,
                host_arch=_platform.machine(),
            )
        env = _materialise_env(base_env, spec, interp)
        # 缺口 10: inject the live global-proxy URL (mechanism B) into the
        # runner subprocess so its ``urllib`` weight downloads route through
        # the proxy. Read at spawn time so a runtime proxy edit hot-applies;
        # a ``None`` return leaves the env untouched (direct connection).
        if proxy_provider is not None:
            _apply_proxy_env(env, proxy_provider)
        argv = (
            str(interp.resolve()),
            "-u",
            str(spec.script_path),
        )
        # When ``repo_root`` is wired by DI we serialise the runner
        # request envelope so the Pack ``runner.py`` finds its inputs
        # on stdin. Without it (e.g. existing tests that never passed
        # ``repo_root``) we keep PR-302's argv-only spawn so byte-for-
        # byte regression is preserved.
        stdin_data: bytes | None = None
        if repo_root is not None:
            inputs_payload, params_payload, variant = _split_run_inputs(
                run.inputs
            )
            # Resolve logical upload paths (``uploads/audio/…`` /
            # ``uploads/image/…``) to absolute physical paths under the
            # data blob root so the one-shot runner can open the file
            # (it only anchors relative paths against repoRoot/packDir/
            # cwd). V1 parity rationale: see ``input_artifact_resolver``.
            inputs_payload = resolve_input_artifact_paths(
                inputs_payload, blobs_dir=blobs_dir
            )
            stdin_data = build_runner_request_payload(
                repo_root=repo_root,
                # ``RunnerSpec.cwd`` is the Pack root directory (see
                # ``registry_bridge._build_runner_spec`` which sets
                # ``cwd=pack_dir``). Reuse it so we don't have to
                # extend ``RunnerSpec`` with a redundant field.
                pack_dir=spec.cwd,
                inputs=inputs_payload,
                params=params_payload,
                variant=variant,
                run_id=str(run.id),
            )
        return ProcessExecutionRequest(
            argv=argv,
            cwd=str(spec.cwd) if spec.cwd is not None else None,
            env=env,
            timeout_s=spec.timeout_s,
            output_byte_cap=spec.output_byte_cap,
            stdin_data=stdin_data,
        )

    return _resolve


def _split_run_inputs(
    run_inputs: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any] | None, str | None]:
    """Project :attr:`Run.inputs` into the runner request shape.

    ``Run.inputs`` is a domain dict that bundles three things together
    for cache-key stability (see ``run_app._extract_variant_id`` /
    ``_extract_params``):

    * ``variant_id`` — optional string; surfaces as the top-level
      ``variant`` field on the runner request.
    * ``params``    — optional dict; surfaces as the top-level
      ``params`` field on the runner request (the runners read e.g.
      ``req["params"]["language"]``).
    * everything else — the user-facing inputs (``image`` / ``audio``
      / ``text`` / ...). These ride in the request's ``inputs``
      sub-dict (the runners read e.g. ``req["inputs"]["image"]``).

    Returning a fresh ``dict`` for ``inputs_payload`` keeps the
    domain :class:`Run` mapping immutable from the resolver's point
    of view.
    """
    inputs_payload: dict[str, Any] = {}
    params_payload: dict[str, Any] | None = None
    variant: str | None = None
    for key, value in (run_inputs or {}).items():
        if key == "variant_id":
            if isinstance(value, str) and value:
                variant = value
            continue
        if key == "params":
            if isinstance(value, Mapping):
                params_payload = dict(value)
            continue
        inputs_payload[str(key)] = value
    return inputs_payload, params_payload, variant


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------
def _apply_proxy_env(
    env: dict[str, str], proxy_provider: Callable[[], str | None]
) -> None:
    """Inject the live global-proxy URL into the runner spawn env (缺口 10).

    Calls ``proxy_provider()`` and, on a non-empty URL, sets the standard
    proxy environment variables (both upper- and lower-case spellings — Python's
    ``urllib.request.getproxies()`` reads the lower-case ``http_proxy`` etc.)
    so the Pack runner subprocess's ``urllib`` weight download routes through
    the proxy. A ``None`` / empty return leaves the env untouched: the proxy is
    never forced and the runner connects directly (State-Truth-First).

    The provider may raise (e.g. a transient config-read error); we swallow it
    so a proxy hiccup never blocks a Pack run that may not even need the
    network (idempotent weight checks short-circuit when files exist).
    """
    try:
        proxy_url = proxy_provider()
    except Exception:  # noqa: BLE001 — never block a spawn on proxy read
        return
    if not proxy_url:
        return
    for key in (
        "HTTPS_PROXY", "https_proxy",
        "HTTP_PROXY", "http_proxy",
        "ALL_PROXY", "all_proxy",
    ):
        env[key] = proxy_url


def _materialise_env(
    base_env: Mapping[str, str] | None,
    spec: RunnerSpec,
    interpreter: PythonInterpreterResolver | None = None,
) -> dict[str, str]:
    """Build the spawn environment.

    Layering (later wins):

    1. ``os.environ`` (or ``base_env`` if provided);
    2. runner_protocol v3.1 mandatory keys
       (``PYTHONUNBUFFERED=1``, ``PYTHONIOENCODING=utf-8``,
       ``PYTHONFAULTHANDLER=1``);
    3. interpreter resolver's :meth:`extra_env` — typically the QAIRT
       SDK roots (``QAIRT_ROOT`` / ``QNN_SDK_ROOT``) when the resolver
       is a :class:`QairtEnvJsonResolver`. Empty for
       :class:`SysExecutableResolver` so non-QAIRT spawns are
       byte-for-byte unchanged;
    4. ``spec.extra_env`` (Pack-specific knobs override resolver-level
       defaults — same precedence as the legacy module);
    5. ``PYTHONPATH`` prepended with ``spec.extra_pythonpath`` (so
       Pack's shared/ wins over the inherited path);
    6. ``PATH`` prepended with the interpreter resolver's
       :meth:`path_segments` (typically the QAIRT SDK's ``bin/`` +
       ``lib/`` arch sub-directories so the QNN runtime DLLs load
       before any system-wide copy).
    """
    env: dict[str, str] = (
        dict(base_env) if base_env is not None else dict(os.environ)
    )
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONFAULTHANDLER", "1")
    # Resolver-level extras (QAIRT SDK roots etc.). Best-effort: a
    # resolver that doesn't expose ``extra_env`` (e.g. a hand-rolled
    # test stub) is treated as contributing nothing.
    if interpreter is not None:
        extra_env_fn = getattr(interpreter, "extra_env", None)
        if callable(extra_env_fn):
            for k, v in extra_env_fn().items():
                env[str(k)] = str(v)
    for k, v in spec.extra_env.items():
        env[str(k)] = str(v)
    if spec.extra_pythonpath:
        prefix = os.pathsep.join(str(p) for p in spec.extra_pythonpath)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            prefix + (os.pathsep + existing if existing else "")
        )
    # PATH segments from the interpreter resolver (QAIRT SDK bin/lib).
    # Prepended so the SDK DLLs win against any system-wide copy.
    if interpreter is not None:
        path_segments_fn = getattr(interpreter, "path_segments", None)
        if callable(path_segments_fn):
            segments = path_segments_fn()
            if segments:
                prefix = os.pathsep.join(str(s) for s in segments)
                existing = env.get("PATH", "")
                env["PATH"] = (
                    prefix + (os.pathsep + existing if existing else "")
                )
    return env
