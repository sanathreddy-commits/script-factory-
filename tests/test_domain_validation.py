"""Importer only accepts real domains (Healthcare, BFSI). A file name like marathi_kannada_... used to create a
'kannada' domain under Marathi."""
import os
import tempfile

os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "dv.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")
from app import codes  # noqa: E402

_body = "ABCDEFG"
os.environ["SF_ADMIN_CODE"] = _body + codes._chk(_body)

from fastapi.testclient import TestClient  # noqa: E402

from app import db, script_importer as SI  # noqa: E402
from app.main import app  # noqa: E402

import random  # noqa: E402

_rnd = random.Random(4)


def _dialogue():
    """A distinct dialogue each call (so the duplicate check does not skip the sample files)."""
    return "\n".join(f"{'Doctor' if i % 2 else 'Patient'}: " + " ".join(f"w{_rnd.randint(0, 5000)}" for _ in range(20))
                     for i in range(12))


DIALOGUE = _dialogue()


def _write(name, body=DIALOGUE):
    p = os.path.join(_d, name)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(body)
    return p


def test_only_healthcare_and_bfsi_are_imported():
    with TestClient(app, raise_server_exceptions=False):
        pass
    with db.tx() as c:
        c.execute("INSERT INTO languages(code,name,target,created_at) VALUES('MR','Marathi',0,1.0)")
    cases = {
        "marathi_Healthcare_Doctor-Patient_General-Physician_fever.txt": ("imported", "Healthcare"),
        "marathi_healthcare_Doctor-Patient_General-Physician_cough.txt": ("imported", "Healthcare"),   # case normalised
        "marathi_bfsi_Banking-Loans_Home_emi.txt": ("imported", "BFSI"),
        "marathi_kannada_Doctor-Patient_General-Physician_x.txt": ("skipped", None),                 # the bug
        "marathi_punjabi_Banking_Loans_y.txt": ("skipped", None),
    }
    for name, (want, dom) in cases.items():
        with db.tx() as c:
            res = SI.import_script_file(c, _write(name, _dialogue()))
        assert res["status"] == want, (name, res)
        if want == "imported":
            with db.ro() as c:
                assert c.execute("SELECT domain FROM scripts WHERE code=?", (res["code"],)).fetchone()["domain"] == dom
        else:
            assert "Unknown domain" in res["reason"] and "Healthcare, BFSI" in res["reason"], res
    # a valid Taxonomy header inside the file rescues a bad file name
    with db.tx() as c:
        res = SI.import_script_file(c, _write("marathi_kannada_Doctor-Patient_GP_z.txt",
                                              "# Taxonomy: BFSI / Banking-Cards / Credit\n" + _dialogue()))
    assert res["status"] == "imported", res
    with db.ro() as c:
        doms = {r["domain"] for r in c.execute("SELECT DISTINCT domain FROM scripts").fetchall()}
    assert doms == {"Healthcare", "BFSI"}


def test_allowed_domains_setting_can_add_a_domain():
    with TestClient(app, raise_server_exceptions=False):
        pass
    with db.tx() as c:
        db.set_setting(c, "allowed_domains", "Healthcare,BFSI,Retail")
        assert SI.resolve_domain(c, {"domain": "retail"}, "")[0] == "Retail"
        db.set_setting(c, "allowed_domains", "")
        assert SI.resolve_domain(c, {"domain": "retail"}, "")[0] == ""      # back to the default list
