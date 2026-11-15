// Pure mapping from Agent SDK messages to the runner's event protocol (D67).
// No SDK import, so it can be unit tested on its own.

export const MAX_OUTPUT_CHARS = 20000;
const TRUNCATION_MARKER = "\n[output cut: too long]";
// Tool names as the SDK spells them (sdk-tools.d.ts: TodoWriteInput; BUILTIN_TOOL_NAMES has "Agent").
export const TODO_TOOL = "TodoWrite";
export const SUBAGENT_TOOL = "Agent";

const ERROR_TEXT = {
  error_max_turns: "The agent reached its limit on steps before it finished.",
  error_max_budget_usd: "The agent reached its spending limit before it finished.",
  error_during_execution: "The agent hit a problem while working and had to stop.",
  error_max_structured_output_retries: "The agent could not produce a usable answer.",
};

// Flatten a tool_result content value (string or array of blocks) to text and cut it.
export function flattenOutput(content) {
  let text;
  if (typeof content === "string") text = content;
  else if (Array.isArray(content)) {
    text = content
      .map((b) => (b && b.type === "text" && typeof b.text === "string" ? b.text : b && b.type === "image" ? "[image]" : ""))
      .filter((s) => s !== "")
      .join("\n");
  } else text = content == null ? "" : String(content);
  if (text.length > MAX_OUTPUT_CHARS) text = text.slice(0, MAX_OUTPUT_CHARS) + TRUNCATION_MARKER;
  return text;
}

export function resultError(msg) {
  if (!msg.is_error && (!msg.subtype || msg.subtype === "success")) return undefined;
  const known = ERROR_TEXT[msg.subtype];
  if (known) return known;
  if (typeof msg.result === "string" && msg.result) return msg.result;
  if (Array.isArray(msg.errors) && msg.errors.length) return String(msg.errors[0]);
  return "The agent stopped because of an error.";
}

function usageOf(u) {
  u = u || {};
  return {
    input_tokens: u.input_tokens || 0,
    output_tokens: u.output_tokens || 0,
    cache_read_input_tokens: u.cache_read_input_tokens || 0,
    cache_creation_input_tokens: u.cache_creation_input_tokens || 0,
  };
}

// Stateful only in remembering the last assistant text (used as the plan text fallback).
export function createEventMapper() {
  const state = { lastText: "" };
  function map(msg) {
    const out = [];
    if (!msg || typeof msg !== "object") return out;
    if (msg.type === "system") {
      if (msg.subtype === "init" && msg.session_id) out.push({ type: "session", session_id: msg.session_id });
      else if (msg.subtype === "compact_boundary") out.push({ type: "compact" });
    } else if (msg.type === "assistant") {
      const content = msg.message && msg.message.content;
      for (const b of Array.isArray(content) ? content : []) {
        if (b.type === "text" && b.text) {
          if (!msg.parent_tool_use_id) state.lastText = b.text;
          out.push({ type: "text", text: b.text });
        } else if (b.type === "tool_use") {
          const ev = { type: "tool_start", id: b.id, name: b.name, input: b.input ?? {} };
          if (msg.parent_tool_use_id) ev.parent_id = msg.parent_tool_use_id;
          out.push(ev);
          if (b.name === TODO_TOOL && b.input && Array.isArray(b.input.todos)) {
            out.push({ type: "todo", items: b.input.todos });
          }
        }
      }
    } else if (msg.type === "user") {
      const content = msg.message && msg.message.content;
      for (const b of Array.isArray(content) ? content : []) {
        if (b.type === "tool_result") {
          out.push({ type: "tool_end", id: b.tool_use_id, output: flattenOutput(b.content), is_error: !!b.is_error });
        }
      }
    } else if (msg.type === "result") {
      const ev = {
        type: "turn_done",
        usage: usageOf(msg.usage),
        cost_usd: msg.total_cost_usd || 0,
        num_turns: msg.num_turns || 0,
        is_error: !!msg.is_error || (!!msg.subtype && msg.subtype !== "success"),
      };
      const err = resultError(msg);
      if (err) ev.error = err;
      out.push(ev);
    }
    return out;
  }
  return { map, get lastText() { return state.lastText; } };
}
