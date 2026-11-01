// Bridge tests use an injected fake extension that registers a scripted "claude-bridge" provider.
// The real pi-claude-bridge is never loaded here.
import { test } from "node:test";
import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { createAssistantMessageEventStream, getCurrentSystemPrompt, getCurrentTools } from "@earendil-works/pi-ai";
import { createModels, fauxProvider } from "@earendil-works/pi-ai";
import { createGateway } from "../lib/gateway.mjs";
import { createBridgeRunner, TOOL_TRIPWIRE_MESSAGE } from "../lib/bridge.mjs";

// 1x1 PNG
const PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
const USAGE = { input: 11, output: 7, cacheRead: 3, cacheWrite: 2, totalTokens: 23, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } };

function assistant(model, content, extra = {}) {
  return {
    role: "assistant", content, api: model.api, provider: model.provider, model: model.id, usage: USAGE,
    stopReason: "stop", timestamp: Date.now(), ...extra,
  };
}

/** A scripted bridge: `script(ctx, options, stream, model)` drives each provider call. */
function setup(script, { dataDir = mkdtempSync(join(tmpdir(), "bridge-test-")), env = {}, abortGraceMs, wrap = (f) => f } = {}) {
  const calls = [];
  const factory = (pi) => {
    pi.registerProvider("claude-bridge", {
      baseUrl: "claude-bridge", apiKey: "not-used", api: "claude-bridge",
      models: [
        { id: "claude-x", name: "Claude X", reasoning: true, input: ["text", "image"], contextWindow: 200000, maxTokens: 8192, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } },
        { id: "claude-y", name: "Claude Y", reasoning: false, input: ["text"], contextWindow: 100000, maxTokens: 4096, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } },
      ],
      streamSimple: (model, context, options) => {
        const stream = createAssistantMessageEventStream();
        calls.push({ model, context, options, env: { ...process.env } });
        queueMicrotask(async () => {
          try {
            await script(context, options, stream, model);
          } catch (e) {
            stream.push({ type: "error", reason: "error", error: assistant(model, [], { stopReason: "error", errorMessage: e.message }) });
            stream.end();
          }
        });
        return stream;
      },
    });
  };
  const logs = [];
  const bridge = createBridgeRunner({ dataDir, extensionFactories: [wrap(factory)], env, abortGraceMs, log: (m) => logs.push(m) });
  const faux = fauxProvider({ models: [{ id: "m1", name: "Faux One" }] });
  const models = createModels();
  models.setProvider(faux.provider);
  const records = [];
  const gw = createGateway({ models, getApiKey: () => "dummy", write: (o) => records.push(o), bridge });
  const send = (obj) => gw.handleLine(JSON.stringify(obj));
  const of = (id) => records.filter((r) => r.id === id);
  const base = (id, extra = {}) => ({
    id, op: "stream", model: "claude-bridge/claude-x", system: "SYS", system_extra: null,
    messages: [{ role: "user", content: "hi" }], max_tokens: 100, effort: null, ...extra,
  });
  return { calls, logs, bridge, gw, records, send, of, base, dataDir, models, faux };
}

const finish = (stream, model, text) => {
  stream.push({ type: "start", partial: assistant(model, []) });
  const half = Math.ceil(text.length / 2);
  stream.push({ type: "text_start", contentIndex: 0, partial: assistant(model, [{ type: "text", text: "" }]) });
  for (const part of [text.slice(0, half), text.slice(half)]) {
    if (part) stream.push({ type: "text_delta", contentIndex: 0, delta: part, partial: assistant(model, [{ type: "text", text: part }]) });
  }
  const msg = assistant(model, [{ type: "text", text }]);
  stream.push({ type: "text_end", contentIndex: 0, content: text, partial: msg });
  stream.push({ type: "done", reason: "stop", message: msg });
  stream.end();
};

const lastIsDone = (recs) => recs.at(-1).kind === "done" && recs.filter((r) => r.kind === "done").length === 1;
const workFolders = (t) => readdirSync(join(t.dataDir, "bridge", "work"));

test("stream: system prompt, ordered history, no tools, text deltas, usage, done", async () => {
  const t = setup((ctx, opts, stream, model) => finish(stream, model, "Hello from the bridge"));
  t.send(t.base("a", {
    system: "BASE", system_extra: "EXTRA",
    messages: [
      { role: "user", content: "first question" },
      { role: "assistant", content: "first answer" },
      { role: "user", content: [{ type: "text", text: "second" }, { type: "text", text: "question" }] },
    ],
  }));
  await t.gw.idle();
  const r = t.of("a");
  assert.equal(r.filter((x) => x.kind === "error").length, 0, JSON.stringify(r));
  assert.equal(r.filter((x) => x.kind === "text").map((x) => x.text).join(""), "Hello from the bridge");
  assert.ok(r.filter((x) => x.kind === "text").length >= 2);
  const usage = r.find((x) => x.kind === "usage").usage;
  assert.deepEqual(usage, {
    input_tokens: 11, output_tokens: 7, cache_read_input_tokens: 3, cache_creation_input_tokens: 2, cost_usd: 0, stop_reason: "end_turn",
  });
  assert.ok(lastIsDone(r));
  assert.equal(t.calls.length, 1);
  const { context, model } = t.calls[0];
  assert.equal(model.id, "claude-x");
  assert.ok(getCurrentSystemPrompt(context.messages).includes("BASE\n\nEXTRA"));
  assert.deepEqual(getCurrentTools(context.messages), []);
  const convo = context.messages.filter((m) => m.role !== "system");
  assert.deepEqual(convo.map((m) => m.role), ["user", "assistant", "user"]);
  assert.equal(convo[0].content, "first question");
  assert.equal(convo[1].content[0].text, "first answer");
  const lastText = typeof convo[2].content === "string" ? convo[2].content : convo[2].content.map((p) => p.text).join("");
  assert.equal(lastText, "second\n\nquestion");
});

test("complete emits one text record", async () => {
  const t = setup((ctx, opts, stream, model) => finish(stream, model, "whole answer"));
  t.send(t.base("c", { op: "complete" }));
  await t.gw.idle();
  const r = t.of("c");
  assert.deepEqual(r.filter((x) => x.kind === "text").map((x) => x.text), ["whole answer"]);
  assert.equal(r.filter((x) => x.kind === "usage").length, 1);
  assert.ok(lastIsDone(r));
});

test("images in the last user message reach the provider", async () => {
  const t = setup((ctx, opts, stream, model) => finish(stream, model, "ok"));
  t.send(t.base("i", {
    messages: [{ role: "user", content: [
      { type: "text", text: "what is this" },
      { type: "image", source: { type: "base64", media_type: "image/png", data: PNG } },
    ] }],
  }));
  await t.gw.idle();
  assert.equal(t.of("i").filter((x) => x.kind === "error").length, 0, JSON.stringify(t.of("i")));
  const last = t.calls[0].context.messages.filter((m) => m.role === "user").at(-1);
  const parts = Array.isArray(last.content) ? last.content : [{ type: "text", text: last.content }];
  assert.ok(parts.some((p) => p.type === "image" && p.data.length > 0 && p.mimeType === "image/png"));
  assert.ok(parts.some((p) => p.type === "text" && p.text.includes("what is this")));
});

test("effort becomes the thinking level only for reasoning models", async () => {
  const t = setup((ctx, opts, stream, model) => finish(stream, model, "ok"));
  t.send(t.base("e1", { effort: "high" }));
  t.send(t.base("e2", { effort: "high", model: "claude-bridge/claude-y" }));
  await t.gw.idle();
  assert.equal(t.calls[0].options.reasoning, "high");
  assert.ok(!t.calls[1].options.reasoning || t.calls[1].options.reasoning === "off");
});

test("models op lists bridge models only when the bridge is on", async () => {
  const t = setup(() => {});
  t.send({ id: "m", op: "models" });
  await t.gw.idle();
  const ids = t.of("m")[0].models.map((m) => m.id);
  assert.ok(ids.includes("claude-bridge/claude-x") && ids.includes("claude-bridge/claude-y"));
  const x = t.of("m")[0].models.find((m) => m.id === "claude-bridge/claude-x");
  assert.equal(x.images, true);
  assert.equal(x.reasoning, true);
  assert.deepEqual(x.cost, { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 });
  t.send({ id: "m2", op: "models", providers: ["claude-bridge"] });
  await t.gw.idle();
  assert.deepEqual(t.of("m2")[0].models.map((m) => m.id).sort(), ["claude-bridge/claude-x", "claude-bridge/claude-y"]);
  assert.ok(lastIsDone(t.of("m2")));
  assert.deepEqual(workFolders(t), []);
});

test("disabled bridge: claude-bridge models are unknown, and not listed", async () => {
  const faux = fauxProvider({ models: [{ id: "m1", name: "Faux One" }] });
  const models = createModels();
  models.setProvider(faux.provider);
  const records = [];
  const gw = createGateway({ models, getApiKey: () => "dummy", write: (o) => records.push(o) });
  gw.handleLine(JSON.stringify({ id: "u", op: "stream", model: "claude-bridge/claude-x", system: "", messages: [{ role: "user", content: "hi" }], max_tokens: 10 }));
  await gw.idle();
  const err = records.find((r) => r.id === "u" && r.kind === "error");
  assert.equal(err.code, "unknown_model");
  assert.ok(lastIsDone(records.filter((r) => r.id === "u")));
  gw.handleLine(JSON.stringify({ id: "m", op: "models" }));
  assert.ok(!records.find((r) => r.id === "m").models.some((m) => m.provider === "claude-bridge"));
});

test("unknown bridge model, tool_turn and bad max_tokens are rejected", async () => {
  const t = setup(() => {});
  t.send(t.base("u", { model: "claude-bridge/nope" }));
  t.send(t.base("tt", { op: "tool_turn", tools: [{ name: "x", description: "", input_schema: {} }] }));
  t.send(t.base("mt", { max_tokens: 0 }));
  await t.gw.idle();
  assert.equal(t.of("u").find((x) => x.kind === "error").code, "unknown_model");
  const tt = t.of("tt").find((x) => x.kind === "error");
  assert.equal(tt.code, "bad_request");
  assert.equal(tt.error, "Code sessions can't use the Claude subscription.");
  assert.equal(t.of("mt").find((x) => x.kind === "error").code, "bad_request");
  assert.equal(t.calls.length, 0);
});

test("provider errors, including a quota message, become provider errors", async () => {
  const quota = "Claude quota exceeded — resets at 5pm";
  const t = setup((ctx, opts, stream, model) => {
    if (JSON.stringify(ctx.messages).includes("QUOTA")) throw new Error(quota);
    stream.push({ type: "error", reason: "error", error: assistant(model, [], { stopReason: "error", errorMessage: "boom" }) });
    stream.end();
  });
  t.send(t.base("q", { messages: [{ role: "user", content: "QUOTA" }] }));
  await t.gw.idle();
  const q = t.of("q");
  assert.deepEqual(q.filter((x) => x.kind === "error").map((x) => [x.code, x.error]), [["provider", quota]]);
  assert.ok(lastIsDone(q));
  t.send(t.base("b"));
  await t.gw.idle();
  const b = t.of("b").find((x) => x.kind === "error");
  assert.equal(b.code, "provider");
  assert.match(b.error, /boom/);
  assert.equal(t.calls.length, 2, "failed replies are not retried");
  assert.deepEqual(workFolders(t), []);
});

test("abort stops the reply with code aborted", async () => {
  let started;
  const startedP = new Promise((r) => { started = r; });
  const t = setup((ctx, opts, stream, model) => {
    stream.push({ type: "start", partial: assistant(model, []) });
    started();
    opts.signal?.addEventListener("abort", () => {
      stream.push({ type: "error", reason: "aborted", error: assistant(model, [], { stopReason: "aborted", errorMessage: "Operation aborted" }) });
      stream.end();
    });
  });
  t.send(t.base("x"));
  await startedP;
  t.send({ id: "x", op: "abort" });
  await t.gw.idle();
  const r = t.of("x");
  assert.deepEqual(r.filter((x) => x.kind === "error").map((x) => x.code), ["aborted"]);
  assert.ok(lastIsDone(r));
  assert.deepEqual(workFolders(t), []);
});

test("tool-call tripwire stops the reply and logs the event name", async () => {
  const t = setup((ctx, opts, stream, model) => {
    const call = { type: "toolCall", id: "t1", name: "Bash", arguments: { command: "ls" } };
    stream.push({ type: "start", partial: assistant(model, []) });
    stream.push({ type: "toolcall_start", contentIndex: 0, partial: assistant(model, [call]) });
    stream.push({ type: "toolcall_end", contentIndex: 0, toolCall: call, partial: assistant(model, [call]) });
    const msg = assistant(model, [call], { stopReason: "toolUse" });
    stream.push({ type: "done", reason: "toolUse", message: msg });
    stream.end();
  });
  t.send(t.base("tw"));
  await t.gw.idle();
  const r = t.of("tw");
  const err = r.filter((x) => x.kind === "error");
  assert.deepEqual(err.map((x) => [x.code, x.error]), [["provider", TOOL_TRIPWIRE_MESSAGE]]);
  assert.equal(r.filter((x) => x.kind === "usage").length, 0);
  assert.ok(lastIsDone(r));
  assert.ok(t.logs.some((l) => /tool/i.test(l)), t.logs.join("\n"));
  assert.deepEqual(workFolders(t), []);
});

test("work folder is fresh and empty during a reply, and removed afterwards", async () => {
  let seen = null;
  const t = setup((ctx, opts, stream, model) => {
    const cwd = process.cwd();
    const dir = getCurrentSystemPrompt(ctx.messages).match(/<cwd>\n(.*)\n<\/cwd>/)?.[1];
    seen = { dir, exists: dir && existsSync(dir), files: dir && existsSync(dir) ? readdirSync(dir) : null, cwd };
    finish(stream, model, "ok");
  });
  t.send(t.base("w1"));
  t.send(t.base("w2"));
  await t.gw.idle();
  assert.ok(seen.dir.includes("work"), seen.dir);
  assert.equal(seen.exists, true);
  assert.deepEqual(seen.files, []);
  assert.deepEqual(workFolders(t), []);
});

function plant(dataDir) {
  const bridge = join(dataDir, "bridge");
  const claude = join(bridge, "claude-config");
  mkdirSync(join(bridge, "work", "stale-1"), { recursive: true });
  writeFileSync(join(bridge, "work", "stale-1", "x.txt"), "x");
  for (const d of ["projects/-old", "session-env/abc", "todos", "shell-snapshots", "random-new-folder"]) {
    mkdirSync(join(claude, d), { recursive: true });
    writeFileSync(join(claude, d, "f"), "x");
  }
  writeFileSync(join(claude, "history.jsonl"), "{}");
  writeFileSync(join(claude, ".credentials.json"), '{"token":"keep-me"}');
  writeFileSync(join(claude, ".claude.json"), JSON.stringify({
    userID: "u", projects: { "/p": { history: [] } }, promptHistory: ["secret"], Command_History: [], theme: "dark",
  }));
  return { bridge, claude };
}

const SETTINGS_KEYS = ["Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "WebFetch", "WebSearch", "NotebookEdit", "Task", "TodoWrite", "Skill", "BashOutput", "KillShell", "AskUserQuestion", "ExitPlanMode"];

test("startup sweep leaves only the keep list, credentials untouched, .claude.json scrubbed", () => {
  const dataDir = mkdtempSync(join(tmpdir(), "bridge-test-"));
  const { bridge, claude } = plant(dataDir);
  createBridgeRunner({ dataDir, extensionFactories: [], env: {}, log() {} });
  assert.deepEqual(readdirSync(join(bridge, "work")), []);
  assert.deepEqual(readdirSync(claude).sort(), [".claude.json", ".credentials.json", "settings.json"]);
  assert.equal(readFileSync(join(claude, ".credentials.json"), "utf8"), '{"token":"keep-me"}');
  assert.deepEqual(JSON.parse(readFileSync(join(claude, ".claude.json"), "utf8")), { userID: "u", theme: "dark" });
  assert.ok(!existsSync(join(bridge, "reply.lock")));
  rmSync(dataDir, { recursive: true, force: true });
});

test("an unparseable .claude.json is left alone and logged", () => {
  const dataDir = mkdtempSync(join(tmpdir(), "bridge-test-"));
  mkdirSync(join(dataDir, "bridge", "claude-config"), { recursive: true });
  writeFileSync(join(dataDir, "bridge", "claude-config", ".claude.json"), "{not json");
  const logs = [];
  createBridgeRunner({ dataDir, extensionFactories: [], env: {}, log: (m) => logs.push(m) });
  assert.equal(readFileSync(join(dataDir, "bridge", "claude-config", ".claude.json"), "utf8"), "{not json");
  assert.ok(logs.some((l) => l.includes(".claude.json")));
  rmSync(dataDir, { recursive: true, force: true });
});

test("session files written during a reply are removed after it, the login stays", async () => {
  const dataDir = mkdtempSync(join(tmpdir(), "bridge-test-"));
  const claude = join(dataDir, "bridge", "claude-config");
  mkdirSync(claude, { recursive: true });
  writeFileSync(join(claude, ".credentials.json"), "{}");
  const t = setup((ctx, opts, stream, model) => {
    mkdirSync(join(claude, "projects", "-new-project"), { recursive: true });
    writeFileSync(join(claude, "projects", "-new-project", "s.jsonl"), "{}");
    mkdirSync(join(claude, "weird-new-dir"), { recursive: true });
    writeFileSync(join(claude, ".claude.json"), JSON.stringify({ projects: { a: 1 }, keep: 1 }));
    finish(stream, model, "ok");
  }, { dataDir });
  t.send(t.base("s"));
  await t.gw.idle();
  assert.deepEqual(readdirSync(claude).sort(), [".claude.json", ".credentials.json", "settings.json"]);
  assert.deepEqual(JSON.parse(readFileSync(join(claude, ".claude.json"), "utf8")), { keep: 1 });
  rmSync(dataDir, { recursive: true, force: true });
});

test("Claude Code settings deny every tool and are restored if altered", async () => {
  const t = setup((ctx, opts, stream, model) => finish(stream, model, "ok"));
  const file = join(t.dataDir, "bridge", "claude-config", "settings.json");
  const check = () => {
    const s = JSON.parse(readFileSync(file, "utf8"));
    for (const tool of SETTINGS_KEYS) assert.ok(s.permissions.deny.includes(tool), tool);
    assert.equal(s.disableAllHooks, true);
    assert.equal(s.enableAllProjectMcpServers, false);
    assert.deepEqual(s.enabledPlugins, {});
  };
  check();
  writeFileSync(file, '{"permissions":{"allow":["Bash"]}}');
  let seen;
  const t2 = setup((ctx, opts, stream, model) => {
    seen = readFileSync(file, "utf8");
    finish(stream, model, "ok");
  }, { dataDir: t.dataDir });
  t2.send(t2.base("r"));
  await t2.gw.idle();
  assert.deepEqual(JSON.parse(seen).permissions.deny, SETTINGS_KEYS);
  check();
});

test("two runners sharing a data folder never overlap and keep each other's work folders", async () => {
  const dataDir = mkdtempSync(join(tmpdir(), "bridge-test-"));
  let active = 0;
  let maxActive = 0;
  const live = [];
  const script = async (ctx, opts, stream, model) => {
    active++;
    maxActive = Math.max(maxActive, active);
    const dir = getCurrentSystemPrompt(ctx.messages).match(/<cwd>\n(.*)\n<\/cwd>/)[1];
    await new Promise((r) => setTimeout(r, 60));
    live.push(existsSync(dir)); // still there although the other runner is waiting / sweeping
    active--;
    finish(stream, model, "ok");
  };
  const a = setup(script, { dataDir });
  const b = setup(script, { dataDir });
  a.send(a.base("a1"));
  b.send(b.base("b1"));
  a.send(a.base("a2"));
  b.send(b.base("b2"));
  await Promise.all([a.gw.idle(), b.gw.idle()]);
  assert.equal(maxActive, 1);
  assert.deepEqual(live, [true, true, true, true]);
  for (const [t, id] of [[a, "a1"], [a, "a2"], [b, "b1"], [b, "b2"]]) {
    assert.equal(t.of(id).filter((x) => x.kind === "error").length, 0, JSON.stringify(t.of(id)));
  }
  assert.deepEqual(readdirSync(join(dataDir, "bridge", "work")), []);
  assert.ok(!existsSync(join(dataDir, "bridge", "reply.lock")));
  rmSync(dataDir, { recursive: true, force: true });
});

test("a lock left by a dead process is taken over; a live one makes the reply wait and then fail as busy", async () => {
  const dataDir = mkdtempSync(join(tmpdir(), "bridge-test-"));
  mkdirSync(join(dataDir, "bridge"), { recursive: true });
  const lock = join(dataDir, "bridge", "reply.lock");
  // a pid that is not alive
  writeFileSync(lock, JSON.stringify({ pid: 2 ** 22 + 12345, time: Date.now() }));
  const t = setup((ctx, opts, stream, model) => finish(stream, model, "ok"), { dataDir });
  t.send(t.base("d"));
  await t.gw.idle();
  assert.equal(t.of("d").filter((x) => x.kind === "error").length, 0, JSON.stringify(t.of("d")));
  // a live holder (the parent process), fresh: wait up to the timeout, then busy
  writeFileSync(lock, JSON.stringify({ pid: process.ppid, time: Date.now() }));
  const t2 = setup(() => {}, { dataDir, env: { PI_REQUEST_TIMEOUT: "1" } });
  const start = Date.now();
  t2.send(t2.base("busy"));
  await t2.gw.idle();
  const err = t2.of("busy").find((x) => x.kind === "error");
  assert.equal(err.code, "provider");
  assert.equal(err.error, "The Claude subscription is busy. Try again in a minute.");
  assert.ok(Date.now() - start >= 900);
  assert.equal(t2.calls.length, 0);
  assert.ok(existsSync(lock), "a live holder's lock is not removed");
  // too old: taken over even though the holder looks alive
  writeFileSync(lock, JSON.stringify({ pid: process.ppid, time: Date.now() - 200_000 }));
  const t3 = setup((ctx, opts, stream, model) => finish(stream, model, "ok"), { dataDir, env: { PI_REQUEST_TIMEOUT: "1" } });
  t3.send(t3.base("old"));
  await t3.gw.idle();
  assert.equal(t3.of("old").filter((x) => x.kind === "error").length, 0, JSON.stringify(t3.of("old")));
  rmSync(dataDir, { recursive: true, force: true });
});

test("a provider that ignores abort is dropped after the grace time and the queue moves on", async () => {
  let n = 0;
  const t = setup((ctx, opts, stream, model) => {
    n++;
    stream.push({ type: "start", partial: assistant(model, []) });
    if (n > 1) finish(stream, model, "second");
    // the first call never ends and ignores the abort signal
  }, { abortGraceMs: 150 });
  t.send(t.base("hang"));
  await new Promise((r) => setTimeout(r, 300));
  t.send({ id: "hang", op: "abort" });
  t.send(t.base("next"));
  await t.gw.idle();
  assert.deepEqual(t.of("hang").filter((x) => x.kind === "error").map((x) => x.code), ["aborted"]);
  assert.ok(lastIsDone(t.of("hang")));
  assert.deepEqual(t.of("next").filter((x) => x.kind === "text").map((x) => x.text).join(""), "second");
  assert.deepEqual(workFolders(t), []);
});

test("a failed model-list load is not cached; the next try works", async () => {
  let loads = 0;
  const t = setup(() => {}, {
    wrap: (factory) => (pi) => {
      loads++;
      if (loads === 1) throw new Error("bridge failed to load");
      factory(pi);
    },
  });
  t.send({ id: "m1", op: "models", providers: ["claude-bridge"] });
  await t.gw.idle();
  assert.equal(t.of("m1").find((x) => x.kind === "error").code, "provider");
  assert.ok(lastIsDone(t.of("m1")));
  t.send({ id: "m2", op: "models", providers: ["claude-bridge"] });
  await t.gw.idle();
  assert.equal(t.of("m2").find((x) => x.kind === "models").models.length, 2);
});

test("environment: ANTHROPIC_* removed, offline flags and dirs set", () => {
  const env = { ANTHROPIC_API_KEY: "x", ANTHROPIC_BASE_URL: "y", PATH: "p" };
  const dataDir = mkdtempSync(join(tmpdir(), "bridge-test-"));
  createBridgeRunner({ dataDir, extensionFactories: [], env, log() {} });
  assert.equal(Object.keys(env).some((k) => k.startsWith("ANTHROPIC_")), false);
  assert.equal(env.PI_OFFLINE, "1");
  assert.equal(env.PI_SKIP_VERSION_CHECK, "1");
  assert.equal(env.PI_CODING_AGENT_DIR, join(dataDir, "bridge", "pi-agent"));
  assert.equal(env.CLAUDE_CONFIG_DIR, join(dataDir, "bridge", "claude-config"));
  assert.equal(env.PATH, "p");
  rmSync(dataDir, { recursive: true, force: true });
});

test("replies run one at a time", async () => {
  let active = 0;
  let maxActive = 0;
  const t = setup(async (ctx, opts, stream, model) => {
    active++;
    maxActive = Math.max(maxActive, active);
    await new Promise((r) => setTimeout(r, 30));
    active--;
    finish(stream, model, "ok");
  });
  t.send(t.base("s1"));
  t.send(t.base("s2"));
  t.send(t.base("s3"));
  await t.gw.idle();
  assert.equal(maxActive, 1);
  for (const id of ["s1", "s2", "s3"]) assert.equal(t.of(id).filter((x) => x.kind === "error").length, 0);
});

test("history with tool blocks is rejected", async () => {
  const t = setup(() => {});
  t.send(t.base("h", { messages: [
    { role: "user", content: "a" },
    { role: "assistant", content: [{ type: "tool_use", id: "t", name: "n", input: {} }] },
    { role: "user", content: [{ type: "tool_result", tool_use_id: "t", content: "r" }] },
    { role: "user", content: "b" },
  ] }));
  await t.gw.idle();
  assert.equal(t.of("h").find((x) => x.kind === "error").code, "bad_request");
});

test("startup after a crash: a dead holder's lock is taken over and the leftovers are swept", () => {
  const dataDir = mkdtempSync(join(tmpdir(), "bridge-test-"));
  const { bridge, claude } = plant(dataDir);
  writeFileSync(join(bridge, "reply.lock"), JSON.stringify({ pid: 2 ** 22 + 12345, time: Date.now() }));
  createBridgeRunner({ dataDir, extensionFactories: [], env: {}, log() {} });
  assert.deepEqual(readdirSync(join(bridge, "work")), []);
  assert.deepEqual(readdirSync(claude).sort(), [".claude.json", ".credentials.json", "settings.json"]);
  assert.deepEqual(readdirSync(bridge).filter((n) => n.startsWith("reply.lock")), []);
  rmSync(dataDir, { recursive: true, force: true });
});

test("a lock replaced while we take it over is put back, and the next try works", async () => {
  const dataDir = mkdtempSync(join(tmpdir(), "bridge-test-"));
  mkdirSync(join(dataDir, "bridge"), { recursive: true });
  const lock = join(dataDir, "bridge", "reply.lock");
  writeFileSync(lock, JSON.stringify({ pid: 2 ** 22 + 12345, time: Date.now() }));
  let calls = 0;
  const logs = [];
  const bridge = createBridgeRunner({
    dataDir, extensionFactories: [], env: {}, log: (m) => logs.push(m),
    beforeTakeover: () => {
      calls++;
      // another gateway wins the race: it replaces the stale lock with its own, live one
      if (calls === 1) writeFileSync(lock, JSON.stringify({ pid: process.pid, time: Date.now(), other: true }));
    },
  });
  assert.equal(calls, 1);
  assert.ok(logs.some((l) => l.includes("lost a race")));
  // the other gateway's live lock is put back untouched
  assert.deepEqual(readdirSync(join(dataDir, "bridge")).filter((n) => n.startsWith("reply.lock")), ["reply.lock"]);
  assert.equal(JSON.parse(readFileSync(lock, "utf8")).other, true);
  rmSync(lock); // the other gateway finishes
  assert.deepEqual(await bridge.listModels().catch(() => "no extension"), []);
  rmSync(dataDir, { recursive: true, force: true });
});

test("an unreadable lock that is not ours is left alone on release", () => {
  const dataDir = mkdtempSync(join(tmpdir(), "bridge-test-"));
  mkdirSync(join(dataDir, "bridge"), { recursive: true });
  const lock = join(dataDir, "bridge", "reply.lock");
  writeFileSync(lock, "garbage"); // fresh and unreadable: not stale, so startup does not take it
  createBridgeRunner({ dataDir, extensionFactories: [], env: {}, log() {} });
  assert.equal(readFileSync(lock, "utf8"), "garbage");
  rmSync(dataDir, { recursive: true, force: true });
});
