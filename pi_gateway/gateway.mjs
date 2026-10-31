#!/usr/bin/env node
// Entry point: JSONL over stdin/stdout. Only protocol records may reach stdout.
import { createInterface } from "node:readline";
import { readFileSync } from "node:fs";
import { builtinModels } from "@earendil-works/pi-ai/providers/all";
import { createGateway, keyEnvName } from "./lib/gateway.mjs";

// Anything a library prints must not corrupt the protocol stream.
for (const name of ["log", "info", "warn", "debug"]) {
  console[name] = (...args) => console.error(...args);
}

const readJson = (rel) => JSON.parse(readFileSync(new URL(rel, import.meta.url), "utf8"));
const version = readJson("./package.json").version;
const piAiVersion = readJson("./node_modules/@earendil-works/pi-ai/package.json").version;

const out = process.stdout;
// If the reader goes away, stop quietly: nothing further may be written to stdout.
out.on("error", () => process.exit(1));
process.on("unhandledRejection", (e) => console.error("pi_gateway: unhandled rejection:", e));
const gateway = createGateway({
  // Keys come only from SUBZERO_KEY_* (passed per call); hide ambient env vars and credential files from pi-ai.
  models: builtinModels({ authContext: { env: async () => undefined, fileExists: async () => false } }),
  getApiKey: (provider) => process.env[keyEnvName(provider)] || undefined,
  write: (obj) => out.write(JSON.stringify(obj) + "\n"),
  version,
  piAiVersion,
});

// Lines end at "\n"; a trailing "\r" is stripped. (JSON escapes any raw CR/LF inside strings.)
const rl = createInterface({ input: process.stdin, crlfDelay: Infinity });
rl.on("line", (line) => {
  if (line.endsWith("\r")) line = line.slice(0, -1);
  if (line.trim() === "") return;
  gateway.handleLine(line);
});
rl.on("close", async () => {
  await gateway.close();
  // Let stdout drain, then exit cleanly.
  out.write("", () => process.exit(0));
});
