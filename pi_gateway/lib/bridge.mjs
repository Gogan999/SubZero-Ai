// "claude-bridge/<model>": replies through Claude Code (a Claude subscription login) using the pi-claude-bridge
// extension, run inside an in-memory pi-coding-agent session with no tools. See ../README.md and docs/BRIDGE_SPIKE.md.
//
// Guards (all required):
//  - each reply gets a fresh empty working folder <dataDir>/bridge/work/<id>, deleted afterwards;
//  - CLAUDE_CONFIG_DIR / PI_CODING_AGENT_DIR point inside <dataDir>/bridge, and the Claude Code session files written
//    for a reply are deleted afterwards (the login credentials file is never touched);
//  - ANTHROPIC_* variables are removed from the environment before the bridge loads;
//  - any tool call or tool execution aborts the reply with a "provider" error;
//  - replies run one at a time (the bridge keeps module-wide state and one shared Claude Code config folder).
import { randomUUID } from "node:crypto";
import {
  closeSync, linkSync, mkdirSync, openSync, readdirSync, readFileSync, renameSync, rmSync, statSync, writeFileSync, writeSync,
} from "node:fs";
import { join } from "node:path";
import { convertMessages, mapStopReason } from "./convert.mjs";

export const BRIDGE_PROVIDER = "claude-bridge";
export const TOOL_TRIPWIRE_MESSAGE = "Claude tried to use a tool, so this reply was stopped.";
const EFFORTS = new Set(["low", "medium", "high", "xhigh"]);
// Everything else in CLAUDE_CONFIG_DIR is deleted after each reply (session files, histories, caches).
const KEEP_IN_CLAUDE_DIR = new Set([".credentials.json", ".claude.json", "settings.json"]);
const LOCK_POLL_MS = 200;
const BUSY_MESSAGE = "The Claude subscription is busy. Try again in a minute.";
const CLAUDE_TOOLS = [
  "Bash", "Read", "Write", "Edit", "MultiEdit", "Glob", "Grep", "WebFetch", "WebSearch", "NotebookEdit", "Task",
  "TodoWrite", "Skill", "BashOutput", "KillShell", "AskUserQuestion", "ExitPlanMode",
];
/** Claude Code settings that deny every tool and switch off hooks, project MCP servers and plugins. */
export const LOCKED_SETTINGS = {
  permissions: { deny: CLAUDE_TOOLS },
  disableAllHooks: true,
  enableAllProjectMcpServers: false,
  enabledPlugins: {},
};

/** An error with a protocol error code (bad_request, unknown_model, provider, aborted). */
export class BridgeError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (e) {
    return e.code === "EPERM";
  }
}

function listDir(path) {
  try {
    return new Set(readdirSync(path));
  } catch {
    return new Set();
  }
}

function removeQuietly(path, log) {
  try {
    rmSync(path, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
  } catch (e) {
    log(`pi_gateway: bridge cleanup failed for ${path}: ${e?.message || e}`);
  }
}

/** Split our converted last user message into prompt text and images. */
function splitPrompt(message) {
  if (typeof message.content === "string") return { text: message.content, images: [] };
  const text = message.content.filter((p) => p.type === "text").map((p) => p.text).join("\n\n");
  const images = message.content.filter((p) => p.type === "image").map((p) => ({ type: "image", data: p.data, mimeType: p.mimeType }));
  return { text, images };
}

function isToolEvent(ev) {
  if (ev.type === "tool_execution_start" || ev.type === "tool_execution_update" || ev.type === "tool_execution_end") return true;
  if (ev.type === "message_update" && String(ev.assistantMessageEvent?.type || "").startsWith("toolcall")) return true;
  const msg = ev.message;
  if (msg && msg.role === "assistant" && Array.isArray(msg.content) && msg.content.some((p) => p.type === "toolCall")) return true;
  return false;
}

/**
 * @param {object} opts
 * @param {string} opts.dataDir  the app data folder; bridge files live in <dataDir>/bridge
 * @param {string[]} [opts.extensionPaths]  pi extension files to load (the real bridge entry)
 * @param {Array} [opts.extensionFactories]  inline extension factories (tests)
 * @param {object} [opts.env]  environment to prepare (default process.env)
 * @param {(msg: string) => void} [opts.log]  logs to stderr by default
 * @param {number} [opts.abortGraceMs]  how long to wait for a session to stop after an abort (default 10 s)
 * @param {() => void} [opts.beforeTakeover]  test hook: runs after a stale lock is judged stale, before it is moved
 * @param {object} [opts.pi]  pi-coding-agent module (tests may pass it; loaded lazily otherwise)
 */
export function createBridgeRunner({
  dataDir, extensionPaths = [], extensionFactories = [], env = process.env, log = (m) => console.error(m), pi = null, abortGraceMs = 10_000, beforeTakeover = null,
}) {
  if (!dataDir) throw new Error("createBridgeRunner needs dataDir");
  const bridgeDir = join(dataDir, "bridge");
  const agentDir = join(bridgeDir, "pi-agent");
  const claudeDir = join(bridgeDir, "claude-config");
  const workRoot = join(bridgeDir, "work");
  for (const d of [agentDir, claudeDir, workRoot]) mkdirSync(d, { recursive: true });

  // Environment: never let API-key billing or version checks reach the bridge.
  for (const k of Object.keys(env)) if (k.startsWith("ANTHROPIC_")) delete env[k];
  env.PI_OFFLINE = "1";
  env.PI_SKIP_VERSION_CHECK = "1";
  env.PI_CODING_AGENT_DIR = agentDir;
  env.CLAUDE_CONFIG_DIR = claudeDir;

  // One reply at a time across processes (an old and a new gateway can overlap during a restart).
  const requestTimeoutMs = (Number(env.PI_REQUEST_TIMEOUT) > 0 ? Number(env.PI_REQUEST_TIMEOUT) : 300) * 1000;
  const staleMs = requestTimeoutMs + 60_000;
  const lockPath = join(bridgeDir, "reply.lock");

  // There is no heartbeat: a live holder is trusted until its lock is older than the request timeout + 60 s.
  // Python gives up on a reply after the same request timeout, so a lock that old belongs to a stuck holder.
  let mine = false; // we created the lock file and have not released it

  function createLock() {
    try {
      const fd = openSync(lockPath, "wx");
      try {
        writeSync(fd, JSON.stringify({ pid: process.pid, time: Date.now() }));
      } finally {
        closeSync(fd);
      }
      mine = true;
      return true;
    } catch (e) {
      if (e.code !== "EEXIST") throw e;
      return false;
    }
  }

  /** True if this process now holds the lock. Takes over a stale lock atomically (rename, check, delete, create). */
  function tryLock() {
    if (createLock()) return true;
    let raw;
    try {
      raw = readFileSync(lockPath, "utf8");
    } catch (e) {
      if (e.code === "ENOENT") return createLock(); // released just now
      return false;
    }
    let held = null;
    try {
      held = JSON.parse(raw);
    } catch { /* unreadable: judge by age below */ }
    let age;
    try {
      age = Date.now() - (held?.time ?? statSync(lockPath).mtimeMs);
    } catch {
      return createLock();
    }
    const gone = held && Number.isInteger(held.pid) && held.pid !== process.pid && !pidAlive(held.pid);
    if (!gone && !(age > staleMs)) return false;
    beforeTakeover?.();
    const moved = `${lockPath}.${randomUUID()}`;
    try {
      renameSync(lockPath, moved);
    } catch {
      return false; // someone else moved it first
    }
    let same = false;
    try {
      same = readFileSync(moved, "utf8") === raw;
    } catch { /* gone */ }
    if (!same) {
      // Another gateway replaced the stale lock with its own live one before our rename: put it back.
      // linkSync fails if yet another lock appeared meanwhile; then that one stands. (A tiny window remains.)
      try {
        linkSync(moved, lockPath);
      } catch { /* a newer lock exists */ }
      removeQuietly(moved, log);
      log("pi_gateway: lost a race for the bridge lock; trying again");
      return false;
    }
    removeQuietly(moved, log);
    log(`pi_gateway: took over a stale bridge lock (${gone ? "holder gone" : "too old"})`);
    return createLock();
  }

  async function acquire() {
    const deadline = Date.now() + requestTimeoutMs;
    while (!tryLock()) {
      if (Date.now() >= deadline) throw new BridgeError("provider", BUSY_MESSAGE);
      await new Promise((r) => setTimeout(r, LOCK_POLL_MS));
    }
  }

  function release() {
    try {
      const held = JSON.parse(readFileSync(lockPath, "utf8"));
      if (held.pid !== process.pid) return; // someone took it over; not ours to remove
    } catch {
      if (!mine) return; // unreadable and we didn't just create it: leave it alone
    }
    mine = false;
    removeQuietly(lockPath, log);
  }

  /** Make the Claude Code config folder hold only the login, the settings below and a scrubbed .claude.json. */
  function sweep() {
    for (const name of listDir(workRoot)) removeQuietly(join(workRoot, name), log);
    for (const name of listDir(claudeDir)) if (!KEEP_IN_CLAUDE_DIR.has(name)) removeQuietly(join(claudeDir, name), log);
    const file = join(claudeDir, ".claude.json");
    try {
      const data = JSON.parse(readFileSync(file, "utf8"));
      if (data && typeof data === "object" && !Array.isArray(data)) {
        const bad = Object.keys(data).filter((k) => k === "projects" || k.toLowerCase().includes("history"));
        if (bad.length) {
          for (const k of bad) delete data[k];
          writeFileSync(`${file}.tmp`, JSON.stringify(data));
          renameSync(`${file}.tmp`, file);
        }
      }
    } catch (e) {
      if (e.code !== "ENOENT") log(`pi_gateway: could not clean ${file}: ${e?.message || e}`);
    }
    writeLockedSettings();
  }

  function writeLockedSettings() {
    try {
      const file = join(claudeDir, "settings.json");
      const want = JSON.stringify(LOCKED_SETTINGS, null, 2);
      let have = null;
      try {
        have = readFileSync(file, "utf8");
      } catch { /* missing */ }
      if (have !== want) writeFileSync(file, want);
    } catch (e) {
      log(`pi_gateway: could not write Claude Code settings: ${e?.message || e}`);
    }
  }

  // Startup: clean up after earlier runs, unless a live reply holds the lock (its own cleanup will do it).
  for (let i = 0; i < 3; i++) {
    if (!tryLock()) continue;
    try {
      sweep();
    } finally {
      release();
    }
    break;
  }

  let piModule = pi;
  const loadPi = async () => (piModule ??= await import("@earendil-works/pi-coding-agent"));

  let modelCache = null; // BridgeModel[]
  let queue = Promise.resolve();

  /** Run fn when earlier replies (in this process and others) are done; clean up before letting go. */
  function serialize(fn) {
    const locked = async () => {
      await acquire();
      try {
        writeLockedSettings();
        return await fn();
      } finally {
        try {
          sweep();
        } finally {
          release();
        }
      }
    };
    const run = queue.then(locked, locked);
    queue = run.catch(() => {});
    return run;
  }

  async function newLoader(cwd, systemPrompt) {
    const { DefaultResourceLoader, SettingsManager } = await loadPi();
    const settingsManager = SettingsManager.inMemory({
      cacheWarming: "off", retry: { enabled: false }, compaction: { enabled: false },
      enableAnalytics: false, enableInstallTelemetry: false,
    });
    const loader = new DefaultResourceLoader({
      cwd, agentDir, settingsManager, noExtensions: true, noSkills: true, noPromptTemplates: true, noThemes: true,
      noContextFiles: true, additionalExtensionPaths: extensionPaths, extensionFactories, systemPrompt,
    });
    await loader.reload();
    const result = loader.getExtensions();
    if (result.errors?.length) {
      throw new BridgeError("provider", `Claude bridge failed to load: ${result.errors.map((e) => e.error).join("; ")}`);
    }
    return { loader, settingsManager };
  }

  /** The bridge's registered models, as pi-ai Model objects. */
  function modelsFrom(loader) {
    const regs = loader.getExtensions().runtime?.pendingProviderRegistrations ?? [];
    const out = [];
    for (const reg of regs) {
      if (reg.name !== BRIDGE_PROVIDER) continue;
      for (const m of reg.config.models ?? []) {
        out.push({
          ...m, provider: BRIDGE_PROVIDER, api: m.api ?? reg.config.api, baseUrl: m.baseUrl ?? reg.config.baseUrl,
          reasoning: Boolean(m.reasoning), input: m.input ?? ["text"],
          cost: m.cost ?? { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
        });
      }
    }
    return out;
  }

  function modelInfo(m) {
    return {
      id: `${BRIDGE_PROVIDER}/${m.id}`, name: m.name, provider: BRIDGE_PROVIDER,
      images: Array.isArray(m.input) && m.input.includes("image"), reasoning: Boolean(m.reasoning),
      context_window: m.contextWindow, max_tokens: m.maxTokens,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
    };
  }

  /** Load the extension once (in a throwaway folder) to learn the model list. Call only while holding the queue. */
  async function loadModels() {
    if (modelCache) return modelCache;
    const work = join(workRoot, randomUUID());
    mkdirSync(work, { recursive: true });
    try {
      const { loader } = await newLoader(work, "");
      modelCache = modelsFrom(loader);
    } finally {
      removeQuietly(work, log);
    }
    return modelCache;
  }

  async function listModels() {
    return (await serialize(loadModels)).map(modelInfo);
  }

  /**
   * Answer one request.
   * @param {object} req  { model (id without provider), system, system_extra, messages, effort }
   * @param {AbortSignal} signal
   * @param {(text: string) => void} [onText]  streamed text deltas
   * @returns {Promise<{ text: string, usage: object }>} usage in the gateway's wire shape
   */
  function reply(req, signal, onText) {
    return serialize(() => runReply(req, signal, onText));
  }

  async function runReply(req, signal, onText) {
    if (signal.aborted) throw new BridgeError("aborted", "request aborted");
    const known = await loadModels();
    const model = known.find((m) => m.id === req.model);
    if (!model) throw new BridgeError("unknown_model", `unknown model ${BRIDGE_PROVIDER}/${req.model}`);
    if (signal.aborted) throw new BridgeError("aborted", "request aborted");

    let history;
    try {
      history = convertMessages(req.messages, model);
    } catch (e) {
      throw new BridgeError("bad_request", e.message);
    }
    if (history.some((m) => m.role === "toolResult" || (m.role === "assistant" && m.content.some((p) => p.type === "toolCall")))) {
      throw new BridgeError("bad_request", "Code sessions can't use the Claude subscription.");
    }
    const last = history.pop();
    if (!last || last.role !== "user") throw new BridgeError("bad_request", "the last message must be from the user");
    const prompt = splitPrompt(last);
    const systemPrompt = [req.system, req.system_extra].filter((s) => typeof s === "string" && s).join("\n\n");

    const id = randomUUID();
    const work = join(workRoot, id);
    mkdirSync(work, { recursive: true });
    let session = null;
    let unsubscribe = null;
    let giveUp;
    const gaveUp = new Promise((r) => { giveUp = r; });
    let graceTimer = null;
    // After an abort, wait a little for the session to stop, then drop it so the queue can move on.
    const startGrace = () => { graceTimer ??= setTimeout(giveUp, abortGraceMs); };
    const onAbort = () => {
      void session?.abort().catch(() => {});
      startGrace();
    };
    try {
      const { createAgentSession, SessionManager } = await loadPi();
      const { loader, settingsManager } = await newLoader(work, systemPrompt);
      const sessionManager = SessionManager.inMemory(work);
      for (const m of history) sessionManager.appendMessage(m);
      const thinkingLevel = model.reasoning && EFFORTS.has(req.effort) ? req.effort : "off";
      ({ session } = await createAgentSession({
        cwd: work, agentDir, model, thinkingLevel, noTools: "all", tools: [], resourceLoader: loader, sessionManager,
        settingsManager,
      }));
      await session.bindExtensions({ mode: "print" });

      let tripped = null;
      let finalMessage = null;
      let streamed = "";
      unsubscribe = session.subscribe((ev) => {
        if (!tripped && isToolEvent(ev)) {
          tripped = ev.type;
          log(`pi_gateway: bridge reply stopped, tool event ${ev.type} ${ev.toolName || ev.assistantMessageEvent?.type || ""}`.trim());
          void session.abort().catch(() => {});
          startGrace();
          return;
        }
        if (tripped) return;
        if (ev.type === "message_update" && ev.assistantMessageEvent?.type === "text_delta") {
          streamed += ev.assistantMessageEvent.delta;
          onText?.(ev.assistantMessageEvent.delta);
        } else if (ev.type === "message_end" && ev.message?.role === "assistant") {
          finalMessage = ev.message;
        }
      });
      signal.addEventListener("abort", onAbort, { once: true });
      if (signal.aborted) onAbort();

      const running = (async () => {
        await session.prompt(prompt.text, {
          images: prompt.images.length ? prompt.images : undefined, expandPromptTemplates: false,
        });
        await session.waitForIdle();
        return "done";
      })();
      running.catch(() => {});
      let outcome = null;
      try {
        outcome = await Promise.race([running, gaveUp.then(() => "gave up")]);
      } catch (e) {
        if (!tripped && !signal.aborted) throw new BridgeError("provider", e?.message || String(e));
      }
      if (outcome === "gave up") {
        log("pi_gateway: bridge session did not stop after abort; dropping it");
        if (tripped) throw new BridgeError("provider", TOOL_TRIPWIRE_MESSAGE);
        throw new BridgeError("aborted", "request aborted");
      }

      if (tripped) throw new BridgeError("provider", TOOL_TRIPWIRE_MESSAGE);
      if (signal.aborted || finalMessage?.stopReason === "aborted") throw new BridgeError("aborted", "request aborted");
      if (!finalMessage) throw new BridgeError("provider", "Claude returned no reply");
      if (finalMessage.stopReason === "error") {
        throw new BridgeError("provider", finalMessage.errorMessage || "provider error");
      }
      const text = (finalMessage.content ?? []).filter((p) => p.type === "text").map((p) => p.text).join("");
      const u = finalMessage.usage ?? {};
      return {
        text, streamed: streamed.length > 0,
        usage: {
          input_tokens: u.input ?? 0, output_tokens: u.output ?? 0, cache_read_input_tokens: u.cacheRead ?? 0,
          cache_creation_input_tokens: u.cacheWrite ?? 0, cost_usd: 0, stop_reason: mapStopReason(finalMessage.stopReason),
        },
      };
    } finally {
      clearTimeout(graceTimer);
      signal.removeEventListener("abort", onAbort);
      unsubscribe?.();
      if (session) {
        try {
          const runner = session.extensionRunner;
          if (runner.hasHandlers("session_shutdown")) await runner.emit({ type: "session_shutdown", reason: "quit" });
        } catch (e) {
          log(`pi_gateway: bridge shutdown hook failed: ${e?.message || e}`);
        }
        try {
          session.dispose();
        } catch { /* nothing more to do */ }
      }
      removeQuietly(work, log);
    }
  }

  return { reply, listModels, workRoot, claudeDir, agentDir, lockPath };
}
