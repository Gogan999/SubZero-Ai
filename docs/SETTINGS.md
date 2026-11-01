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
