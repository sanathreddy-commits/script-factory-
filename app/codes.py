"""Access codes and script IDs, both with a check character so typos are caught instantly."""
import secrets

ALPHA = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"  # no I, L, O, 0, 1


def _chk(s):
    return ALPHA[sum((ALPHA.index(ch) + 1) * (i + 1) for i, ch in enumerate(s)) % len(ALPHA)]


def new_code():
    body = "".join(secrets.choice(ALPHA) for _ in range(7))
    return body + _chk(body)


def norm(code):
    return "".join(ch for ch in (code or "").upper() if ch.isalnum())


def valid(code):
    c = norm(code)
    return len(c) == 8 and all(ch in ALPHA for ch in c) and _chk(c[:7]) == c[7]


def fmt(code):
    return code[:4] + "-" + code[4:] if code and len(code) == 8 else code


def script_code(lang_code, seq):
    body = f"{seq:05d}"
    w = [3, 7]
    chk = sum(int(d) * w[i % 2] for i, d in enumerate(body)) % 10
    return f"{lang_code}-{body}-{chk}"


def script_code_valid(code):
    try:
        lang, body, chk = (code or "").strip().upper().split("-")
        return script_code(lang, int(body)) == f"{lang}-{body}-{chk}"
    except Exception:
        return False
