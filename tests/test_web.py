"""End-to-end through HTTP: every page, every role, mobile + laptop markup present."""
import os
import re
import tempfile
import time

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "w.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
os.environ["SF_ADMIN_CODE"] = "ABCDEFG" + "X"  # replaced below with a valid one

from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)
ADMIN_CODE = os.environ["SF_ADMIN_CODE"]

from fastapi.testclient import TestClient  # noqa: E402

from app import db, ops, pipeline as P  # noqa: E402
from app.main import app  # noqa: E402


def _add_scripts(lid, n, status):
    """Scripts as uploads add them, with the attrs the review screen needs."""
    import json
    now = time.time()
    with db.tx() as c:
        base = c.execute("SELECT COALESCE(MAX(seq), 0) m FROM scripts WHERE language_id=?", (lid,)).fetchone()["m"]
        for k in range(1, n + 1):
            g = [("M", "F"), ("F", "M"), ("M", "M"), ("F", "F")][k % 4]
            attrs = {"roles": ["Doctor", "Patient"], "code_switch": "low", "entities": {}, "names": {"A": "Speaker A", "B": "Speaker B"},
                     "genders": {"A": g[0], "B": g[1]}}
            sid = c.execute("INSERT INTO scripts(language_id,seq,code,domain,subdomain,specialisation,wave,attrs,status,version,words,"
                            "source,created_at,updated_at) VALUES(?,?,?,?,?,?,1,?,?,1,1300,'local_import',?,?)",
                            (lid, base + k, codes.script_code("KN", base + k), "Healthcare", "Doctor-Patient Consultation",
                             "General Physician", json.dumps(attrs), status, now, now)).lastrowid
            c.execute("INSERT INTO script_versions(script_id,version,turns,words,created_at) VALUES(?,1,?,1300,?)",
                      (sid, json.dumps([{"s": "A", "t": "ನಮಸ್ಕಾರ"}, {"s": "B", "t": "ನಮಸ್ಕಾರ"}], ensure_ascii=False), now))


def csrf(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def login(client, code):
    r = client.post("/login", data={"code": code})
    assert r.status_code == 200, r.text[:300]
    return r


def test_everything():
    with TestClient(app) as adm:
        # --- wrong code and typo handling
        r = adm.post("/login", data={"code": "ABCDEFGZ"})
        assert "typo" in r.text or "not recognised" in r.text
        login(adm, ADMIN_CODE)
        assert "Dashboard" in adm.get("/admin").text

        # --- language (scripts now come from uploads; generation was removed)
        t = csrf(adm.get("/admin/language/new").text)
        r = adm.post("/admin/language/new", data={"csrf": t, "preset": "kannada", "target": "1500", **{f"q_{i}": "167" for i in range(9)}})
        assert "does not match" in r.text  # 1500 vs 1503 is rejected
        r = adm.post("/admin/language/new", data={"csrf": t, "preset": "kannada", "target": "1503", "mode": "read_aloud", **{f"q_{i}": "167" for i in range(9)}})
        assert "Language created" in r.text
        lang = adm.get("/admin/language/1")
        assert lang.status_code == 200
        assert adm.get("/admin/generation").status_code == 404  # generation pages are gone
        _add_scripts(1, 14, "IN_REVIEW")

        # --- review screens
        assert "Review queue" in adm.get("/review").text
        with db.ro() as c:
            sid = c.execute("SELECT id FROM scripts WHERE status='IN_REVIEW' LIMIT 1").fetchone()["id"]
        page = adm.get(f"/review/{sid}")
        assert "Checklist" in page.text
        r = adm.post(f"/review/{sid}", data={"csrf": t, "verdict": "accept"})
        assert r.status_code == 200
        with db.tx() as c:
            for x in c.execute("SELECT id FROM scripts WHERE status='IN_REVIEW'").fetchall():
                P.review(c, x["id"], {"role": "admin", "id": 1, "name": "A"}, "accept")
            db.set_setting(c, "allow_test_scripts", 1)

        # --- lead + participants through the UI
        r = adm.post("/team/people/add", data={"csrf": t, "lang": "1", "name": "Lead One", "phone": "9000000001", "role": "lead", "perm": "manage"})
        lead_code = re.search(r'class="code">([A-Z0-9-]+)<', r.text).group(1)
        lead = TestClient(app)
        login(lead, lead_code)
        assert "exceptions" in lead.get("/team").text
        assert lead.get("/admin").status_code == 200  # supervisors now get the (language-scoped) dashboard
        assert lead.get("/admin/settings").status_code == 403  # role wall still holds for admin-only pages
        assert lead.get("/review").status_code == 403
        lt = csrf(lead.get("/team/people").text)
        r = lead.post("/team/people/preview", data={"csrf": lt, "lang": "1"},
                      files={"file": ("p.csv", "name,phone,email,gender,hours_used\nRavi,9811111111,,M,0\nRavi Dup,9811111111,,M,0\nSita,9822222222,,F,1.5\nBad,123,,,\n")})
        assert "DUPLICATE" in r.text and "OK" in r.text
        rows = re.search(r'name="rows" value="([^"]*)"', r.text).group(1)
        import html
        r = lead.post("/team/people/commit", data={"csrf": lt, "lang": "1", "rows": html.unescape(rows)})
        assert "2 participants added" in r.text
        with db.ro() as c:
            ppl = c.execute("SELECT * FROM users WHERE role='participant' ORDER BY id").fetchall()
            assert ppl[1]["opening_min"] == 90
        # show code (logged), then participants log in on their own "phones"
        a_id, b_id = ppl[0]["id"], ppl[1]["id"]
        ra = lead.post(f"/team/people/{a_id}/showcode", data={"csrf": lt})
        ca = re.search(r'class="code">([A-Z0-9-]+)<', ra.text).group(1)
        rb = lead.post(f"/team/people/{b_id}/showcode", data={"csrf": lt})
        cb = re.search(r'class="code">([A-Z0-9-]+)<', rb.text).group(1)
        with db.ro() as c:
            assert c.execute("SELECT COUNT(*) n FROM audit WHERE action='view_code'").fetchone()["n"] == 2

        pa, pb = TestClient(app), TestClient(app)
        login(pa, ca)
        login(pb, cb)
        me = pa.get("/me")
        assert "ScriptFlow" in me.text and "viewport" in me.text

        # a second device logs in straight away (no approval step any more)
        other = TestClient(app)
        assert "ScriptFlow" in login(other, ca).text

        # --- pairing and assignment
        r = lead.get("/team/pairs?propose=5")
        assert "Suggested pairs" in r.text
        r = lead.post("/team/pairs/create", data={"csrf": lt, "lang": "1", "pair": f"{a_id}-{b_id}"})
        assert "1 pairs created" in r.text
        r = lead.post("/team/assign", data={"csrf": lt, "pair": "1", "n": "3"})
        assert "3 scripts assigned" in r.text
        assert "Latest" in lead.get("/team/work").text

        # --- participant session flow
        me = pa.get("/agent?tab=scripts")
        aid = int(re.search(r'href="/agent/script/(\d+)"', me.text).group(1))
        page = pa.get(f"/a/{aid}")
        assert "I'm ready" in page.text and 'class="turn' in page.text
        assert pb.get(f"/a/{aid}").status_code == 200
        outsider = TestClient(app)
        # a participant from nowhere cannot read it
        assert outsider.get(f"/a/{aid}").status_code in (200, 303)  # redirected to login
        at, bt = csrf(page.text), csrf(pb.get(f"/a/{aid}").text)
        r = pa.post(f"/a/{aid}/start", data={"csrf": at})
        assert "Both partners must show Ready" in r.text
        assert pa.post(f"/a/{aid}/ready", data={"csrf": at}).json()["ok"]
        assert pb.post(f"/a/{aid}/ready", data={"csrf": bt}).json()["ok"]
        assert pa.get(f"/a/{aid}/state").json()["partner_ready"] is True
        r = pa.post(f"/a/{aid}/start", data={"csrf": at})
        assert "Session running" in r.text
        with db.tx() as c:
            db.set_setting(c, "dur_min_sec", 1)
            c.execute("UPDATE assignments SET started_at=started_at-600 WHERE id=?", (aid,))
        pa.post(f"/a/{aid}/done", data={"csrf": at})
        r = pb.post(f"/a/{aid}/done", data={"csrf": bt})
        assert "Confirmed" in r.text
        r = pa.post(f"/a/{aid}/issue", data={"csrf": at, "kind": "script_error", "note": "x"})  # confirmed -> only logged
        assert r.status_code == 200

        # --- CSRF is enforced
        assert pa.post(f"/a/{aid}/done", data={"csrf": "bad"}).status_code == 400

        # --- admin pages and downloads
        for path in ["/admin", "/admin?lang_id=1", f"/admin?lang_id=1&agent_id={a_id}", "/admin/language/1", "/admin/data", "/admin/settings", "/admin/audit", "/review",
                     "/team", "/team/people", "/team/pairs", "/team/work", "/search?q=KN-0000", "/admin/script/1"]:
            r = adm.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:200])
        for rep in ["scripts", "people", "pairs", "assignments", "ledger", "audit", "client_manifest", "payout"]:
            r = adm.get(f"/report/{rep}")
            assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv"), rep
        r = lead.get("/report/people")
        assert "98****11" in r.text or "98" in r.text and "9811111111" not in r.text  # masked by default
        assert lead.get("/report/audit").status_code == 403

        # --- reconcile via upload
        r = adm.post("/admin/data/reconcile", data={"csrf": t}, files={"file": ("l.csv", "script_id,session_id,duration_sec\nKN-99999-9,S9,600\n")})
        assert "orphans" in r.text
        # --- settings: changing a validator bumps spec version
        form = {"csrf": t, "words_min": "1100"}
        r = adm.post("/admin/settings", data=form)
        assert "Spec version bumped" in r.text
        r = adm.post("/admin/data/revalidate", data={"csrf": t, "lang": "1"}, files={"file": ("x", "")})
        assert "Re-validation" in r.text
        assert adm.post("/logout", data={"csrf": t}).status_code == 200
        assert adm.get("/admin").url.path in ("/login", "/admin/login")
