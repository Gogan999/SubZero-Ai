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

  // Widening past the phone layout (a tablet turned sideways) hides the menu button, so close the panel then
  window.matchMedia("(max-width: 900px)").addEventListener("change", (event) => {
    if (!event.matches) setSidebar(false);
  });

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

    const copyValue = event.target.closest("[data-copy-value]");
    if (copyValue) {
      const field = document.querySelector(copyValue.dataset.copyValue);
      if (!field) return;
      try {
        await navigator.clipboard.writeText(field.value);
        copyValue.textContent = "Copied";
      } catch {
        field.select();
        copyValue.textContent = "Press Ctrl+C";
      }
      setTimeout(() => { copyValue.textContent = "Copy"; }, 1600);
      return;
    }

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

    // Appearance: switch at once, then save it to the person's profile
    const themeButton = event.target.closest("[data-theme-form] button[name=theme]");
    if (themeButton) {
      event.preventDefault();
      const theme = themeButton.value;
      const root = document.documentElement;
      if (theme === "system") root.removeAttribute("data-theme");
      else root.dataset.theme = theme;
      document.querySelector('meta[name="color-scheme"]')?.setAttribute("content", theme === "system" ? "light dark" : theme);
      themeButton.parentElement.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b === themeButton)));
      const result = await post("/profile/theme", { theme });
      if (!result.ok) window.alert(result.error || "Couldn't save that. Reload the page and try again.");
      return;
    }

    const toggle = event.target.closest("[data-sidebar-toggle]");
    if (toggle) {
      setSidebar(!document.body.classList.contains("sidebar-open"));
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
      const result = await post("/chats/new", await withFrames(form));
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
      const result = await post(form.dataset.url, await withFrames(form));
      button.disabled = false;
      if (!result.ok) return showError(form, result.error);
      showError(form, "");
      textarea.value = "";
      autosize(textarea);
      setChosen(form, []);
      setRemotes(form, []);
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
    renderChosen(form);
  }

  function chip(label, key, index) {
    const item = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = label;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.dataset[key] = String(index);
    remove.setAttribute("aria-label", `Remove ${label}`);
    remove.textContent = "×";
    item.append(name, remove);
    return item;
  }

  function renderChosen(form) {
    const list = form.querySelector("[data-chosen]");
    const files = chosen(form), added = remoteList(form);
    list.replaceChildren(
      ...files.map((file, i) => chip(`${file.name} (${sizeLabel(file.size)})`, "removeFile", i)),
      ...added.map((item, i) => chip(item.label, "removeRemote", i)));
    list.hidden = files.length + added.length === 0;
  }

  // ------------------------------------------------------------- connectors
  // Files from the team's GitHub repository or a Google Drive link. They're sent as "github" and "drive" values,
  // fetched on the server when the message is sent, and listed with the attached files until then.
  const remotes = new WeakMap(); // form -> [{source, value, label}]

  function remoteList(form) {
    return remotes.get(form) || [];
  }

  function setRemotes(form, items) {
    remotes.set(form, items);
    renderChosen(form);
  }

  function addRemote(form, item, dialog) {
    const input = form.querySelector("[data-files]");
    const most = Number(input?.dataset.maxFiles) || 5;
    const added = remoteList(form);
    if (added.some((r) => r.source === item.source && r.value === item.value)) return true;
    if (chosen(form).length + added.length >= most) {
      connectorError(dialog, `You can add up to ${most} files to one message.`);
      return false;
    }
    setRemotes(form, [...added, item]);
    return true;
  }

  function connectorError(dialog, message) {
    const el = dialog.querySelector("[data-connector-error]");
    el.textContent = message;
    el.hidden = !message;
  }

  const FOLDER_ICON = '<svg class="icon" viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 6V15a1 1 0 0 0 1 1h13a1 1 0 0 0 1-1V7.5a1 1 0 0 0-1-1H10L8.3 4.5H3.5a1 1 0 0 0-1 1z"/></svg>';
  const FILE_ICON = '<svg class="icon" viewBox="0 0 20 20" width="18" height="18" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round"><path d="M5 2.5h6.5L15.5 6.5v10a1 1 0 0 1-1 1h-9.5a1 1 0 0 1-1-1v-13a1 1 0 0 1 1-1zM11.5 2.5v4h4"/></svg>';

  async function browseGitHub(dialog, path) {
    const list = dialog.querySelector("[data-gh-list]");
    list.setAttribute("aria-busy", "true");
    connectorError(dialog, "");
    let result;
    try {
      const res = await fetch(`/connectors/github?path=${encodeURIComponent(path)}`,
        { credentials: "same-origin", headers: { "Accept": "application/json" } });
      result = await res.json();
    } catch {
      result = { ok: false, error: "Couldn't reach the server. Check your connection and try again." };
    }
    list.setAttribute("aria-busy", "false");
    if (!result.ok) return connectorError(dialog, result.error);
    dialog.dataset.path = result.path;
    const crumbs = dialog.querySelector("[data-gh-crumbs]");
    const parts = result.path ? result.path.split("/") : [];
    const crumb = (label, target) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "link-button";
      button.dataset.ghPath = target;
      button.textContent = label;
      return button;
    };
    crumbs.replaceChildren(crumb(result.repo.split("/").pop(), ""),
      ...parts.flatMap((part, i) => [document.createTextNode(" / "), crumb(part, parts.slice(0, i + 1).join("/"))]));
    crumbs.lastElementChild.setAttribute("aria-current", "location");
    const form = dialog.closest("form");
    const added = new Set(remoteList(form).filter((r) => r.source === "github").map((r) => r.value));
    list.replaceChildren(...result.entries.map((entry) => {
      const item = document.createElement("li");
      const button = document.createElement("button");
      button.type = "button";
      button.className = "connector-entry";
      button.innerHTML = entry.kind === "dir" ? FOLDER_ICON : FILE_ICON;
      const name = document.createElement("span");
      name.className = "connector-name";
      name.textContent = entry.name;
      const meta = document.createElement("span");
      meta.className = "connector-meta";
      if (entry.kind === "dir") {
        button.dataset.ghPath = entry.path;
        button.setAttribute("aria-label", `Open folder ${entry.name}`);
      } else if (!entry.supported) {
        button.disabled = true;
        meta.textContent = "Can't add this kind of file";
      } else {
        button.dataset.ghFile = entry.path;
        button.setAttribute("aria-pressed", String(added.has(entry.path)));
        meta.textContent = added.has(entry.path) ? "Added" : entry.size;
      }
      button.append(name, meta);
      item.append(button);
      return item;
    }));
    if (!result.entries.length) {
      const empty = document.createElement("li");
      empty.className = "fine";
      empty.textContent = "This folder is empty.";
      list.append(empty);
    }
  }

  function driveLabel(link) {
    if (/docs\.google\.com\/document\//.test(link)) return "Google Doc";
    if (/docs\.google\.com\/spreadsheets\//.test(link)) return "Google Sheet";
    if (/docs\.google\.com\/presentation\//.test(link)) return "Google Slides";
    return "Drive file";
  }

  function addDriveLink(dialog) {
    const field = dialog.querySelector("[data-drive-link]");
    const link = field.value.trim();
    if (!/^https:\/\/(docs|drive)\.google\.com\//.test(link)) {
      connectorError(dialog, "Paste a link that starts with https://docs.google.com or https://drive.google.com.");
      field.focus();
      return;
    }
    const form = dialog.closest("form");
    const count = remoteList(form).filter((r) => r.source === "drive").length + 1;
    if (addRemote(form, { source: "drive", value: link, label: `${driveLabel(link)} ${count > 1 ? count : ""}`.trim() }, dialog)) {
      field.value = "";
      connectorError(dialog, "");
      dialog.close();
    }
  }

  document.addEventListener("click", (event) => {
    const open = event.target.closest("[data-open-connector]");
    if (open) {
      const form = open.closest("form");
      const dialog = form.querySelector(`[data-connector-dialog="${open.dataset.openConnector}"]`);
      open.closest("details")?.removeAttribute("open");
      connectorError(dialog, "");
      dialog.showModal();
      if (open.dataset.openConnector === "github") browseGitHub(dialog, dialog.dataset.path || "");
      else dialog.querySelector("[data-drive-link]")?.focus();
      return;
    }
    const dialog = event.target.closest("[data-connector-dialog]");
    if (!dialog) {
      const removeRemote = event.target.closest("[data-remove-remote]");
      if (removeRemote) {
        const form = removeRemote.closest("form");
        setRemotes(form, remoteList(form).filter((_, i) => i !== Number(removeRemote.dataset.removeRemote)));
      }
      return;
    }
    if (event.target.closest("[data-close-dialog]")) return dialog.close();
    if (event.target.closest("[data-add-drive]")) return addDriveLink(dialog);
    const folder = event.target.closest("[data-gh-path]");
    if (folder) return browseGitHub(dialog, folder.dataset.ghPath);
    const file = event.target.closest("[data-gh-file]");
    if (file) {
      const form = dialog.closest("form");
      const path = file.dataset.ghFile;
      const added = remoteList(form).some((r) => r.source === "github" && r.value === path);
      if (added) {
        setRemotes(form, remoteList(form).filter((r) => !(r.source === "github" && r.value === path)));
      } else if (!addRemote(form, { source: "github", value: path, label: path.split("/").pop() }, dialog)) {
        return;
      }
      file.setAttribute("aria-pressed", String(!added));
      file.querySelector(".connector-meta").textContent = added ? "Removed" : "Added";
    }
  });

  // Enter in the Drive link field adds the link instead of sending the message
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || !event.target.matches?.("[data-drive-link]")) return;
    event.preventDefault();
    addDriveLink(event.target.closest("[data-connector-dialog]"));
  }, true);

  function sizeLabel(bytes) {
    if (bytes < 1024) return `${bytes} bytes`;
    if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
    return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
  }

  function addFiles(form, files) {
    const input = form?.querySelector("[data-files]");
    if (!input || input.disabled) return;
    const maxFiles = Number(input.dataset.maxFiles), maxBytes = Number(input.dataset.maxBytes);
    const maxTotal = Number(input.dataset.maxTotal) || Infinity;
    const allowed = input.accept.split(",");
    const picked = [...chosen(form)];
    let problem = "";
    for (const file of files) {
      const ext = file.name.includes(".") ? file.name.slice(file.name.lastIndexOf(".")).toLowerCase() : "";
      if (!allowed.includes(ext)) problem = `${file.name} can't be attached. Use code, text, or a PNG or JPG screenshot.`;
      else if (file.size > maxBytes) problem = `${file.name} is over ${Math.round(maxBytes / 1024 / 1024)} MB.`;
      else if (picked.reduce((sum, f) => sum + f.size, file.size) > maxTotal) {
        problem = `These files add up to more than ${Math.round(maxTotal / 1024 / 1024)} MB. Send fewer or smaller ones.`;
      }
      else if (picked.length + remoteList(form).length >= maxFiles) problem = `You can attach up to ${maxFiles} files to one message.`;
      else picked.push(file);
    }
    setChosen(form, picked);
    showError(form, problem);
    picked.filter(isVideo).forEach((file) => stills(file, Number(input.dataset.maxFrames) || 8));
  }

  // ------------------------------------------------------------ video stills
  // Claude can't watch video, so the browser takes a few evenly spaced still frames from each one and sends them
  // with the message (as "frames", named "<video name>@<seconds>.jpg"). The page itself shows the video.
  const frameJobs = new WeakMap(); // File -> Promise<File[]>

  function isVideo(file) {
    return /\.(mp4|m4v|mov|webm)$/i.test(file.name);
  }

  function stills(file, most) {
    if (!frameJobs.has(file)) frameJobs.set(file, takeStills(file, most));
    return frameJobs.get(file);
  }

  function waitFor(video, event) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("timeout")), 15000);
      video.addEventListener(event, () => { clearTimeout(timer); resolve(); }, { once: true });
      video.addEventListener("error", () => { clearTimeout(timer); reject(new Error("error")); }, { once: true });
    });
  }

  async function takeStills(file, most) {
    const url = URL.createObjectURL(file);
    const video = document.createElement("video");
    video.muted = true;
    video.playsInline = true;
    video.preload = "auto";
    try {
      const loaded = waitFor(video, "loadeddata");
      video.src = url;
      await loaded;
      const duration = video.duration;
      if (!Number.isFinite(duration) || duration <= 0 || !video.videoWidth) return [];
      // About one still every 5 seconds, between 3 and the limit, each at most 1024px on its long edge
      const count = Math.min(most, Math.max(3, Math.ceil(duration / 5)));
      const scale = Math.min(1, 1024 / Math.max(video.videoWidth, video.videoHeight));
      const canvas = document.createElement("canvas");
      canvas.width = Math.round(video.videoWidth * scale);
      canvas.height = Math.round(video.videoHeight * scale);
      const frames = [];
      for (let i = 0; i < count; i += 1) {
        const at = (duration * (i + 0.5)) / count;
        const seeked = waitFor(video, "seeked");
        video.currentTime = at;
        await seeked;
        canvas.getContext("2d").drawImage(video, 0, 0, canvas.width, canvas.height);
        const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.8));
        if (blob) frames.push(new File([blob], `${file.name}@${Math.floor(at)}.jpg`, { type: "image/jpeg" }));
      }
      return frames;
    } catch {
      return []; // a format this browser can't play: Claude is told it got no stills
    } finally {
      video.removeAttribute("src");
      video.load();
      URL.revokeObjectURL(url);
    }
  }

  async function withFrames(form) {
    const data = new FormData(form);
    const input = form.querySelector("[data-files]");
    const most = Number(input?.dataset.maxFrames) || 8;
    for (const file of chosen(form).filter(isVideo)) {
      (await stills(file, most)).forEach((frame) => data.append("frames", frame));
    }
    remoteList(form).forEach((item) => data.append(item.source, item.value));
    return data;
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

  // Code sessions: redraw the steps every few seconds while Claude works, and stop once it waits or finishes
  function pollCode() {
    const box = document.querySelector("[data-code-steps]");
    if (!box?.querySelector("[data-code-live]")) return;
    setTimeout(async () => {
      try {
        const res = await fetch(box.dataset.url, { credentials: "same-origin" });
        if (res.ok) box.innerHTML = await res.text();
      } catch {
        // try again on the next round
      }
      pollCode();
    }, 2500);
  }
  pollCode();

  // Composer tools: the ones switched on show as chips next to the tools button, so it's clear what's on
  function updateToolChips(form) {
    const chips = form?.querySelector("[data-tool-chips]");
    if (!chips) return;
    const on = [...form.querySelectorAll("input[data-tool]:checked")].map((input) => input.dataset.tool).filter(Boolean);
    chips.replaceChildren(...on.map((label) => {
      const item = document.createElement("li");
      item.className = "tool-chip";
      item.textContent = label;
      return item;
    }));
    chips.hidden = on.length === 0;
  }

  document.addEventListener("change", (event) => {
    const input = event.target;
    if (!input.matches?.("input[data-tool]")) return;
    const form = input.closest("form");
    // Research already searches the web, so the two don't need to be on together
    if (input.checked && input.name === "research") form.querySelector('input[name="search"]')?.click();
    if (input.checked && input.name === "search") {
      const research = form.querySelector('input[name="research"]');
      if (research?.checked) research.checked = false;
    }
    updateToolChips(form);
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
    composer.querySelectorAll("[data-files]").forEach((input) => { input.disabled = waiting; });
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
    events.addEventListener("searching", (e) => {
      const data = JSON.parse(e.data);
      const typing = document.getElementById("m" + data.id)?.querySelector(".typing");
      if (typing) typing.textContent = `Searching the web for “${data.query}”…`;
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
