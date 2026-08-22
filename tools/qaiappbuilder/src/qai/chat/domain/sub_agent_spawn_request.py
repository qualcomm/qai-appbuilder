# ---------------------------------------------------------------------
# Copyright (c) 2026 Qualcomm Technologies, Inc. and/or its subsidiaries.
# SPDX-License-Identifier: BSD-3-Clause
# ---------------------------------------------------------------------

"""Typed spawn request / handle for the unified sub-agent spawn path.

``SubAgentSpawnRequest`` is what the agent tool hands to the spawn
bridge, and ``SubAgentHandle`` is what comes back.  Both replace the
free-form dict / loose-kwargs passing the spawn path used before, so the
set of per-spawn capabilities (may it spawn children? may it ask the
user?) is a checked, greppable contract instead of a bag of keys nobody
can enumerate.

New capabilities are **tail-appended** with a plain dataclass default
(§3.1) — there is no forward-compatibility promise for this in-process
type, so no validation framework is involved.

Purity: domain layer, standard library only, no runtime side effects.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["SubAgentHandle", "SubAgentSpawnRequest"]


@dataclass(frozen=True, slots=True)
class SubAgentSpawnRequest:
    """One request to spawn a sub-agent.

    :param agent_type: which agent profile to run (e.g. ``"explore"``,
        ``"general"``, ``"reviewer"``).
    :param prompt: the instruction the parent agent gives the sub-agent.
    :param allow_child_spawn: whether this sub-agent may itself spawn
        further sub-agents (depth is still capped independently).
    :param allow_question: whether this sub-agent may ask the user a
        question rather than deciding on its own.
    :param model_hint: optional model override; ``None`` = inherit the
        resolver's choice.
    """

    agent_type: str
    prompt: str
    allow_child_spawn: bool = False
    allow_question: bool = False
    model_hint: str | None = None


@dataclass(frozen=True, slots=True)
class SubAgentHandle:
    """The two ids that identify an accepted spawn.

    :param subagent_id: the persisted sub-agent session id, allocated by
        the spawn bridge.
    :param job_id: the async-job id allocated by the job manager, used to
        await / cancel / deliver the sub-agent's result.
    """

    subagent_id: str
    job_id: str
