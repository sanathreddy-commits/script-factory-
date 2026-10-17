"""Page speed guard. Production talks to Supabase over the network, so every SQL query is a round
trip (15-60 ms). The management pages used to run ~8 queries per agent/pair/script (494 queries for
60 agents, ~1,500 on "All languages"), which made every click take seconds.

These tests fail if a page's query count grows with the amount of data again, and check that script
text is fetched on demand instead of being embedded in every page.
"""
import os
import re
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "qb.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)
ADMIN_CODE = os.environ["SF_ADMIN_CODE"]

import json  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from app import db, ops  # noqa: E402
from app.main import app  # noqa: E402

A = {"role": "admin", "id": 0, "name": "t", "language_id": None}
PAGES = ["/admin/agents", "/admin/scripts", "/admin/pairs", "/admin/reports", "/admin/client_access"]
BUDGET = 70          # constant per page; was 494+ with 60 agents
STATUSES = ["ASSIGNED", "SUBMITTED", "ACCEPTED", "REJECTED", "RELEASED", "COMPLETED"]


def _seed(c, tag, n_agents, n_scripts):
    lid = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES(?,?,0,1.0)", (tag, f"Lang{tag}")).lastrowid
    sids = [c.execute("INSERT INTO scripts(code,language_id,domain,subdomain,specialisation,status,version,attrs,created_at,updated_at) "
                      "VALUES(?,?,?,?,?,'READY',1,?,1.0,1.0)",
                      (f"{tag}{i:04d}", lid, "Healthcare" if i % 2 else "BFSI", "Sub", "Spec",
                       json.dumps({"raw_text": f"FULLTEXT-{tag}-{i} " + "word " * 200}))).lastrowid for i in range(n_scripts)]
    ids = [ops.create_user(c, A, f"{tag} Agent {j}", "participant", lid, phone=f"9{tag}{j:07d}"[:10].ljust(10, "0"))[0]
           for j in range(n_agents)]
    k = 0
    for j in range(0, n_agents - 1, 2):
        ops.manual_pair(c, A, lid, ids[j], ids[j + 1])
        pid = c.execute("SELECT id FROM pairs ORDER BY id DESC").fetchone()["id"]
        for st in STATUSES:
            if k >= n_scripts:   # one open assignment per script (unique index)
                break
            c.execute("INSERT INTO assignments(script_id,version,pair_id,ua,ub,status,conf_min,review_status,updated_at) "
                      "VALUES(?,?,?,?,?,?,?,?,1.0)", (sids[k], 1, pid, ids[j], ids[j + 1], st, 9.0, "PENDING"))
            k += 1
    return lid


class _Count:
    def __init__(self):
        self.n = 0
        self._orig = db.connect

    def __enter__(self):
        outer = self

        class P:
            def __init__(self, c):
                self._c = c

            def execute(self, *a, **k):
                outer.n += 1
                return self._c.execute(*a, **k)

            def __getattr__(self, name):
                return getattr(self._c, name)

        db.connect = lambda: P(self._orig())
        return self

    def __exit__(self, *exc):
        db.connect = self._orig


def _counts(client, lang):
    out = {}
    for p in PAGES:
        with _Count() as k:
            r = client.get(f"{p}?lang={lang}")
        assert r.status_code == 200, (p, r.status_code)
        out[p] = (k.n, r.text)
    return out


def test_management_pages_use_a_constant_number_of_queries():
    with TestClient(app, raise_server_exceptions=False) as adm:
        with db.tx() as c:
            small = _seed(c, "QS", 4, 10)
            big = _seed(c, "QB", 40, 150)
            sup_code = ops.create_user(c, A, "Sup", "lead", big, phone="9555500077")[1]
            sup_id = c.execute("SELECT id FROM users WHERE code=?", (sup_code,)).fetchone()["id"]
            ops.set_supervisor_languages(c, A, sup_id, [big])
        adm.post("/login", data={"code": ADMIN_CODE})
        s, b, al = _counts(adm, small), _counts(adm, big), _counts(adm, "all")
        for p in PAGES:
            assert b[p][0] <= BUDGET and al[p][0] <= BUDGET, f"{p}: {b[p][0]} / {al[p][0]} queries (budget {BUDGET})"
            # 10x the agents and 15x the scripts must not add queries per row
            assert b[p][0] - s[p][0] <= 5, f"{p}: queries grow with data ({s[p][0]} -> {b[p][0]})"
            # script text is loaded by the popup, never embedded in the page
            assert "FULLTEXT-" not in al[p][1], f"{p} embeds script text"
        assert "loadScriptTextInto" in al["/admin/scripts"][1]
    with TestClient(app, raise_server_exceptions=False) as sup:
        sup.post("/login", data={"code": sup_code})
        for p, (n, _) in _counts(sup, "").items():
            assert n <= BUDGET, f"supervisor {p}: {n} queries"


def test_dashboard_kpi_people_and_export_do_not_grow_with_data():
    with TestClient(app, raise_server_exceptions=False) as adm:
        with db.tx() as c:
            small = _seed(c, "DS", 4, 10)
            big = _seed(c, "DB", 40, 150)
            agent_small = c.execute("SELECT id FROM users WHERE language_id=? AND role='participant' ORDER BY id", (small,)).fetchone()["id"]
            agent_big = c.execute("SELECT id FROM users WHERE language_id=? AND role='participant' ORDER BY id", (big,)).fetchone()["id"]
        adm.post("/login", data={"code": ADMIN_CODE})

        def n(url):
            with _Count() as k:
                r = adm.get(url)
            assert r.status_code == 200, (url, r.status_code)
            return k.n

        pairs = [
            (f"/admin?lang_id={small}", f"/admin?lang_id={big}"),
            (f"/admin?agent_id={agent_small}", f"/admin?agent_id={agent_big}"),
            (f"/admin/kpi_rows?kind=pr_total&lang_id={small}", f"/admin/kpi_rows?kind=pr_total&lang_id={big}"),
            (f"/admin/kpi_rows?kind=ag_total&lang_id={small}", f"/admin/kpi_rows?kind=ag_total&lang_id={big}"),
            (f"/team/people?lang={small}", f"/team/people?lang={big}"),
        ]
        for s_url, b_url in pairs:
            ns, nb = n(s_url), n(b_url)
            assert nb <= BUDGET and nb - ns <= 5, f"{b_url}: {ns} -> {nb} queries"
        assert n("/admin/reports/export_csv") <= 15


def test_script_text_endpoint_and_permissions():
    with TestClient(app, raise_server_exceptions=False) as adm:
        with db.tx() as c:
            mine = _seed(c, "TX", 2, 3)
            other = _seed(c, "TY", 2, 3)
            sup_code = ops.create_user(c, A, "Sup T", "lead", mine, phone="9555500088")[1]
            sup_id = c.execute("SELECT id FROM users WHERE code=?", (sup_code,)).fetchone()["id"]
            ops.set_supervisor_languages(c, A, sup_id, [mine])
        adm.post("/login", data={"code": ADMIN_CODE})
        r = adm.get("/admin/scripts/text?code=TX0001")
        assert r.status_code == 200 and r.json()["text"].startswith("FULLTEXT-TX-1")
        assert adm.get("/admin/scripts/text?code=NOPE").status_code == 404
    with TestClient(app, raise_server_exceptions=False) as sup:
        sup.post("/login", data={"code": sup_code})
        assert sup.get("/admin/scripts/text?code=TX0002").status_code == 200
        assert sup.get("/admin/scripts/text?code=TY0002").status_code == 403   # other language
    with TestClient(app, raise_server_exceptions=False) as anon:
        r = anon.get("/admin/scripts/text?code=TX0001", follow_redirects=False)
        assert r.status_code in (302, 303, 307)
