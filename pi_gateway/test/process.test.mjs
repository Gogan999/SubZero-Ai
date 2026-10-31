import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const entry = fileURLToPath(new URL("../gateway.mjs", import.meta.url));

test("child process: only protocol lines on stdout, clean exit on stdin close", async () => {
  const env = { ...process.env };
  for (const k of Object.keys(env)) if (k.startsWith("SUBZERO_KEY_")) delete env[k];
  const child = spawn(process.execPath, [entry], { env, stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "";
  child.stdout.on("data", (d) => (stdout += d));
  const exited = new Promise((resolve) => child.on("close", (code) => resolve(code)));
  child.stdin.write(JSON.stringify({ id: "p1", op: "ping" }) + "\r\n");
  child.stdin.write(JSON.stringify({
    id: "x1", op: "complete", model: "nosuchprovider/foo", system: "s", system_extra: null,
    messages: [{ role: "user", content: "hi" }], max_tokens: 10, effort: null,
  }) + "\n");
  child.stdin.write("garbage\n");
  child.stdin.end();
  const code = await exited;
  assert.equal(code, 0);
  const lines = stdout.split("\n");
  assert.equal(lines.pop(), "");
  const recs = lines.map((l) => JSON.parse(l)); // every line must be JSON
  // Records of different requests may interleave; each request's own records stay in order.
  const by = (id) => recs.filter((r) => r.id === id);
  assert.equal(by("p1")[0].kind, "pong");
  assert.equal(by("p1")[0].pi_ai, "1.4.0");
  assert.deepEqual(by("p1")[1], { id: "p1", kind: "done" });
  assert.equal(by("x1")[0].code, "unknown_model");
  assert.deepEqual(by("x1")[1], { id: "x1", kind: "done" });
  assert.equal(by(null)[0].code, "bad_request");
  assert.equal(recs.length, 6);
});

test("child process: missing key gives no_key", async () => {
  const env = { ...process.env };
  for (const k of Object.keys(env)) if (k.startsWith("SUBZERO_KEY_")) delete env[k];
  const child = spawn(process.execPath, [entry], { env, stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "";
  child.stdout.on("data", (d) => (stdout += d));
  const exited = new Promise((resolve) => child.on("close", (code) => resolve(code)));
  child.stdin.end(JSON.stringify({
    id: "k", op: "stream", model: "anthropic/claude-haiku-4-5", system: "s", system_extra: null,
    messages: [{ role: "user", content: "hi" }], max_tokens: 10, effort: null,
  }) + "\n");
  assert.equal(await exited, 0);
  const recs = stdout.trim().split("\n").map((l) => JSON.parse(l));
  assert.equal(recs[0].code, "no_key");
  assert.equal(recs.at(-1).kind, "done");
});

async function runWith(extraEnv, lines) {
  const env = { ...process.env, ...extraEnv };
  for (const k of Object.keys(env)) if (k.startsWith("SUBZERO_KEY_") || k === "SUBZERO_BRIDGE") delete env[k];
  Object.assign(env, extraEnv);
  const child = spawn(process.execPath, [entry], { env, stdio: ["pipe", "pipe", "pipe"] });
  let stdout = "";
  child.stdout.on("data", (d) => (stdout += d));
  const exited = new Promise((resolve) => child.on("close", (code) => resolve(code)));
  child.stdin.end(lines.map((l) => JSON.stringify(l)).join("\n") + "\n");
  assert.equal(await exited, 0);
  return stdout.trim().split("\n").map((l) => JSON.parse(l));
}

const bridgeReq = { id: "b", op: "complete", model: "claude-bridge/claude-sonnet-5-5", system: "s", messages: [{ role: "user", content: "hi" }], max_tokens: 10 };

test("child process: claude-bridge models are unknown unless SUBZERO_BRIDGE=1 and the folders are set", async () => {
  const off = await runWith({}, [bridgeReq]);
  assert.equal(off.find((r) => r.kind === "error").code, "unknown_model");
  const misconfigured = await runWith({ SUBZERO_BRIDGE: "1", PI_CODING_AGENT_DIR: "/somewhere/else" }, [bridgeReq]);
  assert.equal(misconfigured.find((r) => r.kind === "error").code, "unknown_model");
});

test("child process with the bridge on: the extension sees no keys, API-key models still get theirs", async () => {
  const { mkdtempSync, mkdirSync, rmSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const dataDir = mkdtempSync(join(tmpdir(), "bridge-proc-"));
  const agentDir = join(dataDir, "bridge", "pi-agent");
  mkdirSync(agentDir, { recursive: true });
  const env = { ...process.env };
  for (const k of Object.keys(env)) if (k.startsWith("SUBZERO_KEY_")) delete env[k];
  Object.assign(env, {
    NODE_ENV: "test", SUBZERO_BRIDGE: "1", PI_CODING_AGENT_DIR: agentDir, SUBZERO_KEY_ANTHROPIC: "sk-ant-test-secret-key",
    ANTHROPIC_API_KEY: "sk-ant-from-parent", SUBZERO_BRIDGE_TEST_EXTENSION: fileURLToPath(new URL("./fixtures/env-extension.mjs", import.meta.url)),
  });
  const child = spawn(process.execPath, [entry], { env, stdio: ["pipe", "pipe", "pipe"] });
  const recs = [];
  let buf = "";
  child.stdout.on("data", (d) => {
    buf += d;
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      recs.push(JSON.parse(buf.slice(0, i)));
      buf = buf.slice(i + 1);
    }
  });
  const exited = new Promise((resolve) => child.on("close", resolve));
  const waitDone = async (id, ms) => {
    const end = Date.now() + ms;
    while (Date.now() < end && !recs.some((r) => r.id === id && r.kind === "done")) await new Promise((r) => setTimeout(r, 50));
  };
  const req = (id, model) => ({ id, op: "complete", model, system: "s", messages: [{ role: "user", content: "hi" }], max_tokens: 10 });
  child.stdin.write(JSON.stringify(req("b", "claude-bridge/claude-x")) + "\n");
  await waitDone("b", 30000);
  child.stdin.write(JSON.stringify(req("k", "anthropic/claude-haiku-4-5")) + "\n");
  await waitDone("k", 5000); // no network here: it either errors or is aborted below
  child.stdin.end();
  assert.equal(await exited, 0);
  rmSync(dataDir, { recursive: true, force: true });
  const bridgeText = recs.filter((r) => r.id === "b" && r.kind === "text").map((r) => r.text).join("");
  assert.equal(bridgeText, "[]", JSON.stringify(recs.filter((r) => r.id === "b")));
  assert.ok(!recs.some((r) => r.id === "k" && r.code === "no_key"), "the API-key path lost its key");
  assert.ok(!JSON.stringify(recs).includes("sk-ant-test-secret-key"));
});
