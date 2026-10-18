"""Bulk import helpers for Agents and Pairs.

Accepts: .csv, .xlsx (local upload) or a Google Sheet link.
Header names are case-insensitive; spaces/hyphens are treated like underscores.
"""
import csv
import io
import re
import threading
import time
from typing import Dict, List, Optional

import httpx

from . import db, ops

# ---------------------------------------------------------------- documented formats (shown in the UI)
AGENT_COLUMNS = [
    ("name", "Yes", "Full name", "Sanath Reddy"),
    ("phone", "Yes*", "10-digit mobile (used to match existing agents)", "9108039980"),
    ("email", "Yes*", "Email (used to match if phone is empty)", "sanath.reddy@awign.com"),
    ("gender", "No", "M or F", "M"),
    ("availability", "No", "Morning / Afternoon / Night / All day (default All day)", "Morning"),
    ("language", "No", "Only needed when importing for 'All languages'", "Marathi"),
]
PAIR_COLUMNS = [
    ("agent_a", "Yes", "Name, phone, email or access code of agent A", "Harsh Lalchandani"),
    ("agent_b", "Yes", "Name, phone, email or access code of agent B", "Ramesh"),
    ("availability", "No", "Morning / Afternoon / Night / All day (default All day)", "Morning"),
]

_ALIASES = {
    "agent_name": "name", "full_name": "name", "agent": "name",
    "mobile": "phone", "mobile_number": "phone", "phone_number": "phone", "contact": "phone",
    "email_id": "email", "email_address": "email", "mail": "email",
    "sex": "gender", "shift": "availability", "slot": "availability",
    "agent_1": "agent_a", "agenta": "agent_a", "agent_a_name": "agent_a", "phone_a": "agent_a", "phone_1": "agent_a",
    "agent_2": "agent_b", "agentb": "agent_b", "agent_b_name": "agent_b", "phone_b": "agent_b", "phone_2": "agent_b",
}


def _norm_header(h: str) -> str:
    k = re.sub(r"[\s\-]+", "_", str(h or "").strip().lower())
    k = re.sub(r"[^a-z0-9_]", "", k)
    return _ALIASES.get(k, k)


def _norm_avail(v: str) -> str:
    s = re.sub(r"[\s_\-]+", "", str(v or "").lower())
    return {"morning": "Morning", "afternoon": "Afternoon", "night": "Night", "evening": "Night",
            "allday": "All day", "fullday": "All day", "any": "All day"}.get(s, "All day")


def rows_to_csv_text(rows: List[List]) -> str:
    """rows[0] is the header. Returns CSV text with normalised header names; blank rows dropped."""
    rows = [r for r in rows if r and any(str(x).strip() for x in r if x is not None)]
    if not rows:
        return ""
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([_norm_header(h) for h in rows[0]])
    for r in rows[1:]:
        w.writerow(["" if x is None else (str(int(x)) if isinstance(x, float) and x.is_integer() else str(x)).strip() for x in r])
    return out.getvalue()


def upload_to_csv_text(filename: str, content: bytes) -> str:
    fn = (filename or "").lower()
    if fn.endswith((".xlsx", ".xlsm")):
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True, read_only=True)
        ws = wb.worksheets[0]
        return rows_to_csv_text([list(r) for r in ws.iter_rows(values_only=True)])
    if fn.endswith(".xls"):
        raise ValueError("Old .xls files are not supported. Save the file as .xlsx or .csv and upload again.")
    text = content.decode("utf-8-sig", errors="ignore")
    try:
        dialect = csv.Sniffer().sniff(text[:2048], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return rows_to_csv_text(list(csv.reader(io.StringIO(text), dialect)))


# ---------------------------------------------------------------- Google Sheet link
def _sheet_ids(url: str):
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url or "")
    if not m:
        raise ValueError("That does not look like a Google Sheets link (expected docs.google.com/spreadsheets/d/...).")
    g = re.search(r"[#&?]gid=(\d+)", url)
    return m.group(1), (g.group(1) if g else "0")


def fetch_sheet_csv(url: str, c=None) -> str:
    sid, gid = _sheet_ids(url)
    export = f"https://docs.google.com/spreadsheets/d/{sid}/export?format=csv&gid={gid}"
    r = httpx.get(export, timeout=30, follow_redirects=True)
    ok = r.status_code == 200 and "text/html" not in r.headers.get("content-type", "")
    if ok:
        return rows_to_csv_text(list(csv.reader(io.StringIO(r.content.decode("utf-8-sig", errors="ignore")))))
    # Private sheet: fall back to the service account configured under Developer > Google Sheets
    try:
        from .integrations import sheets
        cfg = sheets.get_sheets_config(c)
        with httpx.Client(timeout=30) as cl:
            headers, params, err = sheets.resolve_auth(cfg, client=cl)
            if not err:
                meta = cl.get(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}", headers=headers,
                              params={**params, "fields": "sheets.properties"})
                if meta.status_code == 200:
                    title = next((s["properties"]["title"] for s in meta.json().get("sheets", [])
                                  if str(s["properties"].get("sheetId")) == gid), None)
                    if title:
                        vr = cl.get(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/'{title}'",
                                    headers=headers, params=params)
                        if vr.status_code == 200:
                            return rows_to_csv_text(vr.json().get("values", []))
    except Exception:
        pass
    raise ValueError("Could not read the sheet. In Google Sheets click Share > General access > 'Anyone with the link' "
                     "> Viewer (or share it with the service-account email configured in the Developer panel), then try again.")


def fetch_sheet_csv_by_name(url_or_sid: str, sheet_name: str, c=None) -> str:
    """Fetch ONE tab (by its visible tab name) from a spreadsheet, as CSV text.
    Uses the gviz endpoint so we can read a named tab (e.g. 'Pairs') from the same spreadsheet as the
    agents tab, without needing its gid. Falls back to the Sheets API (service account) for private sheets."""
    import urllib.parse
    m = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", url_or_sid or "")
    sid = m.group(1) if m else (url_or_sid or "").strip()
    if not sid:
        raise ValueError("No spreadsheet id/url provided.")
    name = (sheet_name or "").strip()
    gviz = f"https://docs.google.com/spreadsheets/d/{sid}/gviz/tq?tqx=out:csv&sheet={urllib.parse.quote(name)}"
    try:
        r = httpx.get(gviz, timeout=30, follow_redirects=True)
        if r.status_code == 200 and "text/html" not in r.headers.get("content-type", ""):
            return rows_to_csv_text(list(csv.reader(io.StringIO(r.content.decode("utf-8-sig", errors="ignore")))))
    except Exception:
        pass
    # Private sheet: Sheets API by tab title
    try:
        from .integrations import sheets
        cfg = sheets.get_sheets_config(c)
        with httpx.Client(timeout=30) as cl:
            headers, params, err = sheets.resolve_auth(cfg, client=cl)
            if not err:
                vr = cl.get(f"https://sheets.googleapis.com/v4/spreadsheets/{sid}/values/'{name}'",
                            headers=headers, params=params)
                if vr.status_code == 200:
                    return rows_to_csv_text(vr.json().get("values", []))
    except Exception:
        pass
    raise ValueError(f"Could not read the '{name}' tab. Check the tab name and that the spreadsheet is shared "
                     "as 'Anyone with the link > Viewer' (or with the service account).")


# ---------------------------------------------------------------- agents upsert
def import_agents(c, actor, default_lang_id, csv_text: str) -> Dict:
    """Upsert agents. Match order inside a language: phone, then email. Never deletes anyone."""
    res = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
    reader = csv.DictReader(io.StringIO(csv_text or ""))
    if not reader.fieldnames or "name" not in reader.fieldnames:
        res["errors"].append("Header row must contain a 'name' column (see the format on the page).")
        return res
    # Runs every 15 minutes from the auto-sync. Read languages and existing agents once (was ~3 queries per row, each
    # a network round trip to Supabase), and write only rows whose values actually change.
    langs = {(r["name"] or "").lower(): r["id"] for r in c.execute("SELECT id, name FROM languages").fetchall()}
    by_phone, by_email = {}, {}

    def _remember(u):
        if u["phone"]:
            by_phone.setdefault((u["language_id"], u["phone"]), u)
        if u["email"]:
            by_email.setdefault((u["language_id"], u["email"]), u)

    for u in c.execute("SELECT id, name, gender, availability, phone, email, language_id FROM users "
                       "WHERE role='participant' ORDER BY id").fetchall():
        _remember(dict(u))
    for i, row in enumerate(reader, start=2):
        name = (row.get("name") or "").strip()
        if not name:
            res["skipped"] += 1
            continue
        phone, email = ops.norm_phone(row.get("phone") or ""), ops.norm_email(row.get("email") or "")
        gender = (row.get("gender") or "").strip().upper()[:1]
        gender = gender if gender in ("M", "F") else ""
        avail = _norm_avail(row.get("availability"))
        lid = default_lang_id if str(default_lang_id or "").isdigit() else None
        lname = (row.get("language") or "").strip()
        if lname:
            lid = langs.get(lname.lower(), lid)
        if not lid:
            res["errors"].append(f"Row {i} ({name}): no language - choose a language or add a 'language' column.")
            continue
        if not phone and not email:
            res["errors"].append(f"Row {i} ({name}): needs a phone or an email.")
            continue
        lid = int(lid)
        ex = None
        if phone:
            ex = by_phone.get((lid, phone))
        if not ex and email:
            ex = by_email.get((lid, email))
        try:
            if ex:
                new = {"name": name, "gender": gender or ex["gender"], "availability": avail,
                       "phone": phone or ex["phone"], "email": email or ex["email"]}
                if any(new[k] != ex[k] for k in new):
                    c.execute("UPDATE users SET name=?, gender=COALESCE(NULLIF(?,''),gender), availability=?, "
                              "phone=COALESCE(NULLIF(?,''),phone), email=COALESCE(NULLIF(?,''),email) WHERE id=?",
                              (name, gender, avail, phone, email, ex["id"]))
                    ex.update(new)
                    _remember(ex)
                res["updated"] += 1
            else:
                uid, _code = ops.create_user(c, actor, name, "participant", lid, phone=phone, email=email, gender=gender)
                c.execute("UPDATE users SET availability=? WHERE id=?", (avail, uid))
                _remember({"id": uid, "name": name, "gender": gender, "availability": avail, "phone": phone,
                           "email": email, "language_id": lid})
                res["created"] += 1
        except Exception as e:
            res["errors"].append(f"Row {i} ({name}): {e}")
    db.audit(c, actor, "import_agents", f"created={res['created']} updated={res['updated']} errors={len(res['errors'])}")
    return res


def summarize(res: Dict) -> str:
    msg = f"{res['created']} added, {res['updated']} updated, {res['skipped']} blank rows skipped."
    if res["errors"]:
        msg += f" {len(res['errors'])} row(s) rejected: " + " | ".join(res["errors"][:3])
    return msg


# ---------------------------------------------------------------- saved link + auto sync
def run_saved_sheet_sync(c, actor=None) -> Dict:
    url = db.setting(c, "agents_sheet_url", "") or ""
    if not url:
        return {"created": 0, "updated": 0, "skipped": 0, "errors": ["No sheet link saved."]}
    lid = db.setting(c, "agents_sheet_lang", "") or ""
    if actor is None:
        actor = c.execute("SELECT * FROM users WHERE role='admin' AND active=1 ORDER BY id LIMIT 1").fetchone()
    try:
        res = import_agents(c, actor, lid, fetch_sheet_csv(url, c))
    except Exception as e:
        res = {"created": 0, "updated": 0, "skipped": 0, "errors": [str(e)]}
    # Same spreadsheet, second tab for pairs (agents imported first so pair rows can reference them).
    pairs_tab = db.setting(c, "pairs_sheet_tab", "") or ""
    if pairs_tab:
        try:
            from . import ops  # lazy import to avoid a circular import at module load
            preport = []
            res["pairs_created"] = ops.bulk_upload_pairs_csv(c, actor, lid, fetch_sheet_csv_by_name(url, pairs_tab, c), report=preport)
            res["pairs_rejected"] = len(preport)
        except Exception as e:
            res.setdefault("errors", []).append(f"pairs tab: {e}")
    db.set_setting(c, "agents_sheet_last", time.strftime("%Y-%m-%d %H:%M:%S") + " - " + summarize(res))
    return res


_thread = None
_stop = threading.Event()


def start_auto_sync(poll_seconds: int = 60):
    """Background loop: every N minutes (setting agents_sheet_every_min, default 15) re-import the saved sheet."""
    global _thread
    if _thread and _thread.is_alive():
        return

    def loop():
        last = 0.0
        while not _stop.is_set():
            try:
                with db.ro() as c:
                    on = db.setting(c, "agents_sheet_auto", "0") == "1"
                    every = max(5, int(db.setting(c, "agents_sheet_every_min", "15") or 15)) * 60
                if on and time.time() - last >= every:
                    with db.tx() as c:
                        run_saved_sheet_sync(c)
                    last = time.time()
            except Exception as e:
                print("[AgentSheetSync]", e)
            _stop.wait(poll_seconds)

    _thread = threading.Thread(target=loop, daemon=True)
    _thread.start()


def stop_auto_sync():
    _stop.set()
