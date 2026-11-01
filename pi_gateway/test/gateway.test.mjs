import { test } from "node:test";
import assert from "node:assert/strict";
import { createModels, fauxProvider, fauxAssistantMessage, fauxText, fauxToolCall } from "@earendil-works/pi-ai";
import { createGateway } from "../lib/gateway.mjs";

function setup({ key = "dummy", tokensPerSecond } = {}) {
  const faux = fauxProvider({ tokensPerSecond, models: [{ id: "m1", name: "Faux One", input: ["text", "image"], reasoning: true }] });
  const models = createModels();
  models.setProvider(faux.provider);
  const records = [];
  const gw = createGateway({
    models, getApiKey: () => key || undefined, write: (o) => records.push(o), version: "9.9.9", piAiVersion: "1.4.0",
  });
  const ref = `${faux.getModel().provider}/m1`;
  const send = (obj) => gw.handleLine(typeof obj === "string" ? obj : JSON.stringify(obj));
  const of = (id) => records.filter((r) => r.id === id);
  const base = (id, extra = {}) => ({
    id, op: "stream", model: ref, system: "SYS", system_extra: null,
    messages: [{ role: "user", content: "hi" }], max_tokens: 100, effort: null, ...extra,
  });
  return { faux, gw, records, send, of, ref, base };
}

const lastIsDone = (recs) => recs.at(-1).kind === "done" && recs.filter((r) => r.kind === "done").length === 1;

test("stream: ordered deltas, usage, done", async () => {
  const t = setup();
  t.faux.setResponses([fauxAssistantMessage("Hello there, this is a streamed reply")]);
  t.send(t.base("a"));
  await t.gw.idle();
  const r = t.of("a");
  const text = r.filter((x) => x.kind === "text");
  assert.ok(text.length >= 1);
  assert.equal(text.map((x) => x.text).join(""), "Hello there, this is a streamed reply");
  const usage = r.filter((x) => x.kind === "usage");
  assert.equal(usage.length, 1);
  for (const k of ["input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens", "cost_usd"]) {
    assert.equal(typeof usage[0].usage[k], "number", k);
  }
  assert.equal(usage[0].usage.stop_reason, "end_turn");
  assert.ok(lastIsDone(r));
  assert.ok(r.indexOf(usage[0]) > r.indexOf(text.at(-1)));
});

test("complete: one text record, usage, done; system_extra appended", async () => {
  const t = setup();
  let seen;
  t.faux.setResponses([(ctx) => { seen = ctx; return fauxAssistantMessage("full answer"); }]);
  t.send(t.base("c", { op: "complete", system_extra: "EXTRA" }));
  await t.gw.idle();
  assert.deepEqual(t.of("c").map((x) => x.kind), ["text", "usage", "done"]);
  assert.equal(t.of("c")[0].text, "full answer");
  const sys = seen.messages.find((m) => m.role === "system");
  assert.ok(JSON.stringify(sys).includes("SYS\\n\\nEXTRA"));
});

test("abort mid-stream", async () => {
  const t = setup({ tokensPerSecond: 5 });
  t.faux.setResponses([fauxAssistantMessage("word ".repeat(200))]);
  t.send(t.base("s"));
  await new Promise((r) => setTimeout(r, 300));
  t.send({ id: "s", op: "abort" });
  await t.gw.idle();
  const r = t.of("s");
  assert.equal(r.filter((x) => x.kind === "error").length, 1);
  assert.equal(r.find((x) => x.kind === "error").code, "aborted");
  assert.ok(lastIsDone(r));
  assert.ok(!r.some((x) => x.kind === "usage"));
  const n = t.records.length;
  await new Promise((r) => setTimeout(r, 100));
  assert.equal(t.records.length, n);
});

test("provider error", async () => {
  const t = setup();
  t.faux.setResponses([fauxAssistantMessage([], { stopReason: "error", errorMessage: "boom upstream" })]);
  t.send(t.base("e"));
  await t.gw.idle();
  const r = t.of("e");
  assert.deepEqual(r.map((x) => x.kind), ["error", "done"]);
  assert.equal(r[0].code, "provider");
  assert.match(r[0].error, /boom upstream/);
});

test("unknown model, no key, malformed line, duplicate id, bad op", async () => {
  const t = setup();
  t.send(t.base("u", { model: "nope/none" }));
  t.send(t.base("u2", { model: "garbage" }));
  await t.gw.idle();
  assert.equal(t.of("u")[0].code, "unknown_model");
  assert.equal(t.of("u2")[0].code, "unknown_model");
  assert.ok(lastIsDone(t.of("u")));

  const nk = setup({ key: "" });
  nk.send(nk.base("k"));
  await nk.gw.idle();
  assert.equal(nk.of("k")[0].code, "no_key");
  assert.ok(lastIsDone(nk.of("k")));

  t.send("{not json");
  assert.deepEqual(t.records.at(-2), { id: null, kind: "error", code: "bad_request", error: t.records.at(-2).error });
  assert.deepEqual(t.records.at(-1), { id: null, kind: "done" });
  t.send({ op: "ping" });
  assert.equal(t.records.at(-2).id, null);
  assert.equal(t.records.at(-2).code, "bad_request");

  const d = setup({ tokensPerSecond: 5 });
  d.faux.setResponses([fauxAssistantMessage("word ".repeat(100))]);
  d.send(d.base("dup"));
  d.send(d.base("dup"));
  const dupErr = d.of("dup").find((x) => x.code === "duplicate_id");
  assert.ok(dupErr);
  d.send({ id: "dup", op: "abort" });
  await d.gw.idle();
  assert.equal(d.of("dup").filter((x) => x.code === "aborted").length, 1);

  t.send({ id: "z", op: "frobnicate" });
  await t.gw.idle();
  assert.equal(t.of("z")[0].code, "bad_request");
});

test("image input becomes a pi image part; blocks and unknown types", async () => {
  const t = setup();
  let seen;
  t.faux.setResponses([(ctx) => { seen = ctx; return fauxAssistantMessage("ok"); }]);
  t.send(t.base("i", {
    op: "complete",
    messages: [{ role: "user", content: [
      { type: "text", text: "look" },
      { type: "image", source: { type: "base64", media_type: "image/png", data: "QUJD" } },
      { type: "mystery", foo: 1 },
    ] }],
  }));
  await t.gw.idle();
  const user = seen.messages.find((m) => m.role === "user");
  assert.deepEqual(user.content, [{ type: "text", text: "look" }, { type: "image", data: "QUJD", mimeType: "image/png" }]);
  assert.ok(lastIsDone(t.of("i")));
});

test("tool_turn: tool call -> tool_use block, then tool_use/tool_result conversion", async () => {
  const t = setup();
  let tools, seen;
  t.faux.setResponses([
    (ctx) => { tools = ctx; return fauxAssistantMessage([fauxText("checking"), fauxToolCall("lookup", { q: "x" }, { id: "tc1" })], { stopReason: "toolUse" }); },
    (ctx) => { seen = ctx; return fauxAssistantMessage("done now"); },
  ]);
  const schema = { type: "object", properties: { q: { type: "string" } }, required: ["q"] };
  t.send(t.base("t1", { op: "tool_turn", tools: [{ name: "lookup", description: "d", input_schema: schema }] }));
  await t.gw.idle();
  const msg = t.of("t1").find((x) => x.kind === "message").message;
  assert.equal(msg.stop_reason, "tool_use");
  assert.deepEqual(msg.content, [{ type: "text", text: "checking" }, { type: "tool_use", id: "tc1", name: "lookup", input: { q: "x" } }]);
  assert.equal(typeof msg.usage.cost_usd, "number");
  assert.deepEqual(t.of("t1").map((x) => x.kind), ["message", "usage", "done"]);
  assert.ok(JSON.stringify(tools).includes('"lookup"'));

  t.send(t.base("t2", {
    op: "tool_turn", tools: [{ name: "lookup", description: "d", input_schema: schema }],
    messages: [
      { role: "user", content: "go" },
      { role: "assistant", content: msg.content },
      { role: "user", content: [{ type: "tool_result", tool_use_id: "tc1", content: "found it", is_error: false }] },
    ],
  }));
  await t.gw.idle();
  const roles = seen.messages.filter((m) => m.role !== "system").map((m) => m.role);
  assert.deepEqual(roles, ["user", "assistant", "toolResult"]);
  const asst = seen.messages.find((m) => m.role === "assistant");
  assert.deepEqual(asst.content.find((p) => p.type === "toolCall"), { type: "toolCall", id: "tc1", name: "lookup", arguments: { q: "x" } });
  const tr = seen.messages.find((m) => m.role === "toolResult");
  assert.equal(tr.toolCallId, "tc1");
  assert.equal(tr.toolName, "lookup");
  assert.equal(tr.isError, false);
  assert.deepEqual(tr.content, [{ type: "text", text: "found it" }]);
});

test("effort becomes the thinking level for a reasoning non-Anthropic model", async () => {
  const t = setup();
  let opts;
  t.faux.setResponses([(ctx, o) => { opts = o; return fauxAssistantMessage("ok"); }]);
  t.send(t.base("f", { op: "complete", effort: "high", max_tokens: 321 }));
  await t.gw.idle();
  assert.equal(opts.reasoning, "high");
  assert.equal(opts.maxTokens, 321);
});

test("ping and models", () => {
  const t = setup();
  t.send({ id: "p", op: "ping" });
  assert.deepEqual(t.of("p"), [
    { id: "p", kind: "pong", version: "9.9.9", pi_ai: "1.4.0", node: process.version },
    { id: "p", kind: "done" },
  ]);
  t.send({ id: "m", op: "models" });
  const rec = t.of("m")[0];
  assert.equal(rec.kind, "models");
  assert.deepEqual(rec.models.map((m) => m.id), [t.ref]);
  assert.equal(rec.models[0].images, true);
  assert.equal(rec.models[0].reasoning, true);
  assert.ok("context_window" in rec.models[0] && "max_tokens" in rec.models[0]);
  assert.deepEqual(Object.keys(rec.models[0].cost).sort(), ["cacheRead", "cacheWrite", "input", "output"]);
  assert.ok(lastIsDone(t.of("m")));
  t.send({ id: "m2", op: "models", providers: ["nonexistent"] });
  assert.deepEqual(t.of("m2")[0].models, []);
});

async function capturePayload(modelId, reqExtra) {
  const { builtinModels } = await import("@earendil-works/pi-ai/providers/all");
  const models = builtinModels({ authContext: { env: async () => undefined, fileExists: async () => false } });
  let payload;
  const orig = models.stream.bind(models);
  models.stream = (m, c, o) => orig(m, c, {
    ...o,
    onPayload: async (p, mm) => { await o.onPayload(p, mm); payload = JSON.parse(JSON.stringify(p)); throw new Error("stop before network"); },
  });
  const records = [];
  const gw = createGateway({ models, getApiKey: () => "dummy", write: (o) => records.push(o) });
  gw.handleLine(JSON.stringify({
    id: "x", op: "complete", model: `anthropic/${modelId}`, system: "SYS", system_extra: null,
    messages: [{ role: "user", content: "hi" }], max_tokens: 50, effort: null, ...reqExtra,
  }));
  await gw.idle();
  assert.equal(records.at(-1).kind, "done");
  return payload;
}

const noMarkers = (p) => assert.ok(!p.messages.some((m) => m.role === "system"), "no system-role marker messages");

for (const modelId of ["claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-5-5", "claude-haiku-4-5"]) {
  test(`anthropic payload matches ClaudeClient: ${modelId}`, async () => {
    const withEffort = await capturePayload(modelId, { effort: "low", system_extra: "EXTRA" });
    assert.equal(withEffort.thinking, undefined);
    noMarkers(withEffort);
    assert.deepEqual(withEffort.output_config, { effort: "low" });
    assert.equal(withEffort.system.length, 2);
    assert.equal(withEffort.system[0].text, "SYS");
    assert.ok(withEffort.system[0].cache_control);
    assert.deepEqual(withEffort.system[1], { type: "text", text: "EXTRA" });

    const plain = await capturePayload(modelId, {});
    assert.equal(plain.thinking, undefined);
    assert.equal(plain.output_config, undefined);
    noMarkers(plain);
    for (const b of plain.betas ?? []) {
      assert.ok(!b.startsWith("mid-conversation-output-config-") && !b.startsWith("thinking-binding-controls-"), b);
    }
    if (plain.betas) assert.ok(plain.betas.length > 0);
    assert.equal(plain.system.length, 1);

    const multi = await capturePayload(modelId, {
      effort: "high",
      messages: [{ role: "user", content: "a" }, { role: "assistant", content: "b" }, { role: "user", content: "c" }],
    });
    noMarkers(multi);
    assert.deepEqual(multi.messages.map((m) => m.role), ["user", "assistant", "user"]);
    assert.equal(multi.thinking, undefined);
  });
}

test("betas: only the thinking/effort betas are removed; tool betas stay", async () => {
  const p = await capturePayload("claude-sonnet-5-5", {
    op: "tool_turn", tools: [{ name: "t", description: "d", input_schema: { type: "object", properties: {} } }],
  });
  assert.ok(p.betas.some((b) => b.startsWith("inline-tools-")));
  assert.equal(p.betas.length, 1);
  const q = await capturePayload("claude-sonnet-5-5", {});
  assert.equal(q.betas, undefined);
});

test("API key is redacted from error text", async () => {
  const t = setup({ key: "sk-secret-123" });
  t.faux.setResponses([fauxAssistantMessage([], { stopReason: "error", errorMessage: "bad key sk-secret-123 rejected" })]);
  t.send(t.base("r"));
  await t.gw.idle();
  const err = t.of("r").find((x) => x.kind === "error");
  assert.equal(err.error, "bad key *** rejected");
});

test("a throwing write cannot escape", async () => {
  const faux = fauxProvider();
  const models = createModels();
  models.setProvider(faux.provider);
  faux.setResponses([fauxAssistantMessage("hello")]);
  const gw = createGateway({ models, getApiKey: () => "k", write: () => { throw new Error("EPIPE"); } });
  const ref = `${faux.getModel().provider}/${faux.getModel().id}`;
  gw.handleLine(JSON.stringify({ id: "w", op: "ping" }));
  gw.handleLine(JSON.stringify({ id: "w2", op: "complete", model: ref, system: "s", messages: [{ role: "user", content: "x" }], max_tokens: 5 }));
  await gw.idle();
});
