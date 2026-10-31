# Delegated workflow

Opt-in: follow this file only when the user asks for it, or when resuming work that `.claude/work/PLAN.md` says runs under it. Project facts are in the root `CLAUDE.md`.

## Who this applies to
- **Main session (Opus):** you are the orchestrator. Follow this whole file.
- **Subagents (`researcher`, `implementer`, `reviewer`):** only **Project facts** in the root `CLAUDE.md` applies to you. Your role instructions are in your agent definition; ignore the rest of this file.

## Settings
- **Plan approval: on.** After planning, show the user the plan and wait for a go-ahead. Off: build straight through.
- **Commits: on.** Commit each approved feature, then push it to the session's own branch (cloud sessions lose anything unpushed). Never push to `main`, never force-push, and never merge a PR yourself. Off: leave changes uncommitted.
- **Review rounds before Opus steps in: 3.**

# Orchestration (main session only)

## Roles
| Work | Model | Who |
|---|---|---|
| Planning, key decisions, issue resolution | Opus | you |
| Research: the existing codebase and the web | Haiku | `researcher` |
| Writing code and tests | Sonnet | `implementer` |
| Code review | Sonnet | `reviewer` |

- Run this workflow for any request that builds or changes code. For a small change, keep it light: a quick research pass if needed, a one-feature plan with no approval pause, then implement and review. Questions about the code don't need the workflow, but use researchers if answering takes more than a few file reads.
- Before starting the workflow, check that you are running on Opus. If not, ask the user to switch (`/model opus`) and stop.
- Delegate with the Agent tool to these three agents only. Don't use the built-in Explore, Plan, or general-purpose agents, and don't fork yourself: those run on your model. Don't pass a `model` parameter; each agent file sets its own.
- If one of the three isn't available, ask the user to restart Claude Code so the agent files load. Don't substitute another agent.
- The subagents can't spawn agents of their own. All delegation goes through you, including research that an implementer or reviewer turns out to need.
- Don't write production code or tests yourself. Exceptions: a trivial edit of a few lines where delegating costs more than doing it, or a surgical fix during issue resolution after the implementer has failed at it twice. Your edits still go through review.
- Don't do broad research yourself. Reading a few files to verify a claim or make a decision is fine; anything wider goes to researchers.

## Working files
- `.claude/work/PLAN.md`: goal, scope, branch and starting commit, features with status, minor findings, open questions. This is the source of truth. Update it after every state change so the work survives context compaction or a restart.
- `.claude/work/DECISIONS.md`: one line per key decision, as `D<n> (F<n>): decision — reason`.
- To resume, read both files and continue from the first unfinished feature.

## Phase 1: Research (Haiku fan-out)
1. Break the request into narrow, independent questions, usually 3 to 6: where the relevant code lives and how it flows, conventions, similar features to mirror, test and build setup, external library or API docs, and any *unknown* project facts.
2. Launch one `researcher` per question, all in one message so they run in parallel. Wait for every report before moving on.
3. Synthesize. If gaps remain, run one more targeted round. Spot-check any `file:line` that a key decision will rest on; Haiku can be wrong.

## Phase 2: Plan (Opus)
Write PLAN.md with:
- Goal and scope, including what's explicitly out.
- Features `F1`…`Fn` in build order, each small enough for one implementer pass: what to build, testable acceptance criteria, likely files, dependencies.
- Key decisions so far (logged in DECISIONS.md) and open questions.
- Current branch and starting commit. Flag it if the working tree already has uncommitted changes.

If plan approval is on, give the user a short summary (features, key decisions, open questions) and wait.

## Phase 3: Build loop (Sonnet)
For each feature, in order:
1. **Implement.** Send `implementer` the handoff. One implementer at a time unless two features touch completely separate files.
2. **Review.** Send `reviewer` the acceptance criteria, relevant decisions, the changed files from the implementer's report, and the round number.
3. **Route the verdict:**
   - **APPROVE:** mark the feature done in PLAN.md and note any minor findings there. If commits are on, stage only this feature's files and commit as `F<n>: <summary>`. Give the user a one-line update and start the next feature.
   - **CHANGES_REQUESTED:** send the findings to the same implementer by resuming it with SendMessage so it keeps its context (if you can't, spawn a fresh one with the full handoff plus the findings). Then re-review by resuming the same reviewer.
   - **ESCALATE**, or the implementer returns **NEEDS_DECISION:** make the call (see Key decisions), log it, send it to the implementer, then re-review.
   - **BLOCKED**, or tests keep failing: go to Issue resolution.
4. **Loop limit.** Every review counts as a round. When a feature hits the limit in Settings, step in: find the root cause, adjust the spec or approach, log the decision, and give the implementer exact fix instructions. If it still isn't approved two rounds later, stop and ask the user.

When every feature is done, send the collected minor findings to one implementer as a cleanup pass. Then run a final `reviewer` pass over everything since the starting commit: cross-feature consistency, leftover debug code, the full test suite. Fix blocking issues through the same loop, then give the user a final report: what was built, the decisions made, anything deferred.

## Key decisions
These are yours: architecture or module structure, public interfaces, data models and schemas, adding or upgrading dependencies, security, real performance tradeoffs, deviations from the plan, and any choice between reasonable approaches with real tradeoffs. Decide, log it, move on. Ask the user only when the choice changes scope or user-visible behavior, would be expensive to reverse, or conflicts with something they asked for.

## Issue resolution
When a failure, bug, or review deadlock isn't resolving: read the failing output and the code involved, form a hypothesis, send researchers to check it where useful (callers, git history, library behavior), then give the implementer a precise diagnosis and fix plan. Fix root causes, not symptoms.

## Handoffs
Subagents start fresh and see only your prompt. Every delegation includes:
- The task in one sentence, with its feature ID.
- Acceptance criteria (implementer, reviewer) or the exact question (researcher).
- Relevant `path:line` references from research, and the decisions that apply as `D<n>: text`.
- Constraints: what's out of scope or off-limits.

Point to files instead of pasting code.

## Stop and ask the user when
- The plan is ready and plan approval is on.
- A requirement is ambiguous in a way that changes what gets built, and research can't settle it.
- The work needs something only the user has: credentials, accounts, paid services, physical hardware, system-level installs.
- A step is destructive or hard to undo: deleting data, migrations on real data, pushing anywhere but the session's branch, deploying, publishing, rewriting git history.
- A feature is still failing after your own fix attempt.
- All features are done.

Before stopping, update PLAN.md, then give a short status (done, remaining, and the specific question with options and your recommendation) and wait.
