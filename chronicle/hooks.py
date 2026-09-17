"""Claude Code hook handlers. These run inside the user's session — they must be
fast, silent on success, and must never raise. Every path exits 0."""
import os, sys, json, time, select, signal, subprocess, datetime
from .config import ROOT, MEMORY_DIR
from . import checkpoint

STATE = ROOT / "hook-state.json"
DEBOUNCE = 900  # don't re-index from a keystroke more than once every 15 min


def _state():
    try:
        return json.loads(STATE.read_text())
    except Exception:
        return {}


def _save_state(d):
    try:
        STATE.write_text(json.dumps(d))
    except Exception:
        pass


def _detach(args):
    """Run work in the background; the session must never wait on us."""
    try:
        subprocess.Popen(
            [sys.executable, "-m", "chronicle.cli"] + args,
            cwd=str(MEMORY_DIR), start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env={**os.environ, "PYTHONPATH": str(MEMORY_DIR)})
    except Exception:
        pass


def _log(event, msg):
    try:
        with open(ROOT / "hooks.log", "a") as fh:
            fh.write(f"{datetime.datetime.now().isoformat(timespec='seconds')} {event} {msg}\n")
    except Exception:
        pass


def _episode_for(session_id):
    try:
        from . import db
        con = db.connect_ro()
        r = con.execute("SELECT id FROM episodes WHERE session_id=? ORDER BY started DESC LIMIT 1",
                        (session_id,)).fetchone()
        return r["id"] if r else None
    except Exception:
        return None


# ---------------------------------------------------------------- handlers

def pre_compact(p):
    """Context is about to be summarised away. Capture what a summary loses."""
    tp = p.get("transcript_path")
    if not tp or not os.path.exists(tp):
        return {}
    cp = checkpoint.build(tp, p.get("session_id"), p.get("cwd"), p.get("trigger", "auto"))
    path = checkpoint.save(cp)
    _detach(["index"])
    _log("PreCompact", f"{cp['session_id']} {cp['n_records']}rec "
                       f"{len(cp['open_tasks'])}open {len(cp['files'])}files -> {path}")
    n = len(cp["open_tasks"])
    return {"systemMessage":
            f"Chronicle saved a checkpoint before compaction "
            f"({len(cp['prompts'])} prompts, {len(cp['files'])} files"
            + (f", {n} open task{'s' if n != 1 else ''}" if n else "") + ")."}


def session_start(p):
    """After a compaction, hand back the detail. On a cold start, brief the project."""
    if os.environ.get("CHRONICLE_NO_BRIEF"):
        return {}
    trig = p.get("trigger") or p.get("source")
    cp = checkpoint.latest(p.get("session_id")) if trig in ("compact", "resume") else None
    if not cp:
        return _brief(p, trig)
    age = time.time() - os.path.getmtime(cp["path"]) if cp.get("path") and os.path.exists(cp["path"]) else 1e9
    if trig == "resume" and age > 7 * 86400:
        return {}
    ctx = checkpoint.render_context(cp, _episode_for(p.get("session_id", "")))
    _log("SessionStart", f"{trig} restored {cp['session_id']} ({len(ctx)} chars)")
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx},
            "additionalContext": ctx}


def _brief(p, trig):
    """Cold start: what does this project need me to already know?"""
    try:
        from . import db, brief
        con = db.connect_ro(timeout_ms=1500)
        pid = brief.project_for_cwd(con, p.get("cwd"))
        if not pid:
            return {}
        text = brief.build(con, pid, exclude_session=p.get("session_id"))
        if not text:
            return {}
        _log("SessionStart", f"{trig} briefed {pid} ({len(text)} chars)")
        return {"hookSpecificOutput": {"hookEventName": "SessionStart",
                                       "additionalContext": text},
                "additionalContext": text}
    except Exception as e:
        _log("SessionStart", f"brief ERROR {type(e).__name__}: {e}")
        return {}


def session_end(p):
    """Budget here is 1.5s for all hooks — do nothing but hand off."""
    _detach(["index"])
    _detach(["digest"])
    _log("SessionEnd", str(p.get("reason")))
    return {}


MAX_RECALL_PER_SESSION = 2
MAX_LOOKUP_PER_SESSION = 3


def user_prompt_submit(p):
    st = _state()
    now = time.time()
    if now - st.get("last_index", 0) > DEBOUNCE:
        st["last_index"] = now
        _save_state(st)
        _detach(["index"])
    if os.environ.get("CHRONICLE_NO_RECALL"):
        return {}
    try:
        from . import recall
        # Only ever one of the two speaks, and an explicit question about the past
        # outranks an inferred resemblance to it. The budget being spent here is
        # the user's attention, not tokens.
        if recall.intent_of(p.get("user_input") or ""):
            return _lookup(p, st)
        return _recall(p, st)
    except Exception as e:
        _log("UserPromptSubmit", f"recall ERROR {type(e).__name__}: {e}")
        return {}


def _lookup(p, st):
    """The user asked about their own past work — so answer, rather than waiting to
    be asked a second time with the word "chronicle" in it."""
    sid = p.get("session_id", "")
    box = st.setdefault("lookup", {}).setdefault(sid, {"n": 0, "ids": []})
    if box["n"] >= MAX_LOOKUP_PER_SESSION:
        return {}
    from . import db, brief, recall
    try:
        con = db.connect_ro()
        pid = brief.project_for_cwd(con, p.get("cwd"))
        hits = recall.lookup(con, p.get("user_input") or "",
                             cwd_project=pid, exclude_session=sid)
    except Exception:
        return {}          # index busy or missing: stay out of the way
    fresh = [h for h in hits if h["id"] not in box["ids"]]
    if not fresh:
        # logged even when empty: a retrieval question that found nothing is the
        # one signal worth having when tuning this, and nothing else records it.
        # Which kind of empty it was matters — "found nothing" and "found only
        # what I already said" call for different fixes.
        _log("UserPromptSubmit", "intent, already shown" if hits else "intent, no match")
        return {}
    hits = fresh
    box["n"] += 1
    box["ids"].extend(h["id"] for h in hits)
    st["lookup"] = {sid: box}          # only track the live session
    _save_state(st)
    out = recall.render_lookup(hits)
    _log("UserPromptSubmit", "intent " + ", ".join(
        f"#{h['id']}({h['project']})" for h in hits))
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": out},
            "additionalContext": out}


def _recall(p, st):
    text = p.get("user_input") or ""
    if len(text) < 40:
        return {}
    sid = p.get("session_id", "")
    seen = st.setdefault("recall", {}).setdefault(sid, [])
    if len(seen) >= MAX_RECALL_PER_SESSION:
        return {}
    from . import db, recall
    try:
        con = db.connect_ro()
        hits = recall.find(con, text, exclude_session=sid, exclude_ids=tuple(seen))
    except Exception:
        return {}          # index busy or missing: stay out of the way
    if not hits:
        return {}
    seen.extend(h["id"] for h in hits)
    st["recall"] = {sid: seen}          # only track the live session
    _save_state(st)
    out = recall.render(hits)
    _log("UserPromptSubmit", "recall " + ", ".join(
        f"#{h['id']}({h['coverage']:.0%})" for h in hits))
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": out},
            "additionalContext": out}


HANDLERS = {
    "precompact": pre_compact,
    "sessionstart": session_start,
    "sessionend": session_end,
    "userpromptsubmit": user_prompt_submit,
}


# Every hook gets two budgets: how long it may wait for the payload, and how long
# the whole run may take. Both sit well under the timeout `install` writes into
# settings.json, so the harness never has to kill us and never discards output.
BUDGET = {
    "userpromptsubmit": (1.5, 6.0),     # harness timeout 10s
    "sessionstart":     (1.5, 12.0),    # harness timeout 20s
    "sessionend":       (1.0, 3.0),     # harness timeout 5s
    "precompact":       (3.0, 420.0),   # harness timeout 600s
}


def _read_payload(budget, event):
    """Read the hook payload without ever waiting on EOF.

    `sys.stdin.read()` returns only when the writer closes the pipe. Claude Code
    writes the JSON and may hold the pipe open — and then the session stalls until
    the harness kills us and throws the output away ("hook timed out after 30s").
    JSON is self-delimiting, so stop as soon as the object parses, and give up
    quietly at the deadline rather than blocking the user's prompt.
    """
    fd, buf = sys.stdin.fileno(), b""
    deadline = time.monotonic() + budget
    while True:
        if buf.rstrip()[-1:] == b"}":          # cheap: only try to parse a plausible end
            try:
                return json.loads(buf.decode("utf-8", "replace"))
            except Exception:
                pass
        left = deadline - time.monotonic()
        if left <= 0:
            _log(event, f"payload incomplete after {budget}s ({len(buf)}B) — proceeding without it")
            return {}
        try:
            if not select.select([fd], [], [], left)[0]:
                break
            chunk = os.read(fd, 1 << 16)
        except Exception:
            break
        if not chunk:                          # genuine EOF
            break
        buf += chunk
    try:
        return json.loads(buf.decode("utf-8", "replace")) if buf.strip() else {}
    except Exception:
        return {}


def _deadline(seconds):
    """Bound the whole run. A hook that hangs for any reason — a lock, a huge
    transcript, a filesystem stall — costs the user the same as one that crashes,
    so time out ourselves instead of letting the session wait on us."""
    def bail(signum, frame):
        _log("watchdog", f"exceeded {seconds}s — exiting quietly")
        os._exit(0)
    try:
        signal.signal(signal.SIGALRM, bail)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    except Exception:
        pass


def _deadline_off():
    try:
        signal.setitimer(signal.ITIMER_REAL, 0)
    except Exception:
        pass


def run(event):
    ev = event.lower()
    read_budget, total_budget = BUDGET.get(ev, (1.5, 10.0))
    _deadline(total_budget)
    payload = _read_payload(read_budget, ev)
    try:
        out = HANDLERS.get(ev, lambda p: {})(payload)
    except Exception as e:
        _log(event, f"ERROR {type(e).__name__}: {e}")
        out = {}
    _deadline_off()
    if out:
        try:
            sys.stdout.write(json.dumps(out))
            sys.stdout.flush()
        except Exception:
            pass
    return 0
