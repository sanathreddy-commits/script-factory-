"""Admin-side setup: languages, quotas, API keys."""
import time

from . import db
from . import taxonomy as T


def create_language(c, actor, preset, code="", name="", target=None, quotas=None, cap_pair_h=7.5, cap_person_h=15.0,
                    mode="read_aloud", deadline=""):
    if actor is not None and actor["role"] not in ("admin", "developer"):
        raise PermissionError("admin only")
    if preset in T.PRESETS:
        nm, cd, lo, hi, clo, chi, slo, shi, names = T.PRESETS[preset]
    else:
        raise ValueError("unknown preset")
    code = (code or cd).upper()
    name = name or nm
    quotas = quotas or {s[1]: T.PER_SUB for s in T.SUBS}
    total = sum(quotas.values())
    target = target if target is not None else total
    if target != total:
        raise ValueError(f"Target {target} does not match quota total {total}. Fix one of them.")
    if c.execute("SELECT 1 FROM languages WHERE code=?", (code,)).fetchone():
        raise ValueError("language code already exists")
    lid = c.execute(
        "INSERT INTO languages(code,name,target,cap_pair_min,cap_person_min,mode,deadline,lo,hi,c_lo,c_hi,s_lo,s_hi,names,seed,created_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (code, name, target, cap_pair_h * 60, cap_person_h * 60, mode, deadline, lo, hi, clo, chi, slo, shi, names, 1, time.time())).lastrowid
    for dom, sub, *_ in T.SUBS:
        c.execute("INSERT INTO quotas VALUES(?,?,?,?)", (lid, dom, sub, quotas.get(sub, 0)))
    try:
        T.create_library_folders(name)
    except Exception:
        pass
    db.audit(c, actor if actor else "system", "create_language", f"{name} target={target}")
    return lid


def delete_language(c, actor, lang_id):
    """Permanently delete a language and cascade-delete its scripts, pairs, quotas, and participants."""
    if actor is not None and actor["role"] not in ("admin", "developer"):
        raise PermissionError("admin only")
    lang = c.execute("SELECT * FROM languages WHERE id=?", (lang_id,)).fetchone()
    if not lang:
        raise ValueError("Language not found")

    # 1. Scripts and their dependent records
    sids = [r["id"] for r in c.execute("SELECT id FROM scripts WHERE language_id=?", (lang_id,)).fetchall()]
    if sids:
        placeholders = ",".join(["?"] * len(sids))
        c.execute(f"DELETE FROM jobs WHERE script_id IN ({placeholders})", sids)
        c.execute(f"DELETE FROM script_versions WHERE script_id IN ({placeholders})", sids)
        c.execute(f"DELETE FROM reviews WHERE script_id IN ({placeholders})", sids)
        c.execute(f"DELETE FROM assignments WHERE script_id IN ({placeholders})", sids)
        c.execute("DELETE FROM scripts WHERE language_id=?", (lang_id,))

    # 2. Pairs and any remaining pair assignments
    pids = [r["id"] for r in c.execute("SELECT id FROM pairs WHERE language_id=?", (lang_id,)).fetchall()]
    if pids:
        placeholders = ",".join(["?"] * len(pids))
        c.execute(f"DELETE FROM assignments WHERE pair_id IN ({placeholders})", pids)
        c.execute("DELETE FROM pairs WHERE language_id=?", (lang_id,))

    # 3. Clean up participants belonging exclusively to this language
    parts = c.execute("SELECT id FROM users WHERE language_id=? AND role='participant'", (lang_id,)).fetchall()
    for u in parts:
        c.execute("DELETE FROM sessions WHERE user_id=?", (u["id"],))
        c.execute("DELETE FROM device_requests WHERE user_id=?", (u["id"],))
        c.execute("DELETE FROM users WHERE id=?", (u["id"],))

    # 4. Unlink supervisors/leads so their accounts are preserved
    c.execute("UPDATE users SET language_id=NULL WHERE language_id=? AND role IN ('lead', 'supervisor')", (lang_id,))

    # 5. Issues, Quotas, and Import Batches
    c.execute("DELETE FROM issues WHERE language_id=?", (lang_id,))
    c.execute("DELETE FROM quotas WHERE language_id=?", (lang_id,))
    c.execute("DELETE FROM import_batches WHERE language_id=?", (lang_id,))

    # 6. Delete the language record itself
    c.execute("DELETE FROM languages WHERE id=?", (lang_id,))

    db.audit(c, actor if actor else "system", "delete_language", f"id={lang_id} name={lang['name']} code={lang['code']}")
    return True


def update_quotas(c, actor, lang_id, quotas):
    if actor["role"] not in ("admin", "developer"):
        raise PermissionError("admin only")
    L = c.execute("SELECT * FROM languages WHERE id=?", (lang_id,)).fetchone()
    if L["planned"]:
        raise ValueError("Slots are already planned. A quota change now needs a new spec version and re-plan.")
    for sub, n in quotas.items():
        c.execute("UPDATE quotas SET n=? WHERE language_id=? AND subdomain=?", (n, lang_id, sub))
    total = sum(r["n"] for r in c.execute("SELECT n FROM quotas WHERE language_id=?", (lang_id,)))
    c.execute("UPDATE languages SET target=? WHERE id=?", (total, lang_id))
    db.audit(c, actor, "update_quotas", f"lang={lang_id} total={total}")
    return total


def test_supabase(c=None):
    from . import sync
    return sync.test_connection(c=c)


def backup_supabase(c=None):
    from . import sync
    return sync.upload_backup(force=True, c=c)


def restore_supabase(c=None):
    from . import sync
    return sync.restore_backup(c=c)


def sync_supabase_tables(c=None, tables=None):
    from . import sync
    return sync.sync_tables_direct(tables=tables, c=c)

