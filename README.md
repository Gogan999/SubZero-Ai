# SubZero AI

A claude.ai-style chat for FRC Team 5690. Students ask Claude about code, mechanisms, physics, and materials. Every student message waits for a mentor to approve it in Slack before Claude sees it, and the whole team can read approved chats.

This is version 0.3.0: milestones 1 to 3 of the build plan.

## What's in this version

- **Sign in with Slack**, limited to full members of the team workspace. Guest accounts and bots are refused (set `ALLOW_SLACK_GUESTS=1` if some students are guests), and the daily account check signs out anyone who is deactivated or turned into a guest. Mentors are the members of the private #claude-approvals channel; admins are named in `config.env`.
- **Chat**: a conversation list with search and subteam and student filters, Markdown replies with highlighted code and copy buttons, and replies that stream in as Claude writes.
- **Approvals**: each student message posts a card in #claude-approvals with Approve, Edit, Reject, and Handle privately. Students get a Slack DM when a mentor acts. The app's Review page does the same thing if Slack is down. A second mentor tapping a handled card is told who already handled it.
- **Audit log** (admins): sign-ins, messages, every review with the before and after text of edits, alerts, and Slack problems.
- **Attachments**: code and text files (.java, .py, .cpp, .h, .json, .gradle, .yaml, .txt, .md, .csv) and PNG or JPG screenshots, up to 5 per message and 5 MB each. Pick them, drag them onto the message box, or paste a screenshot. Text files are also capped at 200 KB (`MAX_TEXT_FILE_KB`), because Claude reads every line on every reply. Photos are re-encoded on upload, which removes hidden details like a phone's GPS location and shrinks them to the size Claude uses. Slack cards and the Review page list the files; mentors open them in the app.
- **Interactive explainers**: tick *Interactive explainer* and Claude builds a small web page, like a simulation you can play with. It runs in a sandboxed frame that can't reach the network or the app's cookies, with Full screen, Download, and View code buttons. These replies use high effort and up to 16,000 tokens; messages with files also use high effort.
- **Long chats**: once a chat sends Claude about 150,000 tokens per reply, the app suggests starting a fresh one.
- Also: the three-layer system prompt with the child-safety layer, MENTOR ALERT handling, per-student limits, the permanent AI notice, and the "Using SubZero AI responsibly" page.

Still to come: the Windows installer, setup page, Windows services, nightly backups, and admin settings page (milestone 4). Until then the app runs in a console window and settings live in `config.env`.

## Try it in test mode

Test mode runs on any Windows PC with sample sign-ins and a pretend Claude, so you need no accounts or keys. It takes about 10 minutes.

1. Install Python 3.12 or 3.13 from [python.org](https://www.python.org/downloads/windows/). In the installer, tick **Add python.exe to PATH**.
2. Unzip this folder anywhere, for example `C:\SubZeroAI`.
3. Double-click **start-test-mode.cmd**. The first run installs packages, then the browser opens.
4. Sign in as **Ava (student)** and send a message.
5. Open a private browser window, sign in as **Mentor Kim**, open **Review**, and approve it. Ava's window updates by itself and the reply streams in.

Also try:

- Edit, Reject, and Handle privately from Mentor Kim's Review page.
- Signing in as **Ben (student)** to see what other students can and can't see.
- A message containing `ALERT-TEST`, which makes the pretend Claude raise a mentor alert, or `ERROR-TEST`, which makes it fail so you can try **Try again**.
- Attaching files, or ticking **Interactive explainer** to get a sample explainer. Its last line confirms the network is blocked inside the frame.

Test data lives in the `test-data` folder. Delete it to start over. Close the window to stop the app.

On a Mac or Linux computer, run `scripts/run.sh --test-mode --open` instead.

## Set it up for real

This connects real Slack sign-in, approval cards, and Claude. Do it on the mini PC.

### 1. Create config.env

Double-click **start.cmd**. The first time, it creates `C:\ProgramData\SubZeroAI\config.env` and opens it in Notepad. Keep it open for the next two steps; the notes in the file say what goes where. Leave `DEV_LOGIN=0` and `FAKE_CLAUDE=0`.

### 2. Claude API key

At [platform.claude.com](https://platform.claude.com), create a workspace named **SubZero AI**, set its monthly spend limit, and create an API key inside that workspace. The key goes in `ANTHROPIC_API_KEY`.

### 3. Slack app

1. Open `slack-manifest.yaml` and replace `ai.yourdomain.org` with the app's real address.
2. Go to [api.slack.com/apps](https://api.slack.com/apps), choose **Create New App**, then **From a manifest**. Pick the team workspace, choose YAML, paste the file, and create the app.
3. Click **Install to Workspace** and allow it.
4. Copy these into `config.env`, then save it:

| Setting | Where to find it in the Slack app's settings |
| --- | --- |
| `SLACK_BOT_TOKEN` (starts with `xoxb-`) | OAuth & Permissions, Bot User OAuth Token |
| `SLACK_APP_TOKEN` (starts with `xapp-`) | Basic Information, App-Level Tokens: generate one with the `connections:write` scope |
| `SLACK_CLIENT_ID` and `SLACK_CLIENT_SECRET` | Basic Information, App Credentials |

5. In Slack, create a private channel named **#claude-approvals**, add the coaches and mentors, and type `/invite @SubZero AI` in it. Click the channel name, copy the **Channel ID** at the bottom, and put it in `SLACK_APPROVALS_CHANNEL`.
6. Put the coaches' Slack member IDs in `ADMIN_SLACK_IDS`, separated by commas. In Slack, click a person's name, open the three-dot menu, and choose **Copy member ID**.

### 4. Check the settings

Double-click **check-settings.cmd**. It checks each setting, makes a tiny test request to each Claude model (a fraction of a cent), and says exactly what to fix. Run it again until nothing says FAIL.

### 5. Cloudflare Tunnel

In the Cloudflare Zero Trust dashboard, create a tunnel named **subzero-ai** and choose Windows. Follow the dashboard's steps to install the connector on the mini PC as a Windows service. Then add a public hostname (Cloudflare may call it a published application route): **ai.yourdomain.org**, type **HTTP**, URL **127.0.0.1:8787**.

### 6. Start it

Double-click **start.cmd**. It runs the settings check, then starts the app. Keep the window open: closing it stops the app. Milestone 4 turns the app into a Windows service that starts on its own after a reboot.

Open `https://ai.yourdomain.org` on a phone and sign in with Slack.

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

- `config.env`: settings and secrets.
- `data\subzero.db`: the database. Back it up by copying it while the app is stopped. Automatic backups arrive in milestone 4.
- `logs\subzero.log`: the app's log, one file per day, kept for 14 days.
- `prompts\`: optional replacements for the system prompt. Copy `subzero_ai\prompts\team.md` here and edit it to add the programming subteam's language and conventions. Changes apply to the next reply, with no restart.

## Troubleshooting

- **"Python 3.12 or 3.13 isn't installed"**: install it from python.org with "Add python.exe to PATH" ticked, then close and reopen the window.
- **Sign-in says the sign-in couldn't be verified or the link expired**: check that the computer's clock is right and that `PUBLIC_URL` matches the address in the browser exactly.
- **Slack says "redirect_uri did not match"**: the Slack app's redirect URL must be `PUBLIC_URL` followed by `/auth/slack/callback`. Run the settings check to see the exact value.
- **No cards appear in Slack**: run the settings check. Most often the app isn't in the channel (`/invite @SubZero AI`) or the channel ID is wrong.
- **Buttons on the cards don't do anything**: `SLACK_APP_TOKEN` is missing or wrong, or the app window was closed.
- **The site shows a Cloudflare error page**: the app isn't running, or the tunnel's URL isn't `127.0.0.1:8787`.

## For developers

- Run the tests: `python -m pip install -r requirements-dev.txt`, then `python -m unittest discover -s tests -t .`
- `python -m subzero_ai --help` lists the commands.
- Layout: `subzero_ai/web.py` (pages, actions, live updates), `approvals.py` (the approval loop), `generation.py` (Claude replies), `slack_*.py` and `socket_mode.py` (Slack), `auth.py` (Sign in with Slack), `store.py` and `db.py` (SQLite).
- The app talks to Slack and the Claude API over plain HTTPS with `requests`, and to Slack Socket Mode with `websockets`, through small adapters with test stand-ins, instead of the Slack Bolt and Anthropic SDKs. That keeps the dependency list short for the Windows packaging and lets the whole approval loop be tested offline.
- Live updates use server-sent events. Each running app keeps them in memory, so run one copy of the app per database.
