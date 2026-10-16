import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def db_path():
    return os.environ.get("SF_DB", os.path.join(BASE, "data", "factory.db"))


def postgres_dsn():
    """Postgres connection string when Supabase Postgres is the primary store, else ''.

    Set SUPABASE_POSTGRES_URL (or POSTGRES_URL / DATABASE_URL) on the host to the Supabase
    connection pooler URI, e.g.
        postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:6543/postgres
    When unset, the app transparently falls back to the local SQLite file (the secondary).
    """
    return (
        os.environ.get("SUPABASE_POSTGRES_URL", "").strip()
        or os.environ.get("POSTGRES_URL", "").strip()
        or os.environ.get("DATABASE_URL", "").strip()
    )


def using_postgres():
    return bool(postgres_dsn())


def backup_dir():
    return os.environ.get("SF_BACKUP", os.path.join(BASE, "data", "backups"))


PROJECT_NAME = "ScriptFactory"
CONSENT_VERSION = "v1-DRAFT"
# DRAFT text. Your compliance owner must approve this before real participants see it.
CONSENT_TEXT = (
    "I agree that my voice recordings and the details I give (name, phone, language) "
    "will be used for this speech-data project. I understand the data is kept only for the "
    "project period and I can ask for deletion. "
)
OPTIN_TEXT = "I agree to share my contact with my recording partner (optional)."
