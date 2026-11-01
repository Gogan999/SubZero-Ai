# Plan: more tools for student Code sessions

Status: plan only, nothing built. Written 2026-11-01 for Logan. It covers four questions: where Code sessions run
today, whether they should move into disposable containers, whether the model and provider can be picked per chat, and
whether model defaults can be dropdowns in Settings. File references are to `main` at v0.8.0.

## Short answers

1. **Where do Code sessions run today?** On the mini PC itself, as an ordinary subprocess of the app. There is no
   virtualization, no container and no network limit. Yes, they should move to a disposable sandbox before we add any
   more tools.
2. **Can we open code and connectors to students with 48-hour disposable WSL containers?** Yes, with conditions. WSL2
   gives cheap, deletable environments, but it is a convenience boundary, not a hard one. It is good enough for
   classroom risk only if egress goes through a proxy we control and the service account problem (below) is solved.
   Test it for an hour on the real mini PC before committing.
3. **Mentor-only model/provider dropdown when creating a chat?** Yes. It is small: no migration, one route and one
   template change, plus a safety re-check.
4. **Model defaults as dropdowns in Settings?** Yes. Small. The list already exists in the gateway; Settings just shows
   a free-text box today.

## 1. How Code sessions run today

Findings (details in the research notes this plan was built from):

- Claude only has six custom tools: `list_files`, `read_file`, `search` (automatic) and `write_file`, `edit_file`,
  `run_command` (each waits for a mentor). No Bash, no git, no web, no MCP (`code_runner.py:81-116`).
- `run_command` is a plain `subprocess.Popen` with no shell, only for programs in `code_commands` (default
  `gradlew`), in `DATA_DIR/code/<id>/work` with a whitelisted environment (`code_runner.py:337-395`).
- On Windows it runs as `NT SERVICE\SubZeroAI`. That account has **Modify on the whole data folder**, so a command
  can in principle read or rewrite `config.env`, the SQLite database and everyone's chats.
- No network limit: a Gradle build can reach any host and the local network.
- Limits that exist: 600 s per command, 20,000-character output tail, 40 replies per run, 100 MB repo download.
  Limits that don't: disk quota, spend cap, per-user or global session count, and output is buffered in memory until
  the command exits.
- Only mentors can use Code, and every write or command needs a mentor click. Reads are automatic.
- Cleanup removes finished copies after 14 days. Waiting or running sessions are never cleaned up.
- The GitHub connector is read-only and its token is not exposed to commands. Nothing is pushed; a mentor downloads
  a patch.

Problems worth fixing regardless of this plan (ranked):

1. Commands can rewrite the data folder. Either move the DB and `config.env` out of the service account's reach, or
   remove Modify there.
2. The build-file guard is a regex on file names (`BUILD_FILE_RE`). A trailing-dot name like `gradlew.bat.` slips past
   it, and Windows drops the dot, so the real `gradlew.bat` is overwritten. Arguments such as `-I` or `--init-script`
   are passed through to Gradle, so Claude can write an init script and run it. A mentor still sees the command, but the
   stated guarantee does not hold.
3. Unbounded command output in memory; no disk or spend quota; unfinished sessions never pruned.
4. `stop()` does not kill a running command.

**Answer: yes, it should run in a disposable sandbox.** Today's safety rests entirely on mentors reading each card.
Opening Code to students (who cannot judge a command) removes that backstop, so isolation has to carry the weight.

## 2. Disposable WSL sandboxes, deleted after 48 hours

### How it would work

- One WSL2 distro per Code session, created with `wsl --import` from a prebuilt, hash-pinned Linux root filesystem
  and deleted with `wsl --unregister` (this removes its disk file). The root filesystem is downloaded on first use,
  not bundled, so the release zip stays about 255 MB.
- The app keeps running the Claude loop. Tool calls (`run_command`, file edits, reads) execute inside the distro via
  `wsl.exe -d <name> -- ...` instead of on the host. Files live inside the distro, not in `DATA_DIR`.
- New tools become possible because the sandbox is disposable: a real shell, `git`, a package manager, Python and
  Java toolchains, and test runners. Each can be switched on per tier (see Decisions).
- A `sandbox_sessions` table (new migration, next is v14) records distro name, owner, created, expires and state.
  The existing maintenance job (`maintenance.py`) terminates and unregisters anything past 48 hours, and on every
  service start it also reaps any distro with our name prefix that has no record.
- Before deletion the app saves what the student needs: the patch, the transcript (already in the DB) and a zip of
  the working folder if small. Steps and transcripts stay in the DB as now.

### Controls, and how well WSL does each

| Control | Plan | Honest limit |
|---|---|---|
| Filesystem isolation | `/etc/wsl.conf`: `[automount] enabled=false`, `[interop] enabled=false, appendWindowsPath=false`; no host folders mounted | Distros start as root and the student can edit that file. All distros share one VM and kernel. The app must re-verify the config on each start. Fine against mistakes, not against a determined attacker |
| Network egress | Per-session token plus a proxy on the host allowing only the model API, GitHub and package mirrors; host firewall blocks all other traffic from the WSL VM, including the LAN | Windows/Hyper-V firewall rules apply to the whole WSL VM, not per distro, and the Hyper-V firewall needs Windows 11 22H2 or later. Per-student control has to come from the proxy. `nftables` inside the distro is only a convenience |
| Secrets | Never mount `config.env`. The Anthropic key stays with the host proxy, which adds it upstream. GitHub: a short-lived, read-only, per-session token if possible, otherwise the host fetches the repo and copies it in | The 48-hour token must be revoked at expiry. Do not hand a write token to a sandbox |
| CPU, RAM | `.wslconfig` caps the whole VM (for example 8 GB, half the cores). Per-session caps via cgroups inside the distro (memory about 1 GB, `pids.max`, CPU share) | `.wslconfig` changes need `wsl --shutdown`, which stops every session. Cgroup limits are set by root inside the distro, so again advisory |
| Disk | `defaultVhdSize` around 20 GB per distro, plus a quota check by the reaper | Whether `defaultVhdSize` applies to imported distros is unverified |
| Cleanup | Reaper at 48 hours, startup reconcile, delete on mentor request | Unverified whether `unregister` needs the distro stopped; terminate first |
| Concurrency | Start at 4 sessions on a 16 GB machine, raise after measuring | Estimate only |

### Windows mini PC feasibility: the one real blocker to test first

WSL distros are registered per Windows user. The app runs as `NT SERVICE\SubZeroAI`, a virtual service account that may
not be able to create or see distros at all. Likely fix: run the service (or only a small sandbox-controller process)
as a dedicated local user such as `sandboxrunner`. This changes the install scripts and the account model, and has to be
proven before anything else. It is the cheapest thing to test and the likeliest to kill the approach.

Other things we do not know yet and must check on the real machine: Windows edition (WSL2 runs on Home too; Home vs Pro
decides whether Hyper-V VMs are available, so D2 chooses between WSL and Hyper-V, not whether WSL works), Windows 10 vs 11 (Hyper-V firewall), whether WSL is installed, free RAM and disk, and whether
third-party antivirus blocks WSL networking.

### Alternatives considered

- **Hyper-V VM per session** (Pro or Enterprise only): strongest isolation, own kernel and per-VM CPU/RAM caps and
  firewall. More build work. Choose this if the machine is Pro and we later want to trust the sandbox against hostile
  code.
- **Rootless Podman or Docker inside WSL**: adds per-container limits but shares the WSL kernel, so it is not stronger
  than a distro. Only worth it if we want reproducible images.
- **Windows Sandbox**: one instance at a time and discarded on close, so no 48-hour life. Rejected.
- **Windows containers**: heavy, little benefit. Rejected.

### Opening it to students: what changes and what must not

This is the part that needs Logan's say-so, because it touches the approval flow:

- Today every write and command needs a mentor click. In a disposable sandbox it is reasonable to let commands run
  without a click, since the damage is contained and deleted. **That is a relaxation of per-step approval**, which
  CLAUDE.md says not to do without Logan's decision. See Decision D1.
- What must stay: a student's request is approved by a mentor before Claude starts (the existing approval window,
  screener and MENTOR ALERT); students never get a write token to GitHub; output going out of the sandbox (patches,
  downloads) is mentor-approved; everything is written with `db.audit(...)`.
- Connectors for students: read-only repo access first. Anything that writes outward (push, comment, email) stays
  mentor-only and approved.

## 3. Mentor-only model and provider dropdown when creating a chat

Feasible and small.

- A conversation already stores its model as `provider/model` (`conversations.model`, set from `model_default` at
  creation in `approvals.py`), and mentors can already change it later (`set_model`, `web.py`, mentor-only, but only between `model_default` and
  `model_hard`). The picker widens that. So no migration is needed.
- Change: the New chat composer shows a `<select>` for mentors only (students keep the static label). `new_chat`
  (`web.py`) reads a `model` field only when `viewer.is_mentor`; for students it keeps ignoring it.
- The option list comes from one shared helper used by both New chat and `set_model`, and `set_model` must apply the
  same safety-check filter, not just the shared list. It lists only models that are
  (a) from an enabled provider with a key, (b) valid for the active backend (on `anthropic` only Claude models), (c)
  passed the safety check (`requirement_problem`), and (d) not blocked by `check_model`. It also shows cost where the
  gateway reports it.
- Safety: re-run the requirement check when the chat is created and when a reply is sent, not only when settings are
  saved. Today a model can pass at save time and stay usable after the check changes. Also show a note that students
  replying in a mentor-created chat run on that chat's model.
- The list is loaded once at startup (`_load_model_names`, `web.py:116`). Add a refresh when providers or keys change in Settings.
- Tests: students posting `model` to `/new` are ignored; unchecked or unkeyed models rejected; Anthropic backend lists
  only Claude models.

## 4. Model defaults as dropdowns in Settings

Feasible and small, and it shares the helper from section 3.

- Today `model_default`, `model_hard`, `model_titles`, `model_screener` and `model_check_judge` are free-text fields
  (`saved_settings.py`, `kind="model"`). A `choice` select exists only for the config.env fields
  (`server_settings.EnvField`), not in the database-backed `saved_settings.Field`, so M1 must add a choice kind there.
- Build the options from the same helper, grouped by provider, with a hint when a model has not passed the safety
  check (saving still blocks it as now). The screener and judge lists are narrower: the judge must be a Claude model.
- Keep a "type another model id" fallback so a new model is not blocked while the list is stale.
- Update `docs/SETTINGS.md`; the existing test that fails when a field is missing from it will cover coverage.

## Recommendation

Do it in order, and stop after each milestone for Logan to look:

1. Fix today's gaps and add the model dropdowns (M0 to M2). They are small, safe and useful immediately.
2. Prove WSL works for a service user (M3). If it fails, fall back to Hyper-V if the machine is Pro, otherwise keep
   Code mentor-only and just harden it.
3. Only then build the sandbox and open tools to students (M4 to M6), starting with the least-risky tier.

## Milestones

- **M0 Harden today's Code (about 1 day).** Drop the service account's Modify on the data folder (or move DB and
  `config.env`); make the build-file guard compare normalized paths; cap output while streaming; stop kills the running
  command; add a disk and spend cap; prune unfinished sessions after a limit.
- **M1 Shared model list helper and Settings dropdowns (about 1 day).** Section 4, plus periodic refresh.
- **M2 Mentor model picker in New chat (about 1 day).** Section 3, plus the send-time safety re-check.
- **M3 WSL spike on the mini PC (about half a day, Logan's machine).** One-hour test: service user creates two
  distros, they cannot see each other, LAN and host are unreachable, `unregister` removes the disk file, WSL starts
  from the service, and `.wslconfig` is read from the profile of the user running `wsl.exe` (a virtual service account's
  profile is under `C:\Windows\ServiceProfiles\`), so the VM caps apply in that account's or `sandboxrunner`'s profile. Records the Windows edition and spare RAM. Go or no-go for M4.
- **M4 Sandbox controller (about 3 days).** `sandbox.py`: import, exec, limits, reaper, startup reconcile,
  `sandbox_sessions` migration, audit entries, a fake sandbox for tests so the suite stays offline. Still
  mentor-only, per-step approval unchanged.
- **M5 Egress proxy and secrets (about 2 days).** Allowlist proxy with per-session tokens, host firewall rule,
  revocation at expiry, checker in the Settings overview.
- **M6 Wider tools and student access (about 3 days).** Add shell, git, toolchains, read-only repo connector; student
  start with mentor approval of the request; tiered auto-run depending on Decision D1; session list and "ends in 41
  hours" notice; patch export before deletion.

## Risks

- WSL is one shared VM and kernel: a student with root in a distro is not a hard boundary. Mitigation: proxy-only
  egress, no mounted host folders, treat as classroom-grade, upgrade to Hyper-V if hostile use shows up.
- The service account may not be able to run WSL (M3). This could force a service-account change or a Hyper-V route.
- Egress control: Windows firewall rules are per VM, not per student; Windows 10 has no Hyper-V firewall; antivirus can
  interfere.
- Resource exhaustion on a shared mini PC: one runaway build could slow the whole site. Mitigation: VM caps, per-session
  cgroups, concurrency limit, spend cap.
- Cost: more tools means more model turns per student. Needs a per-student daily cap before M6.
- Letting commands run without a mentor click is a real change to the approval model (D1).
- Unsafe or unchecked models appearing in a dropdown. Mitigation: only checked models, re-check at send time.
- Stale model list and missing provider keys causing failures at send time. Mitigation: filter by key, refresh.
- Distros left behind after a crash. Mitigation: startup reconcile and a periodic reaper.
- Unverified claims here (service-account WSL, VHD size on imported distros, sizing numbers) are guesses until M3.

## Decisions needed from Logan

- **D1.** In a student sandbox, should commands run without a mentor click (recommended: yes for sandbox-only commands,
  no for anything leaving the sandbox), or keep per-step approval for everything? This touches the approval flow, so
  nothing ships until you decide.
- **D2.** What Windows edition and version is the mini PC (Home or Pro, Windows 10 or 11)? This decides WSL vs Hyper-V.
- **D3.** Is it fine to run the service, or a small sandbox helper, under a dedicated local user if M3 shows we need it?
- **D4.** Which tools first for students: read-only repo browsing and a shell, or also package installs and web access?
- **D5.** Should a student's Code session need a mentor to approve the starting request (recommended), and who may
  extend a session past 48 hours?
