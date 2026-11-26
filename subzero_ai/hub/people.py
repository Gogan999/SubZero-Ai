"""The People page (each person's role) and the Permissions page (what each role can do)."""

from __future__ import annotations

from typing import Any

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route

from ..roles import is_builtin_user, is_dev_user
from ..store import Viewer
from ..web import fmt_time, page, render, services
from .permissions import ADMIN, ROLES, PermissionChangeError, all_permissions, role_label

MANAGE = "hub.manage"


def people_rows(request: Request) -> list[dict[str, Any]]:
    """Everyone who has signed in, with their role now and where it comes from."""
    s = services(request)
    dev = s.settings.dev_login
    rows = []
    for row in s.db.all("SELECT * FROM users WHERE active = 1 ORDER BY name COLLATE NOCASE"):
        sid = row["slack_user_id"]
        if is_builtin_user(sid) and not (dev and is_dev_user(sid)):
            continue
        source, locked = "", False
        if sid in s.settings.admin_slack_ids:
            source, locked = "Named as an admin in Settings", True
        elif row["role"] == "mentor" and row["assigned_role"] != "mentor" and s.settings.mentors_from_channel:
            source = "A member of the approvals channel"
        rows.append({"id": row["id"], "name": row["name"], "slack_user_id": sid, "role": role_label(row["role"]),
                     "assigned": row["assigned_role"] or "", "source": source, "locked": locked,
                     "last_login": fmt_time(s.store, row["last_login_at"]) if row["last_login_at"] else "Never"})
    return rows


@page(require=MANAGE)
async def people_page(request: Request, viewer: Viewer) -> Response:
    return render(request, "people.html", viewer, people=people_rows(request),
                  roles=[(r, role_label(r)) for r in ROLES], flash=request.session.pop("flash", None))


@page(require=MANAGE)
async def people_set_role(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    uid = request.path_params["uid"]
    form = await request.form()
    role = str(form.get("role") or "")
    row = s.store.user(uid)
    if row is None or role not in ("", *ROLES):
        request.session["flash"] = "That change couldn't be made."
    elif row["id"] == viewer.id:
        request.session["flash"] = "You can't change your own role. Ask another coach."
    elif row["slack_user_id"] in s.settings.admin_slack_ids:
        request.session["flash"] = f"{row['name']} is named as an admin in Settings, so they're always a coach."
    elif (row["assigned_role"] or "") != role:
        s.db.execute("UPDATE users SET assigned_role = ? WHERE id = ?", (role or None, uid))
        s.db.audit("role_assigned", actor_id=viewer.id, target_type="user", target_id=uid,
                   details={"name": row["name"], "before": row["assigned_role"], "after": role or None})
        new_role = await s.directory.role_for(row["slack_user_id"])
        s.store.set_user_role(uid, new_role)
        request.session["flash"] = f"{row['name']} is now {role_label(new_role).lower()}."
    return RedirectResponse("/people", status_code=303)


def permission_table(request: Request) -> list[dict[str, Any]]:
    """One row per permission: its label and, for each role, whether it has it and whether that can change."""
    grants = services(request).directory.grants
    table = []
    for p in all_permissions():
        cells = []
        for role in ROLES:
            fixed = role == ADMIN or role in p.never_roles
            cells.append({"role": role, "name": f"{role}:{p.key}", "on": grants.allows(role, p.key), "fixed": fixed})
        table.append({"key": p.key, "label": p.label, "cells": cells})
    return table


@page(require=MANAGE)
async def permissions_page(request: Request, viewer: Viewer) -> Response:
    return render(request, "permissions.html", viewer, table=permission_table(request),
                  roles=[(r, role_label(r)) for r in ROLES], flash=request.session.pop("flash", None))


@page(require=MANAGE)
async def permissions_save(request: Request, viewer: Viewer) -> Response:
    s = services(request)
    form = await request.form()
    changed, problems = 0, []
    for p in all_permissions():
        for role in ROLES:
            if role == ADMIN or role in p.never_roles:
                continue
            try:
                changed += s.directory.grants.set(role, p.key, form.get(f"{role}:{p.key}") == "1", actor_id=viewer.id)
            except PermissionChangeError as exc:
                problems.append(str(exc))
    request.session["flash"] = " ".join(problems) or (f"Saved {changed} change{'s' if changed != 1 else ''}."
                                                       if changed else "Nothing changed.")
    return RedirectResponse("/permissions", status_code=303)


people_routes = [
    Route("/people", people_page),
    Route("/people/{uid:int}/role", people_set_role, methods=["POST"]),
    Route("/permissions", permissions_page),
    Route("/permissions", permissions_save, methods=["POST"]),
]
