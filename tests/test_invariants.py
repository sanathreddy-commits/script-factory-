"""Invariant tests: the rules the system must never break (blueprint section 4)."""
import json
import os
import sqlite3
import tempfile
import threading
import time

import pytest

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "t.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")

from app import admin_ops, codes, db, ops, pipeline as P, taxonomy as T, validators as V  # noqa: E402

ADMIN = {"role": "admin", "id": 1, "name": "Admin", "language_id": None, "perm": "manage"}


@pytest.fixture(scope="module")
def world():
    db.init_db()
    with db.tx() as c:
        c.execute("INSERT INTO users(name,role,code,created_at) VALUES('Admin','admin','ADMINCODE',?)", (time.time(),))
        lid = admin_ops.create_language(c, ADMIN, "kannada")
        admin_ops.plan(c, ADMIN, lid)
        admin_ops.create_language(c, ADMIN, "hindi", quotas={s[1]: 2 for s in T.SUBS})
        admin_ops.add_key(c, ADMIN, "w", "stub", "stub-w", "writer", rpm=60000)
        admin_ops.add_key(c, ADMIN, "j", "stub", "stub-j", "judge", rpm=60000)
        db.set_setting(c, "allow_test_scripts", 1)
        P.release_wave(c, lid, 1, ADMIN)
    P.run_pending(30)
    with db.tx() as c:  # approve 30 scripts as a reviewer would
        ids = [r["id"] for r in c.execute("SELECT id FROM scripts WHERE status='IN_REVIEW' LIMIT 30")]
        for i in ids:
            P.review(c, i, ADMIN, "accept")
    return lid


def mkpair(lid, n, genders=("", "")):
    with db.tx() as c:
        a, _ = ops.create_user(c, ADMIN, f"P{n}a", "participant", lid, phone=f"98000{n:05d}", gender=genders[0])
        b, _ = ops.create_user(c, ADMIN, f"P{n}b", "participant", lid, phone=f"97000{n:05d}", gender=genders[1])
        for u in (a, b):
            ops.accept_consent(c, u, False, "v")
        return ops.create_pair(c, ADMIN, a, b), a, b


def topup(n=12, accept=True):
    """Generate n more scripts with the stub and (optionally) approve them as a reviewer would."""
    P.run_pending(n)
    if accept:
        with db.tx() as c:
            for r in c.execute("SELECT id FROM scripts WHERE status='IN_REVIEW'").fetchall():
                P.review(c, r["id"], ADMIN, "accept")


def test_quota_math(world):
    with db.ro() as c:
        assert c.execute("SELECT COUNT(*) n FROM scripts WHERE language_id=?", (world,)).fetchone()["n"] == 1503
        for r in c.execute("SELECT subdomain, COUNT(*) n FROM scripts WHERE language_id=? GROUP BY 1", (world,)):
            assert r["n"] == 167
        spec = {r["specialisation"]: r["n"] for r in c.execute(
            "SELECT specialisation, COUNT(*) n FROM scripts WHERE subdomain='Doctor-Patient Consultation' GROUP BY 1")}
        assert spec == {"General Physician": 84, "Pediatrician": 17, "Dermatology": 25, "Dentist": 25, "Gynecologist": 8, "Orthopedist": 8}
        w1 = {r["subdomain"] for r in c.execute("SELECT subdomain FROM scripts WHERE wave=1")}
        assert len(w1) == 9  # wave 1 is stratified


def test_target_must_match_quotas():
    with db.tx() as c:
        with pytest.raises(ValueError):
            admin_ops.create_language(c, ADMIN, "tamil", target=1500)


def test_codes_catch_every_single_character_typo():
    code = codes.new_code()
    assert codes.valid(code)
    for i in range(8):
        for ch in codes.ALPHA:
            if ch != code[i]:
                assert not codes.valid(code[:i] + ch + code[i + 1:]) or codes._chk(code[:7]) == code[7] and i == 7 and False
    sc = codes.script_code("KN", 123)
    assert codes.script_code_valid(sc) and not codes.script_code_valid(sc[:-1] + str((int(sc[-1]) + 1) % 10))


def test_llm_output_stored_only_if_valid(world):
    with db.ro() as c:
        for s in c.execute("SELECT id, version, status FROM scripts WHERE language_id=? AND version>0", (world,)):
            L = c.execute("SELECT * FROM languages WHERE id=?", (world,)).fetchone()
            v = c.execute("SELECT turns FROM script_versions WHERE script_id=? AND version=?", (s["id"], s["version"])).fetchone()
            sc = c.execute("SELECT attrs FROM scripts WHERE id=?", (s["id"],)).fetchone()
            assert V.validate(json.loads(v["turns"]), json.loads(sc["attrs"]), L, db.S(c)) == []


def test_bad_output_rejected_and_flagged(world):
    with db.tx() as c:
        P.cancel_wave(c, world, 1, ADMIN)  # leave only my job in the queue
        db.set_setting(c, "stub_chaos", 1.0)  # every draft is 55% short; targeted repair then succeeds or flags
        s = c.execute("SELECT id FROM scripts WHERE language_id=? AND status='PLANNED' AND wave=2 LIMIT 1", (world,)).fetchone()
        c.execute("UPDATE scripts SET status='QUEUED' WHERE id=?", (s["id"],))
        c.execute("INSERT INTO jobs(script_id,status,seed) VALUES(?, 'QUEUED', 5)", (s["id"],))
    P.run_pending(1)
    with db.tx() as c:
        db.set_setting(c, "stub_chaos", 0.0)
        sc = c.execute("SELECT * FROM scripts WHERE id=?", (s["id"],)).fetchone()
        # either repaired into a valid script, or flagged without storing any version
        if sc["status"] == "NEEDS_HUMAN":
            assert sc["version"] == 0
            assert c.execute("SELECT COUNT(*) n FROM script_versions WHERE script_id=?", (s["id"],)).fetchone()["n"] == 0
        else:
            assert sc["version"] == 1 and 1200 <= sc["words"] <= 1500


def test_validators_catch_problems(world):
    with db.ro() as c:
        L = c.execute("SELECT * FROM languages WHERE id=?", (world,)).fetchone()
        s = c.execute("SELECT * FROM scripts WHERE language_id=? LIMIT 1", (world,)).fetchone()
        S = db.S(c)
    attrs = json.loads(s["attrs"])
    base = [{"s": "A", "t": "ಕ " * 700}, {"s": "B", "t": "ಕ " * 700}]
    assert any(e.startswith("turn_too_long") for e in V.validate(base, attrs, L, S))
    bad = [{"s": "A", "t": "ಕಕ [name] 98765 43210"}, {"s": "B", "t": "hello"}]
    errs = " ".join(V.validate(bad, attrs, L, S))
    assert "placeholder" in errs and "words_short" in errs and "numbers_not_allowed" in errs and "script_ratio" in errs
    assert "speakers" in " ".join(V.validate([{"s": "A", "t": "ಕ"}], attrs, L, S))


def test_job_idempotent(world):
    with db.tx() as c:
        P.release_wave(c, world, 1, ADMIN)
        P.release_wave(c, world, 1, ADMIN)
    with db.ro() as c:
        assert c.execute("SELECT COUNT(*) n, COUNT(DISTINCT script_id) d FROM jobs").fetchone()["n"] == c.execute("SELECT COUNT(DISTINCT script_id) FROM jobs").fetchone()[0]


def test_wave_gate(world):
    with db.tx() as c:
        with pytest.raises(ValueError):
            P.release_wave(c, world, 3, ADMIN)  # wave 2 not finished


def test_only_approved_assignable_and_unique_under_concurrency(world):
    pairs = [mkpair(world, i)[0] for i in range(100, 112)]
    with db.ro() as c:
        stock = c.execute("SELECT COUNT(*) n FROM scripts WHERE status='APPROVED'").fetchone()['n']
    results, errors = [], []

    def go(pid):
        try:
            with db.tx() as c:
                made, _ = ops.assign(c, ADMIN, pid, 5)
                results.extend(made)
        except Exception as e:
            errors.append(e)

    ts = [threading.Thread(target=go, args=(p,)) for p in pairs]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not errors, errors
    with db.ro() as c:
        rows = c.execute("SELECT a.script_id, s.status FROM assignments a JOIN scripts s ON s.id=a.script_id WHERE a.status NOT IN ('RELEASED','ABANDONED')").fetchall()
        ids = [r["script_id"] for r in rows]
        assert len(ids) == len(set(ids)), "a script was assigned twice"
        assert all(r["status"] == "APPROVED" for r in rows), "non-approved script assigned"
        assert len(ids) == stock  # exactly the approved stock was handed out, no more
    # the database itself refuses a duplicate even if app code is bypassed
    con = sqlite3.connect(os.environ["SF_DB"], isolation_level=None, timeout=5)
    with pytest.raises(sqlite3.IntegrityError):
        con.execute("INSERT INTO assignments(script_id,pair_id,ua,ub,status) VALUES(?,?,?,?, 'ASSIGNED')", (ids[0], 1, 1, 2))
    con.close()


def test_caps_never_exceeded(world):
    topup(12)
    pid, a, b = mkpair(world, 300)
    with db.tx() as c:
        c.execute("UPDATE languages SET cap_pair_min=25, cap_person_min=900 WHERE id=?", (world,))
        db.set_setting(c, "open_limit", 50)
        c.execute("UPDATE scripts SET status='APPROVED' WHERE language_id=? AND wave=2 AND status='PLANNED' LIMIT 0", (world,))
        # top up stock with a few approved scripts
        extra = [r["id"] for r in c.execute("SELECT id FROM scripts WHERE language_id=? AND status='IN_REVIEW' LIMIT 5", (world,))]
        for i in extra:
            P.review(c, i, ADMIN, "accept")
        made, note = ops.assign(c, ADMIN, pid, 10)
        assert len(made) == 2 and "cap" in note  # 25 min cap / 10 min each
        assert ops.pair_used(c, pid) <= 25
        with pytest.raises(PermissionError):
            ops.assign(c, {"role": "lead", "language_id": world, "perm": "manage", "id": 9, "name": "x"}, pid, 1, override=True, reason="x")
        c.execute("UPDATE languages SET cap_pair_min=450 WHERE id=?", (world,))


def test_scoping_between_languages(world):
    with db.tx() as c:
        hl = c.execute("SELECT id FROM languages WHERE code='HI'").fetchone()["id"]
        lead_id, _ = ops.create_user(c, ADMIN, "Hindi Lead", "lead", hl)
        lead = c.execute("SELECT * FROM users WHERE id=?", (lead_id,)).fetchone()
        victim, _ = ops.create_user(c, ADMIN, "Kn Person", "participant", world, phone="9611111111")
        with pytest.raises(PermissionError):
            ops.reissue_code(c, lead, victim)
        with pytest.raises(PermissionError):
            ops.view_code(c, lead, victim)
        with pytest.raises(PermissionError):
            ops.create_user(c, lead, "x", "participant", world, phone="9622222222")
        with pytest.raises(PermissionError):
            ops.export(c, {"role": "participant", "language_id": world}, "people")


def test_participant_cannot_open_others_assignment(world):
    topup(6)
    pid, a, b = mkpair(world, 400)
    with db.tx() as c:
        outsider, _ = ops.create_user(c, ADMIN, "Outsider", "participant", world, phone="9633333333")
        made, _ = ops.assign(c, ADMIN, pid, 1)
        with pytest.raises(PermissionError):
            ops._mine(c, outsider, made[0])
        ops._mine(c, a, made[0])


def test_audit_and_ledger_immutable(world):
    con = sqlite3.connect(os.environ["SF_DB"], isolation_level=None, timeout=5)
    for stmt in ("UPDATE audit SET action='x'", "DELETE FROM audit"):
        with pytest.raises(sqlite3.DatabaseError):
            con.execute(stmt)
    with db.tx() as c:
        c.execute("INSERT INTO ledger(ts,assignment_id,user_id,pair_id,minutes,kind) VALUES(1,1,1,1,1,'t')")
    for stmt in ("UPDATE ledger SET minutes=9", "DELETE FROM ledger"):
        with pytest.raises(sqlite3.DatabaseError):
            con.execute(stmt)
    con.close()


def test_full_session_flow_and_ledger_matches_confirmed(world):
    topup(6)
    pid, a, b = mkpair(world, 500)
    with db.tx() as c:
        made, _ = ops.assign(c, ADMIN, pid, 1)
        aid = made[0]
        with pytest.raises(ops.OpsError):
            ops.start(c, a, aid)  # nobody ready
        ops.heartbeat(c, a, aid)
        ops.heartbeat(c, b, aid)
        ops.start(c, a, aid)
        db.set_setting(c, "dur_min_sec", 1)
        c.execute("UPDATE assignments SET started_at=started_at-600 WHERE id=?", (aid,))
        ops.done(c, a, aid)
        assert c.execute("SELECT status FROM assignments WHERE id=?", (aid,)).fetchone()["status"] == "IN_SESSION"
        ops.done(c, b, aid)
        r = c.execute("SELECT * FROM assignments WHERE id=?", (aid,)).fetchone()
        assert r["status"] == "CONFIRMED" and r["verified"] == 0 and 9.9 < r["conf_min"] < 10.5
        for u in (a, b):
            tot = c.execute("SELECT COALESCE(SUM(minutes),0) m FROM ledger WHERE user_id=?", (u,)).fetchone()["m"]
            conf = c.execute("SELECT COALESCE(SUM(conf_min),0) m FROM assignments WHERE status='CONFIRMED' AND (ua=? OR ub=?)", (u, u)).fetchone()["m"]
            assert abs(tot - conf) < 1e-6
        db.set_setting(c, "dur_min_sec", 60)


def test_reconcile_and_qc(world):
    topup(8)
    pid, a, b = mkpair(world, 600)
    with db.tx() as c:
        made, _ = ops.assign(c, ADMIN, pid, 2)
        codes_ = [c.execute("SELECT s.code FROM assignments x JOIN scripts s ON s.id=x.script_id WHERE x.id=?", (m,)).fetchone()["code"] for m in made]
        csvtxt = f"script_id,session_id,duration_sec\n{codes_[0]},S1,620\n{codes_[1]},S2,5\nZZ-00001-0,S3,600\n"
        res = ops.reconcile_csv(c, ADMIN, csvtxt)
        assert res == dict(confirmed=1, duplicate=0, out_of_spec=1, orphan=1)
        assert ops.reconcile_csv(c, ADMIN, csvtxt)["duplicate"] == 1  # idempotent
        q = ops.qc_csv(c, ADMIN, f"script_id,result,reason\n{codes_[0]},FAIL,noise\n")
        assert q["failed"] == 1
        assert c.execute("SELECT COALESCE(SUM(minutes),0) m FROM ledger WHERE assignment_id=?", (made[0],)).fetchone()["m"] == 0


def test_sweeper_releases_and_abandons(world):
    topup(6)
    pid, a, b = mkpair(world, 700)
    with db.tx() as c:
        made, _ = ops.assign(c, ADMIN, pid, 1)
        out = ops.sweep(c, time.time() + 80 * 3600)
        assert out["auto_released"] >= 1
        assert c.execute("SELECT status FROM assignments WHERE id=?", (made[0],)).fetchone()["status"] == "RELEASED"


def test_stop_the_line(world):
    topup(8, accept=False)
    with db.tx() as c:
        db.set_setting(c, "reject_stop_min", 5)
        ids = [r["id"] for r in c.execute("SELECT id FROM scripts WHERE language_id=? AND status='IN_REVIEW' LIMIT 6", (world,))]
        for i in ids:
            P.review(c, i, ADMIN, "reject", "fluency")
        assert db.setting(c, "pause_generation") == "1"
        db.set_setting(c, "pause_generation", 0)


def test_gender_matching_never_violated(world):
    pid, a, b = mkpair(world, 800, ("M", "M"))
    with db.tx() as c:
        db.set_setting(c, "open_limit", 50)
        # make sure there is stock of every gender combination
        for r in c.execute("SELECT id FROM scripts WHERE language_id=? AND status='IN_REVIEW'", (world,)).fetchall():
            P.review(c, r["id"], ADMIN, "accept")
        made, _ = ops.assign(c, ADMIN, pid, 10)
        for m in made:
            at = json.loads(c.execute("SELECT s.attrs FROM assignments x JOIN scripts s ON s.id=x.script_id WHERE x.id=?", (m,)).fetchone()["attrs"])
            assert at["genders"] == {"A": "M", "B": "M"}


def test_unassigned_codepoints_rejected(world):
    with db.ro() as c:
        L = c.execute("SELECT * FROM languages WHERE id=?", (world,)).fetchone()
        s = c.execute("SELECT attrs FROM scripts WHERE language_id=? LIMIT 1", (world,)).fetchone()
        S = db.S(c)
    turns = [{"s": "A", "t": "ಕ೉ಕ " * 5}, {"s": "B", "t": "ಕ"}]
    assert "unassigned_codepoint" in V.validate(turns, json.loads(s["attrs"]), L, S)
