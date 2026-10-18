"""Supabase egress guard. The free plan allows 5 GB/month of data read from the database. Management pages used
to read every script's full text (~17 KB each, 26 MB per view with 1,500 scripts) and assigning read every
available script's text once per script handed out (~6 MB each). These tests fail if bytes read grow with script
text again, and check the caches never serve stale data.
"""
import json
import os
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "eg.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
from app import codes  # noqa: E402

_body = "EGRESSX"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)

from fastapi.testclient import TestClient  # noqa: E402

from app import db, ops, routes_master  # noqa: E402
from app.main import app  # noqa: E402

A = {"role": "admin", "id": 0, "name": "t", "language_id": None}
BIG = "x" * 20000          # ~20 KB of script text per script


class _Bytes:
    """Counts bytes of every value read from the database."""
    def __init__(self):
        self.n = 0
        self._orig = db.connect

    def __enter__(self):
        outer = self

        def size(v):
            return len(v) if isinstance(v, (str, bytes)) else 8

        class Cur:
            def __init__(self, cur):
                self._c = cur

            def _count(self, rows):
                for r in rows:
                    outer.n += sum(size(v) for v in tuple(r))
                return rows

            def fetchone(self):
                r = self._c.fetchone()
                if r is not None:
                    self._count([r])
                return r

            def fetchall(self):
                return self._count(self._c.fetchall())

            def __iter__(self):
                for r in self._c:
                    self._count([r])
                    yield r

            def __getattr__(self, name):
                return getattr(self._c, name)

        class P:
            def __init__(self, c):
                self._c = c

            def execute(self, *a, **k):
                return Cur(self._c.execute(*a, **k))

            def __getattr__(self, name):
                return getattr(self._c, name)

        db.connect = lambda: P(self._orig())
        return self

    def __exit__(self, *exc):
        db.connect = self._orig


def _seed(c, tag, n):
    lid = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES(?,?,0,1.0)", (tag, f"L{tag}")).lastrowid
    for i in range(n):
        c.execute("INSERT INTO scripts(code,language_id,seq,domain,subdomain,specialisation,status,version,attrs,created_at,updated_at) "
                  "VALUES(?,?,?,?,?,?,'READY',1,?,1.0,1.0)",
                  (f"{tag}{i:04d}", lid, i + 1, "Healthcare", "Sub", "Spec",
                   json.dumps({"raw_text": f"TEXT-{tag}-{i} " + BIG, "genders": {"A": "any", "B": "any"}})))
    a = ops.create_user(c, A, f"{tag} A", "participant", lid, phone=f"91{tag}00001"[:10].ljust(10, "1"))[0]
    b = ops.create_user(c, A, f"{tag} B", "participant", lid, phone=f"92{tag}00002"[:10].ljust(10, "2"))[0]
    ops.manual_pair(c, A, lid, a, b)
    pid = c.execute("SELECT id FROM pairs ORDER BY id DESC").fetchone()["id"]
    return lid, pid


def test_management_pages_and_assign_do_not_read_script_text():
    with TestClient(app, raise_server_exceptions=False) as adm:
        with db.tx() as c:
            lid, pid = _seed(c, "EA", 60)
        adm.post("/login", data={"code": os.environ["SF_ADMIN_CODE"]})
        for page in ("/admin/scripts", "/admin/agents", "/admin/pairs"):
            with _Bytes() as m:
                r = adm.get(f"{page}?lang={lid}")
            assert r.status_code == 200
            # 60 scripts x 20 KB = 1.2 MB if text were read; the page itself needs only a few KB.
            assert m.n < 200_000, f"{page} read {m.n} bytes from the database"
        with _Bytes() as m:
            with db.tx() as c:
                made, _ = ops.assign(c, A, pid, 3, override=True, reason="t")
        assert len(made) == 3
        assert m.n < 3 * 2 * len(BIG), f"assigning 3 scripts read {m.n} bytes (each script's text was read)"


def test_script_text_cache_is_never_stale():
    with db.tx() as c:
        lid, _ = _seed(c, "EB", 1)
        sid = c.execute("SELECT id FROM scripts WHERE code='EB0000'").fetchone()["id"]
    with db.ro() as c:
        assert routes_master.get_script_content_text(c, sid, "EB0000").startswith("TEXT-EB-0")
    with _Bytes() as m, db.ro() as c:
        assert routes_master.get_script_content_text(c, sid, "EB0000").startswith("TEXT-EB-0")
    assert m.n < 1000, "an unchanged script must come from the cache"
    with db.tx() as c:   # a re-upload / edit changes updated_at -> must be re-read at once
        c.execute("UPDATE scripts SET attrs=?, updated_at=2.0 WHERE id=?", (json.dumps({"raw_text": "NEW TEXT"}), sid))
    with db.ro() as c:
        assert routes_master.get_script_content_text(c, sid, "EB0000") == "NEW TEXT"


def test_settings_cache_sees_writes_immediately():
    with db.ro() as c:
        assert db.setting(c, "avg_minutes") == "10"
    with db.tx() as c:   # raw SQL write, as several pages do
        c.execute("INSERT OR REPLACE INTO settings(k, v) VALUES('avg_minutes', '12')")
    with db.ro() as c:
        assert db.setting(c, "avg_minutes") == "12"
    with db.tx() as c:
        db.set_setting(c, "avg_minutes", "10")
    with db.ro() as c:
        assert db.S(c)["avg_minutes"] == "10"


def test_responses_are_compressed():
    with TestClient(app, raise_server_exceptions=False) as cl:
        r = cl.get("/static/style.css", headers={"accept-encoding": "gzip"})
        assert r.headers.get("content-encoding") == "gzip"


def test_proof_photos_are_small_readable_webp():
    import io as _io
    import random
    from PIL import Image, ImageDraw
    from app import storage
    random.seed(1)
    im = Image.new("RGB", (2400, 1080), (30, 40, 50))
    d = ImageDraw.Draw(im)
    for i in range(400):   # busy screenshot: lots of text-like detail and noise
        d.text((random.randint(0, 2300), random.randint(0, 1050)), "Session 8262fe10 4:24:23 PM", fill=(random.randint(100, 255),) * 3)
    raw = _io.BytesIO()
    im.save(raw, "PNG")
    out = storage.compress_image(raw.getvalue(), max_bytes=50 * 1024, fmt="WEBP")
    assert len(out) <= 50 * 1024
    assert storage.image_ext(out) == ".webp"
    assert max(Image.open(_io.BytesIO(out)).size) >= 900      # never shrunk below readable size
    assert storage.image_ext(raw.getvalue()) == ".png"


def test_idle_sweeper_reads_almost_nothing():
    import time as _t
    with db.tx() as c:
        lid, pid = _seed(c, "EC", 5)
        ops.assign(c, A, pid, 2, override=True, reason="t")
    now = _t.time()
    with db.ro() as c:
        S = db.S(c)
        with _Bytes() as m:
            busy = ops.sweep_has_work(c, now, S)
    assert not busy and m.n < 200, "fresh assignments need no sweep, and checking must cost ~nothing"
    with db.ro() as c:
        assert ops.sweep_has_work(c, now + 200 * 3600, S)       # past deadline -> work
    with db.tx() as c:
        assert ops.sweep(c, now + 200 * 3600)["auto_released"] >= 2


def test_google_sheet_tabs_mirror_the_page_without_reading_script_text(monkeypatch):
    from app import storage
    from app.integrations import sheets
    monkeypatch.setattr(storage, "signed_url", lambda n, **k: "https://x.supabase.co/sign/" + n)
    with db.tx() as c:
        _seed(c, "ED", 30)
        codes_ = {r["code"] for r in c.execute("SELECT code FROM users WHERE role='participant'")}
    sheets._PAGE_CTX.clear()
    sheets._PAGE_CTX["on"] = True
    try:
        with _Bytes() as m, db.ro() as c:
            ag = sheets.get_agents_roster_data(c)[1]
            sc = sheets.get_scripts_master_data(c)[1]
            pr = sheets.get_pair_assignments_data(c)[1]
            n_scripts = c.execute("SELECT COUNT(*) n FROM scripts").fetchone()["n"]
    finally:
        sheets._PAGE_CTX.clear()
    assert len(sc) == n_scripts and pr and ag
    assert m.n < 400_000, f"sheet sync read {m.n} bytes (script text?)"
    flat = repr(ag)
    assert not any(code in flat for code in codes_), "login access codes must never go to the sheet"
