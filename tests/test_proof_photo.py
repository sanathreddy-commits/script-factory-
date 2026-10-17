"""View Photo: a proof photo whose server copy was wiped (Render redeploy) and whose Supabase upload
failed must still be viewable from its Google Drive copy."""
import os
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "pp.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
for _k in ("SUPABASE_URL", "SUPABASE_KEY"):
    os.environ.pop(_k, None)
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)
ADMIN_CODE = os.environ["SF_ADMIN_CODE"]

from fastapi.testclient import TestClient  # noqa: E402

from app import db, ops, storage  # noqa: E402
from app.integrations import drive  # noqa: E402
from app.main import app  # noqa: E402

A = {"role": "admin", "id": 0, "name": "t", "language_id": None}
JPEG = b"\xff\xd8\xff\xe0fakejpegbytes"


def test_file_id_from_link():
    assert drive.file_id_from_link("https://drive.google.com/file/d/1AbC-_x9/view?usp=drivesdk") == "1AbC-_x9"
    assert drive.file_id_from_link("https://drive.google.com/open?id=XYZ_1") == "XYZ_1"
    assert drive.file_id_from_link("") == ""


def test_view_photo_falls_back_to_drive(monkeypatch):
    calls = []

    def fake_download(name, drive_link="", config=None, c=None):
        calls.append((name, drive_link))
        return JPEG if name == "proof_9_1.jpg" else None

    monkeypatch.setattr(drive, "download_proof", fake_download)
    monkeypatch.setattr(storage, "_cfg", lambda: ("", "", "proofs"))   # Supabase not available
    with TestClient(app, raise_server_exceptions=False) as adm:
        with db.tx() as c:
            lid = c.execute("INSERT INTO languages(code,name,target,created_at) VALUES('PP','Pplang',0,1.0)").lastrowid
            a = ops.create_user(c, A, "Pa", "participant", lid, phone="9123400001")[0]
            b = ops.create_user(c, A, "Pb", "participant", lid, phone="9123400002")[0]
            sid = c.execute("INSERT INTO scripts(code,language_id,domain,subdomain,status,version,created_at,updated_at) "
                            "VALUES('PP001',?,'Healthcare','X','READY',1,1.0,1.0)", (lid,)).lastrowid
            c.execute("INSERT INTO assignments(script_id,version,ua,ub,status,conf_min,proof_img,proof_drive_url,updated_at) "
                      "VALUES(?,1,?,?,'SUBMITTED',8,'proof_9_1.jpg','https://drive.google.com/file/d/DRIVEID9/view',1.0)",
                      (sid, a, b))
        local = os.path.join(storage.uploads_dir(), "proof_9_1.jpg")
        if os.path.exists(local):
            os.remove(local)   # simulate the Render redeploy wiping the disk
        adm.post("/login", data={"code": ADMIN_CODE})
        r = adm.get("/uploads/proof_9_1.jpg")
        assert r.status_code == 200 and r.content == JPEG
        assert calls == [("proof_9_1.jpg", "https://drive.google.com/file/d/DRIVEID9/view")]
        assert os.path.exists(local)                        # re-cached: the next view needs no Drive call
        assert adm.get("/uploads/proof_9_1.jpg").content == JPEG and len(calls) == 1
        r = adm.get("/uploads/proof_404_1.jpg")              # in no storage at all
        assert r.status_code == 404 and "Google Drive" in r.text
        html = adm.get("/admin/reports").text
        assert "proofPhotoFailed" in html and 'id="proofPhotoModalMsg"' in html
