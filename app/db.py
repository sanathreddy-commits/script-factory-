import contextlib
import contextvars
import json
import os
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS languages(
  id INTEGER PRIMARY KEY, code TEXT UNIQUE, name TEXT, target INTEGER, cap_pair_min REAL DEFAULT 450,
  cap_person_min REAL DEFAULT 900, mode TEXT DEFAULT 'read_aloud', deadline TEXT DEFAULT '',
  lo INTEGER, hi INTEGER, c_lo INTEGER, c_hi INTEGER, s_lo INTEGER, s_hi INTEGER,
  names TEXT DEFAULT 'generic', seed INTEGER DEFAULT 1, planned INTEGER DEFAULT 0, created_at REAL);
CREATE TABLE IF NOT EXISTS quotas(language_id INTEGER, domain TEXT, subdomain TEXT, n INTEGER,
  PRIMARY KEY(language_id, subdomain));
CREATE TABLE IF NOT EXISTS scripts(
  id INTEGER PRIMARY KEY, language_id INTEGER, seq INTEGER, code TEXT UNIQUE, domain TEXT, subdomain TEXT,
  specialisation TEXT DEFAULT '', wave INTEGER, attrs TEXT, brief TEXT, status TEXT DEFAULT 'PLANNED',
  version INTEGER DEFAULT 0, words INTEGER DEFAULT 0, is_test INTEGER DEFAULT 0, source TEXT DEFAULT 'generated',
  prompt_version TEXT, spec_version INTEGER, judge TEXT, needs_review INTEGER DEFAULT 0, reject_reason TEXT,
  shingles BLOB, opening TEXT, created_at REAL, updated_at REAL, UNIQUE(language_id, seq));
CREATE INDEX IF NOT EXISTS ix_scripts_status ON scripts(language_id, status);
CREATE TABLE IF NOT EXISTS script_versions(script_id INTEGER, version INTEGER, turns TEXT, words INTEGER,
  prompt_version TEXT, note TEXT, created_at REAL, PRIMARY KEY(script_id, version));
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, script_id INTEGER UNIQUE, status TEXT DEFAULT 'QUEUED',
  attempts INTEGER DEFAULT 0, fails INTEGER DEFAULT 0, lease_until REAL DEFAULT 0, key_id INTEGER, error TEXT,
  cost REAL DEFAULT 0, calls INTEGER DEFAULT 0, seed INTEGER DEFAULT 0, updated_at REAL);
CREATE TABLE IF NOT EXISTS api_keys(id INTEGER PRIMARY KEY, label TEXT, provider TEXT, model TEXT, role TEXT,
  languages TEXT DEFAULT '*', api_key TEXT DEFAULT '', base_url TEXT DEFAULT '', rpm INTEGER DEFAULT 30,
  daily_cap REAL DEFAULT 500, monthly_cap REAL DEFAULT 5000, price_in REAL DEFAULT 0, price_out REAL DEFAULT 0,
  status TEXT DEFAULT 'active', cool_until REAL DEFAULT 0, spent_day REAL DEFAULT 0, spent_month REAL DEFAULT 0,
  day TEXT DEFAULT '', month TEXT DEFAULT '', n429 INTEGER DEFAULT 0, ok INTEGER DEFAULT 0, fail INTEGER DEFAULT 0,
  consec_fail INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, phone TEXT DEFAULT '', email TEXT DEFAULT '',
  role TEXT, language_id INTEGER, code TEXT UNIQUE, active INTEGER DEFAULT 1, gender TEXT DEFAULT '',
  perm TEXT DEFAULT 'manage', device TEXT, password_hash TEXT DEFAULT '', password_salt TEXT DEFAULT '',
  consent_at REAL, consent_v TEXT, optin INTEGER DEFAULT 0,
  opening_min REAL DEFAULT 0, availability TEXT DEFAULT 'All day', created_at REAL, last_seen REAL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS ux_user_phone ON users(phone, language_id) WHERE phone<>'' AND role='participant';
CREATE UNIQUE INDEX IF NOT EXISTS ux_user_email ON users(email, language_id) WHERE email<>'' AND role='participant';
CREATE TABLE IF NOT EXISTS supervisor_languages(user_id INTEGER, language_id INTEGER, PRIMARY KEY(user_id, language_id));
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, csrf TEXT, created REAL);
CREATE TABLE IF NOT EXISTS device_requests(id INTEGER PRIMARY KEY, user_id INTEGER, device TEXT,
  status TEXT DEFAULT 'PENDING', created REAL);
CREATE TABLE IF NOT EXISTS pairs(id INTEGER PRIMARY KEY, language_id INTEGER, a INTEGER, b INTEGER,
  status TEXT DEFAULT 'ACTIVE', availability TEXT DEFAULT 'All day', opening_min REAL DEFAULT 0, created_at REAL, dropped_at REAL, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS assignments(id INTEGER PRIMARY KEY, script_id INTEGER, version INTEGER, pair_id INTEGER,
  ua INTEGER, ub INTEGER, status TEXT, est_min REAL, conf_min REAL, assigned_at REAL, deadline REAL,
  nudged INTEGER DEFAULT 0, started_at REAL, ready_a REAL DEFAULT 0, ready_b REAL DEFAULT 0, done_a REAL, done_b REAL,
  proof_img TEXT DEFAULT '', review_status TEXT DEFAULT 'PENDING', review_note TEXT DEFAULT '', submitted_at REAL DEFAULT 0,
  client_session TEXT, verified INTEGER DEFAULT 0, redo INTEGER DEFAULT 0, note TEXT DEFAULT '', custom_roles TEXT DEFAULT '', updated_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_script ON assignments(script_id)
  WHERE status NOT IN ('RELEASED','ABANDONED','REJECTED');
CREATE INDEX IF NOT EXISTS ix_assign_pair ON assignments(pair_id, status);
CREATE TABLE IF NOT EXISTS ledger(id INTEGER PRIMARY KEY, ts REAL, assignment_id INTEGER, user_id INTEGER,
  pair_id INTEGER, minutes REAL, kind TEXT);
CREATE TABLE IF NOT EXISTS issues(id INTEGER PRIMARY KEY, assignment_id INTEGER, user_id INTEGER, language_id INTEGER,
  kind TEXT, note TEXT, route TEXT, status TEXT DEFAULT 'OPEN', created REAL);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, script_id INTEGER, version INTEGER, reviewer INTEGER,
  verdict TEXT, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS orphans(id INTEGER PRIMARY KEY, ts REAL, payload TEXT, status TEXT DEFAULT 'OPEN');
CREATE TABLE IF NOT EXISTS import_batches(id INTEGER PRIMARY KEY, language_id INTEGER, payload TEXT, created REAL);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, ts REAL, actor TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS connections(id INTEGER PRIMARY KEY, name TEXT UNIQUE, service TEXT, status TEXT DEFAULT 'Connected', last_tested REAL, config TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS dev_keys(id INTEGER PRIMARY KEY, name TEXT, service TEXT, label TEXT, key_val TEXT, status TEXT DEFAULT 'Active', created_at REAL);
CREATE TABLE IF NOT EXISTS app_logs(id INTEGER PRIMARY KEY, ts REAL, category TEXT, user_id INTEGER, actor TEXT, message TEXT, detail TEXT);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
"""

PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS languages(
  id SERIAL PRIMARY KEY, code TEXT UNIQUE, name TEXT, target INTEGER, cap_pair_min DOUBLE PRECISION DEFAULT 450,
  cap_person_min DOUBLE PRECISION DEFAULT 900, mode TEXT DEFAULT 'read_aloud', deadline TEXT DEFAULT '',
  lo INTEGER, hi INTEGER, c_lo INTEGER, c_hi INTEGER, s_lo INTEGER, s_hi INTEGER,
  names TEXT DEFAULT 'generic', seed INTEGER DEFAULT 1, planned INTEGER DEFAULT 0, created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS quotas(language_id INTEGER, domain TEXT, subdomain TEXT, n INTEGER,
  PRIMARY KEY(language_id, subdomain));
CREATE TABLE IF NOT EXISTS scripts(
  id SERIAL PRIMARY KEY, language_id INTEGER, seq INTEGER, code TEXT UNIQUE, domain TEXT, subdomain TEXT,
  specialisation TEXT DEFAULT '', wave INTEGER, attrs TEXT, brief TEXT, status TEXT DEFAULT 'PLANNED',
  version INTEGER DEFAULT 0, words INTEGER DEFAULT 0, is_test INTEGER DEFAULT 0, source TEXT DEFAULT 'generated',
  prompt_version TEXT, spec_version INTEGER, judge TEXT, needs_review INTEGER DEFAULT 0, reject_reason TEXT,
  shingles BYTEA, opening TEXT, created_at DOUBLE PRECISION, updated_at DOUBLE PRECISION, UNIQUE(language_id, seq));
CREATE INDEX IF NOT EXISTS ix_scripts_status ON scripts(language_id, status);
CREATE TABLE IF NOT EXISTS script_versions(script_id INTEGER, version INTEGER, turns TEXT, words INTEGER,
  prompt_version TEXT, note TEXT, created_at DOUBLE PRECISION, PRIMARY KEY(script_id, version));
CREATE TABLE IF NOT EXISTS jobs(id SERIAL PRIMARY KEY, script_id INTEGER UNIQUE, status TEXT DEFAULT 'QUEUED',
  attempts INTEGER DEFAULT 0, fails INTEGER DEFAULT 0, lease_until DOUBLE PRECISION DEFAULT 0, key_id INTEGER, error TEXT,
  cost DOUBLE PRECISION DEFAULT 0, calls INTEGER DEFAULT 0, seed INTEGER DEFAULT 0, updated_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS api_keys(id SERIAL PRIMARY KEY, label TEXT, provider TEXT, model TEXT, role TEXT,
  languages TEXT DEFAULT '*', api_key TEXT DEFAULT '', base_url TEXT DEFAULT '', rpm INTEGER DEFAULT 30,
  daily_cap DOUBLE PRECISION DEFAULT 500, monthly_cap DOUBLE PRECISION DEFAULT 5000, price_in DOUBLE PRECISION DEFAULT 0, price_out DOUBLE PRECISION DEFAULT 0,
  status TEXT DEFAULT 'active', cool_until DOUBLE PRECISION DEFAULT 0, spent_day DOUBLE PRECISION DEFAULT 0, spent_month DOUBLE PRECISION DEFAULT 0,
  day TEXT DEFAULT '', month TEXT DEFAULT '', n429 INTEGER DEFAULT 0, ok INTEGER DEFAULT 0, fail INTEGER DEFAULT 0,
  consec_fail INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS users(id SERIAL PRIMARY KEY, name TEXT, phone TEXT DEFAULT '', email TEXT DEFAULT '',
  role TEXT, language_id INTEGER, code TEXT UNIQUE, active INTEGER DEFAULT 1, gender TEXT DEFAULT '',
  perm TEXT DEFAULT 'manage', device TEXT, password_hash TEXT DEFAULT '', password_salt TEXT DEFAULT '',
  consent_at DOUBLE PRECISION, consent_v TEXT, optin INTEGER DEFAULT 0,
  opening_min DOUBLE PRECISION DEFAULT 0, availability TEXT DEFAULT 'All day', created_at DOUBLE PRECISION, last_seen DOUBLE PRECISION DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS ux_user_phone ON users(phone, language_id) WHERE phone<>'' AND role='participant';
CREATE UNIQUE INDEX IF NOT EXISTS ux_user_email ON users(email, language_id) WHERE email<>'' AND role='participant';
CREATE TABLE IF NOT EXISTS supervisor_languages(user_id INTEGER, language_id INTEGER, PRIMARY KEY(user_id, language_id));
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, csrf TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS device_requests(id SERIAL PRIMARY KEY, user_id INTEGER, device TEXT,
  status TEXT DEFAULT 'PENDING', created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS pairs(id SERIAL PRIMARY KEY, language_id INTEGER, a INTEGER, b INTEGER,
  status TEXT DEFAULT 'ACTIVE', availability TEXT DEFAULT 'All day', opening_min DOUBLE PRECISION DEFAULT 0, created_at DOUBLE PRECISION, dropped_at DOUBLE PRECISION, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS assignments(id SERIAL PRIMARY KEY, script_id INTEGER, version INTEGER, pair_id INTEGER,
  ua INTEGER, ub INTEGER, status TEXT, est_min DOUBLE PRECISION, conf_min DOUBLE PRECISION, assigned_at DOUBLE PRECISION, deadline DOUBLE PRECISION,
  nudged INTEGER DEFAULT 0, started_at DOUBLE PRECISION, ready_a DOUBLE PRECISION DEFAULT 0, ready_b DOUBLE PRECISION DEFAULT 0, done_a DOUBLE PRECISION, done_b DOUBLE PRECISION,
  proof_img TEXT DEFAULT '', review_status TEXT DEFAULT 'PENDING', review_note TEXT DEFAULT '', submitted_at DOUBLE PRECISION DEFAULT 0,
  client_session TEXT, verified INTEGER DEFAULT 0, redo INTEGER DEFAULT 0, note TEXT DEFAULT '', custom_roles TEXT DEFAULT '', updated_at DOUBLE PRECISION);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_script ON assignments(script_id)
  WHERE status NOT IN ('RELEASED','ABANDONED','REJECTED');
CREATE INDEX IF NOT EXISTS ix_assign_pair ON assignments(pair_id, status);
CREATE TABLE IF NOT EXISTS ledger(id SERIAL PRIMARY KEY, ts DOUBLE PRECISION, assignment_id INTEGER, user_id INTEGER,
  pair_id INTEGER, minutes DOUBLE PRECISION, kind TEXT);
CREATE TABLE IF NOT EXISTS issues(id SERIAL PRIMARY KEY, assignment_id INTEGER, user_id INTEGER, language_id INTEGER,
  kind TEXT, note TEXT, route TEXT, status TEXT DEFAULT 'OPEN', created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS reviews(id SERIAL PRIMARY KEY, script_id INTEGER, version INTEGER, reviewer INTEGER,
  verdict TEXT, reason TEXT, ts DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS orphans(id SERIAL PRIMARY KEY, ts DOUBLE PRECISION, payload TEXT, status TEXT DEFAULT 'OPEN');
CREATE TABLE IF NOT EXISTS import_batches(id SERIAL PRIMARY KEY, language_id INTEGER, payload TEXT, created DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS audit(id SERIAL PRIMARY KEY, ts DOUBLE PRECISION, actor TEXT, action TEXT, detail TEXT);
CREATE TABLE IF NOT EXISTS connections(id SERIAL PRIMARY KEY, name TEXT UNIQUE, service TEXT, status TEXT DEFAULT 'Connected', last_tested DOUBLE PRECISION, config TEXT DEFAULT '{}');
CREATE TABLE IF NOT EXISTS dev_keys(id SERIAL PRIMARY KEY, name TEXT, service TEXT, label TEXT, key_val TEXT, status TEXT DEFAULT 'Active', created_at DOUBLE PRECISION);
CREATE TABLE IF NOT EXISTS app_logs(id SERIAL PRIMARY KEY, ts DOUBLE PRECISION, category TEXT, user_id INTEGER, actor TEXT, message TEXT, detail TEXT);
CREATE OR REPLACE FUNCTION sf_immutable() RETURNS trigger AS $sf$ BEGIN RAISE EXCEPTION 'row is immutable / append-only'; END; $sf$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_no_update ON audit;
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit FOR EACH ROW EXECUTE FUNCTION sf_immutable();
DROP TRIGGER IF EXISTS audit_no_delete ON audit;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit FOR EACH ROW EXECUTE FUNCTION sf_immutable();
DROP TRIGGER IF EXISTS ledger_no_update ON ledger;
CREATE TRIGGER ledger_no_update BEFORE UPDATE ON ledger FOR EACH ROW EXECUTE FUNCTION sf_immutable();
DROP TRIGGER IF EXISTS ledger_no_delete ON ledger;
CREATE TRIGGER ledger_no_delete BEFORE DELETE ON ledger FOR EACH ROW EXECUTE FUNCTION sf_immutable();
"""

# Every scripts column except the ~6 KB duplicate-detection fingerprint (shingles). Use instead of s.* wherever
# the fingerprint is not needed: loading it for every script made each management page pull 1-2.5 MB from
# Supabase (egress quota) for data that was never shown.
SCRIPT_COLS = ", ".join("s." + col for col in (
    "id", "language_id", "seq", "code", "domain", "subdomain", "specialisation", "wave", "attrs", "brief", "status",
    "version", "words", "is_test", "source", "prompt_version", "spec_version", "judge", "needs_review",
    "reject_reason", "opening", "created_at", "updated_at"))

DEFAULTS = {
    "words_min": "1200", "words_max": "1500", "max_turn_words": "120", "sim_threshold": "0.15",
    "avg_minutes": "10", "open_limit": "5", "sitting": "3", "deadline_hours": "72", "abandon_hours": "6",
    "review_pct": "10", "reject_stop_pct": "8", "reject_stop_min": "20", "stop_after_review": "0",
    "banned_topics": "", "allow_test_scripts": "0", "verification_mode": "self_report",
    "session_min_min": "5", "session_max_min": "15", "pair_limit_h": "7.5", "user_limit_h": "15.0",
    "dur_min_sec": "300", "dur_max_sec": "900", "client_url": "https://client-portal.recording-system.com",
    "global_cap_inr": "5000", "pause_generation": "0", "rate_per_hour_inr": "0", "spec_version": "1",
    "prompt_version": "p1", "master_prompt": "", "tokens_per_word": "6", "stub_chaos": "0.0",
    "wave1_size": "100", "wave_size": "250",
    "lead_whatsapp_nudge": "Hi {name}, you have scripts waiting to record. Please complete them today.",
    "smtp_host": "smtp.gmail.com", "smtp_port": "587", "smtp_user": "", "smtp_pass": "",
    "smtp_from": "notifications@scriptflow.com", "smtp_tls": "1",
    "supabase_url": "", "supabase_key": "", "supabase_bucket": "factory-data", "supabase_postgres_url": "",
}


def connect():
    dsn = config.postgres_dsn()
    if dsn:
        from . import pgcompat
        return pgcompat.connect(dsn)
    p = config.db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p, timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=30000")
    return c


# Per-read cache: inside `with ro()` the settings table is read once instead of on every
# setting()/S() call (a page used to re-read it ~50 times, each a round trip to Supabase).
# Write transactions never use it, and any write transaction clears the cache of the read
# block it runs inside, so a value written mid-request is never served stale.
_READ_CACHE = contextvars.ContextVar("sf_read_cache", default=None)


def read_cache():
    """The current read block's scratch dict, or None inside a write transaction / outside ro()."""
    return _READ_CACHE.get()


@contextlib.contextmanager
def tx():
    """Write transaction. Never nest: open one, pass `c` down."""
    outer = _READ_CACHE.get()
    token = _READ_CACHE.set(None)
    c = connect()
    try:
        c.execute("BEGIN IMMEDIATE")
        yield c
        c.execute("COMMIT")
    except BaseException:
        try:
            c.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        c.close()
        _READ_CACHE.reset(token)
        if outer is not None:
            outer.clear()


@contextlib.contextmanager
def ro():
    token = _READ_CACHE.set({})
    c = connect()
    try:
        yield c
    finally:
        c.close()
        _READ_CACHE.reset(token)


def init_db():
    c = connect()
    migrations = [
        ("users", "password_hash", "TEXT DEFAULT ''"),
        ("users", "password_salt", "TEXT DEFAULT ''"),
        ("assignments", "proof_img", "TEXT DEFAULT ''"),
        ("assignments", "review_status", "TEXT DEFAULT 'PENDING'"),
        ("assignments", "review_note", "TEXT DEFAULT ''"),
        ("assignments", "submitted_at", "REAL DEFAULT 0"),
        ("assignments", "proof_drive_url", "TEXT DEFAULT ''"),
        ("users", "code_sent_at", "REAL DEFAULT 0"),
        ("pairs", "availability", "TEXT DEFAULT 'All day'"),
    ]
    if config.using_postgres():
        c.executescript(PG_SCHEMA)
        for table, col, col_def in migrations:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {col_def.replace('REAL', 'DOUBLE PRECISION')}")
            except Exception:
                pass
    else:
        c.executescript(SCHEMA)
        # Automatic column migrations for existing databases
        for table, col, col_def in migrations:
            try:
                existing = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
                if col not in existing:
                    c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
            except Exception:
                pass
    # Migrate the participant uniqueness indexes to be PER-LANGUAGE so the same person
    # (phone/email) can be added under different languages. Safe + idempotent on both engines.
    for drop_sql, create_sql in [
        ("DROP INDEX IF EXISTS ux_user_phone",
         "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_phone ON users(phone, language_id) WHERE phone<>'' AND role='participant'"),
        ("DROP INDEX IF EXISTS ux_user_email",
         "CREATE UNIQUE INDEX IF NOT EXISTS ux_user_email ON users(email, language_id) WHERE email<>'' AND role='participant'"),
    ]:
        try:
            c.execute(drop_sql)
            c.execute(create_sql)
        except Exception:
            pass
    for k, v in DEFAULTS.items():
        c.execute("INSERT OR IGNORE INTO settings(k,v) VALUES(?,?)", (k, v))
    try:
        now = time.time()
        c.execute("INSERT OR IGNORE INTO connections(name, service, status, last_tested, config) VALUES(?,?,?,?,?)",
                  ("Email Service", "Email Service", "Connected", now, '{}'))
    except Exception:
        pass
    c.close()


def _settings_rows(c):
    cache = _READ_CACHE.get()
    if cache is not None and "settings" in cache:
        return cache["settings"]
    rows = {r["k"]: r["v"] for r in c.execute("SELECT k,v FROM settings")}
    if cache is not None:
        cache["settings"] = rows
    return rows


def S(c):
    d = dict(DEFAULTS)
    d.update(_settings_rows(c))
    return d


def setting(c, k, default=None):
    if _READ_CACHE.get() is not None:
        rows = _settings_rows(c)
        return rows[k] if k in rows else DEFAULTS.get(k, default)
    r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else DEFAULTS.get(k, default)


def set_setting(c, k, v):
    c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
    cache = _READ_CACHE.get()
    if cache is not None:
        cache.pop("settings", None)


def audit(c, actor, action, detail=""):
    c.execute("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)",
              (time.time(), actor if isinstance(actor, str) else actor_name(actor), action, str(detail)[:2000]))


def log_app(c, category, actor, message, detail="", user_id=None):
    actor_str = actor if isinstance(actor, str) else actor_name(actor)
    uid = user_id
    if uid is None and hasattr(actor, "keys") and "id" in actor.keys():
        uid = actor["id"]
    c.execute(
        "INSERT INTO app_logs(ts, category, user_id, actor, message, detail) VALUES(?,?,?,?,?,?)",
        (time.time(), category, uid, actor_str, message, str(detail)[:2000])
    )


def actor_name(u):
    if u is None:
        return "system"
    return f"{u['role']}:{u['id']}:{u['name']}"


def j(x):
    return json.dumps(x, ensure_ascii=False)

