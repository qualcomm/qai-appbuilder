# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai.remote_deploy`` bounded context — SSH-based remote deployment.

Scope:
------
* Domain: ``RemoteHost`` / ``RemoteInstance`` entities, ``errors.py``.
* Ports:  ``SshExecutorPort`` / ``RemoteInstanceRepositoryPort``.
* Adapters: ``InMemoryRemoteInstanceRepository``.
* Infrastructure: ``ParamikoSshExecutor``.
* Use cases: Connect / Deploy / ListInstances / StopInstance.

Cross-context boundary:
-----------------------
Only imports ``qai.platform.*`` and ``qai.remote_deploy.*`` (v2.7 §3.2).
"""
