# Adding a tool to SubZero Hub

Each tool is a **module**: a folder in `subzero_ai/modules/` with one `Module` subclass, listed in
`subzero_ai/modules/__init__.py`. The core (`subzero_ai/hub/`) handles sign-in, roles, permissions, Slack routing,
the home page and the tool switcher. Look at `modules/calendar/` for a small example and `modules/timeclock/` for
one that uses every hook.

## The Module class

```python
class NotesModule(Module):
    key = "notes"                      # URL prefix (/notes), permission prefix, table prefix (notes_)
    name = "Notes"
    permissions = (Permission("notes.view", "Read notes", frozenset({"admin", "mentor", "lead", "student"})),)
    migrations = ("CREATE TABLE notes_items (...);",)   # append only; each runs once
```

Every method is optional:

| Method | Use it for |
| --- | --- |
| `routes()` | Starlette routes, all under `/<key>`. Import them inside the method. |
| `nav(viewer)` | `NavItem`s for the tool switcher and the home page tiles. |
| `home_card(services, viewer)` | A card on the Hub home page: `{"template": "notes/_home_card.html", ...}`. |
| `slack_home_blocks(services, viewer)` | The module's section of the Slack Home tab. |
| `on_dm(services, event, text)` | Top-level DMs to the bot. Return True to keep a message from the AI. Claim it first with `services.store.claim_dm_event`. |
| `dm_help(services, viewer)` | Lines for the bot's `help` reply, one per DM command the module answers. |
| `on_action(services, payload, action_id)` | Slack buttons whose `action_id` starts with `<key>:`. |
| `commands` / `on_command(services, payload)` | Slash commands. Add each to `slack-manifest.yaml` and `setup_flow.slack_manifest`. |
| `tick(services)` | Runs every minute. Put slow work in `asyncio.to_thread`. |

## Rules

- **Permissions:** check them with `@page(require="notes.view")` or `viewer.can(...)`. Coaches always pass.
  Use `never_roles` for anything students must never get.
- **Data:** tables start with the module key. Write migrations as new list entries, never by editing old ones.
  Backups already include the whole database.
- **State:** keep one service object per app on `services` (see `clock_for` in `modules/timeclock`).
- **Pages:** templates go in `subzero_ai/templates/<key>/`, extend `base.html`, and start with
  `{% block notice %}{% endblock %}` to leave out the AI notice. Use the classes in `app.css` (`page`, `panel`,
  `subnav`, `data`, `who-in`) and only its color tokens. Pages must work on a phone.
- **Session values** that must survive Slack sign-in are named `carry_*`.
- **Comments** say what code does, never why.
- **Tests** go in `tests/test_<key>.py`, using `tests/helpers.py`'s `Harness`.
