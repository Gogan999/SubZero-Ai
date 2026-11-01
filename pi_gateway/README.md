# pi_gateway

A small Node.js (22.19+) helper that the Python service runs as a child process. It talks JSON lines over
stdin/stdout and sends requests to AI providers through `@earendil-works/pi-ai` (pinned, exact version).
It runs no agent and no tools except those in a `tool_turn` request. Only protocol records are written to stdout;
logs go to stderr.

Run: `node gateway.mjs` (after `npm ci --ignore-scripts`). Keys come only from env vars `SUBZERO_KEY_<PROVIDER>`
(provider id upper-cased, other characters become `_`, e.g. `SUBZERO_KEY_OPENROUTER`). Closing stdin exits with code 0.

## Protocol (one JSON object per line)

Requests: `stream` / `complete` (`id, op, model "provider/model-id", system, system_extra, messages, max_tokens, effort`),
`tool_turn` (same plus `tools: [{name, description, input_schema}]`), `abort` (`id` of an in-flight request),
`ping`, `models` (optional `providers`). Messages use Anthropic shapes (text, base64 image, tool_use, tool_result blocks).

Records back: `text`, `usage`, `message` (tool_turn: Anthropic content blocks), `pong`, `models`, and
`error` with `code` one of `bad_request`, `unknown_model`, `no_key`, `provider`, `aborted`, `duplicate_id`.
Every request ends with exactly one `{"id", "kind": "done"}`. Bad JSON or a missing id gives `id: null`.

Effort: for Anthropic Messages models it is sent as `output_config.effort` without enabling extended thinking;
for other models with `reasoning` it becomes the thinking level; otherwise it is ignored.

## Claude subscription (`claude-bridge/<model>`)

Models named `claude-bridge/<model>` answer through Claude Code (a Claude Pro/Max login) using the `pi-claude-bridge`
extension, run inside an in-memory `@earendil-works/pi-coding-agent` session with no tools (`lib/bridge.mjs`). Off unless
env `SUBZERO_BRIDGE=1` (then `claude-bridge/...` gives `unknown_model`). When on, `PI_CODING_AGENT_DIR` must be
`<data dir>/bridge/pi-agent`, and `CLAUDE_CONFIG_DIR` should be `<data dir>/bridge/claude-config`. Rules:

- Each reply runs in a fresh empty folder `<data dir>/bridge/work/<id>`, deleted afterwards. New Claude Code session
  files under `CLAUDE_CONFIG_DIR` (projects, session-env, todos, ...) are deleted after each reply; the login file stays.
- `ANTHROPIC_*` is removed from the environment at start, `SUBZERO_KEY_*` are moved out of it (Claude Code inherits the
  environment), and `PI_OFFLINE=1`, `PI_SKIP_VERSION_CHECK=1` are set.
- Any tool call or tool execution stops the reply with `provider`: "Claude tried to use a tool, so this reply was
  stopped." (the event name goes to stderr). `tool_turn` gives `bad_request`.
- A session dropped after the abort grace time may still write files after the sweep; the next sweep removes them.
- Replies run one at a time (also across gateway processes, with `bridge/reply.lock`). No API key is needed; cost is reported as 0 (usage keys are still sent).
- The `models` op lists the bridge's models (with `images` / `reasoning`) when the bridge is on.

Not supported yet: Bedrock, Azure OpenAI and Google Vertex (pi-ai reads their credentials from the environment directly).

## Tests

`npm test` (uses pi-ai's faux provider and a fake bridge extension; no network or keys; the real bridge is never
loaded). Layout: `lib/convert.mjs` (wire conversion), `lib/gateway.mjs` (`createGateway`), `lib/bridge.mjs`
(`createBridgeRunner`), `gateway.mjs` (stdio entry).
