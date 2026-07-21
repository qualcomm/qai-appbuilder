# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Use cases for ``qai.user_prefs`` (PR-601a + R4/R5/R6 route-thinning)."""
from qai.user_prefs.application.use_cases.forge_config import (
    LoadForgeConfigUseCase,
)
from qai.user_prefs.application.use_cases.load_document import (
    LoadDocumentUseCase,
)
from qai.user_prefs.application.use_cases.proxy import (
    GetProxyUseCase,
    SaveProxyUseCase,
)
from qai.user_prefs.application.use_cases.save_document import (
    SaveDocumentUseCase,
)
from qai.user_prefs.application.use_cases.skills import (
    GetSkillPolicyUseCase,
    ListSkillsUseCase,
    ReloadSkillsUseCase,
    SetSkillModeUseCase,
    SetSkillPolicyModeUseCase,
    SkillModeNotAllowedError,
    SkillNotFoundError,
    ToggleSkillUseCase,
)

__all__ = [
    "GetProxyUseCase",
    "GetSkillPolicyUseCase",
    "ListSkillsUseCase",
    "LoadDocumentUseCase",
    "LoadForgeConfigUseCase",
    "ReloadSkillsUseCase",
    "SaveDocumentUseCase",
    "SaveProxyUseCase",
    "SetSkillModeUseCase",
    "SetSkillPolicyModeUseCase",
    "SkillModeNotAllowedError",
    "SkillNotFoundError",
    "ToggleSkillUseCase",
]
