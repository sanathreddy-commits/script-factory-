"""Smoke tests for the admin/supervisor pages after the pairing, KPI and multi-language changes."""
import os
import re
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "p.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)
ADMIN_CODE = os.environ["SF_ADMIN_CODE"]

from fastapi.testclient import TestClient  # noqa: E402

from app import db, ops  # noqa: E402
from app.main import app  # noqa: E402


def _login(client, code):
    r = client.post("/login", data={"code": code})
    assert r.status_code == 200, r.text[:300]


def test_pages_and_multilanguage_supervisor():
    with TestClient(app) as adm:
        with db.tx() as c:
            ADMIN = {"role": "admin", "id": 0, "name": "t", "language_id": None}
            now = 1.0
            lids = []
            for code, name in (("AA", "Alpha"), ("BB", "Beta")):
                cur = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES(?,?,0,?)", (code, name, now))
                lids.append(cur.lastrowid)
            ppl = {}
            for i, lid in enumerate(lids):
                for k in range(3):
                    uid, _ = ops.create_user(c, ADMIN, f"P{i}{k}", "participant", lid, phone=f"90000{i}{k}001")
                    ppl[(i, k)] = uid
            sup_id, sup_code = ops.create_user(c, ADMIN, "Sup", "lead", lids[0], phone="9555500000")
            ops.set_supervisor_languages(c, ADMIN, sup_id, lids)
            # one person in two manual pairs
            ops.manual_pair(c, ADMIN, None, ppl[(0, 0)], ppl[(0, 1)])
            ops.manual_pair(c, ADMIN, None, ppl[(0, 0)], ppl[(0, 2)])
            assert len(ops.active_pairs_for(c, ppl[(0, 0)])) == 2
        _login(adm, ADMIN_CODE)
        for url in ("/admin", "/admin?lang_id=%d" % lids[0], "/admin/agents?lang=all", "/admin/agents?lang=%d" % lids[1],
                    "/admin/pairs?lang=all", "/admin/assign?lang=%d" % lids[0], "/admin/reports?lang=all", "/admin/scripts?lang=all",
                    "/admin/kpi_detail?kind=assigned&lang_id=all", "/admin/kpi_detail?kind=total&lang_id=%d" % lids[0]):
            r = adm.get(url)
            assert r.status_code == 200, (url, r.status_code, r.text[:300])
        r = adm.get("/admin/agents?lang=%d" % lids[0])
        assert "2 pairs" in r.text  # pair count badge for the person in two pairs

    with TestClient(app) as sup:
        _login(sup, sup_code)
        for url in ("/admin", "/admin/agents", "/admin/agents?lang=all", "/admin/pairs", "/admin/assign?lang=%d" % lids[1]):
            r = sup.get(url)
            assert r.status_code == 200, (url, r.status_code, r.text[:300])
        r = sup.get("/admin/agents?lang=all")
        assert "Alpha" in r.text and "Beta" in r.text


def test_agent_portal_multi_partner_view_as_and_client_urls():
    with TestClient(app) as adm:
        with db.tx() as c:
            ADMIN = {"role": "admin", "id": 0, "name": "t", "language_id": None}
            cur = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES('CC','Gamma',0,1.0)")
            lid = cur.lastrowid
            ids = []
            for k in range(3):
                uid, code = ops.create_user(c, ADMIN, f"G{k}", "participant", lid, phone=f"91111100{k}9")
                ops.accept_consent(c, uid, False, "v")
                ids.append((uid, code))
            p1, _, _ = ops.manual_pair(c, ADMIN, None, ids[0][0], ids[1][0])
            p2, _, _ = ops.manual_pair(c, ADMIN, None, ids[0][0], ids[2][0])
        _login(adm, ADMIN_CODE)
        r = adm.post("/admin/supervisors/add", data={"csrf": "x", "name": "Multi", "language_ids": [str(lid)], "phone": "9444400000"})
        # two links for the same language
        t = re.search(r'name="csrf" value="([^"]+)"', adm.get("/admin/client_access").text).group(1)
        for n in (1, 2):
            adm.post("/admin/client_access/add", data={"csrf": t, "taxonomy": f"HC {n}", "url": f"https://x{n}.example.com", "code": f"C{n}", "language_id": str(lid)})
        page = adm.get(f"/admin/client_access?lang={lid}").text
        assert "x1.example.com" in page and "x2.example.com" in page
        assert adm.get("/admin/view-as-agent").status_code == 200

    with TestClient(app) as ag:
        _login(ag, ids[0][1])
        r = ag.get("/agent")
        assert r.status_code == 200
        assert "G1" in r.text and "G2" in r.text  # both partners shown
        assert "x1.example.com" in r.text and "x2.example.com" in r.text
        assert ag.get(f"/agent?tab=scripts&pair={p1}").status_code == 200
