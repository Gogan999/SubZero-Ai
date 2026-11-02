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
  The host therefore firewalls the sandbox adapter with eleven `szsandbox-*` inbound rules, all `-RemoteAddress Any`
  (except the allow), `-Profile Any` and `-InterfaceAlias` the adapter:
  - `szsandbox-block-tcp-below` (TCP 1 to proxy port minus 1) and `szsandbox-block-tcp-above` (TCP proxy port plus 1 to
    65535) block TCP on every port except the proxy's. A range that would be empty (proxy port 1 or 65535) is left out.
  - `szsandbox-block-udp`, `szsandbox-block-icmp4` and `szsandbox-block-icmp6` block those protocols whole.
  - `szsandbox-block-proto-<n>` for n in 2 (IGMP), 41 (IPv6 in IPv4), 47 (GRE), 50 (ESP) and 51 (AH) block each IP
    protocol whole. The list is `BLOCK_PROTOCOLS` in `subzero_ai/sandbox.py`, and the script's copy is generated from it.
  - `szsandbox-allow-proxy` allows only TCP to the proxy port, from 10.77.0.0/24.
  Windows Firewall gives a block rule priority over an allow rule, so a block that covered the proxy port would stop the
  allow working. That is why the TCP blocks are split around the proxy port: the only port left open is the proxy's.
  Any other IP protocol falls under the Windows Firewall default inbound action, which the check reads for the Public
  profile: it must be Block (`default-inbound-allow` otherwise). M3b must confirm that reading on the mini PC.
- Existing allow rules on the host need no change. Windows Firewall lets an interface-scoped block win over an allow
  rule, so an allow such as File and Printer Sharing or Remote Desktop, on Public or Any, cannot reach the sandboxes
  over TCP, UDP, ICMP or the protocols above. The check therefore does not scan allow rules: an earlier scan of them
  flagged 48 ordinary rules on a typical PC and took about 89 seconds, so it was removed. The check reads only the
  `szsandbox-*` rules, with one query and each rule's filters read once, and it should finish well under 10 seconds.
- IPv6 is switched off at both ends. The install script unbinds TCP/IPv6 (`ms_tcpip6`) from the host's adapter, and the
  check requires it. The base image must also turn IPv6 off inside the guest, for example with
  `net.ipv6.conf.all.disable_ipv6=1` in a sysctl file, so a guest never has an IPv6 address. The per-VM ACL denies IPv6
  as a second layer (below).
- The adapter's network profile is set to Public. The app never creates these rules. An administrator runs
  `packaging/windows/scripts/sandbox-network.ps1 -Action Install` once (it lives with the other install scripts, so
  the release zip ships it). `HyperVSandbox.create` runs the read-only host check first (`host_network_check_script`
  in `subzero_ai/sandbox.py`) and refuses to make a sandbox until it passes; a pass is kept for 60 seconds, a failure
  never. The check names each missing or wrong rule (`block-rule-missing:<rule name>`, `proxy-rule-wrong`,
  `ipv6-enabled`, `default-inbound-allow` and so on). The Settings overview shows the same check, as a failure with the fix when it doesn't pass.
  The problem codes, rule names and PowerShell address helpers are kept identical in `sandbox.py` and the script by
  `SandboxNetworkDriftTest` in `tests/test_sandbox.py`.
  Firewall addresses are compared in prefix form. Windows may report a subnet as `10.77.0.0/255.255.255.0`, so both
  scripts convert that to `10.77.0.0/24` before comparing.
- Sessions must not reach each other: each VM adapter gets a static MAC, DHCP and router guard, and extended ACLs
  (weights in brackets): allow 10.77.0.1/32 (200), deny 10.77.0.0/24 (100), and deny all IPv6, `::/0` (150), so no guest
  can reach another over an fe80:: link-local address. The ACLs do not stop non-IP frames between guests (measured in
  M3b), so each sandbox adapter is also put in a **private VLAN** (primary 100, secondary 200, isolated mode) by
  `isolation_script`, and the host's adapter is promiscuous on the same pair (`sandbox-network.ps1 -Action Install`).
  The host check reads that back (`host-vlan-missing`), and the VM's VLAN is read back after it starts. A VM not
  isolated in the pair is destroyed.
- The guest is driven over SSH from the host with a per-session host key pair and `StrictHostKeyChecking=yes`. The
  host makes the key pair with `ssh-keygen.exe` and locks it with `icacls`, and the public half goes into that
  sandbox's own known_hosts file. The private half goes to the guest on a NoCloud seed disk (a 64 MB FAT32 disk
  labelled CIDATA, built with built-in tools) that is attached before the VM starts. Once SSH answers and cloud-init has
  read the seed, the seed is detached and deleted and the host's copy of the private key is shredded. Nothing is read
  over the network: `ssh-keyscan` fails on Windows OpenSSH (unsupported key exchange), so it is not used. The guest
  user, `sandbox`, takes only the app's client key, has no password and no sudo unless `SANDBOX_GUEST_SUDO` is on.
  Guest output is streamed into a bounded tail and archives are capped, so a guest cannot fill the host's memory or
  disk.

### Base image signature check (Build)

Build checks the release's `SHA256SUMS` against the Ubuntu cloud image signing key (`SHA256SUMS.gpg`) before it
downloads the 30 GB image. It needs `gpg.exe` (Gpg4win). Without it, Build says the check was skipped, and the SHA-256
check of the image still runs. The key ID is `1A5D6C4C7DB87C81`, and the fingerprint is pinned in
`sandbox-image.ps1` as `D2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81`. **Re-check that fingerprint against
https://ubuntu.com/tutorials/how-to-verify-ubuntu before the first import, and again whenever the script changes.**

An administrator imports the key once, as the admin account that runs Build:

```
gpg --keyserver hkps://keyserver.ubuntu.com --recv-keys 0xD2EB44626FDDC30B513D5BB71A5D6C4C7DB87C81
gpg --fingerprint 1A5D6C4C7DB87C81
```

The fingerprint that the second command prints must match the pinned one. The script itself never calls a keyserver;
it refuses to continue when the key is missing from the keyring. Use `-VerifySignature:$false` only for a test.

### M3b results (measured on the mini PC)

- **ACLs do not stop non-IP frames** between two sandbox adapters on the same switch, so the private VLAN (100 with
  200) is what keeps them apart. The VLAN cmdlets were run: `Set-VMNetworkAdapterVlan` on the host adapter
  (`-ManagementOS -Promiscuous -PrimaryVlanId 100 -SecondaryVlanIdList 200`) and on each VM (`-Isolated`), and both read
  back as `Private` with the right IDs. Not yet confirmed: that `PrivateVlanMode` reads `Isolated` on a VM and
  `Promiscuous` on the host (the code requires these, so a mismatch refuses the sandbox rather than letting it run).
- **Boot times:** first boot to SSH about 68 seconds; later boots about 12 seconds.
- **Idle cost:** about 370 MB of RAM per running VM, and about 200 MB of disk per sandbox (its differencing disk).
- **Ports:** the guest's SSH is on TCP 22 and the host's proxy port is the default 3128. Only the proxy port is open to
  a sandbox (see the firewall rules above).
- **Host keys:** `ssh-keyscan` can't read a guest's key on Windows OpenSSH (unsupported key exchange), so host keys are
  injected instead (see above).
- **Seed disk:** the NoCloud seed is built with built-in tools only (New-VHD, Mount-VHD, Initialize-Disk, New-Partition,
  Format-Volume FAT32, Copy-Item).
- **Still to test:** the real `Install` and `Check` runs of `sandbox-network.ps1` with the VLAN step, and a full
  create, command and destroy on a real host with the seed flow. The app's tests cover the script text and the fakes
  only.

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

## Known limits

- **Uploads as well as downloads.** The egress proxy allows the sites on the allow-list (github.com and pypi.org by
  default) in both directions. The allow-list limits where data can go, not which way it goes: a command in a sandbox
  can still send data to a listed site, for example by uploading it with a request.
- **No WSL fallback.** The WSL sandbox in section 2 is not built. Only Hyper-V sandboxes exist, so a computer without
  Hyper-V gets no sandboxes.
- **GRUB loop picks the last kernel alphabetically.** The boot menu's loops keep the last `/vmlinuz-*` and
  `/initrd.img-*` match in alphabetical order, which is the newest kernel only while the version numbers have the same
  length. Check the boot menu after each new base image.
- **`ci.ds=NoCloud` is untested in its other form.** Build writes `ci.ds=NoCloud`, the form the image was tested with.
  cloud-init 23.2 and later warn about this form of the kernel argument. The lowercase `ds=nocloud` form has not been
  tested, so it is not used. Re-test the seed (a sandbox must start, read its seed and reach SSH) with each new Ubuntu
  release before accepting its boot menu.

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
- **M5 Egress proxy and secrets (about 2 days). Built 2026-11-02, not yet run on the mini PC.** What is in place:
  - `subzero_ai/egress_proxy.py`: an asyncio HTTP CONNECT-only proxy on `sandbox_host_address`:`sandbox_proxy_port`
    (default 10.77.0.1:3128). Only `CONNECT name:443` to a name on `sandbox_allowed_hosts` (exact or `*.` suffix) is
    allowed. Everything else gets 405 or 403 in plain words. IP literals are refused. Every resolved address must be
    public: loopback, private, link-local, multicast, unspecified, CGNAT, the 10.77.0.0/24 subnet and this computer's
    own are refused. The connection goes to the address that was checked, so there is no second lookup (DNS
    rebinding). Limits: connect 15 s, idle 120 s, request head 8 KB, connections per sandbox (default 16) and in total
    (64), bytes per sandbox over its life (`sandbox_proxy_mb_limit`, default 2048) and per tunnel (512 MB). Refused
    destinations are audited (`sandbox_proxy_blocked`) at most once a minute per sandbox and name.
  - Tokens: `TokenStore` keeps a SHA-256 hash of each sandbox's `secrets.token_urlsafe(32)` token, in memory only,
    expiring with the sandbox. `SandboxManager` issues one when a sandbox is made, revokes it (and closes its open
    tunnels) when the sandbox is deleted or reaped, and issues a new one for a running sandbox after an app restart.
  - Guest commands get `HTTPS_PROXY` (with the login), `NO_PROXY` empty, `JAVA_TOOL_OPTIONS` and `GRADLE_OPTS` with the
    `https.proxy*` settings, and the JDK's Basic-for-tunnels override. The env is passed per command only. The token
    is redacted from command output (`SandboxManager.redact`), and never written to the audit log or the log.
  - The proxy starts with the app for `CODE_SANDBOX=hyperv`. If it can't bind, sandboxes are refused with the reason, and
    the Settings overview has a row for it. The allow-list and limits apply to the next connection. The address and
    port need a restart.
  - Left to do: run `sandbox-network.ps1` Install and Check on the mini PC, then a real guest: Gradle and pip through the
    proxy (confirm the JDK's Basic login for the tunnel works with the override), a refused name, and a token revoked at
    destroy. Also decide whether the proxy login should stay in the `ssh` command line on the host (it is visible to
    local processes running as admin, though not to the sandbox). It is not in the audit log, the log or the output.
- **M6 Wider tools and student access (about 3 days). Built 2026-11-02, offline (tests use the fake sandbox and the
  scripted Claude); not run on the mini PC.** What is in place:
  - Settings (Code section, `docs/SETTINGS.md`): `code_student_access` (off), `code_student_free_rein` (on),
    `code_mentor_free_rein` (off), `code_sandbox_commands`, `code_student_max_open` (1), `code_student_daily_sessions`
    (3), `code_student_max_turns` (40) and `code_student_command_timeout` (600). Students need `code_sandbox` set to
    hyperv (or fake, for tests); the Settings page refuses to switch students on otherwise.
  - Migration 15: `code_sessions` gets `owner_is_student`, `start_approved_by`, `start_approved_at`, `screen_note`,
    `free_rein` and `patch_released`, and the status `awaiting_approval`. The table is rebuilt because SQLite can't change
    a CHECK constraint, so `migrate()` now turns foreign keys off while migrating. Index `idx_code_sessions_owner`.
  - Flow: a student's request creates a session in `awaiting_approval`, with no sandbox and no copy of the repository.
    A mentor sees it under Student requests on the Code page and in the Review queue, and a plain line goes to the
    approvals channel (no buttons, so approval still happens on the site). Approve makes the sandbox and starts
    Claude. Decline stops it, and the note is shown to the student.
  - Free rein: inside a student's sandbox, file changes and commands run without a click. Mentors' sandboxed sessions
    keep the clicks unless `code_mentor_free_rein` is on. If the sandbox is turned off during a session that needs it,
    the session stops rather than running on the mini PC.
  - Only a mentor can release the patch (`Release to student`). Until then the student sees that a mentor is looking,
    and the download is refused. Students get no zip and no write token. The GitHub read-only download is the only
    connector, as before.
  - Students see only their own sessions (other sessions answer 404). Step output is cut to 6,000 characters for them.
  - Mentors can stop, delete (which deletes the sandbox and the copies), and extend the sandbox's time limit. Extending
    adds `sandbox_ttl_hours` again, up to 7 days from when the sandbox was made. The proxy token's expiry moves with it.
  - Students see how long their workspace lasts ("This workspace is deleted in N hours").
  - Claude's instructions say which setting applies: in a sandbox, it is told it is in a disposable Linux computer, that
    it may install packages through the proxy's allow-list, and that other sites are out of reach.
  - Audit: every transition listed in `docs/SETTINGS.md`.

  Judgement calls for Logan to check:
  - A student's follow-up in a session also waits for a mentor (one pending note, sent to Claude only when approved).
    This is how "nothing a student asks of Claude starts without approval" was read.
  - The screener (`Screener.check`) runs on a student's request and follow-up. A flag is a note for the mentor and never
    starts or skips anything. Code's system prompt does not include the `safety.md` layer that chats use. Not added yet.
  - `code_sandbox_commands` is a new setting. Without it Claude could not run `pip` or `npm` inside a sandbox, which
    the "install packages through the proxy" rule needs. The build-file rule still applies in sandboxes.
  - The daily limit counts requests (new sessions and follow-ups) from the audit log, so a declined request counts.
  - Extension cap: 7 days from creation (D5 is still open: who may extend past 48 hours).

  Still open: a real student run on the mini PC (the guest's `python3` and `pip` come from the base image, which is
  still a TODO in `sandbox.py`); the screener's verdict on a real model; a patch zip for students (only the patch is
  offered).

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
  extend a session past 48 hours? Built 2026-11-02: approval is required (for the start and for follow-ups); mentors
  may extend up to 7 days from creation. Logan to confirm the extension cap.
