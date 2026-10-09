"""Demo data: Kannada language planned (1,503 slots), stub writer+judge keys, Wave 1 queued, 1 lead, 8 participants.
Stub scripts are random placeholder text in Kannada script. They are marked TEST and only assignable because
this seeder switches allow_test_scripts on. Never use stub output for real recording.
Usage: python seed_demo.py"""
import time

from app import admin_ops, db, ops
from app import pipeline as P
from app.main import ensure_admin

db.init_db()
ensure_admin()
with db.tx() as c:
    if c.execute("SELECT 1 FROM languages").fetchone():
        raise SystemExit("Database already has a language. Delete data/factory.db to re-seed.")
    admin = c.execute("SELECT * FROM users WHERE role='admin'").fetchone()
    lid = admin_ops.create_language(c, admin, "kannada")
    admin_ops.plan(c, admin, lid)
    admin_ops.add_key(c, admin, "stub-writer", "stub", "stub-writer", "writer", rpm=6000)
    admin_ops.add_key(c, admin, "stub-judge", "stub", "stub-judge", "judge", rpm=6000)
    db.set_setting(c, "allow_test_scripts", 1)
    db.set_setting(c, "wave1_size", 100)
    P.release_wave(c, lid, 1, admin)
    out = []
    _, lead = ops.create_user(c, admin, "Demo Lead", "lead", lid, phone="9000000001")
    out.append(("Team lead", "Demo Lead", lead))
    _, rev = ops.create_user(c, admin, "Demo Reviewer", "reviewer", lid)
    out.append(("Reviewer", "Demo Reviewer", rev))
    for i in range(8):
        _, code = ops.create_user(c, admin, f"Demo Person {i + 1}", "participant", lid, phone=f"98000000{i + 1:02d}",
                                  gender="MF"[i % 2])
        out.append(("Participant", f"Demo Person {i + 1}", code))
    adm = admin["code"]
print("\nAdmin       ", adm[:4] + "-" + adm[4:])
for role, name, code in out:
    print(f"{role:12}{name:18}{code[:4]}-{code[4:]}")
print("\nStart the server (run.sh). Wave 1 starts generating on its own.\n")
