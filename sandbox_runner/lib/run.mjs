// The runner loop: JSON lines in, SDK query driven with streaming input, events out.
// `query` is injected so tests can stub the SDK.
import { createEventMapper } from "./events.mjs";
import { buildOptions } from "./options.mjs";

const INTERRUPT_GRACE_MS = 5000;

// Simple async queue used as the SDK's streaming input.
class InputQueue {
  constructor() { this.items = []; this.waiters = []; this.closed = false; }
  push(v) {
    if (this.closed) return;
    const w = this.waiters.shift();
    if (w) w({ value: v, done: false }); else this.items.push(v);
  }
  close() {
    this.closed = true;
    for (const w of this.waiters.splice(0)) w({ value: undefined, done: true });
  }
  [Symbol.asyncIterator]() {
    return {
      next: () => {
        if (this.items.length) return Promise.resolve({ value: this.items.shift(), done: false });
        if (this.closed) return Promise.resolve({ value: undefined, done: true });
        return new Promise((r) => this.waiters.push(r));
      },
      return: () => { this.close(); return Promise.resolve({ value: undefined, done: true }); },
    };
  }
}

async function* lines(stdin) {
  let buf = "";
  for await (const chunk of stdin) {
    buf += typeof chunk === "string" ? chunk : chunk.toString("utf8");
    let i;
    while ((i = buf.indexOf("\n")) >= 0) {
      yield buf.slice(0, i).replace(/\r$/, "");
      buf = buf.slice(i + 1);
    }
  }
  if (buf.trim()) yield buf;
}

const userMessage = (text) => ({ type: "user", message: { role: "user", content: text }, parent_tool_use_id: null });

// Values of secret-looking env vars; they are scrubbed from every emitted line.
function secretsFrom(env) {
  const out = [];
  for (const [k, v] of Object.entries(env)) {
    if (/TOKEN|KEY|SECRET|PASSWORD|AUTH/i.test(k) && typeof v === "string" && v.length >= 8) {
      out.push(v);
      const escaped = JSON.stringify(v).slice(1, -1); // how it looks inside a JSON string
      if (escaped !== v) out.push(escaped);
    }
  }
  return out;
}

export async function run({ query, stdin, stdout, env = process.env, versions = {}, control = {}, graceMs = INTERRUPT_GRACE_MS }) {
  const secrets = secretsFrom(env);
  const emit = (ev) => {
    let line = JSON.stringify(ev);
    for (const s of secrets) line = line.split(s).join("[hidden]");
    stdout.write(line + "\n");
  };
  const flush = () => new Promise((r) => stdout.write("", () => r()));

  const mapper = createEventMapper();
  const input = new InputQueue();
  const abort = new AbortController();
  let q = null;
  let pump = null;
  let openTurns = 0; // turns sent to the SDK that have not produced turn_done yet
  let failed = false;
  let ended; // resolves when the SDK stream finishes by itself (outside shutdown)
  const endedP = new Promise((r) => { ended = r; });
  let stopping = false;
  let pendingPlan = null; // { resolve }
  // Lets the entry point release a waiting plan decision if the process is crashing.
  control.cancelPlan = () => { if (pendingPlan) pendingPlan.resolve({ approve: false, note: "stopped" }); };

  emit({ type: "ready", runner_version: versions.runner_version, sdk_version: versions.sdk_version });

  const canUseTool = async (name, toolInput, opts = {}) => {
    if (name !== "ExitPlanMode") return { behavior: "allow", updatedInput: toolInput };
    const plan = typeof toolInput?.plan === "string" && toolInput.plan ? toolInput.plan : mapper.lastText;
    const id = opts.toolUseID || "plan";
    emit({ type: "plan", id, plan });
    const decision = await new Promise((resolve) => { pendingPlan = { resolve }; });
    pendingPlan = null;
    if (decision.approve) {
      try {
        if (q && typeof q.setPermissionMode === "function") await q.setPermissionMode("bypassPermissions");
      } catch (e) {
        emit({ type: "error", message: "The agent could not switch out of planning mode, so it was not allowed to continue.", detail: String(e && e.message || e).slice(0, 200) });
        return { behavior: "deny", message: "Could not switch out of planning mode; stopping here." };
      }
      return { behavior: "allow", updatedInput: toolInput };
    }
    const note = decision.note ? String(decision.note) : "The mentor declined this plan.";
    return { behavior: "deny", message: note };
  };

  async function consume() {
    for await (const msg of q) {
      for (const ev of mapper.map(msg)) {
        if (ev.type === "turn_done" && openTurns > 0) openTurns--;
        emit(ev);
      }
    }
  }

  // The SDK stream ended or threw on its own: close the open turn and stop the runner (exit 1).
  function onStreamEnd(err) {
    failed = true;
    const message = "The agent hit a problem and stopped.";
    if (err) emit({ type: "error", message, detail: String(err && err.message || err).slice(0, 200) });
    else emit({ type: "error", message: "The agent stopped unexpectedly." });
    if (openTurns > 0) {
      openTurns = 0;
      emit({ type: "turn_done", usage: {}, cost_usd: 0, num_turns: 0, is_error: true, error: err ? message : "The agent stopped unexpectedly." });
    }
    if (pendingPlan) pendingPlan.resolve({ approve: false, note: "stopped" });
    ended({ ended: true });
  }

  function start(msg) {
    const options = buildOptions(msg, { env, canUseTool, abortController: abort });
    input.push(userMessage(String(msg.task ?? "")));
    openTurns++;
    q = query({ prompt: input, options });
    pump = consume().then(
      () => { if (!stopping) onStreamEnd(null); },
      (e) => { if (!stopping) onStreamEnd(e); },
    );
  }

  async function shutdown() {
    stopping = true;
    if (pendingPlan) pendingPlan.resolve({ approve: false, note: "stopped" });
    input.close();
    if (q) {
      const timer = setTimeout(() => abort.abort(), graceMs);
      const aborted = new Promise((r) => abort.signal.addEventListener("abort", r));
      const interrupted = (async () => {
        try { if (typeof q.interrupt === "function") await q.interrupt(); } catch { /* fall back to abort */ }
        await pump;
      })();
      // Either the SDK winds down or the 5 s abort fires; stop never waits longer than that.
      await Promise.race([interrupted, aborted]);
      clearTimeout(timer);
    }
    if (openTurns > 0) {
      openTurns = 0;
      emit({ type: "turn_done", usage: {}, cost_usd: 0, num_turns: 0, is_error: true, error: "stopped" });
    }
  }

  try {
    const it = lines(stdin)[Symbol.asyncIterator]();
    while (true) {
      const r = await Promise.race([it.next(), endedP]);
      if (r.ended || r.done) break;
      const raw = r.value;
      if (!raw.trim()) continue;
      let msg;
      try { msg = JSON.parse(raw); } catch { emit({ type: "error", message: "Could not read that input line." }); continue; }
      if (!msg || typeof msg !== "object" || typeof msg.type !== "string") {
        emit({ type: "error", message: "Input line has no type." });
        continue;
      }
      if (msg.type === "start") {
        if (q) emit({ type: "error", message: "Already started." }); else start(msg);
      } else if (msg.type === "message") {
        if (!q) emit({ type: "error", message: "Send start first." });
        else { openTurns++; input.push(userMessage(String(msg.text ?? ""))); }
      } else if (msg.type === "plan_decision") {
        if (pendingPlan) pendingPlan.resolve(msg); else emit({ type: "error", message: "No plan is waiting." });
      } else if (msg.type === "stop") {
        break;
      } else {
        emit({ type: "error", message: "Unknown input type: " + msg.type.slice(0, 40) });
      }
    }
    await shutdown();
  } catch (e) {
    failed = true;
    emit({ type: "error", message: "The runner hit a problem and stopped.", detail: String(e && e.message || e).slice(0, 200) });
  }
  await flush();
  return failed ? 1 : 0;
}
