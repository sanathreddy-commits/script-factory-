"""Postgres compatibility layer for ScriptFactory.

The whole application was written against SQLite (``sqlite3`` style API, ``?`` placeholders,
``INSERT OR IGNORE`` / ``INSERT OR REPLACE``, ``last_insert_rowid()``). To make Supabase
Postgres the *primary* durable store without rewriting ~760 query call sites, this module
wraps psycopg2 with a thin connection/cursor that:

  * translates the handful of SQLite-only SQL idioms this codebase actually uses,
  * returns rows that behave like ``sqlite3.Row`` (index AND name access, ``.keys()``,
    ``.get()``, ``dict(row)``, tuple-unpacking, Jinja ``row.col`` / ``row['col']``),
  * mirrors SQLite's ``isolation_level=None`` autocommit + explicit ``BEGIN``/``COMMIT``.

SQLite remains the secondary/fallback engine (used automatically when no Postgres URL is
configured), so nothing breaks when running locally.
"""
import re
import threading

import psycopg2
import psycopg2.extensions
import psycopg2.pool

# Python bytes -> bytea (shingles BLOB column) without the caller wrapping in psycopg2.Binary.
psycopg2.extensions.register_adapter(bytes, psycopg2.Binary)

# Tables with an auto-increment integer "id" PK -> INSERTs get "RETURNING id" so lastrowid works.
_ID_TABLES = {
    "languages", "scripts", "jobs", "api_keys", "users", "device_requests", "pairs",
    "assignments", "ledger", "issues", "reviews", "orphans", "import_batches", "audit",
    "connections", "dev_keys", "app_logs",
}

# Conflict targets for INSERT OR REPLACE (the only two tables that use it).
_REPLACE_KEYS = {
    "settings": ["k"],
    "script_versions": ["script_id", "version"],
}

_INSERT_TABLE_RE = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", re.I)
_OR_IGNORE_RE = re.compile(r"^(\s*)INSERT\s+OR\s+IGNORE\s+INTO", re.I)
_OR_REPLACE_RE = re.compile(r"^(\s*)INSERT\s+OR\s+REPLACE\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)", re.I)
_IFNULL_RE = re.compile(r"\bIFNULL\s*\(", re.I)


def _translate(sql: str, has_params: bool):
    """Return (pg_sql, kind) where kind is 'pragma' (no-op), 'begin', 'commit', 'rollback',
    'lastid' (SELECT last_insert_rowid()) or '' (ordinary)."""
    s = sql.strip()
    head = s[:6].upper()

    if head.startswith("PRAGMA"):
        return "", "pragma"
    if re.match(r"^BEGIN\b", s, re.I):
        return "", "begin"
    if re.match(r"^COMMIT\b", s, re.I):
        return "", "commit"
    if re.match(r"^ROLLBACK\b", s, re.I):
        return "", "rollback"
    if re.search(r"last_insert_rowid\s*\(\s*\)", s, re.I):
        return "", "lastid"

    want_returning = False

    # INSERT OR IGNORE -> INSERT ... ON CONFLICT DO NOTHING
    m = _OR_IGNORE_RE.match(s)
    if m:
        s = _OR_IGNORE_RE.sub(r"\1INSERT INTO", s, count=1)
        tbl_m = _INSERT_TABLE_RE.match(s)
        s = s.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"
        if tbl_m and tbl_m.group(1).lower() in _ID_TABLES:
            want_returning = True
    else:
        # INSERT OR REPLACE -> INSERT ... ON CONFLICT (keys) DO UPDATE SET ...
        mr = _OR_REPLACE_RE.match(s)
        if mr:
            table = mr.group(2)
            cols = [c.strip() for c in mr.group(3).split(",") if c.strip()]
            keys = _REPLACE_KEYS.get(table.lower(), ["id"])
            updates = [f"{c}=excluded.{c}" for c in cols if c not in keys]
            s = _OR_REPLACE_RE.sub(rf"\1INSERT INTO {table}({mr.group(3)})", s, count=1)
            s = s.rstrip().rstrip(";")
            if updates:
                s += f" ON CONFLICT ({', '.join(keys)}) DO UPDATE SET " + ", ".join(updates)
            else:
                s += f" ON CONFLICT ({', '.join(keys)}) DO NOTHING"
        else:
            # Plain INSERT into an id-PK table -> capture the new id.
            tbl_m = _INSERT_TABLE_RE.match(s)
            if tbl_m and tbl_m.group(1).lower() in _ID_TABLES and "RETURNING" not in s.upper():
                want_returning = True

    # IFNULL -> COALESCE
    s = _IFNULL_RE.sub("COALESCE(", s)

    if want_returning:
        s = s.rstrip().rstrip(";") + " RETURNING id"

    # Placeholder + literal-percent handling (only when params are bound).
    if has_params:
        s = s.replace("%", "%%").replace("?", "%s")

    return s, ("returning" if want_returning else "")


class Row:
    """sqlite3.Row-compatible row: positional index, name index, .keys(), .get(),
    membership, length, value iteration (tuple-unpack), and dict(row)."""
    __slots__ = ("_cols", "_vals", "_map")

    def __init__(self, cols, vals):
        self._cols = cols
        self._vals = vals
        self._map = {c: i for i, c in enumerate(cols)}

    def __getitem__(self, k):
        if isinstance(k, int):
            return self._vals[k]
        return self._vals[self._map[k]]

    def keys(self):
        return list(self._cols)

    def get(self, k, default=None):
        i = self._map.get(k)
        return self._vals[i] if i is not None else default

    def __contains__(self, k):
        return k in self._map

    def __iter__(self):
        return iter(self._vals)

    def __len__(self):
        return len(self._vals)

    def __repr__(self):
        return f"Row({dict(zip(self._cols, self._vals))!r})"


def _to_row(cols, raw):
    vals = [bytes(v) if isinstance(v, memoryview) else v for v in raw]
    return Row(cols, vals)


class _EmptyCursor:
    """Returned for no-op statements (PRAGMA). Behaves like an exhausted cursor."""
    lastrowid = None
    rowcount = -1
    description = None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def fetchmany(self, n=1):
        return []

    def __iter__(self):
        return iter(())


class _ListCursor(_EmptyCursor):
    """Returned for synthesized results (SELECT last_insert_rowid())."""

    def __init__(self, rows):
        self._rows = list(rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        r, self._rows = self._rows, []
        return r

    def fetchmany(self, n=1):
        r, self._rows = self._rows[:n], self._rows[n:]
        return r

    def __iter__(self):
        return iter(self._rows)


class PGCursor:
    """Wraps a psycopg2 cursor, yielding Row objects and exposing lastrowid."""

    def __init__(self, cur, lastrowid=None):
        self._cur = cur
        self.lastrowid = lastrowid
        self.rowcount = cur.rowcount if cur is not None else -1
        self.description = cur.description if cur is not None else None

    def _cols(self):
        return [d.name for d in self._cur.description] if self._cur.description else []

    def fetchone(self):
        raw = self._cur.fetchone()
        return _to_row(self._cols(), raw) if raw is not None else None

    def fetchall(self):
        cols = self._cols()
        return [_to_row(cols, r) for r in self._cur.fetchall()]

    def fetchmany(self, n=1):
        cols = self._cols()
        return [_to_row(cols, r) for r in self._cur.fetchmany(n)]

    def __iter__(self):
        cols = self._cols()
        for r in self._cur:
            yield _to_row(cols, r)


class PGConnection:
    """sqlite3.Connection-compatible wrapper over a pooled psycopg2 connection."""

    def __init__(self, pool):
        self._pool = pool
        self._raw = pool.getconn()
        self._raw.autocommit = True
        self._last_id = None
        self.row_factory = None  # accepted + ignored (always Row)

    # -- sqlite3.Connection surface -------------------------------------------------
    def execute(self, sql, params=()):
        has_params = params is not None and len(params) > 0
        pg_sql, kind = _translate(sql, has_params)

        if kind == "pragma":
            return _EmptyCursor()
        if kind == "begin":
            self._raw.autocommit = False
            return _EmptyCursor()
        if kind == "commit":
            self._raw.commit()
            self._raw.autocommit = True
            return _EmptyCursor()
        if kind == "rollback":
            self._raw.rollback()
            self._raw.autocommit = True
            return _EmptyCursor()
        if kind == "lastid":
            return _ListCursor([_to_row(["last_insert_rowid"], [self._last_id])])

        cur = self._raw.cursor()
        cur.execute(pg_sql, tuple(params) if has_params else None)

        if kind == "returning":
            try:
                row = cur.fetchone()
                self._last_id = row[0] if row else None
            except psycopg2.ProgrammingError:
                self._last_id = None
            return PGCursor(cur, lastrowid=self._last_id)
        return PGCursor(cur, lastrowid=self._last_id)

    def executescript(self, sql):
        # psycopg2 runs a multi-statement string (DDL, no params) in one call.
        cur = self._raw.cursor()
        cur.execute(sql)
        return PGCursor(cur)

    def cursor(self):
        return self  # the app only ever calls .execute on the result

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        try:
            if not self._raw.autocommit:
                self._raw.rollback()
                self._raw.autocommit = True
        except Exception:
            pass
        try:
            self._pool.putconn(self._raw)
        except Exception:
            try:
                self._raw.close()
            except Exception:
                pass


_pool = None
_pool_lock = threading.Lock()


def get_pool(dsn: str):
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = psycopg2.pool.ThreadedConnectionPool(5, 20, dsn=dsn)
        return _pool


def connect(dsn: str) -> PGConnection:
    return PGConnection(get_pool(dsn))
