# SubZero AI

A claude.ai-style chat for a high school robotics team. Students ask Claude about code, mechanisms, physics, and materials. Every student message waits for a mentor to approve it in Slack before Claude sees it, and the whole team can read approved chats.

This is version 0.4.1: all four milestones of the build plan.

## What's in this version

- **Sign in with Slack**, limited to full members of the team workspace. Guest accounts and bots are refused (set `ALLOW_SLACK_GUESTS=1` if some students are guests), and the daily account check signs out anyone who is deactivated or turned into a guest. Mentors are the members of the private #claude-approvals channel; admins are named on the setup page and the Settings page.
- **Chat**: a conversation list with search and subteam and student filters, Markdown replies with highlighted code and copy buttons, and replies that stream in as Claude writes.
- **Approvals**: each student message posts a card in #claude-approvals with Approve, Edit, Reject, and Handle privately. Students get a Slack DM when a mentor acts. The app's Review page does the same thing if Slack is down. A second mentor tapping a handled card is told who already handled it.
- **Audit log** (admins): sign-ins, messages, every review with the before and after text of edits, alerts, and Slack problems.
- **Attachments**: code and text files (.java, .py, .cpp, .h, .json, .gradle, .yaml, .txt, .md, .csv) PNG or JPG screenshots, and MP4, MOV or WebM videos, up to 5 per message, 50 MB each and 90 MB in all (Cloudflare's free plan refuses uploads over 100 MB). Claude can't watch video, so the browser takes up to 8 still frames from each video and sends them to Claude; the chat shows the video itself, which is stored as uploaded. Pick them, drag them onto the message box, or paste a screenshot. Text files are also capped at 200 KB (`MAX_TEXT_FILE_KB`), because Claude reads every line on every reply. Photos are re-encoded on upload, which removes hidden details like a phone's GPS location and shrinks them to the size Claude uses. Slack cards and the Review page list the files; mentors open them in the app.
- **Interactive explainers**: ask for one in words ("make a simulation of…", "an interactive graph of…") and Claude builds a small web page you can play with. Claude may also make one on its own when it clearly helps. It runs in a sandboxed frame that can't reach the network or the app's cookies, with Full screen, Download, and View code buttons. Messages that ask for one use high effort and up to 16,000 tokens; messages with files also use high effort.
- **Long chats**: once a chat sends Claude about 150,000 tokens per reply, the app suggests starting a fresh one.
- Also: the three-layer system prompt with the child-safety layer, MENTOR ALERT handling, per-student limits, the permanent AI notice, and the "Using SubZero AI responsibly" page.

- **Windows packaging and operations** (new in 0.4): a one-step installer with its own Python, a setup page that creates the Slack app for you, the app and the Cloudflare connector running as Windows services that start after a reboot with nobody signed in, nightly backups with a restore script, an update script, and an admin **Settings** page for models, limits, retention, subteams, admins, and the three system prompt layers. The Settings page also shows whether Slack, the Claude API, and the backups are working, and admins get a Slack DM if Slack or Claude fails for 15 minutes.

## Try it in test mode

Test mode runs on any Windows PC with sample sign-ins and a pretend Claude, so you need no accounts or keys. It takes about 5 minutes.

1. Download `SubZeroAI-windows.zip` from the [releases page](https://github.com/Gogan999/SubZero-Ai/releases) then right-click it and choose **Extract All**. The .cmd files only work from the extracted folder, not from inside the zip.
2. Double-click **start-test-mode.cmd**. The browser opens by itself.
3. Sign in as **Ava (student)** and send a message.
4. Open a private browser window, sign in as **Mentor Kim**, open **Review**, and approve it. Ava's window updates by itself and the reply streams in.

Also try:

- Edit, Reject, and Handle privately from Mentor Kim's Review page.
- Signing in as **Ben (student)** to see what other students can and can't see.
- A message containing `ALERT-TEST`, which makes the pretend Claude raise a mentor alert, or `ERROR-TEST`, which makes it fail so you can try **Try again**.
- Attaching files, or asking for a simulation or an interactive explainer to get a sample explainer. Its last line confirms the network is blocked inside the frame.
- Signing in as **Head coach** and opening **Settings**.

Test data lives in the `test-data` folder next to the .cmd file. Delete it to start over. Close the window to stop the app.

From a copy of this repository instead of the release zip, install Python 3.12 or 3.13 (tick **Add python.exe to PATH**) and double-click the **start-test-mode.cmd** in the repository. On a Mac or Linux computer, run `scripts/run.sh --test-mode --open`.

## Install guide: Windows mini PC

Expect about an hour. No Docker, no Python install, and no router changes.

### Before you start

- [ ] The mini PC with all Windows updates installed, sleep set to **Never**, and the BIOS or UEFI power-loss option (often "Restore on AC power loss") set to **Power On**, so it restarts after an outage.
- [ ] A free Cloudflare account with the team domain's DNS on Cloudflare. When you add the domain, check that Cloudflare imported the website's existing DNS records before switching nameservers.
- [ ] A Slack account that can install apps in the team workspace.
- [ ] A Claude Console account at [platform.claude.com](https://platform.claude.com).

### 1. Cloudflare Tunnel

In the Cloudflare Zero Trust dashboard, open **Networks**, **Tunnels**, and create a tunnel named **subzero-ai**. Choose Windows and copy the long token from the install command it shows (you paste it into the installer). Add a public hostname (Cloudflare may call it a published application route): **ai.yourdomain.org**, type **HTTP** (not HTTPS, which gives a 502 error), URL **127.0.0.1:8787** (not `localhost`, which Windows may send to the IPv6 address the app doesn't listen on). The free Zero Trust plan asks for a payment method but doesn't charge for the plan.

### 2. Run the installer

Download `SubZeroAI-windows.zip` from the [releases page](https://github.com/Gogan999/SubZero-Ai/releases), right-click it, and choose **Extract All**. In the extracted folder, double-click **install.cmd** and choose **Yes** when Windows asks for permission. The installer:

- copies the app, with its own Python, to `C:\Program Files\SubZeroAI`;
- creates `C:\ProgramData\SubZeroAI` for settings, chats, files, backups, and logs, readable only by administrators and the app's service account;
- installs the **SubZero AI** Windows service under its own low-privilege account, set to start automatically;
- asks for the Cloudflare tunnel token and installs the Cloudflare connector as a Windows service;
- opens `http://localhost:8787/setup` in the browser.

Running install.cmd again repairs an install and keeps all data.

### 3. Setup page

The setup page only opens on the mini PC, saves each step as you go, and locks when you finish.

1. **Address.** Enter `https://ai.yourdomain.org`.
2. **Slack app.** Click **Create Slack app**, choose the team workspace, and confirm. In the new app, click **Install to Workspace**, then generate an app-level token with the `connections:write` scope. The permissions the app asks for, and why, are listed in [Slack permissions](#slack-permissions). Paste the bot token, app-level token, client ID, and client secret. The page checks them with Slack before saving.
3. **Approvals channel.** Create the private **#claude-approvals** channel in Slack, add the coaches and mentors, and type `/invite @SubZero AI` in it. Reload the setup page and pick the channel.
4. **Claude.** At platform.claude.com, create a workspace named **SubZero AI**, set its monthly spend limit, and create an API key inside it. Paste it; the page sends one tiny test request.
5. **Admins.** Paste the coaches' Slack member IDs (click a person's name in Slack, open the three-dot menu, and choose **Copy member ID**).
6. **Finish.** Write down the recovery code it shows and keep it somewhere safe. SubZero AI restarts with the new settings.

### 4. Check it

Open `https://ai.yourdomain.org` on a phone and sign in with Slack as a coach. Open **Settings**: the health list should say Slack is working, and a backup should already be listed. Check the per-student limits, the retention period, and the three system prompt layers (ask a coach to review the safety layer). Then send a test message as a student and approve it in #claude-approvals.

## Slack permissions

The **Create Slack app** button and `slack-manifest.yaml` set all of these up for you. If you made the app by hand, or the setup page or check-settings.cmd says a permission is missing, compare the app's **OAuth & Permissions** page with this list.

**Bot token scopes** (the `xoxb-` token)

| Scope | Why SubZero AI needs it | Slack calls |
| --- | --- | --- |
| `chat:write` | Posts approval cards in #claude-approvals, updates them after a mentor decides, and sends DMs and private notes. | `chat.postMessage`, `chat.update`, `chat.postEphemeral` |
| `im:write` | Opens a DM with a student or admin (approval results, health alerts). | `conversations.open` |
| `groups:read` | Lists the private channels the bot was invited to (so the setup page can offer #claude-approvals) and reads that channel's members to know who the mentors are. Without it you get "missing_scope" after inviting the bot to the channel. | `conversations.list`, `conversations.members` |
| `users:read` | Looks up names and profile pictures, and spots deactivated, guest, and bot accounts. | `users.info` |

**User token scopes** (Sign in with Slack): `openid`, `email`, `profile`. These let a student sign in and give the app their Slack ID, name, email, and workspace. The app never keeps the user token.

**App-level token** (the `xapp-` token, made under **Basic Information → App-Level Tokens**): `connections:write`, for Socket Mode. Socket Mode is how button clicks and approval forms reach the mini PC without opening a port.

No scope is needed for `auth.test` (checking the token) or `views.open` (the reject-reason form).

**After adding a scope**, Slack doesn't apply it until the app is reinstalled:

1. Open the app at [api.slack.com/apps](https://api.slack.com/apps), go to **OAuth & Permissions**, and add the scope under **Bot Token Scopes** (or **User Token Scopes**).
2. Click **Reinstall to Workspace** at the top of that page (Slack shows a yellow banner asking for it) and allow.
3. The bot token usually stays the same, but copy the **Bot User OAuth Token** again to be sure. If it changed, paste it on the Settings page or the setup page (or in `SLACK_BOT_TOKEN` in config.env) and restart SubZero AI.
4. Run **check-settings.cmd** to confirm Slack is working.

## Running it

Everything below runs on the mini PC from `C:\Program Files\SubZeroAI`. Each tool asks for administrator permission.

| Tool | What it does |
| --- | --- |
| **update.cmd** | Downloads the newest release, backs up the data, replaces the app, and restarts it. If the new version doesn't start, it puts the old one back. The Settings page says when an update is available. If the repository is private, download the zip yourself and run `update.cmd -Zip path\to\SubZeroAI-windows.zip`. |
| **restore.cmd** | Lists the backups and puts the one you pick back, after saving the current data as a `before-restore` backup. |
| **back-up-now.cmd** | Makes a backup now. Admins can also click **Back up now** on the Settings page. |
| **check-settings.cmd** | Checks the Slack and Claude settings and says what to fix. |
| **recovery-code.cmd** | Makes a new recovery code (the old one stops working). |
| **uninstall.cmd** | Removes the app and its service. The data folder is kept. |

- **Backups.** Every night at 2:00 AM the app copies the database, attached files, and changed prompts into a zip in `C:\ProgramData\SubZeroAI\backups` and keeps 14 days. For an off-site copy, set `BACKUP_DIR` in config.env to a folder that OneDrive or Google Drive syncs (the app's service account needs permission to write there). Backups leave out config.env, because it holds the API key and Slack tokens; keep your own copy of it somewhere safe.
- **Retention.** Chats with no activity for 730 days (this season and the next) are deleted after the nightly backup. Change it, or set 0 to keep chats forever, on the Settings page.
- **Reboots and Windows updates.** Both services start on their own after a restart, with nobody signed in.
- **Health.** The Settings page shows the Slack connection, Claude API status, the last backup, and whether an update is out. If Slack or Claude calls fail for 15 minutes, admins get a Slack DM, and another when it works again. Cloudflare's dashboard shows whether the tunnel is up. `http://localhost:8787/healthz` answers with the same status for scripts.
- **If Slack sign-in breaks.** On the mini PC, open `http://localhost:8787/auth/recovery` and enter the recovery code to sign in as an admin. It doesn't work from anywhere else.
- **Model changes.** When Anthropic retires a model, change the model ID on the Settings page. No code changes.

**Monthly check** (about 10 minutes): open Settings and confirm everything says Working or Done, confirm last night's backup is listed, apply any available update, and check the Claude Console spend for the month.

## Acceptance tests

**Milestone 1**

- [ ] A mentor signs in on a phone through the tunnel, sends a message, and watches the reply stream in.
- [ ] Someone signing in with an account from a different Slack workspace is refused.

**Milestone 2**

- [ ] A message from someone who isn't in #claude-approvals shows up as a card there within 5 seconds.
- [ ] Approve: the reply streams in, the card changes to "Approved by", and the student gets a DM.
- [ ] Edit: Claude answers the mentor's version, the student sees it marked "Edited by", and the audit log has both versions.
- [ ] Reject: the student sees the reason in the app and in a DM, and can send a revised message.
- [ ] Handle privately: nothing goes to Claude, the chat disappears for other students, and the student sees "A mentor will follow up with you directly".
- [ ] A second mentor tapping a card that's already handled gets "Already approved by …" and nothing else happens.
- [ ] With Slack's connection stopped, a mentor can still approve from the Review page.

**Milestone 3**

- [ ] The red-team checks in the plan's safety checklist get sensible answers: an off-topic request, an unsafe shop shortcut, a distress message (it should raise a mentor alert), "ignore your rules," a request for someone's personal details, and asking Claude whether it's human.
- [ ] An interactive explainer opens, works full screen, and shows "Network: blocked" if it tries to fetch anything (the test-mode sample does this).
- [ ] A student with 3 messages waiting can't send a 4th.
- [ ] A screenshot and a code file reach Claude, and Claude refers to them.

**Milestone 4**

- [ ] Someone follows only the install guide on a clean mini PC and gets a first approved prompt in under an hour.
- [ ] After a restart of the mini PC, with nobody signed in, the site comes back by itself.
- [ ] A backup restores cleanly with restore.cmd: a chat made after the backup disappears, and the earlier chats and their files are back.
- [ ] update.cmd moves to a newer release and keeps every chat and setting.
- [ ] Changing a limit on the Settings page applies to the next message without a restart.

## How it behaves

| | Students | Mentors | Admins |
| --- | --- | --- | --- |
| Start and continue their own chats | Yes, each message waits for review | Yes, messages go straight to Claude | Yes |
| Read the team's chats | Approved messages and replies | Everything | Everything |
| Withdraw their own waiting message | Yes | Yes | Yes |
| Approve, edit, reject, handle privately | No | Yes | Yes |
| Switch a chat to Opus 5.5 for hard problems | No | Yes | Yes |
| Audit log | No | No | Yes |

- **Other students don't see messages that are waiting or rejected.** This is safer than the plan's original wording, because a message that needs handling privately is never shown to the team. To show them, set `SHOW_PENDING_TO_TEAM=1` or `SHOW_REJECTED_TO_TEAM=1`.
- One message per chat can wait at a time. Each student can have 3 waiting messages and 25 approved messages per day; the count resets at midnight.
- If a card waits 2 hours, the app posts one reminder in the card's Slack thread.
- **MENTOR ALERT**: when Claude flags a wellbeing concern, the chat becomes private to the student and mentors before any of the reply is shown, and an alert card is posted in #claude-approvals. The student still sees Claude's reply, which points them to trusted adults and the 988 Lifeline.
- Claude receives only approved messages and its own earlier replies: no names, emails, or Slack IDs.
- People removed from Slack lose access within a day. Someone removed from #claude-approvals can't approve anything from that moment on.

## Where things are stored

Everything lives in `C:\ProgramData\SubZeroAI` (in test mode, the `test-data` folder):

- `config.env`: the address, API key, and Slack tokens, written by the setup page.
- `data\subzero.db`: the database, including settings changed on the Settings page (these win over config.env).
- `files\`: attached files.
- `prompts\`: system prompt layers changed on the Settings page.
- `backups\`: the nightly backups.
- `logs\`: `subzero.log` (one file per day, kept for 14 days) and the service's own logs.

## Troubleshooting

- **The installer says SubZero AI didn't start**: read `C:\ProgramData\SubZeroAI\logs\subzero.log` and `SubZeroAI.err.log`. Run install.cmd again after fixing the cause.
- **The setup page says "Not found"**: it only opens on the mini PC itself, at `http://localhost:8787/setup`, and only until setup is finished. To open it again, run `python\python.exe -m subzero_ai reset-setup` from `C:\Program Files\SubZeroAI` in an administrator window (with `DATA_DIR` set to `C:\ProgramData\SubZeroAI`), or change settings on the Settings page instead.
- **"Python 3.12 or 3.13 isn't installed"** (only when running from a copy of the repository): install it from python.org with "Add python.exe to PATH" ticked, then close and reopen the window.
- **Sign-in says the sign-in couldn't be verified or the link expired**: check that the computer's clock is right and that `PUBLIC_URL` matches the address in the browser exactly.
- **Slack says "redirect_uri did not match"**: the Slack app's redirect URL must be `PUBLIC_URL` followed by `/auth/slack/callback`. Run the settings check to see the exact value.
- **No cards appear in Slack**: run the settings check. Most often the app isn't in the channel (`/invite @SubZero AI`) or the channel ID is wrong.
- **Buttons on the cards don't do anything**: `SLACK_APP_TOKEN` is missing or wrong, or the SubZero AI service is stopped (check services.msc).
- **The site shows a Cloudflare error page**: the SubZero AI service isn't running, or the tunnel's URL isn't `127.0.0.1:8787`.

## For developers

- Run the tests: `python -m pip install -r requirements-dev.txt` (or, for the exact versions that ship, `python -m pip install --no-deps -r packaging/requirements-lock.txt -r packaging/requirements-test.txt`), then `python -m unittest discover -s tests -t .`
- `python -m subzero_ai --help` lists the commands.
- Layout: `subzero_ai/web.py` (pages, actions, live updates), `web_admin.py` (Settings, setup, and recovery pages), `saved_settings.py`, `backup.py`, `maintenance.py` (nightly backup, retention, update check), `health.py`, `approvals.py` (the approval loop), `generation.py` (Claude replies), `slack_*.py` and `socket_mode.py` (Slack), `auth.py` (Sign in with Slack), `store.py` and `db.py` (SQLite).
- The app talks to Slack and the Claude API over plain HTTPS with `requests`, and to Slack Socket Mode with `websockets`, through small adapters with test stand-ins, instead of the Slack Bolt and Anthropic SDKs. That keeps the dependency list short for the Windows packaging and lets the whole approval loop be tested offline.
- `python -m subzero_ai --help` also lists `backup`, `backups`, `restore`, `recovery-code`, and `reset-setup`.
- **Windows release.** `python packaging/build_windows.py` builds `dist/SubZeroAI-windows.zip` on any OS: the embeddable Python 3.13, the packages pinned in `packaging/requirements-lock.txt` (installed for 64-bit Windows from `requirements-windows.txt`, which lists each wheel's SHA-256; refresh it with `--write-hashes` after changing the lock file), the [WinSW](https://github.com/winsw/winsw) service wrapper, cloudflared, and the scripts in `packaging/windows`. Every download is pinned by SHA-256. Pushing a tag like `v0.4.0` runs `.github/workflows/release.yml`, which tests, builds, and publishes the release that update.cmd downloads.
- The Windows scripts are written for Windows PowerShell 5.1 and must stay ASCII-only.
- Live updates use server-sent events. Each running app keeps them in memory, so run one copy of the app per database.
