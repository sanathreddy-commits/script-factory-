"""Developer panel: Overview (online counts, data flow, errors), Connections page, email key handling."""
import json
import os
import re
import tempfile
import time

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "dev.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
os.environ.pop("SMTP_PASS", None)
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import db, ops  # noqa: E402
from app.integrations import email_service  # noqa: E402
from app.main import app  # noqa: E402

A = {"role": "admin", "id": 0, "name": "t", "language_id": None}
GOOD = "xkeysib-" + "a" * 56 + "-GOODKEY1234"
SECRET_SB = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.SUPERSECRETSUPABASE"


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._body = code, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def _fake_brevo(url, headers=None, **kw):
    if headers.get("api-key") == GOOD:
        return _Resp(200, {"email": "owner@example.com"})
    return _Resp(401, {"message": "Key not found", "code": "unauthorized"})


def _csrf(client, url):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def _dev_login(client):
    with db.tx() as c:
        row = c.execute("SELECT code FROM users WHERE role='developer' ORDER BY id LIMIT 1").fetchone()
        if row:
            code = row["code"]
        else:
            code = codes.new_code()
            c.execute("INSERT INTO users(name, email, role, code, active, created_at) VALUES('Dev','dev@test.local','developer',?,1,?)",
                      (code, time.time()))
    r = client.post("/login", data={"code": code}, follow_redirects=False)
    assert r.status_code in (302, 303), r.text[:300]


def test_overview_counts_people_and_lists_errors():
    with TestClient(app, raise_server_exceptions=False) as dev:
        with db.tx() as c:
            lid = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES('OV','Ovlang',0,1.0)").lastrowid
            a1 = ops.create_user(c, A, "Online Agent", "participant", lid, phone="9111100001")[0]
            ops.create_user(c, A, "Idle Agent", "participant", lid, phone="9111100002")
            s1 = ops.create_user(c, A, "Online Sup", "lead", lid, phone="9111100003")[0]
            c.execute("UPDATE users SET last_seen=? WHERE id IN (?,?)", (time.time(), a1, s1))
            db.log_app(c, "email", "system", "Email failed to x@y.com: Brevo does not recognise the key", "detail-xyz")
        _dev_login(dev)
        r = dev.get("/developer")
        assert r.status_code == 200
        html = r.text
        assert "How the system is doing" in html and "Where the data goes" in html
        assert "Online Agent" in html and "Online Sup" in html and "Idle Agent" not in html
        assert "Brevo does not recognise the key" in html and "detail-xyz" in html
        # real tab names, no DV1..DV6 labels
        assert "DV1" not in html and ">API Keys<" in html and ">Rules &amp; Limits<" in html
        m = re.search(r'Agents</div>\s*<div class="ov-num">(?:<span class="ov-dot"></span>)?(\d+)', html)
        assert m and m.group(1) == "1"


def test_connections_page_never_contains_secrets_and_email_save_keeps_key(monkeypatch):
    monkeypatch.setattr(email_service.httpx, "get", _fake_brevo)
    with TestClient(app, raise_server_exceptions=False) as dev:
        with db.tx() as c:
            c.execute("UPDATE connections SET config=? WHERE service='Supabase' OR name LIKE '%Supabase%'",
                      (json.dumps({"url": "https://x.supabase.co", "key": SECRET_SB}),))
        _dev_login(dev)
        tok = _csrf(dev, "/dev/connections")

        # SMTP key is refused with a clear message and nothing is saved
        r = dev.post("/dev/email/setup", data={"csrf": tok, "provider": "brevo", "api_key": "xsmtpsib-abc123",
                                               "from_email": "me@example.com"}, follow_redirects=False)
        assert "SMTP+key" in r.headers["location"] and "Nothing+was+saved" in r.headers["location"]

        # good key (pasted with spaces/quotes) is cleaned, saved and verified in one click
        r = dev.post("/dev/email/setup", data={"csrf": tok, "provider": "brevo", "api_key": f' "{GOOD}" \n',
                                               "from_email": "me@example.com", "from_name": "Awign Team"},
                     follow_redirects=False)
        assert "saved+and+verified" in r.headers["location"], r.headers["location"]
        with db.ro() as c:
            assert email_service.get_email_config(c)["password"] == GOOD

        # saving again with a blank key box keeps the saved key
        r = dev.post("/dev/email/setup", data={"csrf": tok, "provider": "brevo", "api_key": "",
                                               "from_email": "new@example.com"}, follow_redirects=False)
        assert "saved+and+verified" in r.headers["location"]
        with db.ro() as c:
            cfg = email_service.get_email_config(c)
            assert cfg["password"] == GOOD and cfg["from_email"] == "new@example.com" and cfg["from_name"] == "Awign Team"
            assert c.execute("SELECT status FROM connections WHERE name LIKE '%Email%'").fetchone()["status"] == "Connected"

        page = dev.get("/dev/connections").text
        assert GOOD not in page and SECRET_SB not in page          # secrets never in HTML
        assert email_service.key_hint(GOOD) in page and "saved in this app" in page
        assert "Email delivery" in page and "Google Drive storage" in page

        # missing CSRF is rejected
        assert dev.post("/dev/email/setup", data={"provider": "brevo", "api_key": GOOD}).status_code == 400


def test_brevo_errors_say_which_key_and_where_from(monkeypatch):
    monkeypatch.setattr(email_service.httpx, "get", _fake_brevo)
    bad = "xkeysib-" + "b" * 56 + "-OLDKEY9999"
    res = email_service.test_connection({"host": "smtp-relay.brevo.com", "password": bad, "password_source": "env:SMTP_PASS",
                                         "from_email": "a@b.com"})
    assert not res.ok
    assert email_service.key_hint(bad) in res.message and "Render environment variable SMTP_PASS" in res.message
    assert "does not recognise" in res.message
    res = email_service.test_connection({"host": "smtp-relay.brevo.com", "password": "", "from_email": "a@b.com"})
    assert not res.ok and "No API key" in res.message
    assert email_service.clean_key(" 'xkeysib-1 2\n3' ") == "xkeysib-123"


def test_activity_updates_last_seen():
    with TestClient(app, raise_server_exceptions=False) as dev:
        with db.tx() as c:
            c.execute("UPDATE users SET last_seen=0 WHERE role='developer'")
        _dev_login(dev)
        with db.tx() as c:
            c.execute("UPDATE users SET last_seen=0 WHERE role='developer'")
        dev.get("/dev/overview")
        with db.ro() as c:
            assert c.execute("SELECT last_seen FROM users WHERE role='developer' ORDER BY id LIMIT 1").fetchone()["last_seen"] > time.time() - 30
