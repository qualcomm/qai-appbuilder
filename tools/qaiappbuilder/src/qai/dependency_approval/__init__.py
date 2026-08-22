# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""``qai.dependency_approval`` bounded context — dependency installation approval.

This BC manages pending dependency-install requests in the security sandbox.
Callers (e.g. AI-coding tools that want to ``pip install`` a package) submit
an install request; the dep_broker queues it for human approval, then the
operator (via ``/api/security/dep_broker/approve`` or ``reject``) resolves it.

Scope (S7.5 lane L6 — PR-603):
-------------------------------
* Domain: ``PendingRequest`` entity + ``RequestStatus`` enum.
* Port: ``DepBrokerPort`` — ``get_pending()`` / ``resolve()``.
* Adapter: ``InMemoryDepBroker`` — production in-memory queue.
* Use cases: ``GetPendingRequestsUseCase``, ``ResolveRequestUseCase``.

Cross-context boundary:
-----------------------
Only imports ``qai.platform.*`` and ``qai.dependency_approval.*`` (v2.7 §3.2).
"""
