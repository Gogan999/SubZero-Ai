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

| Feature | Where the placeholder is | What it should do | Notes for building it |
|---|---|---|---|
| Projects | Sidebar, `/projects` | Group chats about one mechanism or subsystem, with shared reference files and project instructions that go into every chat. | New `projects` table; conversations get a `project_id`. Project files reuse the attachment store. Project instructions written by mentors, or reviewed like messages. |
| Claude Code | Sidebar, `/code` | Let Claude read the team's robot code repository, run builds and tests, and propose changes as a pull request. | The biggest item. Needs a sandboxed runner, a narrowly scoped GitHub token, and mentor approval for every step that writes. Probably mentors only at first. |
| Web search | Composer tools menu | Let Claude look up datasheets, game rules and part specs. | API web search tool. Costs extra per search: add a daily cap and a Settings switch. Show the searches on the mentor's review card. |
| Extended thinking | Composer tools menu | Let Claude think longer on hard problems. | API `thinking` setting. Costs more, so maybe mentors turn it on per chat, like the model switch. |
| Research | Composer tools menu | Longer reports built from many searches. | Builds on web search. Likely mentors only because of cost. |
| Response styles | Composer tools menu | Choose how Claude answers: concise, explanatory, step by step. | One extra system-prompt line per style, chosen per message. |
| Connectors (GitHub, Drive) | Composer tools menu | Pull files from the team's GitHub or Google Drive into a message instead of attaching them. | OAuth per service. Pulled files show on the review card like attachments. |
| Share | Chat header | A read-only link to a chat for people outside the team, such as a parent. | Chats are already visible to the team. Needs a revocable link and mentor approval. |
| Profile and preferences | Account menu | Display name, default subteam, Slack DM notifications on or off. | Store on the `users` row. |
| Appearance | Account menu | Light, dark or match the device. | Known gap 2 in `docs/DESIGN.md`: `data-theme` on `<html>`, remembered per browser. |

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
- **Usage for students**: how many messages they have left today, in the account menu.
- **Mentor dashboard**: approval times, busiest subteams, most-asked topics.

## Done

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
