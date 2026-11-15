# Features to implement

Since version 0.11 the website is **Code plus the admin Settings page**, with the mentors' Review page, a read-only
chat page for mentors, "Needs a mentor", and a Files page. Chatting happens in Slack DMs with mentor approval. The
old claude.ai-style web chat, projects, artifacts, share links, uploads and the "Soon" placeholders are gone, and the
ideas that only made sense for them were dropped from this list.

This file is the backlog. When a feature ships, move it to "Done" at the bottom.

Every feature here must keep the mentor approval flow: nothing a student writes reaches Claude until a mentor
approves it (or it passes inside the approval window), and a student's Code session or follow-up never starts without a
mentor's approval.

## Waiting for the mini PC

Nobody has run these on the real server yet. They are the first thing to do after installing 0.11.

- **Prepare the sandbox image** (`prepare-sandbox-image.cmd`) and read the 14 assumptions in `docs/SANDBOX_IMAGE.md`.
- **Code sign-in with Anthropic**: the API key path and the team subscription token path are unverified against
  Anthropic until the first real session (decision D65).
- **Plan-first approval** in the Agent SDK version we pin (0.3.316): how the plan is held and released is unverified (D68).
- **Run one mentor session, then one student session** with a mentor approving, and watch cost and the cap.
- **The sandbox network and its firewall rules** (`docs/CODE_SANDBOX_PLAN.md`, "M3b results") including the new gateway port.

## Ideas for Code

- **Open the patch as a pull request** with a token that can write (today a mentor downloads the patch).
- **Mentor dashboard**: approval times, session cost per week, busiest hours.
- **Stop a running session** from the "Needs a mentor" list.
- **Rename or hide old sessions** from the Code home.
- **Voice or photo input** for a request (a photo of the mechanism). Needs a privacy check first.
- **Download a session's transcript** for the engineering notebook.

## Ideas for Slack chat

- **Files in Slack DMs** (the bot says "I can't take files yet").
- **Review-card labels** may still say "Web search" on a pending message after a model switch drops that feature.
- **Memory**: facts about the team's robot that Claude remembers across chats, edited by mentors like the prompt layers.
- **Real-API tests** for OpenAI and OpenRouter models through the Pi gateway (only Anthropic has been tried live).
- **Bridge spike on the mini PC:** run `docs/BRIDGE_SPIKE.md` before turning on the Claude subscription provider for students.

## Known small things

- The session page on phones scrolls to the bottom on load and can hide the title.
- The Code home links to Settings; it should link to the Code tab.

## Done

- Slack DMs (0.6): people message the bot in Slack, each top-level DM starts a chat and its thread continues it; every
  DM goes through `ApprovalService.submit` and the reply is posted back in the thread.
- AI providers (0.5): `AI_BACKEND` (anthropic, pi, fake), the Node.js Pi gateway, `provider/model` ids, the model
  safety check, the optional Claude subscription bridge, Windows packaging with Node.js.
- Approval window and screener (0.8), Settings page with eight tabs, health list, backups, update check (0.4 to 0.10).
- Code sandboxes (0.9): a disposable Hyper-V VM per session, the egress proxy, students' Code sessions with mentor
  approval and patch release (`docs/CODE_SANDBOX_PLAN.md`).
- **0.11, Code only:**
  - Website reduced to Code and Settings; projects, artifacts and explainers, share links (they now say "no longer
    available"), attachments and uploads, web chat and the composer tools removed. Nothing in the database was dropped.
  - New Code interface: sidebar, Code home with one box, session transcript, Changes panel with the diff, "Needs a mentor".
  - New Code engine (`code_engine.py`, `sandbox_runner/`, `anthropic_gateway.py`): the Agent SDK inside the sandbox,
    plan first, sub-agents, web tools, a cost cap per session, two sessions at once, and a Settings switch back to the
    classic engine. Code sign-in is an API key or "Team subscription (testing only)".
  - Sandbox image provisioning (`prepare-sandbox-image.cmd`) with a version check at session start.
  - Settings reorganised into eight tabs, with `archive.py` (zip archive of old website chats, and a backed-up delete).
  - Files page for mentors and admins (`files_admin.py`, `web_files.py`).
  - The mentor Review page and a read-only chat page with the approve buttons stay; students get no chat pages.
