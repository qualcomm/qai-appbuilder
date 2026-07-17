# Skill Evolution (evolve) Reference

> **Purpose**: General knowledge, guardrails, and setup for the `evolve` phase — skill self-improvement after a project completes.
> This is NOT a step-by-step task guide. It describes principles, constraints, and the verification protocol for updating the aipc skill.

---

## What is evolve?

The `evolve` phase is an **optional, opt-in** post-project improvement cycle.
It activates only when `EVOLVE = YES` is set in the project config. By default it is **disabled**.

After a project's main work phases complete, the Evolve Orchestrator reviews what was learned and proposes targeted improvements to the aipc skill itself — specifically to `aipc_plan.md`, `aipc_AGENTS.md`, and reference documents.

The goal is to make the skill better for the next project, not to document what happened in this one.

---

## Activation

Set in `aipc_plan.md` Config:

```
EVOLVE = <!-- YES / NO (default NO) — run skill self-improvement phase after Phase 6/7/8 -->
EVOLVE_MODE = <!-- inherit / batch / interactive (default inherit) — inherit uses MODE; batch applies verified changes automatically; interactive asks user before applying -->
AIPC_SKILL_DIR = <!-- path to the aipc skill checkout to update; global or project-local -->
```

If `EVOLVE = NO` or unset: skip everything in this phase entirely.

If `EVOLVE_MODE = inherit` or unset, use the project `{MODE}` value. In effective `batch` mode,
apply approved/revised changes automatically after Verification Subagent review and record decisions
in the evolve summary. In effective `interactive` mode, present the candidate list and final
approved/revised diff to the user for confirmation before writing changes to the skill repository.
For `REVISE` verdicts, this confirmation is mandatory: show the final revised diff and the proposed
commit message, then wait for explicit user approval before applying the diff or creating the commit.

Before starting evolve, resolve `AIPC_SKILL_DIR` to the skill copy that will be updated. This may be
the globally installed aipc skill or a project-local skill checkout, but it must be an actual git
repository. If it is not already a git repository, run `git init`, add the current skill files, and
commit the initial state before proposing or applying any evolve changes.

---

## Scope of Allowed Changes

### What CAN be updated

- **`aipc_plan.md`**: new config variables, guardrail notes, phase instructions, task checklists that apply to any project on any model
- **`aipc_AGENTS.md`**: agent role clarifications, decision rules, blocking conditions, workflow steps that apply generally
- **Reference documents** (`references/*.md`): general setup, known patterns, guardrails, common pitfalls, environment knowledge
- **`SKILL.md`**: trigger phrases, required guardrails, cross-platform notes

### What CANNOT be updated

- **Model-specific or project-specific details** — no hardcoded model names, layer names, file paths from the current project
- **Task-specific workarounds** — a fix found for one model is not a general rule unless it applies broadly
- **Temporary decisions** — things decided for this project that do not generalize
- **Windows-only or Linux-only content without a cross-platform note** — every addition must consider both platforms (ARM Windows, x86 Linux, ARM Linux); if a rule is platform-specific, say so explicitly

### References vs. Plan/Agents

| Document type | What belongs here |
|---|---|
| `references/*.md` | General knowledge: environment setup, known tool behaviors, common error patterns, guardrails, configuration options |
| `aipc_plan.md` | Flow, config variables, phase task lists, exit criteria, progress tracking |
| `aipc_AGENTS.md` | Agent roles, decision rules, blocking conditions, handoff protocol |

References are **encyclopedic** — they explain *how things work* in general.
Plan and Agents are **prescriptive** — they tell agents *what to do* in order.

---

## Guardrails for Skill Updates

1. **General, not specific**: every proposed change must apply to at least two different models or scenarios to qualify.
2. **Cross-platform**: flag whether a change is Windows-only, Linux-only, or both. Default assumption must be both.
3. **No redundancy**: do not duplicate content already in a reference; link to it instead.
4. **Minimal diff**: prefer adding a single clear sentence to an existing section over creating a new section.
5. **No retroactive history**: do not record "we found this in project X" — rewrite as a general principle.
6. **Subagent verification is mandatory**: the Evolve Orchestrator must spawn a separate Verification Subagent with a clean context to review every proposed change before applying it. See protocol below.

---

## Work History Inputs

The Evolve Orchestrator reads the following to build context before proposing changes:

1. `aipc_plan.md` — Issue Log, per-phase notes, any blocking conditions hit
2. `REPORT.md` — final accuracy/latency metrics and observations
3. `logs/` — stderr/stdout from each phase (especially errors and warnings)
4. All reference documents the current project used (linked from `aipc_plan.md` References table)
5. Current `aipc_AGENTS.md` and `SKILL.md` — to understand what already exists before proposing additions

The orchestrator synthesizes from these inputs a list of **candidate improvements** with rationale, before invoking the Verification Subagent.

---

## Verification Protocol (Mandatory)

Every proposed skill update **must** be reviewed by a Verification Subagent before being written.

### Why a subagent?

The Evolve Orchestrator accumulated context from a long project session. A fresh subagent with no project history applies the guardrails neutrally and catches over-generalizations the orchestrator may miss.

### Subagent input

Provide the subagent with:

1. The **full text of the proposed change** (diff-style or section replacement)
2. The **target file and section** where it would be inserted
3. The **rationale** (what was observed that motivates this change)
4. The **full current content** of the target file section being modified

The subagent must NOT receive the full project history — only the above inputs.

### Subagent verdict fields

The subagent returns for each proposed change:

| Field | Options | Meaning |
|---|---|---|
| `verdict` | `APPROVE` / `REJECT` / `REVISE` | Whether to apply as-is, discard, or rework |
| `reason` | free text | Why this verdict |
| `revised_text` | optional | Replacement text if `REVISE` |

### Apply rules

- `APPROVE` → apply directly
- `REJECT` → discard; log reason in the evolve summary
- `REVISE` → apply the `revised_text` from the subagent, not the original
- In effective `interactive` mode, do not apply approved/revised changes or create the skill commit
  until the user has reviewed the final diff and proposed commit message and explicitly confirmed.
  This is mandatory for any `REVISE` verdict.
- After all approved/revised changes are applied, commit the skill repository with a concise evolve
  summary message. Do not leave applied skill changes uncommitted.

---

## Categories of Commonly Valuable Updates

These are areas where past projects most often reveal gaps worth fixing in the skill:

### Environment & Setup
- New platform quirks (environment variable interactions, shell differences, path formats)
- Python venv edge cases across Windows/Linux
- QAIRT SDK version-specific behaviors

### Operator Patching
- New operator patterns that successfully replaced unsupported ops (general patterns, not model-specific)
- New blocking conditions or escalation criteria
- Numerical parity validation techniques

### Conversion & Quantization
- Calibration data quality signals (what makes a good vs bad calibration set)
- Precision fallback decision logic
- AIMET vs QAIRT tradeoff clarifications

### Inference & Validation
- New cosine similarity thresholds learned from experience
- Remote target inference reliability patterns
- Context binary deployment naming conventions

### Profiling & Optimization
- New bottleneck patterns (CPU fallback ops, VTCM pressure, precision mismatch)
- Layout optimization decision criteria

### Agent Flow
- New decision rules for Orchestrator (e.g., when to skip a phase)
- Blocking condition clarifications
- Exit criteria gaps discovered during a project

---

## evolve Summary (append to `aipc_plan.md` after phase completes)

After the evolve phase completes, append to `aipc_plan.md`:

```markdown
## Skill Evolution Summary

| # | Target file | Section | Change type | Verdict | Applied |
|---|---|---|---|---|---|
| 1 | | | add / modify / remove | APPROVE / REJECT | ✅ / ❌ |
```

Rejected changes: record reason. Applied changes: record the git-style summary line (one line per file changed).

---

## Progress Row (add to `aipc_plan.md` Progress Summary)

```
| E | Skill Evolution (evolve, post-project, opt-in) | Common | ⬜ Not Started |
```
