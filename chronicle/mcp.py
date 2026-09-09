"""MCP server over stdio. Read-only, stdlib only.

Exposes the index to any MCP client so a session in one project can ask about work
done in another. Nothing here writes: the worst a client can do is read its own history.

stdout carries JSON-RPC and nothing else; diagnostics go to stderr.
"""
import sys, json, os, sqlite3, datetime, traceback

PROTOCOL = "2025-06-18"
SUPPORTED = {"2025-06-18", "2025-03-26", "2024-11-05"}
VERSION = "0.1.0"

INSTRUCTIONS = (
    "Chronicle indexes every Claude Code session on this machine — what was worked on, "
    "when, which agents ran, and what they concluded. Use it to answer questions about "
    "past work instead of guessing or re-doing it. Start with `search` or `timeline`, then "
    "`episode` for detail. Never read raw transcripts from ~/.claude/projects; they are "
    "hundreds of megabytes. Episode ids are permanent and safe to quote back to the user."
)


def _db():
    from .config import DB_PATH
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


def _n(x):
    x = x or 0
    if x >= 1_000_000_000: return f"{x/1e9:.1f}B"
    if x >= 1_000_000: return f"{x/1e6:.1f}M"
    if x >= 1_000: return f"{x/1e3:.1f}k"
    return str(x)


def _dur(s):
    h, m = divmod(int(s or 0) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _proj(con, pid):
    r = con.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
    return r["name"] if r else pid


def _ep_line(e):
    marks = []
    if e["n_agents"]: marks.append(f"{e['n_agents']} agents")
    if e["compactions"]: marks.append(f"{e['compactions']} compactions")
    nf = len(json.loads(e["files_touched"] or "[]"))
    if nf: marks.append(f"{nf} files")
    tail = f" [{', '.join(marks)}]" if marks else ""
    return (f"#{e['id']}  {e['started'][:10]}  {e['project_id']}  {e['title']}\n"
            f"      {e['n_prompts']} prompts · {_dur(e['duration_s'])} · "
            f"{_n(e['out_tokens'])} tokens{tail}")


# ---------------------------------------------------------------- tools

def t_projects(con, a):
    rows = con.execute("""SELECT p.project_id id, COUNT(DISTINCT p.episode_id) eps,
                          MAX(e.ended) last, COUNT(DISTINCT substr(e.started,1,10)) days
                          FROM episode_projects p JOIN episodes e ON e.id=p.episode_id
                          GROUP BY p.project_id ORDER BY last DESC""").fetchall()
    out = ["Projects, most recently worked first:"]
    for r in rows:
        out.append(f"  {_proj(con, r['id'])} (id: {r['id']}) — {r['eps']} "
                   f"episode{'s' if r['eps'] != 1 else ''} over {r['days']} "
                   f"day{'s' if r['days'] != 1 else ''}, last {r['last'][:10]}")
    return "\n".join(out)


def t_search(con, a):
    q = (a.get("query") or "").strip()
    if not q:
        return "Provide a query."
    lim = min(int(a.get("limit") or 10), 30)
    sql = """SELECT e.*, snippet(episodes_fts,2,'<<','>>','…',16) s
             FROM episodes_fts JOIN episodes e ON e.id=episodes_fts.rowid
             WHERE episodes_fts MATCH ?"""
    args = [q]
    if a.get("project"):
        sql += " AND e.id IN (SELECT episode_id FROM episode_projects WHERE project_id LIKE ?)"
        args.append(f"%{a['project']}%")
    sql += " ORDER BY bm25(episodes_fts) LIMIT ?"
    args.append(lim)
    try:
        eps = con.execute(sql, args).fetchall()
        ags = con.execute("""SELECT a.*, snippet(agents_fts,1,'<<','>>','…',12) s
                             FROM agents_fts JOIN agent_runs a ON a.id=agents_fts.rowid
                             WHERE agents_fts MATCH ? ORDER BY bm25(agents_fts) LIMIT 5""",
                          (q,)).fetchall()
    except sqlite3.OperationalError as e:
        return (f"That query is not valid FTS5 syntax ({e}). Use plain words, OR, NOT, "
                f'"exact phrase", or prefix*.')
    if not eps and not ags:
        return f"Nothing matched {q!r}. Try fewer or broader words."
    out = [f"{len(eps)} episodes matching {q!r}:"]
    for e in eps:
        out.append(_ep_line(e))
        if e["s"]:
            out.append(f"      … {' '.join(e['s'].split())}")
    if ags:
        out.append(f"\n{len(ags)} agent runs:")
        for r in ags:
            head = (r["prompt"] or "").strip().splitlines()
            out.append(f"  @{r['id']}  {(r['ts'] or '')[:10]}  {r['project_id']}  "
                       f"{head[0][:110] if head else ''}")
    return "\n".join(out)


def t_timeline(con, a):
    lim = min(int(a.get("limit") or 15), 60)
    q = "SELECT * FROM episodes"
    args = []
    if a.get("project"):
        q += " WHERE id IN (SELECT episode_id FROM episode_projects WHERE project_id LIKE ?)"
        args.append(f"%{a['project']}%")
    q += " ORDER BY started DESC LIMIT ?"
    args.append(lim)
    rows = con.execute(q, args).fetchall()
    if not rows:
        return "No episodes found."
    return "\n".join(["Most recent first:"] + [_ep_line(e) for e in rows])


def t_project(con, a):
    name = a.get("name") or ""
    r = con.execute("SELECT id FROM projects WHERE id LIKE ? OR name LIKE ? LIMIT 1",
                    (f"%{name}%", f"%{name}%")).fetchone()
    pid = r["id"] if r else name
    eps = con.execute("""SELECT DISTINCT e.* FROM episodes e JOIN episode_projects p
                         ON p.episode_id=e.id WHERE p.project_id=? ORDER BY e.started DESC""",
                      (pid,)).fetchall()
    if not eps:
        return f"No project matching {name!r}. Call projects to list them."
    runs = con.execute("SELECT COUNT(*) n FROM agent_runs WHERE project_id=?", (pid,)).fetchone()["n"]
    repos = con.execute("""SELECT root, SUM(n_files) f FROM episode_projects
                           WHERE project_id=? AND source='files' GROUP BY root
                           ORDER BY f DESC""", (pid,)).fetchall()
    days = {e["started"][:10] for e in eps}
    out = [f"{_proj(con, pid)} — {len(eps)} episodes over {len(days)} active days, "
           f"{eps[-1]['started'][:10]} to {eps[0]['ended'][:10]}",
           f"{sum(e['n_prompts'] for e in eps)} prompts · "
           f"{_n(sum(e['out_tokens'] for e in eps))} output tokens · {runs} agent runs · "
           f"{sum(e['compactions'] for e in eps)} compactions"]
    if repos:
        out.append("\nRepos this project lives in:")
        out += [f"  {r['root']} — {r['f']} files" for r in repos]
    out.append("\nEpisodes, most recent first:")
    out += [_ep_line(e) for e in eps[:25]]
    if len(eps) > 25:
        out.append(f"  … {len(eps)-25} older episodes; use timeline with a project filter.")
    return "\n".join(out)


def t_episode(con, a):
    e = con.execute("SELECT * FROM episodes WHERE id=?", (int(a.get("id", 0)),)).fetchone()
    if not e:
        return "No episode with that id."
    msgs = con.execute("SELECT ts,text FROM messages WHERE episode_id=? AND kind='prompt' "
                       "ORDER BY ts", (e["id"],)).fetchall()
    runs = con.execute("SELECT id,prompt,n_tools FROM agent_runs WHERE episode_id=? ORDER BY ts",
                       (e["id"],)).fetchall()
    touched = con.execute("SELECT root,n_files FROM episode_projects WHERE episode_id=? "
                          "AND source='files' ORDER BY n_files DESC", (e["id"],)).fetchall()
    files = json.loads(e["files_touched"] or "[]")
    out = [f"#{e['id']} — {e['title']}",
           f"{_proj(con, e['project_id'])} · {e['started'][:16].replace('T',' ')} to "
           f"{e['ended'][11:16]} · {_dur(e['duration_s'])} · branch {e['branch'] or '—'}",
           f"{e['n_prompts']} prompts · {e['n_tools']} tool calls · "
           f"{_n(e['out_tokens'])} output tokens · {e['compactions']} compactions"]
    if touched:
        out.append("\nWork landed in:")
        out += [f"  {t['root']} — {t['n_files']} file{'s' if t['n_files']!=1 else ''}"
                for t in touched]
    if msgs:
        out.append(f"\nWhat the user asked, verbatim ({len(msgs)}):")
        out += [f"  {m['ts'][11:16]}  {' '.join((m['text'] or '').split())[:400]}" for m in msgs]
    if runs:
        out.append(f"\nAgents spawned ({len(runs)}) — call agent_run for the full conclusion:")
        for r in runs:
            head = (r["prompt"] or "").strip().splitlines()
            out.append(f"  @{r['id']}  {head[0][:120] if head else ''}")
    if files:
        out.append(f"\nFiles touched ({len(files)}):")
        out += [f"  {f}" for f in files[:60]]
    return "\n".join(out)


def t_agent_run(con, a):
    r = con.execute("SELECT * FROM agent_runs WHERE id=?", (int(a.get("id", 0)),)).fetchone()
    if not r:
        return "No agent run with that id."
    return "\n".join([
        f"Agent run @{r['id']} · {_proj(con, r['project_id'])} · "
        f"{(r['ts'] or '')[:16].replace('T',' ')} · {r['n_tools']} tool calls · "
        f"{_n(r['out_tokens'])} output tokens"
        + (f" · from episode #{r['episode_id']}" if r["episode_id"] else ""),
        "\n--- TASK IT WAS GIVEN ---", r["prompt"] or "(none recorded)",
        "\n--- WHAT IT CONCLUDED ---", r["result"] or "(no result recorded)"])


def t_brief(con, a):
    from . import brief
    pid = a.get("project")
    if not pid and a.get("cwd"):
        pid = brief.project_for_cwd(con, a["cwd"])
    if not pid:
        return "Give a project name, or a cwd to resolve one from."
    r = con.execute("SELECT id FROM projects WHERE id LIKE ? OR name LIKE ? LIMIT 1",
                    (f"%{pid}%", f"%{pid}%")).fetchone()
    text = brief.build(con, r["id"] if r else pid)
    return text or "Nothing indexed for that project yet."


def t_report(con, a):
    from . import report
    pid = a.get("project")
    if pid:
        r = con.execute("SELECT id FROM projects WHERE id LIKE ? OR name LIKE ? LIMIT 1",
                        (f"%{pid}%", f"%{pid}%")).fetchone()
        pid = r["id"] if r else pid
    text = report.build(con, pid, a.get("since") or "7d", a.get("until"))
    return text or "No work in that window."


def t_recall(con, a):
    from . import recall
    hits = recall.find(con, a.get("text") or "")
    if not hits:
        return "No closely matching past work. This looks new."
    out = ["This closely matches work already done:"]
    for h in hits:
        out.append(f"  #{h['id']}  {h['date']}  {h['project']}  {h['title']}  "
                   f"(shares {h['coverage']:.0%} of the distinctive terms)")
    out.append("Call episode with one of these ids before starting the work again.")
    return "\n".join(out)


TOOLS = [
    ("projects", "List every project Chronicle knows about, most recently worked first. "
                 "Start here when you do not know what a project is called.",
     {"type": "object", "properties": {}}, t_projects),
    ("search", "Full-text search across every past session and agent run on this machine. "
               "The fastest way to answer 'have we done this before' or 'when did we work on X'. "
               "Supports SQLite FTS5 syntax: bare words are AND-ed, plus OR, NOT, \"phrases\", prefix*.",
     {"type": "object", "properties": {
         "query": {"type": "string", "description": "Words to search for."},
         "project": {"type": "string", "description": "Optional: restrict to one project."},
         "limit": {"type": "integer", "description": "Max episodes (default 10, max 30)."}},
      "required": ["query"]}, t_search),
    ("timeline", "Recent work in order, optionally for one project. Use for "
                 "'what have I been doing lately'.",
     {"type": "object", "properties": {
         "project": {"type": "string"},
         "limit": {"type": "integer", "description": "Default 15, max 60."}}}, t_timeline),
    ("project", "Everything about one project: totals, every repo it spans, and its episodes. "
                "A project is logical, so this covers all the directories it has lived in.",
     {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}, t_project),
    ("episode", "One episode in full: what the user asked verbatim, files touched, where the "
                "work landed, and which agents ran. Use after search or timeline gives you an id.",
     {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}, t_episode),
    ("agent_run", "The task a subagent was given and the full conclusion it reached. These "
                  "results exist nowhere else — they are not in any transcript the user can read.",
     {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]}, t_agent_run),
    ("brief", "A short catch-up on a project: last session, how it ended, open tasks, files in "
              "flight. Use when picking work back up.",
     {"type": "object", "properties": {
         "project": {"type": "string"},
         "cwd": {"type": "string", "description": "Resolve the project from this directory instead."}}},
     t_brief),
    ("report", "A work log for a period, day by day, suitable for turning into a client update "
               "or standup. Returns facts only — write the prose yourself and invent nothing.",
     {"type": "object", "properties": {
         "project": {"type": "string"},
         "since": {"type": "string", "description": "7d, 2w, or YYYY-MM-DD. Default 7d."},
         "until": {"type": "string"}}}, t_report),
    ("recall", "Check whether a task has already been done before starting it. Deliberately "
               "strict: it answers 'this looks new' unless the match is strong.",
     {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}, t_recall),
]
BY_NAME = {t[0]: t for t in TOOLS}


# ---------------------------------------------------------------- transport

def _send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def _err(rid, code, message):
    _send({"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}})


def handle(req):
    m, rid, params = req.get("method"), req.get("id"), req.get("params") or {}

    if m == "initialize":
        want = params.get("protocolVersion")
        _send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": want if want in SUPPORTED else PROTOCOL,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "chronicle", "title": "Chronicle", "version": VERSION},
            "instructions": INSTRUCTIONS}})
    elif m in ("notifications/initialized", "notifications/cancelled"):
        pass
    elif m == "ping":
        _send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif m == "tools/list":
        _send({"jsonrpc": "2.0", "id": rid, "result": {"tools": [
            {"name": n, "description": d, "inputSchema": s} for n, d, s, _ in TOOLS]}})
    elif m == "tools/call":
        name = params.get("name")
        if name not in BY_NAME:
            _err(rid, -32602, f"Unknown tool: {name}")
            return
        try:
            con = _db()
            text = BY_NAME[name][3](con, params.get("arguments") or {})
            con.close()
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": text}], "isError": False}})
        except Exception as e:
            print(traceback.format_exc(), file=sys.stderr)
            _send({"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"Chronicle error: {type(e).__name__}: {e}"}],
                "isError": True}})
    elif rid is not None:
        _err(rid, -32601, f"Method not found: {m}")


def serve():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        try:
            handle(req)
        except Exception:
            print(traceback.format_exc(), file=sys.stderr)


if __name__ == "__main__":
    serve()
