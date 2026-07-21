# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------
"""Child-process audit sentinel: marker package for the protected-paths startup hook.

The actual hook lives in ``sitecustomize.py`` in this directory (its filename is
an external CPython convention — ``site.py`` auto-imports a module named exactly
``sitecustomize`` at interpreter startup, so it must NOT be renamed). The
directory is placed on a child process's ``PYTHONPATH`` (by the exec env builder)
so the interpreter auto-imports ``sitecustomize`` at startup and installs the
protected-path write deny hook BEFORE any user/pipeline code runs.

This package intentionally contains no importable runtime logic (the hook must
run as ``sitecustomize``, not as ``qai.platform.child_process_audit_sentinel``).
"""
