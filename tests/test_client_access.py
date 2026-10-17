"""Client access links: deleting every link must not bring the sample links back."""
import os
import re
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "ca.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)
ADMIN_CODE = os.environ["SF_ADMIN_CODE"]

from fastapi.testclient import TestClient  # noqa: E402

from app import db  # noqa: E402
from app.main import app  # noqa: E402
from app.routes_master import get_portal_access_items  # noqa: E402


def _csrf(client, url):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def test_deleting_all_links_stays_empty_after_refresh():
    with TestClient(app, raise_server_exceptions=False) as adm:
        adm.post("/login", data={"code": ADMIN_CODE})
        with db.ro() as c:
            n = len(get_portal_access_items(c))
        assert n == 4  # fresh install shows the samples
        tok = _csrf(adm, "/admin/client_access")
        for _ in range(n):
            r = adm.post("/admin/client_access/delete/0", data={"csrf": tok}, follow_redirects=False)
            assert "removed" in r.headers["location"]
        with db.ro() as c:
            assert get_portal_access_items(c) == []
        assert "/admin/client_access/delete/" not in adm.get("/admin/client_access").text   # still no rows after a refresh
        adm.post("/admin/client_access/add", data={"csrf": tok, "taxonomy": "BFSI", "url": "https://x.example", "code": "C1"})
        with db.ro() as c:
            assert [i["code"] for i in get_portal_access_items(c)] == ["C1"]
