"""Generation pipeline: queue with leased jobs, key-pool workers, validators, repair, judge, review."""
import json
import os
import random
import threading
import time
import traceback

from . import config, db, llm
from . import taxonomy as T
from . import validators as V

BEAT_FRACS = [0.20, 0.30, 0.30, 0.20]
MASTER = """You write ONE natural spoken two-person conversation for a speech-data project, in the target language and script.
Rules: speaker A and speaker B only; plain spoken words (no stage directions, no brackets, no placeholders, no emojis);
use ONLY the names and figures you are given and never invent phone numbers, account numbers, dosages or dates;
write numbers the way people say them; keep each turn under 60 words; sound human, with hesitations and short replies.
Every conversation must differ in wording from typical templates. Output ONLY JSON: {"turns":[{"s":"A","t":"..."},{"s":"B","t":"..."}]}"""
LEASE = 600


class Requeue(Exception):
    pass


class ProviderFail(Exception):
    pass


class Stop:
    event = threading.Event()


def master_prompt(c):
    return db.setting(c, "master_prompt") or MASTER


def lang_meta(L):
    return {k: L[k] for k in ("lo", "hi", "c_lo", "c_hi", "s_lo", "s_hi")}


def beat_plan(attrs):
    closing = {"resolved": "the issue is fully resolved and the call ends politely",
               "escalated": "the issue is escalated with a reference and a timeline",
               "callback scheduled": "a callback is promised for a specific day"}[attrs["outcome"]]
    return [
        f"OPENING: greetings, both speakers identify themselves, begin with {attrs['opening']}.",
        "PROBLEM: B explains the situation in detail; A asks clarifying questions.",
        "HANDLING: A explains options and steps using only the allowed figures; B reacts and asks follow-ups.",
        f"CLOSING: {closing}; final confirmations and goodbye.",
    ]


def render_text(turns, roles):
    lab = {"A": roles[0], "B": roles[1]}
    return "\n".join(f"{lab.get(t['s'], t['s'])}: {t['t']}" for t in turns)


def lines_to_turns(text, roles):
    pairs = V.parse_lines(text)
    return V.map_labels(pairs, roles)


# ---------------------------------------------------------------- calls with accounting
def _fail_key(key, status=None, cool=0, n429=0):
    with db.tx() as c:
        if status:
            c.execute("UPDATE api_keys SET status=? WHERE id=?", (status, key["id"]))
            db.audit(c, "system", "key_disabled", f"{key['label']} -> {status}")
        else:
            c.execute("UPDATE api_keys SET cool_until=?, n429=n429+?, fail=fail+?, consec_fail=consec_fail+? WHERE id=?",
                      (time.time() + cool, n429, 0 if n429 else 1, 0 if n429 else 1, key["id"]))
            if not n429:
                r = c.execute("SELECT consec_fail FROM api_keys WHERE id=?", (key["id"],)).fetchone()
                if r["consec_fail"] >= 5:  # circuit breaker
                    c.execute("UPDATE api_keys SET cool_until=? WHERE id=?", (time.time() + 120, key["id"]))


def _call(key, job_id, system, prompt, max_tokens, meta):
    try:
        text, tin, tout = llm.call(key, system, prompt, max_tokens, meta)
    except llm.RateLimited as e:
        _fail_key(key, cool=e.retry_after, n429=1)
        raise Requeue("429")
    except llm.AuthError:
        _fail_key(key, status="disabled")
        raise Requeue("auth")
    except llm.ProviderError as e:
        _fail_key(key, cool=5)
        raise ProviderFail(str(e))
    cost = (tin * key["price_in"] + tout * key["price_out"]) / 1e6
    with db.tx() as c:
        k = c.execute("SELECT * FROM api_keys WHERE id=?", (key["id"],)).fetchone()
        llm.roll(c, k)
        c.execute("UPDATE api_keys SET spent_day=spent_day+?, spent_month=spent_month+?, ok=ok+1, consec_fail=0 WHERE id=?",
                  (cost, cost, key["id"]))
        c.execute("UPDATE jobs SET cost=cost+?, calls=calls+1 WHERE id=?", (cost, job_id))
    return text


def _beat(key, job, script, attrs, L, S, beat_desc, words, seed, prev, start_speaker, system):
    tail = "\n".join(f"{t['s']}: {t['t']}" for t in prev[-4:]) or "(start of conversation)"
    prompt = (
        f"TARGET LANGUAGE: {L['name']}\nBRIEF: {script['brief']}\n"
        f"SPEAKERS: A = {attrs['roles'][0]} ({attrs['names']['A']}); B = {attrs['roles'][1]} ({attrs['names']['B']}).\n"
        f"BEAT: {beat_desc}\nWORD BUDGET: about {words} spoken words in this beat (count speech only).\n"
        f"PREVIOUS TURNS:\n{tail}\nContinue the conversation; start with speaker {start_speaker}. JSON only.")
    meta = dict(kind="beat", seed=seed, script_seed=script["id"] * 100 + job["seed"], words=words, lang=lang_meta(L),
                names=attrs["names"], entities=attrs["entities"], chaos=S.get("stub_chaos", 0), start_speaker=start_speaker)
    text = _call(key, job["id"], system, prompt, int(words * float(S["tokens_per_word"]) * 1.5) + 200, meta)
    try:
        d = llm.parse_json(text)
    except llm.ProviderError as e:
        _fail_key(key, cool=2)
        raise ProviderFail(str(e))
    return V.normalise_turns(d.get("turns", []))


def _next_speaker(prev):
    return "B" if (prev and prev[-1]["s"] == "A") else "A"


def draft_all(key, job, script, attrs, L, S, seed, system):
    mid = (int(S["words_min"]) + int(S["words_max"])) // 2
    beats = []
    for i, (desc, f) in enumerate(zip(beat_plan(attrs), BEAT_FRACS)):
        flat = [t for b in beats for t in b]
        beats.append(_beat(key, job, script, attrs, L, S, desc, int(mid * f), seed + i, flat, _next_speaker(flat), system))
    return beats


def flat(beats):
    return [t for b in beats for t in b]


def repair(key, job, script, attrs, L, S, beats, errs, seed, system):
    """Targeted fix for pure length problems; None means 'regenerate everything'."""
    kinds = {e.split(":")[0] for e in errs}
    if not kinds <= {"words_short", "words_long"}:
        return None
    wc = V.count_words(flat(beats))
    mid = (int(S["words_min"]) + int(S["words_max"])) // 2
    if "words_short" in kinds:
        need = mid - wc
        pre = flat(beats[:2])
        extra = _beat(key, job, script, attrs, L, S, "ELABORATION: add more natural detail, questions and reactions to the "
                      "handling part; do not conclude the call.", need, seed + 50, pre, _next_speaker(pre), system)
        beats = beats[:2] + [extra] + beats[2:]
    else:
        excess = wc - mid
        sizes = [V.count_words(b) for b in beats]
        i = 2 if sizes[2] > excess + 80 else max(range(len(beats)), key=lambda k: sizes[k])
        pre = flat(beats[:i])
        beats[i] = _beat(key, job, script, attrs, L, S, beat_plan(attrs)[min(i, 3)], max(60, sizes[i] - excess),
                         seed + 60, pre, _next_speaker(pre), system)
    return beats


def judge(c_info, job, script, attrs, L, S, turns, writer_key):
    with db.ro() as c:
        jk = llm.pick_key(c, "judge", L["code"], exclude_model=writer_key["model"], exclude_id=writer_key["id"])
    if not jk:
        return None
    txt = render_text(turns, attrs["roles"])
    prompt = (f"Rate this {L['name']} conversation 1-5 on fluency, register, topic match, consistency with the brief.\n"
              f"BRIEF: {script['brief']}\n\nSCRIPT:\n{txt}\n\nJSON only: "
              '{"scores":{"fluency":4,"register":4,"topic":4,"consistency":4},"flags":["short issue text"]}')
    out = _call(jk, job["id"], "You are a strict native-speaker reviewer. Output JSON only.", prompt, 300,
                dict(kind="judge", seed=script["id"] * 7 + job["seed"]))
    try:
        d = llm.parse_json(out)
        return {"scores": d.get("scores", {}), "flags": d.get("flags", []), "model": jk["model"]}
    except llm.ProviderError:
        return None


def needs_review(script, jd, S):
    if script["wave"] == 1:
        return 1
    if jd is None:
        return 1
    sc = [v for v in jd["scores"].values() if isinstance(v, (int, float))]
    if jd["flags"] or not sc or min(sc) < 3 or sum(sc) / len(sc) < 3.5:
        return 1
    return 1 if random.Random(script["id"] * 31 + 7).random() < float(S["review_pct"]) / 100 else 0


# ---------------------------------------------------------------- job processing
def process(job_id, key_id):
    with db.ro() as c:
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        script = c.execute("SELECT * FROM scripts WHERE id=?", (job["script_id"],)).fetchone()
        L = c.execute("SELECT * FROM languages WHERE id=?", (script["language_id"],)).fetchone()
        key = c.execute("SELECT * FROM api_keys WHERE id=?", (key_id,)).fetchone()
        S = db.S(c)
        system = master_prompt(c)
    attrs = json.loads(script["attrs"])
    try:
        beats, errs = None, []
        for attempt in range(1, 4):
            seed = job["seed"] * 1000 + attempt * 97
            if beats is None:
                beats = draft_all(key, job, script, attrs, L, S, seed, system)
            turns = flat(beats)
            errs = V.validate(turns, attrs, L, S)
            if not errs:
                with db.ro() as c:
                    errs = V.similarity_check(c, L["id"], script["id"], turns, float(S["sim_threshold"]))
            with db.tx() as c:
                c.execute("UPDATE jobs SET attempts=?, lease_until=? WHERE id=?", (attempt, time.time() + LEASE, job_id))
            if not errs:
                break
            nb = repair(key, job, script, attrs, L, S, beats, errs, seed, system) if attempt < 3 else None
            beats = nb
        if errs:
            with db.tx() as c:
                c.execute("UPDATE jobs SET status='NEEDS_HUMAN', error=?, updated_at=? WHERE id=?",
                          ("; ".join(errs)[:900], time.time(), job_id))
                c.execute("UPDATE scripts SET status='NEEDS_HUMAN', updated_at=? WHERE id=?", (time.time(), script["id"]))
            return "NEEDS_HUMAN"
        turns = flat(beats)
        jd = judge(None, job, script, attrs, L, S, turns, key)
        return store_result(job, script, L, attrs, turns, jd, key, S)
    except Requeue:
        with db.tx() as c:
            c.execute("UPDATE jobs SET status='QUEUED', lease_until=0 WHERE id=?", (job_id,))
            c.execute("UPDATE scripts SET status='QUEUED' WHERE id=? AND status='GENERATING'", (script["id"],))
        return "REQUEUED"
    except ProviderFail as e:
        with db.tx() as c:
            r = c.execute("SELECT fails FROM jobs WHERE id=?", (job_id,)).fetchone()
            f = r["fails"] + 1
            if f >= 6:
                c.execute("UPDATE jobs SET status='NEEDS_HUMAN', fails=?, error=? WHERE id=?", (f, "provider: " + str(e), job_id))
                c.execute("UPDATE scripts SET status='NEEDS_HUMAN' WHERE id=?", (script["id"],))
            else:
                c.execute("UPDATE jobs SET status='QUEUED', fails=?, lease_until=?, error=? WHERE id=?",
                          (f, time.time() + min(60, 2 ** f), str(e)[:200], job_id))
                c.execute("UPDATE scripts SET status='QUEUED' WHERE id=?", (script["id"],))
        return "PROVIDER_FAIL"


def store_result(job, script, L, attrs, turns, jd, key, S):
    """Single transaction: version row + script + job. Similarity re-checked under the write lock."""
    with db.tx() as c:
        errs = V.similarity_check(c, L["id"], script["id"], turns, float(S["sim_threshold"]))
        if errs:
            c.execute("UPDATE jobs SET status='QUEUED', seed=seed+1, lease_until=0, error=? WHERE id=?",
                      ("race:" + errs[0], job["id"]))
            c.execute("UPDATE scripts SET status='QUEUED' WHERE id=?", (script["id"],))
            return "RETRY"
        cur = c.execute("SELECT version, status FROM scripts WHERE id=?", (script["id"],)).fetchone()
        ver = cur["version"] + 1
        wc = V.count_words(turns)
        nr = needs_review(script, jd, S)
        status = "IN_REVIEW" if nr else "APPROVED"
        pv = db.setting(c, "prompt_version")
        c.execute("INSERT INTO script_versions VALUES(?,?,?,?,?,?,?)",
                  (script["id"], ver, json.dumps(turns, ensure_ascii=False), wc, pv, "generated", time.time()))
        c.execute("UPDATE scripts SET status=?, version=?, words=?, shingles=?, opening=?, prompt_version=?, spec_version=?,"
                  " judge=?, needs_review=?, is_test=?, reject_reason=NULL, updated_at=? WHERE id=?",
                  (status, ver, wc, V.pack(V.shingle_set(turns)), V.opening_key(turns), pv, int(S["spec_version"]),
                   json.dumps(jd) if jd else None, nr, 1 if key["provider"] == "stub" else 0, time.time(), script["id"]))
        c.execute("UPDATE jobs SET status='DONE', error=NULL, updated_at=? WHERE id=?", (time.time(), job["id"]))
        if status == "APPROVED":
            backup_script(c, script["id"])
    return status


# ---------------------------------------------------------------- queue, workers, orchestrator
def claim_job(key):
    now = time.time()
    with db.tx() as c:
        S = db.S(c)
        if S["pause_generation"] == "1":
            return None
        spent = c.execute("SELECT COALESCE(SUM(cost),0) x FROM jobs").fetchone()["x"]
        if spent >= float(S["global_cap_inr"]):
            return None
        k = c.execute("SELECT * FROM api_keys WHERE id=?", (key["id"],)).fetchone()
        llm.roll(c, k)
        k = c.execute("SELECT * FROM api_keys WHERE id=?", (key["id"],)).fetchone()
        rows = c.execute(
            "SELECT j.id, l.code lc FROM jobs j JOIN scripts s ON s.id=j.script_id JOIN languages l ON l.id=s.language_id"
            " WHERE j.status IN ('QUEUED','RUNNING') AND j.lease_until<? ORDER BY j.id LIMIT 60", (now,)).fetchall()
        for r in rows:
            if llm.usable(k, r["lc"], now):
                c.execute("UPDATE jobs SET status='RUNNING', lease_until=?, key_id=?, updated_at=? WHERE id=?",
                          (now + LEASE, key["id"], now, r["id"]))
                c.execute("UPDATE scripts SET status='GENERATING' WHERE id=(SELECT script_id FROM jobs WHERE id=?)", (r["id"],))
                return {"id": r["id"]}
    return None


def run_pending(max_jobs=None):
    """Synchronous drain using any usable writer key (used by tests and the 'run now' button)."""
    n = 0
    while max_jobs is None or n < max_jobs:
        with db.ro() as c:
            keys = c.execute("SELECT * FROM api_keys WHERE status='active' AND role IN ('writer','spare')").fetchall()
        done = False
        for k in keys:
            j = claim_job(k)
            if j:
                process(j["id"], k["id"])
                n += 1
                done = True
                break
        if not done:
            break
    return n


class Worker(threading.Thread):
    def __init__(self, key_id):
        super().__init__(daemon=True)
        self.key_id = key_id

    def run(self):
        while not Stop.event.is_set():
            try:
                with db.ro() as c:
                    key = c.execute("SELECT * FROM api_keys WHERE id=?", (self.key_id,)).fetchone()
                if not key or key["status"] == "disabled" or key["role"] not in ("writer", "spare"):
                    return
                if key["status"] != "active" or key["cool_until"] > time.time():
                    Stop.event.wait(2)
                    continue
                job = claim_job(key)
                if not job:
                    Stop.event.wait(1.5)
                    continue
                process(job["id"], key["id"])
            except Exception:
                traceback.print_exc()
                Stop.event.wait(3)


class Orchestrator(threading.Thread):
    """One instance. Spawns one worker per active writer key. Makes no draft calls itself."""

    def __init__(self):
        super().__init__(daemon=True)
        self.workers = {}

    def run(self):
        while not Stop.event.is_set():
            try:
                with db.ro() as c:
                    ids = [r["id"] for r in c.execute(
                        "SELECT id FROM api_keys WHERE status IN ('active') AND role IN ('writer','spare')")]
                for i in ids:
                    w = self.workers.get(i)
                    if not w or not w.is_alive():
                        self.workers[i] = Worker(i)
                        self.workers[i].start()
            except Exception:
                traceback.print_exc()
            Stop.event.wait(3)


def release_wave(c, lang_id, wave, actor):
    if wave > 1:
        left = c.execute("SELECT COUNT(*) n FROM scripts WHERE language_id=? AND wave=? AND status IN"
                         " ('PLANNED','QUEUED','GENERATING','IN_REVIEW','NEEDS_HUMAN')", (lang_id, wave - 1)).fetchone()["n"]
        if left:
            raise ValueError(f"Wave {wave - 1} is not finished ({left} scripts still pending or in review).")
    if not c.execute("SELECT 1 FROM api_keys WHERE status='active' AND role IN ('writer','spare')").fetchone():
        raise ValueError("No active writer key. Add one in Generation > Key pool.")
    rows = c.execute("SELECT id FROM scripts WHERE language_id=? AND wave=? AND status='PLANNED'", (lang_id, wave)).fetchall()
    for r in rows:
        c.execute("INSERT OR IGNORE INTO jobs(script_id,status,seed,updated_at) VALUES(?, 'QUEUED', 1, ?)", (r["id"], time.time()))
        c.execute("UPDATE scripts SET status='QUEUED' WHERE id=?", (r["id"],))
    db.audit(c, actor, "release_wave", f"lang={lang_id} wave={wave} n={len(rows)}")
    return len(rows)


def cancel_wave(c, lang_id, wave, actor):
    ids = [r["id"] for r in c.execute("SELECT s.id FROM scripts s JOIN jobs j ON j.script_id=s.id WHERE s.language_id=?"
                                      " AND s.wave=? AND j.status='QUEUED' AND s.status='QUEUED'", (lang_id, wave))]
    for i in ids:
        c.execute("DELETE FROM jobs WHERE script_id=? AND status='QUEUED'", (i,))
        c.execute("UPDATE scripts SET status='PLANNED' WHERE id=?", (i,))
    db.audit(c, actor, "cancel_wave", f"lang={lang_id} wave={wave} n={len(ids)}")
    return len(ids)


def retry_job(c, script_id, actor):
    j = c.execute("SELECT * FROM jobs WHERE script_id=?", (script_id,)).fetchone()
    if j:
        c.execute("UPDATE jobs SET status='QUEUED', attempts=0, fails=0, seed=seed+1, lease_until=0, error=NULL WHERE id=?", (j["id"],))
    else:
        c.execute("INSERT INTO jobs(script_id,status,seed,updated_at) VALUES(?, 'QUEUED', 1, ?)", (script_id, time.time()))
    c.execute("UPDATE scripts SET status='QUEUED', reject_reason=NULL WHERE id=?", (script_id,))
    db.audit(c, actor, "retry_job", f"script={script_id}")


def estimate(c, lang_id, wave):
    S = db.S(c)
    n = c.execute("SELECT COUNT(*) n FROM scripts WHERE language_id=? AND wave=? AND status='PLANNED'", (lang_id, wave)).fetchone()["n"]
    w = c.execute("SELECT * FROM api_keys WHERE status='active' AND role IN ('writer','spare') ORDER BY id LIMIT 1").fetchone()
    jk = c.execute("SELECT * FROM api_keys WHERE status='active' AND role='judge' ORDER BY id LIMIT 1").fetchone()
    mid = (int(S["words_min"]) + int(S["words_max"])) // 2
    tpw = float(S["tokens_per_word"])
    wout, win = mid * tpw * 1.15, 4 * 900 + mid * tpw * 0.8
    cost = 0.0
    if w:
        cost += n * (win * w["price_in"] + wout * w["price_out"]) / 1e6
    if jk:
        cost += n * ((mid * tpw + 300) * jk["price_in"] + 100 * jk["price_out"]) / 1e6
    return {"scripts": n, "calls": int(n * 5.6), "cost_inr": round(cost, 2), "priced": bool(w and (w["price_in"] or w["price_out"]))}


# ---------------------------------------------------------------- review
def backup_script(c, script_id):
    s = c.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    L = c.execute("SELECT name FROM languages WHERE id=?", (s["language_id"],)).fetchone()
    v = c.execute("SELECT turns FROM script_versions WHERE script_id=? AND version=?", (script_id, s["version"])).fetchone()
    if not v:
        return
    attrs = json.loads(s["attrs"]) if s["attrs"] else {"roles": ["A", "B"]}
    d = os.path.join(config.backup_dir(), config.PROJECT_NAME, L["name"], s["domain"], s["subdomain"], s["specialisation"] or "-")
    try:
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{s['code']}_v{s['version']}.txt"), "w", encoding="utf-8") as f:
            f.write(render_text(json.loads(v["turns"]), attrs["roles"]))
    except OSError as e:
        db.audit(c, "system", "backup_failed", f"{s['code']}: {e}")


def check_stop_the_line(c, actor="system"):
    S = db.S(c)
    rows = c.execute("SELECT verdict FROM reviews WHERE id>? ORDER BY id DESC LIMIT 30", (int(S["stop_after_review"]),)).fetchall()
    if len(rows) >= int(S["reject_stop_min"]):
        rate = 100 * sum(1 for r in rows if r["verdict"] == "reject") / len(rows)
        if rate > float(S["reject_stop_pct"]) and S["pause_generation"] != "1":
            db.set_setting(c, "pause_generation", 1)
            db.audit(c, actor, "stop_the_line", f"reject rate {rate:.1f}% over last {len(rows)} reviews")


def review(c, script_id, user, verdict, reason="", edited_text=None):
    s = c.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    if not s or s["status"] != "IN_REVIEW":
        raise ValueError("script is not in review")
    if user["role"] == "reviewer" and user["language_id"] != s["language_id"]:
        raise PermissionError("other language")
    L = c.execute("SELECT * FROM languages WHERE id=?", (s["language_id"],)).fetchone()
    attrs = json.loads(s["attrs"])
    S = db.S(c)
    now = time.time()
    if verdict == "accept":
        c.execute("UPDATE scripts SET status='APPROVED', updated_at=? WHERE id=?", (now, script_id))
        backup_script(c, script_id)
    elif verdict == "fix":
        turns, err = lines_to_turns(edited_text or "", attrs["roles"])
        if err:
            raise ValueError(err)
        errs = V.validate(turns, attrs, L, S) + V.similarity_check(c, L["id"], script_id, turns, float(S["sim_threshold"]))
        if errs:
            raise ValueError("Edited text fails validation: " + "; ".join(errs))
        ver = s["version"] + 1
        c.execute("INSERT INTO script_versions VALUES(?,?,?,?,?,?,?)",
                  (script_id, ver, json.dumps(turns, ensure_ascii=False), V.count_words(turns), s["prompt_version"], "reviewer fix", now))
        c.execute("UPDATE scripts SET status='APPROVED', version=?, words=?, shingles=?, opening=?, updated_at=? WHERE id=?",
                  (ver, V.count_words(turns), V.pack(V.shingle_set(turns)), V.opening_key(turns), now, script_id))
        backup_script(c, script_id)
    elif verdict == "reject":
        c.execute("UPDATE scripts SET reject_reason=? WHERE id=?", (reason, script_id))
        if c.execute("SELECT 1 FROM jobs WHERE script_id=?", (script_id,)).fetchone():
            retry_job(c, script_id, user)
            c.execute("UPDATE scripts SET reject_reason=? WHERE id=?", (reason, script_id))
        else:
            c.execute("UPDATE scripts SET status='PLANNED', shingles=NULL, opening=NULL WHERE id=?", (script_id,))
    else:
        raise ValueError("bad verdict")
    c.execute("INSERT INTO reviews(script_id,version,reviewer,verdict,reason,ts) VALUES(?,?,?,?,?,?)",
              (script_id, s["version"], user["id"], verdict, reason, now))
    db.audit(c, user, "review_" + verdict, f"{s['code']} {reason}")
    check_stop_the_line(c, user)


def manual_script(c, script_id, user, text):
    """Human-written script for a NEEDS_HUMAN or PLANNED slot. Same validators apply."""
    s = c.execute("SELECT * FROM scripts WHERE id=?", (script_id,)).fetchone()
    L = c.execute("SELECT * FROM languages WHERE id=?", (s["language_id"],)).fetchone()
    attrs = json.loads(s["attrs"])
    turns, err = lines_to_turns(text, attrs["roles"])
    if err:
        raise ValueError(err)
    S = db.S(c)
    errs = V.validate(turns, attrs, L, S) + V.similarity_check(c, L["id"], script_id, turns, float(S["sim_threshold"]))
    if errs:
        raise ValueError("Fails validation: " + "; ".join(errs))
    ver = s["version"] + 1
    now = time.time()
    c.execute("INSERT INTO script_versions VALUES(?,?,?,?,?,?,?)",
              (script_id, ver, json.dumps(turns, ensure_ascii=False), V.count_words(turns), "manual", "manual entry", now))
    c.execute("UPDATE scripts SET status='IN_REVIEW', version=?, words=?, shingles=?, opening=?, source='manual', needs_review=1,"
              " is_test=0, updated_at=? WHERE id=?",
              (ver, V.count_words(turns), V.pack(V.shingle_set(turns)), V.opening_key(turns), now, script_id))
    c.execute("UPDATE jobs SET status='DONE' WHERE script_id=?", (script_id,))
    db.audit(c, user, "manual_script", s["code"])


def quarantine_prompt(c, lang_id, pv, actor):
    ids = [r["id"] for r in c.execute("SELECT id FROM scripts WHERE language_id=? AND prompt_version=? AND status IN"
                                      " ('APPROVED','IN_REVIEW')", (lang_id, pv))]
    hit = []
    for i in ids:
        if c.execute("SELECT 1 FROM assignments WHERE script_id=? AND status NOT IN ('RELEASED','ABANDONED')", (i,)).fetchone():
            hit.append(c.execute("SELECT code FROM scripts WHERE id=?", (i,)).fetchone()["code"])
        else:
            c.execute("UPDATE scripts SET status='QUARANTINED' WHERE id=?", (i,))
    db.audit(c, actor, "quarantine_prompt", f"lang={lang_id} prompt={pv} quarantined={len(ids) - len(hit)} already_assigned={len(hit)}")
    return hit


def requeue_quarantined(c, lang_id, actor):
    ids = [r["id"] for r in c.execute("SELECT id FROM scripts WHERE language_id=? AND status='QUARANTINED'", (lang_id,))]
    for i in ids:
        retry_job(c, i, actor)
        c.execute("UPDATE scripts SET shingles=NULL, opening=NULL WHERE id=?", (i,))
    return len(ids)
