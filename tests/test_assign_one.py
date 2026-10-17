"""Scripts page > Assign: give one specific script to a chosen pair of the same language."""
import os
import re
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "a1.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)
ADMIN_CODE = os.environ["SF_ADMIN_CODE"]

from fastapi.testclient import TestClient  # noqa: E402

from app import db, ops  # noqa: E402
from app.main import app  # noqa: E402

A = {"role": "admin", "id": 0, "name": "t", "language_id": None}


def _csrf(client, url):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def _lang(c, code):
    lid = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES(?,?,0,1.0)", (code, f"L{code}")).lastrowid
    ids = [ops.create_user(c, A, f"{code}{j}", "participant", lid, phone=f"9{ord(code[0])}{j:07d}"[:10])[0] for j in range(4)]
    ops.manual_pair(c, A, lid, ids[0], ids[1])
    ops.manual_pair(c, A, lid, ids[2], ids[3])
    pids = [r["id"] for r in c.execute("SELECT id FROM pairs WHERE language_id=? ORDER BY id", (lid,)).fetchall()]
    for i in range(3):
        c.execute("INSERT INTO scripts(code,language_id,seq,domain,subdomain,specialisation,status,version,created_at,updated_at) "
                  "VALUES(?,?,?,'Healthcare','Sub','Spec','READY',1,1.0,1.0)", (f"{code}S{i}", lid, i))
    return lid, pids


def test_assign_specific_script_to_chosen_pair():
    with TestClient(app, raise_server_exceptions=False) as adm:
        with db.tx() as c:
            lid, pids = _lang(c, "AX")
            other_lid, other_pids = _lang(c, "BY")
            sup_code = ops.create_user(c, A, "Sup", "lead", lid, phone="9555500011")[1]
            sup_id = c.execute("SELECT id FROM users WHERE code=?", (sup_code,)).fetchone()["id"]
            ops.set_supervisor_languages(c, A, sup_id, [lid])
        adm.post("/login", data={"code": ADMIN_CODE})
        page = adm.get(f"/admin/scripts?lang={lid}").text
        assert "sfOpenAssign(" in page and "<th>Assign</th>" in page
        tok = _csrf(adm, f"/admin/scripts?lang={lid}")

        d = adm.get("/admin/scripts/pairs_for?code=AXS2").json()
        assert [p["id"] for p in d["pairs"]] and {p["id"] for p in d["pairs"]} == set(pids)   # only this language
        assert d["already_assigned"] is False

        # the chosen script (not the first by seq) goes to the chosen pair
        r = adm.post("/admin/scripts/assign_one", data={"csrf": tok, "code": "AXS2", "pair_id": str(pids[1])})
        assert r.status_code == 200 and r.json()["ok"], r.text
        with db.ro() as c:
            row = c.execute("SELECT a.pair_id, a.status FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE s.code='AXS2'").fetchone()
            assert row["pair_id"] == pids[1] and row["status"] == "ASSIGNED"
            assert c.execute("SELECT COUNT(*) n FROM assignments").fetchone()["n"] == 1
        d = adm.get("/admin/scripts/pairs_for?code=AXS2").json()
        assert d["already_assigned"] is True
        # Assigned / Pending / Completed must equal what the Pairs page shows for each pair
        with db.tx() as c:   # one finished recording for pair 0
            pa = c.execute("SELECT a, b FROM pairs WHERE id=?", (pids[0],)).fetchone()
            sid0 = c.execute("SELECT id FROM scripts WHERE code='AXS1'").fetchone()["id"]
            c.execute("INSERT INTO assignments(script_id,version,pair_id,ua,ub,status,conf_min,review_status,updated_at) "
                      "VALUES(?,1,?,?,?,'SUBMITTED',8,'PENDING',1.0)", (sid0, pids[0], pa["a"], pa["b"]))
        counts = {p["id"]: (p["assigned"], p["pending"], p["completed"]) for p in adm.get("/admin/scripts/pairs_for?code=AXS0").json()["pairs"]}
        assert counts[pids[1]] == (1, 1, 0) and counts[pids[0]] == (1, 0, 1)
        from app.routes_master import build_management_context

        class _Req:
            query_params = {"lang": str(lid)}
            cookies = {}
        with db.ro() as c:
            admin_u = dict(c.execute("SELECT * FROM users WHERE role='admin' ORDER BY id LIMIT 1").fetchone())
            admin_u["lang_ids"] = []
            ctx = build_management_context(c, admin_u, _Req(), active_tab="pairs")
        page_counts = {p["id"]: (p["assigned_count"], p["pending_count"], p["completed_count"]) for p in ctx["pairs"]}
        assert page_counts == {k: v for k, v in counts.items() if k in page_counts}

        # same script again -> refused, nothing new
        r = adm.post("/admin/scripts/assign_one", data={"csrf": tok, "code": "AXS2", "pair_id": str(pids[0])})
        assert r.status_code == 400 and "not available" in r.json()["error"]
        # pair from another language -> refused
        r = adm.post("/admin/scripts/assign_one", data={"csrf": tok, "code": "AXS0", "pair_id": str(other_pids[0])})
        assert r.status_code == 400 and "language" in r.json()["error"]
        # missing csrf -> refused
        assert adm.post("/admin/scripts/assign_one", data={"code": "AXS0", "pair_id": str(pids[0])}).status_code == 400
        with db.ro() as c:   # the manual one + the finished recording added above; refused attempts added nothing
            assert c.execute("SELECT COUNT(*) n FROM assignments").fetchone()["n"] == 2

    with TestClient(app, raise_server_exceptions=False) as sup:
        sup.post("/login", data={"code": sup_code})
        stok = _csrf(sup, f"/admin/scripts?lang={lid}")
        assert sup.get("/admin/scripts/pairs_for?code=BYS0").status_code == 403
        r = sup.post("/admin/scripts/assign_one", data={"csrf": stok, "code": "AXS0", "pair_id": str(pids[0])})
        assert r.status_code == 200 and r.json()["ok"], r.text
