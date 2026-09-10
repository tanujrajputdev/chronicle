"""Rescue the state of in-flight work before Claude Code compacts it away.

The transcript survives compaction on disk; what dies is the model's grasp of it.
A checkpoint captures the parts a lossy summary reliably drops — the prompts you
actually typed, the files in flight, and the open task list — so the next context
can be handed them back verbatim.
"""
import os, json, datetime, collections
from .config import ROOT, INSTALL_DIR
from .ingest import _dt, _text_of, _clean, is_machinery, MUTATORS, file_root
from .redact import scrub

CP_DIR = ROOT / "checkpoints"
MAX_PROMPTS, MAX_FILES, MAX_TASKS = 12, 25, 20


def _scrub_task(t):
    """Task text is written to disk and handed back into the next context, so it
    goes through the same scrubber as everything else."""
    return {**t, "subject": scrub(t.get("subject", ""))[0],
            "description": scrub(t.get("description", ""))[0]}


def build(transcript_path, session_id=None, cwd=None, trigger="auto"):
    """Read a live transcript and distil everything since the last compaction."""
    records = []
    last_compact = -1
    with open(transcript_path, "r", errors="ignore") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            if o.get("isCompactSummary") or o.get("compactMetadata"):
                last_compact = len(records)
            records.append(o)

    window = records[last_compact + 1:] if last_compact >= 0 else records

    prompts, files, tools = [], [], collections.Counter()
    tasks, order = {}, []
    last_asst, branch, started, ended = "", None, None, None

    for o in window:
        ts = _dt(o.get("timestamp"))
        if ts:
            started = started or ts
            ended = ts
        if o.get("gitBranch"):
            branch = o["gitBranch"]
        msg = o.get("message") or {}

        if o.get("type") == "user" and not o.get("isMeta"):
            text, only_results = _text_of(msg.get("content"))
            if text and not only_results and not is_machinery(text):
                prompts.append((ts, text.strip()))

        elif o.get("type") == "assistant":
            text, _ = _text_of(msg.get("content"))
            if text.strip():
                last_asst = text.strip()
            c = msg.get("content")
            if isinstance(c, list):
                for b in c:
                    if not isinstance(b, dict) or b.get("type") != "tool_use":
                        continue
                    nm, inp = b.get("name") or "?", b.get("input") or {}
                    tools[nm] += 1
                    if nm in MUTATORS and isinstance(inp, dict):
                        fp = inp.get("file_path") or inp.get("notebook_path")
                        if fp and fp not in files:
                            files.append(fp)
                    elif nm == "TaskCreate" and isinstance(inp, dict):
                        tid = str(len(order) + 1)
                        order.append(tid)
                        tasks[tid] = {"subject": inp.get("subject", ""),
                                      "description": inp.get("description", ""),
                                      "status": "pending"}
                    elif nm == "TaskUpdate" and isinstance(inp, dict):
                        tid = str(inp.get("taskId", ""))
                        if tid in tasks:
                            if inp.get("status"):
                                tasks[tid]["status"] = inp["status"]
                            if inp.get("subject"):
                                tasks[tid]["subject"] = inp["subject"]

    goal = next((t for _, t in prompts if len(_clean(t)) > 20), prompts[0][1] if prompts else "")
    open_tasks = [tasks[t] for t in order if tasks[t]["status"] not in ("completed", "cancelled")]
    done_tasks = [tasks[t] for t in order if tasks[t]["status"] == "completed"]

    return {
        "session_id": session_id or os.path.basename(transcript_path)[:-6],
        "transcript": transcript_path,
        "cwd": cwd,
        "trigger": trigger,
        "created": datetime.datetime.now().isoformat(timespec="seconds"),
        "started": started.isoformat() if started else None,
        "ended": ended.isoformat() if ended else None,
        "branch": branch,
        "n_records": len(window),
        "had_prior_compaction": last_compact >= 0,
        "goal": scrub(_clean(goal)[:300])[0],
        "prompts": [scrub(_clean(t)[:400])[0] for _, t in prompts[-MAX_PROMPTS:]],
        "files": files[-MAX_FILES:],
        "roots": sorted({r for r in (file_root(f) for f in files) if r}),
        "tools": dict(tools.most_common(10)),
        "open_tasks": [_scrub_task(x) for x in open_tasks[:MAX_TASKS]],
        "done_tasks": [_scrub_task(x) for x in done_tasks[:MAX_TASKS]],
        "last_assistant": scrub(_clean(last_asst)[:900])[0],
    }


def render_markdown(cp):
    L = [f"# Checkpoint — {cp['created']}",
         f"*session `{cp['session_id']}` · trigger `{cp['trigger']}` · "
         f"{cp['n_records']} records since last compaction*\n"]
    if cp["cwd"]:
        L.append(f"- cwd: `{cp['cwd']}`" + (f" · branch `{cp['branch']}`" if cp["branch"] else ""))
    if cp["goal"]:
        L.append(f"\n## Working on\n{cp['goal']}")
    if cp["open_tasks"]:
        L.append("\n## Open tasks")
        for t in cp["open_tasks"]:
            L.append(f"- [ ] **{t['subject']}** — {t['description'][:220]}")
    if cp["done_tasks"]:
        L.append("\n## Completed this stretch")
        for t in cp["done_tasks"]:
            L.append(f"- [x] {t['subject']}")
    if cp["prompts"]:
        L.append("\n## Your last prompts, verbatim")
        for p in cp["prompts"]:
            L.append(f"- {p}")
    if cp["files"]:
        L.append(f"\n## Files in flight ({len(cp['files'])})")
        for f in cp["files"]:
            L.append(f"- `{f}`")
    if cp["last_assistant"]:
        L.append(f"\n## Where the last turn left off\n{cp['last_assistant']}")
    return "\n".join(L) + "\n"


def render_context(cp, ep_id=None):
    """Compact enough to inject into a fresh context without re-bloating it."""
    L = ["## Chronicle — recovered context (the compaction summary is lossy; this is verbatim)"]
    if cp["goal"]:
        L.append(f"**Working on:** {cp['goal']}")
    if cp["open_tasks"]:
        L.append("**Open tasks:** " + "; ".join(f"{t['subject']}" for t in cp["open_tasks"][:8]))
    if cp["prompts"]:
        L.append("**Last instructions you were given, in the user's own words:**")
        for p in cp["prompts"][-6:]:
            L.append(f"- {p[:200]}")
    if cp["files"]:
        L.append(f"**Files in flight:** " + ", ".join(f"`{f}`" for f in cp["files"][-10:]))
    tail = [f"Full checkpoint: `{cp.get('path','')}`"]
    if ep_id:
        tail.append(f"Full history: `{INSTALL_DIR}/chronicle-cli show {ep_id}`")
    L.append("_" + " · ".join(tail) + "_")
    return "\n".join(L)


def save(cp):
    d = CP_DIR / cp["session_id"]
    d.mkdir(parents=True, exist_ok=True)
    stamp = cp["created"].replace(":", "").replace("-", "")
    path = d / f"{stamp}.md"
    cp["path"] = str(path)
    path.write_text(render_markdown(cp))
    (d / "latest.json").write_text(json.dumps(cp, indent=1))
    return path


def latest(session_id=None):
    if not CP_DIR.exists():
        return None
    if session_id:
        f = CP_DIR / session_id / "latest.json"
        return json.loads(f.read_text()) if f.exists() else None
    best, newest = None, ""
    for d in CP_DIR.iterdir():
        f = d / "latest.json"
        if f.exists():
            cp = json.loads(f.read_text())
            if cp["created"] > newest:
                best, newest = cp, cp["created"]
    return best


def all_checkpoints():
    out = []
    if not CP_DIR.exists():
        return out
    for d in sorted(CP_DIR.iterdir()):
        for f in sorted(d.glob("*.md")):
            out.append(f)
    return out
