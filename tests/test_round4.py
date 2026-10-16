"""Round 4: short-session hours, Excel/CSV/sheet imports, pair bulk upload, durable proof photos, no default passwords."""
import io
import os
import re
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "r4.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
os.environ.pop("SUPABASE_URL", None)
os.environ.pop("SUPABASE_KEY", None)
from app import codes  # noqa: E402

_body = "BCDEFGH"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)
ADMIN_CODE = os.environ["SF_ADMIN_CODE"]

import openpyxl  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, importers, ops, storage  # noqa: E402
from app.main import _hm, app  # noqa: E402

ADMIN = {"role": "admin", "id": 0, "name": "t", "language_id": None}


import pytest  # noqa: E402


@pytest.fixture(autouse=True, scope="module")
def _my_admin_code():
    """Other test modules overwrite SF_ADMIN_CODE at import time; pin ours for this module's fresh DB."""
    old = os.environ.get("SF_ADMIN_CODE")
    os.environ["SF_ADMIN_CODE"] = ADMIN_CODE
    os.environ.pop("SUPABASE_URL", None)
    os.environ.pop("SUPABASE_KEY", None)
    yield
    if old is None:
        os.environ.pop("SF_ADMIN_CODE", None)
    else:
        os.environ["SF_ADMIN_CODE"] = old


def _csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def _lang(c, code="MR", name="Marathi"):
    r = c.execute("SELECT id FROM languages WHERE code=?", (code,)).fetchone()
    return r["id"] if r else c.execute("INSERT INTO languages(code,name,target,created_at) VALUES(?,?,0,1)", (code, name)).lastrowid


def test_hm_filter_shows_minutes():
    assert _hm(8 / 60) == "8 min"
    assert _hm(0) == "0 h"
    assert _hm(1.5) == "1h 30m"
    assert _hm(7.5) == "7h 30m"


def test_8_minutes_is_not_rounded_to_0_1_hours():
    with TestClient(app):
        with db.tx() as c:
            lid = _lang(c)
            a, _ = ops.create_user(c, ADMIN, "A1", "participant", lid, phone="9000000001")
            b, _ = ops.create_user(c, ADMIN, "B1", "participant", lid, phone="9000000002")
            pid, _, _ = ops.manual_pair(c, ADMIN, None, a, b)
            sid = c.execute("INSERT INTO scripts(code,language_id,status,seq,domain,subdomain,specialisation) VALUES('S1',?,'READY',1,'d','s','x')", (lid,)).lastrowid
            c.execute("INSERT INTO assignments(script_id,pair_id,ua,ub,status,conf_min,review_status) VALUES(?,?,?,?,'SUBMITTED',8.0,'PENDING')", (sid, pid, a, b))
            h = ops.get_hours(c, "user", a)
        assert h["recorded_min"] == 8.0
        assert h["recorded_h"] == 0.13   # was 0.1 with 1-decimal rounding


def test_import_formats_and_upsert():
    rows = [["Agent Name", "Mobile", "Email ID", "Sex", "Shift"],
            ["Asha", 9111111111, "asha@x.com", "f", "morning"],
            ["Bala", "9222222222", "", "M", "all day"],
            [None, None, None, None, None],
            ["NoContact", "", "", "M", ""]]
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    xlsx_text = importers.upload_to_csv_text("agents.xlsx", buf.getvalue())
    csv_text = importers.upload_to_csv_text("agents.csv", b"name;phone;email\nAsha;9111111111;asha@x.com\n")
    assert xlsx_text.splitlines()[0] == "name,phone,email,gender,availability"
    assert "name,phone,email" in csv_text

    with TestClient(app):
        with db.tx() as c:
            lid = _lang(c, "KA", "Kannada")
            res = importers.import_agents(c, ADMIN, lid, xlsx_text)
            assert (res["created"], res["updated"]) == (2, 0)
            assert len(res["errors"]) == 1 and "NoContact" in res["errors"][0]
            asha = c.execute("SELECT * FROM users WHERE phone='9111111111'").fetchone()
            assert asha["gender"] == "F" and asha["availability"] == "Morning"
            # second run = update, not duplicate
            res2 = importers.import_agents(c, ADMIN, lid, "name,phone,availability\nAsha Rao,9111111111,Night\n")
            assert (res2["created"], res2["updated"]) == (0, 1)
            assert c.execute("SELECT availability, name FROM users WHERE phone='9111111111'").fetchone()["name"] == "Asha Rao"
            assert c.execute("SELECT COUNT(*) n FROM users WHERE phone='9111111111'").fetchone()["n"] == 1
            bad = importers.import_agents(c, ADMIN, lid, "phone\n9333333333\n")
            assert "name" in bad["errors"][0]


def test_sheet_link_parsing():
    sid, gid = importers._sheet_ids("https://docs.google.com/spreadsheets/d/1AbC_dEf-123/edit?usp=sharing#gid=456")
    assert (sid, gid) == ("1AbC_dEf-123", "456")
    try:
        importers._sheet_ids("https://example.com/x")
        assert False
    except ValueError:
        pass


def test_bulk_pairs_by_name_with_report_and_routes_use_file_field():
    with TestClient(app) as adm:
        with db.tx() as c:
            lid = _lang(c, "GU", "Gujarati")
            for n, ph in (("Harsh L", "9444000001"), ("Ramesh G", "9444000002"), ("Twin", "9444000003"), ("Twin", "9444000004")):
                ops.create_user(c, ADMIN, n, "participant", lid, phone=ph)
        csv_text = importers.rows_to_csv_text([["Agent A", "Agent B", "Availability"],
                                               ["Harsh L", "Ramesh G", "morning"],
                                               ["Harsh L", "Twin", ""],
                                               ["Harsh L", "Nobody", ""],
                                               ["Harsh L", "Ramesh G", ""]])
        with db.tx() as c:
            report = []
            n = ops.bulk_upload_pairs_csv(c, ADMIN, lid, csv_text, report=report)
        assert n == 1
        assert any("share this name" in x for x in report)
        assert any("not found" in x for x in report)
        assert any("already paired" in x for x in report)
        with db.ro() as c:
            assert c.execute("SELECT availability FROM pairs ORDER BY id DESC LIMIT 1").fetchone()["availability"] == "Morning"

        # real HTTP path: the form field is called "file" (it used to be read as "csv_file" -> always 0 imported)
        adm.post("/login", data={"code": ADMIN_CODE})
        tok = _csrf(adm.get("/admin/agents?lang=all").text)
        r = adm.post("/admin/agents/bulk_upload", data={"csrf": tok, "language_id": str(lid)},
                     files={"file": ("a.csv", b"name,phone,gender\nNew Person,9555000001,M\n", "text/csv")}, follow_redirects=True)
        assert r.status_code == 200
        with db.ro() as c:
            assert c.execute("SELECT 1 FROM users WHERE phone='9555000001'").fetchone()
        r = adm.post("/admin/pairs/bulk_upload", data={"csrf": tok, "language_id": str(lid)},
                     files={"file": ("p.csv", b"agent_a,agent_b,availability\nNew Person,Ramesh G,Night\n", "text/csv")}, follow_redirects=True)
        assert r.status_code == 200
        with db.ro() as c:
            assert c.execute("SELECT COUNT(*) n FROM pairs WHERE availability='Night'").fetchone()["n"] == 1
        for kind in ("agents", "pairs"):
            t = adm.get(f"/admin/template/{kind}.csv")
            assert t.status_code == 200 and t.text.splitlines()[0].startswith(("name", "agent_a"))
        page = adm.get("/admin/agents?lang=all").text
        assert "Import Agents" in page and "Google Sheet link" in page and "availability" in page
        pp = adm.get("/admin/pairs?lang=all").text
        assert "assignPreview" in pp and "agent_a" in pp


def test_proof_photo_is_private_and_survives_local_wipe(monkeypatch):
    png = io.BytesIO()
    from PIL import Image
    Image.new("RGB", (400, 300), (10, 120, 200)).save(png, "PNG")
    data = png.getvalue()
    cloud = {}

    class R:
        def __init__(self, code, content=b"", js=None):
            self.status_code, self.content, self._js, self.text = code, content, js or {}, ""

        def json(self):
            return self._js

    def fake_post(url, **kw):
        if "/object/sign/" in url:
            return R(200, js={"signedURL": "/object/sign/proofs/x?token=abc"})
        if "/storage/v1/bucket" in url:
            return R(200)
        cloud[url.rsplit("/", 1)[1]] = kw["content"]
        return R(200)

    def fake_get(url, **kw):
        if "/storage/v1/bucket/" in url:
            return R(200)
        name = url.rsplit("/", 1)[1]
        return R(200, cloud[name]) if name in cloud else R(404)

    monkeypatch.setenv("SUPABASE_URL", "https://x.supabase.co")
    monkeypatch.setenv("SUPABASE_KEY", "k")
    monkeypatch.setattr(storage.httpx, "post", fake_post)
    monkeypatch.setattr(storage.httpx, "get", fake_get)
    storage._bucket_ready.clear()

    with TestClient(app) as adm:
        ok, msg = storage.save_proof("proof_1_1.png", data)
        assert ok, msg
        assert "proof_1_1.png" in cloud
        os.remove(os.path.join(storage.uploads_dir(), "proof_1_1.png"))        # simulate a Render redeploy
        assert storage.get_proof("proof_1_1.png") == data                       # restored from Supabase
        assert storage.signed_url("proof_1_1.png").startswith("https://x.supabase.co/storage/v1/object/sign/")

        # anonymous users cannot fetch proofs; admins can
        anon = TestClient(app)
        r = anon.get("/uploads/proof_1_1.png", follow_redirects=False)
        assert r.status_code in (303, 307, 401, 403) and r.content != data
        adm.post("/login", data={"code": ADMIN_CODE})
        r = adm.get("/uploads/proof_1_1.png")
        assert r.status_code == 200 and r.content == data and r.headers["content-type"] == "image/png"
        os.remove(os.path.join(storage.uploads_dir(), "proof_1_1.png"))
        cloud.clear()
        assert adm.get("/uploads/proof_1_1.png").status_code == 404

        # Excel report embeds the picture inside the cell
        cloud["proof_9_9.png"] = data
        with db.tx() as c:
            lid = _lang(c, "TE", "Telugu")
            a, _ = ops.create_user(c, ADMIN, "E1", "participant", lid, phone="9666000001")
            b, _ = ops.create_user(c, ADMIN, "E2", "participant", lid, phone="9666000002")
            pid, _, _ = ops.manual_pair(c, ADMIN, None, a, b)
            sid = c.execute("INSERT INTO scripts(code,language_id,status,seq,domain,subdomain,specialisation) VALUES('SX',?,'READY',1,'d','s','x')", (lid,)).lastrowid
            c.execute("INSERT INTO assignments(script_id,pair_id,ua,ub,status,conf_min,review_status,proof_img) VALUES(?,?,?,?,'SUBMITTED',8.0,'PENDING','proof_9_9.png')", (sid, pid, a, b))
            from app import reports_excel
            xl = reports_excel.generate_master_excel_report(c, lid)
        wb = openpyxl.load_workbook(io.BytesIO(xl))
        assert len(wb["Submissions & QC"]._images) == 1

        # google sheet gets an =IMAGE() formula with a signed URL
        from app.integrations import sheets
        with db.ro() as c:
            hdr, rows = sheets.get_submissions_data(c)
        assert any(str(r[6]).startswith('=IMAGE("https://x.supabase.co/storage/v1/object/sign/') for r in rows)


def test_no_default_passwords_and_one_time_rotation(monkeypatch, capsys):
    with TestClient(app):
        pass
    monkeypatch.delenv("SF_ADMIN_PASSWORD", raising=False)
    with db.tx() as c:
        # simulate a DB created by the old code (flag absent)
        c.execute("DELETE FROM settings WHERE k LIKE 'seed_pwd_rotated_%'")
        ops.ensure_core_system(c)
    out = capsys.readouterr().out
    m = re.search(r"New one-time admin password: (\S+)", out)
    assert m, out
    new_pw = m.group(1)
    with db.tx() as c:
        assert ops.login_password(c, "admin@awign.com", new_pw, "d")[0]
        assert not ops.login_password(c, "admin@awign.com", "definitely-wrong", "d")[0]
        ops.ensure_core_system(c)                                   # second start: no new rotation
        assert ops.login_password(c, "admin@awign.com", new_pw, "d")[0]
    monkeypatch.setenv("SF_ADMIN_PASSWORD", "From-Env-123!")
    with db.tx() as c:
        ops.ensure_core_system(c)
        assert ops.login_password(c, "admin@awign.com", "From-Env-123!", "d")[0]
        assert not ops.login_password(c, "admin@awign.com", new_pw, "d")[0]


def test_repo_has_no_published_credentials():
    root = os.path.dirname(os.path.dirname(__file__))
    bad = []
    for base, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x not in ("scripts_library", ".git", "__pycache__", ".pytest_cache")]
        for f in files:
            if f.endswith((".py", ".md", ".yaml", ".yml", ".sh", ".txt", ".html")) and f != "test_round4.py":
                t = open(os.path.join(base, f), errors="ignore").read()
                if re.search(r"Awign#|\bAdmin@20\d\d|\bDeveloper@20\d\d", t):
                    bad.append(f)
    assert not bad, bad


def test_same_code_works_on_multiple_devices():
    with TestClient(app):
        with db.tx() as c:
            lid = _lang(c, "HI", "Hindi")
            uid, code = ops.create_user(c, ADMIN, "Multi", "participant", lid, phone="9777000001")
        with db.tx() as c:
            t1 = ops.login(c, code, "device-A")[0]
        with db.tx() as c:
            t2 = ops.login(c, code, "device-B")[0]
        with db.tx() as c:
            t3 = ops.login(c, code, "device-C")[0]
        assert t1 and t2 and t3 and len({t1, t2, t3}) == 3
        with db.ro() as c:
            assert ops.get_session(c, t1)[0] and ops.get_session(c, t2)[0]   # earlier devices stay logged in
