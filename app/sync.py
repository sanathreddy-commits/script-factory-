import os
import time
import json
import threading
import httpx

from . import config

# Module-level defaults & fallback from environment variables
DEFAULT_BUCKET = "factory-data"
FILENAME = "factory.db"

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "").strip() or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
BUCKET = os.environ.get("SUPABASE_BUCKET", DEFAULT_BUCKET).strip() or DEFAULT_BUCKET

_last_sync_mtime = 0
_bg_thread = None
_bg_stop_event = threading.Event()


def get_config(c=None, config_override=None):
    """
    Resolve Supabase configuration using hierarchical precedence:
    1. config_override dictionary (if provided)
    2. Runtime 'connections' table (service='Supabase' or name containing 'Supabase')
    3. Runtime 'dev_keys' table (service='Supabase') for write-only API key
    4. Runtime 'settings' table (supabase_url, supabase_key, supabase_bucket, supabase_postgres_url)
    5. Environment variables (SUPABASE_URL, SUPABASE_KEY, SUPABASE_BUCKET, SUPABASE_POSTGRES_URL)
    """
    global SUPABASE_URL, SUPABASE_KEY, BUCKET

    # 1. Base from environment
    env_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    env_key = os.environ.get("SUPABASE_KEY", "").strip() or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    env_bucket = os.environ.get("SUPABASE_BUCKET", DEFAULT_BUCKET).strip() or DEFAULT_BUCKET
    env_pg = (
        os.environ.get("SUPABASE_POSTGRES_URL", "").strip()
        or os.environ.get("POSTGRES_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )

    resolved = {
        "url": env_url,
        "key": env_key,
        "bucket": env_bucket,
        "postgres_url": env_pg,
    }

    # 2. Check Database (settings, dev_keys, connections)
    def _read_from_cursor(cursor):
        nonlocal resolved
        try:
            # 2a. settings table
            for row in cursor.execute("SELECT k, v FROM settings WHERE k LIKE 'supabase_%' OR k='postgres_url'").fetchall():
                k, v = row["k"], (row["v"] or "").strip()
                if not v:
                    continue
                if k == "supabase_url":
                    resolved["url"] = v.rstrip("/")
                elif k == "supabase_key":
                    resolved["key"] = v
                elif k == "supabase_bucket":
                    if v and (v != DEFAULT_BUCKET or not os.environ.get("SUPABASE_BUCKET")):
                        resolved["bucket"] = v
                elif k in ("supabase_postgres_url", "postgres_url"):
                    resolved["postgres_url"] = v
        except Exception:
            pass

        try:
            # 2b. dev_keys table fallback for API key if missing
            if not resolved["key"]:
                dk = cursor.execute(
                    "SELECT key_val FROM dev_keys WHERE service='Supabase' AND status='Active' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if dk and dk["key_val"]:
                    resolved["key"] = dk["key_val"].strip()
        except Exception:
            pass

        try:
            # 2c. connections table (highest database priority)
            conn = cursor.execute(
                "SELECT config FROM connections WHERE service='Supabase' OR name LIKE '%Supabase%' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if conn and conn["config"]:
                cfg = json.loads(conn["config"])
                if isinstance(cfg, dict):
                    c_url = cfg.get("url") or cfg.get("supabase_url")
                    if c_url:
                        resolved["url"] = str(c_url).strip().rstrip("/")
                    c_key = cfg.get("key") or cfg.get("supabase_key") or cfg.get("apikey")
                    if c_key:
                        resolved["key"] = str(c_key).strip()
                    c_bucket = cfg.get("bucket") or cfg.get("supabase_bucket")
                    if c_bucket:
                        resolved["bucket"] = str(c_bucket).strip()
                    c_pg = cfg.get("postgres_url") or cfg.get("connection_string")
                    if c_pg:
                        resolved["postgres_url"] = str(c_pg).strip()
        except Exception:
            pass

    if c is not None:
        _read_from_cursor(c)
    else:
        db_path = config.db_path()
        if os.path.exists(db_path):
            try:
                from . import db
                with db.ro() as ro_c:
                    _read_from_cursor(ro_c)
            except Exception:
                pass

    # 3. Apply config_override if provided
    if config_override and isinstance(config_override, dict):
        if "url" in config_override:
            resolved["url"] = str(config_override["url"] or "").strip().rstrip("/")
        if "key" in config_override:
            resolved["key"] = str(config_override["key"] or "").strip()
        if "bucket" in config_override:
            resolved["bucket"] = str(config_override["bucket"] or "").strip()
        if "postgres_url" in config_override:
            resolved["postgres_url"] = str(config_override["postgres_url"] or "").strip()

    # Sync module globals for direct access
    SUPABASE_URL = resolved["url"]
    SUPABASE_KEY = resolved["key"]
    BUCKET = resolved["bucket"]

    return resolved


def is_configured(c=None, config_override=None):
    """Check if Supabase URL and Key are configured."""
    cfg = get_config(c=c, config_override=config_override)
    return bool(cfg.get("url") and cfg.get("key"))


def ensure_bucket(cfg=None, client=None):
    """Ensure the specified bucket exists in Supabase Storage."""
    if not cfg:
        cfg = get_config()
    url = cfg.get("url")
    key = cfg.get("key")
    bucket = cfg.get("bucket", DEFAULT_BUCKET)
    if not url or not key or not bucket:
        return False

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    close_client = False
    if client is None:
        client = httpx.Client(timeout=15.0)
        close_client = True
    try:
        r = client.get(f"{url}/storage/v1/bucket/{bucket}", headers=headers)
        if r.status_code in (200, 201):
            return True
        create_resp = client.post(
            f"{url}/storage/v1/bucket",
            headers=headers,
            json={"id": bucket, "name": bucket, "public": False},
        )
        return create_resp.status_code in (200, 201, 400, 409)
    except Exception as e:
        print(f"[CloudSync] ensure_bucket error: {e}")
        return False
    finally:
        if close_client:
            client.close()


def test_connection(c=None, config_override=None):
    """
    Test Supabase connection returning status, ping latency, and error details.
    """
    cfg = get_config(c=c, config_override=config_override)
    url = cfg.get("url", "")
    key = cfg.get("key", "")
    bucket = cfg.get("bucket", DEFAULT_BUCKET)
    postgres_url = cfg.get("postgres_url", "")

    if not url or not key:
        return {
            "ok": False,
            "status": "Not Configured",
            "latency_ms": 0.0,
            "error": "Supabase URL and API Key are required. Configure via environment variables, settings, or connections table.",
            "details": "Missing credentials",
            "bucket": bucket,
            "url": url,
            "postgres_ok": None,
            "postgres_error": None,
        }

    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
    }

    t0 = time.perf_counter()
    storage_ok = False
    details = ""
    error = None
    status = "Disconnected"

    try:
        with httpx.Client(timeout=15.0) as client:
            # 1. Test Storage bucket
            b_resp = client.get(f"{url}/storage/v1/bucket/{bucket}", headers=headers)
            latency_ms = round((time.perf_counter() - t0) * 1000, 2)

            if b_resp.status_code in (200, 201):
                storage_ok = True
                status = "Connected"
                details = f"Connected to Supabase storage bucket '{bucket}' ({latency_ms}ms)."
            elif b_resp.status_code == 404:
                # Bucket doesn't exist yet, but check if auth works via listing buckets
                list_resp = client.get(f"{url}/storage/v1/bucket", headers=headers)
                if list_resp.status_code in (200, 201):
                    storage_ok = True
                    status = "Connected"
                    details = f"Connected to Supabase Storage API ({latency_ms}ms). Bucket '{bucket}' will be created on first upload."
                else:
                    status = "Error"
                    error = f"Bucket '{bucket}' not found and bucket list failed (HTTP {list_resp.status_code})."
                    details = list_resp.text[:200]
            elif b_resp.status_code in (401, 403):
                status = "Authentication Error"
                error = f"Invalid Supabase API Key (HTTP {b_resp.status_code})."
                details = b_resp.text[:200]
            else:
                # Fallback to health / rest ping
                h_resp = client.get(f"{url}/auth/v1/health", headers=headers)
                if h_resp.status_code in (200, 201):
                    storage_ok = True
                    status = "Connected"
                    details = f"Supabase auth/service endpoint reachable ({latency_ms}ms)."
                else:
                    status = "Error"
                    error = f"HTTP {b_resp.status_code}: {b_resp.text[:200]}"
                    details = b_resp.text[:200]

    except httpx.RequestError as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        status = "Disconnected"
        error = f"Network error connecting to {url}: {exc}"
        details = str(exc)
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        status = "Error"
        error = f"Unexpected error: {exc}"
        details = str(exc)

    # Optional Postgres connection string test
    postgres_ok = None
    postgres_error = None
    if postgres_url:
        try:
            import psycopg2
            pg_conn = psycopg2.connect(postgres_url, connect_timeout=5)
            pg_conn.close()
            postgres_ok = True
            details += " | Postgres DB connection verified."
        except ImportError:
            postgres_ok = None  # driver not installed
        except Exception as pg_exc:
            postgres_ok = False
            postgres_error = str(pg_exc)
            details += f" | Postgres connection error: {pg_exc}"

    return {
        "ok": storage_ok,
        "status": status,
        "latency_ms": latency_ms,
        "error": error,
        "details": details,
        "bucket": bucket,
        "url": url,
        "postgres_ok": postgres_ok,
        "postgres_error": postgres_error,
    }


def upload_backup(force=False, c=None, config_override=None):
    """Upload current database to Supabase Storage bucket with detailed result."""
    cfg = get_config(c=c, config_override=config_override)
    if not (cfg.get("url") and cfg.get("key")):
        return {"ok": False, "status": "Not Configured", "bytes": 0, "error": "Supabase not configured"}

    db_file = config.db_path()
    if not os.path.exists(db_file):
        return {"ok": False, "status": "No DB File", "bytes": 0, "error": f"Database file not found: {db_file}"}

    global _last_sync_mtime
    mtime = os.path.getmtime(db_file)
    if not force and mtime <= _last_sync_mtime:
        return {"ok": True, "status": "Up to date", "bytes": 0, "mtime": mtime, "error": None}

    url = f"{cfg['url']}/storage/v1/object/{cfg['bucket']}/{FILENAME}"
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
        "x-upsert": "true",
        "Content-Type": "application/octet-stream",
    }
    # Checkpoint WAL into main database file so recent commits are persisted
    try:
        from . import db
        with db.tx() as chk_c:
            chk_c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except Exception as exc:
        print(f"[CloudSync] WAL checkpoint notice: {exc}")

    try:
        with open(db_file, "rb") as f:
            content = f.read()

        with httpx.Client(timeout=45.0) as client:
            r = client.post(url, headers=headers, content=content)
            if r.status_code in (200, 201):
                _last_sync_mtime = mtime
                print(f"[CloudSync] Successfully backed up database to cloud ({len(content)} bytes)")
                return {"ok": True, "status": "Uploaded", "bytes": len(content), "mtime": mtime, "error": None}
            elif r.status_code == 404:
                # Ensure bucket and retry once
                ensure_bucket(cfg, client=client)
                r2 = client.post(url, headers=headers, content=content)
                if r2.status_code in (200, 201):
                    _last_sync_mtime = mtime
                    print(f"[CloudSync] Successfully backed up database to cloud after ensuring bucket ({len(content)} bytes)")
                    return {"ok": True, "status": "Uploaded", "bytes": len(content), "mtime": mtime, "error": None}
                else:
                    err_text = r2.text[:250]
                    if "row-level security" in err_text or "AccessDenied" in err_text:
                        err_msg = (
                            "Supabase RLS Block: The 'anon' public key cannot write to storage without an RLS policy. "
                            "Please use your 'service_role' secret key (Supabase -> Settings -> API -> service_role) "
                            "which has permission to write backups, or add an RLS policy for bucket 'factory-data'."
                        )
                    else:
                        err_msg = f"Backup failed with status {r2.status_code}: {err_text}"
                    print(f"[CloudSync] {err_msg}")
                    return {"ok": False, "status": "Error", "bytes": 0, "error": err_msg}
            else:
                err_text = r.text[:250]
                if "row-level security" in err_text or "AccessDenied" in err_text:
                    err_msg = (
                        "Supabase RLS Block: The 'anon' public key cannot write to storage without an RLS policy. "
                        "Please use your 'service_role' secret key (Supabase -> Settings -> API -> service_role) "
                        "which has permission to write backups, or add an RLS policy for bucket 'factory-data'."
                    )
                else:
                    err_msg = f"Backup failed with status {r.status_code}: {err_text}"
                print(f"[CloudSync] {err_msg}")
                return {"ok": False, "status": "Error", "bytes": 0, "error": err_msg}
    except Exception as e:
        err_msg = f"Backup error: {e}"
        print(f"[CloudSync] {err_msg}")
        return {"ok": False, "status": "Exception", "bytes": 0, "error": err_msg}


def backup_to_cloud(force=False, c=None, config_override=None):
    """Upload current database to Supabase (returns bool for backward compatibility)."""
    res = upload_backup(force=force, c=c, config_override=config_override)
    return bool(res.get("ok"))


def restore_backup(c=None, config_override=None):
    """Download the latest database from Supabase Storage with detailed result."""
    cfg = get_config(c=c, config_override=config_override)
    if not (cfg.get("url") and cfg.get("key")):
        print("[CloudSync] Not configured (SUPABASE_URL/SUPABASE_KEY not set). Using local DB.")
        return {"ok": False, "status": "Not Configured", "bytes": 0, "error": "Supabase not configured"}

    url = f"{cfg['url']}/storage/v1/object/authenticated/{cfg['bucket']}/{FILENAME}"
    headers = {
        "apikey": cfg["key"],
        "Authorization": f"Bearer {cfg['key']}",
    }
    try:
        with httpx.Client(timeout=30.0) as client:
            r = client.get(url, headers=headers)
            if r.status_code == 200 and len(r.content) > 1000:
                db_file = config.db_path()
                os.makedirs(os.path.dirname(db_file), exist_ok=True)
                with open(db_file, "wb") as f:
                    f.write(r.content)
                global _last_sync_mtime
                _last_sync_mtime = os.path.getmtime(db_file)
                print(f"[CloudSync] Successfully restored database from cloud ({len(r.content)} bytes)")
                return {"ok": True, "status": "Restored", "bytes": len(r.content), "error": None}
            elif r.status_code in (404, 400):
                print("[CloudSync] No cloud backup found yet in bucket. Pushing initial DB...")
                up_res = upload_backup(force=True, c=c, config_override=config_override)
                return {
                    "ok": False,
                    "status": "Not Found",
                    "bytes": 0,
                    "pushed_initial": bool(up_res.get("ok")),
                    "error": "No cloud backup found yet in bucket",
                }
            else:
                err_msg = f"Restore failed with status {r.status_code}: {r.text[:200]}"
                print(f"[CloudSync] {err_msg}")
                return {"ok": False, "status": "Error", "bytes": 0, "error": err_msg}
    except Exception as e:
        err_msg = f"Restore error: {e}"
        print(f"[CloudSync] {err_msg}")
        return {"ok": False, "status": "Exception", "bytes": 0, "error": err_msg}


def restore_from_cloud(c=None, config_override=None):
    """Download latest database from cloud (returns bool for backward compatibility)."""
    res = restore_backup(c=c, config_override=config_override)
    return bool(res.get("ok"))


def sync_tables_direct(tables=None, c=None, config_override=None):
    """
    Optionally sync tables directly if Postgres connection string is provided,
    or sync tables via Supabase PostgREST REST API (/rest/v1/{table}).
    """
    cfg = get_config(c=c, config_override=config_override)
    postgres_url = cfg.get("postgres_url", "")
    url = cfg.get("url", "")
    key = cfg.get("key", "")

    if tables is None:
        tables = ["languages", "settings", "connections"]

    if not postgres_url and not (url and key):
        return {
            "ok": False,
            "error": "Neither Postgres connection string nor Supabase REST API credentials are configured.",
            "synced_tables": {},
            "errors": {},
        }

    synced_counts = {}
    errors = {}

    # Read records from local SQLite
    def _get_table_rows(cur, tbl_name):
        try:
            rows = cur.execute(f"SELECT * FROM {tbl_name}").fetchall()
            return [dict(r) for r in rows]
        except Exception as e:
            errors[tbl_name] = f"SQLite read error: {e}"
            return []

    from . import db
    local_data = {}
    if c is not None:
        for t in tables:
            local_data[t] = _get_table_rows(c, t)
    else:
        with db.ro() as ro_c:
            for t in tables:
                local_data[t] = _get_table_rows(ro_c, t)

    # 1. Direct Postgres sync if Postgres URL is provided and psycopg2 is available
    used_postgres = False
    if postgres_url:
        try:
            import psycopg2
            from psycopg2.extras import execute_values
            pg_conn = psycopg2.connect(postgres_url)
            cur = pg_conn.cursor()
            for tbl, rows in local_data.items():
                if not rows:
                    synced_counts[tbl] = 0
                    continue
                cols = list(rows[0].keys())
                col_names = ", ".join(cols)
                values = [[r[k] for k in cols] for r in rows]
                query = f"INSERT INTO {tbl} ({col_names}) VALUES %s ON CONFLICT DO NOTHING"
                try:
                    execute_values(cur, query, values)
                    synced_counts[tbl] = len(values)
                except Exception as sub_e:
                    errors[tbl] = str(sub_e)
            pg_conn.commit()
            pg_conn.close()
            used_postgres = True
            return {
                "ok": len(errors) == 0,
                "mode": "postgres",
                "synced_tables": synced_counts,
                "errors": errors,
            }
        except ImportError:
            pass  # psycopg2 not installed; fall through to REST API
        except Exception as pg_err:
            errors["postgres_connection"] = str(pg_err)

    # 2. Sync via Supabase PostgREST REST API
    if url and key:
        headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates",
        }
        with httpx.Client(timeout=30.0) as client:
            for tbl, rows in local_data.items():
                if tbl in errors:
                    continue
                if not rows:
                    synced_counts[tbl] = 0
                    continue
                try:
                    endpoint = f"{url}/rest/v1/{tbl}"
                    resp = client.post(endpoint, headers=headers, json=rows)
                    if resp.status_code in (200, 201, 204):
                        synced_counts[tbl] = len(rows)
                    else:
                        errors[tbl] = f"HTTP {resp.status_code}: {resp.text[:200]}"
                except Exception as req_err:
                    errors[tbl] = str(req_err)

        return {
            "ok": len(errors) == 0,
            "mode": "rest",
            "synced_tables": synced_counts,
            "errors": errors,
        }

    return {
        "ok": False,
        "mode": "failed",
        "synced_tables": synced_counts,
        "errors": errors,
    }


def start_background_sync(interval=15):
    """Periodically check if database was modified and upload."""
    global _bg_thread, _bg_stop_event
    if _bg_thread and _bg_thread.is_alive():
        return
    _bg_stop_event.clear()

    def _loop():
        while not _bg_stop_event.is_set():
            time.sleep(interval)
            try:
                if is_configured():
                    backup_to_cloud()
            except Exception as e:
                print(f"[CloudSync] Loop error: {e}")

    _bg_thread = threading.Thread(target=_loop, daemon=True)
    _bg_thread.start()
    print(f"[CloudSync] Background sync thread active (checking every {interval}s)")


def stop_background_sync():
    """Stop the background sync loop."""
    global _bg_stop_event
    _bg_stop_event.set()
