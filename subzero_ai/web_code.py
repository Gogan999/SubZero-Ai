"""Code pages: start a session, watch Claude work, approve or decline each change and command, and download the
changes as a patch. Mentors see every session. Students (when code_student_access is on) ask for a session and see
only their own: a mentor approves the request first, and the patch is downloadable once a mentor releases it. The work
itself is in code_runner.py."""

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
from .render import markdown_html
from .store import Viewer
from .web import Services, error_page, fmt_time, model_label, page, render, services

STATUS_LABELS = {"awaiting_approval": ("Waiting for a mentor to approve", "pending"),
                 "preparing": ("Copying the repository", "pending"), "running": ("Working", "pending"),
                 "waiting": ("Waiting for a mentor", "pending"), "done": ("Done", "ok"),
                 "stopped": ("Stopped", "unknown"), "failed": ("Failed", "bad")}
LIVE = ("preparing", "running")
STUDENT_SHOWN_CHARS = 6000  # a student sees at most this much of each step's output; the rest is cut off


def _arg(args: dict, key: str) -> str:
    return str(args.get(key, "") or "")


def step_view(s: Services, row, *, student_session: bool = False, shown: int | None = None) -> dict[str, Any]:
    args = json.loads(row["input"] or "{}")
    output = row["output"] or ""
    if shown is not None and len(output) > shown:
        output = output[:shown] + "\n(The rest is cut off here.)"
    view = {"id": row["id"], "kind": row["kind"], "tool": row["tool"], "status": row["status"],
            "time": fmt_time(s.store, row["created_at"]), "reviewer": row["reviewer_name"],
            "note": row["review_note"], "reason": _arg(args, "reason"), "output": output,
            "html": None, "label": "", "detail": "", "diff": None, "command": None, "runs_changes": []}
    if row["kind"] == "text":
        view["html"] = markdown_html(output)
        return view
    if row["kind"] == "note":
        if row["status"] == "pending":
            view["label"] = "Asked for more, waiting for a mentor"
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
        view["label"] = f"Asked for {tool}"
    return view


def session_view(s: Services, row) -> dict[str, Any]:
    label, kind = STATUS_LABELS[row["status"]]
    return {"id": row["id"], "title": row["title"], "task": row["task"], "status": row["status"], "label": label,
            "kind": kind, "owner": row["owner_name"], "repo": row["repo"],
            "model": model_label(row["model"], s.model_names),
            "ref": row["ref"], "error": row["error"], "updated": fmt_time(s.store, row["updated_at"]),
            "created": fmt_time(s.store, row["created_at"]), "live": row["status"] in LIVE,
            "tokens": row["input_tokens"] + row["output_tokens"],
            "pending": row["pending"] if "pending" in row.keys() else 0,
            "student": bool(row["owner_is_student"]), "awaiting": row["status"] == "awaiting_approval",
            "screen_note": row["screen_note"]}


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
    """Students get Code only while a mentor or admin has turned on code_student_access. The sidebar hides it the same
    way. Mentors are never blocked here."""
    if viewer.is_mentor or services(request).settings.code_student_access:
        return None
    if request.method != "GET":
        return JSONResponse({"ok": False, "error": "Code isn't open to students yet."}, status_code=403)
    return error_page(request, viewer, 403, "Mentors only", "Code is for mentors. Students can use it once a mentor "
                                                            "turns it on.")


def screen_note(verdict) -> str | None:
    """What the screener said about a student's words, for the mentor. None when it passed them."""
    if verdict is None or verdict.passed:
        return None
    return f"Screener: {verdict.note}"


def code_models(s: Services) -> list[tuple[str, str]]:
    """The default and hard models that can run Code sessions (Claude models only), with their labels."""
    return [(m, model_label(m, s.model_names)) for m in dict.fromkeys(
        normalize_model_id(m) for m in (s.settings.model_default, s.settings.model_hard)) if s.capabilities(m).code]


@page(require="login")
async def code_page(request: Request, viewer: Viewer) -> Response:
    denied = gate(request, viewer)
    if denied is not None:
        return denied
    s = services(request)
    mentor = viewer.is_mentor
    rows = s.code.list(owner_id=None if mentor else viewer.id)
    sessions = [session_view(s, r) for r in rows if visible(viewer, r)]
    problem = None if mentor else s.code.student_problem()
    return render(request, "code.html", viewer, sessions=sessions, mentor=mentor, problem=problem,
                  awaiting=[session_view(s, r) for r in s.code.awaiting()] if mentor else [],
                  available=s.code.available, models=code_models(s), commands=s.code.command_programs(),
                  error=request.session.pop("code_error", None))


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
    try:
        if viewer.is_mentor:
            model = normalize_model_id(form.get("model", ""))
            if model not in allowed:
                model = allowed[0]
            session_id = s.code.start(viewer, str(form.get("task", "")), model)
        else:
            problem = s.code.student_problem()
            if problem:
                raise CodeError(problem)
            task = str(form.get("task", ""))
            verdict = await s.approvals.screener.check(task) if s.settings.screener_enabled else None
            session_id = s.code.request(viewer, task, allowed[0], screen_note(verdict))  # students use the default
    except CodeError as exc:
        request.session["code_error"] = str(exc)
        return RedirectResponse("/code", status_code=303)
    return RedirectResponse(f"/code/{session_id}", status_code=303)


def _session_context(s: Services, row, viewer: Viewer) -> dict[str, Any]:
    mentor = viewer.is_mentor
    student_session = bool(row["owner_is_student"])
    shown = None if mentor else STUDENT_SHOWN_CHARS
    steps = [step_view(s, r, student_session=student_session, shown=shown) for r in s.code.steps(row["id"])]
    workspace = s.code.workspace(row["id"])
    released = bool(row["patch_released"])
    ready = workspace.ready() and row["status"] not in LIVE
    all_changes = workspace.changes() if ready else []
    changes = all_changes if mentor or released else []
    if any(step["status"] == "pending" and step["command"] for step in steps):
        # A build or test run executes the code Claude changed, with the app's own permissions
        changed = [c["name"] for c in (changes or workspace.changes())]
        for step in steps:
            if step["status"] == "pending" and step["command"]:
                step["runs_changes"] = changed
    box = s.sandboxes.live_for_session(row["id"]) if s.sandboxes is not None else None
    waiting = s.code.follow_up_waiting(row["id"])
    return {"session": session_view(s, row), "steps": steps, "changes": changes, "mentor": mentor,
            "can_follow_up": row["status"] in FINISHED and workspace.ready() and waiting is None,
            "follow_up_waiting": waiting is not None,
            "awaiting": row["status"] == "awaiting_approval",
            "waiting_for_changes": student_session and bool(all_changes) and not released and not mentor,
            "can_release": mentor and student_session and ready and not released,
            "released": released,
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
    """The session's steps and status, redrawn while Claude works."""
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
            verdict = await s.approvals.screener.check(text) if s.settings.screener_enabled else None
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
    patch = "".join(change["diff"] for change in workspace.changes())
    s.db.audit("code_patch_downloaded", actor_id=viewer.id, target_type="code", target_id=row["id"])
    return Response(patch, media_type="text/x-diff; charset=utf-8", headers={
        "Content-Disposition": f'attachment; filename="code-session-{row["id"]}.patch"',
        "Cache-Control": "private, no-store"})


code_routes = [
    Route("/code", code_page),
    Route("/code", code_start, methods=["POST"]),
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
