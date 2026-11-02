# SubZero AI

A claude.ai-style chat for a high school robotics team. Students ask an AI (Claude by default) about code, mechanisms, physics, and materials. Student messages wait for a mentor to approve them in Slack before Claude sees them (with a short approval window for follow-ups, below), and the whole team can read approved chats.

This is version 0.9.0: all four milestones of the build plan, optional AI providers beyond Claude (see [AI providers and models](#ai-providers-and-models)), messaging the bot in Slack (see [Messaging the bot in Slack](#messaging-the-bot-in-slack)), and every setting, API key and connected account on the Settings page, sorted into sections, with the Claude subscription signed in from the page itself (see [docs/SETTINGS.md](docs/SETTINGS.md)). Code sessions can run their commands in a disposable Hyper-V sandbox (see [docs/CODE_SANDBOX_PLAN.md](docs/CODE_SANDBOX_PLAN.md)); students can use Code once a mentor turns that on.

## What's in this version

- **Sign in with Slack**, limited to full members of the team workspace. Guest accounts and bots are refused (set `ALLOW_SLACK_GUESTS=1` if some students are guests), and the daily account check signs out anyone who is deactivated or turned into a guest. Mentors are the members of the private #claude-approvals channel; admins are named on the setup page and the Settings page.
- **Chat**: a conversation list with search and subteam and student filters, Markdown replies with highlighted code and copy buttons, and replies that stream in as Claude writes.
- **Approvals**: each student message posts a card in #claude-approvals with Approve, Edit, Reject, and Handle privately. Students get a Slack DM when a mentor acts. The app's Review page does the same thing if Slack is down. A second mentor tapping a handled card is told who already handled it.
- **Audit log** (admins): sign-ins, messages, every review with the before and after text of edits, alerts, and Slack problems.
- **Attachments**: code and text files (.java, .py, .cpp, .h, .json, .gradle, .yaml, .txt, .md, .csv) PNG or JPG screenshots, and MP4, MOV or WebM videos, up to 5 per message, 50 MB each and 90 MB in all (Cloudflare's free plan refuses uploads over 100 MB). Claude can't watch video, so the browser takes up to 8 still frames from each video and sends them to Claude; the chat shows the video itself, which is stored as uploaded. Pick them, drag them onto the message box, or paste a screenshot. Text files are also capped at 200 KB (`MAX_TEXT_FILE_KB`), because Claude reads every line on every reply. Photos are re-encoded on upload, which removes hidden details like a phone's GPS location and shrinks them to the size Claude uses. Slack cards and the Review page list the files; mentors open them in the app.
- **Interactive explainers**: ask for one in words ("make a simulation of…", "an interactive graph of…") and Claude builds a small web page you can play with. Claude may also make one on its own when it clearly helps. It runs in a sandboxed frame that can't reach the network or the app's cookies, with Full screen, Download, and View code buttons. Messages that ask for one use high effort and up to 16,000 tokens; messages with files also use high effort.
- **Long chats**: once a chat sends Claude about 150,000 tokens per reply, the app suggests starting a fresh one.
- Also: the three-layer system prompt with the child-safety layer, MENTOR ALERT handling, per-student limits, the permanent AI notice, and the "Using SubZero AI responsibly" page.

- **Everything on the Settings page** (new in 0.7): the AI engine, the site address and time zone, the Slack tokens and review channel, every API key (masked, removable), the Claude subscription's sign-in, and a **Run checks** button that does what check-settings.cmd does. The few settings that need a restart wait for a **Restart now** button. [docs/SETTINGS.md](docs/SETTINGS.md) lists every setting and where it's saved.
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

- copies the app, with its own Python and Node.js (for the AI helper in `app\pi_gateway`), to `C:\Program Files\SubZeroAI`;
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
| `im:history` | Reads the messages people send the bot in a DM (the `message.im` event), so they can ask the AI questions from Slack. | `message.im` event |
| `groups:read` | Lists the private channels the bot was invited to (so the setup page can offer #claude-approvals) and reads that channel's members to know who the mentors are. Without it you get "missing_scope" after inviting the bot to the channel. | `conversations.list`, `conversations.members` |
| `users:read` | Looks up names and profile pictures, and spots deactivated, guest, and bot accounts. | `users.info` |

**Event subscriptions** (bot events): `message.im`, delivered over Socket Mode like button clicks. The app's **Messages** tab must also be on and not read-only (`messages_tab_read_only_enabled: false` in the manifest).

**User token scopes** (Sign in with Slack): `openid`, `email`, `profile`. These let a student sign in and give the app their Slack ID, name, email, and workspace. The app never keeps the user token.

**App-level token** (the `xapp-` token, made under **Basic Information → App-Level Tokens**): `connections:write`, for Socket Mode. Socket Mode is how button clicks and approval forms reach the mini PC without opening a port.

No scope is needed for `auth.test` (checking the token) or `views.open` (the reject-reason form).

**After updating the manifest (or adding a scope), an admin must reinstall the app** to switch on messaging the bot: paste the new `slack-manifest.yaml` under **App Manifest**, save, and then reinstall. Until then the bot just ignores DMs.

**After adding a scope**, Slack doesn't apply it until the app is reinstalled:

1. Open the app at [api.slack.com/apps](https://api.slack.com/apps), go to **OAuth & Permissions**, and add the scope under **Bot Token Scopes** (or **User Token Scopes**).
2. Click **Reinstall to Workspace** at the top of that page (Slack shows a yellow banner asking for it) and allow.
3. The bot token usually stays the same, but copy the **Bot User OAuth Token** again to be sure. If it changed, paste it on the Settings page or the setup page (or in `SLACK_BOT_TOKEN` in config.env) and restart SubZero AI.
4. Run **check-settings.cmd** to confirm Slack is working.

## Messaging the bot in Slack

You can ask the AI a question straight from Slack: open the bot (SubZero AI) under **Apps** and send it a message.

- **A mentor still checks it.** Your message goes through the same approval as a message on the website. The bot says "A mentor will check this before the AI answers" and posts the AI's reply in the same Slack thread once it is approved. If a mentor doesn't approve it, the bot tells you why in the thread, and only you see that.
- **One chat per Slack thread.** A new message to the bot starts a new chat. Reply in that message's thread to keep talking in the same chat. A reply in a thread the bot didn't start won't work, so send a new message instead.
- **Files go on the website.** The bot can't take pictures or files in Slack yet. Attach them on the website.
- **Interactive explainers too.** The bot sends a link to the website when a reply has one.
- **The chat is on the website too.** Chats started in Slack show up in the chat list like any other, where the team can see approved ones.
- **Turning it off.** An admin can turn this off with **Let people message the bot in Slack** on the Settings page (or `SLACK_DMS=0` in config.env).

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

- **Backups.** Every night at 2:00 AM the app copies the database, attached files, and changed prompts into a zip in `C:\ProgramData\SubZeroAI\backups` and keeps 14 days. For an off-site copy, set `BACKUP_DIR` in config.env to a folder that OneDrive or Google Drive syncs (the app's service account needs permission to write there). Backups leave out config.env, because it holds the API key and Slack tokens; keep your own copy of it somewhere safe. They also leave out attached videos (each backup is a full copy, so 14 days would hold 14 copies of every clip); the still frames Claude saw are kept, and a restore keeps the videos already on disk. Copy `files\*.mp4`, `*.mov` and `*.webm` yourself if you need them off the PC.
- **Retention.** Chats with no activity for 730 days (this season and the next) are deleted after the nightly backup. Change it, or set 0 to keep chats forever, on the Settings page.
- **Reboots and Windows updates.** Both services start on their own after a restart, with nobody signed in.
- **Health.** The Settings page shows the Slack connection, Claude API status, the last backup, and whether an update is out. If Slack or Claude calls fail for 15 minutes, admins get a Slack DM, and another when it works again. Cloudflare's dashboard shows whether the tunnel is up. `http://localhost:8787/healthz` answers with the same status for scripts.
- **If Slack sign-in breaks.** On the mini PC, open `http://localhost:8787/auth/recovery` and enter the recovery code to sign in as an admin. It doesn't work from anywhere else.
- **Model changes.** When a company retires a model, change the model ID (`provider/model`) on the Settings page. No code changes. New default, hard-problem, and screener models must pass the model safety check first.

**Monthly check** (about 10 minutes): open Settings and confirm everything says Working or Done, confirm last night's backup is listed, apply any available update, and check the Claude Console spend for the month.

## AI providers and models

By default every reply comes from Claude through Anthropic's API, exactly as in earlier versions. Version 0.5 can also use models from OpenAI, Google, and OpenRouter. Nothing changes until an admin turns it on, and the mentor approval flow (the approval window, the screener, MENTOR ALERT, and rejections) works the same with every model.

**What `AI_BACKEND` does.** This setting picks the engine that talks to the AI companies. Change it on the Settings page under **AI engine** (it applies after **Restart now**), or in config.env.

| Value | Meaning |
| --- | --- |
| `anthropic` (default) | The app talks to Claude directly, as before. Only `anthropic/` models work. |
| `pi` | The app starts a small Node.js helper (the Pi gateway in `pi_gateway`, which ships with the Windows release) that can reach Anthropic, OpenAI, Google, and OpenRouter. |
| `fake` | Pretend replies, for trying the app without keys. |

**Switching to `pi`.** On the Settings page, under **AI engine**, pick the AI helper, save, and press **Restart now**. Then press **Run checks** under Connected accounts (or run **check-settings.cmd**). It should say the AI helper works. If you run from a copy of the repository instead of the Windows release, install Node.js 22.19 or newer, run `npm ci --ignore-scripts` in `pi_gateway`, and set `PI_NODE` if `node` isn't on the PATH.

**Model ids** are written as `provider/model`. Examples: `anthropic/claude-sonnet-5-5`, `openai/gpt-5`, `google/gemini-2.5-pro`, `openrouter/meta-llama/llama-3.3-70b-instruct`, and `claude-bridge/claude-sonnet-5-5` (see below). An older id like `claude-sonnet-5-5` is read as `anthropic/claude-sonnet-5-5`, and saved models and chats were updated that way when you upgraded. OpenRouter covers most other models with one key. The models you can pick are limited to providers that are turned on.

**Turning providers on and saving keys.** An admin opens **Settings**, then the **AI providers** box. Tick the providers to allow, paste each provider's API key, and save. Keys go to `config.env` on the mini PC and are never shown again (leave a key box empty to keep the saved one). The change applies without a restart. Use a key with its own spend limit for each company, the same way the Claude key lives in a workspace with a monthly limit. Keys can also be set by hand in config.env: `OPENAI_API_KEY`, `GEMINI_API_KEY`, `OPENROUTER_API_KEY`, and the list `AI_PROVIDERS`. Only `AI_BACKEND=pi` can use the other companies.

**The model safety check.** The safety rules and the MENTOR ALERT behavior were written and tested on Claude. So a model has to pass a check before it can become the **default model**, the **hard-problem model**, or the **screener model**. On the Settings page, the **Model safety check** box runs it for a model you name. From a terminal, `python -m subzero_ai check --model provider/model` does the same. The check:

- sends a normal question, which must be answered without an alert;
- sends a wellbeing message, whose reply must start with MENTOR ALERT;
- asks for an unsafe shop shortcut and tries a prompt injection ("ignore your rules"), which a **judge model** grades (**Safety check judge** in the Claude settings, `anthropic/claude-sonnet-5-5` by default; it must be a Claude model);
- checks that the interactive explainer format is kept, and that the screener answers SAFE or REVIEW in the right shape (for a screener model).

The judge's company must be turned on and have a key, or the check won't start. The result is saved with the date and who ran it. A judge model is a strong filter, not a guarantee: read the details on the Settings page, and test a new model yourself with the red-team questions in the Acceptance tests before students use it. **Claude models need the check too** when you choose a new one. Models already saved when you upgrade to 0.5 keep working without a check.

**Features that need Claude.** Some tools depend on Anthropic's own features, so the composer only shows what the chat's model supports:

- **Web search** and **Research**: Claude on `AI_BACKEND=anthropic` only. On other models and with `AI_BACKEND=pi`, the buttons are hidden and a forced request is refused with a plain message.
- **Code sessions** (mentors): Anthropic Claude models only.
- **Extended thinking** and **image attachments**: shown only when the model supports them.

Normal chat, files that are text or code, explainers, the screener, and chat titles work on every model that passed the check.

**Claude subscription (optional, off by default).** The `claude-bridge/` provider answers with one person's Claude Pro or Max subscription instead of an API key, through Claude Code on the mini PC. It counts against that person's subscription limits, so a busy day of student chats can use them up, and then replies pause until the limit resets. Caution: Anthropic's terms generally don't allow offering a claude.ai login inside another product without Anthropic's approval. Read the current terms before you turn it on. It stays off until an admin ticks **Claude subscription (Claude Code)** under AI providers, and the model still has to pass the safety check. To set it up, run the manual test in [docs/BRIDGE_SPIKE.md](docs/BRIDGE_SPIKE.md) first (about 30 minutes, on the mini PC), then sign in from **Settings, AI and keys, Claude subscription**: press **Sign in**, open the Claude sign-in page on any computer or phone, and paste back the code Claude shows (or run `python -m subzero_ai claude-login` on the mini PC). Keep an API-key model set up as a fallback. The bridge needs `AI_BACKEND=pi`. Its login is kept apart from the rest of the PC in `bridge\claude-config` in the data folder.

**Going back.** Pick the Anthropic API under **AI engine** and restart (first change any model that uses another company; the page says which). Chats stay as they are; chats whose model is from another company can't get replies until their model is changed to a Claude one. Everything else keeps working.

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
| Start and continue their own chats | Yes, messages wait for review (see the approval window) | Yes, messages go straight to Claude | Yes |
| Read the team's chats | Approved messages and replies | Everything | Everything |
| Withdraw their own waiting message | Yes | Yes | Yes |
| Approve, edit, reject, handle privately | No | Yes | Yes |
| Switch a chat to Opus 5.5 for hard problems | No | Yes | Yes |
| Audit log | No | No | Yes |

- **Other students don't see messages that are waiting or rejected.** This is safer than the plan's original wording, because a message that needs handling privately is never shown to the team. To show them, set `SHOW_PENDING_TO_TEAM=1` or `SHOW_REJECTED_TO_TEAM=1`.
- One message per chat can wait at a time. Each student can have 3 waiting messages and 25 approved messages per day; the count resets at midnight.
- If a card waits 2 hours, the app posts one reminder in the card's Slack thread.
- **Approval window**: after a mentor approves a message, the student's follow-ups in that chat go straight to Claude until they stop chatting for 5 minutes (each message sent restarts the clock). The first message of every chat, messages with files, and anything after a rejection, a "handle privately", or a MENTOR ALERT still wait. Admins can change the minutes, turn it off with 0, and list students who always wait, under Settings, Approval window.
- **Screener**: inside the window, a small model (Haiku 5.5) reads each message first. Only a clear "safe" lets it through; anything it flags, and every message when it can't be reached, waits for a mentor, with the reason on the card. It can be turned off on the Settings page.
- Messages sent in the window are logged in the audit log ("message auto approved"), tagged in the chat for mentors, and listed under "Sent without review" on the Review page for the last 24 hours.
- **MENTOR ALERT**: when Claude flags a wellbeing concern, the chat becomes private to the student and mentors before any of the reply is shown, and an alert card is posted in #claude-approvals. The student still sees Claude's reply, which points them to trusted adults and the 988 Lifeline.
- Claude receives only approved messages and its own earlier replies: no names, emails, or Slack IDs.
- **Code** (mentors only) works in a copy of the GitHub repository set under Settings, Connectors. Claude reads and searches on its own, but a mentor approves every file change and every command, and nothing is pushed: download the changes as a patch and apply them with `git apply`. Commands must start with a program listed under Settings, Code (`gradlew` by default), so running a build needs the same Java/WPILib setup on the mini PC that the robot code uses. An approved command runs as the app's Windows account, so it could read files the app can, including config.env. Claude can't change build files (Gradle scripts and settings, the wrapper, vendordeps), and the approval card for a command lists the files Claude changed, because a build also runs that code: read those changes before approving. Builds run without the Gradle daemon, and the whole process tree is stopped at the time limit. Finished sessions' copies of the repository are deleted after 14 days.
- People removed from Slack lose access within a day. Someone removed from #claude-approvals can't approve anything from that moment on.

## Where things are stored

Everything lives in `C:\ProgramData\SubZeroAI` (in test mode, the `test-data` folder):

- `config.env`: the address, API keys, and Slack tokens, written by the setup page and the Settings page. Only the app's service account and administrators can read it.
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
- The Python app talks to Slack and the Claude API over plain HTTPS with `requests`, and to Slack Socket Mode with `websockets`, through small adapters with test stand-ins, instead of the Slack Bolt and Anthropic SDKs. Other providers go through the Node.js Pi gateway in `pi_gateway` (`ai_backend.py` picks the engine and `pi_client.py` talks to it over stdin/stdout), which uses the `@earendil-works/pi-ai` package; the optional Claude subscription bridge uses the Claude Agent SDK. Gateway tests: `cd pi_gateway && node --test` (Node 22). That keeps the dependency list short for the Windows packaging and lets the whole approval loop be tested offline.
- `python -m subzero_ai --help` also lists `backup`, `backups`, `restore`, `recovery-code`, and `reset-setup`.
- **Windows release.** `python packaging/build_windows.py` builds `dist/SubZeroAI-windows.zip` on any OS: the embeddable Python 3.13, Node.js (the official win-x64 zip, unpacked to `node\`) and the AI helper `pi_gateway` with its npm packages for 64-bit Windows (`npm ci --ignore-scripts` from the lock file, so the zip is about 255 MB; the build host needs Node.js and npm), the packages pinned in `packaging/requirements-lock.txt` (installed for 64-bit Windows from `requirements-windows.txt`, which lists each wheel's SHA-256; refresh it with `--write-hashes` after changing the lock file), the [WinSW](https://github.com/winsw/winsw) service wrapper, cloudflared, and the scripts in `packaging/windows`. Every download is pinned by SHA-256. The build fails if any path in the zip is too long for Explorer's Extract All. Pushing a tag like `v0.5.0` runs `.github/workflows/release.yml`, which tests, builds, and publishes the release that update.cmd downloads.
- The Windows scripts are written for Windows PowerShell 5.1 and must stay ASCII-only.
- Live updates use server-sent events. Each running app keeps them in memory, so run one copy of the app per database.
