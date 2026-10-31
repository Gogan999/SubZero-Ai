// Conversion between the Anthropic-shaped wire format and pi-ai types.

const ZERO_USAGE = () => ({
  input: 0, output: 0, cacheRead: 0, cacheWrite: 0, totalTokens: 0,
  cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 },
});

/** Split "provider/model-id" at the first slash (model ids may contain slashes). */
export function splitModelRef(ref) {
  if (typeof ref !== "string") return null;
  const i = ref.indexOf("/");
  if (i <= 0 || i === ref.length - 1) return null;
  return { provider: ref.slice(0, i), id: ref.slice(i + 1) };
}

/** Env var name for a provider's key: SUBZERO_KEY_<PROVIDER>. */
export function keyEnvName(provider) {
  return "SUBZERO_KEY_" + String(provider).toUpperCase().replace(/[^A-Z0-9]/g, "_");
}

export function buildSystemPrompt(system, systemExtra) {
  const base = typeof system === "string" ? system : "";
  return systemExtra ? `${base}\n\n${systemExtra}` : base;
}

function convertPart(block) {
  if (typeof block === "string") return { type: "text", text: block };
  if (!block || typeof block !== "object") return null;
  if (block.type === "text" && typeof block.text === "string") return { type: "text", text: block.text };
  if (block.type === "image" && block.source && block.source.type === "base64") {
    return { type: "image", data: block.source.data, mimeType: block.source.media_type };
  }
  return null; // unknown block type: skipped
}

function toParts(content) {
  if (typeof content === "string") return [{ type: "text", text: content }];
  if (!Array.isArray(content)) return [];
  return content.map(convertPart).filter(Boolean);
}

/**
 * Convert Anthropic-shaped messages to pi-ai messages.
 * `model` is the target pi-ai model, used to fill the placeholder fields of assistant turns.
 */
export function convertMessages(messages, model) {
  if (!Array.isArray(messages)) throw new TypeError("messages must be a list");
  const out = [];
  const toolNames = new Map(); // tool_use id -> tool name
  const now = Date.now();
  for (const m of messages) {
    if (!m || typeof m !== "object" || (m.role !== "user" && m.role !== "assistant")) {
      throw new TypeError("each message needs role 'user' or 'assistant'");
    }
    if (m.role === "assistant") {
      const content = [];
      if (typeof m.content === "string") {
        content.push({ type: "text", text: m.content });
      } else if (Array.isArray(m.content)) {
        for (const b of m.content) {
          if (!b || typeof b !== "object") continue;
          if (b.type === "text" && typeof b.text === "string") {
            content.push({ type: "text", text: b.text });
          } else if (b.type === "tool_use") {
            toolNames.set(b.id, b.name);
            content.push({ type: "toolCall", id: b.id, name: b.name, arguments: b.input ?? {} });
          }
        }
      }
      const hasTool = content.some((p) => p.type === "toolCall");
      out.push({
        role: "assistant", content, api: model.api, provider: model.provider, model: model.id,
        usage: ZERO_USAGE(), stopReason: hasTool ? "toolUse" : "stop", timestamp: now,
      });
      continue;
    }
    // user turn: tool_result blocks become toolResult messages, the rest a user message
    if (typeof m.content === "string") {
      out.push({ role: "user", content: m.content, timestamp: now });
      continue;
    }
    const blocks = Array.isArray(m.content) ? m.content : [];
    const results = [];
    const rest = [];
    for (const b of blocks) {
      if (b && typeof b === "object" && b.type === "tool_result") {
        let parts = toParts(b.content);
        if (parts.length === 0) parts = [{ type: "text", text: "" }];
        results.push({
          role: "toolResult", toolCallId: b.tool_use_id, toolName: toolNames.get(b.tool_use_id) ?? "unknown",
          content: parts, isError: Boolean(b.is_error), timestamp: now,
        });
      } else {
        const p = convertPart(b);
        if (p) rest.push(p);
      }
    }
    // Tool results must directly follow the assistant tool call, so they go first.
    out.push(...results);
    if (rest.length) out.push({ role: "user", content: rest, timestamp: now });
  }
  return out;
}

export function convertTools(tools) {
  if (!Array.isArray(tools)) throw new TypeError("tools must be a list");
  return tools.map((t) => {
    if (!t || typeof t.name !== "string") throw new TypeError("each tool needs a name");
    return { name: t.name, description: t.description ?? "", parameters: t.input_schema ?? { type: "object", properties: {} } };
  });
}

const STOP = { stop: "end_turn", length: "max_tokens", toolUse: "tool_use", error: "error", aborted: "aborted" };

export function mapStopReason(reason) {
  return STOP[reason] ?? String(reason);
}

export function mapUsage(msg) {
  const u = msg.usage ?? ZERO_USAGE();
  return {
    input_tokens: u.input ?? 0,
    output_tokens: u.output ?? 0,
    cache_read_input_tokens: u.cacheRead ?? 0,
    cache_creation_input_tokens: u.cacheWrite ?? 0,
    cost_usd: u.cost?.total ?? 0,
    stop_reason: mapStopReason(msg.stopReason),
  };
}

/** Anthropic-shaped content blocks from an AssistantMessage (thinking is dropped). */
export function toAnthropicBlocks(msg) {
  const blocks = [];
  for (const p of msg.content ?? []) {
    if (p.type === "text") blocks.push({ type: "text", text: p.text });
    else if (p.type === "toolCall") blocks.push({ type: "tool_use", id: p.id, name: p.name, input: p.arguments ?? {} });
  }
  return blocks;
}

export function messageText(msg) {
  return (msg.content ?? []).filter((p) => p.type === "text").map((p) => p.text).join("");
}
