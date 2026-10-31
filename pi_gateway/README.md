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

Not supported yet: Bedrock, Azure OpenAI and Google Vertex (pi-ai reads their credentials from the environment directly).

## Tests

`npm test` (uses pi-ai's faux provider; no network or keys). Layout: `lib/convert.mjs` (wire conversion),
`lib/gateway.mjs` (`createGateway`), `gateway.mjs` (stdio entry).
