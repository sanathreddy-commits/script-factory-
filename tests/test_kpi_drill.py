"""Every KPI card drills down; admin and supervisor see the same thing (supervisor scoped to own languages)."""
import os
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "k.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)
ADMIN_CODE = os.environ["SF_ADMIN_CODE"]

from fastapi.testclient import TestClient  # noqa: E402

from app import db, ops  # noqa: E402
from app.main import app  # noqa: E402

KINDS = ["ag_total", "ag_active", "ag_inactive", "ag_paired", "ag_unpaired", "pr_total", "pr_active", "pr_limit",
         "sc_total", "sc_available", "sc_assigned", "sc_pending", "sc_completed", "sc_accepted", "sc_rejected",
         "sc_recorded", "sc_review_pending"]


def test_kpi_rows_all_kinds_and_scoping():
    with TestClient(app) as adm:
        with db.tx() as c:
            ADMIN = {"role": "admin", "id": 0, "name": "t", "language_id": None}
            lids = [c.execute("INSERT INTO languages(code,name,target,created_at) VALUES(?,?,0,1.0)", (k, n)).lastrowid
                    for k, n in (("AA", "Alpha"), ("BB", "Beta"))]
            ppl = {}
            for i, lid in enumerate(lids):
                for k in range(3):
                    ppl[(i, k)], _ = ops.create_user(c, ADMIN, f"P{i}{k}", "participant", lid, phone=f"90000{i}{k}001")
            sup_id, sup_code = ops.create_user(c, ADMIN, "Sup", "lead", lids[0], phone="9555500000")
            ops.set_supervisor_languages(c, ADMIN, sup_id, [lids[0]])
            ops.manual_pair(c, ADMIN, None, ppl[(0, 0)], ppl[(0, 1)])
            ops.manual_pair(c, ADMIN, None, ppl[(1, 0)], ppl[(1, 1)])
        assert adm.post("/login", data={"code": ADMIN_CODE}).status_code == 200
        for k in KINDS:
            r = adm.get(f"/admin/kpi_rows?kind={k}&lang_id=all")
            assert r.status_code == 200, (k, r.text[:200])
            d = r.json()
            assert d["columns"], k
            assert all(len(row) == len(d["columns"]) for row in d["rows"]), k
        a = adm.get("/admin/kpi_rows?kind=ag_total&lang_id=all").json()["rows"]
        assert len(a) == 6
        assert len(adm.get("/admin/kpi_rows?kind=ag_paired&lang_id=all").json()["rows"]) == 4
        assert len(adm.get("/admin/kpi_rows?kind=ag_unpaired&lang_id=all").json()["rows"]) == 2
        assert len(adm.get("/admin/kpi_rows?kind=pr_total&lang_id=all").json()["rows"]) == 2
        assert len(adm.get(f"/admin/kpi_rows?kind=pr_total&lang_id={lids[1]}").json()["rows"]) == 1
        # Pairs & Assignment page renders every clickable card
        html = adm.get("/admin/pairs?lang=all").text
        for k in KINDS[3:9] + ["sc_pending", "sc_accepted"]:
            assert f'data-kpi="{k}"' in html, k
        assert adm.get("/admin/kpi_rows?kind=bogus").json()["rows"] == []

    with TestClient(app) as sup:
        r = sup.post("/login", data={"code": sup_code}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/admin"  # same dashboard as admin
        assert len(sup.get("/admin/kpi_rows?kind=ag_total&lang_id=all").json()["rows"]) == 3  # own language only
        assert sup.get(f"/admin/kpi_rows?kind=ag_total&lang_id={lids[1]}").json()["rows"] == []  # not theirs
        assert 'id="kpiModal"' in sup.get("/admin").text
