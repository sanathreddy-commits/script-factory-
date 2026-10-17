import csv
import io
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse

from . import admin_ops, codes, config, db, importers, ops, reports_excel, storage, sync
from .integrations import drive, email_service, sheets

IST = ZoneInfo("Asia/Kolkata")
router = APIRouter()

UPLOADS_DIR = os.path.join(os.path.dirname(config.db_path()), "uploads")
BACKUPS_DIR = os.path.join(os.path.dirname(config.db_path()), "db_backups")
os.makedirs(UPLOADS_DIR, exist_ok=True)
os.makedirs(BACKUPS_DIR, exist_ok=True)


def current_user(request: Request):
    tok = request.cookies.get("sf_session")
    if not tok:
        return None, ""
    with db.ro() as c:
        return ops.get_session(c, tok)


def gate(request: Request, *roles):
    u, csrf = current_user(request)
    if not u:
        from .main import Redirect
        if "developer" in roles and "admin" not in roles:
            raise Redirect("/dev/login")
        if "admin" in roles and "lead" not in roles and "supervisor" not in roles:
            raise Redirect("/admin/login")
        if "admin" in roles and request.url.path.startswith("/admin"):
            raise Redirect("/admin/login")
        raise Redirect("/login")

    ops.touch_seen(u)
    user_role = u["role"]
    allowed = list(roles)
    if "admin" in allowed:
        allowed.append("developer")
    if "lead" in allowed or "supervisor" in allowed:
        allowed.extend(["lead", "supervisor", "admin", "developer"])
    if "participant" in allowed:
        allowed.extend(["participant", "admin", "developer"])

    if user_role not in allowed:
        raise PermissionError("Your role cannot open this page.")
    request.state.csrf = csrf
    return u


async def _post_form(request: Request, *roles):
    """gate() plus the form and a CSRF check, for POST handlers."""
    u = gate(request, *roles)
    f = await request.form()
    if f.get("csrf") != getattr(request.state, "csrf", None):
        raise HTTPException(400, "Session expired. Reload the page and try again.")
    return u, f


import urllib.parse

def back(url: str, m: str = "", e: str = ""):
    qs = []
    if m:
        qs.append(f"m={urllib.parse.quote_plus(m)}")
    if e:
        qs.append(f"e={urllib.parse.quote_plus(e)}")
    if qs:
        url += ("&" if "?" in url else "?") + "&".join(qs)
    return RedirectResponse(url, 303)


def render(request: Request, template_name: str, user, **ctx):
    from .main import templates
    with db.ro() as c:
        all_langs = c.execute("SELECT * FROM languages ORDER BY id").fetchall()
        
    supervisor_lang = None
    sup_langs = []
    u_role = user["role"] if user else ""
    u_lid = user["language_id"] if (user and "language_id" in user.keys() and user["language_id"]) else None
    if user and u_role in ("lead", "supervisor"):
        ids = list((user["lang_ids"] if "lang_ids" in user.keys() else None) or ([u_lid] if u_lid else []))
        sup_langs = [l for l in all_langs if l["id"] in ids]
        if sup_langs:
            supervisor_lang = sup_langs[0]

    active_L = ctx.get("current_L")
    if not active_L:
        req_lid = request.query_params.get("lang") or request.query_params.get("lang_id")
        cookie_lid = request.cookies.get("sf_lang")
        found_id = None
        if req_lid and str(req_lid).isdigit():
            found_id = int(req_lid)
        elif cookie_lid and str(cookie_lid).isdigit():
            found_id = int(cookie_lid)
        if found_id:
            for l in all_langs:
                if l["id"] == found_id:
                    active_L = l
                    break
    if not active_L:
        active_L = supervisor_lang or (all_langs[0] if all_langs else None)

    ctx.update({
        "request": request,
        "user": user,
        "csrf": getattr(request.state, "csrf", ""),
        "q": request.query_params,
        "path": request.url.path,
        "langs": all_langs,
        "L": active_L,
        "current_L": active_L,
        "supervisor_lang": supervisor_lang,
        "sup_langs": sup_langs,
        "kpi_lang": ctx.get("lang_param") or ctx.get("selected_lid") or "all",
        "kpi_agent": ctx.get("selected_uid") or "",
        "viewing_as_return": bool(request.cookies.get("sf_return")) and u_role in ("participant", "lead", "supervisor"),
        "viewing_as_role": "supervisor" if u_role in ("lead", "supervisor") else "agent",
    })
    return templates.TemplateResponse(request, template_name, ctx)


# ==============================================================================
# GROUP AG. AGENT PORTAL (Mobile 390px)
# ==============================================================================

@router.get("/agent")
@router.get("/me")
async def agent_portal_get(request: Request):
    u = gate(request, "participant")
    tab = request.query_params.get("tab") or "dashboard"
    screen = request.query_params.get("screen")
    aid_param = request.query_params.get("aid")
    err = request.query_params.get("e", "")
    msg = request.query_params.get("m", "")

    with db.ro() as c:
        # Find active pair
        my_pairs = ops.active_pairs_for(c, u["id"])
        sel_pair_raw = request.query_params.get("pair")
        sel_pair = int(sel_pair_raw) if (sel_pair_raw and sel_pair_raw.isdigit() and int(sel_pair_raw) in [x["pair_id"] for x in my_pairs]) else None
        pair = my_pairs[0] if my_pairs else None
        partner = None
        if my_pairs:
            focus = next((x for x in my_pairs if x["pair_id"] == sel_pair), my_pairs[0])
            partner = c.execute("SELECT * FROM users WHERE id=?", (focus["partner_id"],)).fetchone()
        # every pair the agent is in (a person can have several), with its own script counts
        done_sql = ("(a.status IN ('COMPLETED','CONFIRMED','SUBMITTED','ACCEPTED') OR (a.review_status IN ('APPROVED','ACCEPTED','PENDING','REJECTED') AND a.conf_min > 0)) AND a.status != 'ASSIGNED'")
        partners = []
        for x in my_pairs:
            tot = c.execute("SELECT COUNT(*) n FROM assignments a WHERE a.pair_id=? AND a.status NOT IN ('RELEASED','ABANDONED')", (x["pair_id"],)).fetchone()["n"]
            dn = c.execute("SELECT COUNT(*) n FROM assignments a WHERE a.pair_id=? AND a.status NOT IN ('RELEASED','ABANDONED') AND " + done_sql, (x["pair_id"],)).fetchone()["n"]
            partners.append({**x, "total": tot, "completed": dn, "pending": tot - dn, "selected": x["pair_id"] == sel_pair})

        # Find team lead / supervisor
        supervisor = c.execute("SELECT * FROM users WHERE role IN ('lead', 'supervisor') AND language_id=? LIMIT 1", (u["language_id"],)).fetchone()
        client_url = db.setting(c, "client_url", "https://client-portal.recording-system.com")

        # AG4 Completed Screen
        completed_item = None
        if screen == "AG4" and aid_param and aid_param.isdigit():
            asg = c.execute("SELECT a.*, s.code, s.domain, s.subdomain, s.specialisation FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE a.id=?", (int(aid_param),)).fetchone()
            if asg:
                m, s = divmod(int(round(float(asg["conf_min"] or 0) * 60)), 60)
                completed_item = {
                    "script_code": asg["code"],
                    "taxonomy": f"{asg['domain']} > {asg['subdomain']} > {asg['specialisation']}",
                    "time_taken": f"{m:02d}:{s:02d}",
                    "partner_name": (c.execute("SELECT name FROM users WHERE id=?", (asg["ub"] if asg["ua"] == u["id"] else asg["ua"],)).fetchone() or {"name": "Partner"})["name"]
                }

        # Assigned scripts
        if my_pairs:
            use_ids = [sel_pair] if sel_pair else [x["pair_id"] for x in my_pairs]
            all_asg = c.execute(
                "SELECT a.*, s.code, s.domain, s.subdomain, s.specialisation, "
                "(SELECT name FROM users WHERE id=CASE WHEN a.ua=? THEN a.ub ELSE a.ua END) AS partner_name "
                "FROM assignments a JOIN scripts s ON s.id=a.script_id "
                "WHERE a.pair_id IN (%s) AND a.status NOT IN ('RELEASED','ABANDONED') ORDER BY a.id ASC" % ",".join("?" * len(use_ids)),
                [u["id"]] + use_ids
            ).fetchall()
        else:
            all_asg = []

        pending_scripts = []
        completed_scripts = []
        for a in all_asg:
            is_done = (a["status"] in ("COMPLETED", "CONFIRMED", "SUBMITTED", "ACCEPTED") or
                       (a["review_status"] and a["review_status"] in ("APPROVED", "ACCEPTED", "PENDING", "REJECTED") and a["conf_min"] and a["conf_min"] > 0))
            if is_done and a["status"] != "ASSIGNED":
                m, s = divmod(int(round(float(a["conf_min"] or 0) * 60)), 60)
                completed_scripts.append({
                    "id": a["id"],
                    "assignment_id": a["id"],
                    "code": a["code"],
                    "domain": a["domain"],
                    "subdomain": a["subdomain"],
                    "specialisation": a["specialisation"],
                    "review_status": a["review_status"] or a["status"],
                    "partner_name": a["partner_name"],
                    "time_taken": f"{m:02d}:{s:02d}"
                })
            else:
                pending_scripts.append({
                    "id": a["id"],
                    "assignment_id": a["id"],
                    "code": a["code"],
                    "domain": a["domain"],
                    "subdomain": a["subdomain"],
                    "specialisation": a["specialisation"],
                    "partner_name": a["partner_name"],
                    "status": "Pending"
                })

        kpi = {
            "total_assigned": len(all_asg),
            "pending": len(pending_scripts),
            "completed": len(completed_scripts)
        }

        portal_access_items = [
            it for it in get_portal_access_items(c)
            if not str(it.get("language_id") or "").isdigit() or int(it["language_id"]) == (u["language_id"] or 0)
        ]
        training_video_url = db.setting(c, "training_video_url", "https://www.youtube.com/embed/dQw4w9WgXcQ")

        agent_lang = None
        try:
            if u["language_id"]:
                agent_lang = c.execute("SELECT * FROM languages WHERE id=?", (u["language_id"],)).fetchone()
        except Exception:
            pass

        return render(request, "agent_portal.html", u,
                      agent=u,
                      agent_lang=agent_lang,
                      tab=tab,
                      screen=screen,
                      error=err,
                      msg=msg,
                      kpi=kpi,
                      partner=partner,
                      partners=partners,
                      sel_pair=sel_pair,
                      supervisor=supervisor,
                      client_url=client_url,
                      training_video_url=training_video_url,
                      portal_access_items=portal_access_items,
                      pending_scripts=pending_scripts,
                      completed_scripts=completed_scripts,
                      completed_item=completed_item)


@router.get("/agent/login_as/{uid}")
async def agent_login_as_route(request: Request, uid: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.tx() as c:
        target_agent = c.execute("SELECT * FROM users WHERE id=? AND role='participant'", (uid,)).fetchone()
        if not target_agent:
            raise HTTPException(404, "Agent not found")
        tok, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
        c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (tok, target_agent["id"], csrf, time.time()))
        db.audit(c, u, "login_as_agent", f"agent={uid}")
    resp = RedirectResponse("/agent", 303)
    if not request.cookies.get("sf_return"):
        resp.set_cookie("sf_return", request.cookies.get("sf_session", ""), max_age=6 * 3600, httponly=True, samesite="lax")
    resp.set_cookie("sf_session", tok, max_age=ops.SESSION_TTL, httponly=True, samesite="lax")
    return resp


@router.get("/switch_back")
async def switch_back(request: Request):
    """Return from 'view as agent' to the admin/supervisor session that started it."""
    orig = request.cookies.get("sf_return")
    with db.ro() as c:
        ou, _ = ops.get_session(c, orig) if orig else (None, None)
    if not ou or ou["role"] not in ("admin", "developer", "lead", "supervisor"):
        resp = RedirectResponse("/", 303)
        resp.delete_cookie("sf_return")
        return resp
    resp = RedirectResponse("/admin", 303)
    resp.set_cookie("sf_session", orig, max_age=ops.SESSION_TTL, httponly=True, samesite="lax")
    resp.delete_cookie("sf_return")
    return resp


@router.get("/admin/view-as-agent")
async def view_as_agent(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.ro() as c:
        ids = list(u["lang_ids"])
        q = (request.query_params.get("q") or "").strip().lower()
        agents = []
        if ids:
            rows = c.execute("SELECT u.id, u.name, u.phone, u.code, l.name AS lang FROM users u LEFT JOIN languages l ON l.id=u.language_id "
                             "WHERE u.role='participant' AND u.active=1 AND u.language_id IN (%s) ORDER BY l.name, u.name" % ",".join("?" * len(ids)), ids).fetchall()
            agents = [dict(x) for x in rows if not q or q in (x["name"] or "").lower() or q in (x["phone"] or "")]
        return render(request, "admin_view_as.html", u, agents=agents[:300], q=q)


@router.get("/admin/view-as-supervisor")
async def view_as_supervisor(request: Request):
    """Picker of supervisors; click opens that supervisor's view exactly as they see it (mirrors view-as-agent)."""
    u = gate(request, "admin", "developer")
    with db.ro() as c:
        q = (request.query_params.get("q") or "").strip().lower()
        rows = c.execute(
            "SELECT u.id, u.name, u.phone, u.code, u.active, u.role, u.language_id FROM users u WHERE u.role IN ('lead','supervisor') ORDER BY u.name"
        ).fetchall()
        sups = []
        for r in rows:
            d = dict(r)
            ids = ops.user_lang_ids(c, d)
            d["lang"] = ", ".join(x["name"] for x in c.execute(
                "SELECT name FROM languages WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(ids)), ids)) if ids else "-"
            if not q or q in (d["name"] or "").lower() or q in (d["phone"] or ""):
                sups.append(d)
        return render(request, "admin_view_as_supervisor.html", u, supervisors=sups[:300], q=q)


@router.get("/uploads/{fname}")
async def serve_upload_file(request: Request, fname: str):
    fname = os.path.basename(fname)
    if fname.startswith("proof_"):
        # Proof photos show people's faces/screens: only logged-in staff, or the agents named on that assignment.
        u = gate(request, "admin", "lead", "supervisor", "participant")
        with db.ro() as c:
            if u["role"] == "participant":
                own = c.execute("SELECT 1 FROM assignments WHERE proof_img=? AND (ua=? OR ub=?)",
                                (fname, u["id"], u["id"])).fetchone()
                if not own:
                    raise HTTPException(404, "File not found")
            row = c.execute("SELECT proof_drive_url FROM assignments WHERE proof_img=? ORDER BY id DESC LIMIT 1",
                            (fname,)).fetchone()
        data = storage.get_proof(fname, drive_link=(row["proof_drive_url"] if row else "") or "")
        if data is None:
            raise HTTPException(404, "Photo not found on the server, in Supabase Storage or in Google Drive")
        import mimetypes
        return Response(content=data, media_type=mimetypes.guess_type(fname)[0] or "image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})
    file_path = os.path.join(UPLOADS_DIR, fname)
    if not os.path.exists(file_path):
        raise HTTPException(404, "File not found")
    return FileResponse(file_path)


@router.get("/agent/script/{aid}")
async def agent_script_get(request: Request, aid: int):
    u = gate(request, "participant")
    err = request.query_params.get("e", "")
    with db.ro() as c:
        asg = c.execute("SELECT a.*, s.code, s.domain, s.subdomain, s.specialisation FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE a.id=?", (aid,)).fetchone()
        if not asg or (asg["ua"] != u["id"] and asg["ub"] != u["id"]):
            raise HTTPException(404, "Script not found or not assigned to you")

        content_text = get_script_content_text(c, asg["script_id"], asg["code"])

        is_submitted = (asg["status"] in ("SUBMITTED", "ACCEPTED", "CONFIRMED", "COMPLETED", "REJECTED") or
                        (asg["conf_min"] is not None and asg["conf_min"] > 0))
        time_taken_str = ""
        if asg["conf_min"] is not None and asg["conf_min"] > 0:
            m, s = divmod(int(round(float(asg["conf_min"] or 0) * 60)), 60)
            time_taken_str = f"{m:02d}:{s:02d}"

        proof_img = asg["proof_img"] or ""
        proof_url = f"/uploads/{proof_img}" if proof_img else ""

        script_obj = {
            "assignment_id": asg["id"],
            "code": asg["code"],
            "domain": asg["domain"] or "",
            "subdomain": asg["subdomain"] or "",
            "specialisation": asg["specialisation"] or "",
            "content_text": content_text,
            "is_submitted": is_submitted,
            "status": asg["status"],
            "review_status": asg["review_status"] or asg["status"],
            "time_taken": time_taken_str,
            "proof_img": proof_img,
            "proof_url": proof_url
        }

        return render(request, "agent_portal.html", u,
                      agent=u,
                      screen="AG3",
                      tab="scripts",
                      error=err,
                      script=script_obj)


@router.post("/agent/submit/{aid}")
async def agent_submit_post(request: Request, aid: int):
    u = gate(request, "participant")
    f = await request.form()
    time_taken = (f.get("time_taken") or "").strip()
    photo_file = f.get("photo")

    # Photo is strictly required
    if not (photo_file and hasattr(photo_file, "filename") and photo_file.filename and photo_file.filename.strip()):
        return back(f"/agent/script/{aid}", e="Submission proof photo is strictly required.")

    # Rule R3: Time taken must be 5 to 15 minutes. Seconds are optional and default to 0,
    # so "7" or "7:" is accepted as 7:00.
    parts = (time_taken or "").split(":")
    try:
        m = int((parts[0] or "0").strip())
        s = int((parts[1].strip() or "0")) if len(parts) > 1 and parts[1].strip() != "" else 0
        total_sec = m * 60 + s
        if total_sec < 300 or total_sec > 900:
            return back(f"/agent/script/{aid}", e="Time must be 5 to 15 minutes")
    except ValueError:
        return back(f"/agent/script/{aid}", e="Time must be 5 to 15 minutes")

    minutes = round(total_sec / 60.0, 2)
    photo_name = ""
    proof_drive_url = ""
    if photo_file and hasattr(photo_file, "filename") and photo_file.filename:
        ext = (os.path.splitext(photo_file.filename)[1] or ".png").lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp", ".heic"):
            return back(f"/agent/script/{aid}", e="Proof must be an image (png, jpg, jpeg, webp).")
        content = await photo_file.read()
        if len(content) > 12 * 1024 * 1024:
            return back(f"/agent/script/{aid}", e="Photo is larger than 12 MB.")
        # Compress captured photo to ~100 KB JPEG before storing (saves storage + bandwidth).
        content = storage.compress_image(content, max_bytes=100 * 1024)
        photo_name = f"proof_{aid}_{int(time.time())}.jpg"
        # Safety copy -> Supabase Storage (+ local cache). Primary copy -> Google Drive (if configured).
        durable, note = storage.save_proof(photo_name, content)
        problems = []
        if not durable:
            print(f"[Storage] {photo_name}: {note}")
            problems.append(("storage", f"Proof photo {photo_name}: Supabase copy failed", note))
        try:
            if drive.is_configured():
                res = drive.upload_recording_file(os.path.join(UPLOADS_DIR, photo_name), remote_name=photo_name)
                proof_drive_url = (res or {}).get("web_view_link", "") if isinstance(res, dict) else ""
        except Exception as drive_err:
            print(f"[Drive] Submission proof upload error: {drive_err}")
            problems.append(("drive", f"Proof photo {photo_name}: Google Drive upload failed", str(drive_err)[:500]))
        if problems:
            # Show up under Developer > Overview > Problems instead of only in the server log.
            try:
                with db.tx() as c:
                    for cat, msg, detail in problems:
                        db.log_app(c, cat, u, msg, detail)
            except Exception:
                pass

    with db.tx() as c:
        try:
            ops.submit_assignment(c, u["id"], aid, minutes, photo_name)
            if proof_drive_url:
                c.execute("UPDATE assignments SET proof_drive_url=? WHERE id=?", (proof_drive_url, aid))
        except ops.OpsError as ex:
            return back(f"/agent/script/{aid}", e=str(ex))

    return RedirectResponse(f"/agent?screen=AG4&aid={aid}", 303)


# ==============================================================================
# GROUP AD. ADMIN & SUPERVISOR PORTAL
# ==============================================================================

def get_script_content_text(c, script_id: int, script_code: str = "") -> str:
    """Returns the complete, raw script text exactly as in the original .txt file, preserving all header lines, metadata, formatting, and dialogue roles without truncation."""
    s = c.execute("SELECT id, code, attrs, brief, language_id FROM scripts WHERE id=?", (script_id,)).fetchone()
    return _script_text(c, s, script_id, script_code)


def _rows_by_id(c, table, ids):
    """SELECT * FROM <table> for many ids at once -> {id: row}."""
    out = {}
    for part in ops._chunks([i for i in dict.fromkeys(ids) if i is not None]):
        ph = ",".join("?" * len(part))
        for r in c.execute(f"SELECT * FROM {table} WHERE id IN ({ph})", tuple(part)).fetchall():
            out[r["id"]] = r
    return out


def _assignments_touching(c, users=(), pairs=(), where="1=1"):
    """Assignments where ua/ub is one of `users` or pair_id is one of `pairs`, each row once, ordered by id."""
    seen = {}
    cols = "id, script_id, pair_id, ua, ub, status, review_status, conf_min"
    for col, ids in (("ua", users), ("ub", users), ("pair_id", pairs)):
        for part in ops._chunks([i for i in dict.fromkeys(ids) if i is not None]):
            ph = ",".join("?" * len(part))
            for r in c.execute(f"SELECT {cols} FROM assignments WHERE {col} IN ({ph}) AND ({where})", tuple(part)).fetchall():
                seen[r["id"]] = r
    return [seen[k] for k in sorted(seen)]


def script_texts_many(c, script_ids):
    """get_script_content_text() for many scripts: two queries instead of two per script. {id: text}."""
    ids = [i for i in dict.fromkeys(script_ids) if i is not None]
    rows, latest = {}, {}
    for part in ops._chunks(ids):
        ph = ",".join("?" * len(part))
        for r in c.execute(f"SELECT id, code, attrs, brief, language_id FROM scripts WHERE id IN ({ph})", tuple(part)).fetchall():
            rows[r["id"]] = r
        for r in c.execute(f"SELECT script_id, version, turns FROM script_versions WHERE script_id IN ({ph}) "
                           f"ORDER BY script_id, version DESC", tuple(part)).fetchall():
            latest.setdefault(r["script_id"], r)
    return {i: _script_text(c, rows.get(i), i, rows[i]["code"] if i in rows else "", latest_version=latest.get(i, False))
            for i in ids}


def _script_text(c, s, script_id, script_code="", latest_version=None):
    """latest_version: prefetched script_versions row, False if known to have none, None to query it."""

    # 1. Check attrs for raw_text or rel_path
    if s and s["attrs"]:
        try:
            attrs = json.loads(s["attrs"])
            if isinstance(attrs, dict):
                # 1a. Direct raw_text stored in attrs
                raw = attrs.get("raw_text")
                if raw and isinstance(raw, str) and raw.strip():
                    return raw.strip()

                # 1b. Read from rel_path
                rel_path = attrs.get("rel_path")
                if rel_path and os.path.isfile(rel_path):
                    with open(rel_path, "r", encoding="utf-8-sig", errors="replace") as fh:
                        content = fh.read().strip()
                        if content:
                            return content

                # 1c. Check filename in known library folders
                fname = attrs.get("filename")
                if fname:
                    for base in [
                        "scripts_library",
                        "Script Factory/scripts_library",
                        os.path.join(os.getcwd(), "scripts_library")
                    ]:
                        if os.path.exists(base):
                            for root, _, files in os.walk(base):
                                if fname in files:
                                    target_p = os.path.join(root, fname)
                                    with open(target_p, "r", encoding="utf-8-sig", errors="replace") as fh:
                                        content = fh.read().strip()
                                        if content:
                                            return content
        except Exception:
            pass

    # 2. Check brief for filename if not found in attrs
    if s and s["brief"]:
        b = s["brief"]
        m = re.search(r"([A-Za-z0-9_\-]+\.txt)", b)
        if m:
            fname = m.group(1)
            for base in ["scripts_library", "Script Factory/scripts_library"]:
                if os.path.exists(base):
                    for root, _, files in os.walk(base):
                        if fname in files:
                            target_p = os.path.join(root, fname)
                            try:
                                with open(target_p, "r", encoding="utf-8-sig", errors="replace") as fh:
                                    content = fh.read().strip()
                                    if content:
                                        return content
                            except Exception:
                                pass

    # 3. Fallback to script_versions turns if no raw file text is available
    if latest_version is None:
        v = c.execute("SELECT turns FROM script_versions WHERE script_id=? ORDER BY version DESC LIMIT 1", (script_id,)).fetchone()
    else:
        v = latest_version or None
    content_text = ""
    if v and v["turns"]:
        try:
            turns = json.loads(v["turns"])
            if isinstance(turns, list) and turns:
                lines = []
                for t in turns:
                    if isinstance(t, dict):
                        spk = t.get("role") or t.get("speaker") or "Speaker"
                        txt = t.get("text", "")
                        lines.append(f"{spk} : {txt}" if txt else spk)
                    else:
                        lines.append(str(t))
                content_text = "\n\n".join(lines)
            else:
                content_text = str(v["turns"])
        except Exception:
            content_text = str(v["turns"])

    if not content_text:
        content_text = f"Script {script_code or (s['code'] if s else '')}"
    return content_text


def build_dashboard_context(c, u, request: Request):
    all_langs = c.execute("SELECT * FROM languages ORDER BY id").fetchall()
    is_sup = u["role"] in ("lead", "supervisor")
    sup_ids = list(u["lang_ids"]) if is_sup else []
    if is_sup:
        lang_list = [l for l in all_langs if l["id"] in sup_ids]
    else:
        lang_list = all_langs

    req_lid = request.query_params.get("lang_id") or request.query_params.get("lang_drill")
    if req_lid and str(req_lid).isdigit():
        req_lid_int = int(req_lid)
        if is_sup:
            selected_lid = req_lid_int if req_lid_int in sup_ids else None
        else:
            selected_lid = req_lid_int
    else:
        selected_lid = None

    # Language cards: each figure is one grouped query for all languages (was 7 queries per language).
    def _per_lang(sql):
        return {r["lid"]: r["v"] for r in c.execute(sql).fetchall()}

    agents_by = _per_lang("SELECT language_id lid, COUNT(*) v FROM users WHERE role='participant' AND active=1 GROUP BY language_id")
    scripts_by = _per_lang("SELECT language_id lid, COUNT(*) v FROM scripts GROUP BY language_id")
    assigned_by = _per_lang(
        "SELECT s.language_id lid, COUNT(DISTINCT a.script_id) v FROM assignments a JOIN scripts s ON s.id=a.script_id "
        "WHERE a.status NOT IN ('RELEASED','ABANDONED','REJECTED') GROUP BY s.language_id")
    completed_by = _per_lang(
        "SELECT s.language_id lid, COUNT(DISTINCT a.script_id) v FROM assignments a JOIN scripts s ON s.id=a.script_id "
        "WHERE (a.status IN ('SUBMITTED','COMPLETED','CONFIRMED','ACCEPTED') OR a.review_status IN ('APPROVED','ACCEPTED')) "
        "AND a.status NOT IN ('RELEASED','ABANDONED') GROUP BY s.language_id")
    sub_where = "a.conf_min IS NOT NULL AND a.status NOT IN ('ASSIGNED','RELEASED','ABANDONED')"
    appr_by = _per_lang(
        f"SELECT s.language_id lid, SUM(conf_min) v FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE {sub_where} "
        f"AND (a.review_status IN ('APPROVED','ACCEPTED') OR a.status IN ('APPROVED','ACCEPTED')) GROUP BY s.language_id")
    rej_by = _per_lang(
        f"SELECT s.language_id lid, SUM(conf_min) v FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE {sub_where} "
        f"AND (a.review_status='REJECTED' OR a.status='REJECTED') GROUP BY s.language_id")
    pend_by = _per_lang(
        f"SELECT s.language_id lid, SUM(conf_min) v FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE {sub_where} "
        f"AND (a.review_status NOT IN ('APPROVED','ACCEPTED','REJECTED') AND a.status NOT IN ('APPROVED','ACCEPTED','REJECTED')) "
        f"GROUP BY s.language_id")

    lang_cards = []
    for l in lang_list:
        lid = l["id"]
        agents_count = agents_by.get(lid, 0)
        total_scripts = scripts_by.get(lid, 0)
        assigned_cnt = assigned_by.get(lid, 0)
        completed_cnt = completed_by.get(lid, 0)
        pending_cnt = max(0, assigned_cnt - completed_cnt)
        appr_min = appr_by.get(lid) or 0.0
        rej_min = rej_by.get(lid) or 0.0
        pend_rev_min = pend_by.get(lid) or 0.0

        appr_h = round(appr_min / 60.0, 2)
        rej_h = round(rej_min / 60.0, 2)
        pend_h = round(pend_rev_min / 60.0, 2)
        # Recorded matches Accepted + Rejected + Review Pending exactly
        recorded_h = round(appr_h + rej_h + pend_h, 2)

        raw_pct = (completed_cnt / total_scripts * 100) if total_scripts else 0
        pct = min(100, round(raw_pct, 1))
        if pct == int(pct):
            pct = int(pct)

        lang_cards.append({
            "id": lid,
            "name": l["name"],
            "code": l["code"],
            "agent_count": agents_count,
            "agents_count": agents_count,
            "total_scripts": total_scripts,
            "assigned_count": assigned_cnt,
            "pending_count": pending_cnt,
            "completed_count": completed_cnt,
            "recorded_hours": recorded_h,
            "total_hours": recorded_h,
            "accepted_hours": appr_h,
            "approved_hours": appr_h,
            "rejected_hours": rej_h,
            "pending_review_hours": pend_h,
            "pct": pct
        })

    req_uid = request.query_params.get("agent_id") or request.query_params.get("agent_drill")
    selected_uid = int(req_uid) if (req_uid and str(req_uid).isdigit()) else None
    current_selected_agent = c.execute("SELECT * FROM users WHERE id=?", (selected_uid,)).fetchone() if selected_uid else None

    if current_selected_agent and not selected_lid:
        ag_lid = current_selected_agent["language_id"]
        if is_sup and ag_lid not in sup_ids:
            selected_uid = None
            current_selected_agent = None
        else:
            selected_lid = ag_lid

    current_selected_lang = c.execute("SELECT * FROM languages WHERE id=?", (selected_lid,)).fetchone() if selected_lid else None

    # Agents working in that language (only loaded if a language is clicked)
    lang_agents = []
    if selected_lid and current_selected_lang:
        agents_raw = c.execute(
            "SELECT u.* FROM users u WHERE u.role='participant' AND u.language_id=? ORDER BY u.active DESC, u.id ASC",
            (selected_lid,)
        ).fetchall()

        # Bulk lookups for every agent in the language (was ~8 queries per agent).
        dash_ids = [ag["id"] for ag in agents_raw]
        dash_pairs = ops.active_pairs_many(c, dash_ids)
        dash_partner_names = {r["id"]: r["name"] for r in _rows_by_id(
            c, "users", [lst[0]["partner_id"] for lst in dash_pairs.values() if lst]).values()}
        dash_first_pid = {uid: (lst[0]["pair_id"] if lst else None) for uid, lst in dash_pairs.items()}
        dash_asg = _assignments_touching(c, users=dash_ids, pairs=[p for p in dash_first_pid.values() if p],
                                         where="status NOT IN ('RELEASED','ABANDONED')")
        dash_idx_u, dash_idx_p = {}, {}
        for a in dash_asg:
            for k in (a["ua"], a["ub"]):
                dash_idx_u.setdefault(k, []).append(a)
            if a["pair_id"] is not None:
                dash_idx_p.setdefault(a["pair_id"], []).append(a)
        dash_hours = ops.HoursBook(c)
        dash_hours.prefetch(users=dash_ids)

        for ag in agents_raw:
            uid = ag["id"]
            first = dash_pairs.get(uid) or []
            partner_name = ""
            part_id = None
            pid = dash_first_pid.get(uid)
            if first:
                part_id = first[0]["partner_id"]
                if part_id in dash_partner_names:
                    partner_name = dash_partner_names[part_id]

            mine = {a["id"]: a for a in dash_idx_u.get(uid, []) + dash_idx_p.get(pid, [])}.values()
            asg_c = sum(1 for a in mine if a["status"] != "REJECTED")   # rows already exclude RELEASED/ABANDONED/NULL
            cmp_c = sum(1 for a in mine if a["status"] in ('SUBMITTED', 'COMPLETED', 'CONFIRMED', 'ACCEPTED')
                        or a["review_status"] in ('APPROVED', 'ACCEPTED'))
            pnd_c = max(0, asg_c - cmp_c)
            uh = dash_hours.user(uid)
            rec_h = uh["recorded_h"]
            acc_h = uh["accepted_h"]
            if not ag["active"]:
                ag_status = "Inactive"
            elif uh["limit_reached"]:
                ag_status = "Limit reached"
            elif partner_name:
                ag_status = "Paired"
            else:
                ag_status = "Available"

            lang_agents.append({
                "id": uid,
                "language_id": selected_lid,
                "code": ag["code"],
                "name": ag["name"],
                "phone": ag["phone"] or "-",
                "email": ag["email"] or "-",
                "gender": ag["gender"] or "-",
                "availability": ag["availability"] or "All day",
                "partner_id": part_id,
                "partner_name": partner_name,
                "status": ag_status,
                "assigned_count": asg_c,
                "pending_count": pnd_c,
                "completed_count": cmp_c,
                "recorded_h": rec_h,
                "accepted_h": acc_h,
                "total_min": round(uh["recorded_min"], 1),
                "approved_min": round(uh["accepted_min"], 1),
                "active": ag["active"]
            })

    # Complete Agent Work History & KPIs
    agent_scripts = []
    agent_history_kpis = {
        "total_assigned": 0,
        "completed": 0,
        "pending": 0,
        "accepted": 0,
        "recorded_h": 0.0,
        "accepted_h": 0.0,
        "pct_15h": 0,
        "partner_id": None,
        "partner_name": "-"
    }
    if selected_uid and current_selected_agent:
        p_row = c.execute("SELECT id, a, b FROM pairs WHERE status='ACTIVE' AND (a=? OR b=?)", (selected_uid, selected_uid)).fetchone()
        cur_partner_name = ""
        cur_partner_id = None
        if p_row:
            cur_partner_id = p_row["b"] if p_row["a"] == selected_uid else p_row["a"]
            part_u = c.execute("SELECT name FROM users WHERE id=?", (cur_partner_id,)).fetchone()
            if part_u:
                cur_partner_name = part_u["name"]

        # Fetch COMPLETE HISTORY of scripts worked on by this agent across all pairs and sessions
        rows = c.execute(
            "SELECT a.id, a.script_id, a.conf_min, a.status, a.review_status, a.review_note, a.submitted_at, a.assigned_at, "
            "s.code, s.domain, s.subdomain, s.specialisation, "
            "p.id as pair_id, "
            "CASE "
            "    WHEN a.ua = ? THEN ub.name "
            "    WHEN a.ub = ? THEN ua.name "
            "    WHEN p.a = ? THEN u2.name "
            "    ELSE u1.name "
            "END as worked_with_name, "
            "CASE "
            "    WHEN a.ua = ? THEN ub.id "
            "    WHEN a.ub = ? THEN ua.id "
            "    WHEN p.a = ? THEN u2.id "
            "    ELSE u1.id "
            "END as worked_with_id "
            "FROM assignments a "
            "JOIN scripts s ON s.id=a.script_id "
            "LEFT JOIN pairs p ON p.id=a.pair_id "
            "LEFT JOIN users ua ON ua.id=a.ua "
            "LEFT JOIN users ub ON ub.id=a.ub "
            "LEFT JOIN users u1 ON u1.id=p.a "
            "LEFT JOIN users u2 ON u2.id=p.b "
            "WHERE (a.ua=? OR a.ub=? OR a.pair_id IN (SELECT id FROM pairs WHERE a=? OR b=?)) "
            "AND a.status NOT IN ('RELEASED','ABANDONED') "
            "ORDER BY a.id DESC",
            (selected_uid, selected_uid, selected_uid, selected_uid, selected_uid, selected_uid, selected_uid, selected_uid, selected_uid, selected_uid)
        ).fetchall()

        now = time.time()
        history_texts = script_texts_many(c, [r["script_id"] for r in rows])
        for r in rows:
            m, s = divmod(int(round(float(r["conf_min"] or 0) * 60)), 60)
            days = max(0, int((now - r["submitted_at"]) / 86400)) if r["submitted_at"] else 0
            dialogue = history_texts[r["script_id"]]

            rev_stat = (r["review_status"] or "").upper()
            raw_stat = (r["status"] or "").upper()
            if rev_stat in ("APPROVED", "ACCEPTED") or raw_stat in ("APPROVED", "ACCEPTED", "CONFIRMED"):
                stat = "ACCEPTED"
            elif rev_stat == "REJECTED" or raw_stat == "REJECTED":
                stat = "REJECTED"
            elif raw_stat in ("SUBMITTED", "COMPLETED") or r["conf_min"]:
                stat = "SUBMITTED"
            elif raw_stat in ("ASSIGNED", "IN_SESSION"):
                stat = "ASSIGNED"
            else:
                stat = raw_stat or "ASSIGNED"

            partner = r["worked_with_name"] or cur_partner_name or "Solo"
            partner_uid = r["worked_with_id"] or cur_partner_id
            agent_scripts.append({
                "id": r["id"],
                "script_id": r["script_id"],
                "code": r["code"],
                "worked_with": partner,
                "worked_with_id": partner_uid,
                "domain": r["domain"],
                "subdomain": r["subdomain"],
                "specialisation": r["specialisation"],
                "taxonomy": f"{r['domain']} > {r['subdomain']} > {r['specialisation']}",
                "time_taken": f"{m:02d}:{s:02d}" if r["conf_min"] else "-",
                "status": stat,
                "review_status": stat,
                "raw_status": r["status"],
                "review_note": r["review_note"] or "-",
                "days_since_submitted": days,
                "dialogue": dialogue
            })

        tot_asg = len(agent_scripts)
        tot_comp = len([s for s in agent_scripts if s["status"] in ("SUBMITTED", "COMPLETED", "ACCEPTED")])
        tot_pend = max(0, tot_asg - tot_comp)
        tot_acc = len([s for s in agent_scripts if s["status"] == "ACCEPTED"])

        uh = ops.get_hours(c, "user", selected_uid)

        agent_history_kpis = {
            "total_assigned": tot_asg,
            "completed": tot_comp,
            "pending": tot_pend,
            "accepted": tot_acc,
            "recorded_h": uh["recorded_h"],
            "accepted_h": uh["accepted_h"],
            "pct_15h": min(100, round((uh["recorded_h"]) / (uh["cap_h"] or 15.0) * 100)),
            "partner_id": cur_partner_id,
            "partner_name": cur_partner_name or "Unpaired"
        }

    if selected_lid and selected_uid and current_selected_agent:
        current_view = "scripts"
    elif selected_lid and current_selected_lang:
        current_view = "agents"
    else:
        current_view = "languages"

    selected_card = next((l for l in lang_cards if l["id"] == selected_lid), None)
    agent_info = next((ag for ag in lang_agents if ag["id"] == selected_uid), None)

    return {
        "current_view": current_view,
        "selected_card": selected_card,
        "agent_info": agent_info,
        "agent_history_kpis": agent_history_kpis,
        "lang_cards": lang_cards,
        "selected_lang": current_selected_lang,
        "current_selected_lang": current_selected_lang,
        "lang_agents": lang_agents,
        "selected_agent": current_selected_agent,
        "agent_scripts": agent_scripts,
        "selected_lid": selected_lid,
        "selected_uid": selected_uid
    }


def build_management_context(c, u, request: Request, active_tab: str = "agents"):
    all_langs = c.execute("SELECT * FROM languages ORDER BY id").fetchall()
    is_sup = u["role"] in ("lead", "supervisor")
    allowed_ids = [l["id"] for l in all_langs] if not is_sup else [x for x in u["lang_ids"] if any(l["id"] == x for l in all_langs)]
    if not allowed_ids and all_langs:
        allowed_ids = [all_langs[0]["id"]]
    req_lid = request.query_params.get("lang")
    cookie_lang = request.cookies.get("sf_lang")
    pick = req_lid if req_lid not in (None, "") else cookie_lang
    all_mode = False
    if pick == "all":
        all_mode = True
        selected_lids = list(allowed_ids)
        selected_lid = selected_lids[0] if selected_lids else 1
    elif pick and str(pick).isdigit() and int(pick) in allowed_ids:
        selected_lid = int(pick)
        selected_lids = [selected_lid]
    elif is_sup and len(allowed_ids) > 1:
        all_mode = True  # multi-language supervisor with no explicit pick -> see everything they own
        selected_lids = list(allowed_ids)
        selected_lid = selected_lids[0]
    else:
        selected_lid = allowed_ids[0] if allowed_ids else 1
        selected_lids = [selected_lid]

    # every "language_id IN (SELECT id FROM _sel)" below is scoped through this temp table so one code path serves single + all-language views
    c.execute("CREATE TEMP TABLE IF NOT EXISTS _sel(id INTEGER)")
    c.execute("DELETE FROM _sel")
    for _x in selected_lids:
        c.execute("INSERT INTO _sel(id) VALUES(?)", (_x,))

    current_L = c.execute("SELECT * FROM languages WHERE id=?", (selected_lid,)).fetchone()
    if all_mode:
        current_L = {"id": "all", "name": "All Languages", "code": "ALL", "cap_pair_min": 450.0, "cap_person_min": 900.0}
    status_filter = request.query_params.get("status", "ALL").upper()
    taxonomy_filter = (request.query_params.get("taxonomy") or "").strip()
    distinct_taxonomies = c.execute(
        "SELECT DISTINCT domain, subdomain FROM scripts WHERE language_id IN (SELECT id FROM _sel) ORDER BY domain, subdomain",
        ()
    ).fetchall()

    # AD2 Agents Table
    agents_raw = c.execute(
        "SELECT u.*, l.name as lang_name FROM users u LEFT JOIN languages l ON l.id=u.language_id "
        "WHERE u.role='participant' AND u.language_id IN (SELECT id FROM _sel) ORDER BY u.active DESC, u.id ASC",
        ()
    ).fetchall()
    # Everything per-agent is fetched once up front (it used to be ~8 queries per agent).
    hours = ops.HoursBook(c)
    agent_ids = [ag["id"] for ag in agents_raw]
    pairs_by_uid = ops.active_pairs_many(c, agent_ids)
    partner_ids = {x["partner_id"] for lst in pairs_by_uid.values() for x in lst}
    users_by_id = _rows_by_id(c, "users", partner_ids)
    hours.prefetch(users=list(agent_ids) + list(partner_ids))
    first_pid = {uid: (lst[0]["pair_id"] if lst else None) for uid, lst in pairs_by_uid.items()}
    agent_asg = _assignments_touching(c, users=agent_ids, pairs=[p for p in first_pid.values() if p],
                                      where="status NOT IN ('RELEASED','ABANDONED')")
    agent_asg_by_id = {a["id"]: a for a in agent_asg}
    agent_asg_idx_u, agent_asg_idx_p = {}, {}
    for a in agent_asg:
        for k in (a["ua"], a["ub"]):
            agent_asg_idx_u.setdefault(k, set()).add(a["id"])
        agent_asg_idx_p.setdefault(a["pair_id"], set()).add(a["id"])
    agent_asg_idx_p.pop(None, None)
    agents = []
    for ag in agents_raw:
        uid = ag["id"]
        my_pairs = pairs_by_uid.get(uid, [])
        p = {"id": my_pairs[0]["pair_id"]} if my_pairs else None
        partner_id = my_pairs[0]["partner_id"] if my_pairs else None
        partner_name = my_pairs[0]["partner_name"] if my_pairs else ""
        partner_phone = my_pairs[0]["partner_phone"] if my_pairs else "-"
        partner_email = "-"
        partner_gender = "-"
        partner_avail = "All day"
        partner_rec_h = 0.0
        partner_status = "Paired"
        pid = p["id"] if p else None
        if partner_id:
            p_row = users_by_id.get(partner_id)
            if p_row:
                partner_email = p_row["email"] or "-"
                partner_gender = p_row["gender"] or "-"
                partner_avail = p_row["availability"] or "All day"
                partner_status = "Active" if p_row["active"] else "Inactive"
                partner_rec_h = hours.user(partner_id)["recorded_h"]
        partner_cards = []
        for x in my_pairs:
            pr = users_by_id.get(x["partner_id"])
            if not pr:
                continue
            partner_cards.append({
                "name": pr["name"], "phone": pr["phone"] or "-", "email": pr["email"] or "-", "gender": pr["gender"] or "-",
                "availability": pr["availability"] or "All day", "status": "Active" if pr["active"] else "Inactive",
                "recorded_h": hours.user(pr["id"])["recorded_h"], "language": ag["lang_name"] or "-",
            })

        # Accurate individual stats across all assignments for this agent (persists across pair deletion/recreation)
        mine = [agent_asg_by_id[i] for i in (agent_asg_idx_u.get(uid, set()) | agent_asg_idx_p.get(pid, set()))]
        asg_c = len(mine)
        cmp_c = sum(1 for a in mine if a["status"] in ('SUBMITTED', 'COMPLETED', 'CONFIRMED', 'ACCEPTED')
                    or a["review_status"] in ('APPROVED', 'ACCEPTED'))
        pnd_c = max(0, asg_c - cmp_c)
        uh = hours.user(uid)

        agents.append({
            "id": uid,
            "language_id": ag["language_id"] or selected_lid,
            "code": ag["code"],
            "name": ag["name"],
            "lang_name": ag["lang_name"],
            "phone": ag["phone"],
            "email": ag["email"],
            "gender": ag["gender"] or "-",
            "availability": ag["availability"] or "All day",
            "partner_id": partner_id,
            "partner_name": partner_name,
            "partner_phone": partner_phone,
            "partner_email": partner_email,
            "partner_gender": partner_gender,
            "partner_availability": partner_avail,
            "partner_recorded_h": partner_rec_h,
            "partner_status": partner_status,
            "pair_count": len(my_pairs),
            "partners": [x["partner_name"] for x in my_pairs],
            "partners_tooltip": ", ".join(x["partner_name"] for x in my_pairs),
            "partner_cards": partner_cards,
            "assigned_count": asg_c,
            "completed_count": cmp_c,
            "pending_count": pnd_c,
            "recorded_h": uh["recorded_h"],
            "accepted_h": uh["accepted_h"],
            "active": ag["active"]
        })

    # Agents KPI Metrics (Top cards: Total People, Active Agents, Paired Up, Unpaired)
    tot_people = c.execute("SELECT COUNT(*) n FROM users WHERE role='participant' AND language_id IN (SELECT id FROM _sel)", ()).fetchone()["n"]
    act_agents = c.execute("SELECT COUNT(*) n FROM users WHERE role='participant' AND language_id IN (SELECT id FROM _sel) AND active=1", ()).fetchone()["n"]
    paired_users = c.execute(
        "SELECT DISTINCT u.id FROM users u JOIN pairs p ON (p.a=u.id OR p.b=u.id) "
        "WHERE u.role='participant' AND u.language_id IN (SELECT id FROM _sel) AND u.active=1 AND p.status='ACTIVE'",
        ()
    ).fetchall()
    paired_cnt = len(paired_users)
    unpaired_cnt = max(0, act_agents - paired_cnt)

    agents_kpis = {
        "total_people": tot_people,
        "active_agents": act_agents,
        "paired_count": paired_cnt,
        "unpaired_count": unpaired_cnt
    }

    # Status filter for Agents
    if status_filter == "ACTIVE":
        agents = [a for a in agents if a["active"] == 1]
    elif status_filter == "INACTIVE":
        agents = [a for a in agents if a["active"] == 0]
    elif status_filter == "PAIRED":
        agents = [a for a in agents if a["partner_name"]]
    elif status_filter == "UNPAIRED":
        agents = [a for a in agents if not a["partner_name"]]

    # AD3 Scripts Table & KPIs
    tot_scripts = c.execute("SELECT COUNT(*) n FROM scripts WHERE language_id IN (SELECT id FROM _sel)", ()).fetchone()["n"]
    avail_cnt = c.execute(
        "SELECT COUNT(*) n FROM scripts s WHERE s.language_id IN (SELECT id FROM _sel) AND s.status IN ('READY','AVAILABLE','APPROVED') "
        "AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED','REJECTED'))",
        ()
    ).fetchone()["n"]
    asg_cnt = c.execute(
        "SELECT COUNT(DISTINCT a.script_id) n FROM assignments a "
        "JOIN scripts s ON s.id=a.script_id "
        "WHERE s.language_id IN (SELECT id FROM _sel) AND a.status NOT IN ('RELEASED','ABANDONED','REJECTED')",
        ()
    ).fetchone()["n"]
    cmp_cnt = c.execute(
        "SELECT COUNT(DISTINCT a.script_id) n FROM assignments a "
        "JOIN scripts s ON s.id=a.script_id "
        "WHERE s.language_id IN (SELECT id FROM _sel) "
        "AND (a.status IN ('SUBMITTED','COMPLETED','CONFIRMED','ACCEPTED') OR a.review_status IN ('APPROVED','ACCEPTED')) "
        "AND a.status NOT IN ('RELEASED','ABANDONED')",
        ()
    ).fetchone()["n"]
    acc_cnt = c.execute(
        "SELECT COUNT(DISTINCT a.script_id) n FROM assignments a "
        "JOIN scripts s ON s.id=a.script_id "
        "WHERE s.language_id IN (SELECT id FROM _sel) "
        "AND (a.status='ACCEPTED' OR a.review_status IN ('APPROVED','ACCEPTED')) "
        "AND a.status NOT IN ('RELEASED','ABANDONED')",
        ()
    ).fetchone()["n"]
    pnd_cnt = max(0, asg_cnt - cmp_cnt)

    scripts_kpi = {
        "total": tot_scripts,
        "available": avail_cnt,
        "assigned": asg_cnt,
        "pending": pnd_cnt,
        "completed": cmp_cnt,
        "accepted": acc_cnt
    }

    scripts_raw = c.execute(
        "SELECT s.*, l.name as lang_name FROM scripts s JOIN languages l ON l.id=s.language_id "
        "WHERE s.language_id IN (SELECT id FROM _sel) ORDER BY s.id DESC",
        ()
    ).fetchall()
    # Latest open assignment per script, all scripts in one query (was one query per script).
    latest_asg = {}
    for r in c.execute(
            "SELECT a.script_id as sid, p.id as pid, p.availability as p_avail, "
            "u1.id as p_uid_a, u1.name as p_name_a, u1.phone as p_phone_a, "
            "u2.id as p_uid_b, u2.name as p_name_b, u2.phone as p_phone_b, "
            "ua.id as uid_a, ua.name as name_a, ua.phone as phone_a, "
            "ub.id as uid_b, ub.name as name_b, ub.phone as phone_b, "
            "a.conf_min, a.status as a_status, a.review_status "
            "FROM assignments a "
            "JOIN scripts s ON s.id=a.script_id "
            "LEFT JOIN pairs p ON p.id=a.pair_id "
            "LEFT JOIN users u1 ON u1.id=p.a "
            "LEFT JOIN users u2 ON u2.id=p.b "
            "LEFT JOIN users ua ON ua.id=a.ua "
            "LEFT JOIN users ub ON ub.id=a.ub "
            "WHERE s.language_id IN (SELECT id FROM _sel) AND a.status NOT IN ('RELEASED','ABANDONED','REJECTED') "
            "ORDER BY a.script_id, a.id DESC", ()).fetchall():
        latest_asg.setdefault(r["sid"], r)
    hours.prefetch(pairs=[r["pid"] for r in latest_asg.values() if r["pid"]])
    # Script text is not embedded in the page any more: the popup fetches /admin/scripts/text on open.
    scripts_list = []
    for sc in scripts_raw:
        asg = latest_asg.get(sc["id"])
        pair_phone_a = "-"
        pair_phone_b = "-"
        pair_avail = "All day"
        pair_rec_h = 0.0
        sc_uid_a = None
        sc_uid_b = None
        sc_name_a = ""
        sc_name_b = ""
        if asg:
            sc_uid_a = asg["p_uid_a"] or asg["uid_a"]
            sc_uid_b = asg["p_uid_b"] or asg["uid_b"]
            na = asg["p_name_a"] or asg["name_a"] or ""
            nb = asg["p_name_b"] or asg["name_b"] or ""
            sc_name_a = na
            sc_name_b = nb
            pair_phone_a = asg["p_phone_a"] or asg["phone_a"] or "-"
            pair_phone_b = asg["p_phone_b"] or asg["phone_b"] or "-"
            pair_avail = asg["p_avail"] or "All day"
            if asg["pid"]:
                pair_rec_h = hours.pair(asg["pid"])["recorded_h"]
            if na and nb:
                pair_label = f"{na} & {nb}"
            elif na or nb:
                pair_label = na or nb
            else:
                pair_label = "Assigned"

            if asg and asg["conf_min"]:
                m, s = divmod(int(round(float(asg["conf_min"] or 0) * 60)), 60)
                time_taken = f"{m:02d}:{s:02d}"
            else:
                time_taken = "-"

            rev_stat = (asg["review_status"] or "").upper()
            raw_stat = (asg["a_status"] or "").upper()
            if rev_stat in ("APPROVED", "ACCEPTED") or raw_stat in ("APPROVED", "ACCEPTED", "CONFIRMED"):
                status_disp = "ACCEPTED"
            elif rev_stat == "REJECTED" or raw_stat == "REJECTED":
                status_disp = "REJECTED"
            elif raw_stat in ("SUBMITTED", "COMPLETED"):
                status_disp = "SUBMITTED"
            elif raw_stat in ("ASSIGNED", "IN_SESSION"):
                status_disp = "ASSIGNED"
            else:
                status_disp = raw_stat or "AVAILABLE"
        else:
            pair_label = "-"
            time_taken = "-"
            status_disp = "AVAILABLE" if sc["status"] in ("READY", "AVAILABLE") else sc["status"]

        dialogue = ""
        scripts_list.append({
            "id": sc["id"],
            "language_id": sc["language_id"],
            "code": sc["code"],
            "lang_name": sc["lang_name"],
            "domain": sc["domain"],
            "subdomain": sc["subdomain"],
            "specialisation": sc["specialisation"],
            "taxonomy": f"{sc['domain']} > {sc['subdomain']} > {sc['specialisation']}",
            "words": sc["words"],
            "status": status_disp,
            "time_taken": time_taken,
            "uid_a": sc_uid_a,
            "uid_b": sc_uid_b,
            "name_a": sc_name_a,
            "name_b": sc_name_b,
            "pair_names": pair_label,
            "pair_label": pair_label,
            "pair_phone_a": pair_phone_a,
            "pair_phone_b": pair_phone_b,
            "pair_avail": pair_avail,
            "pair_rec_h": pair_rec_h,
            "dialogue": dialogue
        })

    # Status filter for Scripts
    if status_filter == "AVAILABLE":
        scripts_list = [s for s in scripts_list if s["status"] == "AVAILABLE"]
    elif status_filter == "ASSIGNED":
        scripts_list = [s for s in scripts_list if s["status"] in ("ASSIGNED", "IN_SESSION")]
    elif status_filter == "SUBMITTED":
        scripts_list = [s for s in scripts_list if s["status"] in ("SUBMITTED", "COMPLETED")]
    elif status_filter in ("ACCEPTED", "APPROVED"):
        scripts_list = [s for s in scripts_list if s["status"] in ("ACCEPTED", "APPROVED")]

    if taxonomy_filter and taxonomy_filter != "ALL":
        scripts_list = [s for s in scripts_list if taxonomy_filter.lower() in f"{s['domain']} > {s['subdomain']}".lower()]

    # AD4 Pairs Table
    pairs_raw = c.execute(
        "SELECT p.*, ua.name as name_a, ua.gender as gender_a, ua.phone as phone_a, ua.email as email_a, ua.availability as avail_a, "
        "ub.name as name_b, ub.gender as gender_b, ub.phone as phone_b, ub.email as email_b, ub.availability as avail_b, l.name as lang_name "
        "FROM pairs p JOIN users ua ON ua.id=p.a JOIN users ub ON ub.id=p.b JOIN languages l ON l.id=p.language_id "
        "WHERE p.language_id IN (SELECT id FROM _sel) AND p.status='ACTIVE' ORDER BY p.id DESC",
        ()
    ).fetchall()
    hours.prefetch(users=[p["a"] for p in pairs_raw] + [p["b"] for p in pairs_raw], pairs=[p["id"] for p in pairs_raw])
    pair_asg = _assignments_touching(c, users=[p["a"] for p in pairs_raw] + [p["b"] for p in pairs_raw],
                                     pairs=[p["id"] for p in pairs_raw])
    pair_asg_by_pid, pair_asg_by_couple = {}, {}
    for a in pair_asg:
        pair_asg_by_pid.setdefault(a["pair_id"], []).append(a)
        pair_asg_by_couple.setdefault(frozenset((a["ua"], a["ub"])), []).append(a)
    pairs = []
    for p in pairs_raw:
        pid = p["id"]
        # Joint stats for this pair (preserves history if pair was dropped and recreated)
        joint = {a["id"]: a for a in pair_asg_by_pid.get(pid, [])}
        for a in pair_asg_by_couple.get(frozenset((p["a"], p["b"])), []):
            if (a["ua"], a["ub"]) in ((p["a"], p["b"]), (p["b"], p["a"])):
                joint[a["id"]] = a
        a_c = sum(1 for a in joint.values()   # NULL status never matches NOT IN in SQL; keep that
                  if a["status"] is not None and a["status"] not in ('RELEASED', 'ABANDONED', 'REJECTED'))
        c_c = sum(1 for a in joint.values() if a["status"] in ('SUBMITTED', 'COMPLETED', 'CONFIRMED', 'ACCEPTED')
                  or a["review_status"] in ('APPROVED', 'ACCEPTED'))
        p_c = max(0, a_c - c_c)
        ph = hours.pair(pid)
        ha = hours.user(p["a"])
        hb = hours.user(p["b"])

        pairs.append({
            "id": pid,
            "a": p["a"],
            "b": p["b"],
            "language_id": p["language_id"] or selected_lid,
            "agent_a_name": p["name_a"],
            "agent_a_gender": p["gender_a"] or "-",
            "agent_a_phone": p["phone_a"] or "-",
            "agent_a_email": p["email_a"] or "-",
            "agent_a_avail": p["avail_a"] or p["availability"] or "All day",
            "agent_b_name": p["name_b"],
            "agent_b_gender": p["gender_b"] or "-",
            "agent_b_phone": p["phone_b"] or "-",
            "agent_b_email": p["email_b"] or "-",
            "agent_b_avail": p["avail_b"] or p["availability"] or "All day",
            "name_a": p["name_a"],
            "name_b": p["name_b"],
            "rec_a_h": ha["recorded_h"],
            "rec_b_h": hb["recorded_h"],
            "lang_name": p["lang_name"],
            "availability": p["availability"] or "All day",
            "assigned_count": a_c,
            "completed_count": c_c,
            "pending_count": p_c,
            "recorded_h": ph["recorded_h"],
            "accepted_h": ph["accepted_h"],
            "status": "LIMIT_REACHED" if ph["limit_reached"] else p["status"]
        })

    # All active agents in this language for manual pairing (irrespective of paired status)
    all_active_agents = c.execute(
        "SELECT u.*, l.name as lang_name FROM users u JOIN languages l ON l.id=u.language_id "
        "WHERE u.role='participant' AND u.language_id IN (SELECT id FROM _sel) AND u.active=1 ORDER BY u.name ASC",
        ()
    ).fetchall()
    manual_ids = [ag["id"] for ag in all_active_agents]
    manual_pairs = ops.active_pairs_many(c, [i for i in manual_ids if i not in pairs_by_uid])
    manual_pairs.update({i: pairs_by_uid[i] for i in manual_ids if i in pairs_by_uid})
    hours.prefetch(users=manual_ids)
    manual_agents = []
    for ag in all_active_agents:
        ap = manual_pairs[ag["id"]]
        ag_h = hours.user(ag["id"])
        rec_h = ag_h["recorded_h"]
        pct = min(100, round(ag_h["committed_h"] / (ag_h["cap_h"] or 15.0) * 100))
        names = ", ".join(x["partner_name"] for x in ap)
        manual_agents.append({
            "id": ag["id"],
            "code": ag["code"],
            "phone": ag["phone"],
            "name": ag["name"],
            "gender": ag["gender"] or "-",
            "availability": ag["availability"] or "All day",
            "language_id": ag["language_id"],
            "lang_name": ag["lang_name"],
            "is_paired": bool(ap),
            "pair_count": len(ap),
            "partner_name": names,
            "pair_note": (f" (paired with {names})" if ap else " (Unpaired)"),
            "recorded_h": rec_h,
            "committed_h": ag_h["committed_h"],
            "cap_reached": bool(ag_h["limit_reached"]),
            "pct_15h": pct
        })

    # Sort: Unpaired agents FIRST in alphabetical order, then Paired agents in alphabetical order
    manual_agents.sort(key=lambda a: (1 if a["is_paired"] else 0, a["name"].lower()))

    # Pairs KPIs (Includes Total Agents as first card)
    active_pairs_cnt = len([p for p in pairs if p["status"] == "ACTIVE" and p["recorded_h"] < 7.5])
    limit_reached_pairs_cnt = len([p for p in pairs if p["recorded_h"] >= 7.5])
    pairs_kpis = {
        "total_agents": act_agents,
        "total_pairs": len(pairs),
        "active_pairs": active_pairs_cnt,
        "limit_reached": limit_reached_pairs_cnt,
        "paired_count": paired_cnt,
        "unpaired_count": unpaired_cnt
    }

    # AD5 Assignment calculation & KPIs
    kpi_total_scripts = c.execute("SELECT COUNT(*) n FROM scripts WHERE language_id IN (SELECT id FROM _sel)", ()).fetchone()["n"]
    kpi_available_to_assign = c.execute(
        "SELECT COUNT(*) n FROM scripts s WHERE s.language_id IN (SELECT id FROM _sel) AND s.status IN ('READY','AVAILABLE','APPROVED') "
        "AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED','REJECTED'))",
        ()
    ).fetchone()["n"]
    kpi_assigned_scripts = c.execute(
        "SELECT COUNT(DISTINCT a.script_id) n FROM assignments a JOIN scripts s ON s.id=a.script_id "
        "WHERE s.language_id IN (SELECT id FROM _sel) AND a.status NOT IN ('RELEASED','ABANDONED','REJECTED')",
        ()
    ).fetchone()["n"]
    kpi_completed_scripts = c.execute(
        "SELECT COUNT(DISTINCT a.script_id) n FROM assignments a JOIN scripts s ON s.id=a.script_id "
        "WHERE s.language_id IN (SELECT id FROM _sel) AND (a.status IN ('COMPLETED','CONFIRMED','SUBMITTED','ACCEPTED') OR a.review_status IN ('APPROVED','ACCEPTED')) AND a.status NOT IN ('RELEASED','ABANDONED')",
        ()
    ).fetchone()["n"]

    assignable_scripts = kpi_available_to_assign
    active_eligible_pairs = [p for p in pairs if p["status"] == "ACTIVE" and p["recorded_h"] < 7.5]
    assignment_kpis = {
        "total_scripts": kpi_total_scripts,
        "available_scripts": kpi_available_to_assign,
        "available_to_assign": kpi_available_to_assign,
        "assigned_scripts": kpi_assigned_scripts,
        "completed_scripts": kpi_completed_scripts,
        "active_pairs": len(active_eligible_pairs),
        "total_assigned": kpi_assigned_scripts,
        "pending_scripts": pnd_cnt,
        "accepted_scripts": acc_cnt
    }

    # Filter pairs and assign_pairs by status_filter
    assign_pairs = list(pairs)
    if active_tab in ("pairs", "assignment"):
        if status_filter == "ACTIVE":
            pairs = [p for p in pairs if p["status"] == "ACTIVE" and p["recorded_h"] < 7.5]
            assign_pairs = [p for p in assign_pairs if p["status"] == "ACTIVE" and p["recorded_h"] < 7.5]
        elif status_filter in ("LIMIT_REACHED", "LIMIT"):
            pairs = [p for p in pairs if p["recorded_h"] >= 7.5]
            assign_pairs = [p for p in assign_pairs if p["recorded_h"] >= 7.5]

    # AD6 Reports Table & KPIs: Only show actual submissions (not unrecorded assignments)
    where_sql = "WHERE s.language_id IN (SELECT id FROM _sel) AND a.status NOT IN ('ASSIGNED','RELEASED','ABANDONED','IN_SESSION') AND a.conf_min IS NOT NULL"
    params = []
    if status_filter == "SUBMITTED":
        where_sql += " AND a.status IN ('SUBMITTED','COMPLETED') AND (a.review_status='PENDING' OR a.review_status IS NULL)"
    elif status_filter in ("ACCEPTED", "APPROVED"):
        where_sql += " AND (a.status='ACCEPTED' OR a.review_status IN ('APPROVED','ACCEPTED'))"
    elif status_filter == "REJECTED":
        where_sql += " AND (a.status='REJECTED' OR a.review_status='REJECTED')"

    reports_raw = c.execute(
        f"SELECT a.id, a.conf_min, a.proof_img, a.submitted_at, a.review_status, a.review_note, a.note, a.status as a_status, a.pair_id, "
        f"s.language_id as lang_id, s.id as script_id, s.code as script_code, s.domain, s.subdomain, s.specialisation, s.words as script_words, "
        f"ua.id as uid_a, ua.name as name_a, ua.phone as phone_a, ua.email as email_a, ua.gender as gender_a, ua.availability as avail_a, "
        f"ub.id as uid_b, ub.name as name_b, ub.phone as phone_b, ub.email as email_b, ub.gender as gender_b, ub.availability as avail_b, "
        f"l.name as lang_name "
        f"FROM assignments a JOIN scripts s ON s.id=a.script_id JOIN languages l ON l.id=s.language_id "
        f"LEFT JOIN users ua ON ua.id=a.ua LEFT JOIN users ub ON ub.id=a.ub "
        f"{where_sql} ORDER BY a.updated_at DESC",
        params
    ).fetchall()

    reports = []
    now = time.time()
    hours.prefetch(users=[r["uid_a"] for r in reports_raw if r["uid_a"]] + [r["uid_b"] for r in reports_raw if r["uid_b"]])
    for r in reports_raw:
        m, s = divmod(int(round(float(r["conf_min"] or 0) * 60)), 60)
        days = max(0, int((now - r["submitted_at"]) / 86400)) if r["submitted_at"] else 0
        dialogue = ""  # loaded on demand by the popup
        taxonomy = f"{r['domain']} > {r['subdomain']} > {r['specialisation']}"
        rev_note = (r["review_note"] or "").strip()
        if "Supervisor" in rev_note:
            disp_reviewer = "Supervisor"
        elif "System Admin" in rev_note or "Admin" in rev_note:
            disp_reviewer = "System Admin"
        elif rev_note and not rev_note.startswith("<sqlite3"):
            disp_reviewer = rev_note
        elif r["review_status"] in ("APPROVED", "ACCEPTED") or r["a_status"] in ("APPROVED", "ACCEPTED"):
            disp_reviewer = "System Admin"
        else:
            disp_reviewer = "-"

        rec_a_m = hours.user(r["uid_a"])["recorded_min"] if r["uid_a"] else 0.0
        rec_b_m = hours.user(r["uid_b"])["recorded_min"] if r["uid_b"] else 0.0

        reports.append({
            "id": r["id"],
            "language_id": r["lang_id"],
            "script_id": r["script_id"],
            "script_code": r["script_code"],
            "pair_id": r["pair_id"],
            "uid_a": r["uid_a"],
            "agent_a_name": r["name_a"] or "Speaker A",
            "agent_a_phone": r["phone_a"] or "-",
            "agent_a_email": r["email_a"] or "-",
            "agent_a_gender": r["gender_a"] or "-",
            "agent_a_avail": r["avail_a"] or "All day",
            "agent_a_rec_h": round(rec_a_m / 60.0, 2),
            "uid_b": r["uid_b"],
            "agent_b_name": r["name_b"] or "Speaker B",
            "agent_b_phone": r["phone_b"] or "-",
            "agent_b_email": r["email_b"] or "-",
            "agent_b_gender": r["gender_b"] or "-",
            "agent_b_avail": r["avail_b"] or "All day",
            "agent_b_rec_h": round(rec_b_m / 60.0, 2),
            "lang_name": r["lang_name"],
            "domain": r["domain"],
            "subdomain": r["subdomain"],
            "specialisation": r["specialisation"],
            "words": r["script_words"] or 0,
            "taxonomy": taxonomy,
            "dialogue": dialogue,
            "time_taken": f"{m:02d}:{s:02d}" if r["conf_min"] else "-",
            "proof_img": r["proof_img"],
            "days_since_submitted": days,
            "review_status": r["review_status"] or r["a_status"],
            "reviewed_by": disp_reviewer,
            "review_note": r["note"] or ""
        })

    tot_reports = c.execute("SELECT COUNT(*) n FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE s.language_id IN (SELECT id FROM _sel) AND a.conf_min IS NOT NULL", ()).fetchone()["n"]
    pend_rev = c.execute("SELECT COUNT(*) n FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE s.language_id IN (SELECT id FROM _sel) AND a.status IN ('SUBMITTED','COMPLETED') AND (a.review_status='PENDING' OR a.review_status IS NULL)", ()).fetchone()["n"]
    acc_rev = c.execute("SELECT COUNT(*) n FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE s.language_id IN (SELECT id FROM _sel) AND (a.status='ACCEPTED' OR a.review_status IN ('APPROVED','ACCEPTED'))", ()).fetchone()["n"]
    rej_rev = c.execute("SELECT COUNT(*) n FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE s.language_id IN (SELECT id FROM _sel) AND (a.status='REJECTED' OR a.review_status='REJECTED')", ()).fetchone()["n"]
    reports_kpis = {
        "total_reports": tot_reports,
        "pending_review": pend_rev,
        "accepted_reports": acc_rev,
        "rejected_reports": rej_rev
    }

    lang_name_by_id = {l["id"]: l["name"] for l in all_langs}
    portal_access_items = []
    for _i, _it in enumerate(get_portal_access_items(c)):
        _lid = _it.get("language_id")
        _lid = int(_lid) if str(_lid or "").isdigit() else None
        if _lid is not None and _lid not in allowed_ids:
            continue  # supervisors only see links for their own languages
        if _lid is not None and not all_mode and _lid != selected_lid:
            continue
        portal_access_items.append({**_it, "idx": _i, "language_id": _lid, "language_name": lang_name_by_id.get(_lid, "All languages")})
    training_video_url = db.setting(c, "training_video_url", "https://www.youtube.com/embed/dQw4w9WgXcQ")

    return {
        "agent_sheet": {
            "url": db.setting(c, "agents_sheet_url", "") or "",
            "lang": db.setting(c, "agents_sheet_lang", "") or "",
            "auto": db.setting(c, "agents_sheet_auto", "0") == "1",
            "every": db.setting(c, "agents_sheet_every_min", "15") or "15",
            "last": db.setting(c, "agents_sheet_last", "") or "",
            "pairs_tab": db.setting(c, "pairs_sheet_tab", "") or "",
        },
        "agent_columns": importers.AGENT_COLUMNS,
        "pair_columns": importers.PAIR_COLUMNS,
        "proofs_durable": storage.is_configured(),
        "active_tab": active_tab,
        "current_L": current_L,
        "langs": all_langs,
        "lang_param": ("all" if all_mode else selected_lid),
        "all_mode": all_mode,
        "allowed_langs": [l for l in all_langs if l["id"] in allowed_ids],
        "agents": agents,
        "agents_kpis": agents_kpis,
        "total_people": tot_people,
        "active_agents": act_agents,
        "paired_count": paired_cnt,
        "unpaired_count": unpaired_cnt,
        "scripts_kpi": scripts_kpi,
        "script_kpis": scripts_kpi,
        "scripts": scripts_list,
        "scripts_list": scripts_list,
        "script_list": scripts_list,
        "pairs": pairs,
        "pairs_kpis": pairs_kpis,
        "manual_agents": manual_agents,
        "unpaired_agents": manual_agents,
        "assign_pairs": assign_pairs,
        "assignment_kpis": assignment_kpis,
        "assignable_scripts_count": assignable_scripts,
        "active_eligible_pairs_count": len(active_eligible_pairs),
        "max_assignable": assignable_scripts // len(active_eligible_pairs) if active_eligible_pairs else 0,
        "reports": reports,
        "reports_kpis": reports_kpis,
        "filter_status": status_filter,
        "distinct_taxonomies": distinct_taxonomies,
        "taxonomy_filter": taxonomy_filter,
        "portal_access_items": portal_access_items,
        "training_video_url": training_video_url
    }


def get_portal_access_items(c) -> list:
    client_url = db.setting(c, "client_url", "https://client-portal.recording-system.com")
    portal_access_raw = db.setting(c, "portal_access_list", "")
    items = []
    if portal_access_raw:
        try:
            items = json.loads(portal_access_raw)
        except Exception:
            items = []
        if not isinstance(items, list):
            items = []
    # Sample links only on a fresh install. Once the list has been saved, an empty list means the
    # admin deleted everything, so it must stay empty (it used to bring the samples back).
    if not portal_access_raw.strip():
        base_client = client_url.strip() if (client_url and client_url.strip()) else "https://client-portal.recording-system.com"
        items = [
            {
                "taxonomy": "Healthcare > Consultation",
                "name": "Primary Client Recorder",
                "url": base_client,
                "code": "AWIGN-REC-2026"
            },
            {
                "taxonomy": "Healthcare > Telehealth",
                "name": "Backup Recording Portal",
                "url": "https://backup-recorder.recording-system.com",
                "code": "REC-BACKUP-8821"
            },
            {
                "taxonomy": "BFSI > Banking & Cards",
                "name": "Roleplay Session Studio",
                "url": "https://session.recording-system.com/agent-room",
                "code": "CLIENT-SESS-9024"
            },
            {
                "taxonomy": "General / All Domains",
                "name": "Audio QA & Verification Portal",
                "url": "https://qa-verify.recording-system.com/check",
                "code": "AUDIO-VERIFY-4410"
            }
        ]
    else:
        for it in items:
            if not it.get("taxonomy"):
                it["taxonomy"] = it.get("name") or "General"
    return items


# ==============================================================================
# ROUTE ENDPOINTS
# ==============================================================================

@router.get("/admin")
@router.get("/admin/dashboard")
async def admin_dashboard_route(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.ro() as c:
        ctx = build_dashboard_context(c, u, request)
    return render(request, "admin_dashboard.html", u, **ctx)




_KPI_WHERE = {
    "assigned": "a.status NOT IN ('RELEASED','ABANDONED','REJECTED')",
    "completed": "(a.status IN ('SUBMITTED','COMPLETED','CONFIRMED','ACCEPTED') OR a.review_status IN ('APPROVED','ACCEPTED')) AND a.status NOT IN ('RELEASED','ABANDONED')",
    "accepted": "(a.review_status IN ('APPROVED','ACCEPTED') OR a.status IN ('APPROVED','ACCEPTED','CONFIRMED')) AND a.status NOT IN ('RELEASED','ABANDONED')",
    "rejected": "(a.review_status='REJECTED' OR a.status='REJECTED')",
    "recorded": "a.conf_min IS NOT NULL AND a.status NOT IN ('ASSIGNED','RELEASED','ABANDONED')",
    "review_pending": "a.conf_min IS NOT NULL AND a.status NOT IN ('ASSIGNED','RELEASED','ABANDONED') AND a.review_status NOT IN ('APPROVED','ACCEPTED','REJECTED') AND a.status NOT IN ('APPROVED','ACCEPTED','REJECTED')",
}


@router.get("/admin/kpi_detail")
async def admin_kpi_detail(request: Request):
    """Rows behind a dashboard KPI card. kind=total|assigned|pending|completed|accepted|rejected|recorded|review_pending,
    lang_id=<id>|all, optional agent_id."""
    u = gate(request, "admin", "lead", "supervisor", "developer")
    kind = (request.query_params.get("kind") or "assigned").lower()
    lang_raw = request.query_params.get("lang_id") or "all"
    agent_raw = request.query_params.get("agent_id")
    with db.ro() as c:
        allowed = list(u["lang_ids"]) if u["role"] in ("lead", "supervisor") else None
        if lang_raw.isdigit():
            lids = [int(lang_raw)]
            if allowed is not None:
                lids = [x for x in lids if x in allowed]
        else:
            lids = allowed if allowed is not None else [r["id"] for r in c.execute("SELECT id FROM languages")]
        if not lids:
            return JSONResponse({"kind": kind, "rows": []})
        ph = ",".join("?" * len(lids))
        args = list(lids)
        rows = []
        if kind == "total":
            q = ("SELECT s.id sid, s.code, s.domain, s.subdomain, s.specialisation, l.name lang, s.status sstatus "
                 "FROM scripts s JOIN languages l ON l.id=s.language_id WHERE s.language_id IN (%s) ORDER BY s.id" % ph)
            for r in c.execute(q + " LIMIT 3000", args).fetchall():
                rows.append({"code": r["code"], "language": r["lang"], "taxonomy": f"{r['domain']} > {r['subdomain']} > {r['specialisation'] or '-'}",
                             "pair": "-", "status": r["sstatus"], "minutes": "-"})
        else:
            if kind == "pending":
                cond = "(%s) AND NOT (%s)" % (_KPI_WHERE["assigned"], _KPI_WHERE["completed"])
            elif kind in _KPI_WHERE:
                cond = _KPI_WHERE[kind]
            else:
                return JSONResponse({"kind": kind, "rows": []})
            extra = ""
            if agent_raw and agent_raw.isdigit():
                extra = " AND (a.ua=? OR a.ub=?)"
                args += [int(agent_raw), int(agent_raw)]
            q = ("SELECT a.id, a.status, a.review_status, a.conf_min, s.code, s.domain, s.subdomain, s.specialisation, l.name lang, "
                 "ua.name na, ub.name nb FROM assignments a JOIN scripts s ON s.id=a.script_id JOIN languages l ON l.id=s.language_id "
                 "LEFT JOIN users ua ON ua.id=a.ua LEFT JOIN users ub ON ub.id=a.ub "
                 "WHERE s.language_id IN (%s) AND (%s)%s ORDER BY a.id DESC LIMIT 3000" % (ph, cond, extra))
            for r in c.execute(q, args).fetchall():
                m = r["conf_min"]
                rows.append({"code": r["code"], "language": r["lang"],
                             "taxonomy": f"{r['domain']} > {r['subdomain']} > {r['specialisation'] or '-'}",
                             "pair": f"{r['na'] or '?'} & {r['nb'] or '?'}",
                             "status": (r["review_status"] if (r["review_status"] or "").upper() in ("APPROVED", "ACCEPTED", "REJECTED") else r["status"]),
                             "minutes": (f"{divmod(int(round(m * 60)), 60)[0]:02d}:{divmod(int(round(m * 60)), 60)[1]:02d}" if m else "-")})
    return JSONResponse({"kind": kind, "rows": rows})


def _kpi_lids(c, u, lang_raw):
    allowed = list(u["lang_ids"]) if u["role"] in ("lead", "supervisor") else None
    if str(lang_raw).isdigit():
        lids = [int(lang_raw)]
        if allowed is not None:
            lids = [x for x in lids if x in allowed]
        return lids
    return allowed if allowed is not None else [r["id"] for r in c.execute("SELECT id FROM languages")]


def _hhmm(m):
    if not m:
        return "-"
    h, mm = divmod(int(round(m * 60)), 60)
    return f"{h:02d}:{mm:02d}"


@router.get("/admin/kpi_rows")
async def admin_kpi_rows(request: Request):
    """Generic drill-down behind ANY KPI card in the app (admin + supervisor identical, supervisors language-scoped).
    kind: ag_total|ag_active|ag_inactive|ag_paired|ag_unpaired | pr_total|pr_active|pr_limit |
          sc_total|sc_available|sc_assigned|sc_pending|sc_completed|sc_accepted|sc_rejected|sc_recorded|sc_review_pending
    Returns {title, columns:[...], rows:[[...]]}."""
    u = gate(request, "admin", "lead", "supervisor", "developer")
    kind = (request.query_params.get("kind") or "").lower()
    lang_raw = request.query_params.get("lang_id") or "all"
    agent_raw = request.query_params.get("agent_id")
    empty = {"kind": kind, "title": kind, "columns": [], "rows": []}
    with db.ro() as c:
        lids = _kpi_lids(c, u, lang_raw)
        if not lids:
            return JSONResponse(empty)
        ph = ",".join("?" * len(lids))

        if kind.startswith("ag_"):
            titles = {"ag_total": "All people", "ag_active": "Active agents", "ag_inactive": "Inactive agents",
                      "ag_paired": "Paired agents", "ag_unpaired": "Unpaired agents"}
            if kind not in titles:
                return JSONResponse(empty)
            cond = {"ag_total": "1=1", "ag_active": "u.active=1", "ag_inactive": "u.active=0"}.get(kind, "u.active=1")
            rows, meta = [], []
            people = c.execute("SELECT u.id, u.name, u.code, u.gender, u.availability, u.active, u.language_id lid, l.name lang FROM users u "
                               "LEFT JOIN languages l ON l.id=u.language_id WHERE u.role='participant' AND u.language_id IN (%s) AND %s "
                               "ORDER BY u.name" % (ph, cond), lids).fetchall()
            # Partner names for everyone at once (was one query per person).
            partners_of = {r["id"]: [] for r in people}
            act = {}
            for part in ops._chunks(list(partners_of)):
                pq = ",".join("?" * len(part))
                for p in c.execute(f"SELECT p.id, p.a, p.b, ua.name na, ub.name nb FROM pairs p "
                                   f"LEFT JOIN users ua ON ua.id=p.a LEFT JOIN users ub ON ub.id=p.b "
                                   f"WHERE (p.a IN ({pq}) OR p.b IN ({pq})) AND p.status='ACTIVE'", tuple(part) * 2).fetchall():
                    act[p["id"]] = p
            for pid_ in sorted(act):
                p = act[pid_]
                for uid_ in ((p["a"],) if p["a"] == p["b"] else (p["a"], p["b"])):
                    if uid_ in partners_of:
                        partners_of[uid_].append(p["nb"] if p["a"] == uid_ else p["na"])
            for r in people:
                partners = partners_of[r["id"]]
                if kind == "ag_paired" and not partners:
                    continue
                if kind == "ag_unpaired" and partners:
                    continue
                rows.append([r["name"], r["code"] or "-", r["lang"] or "-", r["gender"] or "-", r["availability"] or "All day",
                             ", ".join(partners) or "Unpaired", "Active" if r["active"] else "Inactive"])
                meta.append({"href": f"/admin?lang_id={r['lid']}&agent_id={r['id']}"})
            return JSONResponse({"kind": kind, "title": titles[kind],
                                 "columns": ["Agent", "Code", "Language", "Gender", "Availability", "Partner(s)", "Status"], "rows": rows, "meta": meta})

        if kind.startswith("pr_"):
            titles = {"pr_total": "All pairs", "pr_active": "Active pairs (< 7.5h)", "pr_limit": "Limit reached (>= 7.5h)"}
            if kind not in titles:
                return JSONResponse(empty)
            rows, meta = [], []
            plist = c.execute("SELECT p.id, p.a, p.b, p.status, p.availability, p.language_id lid, l.name lang, ua.name na, ub.name nb FROM pairs p "
                              "LEFT JOIN users ua ON ua.id=p.a LEFT JOIN users ub ON ub.id=p.b "
                              "LEFT JOIN languages l ON l.id=p.language_id WHERE p.language_id IN (%s) AND p.status='ACTIVE' ORDER BY p.id DESC" % ph, lids).fetchall()
            # Hours and open-assignment counts for every pair at once (was 4 queries per pair).
            pair_hours = ops.hours_many(c, "pair", [p["id"] for p in plist])
            open_by_pair = {}
            for part in ops._chunks([p["id"] for p in plist]):
                pq = ",".join("?" * len(part))
                for r in c.execute(f"SELECT pair_id, COUNT(*) n FROM assignments WHERE pair_id IN ({pq}) "
                                   f"AND status NOT IN ('RELEASED','ABANDONED','REJECTED') GROUP BY pair_id", tuple(part)).fetchall():
                    open_by_pair[r["pair_id"]] = r["n"]
            for p in plist:
                h = pair_hours[p["id"]]
                limit = bool(h["recorded_h"] >= 7.5)
                if kind == "pr_active" and not (p["status"] == "ACTIVE" and not limit):
                    continue
                if kind == "pr_limit" and not limit:
                    continue
                n_asg = open_by_pair.get(p["id"], 0)
                rows.append([p["na"] or "?", p["nb"] or "?", p["lang"] or "-", p["availability"] or "All day", n_asg,
                             f"{h['recorded_h']:.1f} h", f"{h['accepted_h']:.1f} h", "LIMIT_REACHED" if limit else p["status"]])
                meta.append({"pair": p["id"], "href": f"/admin/pairs?lang={p['lid']}"})
            return JSONResponse({"kind": kind, "title": titles[kind],
                                 "columns": ["Agent A", "Agent B", "Language", "Availability", "Assigned", "Recorded", "Accepted", "Status"], "rows": rows, "meta": meta})

        if kind.startswith("sc_"):
            sk = kind[3:]
            titles = {"total": "All scripts", "available": "Available to assign", "assigned": "Assigned scripts", "pending": "Pending recording",
                      "completed": "Completed / submitted", "accepted": "Accepted", "recorded": "Recorded (with time)",
                      "review_pending": "Awaiting review", "rejected": "Rejected"}
            if sk not in titles:
                return JSONResponse(empty)
            cols = ["Script", "Language", "Taxonomy", "Pair", "Status", "Time"]
            rows = []
            args = list(lids)
            if sk in ("total", "available"):
                extra = ""
                if sk == "available":
                    extra = (" AND s.status IN ('READY','AVAILABLE','APPROVED') AND NOT EXISTS (SELECT 1 FROM assignments a "
                             "WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED','REJECTED'))")
                for r in c.execute("SELECT s.code, s.domain, s.subdomain, s.specialisation, l.name lang, s.status st FROM scripts s "
                                   "JOIN languages l ON l.id=s.language_id WHERE s.language_id IN (%s)%s ORDER BY s.id LIMIT 3000" % (ph, extra), args).fetchall():
                    rows.append([r["code"], r["lang"], f"{r['domain']} > {r['subdomain']} > {r['specialisation'] or '-'}", "-", r["st"], "-"])
            else:
                if sk == "pending":
                    cond = "(%s) AND NOT (%s)" % (_KPI_WHERE["assigned"], _KPI_WHERE["completed"])
                else:
                    cond = _KPI_WHERE[sk]
                extra = ""
                if agent_raw and str(agent_raw).isdigit():
                    extra = " AND (a.ua=? OR a.ub=?)"
                    args += [int(agent_raw), int(agent_raw)]
                for r in c.execute("SELECT a.status, a.review_status, a.conf_min, s.code, s.domain, s.subdomain, s.specialisation, l.name lang, "
                                   "ua.name na, ub.name nb FROM assignments a JOIN scripts s ON s.id=a.script_id JOIN languages l ON l.id=s.language_id "
                                   "LEFT JOIN users ua ON ua.id=a.ua LEFT JOIN users ub ON ub.id=a.ub "
                                   "WHERE s.language_id IN (%s) AND (%s)%s ORDER BY a.id DESC LIMIT 3000" % (ph, cond, extra), args).fetchall():
                    st = r["review_status"] if (r["review_status"] or "").upper() in ("APPROVED", "ACCEPTED", "REJECTED") else r["status"]
                    rows.append([r["code"], r["lang"], f"{r['domain']} > {r['subdomain']} > {r['specialisation'] or '-'}",
                                 f"{r['na'] or '?'} & {r['nb'] or '?'}", st, _hhmm(r["conf_min"])])
            return JSONResponse({"kind": kind, "title": titles[sk], "columns": cols, "rows": rows})
    return JSONResponse(empty)



@router.get("/admin/management")
async def admin_management_route(request: Request):
    tab = request.query_params.get("tab", "agents")
    if tab in ("assign", "assignments", "assignment"):
        tab = "pairs"  # Pairs + Script Assignment are one combined tab
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.ro() as c:
        ctx = build_management_context(c, u, request, active_tab=tab)
    return render(request, "admin_management.html", u, **ctx)


@router.get("/admin/agent/{uid}")
async def admin_agent_detail_redirect(request: Request, uid: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.ro() as c:
        ag = c.execute("SELECT id, language_id FROM users WHERE id=?", (uid,)).fetchone()
    if not ag:
        return back("/admin/agents", e="Agent not found.")
    lid = ag["language_id"] or 1
    return RedirectResponse(f"/admin?lang_id={lid}&agent_id={uid}", 303)


@router.get("/admin/agents")
async def admin_agents_route(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.ro() as c:
        ctx = build_management_context(c, u, request, active_tab="agents")
    return render(request, "admin_management.html", u, **ctx)


@router.get("/admin/scripts")
async def admin_scripts_route(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.ro() as c:
        ctx = build_management_context(c, u, request, active_tab="scripts")
    return render(request, "admin_management.html", u, **ctx)


@router.get("/admin/scripts/text")
async def admin_script_text(request: Request):
    """Full text of one script, loaded when its popup opens (pages no longer embed every script's text)."""
    u = gate(request, "admin", "lead", "supervisor", "developer")
    code = (request.query_params.get("code") or "").strip()
    with db.ro() as c:
        s = c.execute("SELECT id, code, language_id FROM scripts WHERE code=?", (code,)).fetchone()
        if not s:
            return JSONResponse({"error": "Script not found."}, status_code=404)
        if u["role"] in ("lead", "supervisor") and s["language_id"] not in list(u["lang_ids"]):
            return JSONResponse({"error": "Not allowed for this language."}, status_code=403)
        text = get_script_content_text(c, s["id"], s["code"])
    return JSONResponse({"code": s["code"], "text": text})


@router.get("/admin/scripts/tree")
async def admin_scripts_tree(request: Request):
    """Flat list of scripts (language, taxonomy, status) for the client-side folder browser."""
    u = gate(request, "admin", "lead", "supervisor", "developer")
    lang = (request.query_params.get("lang") or "").strip()
    with db.ro() as c:
        where, args = [], []
        if u["role"] in ("lead", "supervisor"):
            lids = list(u["lang_ids"])
            if not lids:
                return JSONResponse({"items": []})
            where.append("s.language_id IN (%s)" % ",".join("?" * len(lids)))
            args.extend(lids)
        elif lang.isdigit():
            where.append("s.language_id=?")
            args.append(int(lang))
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        rows = c.execute(
            "SELECT s.code, s.language_id, l.name AS lang, s.domain, s.subdomain, s.specialisation, s.status, "
            "(SELECT a.status FROM assignments a WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED') ORDER BY a.id DESC LIMIT 1) AS astat "
            "FROM scripts s JOIN languages l ON l.id=s.language_id" + where_sql +
            " ORDER BY l.name, s.domain, s.subdomain, s.specialisation, s.code", tuple(args)
        ).fetchall()
        items = []
        for r in rows:
            ast = (r["astat"] or "").upper()
            sst = (r["status"] or "").upper()
            if ast in ("CONFIRMED", "ACCEPTED", "APPROVED"):
                st = "Completed"
            elif ast in ("ASSIGNED", "IN_SESSION", "SUBMITTED", "COMPLETED", "DISPUTED"):
                st = "Assigned"
            elif sst in ("READY", "APPROVED", "AVAILABLE"):
                st = "Available"
            else:
                st = (r["status"] or "Planned").title()
            items.append({
                "lang": r["lang"], "lang_id": r["language_id"], "domain": r["domain"] or "Uncategorized",
                "sub": r["subdomain"] or "", "spec": r["specialisation"] or "",
                "code": r["code"], "status": st,
            })
    return JSONResponse({"items": items})


@router.get("/admin/pairs")
async def admin_pairs_route(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.ro() as c:
        ctx = build_management_context(c, u, request, active_tab="pairs")
    return render(request, "admin_management.html", u, **ctx)


@router.get("/admin/assign")
@router.get("/admin/assignment")
async def admin_assign_route(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.ro() as c:
        ctx = build_management_context(c, u, request, active_tab="pairs")
    return render(request, "admin_management.html", u, **ctx)


@router.get("/admin/reports")
async def admin_reports_route(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.ro() as c:
        ctx = build_management_context(c, u, request, active_tab="reports")
    return render(request, "admin_management.html", u, **ctx)


@router.get("/admin/client_access")
async def admin_client_access_route(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.ro() as c:
        ctx = build_management_context(c, u, request, active_tab="client_access")
    return render(request, "admin_management.html", u, **ctx)


@router.post("/admin/client_access/video")
async def admin_client_access_video_post(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    video_file = f.get("video_file")
    video_url = (f.get("video_url") or "").strip()

    saved_url = ""
    if video_file and hasattr(video_file, "filename") and video_file.filename:
        ext = os.path.splitext(video_file.filename)[1].lower() or ".mp4"
        fname = f"training_video_{int(time.time())}{ext}"
        save_path = os.path.join(UPLOADS_DIR, fname)
        content = await video_file.read()
        if content:
            with open(save_path, "wb") as out_f:
                out_f.write(content)
            saved_url = f"/uploads/{fname}"

    if not saved_url and video_url:
        # Convert standard YouTube watch links to embed format automatically
        if "youtube.com/watch?v=" in video_url:
            vid_id = video_url.split("v=")[1].split("&")[0]
            saved_url = f"https://www.youtube.com/embed/{vid_id}"
        elif "youtu.be/" in video_url:
            vid_id = video_url.split("youtu.be/")[1].split("?")[0]
            saved_url = f"https://www.youtube.com/embed/{vid_id}"
        elif "drive.google.com/file/d/" in video_url:
            # Google Drive share link -> embeddable preview URL
            drive_id = video_url.split("/file/d/")[1].split("/")[0].split("?")[0]
            saved_url = f"https://drive.google.com/file/d/{drive_id}/preview"
        else:
            saved_url = video_url

    if not saved_url:
        return back("/admin/client_access", e="Please upload a video file or provide a valid video URL.")

    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('training_video_url', ?)", (saved_url,))
        db.audit(c, u, "update_training_video", saved_url)

    return back("/admin/client_access", m="Training video updated successfully.")


@router.post("/admin/client_access/add")
async def admin_client_access_add_post(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    taxonomy = (f.get("taxonomy") or "").strip()
    url = (f.get("url") or "").strip()
    code = (f.get("code") or "").strip()
    if not taxonomy or not url or not code:
        return back("/admin/client_access", e="Taxonomy, URL, and Code are all required.")
    lang_raw = (f.get("language_id") or "").strip()
    item_lid = int(lang_raw) if lang_raw.isdigit() else None
    if u["role"] in ("lead", "supervisor") and (item_lid is None or item_lid not in u["lang_ids"]):
        return back("/admin/client_access", e="Pick one of your own languages for this link.")

    with db.tx() as c:
        items = get_portal_access_items(c)
        items.append({
            "taxonomy": taxonomy,
            "name": taxonomy,
            "url": url,
            "code": code,
            "language_id": item_lid
        })
        c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('portal_access_list', ?)", (json.dumps(items, ensure_ascii=False),))
        db.audit(c, u, "add_client_access_link", f"taxonomy={taxonomy} code={code}")

    return back("/admin/client_access", m=f"Client access link for '{taxonomy}' added.")


@router.post("/admin/client_access/bulk")
async def admin_client_access_bulk_post(request: Request):
    """Bulk-add client portal links. One per line: language, taxonomy, url, code (comma or tab separated)."""
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    text = (f.get("bulk") or "").strip()
    if not text:
        return back("/admin/client_access", e="Paste at least one line (language, taxonomy, url, code).")
    added, skipped = 0, 0
    with db.tx() as c:
        langs = {r["name"].strip().lower(): r["id"] for r in c.execute("SELECT id, name FROM languages").fetchall()}
        items = get_portal_access_items(c)
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in (line.split("\t") if "\t" in line else line.split(","))]
            if len(parts) < 4:
                skipped += 1
                continue
            lang_s, taxonomy, url, code = parts[0], parts[1], parts[2], parts[3]
            lid = None
            if lang_s.isdigit():
                lid = int(lang_s)
            elif lang_s.lower() in langs:
                lid = langs[lang_s.lower()]
            elif lang_s.lower() in ("all", "all languages", ""):
                lid = None
            if u["role"] in ("lead", "supervisor") and (lid is None or lid not in u["lang_ids"]):
                skipped += 1
                continue
            if not (taxonomy and url and code):
                skipped += 1
                continue
            items.append({"taxonomy": taxonomy, "name": taxonomy, "url": url, "code": code, "language_id": lid})
            added += 1
        c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('portal_access_list', ?)", (json.dumps(items, ensure_ascii=False),))
        db.audit(c, u, "bulk_add_client_access", f"added={added} skipped={skipped}")
    msg = f"Added {added} client access link(s)." + (f" {skipped} line(s) skipped." if skipped else "")
    return back("/admin/client_access", m=msg)


@router.post("/admin/client_access/edit/{idx}")
async def admin_client_access_edit_post(request: Request, idx: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    taxonomy = (f.get("taxonomy") or "").strip()
    url = (f.get("url") or "").strip()
    code = (f.get("code") or "").strip()
    if not taxonomy or not url or not code:
        return back("/admin/client_access", e="Taxonomy, URL, and Code are all required.")
    lang_raw = (f.get("language_id") or "").strip()
    item_lid = int(lang_raw) if lang_raw.isdigit() else None
    if u["role"] in ("lead", "supervisor") and (item_lid is None or item_lid not in u["lang_ids"]):
        return back("/admin/client_access", e="Pick one of your own languages for this link.")

    with db.tx() as c:
        items = get_portal_access_items(c)
        if idx < 0 or idx >= len(items):
            return back("/admin/client_access", e="Client access link not found.")
        old_l = items[idx].get("language_id")
        if u["role"] in ("lead", "supervisor") and str(old_l or "").isdigit() and int(old_l) not in u["lang_ids"]:
            return back("/admin/client_access", e="That link belongs to another language.")
        items[idx] = {
            "taxonomy": taxonomy,
            "name": taxonomy,
            "url": url,
            "code": code,
            "language_id": item_lid
        }
        c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('portal_access_list', ?)", (json.dumps(items, ensure_ascii=False),))
        db.audit(c, u, "edit_client_access_link", f"idx={idx} taxonomy={taxonomy} code={code}")

    return back("/admin/client_access", m=f"Client access link #{idx + 1} updated.")


@router.post("/admin/client_access/delete/{idx}")
async def admin_client_access_delete_post(request: Request, idx: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.tx() as c:
        items = get_portal_access_items(c)
        if idx < 0 or idx >= len(items):
            return back("/admin/client_access", e="Client access link not found.")
        old_l = items[idx].get("language_id")
        if u["role"] in ("lead", "supervisor") and not (str(old_l or "").isdigit() and int(old_l) in u["lang_ids"]):
            return back("/admin/client_access", e="That link belongs to another language.")
        removed = items.pop(idx)
        c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('portal_access_list', ?)", (json.dumps(items, ensure_ascii=False),))
        db.audit(c, u, "delete_client_access_link", f"idx={idx} taxonomy={removed.get('taxonomy')}")

    return back("/admin/client_access", m="Client access link removed.")


# ---------------------------------------------------------------- Admin Actions

@router.post("/admin/agents/add")
@router.post("/admin/agent/add")
async def admin_agent_add_post(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    name = (f.get("name") or "").strip()
    lid = u["language_id"] if u["role"] in ("lead", "supervisor") else int(f.get("language_id") or 1)
    phone = (f.get("phone") or "").strip()
    gender = (f.get("gender") or "M").strip()
    email = (f.get("email") or "").strip()
    avail = (f.get("availability") or "All day").strip()
    with db.tx() as c:
        try:
            uid, acode = ops.create_user(c, u, name, "participant", lid, phone=phone, email=email, gender=gender)
            c.execute("UPDATE users SET availability=? WHERE id=?", (avail, uid))
        except Exception as ex:
            return back(f"/admin/agents?lang={lid}", e=str(ex))
    return back(f"/admin/agents?lang={lid}", m=f"Agent {name} created with Access Code: {acode}")


@router.post("/admin/agents/bulk_upload")
@router.post("/admin/agent/bulk")
async def admin_agent_bulk_post(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    up = f.get("file") or f.get("csv_file")          # the form field is named "file"
    lid = u["language_id"] if u["role"] in ("lead", "supervisor") else (f.get("language_id") or "")
    try:
        if up and hasattr(up, "read"):
            text = importers.upload_to_csv_text(getattr(up, "filename", ""), await up.read())
        else:
            text = importers.rows_to_csv_text(list(csv.reader(io.StringIO(f.get("csv_data", "")))))
        if not text.strip():
            return back(f"/admin/agents?lang={lid}", e="The file is empty. Use the format shown in the Import dialog.")
        with db.tx() as c:
            res = importers.import_agents(c, u, lid, text)
    except Exception as ex:
        return back(f"/admin/agents?lang={lid}", e=f"Import failed: {ex}")
    if res["errors"] and not (res["created"] or res["updated"]):
        return back(f"/admin/agents?lang={lid}", e=importers.summarize(res))
    return back(f"/admin/agents?lang={lid}", m=importers.summarize(res))


@router.post("/admin/agents/sheet_link")
async def admin_agents_sheet_link_post(request: Request):
    """Save the Google Sheet link (+ auto-sync switch) and optionally import right now."""
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    url = (f.get("sheet_url") or "").strip()
    pairs_tab = (f.get("pairs_tab") or "").strip()   # same spreadsheet, a second tab for pairs
    lid = u["language_id"] if u["role"] in ("lead", "supervisor") else (f.get("language_id") or "")
    auto = "1" if f.get("auto") else "0"
    try:
        every = str(max(5, min(1440, int(f.get("every_min") or 15))))
    except ValueError:
        every = "15"
    try:
        if url:
            importers._sheet_ids(url)
        with db.tx() as c:
            db.set_setting(c, "agents_sheet_url", url)
            db.set_setting(c, "agents_sheet_lang", lid)
            db.set_setting(c, "agents_sheet_auto", auto if url else "0")
            db.set_setting(c, "agents_sheet_every_min", every)
            db.set_setting(c, "pairs_sheet_tab", pairs_tab)
            db.audit(c, u, "agents_sheet_link", f"auto={auto} every={every}min pairs_tab={pairs_tab}")
            if url and f.get("action") == "sync_now":
                res = importers.run_saved_sheet_sync(c, u)
                msg = "Agents synced. " + importers.summarize(res)
                # Same spreadsheet, second tab -> import pairs. Agents imported first above, so
                # the "an agent must already exist to be paired" rule is satisfied for new agents too.
                if pairs_tab:
                    try:
                        ptext = importers.fetch_sheet_csv_by_name(url, pairs_tab, c)
                        preport = []
                        pcount = ops.bulk_upload_pairs_csv(c, u, lid, ptext, report=preport)
                        msg += f" | Pairs: {pcount} created"
                        if preport:
                            msg += f", {len(preport)} rejected (" + "; ".join(preport[:3]) + ")"
                    except Exception as pex:
                        msg += f" | Pairs tab error: {pex}"
                if res["errors"] and not (res["created"] or res["updated"]) and not pairs_tab:
                    return back(f"/admin/agents?lang={lid}", e=importers.summarize(res))
                return back(f"/admin/agents?lang={lid}", m=msg)
    except Exception as ex:
        return back(f"/admin/agents?lang={lid}", e=str(ex))
    return back(f"/admin/agents?lang={lid}", m="Sheet link saved." + (f" Auto-sync every {every} min." if auto == "1" and url else ""))


@router.get("/admin/template/{kind}.csv")
async def admin_import_template(request: Request, kind: str):
    gate(request, "admin", "lead", "supervisor", "developer")
    if kind == "agents":
        cols, ex = importers.AGENT_COLUMNS, [[x[3] for x in importers.AGENT_COLUMNS],
                                             ["Ramesh", "7087317670", "ramesh@example.com", "M", "Morning", "Marathi"]]
    elif kind == "pairs":
        cols, ex = importers.PAIR_COLUMNS, [[x[3] for x in importers.PAIR_COLUMNS], ["Sanath Reddy", "kushal Reddy R", "Morning"]]
    else:
        raise HTTPException(404, "Unknown template")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([x[0] for x in cols])
    w.writerows(ex)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{kind}_template.csv"'})


@router.get("/admin/template/{kind}.xlsx")
async def admin_import_template_xlsx(request: Request, kind: str):
    gate(request, "admin", "lead", "supervisor", "developer")
    if kind == "agents":
        cols, ex = importers.AGENT_COLUMNS, [["Ramesh", "7087317670", "ramesh@example.com", "M", "Morning", "Marathi"]]
    elif kind == "pairs":
        cols, ex = importers.PAIR_COLUMNS, [["Sanath Reddy", "kushal Reddy R", "Morning"]]
    else:
        raise HTTPException(404, "Unknown template")
    import openpyxl
    from openpyxl.styles import Font, PatternFill
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = kind.capitalize()
    headers = [x[0] for x in cols]
    ws.append(headers)
    for ci in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=ci)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill(start_color="00A896", end_color="00A896", fill_type="solid")
        ws.column_dimensions[openpyxl.utils.get_column_letter(ci)].width = max(14, len(headers[ci - 1]) + 4)
    for row in ex:
        ws.append(row)
    ws.freeze_panes = "A2"
    out = io.BytesIO()
    wb.save(out)
    return Response(out.getvalue(),
                    media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{kind}_template.xlsx"'})


@router.post("/admin/agents/send_code/{uid}")
@router.post("/admin/agent/{uid}/send_code")
async def admin_agent_send_code_single(request: Request, uid: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    next_url = (f.get("next") or "/admin/agents").strip()
    if not next_url.startswith("/"):
        next_url = "/admin/agents"

    with db.tx() as c:
        ag = c.execute("SELECT name, email, code FROM users WHERE id=?", (uid,)).fetchone()
        if not ag:
            return back(next_url, e="Agent not found.")
        res = email_service.send_access_code_to_agent(uid, c=c, actor=db.actor_name(u))

    if res.ok:
        return back(next_url, m=f"Access code ({ag['code']}) emailed to {ag['name']} ({ag['email']}).")
    else:
        return back(next_url, e=res.get("error") or f"Failed to send access code email to {ag['name']}.")


@router.post("/admin/agents/send_codes_all")
async def admin_agents_send_codes_all(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.tx() as c:
        res = email_service.send_access_codes_to_agents(c=c, actor=db.actor_name(u))

    sent = res.get("sent", 0)
    skipped = res.get("skipped_no_email", 0)
    failed = res.get("failed", 0)

    if sent > 0:
        msg = f"Access codes successfully emailed to {sent} active agent(s)."
        if skipped > 0:
            msg += f" ({skipped} active agents skipped without email)."
        if failed > 0:
            msg += f" ({failed} failed)."
        return back("/admin/agents", m=msg)
    elif skipped > 0 and res.get("with_email", 0) == 0:
        return back("/admin/agents", m=f"No active agents have email addresses registered ({skipped} skipped).")
    elif failed > 0:
        err = res["errors"][0]["error"] if res.get("errors") else "Delivery failed"
        return back("/admin/agents", m=f"Failed to deliver access codes: {err}")
    else:
        return back("/admin/agents", m="No active agents found in the system.")


@router.post("/admin/agents/send_codes_unsent")
async def admin_agents_send_codes_unsent(request: Request):
    """Send access codes ONLY to active agents who have never been sent one before."""
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.tx() as c:
        res = email_service.send_access_codes_to_agents(c=c, actor=db.actor_name(u), only_unsent=True)
    sent = res.get("sent", 0)
    skipped = res.get("skipped_no_email", 0)
    failed = res.get("failed", 0)
    if res.get("total_active", 0) == 0:
        return back("/admin/agents", m="Everyone has already been sent a code — nothing to send.")
    if sent > 0:
        msg = f"Access codes sent to {sent} not-yet-sent agent(s)."
        if skipped > 0:
            msg += f" ({skipped} skipped without email)."
        if failed > 0:
            msg += f" ({failed} failed)."
        return back("/admin/agents", m=msg)
    if skipped > 0 and res.get("with_email", 0) == 0:
        return back("/admin/agents", m=f"Not-yet-sent agents have no email addresses ({skipped} skipped).")
    if failed > 0:
        err = res["errors"][0]["error"] if res.get("errors") else "Delivery failed"
        return back("/admin/agents", e=f"Failed to deliver access codes: {err}")
    return back("/admin/agents", m="No un-sent agents found.")


@router.post("/admin/agents/toggle/{uid}")
@router.post("/admin/agent/{uid}/toggle_active")
async def admin_agent_toggle_post(request: Request, uid: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.tx() as c:
        c.execute("UPDATE users SET active = (1 - active) WHERE id=?", (uid,))
        db.audit(c, u, "toggle_active_agent", f"user={uid}")
    return back("/admin/agents", m="Agent status updated.")


@router.post("/admin/agents/edit/{uid}")
@router.post("/admin/agent/{uid}/edit")
async def admin_agent_edit_post(request: Request, uid: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    name = (f.get("name") or "").strip()
    phone = (f.get("phone") or "").strip()
    email = (f.get("email") or "").strip()
    gender = (f.get("gender") or "").strip()
    availability = (f.get("availability") or "All day").strip()

    if not name or not phone:
        return back("/admin/agents", e="Name and Phone are required.")

    with db.tx() as c:
        dup = c.execute("SELECT id FROM users WHERE phone=? AND id<>? AND role='participant' AND language_id=(SELECT language_id FROM users WHERE id=?)", (phone, uid, uid)).fetchone()
        if dup:
            return back("/admin/agents", e=f"An agent with phone {phone} already exists in this language.")

        c.execute(
            "UPDATE users SET name=?, phone=?, email=?, gender=?, availability=? WHERE id=?",
            (name, phone, email, gender, availability, uid)
        )
        db.audit(c, u, "edit_agent", f"user={uid} name={name} phone={phone}")

    return back("/admin/agents", m=f"Agent '{name}' updated successfully.")


@router.post("/admin/agents/delete/{uid}")
@router.post("/admin/agent/{uid}/delete")
async def admin_agent_delete_post(request: Request, uid: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    reason = (f.get("reason") or "Deleted by admin").strip()
    with db.tx() as c:
        db.audit(c, u, "delete_agent_reason", f"user={uid} reason={reason}")
        ops.delete_user(c, u, uid)
    return back("/admin/agents", m="Agent deleted successfully.")


@router.post("/admin/scripts/delete")
async def admin_scripts_delete_post(request: Request):
    """Delete scripts by code list (single / multiple) or by folder scope (language, domain, sub-domain,
    specialisation). dry_run=1 only counts. Admin/developer only; busy scripts are skipped, not deleted."""
    u = gate(request, "admin", "developer")
    f = await request.form()
    if f.get("csrf") != getattr(request.state, "csrf", None):
        return JSONResponse({"error": "Session expired. Reload the page and try again."}, status_code=400)
    dry = str(f.get("dry_run") or "") == "1"
    with db.tx() as c:
        raw_codes = str(f.get("codes") or "")
        if raw_codes.strip():
            ids = ops.script_ids_for_codes(c, re.split(r"[\s,]+", raw_codes))
        else:
            lid = str(f.get("lang_id") or "")
            if not lid.isdigit():
                return JSONResponse({"error": "Pick scripts or a folder to delete."}, status_code=400)
            ids = ops.script_ids_in_scope(c, int(lid), (f.get("domain") or "").strip() or None,
                                          (f.get("sub") or "").strip() or None, (f.get("spec") or "").strip() or None)
        if not ids:
            return JSONResponse({"error": "No matching scripts found."}, status_code=404)
        res = ops.delete_scripts(c, u, ids, dry_run=dry)
    res["dry_run"] = dry
    return JSONResponse(res)


@router.get("/admin/scripts/sync")
@router.post("/admin/scripts/sync")
async def admin_scripts_sync_post(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    lid = u["language_id"] if u["role"] in ("lead", "supervisor") else None
    with db.tx() as c:
        synced = ops.sync_library_scripts(c, language_id=lid)
    return back("/admin", m=f"Sync completed! {synced} scripts processed into database.")


@router.post("/admin/scripts/import_drive")
async def admin_scripts_import_drive_post(request: Request):
    """Import scripts from a Google Drive folder / file / Google Doc link. Same importer, same duplicate checks and
    same per-file result rows as the browser upload. Never fetches anything except Google Drive/Docs by ID."""
    import shutil
    import tempfile
    from starlette.concurrency import run_in_threadpool
    from . import script_importer
    from .integrations import drive as drive_mod
    from .integrations import drive_import as DI

    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    if f.get("csrf") != getattr(request.state, "csrf", None):
        return JSONResponse({"error": "Session expired. Reload the page and try again."}, status_code=400)

    lang_raw = str(f.get("lang") or "")
    is_sup = u["role"] in ("lead", "supervisor")
    target = int(lang_raw) if lang_raw.isdigit() else None
    if is_sup and (target is None or target not in list(u["lang_ids"])):
        return JSONResponse({"error": "Pick one of your languages in the sidebar before importing."}, status_code=403)

    url = str(f.get("drive_url") or "").strip()
    try:
        DI.parse_drive_url(url)
    except DI.DriveImportError as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)

    with db.ro() as c:
        drive_cfg = drive_mod.get_drive_config(c)

    def _fetch():
        return DI.Importer(drive_cfg).collect(url)

    try:
        fetched = await run_in_threadpool(_fetch)
    except DI.DriveImportError as ex:
        return JSONResponse({"error": str(ex)}, status_code=400)
    except Exception as ex:  # network down, Google changed something, etc.
        return JSONResponse({"error": f"Could not read Google Drive: {type(ex).__name__}: {str(ex)[:160]}"}, status_code=502)

    results = []
    tmp_root = tempfile.mkdtemp(prefix="sf_drive_")
    try:
        for i, it in enumerate(fetched.items):
            row = {"file": it.path, "status": "error", "reason": "", "code": "", "language": "", "taxonomy": "", "drive": "source"}
            results.append(row)
            if it.data is None:
                row.update(status="skipped" if "skipped" in it.error else "error", reason=it.error)
                continue
            d = os.path.join(tmp_root, str(i))
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, it.name)
            with open(path, "wb") as fh:
                fh.write(it.data)
            try:
                with db.tx() as c:
                    res = script_importer.import_script_file(c, path, target_lang_id=target, status="READY")
                    if res.get("status") == "imported":
                        sc = c.execute("SELECT s.domain, s.subdomain, s.specialisation, l.name lang FROM scripts s "
                                       "JOIN languages l ON l.id=s.language_id WHERE s.id=?", (res["script_id"],)).fetchone()
                        row["language"] = sc["lang"]
                        row["taxonomy"] = f"{sc['domain']} > {sc['subdomain']} > {sc['specialisation'] or '-'}"
                        db.audit(c, u, "import_script_drive", f"code={res['code']} file={it.name}")
            except Exception as ex:
                row.update(status="error", reason=f"{type(ex).__name__}: {ex}"[:200])
                continue
            row.update(status=res.get("status", "error"), reason=res.get("reason") or res.get("error") or "", code=res.get("code") or "")
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    summary = {k: sum(1 for r in results if r["status"] == k) for k in ("imported", "skipped", "error")}
    return JSONResponse({"summary": summary, "mode": fetched.mode, "truncated": fetched.truncated, "results": results})


_UPLOAD_MAX_FILE = 1024 * 1024  # 1 MB per script is far above any real script
_UPLOAD_MAX_BATCH = 40


@router.post("/admin/scripts/upload_batch")
async def admin_scripts_upload_batch(request: Request):
    """Folder/file upload from the browser. Each file goes through the same importer as the library sync
    (filename -> language/taxonomy, 5-layer duplicate check), then is mirrored to the configured Drive folder.
    Returns one result row per file so nothing fails silently."""
    import shutil
    import tempfile
    from starlette.concurrency import run_in_threadpool
    from . import script_importer
    from .integrations import drive as drive_mod

    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    if f.get("csrf") != getattr(request.state, "csrf", None):
        return JSONResponse({"error": "Session expired. Reload the page and try again."}, status_code=400)

    lang_raw = str(f.get("lang") or "")
    is_sup = u["role"] in ("lead", "supervisor")
    target = int(lang_raw) if lang_raw.isdigit() else None
    if is_sup and (target is None or target not in list(u["lang_ids"])):
        return JSONResponse({"error": "Pick one of your languages in the sidebar before uploading."}, status_code=403)

    uploads = [x for x in f.getlist("files") if hasattr(x, "read")]
    if not uploads:
        return JSONResponse({"error": "No files received."}, status_code=400)
    if len(uploads) > _UPLOAD_MAX_BATCH:
        return JSONResponse({"error": f"Max {_UPLOAD_MAX_BATCH} files per request."}, status_code=400)

    with db.ro() as c:
        drive_cfg = drive_mod.get_drive_config(c)
    mirror = drive_mod.is_configured(drive_cfg)
    client = drive_mod.GoogleDriveClient(config=drive_cfg) if mirror else None

    results = []
    tmp_root = tempfile.mkdtemp(prefix="sf_upload_")
    try:
        for i, up in enumerate(uploads):
            raw_name = (up.filename or "").replace("\\", "/")
            name = os.path.basename(raw_name)
            row = {"file": raw_name or name, "status": "error", "reason": "", "code": "", "language": "", "taxonomy": "", "drive": "off"}
            results.append(row)
            if not name or name.startswith(".") or name.lower().endswith((".ds_store", ".tmp")):
                row.update(status="skipped", reason="Hidden/system file")
                continue
            if not name.lower().endswith((".txt", ".md")):
                row.update(status="skipped", reason="Not a .txt/.md script")
                continue
            data = await up.read()
            if len(data) > _UPLOAD_MAX_FILE:
                row.update(reason="File larger than 1 MB")
                continue
            d = os.path.join(tmp_root, str(i))
            os.makedirs(d, exist_ok=True)
            path = os.path.join(d, name)
            with open(path, "wb") as fh:
                fh.write(data)
            try:
                with db.tx() as c:
                    res = script_importer.import_script_file(c, path, target_lang_id=target, status="READY")
                    if res.get("status") == "imported":
                        sc = c.execute("SELECT s.domain, s.subdomain, s.specialisation, l.name lang FROM scripts s "
                                       "JOIN languages l ON l.id=s.language_id WHERE s.id=?", (res["script_id"],)).fetchone()
                        row["language"] = sc["lang"]
                        row["taxonomy"] = f"{sc['domain']} > {sc['subdomain']} > {sc['specialisation'] or '-'}"
                        db.audit(c, u, "upload_script_file", f"code={res['code']} file={name}")
            except Exception as ex:  # one bad file must not kill the batch
                row.update(status="error", reason=f"{type(ex).__name__}: {ex}"[:200])
                continue
            row.update(status=res.get("status", "error"), reason=res.get("reason") or res.get("error") or "", code=res.get("code") or "")
            if res.get("status") == "imported" and client:
                try:
                    out = await run_in_threadpool(client.upload_file, path, name)
                    row["drive"] = "uploaded" if out.get("success") else "failed"
                except Exception as ex:
                    row["drive"] = "failed"
                    row["reason"] = (row["reason"] + " | " if row["reason"] else "") + f"Drive: {str(ex)[:160]}"
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    summary = {k: sum(1 for r in results if r["status"] == k) for k in ("imported", "skipped", "error")}
    summary["drive_failed"] = sum(1 for r in results if r["drive"] == "failed")
    return JSONResponse({"summary": summary, "drive_mirror": mirror, "results": results})


@router.post("/admin/pairs/auto")
async def admin_pairs_auto_post(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    with db.tx() as c:
        if u["role"] in ("lead", "supervisor"):
            lids = list(u["lang_ids"])
        else:
            req_lid = f.get("language_id")
            if req_lid and str(req_lid).isdigit():
                lids = [int(req_lid)]
            else:
                lids = [l["id"] for l in c.execute("SELECT id FROM languages")]
        total_paired = 0
        for lid in lids:
            pairs_made = ops.auto_pair(c, u, lid)
            total_paired += pairs_made
    return back("/admin/pairs", m=f"Auto-paired {total_paired} pairs based on matching availability and opposite genders.")


@router.post("/admin/pairs/create_manual")
@router.post("/admin/pairs/manual")
async def admin_pairs_manual_post(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    uid_a_raw = f.get("agent_a_id") or f.get("uid_a")
    uid_b_raw = f.get("agent_b_id") or f.get("uid_b")
    if not uid_a_raw or not uid_b_raw:
        return back("/admin/pairs", e="Please select two agents.")
    uid_a = int(uid_a_raw)
    uid_b = int(uid_b_raw)
    avail = f.get("availability", "All day")
    try:
        with db.tx() as c:
            pid, name_a, name_b = ops.manual_pair(c, u, None, uid_a, uid_b, avail)
    except Exception as ex:
        return back("/admin/pairs", e=str(ex))
    return back("/admin/pairs", m=f"{name_a} & {name_b} have been paired.")


@router.post("/admin/pairs/bulk_upload")
@router.post("/admin/pairs/bulk")
async def admin_pairs_bulk_post(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    up = f.get("file") or f.get("csv_file")          # the form field is named "file"
    lid = u["language_id"] if u["role"] in ("lead", "supervisor") else (f.get("language_id") or "")
    try:
        if up and hasattr(up, "read"):
            text = importers.upload_to_csv_text(getattr(up, "filename", ""), await up.read())
        else:
            text = importers.rows_to_csv_text(list(csv.reader(io.StringIO(f.get("csv_data", "")))))
        if not text.strip():
            return back("/admin/pairs", e="The file is empty. Use columns: Agent A, Agent B, Availability.")
        report = []
        with db.tx() as c:
            cnt = ops.bulk_upload_pairs_csv(c, u, lid, text, report=report)
    except Exception as ex:
        return back("/admin/pairs", e=f"Import failed: {ex}")
    if report:
        msg = f"{cnt} pair(s) created, {len(report)} row(s) rejected: " + " | ".join(report[:4])
        return back("/admin/pairs", m=msg) if cnt else back("/admin/pairs", e=msg)
    return back("/admin/pairs", m=f"Uploaded and created {cnt} pairs.")


@router.post("/admin/pairs/delete/{pid}")
@router.post("/admin/pairs/{pid}/delete")
async def admin_pair_delete_post(request: Request, pid: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    reason = (f.get("reason") or "Deleted by admin").strip()
    with db.tx() as c:
        ops.drop_pair(c, u, pid, reason)
    return back("/admin/pairs", m=f"Pair #{pid} deleted. Reason recorded in audit log.")


@router.get("/admin/pairs/{pid}/scripts")
async def pair_scripts_get(request: Request, pid: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.ro() as c:
        p = c.execute(
            "SELECT p.*, ua.name as name_a, ua.phone as phone_a, ua.availability as avail_a, ua.gender as gender_a, "
            "ub.name as name_b, ub.phone as phone_b, ub.availability as avail_b, ub.gender as gender_b, "
            "l.name as lang_name "
            "FROM pairs p JOIN users ua ON ua.id=p.a JOIN users ub ON ub.id=p.b JOIN languages l ON l.id=p.language_id "
            "WHERE p.id=?", (pid,)
        ).fetchone()
        if not p:
            raise HTTPException(404, "Pair not found")

        assignments = c.execute(
            "SELECT a.id, a.script_id, a.conf_min, a.status, a.review_status, a.submitted_at, a.assigned_at, a.proof_img, "
            "s.code, s.domain, s.subdomain, s.specialisation "
            "FROM assignments a JOIN scripts s ON s.id=a.script_id "
            "WHERE (a.pair_id=? OR ((a.ua=? AND a.ub=?) OR (a.ua=? AND a.ub=?))) AND a.status NOT IN ('RELEASED','ABANDONED') ORDER BY a.id ASC",
            (pid, p["a"], p["b"], p["b"], p["a"])
        ).fetchall()

        scripts_data = []
        for a in assignments:
            m, s = divmod(int(round(float(a["conf_min"] or 0) * 60)), 60)
            time_disp = f"{m:02d}:{s:02d}" if a["conf_min"] else "-"
            dialogue = get_script_content_text(c, a["script_id"], a["code"])

            rev_stat = (a["review_status"] or "").upper()
            raw_stat = (a["status"] or "").upper()
            if rev_stat in ("APPROVED", "ACCEPTED") or raw_stat in ("APPROVED", "ACCEPTED", "CONFIRMED"):
                stat = "ACCEPTED"
            elif rev_stat == "REJECTED" or raw_stat == "REJECTED":
                stat = "REJECTED"
            elif raw_stat in ("SUBMITTED", "COMPLETED") or a["conf_min"]:
                stat = "SUBMITTED"
            elif raw_stat in ("ASSIGNED", "IN_SESSION"):
                stat = "ASSIGNED"
            else:
                stat = raw_stat or "ASSIGNED"

            sub_date = "-"
            if a["submitted_at"]:
                sub_date = datetime.fromtimestamp(a["submitted_at"]).strftime("%d %b %Y, %H:%M")
            elif a["assigned_at"]:
                sub_date = "Assigned " + datetime.fromtimestamp(a["assigned_at"]).strftime("%d %b %Y")

            scripts_data.append({
                "assignment_id": a["id"],
                "script_id": a["script_id"],
                "code": a["code"],
                "domain": a["domain"],
                "subdomain": a["subdomain"],
                "specialisation": a["specialisation"] or "-",
                "taxonomy": f"{a['domain']} > {a['subdomain']}" + (f" > {a['specialisation']}" if a['specialisation'] else ""),
                "status": stat,
                "raw_status": a["status"],
                "time_taken": time_disp,
                "submitted_at": sub_date,
                "can_release": (stat == "ASSIGNED" and not a["conf_min"] and not a["proof_img"] and not a["submitted_at"]),
                "dialogue": dialogue
            })

        ph = ops.get_hours(c, "pair", pid)
        comp_cnt = len([s for s in scripts_data if s['status'] in ('APPROVED','ACCEPTED','COMPLETED','SUBMITTED')])
        pend_cnt = max(0, len(scripts_data) - comp_cnt)
        pair_status = "Limit reached" if ph["limit_reached"] else (p["status"] or "Active")

        return {
            "pair_id": pid,
            "pair_name": f"{p['name_a']} & {p['name_b']}",
            "name_a": p["name_a"],
            "phone_a": p["phone_a"] or "-",
            "avail_a": p["avail_a"] or "All day",
            "gender_a": p["gender_a"] or "-",
            "name_b": p["name_b"],
            "phone_b": p["phone_b"] or "-",
            "avail_b": p["avail_b"] or "All day",
            "gender_b": p["gender_b"] or "-",
            "lang_name": p["lang_name"],
            "availability": p["availability"] or "All day",
            "pair_status": pair_status,
            "assigned_count": len(scripts_data),
            "completed_count": comp_cnt,
            "pending_count": pend_cnt,
            "recorded_h": ph["recorded_h"],
            "accepted_h": ph["accepted_h"],
            "scripts": scripts_data
        }


@router.post("/admin/pairs/{pid}/release/{aid}")
async def pair_release_single_script(request: Request, pid: int, aid: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.tx() as c:
        p = c.execute("SELECT a, b, language_id FROM pairs WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "Pair not found")
        if not ops.can_manage(u, p["language_id"]):
            raise HTTPException(403, "This pair belongs to another language.")
        asg = c.execute(
            "SELECT * FROM assignments WHERE id=? AND (pair_id=? OR ((ua=? AND ub=?) OR (ua=? AND ub=?)))",
            (aid, pid, p["a"], p["b"], p["b"], p["a"])
        ).fetchone()
        if not asg:
            raise HTTPException(404, "Assignment not found")
        if asg["status"] in ("SUBMITTED", "COMPLETED", "ACCEPTED", "CONFIRMED") or asg["conf_min"] or asg["proof_img"] or asg["submitted_at"]:
            raise HTTPException(400, "Completed or submitted scripts cannot be released and remain in history.")
        if asg["status"] not in ("ASSIGNED", "IN_SESSION"):
            raise HTTPException(400, "Only open unrecorded scripts can be released.")
        now = time.time()
        c.execute("UPDATE assignments SET status='RELEASED', updated_at=? WHERE id=?", (now, aid))
        c.execute("UPDATE scripts SET status='READY', updated_at=? WHERE id=?", (now, asg["script_id"]))
        sc = c.execute("SELECT code FROM scripts WHERE id=?", (asg["script_id"],)).fetchone()
        db.audit(c, u, "release_script", f"pair={pid} assignment={aid} script={asg['script_id']}")
    return {"success": True, "message": f"Script {sc['code'] if sc else ''} released back to Available pool."}


@router.post("/admin/pairs/{pid}/release_all")
async def pair_release_all_scripts(request: Request, pid: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    with db.tx() as c:
        p = c.execute("SELECT a, b, language_id FROM pairs WHERE id=?", (pid,)).fetchone()
        if not p:
            raise HTTPException(404, "Pair not found")
        if not ops.can_manage(u, p["language_id"]):
            raise HTTPException(403, "This pair belongs to another language.")
        open_asgs = c.execute(
            "SELECT a.id, a.script_id FROM assignments a WHERE (a.pair_id=? OR ((a.ua=? AND a.ub=?) OR (a.ua=? AND a.ub=?))) AND a.status IN ('ASSIGNED','IN_SESSION') AND a.conf_min IS NULL AND a.proof_img IS NULL AND a.submitted_at IS NULL",
            (pid, p["a"], p["b"], p["b"], p["a"])
        ).fetchall()
        now = time.time()
        for asg in open_asgs:
            c.execute("UPDATE assignments SET status='RELEASED', updated_at=? WHERE id=?", (now, asg["id"]))
            c.execute("UPDATE scripts SET status='READY', updated_at=? WHERE id=?", (now, asg["script_id"]))
        db.audit(c, u, "release_all_pair_scripts", f"pair={pid} count={len(open_asgs)}")
    return {"success": True, "count": len(open_asgs), "message": f"Released {len(open_asgs)} scripts back to Available pool."}


@router.post("/admin/assign/execute")
@router.post("/admin/assignments/assign")
async def admin_assign_execute_post(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    try:
        count = min(500, max(1, int(f.get("count", 2))))
    except (ValueError, TypeError):
        count = 2
    req_lid = f.get("language_id") or request.query_params.get("lang")
    allowed = u["lang_ids"] if u["role"] in ("lead", "supervisor") else None
    if req_lid in (None, "", "all"):
        target_lids = allowed  # None = every language for admin
    elif str(req_lid).isdigit() and (allowed is None or int(req_lid) in allowed):
        target_lids = [int(req_lid)]
    else:
        target_lids = allowed

    total_assigned, notes = 0, []
    with db.tx() as c:
        q = "SELECT id, language_id FROM pairs WHERE status='ACTIVE'"
        args = ()
        if target_lids is not None:
            q += " AND language_id IN (%s)" % ",".join("?" * len(target_lids))
            args = tuple(target_lids)
        by_lang = {}
        for p in c.execute(q, args).fetchall():
            ph = ops.get_hours(c, "pair", p["id"])
            if ph["limit_reached"]:
                c.execute("UPDATE pairs SET status='LIMIT_REACHED' WHERE id=?", (p["id"],))
                continue
            by_lang.setdefault(p["language_id"], []).append(p["id"])
        # equal split per language: pairs of one language share one script pool
        for lid, pids in by_lang.items():
            made, got, nts = ops.assign_fair(c, u, pids, count, override=u["role"] in ("admin", "developer"))
            total_assigned += made
            notes.extend(f"Pair #{k}: {v}" for k, v in nts.items())
    redirect_url = f"/admin/assign?lang={target_lids[0]}" if (target_lids and len(target_lids) == 1) else "/admin/assign?lang=all"
    msg = f"Assigned {total_assigned} scripts, spread equally across active pairs (up to {count} each)."
    if notes:
        msg += " Notes: " + " | ".join(notes[:3])
    return back(redirect_url, m=msg)


@router.post("/admin/assign/pair/{pid}")
async def admin_assign_single_pair_post(request: Request, pid: int):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    count_raw = f.get("count", 1)
    try:
        count = min(500, max(1, int(count_raw)))
    except (ValueError, TypeError):
        count = 1
    override = u["role"] in ("admin", "developer")  # ops.assign only lets admins override caps; supervisors respect them
    with db.tx() as c:
        p = c.execute(
            "SELECT p.*, ua.name as name_a, ub.name as name_b "
            "FROM pairs p JOIN users ua ON ua.id=p.a JOIN users ub ON ub.id=p.b "
            "WHERE p.id=?",
            (pid,)
        ).fetchone()
        if not p:
            return back("/admin/assign", e="Pair not found.")
        try:
            made, note = ops.assign(c, u, pid, count, override=override)
            if not made:
                msg = f"Could not assign scripts to {p['name_a']} & {p['name_b']}"
                if note:
                    msg += f": {note}"
                return back(f"/admin/assign?lang={p['language_id']}", e=msg)
            short = f" (asked for {count}; {note or 'limit reached'})" if len(made) < count else ""
            return back(f"/admin/assign?lang={p['language_id']}", m=f"Assigned {len(made)} script(s) to {p['name_a']} & {p['name_b']}{short}.")
        except Exception as ex:
            return back(f"/admin/assign?lang={p['language_id']}", e=f"Assignment error: {str(ex)}")


@router.post("/admin/reports/review_single")
async def admin_reports_review_single(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    aid_str = f.get("assignment_id") or f.get("report_id")
    if not aid_str:
        raise HTTPException(400, "Missing report/assignment ID")
    aid = int(aid_str)
    verdict = (f.get("verdict") or "ACCEPT").upper()
    reason = (f.get("reason") or "").strip()
    try:
        with db.tx() as c:
            ops.review_report(c, u, aid, verdict, reason)
    except (ops.OpsError, PermissionError) as e:
        return back("/admin/reports", e=str(e))
    return back("/admin/reports", m=f"Report #{aid} marked as {verdict.lower()}ed.")


@router.post("/admin/reports/bulk_review")
async def admin_reports_bulk_review(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    f = await request.form()
    action = (f.get("action") or "accept").upper()
    selected_ids = f.getlist("selected_ids")
    verdict = "ACCEPT" if action == "ACCEPT" else "REJECT"
    count, failed = 0, []
    with db.tx() as c:
        for aid_str in selected_ids:
            try:
                ops.review_report(c, u, int(aid_str), verdict, f"Bulk {verdict.lower()}ed")
                count += 1
            except (ops.OpsError, PermissionError, ValueError) as e:
                failed.append(f"#{aid_str}: {e}")
    if failed:
        return back("/admin/reports", m=f"Bulk updated {count} report(s) to {verdict.lower()}ed.", e=f"{len(failed)} skipped - " + "; ".join(failed[:3]))
    return back("/admin/reports", m=f"Bulk updated {count} report(s) to {verdict.lower()}ed.")


def _export_filters(request, u):
    """Resolve (lang_id, sup_lang_ids, date_from, date_to) from query params + role scope."""
    sup = u["role"] in ("lead", "supervisor")
    sup_lang_ids = list(u["lang_ids"]) if sup else None
    req_lid = request.query_params.get("lang") or request.query_params.get("lang_id")
    lang_id = None
    if not sup and req_lid and str(req_lid).isdigit():
        lang_id = int(req_lid)
    date_from = (request.query_params.get("from") or "").strip() or None
    date_to = (request.query_params.get("to") or "").strip() or None
    return lang_id, sup_lang_ids, date_from, date_to


@router.get("/admin/reports/export_csv")
async def admin_reports_export_csv(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    lang_id, sup_lang_ids, date_from, date_to = _export_filters(request, u)
    with db.ro() as c:
        rows = reports_excel._fetch_sessions(c, lang_id=lang_id, date_from=date_from,
                                             date_to=date_to, sup_lang_ids=sup_lang_ids)
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(["S.No", "Date", "Language", "Topic", "Script Code", "User 1 (Host)",
                         "User 2 (Guest)", "Duration (mm:ss)", "Status", "Review", "Comment / Invalid Reason", "Proof"])
        for i, r in enumerate(rows):
            ts = r["submitted_at"] or r["assigned_at"]
            date_str = datetime.fromtimestamp(ts, IST).strftime("%d-%b-%Y %H:%M") if ts else ""
            topic = " / ".join([x for x in [r["domain"], r["subdomain"], r["specialisation"]] if x])
            writer.writerow([i + 1, date_str, r["lang"], topic, r["code"],
                             reports_excel._who(r["na"], r["ea"]), reports_excel._who(r["nb"], r["eb"]),
                             reports_excel._mmss(r["conf_min"]), r["status"] or "",
                             reports_excel._review_label(r["review_status"], r["status"]),
                             (r["review_note"] or "").strip(),
                             (reports_excel._proof_link(r)[0] or (r["proof_img"] or "").strip())])
        csv_bytes = buf.getvalue().encode("utf-8-sig")
    return Response(csv_bytes, media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=sessions_report.csv"})


@router.get("/admin/export/excel")
@router.get("/admin/reports/export_excel")
async def admin_export_excel(request: Request):
    u = gate(request, "admin", "lead", "supervisor", "developer")
    lang_id, sup_lang_ids, date_from, date_to = _export_filters(request, u)
    with db.ro() as c:
        excel_bytes = reports_excel.generate_sessions_report(
            c, lang_id=lang_id, date_from=date_from, date_to=date_to, sup_lang_ids=sup_lang_ids)
    fname = f"sessions_report_{datetime.now(IST).strftime('%Y%m%d_%H%M')}.xlsx"
    return Response(
        excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename={fname}"}
    )


@router.get("/admin/languages")
@router.get("/admin/languages/add")
async def admin_languages_get_redirect(request: Request):
    return Redirect("/admin")


@router.post("/admin/languages/add")
@router.post("/admin/language/add")
async def admin_languages_add_post(request: Request):
    u = gate(request, "admin", "developer")
    f = await request.form()
    name = (f.get("name") or "").strip()
    code = (f.get("code") or "").strip().upper()
    target = int(f.get("target") or 0)  # no per-language script target any more
    mode = (f.get("mode") or "conversation").strip()
    try:
        cap_pair_h = float(f.get("cap_pair_h") or 7.5)
    except Exception:
        cap_pair_h = 7.5
    try:
        cap_person_h = float(f.get("cap_person_h") or 15.0)
    except Exception:
        cap_person_h = 15.0
    cap_pair_min = cap_pair_h * 60.0
    cap_person_min = cap_person_h * 60.0

    if not name or not code:
        return back("/admin", e="Language name and code are required.")

    with db.tx() as c:
        if c.execute("SELECT 1 FROM languages WHERE code=? OR name=?", (code, name)).fetchone():
            return back("/admin", e=f"Language '{name}' ({code}) already exists.")
        c.execute(
            "INSERT INTO languages(code, name, target, cap_pair_min, cap_person_min, mode, created_at) VALUES(?,?,?,?,?,?,?)",
            (code, name, target, cap_pair_min, cap_person_min, mode, time.time())
        )
        db.audit(c, u, "add_language", f"{code} {name}")
    return back("/admin", m=f"Language {name} ({code}) added successfully.")


@router.post("/admin/languages/delete/{lid}")
@router.post("/admin/language/{lid}/delete")
@router.post("/admin/language/delete/{lid}")
async def admin_language_delete_post(request: Request, lid: int):
    u = gate(request, "admin", "developer")
    with db.tx() as c:
        lang = c.execute("SELECT * FROM languages WHERE id=?", (lid,)).fetchone()
        if not lang:
            return back("/admin", e="Language not found.")
        lang_name = lang["name"]
        lang_code = lang["code"]

        admin_ops.delete_language(c, u, lid)

    return back("/admin", m=f"Language '{lang_name}' ({lang_code}) and all its associated data deleted successfully.")


# ==============================================================================
# AD7 SUPERVISORS (Admin & Dev only)
# ==============================================================================

@router.get("/admin/supervisors")
async def admin_supervisors_get(request: Request):
    u = gate(request, "admin", "developer")
    with db.ro() as c:
        sups = c.execute(
            "SELECT u.*, l.name as lang_name FROM users u LEFT JOIN languages l ON l.id=u.language_id "
            "WHERE u.role IN ('lead', 'supervisor') ORDER BY u.id ASC"
        ).fetchall()
        sup_rows = []
        for r in sups:
            d = dict(r)
            ids = ops.user_lang_ids(c, d)
            d["lang_ids"] = ids
            d["lang_names"] = ", ".join(x["name"] for x in c.execute("SELECT name FROM languages WHERE id IN (%s) ORDER BY id" % ",".join("?" * len(ids)), ids)) if ids else "-"
            sup_rows.append(d)
        return render(request, "admin_supervisors.html", u, supervisors=sup_rows)


@router.post("/admin/supervisors/add")
async def admin_supervisors_add_post(request: Request):
    u = gate(request, "admin", "developer")
    f = await request.form()
    name = (f.get("name") or "").strip()
    lids = [int(x) for x in f.getlist("language_ids") if str(x).isdigit()] or ([int(f.get("language_id"))] if f.get("language_id") else [])
    if not lids:
        return back("/admin/supervisors", m="Pick at least one language.")
    phone = (f.get("phone") or "").strip()
    email = (f.get("email") or "").strip()
    with db.tx() as c:
        uid, acode = ops.create_user(c, u, name, "lead", lids[0], phone=phone, email=email)
        ops.set_supervisor_languages(c, u, uid, lids)
    return back("/admin/supervisors", m=f"Supervisor {name} created with Access Code: {acode}")


@router.post("/admin/supervisors/languages/{sid}")
async def admin_supervisors_languages_post(request: Request, sid: int):
    u = gate(request, "admin", "developer")
    f = await request.form()
    lids = [int(x) for x in f.getlist("language_ids") if str(x).isdigit()]
    if not lids:
        return back("/admin/supervisors", m="Pick at least one language.")
    with db.tx() as c:
        ops.set_supervisor_languages(c, u, sid, lids)
    return back("/admin/supervisors", m="Supervisor languages updated.")


@router.post("/admin/supervisors/edit/{sid}")
async def admin_supervisors_edit_post(request: Request, sid: int):
    u = gate(request, "admin", "developer")
    f = await request.form()
    name = (f.get("name") or "").strip()
    phone = (f.get("phone") or "").strip()
    email = (f.get("email") or "").strip()
    if not name:
        return back("/admin/supervisors", e="Name is required.")
    with db.tx() as c:
        row = c.execute("SELECT id FROM users WHERE id=? AND role IN ('lead','supervisor')", (sid,)).fetchone()
        if not row:
            return back("/admin/supervisors", e="Supervisor not found.")
        c.execute("UPDATE users SET name=?, phone=?, email=? WHERE id=?", (name, phone, email, sid))
        db.audit(c, u, "edit_supervisor", f"user={sid} name={name}")
    return back("/admin/supervisors", m=f"Supervisor '{name}' updated.")


@router.get("/admin/supervisors/login_as/{sid}")
async def admin_supervisors_login_as(request: Request, sid: int):
    """Admin opens a supervisor's own view (impersonate). 'Switch back' returns to admin."""
    u = gate(request, "admin", "developer")
    with db.tx() as c:
        target = c.execute("SELECT * FROM users WHERE id=? AND role IN ('lead','supervisor')", (sid,)).fetchone()
        if not target:
            raise HTTPException(404, "Supervisor not found")
        tok, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
        c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (tok, target["id"], csrf, time.time()))
        db.audit(c, u, "view_as_supervisor", f"supervisor={sid}")
    resp = RedirectResponse("/admin", 303)
    if not request.cookies.get("sf_return"):
        resp.set_cookie("sf_return", request.cookies.get("sf_session", ""), max_age=6 * 3600, httponly=True, samesite="lax")
    resp.set_cookie("sf_session", tok, max_age=ops.SESSION_TTL, httponly=True, samesite="lax")
    return resp


@router.post("/admin/supervisors/toggle/{sid}")
async def admin_supervisors_toggle_post(request: Request, sid: int):
    u = gate(request, "admin", "developer")
    with db.tx() as c:
        c.execute("UPDATE users SET active = (1 - active) WHERE id=?", (sid,))
        db.audit(c, u, "toggle_supervisor", f"user={sid}")
    return back("/admin/supervisors", m="Supervisor status updated.")


@router.post("/admin/supervisors/delete/{sid}")
async def admin_supervisors_delete_post(request: Request, sid: int):
    u = gate(request, "admin", "developer")
    f = await request.form()
    reason = (f.get("reason") or "Deleted by admin").strip()
    with db.tx() as c:
        db.audit(c, u, "delete_supervisor_reason", f"user={sid} reason={reason}")
        c.execute("UPDATE users SET active=0 WHERE id=?", (sid,))
        c.execute("DELETE FROM sessions WHERE user_id=?", (sid,))
    return back("/admin/supervisors", m=f"Supervisor deleted. Reason recorded.")


# ==============================================================================
# GROUP DV. DEVELOPER PANEL (DV1 - DV6)
# ==============================================================================

@router.get("/dev/login")
async def dev_login_route(request: Request):
    from .main import render
    return render(request, "login.html", None, err=request.query_params.get("e", ""), role="dev", tab="L1", code="")


@router.get("/admin/login")
async def admin_login_route(request: Request):
    from .main import render
    return render(request, "login.html", None, err=request.query_params.get("e", ""), role="admin", tab="L2", code="")


@router.get("/login")
async def code_login_route(request: Request):
    from .main import render
    return render(request, "login.html", None, err=request.query_params.get("e", ""), role="agent", tab="L4", code=request.query_params.get("code", ""))


_SECRET_KEYS = ("password", "pass", "key", "api_key", "access_token", "service_account", "service_account_json",
                "credentials_json", "postgres_url", "supabase_key", "google_api_key")


def _public_config(raw):
    """Connection config with secrets replaced by a short hint, safe to embed in the page."""
    try:
        cfg = json.loads(raw or "{}")
    except Exception:
        return {}
    if not isinstance(cfg, dict):
        return {}
    out = {}
    for k, v in cfg.items():
        if k in _SECRET_KEYS:
            out[k + "_hint"] = email_service.key_hint(v if isinstance(v, str) else json.dumps(v)) if v else ""
        else:
            out[k] = v
    return out


_ROLE_GROUPS = (("Agents", ("participant", "agent")), ("Supervisors", ("lead", "supervisor", "reviewer")),
                ("Admins", ("admin",)), ("Developers", ("developer",)))
_ERROR_WORDS = ("fail", "error", "denied", "rejected", "unauthor", "blocked", "refused", "invalid")


def _ago(ts, now):
    s = max(0, int(now - (ts or 0)))
    if s < 60:
        return "just now"
    if s < 3600:
        return f"{s // 60} min ago"
    if s < 86400:
        return f"{s // 3600} h ago"
    return f"{s // 86400} d ago"


def _overview(c, conns):
    """Plain-language system picture for the developer Overview tab: who is online, where data goes, what broke."""
    now = time.time()
    online_cut, today_cut = now - 300, now - 86400
    people = []
    for label, roles in _ROLE_GROUPS:
        qs = ",".join("?" * len(roles))
        r = c.execute(
            f"SELECT COUNT(*) total, "
            f"SUM(CASE WHEN COALESCE(last_seen,0)>? THEN 1 ELSE 0 END) online, "
            f"SUM(CASE WHEN COALESCE(last_seen,0)>? THEN 1 ELSE 0 END) today "
            f"FROM users WHERE active=1 AND role IN ({qs})", (online_cut, today_cut, *roles)).fetchone()
        people.append({"label": label, "total": r["total"] or 0, "online": r["online"] or 0, "today": r["today"] or 0})
    role_name = {r: label[:-1] for label, roles in _ROLE_GROUPS for r in roles}
    online_list = [{
        "name": r["name"], "role": role_name.get(r["role"], r["role"]), "lang": r["lang"] or "-",
        "ago": _ago(r["last_seen"], now)}
        for r in c.execute(
            "SELECT u.name, u.role, u.last_seen, l.name lang FROM users u LEFT JOIN languages l ON l.id=u.language_id "
            "WHERE u.active=1 AND COALESCE(u.last_seen,0)>? ORDER BY u.last_seen DESC LIMIT 40", (online_cut,)).fetchall()]

    def conn_status(*words):
        for cn in conns:
            hay = (cn["name"] + " " + cn["service"]).lower()
            if any(w in hay for w in words):
                return {"ok": cn["status"] == "Connected", "status": cn["status"], "tested": cn["last_tested_str"]}
        return {"ok": False, "status": "Not set up", "tested": "Never"}

    pg = config.using_postgres()
    flow = {
        "users": sum(p["online"] for p in people),
        "db": {"ok": True, "name": "Supabase Postgres" if pg else "SQLite file on the server",
               "note": "Main database in the cloud. Survives redeploys." if pg else
                       "Data is in a file on the Render server. A redeploy can wipe it unless Supabase backup is on."},
        "drive": conn_status("drive"),
        "sheets": conn_status("sheet"),
        "email": conn_status("email"),
        "supabase": conn_status("supabase"),
    }
    week = now - 7 * 86400
    like = " OR ".join("lower(message) LIKE ?" for _ in _ERROR_WORDS)
    errors = [{
        "when": datetime.fromtimestamp(r["ts"], IST).strftime("%d %b %H:%M") if r["ts"] else "-",
        "area": (r["category"] or "app").title(), "message": (r["message"] or "")[:300],
        "detail": (r["detail"] or "")[:600]}
        for r in c.execute(f"SELECT ts, category, message, detail FROM app_logs WHERE ts>? AND ({like}) "
                           "ORDER BY id DESC LIMIT 40", (week, *[f"%{w}%" for w in _ERROR_WORDS])).fetchall()]
    broken = [cn for cn in conns if cn["status"] != "Connected"]
    emails = c.execute("SELECT SUM(CASE WHEN message LIKE 'Email delivered%' THEN 1 ELSE 0 END) sent, "
                       "SUM(CASE WHEN message LIKE 'Email failed%' THEN 1 ELSE 0 END) failed "
                       "FROM app_logs WHERE category='email' AND ts>?", (today_cut,)).fetchone()
    return {"people": people, "online_list": online_list, "flow": flow, "errors": errors, "broken": broken,
            "emails_sent": emails["sent"] or 0, "emails_failed": emails["failed"] or 0,
            "now_str": datetime.fromtimestamp(now, IST).strftime("%d %b %H:%M")}


@router.get("/developer")
@router.get("/dev/{tab}")
async def developer_panel_get(request: Request, tab: str = "overview"):
    if tab == "login":
        return await dev_login_route(request)
    u = gate(request, "developer")
    with db.ro() as c:
        # Ensure Email Service connection exists in connections table
        has_email = c.execute("SELECT 1 FROM connections WHERE service IN ('Email', 'Email Service') OR name LIKE '%Email%'").fetchone()
        if not has_email:
            with db.tx() as tx_c:
                now = time.time()
                tx_c.execute("INSERT OR IGNORE INTO connections(name, service, status, last_tested, config) VALUES(?,?,?,?,?)",
                             ("Email Service", "Email Service", "Not set up", now, '{}'))

        conns_raw = c.execute("SELECT * FROM connections ORDER BY id ASC").fetchall()
        conns = []
        for row in conns_raw:
            lt_str = datetime.fromtimestamp(row["last_tested"], IST).strftime("%d %b %H:%M") if row["last_tested"] else "Never"
            conns.append({
                "id": row["id"],
                "name": row["name"],
                "service": row["service"],
                "status": row["status"],
                "last_tested_str": lt_str,
                "config_json": json.dumps(_public_config(row["config"])),
            })
        email_info = email_service.describe_config(c=c)
        overview = _overview(c, conns) if tab == "overview" else None

        keys_raw = c.execute("SELECT * FROM dev_keys ORDER BY id ASC").fetchall()
        api_keys = []
        for k in keys_raw:
            val = k["key_val"] or ""
            masked = (val[:8] + "••••••••" + val[-4:]) if len(val) > 12 else "••••••••"
            last4 = val[-4:] if len(val) >= 4 else val
            added_on = datetime.fromtimestamp(k["created_at"], IST).strftime("%d %b %Y") if k["created_at"] else "Recently"
            api_keys.append({
                "id": k["id"],
                "name": k["name"],
                "service": k["service"],
                "label": k["label"] or k["name"],
                "masked_val": masked,
                "last4": last4,
                "added_on_str": added_on,
                "status": k["status"]
            })

        admins_raw = c.execute("SELECT * FROM users WHERE role='admin' ORDER BY id ASC").fetchall()
        admins_list = []
        for ad in admins_raw:
            c_str = datetime.fromtimestamp(ad["created_at"], IST).strftime("%d %b %Y") if ad["created_at"] else "Recently"
            admins_list.append({
                "id": ad["id"],
                "name": ad["name"],
                "email": ad["email"],
                "active": ad["active"],
                "created_str": c_str
            })

        email_tmpls = email_service.get_email_templates(c)
        rules = {
            "session_min_min": db.setting(c, "session_min_min", "5"),
            "session_max_min": db.setting(c, "session_max_min", "15"),
            "pair_limit_h": db.setting(c, "pair_limit_h", "7.5"),
            "user_limit_h": db.setting(c, "user_limit_h", "15.0"),
            "client_url": db.setting(c, "client_url", "https://client-portal.recording-system.com"),
            "email_subject_template": email_tmpls["subject"],
            "email_body_template": email_tmpls["body"],
            "default_email_subject": email_service.DEFAULT_EMAIL_SUBJECT,
            "default_email_body": email_service.DEFAULT_EMAIL_BODY,
        }

        # Backups
        backup_files = []
        if os.path.exists(BACKUPS_DIR):
            for fname in sorted(os.listdir(BACKUPS_DIR), reverse=True):
                if fname.endswith(".db"):
                    p = os.path.join(BACKUPS_DIR, fname)
                    sz = f"{os.path.getsize(p) / (1024*1024):.2f} MB"
                    c_date = datetime.fromtimestamp(os.path.getmtime(p), IST).strftime("%d %b %Y %H:%M")
                    backup_files.append({"filename": fname, "size_str": sz, "created_str": c_date})

        drive_backups = []
        if drive.is_configured(c=c):
            try:
                drive_backups = drive.list_backups(c=c)
            except Exception:
                pass

        # Logs
        log_cat = request.query_params.get("cat", "all")
        logs_list = []
        if log_cat == "audit":
            logs_raw = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 50").fetchall()
            for l in logs_raw:
                ts_str = datetime.fromtimestamp(l["ts"], IST).strftime("%d %b %H:%M:%S") if l["ts"] else "-"
                logs_list.append({
                    "id": l["id"],
                    "ts_str": ts_str,
                    "actor": l["actor"],
                    "message": l["action"],
                    "action": l["action"],
                    "detail": l["detail"] or ""
                })
        else:
            q = "SELECT * FROM app_logs"
            params = []
            if log_cat not in ("all", ""):
                q += " WHERE category=?"
                params.append(log_cat)
            q += " ORDER BY id DESC LIMIT 50"
            logs_raw = c.execute(q, params).fetchall()
            if not logs_raw and log_cat == "all":
                logs_raw = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 50").fetchall()
                for l in logs_raw:
                    ts_str = datetime.fromtimestamp(l["ts"], IST).strftime("%d %b %H:%M:%S") if l["ts"] else "-"
                    logs_list.append({
                        "id": l["id"],
                        "ts_str": ts_str,
                        "actor": l["actor"],
                        "message": l["action"],
                        "action": l["action"],
                        "detail": l["detail"] or ""
                    })
            else:
                for l in logs_raw:
                    ts_str = datetime.fromtimestamp(l["ts"], IST).strftime("%d %b %H:%M:%S") if l["ts"] else "-"
                    logs_list.append({
                        "id": l["id"],
                        "ts_str": ts_str,
                        "actor": l["actor"],
                        "message": l["message"],
                        "action": l["message"],
                        "detail": l["detail"] or ""
                    })

        return render(request, "developer_panel.html", u,
                      dv_tab=tab,
                      log_cat=log_cat,
                      connections=conns,
                      api_keys=api_keys,
                      dev_keys=api_keys,
                      admins_list=admins_list,
                      admin_users=admins_list,
                      rules=rules,
                      backups=backup_files,
                      backups_list=backup_files,
                      drive_backups=drive_backups,
                      drive_configured=drive.is_configured(c=c),
                      email_info=email_info,
                      overview=overview,
                      logs_list=logs_list,
                      audit_logs=logs_list)


@router.post("/dev/connections/test/{cid}")
async def dev_conn_test_post(request: Request, cid: int):
    u = gate(request, "developer")
    with db.tx() as c:
        row = c.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()
        if not row:
            return back("/dev/connections", m=f"Connection #{cid} not found.")

        is_email = (row["service"] in ("Email", "Email Service")) or ("email" in (row["name"] or "").lower())
        if is_email:
            res = email_service.test_connection(c=c)
            now = time.time()
            new_status = "Connected" if res.ok else "Disconnected"
            c.execute("UPDATE connections SET status=?, last_tested=? WHERE id=?", (new_status, now, cid))
            db.audit(c, u, "test_connection", f"conn={cid} service=Email status={new_status} msg={res.message}")
            db.log_app(c, "email", u, f"Test Email connection: {new_status}", res.message)
            if res.ok:
                return back("/dev/connections", m=f"Email Service connection verified! {res.message}")
            else:
                return back("/dev/connections", e=f"Email Service test failed: {res.message}")
        elif (row["service"] == "Supabase") or ("supabase" in (row["name"] or "").lower()):
            res = sync.test_connection(c=c)
            now = time.time()
            new_status = "Connected" if res["ok"] else "Disconnected"
            c.execute("UPDATE connections SET status=?, last_tested=? WHERE id=?", (new_status, now, cid))
            db.audit(c, u, "test_connection", f"conn={cid} service=Supabase status={new_status} latency={res.get('latency_ms', 0)}ms")
            if res["ok"]:
                return back("/dev/connections", m=f"Supabase connection verified! Ping latency: {res.get('latency_ms', 0)}ms. {res.get('details', '')}")
            else:
                return back("/dev/connections", e=f"Supabase connection test failed: {res.get('error')}")
        elif (row["service"] in ("Google Sheets", "Sheets")) or ("sheet" in (row["name"] or "").lower()):
            res = sheets.test_connection(c=c)
            now = time.time()
            new_status = "Connected" if res.get("ok") else "Disconnected"
            c.execute("UPDATE connections SET status=?, last_tested=? WHERE id=?", (new_status, now, cid))
            db.audit(c, u, "test_connection", f"conn={cid} service=Google Sheets status={new_status} title={res.get('title')}")
            if res.get("ok"):
                return back("/dev/connections", m=f"Google Sheets connection verified! {res.get('message', '')}")
            else:
                return back("/dev/connections", e=f"Google Sheets test failed: {res.get('error') or res.get('message')}")
        elif (row["service"] in ("Google Drive", "Drive")) or ("drive" in (row["name"] or "").lower()):
            res = drive.test_connection(c=c)
            now = time.time()
            new_status = "Connected" if res.get("success") else "Failed"
            c.execute("UPDATE connections SET status=?, last_tested=? WHERE id=?", (new_status, now, cid))
            msg = res.get("message") if res.get("success") else res.get("error")
            db.audit(c, u, "test_connection", f"conn={cid} service=Google Drive status={new_status} msg={msg}")
            db.log_app(c, "drive", u, f"Test Google Drive connection: {new_status}", msg or "")
            if res.get("success"):
                return back("/dev/connections", m=f"Google Drive verified! {res.get('message')}")
            else:
                return back("/dev/connections", e=f"Google Drive test failed: {res.get('error')}")
        else:
            c.execute("UPDATE connections SET status='Connected', last_tested=? WHERE id=?", (time.time(), cid))
            db.audit(c, u, "test_connection", f"conn={cid}")
            return back("/dev/connections", m=f"Connection #{cid} verified and ping successful!")


@router.post("/dev/connections/sync/{cid}")
@router.post("/dev/connections/sync")
async def dev_conn_sync_post(request: Request, cid: int = None):
    u = gate(request, "developer")
    row = None
    if cid:
        with db.ro() as c:
            row = c.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()

    if row and ((row["service"] in ("Google Sheets", "Sheets")) or ("sheet" in (row["name"] or "").lower())):
        with db.tx() as c:
            res = sheets.sync_all(c=c)
        if res.get("ok"):
            return back("/dev/connections", m=res.get("message", "Google Sheets live sync complete!"))
        else:
            return back("/dev/connections", e=f"Google Sheets sync failed: {res.get('error')}")

    with db.ro() as c:
        if not sync.is_configured(c=c):
            return back("/dev/connections", e="Supabase connection is not configured. Please edit connection credentials first.")
    res = sync.upload_backup(force=True)
    with db.tx() as c:
        if res.get("ok"):
            db.audit(c, u, "cloud_sync", f"conn={cid or 'supabase'} bytes={res.get('bytes')}")
            return back("/dev/connections", m=f"Database backup ({res.get('bytes', 0)} bytes) successfully uploaded to Supabase Storage!")
        else:
            db.audit(c, u, "cloud_sync_failed", f"conn={cid or 'supabase'} err={res.get('error')}")
            return back("/dev/connections", e=f"Supabase sync failed: {res.get('error')}")


@router.post("/dev/connections/restore/{cid}")
@router.post("/dev/connections/restore")
async def dev_conn_restore_post(request: Request, cid: int = None):
    u = gate(request, "developer")
    with db.ro() as c:
        if not sync.is_configured(c=c):
            return back("/dev/connections", e="Supabase connection is not configured.")
    res = sync.restore_backup()
    with db.tx() as c:
        if res.get("ok"):
            db.audit(c, u, "cloud_restore", f"conn={cid or 'supabase'} bytes={res.get('bytes')}")
            return back("/dev/connections", m=f"Database restored successfully from Supabase Storage ({res.get('bytes', 0)} bytes).")
        else:
            db.audit(c, u, "cloud_restore_failed", f"conn={cid or 'supabase'} err={res.get('error')}")
            return back("/dev/connections", e=f"Supabase restore failed: {res.get('error')}")


@router.post("/dev/connections/update")
async def dev_conn_update_post(request: Request):
    u = gate(request, "developer")
    f = await request.form()
    cid = int(f.get("conn_id") or 0)
    url = (f.get("url") or "").strip()
    key = (f.get("key") or "").strip()
    bucket = (f.get("bucket") or "").strip()
    postgres_url = (f.get("postgres_url") or "").strip()
    with db.tx() as c:
        conn = c.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()
        if not conn:
            return back("/dev/connections", e="Connection not found.")
        cfg = {}
        if conn["config"]:
            try:
                cfg = json.loads(conn["config"])
            except Exception:
                cfg = {}
        if url:
            cfg["url"] = url
        if key:
            cfg["key"] = key
        if bucket:
            cfg["bucket"] = bucket
        if postgres_url:
            cfg["postgres_url"] = postgres_url
        c.execute("UPDATE connections SET config=? WHERE id=?", (json.dumps(cfg), cid))
        db.audit(c, u, "update_connection_config", f"conn={cid}")
    return back("/dev/connections", m=f"Connection #{cid} ({conn['name']}) settings updated.")


@router.post("/dev/connections/sync_tables")
async def dev_conn_sync_tables_post(request: Request):
    u = gate(request, "developer")
    with db.ro() as c:
        res = sync.sync_tables_direct(c=c)
    with db.tx() as c:
        db.audit(c, u, "sync_tables_direct", f"ok={res.get('ok')} mode={res.get('mode')}")
    if res.get("ok"):
        return back("/dev/connections", m=f"Direct table sync completed ({res.get('mode')} mode): {res.get('synced_tables')}")
    return back("/dev/connections", e=f"Direct table sync failed: {res.get('error')}")


@router.post("/admin/cloud_test")
async def admin_cloud_test_post(request: Request):
    u = gate(request, "admin", "lead", "developer")
    with db.ro() as c:
        res = sync.test_connection(c=c)
    if res["ok"]:
        return back("/admin/data", m=f"Supabase connection verified! Ping latency: {res.get('latency_ms', 0)}ms. {res.get('details', '')}")
    return back("/admin/data", e=f"Supabase connection test failed: {res.get('error')}")


@router.post("/admin/cloud_sync_tables")
async def admin_cloud_sync_tables_post(request: Request):
    u = gate(request, "admin", "developer")
    with db.ro() as c:
        res = sync.sync_tables_direct(c=c)
    if res.get("ok"):
        return back("/admin/data", m=f"Tables successfully synced to cloud: {res.get('synced_tables')}")
    return back("/admin/data", e=f"Table sync failed: {res.get('error')}")


@router.post("/dev/connections/edit/{cid}")
async def dev_conn_edit_post(request: Request, cid: int):
    u = gate(request, "developer")
    f = await request.form()
    with db.tx() as c:
        row = c.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()
        if not row:
            return back("/dev/connections", m=f"Connection #{cid} not found.")

        name = (f.get("name") or row["name"]).strip()
        service = (f.get("service") or row["service"]).strip()
        is_email = (service in ("Email", "Email Service")) or ("email" in name.lower()) or bool(f.get("smtp_host"))

        if is_email:
            existing_cfg = {}
            if row["config"]:
                try:
                    existing_cfg = json.loads(row["config"])
                except Exception:
                    existing_cfg = {}
            new_pass = email_service.clean_key(f.get("smtp_pass") or "")
            if not new_pass:
                new_pass = existing_cfg.get("password") or ""

            cfg = {
                "host": (f.get("smtp_host") or existing_cfg.get("host") or "smtp.gmail.com").strip(),
                "port": int(f.get("smtp_port") or existing_cfg.get("port") or 587),
                "user": (f.get("smtp_user") or existing_cfg.get("user") or "").strip(),
                "password": new_pass,
                "from_email": (f.get("smtp_from") or existing_cfg.get("from_email") or "notifications@scriptflow.com").strip(),
                "tls": f.get("smtp_tls") in ("1", "true", "on", "yes", True) if "smtp_tls" in f else existing_cfg.get("tls", True),
            }
            c.execute("UPDATE connections SET name=?, service=?, config=? WHERE id=?", (name, service, json.dumps(cfg), cid))
            email_service.save_email_config(cfg, c=c)
            db.audit(c, u, "update_connection", f"conn={cid} service=Email host={cfg['host']}")
            db.log_app(c, "email", u, f"Updated Email Service config #{cid}", f"host={cfg['host']} port={cfg['port']}")
        elif (service == "Supabase") or ("supabase" in name.lower()) or bool(f.get("url")) or bool(f.get("supabase_url")):
            existing_cfg = {}
            if row["config"]:
                try:
                    existing_cfg = json.loads(row["config"])
                except Exception:
                    existing_cfg = {}
            cfg = dict(existing_cfg)
            url_val = (f.get("url") or f.get("supabase_url") or "").strip().rstrip("/")
            if url_val:
                cfg["url"] = url_val
                db.set_setting(c, "supabase_url", url_val)
            key_val = (f.get("key") or f.get("supabase_key") or "").strip()
            if key_val:
                cfg["key"] = key_val
                db.set_setting(c, "supabase_key", key_val)
            bucket_val = (f.get("bucket") or f.get("supabase_bucket") or "").strip()
            if bucket_val:
                cfg["bucket"] = bucket_val
                db.set_setting(c, "supabase_bucket", bucket_val)
            pg_val = (f.get("postgres_url") or f.get("supabase_postgres_url") or "").strip()
            if pg_val:
                cfg["postgres_url"] = pg_val
                db.set_setting(c, "supabase_postgres_url", pg_val)
            c.execute("UPDATE connections SET name=?, service=?, config=? WHERE id=?", (name, service, json.dumps(cfg), cid))
            db.audit(c, u, "update_connection", f"conn={cid} service=Supabase url={cfg.get('url')}")
        elif (service in ("Google Drive", "Drive")) or ("drive" in name.lower()) or bool(f.get("folder_id")):
            existing_cfg = {}
            if row["config"]:
                try:
                    existing_cfg = json.loads(row["config"])
                except Exception:
                    existing_cfg = {}
            cfg = dict(existing_cfg)
            if (f.get("folder_id") or "").strip():
                cfg["folder_id"] = f.get("folder_id").strip()
            # Secrets are never sent to the page, so a blank box means "keep the saved value".
            for k in ("api_key", "service_account_json", "credentials_path", "access_token"):
                if (f.get(k) or "").strip():
                    cfg[k] = f.get(k).strip()
            c.execute("UPDATE connections SET name=?, service=?, config=? WHERE id=?", (name, service, json.dumps(cfg), cid))
            drive.save_drive_config(
                folder_id=cfg.get("folder_id"),
                service_account_json=cfg.get("service_account_json"),
                api_key=cfg.get("api_key"),
                credentials_path=cfg.get("credentials_path"),
                access_token=cfg.get("access_token"),
                c=c
            )
            db.audit(c, u, "update_connection", f"conn={cid} service=Google Drive folder={cfg.get('folder_id')}")
            db.log_app(c, "drive", u, f"Updated Google Drive config #{cid}", f"folder={cfg.get('folder_id')}")
        elif (service in ("Google Sheets", "Sheets")) or ("sheet" in name.lower()) or bool(f.get("sheet_id")) or bool(f.get("google_sheet_id")):
            sheet_id = (f.get("sheet_id") or f.get("google_sheet_id") or "").strip()
            api_key = (f.get("api_key") or f.get("google_api_key") or "").strip()
            service_account = (f.get("service_account") or f.get("google_service_account") or "").strip()
            access_token = (f.get("access_token") or f.get("google_access_token") or "").strip()
            sheets.save_sheets_config(
                c,
                sheet_id=sheet_id if sheet_id else None,
                api_key=api_key if api_key else None,
                service_account=service_account if service_account else None,
                access_token=access_token if access_token else None,
                name=name
            )
            db.audit(c, u, "update_connection", f"conn={cid} service=Google Sheets sheet_id={sheet_id}")
            try:
                db.log_app(c, "sheets", u, f"Updated Google Sheets config #{cid}", f"sheet_id={sheet_id}")
            except Exception:
                pass
        else:
            c.execute("UPDATE connections SET name=?, service=? WHERE id=?", (name, service, cid))
            db.audit(c, u, "update_connection", f"conn={cid} service={service}")

    return back("/dev/connections", m=f"Connection '{name}' updated successfully.")


_EMAIL_PROVIDER_HOST = {
    "brevo": ("smtp-relay.brevo.com", 587),
    "resend": ("api.resend.com", 443),
    "sendgrid": ("smtp.sendgrid.net", 587),
    "gmail": ("smtp.gmail.com", 587),
}


@router.post("/dev/email/setup")
async def dev_email_setup_post(request: Request):
    """One-screen email provider setup. Pick a provider, paste the API key (or Gmail app
    password), set the sender. Brevo/Resend send over HTTPS (port 443) so they work on Render."""
    u, f = await _post_form(request, "developer")
    provider = (f.get("provider") or "brevo").strip().lower()
    api_key = email_service.clean_key(f.get("api_key") or "") if provider != "gmail" else (f.get("api_key") or "").strip()
    from_email = (f.get("from_email") or "").strip()
    from_name = (f.get("from_name") or "").strip()
    gmail_user = (f.get("gmail_user") or "").strip()
    host, port = _EMAIL_PROVIDER_HOST.get(provider, ("smtp-relay.brevo.com", 587))
    with db.tx() as c:
        current = email_service.get_email_config(c)
    # Blank key box = keep the saved key (only if it belongs to the same provider).
    if not api_key and current.get("password_source") == "saved" and \
            email_service._detect_provider(current) == email_service._detect_provider({"host": host}):
        api_key = current["password"]
    if provider == "brevo" and api_key.startswith("xsmtpsib-"):
        return back("/dev/connections", e=email_service.key_problem("brevo", api_key) + " Nothing was saved.")
    cfg = {
        "host": host,
        "port": port,
        "user": gmail_user if provider == "gmail" else "",
        "password": api_key,
        "from_email": from_email or current.get("from_email") or (gmail_user if provider == "gmail" else "notifications@scriptflow.com"),
        "from_name": from_name or current.get("from_name", ""),
        "tls": True,
    }
    with db.tx() as c:
        email_service.save_email_config(cfg, c=c)
        res = email_service.test_connection(c=c)
        status = "Connected" if res.ok else "Disconnected"
        c.execute("UPDATE connections SET status=?, last_tested=? WHERE service IN ('Email','Email Service') OR name LIKE '%Email%'",
                  (status, time.time()))
        db.audit(c, u, "email_setup", f"provider={provider} key={email_service.key_hint(api_key)} ok={res.ok}")
        db.log_app(c, "email", u, f"Email provider set to {provider}: {status}", res.message)
    if res.ok:
        return back("/dev/connections", m=f"Email saved and verified. {res.message} Now send yourself a test email.")
    return back("/dev/connections", e=f"Email settings saved, but the check failed: {res.message}")


@router.post("/dev/email/verify")
async def dev_email_verify_post(request: Request):
    u, _ = await _post_form(request, "developer")
    with db.tx() as c:
        res = email_service.test_connection(c=c)
        c.execute("UPDATE connections SET status=?, last_tested=? WHERE service IN ('Email','Email Service') OR name LIKE '%Email%'",
                  ("Connected" if res.ok else "Disconnected", time.time()))
        db.log_app(c, "email", u, f"Email verify: {'ok' if res.ok else 'failed'}", res.message)
    if res.ok:
        return back("/dev/connections", m=f"Email key verified. {res.message}")
    return back("/dev/connections", e=f"Email key check failed: {res.message}")


@router.post("/dev/email/test")
async def dev_email_test_post(request: Request):
    u = gate(request, "developer")
    f = await request.form()
    to = (f.get("to") or "").strip()
    if not to:
        return back("/dev/connections", e="Enter an email address to send the test to.")
    with db.tx() as c:
        res = email_service.send_email(
            to, "ScriptFactory test email",
            "This is a test email from ScriptFactory. If you received it, your email delivery is working correctly.",
            c=c, actor=db.actor_name(u))
    if getattr(res, "ok", False) or (isinstance(res, dict) and res.get("ok")):
        prov = (res.get("provider") if isinstance(res, dict) else "") or ""
        return back("/dev/connections", m=f"Test email sent to {to}{(' via ' + prov) if prov else ''}. Check the inbox (and spam).")
    err = res.get("error") if isinstance(res, dict) else "unknown error"
    return back("/dev/connections", e=f"Test email failed: {err}")


@router.post("/dev/drive/setup")
async def dev_drive_setup_post(request: Request):
    """Easy Google Drive setup: folder ID + service-account JSON. Drive becomes the primary
    image store; Supabase keeps a safety copy."""
    u = gate(request, "developer")
    f = await request.form()
    folder_id = (f.get("folder_id") or "").strip()
    sa_json = (f.get("service_account_json") or "").strip()
    with db.tx() as c:
        if folder_id:
            db.set_setting(c, "google_drive_folder_id", folder_id)
        if sa_json:
            db.set_setting(c, "google_service_account_json", sa_json)
        row = c.execute("SELECT id FROM connections WHERE service='Google Drive' OR name LIKE '%Drive%' ORDER BY id ASC LIMIT 1").fetchone()
        if row:
            c.execute("UPDATE connections SET status='Connected' WHERE id=?", (row["id"],))
        db.audit(c, u, "drive_setup", f"folder={folder_id[:16]}")
        db.log_app(c, "drive", u, "Google Drive settings saved", f"folder={folder_id[:16]}")
    return back("/dev/connections", m="Google Drive settings saved. Click 'Test Drive' to verify.")


@router.post("/dev/drive/test")
async def dev_drive_test_post(request: Request):
    u = gate(request, "developer")
    with db.tx() as c:
        if not drive.is_configured(c=c):
            return back("/dev/connections", e="Google Drive not configured: add a folder ID and service-account JSON first.")
        try:
            res = drive.test_connection(c=c)
        except Exception as e:
            return back("/dev/connections", e=f"Google Drive test error: {e}")
        ok = bool(res.get("ok") or res.get("success") or res.get("status") in ("Connected", "ok"))
        row = c.execute("SELECT id FROM connections WHERE service='Google Drive' OR name LIKE '%Drive%' ORDER BY id ASC LIMIT 1").fetchone()
        if row:
            c.execute("UPDATE connections SET status=?, last_tested=? WHERE id=?",
                      ("Connected" if ok else "Disconnected", time.time(), row["id"]))
    if ok:
        return back("/dev/connections", m=f"Google Drive connected. {res.get('details', res.get('message',''))}")
    return back("/dev/connections", e=f"Google Drive test failed: {res.get('error', res.get('message','unknown'))}")


@router.post("/dev/sheets/sync")
async def dev_sheets_sync_post(request: Request):
    u = gate(request, "developer")
    with db.tx() as c:
        res = sheets.sync_all(c=c)
    if res.get("ok"):
        return back("/dev/connections", m=res.get("message", "Google Sheets mirror updated successfully!"))
    return back("/dev/connections", e=f"Google Sheets sync failed: {res.get('error')}")


@router.post("/dev/sheets/test")
async def dev_sheets_test_post(request: Request):
    u = gate(request, "developer")
    with db.ro() as c:
        res = sheets.test_connection(c=c)
    if res.get("ok"):
        return back("/dev/connections", m=res.get("message", "Google Sheets connected successfully!"))
    return back("/dev/connections", e=f"Google Sheets test failed: {res.get('error')}")


@router.post("/admin/sheets/sync")
async def admin_sheets_sync_post(request: Request):
    u = gate(request, "admin", "lead", "developer")
    with db.tx() as c:
        res = sheets.sync_all(c=c)
        if res.get("ok"):
            db.audit(c, u, "admin_sheets_sync", f"agents={res.get('agents',{}).get('rows')} pairs={res.get('assignments',{}).get('rows')} scripts={res.get('scripts',{}).get('rows')}")
    target_url = request.headers.get("referer") or "/admin"
    if res.get("ok"):
        return back(target_url, m=res.get("message", "Google Sheets live sync complete! Mirror updated."))
    return back(target_url, e=f"Google Sheets sync failed: {res.get('error')}")


@router.post("/admin/sheets/test")
async def admin_sheets_test_post(request: Request):
    u = gate(request, "admin", "lead", "developer")
    with db.ro() as c:
        res = sheets.test_connection(c=c)
    if res.get("ok"):
        return back("/admin", m=res.get("message", "Google Sheets connection verified!"))
    return back("/admin", e=f"Google Sheets connection test failed: {res.get('error')}")


@router.post("/api/sheets/sync")
async def api_sheets_sync_post(request: Request):
    u = gate(request, "admin", "developer")
    with db.tx() as c:
        res = sheets.sync_all(c=c)
    return JSONResponse(status_code=200 if res.get("ok") else 400, content=res)


@router.get("/api/sheets/test")
@router.post("/api/sheets/test")
async def api_sheets_test_post(request: Request):
    u = gate(request, "admin", "developer")
    with db.ro() as c:
        res = sheets.test_connection(c=c)
    return JSONResponse(status_code=200 if res.get("ok") else 400, content=res)


@router.post("/dev/connections/backup/{cid}")
async def dev_conn_backup_post(request: Request, cid: int):
    u = gate(request, "developer")
    with db.tx() as c:
        row = c.execute("SELECT * FROM connections WHERE id=?", (cid,)).fetchone()
        if not row:
            return back("/dev/connections", e=f"Connection #{cid} not found.")
        is_drive = (row["service"] in ("Google Drive", "Drive")) or ("drive" in (row["name"] or "").lower())
        if not is_drive:
            return back("/dev/connections", e=f"Manual backup trigger not supported for {row['service']}.")

        db_file = config.db_path()
        if not os.path.exists(db_file):
            return back("/dev/connections", e="Database file (factory.db) not found.")

        try:
            res = drive.upload_backup(db_file, c=c)
            db.audit(c, u, "drive_backup", f"file={res.get('name')} id={res.get('id')}")
            db.log_app(c, "drive", u, f"Manual Drive backup uploaded #{cid}", res.get("name"))
            return back("/dev/connections", m=f"Database snapshot '{res.get('name')}' successfully uploaded to Google Drive!")
        except Exception as ex:
            db.audit(c, u, "drive_backup_failed", str(ex))
            return back("/dev/connections", e=f"Failed to upload backup to Google Drive: {str(ex)}")


@router.post("/dev/backups/drive_backup")
async def dev_backups_drive_backup_post(request: Request):
    u = gate(request, "developer")
    db_file = config.db_path()
    if not os.path.exists(db_file):
        return back("/dev/backups", e="Database file (factory.db) not found.")
    try:
        res = drive.upload_backup(db_file)
        with db.tx() as c:
            db.audit(c, u, "drive_backup", f"file={res.get('name')} id={res.get('id')}")
            db.log_app(c, "drive", u, "Manual Drive backup created from DV5", res.get("name"))
        return back("/dev/backups", m=f"Database successfully backed up to Google Drive: {res.get('name')}")
    except Exception as ex:
        return back("/dev/backups", e=f"Google Drive backup failed: {str(ex)}")


@router.post("/dev/keys/add")
async def dev_key_add_post(request: Request):
    u = gate(request, "developer")
    f = await request.form()
    name = (f.get("name") or "").strip()
    service = (f.get("service") or "").strip()
    key_val = (f.get("key_val") or "").strip()
    with db.tx() as c:
        c.execute("INSERT INTO dev_keys(name, service, label, key_val, status, created_at) VALUES(?,?,?,?, 'Active', ?)",
                  (name, service, name, key_val, time.time()))
        db.audit(c, u, "add_dev_key", f"service={service} name={name}")
    return back("/dev/keys", m=f"Key for {service} added securely.")


@router.post("/dev/keys/replace")
async def dev_key_replace_post(request: Request):
    u = gate(request, "developer")
    f = await request.form()
    kid = int(f.get("key_id"))
    new_val = (f.get("key_val") or "").strip()
    with db.tx() as c:
        c.execute("UPDATE dev_keys SET key_val=?, status='Active' WHERE id=?", (new_val, kid))
        db.audit(c, u, "replace_dev_key", f"key={kid}")
    return back("/dev/keys", m="API Key replaced and active.")


@router.post("/dev/keys/revoke/{kid}")
async def dev_key_revoke_post(request: Request, kid: int):
    u = gate(request, "developer")
    with db.tx() as c:
        c.execute("UPDATE dev_keys SET status='Revoked' WHERE id=?", (kid,))
        db.audit(c, u, "revoke_dev_key", f"key={kid}")
    return back("/dev/keys", m="API Key revoked.")


@router.post("/dev/admins/create")
async def dev_admin_create_post(request: Request):
    u = gate(request, "developer")
    f = await request.form()
    name = (f.get("name") or "").strip()
    email = (f.get("email") or "").strip().lower()
    pwd = (f.get("password") or "").strip()
    p_hash, p_salt = ops.hash_password(pwd)
    with db.tx() as c:
        if c.execute("SELECT 1 FROM users WHERE lower(email)=?", (email,)).fetchone():
            return back("/dev/admins", e="Email already in use.")
        c.execute("INSERT INTO users(name, email, role, code, active, password_hash, password_salt, created_at) VALUES(?,?,'admin',?,1,?,?,?)",
                  (name, email, codes.new_code(), p_hash, p_salt, time.time()))
        db.audit(c, u, "create_admin", f"email={email}")
    return back("/dev/admins", m=f"Admin {name} created successfully.")


@router.post("/dev/admins/reset_pwd")
async def dev_admin_reset_pwd_post(request: Request):
    u = gate(request, "developer")
    f = await request.form()
    uid = int(f.get("user_id"))
    new_pwd = (f.get("password") or "").strip()
    p_hash, p_salt = ops.hash_password(new_pwd)
    with db.tx() as c:
        c.execute("UPDATE users SET password_hash=?, password_salt=? WHERE id=?", (p_hash, p_salt, uid))
        db.audit(c, u, "reset_admin_password", f"user={uid}")
    return back("/dev/admins", m="Admin password updated successfully.")


@router.post("/dev/admins/suspend/{uid}")
async def dev_admin_suspend_post(request: Request, uid: int):
    u = gate(request, "developer")
    with db.tx() as c:
        c.execute("UPDATE users SET active = (1 - active) WHERE id=?", (uid,))
        db.audit(c, u, "toggle_admin_status", f"user={uid}")
    return back("/dev/admins", m="Admin status updated.")


@router.post("/dev/admins/delete/{uid}")
async def dev_admin_delete_post(request: Request, uid: int):
    u = gate(request, "developer")
    f = await request.form()
    reason = (f.get("reason") or "Deleted by developer").strip()
    with db.tx() as c:
        db.audit(c, u, "delete_admin_reason", f"user={uid} reason={reason}")
        c.execute("DELETE FROM users WHERE id=? AND role='admin'", (uid,))
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    return back("/dev/admins", m="Admin account removed.")


@router.post("/dev/rules/update")
async def dev_rules_update_post(request: Request):
    u = gate(request, "developer")
    f = await request.form()
    with db.tx() as c:
        for k in ["session_min_min", "session_max_min", "pair_limit_h", "user_limit_h", "client_url", "email_subject_template", "email_body_template"]:
            if f.get(k) is not None:
                c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES(?, ?)", (k, str(f.get(k)).strip()))
        db.audit(c, u, "update_rules", "dynamic thresholds and email template updated")
    return back("/dev/rules", m="System rules and email templates updated successfully.")


@router.post("/dev/email_template/update")
async def dev_email_template_update_post(request: Request):
    u = gate(request, "developer")
    f = await request.form()
    subj = (f.get("email_subject_template") or "").strip()
    body = (f.get("email_body_template") or "").strip()
    with db.tx() as c:
        email_service.save_email_templates(subj, body, c=c)
        db.audit(c, u, "update_email_template", f"subject={subj[:60]}")
    return back("/dev/rules", m="Access code email subject and body templates saved!")


@router.post("/dev/backups/create")
async def dev_backup_create_post(request: Request):
    u = gate(request, "developer")
    src_db = config.db_path()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst_db = os.path.join(BACKUPS_DIR, f"scriptflow_backup_{timestamp}.db")
    src = sqlite3.connect(src_db)
    dst = sqlite3.connect(dst_db)
    src.backup(dst)
    dst.close()
    src.close()
    with db.tx() as c:
        db.audit(c, u, "create_backup", f"file={os.path.basename(dst_db)}")
    return back("/dev/backups", m=f"Backup snapshot created: {os.path.basename(dst_db)}")


@router.post("/dev/backups/restore/{fname}")
async def dev_backup_restore_post(request: Request, fname: str):
    u = gate(request, "developer")
    src_p = os.path.join(BACKUPS_DIR, fname)
    if not os.path.exists(src_p):
        return back("/dev/backups", e="Backup file not found.")
    dst_db = config.db_path()
    shutil.copy2(src_p, dst_db)
    with db.tx() as c:
        db.audit(c, u, "restore_backup", f"file={fname}")
    return back("/dev/backups", m=f"Database restored successfully from {fname}.")


@router.get("/dev/backups/download")
async def dev_backup_download_get(request: Request):
    u = gate(request, "developer")
    db_file = config.db_path()
    return FileResponse(db_file, filename="scriptflow_live.db", media_type="application/octet-stream")
