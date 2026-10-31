# CLAUDE.md

This project uses a delegated workflow: Opus plans and decides, Haiku researches, Sonnet writes and reviews the code.

## Who this applies to
- **Main session (Opus):** you are the orchestrator. Follow this whole file.
- **Subagents (`researcher`, `implementer`, `reviewer`):** only **Project facts** applies to you. Your role instructions are in your agent definition; ignore the rest of this file.

## Settings
- **Plan approval: on.** After planning, show the user the plan and wait for a go-ahead. Off: build straight through.
- **Commits: on.** Commit each approved feature locally. Never push. Off: leave changes uncommitted.
- **Review rounds before Opus steps in: 3.**

## Project facts
Shared with every agent. The first research pass fills in anything marked *unknown*; update it here and tell the user.
- Stack: Python 3.12/3.13 web app in `subzero_ai/`: Starlette + Uvicorn, Jinja2 templates (`subzero_ai/templates/`), plain JS/CSS (`subzero_ai/static/`), SQLite (`subzero_ai/db.py`, append-only `MIGRATIONS` list keyed by `PRAGMA user_version`). Hand-written HTTP clients, no vendor SDKs: Claude Messages API in `subzero_ai/claude.py`, Slack Web API in `subzero_ai/slack_api.py`, Slack Socket Mode over `websockets` in `subzero_ai/socket_mode.py`. Runs as a Windows service (WinSW, embedded Python) on a mini PC behind a Cloudflare Tunnel to `http://127.0.0.1:8787`.
- Build: `python packaging/build_windows.py` makes the Windows release zip (every download pinned by SHA-256, Python wheels from `requirements-lock.txt`). GitHub Actions minutes are used up, so don't rely on CI; build and test locally.
- Test: `pip install -r requirements-dev.txt`, then `python -m unittest discover -s tests -t .` (226 tests, about 20 s). Tests use `FakeClaude` (`subzero_ai/claude.py`) and fake Slack clients from `tests/helpers.py`; no network or keys needed.
- Lint/format: none configured. Match the surrounding style (type hints, `from __future__ import annotations`, 120-column lines, short docstrings).
- Conventions: every AI call goes through one client object with `stream()` and `complete()` (built in `build_services`, `subzero_ai/web.py`). Settings are a dataclass in `subzero_ai/config.py` (from `config.env`), overridable from the admin page via `subzero_ai/saved_settings.py`. Important actions are written with `db.audit(...)`. User-facing text is plain language for high-school students. `docs/DESIGN.md` is the visual standard. `FAKE_CLAUDE=1` gives canned replies, with magic words (ALERT-TEST, ERROR-TEST, SCREEN-FLAG, SCREEN-ERROR) for testing.
- Off-limits: don't weaken the mentor approval flow (approval window, screener, MENTOR ALERT, rejections) without the user's say-so. Keep the team's name and number off the site; say "the team". Never put API keys or tokens in code, tests, commits, or chat. Users install from `SubZeroAI-windows.zip`, never GitHub's source zip.

---

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
- A step is destructive or hard to undo: deleting data, migrations on real data, pushing, deploying, publishing, rewriting git history.
- A feature is still failing after your own fix attempt.
- All features are done.

Before stopping, update PLAN.md, then give a short status (done, remaining, and the specific question with options and your recommendation) and wait.
