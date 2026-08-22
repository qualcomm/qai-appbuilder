# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for the App Builder application layer.

Each use case is a small orchestrator with explicit, constructor-injected
dependencies. None of the use cases imports adapters / infrastructure
modules — only domain objects and ports. See ``application/ports.py``
for the abstract dependencies.

PR-045 added 5 missing use cases (issue d): :class:`GetRunUseCase`,
:class:`ListRunArtifactsUseCase`, :class:`GetWorkerStatusUseCase`,
:class:`CreateShareUseCase`, :class:`GetShareByTokenUseCase`.
"""

from __future__ import annotations

from .cancel_run import CancelRunUseCase
from .delete_app_model import DeleteAppModelUseCase
from .get_app_model import GetAppModelUseCase
from .get_run import GetRunUseCase
from .get_worker_status import GetWorkerStatusUseCase
from .import_workflow import (
    ImportCommitUseCase,
    ImportDryRunUseCase,
    ImportRollbackUseCase,
)
from .list_app_models import ListAppModelsUseCase
from .list_run_artifacts import ListRunArtifactsUseCase
from .run_app import RunAppUseCase
from .share import CreateShareUseCase, GetShareByTokenUseCase
from .upload_audio import UploadAudioUseCase
from .voice_preference import (
    GetVoicePreferenceUseCase,
    SetVoicePreferenceUseCase,
)

__all__ = [
    "RunAppUseCase",
    "CancelRunUseCase",
    "ListAppModelsUseCase",
    "GetAppModelUseCase",
    "DeleteAppModelUseCase",
    "UploadAudioUseCase",
    "ImportDryRunUseCase",
    "ImportCommitUseCase",
    "ImportRollbackUseCase",
    "GetVoicePreferenceUseCase",
    "SetVoicePreferenceUseCase",
    # PR-045 — issue d
    "GetRunUseCase",
    "ListRunArtifactsUseCase",
    "GetWorkerStatusUseCase",
    "CreateShareUseCase",
    "GetShareByTokenUseCase",
]
