import sys
import os
import time
import json
import sqlite3
from pathlib import Path

# Add app to path
sys.path.insert(0, os.path.abspath('app'))

from app import db, ops, admin_ops, taxonomy, codes

def seed():
    with db.tx() as c:
        L = c.execute("SELECT * FROM languages WHERE name='Kannada'").fetchone()
        lid = L["id"]

        # Import Scripts
        project_dir = Path("C:/Users/sanat/Desktop/Awign Doc/Audio project")
        script_files = list(project_dir.rglob("*.txt")) + list(project_dir.rglob("*.md"))
        script_files = [f for f in script_files if "extracted_scriptfactory" not in str(f) and "Script Factory" not in str(f)]
        
        imported = 0
        for f in script_files:
            try:
                text = f.read_text(encoding="utf-8")
                if len(text.strip()) < 50: continue
                
                domain = "General"
                subdomain = f.stem
                
                # Check if script exists already by subdomain (prevent dupes if ran multiple times)
                if c.execute("SELECT 1 FROM scripts WHERE subdomain=?", (subdomain,)).fetchone():
                    continue
                
                # Insert into scripts as READY
                code = codes.new_code()
                seq = c.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM scripts WHERE language_id=?", (lid,)).fetchone()[0]
                c.execute(
                    "INSERT INTO scripts(language_id, seq, code, domain, subdomain, status, source, wave) VALUES(?, ?, ?, ?, ?, 'READY', 'imported', 1)",
                    (lid, seq, code, domain, subdomain)
                )
                script_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
                
                # Insert version
                turn_json = json.dumps([{"role": "Speaker", "text": text}])
                words = len(text.split())
                c.execute("INSERT INTO script_versions(script_id, version, turns, words, created_at) VALUES(?, 1, ?, ?, ?)",
                          (script_id, turn_json, words, time.time()))
                imported += 1
            except Exception as e:
                print(f"Error importing {f.name}: {e}")
                
        print(f"Imported {imported} scripts as READY.")
        
        # Get one agent code to show user
        agent = c.execute("SELECT * FROM users WHERE role='participant' LIMIT 1").fetchone()
        if agent:
            print(f"Sample Agent Code: {codes.fmt(agent['code'])}")
        
seed()
