"""Bulk helpers must return exactly what the per-item helpers return.

hours_many / HoursBook  == get_hours            (users and pairs)
active_pairs_many       == active_pairs_for
script_texts_many       == get_script_content_text
plus: db.setting()/db.S() read cache inside db.ro() is never stale.
"""
import itertools
import json
import os
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "bulk.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, ops  # noqa: E402
from app.main import app  # noqa: E402
from app.routes_master import get_script_content_text, script_texts_many  # noqa: E402

A = {"role": "admin", "id": 0, "name": "t", "language_id": None}
STATUSES = ["ASSIGNED", "IN_SESSION", "SUBMITTED", "COMPLETED", "ACCEPTED", "CONFIRMED", "REJECTED",
            "RELEASED", "ABANDONED"]
REVIEWS = ["PENDING", "APPROVED", "ACCEPTED", "REJECTED"]
MISSING = [0, -1, 987654, 31337]
NAMES = ["Asha", "Ravi", "Meena", "Kiran", "Sita", "Mohan", "Geeta", "Anil", "Rekha", "Vijay", "Lata", "Suresh"]


def _seed():
    """Rich, varied data set. Returns dict of ids to compare."""
    tmp = tempfile.mkdtemp()
    full_txt = os.path.join(tmp, "full.txt")
    with open(full_txt, "w", encoding="utf-8") as fh:
        fh.write("\n  HEADER: from rel_path\nDoctor: hi\n")
    empty_txt = os.path.join(tmp, "empty.txt")
    open(empty_txt, "w").close()

    with db.tx() as c:
        langs = []
        for code, name, cap_pair, cap_person in [("BG", "Bulkgu", 450, 900), ("BK", "Bulkkn", 300, 600),
                                                 ("BT", "Bulkta", None, None)]:
            langs.append(c.execute("INSERT INTO languages(code,name,target,cap_pair_min,cap_person_min,created_at) "
                                   "VALUES(?,?,0,?,?,1.0)", (code, name, cap_pair, cap_person)).lastrowid)

        sc = itertools.count()

        def new_script(lid, attrs=None, brief="", code="auto"):
            n = next(sc)
            code = f"BULK{lid}_{n:04d}" if code == "auto" else code
            return c.execute("INSERT INTO scripts(code,language_id,domain,subdomain,specialisation,status,version,words,"
                             "attrs,brief,created_at,updated_at) VALUES(?,?,'Healthcare','Doctor-Patient','GP','READY',1,"
                             "100,?,?,1.0,1.0)", (code, lid, attrs, brief)).lastrowid

        def add_version(sid, v, turns):
            c.execute("INSERT INTO script_versions(script_id,version,turns,words,created_at) VALUES(?,?,?,10,1.0)",
                      (sid, v, turns))

        # ---------------- scripts with every text source variant
        text_sids = []
        for li, lid in enumerate(langs):
            for i in range(14):
                variant = i % 14
                attrs, brief = None, ""
                if variant == 0:
                    attrs = json.dumps({"raw_text": f"  RAW TEXT {lid}-{i}\nDoctor: hello {i}  "})
                elif variant == 1:
                    attrs = json.dumps({"filename": f"bulk_missing_{lid}_{i}.txt"})
                elif variant == 2:
                    attrs = json.dumps({"raw_text": "   ", "filename": "nope_never.txt"})
                elif variant == 3:
                    attrs = json.dumps({"rel_path": full_txt})
                elif variant == 4:
                    attrs = json.dumps({"rel_path": empty_txt, "raw_text": ""})
                elif variant == 5:
                    attrs = "{not json"
                elif variant == 6:
                    attrs = json.dumps(["a", "list"])
                elif variant == 7:
                    brief = f"see bulk_brief_{i}.txt for details"
                elif variant == 8:
                    attrs = json.dumps({"raw_text": 12345})
                elif variant == 9:
                    attrs = ""
                sid = new_script(lid, attrs, brief)
                text_sids.append(sid)
                # versions: several shapes, highest version wins
                if variant in (0, 1, 2, 5, 7):
                    add_version(sid, 1, json.dumps([{"role": "Doctor", "text": f"v1 {i}"}]))
                    add_version(sid, 3, json.dumps([{"speaker": "Patient", "text": f"v3 {i}"}, "plain turn",
                                                    {"role": "", "speaker": "", "text": "nobody"},
                                                    {"role": "Nurse", "text": ""}, 42, None]))
                    add_version(sid, 2, json.dumps([{"role": "Doctor", "text": "v2 should lose"}]))
                elif variant == 6:
                    add_version(sid, 1, "this is not json at all")
                elif variant == 8:
                    add_version(sid, 1, json.dumps([]))
                elif variant == 9:
                    add_version(sid, 1, json.dumps({"turns": "dict not list"}))
                elif variant == 10:
                    add_version(sid, 1, json.dumps([{"role": "Doctor", "text": "older"}]))
                    add_version(sid, 2, None)  # latest has NULL turns -> fallback label
                elif variant == 11:
                    add_version(sid, 1, "")
                elif variant == 12:
                    add_version(sid, 5, json.dumps("just a string"))
                # variants 3, 4, 13: no versions
        nocode = new_script(langs[0], None, "", code=None)
        add_version(nocode, 1, "")
        text_sids.append(nocode)
        text_sids.append(new_script(langs[1], None, "", code=None))

        # ---------------- people
        users = []
        all_pairs = []
        phones = itertools.count(9000000001)
        for li, lid in enumerate(langs):
            people = []
            for j in range(12):
                uid = ops.create_user(c, A, f"{NAMES[j]} B{li}", "participant", lid, phone=str(next(phones)),
                                      email=f"b{li}{j}@x.com" if j % 2 else "", gender="M" if j % 2 else "F",
                                      opening_min=[0, 30, 0, 120, 12.5, 7.25][j % 6])[0]
                people.append(uid)
            # pairs: person 0 in three active pairs, person 2 in two
            for a, b in [(0, 1), (2, 3), (0, 4), (0, 5), (6, 2)]:
                ops.manual_pair(c, A, lid, people[a], people[b])
            ops.manual_pair(c, A, lid, people[7], people[8])
            ops.manual_pair(c, A, lid, people[9], people[10])
            pids = [r["id"] for r in c.execute("SELECT id FROM pairs WHERE language_id=? ORDER BY id", (lid,)).fetchall()]
            dropped, dropped2 = pids[-2], pids[-1]
            c.execute("UPDATE pairs SET opening_min=45 WHERE id=?", (pids[1],))
            c.execute("UPDATE pairs SET opening_min=NULL WHERE id=?", (pids[2],))
            c.execute("UPDATE users SET opening_min=NULL WHERE id=?", (people[5],))
            c.execute("UPDATE users SET active=0 WHERE id=?", (people[11],))
            c.execute("UPDATE users SET active=0 WHERE id=?", (people[4],))   # inactive but in an active pair
            c.execute("UPDATE users SET phone='' WHERE id=?", (people[1],))   # partner phone '' -> '-'
            users += people
            all_pairs += pids

            # assignments: every status x review on every pair, each on its own script
            k = 0
            for pi, pid in enumerate(pids):
                p = c.execute("SELECT * FROM pairs WHERE id=?", (pid,)).fetchone()
                combos = list(itertools.product(STATUSES, REVIEWS))
                for st, rv in combos[pi::2] if pi % 3 else combos:
                    k += 1
                    sid = new_script(lid)
                    conf = None if (st in ("ASSIGNED", "IN_SESSION") or k % 7 == 0) else [0, 9.5, 12.25, 33.333][k % 4]
                    est = [8, None, 12, 0, 15.5][k % 5]
                    c.execute("INSERT INTO assignments(script_id,version,pair_id,ua,ub,status,est_min,conf_min,assigned_at,"
                              "review_status,updated_at) VALUES(?,1,?,?,?,?,?,?,?,?,?)",
                              (sid, pid, p["a"], p["b"], st, est, conf, 1000.0 + k, rv, 2000.0 + k))
            ops.drop_pair(c, A, dropped, "test")      # releases its ASSIGNED / IN_SESSION
            ops.drop_pair(c, A, dropped2, "test2")
            # odd assignments
            odd = [
                (None, people[7], people[8], "SUBMITTED", None, 9.5, "PENDING"),     # pair-less, ex-partners
                (None, people[3], people[3], "ACCEPTED", 10, 20, "ACCEPTED"),       # ua == ub
                (None, people[6], None, "COMPLETED", None, None, "PENDING"),        # ub NULL
                (None, None, people[6], "IN_SESSION", 7, None, "PENDING"),          # ua NULL
                (pids[0], people[9], people[10], "SUBMITTED", 5, 4.5, "APPROVED"),  # pair members drift
                (pids[0], p["a"], p["b"], "submitted", 5, 3.0, "approved"),         # lowercase
                (pids[1], people[2], people[3], None, 5, 3.0, None),                # NULL status / review
                (pids[1], people[2], people[3], "CONFIRMED", 5, 2.0, ""),           # empty review
                (pids[3], people[0], people[5], "WEIRD", None, 1.0, "PENDING"),     # unknown status
            ]
            for pid, ua, ub, st, est, conf, rv in odd:
                c.execute("INSERT INTO assignments(script_id,version,pair_id,ua,ub,status,est_min,conf_min,assigned_at,"
                          "review_status,updated_at) VALUES(?,1,?,?,?,?,?,?,1.0,?,1.0)",
                          (new_script(lid), pid, ua, ub, st, est, conf, rv))

        # users outside the normal path: no language, unknown language, leads
        nolang = c.execute("INSERT INTO users(name,phone,role,language_id,code,opening_min,active,created_at) "
                           "VALUES('No Lang','','participant',NULL,'NOLANG01',NULL,1,1.0)").lastrowid
        badlang = c.execute("INSERT INTO users(name,phone,role,language_id,code,opening_min,active,created_at) "
                            "VALUES('Bad Lang','9111111111','participant',999,'BADLANG1',60,1,1.0)").lastrowid
        lead = ops.create_user(c, A, "Bulk Lead", "lead", langs[0], phone="9555500091")[0]
        users += [nolang, badlang, lead]
        # odd pairs: self pair, partner that does not exist, no language, unknown language, non-ACTIVE status
        odd_pairs = [
            (langs[0], nolang, nolang, "ACTIVE", 0),
            (langs[1], badlang, 4242424, "ACTIVE", 15),
            (None, nolang, badlang, "ACTIVE", None),
            (999, badlang, users[0], "ACTIVE", 5),
            (langs[2], users[0], users[1], "LIMIT_REACHED", 0),
        ]
        for lid, a, b, st, om in odd_pairs:
            pid = c.execute("INSERT INTO pairs(language_id,a,b,status,opening_min,created_at) VALUES(?,?,?,?,?,1.0)",
                            (lid, a, b, st, om)).lastrowid
            all_pairs.append(pid)
            c.execute("INSERT INTO assignments(script_id,version,pair_id,ua,ub,status,est_min,conf_min,assigned_at,"
                      "review_status,updated_at) VALUES(?,1,?,?,?,'SUBMITTED',NULL,11.5,1.0,'PENDING',1.0)",
                      (new_script(langs[0]), pid, a, b))
        all_script_ids = [r["id"] for r in c.execute("SELECT id FROM scripts ORDER BY id").fetchall()]
    return {"users": users, "pairs": all_pairs, "text_sids": text_sids, "all_sids": all_script_ids}


@pytest.fixture(scope="module")
def seeded(_isolated_db):
    with TestClient(app, raise_server_exceptions=False):  # startup creates the tables
        pass
    return _seed()


@pytest.fixture(params=["defaults", "custom_limits", "bad_limits"])
def limits(request):
    vals = {"defaults": ("7.5", "15.0"), "custom_limits": ("8.25", "11"), "bad_limits": ("abc", "")}[request.param]
    with db.tx() as c:
        db.set_setting(c, "pair_limit_h", vals[0])
        db.set_setting(c, "user_limit_h", vals[1])
    yield request.param
    with db.tx() as c:
        db.set_setting(c, "pair_limit_h", "7.5")
        db.set_setting(c, "user_limit_h", "15.0")


@pytest.fixture(params=[500, 3])
def chunk(request, monkeypatch):
    """Also run with a tiny chunk size so rows that span chunks (ua in one chunk, ub in another) are exercised."""
    real = ops._chunks
    monkeypatch.setattr(ops, "_chunks", lambda ids, n=500: real(ids, request.param))
    return request.param


def _diff(expected, actual):
    bad = []
    for k in sorted(set(expected) | set(actual), key=repr):
        if expected.get(k, "<absent>") != actual.get(k, "<absent>"):
            bad.append((k, expected.get(k, "<absent>"), actual.get(k, "<absent>")))
    return bad


def test_seed_is_varied(seeded):
    with db.ro() as c:
        sts = {r["status"] for r in c.execute("SELECT DISTINCT status FROM assignments").fetchall()}
        rvs = {r["review_status"] for r in c.execute("SELECT DISTINCT review_status FROM assignments").fetchall()}
        assert set(STATUSES) <= sts and set(REVIEWS) <= rvs
        assert c.execute("SELECT COUNT(*) n FROM assignments WHERE pair_id IS NULL").fetchone()["n"] > 0
        assert c.execute("SELECT COUNT(*) n FROM pairs WHERE status='DROPPED'").fetchone()["n"] >= 6
        multi = c.execute("SELECT a, COUNT(*) n FROM pairs WHERE status='ACTIVE' GROUP BY a HAVING COUNT(*)>2").fetchall()
        assert multi
    assert len(seeded["users"]) >= 39 and len(seeded["pairs"]) >= 26 and len(seeded["text_sids"]) >= 44


@pytest.mark.parametrize("kind", ["user", "pair"])
def test_hours_many_equals_get_hours(seeded, limits, chunk, kind):
    ids = seeded["users"] if kind == "user" else seeded["pairs"]
    ids = list(ids) + MISSING
    with db.ro() as c:
        expected = {i: ops.get_hours(c, kind, i) for i in ids}
        actual = ops.hours_many(c, kind, ids)
        # shuffled order + duplicates must not change anything
        actual_rev = ops.hours_many(c, kind, list(reversed(ids)) + ids[:5])
    assert _diff(expected, actual) == []
    assert _diff(expected, actual_rev) == []
    assert list(actual) == ids


def test_hours_many_inside_tx_and_aliases(seeded):
    with db.tx() as c:
        for kind, alias, ids in [("user", "person", seeded["users"]), ("pair", "pairs", seeded["pairs"])]:
            ids = list(ids) + MISSING
            expected = {i: ops.get_hours(c, kind, i) for i in ids}
            assert _diff(expected, ops.hours_many(c, kind, ids)) == []
            assert _diff(expected, ops.hours_many(c, alias, ids)) == []
        assert ops.hours_many(c, "user", []) == {}
        assert ops.hours_many(c, "user", [None]) == {}


def test_hours_book_equals_get_hours(seeded, chunk):
    users = list(seeded["users"]) + MISSING
    pairs = list(seeded["pairs"]) + MISSING
    with db.ro() as c:
        exp_u = {i: ops.get_hours(c, "user", i) for i in users}
        exp_p = {i: ops.get_hours(c, "pair", i) for i in pairs}
        book = ops.HoursBook(c)
        book.prefetch(users=users[::2] + [None], pairs=pairs[::3] + [None])   # partial prefetch
        got_u = {i: book.user(i) for i in users}                              # rest fetched lazily
        got_p = {i: book.pair(i) for i in pairs}
        fresh = ops.HoursBook(c)                                              # no prefetch at all
        got_u2 = {i: fresh.user(i) for i in users}
        got_p2 = {i: fresh.pair(i) for i in pairs}
    assert _diff(exp_u, got_u) == []
    assert _diff(exp_p, got_p) == []
    assert _diff(exp_u, got_u2) == []
    assert _diff(exp_p, got_p2) == []


def test_active_pairs_many_equals_active_pairs_for(seeded, chunk):
    with db.ro() as c:
        partners = [r["b"] for r in c.execute("SELECT b FROM pairs").fetchall()]
        uids = list(dict.fromkeys(list(seeded["users"]) + partners + MISSING))
        expected = {u: ops.active_pairs_for(c, u) for u in uids}
        actual = ops.active_pairs_many(c, uids)
        actual_rev = ops.active_pairs_many(c, list(reversed(uids)) + uids[:3] + [None])
        assert sum(1 for v in expected.values() if len(v) >= 3) >= 3        # person in 3 active pairs
    assert _diff(expected, actual) == []
    assert _diff(expected, actual_rev) == []
    assert list(actual) == uids


def test_script_texts_many_equals_get_script_content_text(seeded, chunk):
    ids = list(dict.fromkeys(seeded["text_sids"] + seeded["all_sids"])) + MISSING
    with db.ro() as c:
        code = {r["id"]: r["code"] for r in c.execute("SELECT id, code FROM scripts").fetchall()}
        expected = {i: get_script_content_text(c, i, code.get(i, "")) for i in ids}
        actual = script_texts_many(c, ids)
        actual_rev = script_texts_many(c, list(reversed(ids)) + ids[:4] + [None])
    assert _diff(expected, actual) == []
    assert _diff(expected, actual_rev) == []
    assert list(actual) == ids
    # sanity: the variants really produced different kinds of text
    texts = set(expected.values())
    assert any(t.startswith("RAW TEXT") for t in texts)
    assert any(t.startswith("HEADER: from rel_path") for t in texts)
    assert any("Patient : v3" in t and "plain turn" in t for t in texts)
    assert "this is not json at all" in texts and "[]" in texts and "Script None" in texts


def test_settings_cache_matches_and_is_never_stale(seeded):
    with db.tx() as c:
        db.set_setting(c, "bulk_probe", "one")
    with db.tx() as c:
        out_S = db.S(c)
        out_vals = {k: db.setting(c, k) for k in list(out_S) + ["missing_key"]}
        out_default = db.setting(c, "missing_key", "dflt")
    with db.ro() as c:
        assert db.read_cache() is not None
        assert db.S(c) == out_S
        assert {k: db.setting(c, k) for k in list(out_S) + ["missing_key"]} == out_vals
        assert db.setting(c, "missing_key", "dflt") == out_default == "dflt"
        assert db.setting(c, "bulk_probe") == "one"
        # nested write transaction: cache must be dropped for the outer read block
        with db.tx() as w:
            assert db.read_cache() is None
            db.set_setting(w, "bulk_probe", "two")
            db.set_setting(w, "pair_limit_h", "9.5")
            assert db.setting(w, "bulk_probe") == "two"
        assert db.read_cache() is not None
        assert db.setting(c, "bulk_probe") == "two"
        assert db.S(c)["bulk_probe"] == "two"
        assert db.S(c)["pair_limit_h"] == "9.5"
        # hours computed after the write use the new limit, same as outside the cache
        pid = seeded["pairs"][-1]
        cached = ops.hours_many(c, "pair", [pid])[pid]
        # write through the read connection itself also invalidates
        db.set_setting(c, "bulk_probe", "three")
        assert db.setting(c, "bulk_probe") == "three"
        assert db.S(c)["bulk_probe"] == "three"
    with db.tx() as c:
        assert db.setting(c, "bulk_probe") == "three"
        assert ops.get_hours(c, "pair", pid) == cached
        db.set_setting(c, "pair_limit_h", "7.5")
    assert db.read_cache() is None
