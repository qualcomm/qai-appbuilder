# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Backward-compatible re-export of the shared ``ProcessKillGroup``.

The OS-level "parent dies -> child dies" safeguard (Win32 Job Object with
``KILL_ON_JOB_CLOSE``) is a CROSS-CONTEXT utility, so it now lives in the
platform shared kernel at ``qai.platform.process.kill_group`` where both
``qai.model_runtime`` (GenieAPIService) and ``qai.app_builder`` (the sticky
worker) can use it without violating the ``context-isolation`` contract.

This module keeps the original import path working for existing
``model_runtime`` callers (e.g. ``process_service.py``); it simply re-exports
the platform implementation. See AGENTS.md State-Truth-First 铁律 5.
"""

from __future__ import annotations

from qai.platform.process.kill_group import ProcessKillGroup

__all__ = ["ProcessKillGroup"]
