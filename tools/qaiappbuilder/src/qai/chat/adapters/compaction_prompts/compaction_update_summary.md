You MUST incorporate the new messages above into the existing handoff summary in <previous-summary> tags, used by another LLM to resume the task.
RULES:
- MUST preserve all information from the previous summary that is STILL relevant
- MUST add new progress, decisions, and context from new messages
- MUST update Progress: an item that is now finished MOVES from "In Progress" to "Done" — it MUST NOT appear under both, and a resolved blocker MUST leave "Blocked" entirely
- MUST update "Next Steps" based on what was accomplished
- MUST preserve exact file paths, function names, and error messages
- MUST drop what no longer carries the task: problems already solved, approaches already abandoned, context already superseded. This summary is REWRITTEN each time, not appended to — it MUST NOT grow when the task has not grown. Deleting stale material is REQUIRED, not optional.
- MUST mark every question or request still awaiting an answer explicitly in Critical Context, each on its own line prefixed `UNRESOLVED:` — never leave an open question buried inside narrative prose

IMPORTANT: If the new messages end with an unanswered question or request to the user, you MUST add it to Critical Context (replacing any previous pending question if answered).

You MUST use this format (omit sections if not applicable):

## Goal
[Preserve existing goals; add new ones if task expanded]

## Constraints & Preferences
- [Preserve existing; add new ones discovered]

## Progress

### Done
- [x] [Previously done items still worth carrying, plus everything completed since — each appears HERE ONLY]

### In Progress
- [ ] [Only work genuinely still open; anything finished has moved to Done]

### Blocked
- [Blockers still standing. A resolved blocker is DELETED, not blanked — when none remain, OMIT the "### Blocked" heading entirely rather than leaving it empty]

## Key Decisions
- **[Decision]**: [Brief rationale] (carry decisions that still bind; drop ones a later decision overruled)

## Next Steps
1. [Update based on current state]

## Critical Context
- [Context still needed to continue; drop what is settled. Prefix each still-open question with `UNRESOLVED:`]

## Additional Notes
[Other important info not fitting above]

You MUST output only the structured summary; you NEVER include extra text.

Sections MUST be kept concise. You MUST preserve relevant tool outputs/command results. You MUST include repository state changes (branch, uncommitted changes) if mentioned. A shorter summary that keeps every live thread is BETTER than a longer one padded with settled history.
