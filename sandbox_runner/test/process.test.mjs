import { test } from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";

const entry = fileURLToPath(new URL("../runner.mjs", import.meta.url));

test("--version prints runner and sdk versions as JSON, exit 0", () => {
  const r = spawnSync(process.execPath, [entry, "--version"], { encoding: "utf8" });
  assert.equal(r.status, 0);
  const v = JSON.parse(r.stdout);
  assert.equal(v.runner_version, "0.1.0");
  assert.ok("sdk_version" in v); // null when the SDK is not installed (as in plain test runs)
});
