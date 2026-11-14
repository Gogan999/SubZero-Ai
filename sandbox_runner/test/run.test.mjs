import { test } from "node:test";
import assert from "node:assert/strict";
import { run } from "../lib/run.mjs";
import { io, until, startMsg, stubQuery, initMsg, assistant, toolResult, resultMsg, TOKEN } from "./helpers.mjs";

const versions = { runner_version: "0.1.0", sdk_version: "0.3.316" };
const env = { PATH: "/bin", ANTHROPIC_AUTH_TOKEN: TOKEN, ANTHROPIC_BASE_URL: "http://10.77.0.1:1" };
const go = (query, h) => run({ query, stdin: h.stdin, stdout: h.stdout, env, versions });
const types = (h) => h.events().map((e) => e.type);

// One scripted turn, then wait for follow-ups until the input closes.
const simpleScript = async function* ({ nextInput }) {
  await nextInput();
  yield initMsg;
  yield assistant([{ type: "text", text: "working" }, { type: "tool_use", id: "t1", name: "Bash", input: { command: "ls" } }]);
  yield toolResult("t1", "file.txt");
  yield resultMsg();
  while (await nextInput()) yield resultMsg();
};

test("ready, then start gives session/text/tool_start/tool_end/turn_done in order", async () => {
  const h = io();
  const { query } = stubQuery(simpleScript);
  const p = go(query, h);
  h.send(startMsg());
  await until(() => types(h).includes("turn_done"));
  h.end();
  assert.equal(await p, 0);
  assert.deepEqual(types(h), ["ready", "session", "text", "tool_start", "tool_end", "turn_done"]);
  assert.deepEqual(h.events()[0], { type: "ready", ...versions });
});

test("options for the default case", async () => {
  const h = io();
  const { query, calls } = stubQuery(simpleScript);
  const p = go(query, h);
  h.send(startMsg({ max_turns: 7 }));
  await until(() => calls.length);
  h.end(); await p;
  const o = calls[0].options;
  assert.equal(o.model, "m1");
  assert.equal(o.cwd, "/work");
  assert.deepEqual(o.systemPrompt, { type: "preset", preset: "claude_code", append: "extra" });
  assert.deepEqual(o.settingSources, ["project"]);
  assert.equal(o.maxTurns, 7);
  assert.equal(o.includePartialMessages, false);
  assert.equal(o.permissionMode, "bypassPermissions");
  assert.equal(o.allowDangerouslySkipPermissions, true);
  assert.deepEqual(o.disallowedTools.sort(), ["Agent", "WebFetch", "WebSearch"]);
  assert.equal("resume" in o, false);
  assert.equal(o.env.DISABLE_TELEMETRY, "1");
  assert.equal(o.env.DISABLE_ERROR_REPORTING, "1");
  assert.equal(o.env.DISABLE_AUTOUPDATER, "1");
  assert.equal(o.env.CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC, "1");
  assert.equal(o.env.ANTHROPIC_AUTH_TOKEN, TOKEN); // passes through to the SDK only
});

test("options for plan_first, web on, sub-agents on, resume", async () => {
  const h = io();
  const { query, calls } = stubQuery(simpleScript);
  const p = go(query, h);
  h.send(startMsg({ plan_first: true, allowed_web: true, sub_agents: true, resume: "S0", max_turns: null }));
  await until(() => calls.length);
  h.end(); await p;
  const o = calls[0].options;
  assert.equal(o.permissionMode, "plan");
  assert.equal(o.allowDangerouslySkipPermissions, true);
  assert.deepEqual(o.disallowedTools, []);
  assert.equal(o.resume, "S0");
  assert.equal("maxTurns" in o, false);
});

test("follow-up message reaches the input stream after turn_done", async () => {
  const h = io();
  const seen = [];
  const { query } = stubQuery(async function* ({ nextInput }) {
    seen.push((await nextInput()).message.content);
    yield initMsg; yield resultMsg();
    seen.push((await nextInput()).message.content);
    yield assistant([{ type: "text", text: "again" }]); yield resultMsg();
    await nextInput();
  });
  const p = go(query, h);
  h.send(startMsg({ task: "first" }));
  await until(() => types(h).filter((t) => t === "turn_done").length === 1);
  h.send({ type: "message", text: "second" });
  await until(() => types(h).filter((t) => t === "turn_done").length === 2);
  h.end();
  assert.equal(await p, 0);
  assert.deepEqual(seen, ["first", "second"]);
  assert.equal(types(h).includes("error"), false);
});

function planScript(planInput) {
  return stubQuery(async function* ({ options, nextInput, state }) {
    await nextInput();
    yield initMsg;
    yield assistant([{ type: "text", text: "Here is my plan" }]);
    const r = await options.canUseTool("ExitPlanMode", planInput, { toolUseID: "pl1" });
    state.result = r;
    yield assistant([{ type: "text", text: r.behavior }]);
    yield resultMsg();
    await nextInput();
  });
}

test("plan approve switches to bypassPermissions", async () => {
  const h = io();
  const { query, calls } = planScript({});
  const p = go(query, h);
  h.send(startMsg({ plan_first: true }));
  await until(() => types(h).includes("plan"));
  const plan = h.events().find((e) => e.type === "plan");
  assert.deepEqual(plan, { type: "plan", id: "pl1", plan: "Here is my plan" });
  h.send({ type: "plan_decision", approve: true, note: "" });
  await until(() => types(h).includes("turn_done"));
  h.end(); await p;
  assert.equal(calls[0].state.result.behavior, "allow");
  assert.deepEqual(calls[0].state.modes, ["bypassPermissions"]);
});

test("plan decline denies with the note; plan text from input preferred", async () => {
  const h = io();
  const { query, calls } = planScript({ plan: "Step 1" });
  const p = go(query, h);
  h.send(startMsg({ plan_first: true }));
  await until(() => types(h).includes("plan"));
  assert.equal(h.events().find((e) => e.type === "plan").plan, "Step 1");
  h.send({ type: "plan_decision", approve: false, note: "use tabs" });
  await until(() => types(h).includes("turn_done"));
  h.end(); await p;
  assert.deepEqual(calls[0].state.result, { behavior: "deny", message: "use tabs" });
  assert.deepEqual(calls[0].state.modes, []);
});

test("other tools are allowed", async () => {
  const h = io();
  const { query, calls } = stubQuery(simpleScript);
  const p = go(query, h);
  h.send(startMsg());
  await until(() => calls.length);
  const r = await calls[0].options.canUseTool("Bash", { command: "ls" }, { toolUseID: "x" });
  assert.equal(r.behavior, "allow");
  h.end(); await p;
});

test("stop mid-run interrupts and emits stopped turn_done, exit 0", async () => {
  const h = io();
  const { query, calls } = stubQuery(async function* ({ nextInput, state }) {
    await nextInput();
    yield initMsg;
    while (!state.interrupted) await new Promise((r) => setTimeout(r, 5));
  });
  const p = go(query, h);
  h.send(startMsg());
  await until(() => types(h).includes("session"));
  h.send({ type: "stop" });
  assert.equal(await p, 0);
  assert.equal(calls[0].state.interrupted, true);
  const last = h.events().at(-1);
  assert.equal(last.type, "turn_done");
  assert.equal(last.is_error, true);
  assert.equal(last.error, "stopped");
  assert.equal(types(h).filter((t) => t === "turn_done").length, 1);
});

test("EOF after a finished turn exits 0 without an extra turn_done", async () => {
  const h = io();
  const { query } = stubQuery(simpleScript);
  const p = go(query, h);
  h.send(startMsg());
  await until(() => types(h).includes("turn_done"));
  h.end();
  assert.equal(await p, 0);
  assert.equal(types(h).filter((t) => t === "turn_done").length, 1);
});

test("malformed line gives an error event and the runner keeps going", async () => {
  const h = io();
  const { query } = stubQuery(simpleScript);
  const p = go(query, h);
  h.send("not json {");
  h.send(startMsg());
  await until(() => types(h).includes("turn_done"));
  h.end();
  assert.equal(await p, 0);
  assert.deepEqual(types(h).slice(0, 2), ["ready", "error"]);
});

test("SDK failure: plain error, closing turn_done, exit 1 without more input", async () => {
  const h = io();
  const { query } = stubQuery(async function* ({ nextInput }) {
    await nextInput();
    throw new Error("boom " + TOKEN);
  });
  const p = go(query, h); // stdin is left open on purpose
  h.send(startMsg());
  assert.equal(await p, 1);
  const err = h.events().find((e) => e.type === "error");
  assert.equal(err.message, "The agent hit a problem and stopped.");
  assert.match(err.detail, /boom/);
  const last = h.events().at(-1);
  assert.equal(last.type, "turn_done");
  assert.equal(last.is_error, true);
  assert.equal(h.raw().includes(TOKEN), false);
});

test("SDK stream ending by itself closes the turn and exits 1", async () => {
  const h = io();
  const { query } = stubQuery(async function* ({ nextInput }) { await nextInput(); yield initMsg; });
  const p = go(query, h);
  h.send(startMsg());
  assert.equal(await p, 1);
  assert.deepEqual(types(h).slice(-2), ["error", "turn_done"]);
});

test("stop with a hanging interrupt still ends after the grace period", async () => {
  const h = io();
  const { query } = stubQuery(async function* ({ nextInput }) {
    await nextInput(); yield initMsg;
    await new Promise(() => {}); // never ends
  });
  const wrapped = (a) => { const q = query(a); q.interrupt = () => new Promise(() => {}); return q; };
  const p = run({ query: wrapped, stdin: h.stdin, stdout: h.stdout, env, versions, graceMs: 50 });
  h.send(startMsg());
  await until(() => types(h).includes("session"));
  h.send({ type: "stop" });
  assert.equal(await p, 0);
  assert.equal(h.events().at(-1).error, "stopped");
});

test("stop emits stopped when a queued follow-up turn is still open", async () => {
  const h = io();
  const { query } = stubQuery(async function* ({ nextInput, state }) {
    await nextInput(); yield initMsg; yield resultMsg();
    while (!state.interrupted) await new Promise((r) => setTimeout(r, 5));
  });
  const p = go(query, h);
  h.send(startMsg());
  await until(() => types(h).includes("turn_done"));
  h.send({ type: "message", text: "more" });
  h.send({ type: "stop" });
  assert.equal(await p, 0);
  assert.equal(h.events().at(-1).error, "stopped");
});

test("failed mode switch after plan approval denies the tool and reports an error", async () => {
  const h = io();
  const base = planScript({});
  const query = (a) => { const q = base.query(a); q.setPermissionMode = async () => { throw new Error("nope"); }; return q; };
  const p = go(query, h);
  h.send(startMsg({ plan_first: true }));
  await until(() => types(h).includes("plan"));
  h.send({ type: "plan_decision", approve: true, note: "" });
  await until(() => types(h).includes("turn_done"));
  h.end(); await p;
  assert.equal(base.calls[0].state.result.behavior, "deny");
  assert.ok(h.events().some((e) => e.type === "error" && /switch out of planning/.test(e.message)));
});

test("JSON-escaped secret is scrubbed too", async () => {
  const h = io();
  const odd = 'tok"with\\quote-1234567';
  const { query } = stubQuery(async function* ({ nextInput }) {
    await nextInput();
    yield assistant([{ type: "text", text: "leak " + odd }]);
    yield resultMsg();
    await nextInput();
  });
  const p = run({ query, stdin: h.stdin, stdout: h.stdout, env: { ANTHROPIC_AUTH_TOKEN: odd }, versions });
  h.send(startMsg());
  await until(() => types(h).includes("turn_done"));
  h.end(); await p;
  assert.equal(h.raw().includes(JSON.stringify(odd).slice(1, -1)), false);
  assert.equal(h.raw().includes(odd), false);
});

test("long tool output is truncated in the emitted event", async () => {
  const h = io();
  const { query } = stubQuery(async function* ({ nextInput }) {
    await nextInput();
    yield assistant([{ type: "tool_use", id: "t", name: "Bash", input: {} }]);
    yield toolResult("t", "y".repeat(50000));
    yield resultMsg();
    await nextInput();
  });
  const p = go(query, h);
  h.send(startMsg());
  await until(() => types(h).includes("turn_done"));
  h.end(); await p;
  const out = h.events().find((e) => e.type === "tool_end").output;
  assert.ok(out.length < 20100);
  assert.match(out, /output cut/);
});

test("no env token appears in any emitted event", async () => {
  const h = io();
  const { query } = stubQuery(async function* ({ nextInput }) {
    await nextInput();
    yield initMsg;
    yield assistant([{ type: "tool_use", id: "t", name: "Bash", input: { command: "env" } }]);
    yield toolResult("t", "ANTHROPIC_AUTH_TOKEN=" + TOKEN);
    yield resultMsg();
    throw new Error("failed with " + TOKEN);
  });
  const p = go(query, h);
  h.send(startMsg());
  await until(() => types(h).includes("error"));
  h.end(); await p;
  assert.equal(h.raw().includes(TOKEN), false);
  assert.ok(h.raw().includes("[hidden]"));
});
