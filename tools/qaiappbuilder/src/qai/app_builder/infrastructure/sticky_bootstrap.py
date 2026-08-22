# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""BootstrapSpec builder for the persistent sticky worker (PR-302 wiring).

V1 ``backend/app_builder/runners/sticky_worker.py:131-185``
(``StickyWorker.spawn``) launches the persistent worker with::

    <arm64_python> -u -X faulthandler <_runner_bootstrap.py> --persistent

with ``PYTHONUNBUFFERED=1`` / ``PYTHONIOENCODING=utf-8`` /
``PYTHONFAULTHANDLER=1`` and the Pack ``shared/`` directory prepended to
``PYTHONPATH`` so the resident worker can ``from runner_protocol import
emit`` (and the per-model runners loaded later inherit the same path).

This helper builds the equivalent
:class:`qai.app_builder.infrastructure.sticky_worker.BootstrapSpec` so
the ``apps/api`` lifespan hook stays thin (it only owns *when* to spawn,
not *how*). Kept in infrastructure because it references the bootstrap
script path + env policy, both infrastructure concerns.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from .sticky_worker import BootstrapSpec

__all__ = ["BOOTSTRAP_SCRIPT", "build_persistent_bootstrap_spec"]

# The persistent-mode entry point ships alongside the one-shot bootstrap.
BOOTSTRAP_SCRIPT: Path = (
    Path(__file__).resolve().parent / "_runner_bootstrap.py"
)


def build_persistent_bootstrap_spec(
    *,
    python_exe: Path,
    shared_dir: Path | None = None,
    base_env: Mapping[str, str] | None = None,
    multimodel: bool | None = None,
    trust_token: str | None = None,
) -> BootstrapSpec:
    """Build the ``--persistent`` worker :class:`BootstrapSpec`.

    Parameters
    ----------
    python_exe:
        The interpreter to launch (production: the ARM64 venv python so
        ``qai_appbuilder`` / the QNN runtime load; tests can pass
        ``sys.executable``).
    shared_dir:
        Pack ``shared/`` helper directory (``factory/chat_features/app-builder/shared``)
        prepended to ``PYTHONPATH`` when present.
    base_env:
        Base environment (defaults to ``os.environ``); the runner_protocol
        mandatory keys + ``PYTHONPATH`` are layered on top.
    multimodel:
        When not ``None``, pins ``QAI_STICKY_MULTIMODEL`` so the worker's
        single/multi-model policy matches the host's. Left unset means the
        worker reads its own env default (multi-model on).
    trust_token:
        Phase-1 FileGuard TrustedInfra identity: when set, threaded into
        ``BootstrapSpec.trust_token`` so ``_default_spawn`` injects
        ``QAI_FILEGUARD_TRUST_TOKEN`` into the worker's env. The native
        DLL's DllMain classifies any child with this env var set as
        TrustedInfra (pass through undetermined ops without ASK; op-mask
        deny still enforced for protected paths). ``None`` leaves the
        worker as an untrusted subprocess (normal ASK / rule pipeline).
    """
    env: dict[str, str] = (
        dict(base_env) if base_env is not None else dict(os.environ)
    )
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONFAULTHANDLER", "1")
    if multimodel is not None:
        env["QAI_STICKY_MULTIMODEL"] = "1" if multimodel else "0"
    if shared_dir is not None and shared_dir.is_dir():
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = str(shared_dir) + (
            os.pathsep + existing if existing else ""
        )

    argv = (
        str(python_exe),
        "-u",
        "-X",
        "faulthandler",
        str(BOOTSTRAP_SCRIPT),
        "--persistent",
    )
    return BootstrapSpec(
        argv=argv, cwd=None, env=env, trust_token=trust_token
    )
