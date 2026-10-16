"""
Google Sheets Live Sync & Mirroring Integration for ScriptFlow.

Connects ScriptFlow to Google Sheets for live mirroring and sync of:
- Agents Roster (Name, Phone, Email, Language, Availability, Status, Paired Partner)
- Pair Assignments (Pair, Language, Assigned count, Completed count, Hours recorded)
- Scripts Master List (Code, Language, Domain, Subdomain, Status, Word Count)

Supports configuring Google Sheet ID and Service Account / API Key via:
- connections table (service='Google Sheets')
- settings table (google_sheet_id, google_api_key, google_service_account)
- environment variables (GOOGLE_SHEET_ID, GOOGLE_API_KEY, GOOGLE_SERVICE_ACCOUNT_JSON)
"""

import base64
import contextlib
import json
import os
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
try:
    from Cryptodome.Hash import SHA256
    from Cryptodome.PublicKey import RSA
    from Cryptodome.Signature import pkcs1_15
except ImportError:
    SHA256 = None
    RSA = None
    pkcs1_15 = None

from .. import db

IST = ZoneInfo("Asia/Kolkata")
_token_cache: Dict[str, Any] = {}


def _b64url_encode(data: bytes) -> str:
    """Base64 URL-safe encode without padding."""
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


@contextlib.contextmanager
def _get_client(custom_client=None):
    """Context manager yielding the custom client if provided, or a new httpx.Client."""
    if custom_client is not None:
        yield custom_client
    else:
        with httpx.Client(timeout=30.0) as client:
            yield client


def _update_conn_status(
    conn,
    status: str,
    conn_id: Optional[int] = None,
    last_tested: Optional[float] = None
) -> None:
    """Helper to update the connections table status and last_tested timestamp."""
    if conn is None:
        return
    now = last_tested if last_tested is not None else time.time()
    try:
        if conn_id:
            conn.execute("UPDATE connections SET status=?, last_tested=? WHERE id=?", (status, now, conn_id))
        else:
            conn.execute(
                "UPDATE connections SET status=?, last_tested=? WHERE service='Google Sheets' OR name LIKE '%Google Sheets%'",
                (status, now)
            )
    except Exception:
        pass


def get_sheets_config(c=None) -> Dict[str, Any]:
    """
    Retrieve Google Sheets configuration from connections table, settings, or environment.
    Returns:
      {
        "sheet_id": str,
        "api_key": str,
        "service_account": str,
        "access_token": str,
        "connection_id": int | None
      }
    """
    cfg: Dict[str, Any] = {
        "sheet_id": "",
        "api_key": "",
        "service_account": "",
        "access_token": "",
        "connection_id": None,
    }

    def _read_from_conn(conn):
        nonlocal cfg
        try:
            row = conn.execute(
                "SELECT id, config FROM connections WHERE service='Google Sheets' OR name LIKE '%Google Sheets%' ORDER BY id ASC LIMIT 1"
            ).fetchone()
            if row:
                cfg["connection_id"] = row["id"]
                raw_cfg = row["config"]
                if raw_cfg:
                    try:
                        parsed = json.loads(raw_cfg)
                        if isinstance(parsed, dict):
                            cfg["sheet_id"] = (
                                parsed.get("sheet_id")
                                or parsed.get("google_sheet_id")
                                or parsed.get("spreadsheet_id")
                                or cfg["sheet_id"]
                            )
                            cfg["api_key"] = parsed.get("api_key") or parsed.get("google_api_key") or cfg["api_key"]
                            cfg["service_account"] = (
                                parsed.get("service_account")
                                or parsed.get("service_account_json")
                                or cfg["service_account"]
                            )
                            cfg["access_token"] = parsed.get("access_token") or cfg["access_token"]
                    except Exception:
                        pass
        except Exception:
            pass

    if c is not None:
        _read_from_conn(c)
    else:
        try:
            with db.ro() as ro_c:
                _read_from_conn(ro_c)
        except Exception:
            pass

    def _read_from_settings(conn):
        nonlocal cfg
        try:
            cfg["sheet_id"] = cfg["sheet_id"] or db.setting(conn, "google_sheet_id", "") or db.setting(conn, "sheets_id", "")
            cfg["api_key"] = cfg["api_key"] or db.setting(conn, "google_api_key", "")
            cfg["service_account"] = (
                cfg["service_account"]
                or db.setting(conn, "google_service_account", "")
                or db.setting(conn, "google_service_account_json", "")
            )
            cfg["access_token"] = cfg["access_token"] or db.setting(conn, "google_access_token", "")
        except Exception:
            pass

    if c is not None:
        _read_from_settings(c)
    else:
        try:
            with db.ro() as ro_c:
                _read_from_settings(ro_c)
        except Exception:
            pass

    # Fallback to environment variables
    cfg["sheet_id"] = (cfg["sheet_id"] or os.environ.get("GOOGLE_SHEET_ID", "") or os.environ.get("SPREADSHEET_ID", "")).strip()
    cfg["api_key"] = (cfg["api_key"] or os.environ.get("GOOGLE_API_KEY", "")).strip()
    cfg["service_account"] = (
        cfg["service_account"]
        or os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        or os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")
    ).strip()
    cfg["access_token"] = (cfg["access_token"] or os.environ.get("GOOGLE_ACCESS_TOKEN", "")).strip()

    return cfg


def save_sheets_config(
    c,
    sheet_id: Optional[str] = None,
    api_key: Optional[str] = None,
    service_account: Optional[str] = None,
    access_token: Optional[str] = None,
    name: Optional[str] = None
) -> Dict[str, Any]:
    """
    Persist Google Sheets configuration into settings table and connections table.
    """
    if sheet_id is not None:
        db.set_setting(c, "google_sheet_id", sheet_id.strip())
    if api_key is not None:
        db.set_setting(c, "google_api_key", api_key.strip())
    if service_account is not None:
        db.set_setting(c, "google_service_account", service_account.strip())
    if access_token is not None:
        db.set_setting(c, "google_access_token", access_token.strip())

    row = c.execute(
        "SELECT id, name, config FROM connections WHERE service='Google Sheets' OR name LIKE '%Google Sheets%' ORDER BY id ASC LIMIT 1"
    ).fetchone()
    conf = {}
    if row and row["config"]:
        try:
            conf = json.loads(row["config"])
        except Exception:
            pass

    if sheet_id is not None:
        conf["sheet_id"] = sheet_id.strip()
    if api_key is not None:
        conf["api_key"] = api_key.strip()
    if service_account is not None:
        conf["service_account"] = service_account.strip()
    if access_token is not None:
        conf["access_token"] = access_token.strip()

    conf_json = json.dumps(conf)
    if row:
        cname = name.strip() if name else row["name"]
        c.execute("UPDATE connections SET name=?, config=? WHERE id=?", (cname, conf_json, row["id"]))
    else:
        c.execute(
            "INSERT INTO connections(name, service, status, last_tested, config) VALUES(?,?,?,?,?)",
            (name or "Google Sheets Mirror", "Google Sheets", "Connected", time.time(), conf_json)
        )

    return get_sheets_config(c)


def sign_service_account_jwt(
    sa_info: Dict[str, Any],
    scopes: str = "https://www.googleapis.com/auth/spreadsheets",
    expire_seconds: int = 3600
) -> str:
    """Creates and cryptographically signs an RS256 JWT assertion for Google OAuth2."""
    now = int(time.time())
    header = {"alg": "RS256", "typ": "JWT"}
    payload = {
        "iss": sa_info["client_email"],
        "scope": scopes,
        "aud": sa_info.get("token_uri", "https://oauth2.googleapis.com/token"),
        "exp": now + expire_seconds,
        "iat": now,
    }

    encoded_header = _b64url_encode(json.dumps(header).encode("utf-8"))
    encoded_payload = _b64url_encode(json.dumps(payload).encode("utf-8"))
    signing_input = f"{encoded_header}.{encoded_payload}".encode("utf-8")

    if RSA is None or SHA256 is None or pkcs1_15 is None:
        raise RuntimeError("pycryptodome is required for service account authentication.")

    key = RSA.import_key(sa_info["private_key"])
    h = SHA256.new(signing_input)
    signature = pkcs1_15.new(key).sign(h)
    encoded_sig = _b64url_encode(signature)

    return f"{encoded_header}.{encoded_payload}.{encoded_sig}"


def get_service_account_access_token(
    sa_input: Any,
    client: Optional[httpx.Client] = None
) -> Tuple[Optional[str], Optional[str]]:
    """Exchanges Service Account credentials for a Google OAuth2 access token."""
    sa_info = None
    if isinstance(sa_input, dict):
        sa_info = sa_input
    elif isinstance(sa_input, str):
        s = sa_input.strip()
        if os.path.isfile(s):
            try:
                with open(s, "r", encoding="utf-8") as f:
                    sa_info = json.load(f)
            except Exception as e:
                return None, f"Could not read service account file: {e}"
        elif s.startswith("{"):
            try:
                sa_info = json.loads(s)
            except Exception as e:
                return None, f"Invalid service account JSON: {e}"

    if not sa_info or not isinstance(sa_info, dict):
        return None, "No valid service account dictionary or JSON provided."

    client_email = sa_info.get("client_email")
    private_key = sa_info.get("private_key")
    if not client_email or not private_key:
        return None, "Service account missing client_email or private_key."

    cache_key = client_email
    now = time.time()
    if cache_key in _token_cache:
        cached = _token_cache[cache_key]
        if cached.get("expires_at", 0) > now + 60:
            return cached["access_token"], None

    try:
        jwt_assertion = sign_service_account_jwt(sa_info)
    except Exception as e:
        return None, f"Failed to sign service account JWT: {e}"

    token_uri = sa_info.get("token_uri", "https://oauth2.googleapis.com/token")

    def _post(cl):
        return cl.post(
            token_uri,
            data={
                "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                "assertion": jwt_assertion,
            },
            timeout=20.0,
        )

    try:
        if client is not None:
            resp = _post(client)
        else:
            with httpx.Client(timeout=20.0) as temp_client:
                resp = _post(temp_client)

        if resp.status_code == 200:
            data = resp.json()
            tok = data.get("access_token")
            exp_in = data.get("expires_in", 3600)
            _token_cache[cache_key] = {
                "access_token": tok,
                "expires_at": now + exp_in,
            }
            return tok, None
        else:
            return None, f"OAuth token exchange failed (HTTP {resp.status_code}): {resp.text[:300]}"
    except Exception as e:
        return None, f"OAuth token request error: {e}"


def resolve_auth(
    config: Dict[str, Any],
    client: Optional[httpx.Client] = None
) -> Tuple[Dict[str, str], Dict[str, str], Optional[str]]:
    """Resolves authentication into (headers, query_params, error)."""
    headers: Dict[str, str] = {}
    params: Dict[str, str] = {}

    # 1. Direct Bearer access token
    if config.get("access_token"):
        headers["Authorization"] = f"Bearer {config['access_token']}"
        return headers, params, None

    # 2. Service account JSON or dict
    sa = config.get("service_account")
    sa_err = None
    if sa:
        tok, err = get_service_account_access_token(sa, client=client)
        if tok:
            headers["Authorization"] = f"Bearer {tok}"
            return headers, params, None
        else:
            sa_err = err

    # 3. API key
    if config.get("api_key"):
        params["key"] = config["api_key"]
        return headers, params, None

    # If service account had an error and no fallback key/token
    if sa_err and sa:
        return headers, params, sa_err

    return headers, params, None


def test_connection(c=None, **kwargs) -> Dict[str, Any]:
    """
    Checks Google Sheet access and permissions.
    """
    cfg = get_sheets_config(c)
    cfg.update({k: v for k, v in kwargs.items() if v is not None})

    sheet_id = cfg.get("sheet_id", "").strip()
    if not sheet_id:
        return {
            "ok": False,
            "status": "Not Configured",
            "sheet_id": "",
            "error": "Google Sheet ID is not configured. Set 'google_sheet_id' in settings or connections table.",
            "message": "Google Sheet ID is not configured."
        }

    http_client = kwargs.get("http_client")
    with _get_client(http_client) as client:
        headers, params, auth_err = resolve_auth(cfg, client=client)
        if auth_err:
            return {
                "ok": False,
                "status": "Auth Error",
                "sheet_id": sheet_id,
                "error": auth_err,
                "message": f"Authentication failed: {auth_err}"
            }

        url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        query_params = dict(params)
        query_params["fields"] = "spreadsheetId,properties.title,sheets.properties.title"

        try:
            resp = client.get(url, headers=headers, params=query_params, timeout=20.0)
            if resp.status_code == 200:
                data = resp.json()
                title = data.get("properties", {}).get("title", "Untitled Spreadsheet")
                sheets = [s.get("properties", {}).get("title", "") for s in data.get("sheets", [])]

                _update_conn_status(c, "Connected", cfg.get("connection_id"))
                return {
                    "ok": True,
                    "status": "Connected",
                    "sheet_id": sheet_id,
                    "title": title,
                    "sheets": sheets,
                    "message": f"Successfully connected to Google Sheet '{title}' ({len(sheets)} sheets)."
                }
            elif resp.status_code in (401, 403):
                err_msg = f"Permission denied (HTTP {resp.status_code}): Check API key or Service Account permissions for sheet '{sheet_id}'."
                _update_conn_status(c, "Permission Denied", cfg.get("connection_id"))
                return {
                    "ok": False,
                    "status": "Permission Denied",
                    "sheet_id": sheet_id,
                    "error": err_msg,
                    "message": err_msg
                }
            elif resp.status_code == 404:
                err_msg = f"Spreadsheet with ID '{sheet_id}' was not found (HTTP 404)."
                _update_conn_status(c, "Not Found", cfg.get("connection_id"))
                return {
                    "ok": False,
                    "status": "Not Found",
                    "sheet_id": sheet_id,
                    "error": err_msg,
                    "message": err_msg
                }
            else:
                err_msg = f"Google Sheets API returned HTTP {resp.status_code}: {resp.text[:200]}"
                _update_conn_status(c, "Error", cfg.get("connection_id"))
                return {
                    "ok": False,
                    "status": "Error",
                    "sheet_id": sheet_id,
                    "error": err_msg,
                    "message": err_msg
                }
        except Exception as e:
            err_msg = f"Connection error: {e}"
            _update_conn_status(c, "Error", cfg.get("connection_id"))
            return {
                "ok": False,
                "status": "Error",
                "sheet_id": sheet_id,
                "error": err_msg,
                "message": err_msg
            }


def get_agents_roster_data(c) -> Tuple[List[str], List[List[Any]]]:
    """
    Extracts live Agents Roster data.
    Columns: Name, Phone, Email, Language, Availability, Status, Paired Partner
    """
    headers = ["Name", "Phone", "Email", "Language", "Availability", "Status", "Paired Partner"]
    rows = []
    agents = c.execute(
        "SELECT u.*, l.name as lang_name FROM users u "
        "LEFT JOIN languages l ON l.id=u.language_id "
        "WHERE u.role='participant' ORDER BY u.language_id ASC, u.name ASC"
    ).fetchall()

    for ag in agents:
        uid = ag["id"]
        partner_name = "Unpaired"
        p = c.execute("SELECT * FROM pairs WHERE status='ACTIVE' AND (a=? OR b=?)", (uid, uid)).fetchone()
        if p:
            part_id = p["b"] if p["a"] == uid else p["a"]
            p_row = c.execute("SELECT name FROM users WHERE id=?", (part_id,)).fetchone()
            if p_row:
                partner_name = p_row["name"]

        status_str = "Active" if ag["active"] else "Inactive"
        rows.append([
            ag["name"] or "-",
            ag["phone"] or "-",
            ag["email"] or "-",
            ag["lang_name"] or "-",
            ag["availability"] or "All day",
            status_str,
            partner_name,
        ])
    return headers, rows


def get_pair_assignments_data(c) -> Tuple[List[str], List[List[Any]]]:
    """
    Extracts live Pair Assignments data.
    Columns: Pair, Language, Assigned count, Completed count, Hours recorded
    """
    headers = ["Pair", "Language", "Assigned count", "Completed count", "Hours recorded"]
    rows = []
    pairs = c.execute(
        "SELECT p.*, ua.name as name_a, ub.name as name_b, l.name as lang_name "
        "FROM pairs p "
        "JOIN users ua ON ua.id=p.a "
        "JOIN users ub ON ub.id=p.b "
        "JOIN languages l ON l.id=p.language_id "
        "ORDER BY p.id ASC"
    ).fetchall()

    for p in pairs:
        pid = p["id"]
        a_cnt = c.execute(
            "SELECT COUNT(*) n FROM assignments WHERE pair_id=? AND status NOT IN ('RELEASED','ABANDONED')",
            (pid,)
        ).fetchone()["n"]

        c_cnt = c.execute(
            "SELECT COUNT(*) n FROM assignments WHERE pair_id=? AND ("
            "status IN ('SUBMITTED','COMPLETED','CONFIRMED','ACCEPTED') OR review_status IN ('APPROVED','ACCEPTED'))",
            (pid,)
        ).fetchone()["n"]

        tot_m = c.execute(
            "SELECT COALESCE(SUM(conf_min), 0.0) m FROM assignments WHERE pair_id=? "
            "AND conf_min IS NOT NULL AND status NOT IN ('ASSIGNED','RELEASED','ABANDONED')",
            (pid,)
        ).fetchone()["m"]

        tot_h = round(tot_m / 60.0, 1)
        pair_label = f"Pair #{pid} ({p['name_a']} & {p['name_b']})"

        rows.append([
            pair_label,
            p["lang_name"] or "-",
            a_cnt,
            c_cnt,
            f"{tot_h} h",
        ])
    return headers, rows


def get_scripts_master_data(c) -> Tuple[List[str], List[List[Any]]]:
    """
    Extracts live Scripts Master List data.
    Columns: Code, Language, Domain, Subdomain, Status, Word Count
    """
    headers = ["Code", "Language", "Domain", "Subdomain", "Status", "Word Count"]
    rows = []
    scripts = c.execute(
        "SELECT s.*, l.name as lang_name FROM scripts s "
        "JOIN languages l ON l.id=s.language_id "
        "ORDER BY s.id ASC"
    ).fetchall()

    for sc in scripts:
        asg = c.execute(
            "SELECT a.status as a_stat, a.review_status "
            "FROM assignments a "
            "WHERE a.script_id=? AND a.status NOT IN ('RELEASED','ABANDONED') "
            "ORDER BY a.id DESC LIMIT 1",
            (sc["id"],)
        ).fetchone()

        if asg:
            rev_stat = (asg["review_status"] or "").upper()
            raw_stat = (asg["a_stat"] or "").upper()
            if rev_stat in ("APPROVED", "ACCEPTED") or raw_stat in ("APPROVED", "ACCEPTED", "CONFIRMED"):
                stat_disp = "ACCEPTED"
            elif rev_stat == "REJECTED" or raw_stat == "REJECTED":
                stat_disp = "REJECTED"
            elif raw_stat in ("SUBMITTED", "COMPLETED"):
                stat_disp = "SUBMITTED"
            elif raw_stat in ("ASSIGNED", "IN_SESSION"):
                stat_disp = "ASSIGNED"
            else:
                stat_disp = raw_stat or "ASSIGNED"
        else:
            raw_sc_stat = (sc["status"] or "").upper()
            stat_disp = "AVAILABLE" if raw_sc_stat in ("READY", "AVAILABLE", "APPROVED") else raw_sc_stat

        rows.append([
            sc["code"],
            sc["lang_name"] or "-",
            sc["domain"] or "-",
            sc["subdomain"] or "-",
            stat_disp,
            sc["words"] or 0,
        ])
    return headers, rows


def push_sheet_data(
    cfg: Dict[str, Any],
    sheet_title: str,
    headers: List[str],
    rows: List[List[Any]],
    client: Optional[httpx.Client] = None
) -> Dict[str, Any]:
    """Uploads table data (headers + rows) to Google Sheets under tab `sheet_title`."""
    sheet_id = cfg.get("sheet_id", "").strip()
    if not sheet_id:
        return {"ok": False, "error": "Google Sheet ID not configured"}

    with _get_client(client) as cl:
        auth_headers, auth_params, auth_err = resolve_auth(cfg, client=cl)
        if auth_err:
            return {"ok": False, "error": auth_err}

        # 1. Fetch spreadsheet metadata to check if tab exists
        meta_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
        meta_params = dict(auth_params)
        meta_params["fields"] = "sheets.properties"

        meta_resp = cl.get(meta_url, headers=auth_headers, params=meta_params, timeout=25.0)
        if meta_resp.status_code != 200:
            return {
                "ok": False,
                "error": f"Failed to fetch spreadsheet metadata (HTTP {meta_resp.status_code}): {meta_resp.text[:200]}"
            }

        meta = meta_resp.json()
        sheets_info = meta.get("sheets", [])
        existing_titles = [s.get("properties", {}).get("title") for s in sheets_info]
        sheet_id_num = None
        for s in sheets_info:
            if s.get("properties", {}).get("title") == sheet_title:
                sheet_id_num = s.get("properties", {}).get("sheetId")
                break

        # 2. Add sheet if not present
        if sheet_title not in existing_titles:
            batch_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
            add_body = {
                "requests": [
                    {
                        "addSheet": {
                            "properties": {
                                "title": sheet_title
                            }
                        }
                    }
                ]
            }
            add_resp = cl.post(batch_url, headers=auth_headers, params=auth_params, json=add_body, timeout=25.0)
            if add_resp.status_code == 200:
                try:
                    rep_data = add_resp.json()
                    sheet_id_num = rep_data.get("replies", [{}])[0].get("addSheet", {}).get("properties", {}).get("sheetId")
                except Exception:
                    pass
            elif add_resp.status_code not in (200, 201):
                return {
                    "ok": False,
                    "error": f"Failed to create sheet '{sheet_title}' (HTTP {add_resp.status_code}): {add_resp.text[:200]}"
                }

        # 3. Clear existing values
        clear_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/'{sheet_title}'!A1:Z:clear"
        cl.post(clear_url, headers=auth_headers, params=auth_params, timeout=25.0)

        # 4. Write headers + rows
        values = [headers] + rows
        update_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}/values/'{sheet_title}'!A1"
        update_params = dict(auth_params)
        update_params["valueInputOption"] = "USER_ENTERED"
        update_body = {
            "range": f"'{sheet_title}'!A1",
            "majorDimension": "ROWS",
            "values": values
        }
        up_resp = cl.put(update_url, headers=auth_headers, params=update_params, json=update_body, timeout=30.0)
        if up_resp.status_code not in (200, 201):
            return {
                "ok": False,
                "error": f"Failed to update values in '{sheet_title}' (HTTP {up_resp.status_code}): {up_resp.text[:200]}"
            }

        # 5. Optional header styling & freeze row 1
        if sheet_id_num is not None:
            try:
                style_body = {
                    "requests": [
                        {
                            "repeatCell": {
                                "range": {
                                    "sheetId": sheet_id_num,
                                    "startRowIndex": 0,
                                    "endRowIndex": 1
                                },
                                "cell": {
                                    "userEnteredFormat": {
                                        "backgroundColor": {
                                            "red": 0.0,
                                            "green": 0.6588,
                                            "blue": 0.5882
                                        },
                                        "textFormat": {
                                            "bold": True,
                                            "foregroundColor": {
                                                "red": 1.0,
                                                "green": 1.0,
                                                "blue": 1.0
                                            }
                                        },
                                        "horizontalAlignment": "CENTER"
                                    }
                                },
                                "fields": "userEnteredFormat(backgroundColor,textFormat,horizontalAlignment)"
                            }
                        },
                        {
                            "updateSheetProperties": {
                                "properties": {
                                    "sheetId": sheet_id_num,
                                    "gridProperties": {
                                        "frozenRowCount": 1
                                    }
                                },
                                "fields": "gridProperties.frozenRowCount"
                            }
                        }
                    ]
                }
                batch_style_url = f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate"
                cl.post(batch_style_url, headers=auth_headers, params=auth_params, json=style_body, timeout=15.0)
            except Exception:
                pass

        return {
            "ok": True,
            "sheet": sheet_title,
            "rows": len(rows),
            "columns": len(headers),
            "status": "synced",
            "headers": headers,
            "message": f"Successfully synced {len(rows)} rows to '{sheet_title}'"
        }


def sync_agents(c=None, **kwargs) -> Dict[str, Any]:
    """Synchronizes live Agents Roster to Google Sheets."""
    def _run(conn):
        cfg = get_sheets_config(conn)
        cfg.update({k: v for k, v in kwargs.items() if v is not None})
        headers, rows = get_agents_roster_data(conn)
        return push_sheet_data(cfg, "Agents Roster", headers, rows, client=kwargs.get("http_client"))

    if c is not None:
        return _run(c)
    with db.ro() as ro_c:
        return _run(ro_c)


def sync_assignments(c=None, **kwargs) -> Dict[str, Any]:
    """Synchronizes live Pair Assignments to Google Sheets."""
    def _run(conn):
        cfg = get_sheets_config(conn)
        cfg.update({k: v for k, v in kwargs.items() if v is not None})
        headers, rows = get_pair_assignments_data(conn)
        return push_sheet_data(cfg, "Pair Assignments", headers, rows, client=kwargs.get("http_client"))

    if c is not None:
        return _run(c)
    with db.ro() as ro_c:
        return _run(ro_c)


def sync_scripts(c=None, **kwargs) -> Dict[str, Any]:
    """Synchronizes live Scripts Master List to Google Sheets."""
    def _run(conn):
        cfg = get_sheets_config(conn)
        cfg.update({k: v for k, v in kwargs.items() if v is not None})
        headers, rows = get_scripts_master_data(conn)
        return push_sheet_data(cfg, "Scripts Master List", headers, rows, client=kwargs.get("http_client"))

    if c is not None:
        return _run(c)
    with db.ro() as ro_c:
        return _run(ro_c)


def get_submissions_data(c) -> Tuple[List[str], List[List[Any]]]:
    """Submissions with the proof photo shown INSIDE the cell via =IMAGE(url).
    Google Sheets cannot receive image bytes through the values API and cannot send cookies when it fetches an image,
    so each photo gets a long-lived signed Supabase URL (anyone holding that one link can view that one photo)."""
    from .. import storage
    headers = ["Script Code", "Language", "Speaker A", "Speaker B", "Duration (mm:ss)", "Status", "Proof Photo", "Submitted At"]
    rows = []
    q = ("SELECT a.id, a.conf_min, a.proof_img, a.submitted_at, a.review_status, a.status AS a_status, "
         "s.code AS script_code, l.name AS lang_name, ua.name AS name_a, ub.name AS name_b "
         "FROM assignments a JOIN scripts s ON s.id=a.script_id JOIN languages l ON l.id=s.language_id "
         "LEFT JOIN users ua ON ua.id=a.ua LEFT JOIN users ub ON ub.id=a.ub "
         "WHERE a.conf_min IS NOT NULL AND a.status NOT IN ('ASSIGNED','RELEASED','ABANDONED','IN_SESSION') ORDER BY a.id DESC")
    for r in c.execute(q).fetchall():
        mm, ss = divmod(int(round(float(r["conf_min"] or 0) * 60)), 60)
        rev = (r["review_status"] or "").upper()
        st = "ACCEPTED" if rev in ("APPROVED", "ACCEPTED") or r["a_status"] in ("ACCEPTED", "APPROVED") else (
            "REJECTED" if rev == "REJECTED" or r["a_status"] == "REJECTED" else "SUBMITTED")
        img = ""
        if r["proof_img"]:
            u = storage.signed_url(r["proof_img"])
            img = f'=IMAGE("{u}")' if u else "(photo not in cloud storage)"
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime((r["submitted_at"] or 0) + 19800)) if r["submitted_at"] else "-"
        rows.append([r["script_code"], r["lang_name"], r["name_a"] or "", r["name_b"] or "", f"{mm:02d}:{ss:02d}", st, img, when])
    return headers, rows


def sync_submissions(c=None, **kwargs) -> Dict[str, Any]:
    """Synchronizes submissions + proof photos to the 'Submissions' tab."""
    def _run(conn):
        cfg = get_sheets_config(conn)
        cfg.update({k: v for k, v in kwargs.items() if v is not None})
        headers, rows = get_submissions_data(conn)
        res = push_sheet_data(cfg, "Submissions", headers, rows, client=kwargs.get("http_client"))
        if res.get("ok") and rows:
            _size_photo_rows(cfg, len(rows), client=kwargs.get("http_client"))
        return res

    if c is not None:
        return _run(c)
    with db.ro() as ro_c:
        return _run(ro_c)


def _size_photo_rows(cfg, n_rows, client=None):
    """Make rows tall and the photo column wide enough to actually see the picture (best effort)."""
    try:
        sheet_id = cfg.get("sheet_id", "").strip()
        with _get_client(client) as cl:
            h, p, err = resolve_auth(cfg, client=cl)
            if err:
                return
            meta = cl.get(f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}", headers=h,
                          params={**p, "fields": "sheets.properties"}, timeout=20).json()
            gid = next((x["properties"]["sheetId"] for x in meta.get("sheets", []) if x["properties"]["title"] == "Submissions"), None)
            if gid is None:
                return
            body = {"requests": [
                {"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "ROWS", "startIndex": 1, "endIndex": n_rows + 1},
                                               "properties": {"pixelSize": 120}, "fields": "pixelSize"}},
                {"updateDimensionProperties": {"range": {"sheetId": gid, "dimension": "COLUMNS", "startIndex": 6, "endIndex": 7},
                                               "properties": {"pixelSize": 170}, "fields": "pixelSize"}},
            ]}
            cl.post(f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}:batchUpdate", headers=h, params=p, json=body, timeout=20)
    except Exception:
        pass


def sync_all(c=None, **kwargs) -> Dict[str, Any]:
    """
    Exports live data formatted cleanly with headers to sheets:
    - Agents Roster
    - Pair Assignments
    - Scripts Master List
    Updates connections status and audit trail.
    """
    def _run(conn):
        cfg = get_sheets_config(conn)
        cfg.update({k: v for k, v in kwargs.items() if v is not None})

        sheet_id = cfg.get("sheet_id", "").strip()
        if not sheet_id:
            return {
                "ok": False,
                "status": "Not Configured",
                "error": "Google Sheet ID is not configured. Set 'google_sheet_id' in settings or connections table.",
                "message": "Google Sheet ID not configured."
            }

        client = kwargs.get("http_client")
        res_agents = sync_agents(conn, **kwargs)
        if not res_agents.get("ok"):
            return {
                "ok": False,
                "status": "Error",
                "error": f"Failed syncing Agents Roster: {res_agents.get('error')}",
                "agents": res_agents
            }

        res_assign = sync_assignments(conn, **kwargs)
        if not res_assign.get("ok"):
            return {
                "ok": False,
                "status": "Error",
                "error": f"Failed syncing Pair Assignments: {res_assign.get('error')}",
                "agents": res_agents,
                "assignments": res_assign
            }

        res_scripts = sync_scripts(conn, **kwargs)
        if not res_scripts.get("ok"):
            return {
                "ok": False,
                "status": "Error",
                "error": f"Failed syncing Scripts Master List: {res_scripts.get('error')}",
                "agents": res_agents,
                "assignments": res_assign,
                "scripts": res_scripts
            }

        res_subs = sync_submissions(conn, **kwargs)
        if not res_subs.get("ok"):
            return {
                "ok": False,
                "status": "Error",
                "error": f"Failed syncing Submissions: {res_subs.get('error')}",
                "agents": res_agents, "assignments": res_assign, "scripts": res_scripts, "submissions": res_subs,
            }

        now = time.time()
        _update_conn_status(conn, "Connected", cfg.get("connection_id"), last_tested=now)
        try:
            db.audit(
                conn,
                "system",
                "sync_google_sheets",
                f"agents={res_agents.get('rows', 0)} assignments={res_assign.get('rows', 0)} scripts={res_scripts.get('rows', 0)}"
            )
        except Exception:
            pass

        tot_rows = res_agents.get("rows", 0) + res_assign.get("rows", 0) + res_scripts.get("rows", 0)
        return {
            "ok": True,
            "status": "Connected",
            "sheet_id": sheet_id,
            "agents": res_agents,
            "assignments": res_assign,
            "scripts": res_scripts,
            "submissions": res_subs,
            "total_rows_synced": tot_rows,
            "synced_at": now,
            "message": f"Successfully synchronized {res_agents.get('rows', 0)} agents, {res_assign.get('rows', 0)} pair assignments, and {res_scripts.get('rows', 0)} scripts to Google Sheets."
        }

    if c is not None:
        return _run(c)
    with db.tx() as tx_c:
        return _run(tx_c)
