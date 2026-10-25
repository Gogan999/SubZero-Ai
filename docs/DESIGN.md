# SubZero AI design standards

This file is the rulebook for how the SubZero AI site looks and behaves. Every
change to a template (`subzero_ai/templates/`), the stylesheet
(`subzero_ai/static/app.css`) or front-end script (`subzero_ai/static/app.js`)
must follow it. If a change needs something this file does not allow, update
this file in the same pull request and say why in the PR description.

The standards describe the site as it is today (version 0.4.0). Places where
the current code falls short are listed under [Known gaps](#known-gaps) so they
can be fixed in small, separate PRs. New work must meet the standard even where
old code does not yet.

## 1. Principles

1. **Feels like claude.ai.** Students should already know how to use it. Copy
   claude.ai's layout and interaction patterns (sidebar of chats, centered
   thread, sticky composer at the bottom, Markdown replies with code blocks)
   unless a safety feature needs something different.
2. **Status is never hidden.** Every student message shows where it stands
   (waiting, approved, edited, rejected, held). Use the inspection tag (see
   [Tags](#tags-message-status)) for this, never color alone.
3. **Calm, not cute.** The look is "drafting ink on cool drafting film": quiet
   blues and grays, one brand color, color reserved for meaning. No gradients,
   drop shadows, emoji decoration or animation for its own sake.
4. **No team name or number on screen.** Pages never show the team's name or
   FRC number (the `TEAM_NAME` and `TEAM_NUMBER` settings). Say "the team" or
   "your team" instead. Those settings may still go into Claude's instructions,
   which students don't see. The app's own name (`APP_NAME`) is fine to show.
5. **Works on a shop laptop and a phone.** Every page must work from 320px wide
   up to a desktop monitor, in light and dark mode, with a keyboard alone.
6. **No build step.** Plain CSS and plain JavaScript served from `static/`. No
   frameworks, preprocessors, CSS-in-JS, web fonts or CDN assets. The site runs
   on a mini PC behind a tunnel and must load fast and offline-from-the-internet.
   This rule covers the site's own pages only. Explainer pages that Claude
   writes may load libraries from the CDNs allowed in `EXPLAINER_CDNS`
   (`render.py`) inside their sandbox; that is deliberate.

## 2. Color

All colors are CSS custom properties on `:root` in `app.css`. **Never write a
raw color value outside the token blocks.** If you need a new color, add a
token to both the light block and the dark block.

### Surface and text tokens

| Token | Use | Light | Dark |
|---|---|---|---|
| `--paper` | Page background, input fill | `#eef3f6` | `#0e1922` |
| `--sheet` | Cards, sidebar, top bar, composer box | `#ffffff` | `#15232f` |
| `--frost` | Hover, selected item, mentor message, chips | `#e2ecf2` | `#1c2d3b` |
| `--muted-bg` | Code bars, inline code, quiet panels, table heads | `#e9eef1` | `#1b2a36` |
| `--rule` | Dividers and borders | `#d2dde5` | `#2a3b49` |
| `--ink` | Body text | `#142231` | `#e4ecf2` |
| `--ink-2` | Secondary text: meta, hints, timestamps | `#4a5b6b` | `#a3b3c1` |
| `--brand` | Links, primary buttons, Claude's name, focus accents | `#1a5a86` | `#5aa3d8` |
| `--brand-hover` | Hover for brand items | `#134a70` | `#79b6e3` |
| `--brand-ink` | Text on a `--brand` fill | `#ffffff` | `#071520` |
| `--focus` | Keyboard focus ring | `#0b7fd1` | `#7cc4ff` |
| `--field-edge` | Border of text fields, selects and textareas (3:1 on `--paper` and `--sheet`) | `#7a8a98` | `#62788a` |

### Status tokens

Each status has a text color and a background, always used as a pair.

| Status | Text / background tokens | Meaning |
|---|---|---|
| Pending | `--pending` / `--pending-bg` (+ `--pending-edge` for the queue card stripe) | Waiting for a mentor |
| Approved | `--approved` / `--approved-bg` | Approved, or approved with edits |
| Rejected | `--rejected` / `--rejected-bg` | Rejected, errors, destructive actions |
| Held | `--held` / `--held-bg` | Held by safety checks, private chats |

Rules:

- Status colors mean status. Do not use green, amber, red or violet for
  decoration or for unrelated meaning.
- Red (`--rejected`) is also the color for errors and destructive buttons.
- On the admin Settings and setup pages, the approved pair also means "working" or
  "done" and the rejected pair "not working" or "failed" (`.pill.ok`, `.pill.bad`),
  always with those words.
- Every use of a status color must also carry a word ("Waiting for a mentor",
  "Not approved by Sam"). Color is never the only signal.
- Text must meet WCAG AA contrast: 4.5:1 for normal text, 3:1 for large text
  (24px, or 19px bold) and for icons, borders that identify a control, and the
  focus ring. All current text pairs pass in both themes (lowest is
  `--brand` on `--frost` in dark mode at 5.2:1). Check any new pair before
  merging.
- The one exception to "no raw colors" is the explainer iframe, which keeps a
  white background because the page inside it is written for light mode.

## 3. Typography

- **Fonts:** `--font` (system UI stack, Segoe UI on Windows) for everything,
  `--mono` (Cascadia / SF Mono / Consolas) for code, file paths and code-block
  labels. No web fonts.
- **Base:** 16px, line height 1.55. Claude's Markdown replies (`.prose`) use
  1.65 for easier reading.
- **Scale.** Use only these sizes:

  | Role | Size | Weight |
  |---|---|---|
  | Sign-in title, start-page title | 2.2rem / 2rem (1.6rem on phones) | 700, letter-spacing −0.02em |
  | Page title (`h1`) | 1.6rem | 700, letter-spacing −0.01em |
  | Chat title | 1.35rem | 700 |
  | Section title (`h2`), Markdown headings | 1.15rem / 1.1rem | 700 |
  | Body | 1rem | 400 |
  | Secondary (meta, buttons, form labels) | .88–.95rem | 400–600 |
  | Fine print (hints, chips, notes) | .8–.85rem | 400–650 |
  | Smallest (pills, role badge) | .72–.75rem | 650 |

  Never go below .72rem, and never put sentences (more than a few words)
  below .8rem.
- **Weights:** 400 body, 550–600 for emphasis and buttons, 650 for labels,
  names and tags, 700–800 only for headings and the brand mark (the small brand-filled box in
  the top bar; it holds a generic mark such as the app's initials, never the
  team number).
- **Line length:** keep reading text to about 62–75 characters. The thread
  column is capped at 780px for this reason; `.lede` caps at 62ch.
- Numbers that line up or update (times, counts) use
  `font-variant-numeric: tabular-nums`.
- Long words, URLs and code must not widen the page: use
  `overflow-wrap: anywhere` on user and Claude text, and let code blocks and
  tables scroll inside themselves.

## 4. Spacing, shape and layout

- **Spacing** uses `rem` steps from this set: .25, .35, .5, .65, .75, 1, 1.25,
  1.5, 2, 3. Prefer `gap` on flex and grid containers over margins between
  siblings.
- **Corner radius:**
  - 4px for tiny badges, inline code and small text buttons
  - 6px for nav items, tabs and small panels
  - 8px for buttons, inputs and code/explainer blocks
  - `--radius` (10px) for cards: messages, queue items, the composer box, tables
  - 14px for the sign-in panel only
  - 999px for pills and chips
- **Borders** are 1px `--rule`. Use a heavier stripe (3–4px) only to mark state:
  the selected chat (inset brand stripe) and pending queue cards
  (`--pending-edge`).
- **Shadows:** none, except the 3px `--frost` glow when dragging files onto the
  composer. Use a border or a `--frost` fill to separate things instead.
- **Page widths:**
  - Chat thread and composer: max 780px, centered
  - Text pages (about, sign-in help): `.page.narrow`, max 760px
  - Wide pages (queue, audit log): `.page`, max 1100px
  - Sidebar: 300px on desktop
  - Top bar: `--topbar` (56px) tall, sticky
- Side padding is at least 1rem on phones and 1.25rem on wider screens. There
  must never be sideways page scrolling.

## 5. Components

Reuse these before making anything new. A new component needs a short entry
here in the same PR.

### Buttons

`.button` is the only button style. Variants combine with it:

| Class | Use |
|---|---|
| `.button.primary` | The one main action on a screen (New chat, Send, Approve on the queue). At most one per view area. |
| `.button` (default) | Ordinary actions |
| `.button.quiet` | Low-priority actions next to content (Withdraw) |
| `.button.approve` / `.button.reject` | Mentor review actions only |
| `.button.small` | Inside messages and dense rows |
| `.button.block` | Full-width, in the sidebar or narrow panels |
| `button.link` | A button that should read as a link (Sign out) |

Rules: label buttons with a verb ("Approve", "Try again"), never "OK" or
"Submit". Destructive or hard-to-undo actions use `data-confirm` with a plain
sentence explaining what will happen. Disabled buttons use the `disabled`
attribute, not just a class.

### Form fields

Text inputs, selects and textareas use `font: inherit`, 1px `--rule` border,
8px radius (10px for the composer), `--paper` or `--sheet` fill and `--ink`
text. Every field has a visible `<label>` or, when the design has no room (the
sidebar search), a `.visually-hidden` label. Placeholder text is never the only
label.

### Messages

- **Student or mentor message** (`.msg.person`): a `--sheet` card with a border.
  Mentor messages drop the border and use a `--frost` fill, plus the "Mentor"
  role badge.
- **Claude's reply** (`.msg.claude`): no card, text sits on the page, like
  claude.ai. "Claude" is shown in `--brand`.
- The header row holds avatar (24px circle, or initial on `--frost`), name,
  role badge and time, in that order, time pushed right.
- Errors from Claude are a `--rejected-bg` panel with `role="alert"` and a
  "Try again" button when possible.

### Tags (message status)

The inspection tag (`.tag`) is the site's signature element: a shop tag with a
punched eyelet showing a student message's status. Rules:

- One tag per student message, below the body.
- The tag text is a full phrase ("Waiting for a mentor", "Approved by Sam", "Not approved by Sam").
- Use the matching status class: `.pending`, `.approved`, `.edited`,
  `.rejected`, `.held`. A tag with no status class (withdrawn) uses the
  neutral `--muted-bg` look.
- Do not use the tag shape for anything except message status. For small
  labels elsewhere use a `.pill` (sidebar counts, Private) or `.role` badge.

### Code blocks and Markdown

- Claude's Markdown renders inside `.prose`. Code blocks are `.code` with a
  `.code-bar` showing the language and a Copy button. Syntax colors come from
  Pygments via `/assets/code.css`; they must work in both themes.
- Tables in replies scroll sideways inside themselves.

### Explainers

Interactive explainers sit in an `.explainer` frame with a bar holding the
label and text-style actions (Full screen, Download, View code). The iframe is
sandboxed; never relax the sandbox for styling reasons.

### Composer

- Sticky at the bottom of the thread, fading the page behind it with a
  `--paper` gradient.
- Order: message box, tools row (Attach files, Interactive explainer, hint),
  chosen files as removable chips, then the AI reminder note and the Send
  button on the same row.
- The start page uses `.composer.big`: a taller box, not sticky.

### Notices and banners

- The AI notice under the top bar must appear on every page. Don't remove or
  shrink it below .85rem.
- The test-mode bar (`.devbar`) uses pending colors so it can't be missed.
- Inline warnings (long chat) use the pending pair; private-chat notes use the
  held pair.

### Settings and setup forms

Admin pages built from forms (Settings, setup, recovery sign-in) use these:

- `.panel`: a `--sheet` card (`--radius`, 1px `--rule`) for each section, with
  an `h2` and an `id` so the `.settings-nav` links at the top can jump to it.
- `.field`: label, control, `.field-hint` and `.field-error`, stacked. Controls
  use the `--field-edge` border. `.field.check` puts a checkbox beside its label.
- `.form-actions`: the row of buttons under a form. Each panel's form has one
  `.button.primary` Save button.
- `.flash` (`role="status"`) says what a save did, after the page reloads.
- `.health`: a list of label, status pill (`.pill.ok`, `.pill.bad`,
  `.pill.unknown`, `.pill.plain`) and detail. On phones the detail wraps under the
  label.
- `.recovery-code` shows a one-time code in large monospace type.
- Secrets (API keys, tokens) use `type="password"` fields that start empty and
  are never filled back in. A saved one shows "Saved. Leave blank to keep it."

### Empty states

Every list has an empty state that says what's missing and what to do next
("No chats yet. Start one with New chat.").

## 6. Writing on screen

- Plain, friendly sentences for high school students. Say what happened and
  what to do next.
- Sentence case for everything: buttons, headings, tabs ("Audit log", not
  "Audit Log").
- Say "Claude" for the AI and "mentor" for the reviewer. Don't call Claude
  "the bot" or "the assistant".
- Error messages say what went wrong in plain words and how to fix it. No
  error codes or stack traces on screen.
- Refer to the team generally ("the team", "your team", "team members"),
  never by name or number. See principle 4.

## 7. Accessibility

Target: WCAG 2.2 level AA.

- **Keyboard:** everything works with Tab, Shift+Tab, Enter and Space. Focus
  order follows the visual order. The skip link ("Skip to content") stays first
  on every page.
- **Focus:** use the global `:focus-visible` ring (3px `--focus`, 2px offset).
  Never remove an outline without replacing it with something as visible.
  Hidden inputs styled as buttons (Attach files) show the ring on the wrapper
  with `:focus-within`.
- **Semantics:** use real elements first (`button`, `a`, `nav`, `main`,
  `label`, lists for lists). Use ARIA only to fill gaps: `aria-current="page"`
  for the current nav item and chat, `role="alert"` for errors,
  `aria-label` on counts and icon-only buttons.
- **Touch targets:** at least 24×24px everywhere (WCAG 2.5.8), and aim for
  44×44px for primary actions on phones.
- **Images:** decorative images and avatars next to a name get `alt=""`.
  Student screenshots keep their file name visible as text.
- **Motion:** no animation is required to understand anything. Respect
  `prefers-reduced-motion` (already handled globally).
- **Zoom:** pages must work at 200% zoom and with the browser text size
  increased, so use `rem` for type and spacing, not `px` (borders, radii and
  avatar sizes are the exceptions).
- **Live updates:** streamed replies and queue changes must not move keyboard
  focus. Announce important changes politely (`aria-live="polite"`), never
  with `assertive` except for errors.

## 8. Dark mode

- The site follows the device setting via `prefers-color-scheme`, and
  `<meta name="color-scheme" content="light dark">` keeps form controls and
  scrollbars matched.
- Dark mode is a set of token overrides in one `@media
  (prefers-color-scheme: dark)` block. Components must not have their own dark
  rules; if something looks wrong in dark mode, fix or add a token.
- Dark surfaces are deep blue-gray, never pure black. Dark text is off-white,
  never pure white. Brand and status colors get lighter in dark mode so they
  keep their contrast.
- Every change is checked in both themes before merging (see the checklist).

## 9. Responsive rules

- **Breakpoints:** 900px and 520px, both `max-width`. Don't add new ones unless
  a layout truly breaks; reuse these.
- **Above 900px:** sidebar and thread side by side.
- **900px and below:** sidebar hides behind an "All chats" toggle and opens as a
  full-screen panel under the top bar; user name and app name hide from the
  top bar (the brand mark stays). The nav links scroll sideways inside the
  top bar if they don't fit (admins have four), so the page itself never does.
- **520px and below:** panels like sign-in lose their outer margin.
- Grid and flex children that hold text use `minmax(0, 1fr)` or `min-width: 0`
  so long content can't push the layout wider than the screen.
- Test at 320px, 390px (a typical phone), 768px (tablet) and 1280px+.

## 10. Code conventions for styles

- All CSS lives in `subzero_ai/static/app.css`, grouped by the existing section
  comments (Top bar, Buttons, Layout, Messages, ...). Add new rules to the
  matching section.
- Class names are short, lowercase, hyphenated and describe the thing, not its
  look (`.queue-item`, not `.yellow-card`). Variants are a second class
  (`.button.primary`, `.tag.pending`).
- No inline `style=""` attributes in templates and no styles set from
  JavaScript except toggling classes, `hidden` and sizes the script must
  measure (such as growing the composer).
- No `!important`, except the existing `[hidden]` and reduced-motion rules.
- Bump nothing by hand for caching: templates already add `?v={{ version }}`
  to the stylesheet.
- A short comment above any rule whose reason isn't obvious (see the
  `minmax(0, 1fr)` comment on `.thread`).

## 11. Review checklist

Copy this into the PR description of any change that touches how the site
looks, and tick each item:

- [ ] Uses existing tokens and components; no raw colors or one-off styles
- [ ] Checked in light and dark mode
- [ ] Checked at 320px and desktop width, with no sideways scrolling
- [ ] Works with the keyboard alone, with a visible focus ring
- [ ] New text and control colors meet WCAG AA contrast
- [ ] Status is shown with words, not only color
- [ ] No team name or number shown anywhere on screen
- [ ] Screenshots of before and after attached (light and dark)
- [ ] This file updated if a new token, component or rule was added

## Known gaps

These are places where the current code doesn't yet meet the standards above.
Each should be fixed in its own small PR.

1. **Field borders are too faint.** The sidebar search, subteam selects,
   review textareas and the composer use `--rule`, which is about 1.4:1
   against the background in both themes. WCAG 1.4.11 asks for 3:1 for the
   border that shows where a field is. The `--field-edge` token now exists and
   the Settings and setup forms use it; switch these older fields to it too and
   keep `--rule` for dividers.
2. **No manual theme switch.** claude.ai lets people pick light, dark or
   system. Add a `data-theme` attribute on `<html>` with the dark tokens also
   applied under `:root[data-theme="dark"]`, and a small switch in the top bar,
   remembered per browser.
3. **Spacing and type sizes aren't tokens.** Sizes are written as literal
   `rem` values. Once the scale in sections 3 and 4 settles, turn them into
   `--space-*` and `--text-*` tokens.
4. **Small touch targets.** The × that removes a chosen file is about
   18×20px and fails the 24px minimum today; fix it first. The code Copy
   button, explainer actions and `.button.small` pass 24px but are under 44px
   on phones; enlarge them in the 900px breakpoint.
5. **`100vh` on phones.** The layout and sidebar heights use `100vh`, which on
   mobile browsers includes the hidden address bar area. Use `100dvh` with a
   `100vh` fallback.
6. **Tabs aren't full tabs.** The Team/Mine switch uses `role="tablist"` but
   they are links to new pages and don't support arrow keys. Either drop the
   tab roles and use `aria-current`, or add arrow-key handling.
7. **No icons.** Actions are text-only ("+ Attach files"). That's fine, but if
   icons are added they must be inline SVG using `currentColor`, 16–20px, with a
   text label or `aria-label`, and documented here first.
8. **Streaming replies aren't announced.** Screen-reader users get no signal
   that Claude has finished replying. Add a polite live region that says
   "Claude replied" when streaming ends.
9. **Explainer iframe is always white.** In dark mode a white explainer is
   bright. Acceptable for now; revisit if explainers learn to read the theme.
10. **Team name and number are on screen.** `base.html` puts
    `settings.team_number` in the top-bar brand mark, `login.html` shows
    "FRC Team {number} {name}" above the title, and `about.html` says
    "Team {number} students". Replace them with the app's initials in the
    brand mark, drop the sign-in line, and say "students on the team". Also
    reword the "team number set like a robot bumper number" comment in
    `app.css`.
