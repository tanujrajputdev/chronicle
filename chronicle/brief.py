"""The project brief: what a fresh session needs to know before you type anything.

Fires on every session start. The cost of being wrong here is a few hundred wasted
tokens; the cost of not having it is re-explaining a project you have worked on for
weeks. Keep it short, concrete, and only about this project.
"""
import os, json, datetime
from .config import MEMORY_DIR
from . import checkpoint
from .ingest import _clean

MAX_CHARS = 1800


def project_for_cwd(con, cwd):
    """Longest path match against every root this project has ever touched."""
    if not cwd:
        return None
    best, pid = -1, None
    for r in con.execute("SELECT id, paths FROM projects"):
        for p in json.loads(r["paths"] or "[]"):
            if cwd == p or cwd.startswith(p.rstrip("/") + "/") or p.startswith(cwd.rstrip("/") + "/"):
                score = len(os.path.commonprefix([cwd, p]))
                if score > best:
                    best, pid = score, r["id"]
    return pid


def _ago(iso):
    d = datetime.date.fromisoformat(iso[:10])
    n = (datetime.date.today() - d).days
    if n == 0: return "earlier today"
    if n == 1: return "yesterday"
    if n < 14: return f"{n} days ago"
    if n < 60: return f"{n // 7} weeks ago"
    return f"{d.isoformat()}"


def build(con, pid, exclude_session=None):
    eps = con.execute("""SELECT DISTINCT e.* FROM episodes e
                         JOIN episode_projects p ON p.episode_id=e.id
                         WHERE p.project_id=? AND (? IS NULL OR e.session_id != ?)
                         ORDER BY e.started DESC LIMIT 4""",
                      (pid, exclude_session, exclude_session)).fetchall()
    if not eps:
        return None
    row = con.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
    name = row["name"] if row else pid
    tot = con.execute("""SELECT COUNT(DISTINCT e.id) n, COUNT(DISTINCT substr(e.started,1,10)) d
                         FROM episodes e JOIN episode_projects p ON p.episode_id=e.id
                         WHERE p.project_id=?""", (pid,)).fetchone()
    runs = con.execute("SELECT COUNT(*) n FROM agent_runs WHERE project_id=?", (pid,)).fetchone()["n"]

    last = eps[0]
    L = [f"## Chronicle — {name}, picking up from {_ago(last['started'])}",
         f"**Last session ({last['started'][:10]}, #{last['id']}):** {last['title']}"]

    tail = con.execute("SELECT text FROM messages WHERE episode_id=? AND kind='prompt' "
                       "ORDER BY ts DESC LIMIT 2", (last["id"],)).fetchall()
    if tail:
        L.append("**It ended on:** " + " / ".join(
            f'"{_clean(t["text"] or "")[:130]}"' for t in reversed(tail)))

    cp = None
    for e in eps:
        c = checkpoint.latest(e["session_id"])
        if c and c.get("open_tasks"):
            cp = c
            break
    if cp:
        L.append("**Open tasks when it stopped:** " +
                 "; ".join(t["subject"] for t in cp["open_tasks"][:6]))

    files = []
    for e in eps:
        for f in json.loads(e["files_touched"] or "[]"):
            if f not in files:
                files.append(f)
    if files:
        L.append("**Recently edited:** " + ", ".join(f"`{f}`" for f in files[:6]))

    if len(eps) > 1:
        L.append("**Before that:** " + " · ".join(
            f"{e['started'][:10]} {e['title'][:60]} (#{e['id']})" for e in eps[1:4]))

    dec = MEMORY_DIR / "projects" / pid / "decisions.md"
    if dec.exists():
        body = [l for l in dec.read_text().splitlines()
                if l.strip() and not l.startswith("#") and "Chronicle never touches" not in l
                and "Hand-written" not in l and "Record the" not in l]
        if body:
            L.append("**Standing decisions (from your notes):** " + " ".join(body)[:400])

    L.append(f"_{tot['n']} episodes over {tot['d']} days and {runs} agent runs are indexed for "
             f"{name}. Ask me anything about them — I have the chronicle skill; nothing is loaded "
             f"into context until you ask._")

    out = "\n".join(L)
    return out[:MAX_CHARS]
