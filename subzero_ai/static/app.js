// SubZero AI browser script: sending messages and files, mentor actions, explainers, and live updates.
(() => {
  "use strict";

  const csrf = document.querySelector('meta[name="csrf-token"]')?.content || "";

  async function post(url, data) {
    let body = data;
    if (!(body instanceof FormData)) {
      body = new FormData();
      for (const [key, value] of Object.entries(data || {})) body.append(key, value);
    }
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

  // On phones the sidebar opens over the page from the menu button in the slim top bar.
  function setSidebar(open) {
    document.body.classList.toggle("sidebar-open", open);
    // The page behind the full-screen panel can't be seen, so take it out of Tab order and screen readers
    document.querySelector(".main-col")?.toggleAttribute("inert", open);
    if (open) document.querySelector("#sidebar .side-new")?.focus();
    document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
      button.setAttribute("aria-expanded", String(open));
      const label = open ? button.dataset.closeLabel : button.dataset.openLabel;
      if (label) button.setAttribute("aria-label", label);
    });
  }

  // Pop-up menus (the account menu, the composer's tools) are <details>: close them on a click elsewhere or Escape.
  function closeMenus(except) {
    document.querySelectorAll("details[data-menu][open]").forEach((menu) => {
      if (menu !== except) menu.open = false;
    });
  }

  document.addEventListener("keydown", (event) => {
    if (event.key !== "Escape") return;
    const menu = document.querySelector("details[data-menu][open]");
    if (menu) {
      menu.open = false;
      menu.querySelector("summary")?.focus();
    } else if (document.body.classList.contains("sidebar-open")) {
      setSidebar(false);
      document.querySelector("[data-sidebar-toggle]")?.focus();
    }
  });

  // ------------------------------------------------------------------ clicks
  document.addEventListener("click", async (event) => {
    closeMenus(event.target.closest("details[data-menu]"));

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

    const full = event.target.closest("[data-fullscreen]");
    if (full) {
      const frame = full.closest("[data-explainer]")?.querySelector("iframe");
      if (!frame) return;
      const request = frame.requestFullscreen || frame.webkitRequestFullscreen;
      if (request) {
        try {
          await request.call(frame);
          return;
        } catch { /* fall through to a new tab */ }
      }
      window.open(frame.src, "_blank", "noopener");
      return;
    }

    const viewCode = event.target.closest("[data-view-code]");
    if (viewCode) {
      const code = viewCode.closest("[data-explainer]")?.querySelector("[data-explainer-code]");
      if (!code) return;
      code.hidden = !code.hidden;
      viewCode.setAttribute("aria-expanded", String(!code.hidden));
      viewCode.textContent = code.hidden ? "View code" : "Hide code";
      return;
    }

    const removeFile = event.target.closest("[data-remove-file]");
    if (removeFile) {
      const form = removeFile.closest("form");
      const picked = chosen(form).filter((_, i) => i !== Number(removeFile.dataset.removeFile));
      setChosen(form, picked);
      return;
    }

    const toggle = event.target.closest("[data-sidebar-toggle]");
    if (toggle) {
      setSidebar(!document.body.classList.contains("sidebar-open"));
      return;
    }

    // Starter chips on the new-chat page begin the message for you
    const starter = event.target.closest("[data-starter]");
    if (starter) {
      const textarea = document.querySelector("[data-new-chat] textarea");
      if (!textarea) return;
      // Never throw away what the student typed: replace only an empty box or another starter's text
      const current = textarea.value.trim();
      const isStarter = [...document.querySelectorAll("[data-starter]")].some((b) => b.dataset.starter.trim() === current);
      textarea.value = !current || isStarter ? starter.dataset.starter : starter.dataset.starter + current;
      textarea.focus();
      textarea.setSelectionRange(textarea.value.length, textarea.value.length);
      autosize(textarea);
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

    // Plain form buttons that do something hard to undo ask first, like the data-action buttons below.
    const confirmSubmit = event.target.closest('button[type="submit"][data-confirm]');
    if (confirmSubmit && !window.confirm(confirmSubmit.dataset.confirm)) {
      event.preventDefault();
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
      const result = await post("/chats/new", new FormData(form));
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
      const result = await post(form.dataset.url, new FormData(form));
      button.disabled = false;
      if (!result.ok) return showError(form, result.error);
      showError(form, "");
      textarea.value = "";
      autosize(textarea);
      setChosen(form, []);
      const explainer = form.querySelector('[name="explainer"]');
      if (explainer) explainer.checked = false;
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

  // ------------------------------------------------------------- attachments
  // The chosen files are kept here and copied into the form's file input, so sending the form sends them.
  // Picking more files, dropping them on the form, or pasting a screenshot adds to the list.
  const picks = new WeakMap(); // form -> [File]

  function chosen(form) {
    return picks.get(form) || [];
  }

  function setChosen(form, files) {
    const input = form?.querySelector("[data-files]");
    if (!input) return;
    picks.set(form, files);
    const transfer = new DataTransfer();
    files.forEach((file) => transfer.items.add(file));
    input.files = transfer.files;
    const list = form.querySelector("[data-chosen]");
    list.replaceChildren(...files.map((file, i) => {
      const item = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = `${file.name} (${sizeLabel(file.size)})`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.dataset.removeFile = String(i);
      remove.setAttribute("aria-label", `Remove ${file.name}`);
      remove.textContent = "×";
      item.append(name, remove);
      return item;
    }));
    list.hidden = files.length === 0;
  }

  function sizeLabel(bytes) {
    if (bytes < 1024) return `${bytes} bytes`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function addFiles(form, files) {
    const input = form?.querySelector("[data-files]");
    if (!input || input.disabled) return;
    const maxFiles = Number(input.dataset.maxFiles), maxBytes = Number(input.dataset.maxBytes);
    const allowed = input.accept.split(",");
    const picked = [...chosen(form)];
    let problem = "";
    for (const file of files) {
      const ext = file.name.includes(".") ? file.name.slice(file.name.lastIndexOf(".")).toLowerCase() : "";
      if (!allowed.includes(ext)) problem = `${file.name} can't be attached. Use code, text, or a PNG or JPG screenshot.`;
      else if (file.size > maxBytes) problem = `${file.name} is over 5 MB.`;
      else if (picked.length >= maxFiles) problem = `You can attach up to ${maxFiles} files to one message.`;
      else picked.push(file);
    }
    setChosen(form, picked);
    showError(form, problem);
  }

  document.addEventListener("change", (event) => {
    const input = event.target;
    if (!input.matches?.("[data-files]")) return;
    // The browser replaced the input's files with the new pick: add them to the ones already chosen.
    addFiles(input.closest("form"), Array.from(input.files));
  });

  document.addEventListener("paste", (event) => {
    const form = event.target.closest?.("[data-composer], [data-new-chat]");
    if (!form) return;
    const images = Array.from(event.clipboardData?.files || []).filter((f) => /^image\/(png|jpeg)$/.test(f.type));
    if (!images.length) return;
    event.preventDefault();
    const stamp = new Date().toTimeString().slice(0, 8).replace(/:/g, "");
    addFiles(form, images.map((f, i) => new File([f], `screenshot-${stamp}${i ? "-" + i : ""}.${f.type === "image/png" ? "png" : "jpg"}`, { type: f.type })));
  });

  document.addEventListener("dragover", (event) => {
    const form = event.target.closest?.("[data-composer], [data-new-chat]");
    if (!form || !event.dataTransfer?.types.includes("Files")) return;
    event.preventDefault();
    form.classList.add("dropping");
  });

  document.addEventListener("dragleave", (event) => {
    const form = event.target.closest?.("[data-composer], [data-new-chat]");
    if (form && !form.contains(event.relatedTarget)) form.classList.remove("dropping");
  });

  document.addEventListener("drop", (event) => {
    const form = event.target.closest?.("[data-composer], [data-new-chat]");
    if (!form || !event.dataTransfer?.files.length) return;
    event.preventDefault();
    form.classList.remove("dropping");
    addFiles(form, Array.from(event.dataTransfer.files));
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
    composer.querySelectorAll('[data-files], [name="explainer"]').forEach((input) => { input.disabled = waiting; });
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
