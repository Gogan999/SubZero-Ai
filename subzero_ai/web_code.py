"""Code pages: start a session, watch Claude work, read what it did, and download the changes as a patch. Mentors see
every session and answer what waits for them ("Needs a mentor"). Students (when code_student_access is on) ask for a
session and see only their own: a mentor approves the request first, and the patch can be read and downloaded once a
mentor releases it. The work itself is in code_runner.py (and code_engine.py for the new engine)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from .code_runner import FINISHED, NO_CODE_MODEL, CodeError
from .config import normalize_model_id
from .diff_view import edit_lines, parse_changes
from .model_choices import model_label
from .pricing import cost_usd, usage_line
from .render import markdown_html
from .store import Viewer
from .web_common import Services, error_page, fmt_time, page, render, services, waited

STATUS_LABELS = {"awaiting_approval": ("Waiting for a mentor to approve", "pending"),
                 "preparing": ("Starting", "pending"), "running": ("Working", "pending"),
                 "waiting": ("Waiting for a mentor", "pending"), "done": ("Done", "ok"),
                 "stopped": ("Stopped", "unknown"), "failed": ("Failed", "bad")}
SHORT_LABELS = {"awaiting_approval": "Waiting", "preparing": "Starting", "running": "Working", "waiting": "Waiting",
                "done": "Done", "stopped": "Stopped", "failed": "Failed"}  # for the narrow sidebar
LIVE = ("preparing", "running")
STUDENT_SHOWN_CHARS = 6000  # a student sees at most this much of each step's output; the rest is cut off
SIDEBAR_SESSIONS = 25  # sessions listed in the sidebar
SHOWN_LINES, SHOWN_CHARS = 12, 1500  # an output longer than this is cut with "Show more"
NEEDS_LABELS = {"request": "Student request", "follow_up": "Student follow-up", "plan": "Plan to approve",
                "step": "Step to approve"}
FRIENDLY_FAILED = "Claude couldn't finish this task."
STUDENTS_WAIT = "A mentor has to turn Code on for students."
SLACK_HINT = "To chat with Claude, message the bot in Slack."
QUEUE_NOTE = "Waiting for a free sandbox"  # the start of the note the engine writes while a session waits its turn


def _arg(args: dict, key: str) -> str:
    return str(args.get(key, "") or "")


def _col(row, key: str, default: Any = None) -> Any:
    """A column of a session row, or `default` when the row doesn't have it (older rows, small test rows)."""
    return row[key] if key in row.keys() else default


def usd(amount: float) -> str:
    return "under $0.01" if 0 < amount < 0.005 else f"${amount:,.2f}"


def short_output(text: str) -> tuple[str, str]:
    """A long output as the part shown at once and the rest (for "Show more"). The rest is empty for a short one."""
    lines = text.splitlines(keepends=True)
    if len(lines) <= SHOWN_LINES and len(text) <= SHOWN_CHARS:
        return text, ""
    head = "".join(lines[:SHOWN_LINES])[:SHOWN_CHARS]
    return head, text[len(head):]


def _json_text(data: Any) -> str:
    try:
        return json.dumps(data, indent=2, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(data)


def step_view(s: Services, row, *, student_session: bool = False, shown: int | None = None,
              mentor: bool = True) -> dict[str, Any]:
    try:
        args = json.loads(row["input"] or "{}")
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        args = {"value": args}
    output = row["output"] or ""
    if shown is not None and len(output) > shown:
        output = output[:shown] + "\n(The rest is cut off here.)"
    view = {"id": row["id"], "kind": row["kind"], "tool": row["tool"], "status": row["status"],
            "time": fmt_time(s.store, row["created_at"]), "reviewer": row["reviewer_name"],
            "note": row["review_note"], "reason": _arg(args, "reason"), "output": output,
            "html": None, "label": "", "detail": "", "diff": None, "command": None, "runs_changes": [],
            "plan": None, "plan_html": None, "tasks": None, "parent": "", "tool_use_id": row["tool_use_id"] or "",
            "shape": "classic", "children": [], "lines": None, "content": None, "head": "", "rest": "",
            "person": False, "mentor_only": False}
    view["head"], view["rest"] = short_output(output)
    if row["kind"] == "text":
        view["html"] = markdown_html(output)
        return view
    if row["kind"] == "note":
        view["person"] = bool(row["reviewer_name"]) or row["status"] in ("pending", "declined")
        if row["tool"] == "mentor":  # what went wrong, for mentors only (the page leaves it out for students)
            view["mentor_only"] = True
        if row["status"] == "pending":
            view["label"] = "Asked for more, waiting for a mentor"
            if not mentor:
                view["note"] = ""  # the screener's note is for mentors
        elif row["status"] == "declined":
            view["label"] = "Not sent to Claude"
        elif student_session:
            view["label"] = "Asked for more" if row["reviewer_name"] else ""
        else:
            view["label"] = f"{row['reviewer_name']} asked" if row["reviewer_name"] else ""
        return view
    tool, path = row["tool"], _arg(args, "path") or "the repository root"
    done = row["status"] in ("done", "error", "running")
    if tool == "list_files":
        view["label"] = f"Listed {path}"
    elif tool == "read_file":
        view["label"] = f"Read {path}"
    elif tool == "search":
        view["label"] = f"Searched for {_arg(args, 'pattern')}"
    elif tool in ("write_file", "edit_file"):
        verb = ("Wrote" if tool == "write_file" else "Edited") if done else (
            "Wants to write" if tool == "write_file" else "Wants to edit")
        view["label"] = f"{verb} {path}"
        if row["status"] == "pending" or (row["status"] in ("approved", "declined") and row["output"]):
            view["diff"] = row["output"]
    elif tool == "run_command":
        view["label"] = "Ran a command" if done else "Wants to run a command"
        view["command"] = _arg(args, "command")
    else:
        _engine_view(view, tool, args, done)
    return view


def _engine_view(view: dict[str, Any], tool: str, args: dict, done: bool) -> None:
    """A step from the new Code engine (its tool names are the Agent SDK's). A tool this page doesn't know is shown by
    its name with what it was given, so nothing the engine does can break the page."""
    path = _arg(args, "file_path") or _arg(args, "path") or _arg(args, "notebook_path")
    view["parent"] = _arg(args, "_parent")
    view["shape"] = "line"
    if tool == "plan":
        view["shape"], view["label"] = "plan", "Plan"
        view["plan"] = _arg(args, "plan")
        view["plan_html"] = markdown_html(view["plan"])
    elif tool == "todo":
        items = [i for i in args.get("items", []) if isinstance(i, dict)]
        view["shape"], view["label"] = "todo", "Task list"
        view["tasks"] = [{"text": str(i.get("content") or i.get("activeForm") or ""),
                          "state": str(i.get("status") or "pending")} for i in items]
    elif tool == "Bash":
        view["shape"] = "shell"
        view["label"] = "Ran a command" if done else "Running a command"
        view["command"] = _arg(args, "command")
        view["detail"] = _arg(args, "description")
    elif tool in ("Read", "NotebookRead"):
        view["label"] = f"Read {path or 'a file'}"
    elif tool in ("Write", "NotebookEdit"):
        view["shape"] = "write"
        view["label"] = f"Wrote {path or 'a file'}"
        content = _arg(args, "content") or _arg(args, "new_source")
        if content:
            view["content"] = content
            count = len(content.splitlines())
            view["detail"] = f"{count:,} line{'s' if count != 1 else ''}"
            view["head"], view["rest"] = short_output(content)
    elif tool in ("Edit", "MultiEdit"):
        view["shape"] = "edit"
        view["label"] = f"Edited {path or 'a file'}"
        edits = [e for e in args.get("edits", []) if isinstance(e, dict)] if tool == "MultiEdit" else [args]
        lines: list[tuple[str, str]] = []
        for edit in edits:
            lines += edit_lines(_arg(edit, "old_string"), _arg(edit, "new_string"))
        view["lines"] = lines[:400] or None
    elif tool in ("Glob", "Grep", "LS"):
        view["label"] = f"Searched for {_arg(args, 'pattern') or _arg(args, 'path') or 'files'}"
    elif tool in ("Agent", "Task"):
        view["shape"] = "agent"
        view["label"] = f"Helper: {_arg(args, 'description') or 'a part of the task'}"
    elif tool == "WebSearch":
        view["label"] = f"Searched the web for {_arg(args, 'query')}"
    elif tool == "WebFetch":
        view["label"] = f"Read a web page: {_arg(args, 'url')}"
    else:
        view["shape"] = "generic"
        view["label"] = f"Used {tool}"
        view["detail"] = _json_text({k: v for k, v in args.items() if k != "_parent"})


def nest_steps(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The steps in order, with each helper's own steps (they point at it with `_parent`) moved under it. A step whose
    helper isn't in the list stays where it is."""
    seen: dict[str, dict[str, Any]] = {}
    top = []
    for step in steps:
        parent = seen.get(step["parent"]) if step["parent"] else None
        if parent is not None:
            parent["children"].append(step)
        else:
            top.append(step)
        if step["tool_use_id"]:
            seen[step["tool_use_id"]] = step
    return top


def session_view(s: Services, row) -> dict[str, Any]:
    label, kind = STATUS_LABELS[row["status"]]
    engine = _col(row, "engine", "classic")
    tokens = row["input_tokens"] + row["output_tokens"]
    usage = usage_line(tokens, cost_usd(row["model"], input_tokens=row["input_tokens"],
                                        output_tokens=row["output_tokens"], one_request=False,
                                        model_info=s.model_info))
    return {"id": row["id"], "title": row["title"], "task": row["task"], "status": row["status"], "label": label,
            "short_label": SHORT_LABELS[row["status"]], "kind": kind, "owner": row["owner_name"], "repo": row["repo"],
            "model": model_label(row["model"], s.model_names),
            "ref": row["ref"], "error": row["error"], "updated": fmt_time(s.store, row["updated_at"]),
            "created": fmt_time(s.store, row["created_at"]), "live": row["status"] in LIVE,
            "final": row["status"] in FINISHED, "tokens": tokens, "usage": usage,
            "cost": _cost_text(s, row, engine, usage),
            "engine": engine, "engine_label": "New engine" if engine == "agent" else "Classic",
            "plan_first": bool(_col(row, "plan_first", 0)),
            "pending": row["pending"] if "pending" in row.keys() else 0,
            "student": bool(row["owner_is_student"]), "awaiting": row["status"] == "awaiting_approval",
            "screen_note": row["screen_note"]}


def _cost_text(s: Services, row, engine: str, usage: str) -> str:
    """What the session has cost so far, in words: '$1.20 of $5.00', or 'about $1.20 at API prices' on the team's Claude
    subscription. A classic session shows its tokens and an estimate."""
    if engine != "agent":
        return usage if row["input_tokens"] + row["output_tokens"] else ""
    cost, cap = float(_col(row, "cost_usd", 0) or 0), _col(row, "cap_usd")
    if str(s.settings.code_auth).strip().lower() == "subscription":
        text = f"about {usd(cost)} at API prices"
        return f"{text}, cap {usd(float(cap))}" if cap else text
    return f"{usd(cost)} of {usd(float(cap))}" if cap else f"{usd(cost)} so far"


def hours_left(iso: str) -> str:
    """'N hours' (or 'less than an hour') until a sandbox is deleted."""
    left = datetime.fromisoformat(iso) - datetime.now(timezone.utc)
    if left.total_seconds() <= 0:
        return "a moment"
    hours = int(left.total_seconds() // 3600) + (1 if left.total_seconds() % 3600 else 0)
    if hours == 1:
        return "1 hour"
    return "less than an hour" if hours < 1 else f"{hours} hours"


def visible(viewer: Viewer, row) -> bool:
    """Mentors see every session. A student sees only the sessions they asked for."""
    return row is not None and (viewer.is_mentor or (row["created_by"] == viewer.id and bool(row["owner_is_student"])))


def gate(request: Request, viewer: Viewer) -> Response | None:
    """Students get Code only while a mentor or admin has turned on code_student_access. Mentors are never blocked here.
    (The Code home page itself explains it to students instead of refusing them.)"""
    if viewer.is_mentor or services(request).settings.code_student_access:
        return None
    if request.method != "GET":
        return JSONResponse({"ok": False, "error": "Code isn't open to students yet."}, status_code=403)
    return error_page(request, viewer, 403, "Code isn't open yet", STUDENTS_WAIT)


def screen_note(verdict) -> str | None:
    """What the screener said about a student's words, for the mentor. None when it passed them."""
    if verdict is None or verdict.passed:
        return None
    return f"Screener: {verdict.note}"


def code_models(s: Services) -> list[tuple[str, str]]:
    """The default and hard models that can run Code sessions (Claude models only), with their labels."""
    return [(m, model_label(m, s.model_names)) for m in dict.fromkeys(
        normalize_model_id(m) for m in (s.settings.model_default, s.settings.model_hard)) if s.capabilities(m).code]


def sidebar_sessions(s: Services, viewer: Viewer) -> list[dict[str, Any]]:
    """The sessions the sidebar lists: a mentor's every session, a student's own. Newest activity first."""
    if not (viewer.is_mentor or s.settings.code_student_access):
        return []
    rows = s.code.list(limit=SIDEBAR_SESSIONS, owner_id=None if viewer.is_mentor else viewer.id)
    return [{"id": r["id"], "title": r["title"], "kind": STATUS_LABELS[r["status"]][1],
             "label": SHORT_LABELS[r["status"]], "full_label": STATUS_LABELS[r["status"]][0]}
            for r in rows if visible(viewer, r)]


def needs_views(s: Services) -> list[dict[str, Any]]:
    """What waits for a mentor, for the "Needs a mentor" list."""
    out = []
    for item in s.code.needs_mentor():
        kind = NEEDS_LABELS[item["kind"]]
        if item["kind"] == "step" and item["count"] > 1:
            kind = f"{item['count']} steps to approve"
        out.append({"id": item["id"], "title": item["title"], "who": item["owner"], "kind": kind,
                    "waited": waited(item["since"]), "flagged": bool(item["screen_note"])})
    return out


# ---------------------------------------------------------------- the Code home
def _home_state(s: Services, viewer: Viewer) -> dict[str, Any]:
    """Whether the start box can be used, and if not, why. `state` is one of students_off, no_repo, not_ready, no_model
    and ready; `problem` is the detail only admins are shown."""
    mentor = viewer.is_mentor
    if not mentor and not s.settings.code_student_access:
        return {"state": "students_off", "problem": ""}
    if not s.code.available:
        return {"state": "no_repo", "problem": ""}
    problem = s.code.engine_problem() if mentor else (s.code.student_problem() or s.code.engine_problem())
    if problem:
        return {"state": "not_ready", "problem": problem}
    if not code_models(s):
        return {"state": "no_model", "problem": NO_CODE_MODEL}
    return {"state": "ready", "problem": "", "limit": s.code.limit_problem(viewer)}


@page(require="login")
async def code_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    mentor = viewer.is_mentor
    sessions = [session_view(s, r) for r in s.code.list(owner_id=None if mentor else viewer.id) if visible(viewer, r)]
    if not mentor and not s.settings.code_student_access:
        sessions = []
    return render(request, "code.html", viewer, sessions=sessions, mentor=mentor, home=_home_state(s, viewer),
                  needs=needs_views(s) if mentor else [], models=code_models(s),
                  agent=s.code.engine_now() == "agent", plan_first=s.code.plan_first_default(not mentor),
                  repo=s.settings.connector_github_repo, branch=s.settings.connector_github_branch,
                  error=request.session.pop("code_error", None), flash=request.session.pop("flash", None))


@page(require="mentor")
async def code_needs(request: Request, viewer: Viewer) -> Response:
    """The "Needs a mentor" list on its own, redrawn every little while on the Code home page."""
    return render(request, "_code_needs.html", viewer, needs=needs_views(services(request)))


@page(require="login")
async def code_start(request: Request, viewer: Viewer) -> Response:
    denied = gate(request, viewer)
    if denied is not None:
        return denied
    s = services(request)
    form = await request.form()
    allowed = [m for m, _ in code_models(s)]
    if not allowed:
        request.session["code_error"] = NO_CODE_MODEL
        return RedirectResponse("/code", status_code=303)
    # The start box sends 0 first and 1 again when "Plan first" is ticked. A form without the field gets the default.
    plan_first = ("1" in form.getlist("plan_first")) if "plan_first" in form else None
    try:
        if viewer.is_mentor:
            model = normalize_model_id(form.get("model", ""))
            if model not in allowed:
                model = allowed[0]
            session_id = s.code.start(viewer, str(form.get("task", "")), model, plan_first=plan_first)
        else:
            problem = s.code.student_problem()
            if problem:
                raise CodeError(problem)
            task = str(form.get("task", ""))
            verdict = await s.screener.check(task) if s.settings.screener_enabled else None
            session_id = s.code.request(viewer, task, allowed[0], screen_note(verdict),  # students use the default
                                        plan_first=plan_first)
    except CodeError as exc:
        request.session["code_error"] = str(exc)
        return RedirectResponse("/code", status_code=303)
    return RedirectResponse(f"/code/{session_id}", status_code=303)


# ---------------------------------------------------------------- one session
def _what_next(row, mentor: bool, student_session: bool) -> dict[str, str] | None:
    """For a session that ended without finishing (declined, stopped or failed): what happened and what to try next."""
    status, error = row["status"], row["error"] or ""
    if status == "awaiting_approval" or status not in ("stopped", "failed"):
        return None
    if status == "stopped" and error.startswith("A mentor declined this request"):
        return {"title": "A mentor declined this request", "reason": error.replace("A mentor declined this request.",
                                                                                   "", 1).strip(),
                "next": "You can ask again with more detail, or talk to a mentor about what you need."
                if not mentor else "The student has been told. Nothing was started."}
    if status == "stopped":
        return {"title": "This session was stopped", "reason": error,
                "next": ("A mentor can send a follow-up to carry on." if not mentor else
                         "Send a follow-up below to carry on, or start a new session.")}
    friendly = error if _col(row, "engine", "classic") == "agent" and error else FRIENDLY_FAILED
    return {"title": "Claude couldn't finish this task", "reason": "" if friendly == FRIENDLY_FAILED else friendly,
            "raw": error if mentor and error and error != friendly else "",
            "next": ("A mentor can send a follow-up to try again, or you can ask for a new session."
                     if not mentor else "Send a follow-up below to try again, or start a new session.")}


def _session_context(s: Services, row, viewer: Viewer) -> dict[str, Any]:
    mentor = viewer.is_mentor
    student_session = bool(row["owner_is_student"])
    shown = None if mentor else STUDENT_SHOWN_CHARS
    # A note whose tool is "mentor" (the new engine's details about a failure) is for mentors only
    steps = [step_view(s, r, student_session=student_session, shown=shown, mentor=mentor)
             for r in s.code.steps(row["id"]) if mentor or r["tool"] != "mentor"]
    workspace = s.code.workspace(row["id"])
    released = bool(row["patch_released"])
    ready = workspace.ready() and row["status"] not in LIVE
    all_changes = workspace.changes() if ready else []
    patch_problem = s.code.agent.patch_problem(row["id"]) if _col(row, "engine", "classic") == "agent" else ""
    changes = all_changes if (mentor or released) and not patch_problem else []
    if any(step["status"] == "pending" and step["command"] for step in steps):
        # A build or test run executes the code Claude changed, with the app's own permissions
        changed = [c["name"] for c in (changes or workspace.changes())]
        for step in steps:
            if step["status"] == "pending" and step["command"]:
                step["runs_changes"] = changed
    box = s.sandboxes.live_for_session(row["id"]) if s.sandboxes is not None else None
    waiting = s.code.follow_up_waiting(row["id"])
    agent = _col(row, "engine", "classic") == "agent"
    view = session_view(s, row)
    can_follow_up = ((row["status"] in FINISHED and workspace.ready() and waiting is None)
                     or (mentor and agent and row["status"] == "running"))
    queue_note = next((st["output"] for st in reversed(steps) if st["kind"] == "note" and not st["person"]
                       and st["output"].startswith(QUEUE_NOTE)), "") if row["status"] == "preparing" else ""
    files, more_files = parse_changes(changes) if changes else ([], 0)
    plan = next((st for st in reversed(steps) if st["shape"] == "plan" and st["status"] == "pending"), None)
    # While the page is open it asks for itself again: quickly while Claude works, slowly while it waits for a person
    if row["status"] in LIVE:
        poll = "fast"
    elif not view["final"] or waiting is not None or (student_session and not mentor and ready and all_changes
                                                      and not released):
        poll = "slow"
    else:
        poll = "off"
    if can_follow_up:
        composer = ("A mentor reads this before Claude sees it." if not mentor else
                    "Claude is working. Your message goes in as soon as this step is done."
                    if row["status"] == "running" else "")
    elif waiting is not None:
        composer = "Your last request is waiting for a mentor."
    elif row["status"] == "awaiting_approval":
        composer = "You can send more once Claude has started."
    elif not view["final"]:
        composer = "You can send more when Claude is done."
    else:
        composer = "This session's files were deleted, so it can't carry on. Start a new session."
    return {"session": view, "patch_problem": patch_problem if (mentor or released) else "",
            "steps": nest_steps(steps), "changes": changes, "files": files, "more_files": more_files,
            "mentor": mentor, "poll": poll, "student_session": student_session,
            "can_follow_up": can_follow_up, "composer_hint": composer,
            "follow_up_waiting": waiting is not None, "awaiting": row["status"] == "awaiting_approval",
            "queue_note": queue_note, "plan_waiting": plan is not None, "what_next": _what_next(
                row, mentor, student_session),
            "waiting_for_changes": student_session and bool(all_changes) and not released and not mentor,
            "no_changes": ready and not all_changes and row["status"] == "done",
            "can_release": (mentor and student_session and ready and not released and bool(changes)
                            and not patch_problem),
            "released": released, "free_clicks": agent or bool(s.settings.code_student_free_rein),
            "screen_note": row["screen_note"] if mentor else None,
            "sandbox_left": hours_left(box["expires_at"]) if box is not None else None,
            "can_extend": mentor and box is not None, "can_stop": mentor and row["status"] not in FINISHED}


@page(require="login")
async def code_session(request: Request, viewer: Viewer) -> Response:
    denied = gate(request, viewer)
    if denied is not None:
        return denied
    s = services(request)
    row = s.code.get(request.path_params["sid"])
    if not visible(viewer, row):
        return error_page(request, viewer, 404, "Session not found", "This Code session doesn't exist.")
    return render(request, "code_session.html", viewer, error=request.session.pop("code_error", None),
                  **_session_context(s, row, viewer))


@page(require="login")
async def code_steps(request: Request, viewer: Viewer) -> Response:
    """The session's steps and status, redrawn while the page is open."""
    denied = gate(request, viewer)
    if denied is not None:
        return denied
    s = services(request)
    row = s.code.get(request.path_params["sid"])
    if not visible(viewer, row):
        return Response(status_code=404)
    return render(request, "_code_steps.html", viewer, **_session_context(s, row, viewer))


@page(require="login")
async def code_follow_up(request: Request, viewer: Viewer) -> Response:
    """A mentor's follow-up runs at once. A student's waits for a mentor, like the first request."""
    denied = gate(request, viewer)
    if denied is not None:
        return denied
    s = services(request)
    form = await request.form()
    sid = request.path_params["sid"]
    try:
        if viewer.is_mentor:
            s.code.follow_up(viewer, sid, str(form.get("text", "")))
        else:
            text = str(form.get("text", ""))
            verdict = await s.screener.check(text) if s.settings.screener_enabled else None
            s.code.request_follow_up(viewer, sid, text, screen_note(verdict))
    except CodeError as exc:
        request.session["code_error"] = str(exc)
    return RedirectResponse(f"/code/{sid}", status_code=303)


def _mentor_post(handler):
    """A mentor's form POST. The handler returns the page to go to next; a CodeError goes back to the session page
    (or the list) as a note."""
    @page(require="mentor")
    async def endpoint(request: Request, viewer: Viewer) -> Response:
        s = services(request)
        form = await request.form()
        back = "/code"
        try:
            back = await handler(request, viewer, s, form)
        except CodeError as exc:
            request.session["code_error"] = str(exc)
            if "sid" in request.path_params:
                back = f"/code/{request.path_params['sid']}"
            elif "step" in request.path_params:
                back = f"/code/{_session_of_step(s, request.path_params['step'])}"
        return RedirectResponse(back, status_code=303)

    endpoint.__name__ = handler.__name__
    return endpoint


def _session_of_step(s: Services, step_id) -> int:
    step = s.db.one("SELECT session_id FROM code_steps WHERE id = ?", (step_id,))
    if step is None:
        raise CodeError("That step doesn't exist.")
    return step["session_id"]


def _step_action(approve: bool, follow_up: bool):
    async def handler(request, viewer, s: Services, form) -> str:
        step_id = request.path_params["step"]
        if follow_up:
            sid = s.code.decide_follow_up(viewer, step_id, approve, str(form.get("note", "")))
        else:
            sid = s.code.decide(viewer, step_id, approve, str(form.get("note", "")))
        return f"/code/{sid}"

    handler.__name__ = f"code_{'approve' if approve else 'decline'}{'_follow_up' if follow_up else ''}_step"
    return _mentor_post(handler)


def _request_action(approve: bool):
    async def handler(request, viewer, s: Services, form) -> str:
        sid = request.path_params["sid"]
        s.code.decide_request(viewer, sid, approve, str(form.get("note", "")))
        return f"/code/{sid}"

    handler.__name__ = f"code_{'approve' if approve else 'decline'}_request"
    return _mentor_post(handler)


async def _stop(request, viewer, s: Services, form) -> str:
    sid = request.path_params["sid"]
    s.code.stop(viewer, sid)
    return f"/code/{sid}"


async def _release(request, viewer, s: Services, form) -> str:
    sid = request.path_params["sid"]
    s.code.release_patch(viewer, sid)
    return f"/code/{sid}"


async def _extend(request, viewer, s: Services, form) -> str:
    sid = request.path_params["sid"]
    s.code.extend_sandbox(viewer, sid)
    return f"/code/{sid}"


async def _delete(request, viewer, s: Services, form) -> str:
    sid = request.path_params["sid"]
    # Deleting the sandbox takes a moment, so it runs in a worker thread and the app keeps answering meanwhile
    await asyncio.to_thread(s.code.delete, viewer, sid)
    return "/code"


code_stop = _mentor_post(_stop)
code_release = _mentor_post(_release)
code_extend = _mentor_post(_extend)
code_delete = _mentor_post(_delete)


@page(require="login")
async def code_patch(request: Request, viewer: Viewer) -> Response:
    denied = gate(request, viewer)
    if denied is not None:
        return denied
    s = services(request)
    row = s.code.get(request.path_params["sid"])
    if not visible(viewer, row):
        return error_page(request, viewer, 404, "Nothing to download", "This session has no changes to download.")
    workspace = s.code.workspace(row["id"])
    if not workspace.ready():
        return error_page(request, viewer, 404, "Nothing to download", "This session has no changes to download.")
    if not viewer.is_mentor and not row["patch_released"]:
        return error_page(request, viewer, 403, "Not released yet", "A mentor is looking at your changes. You can "
                                                                    "download them once a mentor releases them.")
    patch = workspace.patch_text()  # a new-engine session's patch comes from its sandbox, whole
    if patch is None:
        patch = "".join(change["diff"] for change in workspace.changes())
    s.db.audit("code_patch_downloaded", actor_id=viewer.id, target_type="code", target_id=row["id"])
    return Response(patch, media_type="text/x-diff; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="code-session-{row["id"]}.patch"',
        "Cache-Control": "private, no-store"})


code_routes = [
    Route("/code", code_page),
    Route("/code", code_start, methods=["POST"]),
    Route("/code/needs", code_needs),
    Route("/code/{sid:int}", code_session),
    Route("/code/{sid:int}/steps", code_steps),
    Route("/code/{sid:int}/stop", code_stop, methods=["POST"]),
    Route("/code/{sid:int}/follow-up", code_follow_up, methods=["POST"]),
    Route("/code/{sid:int}/approve-request", _request_action(True), methods=["POST"]),
    Route("/code/{sid:int}/decline-request", _request_action(False), methods=["POST"]),
    Route("/code/{sid:int}/release", code_release, methods=["POST"]),
    Route("/code/{sid:int}/extend", code_extend, methods=["POST"]),
    Route("/code/{sid:int}/delete", code_delete, methods=["POST"]),
    Route("/code/{sid:int}/changes.patch", code_patch),
    Route("/code/steps/{step:int}/approve", _step_action(True, False), methods=["POST"]),
    Route("/code/steps/{step:int}/decline", _step_action(False, False), methods=["POST"]),
    Route("/code/steps/{step:int}/approve-follow-up", _step_action(True, True), methods=["POST"]),
    Route("/code/steps/{step:int}/decline-follow-up", _step_action(False, True), methods=["POST"]),
]
