# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Pure domain layer for the ``app_builder`` bounded context.

This package contains entities, value objects, domain services, domain
events and domain errors for App Builder. It MUST NOT import any
framework (fastapi, sqlalchemy, aiosqlite, httpx, pydantic_settings)
nor any adapters/infrastructure module — see ``.importlinter`` contract
``domain-purity``.

Public submodules:

* :mod:`qai.app_builder.domain.value_objects` — primitive VOs
* :mod:`qai.app_builder.domain.taxonomy` — taxonomy / category VOs
* :mod:`qai.app_builder.domain.app_model` — ``AppModelDefinition`` entity
* :mod:`qai.app_builder.domain.artifact` — ``Artifact`` value object
* :mod:`qai.app_builder.domain.run` — ``Run`` aggregate + state machine
* :mod:`qai.app_builder.domain.voice_preference` — voice preference VO
* :mod:`qai.app_builder.domain.import_plan` — three-state import plan VO
* :mod:`qai.app_builder.domain.events` — domain events
* :mod:`qai.app_builder.domain.errors` — domain errors
"""

from __future__ import annotations
