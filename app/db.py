import contextlib
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
  perm TEXT DEFAULT 'manage', device TEXT, consent_at REAL, consent_v TEXT, optin INTEGER DEFAULT 0,
  opening_min REAL DEFAULT 0, availability TEXT DEFAULT '[]', created_at REAL, last_seen REAL DEFAULT 0);
CREATE UNIQUE INDEX IF NOT EXISTS ux_user_phone ON users(phone) WHERE phone<>'' AND role='participant';
CREATE UNIQUE INDEX IF NOT EXISTS ux_user_email ON users(email) WHERE email<>'' AND role='participant';
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER, csrf TEXT, created REAL);
CREATE TABLE IF NOT EXISTS device_requests(id INTEGER PRIMARY KEY, user_id INTEGER, device TEXT,
  status TEXT DEFAULT 'PENDING', created REAL);
CREATE TABLE IF NOT EXISTS pairs(id INTEGER PRIMARY KEY, language_id INTEGER, a INTEGER, b INTEGER,
  status TEXT DEFAULT 'ACTIVE', opening_min REAL DEFAULT 0, created_at REAL, dropped_at REAL, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS assignments(id INTEGER PRIMARY KEY, script_id INTEGER, version INTEGER, pair_id INTEGER,
  ua INTEGER, ub INTEGER, status TEXT, est_min REAL, conf_min REAL, assigned_at REAL, deadline REAL,
  nudged INTEGER DEFAULT 0, started_at REAL, ready_a REAL DEFAULT 0, ready_b REAL DEFAULT 0, done_a REAL, done_b REAL,
  client_session TEXT, verified INTEGER DEFAULT 0, redo INTEGER DEFAULT 0, note TEXT DEFAULT '', custom_roles TEXT DEFAULT '', updated_at REAL);
CREATE UNIQUE INDEX IF NOT EXISTS ux_active_script ON assignments(script_id)
  WHERE status NOT IN ('RELEASED','ABANDONED');
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
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'audit is immutable'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_update BEFORE UPDATE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete BEFORE DELETE ON ledger BEGIN SELECT RAISE(ABORT,'ledger is append-only'); END;
"""

DEFAULTS = {
    "words_min": "1200", "words_max": "1500", "max_turn_words": "120", "sim_threshold": "0.15",
    "avg_minutes": "10", "open_limit": "5", "sitting": "3", "deadline_hours": "72", "abandon_hours": "6",
    "review_pct": "10", "reject_stop_pct": "8", "reject_stop_min": "20", "stop_after_review": "0",
    "banned_topics": "", "allow_test_scripts": "0", "verification_mode": "self_report",
    "dur_min_sec": "60", "dur_max_sec": "3600", "client_url": "", "global_cap_inr": "5000",
    "pause_generation": "0", "rate_per_hour_inr": "0", "spec_version": "1", "prompt_version": "p1",
    "master_prompt": "", "tokens_per_word": "6", "stub_chaos": "0.0", "wave1_size": "100", "wave_size": "250",
    "lead_whatsapp_nudge": "Hi {name}, you have scripts waiting to record. Please complete them today.",
}


def connect():
    p = config.db_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    c = sqlite3.connect(p, timeout=30, isolation_level=None, check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA foreign_keys=ON")
    c.execute("PRAGMA busy_timeout=30000")
    return c


@contextlib.contextmanager
def tx():
    """Write transaction. Never nest: open one, pass `c` down."""
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


@contextlib.contextmanager
def ro():
    c = connect()
    try:
        yield c
    finally:
        c.close()


def init_db():
    c = connect()
    c.executescript(SCHEMA)
    for k, v in DEFAULTS.items():
        c.execute("INSERT OR IGNORE INTO settings(k,v) VALUES(?,?)", (k, v))
    c.close()


def S(c):
    d = dict(DEFAULTS)
    for r in c.execute("SELECT k,v FROM settings"):
        d[r["k"]] = r["v"]
    return d


def setting(c, k, default=None):
    r = c.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
    return r["v"] if r else DEFAULTS.get(k, default)


def set_setting(c, k, v):
    c.execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))


def audit(c, actor, action, detail=""):
    c.execute("INSERT INTO audit(ts,actor,action,detail) VALUES(?,?,?,?)",
              (time.time(), actor if isinstance(actor, str) else actor_name(actor), action, str(detail)[:2000]))


def actor_name(u):
    if u is None:
        return "system"
    return f"{u['role']}:{u['id']}:{u['name']}"


def j(x):
    return json.dumps(x, ensure_ascii=False)
