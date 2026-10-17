"""Upload duplicate check: the in-memory fingerprint cache must give exactly the same answers as reading every
fingerprint from the database for every file (the old behaviour, which cost GBs of Supabase egress)."""
import os
import random
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "dc.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)

from fastapi.testclient import TestClient  # noqa: E402

from app import db, script_importer as SI  # noqa: E402
from app.main import app  # noqa: E402

WORDS = [f"w{i}" for i in range(400)]


def _text(rnd, base=None, keep=1.0):
    """A dialogue; with base, keep `keep` fraction of its lines (near-copy) and make up the rest."""
    lines = []
    for t in range(40):
        if base is not None and rnd.random() < keep:
            lines.append(base[t])
        else:
            lines.append(("Doctor: " if t % 2 else "Patient: ") + " ".join(rnd.choice(WORDS) for _ in range(25)))
    return lines


def _old_check5(c, language_id, content):
    """The previous implementation of Check 5, verbatim in behaviour."""
    fp = SI.compute_content_fingerprint(content)
    if not fp["shingles"]:
        return None
    mine = fp["shingles"]
    for r in c.execute("SELECT id, code, shingles FROM scripts WHERE language_id = ? AND shingles IS NOT NULL",
                       (language_id,)).fetchall():
        if r["shingles"]:
            other = SI.unpack_shingles(r["shingles"])
            if other and min(len(mine), len(other)) > 0:
                ov = len(mine & other) / min(len(mine), len(other))
                if ov >= 0.70:
                    return (r["id"], r["code"], int(ov * 100))
    return None


def _new_check5(c, language_id, content):
    fp = SI.compute_content_fingerprint(content)
    mine = fp["shingles"]
    if not mine:
        return None
    for sid, code, packed in SI._language_shingles(c, language_id):
        if packed:
            other = SI.unpack_shingles(packed)
            if other and min(len(mine), len(other)) > 0:
                ov = len(mine & other) / min(len(mine), len(other))
                if ov >= 0.70:
                    return (sid, code, int(ov * 100))
    return None


def _insert(c, lid, i, content):
    fp = SI.compute_content_fingerprint(content)
    return c.execute("INSERT INTO scripts(code,language_id,seq,domain,subdomain,status,version,shingles,created_at,updated_at) "
                     "VALUES(?,?,?,'Healthcare','X','READY',1,?,1.0,1.0)", (f"DC{lid}-{i:04d}", lid, i, fp["packed_shingles"])).lastrowid


def test_cache_matches_old_check_through_uploads_deletes_and_rewrites():
    rnd = random.Random(5)
    with TestClient(app, raise_server_exceptions=False):
        pass
    SI.shingles_changed()
    bases = []
    with db.tx() as c:
        lid = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES('DC','Dclang',0,1.0)").lastrowid
        other = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES('DD','Ddlang',0,1.0)").lastrowid
    n = 0
    for step in range(120):
        with db.tx() as c:
            r = rnd.random()
            if r < 0.45 or not bases:                     # brand-new script
                lines = _text(rnd)
            elif r < 0.75:                                 # near copy of an existing one (some >=70%, some below)
                lines = _text(rnd, rnd.choice(bases), keep=rnd.choice([0.95, 0.8, 0.6, 0.3]))
            else:                                          # exact copy
                lines = list(rnd.choice(bases))
            content = "\n".join(lines)
            old, new = _old_check5(c, lid, content), _new_check5(c, lid, content)
            assert old == new, (step, old, new)
            if old is None:                                # what the importer does: insert only non-duplicates
                _insert(c, lid, n, content)
                bases.append(lines)
                n += 1
            if step % 17 == 5:                             # other language changing must not matter
                _insert(c, other, 1000 + step, "\n".join(_text(rnd)))
            if step % 29 == 9 and n > 3:                   # a script deleted
                victim = c.execute("SELECT id FROM scripts WHERE language_id=? ORDER BY id LIMIT 1 OFFSET 1", (lid,)).fetchone()["id"]
                c.execute("DELETE FROM scripts WHERE id=?", (victim,))
            if step % 23 == 11 and n > 2:                  # a fingerprint rewritten in place (generator / re-import)
                tgt = c.execute("SELECT id FROM scripts WHERE language_id=? ORDER BY id DESC LIMIT 1", (lid,)).fetchone()["id"]
                c.execute("UPDATE scripts SET shingles=? WHERE id=?",
                          (SI.compute_content_fingerprint("\n".join(_text(rnd)))["packed_shingles"], tgt))
                SI.shingles_changed()
    assert n > 30


def test_cache_reads_few_bytes_per_check():
    """After the first check, a check with nothing new must not download the fingerprints again."""
    rnd = random.Random(9)
    with TestClient(app, raise_server_exceptions=False):
        pass
    with db.tx() as c:
        lid = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES('DE','Delang',0,1.0)").lastrowid
        for i in range(60):
            _insert(c, lid, i, "\n".join(_text(rnd)))
    SI.shingles_changed()
    blob_reads = []
    orig = db.connect

    class P:
        def __init__(self, c):
            self._c = c

        def execute(self, sql, *a, **k):
            if "shingles FROM scripts" in sql:
                blob_reads.append(sql)
            return self._c.execute(sql, *a, **k)

        def __getattr__(self, name):
            return getattr(self._c, name)

    db.connect = lambda: P(orig())
    try:
        for _ in range(10):
            with db.ro() as c:
                SI.find_duplicate_script(c, lid, f"new_{rnd.random()}.txt", "\n".join(_text(rnd)),
                                         {"domain": "Healthcare", "subdomain": "X", "specialisation": "", "scenario": ""})
        assert len(blob_reads) == 1, blob_reads        # loaded once, then served from memory
        with db.tx() as c:
            _insert(c, lid, 999, "\n".join(_text(rnd)))
        with db.ro() as c:
            SI.find_duplicate_script(c, lid, "x.txt", "\n".join(_text(rnd)),
                                     {"domain": "Healthcare", "subdomain": "X", "specialisation": "", "scenario": ""})
        assert len(blob_reads) == 2 and "id > ?" in blob_reads[-1]   # only the new script fetched
    finally:
        db.connect = orig
