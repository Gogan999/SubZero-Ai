# Decisions

- D1 (F2, F3): Keep the Python app and run Pi in a Node.js child process the service starts and talks to over stdin/stdout JSONL — Pi is TypeScript-only, a rewrite would risk the approval flow, and stdio needs no new port or shared secret.
- D2 (F2): API-key providers go straight through `@earendil-works/pi-ai`, not the agent runtime — it sends our exact system prompt with no tools and no agent prompt, and it's stateless like our SQLite history.
- D3 (F5): `claude-bridge/...` models run in an in-memory `pi-coding-agent` SDK session with no tools and only the bridge extension — extensions only load in the agent runtime, and no tools keeps students away from shell or file access.
- D4 (F1): Keep `ClaudeClient` as `AI_BACKEND=anthropic`, the default until Pi has run well on the mini PC, and keep `FakeClaude` for tests — gives a one-setting rollback.
- D5 (F4): Model IDs become `provider/model`, with a migration that prefixes existing rows with `anthropic/` — one unambiguous ID across providers; the Fable/Mythos block stays.
- D6 (F1-F8): Approval, the window, the screener, MENTOR ALERT, limits and rejections stay above the backend and don't change; the screener and titles also run through the chosen backend — the safety flow is model-independent by construction.
- D7 (F2, F6): Pin exact npm versions with a lockfile, install with `npm ci --ignore-scripts`, and bundle the official Node.js 22 LTS zip checked against a pinned SHA-256 — matches the existing build's pinning policy.
- D8 (F5): The bridge provider is off by default and only an admin can turn it on — pending Q1 on subscription terms.
- D9 (S1): Slack DMs go through `ApprovalService.submit` like web messages, with no bypass — keeps one approval flow.
- D10 (F2, F3): Python tests use a fake gateway written in Python; the gateway has its own `node --test` suite with pi-ai's `fauxProvider` — the main suite stays fast and needs no Node or keys.
