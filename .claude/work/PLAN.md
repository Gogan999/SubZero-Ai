# Plan: move AI access onto the Pi harness

Status: **approved by Logan 2026-10-31; build running (thread "Build the Pi harness backend").** Build in a fresh session so the `researcher`/`implementer`/`reviewer` agents load, under `.claude/WORKFLOW.md`. Start at F1 (from PR #8's head if #8 is still open).

- Branch: `claude/project-thread-lcsb78` (build). Planning was on `claude/project-thread-ntchvl` (PR #10, merged).
- Starting commit: `9a36654` = PR #8's head `4eb3601` merged with main `aaf4aa1`, clean tree, 260 tests passing. PR #8 then merged (main `47f59c0`, same tree), merged in as `07cc76d`.
- Subagents: the custom agent files don't load in this cloud session, so each role runs as the general agent pinned to its model (researcher=Haiku, implementer/reviewer=Sonnet) with the role file as its instructions (D14).
- Runs under the delegated workflow in `.claude/WORKFLOW.md` (opt-in, see the root `CLAUDE.md`).
- **Sequencing:** PR #8 (projects, composer tools, Code sessions; branch `claude/project-thread-cg9ea7`) is still
  open and widens the AI client interface. Build F1 on top of main *after* PR #8 merges, so the seam covers
  its methods. If #8 is still open when Logan approves, F1 starts from #8's head instead.

## Goal

Today every AI call goes straight to Anthropic's Messages API through a hand-written client
(`subzero_ai/claude.py`). Move that onto the Pi agent harness (`@earendil-works/pi-ai` and
`pi-coding-agent`, from the pi-mono repo) so the app can use many model providers (Anthropic, OpenAI,
Google, OpenRouter, local models, and others), and add `pi-claude-bridge` as one more provider that
reaches Claude through Claude Code. The mentor approval flow must work exactly as it does now, whichever
model answers.

Stretch goal, lower priority: students and mentors can message the Slack bot directly, and those
messages go through the same approval flow.

## What the code looks like today (from research)

- One seam: `build_services` (`subzero_ai/web.py:176-190`) builds a single `claude` object
  (`ClaudeClient` or `FakeClaude`) and hands it to everything that calls a model.
- That object has two methods, `stream(...)` yielding `StreamEvent(kind=text|usage|error|done)` and
  `complete(...) -> str` (`subzero_ai/claude.py:95-210`). Callers:
  - replies: `subzero_ai/generation.py:182` (stream), titles: `generation.py:244` (complete)
  - pre-screen: `subzero_ai/screener.py:57` (complete)
  - health ping: `subzero_ai/health.py:98`, `check` command: `subzero_ai/checks.py:61-72`
- The full conversation history lives in SQLite and is re-sent on every request (`generation.py:56`),
  in Anthropic content-block form (text, `<file>` text blocks, base64 images). There is no
  server-side session state, which suits a stateless backend.
- Model IDs are bare Claude IDs: defaults in `subzero_ai/config.py:62-94`, admin validation by
  `MODEL_RE = claude-...` and a Fable/Mythos block list (`subzero_ai/saved_settings.py:24-27,150-155`),
  display names in `MODEL_LABELS` (`web.py:84`), and stored per conversation and per message
  (`conversations.model`, `messages.model` in `subzero_ai/db.py`). Mentors switch a chat between the
  default and hard-problem model (`web.py:704-717`).
- Effort (`low|medium|high`) is sent as `output_config.effort`; the system prompt is cached with
  `cache_control` (`claude.py:99-116`).
- PR #8 (not merged yet) adds Claude-only features on top of that client: `stream(..., web_searches=n)`
  using Anthropic's server-side `web_search` tool with citations; Research (up to 20 searches);
  extended thinking (`xhigh` effort); response styles (system-prompt text, so model-neutral); and
  Code sessions through a new `tool_turn(...)` method with client-side tools where a mentor approves
  every edit and command (`subzero_ai/web_code.py`).
- Approval, screener, MENTOR ALERT, limits and rejections all sit above the client
  (`subzero_ai/approvals.py`, `generation.py`), so they don't need to change.
- Slack: Socket Mode only handles `interactive` envelopes today (`subzero_ai/slack_handlers.py:27-38`).
  The manifest has the Messages tab read-only and no `im:history` scope (`slack-manifest.yaml`).

## What Pi offers (checked against pi-mono 1.4.0 and pi-claude-bridge 0.9.3)

- Pi is TypeScript and needs Node.js 22.19 or newer. It has no Python library.
- `@earendil-works/pi-ai` is a plain multi-provider LLM API: `stream`/`complete` over a context of
  system prompt plus messages, image input, unified thinking levels, usage with cost, a test
  `fauxProvider`, and Anthropic-style `cache_control` support.
- `@earendil-works/pi-coding-agent` is the agent runtime. It can be driven by its TypeScript SDK
  (in-memory sessions, `noTools`) or as a subprocess over JSONL (`pi --mode rpc`). Extensions, such as
  the bridge, only load inside this runtime.
- `pi-claude-bridge` is a Pi extension that registers `claude-bridge/<model>` models. It runs Claude
  Code through the Claude Agent SDK with Claude Code's own tools turned off (`tools: []`), using
  **Claude Code's login (a Claude Pro/Max subscription)**, not an API key. It wraps Pi's system prompt
  rather than replacing it, and Claude Code adds its own coding-agent system prompt on top.

## Design in one paragraph

Keep the Python app. Add a small Node.js helper ("Pi gateway", `pi_gateway/`) that the Python service
starts as a child process and talks to over stdin/stdout, one JSON object per line. The Python side
gets a new `PiClient` with the same `stream()`/`complete()` interface as `ClaudeClient`, so
generation, the screener, titles, health and `check` don't change. Inside the gateway, normal API-key
providers go straight through `pi-ai` (our exact system prompt, no tools, no agent prompt), and
`claude-bridge/...` models go through an in-memory `pi-coding-agent` SDK session with no tools and
only the bridge extension loaded. `ClaudeClient` stays as a fallback backend until Pi has run well on
the mini PC.

## Scope

In: the backend seam, the Pi gateway, `PiClient`, provider-qualified model IDs and the admin settings
for them, provider keys, the bridge provider, Windows packaging of Node and the gateway, docs, and a
regression suite showing the approval flow behaves the same on every backend.

Out (for now): giving models tools (web search, code execution, file access); Pi sessions,
compaction or branching (our SQLite history stays the source of truth); per-student model choice;
changing the approval rules; Slack DMs until F1-F8 are done.

## Features (build order)

Each is sized for one implementer pass. Status: F1 **done** (approved round 1, commit 1b908ee); F2 **done** (approved round 2); F3 **in progress**; the rest **not started**.

### F1. Backend seam and `AI_BACKEND` setting
- Build: a small `AIBackend` protocol (the existing `stream`/`complete` signatures, PR #8's
  `web_searches` argument and `tool_turn`, plus `StreamEvent`) and a per-model capability record
  (`web_search`, `tools`, `thinking`, `images`, D11), a factory used by `build_services` and `checks.py`, and a new setting
  `AI_BACKEND = anthropic | pi | fake` (default `anthropic`; `FAKE_CLAUDE=1` keeps working as an alias
  for `fake`). No behavior change.
- Acceptance: all existing tests pass unchanged; new tests show the factory picks the right client
  for each value and rejects unknown ones with a clear `check` message.
- Files: `subzero_ai/claude.py` (or a new `subzero_ai/ai_backend.py`), `web.py:176-190`, `checks.py`,
  `config.py`, `config.example.env`.

### F2. Pi gateway (Node.js), API-key providers
- Build: `pi_gateway/` with `package.json` + `package-lock.json` pinning exact versions of
  `@earendil-works/pi-ai`; `src/gateway.ts` (or plain `.mjs` to skip a build step) reading JSONL
  requests `{id, op: "stream"|"complete"|"abort"|"models"|"ping", model: "provider/id", system,
  system_extra, messages, max_tokens, thinking}` and writing JSONL events `{id, kind: "text"|"usage"|
  "error"|"done", ...}`. Converts our Anthropic-style content blocks (text, `<file>` text, base64 image)
  to pi-ai messages. System prompt is cached where the provider supports it. Provider keys come from
  environment variables the Python side passes in; nothing is written to Pi's own `~/.pi` auth file.
  Splits stdout only on `\n` as Pi's docs require.
- Acceptance: `node --test` suite using pi-ai's `fauxProvider` covers streaming, complete, abort mid
  stream, provider error, unknown model, image input, and usage/cost mapping; the gateway never
  prints anything but protocol records on stdout.
- Files: new `pi_gateway/`.
- Depends on: D1, D2, D7.

### F3. `PiClient` in Python
- Build: `subzero_ai/pi_client.py` that starts and supervises the gateway (restart with backoff if it
  dies, kill it on shutdown), multiplexes requests by id, implements `stream`/`complete`, sends
  `abort` when a stream's consumer stops, applies timeouts, maps effort to Pi thinking levels, and
  maps Pi usage (`input`, `output`, `cacheRead`, `cacheWrite`, `cost`) onto the keys generation already
  stores. Implements PR #8's `tool_turn` through pi-ai's tool calling, and `web_searches` only for
  models whose capability record has `web_search` (D11). Errors come back as the same plain-language messages `ClaudeClient` uses today.
- Acceptance: tests drive `PiClient` against a fake gateway script written in Python (so the Python
  tests need no Node), covering streaming, complete, abort, gateway crash and restart, timeout, and
  malformed lines. With `AI_BACKEND=pi`, `generation.py`, `screener.py`, titles, health and `check`
  work unchanged.
- Files: new `subzero_ai/pi_client.py`, `tests/test_pi_client.py`, factory from F1.
- Depends on: F1, F2's protocol (not its code).

### F4. Provider-qualified model IDs and provider keys
- Build: model IDs become `provider/model` (for example `anthropic/claude-sonnet-5-5`). A database
  migration rewrites `conversations.model`, `messages.model` and saved model settings from bare
  `claude-...` to `anthropic/claude-...` (and the `anthropic` backend strips the prefix). Admin
  validation accepts `provider/id` only for providers the admin has turned on; the Fable/Mythos block
  stays. Display names come from the gateway's `models` op, falling back to `MODEL_LABELS`. The admin
  page gets one key field per enabled provider, stored the way the Anthropic key is stored today
  (`web_admin.py:342-354`, never shown again after saving). `check` pings each configured model.
- Acceptance: migration test on a copy of a v0.4 database; validation tests for allowed, unknown, and
  blocked IDs; the mentor model switch still only offers the default and hard-problem models.
- Files: `db.py` (new migration at the end of `MIGRATIONS`), `saved_settings.py`, `config.py`,
  `web.py`, `web_admin.py`, `templates/settings.html`, `checks.py`.
- Depends on: F3, Q2, Q3.

### F4b. Claude-only features on other models (PR #8)
- Build: the composer shows Web search and Research only when the chat's model has `web_search`
  (today: Anthropic Claude models); extended thinking maps to the model's highest Pi thinking level or
  hides when it has none; response styles work everywhere. Code sessions stay Claude-only at first
  (Q7). A mentor switching a chat to a model without a feature sees a plain note that it's off.
- Acceptance: tests for each feature × {Claude, non-Claude fake model}: hidden or shown correctly, and
  the server refuses a forced request for a missing feature.
- Files: `web.py` (`clean_options`), `templates/_composer_tools.html`, `generation.py`, `web_code.py`.
- Depends on: F1, F3, F4, Q7.

### F4c. Safety check before a model reaches students (D12)
- Build: an admin-run "Model safety check" (also `subzero-ai check --model provider/id`) that sends a
  fixed set of test prompts through the real system prompt and checks the answers: refuses the unsafe
  ones, writes `[MENTOR ALERT]` first for the wellbeing ones, follows the explainer format, and passes
  the screener's SAFE/REVIEW format if used as the screener. The result is stored with the model ID
  and date; admin validation refuses a model for the default, hard-problem or screener slot until it
  has a passing check (Anthropic models included, so the rule is the same for all).
- Acceptance: tests with a fake model that passes, one that misses the alert, and one that errors;
  the setting can't be saved for a failing or unchecked model.
- Files: new `subzero_ai/model_check.py`, `checks.py`, `saved_settings.py`, `web_admin.py`, settings template.
- Depends on: F3, F4.

### F5. Claude Code via `pi-claude-bridge` (Q1: available to everyone)
- Build: add `pi-claude-bridge` and `@earendil-works/pi-coding-agent` (exact versions) to the gateway.
  `claude-bridge/...` models run through an in-memory SDK session: `noTools`, no extensions except the
  bridge, an empty working folder under `DATA_DIR`, `CLAUDE_CONFIG_DIR` and `PI_CODING_AGENT_DIR`
  under `DATA_DIR`, our system prompt passed as Pi's custom prompt (the bridge refuses a replaced
  prompt), and prior turns seeded into the session. Claude Code's MCP servers stay blocked
  (`strictMcpConfig`, the bridge default). A `subzero-ai claude-login` command runs Claude Code's login
  for the service's config folder; `check` reports whether it's logged in. Admin toggle, off until the admin turns it on; once on, bridge models can be picked for students like any other model, after passing F4c. The admin page notes that bridge replies use the logged-in Claude subscription's quota, and shows a clear error when that quota runs out (the `anthropic` API-key path stays available as a fallback model).
- Acceptance: a spike first (one afternoon) confirms on the mini PC that a bridge reply honors our
  safety prompt and MENTOR ALERT format with no tool use, and measures its latency; then gateway
  tests with the bridge's query stubbed, and a manual test plan for the real login.
- Files: `pi_gateway/`, `subzero_ai/__main__.py`, `checks.py`, `docs`.
- Depends on: F2, F4, F4c.

### F6. Windows packaging
- Build: `packaging/build_windows.py` downloads the official Node.js 22 LTS Windows zip (pinned
  SHA-256), runs `npm ci --ignore-scripts --omit=dev` for `pi_gateway/` against the lockfile, and puts
  both in the release zip. Install/update/rollback scripts carry the new folders; the service account
  gets read access to them; `check` verifies Node's version.
- Acceptance: a zip built in the cloud container installs on a clean Windows VM or the mini PC, the
  service starts the gateway as `NT SERVICE\SubZeroAI`, and `check` passes with a real key. (Needs
  Logan or a Windows machine; GitHub CI isn't available.)
- Files: `packaging/build_windows.py`, `packaging/windows/scripts/*.ps1`, `packaging/windows/README.txt`.
- Depends on: F2, F3 (F5 adds the bridge's bundled Claude Code binary to the size).

### F7. Approval-flow regression suite
- Build: run the existing flow tests (`tests/test_flow.py`, `test_approval_window.py`,
  `test_slack_interactions.py`) against `FakeClaude`, `PiClient` + fake gateway, and the `anthropic`
  backend, so approval, the window, the screener, MENTOR ALERT and limits are shown to behave the same.
- Acceptance: the suite is parameterized by backend and passes for all three.
- Depends on: F3.

### F8. Docs and switch-over
- Build: README sections for providers, keys, model IDs, the bridge and its login, and how to switch
  `AI_BACKEND` back to `anthropic` if Pi misbehaves. Update `config.example.env`, the responsible-use
  page if it names Claude as the only model, and `docs/FEATURES_TODO.md`. Bump the version to 0.5.0.
- Acceptance: a mentor can follow the README from a fresh install to a working non-Claude model.
- Depends on: F1-F7, F4b, F4c.

### S1 (stretch, second priority). Direct messages to the Slack bot
- Build: turn the bot's Messages tab on for writing, add the `im:history` bot scope and the
  `message.im` bot event (Socket Mode delivers `events_api` envelopes, which
  `slack_handlers.py:27` ignores today). A DM from a team member is matched to their account
  (`roles.Directory`), and each top-level DM starts a "Slack" chat while replies in its Slack thread
  continue it (Q6). Every DM goes through `ApprovalService.submit` exactly like a web message: same
  window, screener, limits and mentor approval; the student sees "waiting for a mentor" in the thread.
  The reply is posted back in the thread (updated in a few chunks rather than streamed), with a link
  to the chat on the website for explainers and files. Guests and unknown users get a polite refusal.
  The chat model is the default model; mentors can switch it on the website.
- Acceptance: tests for DM → pending message → approve → reply in thread; rejection reaches only the
  student; MENTOR ALERT still fires; bot's own messages and edits are ignored; duplicate event
  deliveries don't double-submit. README "Slack permissions" updated; Logan reinstalls the app.
- Files: `slack-manifest.yaml`, `slack_handlers.py`, `socket_mode.py`, new `subzero_ai/slack_dm.py`,
  `approvals.py` (a hook to post replies to Slack), README.
- Depends on: F1-F3 so DMs can use any model; could be built on the current backend if Logan wants it
  sooner.

## Key decisions

See `DECISIONS.md` (D1-D12).

## Open questions for Logan

- **Q1. Claude subscription through the bridge. ANSWERED 2026-10-31: Logan chose "bridge for everyone"** (students' chats may run on the subscription). He accepted the terms risk described here. The bridge uses a Claude Pro/Max login.
  Anthropic's Agent SDK docs say: "Unless previously approved, Anthropic does not allow third party
  developers to offer claude.ai login or rate limits for their products, including agents built on the
  Claude Agent SDK. Use the API key authentication methods... instead." Serving the whole team from one
  personal subscription also looks like account sharing under the consumer terms. Options:
  (a) build the bridge but keep it admin-only, for Logan's own testing, while students use API-key
  models (recommended); (b) skip the bridge and reach Claude through pi-ai with the existing API key;
  (c) ask Anthropic for approval first. The PR review leaned toward (b); (a) only makes sense if
  you personally want to try Claude Code through Pi.
- **Q2. Which providers first?** Recommendation: Anthropic (API key) plus one other, such as OpenAI or
  OpenRouter, and optionally a local model through Ollama. Every model has to pass the F4c safety check
  before students can use it (now a planned feature, D12).
- **Q3. Who picks models?** Recommendation: unchanged. Admins choose the default, hard-problem,
  title and screener models (now from any enabled provider); mentors switch a chat between default
  and hard-problem.
- **Q4. Cost limits across providers.** Limits today count messages, not dollars. Pi reports cost per
  reply; do you want a monthly spend cap per provider? Recommendation: show spend on the admin page
  now, add a cap later if needed.
- **Q5. ANSWERED 2026-10-31: Claude may push (to the session's branch only; WORKFLOW.md updated).** "Never push" in CLAUDE.md. In this cloud setup work only survives if it's pushed to the
  thread's branch and opened as a PR. Should the rule read "push only to the session's branch, never
  to main"?
- **Q6. Slack DMs (S1):** one chat per Slack thread (recommended), or one running chat per person?
- **Q7. PR #8's Claude-only features on other models.** Web search and Research use Anthropic's own
  search, so they'd be hidden on other models (recommended), or we add a separate search service later
  (more cost and keys). Code sessions: keep Claude-only (recommended, since the mentor-approved edit
  flow was tuned on Claude), or allow any model with tool calling after it passes F4c.

## Review feedback folded in (2026-10-31)

- The root `CLAUDE.md` loads in every session, so the delegated workflow is now opt-in: the root file
  holds only Project facts and a pointer; the workflow moved to `.claude/WORKFLOW.md`.
- Planned against PR #8's wider client interface (F1, F3, F4b, Q7).
- Safety check before non-Claude models reach students is now a feature (F4c, D12).
- Bridge: reviewer leans toward skipping it (Q1).

## Risks

- Node becomes a second runtime on the mini PC: bigger zip (~30 MB for Node, more with the bridge's
  Claude Code binary), a second process to supervise, and new npm dependencies to keep pinned.
- Non-Claude models may follow the safety prompt and MENTOR ALERT format less reliably (Q2 test gate).
- The bridge puts Claude Code's coding-agent prompt ahead of ours and adds latency per reply; the F5
  spike measures both before we commit to it.
- Prompt caching and effort behave differently per provider, so costs will shift.
- Running students' chats on one Claude subscription goes against Anthropic's stated Agent SDK policy, and the account could be limited or suspended (Logan's accepted risk, Q1). Keep an API-key model configured as a fallback.
- Claude Code's login has to be done for the Windows service account, which is awkward (F5 command).

## Minor findings

- F1-M1: `tests/test_ai_backend.py` protocol test only checks the methods exist; compare `inspect.signature` across ClaudeClient, FakeClaude, UnavailableClient and AIBackend.
- F1-M2: blank `AI_BACKEND=` counts as unknown and FAILs; treat empty/whitespace as `anthropic`, with a test.
- F1-M3: UnavailableClient's message reaches students; give students "The AI isn't set up right now. Tell a mentor." and keep the config.env detail for `check`/logs.
- F2-M3: pi-ai reads some env directly (AWS, Azure, Google ADC, PI_CACHE_RETENTION); bedrock/azure/vertex unsupported for now, documented in pi_gateway/README.md.
- F2-M5: CLAUDE.md test count is stale (226); update at the end.
- F2-M6: a user turn mixing tool_result and text/image becomes two consecutive user messages in pi-ai's body; optionally merge (tool results first).
- F1-M4 (no change needed now): setup step doesn't clear in-memory `fake_claude`; pre-existing, takes effect after restart.
