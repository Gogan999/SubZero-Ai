// Test-only fake bridge extension: answers with the names of any secret-looking variables it can see.
import { createAssistantMessageEventStream } from "@earendil-works/pi-ai";

export default function (pi) {
  pi.registerProvider("claude-bridge", {
    baseUrl: "claude-bridge", apiKey: "not-used", api: "claude-bridge",
    models: [{ id: "claude-x", name: "Claude X", reasoning: false, input: ["text"], contextWindow: 1000, maxTokens: 100,
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 } }],
    streamSimple: (model) => {
      const stream = createAssistantMessageEventStream();
      const names = Object.keys(process.env).filter((k) => /^(SUBZERO_KEY_|ANTHROPIC_)/.test(k));
      const text = JSON.stringify(names);
      const msg = {
        role: "assistant", content: [{ type: "text", text }], api: model.api, provider: model.provider, model: model.id,
        usage: { input: 1, output: 1, cacheRead: 0, cacheWrite: 0, totalTokens: 2, cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0, total: 0 } },
        stopReason: "stop", timestamp: Date.now(),
      };
      queueMicrotask(() => {
        stream.push({ type: "start", partial: msg });
        stream.push({ type: "done", reason: "stop", message: msg });
        stream.end();
      });
      return stream;
    },
  });
}
