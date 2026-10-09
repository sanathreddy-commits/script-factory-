"""Hard validators + similarity gate. LLM output is stored only if it passes these."""
import array
import re
import unicodedata
import zlib

_PUNCT = str.maketrans({ch: " " for ch in ".,;:!?\"'()[]{}-–—…।॥|/\\*"})
PLACEHOLDER = re.compile(r"\[[^\]]*\]|\{[^}]*\}|<[^>]*>|_{3,}|\bTODO\b|\bX{3,}\b|lorem ipsum", re.I)
STAGE_DIR = re.compile(r"\*[^*]+\*|^\s*\([^)]*\)\s*$|^\s*\[[^\]]*\]\s*$")
PHONE = re.compile(r"(?<!\d)(?:\+?91[-\s]?)?[6-9]\d{9}(?!\d)")
EMAIL = re.compile(r"\S+@\S+\.\S+")
AADHAAR = re.compile(r"(?<!\d)\d{4}\s?\d{4}\s?\d{4}(?!\d)")
PAN = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")
NUM = re.compile(r"\d+")
MIN_RATIO = {"low": 0.85, "medium": 0.70, "high": 0.55}


def words(text):
    return text.split()


def count_words(turns):
    """Single counting rule, used by validator AND reviewer view: speech only, labels excluded."""
    return sum(len(words(t["t"])) for t in turns)


def nums(text):
    return [int(x) for x in NUM.findall(re.sub(r"(?<=\d),(?=\d)", "", text or ""))]


def allowed_numbers(attrs):
    ok = set(range(0, 32))
    for v in attrs.get("entities", {}).values():
        ok.update(nums(str(v)))
    return ok


def normalise_turns(raw):
    out = []
    for t in raw:
        s = str(t.get("s", "")).strip().upper()[:1]
        out.append({"s": s, "t": " ".join(str(t.get("t", "")).split())})
    return out


def validate(turns, attrs, lang, S):
    """Return list of error strings; empty means pass. lang: row with lo/hi."""
    errs = []
    if not turns:
        return ["empty"]
    wc = count_words(turns)
    wmin, wmax = int(S["words_min"]), int(S["words_max"])
    if wc < wmin:
        errs.append(f"words_short:{wc}<{wmin}")
    elif wc > wmax:
        errs.append(f"words_long:{wc}>{wmax}")
    speakers = {t["s"] for t in turns}
    if speakers != {"A", "B"}:
        errs.append(f"speakers:{sorted(speakers)}")
    maxw = int(S["max_turn_words"])
    for i, t in enumerate(turns):
        n = len(words(t["t"]))
        if n == 0:
            errs.append(f"empty_turn:{i}")
            break
        if n > maxw:
            errs.append(f"turn_too_long:{i}:{n}")
            break
    text = "\n".join(t["t"] for t in turns)
    if "�" in text or any(ord(ch) < 32 and ch not in "\n\t" for ch in text):
        errs.append("bad_unicode")
    elif any(unicodedata.category(ch) == "Cn" for ch in text):
        errs.append("unassigned_codepoint")
    letters = [ch for ch in text if ch.isalpha()]
    if letters:
        inr = sum(1 for ch in letters if lang["lo"] <= ord(ch) <= lang["hi"]) / len(letters)
        need = MIN_RATIO.get(attrs.get("code_switch", "low"), 0.85)
        if inr < need:
            errs.append(f"script_ratio:{inr:.2f}<{need}")
    if PLACEHOLDER.search(text):
        errs.append("placeholder")
    if any(STAGE_DIR.search(t["t"]) for t in turns):
        errs.append("stage_direction")
    if PHONE.search(text) or EMAIL.search(text) or AADHAAR.search(text) or PAN.search(text):
        errs.append("personal_data_pattern")
    allowed = allowed_numbers(attrs)
    bad = sorted({n for n in nums(text) if n not in allowed})
    if bad:
        errs.append("numbers_not_allowed:" + ",".join(map(str, bad[:6])))
    low = text.lower()
    for w in [x.strip().lower() for x in (S.get("banned_topics") or "").split(",") if x.strip()]:
        if w in low:
            errs.append(f"banned:{w}")
    return errs


# ---------------------------------------------------------------- similarity
def _tokens(text):
    return text.translate(_PUNCT).lower().split()


def shingle_set(turns, n=5):
    toks = _tokens(" ".join(t["t"] for t in turns))
    return {zlib.crc32(" ".join(toks[i:i + n]).encode()) for i in range(max(0, len(toks) - n + 1))}


def pack(sh):
    return zlib.compress(array.array("I", sorted(sh)).tobytes())


def unpack(b):
    if not b:
        return set()
    a = array.array("I")
    a.frombytes(zlib.decompress(b))
    return set(a)


def opening_key(turns):
    return " ".join(_tokens(turns[0]["t"])[:8]) if turns else ""


def similarity_check(c, lang_id, script_id, turns, threshold):
    """Compare against whole corpus of the language. Returns list of error strings."""
    mine = shingle_set(turns)
    op = opening_key(turns)
    errs = []
    rows = c.execute(
        "SELECT id,code,shingles,opening FROM scripts WHERE language_id=? AND id<>? AND shingles IS NOT NULL"
        " AND status NOT IN ('REJECTED','QUARANTINED')", (lang_id, script_id)).fetchall()
    for r in rows:
        if op and r["opening"] == op:
            errs.append(f"opening_duplicate:{r['code']}")
            break
    if mine:
        for r in rows:
            other = unpack(r["shingles"])
            if not other:
                continue
            ov = len(mine & other) / min(len(mine), len(other))
            if ov > threshold:
                errs.append(f"near_copy:{r['code']}:{ov:.2f}")
                break
    return errs


def parse_lines(text):
    """'Label: line' -> list of (label, text); used for import and reviewer edits."""
    out = []
    for ln in text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        m = re.match(r"^([^:：]{1,40})[:：]\s*(.+)$", ln)
        if m:
            out.append((m.group(1).strip(), m.group(2).strip()))
        elif out:
            out[-1] = (out[-1][0], out[-1][1] + " " + ln)
        else:
            out.append(("?", ln))
    return out


def map_labels(pairs, roles):
    """Map speaker-label variants to canonical A/B. roles=(roleA, roleB). Returns (turns, error|None)."""
    labels = []
    for lab, _ in pairs:
        if lab not in labels:
            labels.append(lab)
    if len(labels) != 2:
        return None, f"expected exactly 2 speaker labels, found {len(labels)}: {labels[:5]}"
    ra, rb = roles[0].lower(), roles[1].lower()

    def hit(lab, role):
        l = lab.lower()
        return l == role or l in (role.split()[0],) or l.startswith(role[:4])

    if hit(labels[0], rb) and not hit(labels[0], ra):
        m = {labels[0]: "B", labels[1]: "A"}
    elif hit(labels[1], ra) and not hit(labels[1], rb):
        m = {labels[0]: "B", labels[1]: "A"}
    else:
        m = {labels[0]: "A", labels[1]: "B"}
    return [{"s": m[lab], "t": t} for lab, t in pairs], None
