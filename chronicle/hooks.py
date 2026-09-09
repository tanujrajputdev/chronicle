"""Claude Code hook handlers. These run inside the user's session — they must be
fast, silent on success, and must never raise. Every path exits 0."""
import os, sys, json, time, subprocess, datetime
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
        return _recall(p, st)
    except Exception as e:
        _log("UserPromptSubmit", f"recall ERROR {type(e).__name__}: {e}")
        return {}


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


def run(event):
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}
    try:
        out = HANDLERS.get(event.lower(), lambda p: {})(payload)
    except Exception as e:
        _log(event, f"ERROR {type(e).__name__}: {e}")
        out = {}
    if out:
        sys.stdout.write(json.dumps(out))
    return 0
