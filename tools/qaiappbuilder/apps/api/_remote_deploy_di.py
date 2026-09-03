# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""DI wiring for the ``remote_deploy`` bounded context.

Field-name lock (v2.7 §3.1)
---------------------------
Once :class:`RemoteDeployServices` is wired into ``Container.remote_deploy``
its existing field names are part of the public namespace contract:
they may only be **tail-appended** by future PRs, never renamed or
removed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from qai.remote_deploy.adapters import InMemoryRemoteInstanceRepository
from qai.remote_deploy.application.ports import (
    RemoteInstanceRepositoryPort,
    SshExecutorPort,
)
from qai.remote_deploy.application.use_cases import (
    ConnectRemoteUseCase,
    DeployRemoteUseCase,
    InstallRemoteUseCase,
    ListInstancesUseCase,
    StartRemoteUseCase,
    StopInstanceUseCase,
)
from qai.remote_deploy.infrastructure import ParamikoSshExecutor

if TYPE_CHECKING:  # pragma: no cover
    from .di import Container

__all__ = [
    "RemoteDeployServices",
    "build_remote_deploy_services",
]


@dataclass(slots=True)
class RemoteDeployServices:
    """Application services / ports for the ``remote_deploy`` namespace.

    Field-name lock — tail-append only (v2.7 §3.1).
    """

    executor: SshExecutorPort
    repository: RemoteInstanceRepositoryPort
    connect_use_case: ConnectRemoteUseCase
    deploy_use_case: DeployRemoteUseCase
    list_instances_use_case: ListInstancesUseCase
    stop_instance_use_case: StopInstanceUseCase
    # Local Paramiko tunnel service; tail-appended to preserve the public
    # container field contract.
    tunnel_manager: SshExecutorPort
    # Install / start split; tail-appended per the field-name lock. The
    # composed ``deploy_use_case`` above still runs both in one call.
    install_use_case: InstallRemoteUseCase
    start_use_case: StartRemoteUseCase


def build_remote_deploy_services(container: "Container") -> RemoteDeployServices:
    """Wire the remote_deploy namespace.

    The ``ParamikoSshExecutor`` receives the platform ``SecretStore`` so it
    can resolve auth credentials at connection time without ever persisting
    them. The ``InMemoryRemoteInstanceRepository`` is process-lifetime; a
    restart clears the instance list (acceptable — the user can re-add).
    """
    secret_store = container.secret_store
    executor = ParamikoSshExecutor(secret_store=secret_store)
    repository = InMemoryRemoteInstanceRepository()

    return RemoteDeployServices(
        executor=executor,
        repository=repository,
        connect_use_case=ConnectRemoteUseCase(executor=executor),
        deploy_use_case=DeployRemoteUseCase(
            executor=executor, repository=repository
        ),
        list_instances_use_case=ListInstancesUseCase(repository=repository),
        stop_instance_use_case=StopInstanceUseCase(
            executor=executor, repository=repository
        ),
        tunnel_manager=executor,
        install_use_case=InstallRemoteUseCase(
            executor=executor, repository=repository
        ),
        start_use_case=StartRemoteUseCase(
            executor=executor, repository=repository
        ),
    )
