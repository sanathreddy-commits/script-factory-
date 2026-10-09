import os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def db_path():
    return os.environ.get("SF_DB", os.path.join(BASE, "data", "factory.db"))


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
