"""People, pairing, assignment, session flow, verification, watchdogs, import/export."""
import csv
import io
import json
import re
import secrets
import time
import zipfile
from datetime import datetime

from . import codes, db
from . import pipeline as P
from . import validators as V


class OpsError(Exception):
    pass


ACTIVE_EST = ("ASSIGNED", "IN_SESSION", "SELF_REPORTED", "DISPUTED")
LIVE = "('ASSIGNED','IN_SESSION','SELF_REPORTED','DISPUTED','CONFIRMED','QC_FAIL')"
SESSION_TTL = 14 * 86400


# ---------------------------------------------------------------- helpers / permissions
def norm_phone(p):
    d = re.sub(r"\D", "", p or "")
    return d[-10:] if len(d) >= 10 else d


def norm_email(e):
    return (e or "").strip().lower()


def can_manage(actor, language_id):
    if actor is None or actor["role"] == "admin":
        return True
    return actor["role"] == "lead" and actor["language_id"] == language_id and actor["perm"] == "manage"


def can_view(actor, language_id):
    return actor["role"] == "admin" or (actor["role"] in ("lead", "reviewer") and actor["language_id"] == language_id)


def need_manage(actor, language_id):
    if not can_manage(actor, language_id):
        raise PermissionError("not allowed for this language / permission")


# ---------------------------------------------------------------- users, codes, auth
def create_user(c, actor, name, role, language_id, phone="", email="", gender="", perm="manage", opening_min=0.0):
    if role not in ("admin", "lead", "reviewer", "participant"):
        raise OpsError("bad role")
    if role == "admin" and (actor is None or actor["role"] != "admin"):
        raise PermissionError("admin only")
    if role != "admin":
        if not language_id:
            raise OpsError("language required")
        need_manage(actor, language_id) if role == "participant" else _admin_only(actor)
    name = (name or "").strip()
    if not name:
        raise OpsError("name required")
    phone, email = norm_phone(phone), norm_email(email)
    if role == "participant":
        if phone and c.execute("SELECT 1 FROM users WHERE phone=? AND role='participant'", (phone,)).fetchone():
            raise OpsError(f"duplicate phone {phone}")
        if email and c.execute("SELECT 1 FROM users WHERE email=? AND role='participant'", (email,)).fetchone():
            raise OpsError(f"duplicate email {email}")
    gender = gender.upper()[:1] if gender.upper()[:1] in ("M", "F") else ""
    for _ in range(20):
        code = codes.new_code()
        if not c.execute("SELECT 1 FROM users WHERE code=?", (code,)).fetchone():
            break
    cur = c.execute("INSERT INTO users(name,phone,email,role,language_id,code,gender,perm,opening_min,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (name, phone, email, role, language_id, code, gender, perm, opening_min, time.time()))
    db.audit(c, actor if actor else "system", "create_user", f"{role} {name} lang={language_id}")
    return cur.lastrowid, code


def update_user(c, actor, uid, name, phone="", email="", role=None, language_id=None, active=None):
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        raise OpsError("user not found")
    if u["role"] == "admin" and (actor is None or actor["role"] != "admin"):
        raise PermissionError("admin only")
    need_manage(actor, u["language_id"]) if u["role"] == "participant" else _admin_only(actor)
    
    name = (name or "").strip()
    if not name:
        raise OpsError("name required")
    ph = norm_phone(phone)
    em = norm_email(email)
    
    if ph:
        dup = c.execute("SELECT id FROM users WHERE phone=? AND role='participant' AND id!=?", (ph, uid)).fetchone()
        if dup:
            raise OpsError(f"phone {ph} is already in use by another participant")
    if em:
        dup = c.execute("SELECT id FROM users WHERE email=? AND role='participant' AND id!=?", (em, uid)).fetchone()
        if dup:
            raise OpsError(f"email {em} is already in use by another participant")
            
    c.execute("UPDATE users SET name=?, phone=?, email=? WHERE id=?", (name, ph, em, uid))
    if role and (actor["role"] == "admin" or u["role"] == "participant"):
        if role in ("participant", "lead", "reviewer"):
            c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    if language_id and actor["role"] == "admin":
        c.execute("UPDATE users SET language_id=? WHERE id=?", (language_id, uid))
    if active is not None:
        c.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, uid))
        if not active:
            c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
            
    db.audit(c, actor, "update_user", f"id={uid} name={name}")


def delete_user(c, actor, uid):
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        raise OpsError("user not found")
    if u["role"] == "admin":
        raise OpsError("cannot delete admin account")
    if actor is not None and actor["id"] == uid:
        raise OpsError("cannot delete your own account while logged in")
    if actor is not None and actor["role"] != "admin":
        need_manage(actor, u["language_id"])
        if u["role"] in ("lead", "admin"):
            raise PermissionError("team lead can only delete participants")
    
    now = time.time()
    # 1. Release any unconfirmed assignments assigned to this user
    c.execute("UPDATE assignments SET status='RELEASED', note='agent deleted', updated_at=? WHERE (ua=? OR ub=?) AND status IN ('ASSIGNED','IN_SESSION')", (now, uid, uid))
    
    # 2. Clean up pairs involving this user
    user_pairs = c.execute("SELECT id FROM pairs WHERE a=? OR b=?", (uid, uid)).fetchall()
    for p in user_pairs:
        pid = p["id"]
        c.execute("UPDATE assignments SET status='RELEASED', note='agent deleted', updated_at=? WHERE pair_id=? AND status IN ('ASSIGNED','IN_SESSION')", (now, pid))
        conf_count = c.execute("SELECT COUNT(*) n FROM assignments WHERE pair_id=? AND status='CONFIRMED'", (pid,)).fetchone()["n"]
        if conf_count == 0:
            c.execute("DELETE FROM pairs WHERE id=?", (pid,))
        else:
            c.execute("UPDATE pairs SET status='DROPPED', dropped_at=?, note='agent deleted' WHERE id=?", (now, pid))
            
    # 3. Clean up sessions, device requests, issues
    c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    c.execute("DELETE FROM device_requests WHERE user_id=?", (uid,))
    c.execute("DELETE FROM issues WHERE user_id=?", (uid,))
    
    # 4. Delete the user
    c.execute("DELETE FROM users WHERE id=?", (uid,))
    db.audit(c, actor, "delete_user", f"id={uid} name={u['name']} role={u['role']}")
    return True


def delete_all_agents(c, actor, language_id="all"):
    if actor is not None and actor["role"] != "admin":
        if language_id == "all":
            language_id = actor["language_id"]
        need_manage(actor, language_id)
    
    if language_id == "all":
        rows = c.execute("SELECT id FROM users WHERE role='participant'").fetchall()
    else:
        rows = c.execute("SELECT id FROM users WHERE role='participant' AND language_id=?", (int(language_id),)).fetchall()
        
    count = 0
    for r in rows:
        delete_user(c, actor, r["id"])
        count += 1
    db.audit(c, actor, "delete_all_agents", f"lang={language_id} count={count}")
    return count


def _admin_only(actor):
    if actor is not None and actor["role"] != "admin":
        raise PermissionError("admin only")


def bulk_preview(c, language_id, text):
    rows, seen_p, seen_e = [], set(), set()
    rd = csv.DictReader(io.StringIO(text.strip()))
    for r in rd:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        name, ph, em = r.get("name", ""), norm_phone(r.get("phone", "")), norm_email(r.get("email", ""))
        st = "OK"
        if not name:
            st = "INVALID: no name"
        elif ph and (len(ph) != 10 or ph in seen_p or c.execute("SELECT 1 FROM users WHERE phone=? AND role='participant'", (ph,)).fetchone()):
            st = "DUPLICATE/INVALID phone"
        elif em and (em in seen_e or c.execute("SELECT 1 FROM users WHERE email=? AND role='participant'", (em,)).fetchone()):
            st = "DUPLICATE email"
        elif not ph and not em:
            st = "INVALID: phone or email needed"
        if st == "OK":
            seen_p.add(ph)
            seen_e.add(em)
        try:
            hrs = float(r.get("hours_used", "0") or 0)
        except ValueError:
            hrs, st = 0, "INVALID: hours_used"
        rows.append(dict(name=name, phone=ph, email=em, gender=r.get("gender", ""), hours_used=hrs, status=st))
    return rows


def bulk_commit(c, actor, language_id, rows):
    need_manage(actor, language_id)
    n = 0
    for r in rows:
        if r["status"] == "OK":
            create_user(c, actor, r["name"], "participant", language_id, r["phone"], r["email"], r["gender"],
                        opening_min=r["hours_used"] * 60)
            n += 1
    return n


def login(c, code, device, ip="?"):
    """Returns (token|None, message, pending_flag)."""
    if not codes.valid(code):
        return None, "That code has a typo. Check it and try again.", False
    u = c.execute("SELECT * FROM users WHERE code=?", (codes.norm(code),)).fetchone()
    if not u or not u["active"]:
        return None, "Code not recognised or switched off. Ask your team lead.", False
    if u["role"] != "admin":
        if not u["device"]:
            c.execute("UPDATE users SET device=? WHERE id=?", (device, u["id"]))
        elif u["device"] != device:
            if not c.execute("SELECT 1 FROM device_requests WHERE user_id=? AND device=? AND status='PENDING'", (u["id"], device)).fetchone():
                c.execute("INSERT INTO device_requests(user_id,device,created) VALUES(?,?,?)", (u["id"], device, time.time()))
                db.audit(c, "system", "device_request", f"user={u['id']}")
            return None, "This is a new device. Your team lead must approve it. Ask them and try again.", True
    tok, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
    c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (tok, u["id"], csrf, time.time()))
    c.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), u["id"]))
    db.audit(c, u, "login", ip)
    return tok, "", False


def get_session(c, tok):
    if not tok:
        return None, None
    r = c.execute("SELECT s.csrf, s.created, u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (tok,)).fetchone()
    if not r or not r["active"] or time.time() - r["created"] > SESSION_TTL:
        return None, None
    return r, r["csrf"]


def reissue_code(c, actor, uid):
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    need_manage(actor, u["language_id"]) if u["role"] == "participant" else _admin_only(actor)
    new = codes.new_code()
    c.execute("UPDATE users SET code=?, device=NULL, active=1 WHERE id=?", (new, uid))
    c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    db.audit(c, actor, "reissue_code", f"user={uid}")
    return new


def set_active(c, actor, uid, active):
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    need_manage(actor, u["language_id"]) if u["role"] == "participant" else _admin_only(actor)
    c.execute("UPDATE users SET active=? WHERE id=?", (1 if active else 0, uid))
    if not active:
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
    db.audit(c, actor, "activate" if active else "revoke", f"user={uid}")


def view_code(c, actor, uid):
    """Plain-text code visible only to admin / the language's managing lead. Every view is logged."""
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if u["role"] == "admin":
        raise PermissionError("no")
    need_manage(actor, u["language_id"]) if u["role"] == "participant" else _admin_only(actor)
    db.audit(c, actor, "view_code", f"user={uid}")
    return u["code"]


def revoke_all(c, actor, language_id):
    _admin_only(actor)
    c.execute("UPDATE users SET active=0 WHERE language_id=? AND role='participant'", (language_id,))
    c.execute("DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE language_id=? AND role='participant')", (language_id,))
    db.audit(c, actor, "revoke_all_codes", f"lang={language_id}")


def decide_device(c, actor, req_id, ok):
    r = c.execute("SELECT d.*, u.language_id FROM device_requests d JOIN users u ON u.id=d.user_id WHERE d.id=?", (req_id,)).fetchone()
    need_manage(actor, r["language_id"])
    if ok:
        c.execute("UPDATE users SET device=? WHERE id=?", (r["device"], r["user_id"]))
    c.execute("UPDATE device_requests SET status=? WHERE id=?", ("APPROVED" if ok else "DENIED", req_id))
    db.audit(c, actor, "device_" + ("approve" if ok else "deny"), f"user={r['user_id']}")


def accept_consent(c, uid, optin, version, text_ok=True):
    c.execute("UPDATE users SET consent_at=?, consent_v=?, optin=? WHERE id=?", (time.time(), version, 1 if optin else 0, uid))
    c.execute("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)", (time.time(), f"participant:{uid}", "consent", f"{version} optin={optin}"))


def set_availability(c, uid, slots):
    c.execute("UPDATE users SET availability=? WHERE id=?", (json.dumps(sorted(set(slots))), uid))


# ---------------------------------------------------------------- pairs
def unpaired(c, language_id):
    if language_id == "all":
        return c.execute("SELECT u.*, l.name lang_name FROM users u JOIN languages l ON l.id=u.language_id WHERE u.role='participant' AND u.active=1 AND u.id NOT IN"
                         " (SELECT a FROM pairs WHERE status='ACTIVE' UNION SELECT b FROM pairs WHERE status='ACTIVE')"
                         " ORDER BY u.language_id, u.id").fetchall()
    return c.execute("SELECT u.*, l.name lang_name FROM users u JOIN languages l ON l.id=u.language_id WHERE u.role='participant' AND u.active=1 AND u.language_id=? AND u.id NOT IN"
                     " (SELECT a FROM pairs WHERE status='ACTIVE' UNION SELECT b FROM pairs WHERE status='ACTIVE')"
                     " ORDER BY u.id", (language_id,)).fetchall()


def propose_pairs(c, language_id, n):
    if language_id == "all":
        out = []
        for l in c.execute("SELECT id FROM languages").fetchall():
            out.extend(propose_pairs(c, l["id"], n))
        return out
    pool = list(unpaired(c, language_id))
    hist = {(r["a"], r["b"]) for r in c.execute("SELECT a,b FROM pairs WHERE language_id=?", (language_id,))}
    hist |= {(b, a) for a, b in hist}
    av = {u["id"]: set(json.loads(u["availability"] or "[]")) for u in pool}
    pool.sort(key=lambda u: len(av[u["id"]]))
    used, out = set(), []
    for u in pool:
        if u["id"] in used or len(out) >= n:
            continue
        best = None
        for v in pool:
            if v["id"] == u["id"] or v["id"] in used or (u["id"], v["id"]) in hist:
                continue
            ov = len(av[u["id"]] & av[v["id"]])
            if best is None or ov > best[0]:
                best = (ov, v)
        if best:
            used |= {u["id"], best[1]["id"]}
            out.append({"a": u, "b": best[1], "overlap": best[0]})
    return out


def create_pair(c, actor, a, b):
    ua = c.execute("SELECT * FROM users WHERE id=?", (a,)).fetchone()
    ub = c.execute("SELECT * FROM users WHERE id=?", (b,)).fetchone()
    if not ua or not ub or a == b or ua["role"] != "participant" or ub["role"] != "participant":
        raise OpsError("pick two different participants")
    if ua["language_id"] != ub["language_id"]:
        raise OpsError("different languages")
    need_manage(actor, ua["language_id"])
    for x in (a, b):
        if c.execute("SELECT 1 FROM pairs WHERE status='ACTIVE' AND (a=? OR b=?)", (x, x)).fetchone():
            raise OpsError("someone is already in an active pair")
    cur = c.execute("INSERT INTO pairs(language_id,a,b,created_at) VALUES(?,?,?,?)", (ua["language_id"], a, b, time.time()))
    db.audit(c, actor, "create_pair", f"{a}+{b}")
    return cur.lastrowid


def drop_pair(c, actor, pair_id, reason=""):
    p = c.execute("SELECT * FROM pairs WHERE id=?", (pair_id,)).fetchone()
    need_manage(actor, p["language_id"])
    c.execute("UPDATE pairs SET status='DROPPED', dropped_at=?, note=? WHERE id=?", (time.time(), reason, pair_id))
    n = c.execute("UPDATE assignments SET status='RELEASED', note='pair dropped', updated_at=? WHERE pair_id=? AND status='ASSIGNED'",
                  (time.time(), pair_id)).rowcount
    c.execute("UPDATE assignments SET status='ABANDONED', note='pair dropped', updated_at=? WHERE pair_id=? AND status='IN_SESSION'",
              (time.time(), pair_id))
    db.audit(c, actor, "drop_pair", f"pair={pair_id} released={n} {reason}")


def pair_members(c, pair):
    return [c.execute("SELECT * FROM users WHERE id=?", (pair[k],)).fetchone() for k in ("a", "b")]


# ---------------------------------------------------------------- hours
def person_used(c, uid):
    u = c.execute("SELECT opening_min FROM users WHERE id=?", (uid,)).fetchone()
    r = c.execute("SELECT COALESCE(SUM(CASE WHEN status='CONFIRMED' THEN conf_min ELSE 0 END),0) x FROM assignments WHERE ua=? OR ub=?", (uid, uid)).fetchone()
    return (u["opening_min"] or 0) + r["x"]


def pair_used(c, pid):
    p = c.execute("SELECT opening_min FROM pairs WHERE id=?", (pid,)).fetchone()
    r = c.execute("SELECT COALESCE(SUM(CASE WHEN status='CONFIRMED' THEN conf_min ELSE 0 END),0) x FROM assignments WHERE pair_id=?", (pid,)).fetchone()
    return (p["opening_min"] or 0) + r["x"]


# ---------------------------------------------------------------- assignment
def gender_ok(sg, ug):
    return not sg or ug not in ("M", "F") or sg == ug


def role_map(attrs, u1, u2, last_ua):
    cands = [(u1, u2), (u2, u1)]
    if last_ua == u1["id"]:
        cands.reverse()
    for ua, ub in cands:
        if gender_ok(attrs["genders"]["A"], ua["gender"]) and gender_ok(attrs["genders"]["B"], ub["gender"]):
            return ua, ub
    return None


def assign(c, actor, pair_id, n, only=None, override=False, reason=""):
    """Atomic: runs inside the caller's write transaction. Returns (list of assignment ids, note)."""
    p = c.execute("SELECT * FROM pairs WHERE id=?", (pair_id,)).fetchone()
    if not p or p["status"] != "ACTIVE":
        raise OpsError("pair is not active")
    if actor is not None:
        need_manage(actor, p["language_id"])
        if override and actor["role"] != "admin":
            raise PermissionError("only an admin can override caps")
        if override and not reason.strip():
            raise OpsError("override needs a reason")
    S = db.S(c)
    L = c.execute("SELECT * FROM languages WHERE id=?", (p["language_id"],)).fetchone()
    if db.setting(c, f"pause_assign_{L['id']}") == "1":
        raise OpsError("assignment is paused for this language")
    u1, u2 = pair_members(c, p)
    for u in (u1, u2):
        if not u["active"]:
            raise OpsError(f"{u['name']} is switched off")
        # if not u["consent_at"]:
        #     raise OpsError(f"{u['name']} has not accepted consent yet")
    open_n = c.execute("SELECT COUNT(*) n FROM assignments WHERE pair_id=? AND status IN ('ASSIGNED','IN_SESSION')", (pair_id,)).fetchone()["n"]
    room = int(S["open_limit"]) - open_n
    if room <= 0:
        raise OpsError(f"pair already has {open_n} open scripts (limit {S['open_limit']})")
    n = min(n, room)
    est = float(S["avg_minutes"])
    made, note = [], ""
    allow_test = 1 if S["allow_test_scripts"] == "1" else 0
    for _ in range(n):
        if not override:
            if pair_used(c, pair_id) + est > L["cap_pair_min"]:
                note = "pair hours cap reached"
                break
            if person_used(c, u1["id"]) + est > L["cap_person_min"] or person_used(c, u2["id"]) + est > L["cap_person_min"]:
                note = "a person's hours cap reached"
                break
        pick = pick_script(c, L, p, u1, u2, allow_test, only)
        if not pick:
            note = "no eligible approved script left"
            break
        s, ua, ub = pick
        now = time.time()
        try:
            cur = c.execute("INSERT INTO assignments(script_id,version,pair_id,ua,ub,status,est_min,assigned_at,deadline,updated_at)"
                            " VALUES(?,?,?,?,?,'ASSIGNED',?,?,?,?)",
                            (s["id"], s["version"], pair_id, ua["id"], ub["id"], est, now,
                             now + float(S["deadline_hours"]) * 3600, now))
        except Exception as e:  # unique index = last line of defence
            raise OpsError("script already taken: " + str(e))
        made.append(cur.lastrowid)
    if made:
        db.audit(c, actor if actor else "system", "assign", f"pair={pair_id} n={len(made)}" + (f" OVERRIDE {reason}" if override else ""))
    return made, note


def pick_script(c, L, pair, u1, u2, allow_test, only):
    rows = c.execute(
        "SELECT s.* FROM scripts s WHERE s.language_id=? AND s.status IN ('APPROVED','READY') AND (s.is_test=0 OR ?=1) AND NOT EXISTS"
        " (SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED')) ORDER BY s.seq",
        (L["id"], allow_test)).fetchall()
    if only:
        rows = [r for r in rows if (r["subdomain"], r["specialisation"]) == only]
    if not rows:
        return None
    # For imported scripts without attrs, just pick round-robin by least-used subdomain
    last = c.execute("SELECT a.ua FROM assignments a JOIN pairs p ON p.id=a.pair_id WHERE a.pair_id=? ORDER BY a.id DESC LIMIT 1",
                     (pair["id"],)).fetchone()
    last_ua = last["ua"] if last else None
    # Simple fair distribution: group by subdomain, pick from least-assigned group
    tot, used_count = {}, {}
    for r in c.execute("SELECT subdomain, specialisation, COUNT(*) n, SUM(EXISTS(SELECT 1 FROM assignments a WHERE a.script_id=scripts.id"
                       " AND a.status NOT IN ('RELEASED','ABANDONED'))) u FROM scripts WHERE language_id=? GROUP BY 1,2", (L["id"],)):
        tot[(r["subdomain"], r["specialisation"])] = r["n"]
        used_count[(r["subdomain"], r["specialisation"])] = r["u"] or 0
    groups = {}
    for r in rows:
        groups.setdefault((r["subdomain"], r["specialisation"]), []).append(r)
    for g in sorted(groups, key=lambda g: (used_count.get(g, 0) / max(1, tot.get(g, 1)), g)):
        cands = groups[g]
        for r in cands:
            attrs = None
            if r["attrs"]:
                try:
                    attrs = json.loads(r["attrs"])
                except Exception:
                    pass
            if attrs and "genders" in attrs:
                m = role_map(attrs, u1, u2, last_ua)
                if m:
                    return r, m[0], m[1]
            else:
                # No attrs (imported script) — assign alternating speaker roles
                if last_ua == u1["id"]:
                    return r, u2, u1
                else:
                    return r, u1, u2
    return None


def release_assignment(c, actor, aid, why="released by lead"):
    a = c.execute("SELECT a.*, p.language_id FROM assignments a JOIN pairs p ON p.id=a.pair_id WHERE a.id=?", (aid,)).fetchone()
    need_manage(actor, a["language_id"])
    if a["status"] not in ("ASSIGNED", "IN_SESSION", "DISPUTED", "SELF_REPORTED"):
        raise OpsError("cannot release in status " + a["status"])
    c.execute("UPDATE assignments SET status='RELEASED', note=?, updated_at=? WHERE id=?", (why, time.time(), aid))
    db.audit(c, actor, "release_assignment", f"{aid} {why}")


# ---------------------------------------------------------------- session flow
def _mine(c, uid, aid):
    a = c.execute("SELECT a.*, s.code, s.attrs, s.subdomain, s.specialisation, s.language_id lang FROM assignments a"
                  " JOIN scripts s ON s.id=a.script_id WHERE a.id=?", (aid,)).fetchone()
    if not a or uid not in (a["ua"], a["ub"]):
        raise PermissionError("not your assignment")
    return a, ("a" if a["ua"] == uid else "b")


def heartbeat(c, uid, aid):
    a, w = _mine(c, uid, aid)
    c.execute(f"UPDATE assignments SET ready_{w}=? WHERE id=?", (time.time(), aid))


def unready(c, uid, aid):
    a, w = _mine(c, uid, aid)
    c.execute(f"UPDATE assignments SET ready_{w}=0 WHERE id=?", (aid,))


def start(c, uid, aid):
    a, w = _mine(c, uid, aid)
    if a["status"] != "ASSIGNED":
        raise OpsError("already started or closed")
    now = time.time()
    if now - a["ready_a"] > 60 or now - a["ready_b"] > 60:
        raise OpsError("Both partners must show Ready first.")
    c.execute("UPDATE assignments SET status='IN_SESSION', started_at=?, updated_at=? WHERE id=?", (now, now, aid))
    db.audit(c, f"participant:{uid}", "start", a["code"])


def confirm(c, actor, aid, minutes, verified, note=""):
    a = c.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
    if a["status"] == "CONFIRMED":
        return False
    now = time.time()
    c.execute("UPDATE assignments SET status='CONFIRMED', conf_min=?, verified=?, note=?, updated_at=? WHERE id=?",
              (round(minutes, 1), 1 if verified else 0, note, now, aid))
    for u in (a["ua"], a["ub"]):
        c.execute("INSERT INTO ledger(ts,assignment_id,user_id,pair_id,minutes,kind) VALUES(?,?,?,?,?,?)",
                  (now, aid, u, a["pair_id"], round(minutes, 1), "confirmed"))
    db.audit(c, actor, "confirm", f"assignment={aid} min={minutes:.1f} verified={verified}")
    return True


def _issue(c, aid, uid, lang, kind, note, route):
    c.execute("INSERT INTO issues(assignment_id,user_id,language_id,kind,note,route,created) VALUES(?,?,?,?,?,?,?)",
              (aid, uid, lang, kind, note[:500], route, time.time()))


def done(c, uid, aid, client_session=""):
    a, w = _mine(c, uid, aid)
    if a["status"] not in ("IN_SESSION", "SELF_REPORTED"):
        raise OpsError("start the session first")
    now = time.time()
    c.execute(f"UPDATE assignments SET done_{w}=?, client_session=COALESCE(NULLIF(?, ''), client_session), updated_at=? WHERE id=?",
              (now, client_session.strip(), now, aid))
    a = c.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
    if a["done_a"] and a["done_b"]:
        el = max(10.0, max(a["done_a"], a["done_b"]) - (a["started_at"] or (now - 60)))
        confirm(c, "system", aid, round(el / 60, 1), True, "Completed by both agents")


def a_lang(c, aid):
    return c.execute("SELECT p.language_id l FROM assignments a JOIN pairs p ON p.id=a.pair_id WHERE a.id=?", (aid,)).fetchone()["l"]


def problem(c, uid, aid, note):
    a, w = _mine(c, uid, aid)
    if a["status"] in ("CONFIRMED", "RELEASED", "ABANDONED"):
        raise OpsError("closed")
    c.execute("UPDATE assignments SET status='DISPUTED', note=?, updated_at=? WHERE id=?", ("problem: " + note[:200], time.time(), aid))
    _issue(c, aid, uid, a_lang(c, aid), "problem", note, "lead")
    db.audit(c, f"participant:{uid}", "problem", a["code"])


ROUTES = {"partner_no_show": "lead", "audio_problem": "lead", "script_error": "admin", "tech_failure": "admin", "other": "lead"}


def report_issue(c, uid, aid, kind, note):
    a, w = _mine(c, uid, aid)
    kind = kind if kind in ROUTES else "other"
    lang = a_lang(c, aid)
    _issue(c, aid, uid, lang, kind, note, ROUTES[kind])
    msg = "Reported to your " + ("team lead." if ROUTES[kind] == "lead" else "admin.")
    if kind == "script_error" and a["status"] in ("ASSIGNED", "IN_SESSION"):
        c.execute("UPDATE assignments SET status=?, note='script reported faulty', updated_at=? WHERE id=?",
                  ("RELEASED" if a["status"] == "ASSIGNED" else "ABANDONED", time.time(), aid))
        c.execute("UPDATE scripts SET status='IN_REVIEW', reject_reason='reported faulty by participant', needs_review=1 WHERE id=?",
                  (a["script_id"],))
        made, why = assign(c, None, a["pair_id"], 1, only=(a["subdomain"], a["specialisation"]))
        msg += " A replacement script was assigned." if made else " No replacement yet: " + why
    db.audit(c, f"participant:{uid}", "issue_" + kind, a["code"])
    return msg


def resolve_issue(c, actor, issue_id):
    i = c.execute("SELECT * FROM issues WHERE id=?", (issue_id,)).fetchone()
    if not i:
        raise OpsError("issue not found")
    need_manage(actor, i["language_id"])
    c.execute("UPDATE issues SET status='RESOLVED' WHERE id=?", (issue_id,))
    db.audit(c, actor, "resolve_issue", str(issue_id))


def lead_confirm(c, actor, aid, minutes):
    a = c.execute("SELECT a.*, p.language_id l FROM assignments a JOIN pairs p ON p.id=a.pair_id WHERE a.id=?", (aid,)).fetchone()
    need_manage(actor, a["l"])
    if a["status"] not in ("DISPUTED", "SELF_REPORTED", "IN_SESSION"):
        raise OpsError("not confirmable")
    confirm(c, actor, aid, float(minutes), False, "UNVERIFIED lead-accepted")


def qc_decide(c, actor, aid, decision):
    a = c.execute("SELECT a.*, p.language_id l FROM assignments a JOIN pairs p ON p.id=a.pair_id WHERE a.id=?", (aid,)).fetchone()
    need_manage(actor, a["l"])
    if a["status"] != "QC_FAIL":
        raise OpsError("not a QC fail")
    if decision == "redo":
        c.execute("UPDATE assignments SET status='ASSIGNED', redo=redo+1, started_at=NULL, done_a=NULL, done_b=NULL, ready_a=0, ready_b=0,"
                  " conf_min=NULL, deadline=?, updated_at=? WHERE id=?", (time.time() + 72 * 3600, time.time(), aid))
    else:
        c.execute("UPDATE assignments SET status='RELEASED', note='QC fail released', updated_at=? WHERE id=?", (time.time(), aid))
    db.audit(c, actor, "qc_" + decision, str(aid))


# ---------------------------------------------------------------- reconcile / QC import
def _col(row, names):
    for n in names:
        if n in row and str(row[n]).strip():
            return str(row[n]).strip()
    return ""


def _secs(v):
    v = str(v).strip()
    if not v:
        return None
    try:
        if ":" in v:
            parts = [float(x) for x in v.split(":")]
            s = 0
            for x in parts:
                s = s * 60 + x
            return s
        return float(v)
    except ValueError:
        return None


def _ts(v):
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except Exception:
        try:
            return float(v)
        except Exception:
            return None


def reconcile_csv(c, actor, text):
    S = db.S(c)
    lo, hi = float(S["dur_min_sec"]), float(S["dur_max_sec"])
    res = dict(confirmed=0, duplicate=0, out_of_spec=0, orphan=0)
    for row in csv.DictReader(io.StringIO(text.strip())):
        row = {(k or "").strip().lower(): v for k, v in row.items()}
        sid = _col(row, ["script_id", "task_id", "script", "script_code"]).upper()
        ses = _col(row, ["session_id", "id", "recording_id"])
        dur = _secs(_col(row, ["duration_sec", "duration_seconds", "duration", "seconds"]))
        a = None
        if sid:
            a = c.execute("SELECT a.* FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE s.code=? AND a.status NOT IN"
                          " ('RELEASED','ABANDONED')", (sid,)).fetchone()
        if not a:
            who = _col(row, ["user", "participant", "phone", "email", "access_code"])
            t = _ts(_col(row, ["start", "started_at", "timestamp", "start_time"]))
            u = None
            if who:
                u = c.execute("SELECT * FROM users WHERE phone=? OR email=? OR code=?", (norm_phone(who), norm_email(who), codes.norm(who))).fetchone()
            if u and t and dur:
                for cand in c.execute("SELECT * FROM assignments WHERE (ua=? OR ub=?) AND status IN ('IN_SESSION','SELF_REPORTED','ASSIGNED')", (u["id"], u["id"])):
                    if cand["started_at"] and abs(cand["started_at"] - t) <= 1800:
                        a = cand
                        break
        if not a or dur is None:
            c.execute("INSERT INTO orphans(ts,payload) VALUES(?,?)", (time.time(), json.dumps(row, ensure_ascii=False)))
            res["orphan"] += 1
            continue
        if a["status"] == "CONFIRMED":
            res["duplicate"] += 1
            continue
        if a["status"] == "QC_FAIL":
            res["duplicate"] += 1
            continue
        c.execute("UPDATE assignments SET client_session=? WHERE id=?", (ses, a["id"]))
        if dur < lo or dur > hi:
            c.execute("UPDATE assignments SET status='DISPUTED', note=? WHERE id=?", (f"client duration {dur:.0f}s outside spec", a["id"]))
            _issue(c, a["id"], None, a_lang(c, a["id"]), "duration_out_of_range", f"{dur:.0f}s", "lead")
            res["out_of_spec"] += 1
        else:
            confirm(c, actor, a["id"], dur / 60, True, "client log")
            res["confirmed"] += 1
    db.audit(c, actor, "reconcile", json.dumps(res))
    return res


def qc_csv(c, actor, text):
    res = dict(passed=0, failed=0, unknown=0)
    for row in csv.DictReader(io.StringIO(text.strip())):
        row = {(k or "").strip().lower(): v for k, v in row.items()}
        sid = _col(row, ["script_id", "task_id", "script"]).upper()
        r = _col(row, ["result", "qc", "status"]).upper()
        a = c.execute("SELECT a.* FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE s.code=? AND a.status='CONFIRMED'", (sid,)).fetchone()
        if not a:
            res["unknown"] += 1
            continue
        if r.startswith("F") or r == "REJECT":
            now = time.time()
            c.execute("UPDATE assignments SET status='QC_FAIL', note=?, updated_at=? WHERE id=?", ("QC: " + _col(row, ["reason", "note"]), now, a["id"]))
            for u in (a["ua"], a["ub"]):
                c.execute("INSERT INTO ledger(ts,assignment_id,user_id,pair_id,minutes,kind) VALUES(?,?,?,?,?,?)",
                          (now, a["id"], u, a["pair_id"], -(a["conf_min"] or 0), "qc_reversal"))
            _issue(c, a["id"], None, a_lang(c, a["id"]), "qc_fail", _col(row, ["reason", "note"]), "lead")
            fails = c.execute("SELECT COUNT(*) n FROM assignments WHERE script_id=? AND status='QC_FAIL'", (a["script_id"],)).fetchone()["n"]
            if fails >= 2:
                c.execute("UPDATE scripts SET status='IN_REVIEW', needs_review=1, reject_reason='failed client QC repeatedly' WHERE id=?", (a["script_id"],))
            res["failed"] += 1
        else:
            res["passed"] += 1
    db.audit(c, actor, "qc_import", json.dumps(res))
    return res


# ---------------------------------------------------------------- watchdogs
def sweep(c, now=None):
    now = now or time.time()
    S = db.S(c)
    out = dict(nudged=0, auto_released=0, abandoned=0, disputed=0)
    for a in c.execute("SELECT * FROM assignments WHERE status='ASSIGNED'").fetchall():
        if a["deadline"] < now:
            c.execute("UPDATE assignments SET status='RELEASED', note='auto-released at deadline', updated_at=? WHERE id=?", (now, a["id"]))
            out["auto_released"] += 1
        elif not a["nudged"] and now > a["assigned_at"] + (a["deadline"] - a["assigned_at"]) / 2:
            c.execute("UPDATE assignments SET nudged=1 WHERE id=?", (a["id"],))
            out["nudged"] += 1
    for a in c.execute("SELECT * FROM assignments WHERE status='IN_SESSION' AND started_at<?", (now - float(S["abandon_hours"]) * 3600,)).fetchall():
        c.execute("UPDATE assignments SET status='ABANDONED', note='no Done after limit', updated_at=? WHERE id=?", (now, a["id"]))
        out["abandoned"] += 1
    if S["verification_mode"] == "client_log":
        for a in c.execute("SELECT * FROM assignments WHERE status='SELF_REPORTED' AND updated_at<?", (now - 48 * 3600,)).fetchall():
            c.execute("UPDATE assignments SET status='DISPUTED', note='no client match after 48h', updated_at=? WHERE id=?", (now, a["id"]))
            _issue(c, a["id"], None, a_lang(c, a["id"]), "unmatched_self_report", "no client session found", "lead")
            out["disputed"] += 1
    if any(out.values()):
        db.audit(c, "system", "sweep", json.dumps(out))
    return out


def alerts(c):
    S = db.S(c)
    out = []
    spent = c.execute("SELECT COALESCE(SUM(cost),0) x FROM jobs").fetchone()["x"]
    cap = float(S["global_cap_inr"])
    if spent >= 0.8 * cap:
        out.append(("bad" if spent >= cap else "warn", f"Generation spend Rs {spent:,.0f} of Rs {cap:,.0f} cap"))
    if S["pause_generation"] == "1":
        out.append(("bad", "Generation is PAUSED (stop-the-line or manual)."))
    for k in c.execute("SELECT * FROM api_keys WHERE status!='active' OR cool_until>?", (time.time(),)):
        out.append(("warn", f"Key '{k['label']}' is {k['status'] if k['status'] != 'active' else 'cooling down'}"))
    n = c.execute("SELECT COUNT(*) n FROM scripts WHERE status='NEEDS_HUMAN'").fetchone()["n"]
    if n:
        out.append(("warn", f"{n} scripts failed auto-repair (NEEDS_HUMAN)"))
    n = c.execute("SELECT COUNT(*) n FROM scripts WHERE status='IN_REVIEW'").fetchone()["n"]
    if n > 50:
        out.append(("warn", f"Review backlog: {n} scripts"))
    for L in c.execute("SELECT * FROM languages"):
        stock = c.execute("SELECT COUNT(*) n FROM scripts s WHERE language_id=? AND status IN ('APPROVED','READY') AND NOT EXISTS (SELECT 1 FROM assignments a"
                          " WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED'))", (L["id"],)).fetchone()["n"]
        rate = c.execute("SELECT COUNT(*) n FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE s.language_id=? AND a.assigned_at>?",
                         (L["id"], time.time() - 7 * 86400)).fetchone()["n"] / 7
        if L["planned"] and rate > 0 and stock / rate < 3:
            out.append(("warn", f"{L['name']}: only {stock / rate:.1f} days of approved stock. Release the next wave."))
        for p in c.execute("SELECT * FROM pairs WHERE language_id=? AND status='ACTIVE'", (L["id"],)):
            used = pair_used(c, p["id"])
            if used >= 0.8 * L["cap_pair_min"]:
                out.append(("warn", f"{L['name']}: pair #{p['id']} at {100 * used / L['cap_pair_min']:.0f}% of hours cap"))
            u1, u2 = pair_members(c, p)
            if u1["device"] and u1["device"] == u2["device"]:
                out.append(("bad", f"{L['name']}: pair #{p['id']} both partners use the same device"))
    n = c.execute("SELECT COUNT(*) n FROM orphans WHERE status='OPEN'").fetchone()["n"]
    if n:
        out.append(("warn", f"{n} client sessions with no assignment (orphans)"))
    n = c.execute("SELECT COUNT(*) n FROM issues WHERE status='OPEN' AND route='admin'").fetchone()["n"]
    if n:
        out.append(("warn", f"{n} open admin issues"))
    return out


# ---------------------------------------------------------------- stats
def funnel(c, lang_id):
    d = {}
    for r in c.execute("SELECT status, COUNT(*) n FROM scripts WHERE language_id=? GROUP BY status", (lang_id,)):
        d[r["status"]] = r["n"]
    a = {}
    for r in c.execute("SELECT a.status, COUNT(*) n FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE s.language_id=? GROUP BY a.status", (lang_id,)):
        a[r["status"]] = r["n"]
    return d, a


def coverage(c, lang_id):
    rows = []
    for r in c.execute(
            "SELECT s.subdomain, s.specialisation, COUNT(*) planned,"
            " SUM(s.status='APPROVED' OR s.status='IN_REVIEW') made,"
            " SUM(EXISTS(SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED'))) assigned,"
            " SUM(EXISTS(SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status='CONFIRMED')) confirmed"
            " FROM scripts s WHERE s.language_id=? GROUP BY 1,2 ORDER BY MIN(s.seq)", (lang_id,)):
        rows.append(dict(r))
    return rows


def lead_exceptions(c, lang_id, now=None):
    now = now or time.time()
    ex = []
    for a in c.execute("SELECT a.*, s.code FROM assignments a JOIN pairs p ON p.id=a.pair_id JOIN scripts s ON s.id=a.script_id"
                       " WHERE p.language_id=? AND a.status IN ('ASSIGNED','IN_SESSION','DISPUTED','SELF_REPORTED','QC_FAIL')", (lang_id,)):
        if a["status"] == "ASSIGNED" and a["nudged"]:
            ex.append(("stalled", a, "Not started, past half the deadline"))
        elif a["status"] == "IN_SESSION" and now - a["started_at"] > 3600:
            ex.append(("stalled", a, "In session over 1 hour"))
        elif a["status"] in ("DISPUTED", "SELF_REPORTED"):
            ex.append(("dispute", a, a["note"] or "Needs a decision"))
        elif a["status"] == "QC_FAIL":
            ex.append(("qc", a, a["note"] or "Client QC failed: choose redo or release"))
    return ex


# ---------------------------------------------------------------- exports
def mask(v):
    v = v or ""
    return v[:2] + "*" * max(0, len(v) - 4) + v[-2:] if len(v) > 4 else "*" * len(v)


REPORTS = ["scripts", "people", "pairs", "assignments", "ledger", "generation_cost", "audit", "client_manifest", "payout"]


def export(c, actor, name, lang_id=None, masked=True):
    if name not in REPORTS:
        raise OpsError("unknown report")
    if actor["role"] == "lead":
        lang_id = actor["language_id"]
    elif actor["role"] != "admin":
        raise PermissionError("no")
    lf, args = ("WHERE s.language_id=?", (lang_id,)) if lang_id else ("", ())
    if name == "scripts":
        h = ["script_id", "domain", "subdomain", "specialisation", "wave", "status", "version", "words", "source", "is_test", "prompt_version"]
        rows = [[r[k] for k in h[:1]] + [r[k] for k in h[1:]] for r in c.execute(
            "SELECT s.code script_id, s.domain, s.subdomain, s.specialisation, s.wave, s.status, s.version, s.words, s.source, s.is_test,"
            " s.prompt_version FROM scripts s " + lf + " ORDER BY s.seq", args)]
    elif name == "people":
        h = ["id", "name", "role", "language", "phone", "email", "gender", "active", "consent", "opened_minutes"]
        rows = [[r["id"], r["name"], r["role"], r["lang"], mask(r["phone"]) if masked else r["phone"], mask(r["email"]) if masked else r["email"],
                 r["gender"], r["active"], "yes" if r["consent_at"] else "no", r["opening_min"]] for r in c.execute(
            "SELECT u.*, l.name lang FROM users u LEFT JOIN languages l ON l.id=u.language_id " +
            ("WHERE u.language_id=?" if lang_id else "") + " ORDER BY u.id", (lang_id,) if lang_id else ())]
    elif name == "pairs":
        h = ["pair_id", "language", "a", "b", "status", "created", "hours_used"]
        rows = [[r["id"], r["lang"], r["an"], r["bn"], r["status"], r["created_at"], round(pair_used(c, r["id"]) / 60, 2)] for r in c.execute(
            "SELECT p.*, l.name lang, ua.name an, ub.name bn FROM pairs p JOIN languages l ON l.id=p.language_id JOIN users ua ON ua.id=p.a"
            " JOIN users ub ON ub.id=p.b " + ("WHERE p.language_id=?" if lang_id else ""), (lang_id,) if lang_id else ())]
    elif name in ("assignments", "client_manifest"):
        h = ["script_id", "subdomain", "specialisation", "pair", "speaker_a", "speaker_b", "status", "verified", "minutes", "client_session", "redo"]
        extra = " AND a.status='CONFIRMED'" if name == "client_manifest" else ""
        rows = [[r["code"], r["subdomain"], r["specialisation"], r["pair_id"], r["an"], r["bn"], r["status"], "VERIFIED" if r["verified"] else "UNVERIFIED",
                 r["conf_min"], r["client_session"], r["redo"]] for r in c.execute(
            "SELECT s.code, s.subdomain, s.specialisation, a.*, ua.name an, ub.name bn FROM assignments a JOIN scripts s ON s.id=a.script_id"
            " JOIN users ua ON ua.id=a.ua JOIN users ub ON ub.id=a.ub WHERE 1=1 " + extra + (" AND s.language_id=?" if lang_id else ""),
            (lang_id,) if lang_id else ())]
    elif name == "ledger":
        h = ["ts", "assignment", "user", "pair", "minutes", "kind"]
        rows = [[datetime.fromtimestamp(r["ts"]).isoformat(timespec="seconds"), r["assignment_id"], r["user_id"], r["pair_id"], r["minutes"], r["kind"]]
                for r in c.execute("SELECT * FROM ledger ORDER BY id")]
    elif name == "generation_cost":
        h = ["script_id", "status", "attempts", "calls", "cost_inr", "error"]
        rows = [[r["code"], r["status"], r["attempts"], r["calls"], round(r["cost"], 2), r["error"]] for r in c.execute(
            "SELECT s.code, j.* FROM jobs j JOIN scripts s ON s.id=j.script_id " + lf, args)]
    elif name == "audit":
        _admin_only(actor)
        h = ["ts", "actor", "action", "detail"]
        rows = [[datetime.fromtimestamp(r["ts"]).isoformat(timespec="seconds"), r["actor"], r["action"], r["detail"]]
                for r in c.execute("SELECT * FROM audit ORDER BY id")]
    else:  # payout: confirmed minutes only, from the ledger
        rate = float(db.setting(c, "rate_per_hour_inr"))
        h = ["person", "phone", "minutes_confirmed", "hours", "rate_inr_per_hour", "amount_inr"]
        rows = []
        for r in c.execute("SELECT u.name, u.phone, SUM(l.minutes) m FROM ledger l JOIN users u ON u.id=l.user_id " +
                           ("WHERE u.language_id=? " if lang_id else "") + "GROUP BY u.id ORDER BY u.name", (lang_id,) if lang_id else ()):
            m = r["m"] or 0
            rows.append([r["name"], mask(r["phone"]) if masked else r["phone"], round(m, 1), round(m / 60, 2), rate, round(m / 60 * rate, 2)])
    db.audit(c, actor, "export", f"{name} lang={lang_id} masked={masked}")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(h)
    w.writerows(rows)
    return buf.getvalue()


# ---------------------------------------------------------------- import ready-made scripts
def import_preview(c, actor, lang_id, files):
    """files: list of (filename, bytes). CSV(subdomain,specialisation,text) or ZIP(txt/md + index.csv)."""
    need_manage(actor, lang_id)
    L = c.execute("SELECT * FROM languages WHERE id=?", (lang_id,)).fetchone()
    S = db.S(c)
    items = []
    for fn, data in files:
        if fn.lower().endswith(".zip"):
            z = zipfile.ZipFile(io.BytesIO(data))
            idx = {}
            for n in z.namelist():
                if n.lower().endswith("index.csv"):
                    for r in csv.DictReader(io.StringIO(z.read(n).decode("utf-8-sig"))):
                        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
                        idx[r.get("file", "")] = r
            for n in z.namelist():
                if n.lower().endswith((".txt", ".md")):
                    m = idx.get(n) or idx.get(n.split("/")[-1]) or {}
                    items.append((n, m.get("subdomain", ""), m.get("specialisation", ""), z.read(n).decode("utf-8-sig")))
        else:
            for i, r in enumerate(csv.DictReader(io.StringIO(data.decode("utf-8-sig"))), 1):
                r = {(k or "").strip().lower(): (v or "") for k, v in r.items()}
                items.append((f"{fn}#{i}", r.get("subdomain", "").strip(), r.get("specialisation", "").strip(), r.get("text", "")))
    out, taken, batch_sh = [], set(), []
    for label, sub, spec, text in items:
        it = dict(label=label, subdomain=sub, specialisation=spec, status="OK", notes=[])
        slot = None
        for r in c.execute("SELECT * FROM scripts WHERE language_id=? AND subdomain=? AND specialisation=? AND status='PLANNED' ORDER BY seq",
                           (lang_id, sub, spec if sub == "Doctor-Patient Consultation" else "")):
            if r["id"] not in taken:
                slot = r
                break
        if not slot:
            it["status"], it["notes"] = "REJECT", ["no free slot in this sub-domain/specialisation (quota full or name wrong)"]
            out.append(it)
            continue
        attrs = json.loads(slot["attrs"])
        turns, err = P.lines_to_turns(text, attrs["roles"])
        if err:
            it["status"], it["notes"] = "REJECT", [err]
            out.append(it)
            continue
        errs = [e for e in V.validate(turns, attrs, L, S) if not e.startswith("numbers_not_allowed")]
        errs += V.similarity_check(c, lang_id, slot["id"], turns, float(S["sim_threshold"]))
        sh = V.shingle_set(turns)
        for o in batch_sh:
            if sh and o and len(sh & o) / min(len(sh), len(o)) > float(S["sim_threshold"]):
                errs.append("near_copy_in_batch")
                break
        if errs:
            it["status"], it["notes"] = "REJECT", errs
        else:
            taken.add(slot["id"])
            batch_sh.append(sh)
            it.update(slot_id=slot["id"], slot_code=slot["code"], turns=turns, words=V.count_words(turns))
        out.append(it)
    bid = c.execute("INSERT INTO import_batches(language_id,payload,created) VALUES(?,?,?)",
                    (lang_id, json.dumps(out, ensure_ascii=False), time.time())).lastrowid
    return bid, out


def import_commit(c, actor, batch_id):
    b = c.execute("SELECT * FROM import_batches WHERE id=?", (batch_id,)).fetchone()
    need_manage(actor, b["language_id"])
    S = db.S(c)
    items = json.loads(b["payload"])
    done_n, skipped = 0, 0
    now = time.time()
    for it in items:
        if it["status"] != "OK":
            continue
        s = c.execute("SELECT * FROM scripts WHERE id=?", (it["slot_id"],)).fetchone()
        if s["status"] != "PLANNED":
            skipped += 1
            continue
        import random as _r
        nr = 1 if _r.Random(s["id"] * 13).random() < float(S["review_pct"]) / 100 else 0
        ver = 1
        c.execute("INSERT INTO script_versions VALUES(?,?,?,?,?,?,?)",
                  (s["id"], ver, json.dumps(it["turns"], ensure_ascii=False), it["words"], "import", "imported", now))
        c.execute("UPDATE scripts SET status=?, version=1, words=?, shingles=?, opening=?, source='import', prompt_version='import',"
                  " spec_version=?, needs_review=?, is_test=0, updated_at=? WHERE id=?",
                  ("IN_REVIEW" if nr else "APPROVED", it["words"], V.pack(V.shingle_set(it["turns"])), V.opening_key(it["turns"]),
                   int(S["spec_version"]), nr, now, s["id"]))
        if not nr:
            P.backup_script(c, s["id"])
        done_n += 1
    c.execute("DELETE FROM import_batches WHERE id=?", (batch_id,))
    db.audit(c, actor, "import_commit", f"batch={batch_id} imported={done_n} skipped={skipped}")
    return done_n, skipped


def revalidate(c, lang_id):
    """Spec change impact report: every script that no longer passes."""
    L = c.execute("SELECT * FROM languages WHERE id=?", (lang_id,)).fetchone()
    S = db.S(c)
    bad = []
    for s in c.execute("SELECT * FROM scripts WHERE language_id=? AND version>0 AND status NOT IN ('QUARANTINED')", (lang_id,)):
        v = c.execute("SELECT turns FROM script_versions WHERE script_id=? AND version=?", (s["id"], s["version"])).fetchone()
        errs = [e for e in V.validate(json.loads(v["turns"]), json.loads(s["attrs"]), L, S)
                if not (s["source"] == "import" and e.startswith("numbers_not_allowed"))]
        if errs:
            bad.append((s["code"], s["status"], "; ".join(errs)))
    return bad


def drop_all_pairs(c, actor, language_id):
    if language_id == "all":
        if actor is not None and actor["role"] != "admin":
            raise PermissionError("admin only")
        pairs = c.execute("SELECT id FROM pairs WHERE status='ACTIVE'").fetchall()
    else:
        need_manage(actor, language_id)
        pairs = c.execute("SELECT id FROM pairs WHERE language_id=? AND status='ACTIVE'", (language_id,)).fetchall()
    for p in pairs:
        drop_pair(c, actor, p["id"], "bulk dropped")
    return len(pairs)
