<!-- CCB_CONFIG_START -->
## AI Collaboration
Use `/ask <agent>` to contact another CCB agent by name.
Use `/ping <agent|ccbd>` to inspect project control-plane health.
Use `/pend <agent|job_id>` to inspect mailbox/job replies.

Agent names come from `.ccb/ccb.config`. Providers are implementation details.

## Async Guardrail (MANDATORY)

When you run `ask` (via `/ask` skill OR direct `Bash(ask ...)`) and the output contains `[CCB_ASYNC_SUBMITTED`:
1. Reply with exactly one line: `<Agent> processing...` (use the target agent name)
2. **END YOUR TURN IMMEDIATELY** — do not call any more tools
3. Do NOT poll, sleep, call `pend`, check logs, or add follow-up text
4. Wait for the user or completion hook to deliver results in a later turn

This rule applies unconditionally. Violating it causes duplicate requests and wasted resources.

<!-- CCB_ROLES_START -->
## Role Assignment

Abstract roles map to concrete agents defined by the current project layout. Skills reference roles, not providers directly.

| Role | Agent | Description |
|------|-------|-------------|
| `designer` | `<agent-name>` | Primary planner and architect — owns plans and designs |
| `inspiration` | `<agent-name>` | Creative brainstorming — provides ideas as reference only |
| `reviewer` | `<agent-name>` | Scored quality gate — evaluates plans/code using Rubrics |
| `executor` | `<agent-name>` | Code implementation — writes and modifies code |

Role assignment authority: `.ccb/ccb.config` in the current project is the single source of truth.
The table above is illustrative only. When a skill references a role (e.g. `reviewer`), resolve it from `.ccb/ccb.config`.
<!-- CCB_ROLES_END -->

<!-- REVIEW_FRAMEWORK_START -->
## Peer Review Framework

The `designer` MUST send to `reviewer` (via `/ask`) at two checkpoints:
1. **Plan Review** — after finalizing a plan, BEFORE writing code. Tag: `[PLAN REVIEW REQUEST]`.
2. **Code Review** — after completing code changes, BEFORE reporting done. Tag: `[CODE REVIEW REQUEST]`.

Include the full plan or `git diff` between `--- PLAN START/END ---` or `--- CHANGES START/END ---` delimiters.
The `reviewer` scores using Rubrics defined in `AGENTS.md` and returns JSON.

**Pass criteria**: overall >= 7.0 AND no single dimension <= 3.
**On fail**: fix issues from response, re-submit (max 3 rounds). After 3 failures, present results to user.
**On pass**: display final scores as a summary table.
<!-- REVIEW_FRAMEWORK_END -->

<!-- INSPIRATION_CONSULTATION_START -->
## Inspiration Consultation

For creative tasks (UI/UX design, copywriting, naming, brainstorming), the `designer` SHOULD consult `inspiration` (via `/ask`) for reference ideas.
Do not blindly apply suggestions. Exercise independent judgment and present options to the user for decision.
<!-- INSPIRATION_CONSULTATION_END -->

<!-- CCB_CONFIG_END -->
