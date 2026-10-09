import os
import time
import threading
import httpx

from . import config

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")
BUCKET = os.environ.get("SUPABASE_BUCKET", "factory-data")
FILENAME = "factory.db"

_last_sync_mtime = 0


def is_configured():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def restore_from_cloud():
    """Download the latest database from Supabase on startup."""
    if not is_configured():
        print("[CloudSync] Not configured (SUPABASE_URL/SUPABASE_KEY not set). Using local DB.")
        return False

    url = f"{SUPABASE_URL}/storage/v1/object/authenticated/{BUCKET}/{FILENAME}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}"
    }
    try:
        r = httpx.get(url, headers=headers, timeout=30.0)
        if r.status_code == 200 and len(r.content) > 1000:
            db_file = config.db_path()
            os.makedirs(os.path.dirname(db_file), exist_ok=True)
            with open(db_file, "wb") as f:
                f.write(r.content)
            print(f"[CloudSync] Successfully restored database from cloud ({len(r.content)} bytes)")
            global _last_sync_mtime
            _last_sync_mtime = os.path.getmtime(db_file)
            return True
        elif r.status_code in (404, 400):
            print("[CloudSync] No cloud backup found yet in bucket. Pushing initial DB...")
            backup_to_cloud(force=True)
        else:
            print(f"[CloudSync] Restore failed with status {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[CloudSync] Restore error: {e}")
    return False


def backup_to_cloud(force=False):
    """Upload current database to Supabase."""
    if not is_configured():
        return False

    db_file = config.db_path()
    if not os.path.exists(db_file):
        return False

    global _last_sync_mtime
    mtime = os.path.getmtime(db_file)
    if not force and mtime <= _last_sync_mtime:
        return True

    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET}/{FILENAME}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "x-upsert": "true",
        "Content-Type": "application/octet-stream"
    }
    try:
        with open(db_file, "rb") as f:
            content = f.read()
        r = httpx.post(url, headers=headers, content=content, timeout=45.0)
        if r.status_code in (200, 201):
            _last_sync_mtime = mtime
            print(f"[CloudSync] Successfully backed up database to cloud ({len(content)} bytes)")
            return True
        else:
            print(f"[CloudSync] Backup failed with status {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[CloudSync] Backup error: {e}")
    return False


def start_background_sync(interval=15):
    """Periodically check if database was modified and upload."""
    if not is_configured():
        return

    def _loop():
        while True:
            time.sleep(interval)
            try:
                backup_to_cloud()
            except Exception as e:
                print(f"[CloudSync] Loop error: {e}")

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    print(f"[CloudSync] Background sync thread active (checking every {interval}s)")
