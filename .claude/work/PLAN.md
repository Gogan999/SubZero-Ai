# Plan: Code-only pivot, v0.11.0

Status: **approved by Logan 2026-11-14; build running** (thread "Build Code-only pivot v0.11.0") under
`.claude/WORKFLOW.md`. Plan approval is already given, so build straight through.

- Source of truth for scope: the plan doc https://claude.ai/artifact/QhxREwgrj1b2nLyX2UadYK ("SubZero AI:
  Code-only pivot plan"). This file is the build breakdown.
- Branch: `claude/project-thread-zyny6p`. Starting commit: `cf8005c` (main, v0.10.0), clean tree.
- Baseline: 1042 Python tests (74 s), 46 gateway tests, all passing.
- One pull request, one release. Sessions can't push tags; Logan publishes.
- Orchestrator: Fable. `researcher` = Haiku, `implementer`/`reviewer` = Sonnet. Opus reviews the gateway and
  the whole diff at the end.

## Logan's decisions (2026-11-14)
- Website = Code + admin Settings. Slack DM chat with mentor approval stays unchanged.
- Mentor web Review page stays and opens a read-only chat page with the approve buttons. Students get no chat pages.
- Removed from the site: projects, artifacts/explainers, share links (existing links die), attachments.
- Plan-first approval replaces step-by-step mentor clicks in the new engine.
- Code sign-in is a setting: API key, or "Team subscription (testing only)" with a permanent warning. No email to Anthropic.
- 24 GB RAM: two Code sessions at once, a third waits.
- Files page for mentors and admins: list and delete old files; any mentor may delete; confirmed, audited; running sessions protected.
- **Everything ships ENABLED by default**, including the new engine. Keep a clear Settings switch back to the classic engine.
- Never call the feature "Claude Code". Keep the team's name and number off the site.

## Rules that must still hold (each needs a passing test at the end)
1. A student's DM reaches Claude only after a mentor approves it, or inside an open approval window when the screener passes it.
2. A rejected or held message never reaches Claude, and only the student sees the reason.
3. A MENTOR ALERT posts to the approvals channel.
4. Waiting messages trigger reminders; per-student limits and the always-review list apply.
5. A student Code session never starts without a mentor's approval, and neither does a follow-up.
6. Student Code sessions run only with the sandbox on.
7. A student can download a patch only after a mentor releases it, and sees only their own sessions.
8. The screener's note reaches the mentor on student Code requests.
9. The safety rules are in the system prompt of every DM reply and every Code session.
10. Guests, bots and people outside the workspace can neither sign in nor message the bot.
11. Every approval, rejection, release, setting change and deletion is written to the audit log.

## Features

Track A (website) runs in order. Track B (engine) starts in parallel with new files only, then joins.

| ID | Feature | Status |
|---|---|---|
| F1 | Untangle, change nothing: `web_common.py`, `prompts.py`, screener as its own service, GitHub split from Drive | done f6416be |
| F2 | Rule tests through Slack (`tests/test_rules.py`), old web tests still present | todo |
| F3a | Remove projects, share links, artifacts/explainers, Drive, uploads/attachments, composer tools | todo |
| F3b | Remove web chat pages; read-only mentor chat page + Review; redirects; Slack wording; removed settings; test triage | todo |
| F4 | Code interface: shell/sidebar, Code home, "Needs a mentor", session states, on-page diff, About, role fallback fix | todo |
| F5 | Settings reorganised + `archive.py` (archive download, delete old website chats) | todo |
| F6 | Files page for mentors and admins | todo |
| G1 | `anthropic_gateway.py` (new file, parallel) | todo |
| G2 | `sandbox_runner/` Node runner on the Agent SDK + fake runner for tests (new files, parallel) | done |
| G3 | Engine driver `code_engine.py`, migration v17, engine settings, concurrency queue, defaults on | todo |
| G4 | Site for the new engine: transcript, plan-first, cost and cap, sign-in setting with warning | todo |
| G5 | Sandbox base image provisioning (tools + runner baked in, version check) | todo |
| H1 | Docs, CLAUDE.md, version 0.11.0 | todo |
| H2 | Verify: suites, upgrade/rollback, walk-through, screenshots, zip, whole-diff review | todo |

### F1 Untangle (no behaviour change)
- New `subzero_ai/web_common.py` holding what every web module shares (Services, build_services may stay in web.py if
  moving it creates cycles; at minimum: services(), csrf, current_viewer, is_local_request, page, render, error_page,
  fmt_time, waited, json_error, short_text, security middleware). `web_code.py` and `web_admin.py` import from
  `web_common`, not `web`. `web.py` re-exports the moved names so existing imports and tests keep working.
- New `subzero_ai/prompts.py` with `load_safety_prompt`, `load_system_prompt`, PROMPT_FILES; `generation.py` re-exports.
- Screener built once in `build_services` and held on `Services.screener`; approvals and Code use that one object.
- `connectors.py`: GitHub and Drive separable (Drive goes in F3a).
- Accept: all 1042 tests pass with **no test file edited**; `node --test` untouched.

### F2 Rule tests through Slack
- New `tests/test_rules.py`: one or more tests per rule 1-11. Rules 1-4 driven only through fake Slack DMs
  (`SlackDMs`) and card taps (`SlackInteractions`), never through `/c/...` or `/chats/new`. Gaps today: reminders,
  daily approved limit, always-review list have no Slack-driven test. Rules 5-11 may call existing Code/auth helpers.
- A harness helper to seed a chat by Slack DM (used later by F3b to rewrite tests).
- Accept: new tests pass next to the old ones; no production code change unless a real bug is found (report it).

### F3a Remove projects, shares, artifacts, Drive, uploads
- Delete `projects.py`, `shares.py`, Drive half of `connectors.py`, upload half of `attachments.py` (keep what
  approvals/slack_cards/generation/Files page need to read old files), routes and templates for projects, artifacts,
  explainers, shares, `/files` uploads, composer tools (research/thinking/style per message), GitHub file picker for chat.
- `/s/{token}` and `/s/{token}/x/...` return a plain "This link is no longer available" page (HTTP 410).
- formats prompt: tell Claude not to make interactive explainers; drop EXPLAINER_REQUEST path. Slack cards drop the project line.
- generation.py keeps working for chats that have old attachments/projects rows (ignore them; never crash).
- No table dropped. Tests for removed features deleted; anything testing a rule is rewritten, not deleted.

### F3b Remove web chat; mentor read-only chat; Review
- Remove: `/chats`, `/chats/new`, `/recents`, `/c/{id}/messages`, `/c/{id}/events`, `/c/{id}/m/{mid}`, `/c/{id}/model`,
  `/c/{id}/delete`, chats/recents/conversation templates, composer, chat JS/CSS, sidebar Recents, profile preferences
  (subteam, DM-me), `_notice.html` chat disclaimer on non-chat pages.
- Keep `/queue`, `/queue/items`, `/queue/events`, `/m/{mid}/{approve,reject,hold,withdraw,retry}` with unchanged
  authorisation in `ApprovalService`.
- New read-only `/c/{id}` for mentors and admins only: whole chat, Approve / Edit / Reject / Handle privately on the
  waiting message, no message box, no list. Students get a redirect to `/code` with a one-time note
  "Chats have moved to Slack. Message the bot there." Slack cards and MENTOR ALERT link to `/c/{id}`.
- Old addresses `/chats`, `/recents`, `/projects...`, `/artifacts...` redirect to `/code` with that note.
- `/` and post-login go to `/code`.
- Slack wording (slack_dm.py:294, 297, 334, 107, 420; slack_cards.py) no longer sends students to the website.
- Settings removed from the page and ignored if stored: max_tokens_explainer, max_text_file_kb, subteams,
  show_pending_to_team, show_rejected_to_team, connector_drive_links, research_for_students. Unknown/old keys in
  config.env or the settings table never stop start-up.
- Test triage: every web-chat test is kept, rewritten through Slack, or deleted with its feature. Table in
  `.claude/work/TRIAGE.md`. No rule loses its only test.

### F4 Code interface
See plan doc "UX: the Code interface and Settings". Sidebar per role, Code home with one box, "Needs a mentor"
list with count in sidebar and tab title, session page states (waiting / starting / working / step needs approval /
done / declined-stopped-failed) with self-refresh that never wipes a half-typed follow-up, on-page diff in a Changes
panel, About rewritten, Profile = appearance only, audit filters (who, what, date), role fallback (keep last known
mentor list on disk when the Slack read fails at start-up, warn admins). docs/DESIGN.md is the standard.

### F5 Settings + archive
Tabs: Overview, Code (new), AI and keys, Slack chat review, People, Slack, Site and server, History. Advanced folds,
one restart banner on every tab, unsaved-changes warning, read-only mentor/admin list, waiting-messages row with
"post cards again", workspace keep-days setting, `archive.py` (zip archive of old website chats to the backups folder;
"Delete old website chats" = backup first, typed confirmation word, only chats with no `slack_threads` row, audited).

### F6 Files page
`/files` for mentors and admins: totals per kind and free disk space, list with kind/owner/size/age, sort and filter,
view pictures/text, delete one / ticked set / all of a kind older than N days. Confirm names count and size; more than
20 items needs the number typed. Audited. Running sessions and their sandboxes can't be deleted. Kinds: old chat
attachments, old project files, Code workspaces, sandbox disks, old-chat archives. Shows the automatic clean-up rules.

### G1 Gateway (`subzero_ai/anthropic_gateway.py`)
HTTP server on `sandbox_host_address:sandbox_gateway_port` only. See DECISIONS D60-D66.

### G2 Runner (`sandbox_runner/`)
Node 22 script on `@anthropic-ai/claude-agent-sdk` 0.3.316 (same pin as pi_gateway). JSON lines on stdin/stdout.
See D67-D70.

### G3 Driver
`code_engine.py`; `CodeRunner` picks the engine per session; migration v17; settings; queue; defaults. See D71-D80.

### G4 Site for the engine
Transcript shows shell commands, plans, task lists, sub-agents; plan-first switch on the start form; plan approval
card; cost and cap in the header; Settings: Code engine switch, Code sign-in with permanent warning.

### G5 Base image
Provisioning that puts Node, JDK, Python, git and the runner in the base disk; version check at session start with a
plain message when the image is too old.

## Minor findings (for the clean-up pass)
(none yet)

## Open questions / for Logan at the end
- Mini PC steps (image, sign-in token, sandbox test, deploy).
