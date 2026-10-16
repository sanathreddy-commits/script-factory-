"""Script deletion: single, multiple, folder (taxonomy) and whole language; busy scripts are kept."""
import os
import re
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "del.db")
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


def _add(c, lid, code, domain, sub, spec="GP"):
    return c.execute("INSERT INTO scripts(code,language_id,domain,subdomain,specialisation,status,version,created_at,updated_at) "
                     "VALUES(?,?,?,?,?,'READY',1,1.0,1.0)", (code, lid, domain, sub, spec)).lastrowid


def _codes(c, lid):
    return {r["code"] for r in c.execute("SELECT code FROM scripts WHERE language_id=?", (lid,)).fetchall()}


def test_delete_single_multiple_folder_language_and_keeps_busy_scripts():
    with TestClient(app, raise_server_exceptions=False):  # startup creates the tables
        pass
    with db.tx() as c:
        lid = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES('DX','Delang',0,1.0)").lastrowid
        other = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES('DY','Otherlang',0,1.0)").lastrowid
        for i in range(4):
            _add(c, lid, f"DXHC{i:03d}", "Healthcare", "Doctor-Patient")
        for i in range(3):
            _add(c, lid, f"DXBK{i:03d}", "Banking", "Loans", "Home")
        busy = _add(c, lid, "DXHC900", "Healthcare", "Doctor-Patient")
        recorded = _add(c, lid, "DXHC901", "Healthcare", "Doctor-Patient")
        released = _add(c, lid, "DXHC902", "Healthcare", "Doctor-Patient")
        c.execute("INSERT INTO script_versions(script_id,version,turns,words,created_at) VALUES(?,1,'[]',0,1.0)", (released,))
        c.execute("INSERT INTO assignments(script_id,version,status,assigned_at) VALUES(?,1,'ASSIGNED',1.0)", (busy,))
        aid = c.execute("INSERT INTO assignments(script_id,version,status,assigned_at) VALUES(?,1,'RELEASED',1.0)", (recorded,)).lastrowid
        c.execute("INSERT INTO ledger(ts,assignment_id,minutes,kind) VALUES(1.0,?,5,'RECORDED')", (aid,))
        c.execute("INSERT INTO assignments(script_id,version,status,assigned_at) VALUES(?,1,'RELEASED',1.0)", (released,))
        _add(c, other, "DYHC000", "Healthcare", "Doctor-Patient")
        sup_id, sup_code = ops.create_user(c, A, "Sup", "lead", lid, phone="9555500077")
        ops.set_supervisor_languages(c, A, sup_id, [lid])

    with TestClient(app, raise_server_exceptions=False) as adm:
        adm.post("/login", data={"code": ADMIN_CODE})
        html = adm.get(f"/admin/scripts?lang={lid}").text
        assert 'class="sf-sel"' in html and "sfDelSelected" in html
        tok = _csrf(adm, f"/admin/scripts?lang={lid}")
        tree = adm.get(f"/admin/scripts/tree?lang={lid}").json()["items"]
        assert all(it["lang_id"] == lid for it in tree)

        # no csrf -> refused
        assert adm.post("/admin/scripts/delete", data={"codes": "DXHC000"}).status_code == 400

        # single, dry run first: nothing removed
        r = adm.post("/admin/scripts/delete", data={"csrf": tok, "codes": "DXHC000", "dry_run": "1"}).json()
        assert r["deleted"] == 1 and r["dry_run"] is True
        with db.ro() as c:
            assert "DXHC000" in _codes(c, lid)
        r = adm.post("/admin/scripts/delete", data={"csrf": tok, "codes": "DXHC000"}).json()
        assert r["deleted"] == 1 and r["codes"] == ["DXHC000"]

        # multiple, including a busy one which is kept
        r = adm.post("/admin/scripts/delete", data={"csrf": tok, "codes": "DXHC001,DXHC002 DXHC900"}).json()
        assert r["deleted"] == 2 and [s["code"] for s in r["skipped"]] == ["DXHC900"]

        # folder (sub-domain) scope: recorded script kept, released-only script removed with its history
        r = adm.post("/admin/scripts/delete", data={"csrf": tok, "lang_id": str(lid), "domain": "Healthcare",
                                                    "sub": "Doctor-Patient"}).json()
        assert r["deleted"] == 2  # DXHC003 + DXHC902
        assert {s["code"] for s in r["skipped"]} == {"DXHC900", "DXHC901"}
        with db.ro() as c:
            assert c.execute("SELECT COUNT(*) n FROM assignments WHERE script_id=?", (released,)).fetchone()["n"] == 0
            assert c.execute("SELECT COUNT(*) n FROM script_versions WHERE script_id=?", (released,)).fetchone()["n"] == 0
            assert c.execute("SELECT COUNT(*) n FROM ledger WHERE assignment_id=?", (aid,)).fetchone()["n"] == 1

    # supervisors cannot delete
    with TestClient(app, raise_server_exceptions=False) as sup:
        sup.post("/login", data={"code": sup_code})
        stok = _csrf(sup, f"/admin/scripts?lang={lid}")
        page = sup.get(f"/admin/scripts?lang={lid}").text
        assert 'class="sf-sel"' not in page and 'id="sfDelHere"' not in page
        r = sup.post("/admin/scripts/delete", data={"csrf": stok, "codes": "DXBK000"})
        assert r.status_code == 403

    with TestClient(app, raise_server_exceptions=False) as adm:
        adm.post("/login", data={"code": ADMIN_CODE})
        tok = _csrf(adm, f"/admin/scripts?lang={lid}")
        # whole language: only busy/recorded remain, other language untouched
        r = adm.post("/admin/scripts/delete", data={"csrf": tok, "lang_id": str(lid)}).json()
        assert r["deleted"] == 3
        with db.ro() as c:
            assert _codes(c, lid) == {"DXHC900", "DXHC901"}
            assert _codes(c, other) == {"DYHC000"}
            assert c.execute("SELECT COUNT(*) n FROM audit WHERE action='delete_scripts'").fetchone()["n"] == 4
        assert adm.post("/admin/scripts/delete", data={"csrf": tok, "codes": "NOPE123"}).status_code == 404
