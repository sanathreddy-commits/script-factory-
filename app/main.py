import json
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import admin_ops, codes, config, db, ops
from . import pipeline as P
from . import taxonomy as T
from . import validators as V

HERE = os.path.dirname(os.path.abspath(__file__))
IST = ZoneInfo("Asia/Kolkata")


# ---------------------------------------------------------------- startup
def ensure_admin():
    with db.tx() as c:
        if not c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
            code = os.environ.get("SF_ADMIN_CODE") or codes.new_code()
            if not codes.valid(code):
                raise SystemExit("SF_ADMIN_CODE is not a valid 8-char code (no I, L, O, 0, 1; last char is a check char).")
            c.execute("INSERT INTO users(name,role,code,created_at) VALUES('Admin','admin',?,?)", (code, time.time()))
            d = os.path.dirname(config.db_path())
            os.makedirs(d, exist_ok=True)
            with open(os.path.join(d, "ADMIN_CODE.txt"), "w") as f:
                f.write(code + "\n")
            print(f"\n  ADMIN ACCESS CODE: {codes.fmt(code)}   (also saved next to the database as ADMIN_CODE.txt)\n")


def sweeper():
    last_backup = 0
    while not P.Stop.event.is_set():
        try:
            with db.tx() as c:
                ops.sweep(c)
            if time.time() - last_backup > 86400:
                backup_db()
                last_backup = time.time()
        except Exception as e:
            print("sweeper:", e)
        P.Stop.event.wait(60)


def backup_db():
    import sqlite3
    d = os.path.join(os.path.dirname(config.db_path()), "db_backups")
    os.makedirs(d, exist_ok=True)
    src = sqlite3.connect(config.db_path())
    dst = sqlite3.connect(os.path.join(d, f"factory_{datetime.now():%Y%m%d}.db"))
    src.backup(dst)
    dst.close()
    src.close()


@asynccontextmanager
async def lifespan(app):
    db.init_db()
    ensure_admin()
    P.Stop.event.clear()
    if not os.environ.get("SF_NO_THREADS"):
        P.Orchestrator().start()
        threading.Thread(target=sweeper, daemon=True).start()
    yield
    P.Stop.event.set()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(HERE, "templates"))
templates.env.filters["ist"] = lambda ts: datetime.fromtimestamp(ts, IST).strftime("%d %b %H:%M") if ts else "-"
templates.env.filters["fmtcode"] = codes.fmt
templates.env.filters["loads"] = lambda s: json.loads(s) if s else {}
templates.env.filters["hrs"] = lambda m: f"{(m or 0) / 60:.1f} h"
templates.env.filters["inr"] = lambda v: "Rs " + f"{float(v or 0):,.2f}"
templates.env.globals["mask_key"] = admin_ops.mask_key


class Redirect(Exception):
    def __init__(self, url):
        self.url = url


@app.exception_handler(Redirect)
async def _redir(request, exc):
    return RedirectResponse(exc.url, 303)


def err_page(request, title, text, status):
    u, csrf = None, ""
    try:
        u, csrf = current(request)
    except Exception:
        pass
    return templates.TemplateResponse(request, "msg.html", dict(user=u, title=title, text=text, csrf=csrf or "", q=request.query_params,
                                                                nav=nav_for(u), path=request.url.path), status_code=status)


@app.exception_handler(PermissionError)
async def _perm(request, exc):
    return err_page(request, "Not allowed", str(exc), 403)


@app.exception_handler(HTTPException)
async def _http(request, exc):
    return err_page(request, f"Error {exc.status_code}", str(exc.detail), exc.status_code)


# ---------------------------------------------------------------- auth plumbing
FAILS = defaultdict(deque)


def current(request):
    with db.ro() as c:
        return ops.get_session(c, request.cookies.get("sf_session"))


def gate(request, *roles, consent=True):
    u, csrf = current(request)
    if not u:
        if "admin" in roles:
            raise Redirect("/admin/login")
        if "lead" in roles:
            raise Redirect("/lead/login")
        raise Redirect("/")
    if roles and u["role"] not in roles:
        raise PermissionError("Your role cannot open this page.")
    # if u["role"] == "participant" and consent and not u["consent_at"]:
    #     raise Redirect("/consent")
    request.state.csrf = csrf
    return u


async def post(request, *roles, consent=True):
    u = gate(request, *roles, consent=consent)
    form = await request.form()
    if form.get("csrf") != request.state.csrf:
        raise HTTPException(400, "Session expired. Reload the page and try again.")
    return u, form


def render(request, name, user, **ctx):
    open_count = 0
    if user:
        with db.ro() as _c:
            if "langs" not in ctx:
                ctx["langs"] = langs(_c)
            if "L" not in ctx:
                ctx["L"] = pick_lang(_c, user, request)
            if user["role"] in ("admin", "lead"):
                try:
                    if user["role"] == "admin":
                        open_count = _c.execute("SELECT COUNT(*) n FROM issues WHERE status='OPEN'").fetchone()["n"]
                    else:
                        open_count = _c.execute("SELECT COUNT(*) n FROM issues WHERE status='OPEN' AND language_id=?", (user.get("language_id"),)).fetchone()["n"]
                except Exception:
                    open_count = 0
    ctx.update(user=user, csrf=getattr(request.state, "csrf", ""), q=request.query_params,
               nav=nav_for(user, open_count), path=request.url.path)
    return templates.TemplateResponse(request, name, ctx)


def nav_for(u, open_issues=0):
    if not u:
        return []
    r = u["role"]
    iss_label = f"Issues ({open_issues})" if open_issues > 0 else "Issues"
    if r == "admin":
        return [("/admin", "Dashboard"), ("/admin/scripts", "Scripts"),
                ("/team/people", "People"), ("/team/pairs", "Pairs"), ("/team/work", "Assignments"),
                ("/team/issues", iss_label)]
    if r == "lead":
        return [("/team/people", "People"), ("/team/pairs", "Pairs"), ("/team/work", "Assignments"),
                ("/team/issues", iss_label)]
    if r == "reviewer":
        return [("/review", "Review")]
    return [("/me", "My work")]


def back(url, m="", e=""):
    qs = {k: v for k, v in (("m", m), ("e", e)) if v}
    if qs:
        url += ("&" if "?" in url else "?") + urlencode(qs)
    return RedirectResponse(url, 303)


def langs(c):
    return c.execute("SELECT * FROM languages ORDER BY id").fetchall()


def pick_lang(c, u, request, allow_all=True):
    """Admin chooses with ?lang= or cookie sf_lang, lead/reviewer pinned to their language."""
    if not u:
        return None
    if u["role"] != "admin":
        try:
            lid = u["language_id"]
            if lid:
                return c.execute("SELECT * FROM languages WHERE id=?", (lid,)).fetchone()
        except (KeyError, IndexError):
            pass
        return None
    lid = request.query_params.get("lang") or request.cookies.get("sf_lang")
    if lid == "all" and allow_all:
        return {"id": "all", "name": "All Languages", "code": "ALL", "cap_pair_min": 450.0, "cap_person_min": 900.0}
    if lid and str(lid).isdigit():
        r = c.execute("SELECT * FROM languages WHERE id=?", (int(lid),)).fetchone()
        if r:
            return r
    return c.execute("SELECT * FROM languages ORDER BY id LIMIT 1").fetchone()


@app.post("/set_language")
async def set_language_post(request: Request):
    f = await request.form()
    lang = f.get("lang", "all")
    next_url = f.get("next", "/team/people")
    if not next_url.startswith("/"):
        next_url = "/team/people"
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie("sf_lang", str(lang), max_age=30 * 86400, path="/")
    return resp


@app.get("/set_language")
async def set_language_get(request: Request):
    lang = request.query_params.get("lang", "all")
    next_url = request.query_params.get("next", "/team/people")
    if not next_url.startswith("/"):
        next_url = "/team/people"
    resp = RedirectResponse(next_url, status_code=303)
    resp.set_cookie("sf_lang", str(lang), max_age=30 * 86400, path="/")
    return resp


def lang_url(L, path):
    try:
        lid = str(L['id'])
        return f"{path}?lang={lid}" if lid != 'all' else path
    except Exception:
        return path


def ip(request):
    return request.client.host if request.client else "?"


# ---------------------------------------------------------------- login
@app.get("/")
@app.get("/login")
async def index_page(request: Request):
    tok = request.cookies.get("sf_session")
    if tok:
        with db.ro() as c:
            u = c.execute("SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=?", (tok,)).fetchone()
            if u:
                if u["role"] == "admin": return RedirectResponse("/admin", 303)
                if u["role"] == "lead": return RedirectResponse("/team", 303)
                return RedirectResponse("/me", 303)
    return render(request, "login.html", None, err=request.query_params.get("e", ""), role="agent", code=request.query_params.get("code", ""))

@app.post("/")
@app.post("/login")
async def index_login_post(request: Request):
    return await login_post(request, role="agent")

@app.get("/roles")
async def roles_page(request: Request):
    return render(request, "index.html", None)

@app.get("/{role}/login")
async def login_page(request: Request, role: str):
    if role not in ("admin", "lead", "agent"):
        raise HTTPException(404, "Not found")
    tok = request.cookies.get("sf_session")
    if tok:
        with db.ro() as c:
            u = c.execute("SELECT u.* FROM users u JOIN sessions s ON s.user_id=u.id WHERE s.token=?", (tok,)).fetchone()
            if u:
                if u["role"] == "admin": return RedirectResponse("/admin", 303)
                if u["role"] == "lead": return RedirectResponse("/team", 303)
                return RedirectResponse("/me", 303)
    return render(request, "login.html", None, err=request.query_params.get("e", ""), role=role, code=request.query_params.get("code", ""))

@app.post("/{role}/login")
async def login_post(request: Request, role: str):
    if role not in ("admin", "lead", "agent"):
        raise HTTPException(404, "Not found")
    f = await request.form()
    addr = ip(request)
    now = time.time()
    dq = FAILS[addr]
    while dq and now - dq[0] > 600:
        dq.popleft()
    if len(dq) >= 8:
        return render(request, "login.html", None, err="Too many wrong codes. Wait 10 minutes.", role=role)
    device = request.cookies.get("sf_device") or secrets.token_urlsafe(12)
    with db.tx() as c:
        tok, msg, pending = ops.login(c, f.get("code", ""), device, addr)
        if tok:
            u = c.execute("SELECT role FROM users WHERE id=(SELECT user_id FROM sessions WHERE token=?)", (tok,)).fetchone()
            if u:
                actual_role = u["role"]
                if actual_role == "participant":
                    actual_role = "agent"
                if actual_role != role and not (actual_role == "admin"): # Admin can login anywhere
                    tok = None
                    msg = f"This code belongs to a {u['role']}. Please use the correct portal."
    
    if not tok:
        if not pending:
            dq.append(now)
        r = render(request, "login.html", None, err=msg, pending=pending, role=role)
        r.set_cookie("sf_device", device, max_age=3 * 365 * 86400, httponly=True, samesite="lax")
        return r
    
    dest = "/" + ("admin" if u["role"] == "admin" else "team" if u["role"] == "lead" else "me")
    r = RedirectResponse(dest, 303)
    r.set_cookie("sf_session", tok, max_age=ops.SESSION_TTL, httponly=True, samesite="lax")
    r.set_cookie("sf_device", device, max_age=3 * 365 * 86400, httponly=True, samesite="lax")
    return r

@app.get("/logout")
@app.post("/logout")
async def logout(request: Request):
    tok = request.cookies.get("sf_session")
    if tok:
        with db.tx() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (tok,))
    r = RedirectResponse("/", 303)
    r.delete_cookie("sf_session")
    return r


@app.get("/healthz")
async def healthz():
    return {"ok": True}


# ---------------------------------------------------------------- participant
@app.get("/consent")
async def consent_page(request: Request):
    u = gate(request, "participant", consent=False)
    return render(request, "consent.html", u, text=config.CONSENT_TEXT, optin=config.OPTIN_TEXT)


@app.post("/consent")
async def consent_post(request: Request):
    u, f = await post(request, "participant", consent=False)
    if not f.get("agree"):
        return back("/consent", e="You must accept to continue.")
    with db.tx() as c:
        ops.accept_consent(c, u["id"], bool(f.get("optin")), config.CONSENT_VERSION)
    return back("/me")


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
BLOCKS = ["AM", "PM", "Eve"]


@app.get("/me")
async def me(request: Request):
    u = gate(request, "participant")
    with db.ro() as c:
        S = db.S(c)
        L = c.execute("SELECT * FROM languages WHERE id=?", (u["language_id"],)).fetchone()
        pair = c.execute("SELECT * FROM pairs WHERE status='ACTIVE' AND (a=? OR b=?)", (u["id"], u["id"])).fetchone()
        partner, lead = None, None
        total_assigned = 0
        completed_count = 0
        pending_count = 0
        current_script = None
        current_index = 0
        now = time.time()
        
        if pair:
            pid = pair["b"] if pair["a"] == u["id"] else pair["a"]
            partner = c.execute("SELECT * FROM users WHERE id=?", (pid,)).fetchone()
            
            all_assigns = c.execute(
                "SELECT a.*, s.code, s.subdomain, s.specialisation "
                "FROM assignments a JOIN scripts s ON s.id=a.script_id "
                "WHERE a.pair_id=? AND a.status NOT IN ('RELEASED','ABANDONED') ORDER BY a.id ASC",
                (pair["id"],)
            ).fetchall()
            
            total_assigned = len(all_assigns)
            completed_assigns = [a for a in all_assigns if a["status"] == "CONFIRMED"]
            completed_count = len(completed_assigns)
            
            uncompleted = [a for a in all_assigns if a["status"] != "CONFIRMED"]
            pending_count = len(uncompleted)
            if uncompleted:
                current_script = uncompleted[0]
                current_index = completed_count + 1
            else:
                current_index = total_assigned
                
        lead = c.execute("SELECT * FROM users WHERE role='lead' AND language_id=? AND active=1 ORDER BY id LIMIT 1", (u["language_id"],)).fetchone()
        
        partner_wa = ""
        if partner and partner["phone"]:
            p_digits = "".join(filter(str.isdigit, partner["phone"] or ""))
            if len(p_digits) == 10:
                p_digits = "91" + p_digits
            partner_wa = p_digits
            
        p_name = partner["name"] if partner else "partner"
        partner_name = "partner" if p_name.lower().startswith("agent") else p_name
        if current_script:
            w = "a" if current_script["ua"] == u["id"] else "b"
            other = "b" if w == "a" else "a"
            me_ready = (now - (current_script[f"ready_{w}"] or 0)) < 60
            partner_ready = (now - (current_script[f"ready_{other}"] or 0)) < 60
            
        return render(request, "me.html", u, L=L, pair=pair, partner=partner, partner_wa=partner_wa, lead=lead,
                      current_script=current_script, current_index=current_index,
                      total_assigned=total_assigned, completed_count=completed_count,
                      pending_count=pending_count, me_ready=me_ready, partner_ready=partner_ready,
                      partner_name=partner_name, avail=set(json.loads(u["availability"] or "[]")), days=DAYS, blocks=BLOCKS)


@app.get("/me/state")
async def me_state(request: Request):
    u = gate(request, "participant")
    with db.ro() as c:
        pair = c.execute("SELECT * FROM pairs WHERE status='ACTIVE' AND (a=? OR b=?)", (u["id"], u["id"])).fetchone()
        if not pair:
            return JSONResponse({"paired": False, "completed_count": 0, "pending_count": 0, "total_assigned": 0})
        pid = pair["b"] if pair["a"] == u["id"] else pair["a"]
        partner = c.execute("SELECT * FROM users WHERE id=?", (pid,)).fetchone()
        p_name = partner["name"] if partner else "partner"
        partner_name = "partner" if p_name.lower().startswith("agent") else p_name
        
        all_assigns = c.execute(
            "SELECT a.*, s.code, s.subdomain, s.specialisation "
            "FROM assignments a JOIN scripts s ON s.id=a.script_id "
            "WHERE a.pair_id=? AND a.status NOT IN ('RELEASED','ABANDONED') ORDER BY a.id ASC",
            (pair["id"],)
        ).fetchall()
        total_assigned = len(all_assigns)
        completed_count = len([a for a in all_assigns if a["status"] == "CONFIRMED"])
        uncompleted = [a for a in all_assigns if a["status"] != "CONFIRMED"]
        pending_count = len(uncompleted)
        current_script = uncompleted[0] if uncompleted else None
        
        now = time.time()
        me_ready = False
        partner_ready = False
        status = None
        current_id = None
        if current_script:
            current_id = current_script["id"]
            status = current_script["status"]
            w = "a" if current_script["ua"] == u["id"] else "b"
            other = "b" if w == "a" else "a"
            me_ready = (now - (current_script[f"ready_{w}"] or 0)) < 60
            partner_ready = (now - (current_script[f"ready_{other}"] or 0)) < 60
            
        return JSONResponse({
            "paired": True,
            "has_script": bool(current_script),
            "current_id": current_id,
            "status": status,
            "partner_name": partner_name,
            "partner_ready": partner_ready,
            "me_ready": me_ready,
            "completed_count": completed_count,
            "pending_count": pending_count,
            "total_assigned": total_assigned
        })


@app.post("/me/ready")
async def me_ready_toggle(request: Request):
    u, f = await post(request, "participant")
    with db.tx() as c:
        pair = c.execute("SELECT * FROM pairs WHERE status='ACTIVE' AND (a=? OR b=?)", (u["id"], u["id"])).fetchone()
        if not pair:
            return JSONResponse({"error": "No active pair"}, status_code=400)
        curr = c.execute("SELECT * FROM assignments WHERE pair_id=? AND status='ASSIGNED' ORDER BY id ASC LIMIT 1", (pair["id"],)).fetchone()
        if not curr:
            return JSONResponse({"error": "No assigned script"}, status_code=400)
        act = f.get("action", "toggle")
        w = "a" if curr["ua"] == u["id"] else "b"
        now = time.time()
        is_ready = (now - (curr[f"ready_{w}"] or 0)) < 60
        if act == "unready" or (act == "toggle" and is_ready):
            ops.unready(c, u["id"], curr["id"])
            new_ready = False
        else:
            ops.heartbeat(c, u["id"], curr["id"])
            new_ready = True
        return JSONResponse({"ok": True, "ready": new_ready, "aid": curr["id"]})


@app.post("/me/issue")
async def me_issue_report(request: Request):
    u, f = await post(request, "participant")
    with db.tx() as c:
        pair = c.execute("SELECT * FROM pairs WHERE status='ACTIVE' AND (a=? OR b=?)", (u["id"], u["id"])).fetchone()
        if not pair:
            return back("/me", e="No active pair found.")
        curr = c.execute("SELECT * FROM assignments WHERE pair_id=? AND status IN ('ASSIGNED','IN_SESSION') ORDER BY id ASC LIMIT 1", (pair["id"],)).fetchone()
        if not curr:
            return back("/me", e="No active script assignment.")
        msg = ops.report_issue(c, u["id"], curr["id"], f.get("kind", "other"), f.get("note", ""))
    return back("/me", m=msg)


@app.post("/me/availability")
async def me_avail(request: Request):
    u, f = await post(request, "participant")
    slots = [f"{d}-{b}" for d in DAYS for b in BLOCKS if f.get(f"{d}-{b}")]
    with db.tx() as c:
        ops.set_availability(c, u["id"], slots)
    return back("/me", m="Availability saved.")


def extract_scenario_and_dialogue(text):
    if not text:
        return "", ""
    text = text.strip()
    # 1. Look for explicit dialogue/script section markers
    pattern_split = r'(?:\r?\n)(?:#+\s*|\*{1,3})?(?:DIALOGUE\s+SCRIPT|SCRIPT)\b(?:\*{1,3})?[\s:]*(?:\r?\n)'
    m = re.search(pattern_split, text, re.IGNORECASE)
    if m:
        return text[:m.start()].strip(), text[m.end():].strip()

    # 2. Look for speaker tag after metadata
    m_speaker = re.search(r'(?:\r?\n){2,}(?:\*{1,3})?(DOCTOR|PATIENT|MOTHER|FATHER|SPEAKER\s*[12AB]|ಡಾಕ್ಟರ್|ರೋಗಿ)(?:\*{1,3})?[\s:]*(?:\r?\n)', text)
    if m_speaker and m_speaker.start() > 30:
        prefix = text[:m_speaker.start()]
        if any(kw in prefix.lower() for kw in ['specialty', 'case summary', 'metadata', 'patient', 'est. run time', 'word count']):
            return prefix.strip(), text[m_speaker.start():].strip()

    return "", text


def parse_scenario_meta(sc_text):
    if not sc_text:
        return {}
    meta = {}
    m = re.search(r'Specialty\s*:\s*([^\n|]+)', sc_text, re.IGNORECASE)
    if m:
        meta['specialty'] = m.group(1).strip()
    m = re.search(r'Doctor\s*:\s*([^\n|]+)', sc_text, re.IGNORECASE)
    if m:
        meta['doctor'] = m.group(1).strip()
    m = re.search(r'Patient(?:\s*\([^)]*\))?\s*:\s*([^\n|]+)', sc_text, re.IGNORECASE)
    if m:
        meta['patient'] = m.group(1).strip()
    m = re.search(r'(?:Est\.?\s*Run\s*Time|Estimated\s*Run\s*Time)\s*:\s*([^\n|]+)', sc_text, re.IGNORECASE)
    if m:
        meta['est_time'] = m.group(1).strip()
    m = re.search(r'(?:Word\s*Count|Spoken\s*Word\s*Count)\s*:\s*([^\n|]+)', sc_text, re.IGNORECASE)
    if m:
        meta['word_count'] = m.group(1).strip()

    m = re.search(r'(?:#+\s*)?CASE\s+SUMMARY\s*:?\s*\n+([\s\S]+?)(?:(?:\n---|\n#|\n\*\*|$))', sc_text + '\n', re.IGNORECASE)
    if not m:
        m = re.search(r'(?:#+\s*)?CASE\s+SUMMARY\s*:?\s*\n+([\s\S]+)', sc_text, re.IGNORECASE)
    if m:
        meta['case_summary'] = m.group(1).strip().strip('-').strip()
    return meta


def parse_dialogue_turns(text):
    dialogue_text = (text or "").strip()
    if "# DIALOGUE SCRIPT" in dialogue_text:
        parts = dialogue_text.split("# DIALOGUE SCRIPT", 1)
        dialogue_text = parts[1].strip()

    # Try speaker on own line (handles Markdown bold or plain caps/Kannada)
    pattern_newline = r'(?:\r?\n){2,}(?:\*{1,2})?([^\n\r:\*\[\]]{2,35}?)(?:\*{1,2})?:?\s*(?:\r?\n)'
    splits = re.split(pattern_newline, '\n\n' + dialogue_text)
    if len(splits) > 4:
        turns = []
        speakers_seen = []
        for i in range(1, len(splits), 2):
            speaker_name = splits[i].strip().replace('*', '').strip()
            turn_text = splits[i+1].strip() if i+1 < len(splits) else ""
            if not speaker_name or len(speaker_name) > 35:
                continue
            if speaker_name not in speakers_seen:
                speakers_seen.append(speaker_name)
            speaker_idx = speakers_seen.index(speaker_name)
            s_code = "A" if speaker_idx % 2 == 0 else "B"
            turns.append({
                "s": s_code,
                "role": speaker_name.title(),
                "t": turn_text
            })
        if len(turns) >= 2:
            return turns

    # Try inline speaker format (e.g. DOCTOR [cue] text, or PATIENT: text)
    pattern_inline = r'^\s*(?:\*{1,2})?([A-Z0-9_\s]{2,20}|ಡಾಕ್ಟರ್|ರೋಗಿ|ದಿವ್ಯ|ಸ್ವಾತಿ|ರಮೇಶ್|ಕಾವ್ಯ|ಮೀರಾ|ತಾಯಿ)(?:\*{1,2})?[:\s]+'
    blocks = [b.strip() for b in dialogue_text.split("\n\n") if b.strip()]
    inline_turns = []
    speakers_seen = []
    for b in blocks:
        m = re.match(pattern_inline, b)
        if m:
            spk = m.group(1).strip().replace('*', '').strip()
            speech = b[m.end():].strip()
            if spk not in speakers_seen:
                speakers_seen.append(spk)
            idx = speakers_seen.index(spk)
            inline_turns.append({'s': 'A' if idx % 2 == 0 else 'B', 'role': spk.title(), 't': speech})
        else:
            inline_turns.append({'s': 'A', 'role': 'Speaker', 't': b})
    if len(inline_turns) >= 2 and len(speakers_seen) >= 1:
        return inline_turns

    # Fallback to double newline blocks
    turns = []
    for i, b in enumerate(blocks):
        turns.append({
            "s": "A" if i % 2 == 0 else "B",
            "role": f"Speaker {'A' if i % 2 == 0 else 'B'}",
            "t": b
        })
    return turns


@app.get("/a/{aid}")
async def assignment_page(request: Request, aid: int):
    u = gate(request, "participant")
    with db.ro() as c:
        try:
            a, w = ops._mine(c, u["id"], aid)
        except PermissionError:
            raise HTTPException(404, "Not found")
        s = c.execute("SELECT * FROM scripts WHERE id=?", (a["script_id"],)).fetchone()
        L = c.execute("SELECT * FROM languages WHERE id=?", (s["language_id"],)).fetchone()
        v = c.execute("SELECT turns FROM script_versions WHERE script_id=? AND version=?", (s["id"], a["version"])).fetchone()
        if not v or not v["turns"]:
            v = c.execute("SELECT turns FROM script_versions WHERE script_id=? ORDER BY version DESC LIMIT 1", (s["id"],)).fetchone()
        S = db.S(c)
        try:
            attrs = json.loads(s["attrs"]) if s["attrs"] else {}
        except Exception:
            attrs = {}
        if not attrs.get("roles"):
            attrs["roles"] = ["Speaker 1", "Speaker 2"]
        if not attrs.get("names"):
            attrs["names"] = {"A": "Speaker 1", "B": "Speaker 2"}
        if not attrs.get("scenario"):
            attrs["scenario"] = s["subdomain"] or "Dialogue"
        if not attrs.get("vocab"):
            attrs["vocab"] = []
        if not attrs.get("entities"):
            attrs["entities"] = {}
        partner_id = a["ub"] if w == "a" else a["ua"]
        partner = c.execute("SELECT name FROM users WHERE id=?", (partner_id,)).fetchone()
        url = (S["client_url"] or "").replace("{script_id}", s["code"]).replace("{session}", f"{aid}")
        
        raw_turns = []
        raw_script_text = ""
        full_script_text = ""
        if v and v["turns"]:
            raw_script_text = v["turns"]
            try:
                raw_turns = json.loads(v["turns"])
            except Exception:
                raw_turns = [{"role": "Speaker", "text": v["turns"]}]
        
        if len(raw_turns) == 1 and isinstance(raw_turns[0], dict) and "text" in raw_turns[0]:
            full_script_text = raw_turns[0]["text"]
        elif isinstance(raw_turns, list) and len(raw_turns) > 1:
            full_script_text = "\n\n".join(f"{t.get('role', 'Speaker')}:\n{t.get('t', t.get('text', ''))}" if isinstance(t, dict) else str(t) for t in raw_turns)
        else:
            full_script_text = str(raw_script_text)

        clean_text = full_script_text.strip()
        if clean_text.startswith("```markdown"):
            clean_text = clean_text[11:].strip()
        elif clean_text.startswith("```"):
            clean_text = clean_text[3:].strip()
        elif clean_text.startswith("markdown\n"):
            clean_text = clean_text[9:].strip()
        if clean_text.endswith("```"):
            clean_text = clean_text[:-3].strip()
        full_script_text = clean_text

        scenario_text, dialogue_text = extract_scenario_and_dialogue(full_script_text)
        scenario_meta = parse_scenario_meta(scenario_text)

        if dialogue_text:
            turns = parse_dialogue_turns(dialogue_text)
        else:
            turns = parse_dialogue_turns(full_script_text)

        default_role_a = next((t["role"] for t in turns if t["s"] == "A"), attrs.get("names", {}).get("A", "Speaker 1"))
        default_role_b = next((t["role"] for t in turns if t["s"] == "B"), attrs.get("names", {}).get("B", "Speaker 2"))

        custom_roles = {}
        if "custom_roles" in a.keys() and a["custom_roles"]:
            try:
                custom_roles = json.loads(a["custom_roles"])
            except Exception:
                custom_roles = {}

        role_a = (custom_roles.get("A") if custom_roles else "") or default_role_a
        role_b = (custom_roles.get("B") if custom_roles else "") or default_role_b

        if custom_roles:
            for t in turns:
                t["role"] = role_a if t.get("s") == "A" else role_b

        me_role_label = role_a if w == "a" else role_b
        partner_role_label = role_b if w == "a" else role_a
        
        now = time.time()
        other = "b" if w == "a" else "a"
        me_ready = (now - (a[f"ready_{w}"] or 0)) < 60
        partner_ready = (now - (a[f"ready_{other}"] or 0)) < 60
        me_done = bool(a[f"done_{w}"])
        partner_done = bool(a[f"done_{other}"])
        p_name = partner["name"] if partner else "partner"
        partner_name = "partner" if p_name.lower().startswith("agent") else p_name

        return render(request, "assignment.html", u, a=a, s=s, L=L, turns=turns,
                      raw_script_text=raw_script_text, full_script_text=full_script_text,
                      scenario_text=scenario_text, scenario_meta=scenario_meta,
                      role_a=role_a, role_b=role_b,
                      default_role_a=default_role_a, default_role_b=default_role_b,
                      custom_roles=custom_roles,
                      me_role_label=me_role_label, partner_role_label=partner_role_label,
                      attrs=attrs, me_role=("A" if w == "a" else "B"),
                      partner=partner["name"] if partner else "Partner",
                      partner_name=partner_name, client_url=url, S=S,
                      me_ready=me_ready, partner_ready=partner_ready,
                      me_done=me_done, partner_done=partner_done)


@app.get("/a/{aid}/state")
async def assignment_state(request: Request, aid: int):
    u = gate(request, "participant")
    with db.ro() as c:
        a, w = ops._mine(c, u["id"], aid)
        now = time.time()
        other = "b" if w == "a" else "a"
        partner_id = a["ub"] if w == "a" else a["ua"]
        partner = c.execute("SELECT name FROM users WHERE id=?", (partner_id,)).fetchone()
        p_name = partner["name"] if partner else "partner"
        partner_name = "partner" if p_name.lower().startswith("agent") else p_name
        return JSONResponse(dict(
            status=a["status"],
            partner_name=partner_name,
            partner_ready=(now - (a[f"ready_{other}"] or 0)) < 60,
            me_ready=(now - (a[f"ready_{w}"] or 0)) < 60,
            me_done=bool(a[f"done_{w}"]),
            partner_done=bool(a[f"done_{other}"]),
            started_at=a["started_at"] or 0,
            me_role=w.upper()
        ))



@app.post("/a/{aid}/save_time")
async def assignment_save_time(request: Request, aid: int):
    u, f = await post(request, "participant")
    raw_time = (f.get("minutes") or f.get("recorded_time") or "").strip()
    if not raw_time:
        return back(f"/a/{aid}", e="Please enter the recorded time.")
    minutes = 0.0
    try:
        if ":" in raw_time:
            parts = raw_time.split(":")
            minutes = float(parts[0]) + float(parts[1]) / 60.0
        else:
            minutes = float(raw_time)
    except Exception:
        return back(f"/a/{aid}", e="Please enter a valid time (e.g. 15 or 15.5)")
    
    if minutes <= 0:
        return back(f"/a/{aid}", e="Recorded time must be greater than 0 minutes.")
    
    minutes = round(minutes, 1)
    with db.tx() as c:
        a, w = ops._mine(c, u["id"], aid)
        if a["status"] in ("RELEASED", "ABANDONED"):
            return back("/me", e="This assignment has already been closed.")
        now = time.time()
        if a["status"] == "CONFIRMED":
            old_min = a["conf_min"] or 0.0
            diff = minutes - old_min
            c.execute("UPDATE assignments SET conf_min=?, updated_at=? WHERE id=?", (minutes, now, aid))
            if diff != 0:
                for uid in (a["ua"], a["ub"]):
                    c.execute("INSERT INTO ledger(ts,assignment_id,user_id,pair_id,minutes,kind) VALUES(?,?,?,?,?,?)",
                              (now, aid, uid, a["pair_id"], round(diff, 1), "confirmed"))
            db.audit(c, f"participant:{u['id']}", "update_time", f"assignment={aid} min={minutes:.1f}")
        else:
            ops.confirm(c, f"participant:{u['id']}", aid, minutes, verified=True, note=f"Recorded time entered by {u['name']}")
            
    return back("/me", m=f"✅ Recording saved successfully! Total time: {minutes:.1f} minutes.")


@app.post("/a/{aid}/{action}")
async def assignment_action(request: Request, aid: int, action: str):
    u, f = await post(request, "participant")
    msg, err = "", ""
    with db.tx() as c:
        try:
            if action == "ready":
                ops.heartbeat(c, u["id"], aid)
                return JSONResponse({"ok": True})
            elif action == "unready":
                ops.unready(c, u["id"], aid)
                return JSONResponse({"ok": True})
            elif action == "swap_role":
                a, w = ops._mine(c, u["id"], aid)
                c.execute("""
                    UPDATE assignments 
                    SET ua=ub, ub=ua, 
                        ready_a=ready_b, ready_b=ready_a, 
                        done_a=done_b, done_b=done_a,
                        updated_at=?
                    WHERE id=?
                """, (time.time(), aid))
                return JSONResponse({"ok": True, "swapped": True})
            elif action == "select_role":
                target = (f.get("role") or "").upper()
                a, w = ops._mine(c, u["id"], aid)
                if target in ("A", "B") and target != w.upper():
                    c.execute("""
                        UPDATE assignments 
                        SET ua=ub, ub=ua, 
                            ready_a=ready_b, ready_b=ready_a, 
                            done_a=done_b, done_b=done_a,
                            updated_at=?
                        WHERE id=?
                    """, (time.time(), aid))
                return JSONResponse({"ok": True, "role": target})
            elif action == "custom_roles":
                role_a_name = (f.get("role_a") or "").strip()
                role_b_name = (f.get("role_b") or "").strip()
                reset = f.get("reset") == "1"
                if reset:
                    c.execute("UPDATE assignments SET custom_roles='', updated_at=? WHERE id=?", (time.time(), aid))
                elif role_a_name or role_b_name:
                    custom_json = json.dumps({"A": role_a_name, "B": role_b_name})
                    c.execute("UPDATE assignments SET custom_roles=?, updated_at=? WHERE id=?", (custom_json, time.time(), aid))
                return JSONResponse({"ok": True})
            elif action == "start":
                ops.start(c, u["id"], aid)
                msg = "Session started. Record on the client page now."
            elif action == "done":
                ops.done(c, u["id"], aid, f.get("client_session", ""))
                msg = "Marked done. Waiting for your partner if they have not tapped Done."
            elif action == "problem":
                ops.problem(c, u["id"], aid, f.get("note", ""))
                msg = "Problem reported to your team lead."
            elif action == "issue":
                msg = ops.report_issue(c, u["id"], aid, f.get("kind", "other"), f.get("note", ""))
            elif action == "save_time":
                raw_time = (f.get("minutes") or f.get("recorded_time") or "").strip()
                if not raw_time:
                    return back(f"/a/{aid}", e="Please enter the recorded time.")
                minutes = 0.0
                try:
                    if ":" in raw_time:
                        parts = raw_time.split(":")
                        minutes = float(parts[0]) + float(parts[1]) / 60.0
                    else:
                        minutes = float(raw_time)
                except Exception:
                    return back(f"/a/{aid}", e="Please enter a valid time (e.g. 15 or 15.5)")
                if minutes <= 0:
                    return back(f"/a/{aid}", e="Recorded time must be greater than 0 minutes.")
                minutes = round(minutes, 1)
                a, w = ops._mine(c, u["id"], aid)
                if a["status"] in ("RELEASED", "ABANDONED"):
                    return back("/me", e="This assignment has already been closed.")
                now = time.time()
                if a["status"] == "CONFIRMED":
                    old_min = a["conf_min"] or 0.0
                    diff = minutes - old_min
                    c.execute("UPDATE assignments SET conf_min=?, updated_at=? WHERE id=?", (minutes, now, aid))
                    if diff != 0:
                        for uid in (a["ua"], a["ub"]):
                            c.execute("INSERT INTO ledger(ts,assignment_id,user_id,pair_id,minutes,kind) VALUES(?,?,?,?,?,?)",
                                      (now, aid, uid, a["pair_id"], round(diff, 1), "confirmed"))
                    db.audit(c, f"participant:{u['id']}", "update_time", f"assignment={aid} min={minutes:.1f}")
                else:
                    ops.confirm(c, f"participant:{u['id']}", aid, minutes, verified=True, note=f"Recorded time entered by {u['name']}")
                return back("/me", m=f"✅ Recording saved successfully! Total time: {minutes:.1f} minutes.")
            else:
                raise HTTPException(404, "unknown action")
        except ops.OpsError as e:
            err = str(e)
    return back(f"/a/{aid}", m=msg, e=err)


# ---------------------------------------------------------------- team (lead + admin)
@app.get("/team")
async def team_home(request: Request):
    u = gate(request, "lead", "admin")
    with db.ro() as c:
        L = pick_lang(c, u, request)
        if not L:
            return render(request, "msg.html", u, title="No language yet", text="Ask the admin to create a language first.")
        ex = ops.lead_exceptions(c, L["id"])
        issues = c.execute("SELECT i.*, s.code FROM issues i LEFT JOIN assignments a ON a.id=i.assignment_id LEFT JOIN scripts s ON s.id=a.script_id"
                           " WHERE i.status='OPEN' AND i.language_id=? AND (i.route='lead' OR ?='admin') ORDER BY i.id DESC",
                           (L["id"], u["role"])).fetchall()
        devs = c.execute("SELECT d.*, u.name FROM device_requests d JOIN users u ON u.id=d.user_id WHERE d.status='PENDING' AND u.language_id=?", (L["id"],)).fetchall()
        no_pair = ops.unpaired(c, L["id"])
        phones = {r["id"]: r["phone"] for r in c.execute("SELECT id, phone FROM users WHERE language_id=?", (L["id"],))}
        names = {r["id"]: r["name"] for r in c.execute("SELECT id, name FROM users WHERE language_id=?", (L["id"],))}
        nudge = db.setting(c, "lead_whatsapp_nudge")
        return render(request, "team_home.html", u, L=L, langs=langs(c), ex=ex, issues=issues, devs=devs, no_pair=no_pair,
                      phones=phones, names=names, nudge=nudge, can=ops.can_manage(u, L["id"]))


@app.get("/team/issues")
@app.get("/admin/issues")
async def team_issues(request: Request):
    u = gate(request, "lead", "admin")
    tab = request.query_params.get("tab", "open")
    kind_filter = request.query_params.get("kind", "all")
    with db.ro() as c:
        L = pick_lang(c, u, request)
        if not L:
            raise Redirect("/team")
            
        lang_where = ""
        lang_args = ()
        if L["id"] != "all":
            lang_where = " AND i.language_id = ?"
            lang_args = (L["id"],)
            
        total_issues = c.execute(f"SELECT COUNT(*) n FROM issues i WHERE 1=1{lang_where}", lang_args).fetchone()["n"]
        open_issues = c.execute(f"SELECT COUNT(*) n FROM issues i WHERE i.status='OPEN'{lang_where}", lang_args).fetchone()["n"]
        resolved_issues = c.execute(f"SELECT COUNT(*) n FROM issues i WHERE i.status='RESOLVED'{lang_where}", lang_args).fetchone()["n"]
        no_show_issues = c.execute(f"SELECT COUNT(*) n FROM issues i WHERE i.kind='partner_no_show' AND i.status='OPEN'{lang_where}", lang_args).fetchone()["n"]
        
        status_filter = " AND i.status='RESOLVED'" if tab == "resolved" else " AND i.status='OPEN'"
        kind_clause = ""
        kind_args = ()
        if kind_filter != "all":
            kind_clause = " AND i.kind = ?"
            kind_args = (kind_filter,)
            
        sql = f"""
            SELECT 
                i.id, i.assignment_id, i.user_id, i.language_id, i.kind, i.note, i.route, i.status, i.created,
                u.name as reporter_name, u.phone as reporter_phone, u.email as reporter_email,
                a.pair_id, a.status as assignment_status, a.est_min,
                s.id as script_id, s.code as script_code, s.domain, s.subdomain, s.specialisation,
                l.name as lang_name,
                ua.id as ua_id, ua.name as ua_name, ua.phone as ua_phone,
                ub.id as ub_id, ub.name as ub_name, ub.phone as ub_phone
            FROM issues i
            LEFT JOIN users u ON u.id = i.user_id
            LEFT JOIN assignments a ON a.id = i.assignment_id
            LEFT JOIN scripts s ON s.id = a.script_id
            LEFT JOIN languages l ON l.id = i.language_id
            LEFT JOIN users ua ON ua.id = a.ua
            LEFT JOIN users ub ON ub.id = a.ub
            WHERE 1=1 {status_filter} {lang_where} {kind_clause}
            ORDER BY i.id DESC LIMIT 500
        """
        rows = c.execute(sql, lang_args + kind_args).fetchall()
        
        kind_labels = {
            "partner_no_show": "Partner No-Show",
            "audio_problem": "Audio Quality",
            "script_error": "Script Error",
            "tech_failure": "Recorder Failure",
            "other": "Other Issue",
            "problem": "Session Problem"
        }
        
        issue_list = []
        for r in rows:
            if r["user_id"] == r["ua_id"]:
                partner_id = r["ub_id"]
                partner_name = r["ub_name"]
                partner_phone = r["ub_phone"]
            else:
                partner_id = r["ua_id"]
                partner_name = r["ua_name"]
                partner_phone = r["ua_phone"]
                
            clean_p_phone = re.sub(r'\D', '', partner_phone or '')
            if len(clean_p_phone) == 10:
                partner_wa = f"91{clean_p_phone}"
            elif len(clean_p_phone) > 10:
                partner_wa = clean_p_phone
            else:
                partner_wa = None
                
            issue_list.append(dict(
                id=r["id"],
                assignment_id=r["assignment_id"],
                pair_id=r["pair_id"],
                reporter_id=r["user_id"],
                reporter_name=r["reporter_name"] or "Agent",
                reporter_phone=r["reporter_phone"] or "",
                reporter_email=r["reporter_email"] or "",
                partner_id=partner_id,
                partner_name=partner_name or "Partner",
                partner_phone=partner_phone or "",
                partner_wa=partner_wa,
                script_id=r["script_id"],
                script_code=r["script_code"] or "—",
                domain=r["domain"] or "",
                subdomain=r["subdomain"] or "",
                specialisation=r["specialisation"] or "",
                lang_name=r["lang_name"] or "",
                kind=r["kind"],
                kind_label=kind_labels.get(r["kind"], r["kind"].replace("_", " ").title()),
                note=r["note"] or "",
                route=r["route"],
                status=r["status"],
                created=r["created"]
            ))
            
        return render(request, "team_issues.html", u, L=L, langs=langs(c), tab=tab, kind_filter=kind_filter,
                      issues=issue_list, total_issues=total_issues, open_issues=open_issues,
                      resolved_issues=resolved_issues, no_show_issues=no_show_issues,
                      can=(u["role"] == "admin" or ops.can_manage(u, L["id"])))


@app.post("/team/issue/{iid}/resolve")
async def issue_resolve(request: Request, iid: int):
    u, f = await post(request, "lead", "admin")
    with db.tx() as c:
        ops.resolve_issue(c, u, iid)
    return back(f.get("next", "/team/issues"), m="Issue marked as resolved.")


@app.post("/team/issue/{iid}/reopen")
async def issue_reopen(request: Request, iid: int):
    u, f = await post(request, "lead", "admin")
    with db.tx() as c:
        c.execute("UPDATE issues SET status='OPEN' WHERE id=?", (iid,))
        db.audit(c, u, "reopen_issue", str(iid))
    return back(f.get("next", "/team/issues"), m="Issue reopened.")


@app.post("/team/device/{rid}/{act}")
async def device_decide(request: Request, rid: int, act: str):
    u, f = await post(request, "lead", "admin")
    with db.tx() as c:
        ops.decide_device(c, u, rid, act == "approve")
    return back(f.get("next", "/team"), m="Device " + ("approved." if act == "approve" else "denied."))


@app.get("/team/people")
async def team_people(request: Request):
    u = gate(request, "lead", "admin")
    reveal = request.query_params.get("reveal") == "1"
    with db.tx() as c:
        L = pick_lang(c, u, request)
        if not L:
            raise Redirect("/admin/language/new" if u["role"] == "admin" else "/team")
        if reveal and ops.can_manage(u, L["id"]):
            db.audit(c, u, "reveal_pii", f"people lang={L['id']}")
        else:
            reveal = False
    with db.ro() as c:
        p_filter = "" if L["id"] == "all" else " WHERE language_id = ?"
        p_args = () if L["id"] == "all" else (L["id"],)
        total_people = c.execute(f"SELECT COUNT(*) n FROM users{p_filter}", p_args).fetchone()["n"]
        active_agents = c.execute(f"SELECT COUNT(*) n FROM users{p_filter} {'AND' if p_filter else 'WHERE'} role='participant' AND active=1", p_args).fetchone()["n"]
        unpaired_count = len(ops.unpaired(c, L["id"]))
        paired_count = max(0, active_agents - unpaired_count)

        if L["id"] == "all":
            rows = c.execute("SELECT u.*, l.name lang_name FROM users u LEFT JOIN languages l ON l.id=u.language_id WHERE u.role IN ('participant','lead','reviewer') ORDER BY u.language_id, u.role, u.id").fetchall()
            pairs = {}
            for p in c.execute("SELECT * FROM pairs WHERE status='ACTIVE'"):
                pairs[p["a"]] = pairs[p["b"]] = p["id"]
        else:
            rows = c.execute("SELECT u.*, l.name lang_name FROM users u LEFT JOIN languages l ON l.id=u.language_id WHERE u.language_id=? AND u.role IN ('participant','lead','reviewer') ORDER BY u.role, u.id", (L["id"],)).fetchall()
            pairs = {}
            for p in c.execute("SELECT * FROM pairs WHERE status='ACTIVE' AND language_id=?", (L["id"],)):
                pairs[p["a"]] = pairs[p["b"]] = p["id"]
        used = {r["id"]: ops.person_used(c, r["id"]) for r in rows}
        return render(request, "team_people.html", u, L=L, langs=langs(c), rows=rows, pairs=pairs, used=used, reveal=reveal,
                      total_people=total_people, active_agents=active_agents, paired_count=paired_count, unpaired_count=unpaired_count,
                      mask=ops.mask, can=(u["role"] == "admin" or ops.can_manage(u, L["id"])), preview=None)


@app.get("/team/people/share_all")
async def people_share_all(request: Request):
    u = gate(request, "lead", "admin")
    with db.ro() as c:
        L = pick_lang(c, u, request)
        if not L:
            raise Redirect("/team")
        if L["id"] == "all":
            rows = c.execute("SELECT u.*, l.name lang_name FROM users u LEFT JOIN languages l ON l.id=u.language_id WHERE u.role='participant' AND u.active=1 ORDER BY u.language_id, u.id").fetchall()
        else:
            rows = c.execute("SELECT u.*, l.name lang_name FROM users u LEFT JOIN languages l ON l.id=u.language_id WHERE u.language_id=? AND u.role='participant' AND u.active=1 ORDER BY u.id", (L["id"],)).fetchall()
        
        host = request.headers.get("host", "localhost:8000")
        scheme = request.url.scheme
        base_url = f"{scheme}://{host}"
        
        agent_list = []
        emails = []
        messages_text = []
        for r in rows:
            code_fmt = codes.fmt(r["code"])
            login_url = f"{base_url}/agent/login?code={code_fmt}"
            msg = f"Hello {r['name']}, your ScriptFactory audio recording access code is {code_fmt}. Sign in here: {login_url}"
            agent_list.append(dict(
                id=r["id"],
                name=r["name"],
                phone=r["phone"],
                email=r["email"],
                lang_name=r["lang_name"],
                code=code_fmt,
                login_url=login_url,
                msg=msg
            ))
            if r["email"]:
                emails.append(r["email"])
            messages_text.append(f"{r['name']} ({r['phone'] or r['email']}): Code {code_fmt} | Link: {login_url}")
            
        bcc = ",".join(emails)
        subject = "Your ScriptFactory Audio Project Access Code"
        body = (
            "Hello,\n\n"
            "You have been registered as a recording agent for ScriptFactory.\n"
            f"Please visit the Speaker Portal: {base_url}/agent/login\n\n"
            "Enter your 8-character access code to view and record your assigned scripts.\n\n"
            "Thank you,\nScriptFactory Team"
        )
        return render(request, "team_people_share.html", u, L=L, langs=langs(c), agents=agent_list,
                      bcc=bcc, subject=subject, body=body, all_text="\n".join(messages_text))


@app.get("/team/people/export_codes")
async def people_export_codes(request: Request):
    u = gate(request, "lead", "admin")
    with db.ro() as c:
        L = pick_lang(c, u, request)
        if L["id"] == "all":
            rows = c.execute("SELECT u.*, l.name lang_name FROM users u LEFT JOIN languages l ON l.id=u.language_id WHERE u.role='participant' AND u.active=1 ORDER BY u.language_id, u.id").fetchall()
        else:
            rows = c.execute("SELECT u.*, l.name lang_name FROM users u LEFT JOIN languages l ON l.id=u.language_id WHERE u.language_id=? AND u.role='participant' AND u.active=1 ORDER BY u.id", (L["id"],)).fetchall()
        
        host = request.headers.get("host", "localhost:8000")
        scheme = request.url.scheme
        base_url = f"{scheme}://{host}"
        
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["Name", "Language", "Phone", "Email", "Access Code", "Login Link"])
        for r in rows:
            code_fmt = codes.fmt(r["code"])
            writer.writerow([r["name"], r["lang_name"], r["phone"], r["email"], code_fmt, f"{base_url}/agent/login?code={code_fmt}"])
        
        data = out.getvalue().encode("utf-8-sig")
        return Response(data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": 'attachment; filename="agents_access_codes.csv"'})


@app.post("/team/people/add")
async def people_add(request: Request):
    u, f = await post(request, "lead", "admin")
    lid_val = f.get("lang")
    if not lid_val or lid_val == "all":
        with db.ro() as _c:
            first_l = _c.execute("SELECT id FROM languages ORDER BY id LIMIT 1").fetchone()
            lid = first_l["id"] if first_l else 1
    else:
        lid = int(lid_val)
    role = f.get("role", "participant")
    with db.tx() as c:
        try:
            uid, code = ops.create_user(c, u, f.get("name"), role, lid, f.get("phone", ""), f.get("email", ""), f.get("gender", ""),
                                        f.get("perm", "manage"), float(f.get("hours_used") or 0) * 60)
        except (ops.OpsError, ValueError) as e:
            return back(f"/team/people", e=str(e))
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    return render(request, "code.html", u, row=row, code=code, next=f"/team/people")


@app.post("/team/people/preview")
async def people_preview(request: Request):
    u, f = await post(request, "lead", "admin")
    lid_val = f.get("lang")
    if not lid_val or lid_val == "all":
        with db.ro() as _c:
            first_l = _c.execute("SELECT id FROM languages ORDER BY id LIMIT 1").fetchone()
            lid = first_l["id"] if first_l else 1
    else:
        lid = int(lid_val)
    ops.need_manage(u, lid)
    up = f.get("file")
    raw = await up.read() if hasattr(up, "read") else b""
    text = raw.decode("utf-8-sig") if raw else f.get("csv_paste", "")
    with db.ro() as c:
        rows = ops.bulk_preview(c, lid, text)
        L = c.execute("SELECT * FROM languages WHERE id=?", (lid,)).fetchone()
        return render(request, "team_people.html", u, L=L, langs=langs(c), rows=[], pairs={}, used={}, reveal=False, mask=ops.mask,
                      can=True, preview=rows, preview_json=json.dumps(rows))


@app.post("/team/people/commit")
async def people_commit(request: Request):
    u, f = await post(request, "lead", "admin")
    lid_val = f.get("lang")
    if not lid_val or lid_val == "all":
        with db.ro() as _c:
            first_l = _c.execute("SELECT id FROM languages ORDER BY id LIMIT 1").fetchone()
            lid = first_l["id"] if first_l else 1
    else:
        lid = int(lid_val)
    rows = json.loads(f.get("rows", "[]"))
    with db.tx() as c:
        n = ops.bulk_commit(c, u, lid, rows)
    return back(f"/team/people", m=f"{n} participants added. Open each person to share their code.")


@app.post("/team/people/{uid}/edit")
async def people_edit(request: Request, uid: int):
    u, f = await post(request, "lead", "admin")
    name = f.get("name", "").strip()
    phone = f.get("phone", "").strip()
    email = f.get("email", "").strip()
    role = f.get("role")
    lang_val = f.get("lang")
    lang_id = int(lang_val) if lang_val and lang_val.isdigit() else None
    active = f.get("active") == "1" if "active" in f else None
    
    try:
        with db.tx() as c:
            ops.update_user(c, u, uid, name, phone, email, role=role, language_id=lang_id, active=active)
        return back("/team/people", m=f"Updated details for {name}.")
    except Exception as e:
        return back("/team/people", e=str(e))


@app.post("/team/people/{uid}/{act}")
async def people_act(request: Request, uid: int, act: str):
    if act == "edit":
        return await people_edit(request, uid)
    u, f = await post(request, "lead", "admin")
    with db.tx() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise HTTPException(404, "no such user")
        lid = row["language_id"]
        if u["role"] == "lead" and lid != u["language_id"]:
            raise PermissionError("other language")
        if act == "delete":
            name = row["name"]
            ops.delete_user(c, u, uid)
            return back(f"/team/people", m=f"Deleted {name} successfully.")
        if act == "showcode":
            code = ops.view_code(c, u, uid)
            return render(request, "code.html", u, row=row, code=code, next=f"/team/people")
        if act == "reissue":
            code = ops.reissue_code(c, u, uid)
            return render(request, "code.html", u, row=row, code=code, next=f"/team/people")
        if act in ("revoke", "activate"):
            ops.set_active(c, u, uid, act == "activate")
    return back(f"/team/people", m="Done.")


@app.post("/team/people/delete_all")
async def people_delete_all(request: Request):
    u, f = await post(request, "lead", "admin")
    lid = f.get("lang", "all")
    with db.tx() as c:
        count = ops.delete_all_agents(c, u, lid)
    return back(f"/team/people?lang={lid}", m=f"Deleted all {count} participant agents.")


@app.post("/team/people/reissue_all")
async def people_reissue_all(request: Request):
    u, f = await post(request, "lead", "admin")
    lid = f.get("lang", "all")
    with db.tx() as c:
        if lid == "all":
            rows = c.execute("SELECT * FROM users WHERE role='participant' AND active=1").fetchall()
        else:
            rows = c.execute("SELECT * FROM users WHERE language_id=? AND role='participant' AND active=1", (int(lid),)).fetchall()
        
        count = 0
        for r in rows:
            ops.reissue_code(c, u, r["id"])
            count += 1
            
        db.audit(c, u, "reissue_all_codes", f"lang={lid} count={count}")
    return back(f"/team/people?lang={lid}", m=f"Generated new access codes for {count} agents. Use 'Send Codes to All' or 'Export CSV' to share them.")


@app.get("/team/pairs")
async def team_pairs(request: Request):
    u = gate(request, "lead", "admin")
    with db.ro() as c:
        L = pick_lang(c, u, request)
        if not L:
            raise Redirect("/team")
        if L["id"] == "all":
            all_pairs = c.execute("SELECT p.*, ua.name an, ub.name bn, l.name lang_name FROM pairs p JOIN users ua ON ua.id=p.a JOIN users ub ON ub.id=p.b JOIN languages l ON l.id=p.language_id ORDER BY p.language_id, p.id DESC").fetchall()
            loose = ops.unpaired(c, "all")
            n = request.query_params.get("propose")
            proposals = ops.propose_pairs(c, "all", int(n)) if (n and n.isdigit()) else None
        else:
            all_pairs = c.execute("SELECT p.*, ua.name an, ub.name bn, l.name lang_name FROM pairs p JOIN users ua ON ua.id=p.a JOIN users ub ON ub.id=p.b JOIN languages l ON l.id=p.language_id WHERE p.language_id=? ORDER BY p.id DESC", (L["id"],)).fetchall()
            loose = ops.unpaired(c, L["id"])
            n = request.query_params.get("propose")
            proposals = ops.propose_pairs(c, L["id"], int(n)) if (n and n.isdigit()) else None
        
        pair_filter = "" if L["id"] == "all" else " WHERE language_id = ?"
        pair_args = () if L["id"] == "all" else (L["id"],)
        total_pairs = c.execute(f"SELECT COUNT(*) n FROM pairs{pair_filter}", pair_args).fetchone()["n"]
        active_pairs = c.execute(f"SELECT COUNT(*) n FROM pairs{pair_filter} {'AND' if pair_filter else 'WHERE'} status='ACTIVE'", pair_args).fetchone()["n"]
        dropped_pairs = c.execute(f"SELECT COUNT(*) n FROM pairs{pair_filter} {'AND' if pair_filter else 'WHERE'} status='DROPPED'", pair_args).fetchone()["n"]
        unpaired_count = len(loose)
        
        tab = request.query_params.get("tab", "active")
        active_list = [p for p in all_pairs if p["status"] == "ACTIVE"]
        dropped_list = [p for p in all_pairs if p["status"] == "DROPPED"]
        pairs = dropped_list if tab == "history" else active_list
        
        used = {p["id"]: ops.pair_used(c, p["id"]) for p in all_pairs}
        return render(request, "team_pairs.html", u, L=L, langs=langs(c), pairs=pairs, used=used, proposals=proposals,
                      total_pairs=total_pairs, active_pairs=active_pairs, dropped_pairs=dropped_pairs, unpaired_count=unpaired_count,
                      tab=tab, loose=loose, can=(u["role"] == "admin" or ops.can_manage(u, L["id"])))


@app.post("/team/pairs/create")
async def pairs_create(request: Request):
    u, f = await post(request, "lead", "admin")
    lid = f.get("lang", "all")
    made = 0
    with db.tx() as c:
        for v in f.getlist("pair"):
            a, b = v.split("-")
            try:
                ops.create_pair(c, u, int(a), int(b))
                made += 1
            except ops.OpsError:
                pass
        if f.get("manual_a") and f.get("manual_b"):
            try:
                ops.create_pair(c, u, int(f["manual_a"]), int(f["manual_b"]))
                made += 1
            except ops.OpsError as e:
                return back(f"/team/pairs", e=str(e))
    return back(f"/team/pairs", m=f"{made} pairs created.")


@app.post("/team/pairs/{pid}/drop")
async def pairs_drop(request: Request, pid: int):
    u, f = await post(request, "lead", "admin")
    with db.tx() as c:
        lid = c.execute("SELECT language_id FROM pairs WHERE id=?", (pid,)).fetchone()["language_id"]
        ops.drop_pair(c, u, pid, f.get("reason", ""))
    return back(f"/team/pairs", m="Pair dropped. Unstarted scripts returned to the pool.")


@app.post("/team/pairs/drop_all")
async def pairs_drop_all(request: Request):
    u, f = await post(request, "lead", "admin")
    lid = f.get("lang", "all")
    with db.tx() as c:
        n = ops.drop_all_pairs(c, u, lid if lid == "all" else int(lid))
    return back(f"/team/pairs", m=f"Dropped {n} pairs. Unstarted scripts returned to the pool.")


@app.get("/team/work")
async def team_work(request: Request):
    u = gate(request, "lead", "admin")
    with db.ro() as c:
        L = pick_lang(c, u, request)
        if not L:
            raise Redirect("/team")
        S = db.S(c)
        if L["id"] == "all":
            pairs = c.execute("SELECT p.*, ua.name an, ub.name bn, l.name lang_name FROM pairs p JOIN users ua ON ua.id=p.a JOIN users ub ON ub.id=p.b JOIN languages l ON l.id=p.language_id WHERE p.status='ACTIVE' ORDER BY p.language_id, p.id").fetchall()
            stock = c.execute("SELECT COUNT(*) n FROM scripts s WHERE s.status IN ('APPROVED','READY') AND (s.is_test=0 OR ?=1) AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED'))", (1 if S["allow_test_scripts"] == "1" else 0,)).fetchone()["n"]
            paused = False
            total_in_progress = c.execute("SELECT COUNT(*) n FROM assignments WHERE status IN ('ASSIGNED','IN_SESSION')").fetchone()["n"]
            total_completed = c.execute("SELECT COUNT(*) n FROM assignments WHERE status='CONFIRMED'").fetchone()["n"]
        else:
            pairs = c.execute("SELECT p.*, ua.name an, ub.name bn, l.name lang_name FROM pairs p JOIN users ua ON ua.id=p.a JOIN users ub ON ub.id=p.b JOIN languages l ON l.id=p.language_id WHERE p.language_id=? AND p.status='ACTIVE'", (L["id"],)).fetchall()
            stock = c.execute("SELECT COUNT(*) n FROM scripts s WHERE s.language_id=? AND s.status IN ('APPROVED','READY') AND (s.is_test=0 OR ?=1) AND NOT EXISTS (SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED'))", (L["id"], 1 if S["allow_test_scripts"] == "1" else 0)).fetchone()["n"]
            paused = db.setting(c, f"pause_assign_{L['id']}") == "1"
            total_in_progress = c.execute("SELECT COUNT(*) n FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE s.language_id=? AND a.status IN ('ASSIGNED','IN_SESSION')", (L["id"],)).fetchone()["n"]
            total_completed = c.execute("SELECT COUNT(*) n FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE s.language_id=? AND a.status='CONFIRMED'", (L["id"],)).fetchone()["n"]

        today_str = datetime.now().strftime("%Y-%m-%d")
        info = {}
        pair_scripts = {}
        for p in pairs:
            p_in_session = c.execute("SELECT COUNT(*) n FROM assignments WHERE pair_id=? AND status='IN_SESSION'", (p["id"],)).fetchone()["n"]
            p_waiting = c.execute("SELECT COUNT(*) n FROM assignments WHERE pair_id=? AND status='ASSIGNED'", (p["id"],)).fetchone()["n"]
            p_comp = c.execute("SELECT COUNT(*) n FROM assignments WHERE pair_id=? AND status='CONFIRMED'", (p["id"],)).fetchone()["n"]
            p_tot = p_waiting + p_in_session + p_comp
            
            # State machine requested by user:
            # - Not Started: until agent starts 1st script
            # - In Progress: actively recording script
            # - Idle: completed one and until next is started
            # - Completed: all assigned scripts completed
            if p_tot == 0:
                p_status = "Not Started"
            elif p_in_session > 0:
                p_status = "In Progress"
            elif p_comp > 0 and p_waiting > 0:
                p_status = "Idle"
            elif p_comp > 0 and p_waiting == 0 and p_in_session == 0:
                p_status = "Completed"
            elif p_comp == 0 and p_in_session == 0:
                p_status = "Not Started"
            else:
                p_status = "Idle"
                
            info[p["id"]] = dict(
                used=ops.pair_used(c, p["id"]),
                open=p_waiting + p_in_session,
                in_progress=p_in_session,
                waiting=p_waiting,
                completed=p_comp,
                total=p_tot,
                status=p_status
            )
            
            p_assigns = c.execute(
                "SELECT a.*, s.code, s.subdomain, s.specialisation "
                "FROM assignments a JOIN scripts s ON s.id=a.script_id "
                "WHERE a.pair_id=? ORDER BY a.id DESC", (p["id"],)
            ).fetchall()
            
            p_list = []
            for a in p_assigns:
                ts = a["assigned_at"] or time.time()
                dt = datetime.fromtimestamp(ts)
                p_list.append(dict(
                    id=a["id"],
                    code=a["code"],
                    subdomain=a["subdomain"],
                    specialisation=a["specialisation"] or "",
                    status=a["status"],
                    min=a["conf_min"] if a["conf_min"] is not None else a["est_min"],
                    assigned_date=dt.strftime("%Y-%m-%d"),
                    assigned_time=dt.strftime("%H:%M"),
                    assigned_display=dt.strftime("%d %b, %H:%M")
                ))
            pair_scripts[p["id"]] = p_list
            
        return render(request, "team_work.html", u, L=L, langs=langs(c), pairs=pairs, info=info,
                      pair_scripts=pair_scripts, stock=stock, S=S, paused=paused,
                      total_in_progress=total_in_progress, total_completed=total_completed,
                      today_str=today_str, can=(u["role"] == "admin" or ops.can_manage(u, L["id"])))


@app.post("/team/assign_all")
async def team_assign_all(request: Request):
    u, f = await post(request, "lead", "admin")
    lid = f.get("lang", "all")
    n = int(f.get("n") or 1)
    with db.tx() as c:
        if lid == "all":
            pairs = c.execute("SELECT id, language_id FROM pairs WHERE status='ACTIVE'").fetchall()
        else:
            pairs = c.execute("SELECT id, language_id FROM pairs WHERE language_id=? AND status='ACTIVE'", (int(lid),)).fetchall()
        total_made = 0
        notes = []
        for p in pairs:
            try:
                made, note = ops.assign(c, u, p["id"], n, override=False, reason="")
                total_made += len(made)
                if note: notes.append(f"Pair #{p['id']}: {note}")
            except Exception as e:
                notes.append(f"Pair #{p['id']}: {str(e)}")
    
    msg = f"Bulk assigned {total_made} scripts across {len(pairs)} pairs."
    if notes:
        msg += " Some pairs stopped early: " + " | ".join(notes[:3])
    return back(f"/team/work", m=msg)

@app.post("/team/assign")
async def team_assign(request: Request):
    u, f = await post(request, "lead", "admin")
    pid = int(f.get("pair"))
    with db.tx() as c:
        lid = c.execute("SELECT language_id FROM pairs WHERE id=?", (pid,)).fetchone()["language_id"]
        try:
            made, note = ops.assign(c, u, pid, int(f.get("n") or 1), override=bool(f.get("override")), reason=f.get("reason", ""))
        except (ops.OpsError, ValueError) as e:
            return back(f"/team/work?lang={lid}", e=str(e))
    return back(f"/team/work?lang={lid}", m=f"{len(made)} scripts assigned." + (f" Stopped: {note}." if note else ""))


@app.post("/team/assignment/{aid}/{act}")
async def assignment_manage(request: Request, aid: int, act: str):
    u, f = await post(request, "lead", "admin")
    with db.tx() as c:
        lid = ops.a_lang(c, aid)
        try:
            if act == "release":
                ops.release_assignment(c, u, aid)
            elif act == "confirm":
                ops.lead_confirm(c, u, aid, float(f.get("minutes") or db.setting(c, "avg_minutes")))
            elif act in ("redo", "qc_release"):
                ops.qc_decide(c, u, aid, "redo" if act == "redo" else "release")
        except ops.OpsError as e:
            return back(f.get("next", f"/team/work?lang={lid}"), e=str(e))
    return back(f.get("next", f"/team/work?lang={lid}"), m="Done.")


# ---------------------------------------------------------------- review
@app.get("/review")
async def review_list(request: Request):
    u = gate(request, "admin", "reviewer")
    with db.ro() as c:
        L = pick_lang(c, u, request)
        rows = c.execute("SELECT * FROM scripts WHERE language_id=? AND status IN ('IN_REVIEW','NEEDS_HUMAN') ORDER BY (status='NEEDS_HUMAN') DESC, wave, seq LIMIT 300",
                         (L["id"],)).fetchall() if L else []
        stats = c.execute("SELECT verdict, COUNT(*) n FROM reviews GROUP BY verdict").fetchall()
        return render(request, "review_list.html", u, L=L, langs=langs(c), rows=rows, stats={r["verdict"]: r["n"] for r in stats},
                      paused=db.setting(c, "pause_generation") == "1")


@app.get("/review/{sid}")
async def review_one(request: Request, sid: int):
    u = gate(request, "admin", "reviewer")
    with db.ro() as c:
        s = c.execute("SELECT * FROM scripts WHERE id=?", (sid,)).fetchone()
        if not s or (u["role"] == "reviewer" and s["language_id"] != u["language_id"]):
            raise HTTPException(404, "Not found")
        L = c.execute("SELECT * FROM languages WHERE id=?", (s["language_id"],)).fetchone()
        attrs = json.loads(s["attrs"])
        v = c.execute("SELECT turns FROM script_versions WHERE script_id=? AND version=?", (sid, s["version"])).fetchone()
        turns = json.loads(v["turns"]) if v else []
        job = c.execute("SELECT * FROM jobs WHERE script_id=?", (sid,)).fetchone()
        errs = V.validate(turns, attrs, L, db.S(c)) if turns else []
        return render(request, "review_one.html", u, s=s, L=L, attrs=attrs, turns=turns, text=P.render_text(turns, attrs["roles"]),
                      judge=json.loads(s["judge"]) if s["judge"] else None, job=job, errs=errs,
                      reasons=["fluency", "wrong_register", "topic_mismatch", "inconsistent", "unnatural_numbers", "offensive", "too_repetitive", "other"])


@app.post("/review/{sid}")
async def review_do(request: Request, sid: int):
    u, f = await post(request, "admin", "reviewer")
    verdict = f.get("verdict")
    with db.tx() as c:
        s = c.execute("SELECT language_id FROM scripts WHERE id=?", (sid,)).fetchone()
        try:
            if verdict == "manual":
                P.manual_script(c, sid, u, f.get("text", ""))
            else:
                P.review(c, sid, u, verdict, f.get("reason", ""), f.get("text"))
        except (ValueError, PermissionError) as e:
            return back(f"/review/{sid}", e=str(e))
        nxt = c.execute("SELECT id FROM scripts WHERE language_id=? AND status='IN_REVIEW' ORDER BY wave, seq LIMIT 1", (s["language_id"],)).fetchone()
    return back(f"/review/{nxt['id']}" if nxt else "/review", m="Saved.")


# ---------------------------------------------------------------- admin: dashboard & languages
@app.get("/admin")
async def admin_home(request: Request):
    u = gate(request, "admin")
    with db.ro() as c:
        tiles = []
        for L in langs(c):
            d, a = ops.funnel(c, L["id"])
            hum, iss = d.get("NEEDS_HUMAN", 0), c.execute("SELECT COUNT(*) n FROM issues WHERE status='OPEN' AND language_id=?", (L["id"],)).fetchone()["n"]
            score = max(0, 100 - 2 * hum - 3 * iss - (10 if db.setting(c, "pause_generation") == "1" else 0))
            conf = a.get("CONFIRMED", 0)
            tiles.append(dict(L=L, d=d, a=a, score=score, planned=L["target"] if L["planned"] else 0, conf=conf,
                              approved=d.get("APPROVED", 0), review=d.get("IN_REVIEW", 0)))
        spent = c.execute("SELECT COALESCE(SUM(cost),0) x FROM jobs").fetchone()["x"]
        
        current_L = pick_lang(c, u, request)
        from . import taxonomy
        taxonomy_rows, taxonomy_summary = taxonomy.get_taxonomy_stats(c, current_L["id"] if current_L else None)
        
        return render(request, "admin_home.html", u, tiles=tiles, alerts=ops.alerts(c), spent=spent,
                      current_L=current_L, taxonomy_rows=taxonomy_rows, taxonomy_summary=taxonomy_summary,
                      cap=float(db.setting(c, "global_cap_inr")), orphans=c.execute("SELECT COUNT(*) n FROM orphans WHERE status='OPEN'").fetchone()["n"])



import io
import csv

@app.post("/admin/language/{lid}/generate_upload")
async def generate_upload(request: Request, lid: int):
    u, f = await post(request, "admin")
    up = f.get("file")
    domain = f.get("domain", "General")
    text = (await up.read()).decode("utf-8-sig") if hasattr(up, "read") else ""
    if not text:
        return back(f"/admin/language/{lid}", e="Empty file.")
        
    code = codes.new_code()
    with db.tx() as c:
        # Check if language exists, if not, wait.
        seq = c.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM scripts WHERE language_id=?", (lid,)).fetchone()[0]
        c.execute("""
            INSERT INTO scripts(language_id, seq, code, domain, subdomain, status, source, wave) 
            VALUES(?, ?, ?, ?, ?, 'READY', 'uploaded_master', 1)
        """, (lid, seq, code, domain, domain))
        script_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        # insert version
        turn_json = json.dumps([{"role": "Speaker", "text": text}])
        c.execute("INSERT INTO script_versions(script_id, version, turns, words, created_at) VALUES(?, 1, ?, ?, ?)",
                  (script_id, turn_json, len(text.split()), time.time()))
        
    return back(f"/admin/language/{lid}", m="Master script uploaded and marked READY.")

@app.post("/admin/language/{lid}/generate_chat")
async def generate_chat(request: Request, lid: int):
    u, f = await post(request, "admin")
    prompt = f.get("prompt", "")
    count = int(f.get("count", 5))
    if not prompt:
        return back(f"/admin/language/{lid}", e="Empty prompt.")
    
    with db.tx() as c:
        for _ in range(count):
            code = codes.new_code()
            seq = c.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM scripts WHERE language_id=?", (lid,)).fetchone()[0]
            c.execute("""
                INSERT INTO scripts(language_id, seq, code, domain, subdomain, status, source, wave) 
                VALUES(?, ?, ?, ?, ?, 'READY', 'chat_generated', 1)
            """, (lid, seq, code, "Generated", "Chat"))
            script_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            
            # Simple mock generation for now - would integrate with LLM
            gen_text = f"Generated variation of: {prompt[:50]}..."
            turn_json = json.dumps([{"role": "Speaker A", "text": gen_text}, {"role": "Speaker B", "text": "Ok."}])
            c.execute("INSERT INTO script_versions(script_id, version, turns, words, created_at) VALUES(?, 1, ?, ?, ?)",
                      (script_id, turn_json, len(gen_text.split()), time.time()))
            
    return back(f"/admin/language/{lid}", m=f"{count} scripts generated and marked READY.")

@app.get("/admin/language/new")
async def lang_new(request: Request):
    u = gate(request, "admin")
    return render(request, "admin_lang_new.html", u, presets={k: v[0] for k, v in T.PRESETS.items()}, subs=T.SUBS, per=T.PER_SUB)


@app.post("/admin/language/new")
async def lang_create(request: Request):
    u, f = await post(request, "admin")
    quotas = {s[1]: int(f.get(f"q_{i}") or 0) for i, s in enumerate(T.SUBS)}
    try:
        with db.tx() as c:
            lid = admin_ops.create_language(c, u, f.get("preset"), f.get("code", ""), f.get("name", ""), int(f.get("target") or 0), quotas,
                                            float(f.get("cap_pair_h") or 7.5), float(f.get("cap_person_h") or 15), f.get("mode", "read_aloud"),
                                            f.get("deadline", ""))
    except (ValueError, KeyError) as e:
        return back("/admin/language/new", e=str(e))
    return back(f"/admin/language/{lid}", m="Language created. Review quotas, then plan the slots.")


@app.get("/admin/language/{lid}")
async def lang_page(request: Request, lid: int):
    u = gate(request, "admin")
    with db.ro() as c:
        L = c.execute("SELECT * FROM languages WHERE id=?", (lid,)).fetchone()
        if not L:
            raise HTTPException(404, "no such language")
        quotas = c.execute("SELECT * FROM quotas WHERE language_id=?", (lid,)).fetchall()
        d, a = ops.funnel(c, lid)
        waves = c.execute("SELECT wave, COUNT(*) n, SUM(status='PLANNED') planned, SUM(status IN ('QUEUED','GENERATING')) running,"
                          " SUM(status='IN_REVIEW') review, SUM(status='APPROVED') approved, SUM(status='NEEDS_HUMAN') human"
                          " FROM scripts WHERE language_id=? GROUP BY wave ORDER BY wave", (lid,)).fetchall()
        est = {w["wave"]: P.estimate(c, lid, w["wave"]) for w in waves if w["planned"]}
        days = c.execute("SELECT date(a.updated_at,'unixepoch','+5 hours','+30 minutes') d, COUNT(*) n FROM assignments a JOIN scripts s ON s.id=a.script_id"
                         " WHERE s.language_id=? AND a.status='CONFIRMED' GROUP BY 1 ORDER BY 1", (lid,)).fetchall()
        cum, run = [], 0
        for r in days:
            run += r["n"]
            cum.append((r["d"], run))
        pv = c.execute("SELECT prompt_version pv, COUNT(*) n FROM scripts WHERE language_id=? AND prompt_version IS NOT NULL GROUP BY 1", (lid,)).fetchall()
        capacity = None
        if L["cap_pair_min"]:
            avg = float(db.setting(c, "avg_minutes"))
            capacity = int(-(-(L["target"] * avg) // L["cap_pair_min"]))
        recent_scripts = c.execute("SELECT * FROM scripts WHERE language_id=? ORDER BY id DESC LIMIT 10", (lid,)).fetchall()
        return render(request, "admin_lang.html", u, L=L, quotas=quotas, d=d, a=a, cov=ops.coverage(c, lid), waves=waves, est=est, cum=cum, recent_scripts=recent_scripts,
                      prompts=pv, capacity=capacity, subs=T.SUBS, split=T.spec_split(T.PER_SUB),
                      paused_assign=db.setting(c, f"pause_assign_{lid}") == "1", gen_paused=db.setting(c, "pause_generation") == "1",
                      jobs_started=c.execute("SELECT COUNT(*) n FROM jobs").fetchone()["n"] > 0,
                      keys_ok=bool(c.execute("SELECT 1 FROM api_keys WHERE status='active' AND role IN ('writer','spare')").fetchone()))


@app.post("/admin/language/{lid}/{act}")
async def lang_act(request: Request, lid: int, act: str):
    u, f = await post(request, "admin")
    url = f"/admin/language/{lid}"
    try:
        with db.tx() as c:
            if act == "quotas":
                q = {s[1]: int(f.get(f"q_{i}") or 0) for i, s in enumerate(T.SUBS)}
                tot = admin_ops.update_quotas(c, u, lid, q)
                return back(url, m=f"Quotas saved. Target is now {tot}.")
            if act == "plan":
                n = admin_ops.plan(c, u, lid)
                return back(url, m=f"{n} slots planned (code decided every attribute). Wave 1 is ready to release.")
            if act == "wave":
                w, what = int(f.get("wave")), f.get("what")
                if what == "release":
                    n = P.release_wave(c, lid, w, u)
                    return back(url, m=f"Wave {w} released: {n} jobs queued.")
                n = P.cancel_wave(c, lid, w, u)
                return back(url, m=f"Wave {w}: {n} queued jobs cancelled.")
            if act == "quarantine":
                hit = P.quarantine_prompt(c, lid, f.get("pv"), u)
                return back(url, m="Quarantined." + (f" ALREADY ASSIGNED (tell the client if recorded): {', '.join(hit)}" if hit else ""))
            if act == "requeue":
                return back(url, m=f"{P.requeue_quarantined(c, lid, u)} quarantined scripts re-queued.")
            if act == "revokeall":
                ops.revoke_all(c, u, lid)
                return back(url, m="All participant codes revoked.")
            if act == "pauseassign":
                cur = db.setting(c, f"pause_assign_{lid}") == "1"
                db.set_setting(c, f"pause_assign_{lid}", 0 if cur else 1)
                db.audit(c, u, "pause_assign", f"lang={lid} now={not cur}")
                return back(url, m="Assignment " + ("resumed." if cur else "paused."))
            if act == "mode":
                c.execute("UPDATE languages SET mode=? WHERE id=?", (f.get("mode"), lid))
                return back(url, m="Script view mode saved.")
            if act == "caps":
                c.execute("UPDATE languages SET cap_pair_min=?, cap_person_min=?, deadline=? WHERE id=?",
                          (float(f["cap_pair_h"]) * 60, float(f["cap_person_h"]) * 60, f.get("deadline", ""), lid))
                db.audit(c, u, "caps", f"lang={lid} pair={f['cap_pair_h']}h person={f['cap_person_h']}h")
                return back(url, m="Caps saved.")
    except (ValueError, PermissionError) as e:
        return back(url, e=str(e))
    raise HTTPException(404, "unknown action")


# ---------------------------------------------------------------- admin: generation
@app.get("/admin/generation")
async def generation(request: Request):
    u = gate(request, "admin")
    with db.ro() as c:
        keys = c.execute("SELECT * FROM api_keys ORDER BY id").fetchall()
        q = {r["status"]: r["n"] for r in c.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}
        human = c.execute("SELECT s.id, s.code, j.error, j.attempts FROM jobs j JOIN scripts s ON s.id=j.script_id WHERE j.status='NEEDS_HUMAN' LIMIT 100").fetchall()
        spent = c.execute("SELECT COALESCE(SUM(cost),0) x FROM jobs").fetchone()["x"]
        remaining = q.get("QUEUED", 0) + q.get("RUNNING", 0)
        done = q.get("DONE", 0)
        return render(request, "admin_gen.html", u, keys=keys, q=q, human=human, spent=spent, cap=float(db.setting(c, "global_cap_inr")),
                      paused=db.setting(c, "pause_generation") == "1", remaining=remaining, done=done,
                      threads=not os.environ.get("SF_NO_THREADS"), langs=langs(c))


@app.post("/admin/keys/add")
async def key_add(request: Request):
    u, f = await post(request, "admin")
    try:
        with db.tx() as c:
            admin_ops.add_key(c, u, f.get("label") or "key", f.get("provider"), f.get("model") or "model", f.get("role"), f.get("api_key", ""),
                              f.get("base_url", ""), f.get("languages") or "*", int(f.get("rpm") or 30), float(f.get("daily_cap") or 500),
                              float(f.get("monthly_cap") or 5000), float(f.get("price_in") or 0), float(f.get("price_out") or 0))
    except ValueError as e:
        return back("/admin/generation", e=str(e))
    return back("/admin/generation", m="Key added. A worker starts automatically.")


@app.post("/admin/keys/{kid}/{act}")
async def key_act(request: Request, kid: int, act: str):
    u, f = await post(request, "admin")
    with db.tx() as c:
        if act in ("pause", "resume", "disable"):
            c.execute("UPDATE api_keys SET status=?, cool_until=0 WHERE id=?", ({"pause": "paused", "resume": "active", "disable": "disabled"}[act], kid))
        elif act == "delete":
            c.execute("DELETE FROM api_keys WHERE id=?", (kid,))
        elif act == "rotate":
            c.execute("UPDATE api_keys SET api_key=? WHERE id=?", (f.get("api_key", ""), kid))
        db.audit(c, u, "key_" + act, str(kid))
    return back("/admin/generation", m="Done.")


@app.post("/admin/gen/{act}")
async def gen_toggle(request: Request, act: str):
    u, f = await post(request, "admin")
    with db.tx() as c:
        db.set_setting(c, "pause_generation", 1 if act == "pause" else 0)
        if act == "resume":
            r = c.execute("SELECT COALESCE(MAX(id),0) m FROM reviews").fetchone()["m"]
            db.set_setting(c, "stop_after_review", r)
        db.audit(c, u, "generation_" + act)
    return back("/admin/generation", m="Generation " + ("paused." if act == "pause" else "resumed (stop-the-line window reset)."))


@app.post("/admin/script/{sid}/retry")
async def script_retry(request: Request, sid: int):
    u, f = await post(request, "admin")
    with db.tx() as c:
        P.retry_job(c, sid, u)
    return back("/admin/generation", m="Re-queued with a new seed.")


@app.get("/admin/script/{sid}")
async def script_lineage(request: Request, sid: int):
    u = gate(request, "admin")
    with db.ro() as c:
        s = c.execute("SELECT * FROM scripts WHERE id=?", (sid,)).fetchone()
        if not s:
            raise HTTPException(404, "no such script")
        versions = c.execute("SELECT version, words, prompt_version, note, created_at FROM script_versions WHERE script_id=?", (sid,)).fetchall()
        # Extract script text from latest version
        script_text = ""
        latest = c.execute("SELECT turns FROM script_versions WHERE script_id=? ORDER BY version DESC LIMIT 1", (sid,)).fetchone()
        if latest and latest["turns"]:
            try:
                turns = json.loads(latest["turns"])
                script_text = "\n\n".join(t.get("text", "") for t in turns if t.get("text"))
            except Exception:
                script_text = str(latest["turns"])
        return render(request, "admin_lineage.html", u, s=s, script_text=script_text,
                      versions=versions,
                      assigns=c.execute("SELECT * FROM assignments WHERE script_id=?", (sid,)).fetchall())


# ---------------------------------------------------------------- admin: data, settings, audit
@app.get("/admin/data")
async def data_page(request: Request):
    u = gate(request, "admin", "lead")
    with db.ro() as c:
        L = pick_lang(c, u, request)
        orph = c.execute("SELECT * FROM orphans WHERE status='OPEN' ORDER BY id DESC LIMIT 50").fetchall() if u["role"] == "admin" else []
        return render(request, "admin_data.html", u, L=L, langs=langs(c), reports=ops.REPORTS if u["role"] == "admin" else
                      [r for r in ops.REPORTS if r != "audit"], orphans=[(o["id"], json.loads(o["payload"])) for o in orph],
                      mode=db.setting(c, "verification_mode"), result=None)


@app.post("/admin/data/{act}")
async def data_act(request: Request, act: str):
    u, f = await post(request, "admin")
    up = f.get("file")
    raw = await up.read() if hasattr(up, "read") else b""
    lid = int(f.get("lang") or 0)
    with db.tx() as c:
        try:
            if act == "reconcile":
                r = ops.reconcile_csv(c, u, raw.decode("utf-8-sig"))
                return back("/admin/data", m=f"Reconcile: {r['confirmed']} confirmed, {r['duplicate']} duplicates, {r['out_of_spec']} out of spec, {r['orphan']} orphans.")
            if act == "qc":
                r = ops.qc_csv(c, u, raw.decode("utf-8-sig"))
                return back("/admin/data", m=f"QC: {r['passed']} pass, {r['failed']} fail, {r['unknown']} unknown.")
            if act == "import":
                bid, items = ops.import_preview(c, u, lid, [(up.filename, raw)])
                L = c.execute("SELECT * FROM languages WHERE id=?", (lid,)).fetchone()
                return render(request, "admin_data.html", u, L=L, langs=langs(c), reports=ops.REPORTS, orphans=[], mode="",
                              result=dict(batch=bid, rows=items))
            if act == "importcommit":
                n, sk = ops.import_commit(c, u, int(f.get("batch")))
                return back("/admin/data", m=f"Imported {n} scripts ({sk} skipped). 10% sample sent to review.")
            if act == "orphan":
                c.execute("UPDATE orphans SET status='DISMISSED' WHERE id=?", (int(f.get("id")),))
                return back("/admin/data", m="Dismissed.")
            if act == "revalidate":
                bad = ops.revalidate(c, lid)
                L = c.execute("SELECT * FROM languages WHERE id=?", (lid,)).fetchone()
                return render(request, "admin_data.html", u, L=L, langs=langs(c), reports=ops.REPORTS, orphans=[], mode="",
                              result=dict(revalidate=bad))
        except (ValueError, ops.OpsError, KeyError, UnicodeDecodeError) as e:
            return back("/admin/data", e=str(e))
    raise HTTPException(404, "unknown")


@app.get("/report/{name}")
async def report(request: Request, name: str):
    u = gate(request, "admin", "lead")
    masked = request.query_params.get("mask", "1") != "0"
    lid = request.query_params.get("lang")
    with db.tx() as c:
        data = ops.export(c, u, name, int(lid) if lid and lid.isdigit() else None, masked)
    return Response(data, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{name}.csv"'})


SETTING_HELP = [
    ("Validators (changing any bumps the Spec version)", [
        ("words_min", "Min words per script"), ("words_max", "Max words per script"), ("max_turn_words", "Max words in one turn"),
        ("sim_threshold", "Near-copy threshold (5-gram overlap, 0-1)"), ("banned_topics", "Banned words/topics (comma separated)")]),
    ("Assignment & hours", [
        ("avg_minutes", "Estimated minutes per script"), ("open_limit", "Max open scripts per pair"), ("deadline_hours", "Hours before an untouched script auto-releases"),
        ("abandon_hours", "Hours in session before ABANDONED"), ("allow_test_scripts", "1 = allow stub TEST scripts to be assigned (demo only)")]),
    ("Verification (Gate 0 answer)", [
        ("verification_mode", "self_report (UNVERIFIED) or client_log"), ("dur_min_sec", "Allowed recording duration, min seconds"),
        ("dur_max_sec", "Allowed recording duration, max seconds"), ("client_url", "Client recorder link, use {script_id}"),
        ("rate_per_hour_inr", "Payout rate, Rs per hour")]),
    ("Review & cost", [
        ("review_pct", "Random review % after Wave 1"), ("reject_stop_pct", "Stop-the-line reject % (last 30 reviews)"), ("reject_stop_min", "Min reviews before the rule applies"),
        ("global_cap_inr", "Global generation spend cap, Rs"), ("tokens_per_word", "Tokens per word (for estimates; measure in the pilot)"),
        ("wave1_size", "Wave 1 size (applies when planning)"), ("wave_size", "Later wave size (applies when planning)"),
        ("stub_chaos", "Stub test provider: chance of a bad draft (0-1)")]),
    ("Messages", [("lead_whatsapp_nudge", "WhatsApp nudge text. {name} is replaced")]),
]
SPEC_KEYS = {"words_min", "words_max", "max_turn_words", "sim_threshold", "banned_topics"}


@app.get("/admin/settings")
async def settings_page(request: Request):
    u = gate(request, "admin")
    with db.ro() as c:
        S = db.S(c)
        return render(request, "admin_settings.html", u, S=S, groups=SETTING_HELP, prompt=P.master_prompt(c))


@app.post("/admin/settings")
async def settings_save(request: Request):
    u, f = await post(request, "admin")
    with db.tx() as c:
        S = db.S(c)
        spec_changed = False
        for _, items in SETTING_HELP:
            for k, _l in items:
                if k in f and f[k] != S.get(k):
                    db.set_setting(c, k, f[k])
                    db.audit(c, u, "setting", f"{k}: {S.get(k)} -> {f[k]}")
                    spec_changed = spec_changed or k in SPEC_KEYS
        if f.get("master_prompt") is not None and f["master_prompt"].strip() != P.master_prompt(c).strip():
            db.set_setting(c, "master_prompt", f["master_prompt"].strip())
            n = int(S["prompt_version"].lstrip("p") or 1) + 1
            db.set_setting(c, "prompt_version", f"p{n}")
            db.audit(c, u, "master_prompt", f"new version p{n}")
        if spec_changed:
            db.set_setting(c, "spec_version", int(S["spec_version"]) + 1)
            db.audit(c, u, "spec_version", "bumped; run Re-validate to see impact")
    return back("/admin/settings", m="Saved." + (" Spec version bumped: run Re-validate on the Data page." if spec_changed else ""))


@app.get("/admin/audit")
async def audit_page(request: Request):
    u = gate(request, "admin")
    with db.ro() as c:
        rows = c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT 400").fetchall()
        return render(request, "admin_audit.html", u, rows=rows)



@app.get("/admin/scripts")
async def scripts_page(request: Request):
    u = gate(request, "admin", "lead")
    view_tab = request.query_params.get("tab", "available")
    req_domain = request.query_params.get("domain", "")
    req_sub = request.query_params.get("subdomain", "")
    req_spec = request.query_params.get("spec", "")
    view_mode = request.query_params.get("view", "folder")

    with db.ro() as c:
        L = pick_lang(c, u, request)
        from . import taxonomy
        folder_nav = taxonomy.get_folder_navigation(c, L, req_domain=req_domain, req_sub=req_sub, req_spec=req_spec, tab=view_tab)
        
        # When browsing a folder, filter scripts to that taxonomy branch
        matching_ids = folder_nav["matching_ids"]
        where_s = []
        args = []
        if L["id"] != "all":
            where_s.append("s.language_id = ?")
            args.append(L["id"])

        if req_domain or req_sub or req_spec:
            if matching_ids:
                where_s.append(f"s.id IN ({','.join(str(i) for i in matching_ids)})")
            else:
                where_s.append("1=0")

        wh_s = (" WHERE " + " AND ".join(where_s)) if where_s else ""
        total_scripts = c.execute(f"SELECT COUNT(*) n FROM scripts s{wh_s}", args).fetchone()["n"]

        conds_avail = list(where_s) + [
            "s.status IN ('APPROVED','READY')",
            "NOT EXISTS (SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED'))"
        ]
        wh_avail = " WHERE " + " AND ".join(conds_avail)
        avail_scripts = c.execute(f"SELECT COUNT(*) n FROM scripts s{wh_avail}", args).fetchone()["n"]

        conds_prog = list(where_s) + ["a.status IN ('ASSIGNED','IN_SESSION')"]
        wh_prog = " WHERE " + " AND ".join(conds_prog)
        in_prog_scripts = c.execute(f"SELECT COUNT(*) n FROM assignments a JOIN scripts s ON s.id=a.script_id{wh_prog}", args).fetchone()["n"]

        conds_comp = list(where_s) + ["a.status = 'CONFIRMED'"]
        wh_comp = " WHERE " + " AND ".join(conds_comp)
        completed_scripts = c.execute(f"SELECT COUNT(*) n FROM assignments a JOIN scripts s ON s.id=a.script_id{wh_comp}", args).fetchone()["n"]

        available_rows = []
        assigned_rows = []
        completed_rows = []

        if view_tab == "assigned":
            conds_assign_rows = list(where_s) + ["a.status IN ('ASSIGNED', 'IN_SESSION', 'DISPUTED')"]
            wh_assign = " WHERE " + " AND ".join(conds_assign_rows)
            assigned_rows = c.execute(
                f"SELECT s.id, s.code, s.domain, s.subdomain, s.specialisation, l.name lang_name, "
                f"a.id as aid, a.pair_id, a.est_min, a.assigned_at, a.status as a_status, "
                f"ua.name as an, ub.name as bn "
                f"FROM assignments a JOIN scripts s ON s.id=a.script_id JOIN languages l ON l.id=s.language_id "
                f"JOIN users ua ON ua.id=a.ua JOIN users ub ON ub.id=a.ub "
                f"{wh_assign} "
                f"ORDER BY a.id DESC LIMIT 500",
                args
            ).fetchall()
        elif view_tab == "completed":
            conds_comp_rows = list(where_s) + ["a.status = 'CONFIRMED'"]
            wh_comp_rows = " WHERE " + " AND ".join(conds_comp_rows)
            completed_rows = c.execute(
                f"SELECT s.id, s.code, s.domain, s.subdomain, s.specialisation, l.name lang_name, "
                f"a.id as aid, a.pair_id, a.conf_min, a.est_min, a.assigned_at, a.updated_at, a.status as a_status, "
                f"ua.name as an, ua.phone as a_phone, ua.email as a_email, "
                f"ub.name as bn, ub.phone as b_phone, ub.email as b_email "
                f"FROM assignments a JOIN scripts s ON s.id=a.script_id JOIN languages l ON l.id=s.language_id "
                f"JOIN users ua ON ua.id=a.ua JOIN users ub ON ub.id=a.ub "
                f"{wh_comp_rows} "
                f"ORDER BY a.updated_at DESC LIMIT 500",
                args
            ).fetchall()
        else:
            view_tab = "available"
            conds_avail_rows = list(where_s) + [
                "s.status IN ('APPROVED','READY')",
                "NOT EXISTS (SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED'))"
            ]
            wh_avail_rows = " WHERE " + " AND ".join(conds_avail_rows)
            available_rows = c.execute(
                f"SELECT s.*, l.name as lang_name FROM scripts s JOIN languages l ON s.language_id=l.id "
                f"{wh_avail_rows} "
                f"ORDER BY s.id DESC LIMIT 500",
                args
            ).fetchall()
            
        return render(request, "admin_scripts.html", u, L=L, view_tab=view_tab,
                      available_rows=available_rows, assigned_rows=assigned_rows, completed_rows=completed_rows,
                      total_scripts=total_scripts, avail_scripts=avail_scripts,
                      in_prog_scripts=in_prog_scripts, completed_scripts=completed_scripts, langs=langs(c),
                      folder_nav=folder_nav, view_mode=view_mode,
                      req_domain=req_domain, req_sub=req_sub, req_spec=req_spec)


@app.post("/admin/scripts/sync_folder")
async def scripts_sync_folder(request: Request):
    u = gate(request, "admin", "lead")
    f = await request.form()
    lid_val = f.get("lang")
    from . import taxonomy
    with db.tx() as c:
        res = taxonomy.scan_and_import_folder(c, base_dir="scripts_library", target_lang_id=lid_val)
    msg = f"Synced scripts_library: {res['imported']} new scripts imported, {res['skipped']} already existed."
    return back(f"/admin/scripts?lang={lid_val}", m=msg)


@app.post("/admin/scripts/upload_zip")
async def scripts_upload_zip(request: Request):
    u = gate(request, "admin", "lead")
    f = await request.form()
    up = f.get("zip_file")
    lid_val = f.get("lang")
    if not up or not hasattr(up, "read"):
        return back(f"/admin/scripts?lang={lid_val}", e="Please select a .zip file.")
    import zipfile, io
    zip_bytes = await up.read()
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall("scripts_library")
    except Exception as ex:
        return back(f"/admin/scripts?lang={lid_val}", e=f"Invalid zip archive: {str(ex)}")
    from . import taxonomy
    with db.tx() as c:
        res = taxonomy.scan_and_import_folder(c, base_dir="scripts_library", target_lang_id=lid_val)
    msg = f"Extracted & imported zip: {res['imported']} new scripts added, {res['skipped']} already existed."
    return back(f"/admin/scripts?lang={lid_val}", m=msg)


@app.post("/admin/scripts/upload")
async def script_upload_single(request: Request):
    u, f = await post(request, "admin", "lead")
    lid_val = f.get("lang")
    if not lid_val or lid_val == "all":
        with db.ro() as _c:
            first_l = _c.execute("SELECT id FROM languages ORDER BY id LIMIT 1").fetchone()
            lid = first_l["id"] if first_l else 1
    else:
        lid = int(lid_val)
        
    up = f.get("file")
    paste_text = (f.get("paste_text") or "").strip()
    
    text = ""
    if up and hasattr(up, "read"):
        raw = await up.read()
        if raw:
            text = raw.decode("utf-8-sig", errors="ignore").strip()
    if not text and paste_text:
        text = paste_text
        
    if not text:
        return back(f"/admin/scripts?tab=available&lang={lid}", e="Please upload a script file or paste dialogue text.")
        
    domain = (f.get("domain") or "General").strip()
    subdomain = (f.get("subdomain") or domain).strip()
    
    code = codes.new_code()
    with db.tx() as c:
        seq = c.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM scripts WHERE language_id=?", (lid,)).fetchone()[0]
        c.execute("""
            INSERT INTO scripts(language_id, seq, code, domain, subdomain, status, source, wave)
            VALUES(?, ?, ?, ?, ?, 'READY', 'uploaded', 1)
        """, (lid, seq, code, domain, subdomain))
        script_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        
        turn_json = json.dumps([{"role": "Speaker", "text": text}])
        word_count = len(text.split())
        c.execute("""
            INSERT INTO script_versions(script_id, version, turns, words, created_at)
            VALUES(?, ?, ?, ?, ?)
        """, (script_id, 1, turn_json, word_count, time.time()))
        
        db.audit(c, u, "upload_script", f"id={script_id} code={code} lang={lid}")
        
    return back(f"/admin/scripts?tab=available&lang={lid}", m=f"Script {code} ({domain}) uploaded and marked READY to assign!")


@app.post("/admin/scripts/bulk_upload")
async def script_bulk_upload(request: Request):
    u, f = await post(request, "admin", "lead")
    lid_val = f.get("lang")
    if not lid_val or lid_val == "all":
        with db.ro() as _c:
            first_l = _c.execute("SELECT id FROM languages ORDER BY id LIMIT 1").fetchone()
            lid = first_l["id"] if first_l else 1
    else:
        lid = int(lid_val)
        
    up = f.get("file")
    paste_csv = (f.get("csv_paste") or "").strip()
    
    csv_content = ""
    if up and hasattr(up, "read"):
        raw = await up.read()
        if raw:
            csv_content = raw.decode("utf-8-sig", errors="ignore").strip()
    if not csv_content and paste_csv:
        csv_content = paste_csv
        
    if not csv_content:
        return back(f"/admin/scripts?tab=available&lang={lid}", e="Please upload a CSV file or paste CSV text.")
        
    count = 0
    with db.tx() as c:
        reader = csv.DictReader(io.StringIO(csv_content))
        for r in reader:
            r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
            text = r.get("text") or r.get("script") or r.get("content") or r.get("dialogue") or ""
            if not text:
                continue
            domain = r.get("domain") or r.get("topic") or "General"
            subdomain = r.get("subdomain") or r.get("scenario") or domain
            
            code = codes.new_code()
            seq = c.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM scripts WHERE language_id=?", (lid,)).fetchone()[0]
            c.execute("""
                INSERT INTO scripts(language_id, seq, code, domain, subdomain, status, source, wave)
                VALUES(?, ?, ?, ?, ?, 'READY', 'csv_bulk', 1)
            """, (lid, seq, code, domain, subdomain))
            script_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            
            turn_json = json.dumps([{"role": "Speaker", "text": text}])
            word_count = len(text.split())
            c.execute("""
                INSERT INTO script_versions(script_id, version, turns, words, created_at)
                VALUES(?, ?, ?, ?, ?)
            """, (script_id, 1, turn_json, word_count, time.time()))
            count += 1
            
        db.audit(c, u, "bulk_upload_scripts", f"lang={lid} count={count}")
        
    return back(f"/admin/scripts?tab=available&lang={lid}", m=f"Bulk imported {count} scripts into stock. They are now READY to assign!")


@app.get("/search")
async def search(request: Request):
    u = gate(request, "admin", "lead")
    qs = (request.query_params.get("q") or "").strip()
    res = dict(scripts=[], people=[])
    if qs:
        with db.ro() as c:
            sf, sa = ("", ()) if u["role"] == "admin" else (" AND language_id=?", (u["language_id"],))
            res["scripts"] = c.execute("SELECT * FROM scripts WHERE code LIKE ?" + sf + " LIMIT 20", (qs.upper() + "%",) + sa).fetchall()
            res["people"] = c.execute("SELECT * FROM users WHERE (name LIKE ? OR phone LIKE ?) AND role<>'admin'" + sf + " LIMIT 20",
                                      (f"%{qs}%", f"%{ops.norm_phone(qs) or qs}%") + sa).fetchall()
    return render(request, "search.html", u, qs=qs, res=res, mask=ops.mask)
