# SubZero Hub build plan

SubZero Hub is SubZero AI grown into one site for the team's tools, all behind one Sign in with Slack.
The AI chat stays exactly as it is and becomes one tool inside the Hub. The first new tools are a
**Time Clock** (to replace Attendance Bot) and a **Calendar**. More tools can be added later without
touching the core.

The Hub is kept close enough to SubZero AI that it can be merged back later. That rule shapes many of
the choices below. For example, existing files aren't moved or renamed, and internal names stay the same.

## Decisions so far

| Topic | Decision |
| --- | --- |
| Name | **SubZero Hub**. The AI chat is still called SubZero AI inside the Hub. |
| Repository | The `hub` branch of `Gogan999/SubZero-Ai`, to be merged into `main` when it's ready. |
| Host | The same Windows mini PC, installed as an **in-place upgrade**: chats, users and settings are kept. It must also be able to move to a cloud server later. |
| Internal names | No change: the `subzero_ai` package, the `SubZeroAI` Windows service, `C:\ProgramData\SubZeroAI`, and the release zip name. Only the names people see change. |
| Web address | `hub.yourdomain.org`. The old `ai.` address redirects to it. |
| Slack | One Slack app for every tool (one of the free plan's 10 app slots), renamed to SubZero Hub. |
| Roles | **Coach, Mentor, Lead, Student**, one role per person. Coaches assign them on a People page, and new people start as students. Being in #claude-approvals can still make someone a mentor (optional). |
| Permissions | Coaches can change which roles can do what on a Permissions page. Coaches always keep full access. |
| Users | 25 to 45 people, students plus adults. No payroll, shifts, breaks, pay periods or approval steps. |
| Practice window | Configurable. Default: weekdays after 3:15 pm, any time on weekends. Calendar events also open clock-in 15 minutes before they start. |
| Reminder | A "Still here?" Slack DM after **5 hours** clocked in. |
| Editing time | Everyone can edit their own entries. Only people with permission (coaches and mentors by default) can edit other people's. Every edit is logged. |
| Calendars | Show the team schedule, and open clock-in around events. Google Calendar syncs from its private iCal address; any other calendar can be uploaded as a `.ics` file. Everyone signed in can see the calendar. |
| Exports | CSV. The Attendance Bot history is imported from its CSV export once we have a sample. |

## What can't be done, and what we do instead

- **Bluetooth detection by a laptop doesn't work.** iPhones and iPads change their Bluetooth address every few
  minutes. Recognizing them would mean pairing every device with the laptop, and a locked phone only
  broadcasts now and then. Safari has no Web Bluetooth either.
- **A geofence can't tell when practice starts.** Students are already at school, so an "Arrive" automation
  fires in the morning. A geofence also can't tell the shop apart from the rest of the building.
- **What we do instead:** clock-in is one deliberate action at the shop door: scan a rotating QR code
  on a kiosk laptop, or tap an NFC sticker. Clock-out is automatic when someone leaves the school (a
  Shortcuts "Leave" automation), with safety nets for anyone who forgets.

## Architecture

### Core and modules

The **core** owns everything the tools share. Each **tool is a module** that plugs into the core. A
module never reaches into another module's tables. If two tools need to work together (for example,
calendar events opening the time clock), one module calls the other through a small function that
module offers.

```
subzero_ai/
  (existing files stay where they are: web.py, store.py, db.py, slack_handlers.py, ...)
  hub/                      core additions
    modules.py              the Module interface, per-module migrations, the tool switcher's entries
    permissions.py          roles, permissions, role defaults and the Grants table
    core.py                 the Hub's own module (hub.manage, the assigned_role column, hub_role_permissions)
    people.py               People and Permissions pages
    router.py               routes Slack DMs, button taps and the Home tab to modules
  modules/
    ai.py                   the existing chat as a module: its permission, tool entry and Home tab section
    timeclock/              routes.py, store.py, slack.py, rules.py, kiosk.py, templates/
    calendar/               routes.py, store.py, ics.py, sync.py, templates/
```

Existing files aren't moved in this work. That keeps merge conflicts with SubZero AI small. The AI
module is a thin wrapper that registers the pages that already exist.

### The Module interface

Every module is one object that tells the core what it adds:

```python
class Module:
    key = "timeclock"                 # URL prefix, permission prefix, settings section
    name = "Time Clock"
    permissions: list[Permission]     # e.g. Permission("timeclock.edit_others", "Edit other people's time",
                                      #                 default_roles={"coach", "mentor"})
    migrations: list[str]             # this module's own SQL, applied in order

    def routes(self) -> list[BaseRoute]: ...              # mounted under /timeclock
    def nav(self, viewer) -> list[NavItem]: ...            # entries for the tool switcher
    def home_cards(self, s, viewer) -> list[Card]: ...     # cards on the Hub home page
    def slack_home_blocks(self, s, viewer) -> list[dict]: ...   # section of the Slack Home tab
    async def on_dm(self, s, event, viewer) -> bool: ...    # True if this module handled the DM
    async def on_action(self, s, payload) -> bool: ...      # button taps whose action_id starts with "timeclock:"
    async def on_command(self, s, payload) -> dict | None: ...  # slash commands it owns (/clock)
    async def tick(self, s, now) -> None: ...               # called every minute (reminders, syncs)
    def settings_fields(self) -> list[SettingField]: ...    # its section of the Settings page
    def health(self, s) -> list[HealthItem]: ...            # lines in the Settings page's health list
```

Every method is optional. A module that only adds pages fills in `routes` and `nav`.

### Database

- SQLite stays. It's plenty for 45 people and is already backed up every night. **No Docker and no Postgres.**
- The core's `MIGRATIONS` list stays as it is, so the AI tables keep their history.
- Each module has its own migration list. A new `module_migrations(module, version)` table records how far
  each module has been upgraded.
- Module tables are prefixed with the module key (`tc_`, `cal_`), so nothing clashes with SubZero AI's tables.
- Nightly backups already copy the whole database, so new modules are covered without extra work.

### Roles and permissions

- **Roles:** the stored role keys stay SubZero AI's `admin`, `mentor` and `student`, plus a new `lead`. People
  see them as **Coach**, **Mentor**, **Lead** and **Student**. A `users.assigned_role` column holds the role picked
  on the People page.
- **How a person's role is worked out:**
  1. People listed as admins in Settings, and the recovery sign-in, are always coaches. This means a coach
     can never be locked out.
  2. Members of #claude-approvals are mentors, unless their assigned role is coach. This can be turned off on
     the Settings page ("Members of the approvals channel are mentors").
  3. Everyone else gets their assigned role. New people start as students.
- **Permissions:** each module registers its permissions with default roles. A `hub_role_permissions` table holds
  what coaches change on the Permissions page, which is a grid of checkboxes. The Coach column can't be unticked.
- **Safety rule:** a permission can name roles that may never have it. Acting as an AI mentor and managing the
  Hub can never be given to students, which keeps the mentor approval flow as strong as it is today.
- **Code:** `Viewer.can("timeclock.edit_others")`. The existing `is_mentor` and `is_admin` become
  `can("ai.mentor")` and `can("hub.manage")`, so the current checks keep working unchanged. `page(require=...)`
  also accepts a permission name.

Default permissions:

| Permission | Coach | Mentor | Lead | Student |
| --- | :-: | :-: | :-: | :-: |
| `timeclock.use`: clock in and out | ✓ | ✓ | ✓ | ✓ |
| `timeclock.edit_own`: edit own entries | ✓ | ✓ | ✓ | ✓ |
| `timeclock.view_all`: who's in now, everyone's hours | ✓ | ✓ | ✓ | – |
| `timeclock.edit_others`: edit anyone's time, CSV export and import | ✓ | ✓ | – | – |
| `ai.mentor`: act as a mentor in SubZero AI (what mentors do today) | ✓ | ✓ | – | never |
| `calendar.view`: see the calendar | ✓ | ✓ | ✓ | ✓ |
| `calendar.manage`: add calendars and upload files | ✓ | ✓ | – | – |
| `hub.manage`: Settings, People, Permissions, audit log | ✓ | – | – | – |

### Slack

- **One app, renamed SubZero Hub.** The manifest adds:
  - the Home tab (`home_tab_enabled: true`) and the `app_home_opened` event
  - a `/clock` slash command, which needs the `commands` scope
  - the new `hub.` redirect URL

  Coaches update the manifest and reinstall the app once.
- **DM router:** every message sent to the bot goes to each module's `on_dm` in turn. The Time Clock
  handles only exact commands:
  - `in`, `out`, `status`, `hours`, `clock in`, `clock out`
  - any capitalization, with an optional `!` or `.`

  **Everything else goes to the AI exactly as it does today**, through mentor approval. A message in a
  thread the AI started always goes to the AI.
- **Buttons:** an action ID starts with the module key (`timeclock:out_now`), and the router sends it to that
  module. The AI's existing action IDs have no prefix and keep going to the AI.
- **Home tab:** the core builds it from each module's `slack_home_blocks`, such as the Clock In/Out button,
  your hours this week and today's events.

### Web layout

- `/` becomes the **Hub home page**: your clock status with one big button, today's events, and a tile
  for each tool you can use.
- The sidebar gets a **tool switcher** (Home, AI, Time Clock, Calendar) above the AI's own links.
- **The AI's addresses don't change** (`/chats`, `/c/123` and so on), so old links and Slack messages keep working.
- New tools live under their key (`/timeclock/...`, `/calendar/...`).
- `docs/DESIGN.md` is still the visual standard. The new pages are built for phones first, because
  students clock in on phones and iPads.

## Time Clock module

### Data

- `tc_entries`: id, user, clock-in time, clock-out time, how each was recorded, flags, note.
  - "How it was recorded" is one of: slack, web, kiosk, nfc, shortcut_leave, auto_close, edit.
  - A unique index allows only one open entry per person.
- `tc_edits`: entry, who edited it, the times before and after, an optional reason, when. Nothing is ever
  overwritten without a record here.
- `tc_tokens`: personal keys for Shortcuts. They're stored hashed and shown once. A person can have
  several (phone and iPad) and can revoke any of them.
- `tc_kiosks`: paired kiosk laptops (name, hashed secret, last seen).
- **Settings:**
  - practice-window rules (default: Mon to Fri from 3:15 pm, Sat and Sun all day)
  - how many minutes before a calendar event clock-in opens (15)
  - reminder after 5 hours
  - nightly auto clock-out at 10:00 pm
  - Leave grace period of 10 minutes

### Rules

- **Clocking in:** allowed inside the practice window, or from 15 minutes before a calendar event that has
  "opens the time clock" turned on until it ends. Outside those times, the bot and the kiosk say when
  clock-in opens. People with `timeclock.edit_others` can always add time.
- **Clocking in twice** does nothing and says "You've been clocked in since 3:22 pm."
- **Clocking out** when you aren't clocked in does nothing (this is why leaving school at lunch is harmless).
- **Leave events** from Shortcuts wait out the grace period before closing an entry. If the person scans in
  again during that time, the entry stays open. This smooths over an iPad's rough location.
- **After 5 hours**, a Slack DM: "Still here? [Clock out now] [Clock out at…] [Still here]". "Still here"
  asks again 2 hours later.
- **Nightly auto clock-out:** any entry still open at 10:00 pm is closed at the **last time we know the
  person was there**: when they clocked in, or their last "Still here" tap. This never adds hours by
  accident. The entry is flagged, and the next time the person uses the bot it asks "When did you leave
  on Tuesday?" with quick buttons. People with `timeclock.edit_others` see flagged entries in a list.
- **Edits:** people with `timeclock.edit_own` can change, add or delete their own entries. The edit is
  logged, and the entry shows a small "edited" mark to people with `timeclock.view_all`. Edits are allowed
  outside the practice window, because the point is fixing mistakes.

### Ways to clock in and out

| Way | In | Out | Works on |
| --- | :-: | :-: | --- |
| Slack DM `in` / `out`, the Home-tab buttons, `/clock` | ✓ | ✓ | everything |
| Web app button (on the Hub home page, and can be added to the home screen) | ✓ | ✓ | everything |
| **Kiosk QR code** at the shop door | ✓ | ✓ | anything with a camera |
| **NFC sticker** with a Shortcuts NFC automation | ✓ | ✓ | iPhone |
| **Shortcuts "Leave"** automation on the school's location | – | ✓ | iPhone, iPad |

### Kiosk

- **Pairing:** a coach opens `/timeclock/kiosk` on the shop laptop and enters a code shown on the Settings
  page. The laptop gets a long-lived kiosk cookie. It needs no Slack sign-in, and it can only show the
  kiosk page.
- **What it shows:** a full-screen page with:
  - a QR code that changes every 30 seconds
  - who's in right now (first names only)
  - "Clock-in opens at 3:15" outside the window
- **The QR code** links to `/timeclock/k/<kiosk>/<code>`. The code is an HMAC of the kiosk's secret and the
  current 30-second slot, and the current and previous slots are accepted. A photo of the code stops
  working within a minute. The page clocks the person in, or out if they're already in, and shows a big
  confirmation.
- **Sign-in for scans:** a Camera scan always opens in Safari, so the person must be signed in to the Hub in
  Safari. A Hub icon added to the home screen keeps its own, separate sign-in. Sign-ins last 14 days and renew on
  every visit (the existing session cookie). If someone isn't signed in, the scan goes through Sign in with Slack and
  then finishes clocking them in.
- **The QR image** is made in the browser with a small pinned JavaScript library, or on the server with
  `segno`, which is pure Python. Either is pinned by SHA-256 like everything else in the Windows build.
- **Laptop setup:** Edge or Chrome in kiosk mode, started when Windows starts, with sleep turned off. Any
  laptop with internet works.

### Shortcuts

- **Setup:** on the Time Clock's setup page, a person taps **Make my key**, then **Add the Shortcut**. That's
  a shared iCloud link; when they add it, it asks once for the key. A step-by-step page with screenshots
  shows how to make:
  - the **Leave** automation: location = school, Run Immediately
  - the **NFC** automation (iPhone): scan the sticker, Run Immediately

  Apple doesn't let anyone share automations, so each person makes their own. It takes about 2 minutes.
- **What the Shortcut sends:** `POST /api/timeclock/event`, with the key as `Authorization: Bearer <key>` and a
  body of `{"event": "leave" | "nfc", "tag": "<sticker id>"}`. The phone's location never leaves the phone.
- **Check first:** whether Jamf allows Shortcuts and shared shortcuts on the school iPads. Try it on one
  iPad before rolling it out.

### Pages

- **My time:** this week's and the season's hours, my entries (edit, add, delete), and Shortcut setup.
- **Team:** for people with `timeclock.view_all`: who's in now, and hours per person for any date range.
- **Admin:** for people with `timeclock.edit_others`: edit anyone's time, the flagged list, CSV export
  (one row per entry or per person), CSV import, and kiosk pairing.

### Attendance Bot import

- We'll write the column mapping once we have a sample export.
- The import page shows a preview first, matches people by Slack ID or email, lists rows it can't match,
  and records that the import happened.
- Imported entries are marked as imported and can be undone as one batch.

## Calendar module

- **Calendars:** `cal_sources`: name, colour, type (iCal address or uploaded file), whether it opens the
  time clock, last sync, last error.
  - The Google private iCal address is a secret: masked like an API key, visible only to people with `calendar.manage`.
  - Outlook and Apple published calendar addresses work the same way.
- **Events:** `cal_events`: calendar, iCal UID, start, end, all-day, title, location, notes.
  - Recurring events are expanded from 30 days back to 180 days ahead each time a calendar syncs.
  - Uses the `icalendar` and `recurring-ical-events` libraries, pinned in `requirements-lock.txt`.
- **Syncing:** iCal addresses refresh every 15 minutes, using the module's `tick`.
  - If a sync fails, the old events are kept and the health list shows the error.
  - Uploaded `.ics` files are a one-time import. Uploading the same calendar again replaces its events.
- **Pages:** an agenda list (the default on phones), a week view and a month view. Events can be filtered
  by calendar, and today's events show on the Hub home page and the Slack Home tab.
- **Linked to the Time Clock:** the calendar module offers `events_between(start, end, opens_clock=True)`, and
  the Time Clock's rules call it.
- **Later, if wanted:** a Slack reminder before events, the Hub's own iCal feed people can subscribe to, and
  attendance per event.

## Renaming and upgrading the mini PC

- **Names people see change from SubZero AI to SubZero Hub:**
  - page titles, the sidebar, the setup page and the Slack manifest
  - the README and the `.cmd` tools' messages
  - "SubZero AI" stays as the name of the AI tool
- **Update source:** unchanged (`Gogan999/SubZero-Ai`). Hub releases are published there from the `hub` branch,
  and `update.cmd` installs the latest one. Version numbers start at **1.0.0**.
- **Upgrade steps on the mini PC:**
  1. Run `back-up-now.cmd`.
  2. Run `update.cmd` from a SubZero Hub release zip (`update.cmd -Zip path\to\zip` for the first one).
  3. In Cloudflare, add a public hostname `hub.yourdomain.org` on the same tunnel, pointing at `127.0.0.1:8787`.
  4. On the Settings page, change the site address to `https://hub.yourdomain.org`. Requests to the old
     address get a 301 redirect.
  5. At api.slack.com, paste the new `slack-manifest.yaml` and reinstall the app.
  6. Run `check-settings.cmd`. Then open People and check that everyone's role is right.
- **Moving to the cloud later:** the app already runs on Linux (`scripts/run.sh`). The steps:
  1. Make a small Ubuntu server.
  2. Install the app as a systemd service, and install `cloudflared` with the same tunnel token.
  3. Copy `config.env` and restore the latest backup zip.
  4. Point the tunnel at the new server.

  No code changes. A "Moving to a cloud server" guide is part of the docs work below.

## Build order

Each phase ends with every existing test passing, plus new tests for what it adds, and a short
README update.

0. **Repository:** *done: the `hub` branch of SubZero-Ai*
   - change the user-facing name
   - update CLAUDE.md
1. **Core and modules:** *done (on the local `hub` branch)*
   - module registry and per-module migrations
   - roles and permissions, People and Permissions pages
   - Slack router, Home tab, Hub home page, tool switcher
   - the AI module wrapper

   No change to how the AI chat behaves. Until a second tool exists, `/` still opens the chat and the sidebar
   shows no tool switcher.
2. **Time Clock basics:**
   - Slack `in` / `out` / `status` / `hours`, `/clock`, Home-tab buttons
   - the My time, Team and Admin pages
   - CSV export
   - edit log

   *This is enough to replace Attendance Bot.*
3. **Safety nets:** practice window, 5-hour DM, nightly auto clock-out, the "when did you leave?" prompt,
   the flagged list.
4. **Door check-in:** kiosk pairing and the rotating QR code, NFC, personal keys and the Shortcuts API,
   setup guides.
5. **Calendar:** iCal address sync, `.ics` upload, agenda/week/month pages, calendar events opening the
   time clock.
6. **Attendance Bot import:** once a sample CSV is available.
7. **Docs:** the upgrade guide, kiosk setup, Shortcut setup with screenshots, moving to a cloud server.

If the Attendance Bot trial runs out before phase 2 ships, phase 1 can be cut down to the module registry
and the Slack router, with the People and Permissions pages finished after phase 2.

## Testing

- Same setup as today: `python -m unittest discover -s tests -t .`, with fake Slack clients and no network.
- **Time rules are tested with a fixed clock:** the window edges at 3:14 and 3:15, weekends, daylight-saving
  changes, entries that cross midnight, the grace period, the nightly close.
- **The DM router has tests showing AI messages still go through mentor approval.** The words `in` and `out`
  are the only change, and messages in AI threads are never treated as clock commands.
- **Kiosk codes:** an expired code is refused, a code from another kiosk is refused, and a scan without a
  session goes through sign-in and then clocks the person in.
- **Calendar:** sample `.ics` files from Google, Outlook and Apple, with recurring events, exceptions and
  all-day events.
- **Test mode** gets sample people for all four roles, a fake kiosk and a sample calendar.

## Still to find out

- A sample Attendance Bot CSV export, for phase 6.
- The team's domain, for the `hub.` address.
- Whether Shortcuts and shared shortcuts work on a Jamf-managed iPad (test on one iPad).
- Which laptop will be the kiosk (any laptop with a browser and internet works).
