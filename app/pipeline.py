"""Script review and manual fixes (script generation was removed: scripts come from library uploads)."""
import json
import os
import threading
import time

from . import config, db
from . import validators as V


def _shingles_changed():
    """Tell the upload duplicate-check cache that a fingerprint was rewritten (see script_importer)."""
    from .script_importer import shingles_changed
    shingles_changed()


class Stop:
    """Shutdown signal for the background threads in main.py."""
    event = threading.Event()


def render_text(turns, roles):
    lab = {"A": roles[0], "B": roles[1]}
    return "\n".join(f"{lab.get(t['s'], t['s'])}: {t['t']}" for t in turns)


def lines_to_turns(text, roles):
    pairs = V.parse_lines(text)
    return V.map_labels(pairs, roles)


# ---------------------------------------------------------------- review
def backup_script(c, script_id):
    s = c.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    L = c.execute("SELECT name FROM languages WHERE id=?", (s["language_id"],)).fetchone()
    v = c.execute("SELECT turns FROM script_versions WHERE script_id=? AND version=?", (script_id, s["version"])).fetchone()
    if not v:
        return
    attrs = json.loads(s["attrs"]) if s["attrs"] else {"roles": ["A", "B"]}
    d = os.path.join(config.backup_dir(), config.PROJECT_NAME, L["name"], s["domain"], s["subdomain"], s["specialisation"] or "-")
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{s['code']}_v{s['version']}.txt"), "w", encoding="utf-8") as f:
            f.write(render_text(json.loads(v["turns"]), attrs["roles"]))
    except OSError as e:
        db.audit(c, "system", "backup_failed", f"{s['code']}: {e}")


def review(c, script_id, user, verdict, reason="", edited_text=None):
    s = c.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    if not s or s["status"] != "IN_REVIEW":
        raise ValueError("script is not in review")
    if user["role"] == "reviewer" and user["language_id"] != s["language_id"]:
        raise PermissionError("other language")
    L = c.execute("SELECT * FROM languages WHERE id=?", (s["language_id"],)).fetchone()
    attrs = json.loads(s["attrs"])
    S = db.S(c)
    now = time.time()
    if verdict == "accept":
        c.execute("UPDATE scripts SET status='APPROVED', updated_at=? WHERE id=?", (now, script_id))
        backup_script(c, script_id)
    elif verdict == "fix":
        turns, err = lines_to_turns(edited_text or "", attrs["roles"])
        if err:
            raise ValueError(err)
        errs = V.validate(turns, attrs, L, S) + V.similarity_check(c, L["id"], script_id, turns, float(S["sim_threshold"]))
        if errs:
            raise ValueError("Edited text fails validation: " + "; ".join(errs))
        ver = s["version"] + 1
        c.execute("INSERT INTO script_versions VALUES(?,?,?,?,?,?,?)",
                  (script_id, ver, json.dumps(turns, ensure_ascii=False), V.count_words(turns), s["prompt_version"], "reviewer fix", now))
        c.execute("UPDATE scripts SET status='APPROVED', version=?, words=?, shingles=?, opening=?, updated_at=? WHERE id=?",
                  (ver, V.count_words(turns), V.pack(V.shingle_set(turns)), V.opening_key(turns), now, script_id))
        _shingles_changed()
        backup_script(c, script_id)
    elif verdict == "reject":
        c.execute("UPDATE scripts SET reject_reason=?, status='PLANNED', shingles=NULL, opening=NULL WHERE id=?", (reason, script_id))
        _shingles_changed()
    else:
        raise ValueError("bad verdict")
    c.execute("INSERT INTO reviews(script_id,version,reviewer,verdict,reason,ts) VALUES(?,?,?,?,?,?)",
              (script_id, s["version"], user["id"], verdict, reason, now))
    db.audit(c, user, "review_" + verdict, f"{s['code']} {reason}")


def manual_script(c, script_id, user, text):
    """Human-written script for a NEEDS_HUMAN or PLANNED slot. Same validators apply."""
    s = c.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    L = c.execute("SELECT * FROM languages WHERE id=?", (s["language_id"],)).fetchone()
    attrs = json.loads(s["attrs"])
    turns, err = lines_to_turns(text, attrs["roles"])
    if err:
        raise ValueError(err)
    S = db.S(c)
    errs = V.validate(turns, attrs, L, S) + V.similarity_check(c, L["id"], script_id, turns, float(S["sim_threshold"]))
    if errs:
        raise ValueError("Fails validation: " + "; ".join(errs))
    ver = s["version"] + 1
    now = time.time()
    c.execute("INSERT INTO script_versions VALUES(?,?,?,?,?,?,?)",
              (script_id, ver, json.dumps(turns, ensure_ascii=False), V.count_words(turns), "manual", "manual entry", now))
    c.execute("UPDATE scripts SET status='IN_REVIEW', version=?, words=?, shingles=?, opening=?, source='manual', needs_review=1,"
              " is_test=0, updated_at=? WHERE id=?",
              (ver, V.count_words(turns), V.pack(V.shingle_set(turns)), V.opening_key(turns), now, script_id))
    _shingles_changed()
    db.audit(c, user, "manual_script", s["code"])
