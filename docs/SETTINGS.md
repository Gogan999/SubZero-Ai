# Every setting, and where to change it

Since version 0.7, admins can change every setting, API key and connected account on the **Settings** page.
This page lists them all. Since 0.8 the page is split into sections, picked from the menu on the left (a row of tabs
on a phone): Overview (health, backups, Run checks), AI and keys (Claude settings, API keys, Claude subscription,
Model safety check, AI engine), Mentor review (limits, approval window, system prompt), Accounts and people
(connected accounts, subteams and admins), Slack, Site and server (site, data, connectors, Code) and Change history. The config.env key is what you'd write if you edited `C:\ProgramData\SubZeroAI\config.env`
by hand.

**Where a setting is saved.** "Database" settings win over config.env and apply to the next message. "config.env"
settings are written to config.env by the Settings page. Secrets (keys and tokens) are always in config.env, never
shown again on the page (only their last four characters), never written to the log or the audit log, and left
out of backups. A setting marked **restart** is saved right away but used only after the app restarts: the Settings
page shows a **Restart now** button when one is waiting. The Windows service waits a little longer before each
restart in the same hour (3 seconds, then 20 seconds, then a minute), so a third restart in an hour takes about a
minute to come back.

**Environment variables win.** If a Windows environment variable with the same name is set on the mini PC, it
beats config.env, so the Settings page shows that field locked and says so.

## Claude settings (database)

| Setting | config.env key |
| --- | --- |
| AI providers | `AI_PROVIDERS` |
| Default, hard-problem and title models | `MODEL_DEFAULT`, `MODEL_HARD`, `MODEL_TITLES` |
| Safety check judge (must be a Claude model) | `MODEL_CHECK_JUDGE` |
| Everyday effort, effort for explainers and files | `EFFORT_DEFAULT`, `EFFORT_HIGH` |

**Model dropdowns.** The default, hard-problem, title, screener and judge models are dropdowns, grouped by company.
Each list shows the models that can be used right now: the company is turned on and has its key saved (or the Claude
subscription is signed in), and the active AI engine can run it (with `AI_BACKEND=anthropic` only Claude models are
listed). The default and hard-problem models also need a passing Model safety check, and the screener model needs
its screener part; a model that hasn't been checked yet is listed too, marked "not yet safety-checked", and saving
it still fails until the check passes. The judge list has Claude models only. Pick **Other (type a model id)** to
type an id that isn't listed yet, such as a new model. Saving checks the typed id the same way as a listed one.
Fable and Mythos models are never listed or accepted.

The AI helper's model list is refreshed whenever AI providers or keys are saved on this page, so a new company's
models appear without a restart. The refresh runs in the background, so reload the page a moment after saving if a
new model is still missing.

| Longest reply, longest explainer, fresh-chat suggestion | `MAX_TOKENS_CHAT`, `MAX_TOKENS_EXPLAINER`, `LONG_CHAT_TOKENS` |
| Web search, daily search limit, Research for students | `WEB_SEARCH_ENABLED`, `WEB_SEARCH_DAILY_LIMIT`, `RESEARCH_FOR_STUDENTS` |

## Limits, approval window, data, code, people (database)

| Setting | config.env key |
| --- | --- |
| Waiting messages per student, approved per day, reminder minutes | `LIMIT_PENDING_PER_STUDENT`, `LIMIT_APPROVED_PER_DAY`, `REMINDER_MINUTES` |
| Mentors need approval, Slack DMs, show pending or rejected to the team | `MENTORS_NEED_APPROVAL`, `SLACK_DMS`, `SHOW_PENDING_TO_TEAM`, `SHOW_REJECTED_TO_TEAM` |
| Approval window, screener on, screener model, students who always wait | `APPROVAL_WINDOW_MINUTES`, `SCREENER_ENABLED`, `MODEL_SCREENER`, `ALWAYS_REVIEW_SLACK_IDS` |
| Retention, days of backups kept | `RETENTION_DAYS`, `BACKUP_KEEP_DAYS` |
| Code commands and timeout | `CODE_COMMANDS`, `CODE_COMMAND_TIMEOUT` |

## Code sandboxes (database)

Each Code session's commands can run in their own disposable sandbox (`subzero_ai/sandbox.py`) instead of on the mini
PC. With **Off**, commands run on the mini PC as before. **Hyper-V** gives each session a virtual computer made from
one base disk and attached only to the internal switch below. The host has a virtual adapter on that switch at
10.77.0.1/24, and the host must firewall that adapter so sandboxes can reach only the host and nothing else on the
mini PC's network. Each sandbox's adapter is also locked down: a static MAC, DHCP and router guards, ACLs that allow only the host, and
a private VLAN (100 with 200, isolated) so sandboxes can't reach each other, even with non-IP frames. Each sandbox
has its own SSH host key, made on the host and delivered on a small seed disk that is deleted once the guest is up.
See `docs/CODE_SANDBOX_PLAN.md`.
**Fake** runs commands in a folder on the mini PC with no protection and is for testing only.

| Setting | config.env key |
| --- | --- |
| Run Code commands in a sandbox (off, hyperv, fake) | `CODE_SANDBOX` |
| Delete a sandbox this many hours after it was made (1 to 168) | `SANDBOX_TTL_HOURS` |
| Sandboxes that can run at the same time (1 to 10) | `SANDBOX_MAX_SESSIONS` |
| Memory per sandbox, processors per sandbox, disk per sandbox | `SANDBOX_MEMORY_MB`, `SANDBOX_CPUS`, `SANDBOX_DISK_GB` |
| Keep this much disk free before a new sandbox is made, in GB | `SANDBOX_MIN_FREE_GB` |
| Base disk (full path to a `.vhdx` file) | `SANDBOX_BASE_IMAGE` |
| Internal network switch (the host's vNIC is 10.77.0.1/24) | `SANDBOX_SWITCH` (default `szsandbox-internal`) |
| Host port sandboxes may reach, for the egress proxy (default 3128). Restart after changing | `SANDBOX_PROXY_PORT` |
| This PC's address on the sandbox switch, where the proxy listens (default 10.77.0.1, a private IPv4 address). Restart after changing | `SANDBOX_HOST_ADDRESS` |
| Websites a sandbox may reach, separated by commas (`*.example.com` covers names under it; empty allows none) | `SANDBOX_ALLOWED_HOSTS` |
| Ports a sandbox may use through the proxy (default 443) | `SANDBOX_ALLOWED_PORTS` |
| Data a sandbox may move through the proxy over its whole life, in MB (default 2048, both directions) | `SANDBOX_PROXY_MB_LIMIT` |
| Connections one sandbox may hold open through the proxy at once (default 16) | `SANDBOX_PROXY_MAX_PER_SANDBOX` |
| Let the sandbox's user use sudo, root inside the sandbox only (off by default) | `SANDBOX_GUEST_SUDO` |

**Internet access for sandboxes.** A sandbox can't reach the internet directly. Its commands reach it only through the egress proxy on the mini PC (`subzero_ai/egress_proxy.py`), which runs when `CODE_SANDBOX` is `hyperv`. The proxy opens only secure connections (port 443 by default) to the names in `SANDBOX_ALLOWED_HOSTS`. It refuses IP addresses, names that point at this PC or a private network, and every request that isn't a secure connection. Each sandbox has its own login, which is cut off when the sandbox is deleted, and the login is never written to the log, the audit log or a command's output. Refused websites are written to the audit log (`sandbox_proxy_blocked`), at most once a minute for each sandbox and name. Changing the websites, ports or limits applies to the next connection. If the proxy can't start (for example, the host address isn't on this PC), no sandbox is made, and the Settings overview says why.

**One-time administrator step.** Before the first Hyper-V sandbox, an administrator runs the sandbox network setup
script once, in an administrator PowerShell on the mini PC, from the SubZero AI folder:

```
powershell -ExecutionPolicy Bypass -File scripts\sandbox-network.ps1 -Action Install
```

It creates the internal switch if it's missing, gives the host's adapter for it 10.77.0.1/24 with no gateway, turns
IP forwarding and IPv6 off on it, sets its network profile to Public, puts it on the private VLAN 100 with 200 in
promiscuous mode, and adds the firewall rules that block all
inbound traffic to the sandboxes except the proxy port (`szsandbox-block-tcp-below`, `szsandbox-block-tcp-above`,
`szsandbox-block-udp`, `szsandbox-block-icmp4`, `szsandbox-block-icmp6`, and `szsandbox-block-proto-2`, `-41`, `-47`,
`-50`, `-51` for IGMP, IPv6 in IPv4, GRE, ESP and AH) and the one that allows the proxy port
(`szsandbox-allow-proxy`). Other IP protocols are blocked by the default inbound action, which must be Block for the
Public profile; the check reads it. Existing allow rules on the host (File and Printer Sharing, Remote Desktop, games
and so on) need no change: the blocks on the sandbox adapter take precedence over them. It changes nothing else: other
adapters, switches, VMs and rules are never touched. The
app never runs it. `-Action Check` lists anything missing, and `-Action Remove` deletes the sandbox rules and the switch
(it refuses while a VM still uses the switch). If you change `SANDBOX_SWITCH` or `SANDBOX_PROXY_PORT`, run `-Action
Install` again with the same `-Switch` and `-ProxyPort`.

`SANDBOX_GUEST_SUDO` (off by default) gives the sandbox's user `sudo` without a password. Turn it on only if students
need root inside a sandbox. It gives root inside that disposable virtual computer only, never on the mini PC, and it
applies to sandboxes made after the change.

Creating a sandbox checks this network first. **Create refuses until the check passes**, with a message that names
what is missing, and the Settings overview shows the same check. A passing check is kept for one minute; a failure is
never kept. The check is read-only. It is not yet tested on the mini PC.

A new session waits for a free sandbox, and a new sandbox is refused while the disk is below the free-space limit.
A sandbox is deleted when its Code session finishes, after its time limit, and when no record of it is left. Every
sandbox is written to the audit log (`sandbox_created`, `sandbox_destroyed`, `sandbox_reaped`, `sandbox_extended`).

**Students' Code sessions (off by default).** With `CODE_STUDENT_ACCESS` on and a Hyper-V sandbox (or the fake one,
for tests), students can ask for a Code session. Nothing starts until a mentor approves the request. It shows on the
Code page under Student requests, and a plain line goes to the approvals channel in Slack. Approving makes the
session's sandbox and starts Claude. Declining stops the request, and the student sees the mentor's note. If the
sandbox is off or its network check fails, students are told why and nothing is started. Students never get a GitHub
write token, and they see only their own sessions.

Inside a student's sandbox, Claude's file changes and commands run without a mentor's click (`CODE_STUDENT_FREE_REIN`,
on by default). Each step still shows on the session page. A student's follow-up waits for a mentor, as the first
request did. The patch is released to a student only after a mentor clicks **Release to student**. Mentors can stop,
delete and extend any session's sandbox at any time. Extending adds the time limit again, but a sandbox never lives
longer than 7 days from when it was made. Each student's request, approval, decline and release is written to the
audit log (`code_student_requested`, `code_student_approved`, `code_student_declined`,
`code_student_followup_requested`, `code_student_followup_approved`, `code_student_followup_declined`,
`code_patch_released`, `code_session_stopped`, `code_session_deleted`).

| Setting | config.env key |
| --- | --- |
| Students may ask for Code sessions (needs the Hyper-V or fake sandbox) | `CODE_STUDENT_ACCESS` (default off) |
| Inside a student's sandbox, changes and commands run without a mentor's click | `CODE_STUDENT_FREE_REIN` (default on) |
| Inside a mentor's sandbox, changes and commands run without a click (sandbox only) | `CODE_MENTOR_FREE_REIN` (default off) |
| Programs Claude may run inside a sandbox, on top of `CODE_COMMANDS` | `CODE_SANDBOX_COMMANDS` (default gradlew, python3, pip, npm, node, java, git) |
| Code sessions a student can have open at once (1 to 5) | `CODE_STUDENT_MAX_OPEN` (default 1) |
| Code requests a student can make in 24 hours, new sessions and follow-ups (1 to 50) | `CODE_STUDENT_DAILY_SESSIONS` (default 3) |
| Claude replies in one run, for students (5 to 200) | `CODE_STUDENT_MAX_TURNS` (default 40) |
| Stop a student's command after this many seconds (30 to 3600) | `CODE_STUDENT_COMMAND_TIMEOUT` (default 600) |
| Subteams, admins | `SUBTEAMS`, `ADMIN_SLACK_IDS` |

## AI engine (config.env)

| Setting | config.env key | |
| --- | --- | --- |
| AI engine: Anthropic API, AI helper, or pretend | `AI_BACKEND` (and `FAKE_CLAUDE`) | restart |
| AI helper reply and pause timeouts | `PI_REQUEST_TIMEOUT`, `PI_IDLE_TIMEOUT` | restart |
| Node.js program, AI helper script | `PI_NODE`, `PI_GATEWAY_SCRIPT` | restart |
| Anthropic API address | `ANTHROPIC_BASE_URL` | restart |

The page won't switch to the Anthropic API while a model setting uses another company or the Claude subscription
is turned on. Change those first.

**Mentors' model picker.** When a mentor starts a new chat, the New chat page has a model dropdown with the same list
as the default-model dropdown above (only models that passed the safety check). Students can't pick a model: the
chat uses the default model. A mentor can also switch an existing chat to any model on the list from the chat's
Model menu. If a chat's model later stops being allowed (its key is removed, its company is turned off, or its
safety check fails or goes out of date), the next reply uses the default model instead. That change is written to the audit log as
"model_fallback", and the chat's Model menu shows the new model.

## API keys (config.env, secret)

`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`. New keys work from the next message.
A key can be removed unless a model setting still needs it.

## Slack connection (config.env)

| Setting | config.env key | |
| --- | --- | --- |
| Bot token (secret) | `SLACK_BOT_TOKEN` | restart |
| App-level token (secret) | `SLACK_APP_TOKEN` | restart |
| Client ID, client secret (secret) | `SLACK_CLIENT_ID`, `SLACK_CLIENT_SECRET` | |
| Mentor review channel | `SLACK_APPROVALS_CHANNEL` | |
| Slack guests can sign in | `ALLOW_SLACK_GUESTS` | |

New tokens are tried with Slack before they're saved. A bot token for a different workspace is refused, because
everyone's accounts belong to the current one: moving to a new workspace is done on the setup page.
`SLACK_TEAM_ID` is filled in from the bot token automatically.

## Connectors (config.env)

`CONNECTOR_GITHUB_REPO`, `CONNECTOR_GITHUB_BRANCH`, `CONNECTOR_GITHUB_TOKEN` (secret), `CONNECTOR_DRIVE_LINKS`.

## Site and server (config.env)

| Setting | config.env key | |
| --- | --- | --- |
| Site address | `PUBLIC_URL` | restart |
| App name | `APP_NAME` | |
| Time zone | `TIMEZONE` | restart |
| Largest text file | `MAX_TEXT_FILE_KB` | |
| Backups folder, nightly backup hour | `BACKUP_DIR`, `BACKUP_HOUR` | |
| Check for new versions, where they come from | `CHECK_FOR_UPDATES`, `UPDATE_REPO` | |

## Connected accounts

The **Connected accounts** box shows Slack (workspace, live connection, Sign in with Slack), each AI company (key
saved and its last four characters), the Claude subscription (signed in or not, with a Sign out button), GitHub and
the Cloudflare Tunnel's address. **Run checks** runs the same checks as check-settings.cmd and shows the results.

The **Claude subscription** box (under AI and keys) signs Claude Code in from the page. **Sign in** starts
`claude auth login --claudeai` on the mini PC with the bridge's own config folder and shows the sign-in address it
prints. Open it on any device, sign in, and paste the code Claude shows into the box; the page hands the code to the
waiting program and never saves or logs it. A sign-in nobody finishes stops after 15 minutes. `python -m subzero_ai
claude-login` on the mini PC still works too. The Cloudflare Tunnel itself is managed in Cloudflare's Zero Trust dashboard.

## Kept in config.env only

| Key | Why |
| --- | --- |
| `DATA_DIR` | Moving it means moving the files. |
| `HOST`, `PORT` | The tunnel points at 127.0.0.1:8787; changing them from the page would cut the page off. |
| `DEV_LOGIN` | Test sign-in lets anyone on the mini PC sign in as anyone. |
| `SESSION_SECRET` | Made automatically; changing it signs everyone out. |

## Notes added with the sandbox work

- After a model-check suite update, the default and hard-problem models keep working even though their old check result
  is out of date; run the check again to refresh it. A current check that failed still blocks a model.
- A Code session that has waited for a mentor, or run, for 3 days is stopped and cleaned up by the nightly job.
- After this update, run `sandbox-network.ps1 -Action Install` once more, which puts the host's adapter on the private
  VLAN. Until then `-Action Check` reports `host-vlan-missing`, and Create refuses.
- An existing install that already saved `sandbox_switch` keeps that value: change it to `szsandbox-internal` on the
  Settings page.
- The sandbox network script's Install does not turn Windows Firewall on. If the Public profile is off, Check reports
  `firewall-off` and no sandbox can start. Turn it on with `Set-NetFirewallProfile -Profile Public -Enabled True`
  (the sandbox adapter is always on the Public profile).

## Sandbox base image (one-time administrator step)

Every Hyper-V sandbox is made from one base disk, set by `SANDBOX_BASE_IMAGE`. An administrator builds it once, on the
mini PC, in an administrator PowerShell, from the SubZero AI folder:

```
powershell -ExecutionPolicy Bypass -File scripts\sandbox-image.ps1 -Action Build -ReleaseDate 20261015
```

Use the newest dated release folder under https://cloud-images.ubuntu.com/noble/ (eight digits, such as 20261015).
`current` is not accepted, so the image never changes under the app. Build needs 45 GB free on the drive that holds the
disk, because the Ubuntu disk image is 30 GB while it is converted. It downloads the image, checks its SHA-256 against
that release's `SHA256SUMS`, converts it to a read-only dynamic disk at
`C:\ProgramData\SubZeroAI\sandbox\images\sandbox-base.vhdx`, and changes its boot menu so cloud-init reads the sandbox's
seed (`ci.ds=NoCloud`). The new boot menu keeps the image's own `search.fs_uuid` line, which points GRUB at the boot
partition. The root is the largest Linux partition on the disk, and its PARTUUID is read from Windows (on a GPT disk the
PARTUUID is the partition's GUID). The kernel and the initrd are not named: GRUB takes the last `vmlinuz-*` and
`initrd.img-*` on the boot partition when the sandbox starts. That is the last name in alphabetical order, so it is the
newest kernel only while the version numbers have the same length; check the boot menu after each new release. Build
prints the path and the SHA-256. Set `SANDBOX_BASE_IMAGE` to that path. Build never overwrites
an existing base disk; if one must be replaced, run `-Action Remove` first, only when no sandbox uses it.

`-Action Check` changes nothing and reports whether the disk is present, read-only and dynamic. `-Action Remove`
deletes only the base disk, and refuses while any VM disk still uses it as its parent. The app never runs this script.

Build also checks the release's `SHA256SUMS.gpg` signature against the Ubuntu cloud image key, when `gpg.exe` is
installed. Without it, Build says the signature was not checked. The key must be in gpg's keyring first (see
`docs/CODE_SANDBOX_PLAN.md`, Base image signature check). `-VerifySignature:$false` turns the check off. Build saves
the disk's SHA-256 in `sandbox-base.vhdx.sha256`, next to the disk. `-Action Check -Deep` hashes the disk (about a
minute) and compares it with that saved value.

IPv6 inside the guest is not turned off by this step, and the app's cloud-init user-data does not turn it off either.
The host side is the protection: the sandbox's virtual adapter rules deny all IPv6 (see `isolation_script` in
`subzero_ai/sandbox.py`), and those rules are not yet tested on the mini PC.
