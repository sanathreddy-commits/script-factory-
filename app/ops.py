"""People, pairing, assignment, session flow, verification, watchdogs, import/export."""
import csv
import io
import os
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


def user_lang_ids(c, u):
    """Languages a user may work in. Supervisors can own several (supervisor_languages) on top of users.language_id."""
    role = u["role"]
    if role in ("admin", "developer"):
        return [r["id"] for r in c.execute("SELECT id FROM languages ORDER BY id")]
    ids = []
    if u["language_id"]:
        ids.append(int(u["language_id"]))
    for r in c.execute("SELECT language_id FROM supervisor_languages WHERE user_id=? ORDER BY language_id", (u["id"],)):
        if r["language_id"] not in ids:
            ids.append(int(r["language_id"]))
    return ids


def _actor_lang_ids(actor):
    try:
        ids = actor.get("lang_ids") if hasattr(actor, "get") else None
    except Exception:
        ids = None
    if ids:
        return [int(x) for x in ids]
    try:
        lid = actor["language_id"]
    except Exception:
        lid = None
    return [int(lid)] if lid else []


def set_supervisor_languages(c, actor, uid, lang_ids):
    _admin_only(actor)
    lang_ids = [int(x) for x in lang_ids if str(x).isdigit()]
    if not lang_ids:
        raise OpsError("pick at least one language")
    c.execute("DELETE FROM supervisor_languages WHERE user_id=?", (uid,))
    for lid in lang_ids:
        c.execute("INSERT OR IGNORE INTO supervisor_languages(user_id, language_id) VALUES(?,?)", (uid, lid))
    c.execute("UPDATE users SET language_id=? WHERE id=?", (lang_ids[0], uid))
    db.audit(c, actor, "set_supervisor_languages", f"user={uid} langs={lang_ids}")


def can_manage(actor, language_id):
    if actor is None or actor == "system":
        return True
    try:
        role = actor["role"] if ("role" in actor.keys() or hasattr(actor, "__getitem__")) else actor.get("role")
    except Exception:
        role = None
    if role in ("admin", "developer"):
        return True
    if role == "lead":
        try:
            perm = actor["perm"] if ("perm" in actor.keys() or hasattr(actor, "__getitem__")) else actor.get("perm", "manage")
            return int(language_id or 0) in _actor_lang_ids(actor) and perm == "manage"
        except Exception:
            return False
    return False


def can_view(actor, language_id):
    if actor is None or actor == "system":
        return True
    try:
        role = actor["role"] if ("role" in actor.keys() or hasattr(actor, "__getitem__")) else actor.get("role")
    except Exception:
        role = None
    if role in ("admin", "developer"):
        return True
    if role in ("lead", "reviewer", "supervisor"):
        try:
            return int(language_id or 0) in _actor_lang_ids(actor)
        except Exception:
            return False
    return False


def need_manage(actor, language_id):
    if not can_manage(actor, language_id):
        raise PermissionError("not allowed for this language / permission")


# ---------------------------------------------------------------- users, codes, auth
def create_user(c, actor, name, role, language_id, phone="", email="", gender="", perm="manage", opening_min=0.0):
    if role not in ("admin", "lead", "reviewer", "participant"):
        raise OpsError("bad role")
    if role == "admin" and (actor is None or actor["role"] not in ("admin", "developer")):
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
        # Per-language uniqueness: the same person may be added under a different language.
        if phone and c.execute("SELECT 1 FROM users WHERE phone=? AND role='participant' AND language_id=?", (phone, language_id)).fetchone():
            raise OpsError(f"duplicate phone {phone} in this language")
        if email and c.execute("SELECT 1 FROM users WHERE email=? AND role='participant' AND language_id=?", (email, language_id)).fetchone():
            raise OpsError(f"duplicate email {email} in this language")
    gender = gender.upper()[:1] if gender.upper()[:1] in ("M", "F") else ""
    for _ in range(20):
        code = codes.new_code()
        if not c.execute("SELECT 1 FROM users WHERE code=?", (code,)).fetchone():
            break
    cur = c.execute("INSERT INTO users(name,phone,email,role,language_id,code,gender,perm,opening_min,active,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (name, phone, email, role, language_id, code, gender, perm, opening_min, 1, time.time()))
    db.audit(c, actor if actor else "system", "create_user", f"{role} {name} lang={language_id}")
    return cur.lastrowid, code


def update_user(c, actor, uid, name, phone="", email="", role=None, language_id=None, active=None):
    u = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if not u:
        raise OpsError("user not found")
    if u["role"] == "admin" and (actor is None or actor["role"] not in ("admin", "developer")):
        raise PermissionError("admin only")
    need_manage(actor, u["language_id"]) if u["role"] == "participant" else _admin_only(actor)
    
    name = (name or "").strip()
    if not name:
        raise OpsError("name required")
    ph = norm_phone(phone)
    em = norm_email(email)
    
    if ph:
        dup = c.execute("SELECT id FROM users WHERE phone=? AND role='participant' AND id!=? AND language_id=?", (ph, uid, u["language_id"])).fetchone()
        if dup:
            raise OpsError(f"phone {ph} is already in use by another participant in this language")
    if em:
        dup = c.execute("SELECT id FROM users WHERE email=? AND role='participant' AND id!=? AND language_id=?", (em, uid, u["language_id"])).fetchone()
        if dup:
            raise OpsError(f"email {em} is already in use by another participant in this language")
            
    c.execute("UPDATE users SET name=?, phone=?, email=? WHERE id=?", (name, ph, em, uid))
    if role and (actor["role"] in ("admin", "developer") or u["role"] == "participant"):
        if role in ("participant", "lead", "reviewer"):
            c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    if language_id and actor["role"] in ("admin", "developer"):
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
    if actor is not None and actor["role"] not in ("admin", "developer"):
        need_manage(actor, u["language_id"])
        if u["role"] in ("lead", "admin"):
            raise PermissionError("team lead can only delete participants")
    
    now = time.time()
    # 1. Release any unconfirmed assignments assigned to this user and reset their script status to READY
    open_sids = [
        row["script_id"] for row in c.execute(
            "SELECT script_id FROM assignments WHERE (ua=? OR ub=?) AND status IN ('ASSIGNED','IN_SESSION')",
            (uid, uid)
        ).fetchall()
    ]
    c.execute("UPDATE assignments SET status='RELEASED', note='agent deleted', updated_at=? WHERE (ua=? OR ub=?) AND status IN ('ASSIGNED','IN_SESSION')", (now, uid, uid))
    if open_sids:
        c.execute(f"UPDATE scripts SET status='READY', updated_at=? WHERE id IN ({','.join(['?']*len(open_sids))})", (now, *open_sids))
    
    # 2. Clean up pairs involving this user
    user_pairs = c.execute("SELECT id FROM pairs WHERE a=? OR b=?", (uid, uid)).fetchall()
    for p in user_pairs:
        pid = p["id"]
        pair_open_sids = [
            row["script_id"] for row in c.execute(
                "SELECT script_id FROM assignments WHERE pair_id=? AND status IN ('ASSIGNED','IN_SESSION')",
                (pid,)
            ).fetchall()
        ]
        c.execute("UPDATE assignments SET status='RELEASED', note='agent deleted', updated_at=? WHERE pair_id=? AND status IN ('ASSIGNED','IN_SESSION')", (now, pid))
        if pair_open_sids:
            c.execute(f"UPDATE scripts SET status='READY', updated_at=? WHERE id IN ({','.join(['?']*len(pair_open_sids))})", (now, *pair_open_sids))
            
        conf_count = c.execute("SELECT COUNT(*) n FROM assignments WHERE pair_id=? AND status IN ('CONFIRMED','ACCEPTED','SUBMITTED','COMPLETED')", (pid,)).fetchone()["n"]
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
    if actor is not None and actor["role"] not in ("admin", "developer"):
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
    if actor is not None and actor["role"] not in ("admin", "developer"):
        raise PermissionError("admin only")


# ---------------------------------------------------------------- script deletion
# Only RELEASED/ABANDONED assignment history may be removed with a script; anything else
# (open work, submitted/accepted/rejected recordings) or any ledger/issue row keeps the script.
_DEAD_ASSIGNMENT = ("RELEASED", "ABANDONED")


def script_ids_in_scope(c, language_id, domain=None, subdomain=None, specialisation=None):
    """Script ids under a folder of the scripts browser (language > domain > sub-domain > specialisation).
    The browser shows an empty domain as 'Uncategorized', so that label matches both."""
    where, args = ["language_id=?"], [int(language_id)]
    if domain:
        if domain == "Uncategorized":
            where.append("COALESCE(domain,'') IN ('','Uncategorized')")
        else:
            where.append("domain=?")
            args.append(domain)
    if subdomain:
        where.append("subdomain=?")
        args.append(subdomain)
    if specialisation:
        where.append("specialisation=?")
        args.append(specialisation)
    return [r["id"] for r in c.execute("SELECT id FROM scripts WHERE " + " AND ".join(where), tuple(args)).fetchall()]


def script_ids_for_codes(c, script_codes):
    ids = []
    codes_ = [s.strip() for s in script_codes if s and s.strip()]
    for i in range(0, len(codes_), 500):
        part = codes_[i:i + 500]
        ids += [r["id"] for r in c.execute(
            f"SELECT id FROM scripts WHERE code IN ({','.join('?' * len(part))})", tuple(part)).fetchall()]
    return ids


def delete_scripts(c, actor, script_ids, dry_run=False):
    """Hard-delete scripts and their versions/jobs/reviews. Returns {deleted, codes, skipped:[{code, reason}]}.
    Scripts with live or recorded work are skipped, never deleted, so hours and reports stay intact."""
    _admin_only(actor)
    deleted, skipped = [], []
    ids = list(dict.fromkeys(int(x) for x in script_ids))
    for i in range(0, len(ids), 500):
        part = ids[i:i + 500]
        qs = ",".join("?" * len(part))
        scripts = c.execute(f"SELECT id, code FROM scripts WHERE id IN ({qs})", tuple(part)).fetchall()
        busy = {}
        for r in c.execute(f"SELECT script_id, status FROM assignments WHERE script_id IN ({qs})", tuple(part)).fetchall():
            st = (r["status"] or "").upper()
            if st not in _DEAD_ASSIGNMENT:
                busy.setdefault(r["script_id"], f"has {st.lower()} assignment")
        for r in c.execute(
                f"SELECT a.script_id FROM assignments a WHERE a.script_id IN ({qs}) AND ("
                "EXISTS(SELECT 1 FROM ledger l WHERE l.assignment_id=a.id) OR "
                "EXISTS(SELECT 1 FROM issues x WHERE x.assignment_id=a.id))", tuple(part)).fetchall():
            busy.setdefault(r["script_id"], "has recorded hours or issues")
        ok = []
        for s in scripts:
            if s["id"] in busy:
                skipped.append({"code": s["code"], "reason": busy[s["id"]]})
            else:
                ok.append(s)
        if ok and not dry_run:
            oq = ",".join("?" * len(ok))
            oids = tuple(s["id"] for s in ok)
            c.execute(f"DELETE FROM assignments WHERE script_id IN ({oq})", oids)
            for t in ("script_versions", "jobs", "reviews"):
                c.execute(f"DELETE FROM {t} WHERE script_id IN ({oq})", oids)
            c.execute(f"DELETE FROM scripts WHERE id IN ({oq})", oids)
        deleted += [s["code"] for s in ok]
    if deleted and not dry_run:
        shown = ",".join(deleted[:50]) + (f",...(+{len(deleted) - 50})" if len(deleted) > 50 else "")
        db.audit(c, actor, "delete_scripts", f"count={len(deleted)} skipped={len(skipped)} codes={shown}")
    return {"deleted": len(deleted), "codes": deleted, "skipped": skipped}


def bulk_preview(c, language_id, text):
    rows, seen_p, seen_e = [], set(), set()
    rd = csv.DictReader(io.StringIO(text.strip()))
    for r in rd:
        r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
        name, ph, em = r.get("name", ""), norm_phone(r.get("phone", "")), norm_email(r.get("email", ""))
        st = "OK"
        if not name:
            st = "INVALID: no name"
        elif ph and (len(ph) != 10 or ph in seen_p or c.execute("SELECT 1 FROM users WHERE phone=? AND role='participant' AND language_id=?", (ph, language_id)).fetchone()):
            st = "DUPLICATE/INVALID phone"
        elif em and (em in seen_e or c.execute("SELECT 1 FROM users WHERE email=? AND role='participant' AND language_id=?", (em, language_id)).fetchone()):
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


def hash_password(password: str, salt: str = None) -> tuple:
    import hashlib
    if not salt:
        salt = secrets.token_hex(16)
    h = hashlib.sha256((salt + password).encode("utf-8")).hexdigest()
    return h, salt


def verify_password(stored_hash: str, stored_salt: str, password: str) -> bool:
    import hashlib
    if not stored_hash or not stored_salt or not password:
        return False
    return hashlib.sha256((stored_salt + password).encode("utf-8")).hexdigest() == stored_hash


def login(c, code, device, ip="?"):
    """Returns (token|None, message, pending_flag). Allows multiple devices to use same code."""
    raw = (code or "").strip()
    norm_c = codes.norm(raw)

    # 1. Match by code (normalized, without dashes, or raw)
    u = c.execute(
        "SELECT * FROM users WHERE UPPER(code)=? OR UPPER(replace(code, '-', ''))=? OR code=?",
        (norm_c, norm_c, raw)
    ).fetchone()

    # 2. Match by phone, email, or exact name
    if not u and raw:
        ph = norm_phone(raw)
        em = norm_email(raw)
        u = c.execute(
            "SELECT * FROM users WHERE (phone=? AND phone!='') OR (lower(email)=? AND email!='') OR lower(name)=?",
            (ph, em.lower(), raw.lower())
        ).fetchone()

    # 3. If still not found
    if not u:
        if not codes.valid(raw):
            return None, "That doesn't look like a valid code (typo?).", False
        return None, "Code not recognised. Ask your team lead.", False

    if not u["active"]:
        return None, "Code switched off or inactive. Ask your team lead.", False

    # No device restriction: the same code/login works on any number of devices at once.
    # (Each login just creates another session row; concurrent sessions per user are allowed.)
    if not u["device"]:
        c.execute("UPDATE users SET device=? WHERE id=?", (device, u["id"]))

    tok, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
    c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (tok, u["id"], csrf, time.time()))
    c.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), u["id"]))
    db.audit(c, u, "login", ip)
    return tok, "", False


def login_password(c, email, password, device, ip="?"):
    """Email & password login for developer and admin."""
    em = norm_email(email)
    if not em or not password:
        return None, "Please enter both email and password.", False

    u = None
    if em.lower() in ("dev@scriptflow.com", "sanath.reddy@awign.com"):
        u = c.execute("SELECT * FROM users WHERE role='developer' AND active=1 ORDER BY id ASC LIMIT 1").fetchone()
        if not u or not u["password_hash"] or not verify_password(u["password_hash"], u["password_salt"], password):
            return None, "Invalid email or password.", False
    elif em.lower() in ("admin@scriptflow.com", "admin@awign.com"):
        u = c.execute("SELECT * FROM users WHERE role='admin' AND active=1 ORDER BY id ASC LIMIT 1").fetchone()
        if not u or not u["password_hash"] or not verify_password(u["password_hash"], u["password_salt"], password):
            return None, "Invalid email or password.", False
    else:
        u = c.execute("SELECT * FROM users WHERE lower(email)=? AND active=1", (em.lower(),)).fetchone()
        if not u or not u["password_hash"] or not verify_password(u["password_hash"], u["password_salt"], password):
            return None, "Invalid email or password.", False

    c.execute("UPDATE users SET device=?, last_seen=? WHERE id=?", (device, time.time(), u["id"]))
    tok, csrf = secrets.token_urlsafe(24), secrets.token_urlsafe(16)
    c.execute("INSERT INTO sessions VALUES(?,?,?,?)", (tok, u["id"], csrf, time.time()))
    db.audit(c, u, "login_pwd", ip)
    return tok, "", False


def get_session(c, tok):
    if not tok:
        return None, None
    r = c.execute("SELECT s.csrf, s.created, u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=?", (tok,)).fetchone()
    if not r or not r["active"] or time.time() - r["created"] > SESSION_TTL:
        return None, None
    u = dict(r)
    u["lang_ids"] = user_lang_ids(c, u)
    return u, r["csrf"]


def touch_seen(u):
    """Record activity for the 'online now' counts. At most one write per user per minute; never breaks a page."""
    try:
        if u and time.time() - float(u.get("last_seen") or 0) > 60:
            with db.tx() as c:
                c.execute("UPDATE users SET last_seen=? WHERE id=?", (time.time(), u["id"]))
    except Exception:
        pass


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
    def _parse_av(val):
        if not val: return set()
        try:
            parsed = json.loads(val)
            return set(parsed) if isinstance(parsed, list) else {str(parsed)}
        except Exception:
            return {val}
    av = {u["id"]: _parse_av(u["availability"]) for u in pool}
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


def can_pair(c, a, b) -> tuple:
    """
    Unified validation rule for pairing two agents across all pairing paths (propose, auto, manual, create).
    Returns (True, "") or (False, reason_string).
    Enforces:
    1. Distinct agents
    2. Both exist, active, role == 'participant'
    3. Same language
    4. Neither agent is currently in an ACTIVE pair
    5. Neither agent has exceeded individual 15h cap (900 min)
    6. This exact pair has not already reached the 7.5h pair cap (450 min)
    """
    if not a or not b or int(a) == int(b):
        return False, "Must select two distinct participants."
    ua = c.execute("SELECT * FROM users WHERE id=?", (a,)).fetchone()
    ub = c.execute("SELECT * FROM users WHERE id=?", (b,)).fetchone()
    if not ua or not ub:
        return False, "One or both participants not found."
    if ua["role"] != "participant" or ub["role"] != "participant":
        return False, "Both users must be participants."
    if not ua["active"] or not ub["active"]:
        return False, "Both participants must be active."
    if ua["language_id"] != ub["language_id"]:
        return False, "Participants must have the same language."

    # Check 15h personal caps using unified get_hours
    ha = get_hours(c, "user", a)
    if ha["committed_min"] >= ha["cap_min"]:
        return False, f"{ua['name']} has reached individual hours cap ({ha['cap_h']}h)."
    hb = get_hours(c, "user", b)
    if hb["committed_min"] >= hb["cap_min"]:
        return False, f"{ub['name']} has reached individual hours cap ({hb['cap_h']}h)."

    # Check 7.5h pair cap between a and b across ALL their joint history (including opening minutes)
    past_pairs = c.execute("SELECT id, opening_min FROM pairs WHERE (a=? AND b=?) OR (a=? AND b=?)", (a, b, b, a)).fetchall()
    joint_opening = sum(float(p["opening_min"] or 0.0) for p in past_pairs)
    past_pair_rec = c.execute(
        "SELECT COALESCE(SUM(conf_min), 0.0) s FROM assignments "
        "WHERE ((ua=? AND ub=?) OR (ua=? AND ub=?)) "
        "AND status IN ('ACCEPTED', 'SUBMITTED', 'COMPLETED', 'REJECTED') "
        "AND status NOT IN ('RELEASED', 'ABANDONED')",
        (a, b, b, a)
    ).fetchone()["s"] or 0.0
    tot_joint = joint_opening + past_pair_rec

    S = db.S(c)
    try:
        pair_cap_min = float(S.get("pair_limit_h", 7.5)) * 60.0
    except Exception:
        pair_cap_min = 450.0

    if tot_joint >= pair_cap_min:
        return False, f"Pair {ua['name']} and {ub['name']} already completed {tot_joint/60.0:.1f}h together (limit {pair_cap_min/60.0:.1f}h reached)."

    if c.execute("SELECT 1 FROM pairs WHERE ((a=? AND b=?) OR (a=? AND b=?)) AND status='LIMIT_REACHED'", (a, b, b, a)).fetchone():
        return False, f"Pair {ua['name']} and {ub['name']} previously reached the limit and cannot be re-formed."

    return True, ""


def create_pair(c, actor, a, b):
    ok, err = can_pair(c, a, b)
    if not ok:
        raise OpsError(err)
    ua = c.execute("SELECT * FROM users WHERE id=?", (a,)).fetchone()
    ub = c.execute("SELECT * FROM users WHERE id=?", (b,)).fetchone()
    need_manage(actor, ua["language_id"])
    for x in (a, b):
        if c.execute("SELECT 1 FROM pairs WHERE status='ACTIVE' AND (a=? OR b=?)", (x, x)).fetchone():
            raise OpsError("someone is already in an active pair")
    now = time.time()
    cur = c.execute("INSERT INTO pairs(language_id,a,b,created_at,status) VALUES(?,?,?,?,'ACTIVE')", (ua["language_id"], a, b, now))
    new_pid = cur.lastrowid
    # DO NOT rewrite assignments.pair_id! Pair history is immutable.
    db.audit(c, actor, "create_pair", f"{a}+{b}")
    return new_pid


def drop_pair(c, actor, pair_id, reason=""):
    p = c.execute("SELECT * FROM pairs WHERE id=?", (pair_id,)).fetchone()
    if not p:
        return 0
    need_manage(actor, p["language_id"])
    now = time.time()
    c.execute("UPDATE pairs SET status='DROPPED', dropped_at=?, note=? WHERE id=?", (now, reason, pair_id))
    open_sids = [r["script_id"] for r in c.execute("SELECT script_id FROM assignments WHERE pair_id=? AND status IN ('ASSIGNED','IN_SESSION')", (pair_id,)).fetchall()]
    n = c.execute("UPDATE assignments SET status='RELEASED', note='pair dropped', updated_at=? WHERE pair_id=? AND status IN ('ASSIGNED','IN_SESSION')",
                  (now, pair_id)).rowcount
    for sid in open_sids:
        c.execute("UPDATE scripts SET status='READY', updated_at=? WHERE id=?", (now, sid))
    db.audit(c, actor, "drop_pair", f"pair={pair_id} released={n} {reason}")
    return n


def pair_members(c, pair):
    return [c.execute("SELECT * FROM users WHERE id=?", (pair[k],)).fetchone() for k in ("a", "b")]


# ---------------------------------------------------------------- hours
def get_hours(c, entity_type: str, entity_id: int) -> dict:
    """
    Unified hours engine for pairs and users across the entire system.
    Returns:
      recorded_min, accepted_min, pending_min, rejected_min, planned_min,
      recorded_h, accepted_h, pending_h, rejected_h, planned_h,
      committed_min, committed_h, cap_min, cap_h, limit_reached
    """
    etype = str(entity_type or "").lower().strip()
    if etype in ("pair", "pairs"):
        p = c.execute("SELECT * FROM pairs WHERE id=?", (entity_id,)).fetchone()
        if not p:
            return {
                "opening_min": 0.0, "recorded_min": 0.0, "accepted_min": 0.0,
                "pending_min": 0.0, "rejected_min": 0.0, "planned_min": 0.0,
                "committed_min": 0.0, "recorded_h": 0.0, "accepted_h": 0.0,
                "pending_h": 0.0, "rejected_h": 0.0, "planned_h": 0.0,
                "committed_h": 0.0, "cap_min": 450.0, "cap_h": 7.5,
                "limit_reached": False
            }
        opening = float(p["opening_min"] or 0.0)
        L = c.execute("SELECT * FROM languages WHERE id=?", (p["language_id"],)).fetchone() if p["language_id"] else None
        S = db.S(c)
        try:
            pair_limit_h = float(S.get("pair_limit_h", 7.5))
        except Exception:
            pair_limit_h = 7.5
        cap_min = float(L["cap_pair_min"]) if (L and L["cap_pair_min"]) else (pair_limit_h * 60.0)
        rows = c.execute(
            "SELECT conf_min, est_min, status, review_status FROM assignments "
            "WHERE pair_id=? AND status NOT IN ('RELEASED', 'ABANDONED') ORDER BY id",
            (entity_id,)
        ).fetchall()

    elif etype in ("user", "person", "agent"):
        u = c.execute("SELECT * FROM users WHERE id=?", (entity_id,)).fetchone()
        if not u:
            return {
                "opening_min": 0.0, "recorded_min": 0.0, "accepted_min": 0.0,
                "pending_min": 0.0, "rejected_min": 0.0, "planned_min": 0.0,
                "committed_min": 0.0, "recorded_h": 0.0, "accepted_h": 0.0,
                "pending_h": 0.0, "rejected_h": 0.0, "planned_h": 0.0,
                "committed_h": 0.0, "cap_min": 900.0, "cap_h": 15.0,
                "limit_reached": False
            }
        opening = float(u["opening_min"] or 0.0)
        L = c.execute("SELECT * FROM languages WHERE id=?", (u["language_id"],)).fetchone() if u["language_id"] else None
        S = db.S(c)
        try:
            user_limit_h = float(S.get("user_limit_h", 15.0))
        except Exception:
            user_limit_h = 15.0
        cap_min = float(L["cap_person_min"]) if (L and L["cap_person_min"]) else (user_limit_h * 60.0)
        rows = c.execute(
            "SELECT conf_min, est_min, status, review_status FROM assignments "
            "WHERE (ua=? OR ub=?) AND status NOT IN ('RELEASED', 'ABANDONED') ORDER BY id",
            (entity_id, entity_id)
        ).fetchall()
    else:
        raise ValueError(f"Unknown entity_type: {entity_type}")
    return _hours_calc(opening, cap_min, rows)


def _empty_hours(cap_min):
    return {
        "opening_min": 0.0, "recorded_min": 0.0, "accepted_min": 0.0,
        "pending_min": 0.0, "rejected_min": 0.0, "planned_min": 0.0,
        "committed_min": 0.0, "recorded_h": 0.0, "accepted_h": 0.0,
        "pending_h": 0.0, "rejected_h": 0.0, "planned_h": 0.0,
        "committed_h": 0.0, "cap_min": cap_min, "cap_h": round(cap_min / 60.0, 2),
        "limit_reached": False
    }


def _chunks(ids, n=500):
    ids = list(ids)
    for i in range(0, len(ids), n):
        yield ids[i:i + n]


def hours_many(c, entity_type, ids):
    """get_hours() for many users or pairs in a few queries. Same rules, same numbers (shares _hours_calc)."""
    etype = str(entity_type or "").lower().strip()
    is_pair = etype in ("pair", "pairs")
    ids = [i for i in dict.fromkeys(ids) if i is not None]
    out = {}
    if not ids:
        return out
    S = db.S(c)
    langs = {r["id"]: r for r in c.execute("SELECT * FROM languages").fetchall()}
    ents, rows_by = {}, {i: [] for i in ids}
    table = "pairs" if is_pair else "users"
    for part in _chunks(ids):
        ph = ",".join("?" * len(part))
        for r in c.execute(f"SELECT * FROM {table} WHERE id IN ({ph})", tuple(part)).fetchall():
            ents[r["id"]] = r
        if is_pair:
            q = (f"SELECT id, pair_id, conf_min, est_min, status, review_status FROM assignments "
                 f"WHERE pair_id IN ({ph}) AND status NOT IN ('RELEASED', 'ABANDONED') ORDER BY id")
            for r in c.execute(q, tuple(part)).fetchall():
                rows_by[r["pair_id"]].append(r)
    if not is_pair:
        seen = {}
        for part in _chunks(ids):
            ph = ",".join("?" * len(part))
            q = (f"SELECT id, ua, ub, conf_min, est_min, status, review_status FROM assignments "
                 f"WHERE (ua IN ({ph}) OR ub IN ({ph})) AND status NOT IN ('RELEASED', 'ABANDONED')")
            for r in c.execute(q, tuple(part) * 2).fetchall():
                seen[r["id"]] = r
        for aid in sorted(seen):
            r = seen[aid]
            for uid in {r["ua"], r["ub"]}:
                if uid in rows_by:
                    rows_by[uid].append(r)
    key, cap_col, default_h = ("pair_limit_h", "cap_pair_min", 7.5) if is_pair else ("user_limit_h", "cap_person_min", 15.0)
    try:
        limit_h = float(S.get(key, default_h))
    except Exception:
        limit_h = default_h
    for i in ids:
        e = ents.get(i)
        if not e:
            out[i] = _empty_hours(450.0 if is_pair else 900.0)
            continue
        L = langs.get(e["language_id"]) if e["language_id"] else None
        cap_min = float(L[cap_col]) if (L and L[cap_col]) else (limit_h * 60.0)
        out[i] = _hours_calc(float(e["opening_min"] or 0.0), cap_min, rows_by[i])
    return out


class HoursBook:
    """Request-scoped hours lookup: prefetch many ids at once, then read them for free."""

    def __init__(self, c):
        self.c, self._u, self._p = c, {}, {}

    def prefetch(self, users=(), pairs=()):
        nu = [i for i in users if i is not None and i not in self._u]
        npr = [i for i in pairs if i is not None and i not in self._p]
        if nu:
            self._u.update(hours_many(self.c, "user", nu))
        if npr:
            self._p.update(hours_many(self.c, "pair", npr))

    def user(self, uid):
        if uid not in self._u:
            self.prefetch(users=[uid])
        return self._u[uid]

    def pair(self, pid):
        if pid not in self._p:
            self.prefetch(pairs=[pid])
        return self._p[pid]


def _hours_calc(opening, cap_min, rows):
    acc_m = 0.0
    pend_m = 0.0
    rej_m = 0.0
    plan_m = 0.0

    for r in rows:
        st = (r["status"] or "").upper()
        rev = (r["review_status"] or "").upper()
        conf = float(r["conf_min"] or 0.0)
        est = float(r["est_min"] or 10.0)

        if rev in ("APPROVED", "ACCEPTED") or st in ("APPROVED", "ACCEPTED", "CONFIRMED"):
            acc_m += conf
        elif rev == "REJECTED" or st == "REJECTED":
            rej_m += conf
        elif st in ("SUBMITTED", "COMPLETED"):
            pend_m += conf
        elif st in ("ASSIGNED", "IN_SESSION"):
            plan_m += est

    rec_m = acc_m + pend_m + rej_m
    committed_m = opening + acc_m + pend_m + rej_m + plan_m

    cap_h = round(cap_min / 60.0, 2)
    acc_h = round(acc_m / 60.0, 2)
    pend_h = round(pend_m / 60.0, 2)
    rej_h = round(rej_m / 60.0, 2)
    plan_h = round(plan_m / 60.0, 2)
    rec_h = round(rec_m / 60.0, 2)
    committed_h = round(committed_m / 60.0, 2)
    limit_reached = (committed_m >= cap_min) or (acc_m >= cap_min)

    return {
        "opening_min": opening,
        "accepted_min": round(acc_m, 2),
        "pending_min": round(pend_m, 2),
        "rejected_min": round(rej_m, 2),
        "planned_min": round(plan_m, 2),
        "recorded_min": round(rec_m, 2),
        "committed_min": round(committed_m, 2),
        "accepted_h": acc_h,
        "pending_h": pend_h,
        "rejected_h": rej_h,
        "planned_h": plan_h,
        "recorded_h": rec_h,
        "committed_h": committed_h,
        "cap_min": cap_min,
        "cap_h": cap_h,
        "limit_reached": limit_reached
    }


def person_used(c, uid):
    return get_hours(c, "user", uid)["committed_min"]


def pair_used(c, pid):
    return get_hours(c, "pair", pid)["committed_min"]


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


def assign(c, actor, pair_id, n, only=None, override=False, reason="", only_script=None):
    """Atomic: runs inside the caller's write transaction. Returns (list of assignment ids, note).
    only_script: assign exactly this script id (manual pick from the Scripts page); same rules apply."""
    p = c.execute("SELECT * FROM pairs WHERE id=?", (pair_id,)).fetchone()
    if not p or p["status"] != "ACTIVE":
        raise OpsError("pair is not active")
    if actor is not None and actor != "system":
        need_manage(actor, p["language_id"])
        try:
            actor_role = actor["role"] if ("role" in actor.keys() or hasattr(actor, "__getitem__")) else actor.get("role")
        except Exception:
            actor_role = None
        is_adm = actor_role in ("admin", "developer")
        if override and not is_adm:
            raise PermissionError("only an admin can override caps")
        if override and not reason.strip():
            reason = "Admin override"
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
    if not override:
        room = int(S["open_limit"]) - open_n
        if room <= 0:
            raise OpsError(f"pair already has {open_n} open scripts (limit {S['open_limit']})")
        n = min(n, room)
    est = float(S["avg_minutes"])
    made, note = [], ""
    allow_test = 1 if S["allow_test_scripts"] == "1" else 0
    for _ in range(n):
        if not override:
            ph = get_hours(c, "pair", pair_id)
            if ph["committed_min"] + est > ph["cap_min"]:
                note = "pair hours cap reached"
                break
            h1 = get_hours(c, "user", u1["id"])
            h2 = get_hours(c, "user", u2["id"])
            if h1["committed_min"] + est > h1["cap_min"] or h2["committed_min"] + est > h2["cap_min"]:
                note = "a person's hours cap reached"
                break
        pick = pick_script(c, L, p, u1, u2, allow_test, only, only_script=only_script)
        if not pick:
            note = ("this script is not available for this pair (already assigned, not ready, or another language)"
                    if only_script else "no eligible approved script left")
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


def assign_fair(c, actor, pair_ids, per_pair, override=False, reason=""):
    """Equal distribution. Hands out scripts ONE AT A TIME, round-robin across the pairs, until every pair has
    `per_pair` or the pool is empty. 35 scripts / 2 pairs / ask 33 each -> 18 + 17, not 33 + 2.
    Returns (total_made, {pair_id: n_made}, notes)."""
    per_pair = max(1, int(per_pair))
    got = {pid: 0 for pid in pair_ids}
    notes = {}
    live = list(pair_ids)
    total = 0
    while live:
        nxt = []
        for pid in live:
            if got[pid] >= per_pair:
                continue
            try:
                made, note = assign(c, actor, pid, 1, override=override, reason=reason)
            except (OpsError, PermissionError) as e:
                notes[pid] = str(e)
                continue
            if not made:
                if note:
                    notes[pid] = note
                continue  # this pair is done (cap / no matching script); others keep going
            got[pid] += len(made)
            total += len(made)
            if got[pid] < per_pair:
                nxt.append(pid)
        live = nxt
    return total, got, notes


def pick_script(c, L, pair, u1, u2, allow_test, only, only_script=None):
    rows = c.execute(
        f"SELECT {db.SCRIPT_COLS} FROM scripts s WHERE s.language_id=? AND s.status IN ('APPROVED','READY') AND (s.is_test=0 OR ?=1) AND NOT EXISTS"
        " (SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED','REJECTED')) ORDER BY s.seq",
        (L["id"], allow_test)).fetchall()
    if only_script is not None:
        rows = [r for r in rows if r["id"] == only_script]
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
    # CASE ... THEN 1 ELSE 0: Postgres cannot sum a boolean (SQLite can); see tests/test_sql_portability.py.
    for r in c.execute("SELECT subdomain, specialisation, COUNT(*) n, SUM(CASE WHEN EXISTS(SELECT 1 FROM assignments a WHERE a.script_id=scripts.id"
                       " AND a.status NOT IN ('RELEASED','ABANDONED','REJECTED')) THEN 1 ELSE 0 END) u FROM scripts WHERE language_id=? GROUP BY 1,2", (L["id"],)):
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
    a = c.execute(
        "SELECT a.*, COALESCE(p.language_id, s.language_id) as language_id "
        "FROM assignments a "
        "LEFT JOIN pairs p ON p.id=a.pair_id "
        "LEFT JOIN scripts s ON s.id=a.script_id "
        "WHERE a.id=?",
        (aid,)
    ).fetchone()
    if not a:
        raise OpsError("assignment not found")
    need_manage(actor, a["language_id"])
    if a["status"] in ("SUBMITTED", "COMPLETED", "ACCEPTED", "CONFIRMED") or a["conf_min"] or a["proof_img"] or a["submitted_at"]:
        raise OpsError("cannot release submitted or completed script")
    if a["status"] not in ("ASSIGNED", "IN_SESSION", "DISPUTED", "SELF_REPORTED"):
        raise OpsError("cannot release in status " + a["status"])
    now = time.time()
    c.execute("UPDATE assignments SET status='RELEASED', note=?, updated_at=? WHERE id=?", (why, now, aid))
    c.execute("UPDATE scripts SET status='READY', updated_at=? WHERE id=?", (now, a["script_id"]))
    db.audit(c, actor, "release_assignment", f"{aid} {why}")


# ---------------------------------------------------------------- session flow
def _mine(c, uid, aid):
    a = c.execute("SELECT a.*, s.code, s.attrs, s.subdomain, s.specialisation, s.language_id lang FROM assignments a"
                  " JOIN scripts s ON s.id=a.script_id WHERE a.id=?", (aid,)).fetchone()
    if not a:
        raise PermissionError("not your assignment")
    if uid not in (a["ua"], a["ub"]):
        u = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        if u and u["role"] in ("admin", "developer"):
            return a, "a"
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
        confirm(c, "system", aid, round(el / 60, 1), False, "Completed by both agents")


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
            c.execute("UPDATE scripts SET status='READY', updated_at=? WHERE id=?", (now, a["script_id"]))
            out["auto_released"] += 1
        elif not a["nudged"] and now > a["assigned_at"] + (a["deadline"] - a["assigned_at"]) / 2:
            c.execute("UPDATE assignments SET nudged=1 WHERE id=?", (a["id"],))
            out["nudged"] += 1
    for a in c.execute("SELECT * FROM assignments WHERE status='IN_SESSION' AND started_at<?", (now - float(S["abandon_hours"]) * 3600,)).fetchall():
        c.execute("UPDATE assignments SET status='ABANDONED', note='no Done after limit', updated_at=? WHERE id=?", (now, a["id"]))
        c.execute("UPDATE scripts SET status='READY', updated_at=? WHERE id=?", (now, a["script_id"]))
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
                          " WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED','REJECTED'))", (L["id"],)).fetchone()["n"]
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
            " SUM(CASE WHEN s.status IN ('APPROVED','IN_REVIEW') THEN 1 ELSE 0 END) made,"
            " SUM(CASE WHEN EXISTS(SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status NOT IN ('RELEASED','ABANDONED')) THEN 1 ELSE 0 END) assigned,"
            " SUM(CASE WHEN EXISTS(SELECT 1 FROM assignments a WHERE a.script_id=s.id AND a.status='CONFIRMED') THEN 1 ELSE 0 END) confirmed"
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
    elif actor["role"] not in ("admin", "developer"):
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
        if actor is not None and actor["role"] not in ("admin", "developer"):
            raise PermissionError("admin only")
        pairs = c.execute("SELECT id FROM pairs WHERE status='ACTIVE'").fetchall()
    else:
        need_manage(actor, language_id)
        pairs = c.execute("SELECT id FROM pairs WHERE language_id=? AND status='ACTIVE'", (language_id,)).fetchall()
    for p in pairs:
        drop_pair(c, actor, p["id"], "bulk dropped")
    return len(pairs)


# ---------------------------------------------------------------- Auto-Pair & Pairing Ops
def auto_pair(c, actor, language_id):
    """
    Pairs all eligible active agents based on availability ('Morning', 'Afternoon', 'Night', 'All day')
    and prefers opposite genders for partners.
    """
    if language_id == "all" or not language_id:
        total = 0
        for l in c.execute("SELECT id FROM languages").fetchall():
            total += auto_pair(c, actor, l["id"])
        return total

    # Only active participants who are NOT in an active pair and have not reached the 15h user limit (Rule R5)
    raw_users = c.execute(
        "SELECT * FROM users WHERE role='participant' AND active=1 AND language_id=? "
        "AND id NOT IN (SELECT a FROM pairs WHERE status='ACTIVE' UNION SELECT b FROM pairs WHERE status='ACTIVE') "
        "ORDER BY id", (language_id,)
    ).fetchall()

    rows = []
    for u in raw_users:
        hu = get_hours(c, "user", u["id"])
        if hu["committed_min"] < hu["cap_min"]:
            rows.append(u)

    if len(rows) < 2:
        return 0

    def norm_slot(val):
        if not val:
            return "All day"
        s = str(val).strip()
        if s.startswith("["):
            try:
                parsed = json.loads(s)
                if parsed and isinstance(parsed, list):
                    s = str(parsed[0])
            except Exception:
                pass
        s_low = s.lower()
        if "morg" in s_low or "morn" in s_low:
            return "Morning"
        if "after" in s_low or "noon" in s_low:
            return "Afternoon"
        if "night" in s_low or "eve" in s_low:
            return "Night"
        return "All day"

    def compatible(s1, s2):
        if s1 == "All day" or s2 == "All day":
            return True
        return s1 == s2

    def gender_opp(u1, u2):
        g1 = (u1["gender"] or "").upper()[:1]
        g2 = (u2["gender"] or "").upper()[:1]
        return 1 if ((g1 == "M" and g2 == "F") or (g1 == "F" and g2 == "M")) else 0

    candidates = list(rows)
    paired = set()
    pairs_created = 0

    # Pass 1: Match opposite genders with compatible availability
    for i, u1 in enumerate(candidates):
        if u1["id"] in paired:
            continue
        s1 = norm_slot(u1["availability"])
        best_partner = None

        for j, u2 in enumerate(candidates):
            if i == j or u2["id"] in paired:
                continue
            s2 = norm_slot(u2["availability"])
            if compatible(s1, s2) and gender_opp(u1, u2) == 1:
                ok, _ = can_pair(c, u1["id"], u2["id"])
                if ok:
                    chosen_slot = s1 if s1 != "All day" else s2
                    best_partner = (u2, chosen_slot)
                    break

        if best_partner:
            u2, slot = best_partner
            paired.add(u1["id"])
            paired.add(u2["id"])
            cur = c.execute(
                "INSERT INTO pairs(language_id, a, b, status, availability, created_at) VALUES(?,?,?,'ACTIVE',?,?)",
                (language_id, u1["id"], u2["id"], slot, time.time())
            )
            pairs_created += 1

    # Pass 2: Match any remaining agents with compatible availability
    for i, u1 in enumerate(candidates):
        if u1["id"] in paired:
            continue
        s1 = norm_slot(u1["availability"])
        best_partner = None

        for j, u2 in enumerate(candidates):
            if i == j or u2["id"] in paired:
                continue
            s2 = norm_slot(u2["availability"])
            if compatible(s1, s2):
                ok, _ = can_pair(c, u1["id"], u2["id"])
                if ok:
                    chosen_slot = s1 if s1 != "All day" else s2
                    best_partner = (u2, chosen_slot)
                    break

        if best_partner:
            u2, slot = best_partner
            paired.add(u1["id"])
            paired.add(u2["id"])
            cur = c.execute(
                "INSERT INTO pairs(language_id, a, b, status, availability, created_at) VALUES(?,?,?,'ACTIVE',?,?)",
                (language_id, u1["id"], u2["id"], slot, time.time())
            )
            pairs_created += 1

    # Pass 3: Pair any remaining unpaired agents regardless of timing/gender differences
    remaining = [u for u in candidates if u["id"] not in paired]
    for k in range(0, len(remaining) - 1, 2):
        u1 = remaining[k]
        u2 = remaining[k + 1]
        ok, _ = can_pair(c, u1["id"], u2["id"])
        if not ok:
            continue
        paired.add(u1["id"])
        paired.add(u2["id"])
        slot = norm_slot(u1["availability"]) if norm_slot(u1["availability"]) != "All day" else norm_slot(u2["availability"])
        cur = c.execute(
            "INSERT INTO pairs(language_id, a, b, status, availability, created_at) VALUES(?,?,?,'ACTIVE',?,?)",
            (language_id, u1["id"], u2["id"], slot or "All day", time.time())
        )
        pairs_created += 1

    if pairs_created:
        db.audit(c, actor if actor else "system", "auto_pair", f"lang={language_id} pairs={pairs_created}")
    return pairs_created


def active_pairs_for(c, uid):
    """All ACTIVE pairs a person is in, with partner info. One person can be in several (manual pairing only)."""
    out = []
    for p in c.execute("SELECT * FROM pairs WHERE status='ACTIVE' AND (a=? OR b=?) ORDER BY id", (uid, uid)).fetchall():
        pid_partner = p["b"] if p["a"] == uid else p["a"]
        pu = c.execute("SELECT id, name, phone, gender FROM users WHERE id=?", (pid_partner,)).fetchone()
        out.append({"pair_id": p["id"], "partner_id": pid_partner,
                    "partner_name": pu["name"] if pu else "?", "partner_phone": (pu["phone"] if pu else "") or "-"})
    return out


def active_pairs_many(c, uids):
    """active_pairs_for() for many people in two queries. Returns {uid: [same dicts, same order]}."""
    uids = [u for u in dict.fromkeys(uids) if u is not None]
    out = {u: [] for u in uids}
    pairs = {}
    for part in _chunks(uids):
        ph = ",".join("?" * len(part))
        for p in c.execute(f"SELECT * FROM pairs WHERE status='ACTIVE' AND (a IN ({ph}) OR b IN ({ph}))", tuple(part) * 2).fetchall():
            pairs[p["id"]] = p
    partner_ids = {p["a"] for p in pairs.values()} | {p["b"] for p in pairs.values()}
    users = {}
    for part in _chunks(partner_ids):
        ph = ",".join("?" * len(part))
        for r in c.execute(f"SELECT id, name, phone, gender FROM users WHERE id IN ({ph})", tuple(part)).fetchall():
            users[r["id"]] = r
    for pid in sorted(pairs):
        p = pairs[pid]
        for uid in ((p["a"],) if p["a"] == p["b"] else (p["a"], p["b"])):
            if uid not in out:
                continue
            partner = p["b"] if p["a"] == uid else p["a"]
            pu = users.get(partner)
            out[uid].append({"pair_id": p["id"], "partner_id": partner,
                             "partner_name": pu["name"] if pu else "?", "partner_phone": (pu["phone"] if pu else "") or "-"})
    return out


def manual_pair(c, actor, language_id, uid_a, uid_b, availability="All day"):
    """Manual pairing. A person may be in MORE THAN ONE active pair; the personal 15h cap stays shared
    across all of their pairs (get_hours('user') sums every assignment). Pairing two people who are
    already an active pair is rejected instead of dropping and re-creating the pair (that used to
    release open scripts and flip A/B)."""
    uid_a, uid_b = int(uid_a), int(uid_b)
    ua = c.execute("SELECT * FROM users WHERE id=?", (uid_a,)).fetchone()
    ub = c.execute("SELECT * FROM users WHERE id=?", (uid_b,)).fetchone()
    if not ua or not ub:
        raise OpsError("Both agents must exist.")
    if uid_a == uid_b:
        raise OpsError("Please select two distinct agents.")
    if ua["language_id"] != ub["language_id"]:
        raise OpsError("Both agents must belong to the same language.")
    lid = ua["language_id"]
    need_manage(actor, lid)

    same = c.execute("SELECT id FROM pairs WHERE status='ACTIVE' AND ((a=? AND b=?) OR (a=? AND b=?))",
                     (uid_a, uid_b, uid_b, uid_a)).fetchone()
    if same:
        raise OpsError(f"{ua['name']} & {ub['name']} are already paired (Pair #{same['id']}). Nothing changed.")

    ok, err = can_pair(c, uid_a, uid_b)
    if not ok:
        raise OpsError(err)

    now = time.time()
    cur = c.execute(
        "INSERT INTO pairs(language_id, a, b, status, availability, created_at) VALUES(?,?,?,'ACTIVE',?,?)",
        (lid, uid_a, uid_b, availability or "All day", now)
    )
    new_pid = cur.lastrowid
    # DO NOT rewrite assignments.pair_id! Pair history is immutable.
    db.audit(c, actor, "manual_pair", f"{uid_a}+{uid_b}")
    return new_pid, ua["name"], ub["name"]


def bulk_upload_pairs_csv(c, actor, language_id, csv_text, report=None):
    """Create pairs from rows of: agent_a, agent_b, availability.
    An agent is matched by access code, phone, email or (unique) name. Returns the number of pairs created;
    pass a list as `report` to receive one human-readable line per rejected row."""
    import csv, io
    from . import importers
    reader = csv.DictReader(io.StringIO((csv_text or "").strip()))
    count = 0
    lid_filter = int(language_id) if str(language_id or "").isdigit() else None
    for n, row in enumerate(reader, start=2):
        def find_u(val):
            val = str(val or "").strip()
            if not val:
                return None, "empty"
            q = "SELECT id, name, language_id FROM users WHERE role='participant' AND active=1 AND "
            lang_sql, lang_args = ("AND language_id=? ", (lid_filter,)) if lid_filter else ("", ())
            r = c.execute(q + "(code=? OR phone=? OR lower(email)=?) " + lang_sql, (val.upper().replace(" ", ""), norm_phone(val), val.lower()) + lang_args).fetchall()
            if len(r) == 1:
                return r[0], ""
            r = c.execute(q + "lower(name)=? " + lang_sql, (val.lower(),) + lang_args).fetchall()
            if len(r) == 1:
                return r[0], ""
            return None, ("not found" if not r else f"{len(r)} agents share this name - use phone or access code")
        a_val = row.get("agent_a") or row.get("phone_a") or row.get("code_a")
        b_val = row.get("agent_b") or row.get("phone_b") or row.get("code_b")
        u1, e1 = find_u(a_val)
        u2, e2 = find_u(b_val)
        why = None
        if not u1:
            why = f"Agent A '{a_val}': {e1}"
        elif not u2:
            why = f"Agent B '{b_val}': {e2}"
        else:
            ok, msg = can_pair(c, u1["id"], u2["id"])
            if not ok:
                why = msg
            elif c.execute("SELECT 1 FROM pairs WHERE status='ACTIVE' AND ((a=? AND b=?) OR (a=? AND b=?))",
                           (u1["id"], u2["id"], u2["id"], u1["id"])).fetchone():
                why = "already paired"
        if why:
            if report is not None:
                report.append(f"Row {n}: {why}")
            continue
        av = importers._norm_avail(row.get("availability"))
        c.execute("INSERT INTO pairs(language_id, a, b, status, availability, created_at) VALUES(?,?,?,'ACTIVE',?,?)",
                  (u1["language_id"], u1["id"], u2["id"], av, time.time()))
        count += 1
    if count:
        db.audit(c, actor, "bulk_upload_pairs", f"count={count}")
    return count


def bulk_upload_agents_csv(c, actor, language_id, csv_text):
    import csv, io
    reader = csv.DictReader(io.StringIO(csv_text.strip()))
    count = 0
    for row in reader:
        name = row.get("name") or row.get("Name") or row.get("agent_name")
        if not name:
            continue
        phone = norm_phone(row.get("phone") or row.get("Phone") or "")
        email = norm_email(row.get("email") or row.get("Email") or "")
        gender = (row.get("gender") or row.get("Gender") or "").upper()[:1]
        av = row.get("availability") or row.get("Availability") or "All day"

        lid = language_id
        if not lid or lid == "all":
            l_name = row.get("language") or row.get("Language")
            if l_name:
                lr = c.execute("SELECT id FROM languages WHERE lower(name)=lower(?)", (l_name.strip(),)).fetchone()
                if lr: lid = lr["id"]
        if not lid or lid == "all":
            fl = c.execute("SELECT id FROM languages LIMIT 1").fetchone()
            lid = fl["id"] if fl else 1

        for _ in range(20):
            code = codes.new_code()
            if not c.execute("SELECT 1 FROM users WHERE code=?", (code,)).fetchone():
                break

        try:
            c.execute(
                "INSERT INTO users(name, phone, email, role, language_id, code, gender, availability, active, created_at) "
                "VALUES(?,?,?,'participant',?,?,?,?,1,?)",
                (name.strip(), phone, email, lid, code, gender, av, time.time())
            )
            count += 1
        except Exception:
            pass
    if count:
        db.audit(c, actor, "bulk_upload_agents", f"count={count}")
    return count


# ---------------------------------------------------------------- Submission & Review
def submit_assignment(c, uid, aid, minutes_or_time, proof_filename):
    """Handles agent submission with completion time (mm:ss) and proof photo. Validates 5 to 15 minutes."""
    a = c.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
    if not a:
        raise OpsError("Assignment not found.")
    if a["status"] in ("RELEASED", "ABANDONED"):
        raise OpsError("This assignment has already been closed.")
    if a["status"] in ("ACCEPTED", "APPROVED") or a["review_status"] in ("ACCEPTED", "APPROVED"):
        raise OpsError("This submission has already been approved and confirmed. It cannot be edited.")
    if a["status"] in ("SUBMITTED", "COMPLETED"):
        raise OpsError("This submission has already been submitted. It cannot be edited.")
    if uid not in (a["ua"], a["ub"]):
        u = c.execute("SELECT role FROM users WHERE id=?", (uid,)).fetchone()
        if not (u and u["role"] in ("admin", "developer")):
            raise PermissionError("You are not assigned to this script.")

    # Rule R3: Session time and proof photo are strictly required (5 to 15 minutes)
    raw_str = str(minutes_or_time or "").strip()
    if not raw_str:
        raise OpsError("Session completion time is strictly required.")

    proof_img_val = proof_filename or a["proof_img"] or ""
    if not proof_img_val:
        raise OpsError("Submission proof photo is strictly required.")

    mins = 0.0
    if ":" in raw_str:
        parts = raw_str.split(":")
        try:
            m = int(parts[0])
            s = int(parts[1]) if len(parts) > 1 else 0
            mins = m + (s / 60.0)
        except ValueError:
            raise OpsError("Time must be 5 to 15 minutes")
    else:
        try:
            mins = float(raw_str)
        except ValueError:
            raise OpsError("Time must be 5 to 15 minutes")

    if mins < 5.0 or mins > 15.0:
        raise OpsError("Time must be 5 to 15 minutes")

    now = time.time()
    c.execute(
        "UPDATE assignments SET conf_min=?, proof_img=?, status='SUBMITTED', review_status='PENDING', "
        "submitted_at=?, updated_at=? WHERE id=?",
        (round(mins, 2), proof_img_val, now, now, aid)
    )
    # Update script status to SUBMITTED
    c.execute("UPDATE scripts SET status='SUBMITTED', updated_at=? WHERE id=?", (now, a["script_id"]))

    if not a["started_at"]:
        c.execute("UPDATE assignments SET started_at=? WHERE id=?", (now - (mins * 60), aid))

    db.audit(c, f"participant:{uid}", "submit_script", f"assignment={aid} min={mins:.2f} photo={proof_filename}")
    return a


def review_report(c, actor, aid, verdict, reason=""):
    """Admin or supervisor approves or rejects a recording report. Implements Rules R1, R2, R4."""
    # Permission check: admin, developer, lead, supervisor, reviewer
    # NB: sessions used to be sqlite3.Row, which is not a dict, so the old isinstance(actor, dict) test rejected every
    # real user and only passed for test fixtures. Read the role by key instead.
    if actor not in ("system", None):
        try:
            _role = actor["role"]
        except (KeyError, TypeError, IndexError):
            _role = None
        if _role not in ("admin", "developer", "lead", "supervisor", "reviewer"):
            raise PermissionError("Only admins, leads, reviewers, or supervisors can review assignments.")

    a = c.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
    if not a:
        raise OpsError("Assignment not found.")

    if actor not in ("system", None):
        _lang = c.execute("SELECT language_id FROM scripts WHERE id=?", (a["script_id"],)).fetchone()
        if _lang:
            need_manage(actor, _lang["language_id"])  # supervisors can only review their own languages
    cur_stat = (a["status"] or "").upper()
    cur_rev = (a["review_status"] or "").upper()
    if cur_stat in ("RELEASED", "ABANDONED"):
        raise OpsError(f"Cannot review an assignment that has been {cur_stat}.")
    if cur_stat in ("ASSIGNED", "IN_SESSION"):
        raise OpsError("Cannot review an assignment that has not been submitted yet.")

    now = time.time()
    v_norm = verdict.upper().strip()
    try:
        reviewer_name = actor["name"] if ("name" in actor.keys() or hasattr(actor, "__getitem__")) else actor.get("name", "Reviewer")
    except Exception:
        reviewer_name = "Reviewer"
    if not reviewer_name or str(reviewer_name).startswith("<sqlite3"):
        reviewer_name = "System Admin"
    reviewer_label = reviewer_name
    full_rev_note = f"{reviewer_label}: {reason}" if reason else reviewer_label

    if v_norm in ("APPROVE", "ACCEPT", "ACCEPTED"):
        if cur_stat == "ACCEPTED" or cur_rev == "ACCEPTED":
            # Idempotent: already accepted, do not duplicate ledger rows!
            return True

        c.execute("UPDATE assignments SET review_status='ACCEPTED', status='ACCEPTED', review_note=?, note=?, updated_at=? WHERE id=?",
                  (full_rev_note, reason, now, aid))
        c.execute("UPDATE scripts SET status='ACCEPTED', updated_at=? WHERE id=?", (now, a["script_id"]))
        
        # Add to ledger for both participants in pair (strictly guarded against duplicate rows)
        mins = float(a["conf_min"] or 0.0)
        if mins > 0:
            existing = c.execute("SELECT 1 FROM ledger WHERE assignment_id=? AND kind='ACCEPTED'", (aid,)).fetchone()
            if not existing:
                c.execute("INSERT INTO ledger(ts, assignment_id, user_id, pair_id, minutes, kind) VALUES(?,?,?,?,?,'ACCEPTED')",
                          (now, aid, a["ua"], a["pair_id"], mins))
                c.execute("INSERT INTO ledger(ts, assignment_id, user_id, pair_id, minutes, kind) VALUES(?,?,?,?,?,'ACCEPTED')",
                          (now, aid, a["ub"], a["pair_id"], mins))
        
        # Check pair limit 7.5 hours (Rule R4) using get_hours
        ph = get_hours(c, "pair", a["pair_id"])
        if ph["accepted_min"] >= ph["cap_min"]:
            c.execute("UPDATE pairs SET status='LIMIT_REACHED' WHERE id=?", (a["pair_id"],))

        db.audit(c, actor, "accept_report", f"assignment={aid} note={reason}")
    else:
        # Rejecting
        if cur_stat == "REJECTED" or cur_rev == "REJECTED":
            return True

        # If reversing a previously accepted assignment, insert compensating reversal rows into append-only ledger
        if cur_stat == "ACCEPTED" or cur_rev == "ACCEPTED":
            accepted_rows = c.execute("SELECT * FROM ledger WHERE assignment_id=? AND kind='ACCEPTED'", (aid,)).fetchall()
            for l_row in accepted_rows:
                c.execute("INSERT INTO ledger(ts, assignment_id, user_id, pair_id, minutes, kind) VALUES(?,?,?,?,?,'REVERSAL')",
                          (now, aid, l_row["user_id"], l_row["pair_id"], -float(l_row["minutes"])))

        # Rule R1: If rejected, the script goes back to Available (the unassigned pool) and can be assigned again.
        c.execute("UPDATE assignments SET review_status='REJECTED', status='REJECTED', review_note=?, note=?, updated_at=? WHERE id=?",
                  (full_rev_note, reason, now, aid))
        c.execute("UPDATE scripts SET status='READY', updated_at=? WHERE id=?", (now, a["script_id"]))
        
        # If pair was in LIMIT_REACHED, revert to ACTIVE if time is now under cap
        ph = get_hours(c, "pair", a["pair_id"])
        if not ph["limit_reached"]:
            c.execute("UPDATE pairs SET status='ACTIVE' WHERE id=? AND status='LIMIT_REACHED'", (a["pair_id"],))

        db.audit(c, actor, "reject_report", f"assignment={aid} reason={reason}")
    return True


# ---------------------------------------------------------------- Library Script Sync
def sync_library_scripts(c, language_id=None):
    """Scans script library folders for new scripts and adds them as READY stock using script_importer."""
    ensure_core_system(c)
    from . import script_importer

    script_dirs = [
        "scripts_library",
        "Script Factory/scripts_library",
        "c:/Users/sanat/Desktop/Awign Doc/Audio project/Script Factory/scripts_library",
        "c:/Users/sanat/Desktop/Awign Doc/Audio project/scripts_library",
    ]

    synced = 0
    scanned_paths = set()
    for sdir in script_dirs:
        resolved = script_importer.resolve_directory_path(sdir)
        if resolved and str(resolved) not in scanned_paths:
            scanned_paths.add(str(resolved))
            res = script_importer.import_directory(c, resolved, target_lang_id=language_id, status="READY")
            synced += res.get("imported", 0)

    return synced



def _random_password() -> str:
    return secrets.token_urlsafe(14)


def _seed_password_hash(env_name: str, label: str):
    """Hash for a NEW seeded account: the env var if set, else a random password printed once to the server log."""
    pwd = os.environ.get(env_name, "").strip()
    if not pwd:
        pwd = _random_password()
        print(f"\n  [SETUP] No {env_name} set. One-time generated {label} password: {pwd}\n"
              f"          Log in, then set {env_name} on the host so it is stable.\n")
    return hash_password(pwd)


def _apply_seed_password(c, uid, env_name: str, label: str):
    """Existing seeded account.
    - env var set  -> that is the password (kept in sync on every start).
    - env var unset -> the old code reset this account to a default that was published in the public repo on EVERY
      start, so the stored password is assumed compromised: rotate it ONCE (flag in settings), print the new one,
      and afterwards leave whatever the admin chose alone."""
    pwd = os.environ.get(env_name, "").strip()
    row = c.execute("SELECT password_hash, password_salt FROM users WHERE id=?", (uid,)).fetchone()
    if pwd:
        if not (row["password_hash"] and verify_password(row["password_hash"], row["password_salt"], pwd)):
            h, sl = hash_password(pwd)
            c.execute("UPDATE users SET password_hash=?, password_salt=? WHERE id=?", (h, sl, uid))
        db.set_setting(c, f"seed_pwd_rotated_{label}", "1")
        return
    if db.setting(c, f"seed_pwd_rotated_{label}", "") != "1":
        pwd = _random_password()
        h, sl = hash_password(pwd)
        c.execute("UPDATE users SET password_hash=?, password_salt=? WHERE id=?", (h, sl, uid))
        db.set_setting(c, f"seed_pwd_rotated_{label}", "1")
        print(f"\n  [SECURITY] {label} password rotated (the previous default was published on GitHub).\n"
              f"             New one-time {label} password: {pwd}\n"
              f"             Set {env_name} on the host to choose your own.\n")


def ensure_core_system(c):
    """Ensures core Admin & Developer accounts, Connections, and base Languages exist without seeding test data or scripts."""
    now = time.time()
    adm_code = os.environ.get("SF_ADMIN_CODE") or codes.new_code()

    # Developer Account
    dev_user = c.execute("SELECT id FROM users WHERE (lower(email) IN ('sanath.reddy@awign.com', 'dev@scriptflow.com') OR role='developer') ORDER BY id ASC LIMIT 1").fetchone()
    if not dev_user:
        dcode = codes.new_code()
        while c.execute("SELECT 1 FROM users WHERE code=?", (dcode,)).fetchone():
            dcode = codes.new_code()
        c.execute(
            "INSERT INTO users(name, email, role, code, active, password_hash, password_salt, created_at) "
            "VALUES('Sanath Reddy', 'sanath.reddy@awign.com', 'developer', ?, 1, ?, ?, ?)",
            (dcode, *_seed_password_hash("SF_DEV_PASSWORD", "developer"), now)
        )
        db.set_setting(c, "seed_pwd_rotated_developer", "1")
    else:
        c.execute("UPDATE users SET name='Sanath Reddy', email='sanath.reddy@awign.com', role='developer', active=1 WHERE id=?", (dev_user["id"],))
        _apply_seed_password(c, dev_user["id"], "SF_DEV_PASSWORD", "developer")

    # Admin Account
    adm_user = c.execute("SELECT id FROM users WHERE (lower(email) IN ('admin@awign.com', 'admin@scriptflow.com') OR role='admin') ORDER BY id ASC LIMIT 1").fetchone()
    if not adm_user:
        while c.execute("SELECT 1 FROM users WHERE code=?", (adm_code,)).fetchone():
            adm_code = codes.new_code()
        c.execute(
            "INSERT INTO users(name, email, role, code, active, password_hash, password_salt, created_at) "
            "VALUES('System Admin', 'admin@awign.com', 'admin', ?, 1, ?, ?, ?)",
            (adm_code, *_seed_password_hash("SF_ADMIN_PASSWORD", "admin"), now)
        )
        db.set_setting(c, "seed_pwd_rotated_admin", "1")
    else:
        c.execute("UPDATE users SET name='System Admin', email='admin@awign.com', role='admin', active=1 WHERE id=?", (adm_user["id"],))
        _apply_seed_password(c, adm_user["id"], "SF_ADMIN_PASSWORD", "admin")

    c.execute("DELETE FROM users WHERE lower(email) IN ('dev@scriptflow.com', 'admin@scriptflow.com')")

    default_conns = [
        ("Supabase Cloud Sync", "Supabase", "Disconnected"),
        ("Google Drive Archival", "Google Drive", "Disconnected"),
        ("Google Sheets Mirror", "Google Sheets", "Disconnected"),
        ("Generation Machine Cluster", "Generation machine", "Connected"),
        ("Email Service", "Email Service", "Connected"),
    ]
    for cname, cserv, cst in default_conns:
        if not c.execute("SELECT 1 FROM connections WHERE name=?", (cname,)).fetchone():
            c.execute("INSERT INTO connections(name, service, status, last_tested) VALUES(?,?,?,?)",
                      (cname, cserv, cst, now))

    # Base Languages
    base_languages = [
        ("MR", "Marathi", 1503, "conversation"),
        ("KN", "Kannada", 1503, "conversation"),
        ("GU", "Gujarati", 1503, "conversation"),
        ("HI", "Hindi", 1503, "conversation"),
        ("TA", "Tamil", 1503, "conversation"),
        ("PA", "Punjabi", 1503, "conversation"),
        ("OR", "Odia", 1503, "conversation"),
        ("TE", "Telugu", 1503, "conversation"),
        ("ML", "Malayalam", 1503, "conversation"),
        ("BN", "Bengali", 1503, "conversation"),
    ]
    for lcode, lname, tgt, lmode in base_languages:
        existing = c.execute("SELECT id FROM languages WHERE code=? OR name=?", (lcode, lname)).fetchone()
        if not existing:
            c.execute(
                "INSERT INTO languages(code, name, target, cap_pair_min, cap_person_min, mode, created_at) "
                "VALUES(?, ?, ?, 450.0, 900.0, ?, ?)",
                (lcode, lname, tgt, lmode, now)
            )

    return True


def seed_full_demo(c):
    """Generates complete realistic demo dataset for the 6 languages: Kannada, Tamil, Gujarati, Punjabi, Marathi, Odia."""
    now = time.time()
    
    # 1. Ensure Default Developer and Admin Accounts
    ensure_core_system(c)

    # 2. Ensure Connections (DV1)
    default_conns = [
        ("Supabase Cloud Sync", "Supabase", "Connected"),
        ("Google Drive Archival", "Google Drive", "Connected"),
        ("Google Sheets Mirror", "Google Sheets", "Connected"),
        ("Generation Machine Cluster", "Generation machine", "Connected"),
        ("Email Service", "Email Service", "Connected"),
    ]
    for cname, cserv, cst in default_conns:
        if not c.execute("SELECT 1 FROM connections WHERE name=?", (cname,)).fetchone():
            c.execute("INSERT INTO connections(name, service, status, last_tested) VALUES(?,?,?,?)",
                      (cname, cserv, cst, now))

    # 3. Ensure Dev Keys (DV2) with OpenRouter key
    if not c.execute("SELECT 1 FROM dev_keys WHERE service='OpenRouter'").fetchone():
        or_key = os.environ.get("OPENROUTER_API_KEY", "")
        c.execute(
            "INSERT INTO dev_keys(name, service, label, key_val, status, created_at) "
            "VALUES('OpenRouter Key', 'OpenRouter', 'OpenRouter Primary Key', ?, 'Active', ?)",
            (or_key, now)
        )

    # 4. Ensure 6 Languages & Realistic Agents (only if language has no agents yet)
    lang_info = [
        ("KN", "Kannada", [
            ("Priya Rao", "9800000001", "priya@scriptflow.com", "F", "Morning"),
            ("Ramesh Nair", "9800000002", "ramesh@scriptflow.com", "M", "Morning"),
            ("Suresh Gowda", "9800000003", "suresh@scriptflow.com", "M", "Afternoon"),
            ("Kavitha Patil", "9800000004", "kavitha@scriptflow.com", "F", "Afternoon"),
            ("Manjunath K", "9800000005", "manju@scriptflow.com", "M", "Night"),
            ("Deepa Hegde", "9800000006", "deepa@scriptflow.com", "F", "Night"),
            ("Raghavendra B", "9800000007", "raghu@scriptflow.com", "M", "All day"),
            ("Ananya Nayak", "9800000008", "ananya@scriptflow.com", "F", "All day"),
        ]),
        ("TA", "Tamil", [
            ("Karthik Raja", "9800000011", "karthik@scriptflow.com", "M", "Morning"),
            ("Divya Krishnan", "9800000012", "divya@scriptflow.com", "F", "Morning"),
            ("Senthil Kumar", "9800000013", "senthil@scriptflow.com", "M", "Afternoon"),
            ("Meenakshi Sundaram", "9800000014", "meenakshi@scriptflow.com", "F", "Afternoon"),
            ("Murugan Natarajan", "9800000015", "murugan@scriptflow.com", "M", "Night"),
            ("Revathi Subramanian", "9800000016", "revathi@scriptflow.com", "F", "Night"),
            ("Saravanan Pillai", "9800000017", "saravanan@scriptflow.com", "M", "All day"),
            ("Bhavani Iyer", "9800000018", "bhavani@scriptflow.com", "F", "All day"),
        ]),
        ("GU", "Gujarati", [
            ("Amit Patel", "9800000021", "amit@scriptflow.com", "M", "Morning"),
            ("Hetal Shah", "9800000022", "hetal@scriptflow.com", "F", "Morning"),
            ("Jayesh Mehta", "9800000023", "jayesh@scriptflow.com", "M", "Afternoon"),
            ("Kinjal Dave", "9800000024", "kinjal@scriptflow.com", "F", "Afternoon"),
            ("Parth Vora", "9800000025", "parth@scriptflow.com", "M", "Night"),
            ("Riddhi Pandya", "9800000026", "riddhi@scriptflow.com", "F", "Night"),
            ("Bhavesh Desai", "9800000027", "bhavesh@scriptflow.com", "M", "All day"),
            ("Daksha Joshi", "9800000028", "daksha@scriptflow.com", "F", "All day"),
        ]),
        ("PA", "Punjabi", [
            ("Harpreet Singh", "9800000031", "harpreet@scriptflow.com", "M", "Morning"),
            ("Jaspreet Kaur", "9800000032", "jaspreet@scriptflow.com", "F", "Morning"),
            ("Gurpreet Gill", "9800000033", "gurpreet@scriptflow.com", "M", "Afternoon"),
            ("Simran Dhillon", "9800000034", "simran@scriptflow.com", "F", "Afternoon"),
            ("Amrit Sandhu", "9800000035", "amrit@scriptflow.com", "M", "Night"),
            ("Manpreet Grewal", "9800000036", "manpreet@scriptflow.com", "F", "Night"),
            ("Kuldeep Brar", "9800000037", "kuldeep@scriptflow.com", "M", "All day"),
            ("Navneet Sidhu", "9800000038", "navneet@scriptflow.com", "F", "All day"),
        ]),
        ("MR", "Marathi", [
            ("Sachin Shinde", "9800000041", "sachin@scriptflow.com", "M", "Morning"),
            ("Sneha Kulkarni", "9800000042", "sneha@scriptflow.com", "F", "Morning"),
            ("Rohan Deshmukh", "9800000043", "rohan@scriptflow.com", "M", "Afternoon"),
            ("Pooja Joshi", "9800000044", "pooja@scriptflow.com", "F", "Afternoon"),
            ("Rahul Patil", "9800000045", "rahul@scriptflow.com", "M", "Night"),
            ("Neha Sawant", "9800000046", "neha@scriptflow.com", "F", "Night"),
            ("Pradeep Jadhav", "9800000047", "pradeep@scriptflow.com", "M", "All day"),
            ("Pallavi More", "9800000048", "pallavi@scriptflow.com", "F", "All day"),
        ]),
        ("OR", "Odia", [
            ("Soumya Mohanty", "9800000051", "soumya@scriptflow.com", "M", "Morning"),
            ("Subhashree Das", "9800000052", "subhashree@scriptflow.com", "F", "Morning"),
            ("Debasish Panda", "9800000053", "debasish@scriptflow.com", "M", "Afternoon"),
            ("Madhusmita Nayak", "9800000054", "madhusmita@scriptflow.com", "F", "Afternoon"),
            ("Chandan Behera", "9800000055", "chandan@scriptflow.com", "M", "Night"),
            ("LipsaMishra", "9800000056", "lipsa@scriptflow.com", "F", "Night"),
            ("Bikash Sahoo", "9800000057", "bikash@scriptflow.com", "M", "All day"),
            ("Itishree Swain", "9800000058", "itishree@scriptflow.com", "F", "All day"),
        ]),
    ]

    for lcode, lname, agents_data in lang_info:
        l = c.execute("SELECT id FROM languages WHERE code=? OR name=?", (lcode, lname)).fetchone()
        if not l:
            c.execute("INSERT INTO languages(code, name, target, created_at) VALUES(?,?,1503,?)", (lcode, lname, now))
            lid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            lid = l["id"]

        # Only seed default demo agents if this language currently has 0 participants
        existing_lang_agents = c.execute("SELECT COUNT(*) n FROM users WHERE role='participant' AND language_id=?", (lid,)).fetchone()["n"]
        if existing_lang_agents == 0:
            for aname, aphone, aemail, agender, aavail in agents_data:
                existing = c.execute("SELECT id FROM users WHERE phone=? OR (email=? AND email<>'')", (aphone, aemail)).fetchone()
                if not existing:
                    acode = codes.new_code()
                    while c.execute("SELECT 1 FROM users WHERE code=?", (acode,)).fetchone():
                        acode = codes.new_code()
                    c.execute(
                        "INSERT INTO users(name, phone, email, role, language_id, code, gender, availability, active, created_at) "
                        "VALUES(?, ?, ?, 'participant', ?, ?, ?, ?, 1, ?)",
                        (aname, aphone, aemail, lid, acode, agender, aavail, now)
                    )
            auto_pair(c, "system", lid)

        # Ensure supervisor for this language
        if not c.execute("SELECT 1 FROM users WHERE role='lead' AND language_id=?", (lid,)).fetchone():
            c.execute("INSERT INTO users(name, role, language_id, code, active, created_at) VALUES(?, 'lead', ?, ?, 1, ?)",
                      (f"{lname} Supervisor", lid, codes.new_code(), now))

        # Load real scripts from scripts_library if available
        import_real_scripts_from_library(c, lid, lcode, lname, now)

        # Assign scripts to active pairs in this language
        active_pairs = c.execute("SELECT id FROM pairs WHERE language_id=? AND status='ACTIVE'", (lid,)).fetchall()
        for p_idx, p in enumerate(active_pairs):
            existing_count = c.execute("SELECT COUNT(*) n FROM assignments WHERE pair_id=? AND status NOT IN ('RELEASED','ABANDONED','REJECTED')", (p["id"],)).fetchone()["n"]
            needed = 2 - existing_count
            if needed > 0:
                avail_scripts = c.execute(
                    "SELECT id FROM scripts WHERE language_id=? AND status='READY' AND id NOT IN (SELECT script_id FROM assignments WHERE status NOT IN ('RELEASED','ABANDONED','REJECTED')) ORDER BY seq ASC LIMIT ?",
                    (lid, needed)
                ).fetchall()
                for idx, sc in enumerate(avail_scripts):
                    pair_members = c.execute("SELECT a, b FROM pairs WHERE id=?", (p["id"],)).fetchone()
                    if idx == 0 and existing_count == 0:
                        is_accepted = (p_idx % 4 == 0)
                        status = "ACCEPTED" if is_accepted else "SUBMITTED"
                        rev_status = "APPROVED" if is_accepted else "PENDING"
                        rev_note = "Recording verified and approved by Supervisor" if is_accepted else ""
                        c.execute(
                            "INSERT INTO assignments(script_id, pair_id, ua, ub, status, est_min, conf_min, proof_img, review_status, review_note, assigned_at, submitted_at, updated_at) "
                            "VALUES(?, ?, ?, ?, ?, 10.0, 9.5, 'sample_submission.png', ?, ?, ?, ?, ?)",
                            (sc["id"], p["id"], pair_members["a"], pair_members["b"], status, rev_status, rev_note, now - 86400 * 2, now - 86400, now)
                        )
                        aid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                        c.execute("UPDATE scripts SET status=?, updated_at=? WHERE id=?", (status, now, sc["id"]))
                        if is_accepted:
                            for u_id in (pair_members["a"], pair_members["b"]):
                                c.execute(
                                    "INSERT INTO ledger(ts, assignment_id, user_id, pair_id, minutes, kind) VALUES(?, ?, ?, ?, 9.5, 'confirmed')",
                                    (now, aid, u_id, p["id"])
                                )
                    else:
                        c.execute(
                            "INSERT INTO assignments(script_id, pair_id, ua, ub, status, est_min, conf_min, assigned_at, deadline, updated_at) "
                            "VALUES(?, ?, ?, ?, 'ASSIGNED', 10.0, NULL, ?, ?, ?)",
                            (sc["id"], p["id"], pair_members["a"], pair_members["b"], now, now + 86400 * 2, now)
                        )
                        c.execute("UPDATE scripts SET status='ASSIGNED', updated_at=? WHERE id=?", (now, sc["id"]))

    return True


def import_real_scripts_from_library(c, lid: int, lcode: str, lname: str, now: float):
    """Imports genuine clinical & consultation scripts from scripts_library for a language."""
    import os, glob, re
    search_folders = [
        os.path.join("scripts_library", lname),
        os.path.join("scripts_library", "Gujarathi" if lname == "Gujarati" else lname),
        os.path.join(r"C:\Users\sanat\Desktop\Awign Doc\Script", lname),
        os.path.join(r"C:\Users\sanat\Desktop\Awign Doc\Script", "Gujarathi" if lname == "Gujarati" else lname),
    ]
    found_files = []
    for sf in search_folders:
        if os.path.exists(sf):
            found_files = sorted(glob.glob(os.path.join(sf, "**", "*.txt"), recursive=True))
            if found_files:
                break

    if not found_files:
        # Fallback to standard 8 topics
        script_topics = [
            ("Healthcare", "Doctor Consultation", "General Physician Consultation", "CONS"),
            ("Healthcare", "Health Insurance Claims", "Hospitalization Cashless Claim", "INSC"),
            ("Healthcare", "Pharmacy", "Chronic Prescription Refill", "PHAR"),
            ("Healthcare", "Telehealth", "Pediatric Video Consult", "TELE"),
            ("BFSI", "Banking - Accounts", "Digital Zero Balance Savings Account", "ACCT"),
            ("BFSI", "Banking - Cards", "Credit Card Fraud Dispute & Hotlisting", "CARD"),
            ("BFSI", "Banking - Loans", "Home Loan Rate & Eligibility Enquiry", "LOAN"),
            ("BFSI", "Insurance - Policy", "Term Life Insurance Policy Renewal", "POLI"),
        ]
        for s_idx, (dom, subdom, spec, sub_code) in enumerate(script_topics, 1):
            std_code = f"{lcode}-{'HC' if dom=='Healthcare' else 'BF'}-{sub_code}-{s_idx:03d}"
            if not c.execute("SELECT 1 FROM scripts WHERE code=?", (std_code,)).fetchone():
                seq = c.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM scripts WHERE language_id=?", (lid,)).fetchone()[0]
                dialogue = f"Taxonomy: {dom} > {subdom} > {spec}\nLanguage: {lname}\nDoctor: Hello, how can I assist you?\nPatient: Namaste, I am reaching out regarding {spec.lower()}."
                c.execute(
                    "INSERT INTO scripts(language_id, seq, code, domain, subdomain, specialisation, status, source, wave, words, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, 'READY', 'seeded', 1, ?, ?, ?)",
                    (lid, seq, std_code, dom, subdom, spec, len(dialogue.split()), now, now)
                )
                sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                c.execute("INSERT OR REPLACE INTO script_versions(script_id, version, turns, words, created_at) VALUES(?, 1, ?, ?, ?)",
                          (sid, dialogue, len(dialogue.split()), now))
        return

    for idx, fpath in enumerate(found_files, 1):
        try:
            txt = open(fpath, encoding="utf-8").read().strip()
            if not txt:
                continue
            words = len(txt.split())
            domain = "Healthcare"
            subdomain = "Doctor-Patient Consultation"
            specialisation = "General Physician"

            m_id = re.search(r'Script ID\s*:\s*([A-Za-z0-9\-_]+)', txt, re.IGNORECASE)
            m_spec = re.search(r'Specialty\s*:\s*([^\n\r]+)', txt, re.IGNORECASE)
            if m_spec:
                specialisation = m_spec.group(1).strip()
            else:
                parent = os.path.basename(os.path.dirname(fpath))
                if parent in ('Dentist', 'Dermatology', 'General Physician', 'Gynecologist', 'Orthopedist', 'Pediatrician'):
                    specialisation = parent

            if m_id:
                code = m_id.group(1).strip()
            else:
                spec_abbr = re.sub(r'[^A-Za-z]', '', specialisation)[:4].upper()
                code = f"{lcode}-HC-{spec_abbr}-{idx:03d}"

            existing = c.execute("SELECT id FROM scripts WHERE code=?", (code,)).fetchone()
            if not existing:
                seq = c.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM scripts WHERE language_id=?", (lid,)).fetchone()[0]
                c.execute(
                    "INSERT INTO scripts(language_id, seq, code, domain, subdomain, specialisation, status, source, wave, words, created_at, updated_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, 'READY', 'library', 1, ?, ?, ?)",
                    (lid, seq, code, domain, subdomain, specialisation, words, now, now)
                )
                sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
            else:
                sid = existing["id"]
                c.execute("UPDATE scripts SET domain=?, subdomain=?, specialisation=?, words=?, updated_at=? WHERE id=?",
                          (domain, subdomain, specialisation, words, now, sid))

            c.execute("INSERT OR REPLACE INTO script_versions(script_id, version, turns, words, created_at) VALUES(?, 1, ?, ?, ?)",
                      (sid, txt, words, now))
        except Exception:
            pass
