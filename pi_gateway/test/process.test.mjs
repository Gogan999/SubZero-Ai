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
