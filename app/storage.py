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
    from . import db
    cache = db.read_cache()   # one lookup per read block (exports used to re-read it, on a new connection, per row)
    if cache is not None and "storage_cfg" in cache:
        return cache["storage_cfg"]
    out = _cfg_uncached()
    if cache is not None:
        cache["storage_cfg"] = out
    return out


def _cfg_uncached():
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


def compress_image(data: bytes, max_bytes: int = 100 * 1024, max_dim: int = 1280, fmt: str = "JPEG") -> bytes:
    """Downscale + compress a photo to about `max_bytes` (default ~100 KB) as JPEG or WEBP.
    WEBP keeps screenshots as sharp as a JPEG about twice its size (tested on our proof photos: ~45 KB WEBP
    looks the same as the old ~85 KB JPEG, 40 dB PSNR). Falls back to the original bytes if Pillow fails."""
    try:
        import io as _io
        from PIL import Image, ImageOps
        im = ImageOps.exif_transpose(Image.open(_io.BytesIO(data))).convert("RGB")
        if max(im.size) > max_dim:
            im.thumbnail((max_dim, max_dim))

        def enc(q):
            buf = _io.BytesIO()
            if fmt == "WEBP":
                im.save(buf, "WEBP", quality=q, method=6)
            else:
                im.save(buf, "JPEG", quality=q, optimize=True)
            return buf.getvalue()

        best = None
        ladder = (80, 74, 68, 62, 56) if fmt == "WEBP" else (85, 75, 65, 55, 45, 38, 32)
        for q in ladder:
            best = enc(q)
            if len(best) <= max_bytes:
                return best
        # Still too big: keep shrinking dimensions (text stays readable down to ~900 px wide screenshots).
        floor = 900 if fmt == "WEBP" else 420
        while max(im.size) > floor:
            im.thumbnail((int(im.size[0] * 0.88), int(im.size[1] * 0.88)))
            best = enc(60 if fmt == "WEBP" else 34)
            if len(best) <= max_bytes:
                return best
        return best if best is not None else data
    except Exception as e:
        print(f"[Storage] compress_image fallback (kept original): {e}")
        return data


def image_ext(data: bytes) -> str:
    """File extension matching the bytes (so the photo is served with the right type)."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[1:4] == b"PNG":
        return ".png"
    return ".jpg"


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


def _cache_local(path: str, data: bytes) -> None:
    try:
        with open(path, "wb") as f:
            f.write(data)
    except OSError:
        pass


def get_proof(name: str, drive_link: str = "") -> Optional[bytes]:
    """Bytes of a proof photo, or None. Local cache first, then Supabase, then Google Drive (re-caches on hit).

    Render wipes the local disk on every deploy, so a photo whose Supabase upload failed used to be
    unviewable even though its Drive copy existed."""
    name = _safe(name)
    if not name:
        return None
    path = os.path.join(uploads_dir(), name)
    if os.path.exists(path):
        with open(path, "rb") as f:
            return f.read()
    url, key, bucket = _cfg()
    if url and key:
        try:
            r = httpx.get(f"{url}/storage/v1/object/{bucket}/{name}", headers=_headers(key), timeout=30)
            if r.status_code == 200 and r.content:
                _cache_local(path, r.content)
                return r.content
        except Exception as e:
            print(f"[Storage] get_proof error: {e}")
    try:
        from .integrations import drive
        data = drive.download_proof(name, drive_link=drive_link)
        if data:
            _cache_local(path, data)
            return data
    except Exception as e:
        print(f"[Storage] get_proof Drive fallback error: {e}")
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
