"""Script upload paths: single paste upload (was a 500), folder batch upload, supervisor scoping, no traceback leaks."""
import os
import re
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "u.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)
ADMIN_CODE = os.environ["SF_ADMIN_CODE"]

from fastapi.testclient import TestClient  # noqa: E402

from app import db, ops  # noqa: E402
from app.main import app  # noqa: E402

BODY = "Doctor: Good morning, what brings you in today?\nPatient: I have had a dry cough for two weeks now.\nDoctor: Any fever or chest pain along with it?\nPatient: No fever, only the cough at night."


OTHER = "Pharmacist: Please show me the prescription for the antibiotic.\nCustomer: Here it is, the doctor wrote it yesterday evening.\nPharmacist: Take one tablet after dinner for five days.\nCustomer: Thank you, should I avoid any food with it?"
HINDI = "Nurse: Please sit down and extend your arm for the blood pressure reading.\nPatient: I felt dizzy this morning while climbing the stairs at home.\nNurse: Your reading is slightly high, the doctor will see you soon.\nPatient: Okay, I also have a mild fever since last night."


def _csrf(client, url):
    return re.search(r'name="csrf" value="([^"]+)"', client.get(url).text).group(1)


def test_single_and_batch_upload_and_scoping():
    with TestClient(app, raise_server_exceptions=False) as adm:
        with db.tx() as c:
            A = {"role": "admin", "id": 0, "name": "t", "language_id": None}
            lids = [c.execute("INSERT INTO languages(code,name,target,created_at) VALUES(?,?,0,1.0)", (k, n)).lastrowid
                    for k, n in (("TA", "Tamil"), ("HI", "Hindi"))]
            sup_id, sup_code = ops.create_user(c, A, "Sup", "lead", lids[0], phone="9555500000")
            ops.set_supervisor_languages(c, A, sup_id, [lids[0]])
        assert adm.post("/login", data={"code": ADMIN_CODE}).status_code == 200
        tok = _csrf(adm, f"/admin/scripts?lang={lids[0]}")

        # single paste upload used to 500 with NameError
        r = adm.post("/admin/scripts/upload", data={"csrf": tok, "lang": str(lids[0]), "paste_text": OTHER, "domain": "Healthcare", "subdomain": "Doctor-Patient"}, follow_redirects=False)
        assert r.status_code == 303, r.text[:300]
        assert "uploaded" in r.headers["location"] or "m=" in r.headers["location"]
        with db.ro() as c:
            assert c.execute("SELECT COUNT(*) n FROM scripts").fetchone()["n"] == 1

        # batch: 2 good files (1 Tamil, 1 Hindi), 1 duplicate of the first, 1 non-script, 1 hidden
        files = [
            ("files", ("tamil_Healthcare_Doctor-Patient_General-Physician_dry-cough.txt", BODY.encode(), "text/plain")),
            ("files", ("hindi_Healthcare_Doctor-Patient_General-Physician_fever.txt", HINDI.encode(), "text/plain")),
            ("files", ("tamil_Healthcare_Doctor-Patient_General-Physician_dry-cough-copy.txt", BODY.encode(), "text/plain")),
            ("files", ("notes.pdf", b"%PDF", "application/pdf")),
            ("files", (".hidden.txt", BODY.encode(), "text/plain")),
        ]
        r = adm.post("/admin/scripts/upload_batch", data={"csrf": tok, "lang": "all"}, files=files)
        assert r.status_code == 200, r.text[:300]
        d = r.json()
        by = {x["file"]: x for x in d["results"]}
        assert d["summary"]["imported"] == 2, [(x["file"], x["status"], x["reason"]) for x in d["results"]]
        assert by["notes.pdf"]["status"] == "skipped" and by[".hidden.txt"]["status"] == "skipped"
        assert by["tamil_Healthcare_Doctor-Patient_General-Physician_dry-cough-copy.txt"]["status"] == "skipped"  # duplicate content
        assert d["drive_mirror"] is False

        # bad csrf rejected
        assert adm.post("/admin/scripts/upload_batch", data={"csrf": "nope", "lang": "all"}, files=files[:1]).status_code == 400

    with TestClient(app, raise_server_exceptions=False) as sup:
        sup.post("/login", data={"code": sup_code})
        tok = _csrf(sup, f"/admin/scripts?lang={lids[0]}")
        files = [("files", ("hindi_Healthcare_Doctor-Patient_General-Physician_cold.txt", HINDI.replace("fever","cold").encode(), "text/plain"))]
        # supervisor cannot upload without choosing an owned language
        assert sup.post("/admin/scripts/upload_batch", data={"csrf": tok, "lang": "all"}, files=files).status_code == 403
        assert sup.post("/admin/scripts/upload_batch", data={"csrf": tok, "lang": str(lids[1])}, files=files).status_code == 403
        # own language but file is Hindi -> skipped, not imported
        d = sup.post("/admin/scripts/upload_batch", data={"csrf": tok, "lang": str(lids[0])}, files=files).json()
        assert d["summary"]["imported"] == 0 and d["results"][0]["status"] == "skipped"
        # the old server-path "Import Local" endpoint is gone (it let admins read arbitrary server folders)
        assert sup.post("/admin/scripts/import_local", data={"csrf": tok, "folder_path": "/etc"}, follow_redirects=False).status_code in (404, 405)


def test_server_error_page_has_no_traceback(monkeypatch):
    from app import main
    monkeypatch.delenv("SF_DEBUG", raising=False)

    @main.app.get("/__boom")
    async def boom():
        raise RuntimeError("secret-internal-detail")

    with TestClient(main.app, raise_server_exceptions=False) as c:
        r = c.get("/__boom")
        assert r.status_code == 500
        assert "secret-internal-detail" not in r.text and "Traceback" not in r.text
