#!/usr/bin/env node
// Entry point: JSONL over stdin/stdout. Only protocol records may reach stdout.
import { createInterface } from "node:readline";
import { readFileSync } from "node:fs";
import { basename, dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { builtinModels } from "@earendil-works/pi-ai/providers/all";
import { createGateway, keyEnvName } from "./lib/gateway.mjs";
import { createBridgeRunner } from "./lib/bridge.mjs";

// Anything a library prints must not corrupt the protocol stream.
for (const name of ["log", "info", "warn", "debug"]) {
  console[name] = (...args) => console.error(...args);
}

const readJson = (rel) => JSON.parse(readFileSync(new URL(rel, import.meta.url), "utf8"));
const version = readJson("./package.json").version;
const piAiVersion = readJson("./node_modules/@earendil-works/pi-ai/package.json").version;

// Keys come only from SUBZERO_KEY_*. When the Claude bridge is on, copy them aside and remove them from the
// environment, because Claude Code inherits process.env.
const keys = new Map(Object.entries(process.env).filter(([k, v]) => k.startsWith("SUBZERO_KEY_") && v));
let bridge = null;
if (process.env.SUBZERO_BRIDGE === "1") {
  // PI_CODING_AGENT_DIR must be <data dir>/bridge/pi-agent (set by the Python service).
  const agentDir = resolve(process.env.PI_CODING_AGENT_DIR || "");
  if (!process.env.PI_CODING_AGENT_DIR || basename(agentDir) !== "pi-agent" || basename(dirname(agentDir)) !== "bridge") {
    console.error("pi_gateway: SUBZERO_BRIDGE=1 needs PI_CODING_AGENT_DIR=<data dir>/bridge/pi-agent; bridge models stay off");
  } else {
    for (const k of keys.keys()) delete process.env[k];
    bridge = createBridgeRunner({
      dataDir: dirname(dirname(agentDir)),
      extensionPaths: [
        // Tests may swap in a fake extension; this is ignored unless NODE_ENV=test.
        process.env.NODE_ENV === "test" && process.env.SUBZERO_BRIDGE_TEST_EXTENSION
          ? resolve(process.env.SUBZERO_BRIDGE_TEST_EXTENSION)
          : fileURLToPath(new URL("./node_modules/pi-claude-bridge/src/index.ts", import.meta.url)),
      ],
    });
  }
}

const out = process.stdout;
// If the reader goes away, stop quietly: nothing further may be written to stdout.
out.on("error", () => process.exit(1));
process.on("unhandledRejection", (e) => console.error("pi_gateway: unhandled rejection:", e));
const gateway = createGateway({
  // Keys come only from SUBZERO_KEY_* (passed per call); hide ambient env vars and credential files from pi-ai.
  models: builtinModels({ authContext: { env: async () => undefined, fileExists: async () => false } }),
  getApiKey: (provider) => keys.get(keyEnvName(provider)),
  bridge,
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
