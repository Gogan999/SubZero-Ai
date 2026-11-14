# Sandbox runner

Small Node 22 program that runs the Claude Agent SDK for a Code session. It
lives inside the sandbox VM and is never run on the host.

The host starts it over SSH (`node /opt/subzero-runner/runner.mjs`) and talks
JSON lines on stdin/stdout. The host sets `ANTHROPIC_BASE_URL` and
`ANTHROPIC_AUTH_TOKEN` for that run, so the runner never sees a real key.
`runner.mjs --version` prints `{runner_version, sdk_version}`.

## Protocol (one runner process per run)

Host to runner: `start` (`task`, `model`, `cwd`, `resume?`, `plan_first`,
`allowed_web`, `sub_agents`, `max_turns`, `append_system`), `message` (`text`),
`plan_decision` (`approve`, `note`), `stop`.

Runner to host: `ready`, `session`, `text`, `tool_start`, `tool_end`, `plan`,
`todo`, `compact`, `turn_done`, `error`.

After `turn_done` the runner waits for another `message` until stdin closes or
`stop`. Mapping lives in `lib/events.mjs`, option building in `lib/options.mjs`,
the loop in `lib/run.mjs` (the SDK `query` is injected, so tests stub it).

## Install into the sandbox image

Copy this folder to `/opt/subzero-runner`, then `npm ci --omit=dev` there.

## Tests

`node --test` (no `npm ci` needed; the SDK is stubbed).
