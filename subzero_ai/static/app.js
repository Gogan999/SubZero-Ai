// SubZero AI browser script: sending messages, mentor actions, and live updates.
(() => {
  "use strict";

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  async function post(url, data) {
    const body = new FormData();
    for (const [key, value] of Object.entries(data || {})) body.append(key, value);
    let res;
    try {
      res = await fetch(url, {
        method: "POST", body, credentials: "same-origin",
        headers: { "X-CSRF-Token": csrf, "Accept": "application/json" },
      });
    } catch {
      return { ok: false, error: "Couldn't reach the server. Check your connection and try again." };
    }
    try {
      return await res.json();
    } catch {
      return { ok: false, error: "Something went wrong. Reload the page and try again." };
    }
  }

  function showError(form, message) {
    const el = form && form.querySelector(".form-error");
    if (el) {
      el.textContent = message || "";
      el.hidden = !message;
    } else if (message) {
      window.alert(message);
    }
  }

  function autosize(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = Math.min(textarea.scrollHeight + 2, window.innerHeight * 0.4) + "px";
  }

  // ------------------------------------------------------------------ clicks
  document.addEventListener("click", async (event) => {
    const copy = event.target.closest("[data-copy]");
    if (copy) {
      const pre = copy.closest(".code")?.querySelector("pre");
      if (!pre) return;
      try {
        await navigator.clipboard.writeText(pre.innerText);
        copy.textContent = "Copied";
      } catch {
        copy.textContent = "Select the code to copy";
      }
      setTimeout(() => { copy.textContent = "Copy"; }, 1600);
      return;
    }

    const toggle = event.target.closest("[data-sidebar-toggle]");
    if (toggle) {
      const open = document.body.classList.toggle("sidebar-open");
      toggle.setAttribute("aria-expanded", String(open));
      toggle.textContent = open ? "Close chat list" : "All chats";
      return;
    }

    const opener = event.target.closest("[data-open]");
    if (opener) {
      const form = document.getElementById(opener.dataset.open);
      if (!form) return;
      form.closest("[data-review]")?.querySelectorAll(".inline-form").forEach((other) => {
        if (other !== form) other.hidden = true;
      });
      form.hidden = !form.hidden;
      if (!form.hidden) form.querySelector("textarea")?.focus();
      return;
    }

    const closer = event.target.closest("[data-close]");
    if (closer) {
      const form = closer.closest("form");
      if (form) form.hidden = true;
      maybeRefreshQueue();
      return;
    }

    const action = event.target.closest("[data-action]");
    if (action) {
      if (action.dataset.confirm && !window.confirm(action.dataset.confirm)) return;
      action.disabled = true;
      const result = await post(action.dataset.action);
      action.disabled = false;
      if (!result.ok) {
        window.alert(result.error || "That didn't work. Reload the page and try again.");
        return;
      }
      afterAction(action);
    }
  });

  // ----------------------------------------------------------------- forms
  document.addEventListener("submit", async (event) => {
    const form = event.target;

    if (form.matches("[data-form-action]")) {
      event.preventDefault();
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      const result = await post(form.dataset.formAction, Object.fromEntries(new FormData(form)));
      button.disabled = false;
      if (!result.ok) return showError(form, result.error);
      showError(form, "");
      form.hidden = true;
      afterAction(form);
      return;
    }

    if (form.matches("[data-new-chat]")) {
      event.preventDefault();
      const button = form.querySelector('[type="submit"]');
      button.disabled = true;
      const result = await post("/chats/new", Object.fromEntries(new FormData(form)));
      if (!result.ok) {
        button.disabled = false;
        return showError(form, result.error);
      }
      window.location.href = result.url;
      return;
    }

    if (form.matches("[data-composer]")) {
      event.preventDefault();
      const textarea = form.querySelector("textarea");
      const button = form.querySelector('[type="submit"]');
      if (!textarea.value.trim()) return;
      button.disabled = true;
      const result = await post(form.dataset.url, { text: textarea.value });
      button.disabled = false;
      if (!result.ok) return showError(form, result.error);
      showError(form, "");
      textarea.value = "";
      autosize(textarea);
      if (result.message_id) refreshMessage(result.message_id);
    }
  });

  // Enter sends, Shift+Enter adds a line (on devices with a keyboard and mouse).
  document.addEventListener("keydown", (event) => {
    const textarea = event.target;
    if (!(textarea instanceof HTMLTextAreaElement)) return;
    const form = textarea.closest("[data-composer], [data-new-chat]");
    if (!form || event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    if (!window.matchMedia("(pointer: fine)").matches) return;
    event.preventDefault();
    form.requestSubmit();
  });

  document.addEventListener("input", (event) => {
    if (event.target.matches?.(".composer textarea")) autosize(event.target);
  });

  // Filters and the model picker apply as soon as they change.
  document.addEventListener("change", (event) => {
    if (event.target.matches?.("[data-autosubmit]")) event.target.form?.requestSubmit();
  });

  function afterAction(element) {
    if (document.querySelector("[data-queue]")) {
      refreshQueue();
      return;
    }
    const message = element.closest("[data-message]");
    if (message) refreshMessage(message.dataset.message);
  }

  // ------------------------------------------------------- live conversation
  const threadCol = document.querySelector("[data-conversation]");
  const thread = document.querySelector("[data-thread]");
  const conversationId = threadCol?.dataset.conversation;
  const buffers = new Map(); // reply id -> [{seq, text}] that arrived before the reply was on the page

  function nearBottom() {
    return window.innerHeight + window.scrollY >= document.body.scrollHeight - 240;
  }

  function scrollDown(wasNear) {
    if (wasNear) window.scrollTo({ top: document.body.scrollHeight });
  }

  function appendChunk(body, seq, text) {
    const have = Number(body.dataset.seq || 0);
    if (seq < have) return true; // already shown
    if (seq > have) return false; // missed something; caller refetches
    body.append(document.createTextNode(text));
    body.dataset.seq = String(have + 1);
    return true;
  }

  function flushBuffer(id) {
    const node = document.getElementById("m" + id);
    const body = node?.querySelector("[data-stream]");
    const chunks = buffers.get(String(id));
    if (!body || !chunks) return;
    buffers.delete(String(id));
    chunks.sort((a, b) => a.seq - b.seq).forEach((chunk) => appendChunk(body, chunk.seq, chunk.text));
  }

  async function refreshMessage(id) {
    if (!thread || !conversationId) return;
    const wasNear = nearBottom();
    let res;
    try {
      res = await fetch(`/c/${conversationId}/m/${id}`, { credentials: "same-origin" });
    } catch {
      return;
    }
    const existing = document.getElementById("m" + id);
    if (res.status === 204) {
      existing?.remove();
      updateComposerLock();
      return;
    }
    if (!res.ok) return;
    const template = document.createElement("template");
    template.innerHTML = (await res.text()).trim();
    const node = template.content.firstElementChild;
    if (!node) return;
    const fresh = document.getElementById("m" + id);
    if (fresh) fresh.replaceWith(node);
    else thread.appendChild(node);
    flushBuffer(id);
    updateComposerLock();
    scrollDown(wasNear);
  }

  // One message at a time: a student waits for review, and everyone waits for Claude to finish replying.
  function updateComposerLock() {
    const composer = document.querySelector("[data-composer]");
    if (!composer || !thread) return;
    const textarea = composer.querySelector("textarea");
    const button = composer.querySelector('[type="submit"]');
    const waiting = composer.hasAttribute("data-needs-review")
      && !!thread.querySelector('.msg.person[data-status="pending"]');
    const replying = !!thread.querySelector('.msg.claude[data-status="streaming"]');
    textarea.disabled = waiting; // drafting is fine while Claude replies
    button.disabled = waiting || replying;
    textarea.placeholder = waiting ? "Waiting for a mentor to review your last message"
      : replying ? "Claude is replying…" : "Ask a follow-up";
  }

  if (thread && conversationId) {
    const events = new EventSource(`/c/${conversationId}/events`);
    events.addEventListener("changed", (e) => refreshMessage(JSON.parse(e.data).id));
    events.addEventListener("done", (e) => refreshMessage(JSON.parse(e.data).id));
    events.addEventListener("delta", (e) => {
      const data = JSON.parse(e.data);
      const body = document.getElementById("m" + data.id)?.querySelector("[data-stream]");
      if (!body) {
        const list = buffers.get(String(data.id)) || [];
        list.push({ seq: data.seq, text: data.text });
        buffers.set(String(data.id), list);
        return;
      }
      const wasNear = nearBottom();
      if (!appendChunk(body, data.seq, data.text)) refreshMessage(data.id);
      scrollDown(wasNear);
    });
    events.addEventListener("title", (e) => {
      const title = JSON.parse(e.data).title;
      const heading = document.querySelector("[data-title]");
      if (heading) heading.textContent = title;
    });
    events.addEventListener("visibility", () => window.location.reload());
    events.addEventListener("gone", () => {
      events.close();
      window.location.href = "/chats";
    });
    updateComposerLock();
    window.scrollTo({ top: document.body.scrollHeight });
  }

  // ------------------------------------------------------------ live queue
  const queue = document.querySelector("[data-queue]");
  let queueStale = false;

  async function refreshQueue() {
    if (!queue) return;
    if (queue.querySelector(".inline-form:not([hidden])")) {
      queueStale = true; // don't wipe a reason or edit someone is typing
      return;
    }
    queueStale = false;
    try {
      const res = await fetch("/queue/items", { credentials: "same-origin" });
      if (res.ok) queue.innerHTML = await res.text();
    } catch {
      /* try again on the next update */
    }
  }

  function maybeRefreshQueue() {
    if (queueStale) refreshQueue();
  }

  if (queue) {
    const events = new EventSource("/queue/events");
    events.addEventListener("queue", refreshQueue);
  }
})();
