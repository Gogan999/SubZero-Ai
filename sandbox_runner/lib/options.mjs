// Builds the SDK options from the host's `start` message (D68, D69).
import { SUBAGENT_TOOL } from "./events.mjs";

export const QUIET_ENV = {
  DISABLE_TELEMETRY: "1",
  DISABLE_ERROR_REPORTING: "1",
  DISABLE_AUTOUPDATER: "1",
  CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC: "1",
};

export function buildOptions(start, { env = process.env, canUseTool, abortController } = {}) {
  const disallowed = [];
  if (!start.allowed_web) disallowed.push("WebSearch", "WebFetch");
  if (!start.sub_agents) disallowed.push(SUBAGENT_TOOL);
  const opts = {
    model: start.model,
    cwd: start.cwd,
    systemPrompt: { type: "preset", preset: "claude_code", append: start.append_system || "" },
    settingSources: ["project"],
    includePartialMessages: false,
    disallowedTools: disallowed,
    env: { ...env, ...QUIET_ENV },
    canUseTool,
    abortController,
  };
  if (start.max_turns) opts.maxTurns = start.max_turns;
  if (start.resume) opts.resume = start.resume;
  // Must be true in both modes: a plan-first run switches to bypassPermissions after approval.
  opts.allowDangerouslySkipPermissions = true;
  opts.permissionMode = start.plan_first ? "plan" : "bypassPermissions";
  return opts;
}
