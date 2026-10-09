"""Admin-side setup: languages, quotas, API keys."""
import time

from . import db
from . import taxonomy as T


def create_language(c, actor, preset, code="", name="", target=None, quotas=None, cap_pair_h=7.5, cap_person_h=15.0,
                    mode="read_aloud", deadline=""):
    if actor is not None and actor["role"] != "admin":
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


def update_quotas(c, actor, lang_id, quotas):
    if actor["role"] != "admin":
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


def plan(c, actor, lang_id):
    if actor["role"] != "admin":
        raise PermissionError("admin only")
    S = db.S(c)
    n = T.plan_language(c, lang_id, int(S["wave1_size"]), int(S["wave_size"]))
    db.audit(c, actor, "plan_language", f"lang={lang_id} slots={n}")
    return n


def add_key(c, actor, label, provider, model, role, api_key="", base_url="", languages="*", rpm=30, daily_cap=500.0,
            monthly_cap=5000.0, price_in=0.0, price_out=0.0):
    if actor["role"] != "admin":
        raise PermissionError("admin only")
    if provider not in ("stub", "anthropic", "openai"):
        raise ValueError("provider must be stub, anthropic or openai")
    if provider != "stub" and not api_key:
        raise ValueError("API key required")
    kid = c.execute(
        "INSERT INTO api_keys(label,provider,model,role,languages,api_key,base_url,rpm,daily_cap,monthly_cap,price_in,price_out)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (label, provider, model, role, languages or "*", api_key, base_url, rpm, daily_cap, monthly_cap, price_in, price_out)).lastrowid
    db.audit(c, actor, "add_key", f"{label} {provider}/{model} {role}")
    return kid


def mask_key(k):
    s = k or ""
    return "(none)" if not s else (s[:4] + "…" + s[-4:] if len(s) > 10 else "••••")
