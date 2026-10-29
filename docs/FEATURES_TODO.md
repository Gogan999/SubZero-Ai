# Features to implement

The signed-in site is laid out like claude.ai: a sidebar with New chat, Chats,
Projects, Artifacts and Code, a Recents list and an account menu, and a rounded
composer with tools on the left and the model and Send button on the right.
Some of those controls are **placeholders**: they are on screen so the layout
matches claude.ai, but the feature behind them isn't built yet. Every
placeholder carries a dashed **Soon** pill (`.soon`, see `docs/DESIGN.md`) so
students aren't confused, and the sidebar ones open a "Coming soon" page that
says what the feature will do and what to use until then.

This file is the backlog. When a feature ships, remove its **Soon** pill and
move it to "Done" at the bottom.

Every feature here must keep the mentor approval flow: nothing a student
writes reaches Claude until a mentor approves it, and every student message
keeps its status tag.

## Placeholders already on screen

None right now: every placeholder has been built (see Done below). If a new one is added, give it a
row here with what it should do and notes for building it.

The model name in the composer is not a placeholder: students see which model
answers, and mentors get a picker there (or in the chat header when reading
someone else's chat).

## Other claude.ai features worth adding

Not on screen yet. Add a placeholder only when work on one starts.

### Chats

- **Rename, star and delete chats** from a "…" menu on each chat. Starred chats get a sidebar section above Recents. Delete should be soft, so mentors still see it in the audit log.
- **Edit a sent message and branch the chat.** Here the edited message would need approval again.
- **Regenerate a reply** and thumbs up/down feedback. Retry exists only for errors today. Feedback could tell mentors where Claude is weak.
- **Copy a whole reply** with one button, not just code blocks.
- **Stop a reply** while it streams.
- **Collapse the sidebar** on desktop to an icon rail, remembered per browser.
- **Keyboard shortcuts**: Ctrl+K to search chats, Ctrl+Shift+O for a new chat.
- **Search from the sidebar** in a dialog, as claude.ai does. Search lives on the Chats page today.

### Files and output

- **PDF attachments** (datasheets, the game manual) and CAD exports such as STEP previews.
- **Better video for Claude**: stills taken on the server (needs ffmpeg in the Windows package) for formats the browser can't play, such as iPhone HEVC in some browsers, and the audio track as a transcript.
- **Artifacts beyond explainers**: documents, diagrams and code files that open in a side panel next to the chat, with versions as Claude revises them.
- **Download a chat** as Markdown or PDF for the engineering notebook.

### Input

- **Voice dictation** into the composer. Check the privacy of the browser's speech service first.

### Team and mentor side

- **Memory**: facts about the team's robot that Claude remembers across chats, edited by mentors like the prompt layers in Settings.
- **Mentor dashboard**: approval times, busiest subteams, most-asked topics.

## Done

- Code (`/code`, mentors only): a mentor gives Claude a task in the GitHub repository from Connectors.
  Claude works in a copy on the mini PC (downloaded as a zip, so git isn't needed). It lists, reads and
  searches files on its own; every file change (shown as a diff) and every command waits for a mentor to
  approve or decline it with a note. Commands must start with a program allowed in Settings, Code
  (gradlew by default), run in the copy's folder with no shell, a time limit, and none of the app's keys or
  tokens in their environment. Nothing is pushed: the mentor downloads the changes as a patch. Sessions stop
  after 40 Claude replies per run, and every step is in the audit log. Not a full OS sandbox: an approved
  command runs as the app's Windows account, which is why each one needs a mentor.
  Possible next step: open the patch as a pull request with a token that can write.
- Connectors (composer tools menu, "Add files from"): GitHub browses the one repository an admin sets in
  Settings, Connectors (with a read-only token for a private repository, kept in config.env); Google Drive
  takes a link to a Doc, Sheet, Slides deck or file shared as "Anyone with the link" (there's no Google
  sign-in, so private files can't be added). Files are fetched when the message is sent and stored like
  attachments, so they show on the review card and count toward the 5-file limit.
- Share (`/c/<id>/share`): the chat's student or a mentor makes a read-only link and says who it's for, such as
  "my parents". A student's link waits for a mentor, who previews it and approves or declines it in Review
  (with a note in #claude-approvals); a mentor's link works at once. The link shows the chat as it was when
  shared: approved messages and finished replies, people as "Student" or "Mentor" without names, and no
  attached files. Links expire after 30 days, can be revoked any time, stop working if the chat is made
  private, aren't indexed by search engines, and every step is in the audit log.
- Composer tools menu: Web search, Research (mentors only unless an admin allows students), Extended thinking
  and Response styles (Normal, Concise, Explanatory, Step by step). They're picked per message, shown on the
  review card and in the queue, and approved with the message. Web search uses the API's server-side search
  tool with a team-wide daily limit and an on/off switch in Settings; replies list the pages Claude read.
- Appearance: Light, Dark or Auto (match the device) in the account menu, saved on the person's profile so it
  follows them to every device and draws without a flash.
- Profile and preferences (`/profile`): name and picture from Slack, the person's subteam (tags new chats,
  since the new-chat page has no picker), Slack DMs about reviews on or off, appearance, and for students
  how many messages they have left today.
- Projects (`/projects`): mentors make a project per mechanism or subsystem with a description, instructions
  for Claude and up to 20 reference files (code, text, and up to 5 PNG/JPG images). Everyone can read them and start chats in the
  project; Claude gets the instructions after the safety rules and the files before the first message. Only
  mentors change a project, every change is in the audit log, and the review card names the project. Mentors
  can archive a project; its chats keep working.
- Artifacts (`/artifacts`): a gallery of every interactive explainer from chats the viewer can see, with
  search and Team / From my chats tabs, and a page per explainer with Full screen, Download and View code.
  An explainer is exactly as visible as the reply it's in, so private chats stay private.
- Sidebar like claude.ai with New chat, Chats, Recents and an account menu.
- Chats page (`/recents`) with search, subteam filter and Team/Mine, moved from the old sidebar.
- Greeting on the new-chat page.
- Explainers without a switch: asking for one in words ("make a simulation…") gives the reply room for a whole page, and Claude may build one on its own when it clearly helps.
- Model picker in the composer for mentors.
- Video attachments (MP4, MOV, WebM) and a 50 MB per-file limit (90 MB per message, under Cloudflare's 100 MB cap). Claude can't watch video, so the browser sends it up to 8 still frames per video.
- Attachments, interactive explainers and the mentor approval flow (versions 0.2 to 0.4).
