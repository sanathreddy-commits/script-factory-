"""Script Importer module for ScriptFlow.
Imports and adds scripts to SQLite database from local files with customized script code generation.

Requirements:
- 4-part taxonomy: <language>_<domain>_<subdomain>_<specialisation>_<scenario/title>.txt
  -> Script code: e.g. THDG001 (T=Tamil, H=Healthcare, D=Doctor-Patient, G=General-Physician, 001=sequential index)
  -> Script code: e.g. KHDD001 (K=Kannada, H=Healthcare, D=Doctor-Patient, D=Dermatology, 001=sequential index)
- 3-part taxonomy (when no 2nd sub-taxonomy / no specialisation):
  <language>_<domain>_<subdomain>_<scenario/title>.txt
  -> Script code: e.g. THP0001 (T=Tamil, H=Healthcare, P=Patient, 0001=sequential index)

Language prefixes:
  - Kannada: 'K'
  - Tamil: 'T'
  - Marathi: 'M'
  - Gujarati / Gujarathi: 'G'
  - Punjabi: 'P'
  - Odia / Oriya: 'O'
  - Hindi: 'H'
"""

import argparse
import array
import hashlib
import json
import os
import re
import string
import sys
import threading
import time
import zlib
from pathlib import Path

# Language prefix mapping (Requirement 2)
LANGUAGE_PREFIXES = {
    "kannada": "K",
    "kannad": "K",
    "kn": "K",
    "tamil": "T",
    "tam": "T",
    "ta": "T",
    "marathi": "M",
    "mar": "M",
    "mr": "M",
    "gujarati": "G",
    "gujarathi": "G",
    "guj": "G",
    "gu": "G",
    "punjabi": "P",
    "pun": "P",
    "pa": "P",
    "odia": "O",
    "oriya": "O",
    "odi": "O",
    "or": "O",
    "hindi": "H",
    "hin": "H",
    "hi": "H",
}

DEFAULT_BASE_DIRS = [
    Path("scripts_library"),
    Path("Script Factory/scripts_library"),
    Path("c:/Users/sanat/Desktop/Awign Doc/Audio project/Script Factory/scripts_library"),
    Path("c:/Users/sanat/Desktop/Awign Doc/Audio project/scripts_library"),
]


def get_language_prefix(lang_name: str) -> str:
    """Returns standard single-letter language prefix."""
    if not lang_name:
        return "X"
    key = lang_name.strip().lower()
    if key in LANGUAGE_PREFIXES:
        return LANGUAGE_PREFIXES[key]
    for k, v in LANGUAGE_PREFIXES.items():
        if k in key:
            return v
    # Fallback to first alphabetic character uppercase
    for ch in lang_name:
        if ch.isalpha():
            return ch.upper()
    return "X"


def get_taxonomy_letter(token: str) -> str:
    """Extracts first alphabetic character in uppercase."""
    if not token:
        return ""
    for ch in token:
        if ch.isalpha():
            return ch.upper()
    return ""


def parse_filename_metadata(filename: str, content: str = "") -> dict:
    """Parses filename (and optional header content) into taxonomy components and code prefix.
    
    Handles:
    - 4-part taxonomy: <language>_<domain>_<subdomain>_<specialisation>_<scenario/title>.txt
      e.g. tamil_Healthcare_Doctor-Patient_General-Physician_ace-inhibitor-dry-cough.txt
      -> Prefix: THDG (T=Tamil, H=Healthcare, D=Doctor-Patient, G=General-Physician), is_4part=True
    - 3-part taxonomy: <language>_<domain>_<subdomain>_<scenario/title>.txt
      e.g. tamil_Healthcare_Patient_fever.txt
      -> Prefix: THP (T=Tamil, H=Healthcare, P=Patient), is_4part=False
    """
    stem = Path(filename).stem.strip()
    parts = [p.strip() for p in stem.split("_") if p.strip()]

    lang_raw = ""
    domain_raw = ""
    subdomain_raw = ""
    spec_raw = ""
    scenario_raw = ""
    is_4part = False

    # Check for metadata headers in content to enrich / confirm
    content_meta = {}
    if content:
        for line in content.splitlines()[:25]:
            l_strip = line.strip()
            if l_strip.startswith("#") or ":" in l_strip:
                clean_l = l_strip.lstrip("#").strip()
                if ":" in clean_l:
                    k, v = clean_l.split(":", 1)
                    content_meta[k.strip().lower()] = v.strip()

    if len(parts) >= 5:
        # 4-part taxonomy: lang_domain_subdomain_specialisation_scenario
        lang_raw = parts[0]
        domain_raw = parts[1]
        subdomain_raw = parts[2]
        spec_raw = parts[3]
        scenario_raw = "_".join(parts[4:])
        is_4part = True
    elif len(parts) == 4:
        # 3-part taxonomy: lang_domain_subdomain_scenario
        lang_raw = parts[0]
        domain_raw = parts[1]
        subdomain_raw = parts[2]
        spec_raw = ""
        scenario_raw = parts[3]
        is_4part = False
    elif len(parts) == 3:
        lang_raw = parts[0]
        domain_raw = parts[1]
        subdomain_raw = parts[2]
        spec_raw = ""
        scenario_raw = parts[2]
        is_4part = False
    elif len(parts) == 2:
        lang_raw = parts[0]
        domain_raw = "Healthcare"
        subdomain_raw = parts[1]
        spec_raw = ""
        scenario_raw = parts[1]
        is_4part = False
    elif len(parts) == 1:
        lang_raw = parts[0]
        domain_raw = "Healthcare"
        subdomain_raw = "General"
        spec_raw = ""
        scenario_raw = parts[0]
        is_4part = False

    # Check if content has explicit Taxonomy or Specialty
    if "taxonomy" in content_meta:
        tax_tokens = [t.strip() for t in content_meta["taxonomy"].split("/") if t.strip()]
        if len(tax_tokens) >= 3:
            domain_raw = domain_raw or tax_tokens[0]
            subdomain_raw = subdomain_raw or tax_tokens[1]
            spec_raw = spec_raw or tax_tokens[2]
            is_4part = True
        elif len(tax_tokens) == 2:
            domain_raw = domain_raw or tax_tokens[0]
            subdomain_raw = subdomain_raw or tax_tokens[1]
    if "specialty" in content_meta and not spec_raw:
        spec_raw = content_meta["specialty"]
        is_4part = True
    if "language" in content_meta and not lang_raw:
        lang_raw = content_meta["language"]

    lang_letter = get_language_prefix(lang_raw)
    dom_letter = get_taxonomy_letter(domain_raw) or "H"
    sub_letter = get_taxonomy_letter(subdomain_raw) or "D"
    spec_letter = get_taxonomy_letter(spec_raw) if is_4part else ""

    if is_4part and spec_letter:
        prefix = f"{lang_letter}{dom_letter}{sub_letter}{spec_letter}"
    else:
        is_4part = False
        prefix = f"{lang_letter}{dom_letter}{sub_letter}"

    return {
        "filename": filename,
        "language": lang_raw.capitalize() if lang_raw else "Kannada",
        "domain": domain_raw or "Healthcare",
        "subdomain": subdomain_raw or "General",
        "specialisation": spec_raw if is_4part else "",
        "scenario": scenario_raw or stem,
        "is_4part": is_4part,
        "prefix": prefix,
        "code_width": 3 if is_4part else 4,
    }


def generate_script_code(c, prefix: str, is_4part: bool = True) -> str:
    """Generates the next sequential script code for a prefix.
    
    Sequential numbering:
    - 4-part taxonomy: 3 digits, e.g. THDG001, THDG002
    - 3-part taxonomy: 4 digits, e.g. THP0001, THP0002
    Ensures unique code constraint is strictly preserved.
    """
    width = 3 if is_4part else 4
    # Query all existing codes with this prefix
    rows = c.execute("SELECT code FROM scripts WHERE code LIKE ?", (f"{prefix}%",)).fetchall()
    max_seq = 0
    for r in rows:
        code_val = (r["code"] or "").strip()
        if code_val.startswith(prefix):
            suffix = code_val[len(prefix):]
            if suffix.isdigit():
                max_seq = max(max_seq, int(suffix))

    next_seq = max_seq + 1
    code = f"{prefix}{next_seq:0{width}d}"

    # Guard against any collision
    while c.execute("SELECT 1 FROM scripts WHERE code = ?", (code,)).fetchone():
        next_seq += 1
        code = f"{prefix}{next_seq:0{width}d}"

    return code


def parse_dialogue_turns(content: str) -> tuple[list[dict], int]:
    """Parses dialogue content into turns [{\"speaker\": \"A\", \"text\": \"...\"}, ...]
    and calculates total word count.
    
    Standard ScriptFlow turn structure contains:
    - speaker: 'A', 'B', etc.
    - role: original speaker label e.g. 'DOCTOR', 'PATIENT'
    - text: spoken dialogue
    """
    lines = content.splitlines()
    in_script = False
    has_script_heading = any(re.match(r"^(?:#+\s*script|script\s*:)", l.strip(), re.I) for l in lines)

    meta_keys = {
        "language", "taxonomy", "scenario", "setting", "participants", "script id",
        "domain", "sub-domain", "specialty", "specialisation", "est. run time",
        "word count", "case summary"
    }

    speaker_pattern = re.compile(r"^([A-Za-z0-9_\.\-\s]{1,35}?)\s*:\s*(.*)$")

    turns = []
    current_speaker = None
    current_role = None
    current_text = []
    speaker_map = {}

    for line in lines:
        raw = line.strip()
        if not raw:
            continue
        if re.match(r"^(?:#+\s*script|script\s*:)", raw, re.I):
            in_script = True
            continue
        if raw.startswith("---") or raw.startswith("====="):
            continue
        if raw.startswith("#"):
            continue

        m = speaker_pattern.match(raw)
        if m:
            pot_role = m.group(1).strip()
            rest = m.group(2).strip()

            if not in_script and has_script_heading:
                continue
            if pot_role.lower() in meta_keys:
                continue
            if not in_script and ("(" in rest and ")" in rest and any(w in rest.lower() for w in ["female", "male", "years", "consultation"])):
                continue

            # Complete previous turn
            if current_speaker and current_text:
                turns.append({
                    "speaker": current_speaker,
                    "role": current_role,
                    "text": " ".join(current_text)
                })
                current_text = []

            role_norm = pot_role.upper()
            if role_norm not in speaker_map:
                assigned_letter = chr(ord("A") + len(speaker_map))
                speaker_map[role_norm] = assigned_letter

            current_speaker = speaker_map[role_norm]
            current_role = pot_role
            if rest:
                current_text.append(rest)
        else:
            if current_speaker:
                current_text.append(raw)

    if current_speaker and current_text:
        turns.append({
            "speaker": current_speaker,
            "role": current_role,
            "text": " ".join(current_text)
        })

    # If no speaker turns were detected, treat the content as a single dialogue turn
    if not turns:
        cleaned_body = "\n".join(l for l in lines if not l.startswith("#") and not l.startswith("---") and not l.startswith("=====")).strip()
        if cleaned_body:
            turns = [{"speaker": "A", "role": "Speaker", "text": cleaned_body}]

    word_count = sum(len(t["text"].split()) for t in turns)
    return turns, word_count


def find_language_id(c, lang_str: str) -> int:
    """Finds language_id in the database for a language name or code."""
    langs = c.execute("SELECT id, name, code FROM languages").fetchall()
    if not langs:
        return 1

    clean = (lang_str or "").strip().lower()

    # Exact name match
    for l in langs:
        if l["name"].lower() == clean:
            return l["id"]

    # Exact code match
    for l in langs:
        if l["code"].lower() == clean:
            return l["id"]

    # Substring / alias match
    for l in langs:
        lname = l["name"].lower()
        if clean in lname or lname in clean:
            return l["id"]
        # Alias checks
        if "gujarat" in clean and "gujarat" in lname:
            return l["id"]
        if ("odia" in clean or "oriya" in clean) and ("odia" in lname or "oriya" in lname):
            return l["id"]

    return langs[0]["id"]


_PUNCT_TABLE = str.maketrans("", "", string.punctuation + "।॥,.-:;!?\"'()[]{}")


def unpack_shingles(b):
    """Decompresses binary shingle BLOB into integer set."""
    if not b:
        return set()
    try:
        a = array.array("I")
        a.frombytes(zlib.decompress(b))
        return set(a)
    except Exception:
        return set()


# ---- Fingerprint cache for the near-duplicate check (Check 5) ----------------------------------------------
# The check compares a new file with every fingerprint in its language. Reading them all from the database for
# every uploaded file downloaded ~6 KB x (scripts in language) per file: a 1,500-file upload pulled several GB
# from Supabase. Now the packed fingerprints are kept in memory per language. Before each check one tiny query
# reads a signature (count, newest id, total bytes); if it matches, the cached copy is used; if only new scripts
# were added, only those are fetched; anything else re-reads the language. Code that rewrites fingerprints calls
# shingles_changed(), which forces a full re-read. Results are the same as reading everything each time.
_SHINGLE_LOCK = threading.Lock()
_SHINGLE_CACHE = {}      # language_id -> {"sig": (n, max_id, total_len, epoch), "rows": [(id, code, packed_bytes)]}
_SHINGLE_EPOCH = [0]


def shingles_changed():
    """Call after UPDATE-ing scripts.shingles anywhere, so the next duplicate check re-reads fingerprints."""
    with _SHINGLE_LOCK:
        _SHINGLE_EPOCH[0] += 1
        _SHINGLE_CACHE.clear()


def _language_shingles(c, language_id):
    """[(id, code, packed_shingles)] for the language, ordered by id, as the database has them right now."""
    sig_row = c.execute(
        "SELECT COUNT(*) n, COALESCE(MAX(id), 0) m, COALESCE(SUM(LENGTH(shingles)), 0) t "
        "FROM scripts WHERE language_id = ? AND shingles IS NOT NULL", (language_id,)).fetchone()
    with _SHINGLE_LOCK:
        epoch = _SHINGLE_EPOCH[0]
        ent = _SHINGLE_CACHE.get(language_id)
    sig = (int(sig_row["n"]), int(sig_row["m"]), int(sig_row["t"]), epoch)
    if ent and ent["sig"] == sig:
        return ent["rows"]
    rows = None
    if ent and ent["sig"][3] == epoch and sig[1] > ent["sig"][1]:
        new = [(r["id"], r["code"], bytes(r["shingles"])) for r in c.execute(
            "SELECT id, code, shingles FROM scripts WHERE language_id = ? AND shingles IS NOT NULL AND id > ? ORDER BY id",
            (language_id, ent["sig"][1])).fetchall()]
        if ent["sig"][0] + len(new) == sig[0] and ent["sig"][2] + sum(len(b) for _, _, b in new) == sig[2]:
            rows = ent["rows"] + new
    if rows is None:
        rows = [(r["id"], r["code"], bytes(r["shingles"])) for r in c.execute(
            "SELECT id, code, shingles FROM scripts WHERE language_id = ? AND shingles IS NOT NULL ORDER BY id",
            (language_id,)).fetchall()]
    with _SHINGLE_LOCK:
        if _SHINGLE_EPOCH[0] == epoch:
            _SHINGLE_CACHE[language_id] = {"sig": sig, "rows": rows}
    return rows


def compute_content_fingerprint(content: str) -> dict:
    """Computes robust dialogue content fingerprints for deduplication.
    Extracts spoken dialogue, normalized text, opening string, scenario header, 5-gram shingles, and SHA-256 hash.
    """
    turns, word_count = parse_dialogue_turns(content)
    if turns:
        spoken_text = " ".join(t.get("text", "") for t in turns)
        opening = turns[0].get("text", "")[:250].strip()
    else:
        lines = [l for l in content.splitlines() if not l.strip().startswith("#") and not l.strip().startswith("---") and not l.strip().startswith("=====")]
        spoken_text = " ".join(lines)
        opening = lines[0][:250].strip() if lines else ""

    clean_spoken = " ".join(spoken_text.translate(_PUNCT_TABLE).lower().split())
    content_hash = hashlib.sha256(clean_spoken.encode("utf-8")).hexdigest() if clean_spoken else ""

    scenario_desc = ""
    for line in content.splitlines()[:25]:
        clean_l = line.strip().lstrip("#").strip()
        if clean_l.lower().startswith("scenario:"):
            scenario_desc = clean_l.split(":", 1)[1].strip().lower()
            break

    words = clean_spoken.split()
    shingles = set()
    n = 5
    for i in range(max(0, len(words) - n + 1)):
        shingles.add(zlib.crc32(" ".join(words[i:i + n]).encode()))

    packed_shingles = None
    if shingles:
        try:
            packed_shingles = zlib.compress(array.array("I", sorted(shingles)).tobytes())
        except Exception:
            pass

    return {
        "turns": turns,
        "word_count": word_count,
        "opening": opening,
        "clean_spoken": clean_spoken,
        "content_hash": content_hash,
        "scenario_desc": scenario_desc,
        "shingles": shingles,
        "packed_shingles": packed_shingles,
    }


def find_duplicate_script(c, language_id: int, filename: str, content: str, meta: dict = None) -> dict | None:
    """Finds if a script is a duplicate of an existing script in this language.
    Guarantees no duplicate can be imported even if the file name, speaker roles, or headers differ:
    1. Filename / path (brief)
    2. Normalized dialogue content hash (exact dialogue match)
    3. Scenario description in header
    4. Opening dialogue text (first turn match)
    5. Shingle / n-gram similarity overlap (near-copy match >= 70%)
    """
    if meta is None:
        meta = parse_filename_metadata(filename or "script.txt", content)

    p_name = Path(filename).name if filename else ""
    scenario_str = meta.get("scenario", "")

    # Check 1: Filename or exact scenario title match in same taxonomy
    if p_name:
        dup = c.execute(
            """SELECT id, code, brief FROM scripts 
               WHERE language_id = ? 
               AND (
                   brief = ? OR brief LIKE ? OR
                   (domain = ? AND subdomain = ? AND specialisation = ? AND (brief LIKE ? OR attrs LIKE ?))
               ) LIMIT 1""",
            (language_id, p_name, f"%{p_name}%", meta["domain"], meta["subdomain"], meta["specialisation"],
             f"%{scenario_str}%", f"%{scenario_str}%")
        ).fetchone()
        if dup:
            return {"id": dup["id"], "code": dup["code"], "reason": f"File or scenario already imported ({dup['code']})"}

    # Compute content fingerprints
    fp = compute_content_fingerprint(content)
    chash = fp["content_hash"]
    op = fp["opening"]
    sc_desc = fp["scenario_desc"]

    # Check 2: Content hash match (detects same script with different filename or renamed speakers)
    if chash:
        dup = c.execute(
            "SELECT id, code, brief FROM scripts WHERE language_id = ? AND attrs LIKE ? LIMIT 1",
            (language_id, f"%{chash}%")
        ).fetchone()
        if dup:
            return {"id": dup["id"], "code": dup["code"], "reason": f"Identical dialogue content already exists ({dup['code']})"}

    # Check 3: Header scenario description match (>= 25 chars)
    if sc_desc and len(sc_desc) >= 25:
        dup = c.execute(
            "SELECT id, code, brief FROM scripts WHERE language_id = ? AND attrs LIKE ? LIMIT 1",
            (language_id, f"%{sc_desc}%")
        ).fetchone()
        if dup:
            return {"id": dup["id"], "code": dup["code"], "reason": f"Script with identical scenario description already exists ({dup['code']})"}

    # NOTE: An earlier "identical opening dialogue" check was removed. Many legitimate, distinct
    # doctor-patient scripts share a near-identical greeting as their first line, so that check
    # produced false positives and silently dropped genuinely different scripts on import.
    # True duplicates are still caught by the exact content hash (Check 2) and the shingle
    # near-copy overlap (Check 5) below, which compare the whole dialogue rather than one line.

    # Check 5: Shingle similarity overlap (near-copy match >= 70%)
    if fp["shingles"]:
        mine = fp["shingles"]
        for sid, scode, packed in _language_shingles(c, language_id):
            if packed:
                other = unpack_shingles(packed)
                if other and min(len(mine), len(other)) > 0:
                    overlap = len(mine & other) / min(len(mine), len(other))
                    if overlap >= 0.70:
                        return {"id": sid, "code": scode, "reason": f"Near-duplicate ({int(overlap*100)}% text overlap with {scode})"}

    return None


def import_script_file(c, file_path: str | Path, target_lang_id: int | None = None, status: str = "READY") -> dict:
    """Imports a single script file into scripts and script_versions tables.
    
    Idempotent: skips if script with identical filename, content, or taxonomy already exists.
    Status defaults to 'READY' so scripts are immediately available to assign.
    """
    p = Path(file_path)
    if not p.is_file():
        return {"status": "error", "error": f"File not found: {file_path}", "file": str(file_path)}

    try:
        content = p.read_text(encoding="utf-8-sig", errors="replace").strip()
    except Exception as ex:
        return {"status": "error", "error": f"Failed reading file: {ex}", "file": p.name}

    if len(content) < 20:
        return {"status": "skipped", "reason": "Content too short", "file": p.name}

    meta = parse_filename_metadata(p.name, content)
    lid = find_language_id(c, meta["language"])

    # If target_lang_id was explicitly requested and doesn't match, skip
    if target_lang_id is not None and target_lang_id != "all" and int(target_lang_id) != lid:
        return {"status": "skipped", "reason": f"Target language mismatch (file: {lid}, target: {target_lang_id})", "file": p.name}

    # 5-layer content and metadata deduplication check
    dup = find_duplicate_script(c, lid, p.name, content, meta)
    if dup:
        return {
            "status": "skipped",
            "reason": dup.get("reason", "Already imported"),
            "code": dup.get("code"),
            "script_id": dup.get("id"),
            "file": p.name
        }

    # Calculate fingerprints and dialogue turns
    fp = compute_content_fingerprint(content)
    turns = fp["turns"]
    word_count = fp["word_count"]
    opening = fp["opening"]
    packed_shingles = fp["packed_shingles"]
    turn_json = json.dumps(turns, ensure_ascii=False)

    # Generate sequential script code
    code = generate_script_code(c, meta["prefix"], is_4part=meta["is_4part"])

    # Sequential index per language for UNIQUE(language_id, seq)
    seq = c.execute("SELECT IFNULL(MAX(seq), 0) + 1 FROM scripts WHERE language_id=?", (lid,)).fetchone()[0]

    now = time.time()
    scenario_str = meta["scenario"]
    brief = f"{scenario_str} ({p.name})"

    attrs_dict = {
        "filename": p.name,
        "rel_path": str(p),
        "domain": meta["domain"],
        "subdomain": meta["subdomain"],
        "specialisation": meta["specialisation"],
        "scenario": scenario_str,
        "language": meta["language"],
        "is_4part": meta["is_4part"],
        "turns_count": len(turns),
        "genders": {"A": "", "B": ""},
        "raw_text": content,
        "content_hash": fp["content_hash"],
        "scenario_desc": fp["scenario_desc"]
    }
    attrs_json = json.dumps(attrs_dict, ensure_ascii=False)

    c.execute("""
        INSERT INTO scripts(
            language_id, seq, code, domain, subdomain, specialisation,
            wave, attrs, brief, status, version, words, is_test, source,
            opening, shingles, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, 1, ?, 0, 'local_import', ?, ?, ?, ?)
    """, (
        lid, seq, code, meta["domain"], meta["subdomain"], meta["specialisation"],
        attrs_json, brief, status, word_count, opening, packed_shingles, now, now
    ))
    sid = c.execute("SELECT last_insert_rowid()").fetchone()[0]

    c.execute("""
        INSERT OR REPLACE INTO script_versions(script_id, version, turns, words, created_at)
        VALUES (?, 1, ?, ?, ?)
    """, (sid, turn_json, word_count, now))

    return {
        "status": "imported",
        "code": code,
        "script_id": sid,
        "language_id": lid,
        "words": word_count,
        "turns": len(turns),
        "file": p.name
    }


def resolve_directory_path(dir_path: str | Path | None = None) -> Path | None:
    """Resolves directory path, checking relative to CWD and parent workspace folders."""
    candidates = []
    if dir_path:
        candidates.append(Path(dir_path))
        candidates.append(Path(__file__).resolve().parent.parent / dir_path)
    candidates.extend(DEFAULT_BASE_DIRS)

    for cand in candidates:
        if cand.exists() and cand.is_dir():
            return cand.resolve()
    return None


def import_directory(c, directory_path: str | Path, target_lang_id: int | None = None,
                     recursive: bool = True, status: str = "READY") -> dict:
    """Scans local machine directory for .txt and .md scripts and imports them into SQLite DB.
    
    Returns summary dict with imported, skipped, errors, and detail list.
    """
    resolved_dir = resolve_directory_path(directory_path)
    if not resolved_dir:
        return {"imported": 0, "skipped": 0, "errors": 1, "details": [], "error": f"Directory not found: {directory_path}"}

    # Ensure base languages exist in DB before importing scripts
    try:
        if c.execute("SELECT COUNT(*) n FROM languages").fetchone()["n"] == 0:
            from . import ops
            ops.ensure_core_system(c)
    except Exception:
        pass

    results = []
    imported_count = 0
    skipped_count = 0
    error_count = 0

    files_gen = resolved_dir.rglob("*") if recursive else resolved_dir.glob("*")
    files_to_process = []
    for f in files_gen:
        if not f.is_file():
            continue
        if f.suffix.lower() not in (".txt", ".md"):
            continue
        if f.name.startswith(".") or "ADMIN_CODE" in f.name or "README" in f.name:
            continue
        files_to_process.append(f)

    # Sort files deterministically
    files_to_process.sort(key=lambda x: str(x).lower())

    for f in files_to_process:
        res = import_script_file(c, f, target_lang_id=target_lang_id, status=status)
        results.append(res)
        st = res.get("status")
        if st == "imported":
            imported_count += 1
        elif st == "skipped":
            skipped_count += 1
        else:
            error_count += 1

    return {
        "imported": imported_count,
        "skipped": skipped_count,
        "errors": error_count,
        "total_files": len(files_to_process),
        "directory": str(resolved_dir),
        "details": results
    }


def sync_all_library_folders(c, language_id: int | None = None, status: str = "READY") -> int:
    """Top-level sync function called by Admin UI / sync routes.
    
    Scans default scripts_library directory and imports all unimported scripts.
    Returns total number of newly imported scripts.
    """
    total_imported = 0
    resolved = resolve_directory_path("scripts_library")
    if resolved:
        summary = import_directory(c, resolved, target_lang_id=language_id, status=status)
        total_imported += summary.get("imported", 0)
    return total_imported


def main():
    """CLI entrypoint for script importer."""
    parser = argparse.ArgumentParser(description="Import scripts from local files into ScriptFlow SQLite database.")
    parser.add_argument("--dir", default="scripts_library", help="Directory path to scan")
    parser.add_argument("--lang", default=None, help="Target language ID or name (optional)")
    parser.add_argument("--status", default="READY", help="Script status (default: READY)")
    parser.add_argument("--db", default=None, help="Database path override (optional)")
    args = parser.parse_args()

    if args.db:
        os.environ["SF_DB"] = args.db

    # Ensure app imports
    from . import db, ops
    db.init_db()

    target_lid = int(args.lang) if args.lang and str(args.lang).isdigit() else args.lang
    with db.tx() as c:
        ops.ensure_core_system(c)
        summary = import_directory(c, args.dir, target_lang_id=target_lid, status=args.status)

    print(f"Import Summary for '{summary.get('directory', args.dir)}':")
    print(f"  Imported : {summary['imported']}")
    print(f"  Skipped  : {summary['skipped']}")
    print(f"  Errors   : {summary['errors']}")
    print(f"  Total    : {summary.get('total_files', 0)}")


if __name__ == "__main__":
    main()
