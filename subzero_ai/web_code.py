"""Code pages (mentors only): start a session, watch Claude work, approve or decline each change and command,
and download the changes as a patch. The work itself is in code_runner.py."""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from .code_runner import CodeError
from .render import markdown_html
from .store import Viewer
from .web import Services, error_page, fmt_time, model_label, page, render, services

STATUS_LABELS = {"preparing": ("Copying the repository", "pending"), "running": ("Working", "pending"),
                 "waiting": ("Waiting for a mentor", "pending"), "done": ("Done", "ok"),
                 "stopped": ("Stopped", "unknown"), "failed": ("Failed", "bad")}
LIVE = ("preparing", "running")


def _arg(args: dict, key: str) -> str:
    return str(args.get(key, "") or "")


def step_view(s: Services, row) -> dict[str, Any]:
    args = json.loads(row["input"] or "{}")
    view = {"id": row["id"], "kind": row["kind"], "tool": row["tool"], "status": row["status"],
            "time": fmt_time(s.store, row["created_at"]), "reviewer": row["reviewer_name"],
            "note": row["review_note"], "reason": _arg(args, "reason"), "output": row["output"] or "",
            "html": None, "label": "", "detail": "", "diff": None, "command": None, "runs_changes": []}
    if row["kind"] == "text":
        view["html"] = markdown_html(row["output"] or "")
        return view
    if row["kind"] == "note":
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
            "kind": kind, "owner": row["owner_name"], "model": model_label(row["model"]), "repo": row["repo"],
            "ref": row["ref"], "error": row["error"], "updated": fmt_time(s.store, row["updated_at"]),
            "created": fmt_time(s.store, row["created_at"]), "live": row["status"] in LIVE,
            "tokens": row["input_tokens"] + row["output_tokens"],
            "pending": row["pending"] if "pending" in row.keys() else 0}


@page(require="mentor")
async def code_page(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    models = [(m, model_label(m)) for m in dict.fromkeys([s.settings.model_default, s.settings.model_hard])]
    return render(request, "code.html", viewer, sessions=[session_view(s, r) for r in s.code.list()],
                  available=s.code.available, models=models, commands=s.settings.code_commands,
                  error=request.session.pop("code_error", None))


@page(require="mentor")
async def code_start(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    model = str(form.get("model", ""))
    if model not in (s.settings.model_default, s.settings.model_hard):
        model = s.settings.model_default
    try:
        session_id = s.code.start(viewer, str(form.get("task", "")), model)
    except CodeError as exc:
        request.session["code_error"] = str(exc)
        return RedirectResponse("/code", status_code=303)
    return RedirectResponse(f"/code/{session_id}", status_code=303)


def _session_context(s: Services, row) -> dict[str, Any]:
    steps = [step_view(s, r) for r in s.code.steps(row["id"])]
    workspace = s.code.workspace(row["id"])
    changes = workspace.changes() if workspace.ready() and row["status"] not in LIVE else []
    if any(step["status"] == "pending" and step["command"] for step in steps):
        # A build or test run executes the code Claude changed, with the app's own permissions
        changed = [c["name"] for c in (changes or workspace.changes())]
        for step in steps:
            if step["status"] == "pending" and step["command"]:
                step["runs_changes"] = changed
    return {"session": session_view(s, row), "steps": steps, "changes": changes,
            "can_follow_up": row["status"] in ("done", "stopped", "failed") and workspace.ready()}


@page(require="mentor")
async def code_session(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    row = s.code.get(request.path_params["sid"])
    if row is None:
        return error_page(request, viewer, 404, "Session not found", "This Code session doesn't exist.")
    return render(request, "code_session.html", viewer, error=request.session.pop("code_error", None),
                  **_session_context(s, row))


@page(require="mentor")
async def code_steps(request: Request, viewer: Viewer) -> Response:
    """The session's steps and status, redrawn while Claude works."""
    s = services(request)
    row = s.code.get(request.path_params["sid"])
    if row is None:
        return Response(status_code=404)
    return render(request, "_code_steps.html", viewer, **_session_context(s, row))


def _code_action(kind: str):
    @page(require="mentor")
    async def action(request: Request, viewer: Viewer) -> Response:
        s = services(request)
        form = await request.form()
        if "step" in request.path_params:
            step = s.db.one("SELECT session_id FROM code_steps WHERE id = ?", (request.path_params["step"],))
            session_id = step["session_id"] if step else 0
        else:
            session_id = request.path_params["sid"]
        try:
            if kind in ("approve", "decline"):
                s.code.decide(viewer, request.path_params["step"], kind == "approve", str(form.get("note", "")))
            elif kind == "stop":
                s.code.stop(viewer, session_id)
            else:
                s.code.follow_up(viewer, session_id, str(form.get("text", "")))
        except CodeError as exc:
            request.session["code_error"] = str(exc)
        return RedirectResponse(f"/code/{session_id}" if session_id else "/code", status_code=303)

    action.__name__ = f"code_{kind}"
    return action


@page(require="mentor")
async def code_patch(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    row = s.code.get(request.path_params["sid"])
    workspace = s.code.workspace(row["id"]) if row is not None else None
    if workspace is None or not workspace.ready():
        return error_page(request, viewer, 404, "Nothing to download", "This session has no changes to download.")
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
    Route("/code/{sid:int}/stop", _code_action("stop"), methods=["POST"]),
    Route("/code/{sid:int}/follow-up", _code_action("follow_up"), methods=["POST"]),
    Route("/code/{sid:int}/changes.patch", code_patch),
    Route("/code/steps/{step:int}/approve", _code_action("approve"), methods=["POST"]),
    Route("/code/steps/{step:int}/decline", _code_action("decline"), methods=["POST"]),
]
