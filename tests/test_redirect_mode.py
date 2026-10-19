"""Old address after the move: with SF_REDIRECT_TO set, every request is forwarded to the new site (same path and
query, so login-code links keep working), /healthz still answers, and the database is never opened."""
import os
import tempfile

_NEVER = os.path.join(tempfile.mkdtemp(), "never")

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402


def test_everything_forwards_and_database_is_never_opened(monkeypatch):
    monkeypatch.setenv("SF_REDIRECT_TO", "https://new.example.com/")   # only for this test
    monkeypatch.setenv("SF_NO_THREADS", "1")
    monkeypatch.setenv("SF_DB", os.path.join(_NEVER, "created.db"))
    def boom():
        raise AssertionError("database opened in redirect mode")
    monkeypatch.setattr(db, "connect", boom)
    with TestClient(app) as cl:
        r = cl.get("/login?code=ABCD1234", follow_redirects=False)
        assert r.status_code == 302 and r.headers["location"] == "https://new.example.com/login?code=ABCD1234"
        r = cl.get("/admin/scripts?lang=all", follow_redirects=False)
        assert r.headers["location"] == "https://new.example.com/admin/scripts?lang=all"
        r = cl.post("/a/12/save_time", data={"minutes": "9"}, follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "https://new.example.com/a/12/save_time"
        r = cl.get("/", follow_redirects=False)
        assert r.headers["location"] == "https://new.example.com/"
        h = cl.get("/healthz")
        assert h.status_code == 200 and h.text == "ok"
    assert not os.path.exists(_NEVER), "redirect mode must not create a database"
