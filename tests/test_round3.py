"""Round 3: per-pair assign, KPI drill on every tab, Drive import, Agents partner hover, cache busting."""
import os
import re
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "r3.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)
ADMIN_CODE = os.environ["SF_ADMIN_CODE"]

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, ops  # noqa: E402
from app.integrations import drive_import as DI  # noqa: E402
from app.main import app  # noqa: E402

A = {"role": "admin", "id": 0, "name": "t", "language_id": None}


def _csrf(client, url):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


_TOPICS = ["dry cough that gets worse at night", "burning pain while passing urine", "swollen right ankle after a fall",
           "constant headache behind the eyes", "itchy red rash on both forearms", "sharp pain in the lower back"]


def _script(i):
    t = _TOPICS[i % len(_TOPICS)]
    return (f"Doctor: Good morning, I see you came in because of a {t}, so please tell me more about it.\n"
            f"Patient: For about {10 + i} days I have had a {t}, and it is not improving with home remedies.\n"
            f"Doctor: Let me examine you carefully, and I will also check your temperature and blood pressure now.\n"
            f"Patient: Thank you doctor, I am worried because my family member {['Ravi','Sita','Mohan','Geeta','Anil','Rekha'][i % 6]} had the same trouble.")


# ------------------------------------------------------------------ 1. per-pair assign
def test_per_pair_assign_box_and_route_admin_and_supervisor():
    with TestClient(app, raise_server_exceptions=False) as adm:
        with db.tx() as c:
            lid = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES('MH','Marathi',0,1.0)").lastrowid
            ids = [ops.create_user(c, A, n, "participant", lid, phone=f"90000000{i}1", gender="M")[0]
                   for i, n in enumerate(["Aa", "Bb", "Cc", "Dd"])]
            p1 = ops.manual_pair(c, A, lid, ids[0], ids[1])
            p2 = ops.manual_pair(c, A, lid, ids[2], ids[3])
            sup_id, sup_code = ops.create_user(c, A, "Sup", "lead", lid, phone="9555500001")
            ops.set_supervisor_languages(c, A, sup_id, [lid])
            for i in range(12):
                c.execute("INSERT INTO scripts(code,language_id,domain,subdomain,specialisation,status,version,created_at,updated_at) "
                          "VALUES(?,?,?,?,?,'READY',1,1.0,1.0)", (f"MHDG{i:03d}", lid, "Healthcare", "Doctor-Patient", "GP"))
            pids = [r["id"] for r in c.execute("SELECT id FROM pairs ORDER BY id").fetchall()]
        adm.post("/login", data={"code": ADMIN_CODE})
        html = adm.get(f"/admin/pairs?lang={lid}").text
        # one number box + Assign button per pair, posting to the per-pair route
        for pid in pids:
            assert f'action="/admin/assign/pair/{pid}"' in html
        assert html.count('class="pair-assign-form"') == 2
        assert 'name="count"' in html and "Assign to all" in html

        tok = _csrf(adm, f"/admin/pairs?lang={lid}")
        r = adm.post(f"/admin/assign/pair/{pids[0]}", data={"csrf": tok, "count": "5"}, follow_redirects=False)
        assert r.status_code == 303 and "Assigned+5" in r.headers["location"], r.headers["location"]
        with db.ro() as c:
            n = lambda pid: c.execute("SELECT COUNT(*) n FROM assignments WHERE pair_id=? AND status='ASSIGNED'", (pid,)).fetchone()["n"]
            assert n(pids[0]) == 5 and n(pids[1]) == 0   # only the chosen pair got scripts
        # more than the stock: assigns what exists and says so
        r = adm.post(f"/admin/assign/pair/{pids[1]}", data={"csrf": tok, "count": "50"}, follow_redirects=False)
        assert "Assigned+7" in r.headers["location"] and "asked+for+50" in r.headers["location"], r.headers["location"]
        # insane counts are clamped, not honoured
        assert adm.post(f"/admin/assign/pair/{pids[1]}", data={"csrf": tok, "count": "99999999"}, follow_redirects=False).status_code == 303

    # supervisor (non-admin) must also be able to assign; this used to raise "only an admin can override caps".
    # Supervisors respect the open-script limit (5), admins override it.
    with db.tx() as c:
        for i in range(12, 20):
            c.execute("INSERT INTO scripts(code,language_id,domain,subdomain,specialisation,status,version,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,'READY',1,1.0,1.0)", (f"MHDG{i:03d}", lid, "Healthcare", "Doctor-Patient", "GP"))
        e1 = ops.create_user(c, A, "Ee", "participant", lid, phone="9000000091", gender="M")[0]
        e2 = ops.create_user(c, A, "Ff", "participant", lid, phone="9000000092", gender="M")[0]
        p3 = ops.manual_pair(c, A, lid, e1, e2)
        p3 = c.execute("SELECT id FROM pairs ORDER BY id DESC").fetchone()["id"]
    with TestClient(app, raise_server_exceptions=False) as sup:
        sup.post("/login", data={"code": sup_code})
        tok = _csrf(sup, f"/admin/pairs?lang={lid}")
        r = sup.post(f"/admin/assign/pair/{p3}", data={"csrf": tok, "count": "2"}, follow_redirects=False)
        assert r.status_code == 303 and "Assigned+2" in r.headers["location"], r.headers["location"]
        # a pair already at the open limit gets a clear cap message, not a permission error
        r = sup.post(f"/admin/assign/pair/{pids[0]}", data={"csrf": tok, "count": "1"}, follow_redirects=False)
        assert "open+scripts" in r.headers["location"] and "override" not in r.headers["location"], r.headers["location"]
        r = sup.post("/admin/assign/execute", data={"csrf": tok, "count": "1", "language_id": str(lid)}, follow_redirects=False)
        assert "Assigned+" in r.headers["location"] and "override" not in r.headers["location"], r.headers["location"]


# ------------------------------------------------------------------ 2/5. KPI cards on every tab, numbers == drill rows
def test_every_tab_card_is_wired_and_number_matches_rows():
    with TestClient(app, raise_server_exceptions=False) as adm:
        with db.ro() as c:
            lid = c.execute("SELECT id FROM languages WHERE code='MH'").fetchone()["id"]
        adm.post("/login", data={"code": ADMIN_CODE})
        pages = {
            "agents": ("ag_total", "ag_active", "ag_paired", "ag_unpaired"),
            "scripts": ("sc_total", "sc_available", "sc_assigned", "sc_completed"),
            "pairs": ("ag_active", "ag_paired", "ag_unpaired", "pr_total", "pr_active", "pr_limit",
                      "sc_total", "sc_available", "sc_assigned", "sc_pending", "sc_completed", "sc_accepted"),
            "reports": ("sc_recorded", "sc_review_pending", "sc_accepted", "sc_rejected"),
        }
        for tab, kinds in pages.items():
            html = adm.get(f"/admin/{tab}?lang={lid}").text
            for k in kinds:
                m = re.search(r'data-kpi="%s"[^>]*>\s*<div class="kpi-num"[^>]*>\s*([\d.]+)' % k, html)
                assert m, (tab, k)
                shown = float(m.group(1))
                rows = adm.get(f"/admin/kpi_rows?kind={k}&lang_id={lid}").json()["rows"]
                if k not in ("sc_assigned",):   # sc_assigned counts DISTINCT scripts; rows are assignments (equal unless a script is double-assigned)
                    assert int(shown) == len(rows), (tab, k, shown, len(rows))
        # popup rows for agents/pairs carry a link or pair id so rows can be opened
        d = adm.get(f"/admin/kpi_rows?kind=pr_total&lang_id={lid}").json()
        assert len(d["meta"]) == len(d["rows"]) and all(m["pair"] for m in d["meta"])
        d = adm.get(f"/admin/kpi_rows?kind=ag_total&lang_id={lid}").json()
        assert all("agent_id=" in m["href"] for m in d["meta"])
        # the shared popup is on the page and is the only thing a click opens (no navigation attrs on cards)
        html = adm.get(f"/admin/agents?lang={lid}").text
        assert 'id="kpiModal"' in html and "sortKpi" in html


# ------------------------------------------------------------------ 4. agents: paired column shows no names, hover carries details
def test_agents_paired_column_hides_names_and_has_hover_payload():
    with TestClient(app, raise_server_exceptions=False) as adm:
        with db.ro() as c:
            lid = c.execute("SELECT id FROM languages WHERE code='MH'").fetchone()["id"]
        adm.post("/login", data={"code": ADMIN_CODE})
        html = adm.get(f"/admin/agents?lang={lid}").text
        assert "data-hover-partners=" in html
        cells = re.findall(r"<td[^>]*>\s*<span class=\"hover-trigger\" tabindex=\"0\" data-hover-partners='([^']*)'.*?</span>\s*</span>", html, re.S)
        assert cells, "paired cells not found"
        import html as H
        import json
        for raw in cells:
            payload = json.loads(H.unescape(raw))
            assert payload and all({"name", "phone", "email", "gender", "availability", "status", "recorded_h"} <= set(x) for x in payload)
        # visible text of the Paired With cell is only "N pair(s)", partner names appear only inside the hover payload
        m = re.search(r"data-hover-partners='[^']*'[^>]*>\s*<span class=\"status-badge\"[^>]*>([^<]*)</span>\s*</span>", html)
        assert re.fullmatch(r"\s*\d+ pairs?\s*", m.group(1))
        assert 'title="Bb"' not in html and "partners_tooltip" not in html
        # base.html knows how to render it
        assert "renderPartnersCard" in html


# ------------------------------------------------------------------ cache busting (stale CSS made new UI look missing)
def test_static_assets_are_versioned_and_pages_not_cached():
    with TestClient(app, raise_server_exceptions=False) as adm:
        adm.post("/login", data={"code": ADMIN_CODE})
        r = adm.get("/admin/agents?lang=all")
        assert r.headers["cache-control"] == "no-cache"
        v = re.search(r"/static/style\.css\?v=([0-9a-f]{10})", r.text)
        assert v
        s = adm.get(f"/static/style.css?v={v.group(1)}")
        assert "immutable" in s.headers["cache-control"] and ".kpi-modal" in s.text
        assert adm.get("/static/style.css").headers["cache-control"] == "no-cache"


# ------------------------------------------------------------------ 3. Drive import
def test_parse_drive_url():
    P = DI.parse_drive_url
    fid = "1AbCdEfGhIjKlMnOpQrStUv"
    assert P(f"https://drive.google.com/drive/folders/{fid}?usp=sharing") == {"kind": "folder", "id": fid}
    assert P(f"https://drive.google.com/drive/u/0/folders/{fid}") == {"kind": "folder", "id": fid}
    assert P(f"https://drive.google.com/file/d/{fid}/view?usp=drive_link") == {"kind": "file", "id": fid}
    assert P(f"https://docs.google.com/document/d/{fid}/edit") == {"kind": "gdoc", "id": fid}
    assert P(f"https://drive.google.com/open?id={fid}") == {"kind": "unknown", "id": fid}
    for bad in ["", "not a url", "ftp://drive.google.com/x", "https://evil.example.com/drive/folders/" + fid,
                "https://drive.google.com.evil.com/drive/folders/" + fid, "http://169.254.169.254/latest/meta-data",
                "https://drive.google.com/drive/my-drive", "https://drive.google.com/drive/folders/short"]:
        try:
            P(bad)
            raise AssertionError("accepted " + bad)
        except DI.DriveImportError:
            pass


FOLDER = "F0LDERid_0123456789"
SUB = "SUBfolder_0123456789"


def _entry(fid, name, folder=False, doc=False):
    href = (f"https://drive.google.com/drive/folders/{fid}" if folder else
            (f"https://docs.google.com/document/d/{fid}/edit" if doc else f"https://drive.google.com/file/d/{fid}/view?usp=drive_web"))
    return (f'<div class="flip-entry" id="entry-{fid}" tabindex="0"><div class="flip-entry-list-icon"><a href="{href}"></a></div>'
            f'<div class="flip-entry-info"><a href="{href}"><div class="flip-entry-title">{name}</div></a></div></div>')


def _link_transport(seen):
    files = {"FILEaaaaaaaaaa1": ("marathi_Healthcare_Doctor-Patient_General-Physician_cough.txt", _script(1)),
             "FILEbbbbbbbbbb2": ("marathi_Healthcare_Doctor-Patient_General-Physician_fever.txt", _script(2))}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        assert req.url.host in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com"), req.url
        if req.url.path == "/embeddedfolderview":
            fid = req.url.params["id"]
            if fid == FOLDER:
                body = _entry("FILEaaaaaaaaaa1", "marathi_Healthcare_Doctor-Patient_General-Physician_cough.txt") + \
                       _entry("PDFcccccccccc3", "notes.pdf") + _entry(SUB, "more", folder=True) + \
                       _entry("DOCdddddddddd4", "marathi_Healthcare_Doctor-Patient_General-Physician_gdoc", doc=True)
            elif fid == SUB:
                body = _entry("FILEbbbbbbbbbb2", "marathi_Healthcare_Doctor-Patient_General-Physician_fever.txt")
            else:
                return httpx.Response(404)
            return httpx.Response(200, text=f"<html><body>{body}</body></html>")
        if req.url.path == "/download":
            fid = req.url.params["id"]
            if fid in files:
                return httpx.Response(200, content=files[fid][1].encode(), headers={"content-type": "text/plain"})
            if fid == "PDFcccccccccc3":
                return httpx.Response(200, content=b"%PDF", headers={"content-type": "application/pdf"})
        if req.url.path.endswith("/export") and "DOCdddddddddd4" in req.url.path:
            return httpx.Response(200, content=_script(3).encode(), headers={"content-type": "text/plain"})
        return httpx.Response(404)
    return httpx.MockTransport(handler)


def test_drive_importer_link_mode_walks_subfolders_and_filters():
    seen = []
    imp = DI.Importer({}, http=httpx.Client(transport=_link_transport(seen), follow_redirects=True))
    assert imp.use_api is False
    res = imp.collect(f"https://drive.google.com/drive/folders/{FOLDER}?usp=sharing")
    ok = {i.path: i for i in res.items if i.data}
    assert set(ok) == {"marathi_Healthcare_Doctor-Patient_General-Physician_cough.txt",
                       "more/marathi_Healthcare_Doctor-Patient_General-Physician_fever.txt",
                       "marathi_Healthcare_Doctor-Patient_General-Physician_gdoc.txt"}
    bad = [i for i in res.items if not i.data]
    assert [b.path for b in bad] == ["notes.pdf"] and "skipped" in bad[0].error
    assert all(u.startswith(("https://drive.google.com/", "https://docs.google.com/", "https://drive.usercontent.google.com/")) for u in seen)


def test_drive_importer_private_folder_gives_clear_error():
    def handler(req):
        return httpx.Response(200, text="<html>Sign in - accounts.google.com</html>")
    imp = DI.Importer({}, http=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        imp.collect(f"https://drive.google.com/drive/folders/{FOLDER}")
        raise AssertionError("should fail")
    except DI.DriveImportError as e:
        assert "private" in str(e).lower() or "anyone with the link" in str(e).lower()


def test_drive_importer_api_mode_with_token():
    def handler(req):
        assert req.headers.get("authorization") == "Bearer tok123"
        if req.url.path == "/drive/v3/files":
            return httpx.Response(200, json={"files": [
                {"id": "FILEaaaaaaaaaa1", "name": "marathi_Healthcare_Doctor-Patient_General-Physician_cough.txt", "mimeType": "text/plain"},
                {"id": "PDFcccccccccc3", "name": "x.pdf", "mimeType": "application/pdf"}]})
        if req.url.path == "/drive/v3/files/FILEaaaaaaaaaa1":
            assert req.url.params["alt"] == "media"
            return httpx.Response(200, content=_script(1).encode())
        return httpx.Response(404)
    imp = DI.Importer({"access_token": "tok123"}, http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert imp.use_api is True
    res = imp.collect(f"https://drive.google.com/drive/folders/{FOLDER}")
    assert [i.path for i in res.items if i.data] == ["marathi_Healthcare_Doctor-Patient_General-Physician_cough.txt"]


def test_drive_import_route_imports_dedupes_and_scopes(monkeypatch):
    with TestClient(app, raise_server_exceptions=False) as adm:
        with db.ro() as c:
            lid = c.execute("SELECT id FROM languages WHERE code='MH'").fetchone()["id"]
            n0 = c.execute("SELECT COUNT(*) n FROM scripts").fetchone()["n"]
        seen = []
        real = DI.Importer.__init__
        monkeypatch.setattr(DI.Importer, "__init__", lambda self, cfg=None, http=None, now=None: real(
            self, cfg, http=httpx.Client(transport=_link_transport(seen), follow_redirects=True)))
        adm.post("/login", data={"code": ADMIN_CODE})
        tok = _csrf(adm, f"/admin/scripts?lang={lid}")
        url = f"https://drive.google.com/drive/folders/{FOLDER}"
        assert 'name="drive_url"' not in adm.get(f"/admin/scripts?lang={lid}").text  # JS-driven, no server-path form left
        page = adm.get(f"/admin/scripts?lang={lid}").text
        assert "Import from Drive" in page and "Import Local" not in page and "import_local" not in page

        r = adm.post("/admin/scripts/import_drive", data={"csrf": tok, "lang": str(lid), "drive_url": url})
        d = r.json()
        assert r.status_code == 200 and d["mode"] == "link", d
        assert d["summary"]["imported"] == 3, d["results"]
        assert d["summary"]["skipped"] == 1   # notes.pdf
        with db.ro() as c:
            assert c.execute("SELECT COUNT(*) n FROM scripts").fetchone()["n"] == n0 + 3
        # second run: duplicates skipped, nothing new
        d2 = adm.post("/admin/scripts/import_drive", data={"csrf": tok, "lang": str(lid), "drive_url": url}).json()
        assert d2["summary"]["imported"] == 0
        # validation
        assert adm.post("/admin/scripts/import_drive", data={"csrf": "bad", "drive_url": url}).status_code == 400
        assert adm.post("/admin/scripts/import_drive", data={"csrf": tok, "drive_url": "https://evil.example.com/x"}).status_code == 400
        assert adm.post("/admin/scripts/import_drive", data={"csrf": tok, "drive_url": ""}).status_code == 400

    with db.tx() as c:
        sup_id, sup_code = ops.create_user(c, A, "Sup2", "lead", lid, phone="9555500002")
        ops.set_supervisor_languages(c, A, sup_id, [lid])
        other = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES('ZZ','Zulu',0,1.0)").lastrowid
    with TestClient(app, raise_server_exceptions=False) as sup:
        sup.post("/login", data={"code": sup_code})
        tok = _csrf(sup, f"/admin/scripts?lang={lid}")
        url = f"https://drive.google.com/drive/folders/{FOLDER}"
        assert sup.post("/admin/scripts/import_drive", data={"csrf": tok, "lang": "all", "drive_url": url}).status_code == 403
        assert sup.post("/admin/scripts/import_drive", data={"csrf": tok, "lang": str(other), "drive_url": url}).status_code == 403
