"""Google Drive Cloud Storage & Backup integration for ScriptFlow.

Supports:
- Configuring credentials via:
  1. connections table (service = 'Google Drive', config JSON)
  2. settings table (google_drive_folder_id, google_application_credentials, google_service_account_json, google_api_key)
  3. Environment variables (GOOGLE_DRIVE_FOLDER_ID, GOOGLE_APPLICATION_CREDENTIALS, GOOGLE_API_KEY, GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON)
- test_connection(): validates access to the Google Drive folder.
- upload_backup(file_path): uploads timestamped database backup to Google Drive folder.
- upload_recording_file(file_path, remote_name): stores recording and proof assets in Drive.
- list_backups(): lists stored backups in Drive folder.
"""

from datetime import datetime
import json
import mimetypes
import os
import time
from typing import Any, Dict, List, Optional
import httpx

from .. import config, db


class DriveError(Exception):
    """Base exception for Google Drive operations."""
    pass


class DriveConnectionError(DriveError):
    """Raised when connecting or authenticating with Drive fails."""
    pass


class DriveUploadError(DriveError):
    """Raised when uploading a file to Drive fails."""
    pass


class DriveAuthError(DriveError):
    """Raised when credentials or authorization are invalid."""
    pass


def get_drive_config(c=None) -> Dict[str, Any]:
    """Retrieve Google Drive configuration with precedence:
    connections table -> settings table -> environment variables -> dev_keys table.
    """
    conn_cfg: Dict[str, Any] = {}
    settings_cfg: Dict[str, Any] = {}
    dev_key_val = ""

    def _read_from_cursor(cur):
        nonlocal conn_cfg, settings_cfg, dev_key_val
        # 1. connections table
        try:
            row = cur.execute(
                "SELECT config FROM connections WHERE service='Google Drive' OR name LIKE '%Google Drive%' LIMIT 1"
            ).fetchone()
            if row and row["config"]:
                parsed = json.loads(row["config"])
                if isinstance(parsed, dict):
                    conn_cfg = parsed
        except Exception:
            pass

        # 2. settings table
        try:
            settings_cfg = {
                "folder_id": db.setting(cur, "google_drive_folder_id", "") or "",
                "credentials_path": db.setting(cur, "google_application_credentials", "") or "",
                "service_account_json": db.setting(cur, "google_service_account_json", "") or "",
                "api_key": db.setting(cur, "google_api_key", "") or "",
                "access_token": db.setting(cur, "google_drive_access_token", "") or "",
            }
        except Exception:
            pass

        # 3. dev_keys table
        try:
            k_row = cur.execute(
                "SELECT key_val FROM dev_keys WHERE service IN ('Google Drive', 'Google') AND status='Active' LIMIT 1"
            ).fetchone()
            if k_row and k_row["key_val"]:
                dev_key_val = k_row["key_val"]
        except Exception:
            pass

    if c is not None:
        _read_from_cursor(c)
    else:
        try:
            with db.ro() as cur:
                _read_from_cursor(cur)
        except Exception:
            pass

    # Resolve folder_id
    folder_id = (
        conn_cfg.get("folder_id")
        or settings_cfg.get("folder_id")
        or os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "")
    ).strip()

    # Resolve credentials path / Service Account JSON
    credentials_path = (
        conn_cfg.get("credentials_path")
        or conn_cfg.get("service_account_path")
        or settings_cfg.get("credentials_path")
        or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "")
    ).strip()

    raw_sa_json = (
        conn_cfg.get("service_account_json")
        or conn_cfg.get("credentials_json")
        or settings_cfg.get("service_account_json")
        or os.environ.get("GOOGLE_DRIVE_SERVICE_ACCOUNT_JSON", "")
    )

    # Resolve api_key
    api_key = (
        conn_cfg.get("api_key")
        or settings_cfg.get("api_key")
        or os.environ.get("GOOGLE_API_KEY", "")
        or dev_key_val
    ).strip()

    # Resolve access_token
    access_token = (
        conn_cfg.get("access_token")
        or settings_cfg.get("access_token")
        or os.environ.get("GOOGLE_DRIVE_ACCESS_TOKEN", "")
    ).strip()

    # Parse service account JSON dict if available
    service_account_info: Optional[Dict[str, Any]] = None
    if isinstance(raw_sa_json, dict):
        service_account_info = raw_sa_json
    elif isinstance(raw_sa_json, str) and raw_sa_json.strip().startswith("{"):
        try:
            service_account_info = json.loads(raw_sa_json)
        except Exception:
            pass

    # If raw_sa_json not inline dict/json, check credentials_path
    if not service_account_info and credentials_path:
        if credentials_path.startswith("{"):
            try:
                service_account_info = json.loads(credentials_path)
            except Exception:
                pass
        elif os.path.exists(credentials_path):
            try:
                with open(credentials_path, "r", encoding="utf-8") as f:
                    service_account_info = json.load(f)
            except Exception:
                pass

    return {
        "folder_id": folder_id,
        "credentials_path": credentials_path,
        "service_account_json": raw_sa_json if isinstance(raw_sa_json, str) else json.dumps(raw_sa_json or {}),
        "service_account_info": service_account_info,
        "api_key": api_key,
        "access_token": access_token,
    }


def save_drive_config(
    folder_id: Optional[str] = None,
    service_account_json: Optional[str] = None,
    api_key: Optional[str] = None,
    credentials_path: Optional[str] = None,
    access_token: Optional[str] = None,
    c=None
) -> None:
    """Save Google Drive configuration into connections and settings tables."""
    def _do_save(cur):
        # Update settings table
        if folder_id is not None:
            db.set_setting(cur, "google_drive_folder_id", folder_id.strip())
        if service_account_json is not None:
            db.set_setting(cur, "google_service_account_json", service_account_json.strip())
        if api_key is not None:
            db.set_setting(cur, "google_api_key", api_key.strip())
        if credentials_path is not None:
            db.set_setting(cur, "google_application_credentials", credentials_path.strip())
        if access_token is not None:
            db.set_setting(cur, "google_drive_access_token", access_token.strip())

        # Update connections table config
        row = cur.execute(
            "SELECT id, config FROM connections WHERE service='Google Drive' OR name LIKE '%Google Drive%' LIMIT 1"
        ).fetchone()
        current_cfg: Dict[str, Any] = {}
        if row and row["config"]:
            try:
                current_cfg = json.loads(row["config"])
            except Exception:
                pass

        if folder_id is not None:
            current_cfg["folder_id"] = folder_id.strip()
        if service_account_json is not None:
            current_cfg["service_account_json"] = service_account_json.strip()
        if api_key is not None:
            current_cfg["api_key"] = api_key.strip()
        if credentials_path is not None:
            current_cfg["credentials_path"] = credentials_path.strip()
        if access_token is not None:
            current_cfg["access_token"] = access_token.strip()

        cfg_json = json.dumps(current_cfg)
        if row:
            cur.execute("UPDATE connections SET config=? WHERE id=?", (cfg_json, row["id"]))
        else:
            cur.execute(
                "INSERT INTO connections(name, service, status, last_tested, config) VALUES(?,?,?,?,?)",
                ("Google Drive Archival", "Google Drive", "Connected", time.time(), cfg_json)
            )

    if c is not None:
        _do_save(c)
    else:
        with db.tx() as cur:
            _do_save(cur)


def is_configured(config_dict: Optional[Dict[str, Any]] = None, c=None) -> bool:
    """Check if Google Drive has minimal configuration (folder_id + auth)."""
    cfg = config_dict or get_drive_config(c)
    has_folder = bool(cfg.get("folder_id"))
    has_auth = bool(
        cfg.get("service_account_info")
        or cfg.get("credentials_path")
        or cfg.get("api_key")
        or cfg.get("access_token")
    )
    return has_folder and has_auth


class GoogleDriveClient:
    """Client for interacting with Google Drive API v3."""

    def __init__(
        self,
        config: Optional[Dict[str, Any]] = None,
        db_conn=None,
        http_client: Optional[httpx.Client] = None
    ):
        self.config = config or get_drive_config(c=db_conn)
        self.folder_id = self.config.get("folder_id", "")
        self.api_key = self.config.get("api_key", "")
        self.access_token = self.config.get("access_token", "")
        self.service_account_info = self.config.get("service_account_info")
        self.credentials_path = self.config.get("credentials_path", "")
        self._http = http_client

    def _get_http_client(self) -> httpx.Client:
        if self._http is not None:
            return self._http
        return httpx.Client(timeout=60.0)

    def is_configured(self) -> bool:
        return is_configured(self.config)

    def get_auth_headers(self) -> Dict[str, str]:
        """Generate authentication headers based on available credentials."""
        headers: Dict[str, str] = {}

        # 1. Direct Bearer token
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
            return headers

        # 2. Service account token
        if self.service_account_info:
            # Check for embedded access token
            token = self.service_account_info.get("access_token") or self.service_account_info.get("token")
            if token:
                headers["Authorization"] = f"Bearer {token}"
                return headers

            # Try google.oauth2 if installed
            try:
                from google.oauth2 import service_account
                import google.auth.transport.requests
                creds = service_account.Credentials.from_service_account_info(
                    self.service_account_info,
                    scopes=["https://www.googleapis.com/auth/drive"]
                )
                req = google.auth.transport.requests.Request()
                creds.refresh(req)
                if creds.token:
                    headers["Authorization"] = f"Bearer {creds.token}"
                    return headers
            except Exception:
                pass

            # Fallback for Service Account client email header
            client_email = self.service_account_info.get("client_email")
            if client_email:
                headers["X-Service-Account"] = client_email

        # 3. API Key header
        if self.api_key:
            headers["X-Goog-Api-Key"] = self.api_key

        return headers

    def test_connection(self) -> Dict[str, Any]:
        """Validate access to the Google Drive folder."""
        if not self.folder_id:
            return {
                "success": False,
                "error": "Google Drive folder ID is not configured (GOOGLE_DRIVE_FOLDER_ID)."
            }

        headers = self.get_auth_headers()
        params: Dict[str, str] = {
            "fields": "id,name,mimeType,trashed"
        }
        if self.api_key and "Authorization" not in headers:
            params["key"] = self.api_key

        url = f"https://www.googleapis.com/drive/v3/files/{self.folder_id}"

        try:
            client = self._get_http_client()
            response = client.get(url, headers=headers, params=params)

            if response.status_code == 200:
                data = response.json()
                if data.get("trashed"):
                    return {
                        "success": False,
                        "error": f"Drive folder '{self.folder_id}' is in trash."
                    }
                folder_name = data.get("name", self.folder_id)
                return {
                    "success": True,
                    "folder_id": data.get("id"),
                    "folder_name": folder_name,
                    "mime_type": data.get("mimeType", "application/vnd.google-apps.folder"),
                    "message": f"Successfully connected to Google Drive folder '{folder_name}'"
                }
            elif response.status_code in (401, 403):
                err_text = self._extract_error_message(response)
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "error": f"Google Drive authorization failed ({response.status_code}): {err_text}"
                }
            elif response.status_code == 404:
                return {
                    "success": False,
                    "status_code": 404,
                    "error": f"Google Drive folder '{self.folder_id}' not found (404)."
                }
            else:
                err_text = self._extract_error_message(response)
                return {
                    "success": False,
                    "status_code": response.status_code,
                    "error": f"Drive API returned error ({response.status_code}): {err_text}"
                }
        except Exception as ex:
            return {
                "success": False,
                "error": f"Network or connection error connecting to Google Drive: {str(ex)}"
            }

    def upload_file(
        self,
        file_path: str,
        remote_name: Optional[str] = None,
        mime_type: Optional[str] = None,
        description: str = "",
        folder_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """Perform a multipart upload to Google Drive."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Local file not found: {file_path}")

        target_folder = folder_id or self.folder_id
        target_name = remote_name or os.path.basename(file_path)

        if not mime_type:
            guessed, _ = mimetypes.guess_type(target_name)
            mime_type = guessed or "application/octet-stream"

        with open(file_path, "rb") as f:
            file_bytes = f.read()

        file_size = len(file_bytes)

        # Metadata JSON part
        metadata: Dict[str, Any] = {
            "name": target_name,
            "description": description or f"Uploaded by ScriptFlow",
            "mimeType": mime_type
        }
        if target_folder:
            metadata["parents"] = [target_folder]

        boundary = f"==BOUNDARY_SCRIPTFLOW_{int(time.time()*1000)}=="
        boundary_bytes = boundary.encode("utf-8")
        meta_bytes = json.dumps(metadata).encode("utf-8")

        # Construct multipart/related body
        body = (
            b"--" + boundary_bytes + b"\r\n"
            b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
            + meta_bytes + b"\r\n"
            b"--" + boundary_bytes + b"\r\n"
            + f"Content-Type: {mime_type}\r\n\r\n".encode("utf-8")
            + file_bytes + b"\r\n"
            b"--" + boundary_bytes + b"--\r\n"
        )

        headers = self.get_auth_headers()
        headers["Content-Type"] = f"multipart/related; boundary={boundary}"

        params: Dict[str, str] = {
            "uploadType": "multipart",
            "supportsAllDrives": "true",
            "fields": "id,name,mimeType,size,webViewLink,createdTime"
        }
        if self.api_key and "Authorization" not in headers:
            params["key"] = self.api_key

        url = "https://www.googleapis.com/upload/drive/v3/files"

        try:
            client = self._get_http_client()
            response = client.post(url, headers=headers, params=params, content=body)

            if response.status_code in (200, 201):
                data = response.json()
                return {
                    "success": True,
                    "id": data.get("id"),
                    "name": data.get("name", target_name),
                    "mime_type": data.get("mimeType", mime_type),
                    "size": int(data.get("size") or file_size),
                    "web_view_link": data.get("webViewLink", ""),
                    "uploaded_at": time.time(),
                    "status": "success"
                }
            else:
                err_msg = self._extract_error_message(response)
                raise DriveUploadError(f"Drive upload failed ({response.status_code}): {err_msg}")
        except DriveError:
            raise
        except Exception as ex:
            raise DriveUploadError(f"Error during Drive upload: {str(ex)}") from ex

    def upload_backup(self, file_path: str, remote_name: Optional[str] = None) -> Dict[str, Any]:
        """Upload a timestamped database backup to Google Drive."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Database backup file not found: {file_path}")

        base_name = os.path.basename(file_path)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if remote_name:
            target_name = remote_name
        else:
            # If base name doesn't contain digits, add timestamp
            if not any(c.isdigit() for c in base_name):
                name_part, ext = os.path.splitext(base_name)
                target_name = f"{name_part}_backup_{timestamp}{ext or '.db'}"
            else:
                target_name = base_name

        result = self.upload_file(
            file_path=file_path,
            remote_name=target_name,
            mime_type="application/x-sqlite3",
            description="ScriptFlow database backup (factory.db)"
        )
        return result

    def upload_recording_file(self, file_path: str, remote_name: Optional[str] = None) -> Dict[str, Any]:
        """Store recording or proof asset in Google Drive."""
        if not os.path.exists(file_path):
            raise FileNotFoundError(f"Recording/proof file not found: {file_path}")

        target_name = remote_name or os.path.basename(file_path)
        guessed, _ = mimetypes.guess_type(target_name)
        mime_type = guessed or "application/octet-stream"

        result = self.upload_file(
            file_path=file_path,
            remote_name=target_name,
            mime_type=mime_type,
            description="ScriptFlow recording / proof asset"
        )
        return result

    def list_backups(self) -> List[Dict[str, Any]]:
        """List stored backups in Google Drive folder."""
        headers = self.get_auth_headers()
        params: Dict[str, str] = {
            "fields": "files(id,name,size,mimeType,createdTime,modifiedTime,webViewLink,webContentLink)",
            "orderBy": "createdTime desc"
        }
        if self.api_key and "Authorization" not in headers:
            params["key"] = self.api_key

        # Filter for folder and backup files (.db or name contains backup)
        if self.folder_id:
            params["q"] = f"'{self.folder_id}' in parents and trashed = false and (name contains 'backup' or name contains '.db' or mimeType = 'application/x-sqlite3')"
        else:
            params["q"] = "trashed = false and (name contains 'backup' or name contains '.db' or mimeType = 'application/x-sqlite3')"

        url = "https://www.googleapis.com/drive/v3/files"

        try:
            client = self._get_http_client()
            response = client.get(url, headers=headers, params=params)

            if response.status_code == 200:
                data = response.json()
                raw_files = data.get("files", [])
                results: List[Dict[str, Any]] = []
                for f in raw_files:
                    size_int = int(f.get("size") or 0)
                    if size_int >= 1024 * 1024:
                        size_str = f"{size_int / (1024*1024):.2f} MB"
                    elif size_int > 0:
                        size_str = f"{size_int / 1024:.1f} KB"
                    else:
                        size_str = "0 KB"

                    results.append({
                        "id": f.get("id"),
                        "name": f.get("name"),
                        "filename": f.get("name"),
                        "size": size_int,
                        "size_str": size_str,
                        "mime_type": f.get("mimeType"),
                        "created_time": f.get("createdTime"),
                        "modified_time": f.get("modifiedTime"),
                        "web_view_link": f.get("webViewLink", ""),
                        "web_content_link": f.get("webContentLink", "")
                    })
                return results
            else:
                err_text = self._extract_error_message(response)
                print(f"[GoogleDrive] list_backups failed ({response.status_code}): {err_text}")
                return []
        except Exception as ex:
            print(f"[GoogleDrive] list_backups error: {ex}")
            return []

    def _extract_error_message(self, response: httpx.Response) -> str:
        try:
            j = response.json()
            if "error" in j:
                err = j["error"]
                if isinstance(err, dict) and "message" in err:
                    return err["message"]
                return str(err)
        except Exception:
            pass
        return response.text[:300]


# -----------------------------------------------------------------------------
# Module-level convenience functions
# -----------------------------------------------------------------------------

def test_connection(config: Optional[Dict[str, Any]] = None, c=None) -> Dict[str, Any]:
    """Validate access to the Google Drive folder."""
    client = GoogleDriveClient(config=config, db_conn=c)
    return client.test_connection()


def upload_backup(
    file_path: str,
    remote_name: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    c=None
) -> Dict[str, Any]:
    """Upload a timestamped database backup to Google Drive."""
    client = GoogleDriveClient(config=config, db_conn=c)
    return client.upload_backup(file_path, remote_name=remote_name)


def upload_recording_file(
    file_path: str,
    remote_name: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    c=None
) -> Dict[str, Any]:
    """Store recording or proof asset in Google Drive."""
    client = GoogleDriveClient(config=config, db_conn=c)
    return client.upload_recording_file(file_path, remote_name=remote_name)


def list_backups(config: Optional[Dict[str, Any]] = None, c=None) -> List[Dict[str, Any]]:
    """List stored backups in Google Drive folder."""
    client = GoogleDriveClient(config=config, db_conn=c)
    return client.list_backups()
