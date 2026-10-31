// Protocol logic: one JSON request per line in, JSON records out. See ../README.md.
import {
  buildSystemPrompt, convertMessages, convertTools, keyEnvName, mapUsage, messageText, splitModelRef,
  toAnthropicBlocks, mapStopReason,
} from "./convert.mjs";

export { keyEnvName };

const BRIDGE_PROVIDER = "claude-bridge";

class RequestError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

const EFFORTS = new Set(["low", "medium", "high", "xhigh"]);

/** `bridge` is a runner from lib/bridge.mjs, or null when claude-bridge models are switched off. */
export function createGateway({ models, getApiKey, write, version = "0.0.0", piAiVersion = "unknown", bridge = null }) {
  const inflight = new Map(); // id -> { controller, promise }

  const emit = (obj) => {
    try {
      write(obj);
    } catch (e) {
      console.error("pi_gateway: write failed:", e?.message || e);
    }
  };

  function modelInfo(m) {
    return {
      id: `${m.provider}/${m.id}`, name: m.name, provider: m.provider,
      images: Array.isArray(m.input) && m.input.includes("image"), reasoning: Boolean(m.reasoning),
      context_window: m.contextWindow, max_tokens: m.maxTokens,
      cost: {
        input: m.cost?.input ?? 0, output: m.cost?.output ?? 0,
        cacheRead: m.cost?.cacheRead ?? 0, cacheWrite: m.cost?.cacheWrite ?? 0,
      },
    };
  }

  /**
   * Make a pi-ai Anthropic Messages body match what ClaudeClient.request_body sends: no thinking, no effort
   * marker messages, output_config only when an effort was asked for, and system_extra as a second uncached block.
   */
  function anthropicPayloadFixer(effort, systemExtra) {
    return (payload) => {
      if (!payload || typeof payload !== "object") return payload;
      delete payload.thinking;
      if (Array.isArray(payload.messages)) {
        payload.messages = payload.messages.filter(
          (m) => !(m && m.role === "system" && Array.isArray(m.content) && m.content.length === 0 && m.output_config),
        );
      }
      if (Array.isArray(payload.betas)) {
        // Betas that only support the thinking/effort markers we just removed; match by prefix (dates change).
        payload.betas = payload.betas.filter(
          (b) => !(typeof b === "string" && (b.startsWith("mid-conversation-output-config-") || b.startsWith("thinking-binding-controls-"))),
        );
        if (payload.betas.length === 0) delete payload.betas;
      }
      if (effort) payload.output_config = { effort };
      else delete payload.output_config;
      if (systemExtra) {
        const block = { type: "text", text: systemExtra };
        if (Array.isArray(payload.system)) payload.system.push(block);
        else payload.system = [block];
      }
      return payload;
    };
  }

  /** Build options for the right pi-ai entry point and return the event stream. */
  function startStream(model, context, req, apiKey, signal) {
    const base = { apiKey, signal, maxTokens: req.max_tokens };
    const effort = EFFORTS.has(req.effort) ? req.effort : null;
    if (model.api === "anthropic-messages") {
      base.onPayload = anthropicPayloadFixer(effort, req.system_extra || null);
      return models.stream(model, context, base);
    }
    if (effort && model.reasoning) base.reasoning = effort;
    return models.streamSimple(model, context, base);
  }

  /** claude-bridge/<model>: a reply through Claude Code (no tools, no API key). */
  async function runBridge(req, ref, controller) {
    const id = req.id;
    if (!bridge) throw new RequestError("unknown_model", `unknown model ${req.model}`);
    if (req.op === "tool_turn") throw new RequestError("bad_request", "Code sessions can't use the Claude subscription.");
    if (typeof req.max_tokens !== "number" || !(req.max_tokens > 0)) {
      throw new RequestError("bad_request", "max_tokens must be a positive number");
    }
    let result;
    try {
      result = await bridge.reply(
        { ...req, model: ref.id }, controller.signal,
        req.op === "stream" ? (text) => emit({ id, kind: "text", text }) : undefined,
      );
    } catch (e) {
      throw e instanceof RequestError || !e?.code ? e : new RequestError(e.code, e.message);
    }
    if (req.op === "complete" || !result.streamed) {
      if (req.op === "complete" || result.text) emit({ id, kind: "text", text: result.text });
    }
    emit({ id, kind: "usage", usage: result.usage });
  }

  /** The `models` record body; claude-bridge models are added (async) only when the bridge is on. */
  async function listModels(req) {
    const wanted = Array.isArray(req.providers) ? req.providers : [undefined];
    const list = wanted.filter((p) => p !== BRIDGE_PROVIDER).flatMap((p) => [...models.getModels(p)]);
    const infos = list.map(modelInfo);
    if (bridge && (wanted[0] === undefined || wanted.includes(BRIDGE_PROVIDER))) infos.push(...(await bridge.listModels()));
    return infos;
  }

  async function run(req, controller, entry) {
    const id = req.id;
    if (req.op === "models") {
      emit({ id, kind: "models", models: await listModels(req) });
      return;
    }
    const isTool = req.op === "tool_turn";
    const ref = splitModelRef(req.model);
    if (!ref) throw new RequestError("unknown_model", `model must look like provider/model-id, got ${JSON.stringify(req.model)}`);
    if (ref.provider === BRIDGE_PROVIDER) return runBridge(req, ref, controller);
    const model = models.getModel(ref.provider, ref.id);
    if (!model) throw new RequestError("unknown_model", `unknown model ${req.model}`);
    const apiKey = getApiKey(ref.provider);
    entry.key = apiKey || null;
    if (!apiKey) throw new RequestError("no_key", `no API key set for provider ${ref.provider} (${keyEnvName(ref.provider)})`);
    if (typeof req.max_tokens !== "number" || !(req.max_tokens > 0)) {
      throw new RequestError("bad_request", "max_tokens must be a positive number");
    }
    let context;
    try {
      context = {
        systemPrompt: model.api === "anthropic-messages"
          ? buildSystemPrompt(req.system, null) // system_extra is added as a second block in onPayload
          : buildSystemPrompt(req.system, req.system_extra),
        messages: convertMessages(req.messages, model),
        tools: isTool ? convertTools(req.tools) : undefined,
      };
    } catch (e) {
      throw new RequestError("bad_request", e.message);
    }

    let final = null;
    let sawText = false;
    const stream = startStream(model, context, req, apiKey, controller.signal);
    for await (const ev of stream) {
      if (ev.type === "text_delta" && req.op === "stream") {
        sawText = true;
        emit({ id, kind: "text", text: ev.delta });
      } else if (ev.type === "done") {
        final = ev.message;
      } else if (ev.type === "error") {
        final = ev.error;
        if (ev.reason === "aborted") final = { ...ev.error, stopReason: "aborted" };
      }
    }
    if (!final) final = await stream.result();

    if (final.stopReason === "aborted" || controller.signal.aborted) {
      throw new RequestError("aborted", "request aborted");
    }
    if (final.stopReason === "error") {
      throw new RequestError("provider", final.errorMessage || "provider error");
    }
    if (isTool) {
      emit({
        id, kind: "message",
        message: { content: toAnthropicBlocks(final), stop_reason: mapStopReason(final.stopReason), usage: mapUsage(final) },
      });
      emit({ id, kind: "usage", usage: mapUsage(final) });
      return;
    }
    if (req.op === "complete" || !sawText) {
      const text = messageText(final);
      if (req.op === "complete" || text) emit({ id, kind: "text", text });
    }
    emit({ id, kind: "usage", usage: mapUsage(final) });
  }

  async function runTracked(req) {
    const controller = new AbortController();
    const entry = { controller, promise: null, key: null };
    inflight.set(req.id, entry);
    entry.promise = (async () => {
      try {
        await run(req, controller, entry);
      } catch (e) {
        const code = controller.signal.aborted ? "aborted" : e instanceof RequestError ? e.code : "provider";
        let msg = code === "aborted" ? "request aborted" : e?.message || String(e);
        if (entry.key) msg = msg.split(entry.key).join("***");
        emit({ id: req.id, kind: "error", code, error: msg });
      } finally {
        inflight.delete(req.id);
        emit({ id: req.id, kind: "done" });
      }
    })();
    return entry.promise;
  }

  function fail(id, code, error) {
    emit({ id, kind: "error", code, error });
    emit({ id, kind: "done" });
  }

  function handleLine(line) {
    let req;
    try {
      req = JSON.parse(line);
    } catch (e) {
      fail(null, "bad_request", `malformed JSON: ${e.message}`);
      return;
    }
    if (!req || typeof req !== "object" || Array.isArray(req)) return fail(null, "bad_request", "request must be a JSON object");
    if (typeof req.id !== "string" || !req.id) return fail(null, "bad_request", "request needs a string id");
    const id = req.id;

    if (req.op === "abort") {
      // An abort addresses another request's id; it produces no records of its own.
      inflight.get(id)?.controller.abort();
      return;
    }
    if (inflight.has(id)) return fail(id, "duplicate_id", `request id ${id} is already in flight`);

    switch (req.op) {
      case "ping":
        emit({ id, kind: "pong", version, pi_ai: piAiVersion, node: process.version });
        emit({ id, kind: "done" });
        return;
      case "models": {
        if (bridge) {
          void runTracked(req); // loading the bridge's model list is asynchronous
          return;
        }
        try {
          const wanted = Array.isArray(req.providers) ? req.providers : [undefined];
          const list = wanted.flatMap((p) => [...models.getModels(p)]);
          emit({ id, kind: "models", models: list.map(modelInfo) });
          emit({ id, kind: "done" });
        } catch (e) {
          fail(id, "provider", e?.message || String(e));
        }
        return;
      }
      case "stream":
      case "complete":
      case "tool_turn":
        void runTracked(req);
        return;
      default:
        fail(id, "bad_request", `unknown op ${JSON.stringify(req.op)}`);
    }
  }

  /** Wait until every in-flight request has finished. */
  async function idle() {
    while (inflight.size) await Promise.allSettled([...inflight.values()].map((e) => e.promise));
  }

  /** Abort everything in flight and wait for it to finish. */
  async function close() {
    for (const e of inflight.values()) e.controller.abort();
    await idle();
  }

  return { handleLine, idle, close };
}
