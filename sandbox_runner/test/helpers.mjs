// Test doubles: a stubbed SDK query and in-memory stdin/stdout.
import { PassThrough } from "node:stream";

export const TOKEN = "tok-SECRET-0123456789abcdef";

export function io() {
  const stdin = new PassThrough();
  const stdout = new PassThrough();
  let buf = "";
  stdout.on("data", (d) => (buf += d));
  return {
    stdin, stdout,
    send: (obj) => stdin.write((typeof obj === "string" ? obj : JSON.stringify(obj)) + "\n"),
    end: () => stdin.end(),
    events: () => buf.split("\n").filter(Boolean).map((l) => JSON.parse(l)),
    raw: () => buf,
  };
}

export const until = async (fn, ms = 2000) => {
  const t = Date.now();
  while (!fn()) {
    if (Date.now() - t > ms) throw new Error("timed out waiting");
    await new Promise((r) => setTimeout(r, 5));
  }
};

export const startMsg = (over = {}) => ({
  type: "start", task: "do it", model: "m1", cwd: "/work", plan_first: false,
  allowed_web: false, sub_agents: false, max_turns: null, append_system: "extra", ...over,
});

export const initMsg = { type: "system", subtype: "init", session_id: "S1" };
export const assistant = (blocks, parent = null) => ({ type: "assistant", parent_tool_use_id: parent, message: { content: blocks } });
export const toolResult = (id, content, is_error = false) => ({ type: "user", message: { content: [{ type: "tool_result", tool_use_id: id, content, is_error }] } });
export const resultMsg = (over = {}) => ({
  type: "result", subtype: "success", is_error: false, num_turns: 2, total_cost_usd: 0.05,
  usage: { input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 1, cache_creation_input_tokens: 2 }, ...over,
});

// Stub query: `script(ctx)` is an async generator body. ctx.input is the prompt iterator helper.
export function stubQuery(script) {
  const calls = [];
  const query = ({ prompt, options }) => {
    const it = prompt[Symbol.asyncIterator]();
    const nextInput = async () => (await it.next()).value;
    const state = { interrupted: false, modes: [] };
    const gen = script({ options, nextInput, state });
    const q = {
      [Symbol.asyncIterator]: () => gen[Symbol.asyncIterator](),
      next: (...a) => gen.next(...a),
      interrupt: async () => { state.interrupted = true; },
      setPermissionMode: async (m) => { state.modes.push(m); },
    };
    calls.push({ options, state });
    return q;
  };
  return { query, calls };
}
