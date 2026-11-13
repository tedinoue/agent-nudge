#!/usr/bin/env python3
"""The before-act nudge service, one per machine (launchd on macOS, systemd --user on Linux; templates in units/).

Why a service: the hook runs on every act, and a key fetched from a secret store can be slow, so the key is read ONCE
here at start and the hook only asks this process. It listens on 127.0.0.1:8395 and answers
POST /judge {sid, seat, tool_name, tool_input, transcript_path} with {"action": "deny"|"note"|"none", "text": "..."}.
Key: ANTHROPIC_API_KEY in the service's environment.

Policy (calibration notes in docs/CALIBRATION.md):
- "sure" -> deny ONCE: the reminder reaches the agent before the act runs. The same call re-issued unchanged goes through
  (recorded in nudge_fires.jsonl as "same call re-issued"), so a wrong nudge costs one retry, never a block.
- "maybe" -> a note that rides along with the tool result (PreToolUse additionalContext arrives with the result).
- One rule fires at most once per session per 15 judged acts (no repeats).
- Off switch: the file ~/.claude/nudge_off makes every answer "none".
Hard budget 5 s per verdict on the api backend, 15 s on subscription (the hook waits 16 s): a late verdict is dropped unrecorded. 3 failures in a row open a
5-minute breaker (answer "none" at once), write ~/.claude/logs/nudge_service_FAILING and tell each session once; GET /
shows health. POST needs header x-nudge: 1. Logs are mode 600 (they hold command text).
Logs: ~/.claude/logs/nudge_calls.jsonl (every judged act), nudge_acts.jsonl (the same acts with their full text), nudge_shadow.jsonl (the shadow judge's verdict beside Haiku's) and nudge_fires.jsonl (fires, plus what the NEXT act did:
changed / same call re-issued). Any error answers "none" (fail open).
"""
import hashlib, json, os, socket, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nudge_core as N

PORT = int(os.environ.get("NUDGE_PORT", "8395"))
LOGD = os.environ.get("NUDGE_LOGD") or os.path.expanduser("~/.claude/logs")
OFF = os.path.expanduser("~/.claude/nudge_off")
REPEAT_WINDOW = 15
BUDGET_S = 15.0 if N.BACKEND == "subscription" else 5.0   # subscription: complex acts take up to ~13 s (11-10 replay). The hook waits up to 16 s; a verdict later than the budget is dropped unrecorded (review 11-09: a late "deny" was spent unseen)
BREAK_AFTER, BREAK_FOR_S = 3, 300   # 3 failures in a row -> answer "none" at once for 5 minutes (no 9 s tax on every act)
HEALTH = {"consecutive_errors": 0, "last_error": "", "open_until": 0.0, "told": set()}
POOL = __import__("concurrent.futures").futures.ThreadPoolExecutor(8)
# SHADOW: with NUDGE_SHADOW=deepseek, every judged act also goes to DeepSeek AFTER the hook has its
# answer; its verdict is only logged (nudge_shadow.jsonl), never acted on, so it adds no delay and cannot change an act.
SHADOW = os.environ.get("NUDGE_SHADOW", "")
SHADOW_POOL = __import__("concurrent.futures").futures.ThreadPoolExecutor(4)
SHADOW_MAX_QUEUED = 32   # past this the shadow skips (and counts) acts rather than build a backlog
SHADOW_STATE = {"key": "", "queued": 0, "skipped": 0, "errors": 0, "done": 0}
HOST = socket.gethostname()
LOCK = threading.Lock()
STATE = {}  # sid -> {"n": judged acts, "last_rule_at": {rule: n}, "denied": set(hash), "pending": fire record awaiting the next act}


def fetch_key():
    k = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not k:
        raise SystemExit("nudge_service: set ANTHROPIC_API_KEY in the service's environment")
    return k


def tokens(u):
    """The billable counts of one judge call's usage, logged for cost accounting."""
    c = u.get("cache_creation") or {}
    out = {"in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0), "cr": u.get("cache_read_input_tokens", 0),
           "cw5": c.get("ephemeral_5m_input_tokens", u.get("cache_creation_input_tokens", 0) if not c else 0), "cw1h": c.get("ephemeral_1h_input_tokens", 0)}
    if u.get("cost_usd") is not None: out["usd"] = u["cost_usd"]
    return out


def late_record(fut, usage, seat, sid, tool, summary):
    """A verdict that landed after the budget: paid for (calls log) and, for the training set, the act and what the judge
    said (acts log, "late": true; the slowest acts are often the hardest ones)."""
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    log("nudge_calls.jsonl", {"ts": ts, "host": HOST, "seat": seat, "sid": sid, "tool": tool, "late": True, "model": N.MODEL, "usage": tokens(usage)})
    try: v = fut.result()
    except Exception: return
    log("nudge_acts.jsonl", {"ts": ts, "host": HOST, "seat": seat, "sid": sid, "tool": tool, "rule": v and v["rule"],
                             "certainty": v and v["certainty"], "action": "late", "model": N.MODEL, "coding": N.is_coding(seat),
                             "line": v and v["line"], "act": summary})


def fetch_shadow_key():
    return os.environ.get("NUDGE_SHADOW_KEY", "").strip()   # the shadow model's API key (DeepSeek), set in the service's environment


def shadow(summary, seat, sid, tool, rec, h=""):
    """Queue the DeepSeek judgment of this act beside Haiku's (rec); never raises into the request path."""
    if not (SHADOW and SHADOW_STATE["key"]):
        return
    with LOCK:
        if SHADOW_STATE["queued"] >= SHADOW_MAX_QUEUED:
            SHADOW_STATE["skipped"] += 1; return
        SHADOW_STATE["queued"] += 1
    def run():
        t, u, out = time.time(), {}, {}
        try:
            v = N.judge_deepseek(summary, SHADOW_STATE["key"], 20.0, u, N.is_coding(seat))
            out = {"rule": v and v["rule"], "certainty": v and v["certainty"], "line": v and v["line"]}
        except Exception as e:
            out = {"error": f"{type(e).__name__}: {str(e)[-200:]}".replace(SHADOW_STATE["key"], "<key>")}
        with LOCK:
            SHADOW_STATE["queued"] -= 1; SHADOW_STATE["errors" if "error" in out else "done"] += 1
        log("nudge_shadow.jsonl", {"ts": rec["ts"], "host": HOST, "seat": seat, "sid": sid, "tool": tool, "hash": h, "model": N.DS_MODEL,
                                   "secs": round(time.time() - t, 2), "shadow": out,
                                   "haiku": {"rule": rec.get("rule"), "certainty": rec.get("certainty"), "action": rec.get("action"),
                                             "secs": rec.get("secs")},
                                   "usage": {"hit": u.get("prompt_cache_hit_tokens", 0), "miss": u.get("prompt_cache_miss_tokens", 0),
                                             "out": u.get("completion_tokens", 0)}})
    try:
        SHADOW_POOL.submit(run)
    except Exception:
        with LOCK: SHADOW_STATE["queued"] -= 1


# The call's row is either already in the transcript (all such acts 11-11: 0 ms, one 170 ms) or arrives only after this
# hook returns; 0.3 s covers the first and stops paying a full second for the second.
WORDS_WAIT_S = 0.3
UNSEEN_WORDS = "(not available: the agent's words for this action reach the transcript only after this check runs)"


def watch_flush(path, tid, sid, tool, limit=15.0):
    """After an act whose call row was not yet in the transcript: poll until it appears (or `limit` s) and log the delay
    to nudge_flush.jsonl. Measurement only; never on the agent's path."""
    t0 = time.time()
    while time.time() - t0 < limit:
        try:
            with open(path, "rb") as f:
                f.seek(0, 2); n = f.tell(); f.seek(max(0, n - 300_000))
                if tid.encode() in f.read():
                    break
        except Exception:
            return
        time.sleep(0.1)
    log("nudge_flush.jsonl", {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "sid": sid, "tool": tool, "tool_use_id": tid,
                              "appeared_after_ms": int((time.time() - t0) * 1000) if time.time() - t0 < limit else None})


def log(name, rec):
    try:
        os.makedirs(LOGD, exist_ok=True)
        p = os.path.join(LOGD, name)
        with os.fdopen(os.open(p, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600), "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def act_hash(tool_name, tool_input):
    return hashlib.sha256((tool_name + json.dumps(tool_input, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()[:16]


def broken_note(sid):
    """While the service is failing, each session is told ONCE (review 11-09: a dead nudge must not look healthy)."""
    with LOCK:
        if sid in HEALTH["told"]:
            return {"action": "none"}
        HEALTH["told"].add(sid)
    return {"action": "note", "text": f"Nudge service on {HOST} is failing ({HEALTH['last_error']}); before-act reminders are off until it is fixed. Check the service log."}


def decide(req):
    sid, seat, tool = req.get("sid", ""), req.get("seat", ""), req.get("tool_name", "")
    ti = req.get("tool_input") or {}
    if os.path.exists(OFF) or not N.worth_judging(tool):
        return {"action": "none"}
    h = act_hash(tool, ti)
    with LOCK:
        st = STATE.setdefault(sid, {"n": 0, "last_rule_at": {}, "denied": set(), "pending": None})
        st["n"] += 1; n = st["n"]
        p, st["pending"] = st["pending"], None
        if p:  # what the agent did right after a fire
            p["next"] = "same call re-issued" if p["hash"] == h else "changed"
            log("nudge_fires.jsonl", p)
        if h in st["denied"]:  # the agent re-issued a denied call unchanged: let it through
            st["denied"].discard(h)
            log("nudge_calls.jsonl", {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "host": HOST, "seat": seat, "sid": sid, "tool": tool,
                                      "action": "reissued"})   # the paused call sent again unchanged: passed, not judged
            return {"action": "none", "overridden": True}
    if time.time() < HEALTH["open_until"]:   # before the transcript read: "answer none at once" must not wait for it
        return broken_note(sid)
    wait = {}
    words, user_msg, prev = N.transcript_context(req.get("transcript_path", ""), wait_for=req.get("tool_use_id", ""),
                                            max_wait=WORDS_WAIT_S, stats=wait)
    if req.get("helper"):
        # a helper agent's act: its own words live in its subagents/ file, so the newest text in this transcript is the
        # PARENT's (9 of 17 acts in one sample). Same treatment as an unseen row.
        words = UNSEEN_WORDS
    if wait.get("id_seen") is False:
        # This act's own message is not in the transcript yet, so the newest text there is the PREVIOUS message's: judged
        # on it, 10 of 10 such acts on 11-11 got stale words, and their rows landed only after the hook had returned (15 of
        # 15), so no wait can fix it. Judge without the words rather than with the wrong ones.
        words = UNSEEN_WORDS
        # measure how late the row lands, off the agent's path (stale-words study 11-11)
        threading.Thread(target=watch_flush, args=(req.get("transcript_path", ""), req.get("tool_use_id", ""), sid, tool),
                         daemon=True).start()
    summary = N.act_summary(tool, ti, words, user_msg, prev, seat)
    t0 = time.time()
    usage, fut = {}, None
    try:
        fut = POOL.submit(N.judge, summary, KEY, BUDGET_S, usage, N.is_coding(seat))
        r = fut.result(timeout=BUDGET_S)
        with LOCK: HEALTH["consecutive_errors"] = 0
    except Exception as e:
        timed_out = isinstance(e, __import__("concurrent.futures").futures.TimeoutError)
        if fut is not None and timed_out:  # a dropped verdict is still paid for: log its usage when it lands (at once if it already has)
            fut.add_done_callback(lambda f: usage and late_record(f, usage, seat, sid, tool, summary))
        err = ("timeout (verdict dropped)" if timed_out else
               f"claude -p timed out after {e.timeout:g} s" if isinstance(e, subprocess.TimeoutExpired) else
               f"{type(e).__name__}: {str(e)[-200:]}")  # the tail says what happened; the head of a command is just the prompt (11-10)
        with LOCK:
            HEALTH["consecutive_errors"] += 1; HEALTH["last_error"] = err
            if HEALTH["consecutive_errors"] >= BREAK_AFTER:
                HEALTH["open_until"] = time.time() + BREAK_FOR_S
                try: open(os.path.join(LOGD, "nudge_service_FAILING"), "w").write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {err}\n")
                except Exception: pass
        erec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "host": HOST, "seat": seat, "sid": sid, "tool": tool, "error": err}
        if usage and not timed_out:  # the call was answered (and paid for), then failed: e.g. an unparseable verdict
            erec.update(model=N.MODEL, usage=tokens(usage))
        log("nudge_calls.jsonl", erec)
        return broken_note(sid) if time.time() < HEALTH["open_until"] else {"action": "none"}
    try: os.remove(os.path.join(LOGD, "nudge_service_FAILING"))
    except Exception: pass
    secs = round(time.time() - t0, 2)
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "host": HOST, "seat": seat, "sid": sid, "tool": tool, "secs": secs,
           "rule": r and r["rule"], "certainty": r and r.get("certainty"), "model": N.MODEL, "usage": tokens(usage)}
    action = "none"
    if r:
        with LOCK:
            last = st["last_rule_at"].get(r["rule"])
            if last is not None and n - last < REPEAT_WINDOW:
                rec["suppressed"] = "repeat"
            else:
                st["last_rule_at"][r["rule"]] = n
                action = "deny" if r.get("certainty") == "sure" else "note"
                if action == "deny":
                    st["denied"].add(h)
                st["pending"] = dict(rec, action=action, line=r["line"], hash=h, act=summary)  # whole summary: a 600 clip cut the act off once context came first (11-09)
    rec["action"] = action
    log("nudge_calls.jsonl", rec)
    # Every judged act's full text, silent ones included:
    # the labelled set a small local judge is trained and tested on. Its own file (mode 600) so the calls log stays small.
    shadow(summary, seat, sid, tool, rec, h)
    log("nudge_acts.jsonl", dict({k: rec.get(k) for k in ("ts", "host", "seat", "sid", "tool", "rule", "certainty", "action", "suppressed", "model")}, hash=h,
                                 coding=N.is_coding(seat), line=r and r["line"], act=summary,
                                 tool_use_id=req.get("tool_use_id", ""), **wait))   # whole: act_summary is bounded, and a cut drops the context lines
    if action == "none":
        return {"action": "none"}
    return {"action": action, "text": f"Nudge {r['rule']}: {r['line']}"}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("content-type", "application/json"); self.send_header("content-length", str(len(b)))
        self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        self._send(200, {"ok": HEALTH["consecutive_errors"] == 0, "host": HOST, "model": N.MODEL, "backend": N.BACKEND, "rules": len(N.RULE_IDS), "off": os.path.exists(OFF),
                        "consecutive_errors": HEALTH["consecutive_errors"], "last_error": HEALTH["last_error"], "breaker_open": time.time() < HEALTH["open_until"],
                        "shadow": SHADOW and {"model": N.DS_MODEL, "key": bool(SHADOW_STATE["key"]), **{k: v for k, v in SHADOW_STATE.items() if k != "key"}}})

    def do_POST(self):
        if self.headers.get("x-nudge") != "1":  # a browser page cannot send this header without a preflight, which fails
            return self._send(403, {"action": "none"})
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("content-length", "0") or 0)) or b"{}")
            self._send(200, decide(req))
        except Exception as e:
            self._send(200, {"action": "none", "error": str(e)[:200]})


if __name__ == "__main__":
    KEY = fetch_key() if N.BACKEND == "api" else ""   # the subscription backend needs no key
    if SHADOW == "deepseek":
        SHADOW_STATE["key"] = fetch_shadow_key()
        print(f"nudge_service: shadow {N.DS_MODEL} {'on' if SHADOW_STATE['key'] else 'OFF: no shadow key'}", flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print(f"nudge_service: listening on 127.0.0.1:{PORT} ({HOST}, {N.MODEL} via {N.BACKEND}, {len(N.RULE_IDS)} rules)", flush=True)
    srv.serve_forever()
