"""Import script files from a Google Drive link (folder, file or Google Doc).

Two access modes, picked automatically:

* API mode  - when Drive credentials/API key are configured (Developer > Connections). Works for private folders
  that were shared with the service account, and is the most reliable.
* Link mode - no credentials at all. Works for folders/files shared as "Anyone with the link can view".

The user only ever supplies a URL. We extract the Drive ID from it with a strict regex and build every request URL
ourselves, so this can never be pointed at an arbitrary host (no SSRF).
"""

from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from .drive import GoogleDriveClient

MAX_FILES = 300
MAX_DEPTH = 4
MAX_BYTES = 1024 * 1024          # per script; far above any real script
TIME_BUDGET_S = 80               # stay under typical 100 s proxy timeouts; re-running skips what already imported

_ID = r"[A-Za-z0-9_-]{10,}"
GDOC = "application/vnd.google-apps.document"
GFOLDER = "application/vnd.google-apps.folder"
_TEXT_NAME = re.compile(r"\.(txt|md)$", re.I)


class DriveImportError(Exception):
    """User-facing problem (bad link, not shared, nothing found)."""


@dataclass
class Item:
    path: str                      # folder/sub/name.txt, for display
    name: str                      # file name used for language/taxonomy detection
    data: Optional[bytes] = None
    error: str = ""


@dataclass
class Result:
    mode: str                      # "api" | "link"
    items: List[Item] = field(default_factory=list)
    truncated: str = ""            # non-empty = why we stopped early


def parse_drive_url(url: str) -> Dict[str, str]:
    """Return {"kind": "folder"|"file"|"gdoc", "id": "..."} or raise DriveImportError."""
    raw = (url or "").strip()
    if not raw:
        raise DriveImportError("Paste a Google Drive folder or file link.")
    if not re.match(r"^https?://", raw, re.I):
        raise DriveImportError("That doesn't look like a link. Paste the full https://drive.google.com/... URL.")
    u = urlparse(raw)
    host = (u.hostname or "").lower()
    if host not in ("drive.google.com", "docs.google.com", "drive.usercontent.google.com"):
        raise DriveImportError("Only Google Drive / Google Docs links are supported (drive.google.com or docs.google.com).")
    path = unquote(u.path)
    q = parse_qs(u.query)

    m = re.search(r"/folders/(%s)" % _ID, path)
    if m:
        return {"kind": "folder", "id": m.group(1)}
    m = re.search(r"/document/(?:u/\d+/)?d/(%s)" % _ID, path)
    if m and host == "docs.google.com":
        return {"kind": "gdoc", "id": m.group(1)}
    m = re.search(r"/file/(?:u/\d+/)?d/(%s)" % _ID, path)
    if m:
        return {"kind": "file", "id": m.group(1)}
    for key in ("id",):
        v = (q.get(key) or [""])[0]
        if re.fullmatch(_ID, v):
            # open?id= / uc?id= / folderview?id= : kind is resolved later (could be file or folder)
            return {"kind": "folder" if "folderview" in path else "unknown", "id": v}
    if "/drive/" in path and "my-drive" in path:
        raise DriveImportError("That's the 'My Drive' root. Open the specific folder and copy its link.")
    raise DriveImportError("Couldn't find a folder or file ID in that link.")


def _filename_from_headers(resp: httpx.Response) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1)).strip().strip('"')
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I) or re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    return (m.group(1).strip() if m else "").strip('"')


def _safe_name(name: str) -> str:
    n = re.sub(r"[\\/:*?\"<>|\x00-\x1f]+", "_", (name or "").strip())
    return n[:180]


class Importer:
    def __init__(self, drive_config: Optional[Dict[str, Any]] = None, http: Optional[httpx.Client] = None,
                 now=time.monotonic):
        self.http = http or httpx.Client(timeout=30.0, follow_redirects=True,
                                         headers={"User-Agent": "Mozilla/5.0 (ScriptFlow importer)"})
        self._now = now
        self.cfg = drive_config or {}
        cli = GoogleDriveClient(config=self.cfg) if self.cfg else None
        self.headers: Dict[str, str] = {}
        self.api_key = ""
        if cli is not None:
            try:
                self.headers = {k: v for k, v in cli.get_auth_headers().items() if k == "Authorization"}
            except Exception:
                self.headers = {}
            self.api_key = (self.cfg.get("api_key") or "").strip()
        self.use_api = bool(self.headers or self.api_key)

    # ------------------------------------------------------------------ API mode
    def _api_get(self, path: str, params: Dict[str, str]) -> httpx.Response:
        p = dict(params)
        p.setdefault("supportsAllDrives", "true")
        if self.api_key and "Authorization" not in self.headers:
            p["key"] = self.api_key
        return self.http.get("https://www.googleapis.com/drive/v3/" + path, params=p, headers=self.headers)

    def _api_list(self, folder_id: str) -> List[Dict[str, Any]]:
        out, token = [], ""
        while True:
            params = {"q": f"'{folder_id}' in parents and trashed=false",
                      "fields": "nextPageToken,files(id,name,mimeType,size)", "pageSize": "1000",
                      "includeItemsFromAllDrives": "true"}
            if token:
                params["pageToken"] = token
            r = self._api_get("files", params)
            if r.status_code != 200:
                raise DriveImportError(f"Drive API error {r.status_code} while listing the folder.")
            d = r.json()
            out += [{"id": f["id"], "name": f.get("name", ""), "folder": f.get("mimeType") == GFOLDER,
                     "gdoc": f.get("mimeType") == GDOC, "size": int(f.get("size") or 0)} for f in d.get("files", [])]
            token = d.get("nextPageToken") or ""
            if not token:
                return out

    def _api_meta(self, file_id: str) -> Dict[str, Any]:
        r = self._api_get(f"files/{file_id}", {"fields": "id,name,mimeType,size"})
        if r.status_code != 200:
            raise DriveImportError(f"Drive API error {r.status_code}. Is the link correct and shared with the app?")
        d = r.json()
        return {"id": d["id"], "name": d.get("name", ""), "folder": d.get("mimeType") == GFOLDER,
                "gdoc": d.get("mimeType") == GDOC, "size": int(d.get("size") or 0)}

    def _api_download(self, f: Dict[str, Any]) -> bytes:
        if f["gdoc"]:
            r = self._api_get(f"files/{f['id']}/export", {"mimeType": "text/plain"})
        else:
            r = self._api_get(f"files/{f['id']}", {"alt": "media"})
        if r.status_code != 200:
            raise DriveImportError(f"download failed (HTTP {r.status_code})")
        return r.content

    # ------------------------------------------------------------------ link mode
    def _link_list(self, folder_id: str) -> List[Dict[str, Any]]:
        r = self.http.get("https://drive.google.com/embeddedfolderview", params={"id": folder_id})
        if r.status_code in (401, 403, 404):
            raise DriveImportError("Google says this folder isn't reachable. Set sharing to 'Anyone with the link can view'.")
        if r.status_code != 200:
            raise DriveImportError(f"Google Drive returned HTTP {r.status_code} for this folder.")
        body = r.text
        out, seen = [], set()
        parts = re.split(r'(?=<div class="flip-entry"[^>]*id="entry-)', body)
        for chunk in parts:
            m = re.search(r'id="entry-(%s)"' % _ID, chunk)
            if not m or m.group(1) in seen:
                continue
            fid = m.group(1)
            seen.add(fid)
            t = re.search(r'class="flip-entry-title">(.*?)</div>', chunk, re.S)
            name = html.unescape(re.sub(r"<[^>]+>", "", t.group(1))).strip() if t else ""
            is_folder = ("/folders/" in chunk) or ("/drive/folders" in chunk)
            is_doc = "/document/d/" in chunk
            out.append({"id": fid, "name": name, "folder": is_folder, "gdoc": is_doc, "size": 0})
        if not out and ("Sign in" in body or "accounts.google.com" in body):
            raise DriveImportError("This folder is private. Set sharing to 'Anyone with the link can view', or configure Drive credentials.")
        return out

    def _link_download(self, f: Dict[str, Any]) -> bytes:
        if f["gdoc"]:
            r = self.http.get(f"https://docs.google.com/document/d/{f['id']}/export", params={"format": "txt"})
        else:
            r = self.http.get("https://drive.usercontent.google.com/download",
                              params={"id": f["id"], "export": "download", "confirm": "t"})
        if r.status_code != 200:
            raise DriveImportError(f"download failed (HTTP {r.status_code}); is the file shared with 'Anyone with the link'?")
        if r.headers.get("content-type", "").lower().startswith("text/html"):
            raise DriveImportError("Google returned a web page instead of the file; it isn't shared with 'Anyone with the link'.")
        if not f.get("name"):
            f["name"] = _filename_from_headers(r)
        return r.content

    def _link_meta(self, file_id: str, kind: str) -> Dict[str, Any]:
        return {"id": file_id, "name": "", "folder": False, "gdoc": kind == "gdoc", "size": 0}

    # ------------------------------------------------------------------ shared
    def _list(self, folder_id: str) -> List[Dict[str, Any]]:
        if self.use_api:
            try:
                return self._api_list(folder_id)
            except DriveImportError:
                raise
            except httpx.HTTPError:
                pass
        return self._link_list(folder_id)

    def _download(self, f: Dict[str, Any]) -> bytes:
        if self.use_api:
            try:
                return self._api_download(f)
            except (DriveImportError, httpx.HTTPError):
                pass  # fall back to link mode for files that are link-shared but not visible to the service account
        return self._link_download(f)

    def collect(self, url: str) -> Result:
        info = parse_drive_url(url)
        res = Result(mode="api" if self.use_api else "link")
        t0 = self._now()
        todo: List[tuple] = []   # (file dict, display path)

        def over_budget() -> bool:
            if len(todo) >= MAX_FILES:
                res.truncated = f"Stopped at {MAX_FILES} files. Run the import again for the rest (already-imported scripts are skipped)."
                return True
            if self._now() - t0 > TIME_BUDGET_S:
                res.truncated = "Stopped early to stay within the time limit. Run the import again; already-imported scripts are skipped."
                return True
            return False

        def walk(folder_id: str, prefix: str, depth: int):
            for f in self._list(folder_id):
                if over_budget():
                    return
                if f["folder"]:
                    if depth < MAX_DEPTH:
                        walk(f["id"], f"{prefix}{f['name']}/", depth + 1)
                    continue
                if f["gdoc"]:
                    f["name"] = (f["name"] or f["id"]) + ".txt"
                elif not _TEXT_NAME.search(f["name"] or ""):
                    res.items.append(Item(path=prefix + (f["name"] or f["id"]), name=f["name"], error="Not a .txt/.md script (skipped)"))
                    continue
                todo.append((f, prefix + f["name"]))

        kind = info["kind"]
        if kind == "unknown":
            if self.use_api:
                m = self._api_meta(info["id"])
                kind = "folder" if m["folder"] else ("gdoc" if m["gdoc"] else "file")
            else:
                kind = "file"  # open?id= without API: treat as a file; folder links normally contain /folders/
        if kind == "folder":
            walk(info["id"], "", 0)
        else:
            f = self._api_meta(info["id"]) if self.use_api else self._link_meta(info["id"], kind)
            todo.append((f, f["name"] or info["id"]))

        if not todo and not res.items:
            raise DriveImportError("No .txt/.md scripts found at that link.")

        for f, path in todo:
            if self._now() - t0 > TIME_BUDGET_S:
                res.truncated = "Stopped early to stay within the time limit. Run the import again; already-imported scripts are skipped."
                break
            try:
                data = self._download(f)
            except (DriveImportError, httpx.HTTPError) as ex:
                res.items.append(Item(path=path, name=f.get("name") or "", error=str(ex)[:200]))
                continue
            name = _safe_name(f.get("name") or "")
            if not name:
                res.items.append(Item(path=path, name="", error="Couldn't determine the file name"))
                continue
            if not _TEXT_NAME.search(name):
                res.items.append(Item(path=path, name=name, error="Not a .txt/.md script (skipped)"))
                continue
            if len(data) > MAX_BYTES:
                res.items.append(Item(path=path, name=name, error="File larger than 1 MB"))
                continue
            res.items.append(Item(path=path, name=name, data=data))
        return res
