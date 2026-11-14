import { test } from "node:test";
import assert from "node:assert/strict";
import { createEventMapper, flattenOutput, MAX_OUTPUT_CHARS } from "../lib/events.mjs";
import { initMsg, assistant, toolResult, resultMsg } from "./helpers.mjs";

test("maps init, text, tool_use with parent, todo, compact", () => {
  const m = createEventMapper();
  assert.deepEqual(m.map(initMsg), [{ type: "session", session_id: "S1" }]);
  const evs = m.map(assistant([
    { type: "text", text: "hi" },
    { type: "tool_use", id: "t1", name: "TodoWrite", input: { todos: [{ content: "a", status: "pending", activeForm: "A" }] } },
  ], "P1"));
  assert.deepEqual(evs.map((e) => e.type), ["text", "tool_start", "todo"]);
  assert.equal(evs[1].parent_id, "P1");
  assert.equal(evs[2].items[0].content, "a");
  // text from a sub-agent does not become the plan fallback
  assert.equal(m.lastText, "");
  assert.deepEqual(m.map({ type: "system", subtype: "compact_boundary" }), [{ type: "compact" }]);
  assert.equal("parent_id" in m.map(assistant([{ type: "tool_use", id: "x", name: "Bash", input: {} }]))[0], false);
});

test("tool_end flattens and truncates", () => {
  const m = createEventMapper();
  const [e] = m.map(toolResult("t1", [{ type: "text", text: "a" }, { type: "text", text: "b" }], true));
  assert.deepEqual(e, { type: "tool_end", id: "t1", output: "a\nb", is_error: true });
  const long = flattenOutput("x".repeat(MAX_OUTPUT_CHARS + 50));
  assert.ok(long.startsWith("x".repeat(MAX_OUTPUT_CHARS)));
  assert.match(long, /output cut/);
  assert.ok(long.length < MAX_OUTPUT_CHARS + 60);
});

test("turn_done success and error subtypes", () => {
  const m = createEventMapper();
  const [ok] = m.map(resultMsg());
  assert.deepEqual(ok, {
    type: "turn_done", usage: { input_tokens: 10, output_tokens: 5, cache_read_input_tokens: 1, cache_creation_input_tokens: 2 },
    cost_usd: 0.05, num_turns: 2, is_error: false,
  });
  const [bad] = m.map(resultMsg({ subtype: "error_max_turns", is_error: true }));
  assert.equal(bad.is_error, true);
  assert.match(bad.error, /limit on steps/);
});
