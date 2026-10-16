"""Durable storage for submission proof photos.

Render's local disk is wiped on every redeploy/restart (free and non-disk plans), so a photo that only lives
in data/uploads disappears while its database row survives -> "View Photo" shows a broken image.

Photos are therefore written to a PRIVATE Supabase Storage bucket (default name: "proofs") and cached on the
local disk. Reads try the local cache first, then Supabase (and re-cache).

Configuration comes from the same place as the DB backup (env vars on Render):
    SUPABASE_URL            https://<project>.supabase.co
    SUPABASE_KEY            service_role key (server side only, never in the browser or in git)
    SUPABASE_PROOF_BUCKET   optional, default "proofs"
"""
import mimetypes
import os
import threading
import time
from typing import Optional, Tuple

import httpx

from . import config

DEFAULT_PROOF_BUCKET = "proofs"
_bucket_ready = set()
_lock = threading.Lock()


def uploads_dir() -> str:
    d = os.path.join(os.path.dirname(config.db_path()), "uploads")
    os.makedirs(d, exist_ok=True)
    return d


def _cfg():
    """(url, key, bucket) or ('', '', bucket) when Supabase is not configured."""
    try:
        from . import sync
        cfg = sync.get_config()
        url, key = (cfg.get("url") or "").rstrip("/"), cfg.get("key") or ""
    except Exception:
        url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
        key = os.environ.get("SUPABASE_KEY", "").strip() or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    bucket = os.environ.get("SUPABASE_PROOF_BUCKET", DEFAULT_PROOF_BUCKET).strip() or DEFAULT_PROOF_BUCKET
    return url, key, bucket


def is_configured() -> bool:
    url, key, _ = _cfg()
    return bool(url and key)


def _headers(key: str, ctype: Optional[str] = None) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}"}
    if ctype:
        h["Content-Type"] = ctype
    return h


def _ensure_bucket(url: str, key: str, bucket: str) -> None:
    with _lock:
        if (url, bucket) in _bucket_ready:
            return
        try:
            r = httpx.get(f"{url}/storage/v1/bucket/{bucket}", headers=_headers(key), timeout=15)
            if r.status_code != 200:
                httpx.post(f"{url}/storage/v1/bucket", headers=_headers(key, "application/json"),
                           json={"id": bucket, "name": bucket, "public": False}, timeout=15)
            _bucket_ready.add((url, bucket))
        except Exception as e:
            print(f"[Storage] ensure_bucket error: {e}")


def _safe(name: str) -> str:
    return os.path.basename(name or "")


def compress_image(data: bytes, max_bytes: int = 100 * 1024, max_dim: int = 1280) -> bytes:
    """Downscale + JPEG-compress a photo to about `max_bytes` (default ~100 KB).
    Returns JPEG bytes. Falls back to the original bytes if Pillow is unavailable or fails."""
    try:
        import io as _io
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(_io.BytesIO(data))).convert("RGB")
        if max(im.size) > max_dim:
            im.thumbnail((max_dim, max_dim))
        best = None
        for q in (85, 75, 65, 55, 45, 38, 32):
            buf = _io.BytesIO()
            im.save(buf, "JPEG", quality=q, optimize=True)
            best = buf.getvalue()
            if len(best) <= max_bytes:
                return best
        # Still too big: keep shrinking dimensions at low quality.
        while max(im.size) > 420:
            im.thumbnail((int(im.size[0] * 0.82), int(im.size[1] * 0.82)))
            buf = _io.BytesIO()
            im.save(buf, "JPEG", quality=34, optimize=True)
            best = buf.getvalue()
            if len(best) <= max_bytes:
                return best
        return best if best is not None else data
    except Exception as e:
        print(f"[Storage] compress_image fallback (kept original): {e}")
        return data


def save_proof(name: str, data: bytes) -> Tuple[bool, str]:
    """Write to local cache AND Supabase. Returns (durable, message).
    durable is False when only the (ephemeral) local copy exists."""
    name = _safe(name)
    with open(os.path.join(uploads_dir(), name), "wb") as f:
        f.write(data)
    url, key, bucket = _cfg()
    if not (url and key):
        return False, "Supabase not configured - photo kept on local disk only (lost on redeploy)."
    try:
        _ensure_bucket(url, key, bucket)
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        h = _headers(key, ctype)
        h["x-upsert"] = "true"
        r = httpx.post(f"{url}/storage/v1/object/{bucket}/{name}", headers=h, content=data, timeout=60)
        if r.status_code in (200, 201):
            return True, "ok"
        return False, f"Supabase upload failed (HTTP {r.status_code}): {r.text[:200]}"
    except Exception as e:
        return False, f"Supabase upload error: {e}"


def get_proof(name: str) -> Optional[bytes]:
    """Bytes of a proof photo, or None. Local cache first, then Supabase (re-caches on hit)."""
    name = _safe(name)
    if not name:
        return None
    path = os.path.join(uploads_dir(), name)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    url, key, bucket = _cfg()
    if not (url and key):
        return None
    try:
        r = httpx.get(f"{url}/storage/v1/object/{bucket}/{name}", headers=_headers(key), timeout=30)
        if r.status_code == 200 and r.content:
            try:
                with open(path, "wb") as f:
                    f.write(r.content)
            except OSError:
                pass
            return r.content
    except Exception as e:
        print(f"[Storage] get_proof error: {e}")
    return None


def signed_url(name: str, expires_s: int = 60 * 60 * 24 * 365) -> str:
    """Time-limited URL for the photo (used for =IMAGE() in Google Sheets, which cannot send cookies).
    Anyone holding the link can view that one photo until it expires."""
    name = _safe(name)
    url, key, bucket = _cfg()
    if not (url and key and name):
        return ""
    try:
        r = httpx.post(f"{url}/storage/v1/object/sign/{bucket}/{name}", headers=_headers(key, "application/json"),
                       json={"expiresIn": int(expires_s)}, timeout=20)
        if r.status_code == 200:
            rel = r.json().get("signedURL") or r.json().get("signedUrl") or ""
            if rel:
                return rel if rel.startswith("http") else f"{url}/storage/v1{rel}"
    except Exception as e:
        print(f"[Storage] signed_url error: {e}")
    return ""


def backfill_local_to_cloud() -> int:
    """Upload any photo that exists locally but may not be in Supabase yet. Safe to re-run (upsert)."""
    if not is_configured():
        return 0
    n = 0
    for fn in os.listdir(uploads_dir()):
        if fn.startswith("proof_"):
            with open(os.path.join(uploads_dir(), fn), "rb") as f:
                ok, _ = save_proof(fn, f.read())
            n += 1 if ok else 0
    return n
