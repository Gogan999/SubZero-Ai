# CLAUDE.md

## Project facts
Shared with every session and agent. Update these when they change and tell the user.
- Stack: Python 3.12/3.13 web app in `subzero_ai/`: Starlette + Uvicorn, Jinja2 templates (`subzero_ai/templates/`), plain JS/CSS (`subzero_ai/static/`), SQLite (`subzero_ai/db.py`, append-only `MIGRATIONS` list keyed by `PRAGMA user_version`). Hand-written HTTP clients, no vendor SDKs: Claude Messages API in `subzero_ai/claude.py`, Slack Web API in `subzero_ai/slack_api.py`, Slack Socket Mode over `websockets` in `subzero_ai/socket_mode.py`. Runs as a Windows service (WinSW, embedded Python) on a mini PC behind a Cloudflare Tunnel to `http://127.0.0.1:8787`.
- Build: `python packaging/build_windows.py` makes the Windows release zip (every download pinned by SHA-256, Python wheels from `requirements-lock.txt`). GitHub Actions minutes are used up, so don't rely on CI; build and test locally.
- Test: `pip install -r requirements-dev.txt`, then `python -m unittest discover -s tests -t .` (226 tests, about 20 s). Tests use `FakeClaude` (`subzero_ai/claude.py`) and fake Slack clients from `tests/helpers.py`; no network or keys needed.
- Lint/format: none configured. Match the surrounding style (type hints, `from __future__ import annotations`, 120-column lines, short docstrings).
- Conventions: every AI call goes through one client object with `stream()` and `complete()` (built in `build_services`, `subzero_ai/web.py`). Settings are a dataclass in `subzero_ai/config.py` (from `config.env`), overridable from the admin page via `subzero_ai/saved_settings.py`. Important actions are written with `db.audit(...)`. User-facing text is plain language for high-school students. `docs/DESIGN.md` is the visual standard. `FAKE_CLAUDE=1` gives canned replies, with magic words (ALERT-TEST, ERROR-TEST, SCREEN-FLAG, SCREEN-ERROR) for testing.
- Off-limits: don't weaken the mentor approval flow (approval window, screener, MENTOR ALERT, rejections) without the user's say-so. Keep the team's name and number off the site; say "the team". Never put API keys or tokens in code, tests, commits, or chat. Users install from `SubZeroAI-windows.zip`, never GitHub's source zip.

## Delegated workflow (opt-in)
`.claude/WORKFLOW.md` describes a delegated workflow: Opus plans and decides, Haiku researches (`researcher`), Sonnet writes and reviews code (`implementer`, `reviewer`). Follow it only when the user asks for it in the session (for example "use the workflow"), or when resuming work that `.claude/work/PLAN.md` says is running under it. Otherwise work normally.
