// SubZero AI browser script: mentor review actions, the read-only chat page, Code and Settings.
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

  // On phones the sidebar opens over the page from the menu button in the slim top bar.
  function setSidebar(open) {
    document.body.classList.toggle("sidebar-open", open);
    // The page behind the full-screen panel can't be seen, so take it out of Tab order and screen readers
    document.querySelector(".main-col")?.toggleAttribute("inert", open);
    if (open) document.querySelector("#sidebar nav a")?.focus();
    document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
      button.setAttribute("aria-expanded", String(open));
      const label = open ? button.dataset.closeLabel : button.dataset.openLabel;
      if (label) button.setAttribute("aria-label", label);
    });
  }

  // Pop-up menus (the account menu) are <details>: close them on a click elsewhere or Escape.
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
    }
  });

  // ------------------------------------------------------------------ Code
  // Ctrl+Enter (or Cmd+Enter) sends the box you are typing in
  document.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && event.target.matches?.("[data-submit-on-enter]")) {
      event.preventDefault();
      event.target.form?.requestSubmit();
    }
  });

  // A session page asks for itself again while it isn't finished: quickly while Claude works, slowly while it waits
  // for a person, and rarely in a hidden tab. Only blocks that changed are swapped, so open folds, text typed in a
  // note or the follow-up box, and the scroll position stay as they are. The follow-up box is outside the part that
  // is redrawn; its enabled state follows the page's data-composer-state block.
  const codeRegion = document.querySelector("[data-code-region]");
  if (codeRegion) {
    const scroller = document.querySelector("[data-scroller]");
    const composer = document.querySelector("[data-composer]");
    const offline = document.querySelector("[data-connection-note]");
    const drawn = new WeakMap(); // each block's HTML as the server last sent it
    const delays = { fast: 2500, slow: 6000 };
    let timer = 0;
    let busy = false;
    let stopped = false; // the session is gone (or no longer ours): stop asking
    const live = () => codeRegion.querySelector("[data-code-live]");
    const remember = (root) => root.querySelectorAll("[data-key]").forEach((el) => drawn.set(el, el.outerHTML));

    const hasUserState = (el) => {
      const active = document.activeElement;
      if (active && el.contains(active) && active.matches("input, textarea, select")) return true;
      return Array.from(el.querySelectorAll("input[name=note], textarea")).some((f) => f.value !== f.defaultValue);
    };
    const carryOpen = (from, to) => {
      const before = from.querySelectorAll("details");
      to.querySelectorAll("details").forEach((d, i) => { if (before[i]) d.open = before[i].open; });
    };
    function morph(oldParent, freshParent) {
      const olds = new Map();
      for (const child of oldParent.children) if (child.dataset.key) olds.set(child.dataset.key, child);
      const wanted = [];
      for (const fresh of Array.from(freshParent.children)) {
        const old = fresh.dataset.key ? olds.get(fresh.dataset.key) : null;
        const html = fresh.outerHTML;
        if (!old) {
          drawn.set(fresh, html);
          wanted.push(fresh);
        } else if (drawn.get(old) === html) {
          wanted.push(old);
        } else if (fresh.hasAttribute("data-morph") && old.hasAttribute("data-morph")) {
          // A list of steps: go through its items one by one, so a note someone is typing in doesn't freeze the rest
          morph(old, fresh);
          drawn.set(old, html);
          wanted.push(old);
        } else if (hasUserState(old)) {
          wanted.push(old);
        } else {
          carryOpen(old, fresh);
          drawn.set(fresh, html);
          wanted.push(fresh);
        }
      }
      for (const child of Array.from(oldParent.children)) if (!wanted.includes(child)) child.remove();
      let ref = oldParent.firstElementChild;
      for (const el of wanted) {
        if (el === ref) ref = ref.nextElementSibling;
        else oldParent.insertBefore(el, ref);
      }
    }

    function applyComposer() {
      const state = live()?.querySelector("[data-composer-state]");
      if (!state || !composer) return;
      const open = state.dataset.open === "yes";
      composer.querySelector("textarea").disabled = !open;
      composer.querySelector('button[type="submit"]').disabled = !open;
      const hint = composer.querySelector("[data-composer-hint]");
      if (hint) hint.textContent = state.dataset.hint || "";
    }

    function schedule() {
      clearTimeout(timer);
      const mode = live()?.dataset.poll;
      if (stopped || !mode || mode === "off") return;
      const base = delays[mode] || 6000;
      timer = setTimeout(refresh, document.hidden ? Math.max(base * 6, 20000) : base);
    }

    async function refresh() {
      if (busy || stopped) return;
      busy = true;
      clearTimeout(timer);
      try {
        const res = await fetch(codeRegion.dataset.url, { credentials: "same-origin", headers: { Accept: "text/html" } });
        if (res.status === 404 || res.status === 403) {
          stopped = true;
          return;
        }
        if (!res.ok) throw new Error("bad answer");
        const fresh = new DOMParser().parseFromString(await res.text(), "text/html").querySelector("[data-code-live]");
        if (!fresh) throw new Error("not a session page");
        offline.hidden = true;
        const stick = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 80;
        const root = live();
        morph(root, fresh);
        root.dataset.poll = fresh.dataset.poll;
        root.dataset.state = fresh.dataset.state;
        applyComposer();
        if (stick) scroller.scrollTop = scroller.scrollHeight;
      } catch {
        offline.hidden = false; // the next round tries again
      } finally {
        busy = false;
        schedule();
      }
    }

    remember(live());
    // A session that is still working opens at its newest step. A finished one opens at the top, under its title.
    if (live().dataset.poll === "fast") scroller.scrollTop = scroller.scrollHeight;
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && live()?.dataset.poll !== "off") refresh();
    });
    schedule();
  }

  // The Code home's "Needs a mentor" list refreshes now and then, and the counts in the sidebar and tab title with it
  const needsBox = document.querySelector("[data-needs]");
  if (needsBox) {
    setInterval(async () => {
      if (document.hidden) return;
      try {
        const res = await fetch(needsBox.dataset.url, { credentials: "same-origin" });
        if (!res.ok) return;
        const fresh = new DOMParser().parseFromString(await res.text(), "text/html").querySelector("[data-needs-list]");
        const old = needsBox.querySelector("[data-needs-list]");
        if (!fresh || !old) return;
        const change = Number(fresh.dataset.count) - Number(old.dataset.count);
        old.replaceWith(fresh);
        document.querySelectorAll("[data-needs-count]").forEach((badge) => {
          badge.textContent = fresh.dataset.count;
          badge.hidden = fresh.dataset.count === "0";
          badge.setAttribute("aria-label", `${fresh.dataset.count} waiting`);
        });
        if (change) {
          const match = document.title.match(/^\((\d+)\) /);
          const total = Math.max(0, (match ? Number(match[1]) : 0) + change);
          document.title = (total ? `(${total}) ` : "") + document.title.replace(/^\(\d+\) /, "");
        }
      } catch {
        // try again next time
      }
    }, 15000);
  }

  // A model dropdown's "Other (type a model id)" choice shows the box for typing an id.
  document.addEventListener("change", (event) => {
    const pick = event.target.matches?.("[data-model-pick]") ? event.target : null;
    const box = pick && document.querySelector(`[data-model-other="${pick.dataset.modelPick}"]`);
    if (!box) return;
    box.hidden = pick.value !== "__other__";
    if (!box.hidden) box.querySelector("input")?.focus();
  });

  function afterAction(element) {
    if (document.querySelector("[data-queue]")) {
      refreshQueue();
      return;
    }
    if (chatPage) refreshChat();
  }

  // ------------------------------------------------------- read-only chat
  // The mentor's chat page: the whole chat, with review buttons on a waiting message. It looks for changes every few
  // seconds, and at once when the Review queue changes. It leaves the page alone while a mentor is typing an edit.
  const chatPage = document.querySelector("[data-readonly-chat]");
  const thread = document.querySelector("[data-thread]");
  let chatHtml = null;

  async function refreshChat() {
    if (!chatPage || !thread) return;
    if (thread.querySelector(".inline-form:not([hidden])")) return; // don't wipe a reason or edit someone is typing
    let res;
    try {
      res = await fetch(`/c/${chatPage.dataset.readonlyChat}?thread=1`, { credentials: "same-origin" });
    } catch {
      return; // try again on the next round
    }
    if (!res.ok) return;
    const html = (await res.text()).trim();
    if (chatHtml === null) chatHtml = thread.innerHTML.trim();
    if (html === chatHtml) return;
    chatHtml = html;
    thread.innerHTML = html;
  }

  if (chatPage && thread) {
    const events = new EventSource("/queue/events");
    events.addEventListener("queue", refreshChat);
    setInterval(() => {
      if (!document.hidden) refreshChat();
    }, 4000);
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

  // Settings: show one section at a time, picked from the menu. The address keeps the open section or box
  // (#tab-ai, or a box like #bridge), so saving a form comes back to the same place. Without this script every
  // section shows on one long page.
  const settingsTabs = Array.from(document.querySelectorAll("[data-settings-tab]"));
  if (settingsTabs.length) {
    const tabLinks = Array.from(document.querySelectorAll('.settings-nav a[href^="#tab-"]'));
    // Boxes that moved to another tab keep working from old links: the page lists old name -> new name
    let anchors = {};
    try {
      anchors = JSON.parse(document.querySelector("[data-anchors]")?.dataset.anchors || "{}");
    } catch {
      /* no map: old names that still exist work as they are */
    }
    const tabFor = (id) => {
      const el = id && document.getElementById(id);
      return el ? el.closest("[data-settings-tab]") : null;
    };
    const openFolds = (el) => {
      for (let d = el?.closest("details"); d; d = d.parentElement?.closest("details")) d.open = true;
    };

    // Unsaved edits: ask before leaving the page or another tab, but only if a box really changed
    const forms = Array.from(document.querySelectorAll(".settings-page form")).filter((f) => f.querySelector('[name="csrf"]'));
    const stateOf = (form) =>
      JSON.stringify(Array.from(new FormData(form)).filter(([name, value]) => name !== "csrf" && typeof value === "string"));
    const startState = new Map(forms.map((f) => [f, stateOf(f)]));
    const isDirty = (form) => startState.get(form) !== stateOf(form);
    const dirtyIn = (tab) => forms.filter((f) => tab.contains(f) && isDirty(f));
    let submitting = false;
    document.addEventListener("submit", () => {
      submitting = true;
      setTimeout(() => (submitting = false), 3000); // a submit that didn't leave the page guards again
    });
    window.addEventListener("beforeunload", (event) => {
      if (!submitting && forms.some(isDirty)) {
        event.preventDefault();
        event.returnValue = "";
      }
    });

    const showTab = (tab) => {
      for (const t of settingsTabs) t.hidden = t !== tab;
      for (const a of tabLinks) {
        if (a.getAttribute("href") === "#" + tab.id) {
          a.setAttribute("aria-current", "true");
          a.scrollIntoView({ block: "nearest", inline: "nearest" });
        } else {
          a.removeAttribute("aria-current");
        }
      }
    };
    const fromAddress = () => {
      let id = "";
      try {
        id = decodeURIComponent(location.hash.slice(1));
      } catch {
        /* a broken address: open the first section */
      }
      if (id && !document.getElementById(id) && anchors[id]) id = anchors[id];
      // A form that came back with mistakes opens where the first mistake is
      const invalid = document.querySelector('[data-settings-tab] [aria-invalid="true"]');
      const tab = tabFor(id) || (invalid && invalid.closest("[data-settings-tab]")) || settingsTabs[0];
      showTab(tab);
      const target = id && document.getElementById(id);
      openFolds(target || invalid);
      if (target && target !== tab) target.scrollIntoView();
      else if (invalid && !id) invalid.focus();
      else if (target) window.scrollTo(0, 0); // a whole section: start at the top, under the page title
    };
    for (const a of tabLinks) {
      a.addEventListener("click", (event) => {
        event.preventDefault();
        const tab = tabFor(a.getAttribute("href").slice(1));
        if (!tab) return;
        const here = settingsTabs.find((t) => !t.hidden);
        const dirty = here && here !== tab ? dirtyIn(here) : [];
        if (dirty.length) {
          if (!window.confirm("You changed something on this tab and haven't saved it. Leave without saving?")) return;
          for (const f of dirty) f.reset();
        }
        history.replaceState(null, "", a.getAttribute("href"));
        showTab(tab);
        window.scrollTo(0, 0);
      });
    }
    // Links inside the page (like "see Claude subscription") can point into another section
    window.addEventListener("hashchange", fromAddress);
    fromAddress();
    // The browser jumps to the address's box once the page has loaded, after this ran: put the right place back
    window.addEventListener("load", () => setTimeout(fromAddress, 0), { once: true });

    // The old-chats archive or delete job: show how far it is, and show the result when it finishes
    const job = document.querySelector("[data-archive-progress]");
    if (job) {
      const tick = async () => {
        try {
          const res = await fetch("/settings/archive/status", { credentials: "same-origin", headers: { Accept: "application/json" } });
          const data = res.ok ? await res.json() : null;
          if (data && data.state !== "running") {
            if (forms.some(isDirty)) {
              job.querySelector("[data-archive-step]").textContent = "Finished. Reload the page to see the result.";
            } else {
              submitting = true;
              location.reload();
            }
            return;
          }
          if (data) {
            job.querySelector("[data-archive-step]").textContent = data.step;
            const bar = job.querySelector("[data-archive-bar]");
            if (bar) bar.value = Math.min(100, Math.round((100 * data.done) / Math.max(data.total, 1)));
          }
        } catch {
          /* try again on the next round */
        }
        setTimeout(tick, 2000);
      };
      setTimeout(tick, 2000);
    }
  }

  // Files page: the box in the table head ticks or clears every row
  const selectAll = document.querySelector("[data-select-all]");
  if (selectAll) {
    selectAll.addEventListener("change", () => {
      document.querySelectorAll('[data-files-form] input[name="ids"]').forEach((box) => { box.checked = selectAll.checked; });
    });
  }
})();
