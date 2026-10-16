import os, sys, sqlite3, pytest, tempfile
from pathlib import Path

# Set isolated test DB environment
os.environ["SF_NO_THREADS"] = "1"
_d = tempfile.mkdtemp()
os.environ["SF_DB"] = os.path.join(_d, "test_dedup.db")
os.environ["SF_BACKUP"] = os.path.join(_d, "bk")

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from app import db, script_importer


def test_different_filename_duplicate_detection(tmp_path):
    db.init_db()
    with db.tx() as c:
        c.execute("INSERT OR IGNORE INTO languages(id, code, name, planned) VALUES(3, 'GU', 'Gujarati', 0)")

    with db.tx() as c:
        # Initial Script Content
        orig_content = (
            "# Language: Gujarati\n"
            "# Taxonomy: Healthcare / Doctor-Patient / General Physician\n"
            "# Scenario: Megaloblastic Vitamin B12 deficiency in strict vegetarian adult\n"
            "# Setting: In-person Consultation Room\n"
            "# Participants: DOCTOR (General Physician), PATIENT (Adult)\n\n"
            "DOCTOR : નમસ્તે, અંદર આવો અને બેસો. આજે તમને કઈ તકલીફ માટે બતાવવા આવવાનું થયું છે?\n"
            "PATIENT : નમસ્તે doctor, છેલ્લા બે મહિનાથી મારા બંને હાથના આંગળા અને પગના તળિયામાં સતત ટાંકણીઓ ભોંકાતી હોય તેવી ઝણઝણાટી થાય છે.\n"
            "DOCTOR : લાવો, ગઈકાલે તમે જે blood reports કરાવ્યા છે તે જોઈ લઈએ. આ ઝણઝણાટી કયા સમયે વધારે થાય છે?\n"
            "PATIENT : ખાસ કરીને રાત્રે સૂતી વખતે અને સવારે ઉઠતી વખતે હાથ-પગ સાવ ખોટા પડી ગયા હોય તેવું લાગે છે."
        )

        orig_file = tmp_path / "Gujarathi_Healthcare_Doctor-Patient_General-Physician_vitamin-b12.txt"
        orig_file.write_text(orig_content, encoding="utf-8-sig")

        # Import original script
        res_orig = script_importer.import_script_file(c, orig_file, target_lang_id=3)
        assert res_orig["status"] == "imported", f"Expected imported, got: {res_orig}"
        orig_code = res_orig["code"]

        # TEST CASE 1: Exact same content, but COMPLETELY DIFFERENT FILENAME
        fake_file_1 = tmp_path / "random_file_name_98765.txt"
        fake_file_1.write_text(orig_content, encoding="utf-8-sig")

        res1 = script_importer.import_script_file(c, fake_file_1, target_lang_id=3)
        assert res1["status"] == "skipped", f"Expected skipped, got {res1}"
        assert orig_code in res1["reason"], f"Expected duplicate reason citing {orig_code}, got: {res1['reason']}"

        # TEST CASE 2: Different filename AND modified/different header lines
        lines = orig_content.splitlines()
        diag_lines = [l for l in lines if not l.startswith("#")]
        fake_content_2 = "# Language: Gujarati\n# Taxonomy: Custom / Unknown / Special\n# Scenario: Completely New Name\n\n" + "\n".join(diag_lines)
        fake_file_2 = tmp_path / "another_completely_different_name.txt"
        fake_file_2.write_text(fake_content_2, encoding="utf-8-sig")

        res2 = script_importer.import_script_file(c, fake_file_2, target_lang_id=3)
        assert res2["status"] == "skipped", f"Expected skipped, got {res2}"
        assert orig_code in res2["reason"], f"Expected duplicate reason citing {orig_code}, got: {res2['reason']}"

        # TEST CASE 3: Different filename AND renamed speaker roles (DOCTOR/PATIENT -> Speaker A/Speaker B)
        fake_content_3 = orig_content.replace("DOCTOR :", "Speaker A:").replace("PATIENT :", "Speaker B:")
        fake_file_3 = tmp_path / "renamed_speakers_script.txt"
        fake_file_3.write_text(fake_content_3, encoding="utf-8-sig")

        res3 = script_importer.import_script_file(c, fake_file_3, target_lang_id=3)
        assert res3["status"] == "skipped", f"Expected skipped, got {res3}"
        assert orig_code in res3["reason"], f"Expected duplicate reason citing {orig_code}, got: {res3['reason']}"

        # TEST CASE 4: Truly brand new unique script MUST be allowed
        unique_content = (
            "# Language: Gujarati\n"
            "# Taxonomy: Healthcare / Cardiology / Specialist\n"
            "# Scenario: Unique Hypertension Followup\n\n"
            "DOCTOR : નમસ્તે, તમારા કાર્ડિયોગ્રામનો નવો રિપોર્ટ તદ્દન નોર્મલ આવ્યો છે.\n"
            "PATIENT : ખૂબ ખૂબ આભાર સાહેબ, આ તદ્દન નવો સંવાદ છે જે ક્યારેય પહેલાં ડેટાબેઝમાં આવ્યો નથી."
        )
        unique_file = tmp_path / "Gujarathi_Healthcare_Cardiology_Specialist_unique-test-bp.txt"
        unique_file.write_text(unique_content, encoding="utf-8-sig")

        dup_check = script_importer.find_duplicate_script(c, 3, unique_file.name, unique_content)
        assert dup_check is None, f"Expected no duplicate for unique script, found: {dup_check}"

        res_unique = script_importer.import_script_file(c, unique_file, target_lang_id=3)
        assert res_unique["status"] == "imported", f"Expected unique script to be imported, got: {res_unique}"
