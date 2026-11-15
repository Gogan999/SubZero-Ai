// Entry point, run inside the sandbox VM: `node /opt/subzero-runner/runner.mjs` (never on the host).
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";
import { run } from "./lib/run.mjs";

const here = (p) => fileURLToPath(new URL(p, import.meta.url));

function versions() {
  const runner_version = JSON.parse(readFileSync(here("./package.json"), "utf8")).version;
  let sdk_version = null;
  try {
    const require = createRequire(import.meta.url);
    const dir = require.resolve("@anthropic-ai/claude-agent-sdk/package.json");
    sdk_version = JSON.parse(readFileSync(dir, "utf8")).version;
  } catch {
    try {
      sdk_version = JSON.parse(readFileSync(here("./node_modules/@anthropic-ai/claude-agent-sdk/package.json"), "utf8")).version;
    } catch { /* not installed */ }
  }
  return { runner_version, sdk_version };
}

if (process.argv.includes("--version")) {
  process.stdout.write(JSON.stringify(versions()) + "\n");
  process.exit(0);
}

// Last resort: report and exit 1 (the message never includes env values).
const control = {};
let crashing = false;
for (const ev of ["uncaughtException", "unhandledRejection"]) {
  process.on(ev, () => {
    if (crashing) return;
    crashing = true;
    try { control.cancelPlan?.(); } catch { /* ignore */ }
    process.stdout.write(JSON.stringify({ type: "error", message: "The runner crashed unexpectedly." }) + "\n", () => process.exit(1));
  });
}

// Import the SDK only now, so --version and the tests work without it installed.
let query;
try {
  ({ query } = await import("@anthropic-ai/claude-agent-sdk"));
} catch (e) {
  process.stdout.write(JSON.stringify({ type: "error", message: "The agent library is not installed in this sandbox." }) + "\n");
  process.exit(1);
}
process.exit(await run({ query, stdin: process.stdin, stdout: process.stdout, versions: versions(), control }));
