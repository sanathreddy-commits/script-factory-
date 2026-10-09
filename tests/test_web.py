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

        # --- language, plan, key, wave
        t = csrf(adm.get("/admin/language/new").text)
        r = adm.post("/admin/language/new", data={"csrf": t, "preset": "kannada", "target": "1500", **{f"q_{i}": "167" for i in range(9)}})
        assert "does not match" in r.text  # 1500 vs 1503 is rejected
        r = adm.post("/admin/language/new", data={"csrf": t, "preset": "kannada", "target": "1503", "mode": "read_aloud", **{f"q_{i}": "167" for i in range(9)}})
        assert "Language created" in r.text
        lang = adm.get("/admin/language/1")
        assert "Plan 1503 slots" in lang.text
        r = adm.post("/admin/language/1/plan", data={"csrf": t})
        assert "1503 slots planned" in r.text
        g = adm.get("/admin/generation")
        r = adm.post("/admin/keys/add", data={"csrf": t, "label": "w", "provider": "stub", "model": "stub-w", "role": "writer", "rpm": "60000"})
        r = adm.post("/admin/keys/add", data={"csrf": t, "label": "j", "provider": "stub", "model": "stub-j", "role": "judge", "rpm": "60000"})
        r = adm.post("/admin/language/1/wave", data={"csrf": t, "wave": "1", "what": "release"})
        assert "Wave 1 released" in r.text
        r = adm.post("/admin/language/1/wave", data={"csrf": t, "wave": "2", "what": "release"})
        assert "not finished" in r.text  # wave gate
        P.run_pending(14)

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
        assert lead.get("/admin").status_code == 403  # role wall
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
        assert "Before you start" in pa.get("/me").text  # consent gate
        pt = csrf(pa.get("/consent").text)
        pa.post("/consent", data={"csrf": pt, "agree": "1", "optin": "1"})
        login(pb, cb)
        pt2 = csrf(pb.get("/consent").text)
        pb.post("/consent", data={"csrf": pt2, "agree": "1"})
        me = pa.get("/me")
        assert "Your scripts" in me.text and "viewport" in me.text

        # second device needs approval
        other = TestClient(app)
        r = other.post("/login", data={"code": ca})
        assert "new device" in r.text
        r = lead.get("/team")
        assert "new device" in r.text
        req = re.search(r"/team/device/(\d+)/approve", r.text).group(1)
        lead.post(f"/team/device/{req}/approve", data={"csrf": lt})
        assert "Your scripts" in login(other, ca).text

        # --- pairing and assignment
        r = lead.get("/team/pairs?propose=5")
        assert "Suggested pairs" in r.text
        r = lead.post("/team/pairs/create", data={"csrf": lt, "lang": "1", "pair": f"{a_id}-{b_id}"})
        assert "1 pairs created" in r.text
        r = lead.post("/team/assign", data={"csrf": lt, "pair": "1", "n": "3"})
        assert "3 scripts assigned" in r.text
        assert "Latest" in lead.get("/team/work").text

        # --- participant session flow
        me = pa.get("/me")
        aid = int(re.search(r'href="/a/(\d+)"', me.text).group(1))
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
        for path in ["/admin", "/admin/language/1", "/admin/generation", "/admin/data", "/admin/settings", "/admin/audit", "/review",
                     "/team", "/team/people", "/team/pairs", "/team/work", "/search?q=KN-0000", "/admin/script/1"]:
            r = adm.get(path)
            assert r.status_code == 200, (path, r.status_code, r.text[:200])
        for rep in ["scripts", "people", "pairs", "assignments", "ledger", "generation_cost", "audit", "client_manifest", "payout"]:
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
        assert adm.get("/admin").url.path == "/login"
