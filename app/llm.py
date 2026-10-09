"""Provider-agnostic LLM calls + key pool routing. 'stub' produces clearly-labelled TEST text for the pipeline."""
import json
import random
import unicodedata
import threading
import time
from datetime import datetime

import httpx


class RateLimited(Exception):
    def __init__(self, retry_after=30):
        self.retry_after = retry_after


class AuthError(Exception):
    pass


class ProviderError(Exception):
    pass


_lock = threading.Lock()
_last = {}


def throttle(key):
    """Respect the key's requests-per-minute."""
    gap = 60.0 / max(1, key["rpm"])
    with _lock:
        wait = _last.get(key["id"], 0) + gap - time.time()
        _last[key["id"]] = max(time.time(), _last.get(key["id"], 0) + gap)
    if wait > 0:
        time.sleep(min(wait, 30))


def call(key, system, prompt, max_tokens, meta=None):
    """Returns (text, tokens_in, tokens_out)."""
    throttle(key)
    p = key["provider"]
    if p == "stub":
        return stub(key, system, prompt, meta or {})
    try:
        if p == "anthropic":
            r = httpx.post((key["base_url"] or "https://api.anthropic.com") + "/v1/messages", timeout=180,
                           headers={"x-api-key": key["api_key"], "anthropic-version": "2023-06-01"},
                           json={"model": key["model"], "max_tokens": max_tokens, "system": system,
                                 "messages": [{"role": "user", "content": prompt}]})
        elif p == "openai":
            r = httpx.post((key["base_url"] or "https://api.openai.com") + "/v1/chat/completions", timeout=180,
                           headers={"Authorization": "Bearer " + key["api_key"]},
                           json={"model": key["model"], "max_tokens": max_tokens,
                                 "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": prompt}]})
        else:
            raise ProviderError("unknown provider " + p)
    except httpx.HTTPError as e:
        raise ProviderError(f"network: {e}")
    if r.status_code == 429:
        raise RateLimited(int(r.headers.get("retry-after", "30") or 30))
    if r.status_code in (401, 403):
        raise AuthError(f"{r.status_code}")
    if r.status_code >= 500:
        raise ProviderError(f"{r.status_code}")
    if r.status_code >= 400:
        raise ProviderError(f"{r.status_code}: {r.text[:200]}")
    d = r.json()
    if p == "anthropic":
        return d["content"][0]["text"], d["usage"]["input_tokens"], d["usage"]["output_tokens"]
    return d["choices"][0]["message"]["content"], d["usage"]["prompt_tokens"], d["usage"]["completion_tokens"]


def parse_json(text):
    a, b = text.find("{"), text.rfind("}")
    if a < 0 or b < 0:
        raise ProviderError("unparseable: no JSON")
    try:
        return json.loads(text[a:b + 1])
    except Exception:
        raise ProviderError("unparseable JSON")


# ---------------------------------------------------------------- stub (TEST DATA ONLY)
def _lexicon(rng, m, n=260):
    cons = [x for x in range(m["c_lo"], m["c_hi"] + 1) if chr(x).isalpha()] or list(range(97, 123))
    signs = [x for x in range(m["s_lo"], m["s_hi"] + 1) if unicodedata.category(chr(x)).startswith("M")]
    words = []
    for _ in range(n):
        w = ""
        for _ in range(rng.choice([1, 2, 2, 3])):
            w += chr(rng.choice(cons))
            if rng.random() < 0.7 and signs:
                w += chr(rng.choice(signs))
        words.append(w)
    return words


def stub(key, system, prompt, meta):
    kind = meta.get("kind")
    rng = random.Random(meta.get("seed", 0))
    if kind == "judge":
        sc = {k: rng.choice([3, 4, 4, 5]) for k in ("fluency", "register", "topic", "consistency")}
        return json.dumps({"scores": sc, "flags": []}), 900, 60
    m = meta["lang"]
    lex = _lexicon(random.Random(meta["script_seed"]), m)
    names = [meta["names"]["A"], meta["names"]["B"]]
    ents = [str(v) for v in meta["entities"].values()]
    budget = meta["words"]
    chaos = float(meta.get("chaos", 0))
    if chaos and rng.random() < chaos:
        budget = int(budget * 0.55)
    turns, total, s = [], 0, meta.get("start_speaker", "A")
    while total < budget:
        n = min(rng.randint(10, 38), budget - total) or 1
        ws = [rng.choice(lex) for _ in range(n)]
        if rng.random() < 0.25:
            ws.insert(rng.randrange(len(ws) + 1), rng.choice(names + ents))
            ws = ws[:n] if len(" ".join(ws).split()) > n + 3 else ws
        turns.append({"s": s, "t": " ".join(ws)})
        total += len(" ".join(ws).split())
        s = "B" if s == "A" else "A"
    return json.dumps({"turns": turns}, ensure_ascii=False), 700 + budget, int(budget * 3)


# ---------------------------------------------------------------- key pool
def now_day():
    return datetime.now().strftime("%Y-%m-%d")


def roll(c, k):
    """Reset daily/monthly counters when the date changes."""
    d, mo = now_day(), now_day()[:7]
    if k["day"] != d:
        c.execute("UPDATE api_keys SET spent_day=0, day=? WHERE id=?", (d, k["id"]))
    if k["month"] != mo:
        c.execute("UPDATE api_keys SET spent_month=0, month=? WHERE id=?", (mo, k["id"]))


def usable(k, lang_code, now):
    if k["status"] != "active" or k["cool_until"] > now:
        return False
    langs = (k["languages"] or "*").replace(" ", "")
    if langs != "*" and lang_code not in langs.split(","):
        return False
    return k["spent_day"] < k["daily_cap"] and k["spent_month"] < k["monthly_cap"]


def pick_key(c, role, lang_code, exclude_model=None, exclude_id=None):
    now = time.time()
    rows = c.execute("SELECT * FROM api_keys WHERE role IN (?, 'spare')", (role,)).fetchall()
    ok = [k for k in rows if usable(k, lang_code, now) and k["id"] != exclude_id
          and not (exclude_model and k["model"] == exclude_model)]
    ok.sort(key=lambda k: (k["role"] != role, -(k["daily_cap"] - k["spent_day"])))
    return ok[0] if ok else None
