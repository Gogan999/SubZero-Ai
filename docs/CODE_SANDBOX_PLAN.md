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

### The mini PC (checked read-only on 2026-11-01)

- Windows 11 Pro 23H2 (build 22631). Hyper-V is enabled and the hypervisor is running. Virtual Machine Platform and WSL
  are on; Windows Sandbox is off. WSL 2 already has `Ubuntu` and `docker-desktop` distros (both stopped).
- Intel i5-9500T, 6 cores, 6 threads (no hyperthreading). 23.8 GB RAM (12.3 GB free when checked). One drive, `C:`,
  75.7 GB free of 237.6 GB.
- The service runs as `NT SERVICE\SubZeroAI` from `C:\Program Files\SubZeroAI\service`.
- Firmware virtualization is inferred as on (a hypervisor is running); the reported flag can read False in that case.
- 23H2 Pro may no longer receive security updates; check Windows Update, since the sandbox host should be patched.

- Hyper-V Administrators currently has no members, so `NT SERVICE\SubZeroAI` cannot manage VMs. Adding it gives it
  control of every VM on the host, which is broad. Prefer a small dedicated controller (own low-privilege account in
  that group, narrow API to the app) over adding the web service itself. M3 must confirm this works for a service SID.
- Remote Control sessions run non-elevated (UAC), so Hyper-V cmdlets fail there; the spike needs an elevated PowerShell.

- **M3 Hyper-V spike result (2026-11-01, elevated, all cleaned up): passed.** A gen 2 VM with 1 GB RAM, 1 vCPU and a
  50% CPU cap was created and the caps applied while running. A Private switch with no host adapter worked (but the host cannot talk to a VM on a Private switch, so the real design uses an Internal switch; see "Network design"). A
  differencing disk was created from a parent and deleting the child left the parent unchanged. Not tested: PowerShell
  Direct (needs a guest OS image), traffic from a real guest, and adding `NT SERVICE\SubZeroAI` to Hyper-V
  Administrators (its name resolves to a valid SID, so it looks possible).
- The PC also has an External switch (`TrueNAS`), an Internal one (`Default Switch`) and a `TrueNas` VM. Sandbox VMs must
  attach only to their own internal switch (`szsandbox-internal`), and the controller must verify that before starting a VM.

Sizing from this: **disk is the tightest limit.** Plan 2 to 4 cores and 4 to 8 GB RAM for all VMs together, 2 to 3
concurrent sessions at 2 GB RAM and 1 to 2 cores each, a shared read-only base disk with a differencing disk per
session capped near 10 GB, and refuse new sessions when free disk drops under about 20 GB. Raise limits only after
measuring. Another option if demand grows: run sessions on a second small machine.

### Network design (Hyper-V)

- A Hyper-V **Private** switch carries traffic only between VMs; the host has no adapter on it, so the app could not
  reach the guest. The sandbox therefore uses an **Internal** switch (`szsandbox-internal`) with a host adapter at
  10.77.0.1/24, no gateway, no NAT, IP forwarding off. The guest gets a fixed address 10.77.0.<slot>.
- An Internal switch lets a guest reach every port the host listens on (SMB, RDP and so on) and every IPv6 address.
  The host therefore firewalls the sandbox adapter with six `szsandbox-*` inbound rules, all `-RemoteAddress Any` (except
  the allow), `-Profile Any` and `-InterfaceAlias` the adapter:
  - `szsandbox-block-tcp-below` (TCP 1 to proxy port minus 1) and `szsandbox-block-tcp-above` (TCP proxy port plus 1 to
    65535) block TCP on every port except the proxy's. A range that would be empty (proxy port 1 or 65535) is left out.
  - `szsandbox-block-udp`, `szsandbox-block-icmp4` and `szsandbox-block-icmp6` block those protocols whole.
  - `szsandbox-allow-proxy` allows only TCP to the proxy port, from 10.77.0.0/24.
  Windows Firewall gives a block rule priority over an allow rule, so a block that covered the proxy port would stop the
  allow working. That is why the TCP blocks are split around the proxy port: the only port left open is the proxy's.
  Other IP protocols (GRE, ESP and so on) are not named here; they fall under the Windows Firewall default inbound
  action. The check does not read that default, so M3b must confirm it is Block for the Public profile.
- IPv6 is switched off at both ends. The install script unbinds TCP/IPv6 (`ms_tcpip6`) from the host's adapter, and the
  check requires it. The base image must also turn IPv6 off inside the guest, for example with
  `net.ipv6.conf.all.disable_ipv6=1` in a sysctl file, so a guest never has an IPv6 address. The per-VM ACL denies IPv6
  as a second layer (below).
- The adapter's network profile is set to Public. The app never creates these rules. An administrator runs
  `packaging/windows/scripts/sandbox-network.ps1 -Action Install` once (it lives with the other install scripts, so
  the release zip ships it). `HyperVSandbox.create` runs the read-only host check first (`host_network_check_script`
  in `subzero_ai/sandbox.py`) and refuses to make a sandbox until it passes; a pass is kept for 60 seconds, a failure
  never. The check names each missing or wrong rule (`block-rule-missing:<rule name>`, `proxy-rule-wrong`,
  `ipv6-enabled` and so on). The Settings overview shows the same check, as a failure with the fix when it doesn't pass.
  The problem codes, rule names and PowerShell address helpers are kept identical in `sandbox.py` and the script by
  `SandboxNetworkDriftTest` in `tests/test_sandbox.py`.
  Firewall addresses are compared in prefix form. Windows may report a subnet as `10.77.0.0/255.255.255.0`, so both
  scripts convert that to `10.77.0.0/24` before comparing.
- Sessions must not reach each other: each VM adapter gets a static MAC, DHCP and router guard, and extended ACLs
  (weights in brackets): allow 10.77.0.1/32 (200), deny 10.77.0.0/24 (100), and deny all IPv6, `::/0` (150), so no guest
  can reach another over an fe80:: link-local address. The cmdlets are written but unverified until the M3b test on the
  mini PC. Read the rules back with `Get-VMNetworkAdapterExtendedAcl` there.
- The guest is driven over SSH from the host with a per-session host key (scanned once over the host-only link) and
  `StrictHostKeyChecking=yes`. Guest output is streamed into a bounded tail and archives are capped, so a guest cannot
  fill the host's memory or disk.

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

- **Goal (Logan, 2026-11-01): the point of the sandbox is to give students free rein over the commands that run.**
  Inside a session, Claude's shell commands and file edits run with no mentor click. Today's per-step approval
  stays only for today's host-based Code (mentors) until the sandbox replaces it. This is a deliberate relaxation of
  per-step approval, decided by Logan (D1, resolved).
- Because nothing is clicked, **the sandbox itself is the only safety boundary**, so it has to be stronger than a
  convenience layer. That raises the bar for isolation (see "Free rein raises the bar" below).
- What must stay (these are not per-command clicks): a student's request is approved by a mentor before Claude starts (the existing approval window,
  screener and MENTOR ALERT); students never get a write token to GitHub; output going out of the sandbox (patches,
  downloads) is mentor-approved; everything is written with `db.audit(...)`.
- Connectors for students: read-only repo access first. Anything that writes outward (push, comment, email) stays
  mentor-only and approved.

### Free rein raises the bar

With no per-command approval, a student (or Claude acting on a student's prompt) can run anything inside the sandbox:
install packages, run servers, loop forever, scan the network, mine coins, try to break out. The design therefore
must make every one of those harmless:

- **Breakout:** WSL2's shared kernel and root-in-distro make this the weakest point. Prefer a Hyper-V VM per session
  if the machine is Pro or Enterprise (own kernel, own firewall and CPU/RAM caps). If only Home, use WSL2 and accept the
  risk with the other controls tight, or run sessions on a separate cheap machine. D2 now matters more.
- **Outbound abuse:** allowlist-only egress through the proxy, no LAN, no direct internet. Package mirrors only. Free
  web access is a later, separate decision (D4).
- **Resource abuse:** hard CPU, RAM, disk, process and wall-clock limits per session, a global concurrency cap, and
  automatic kill when a session goes over.
- **Data:** nothing of the site's is reachable: no host mounts, no `config.env`, no DB, no real keys, only the
  per-session proxy token.
- **Blast radius:** one session per distro or VM; a session is deleted on abuse detection, not just at 48 hours.
- **Visibility:** every command is still logged and audited (`db.audit`), and mentors can watch live, stop or delete a
  session. Free rein means no click-to-approve, not no oversight.

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

Update 2026-11-01: the mini PC is Windows Pro and students get free rein, so use a **Hyper-V VM per session** (own
kernel, per-VM CPU/RAM caps, per-VM firewall and network switch), created from a hash-pinned base disk as a
differencing disk and deleted at 48 hours. WSL2 is the fallback only if Hyper-V turns out unusable. The sections above
describe WSL in detail because it was the original question; read its table as the fallback design.

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
- **M3 Hyper-V (and WSL fallback) spike on the mini PC (about half a day, Logan's machine).** Test Hyper-V first: the service user can create, start and delete a VM from a base differencing disk, per-VM memory/CPU caps apply, an Internal switch with a host adapter at 10.77.0.1 reaches only the host, host firewall rules block the host's other ports from that adapter, per-VM ACLs stop one VM reaching another, and the host can run commands in the guest over SSH (PowerShell Direct is Windows-guest only). Then, only if that fails, the WSL test below. One-hour test: service user creates two
  distros, they cannot see each other, LAN and host are unreachable, `unregister` removes the disk file, WSL starts
  from the service, and `.wslconfig` is read from the profile of the user running `wsl.exe` (a virtual service account's
  profile is under `C:\Windows\ServiceProfiles\`), so the VM caps apply in that account's or `sandboxrunner`'s profile. Records the Windows edition and spare RAM. Go or no-go for M4.
- **M4 Sandbox controller (about 3 days).** `sandbox.py`: import, exec, limits, reaper, startup reconcile,
  `sandbox_sessions` migration, audit entries, a fake sandbox for tests so the suite stays offline. Still
  mentor-only first so the isolation can be proven before students get it; per-step approval is removed inside the
  sandbox here (Logan's decision).
- **M5 Egress proxy and secrets (about 2 days).** Allowlist proxy with per-session tokens, host firewall rule,
  revocation at expiry, checker in the Settings overview.
- **M6 Wider tools and student access (about 3 days).** Add shell, git, toolchains, read-only repo connector; student
  start with mentor approval of the request; free-rein commands for students; session list and "ends in 41
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
- Letting commands run without a mentor click is a real change to the approval model. Logan chose it on 2026-11-01;
  the cost is that isolation must be strong, so Hyper-V is preferred over WSL when available.
- Unsafe or unchecked models appearing in a dropdown. Mitigation: only checked models, re-check at send time.
- Stale model list and missing provider keys causing failures at send time. Mitigation: filter by key, refresh.
- Distros left behind after a crash. Mitigation: startup reconcile and a periodic reaper.
- Unverified claims here (service-account WSL, VHD size on imported distros, sizing numbers) are guesses until M3.

## Decisions needed from Logan

- **D1 (resolved 2026-11-01).** Commands inside a student sandbox run freely with no mentor click. That is why the
  sandbox is needed.
- **D2 (resolved 2026-11-01).** The mini PC runs Windows 11 Pro. Hyper-V is available,
  so the recommended sandbox is **one Hyper-V VM per session**, not WSL. The M3 spike tests Hyper-V first; WSL is the
  fallback.
- **D3.** Is it fine to run the service, or a small sandbox helper, under a dedicated local user if M3 shows we need it?
- **D4.** Which tools first for students: read-only repo browsing and a shell, or also package installs and web access?
- **D5.** Should a student's Code session need a mentor to approve the starting request (recommended), and who may
  extend a session past 48 hours?
