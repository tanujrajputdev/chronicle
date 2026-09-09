import sys, os, json, argparse, datetime, textwrap, shutil
from . import db, indexer, ingest, digest as digestmod
from .config import DB_PATH, SOURCE, ALIASES_PATH, GAP_SECONDS

W = min(shutil.get_terminal_size((100, 24)).columns, 110)
B, D, R, Y, C, G = "\033[1m", "\033[2m", "\033[0m", "\033[33m", "\033[36m", "\033[32m"


def _n(x):
    x = x or 0
    if x >= 1_000_000_000: return f"{x/1e9:.1f}B"
    if x >= 1_000_000: return f"{x/1e6:.1f}M"
    if x >= 1_000:     return f"{x/1e3:.1f}k"
    return str(x)


def _dur(s):
    s = s or 0
    h, m = divmod(int(s) // 60, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m"


def _day(ts):
    return (ts or "")[:10]


def _rule(t=""):
    print(f"{D}{'─'*W}{R}" if not t else f"\n{B}{t}{R}\n{D}{'─'*W}{R}")


# ---------------------------------------------------------------- commands

def cmd_index(con, a):
    def prog(i, n, name):
        if not sys.stderr.isatty():
            return                       # keep launchd/cron logs clean
        pct = int(i / n * 30)
        sys.stderr.write(f"\r  [{'█'*pct}{'·'*(30-pct)}] {i}/{n} {name[:28]:28}")
        sys.stderr.flush()
    t0 = datetime.datetime.now()
    if sys.stdout.isatty():
        print(f"{B}Indexing{R} {SOURCE}")
    s = indexer.build(con, rebuild=a.rebuild, progress=prog)
    if sys.stderr.isatty():
        sys.stderr.write("\r" + " " * (W - 1) + "\r")
    el = (datetime.datetime.now() - t0).total_seconds()
    print(f"  {G}✓{R} {s['sessions']} sessions → {B}{s['episodes']} episodes{R}, "
          f"{s['messages']} prompts, {s['agents']} agent runs")
    print(f"  {D}{_n(s['bytes'])}B read · {s['skipped']} unchanged · "
          f"{s['redactions']} secrets redacted · {el:.1f}s{R}")
    print(f"  {D}db: {DB_PATH}{R}")


def cmd_stats(con, a):
    r = con.execute("SELECT COUNT(*) n,SUM(out_tokens) o,SUM(cache_read) c,"
                    "MIN(started) a,MAX(ended) b,SUM(n_prompts) p,SUM(compactions) k,"
                    "SUM(duration_s) d FROM episodes").fetchone()
    ag = con.execute("SELECT COUNT(*) n,SUM(out_tokens) o FROM agent_runs").fetchone()
    pj = con.execute("SELECT COUNT(DISTINCT project_id) n FROM episodes").fetchone()
    days = con.execute("SELECT COUNT(DISTINCT substr(started,1,10)) n FROM episodes").fetchone()
    last = con.execute("SELECT v FROM meta WHERE k='last_index'").fetchone()
    _rule("Chronicle")
    print(f"  {B}{r['n']}{R} episodes across {B}{pj['n']}{R} projects · "
          f"{days['n']} active days · {_day(r['a'])} → {_day(r['b'])}")
    print(f"  {r['p']} prompts · {_dur(r['d'])} elapsed · {r['k']} compactions survived")
    print(f"  {ag['n']} agent runs ({_n(ag['o'])} output tokens)")
    print(f"  {_n(r['o'])} output tokens · {_n(r['c'])} cache reads")
    print(f"  {D}last indexed {last['v'] if last else 'never'} · gap threshold {GAP_SECONDS//3600}h{R}")


def cmd_projects(con, a):
    rows = con.execute("""
      SELECT e.project_id p, COUNT(*) n, SUM(e.n_prompts) pr, SUM(e.out_tokens) o,
             SUM(e.n_agents) ag, MIN(e.started) a, MAX(e.ended) b,
             COUNT(DISTINCT substr(e.started,1,10)) d
      FROM episodes e GROUP BY e.project_id ORDER BY b DESC""").fetchall()
    _rule("Projects")
    print(f"  {D}{'project':<20}{'eps':>5}{'prompts':>9}{'days':>6}{'agents':>8}{'tokens':>9}  last{R}")
    for r in rows:
        lbl = con.execute("SELECT name FROM projects WHERE id=?", (r["p"],)).fetchone()
        name = lbl["name"] if lbl else r["p"]
        print(f"  {B}{name[:19]:<20}{R}{r['n']:>5}{r['pr']:>9}{r['d']:>6}{r['ag']:>8}"
              f"{_n(r['o']):>9}  {D}{_day(r['b'])}{R}")


def _ep_line(r, show_proj=False):
    p = f"{r['project_id'][:12]:<13}" if show_proj else ""
    ag = f" {C}⛭{r['n_agents']}{R}" if r["n_agents"] else ""
    ck = f" {Y}⚑{r['compactions']}{R}" if r["compactions"] else ""
    nf = len(json.loads(r["files_touched"] or "[]"))
    fs = f" {D}{nf}f{R}" if nf else ""
    print(f"  {D}#{r['id']:<5}{_day(r['started'])}{R} {p}{r['title'][:W-22]}")
    print(f"        {D}{_dur(r['duration_s'])} · {r['n_prompts']}p · {_n(r['out_tokens'])}tok{R}{fs}{ag}{ck}")


def cmd_timeline(con, a):
    q = "SELECT * FROM episodes"
    args = []
    if a.project:
        q += (" WHERE id IN (SELECT episode_id FROM episode_projects "
              "WHERE project_id LIKE ?)")
        args.append(f"%{a.project}%")
    q += " ORDER BY started DESC LIMIT ?"
    args.append(a.limit)
    rows = con.execute(q, args).fetchall()
    _rule(f"Timeline{' · ' + a.project if a.project else ''}")
    if not rows:
        print(f"  {D}nothing found{R}"); return
    for r in rows:
        _ep_line(r, show_proj=not a.project)


def cmd_search(con, a):
    query = " ".join(a.query)
    _rule(f"Search · {query}")
    sql = """SELECT e.*, bm25(episodes_fts) rank,
                    snippet(episodes_fts,2,'«','»','…',14) snip
             FROM episodes_fts JOIN episodes e ON e.id=episodes_fts.rowid
             WHERE episodes_fts MATCH ?"""
    args = [query]
    if a.project:
        sql += (" AND e.id IN (SELECT episode_id FROM episode_projects "
                "WHERE project_id LIKE ?)"); args.append(f"%{a.project}%")
    sql += " ORDER BY rank LIMIT ?"; args.append(a.limit)
    try:
        rows = con.execute(sql, args).fetchall()
    except Exception as e:
        print(f"  {Y}bad query:{R} {e}"); return
    if not rows:
        print(f"  {D}no episodes matched{R}")
    for r in rows:
        _ep_line(r, show_proj=True)
        sn = " ".join((r["snip"] or "").split())
        if sn:
            print(f"        {D}{sn[:W-10]}{R}")

    ar = con.execute("""SELECT a.*, snippet(agents_fts,1,'«','»','…',12) snip
                        FROM agents_fts JOIN agent_runs a ON a.id=agents_fts.rowid
                        WHERE agents_fts MATCH ? ORDER BY bm25(agents_fts) LIMIT 5""",
                     (query,)).fetchall()
    if ar:
        print(f"\n  {B}agent runs{R}")
        for r in ar:
            print(f"  {D}@{r['id']:<5}{_day(r['ts'])}{R} {r['project_id'][:12]:<13}"
                  f"{(r['prompt'] or '')[:W-42].splitlines()[0] if r['prompt'] else ''}")


def cmd_show(con, a):
    r = con.execute("SELECT * FROM episodes WHERE id=?", (a.id,)).fetchone()
    if not r:
        print("no such episode"); return
    _rule(f"#{r['id']} · {r['title']}")
    print(f"  {D}project{R} {r['project_id']}   {D}branch{R} {r['branch'] or '-'}")
    print(f"  {D}when{R}    {r['started'][:16].replace('T',' ')} → {r['ended'][:16].replace('T',' ')}  ({_dur(r['duration_s'])})")
    print(f"  {D}volume{R}  {r['n_prompts']} prompts · {r['n_tools']} tool calls · "
          f"{_n(r['out_tokens'])} output tokens · {r['compactions']} compactions")
    print(f"  {D}session{R} {r['session_id']}")

    touched = con.execute(
        "SELECT project_id,root,n_files FROM episode_projects WHERE episode_id=? AND source='files' "
        "ORDER BY n_files DESC", (r["id"],)).fetchall()
    if touched:
        print(f"\n  {B}work landed in{R}")
        for t in touched:
            flag = "" if t["project_id"] == r["project_id"] else f"  {Y}← different project{R}"
            print(f"    {t['project_id']:<14}{D}{t['root']}{R}  {t['n_files']}f{flag}")

    files = json.loads(r["files_touched"] or "[]")
    if files:
        print(f"\n  {B}files touched{R} ({len(files)})")
        for f in files[:20]:
            print(f"    {D}{f}{R}")
        if len(files) > 20:
            print(f"    {D}… +{len(files)-20} more{R}")

    tools = json.loads(r["tools"] or "{}")
    if tools:
        print(f"\n  {B}tools{R}  " + "  ".join(f"{k}×{v}" for k, v in list(tools.items())[:12]))

    runs = con.execute("SELECT * FROM agent_runs WHERE episode_id=? ORDER BY ts", (r["id"],)).fetchall()
    if runs:
        print(f"\n  {B}agent runs{R} ({len(runs)})")
        for x in runs:
            head = (x["prompt"] or "").strip().splitlines()
            print(f"    {C}@{x['id']}{R} {D}{x['agent_type']}{R} {head[0][:W-24] if head else ''}")

    msgs = con.execute("SELECT * FROM messages WHERE episode_id=? AND kind='prompt' "
                       "ORDER BY ts", (r["id"],)).fetchall()
    print(f"\n  {B}your prompts{R} ({len(msgs)})")
    for m in msgs[: a.prompts]:
        t = " ".join((m["text"] or "").split())
        print(f"    {D}{m['ts'][11:16]}{R} {textwrap.shorten(t, W-14, placeholder='…')}")
    if len(msgs) > a.prompts:
        print(f"    {D}… +{len(msgs)-a.prompts} more (--prompts N){R}")


def cmd_agents(con, a):
    q = "SELECT * FROM agent_runs"
    args = []
    if a.project:
        q += " WHERE project_id LIKE ?"; args.append(f"%{a.project}%")
    q += " ORDER BY ts DESC LIMIT ?"; args.append(a.limit)
    rows = con.execute(q, args).fetchall()
    _rule("Agent runs")
    for r in rows:
        head = (r["prompt"] or "").strip().splitlines()
        wf = f" {D}[{r['workflow_id']}]{R}" if r["workflow_id"] else ""
        print(f"  {C}@{r['id']:<5}{R}{D}{_day(r['ts'])}{R} {r['project_id'][:12]:<13}"
              f"{r['agent_type']:<9}{(head[0][:W-46] if head else '')}{wf}")


def cmd_agent(con, a):
    r = con.execute("SELECT * FROM agent_runs WHERE id=?", (a.id,)).fetchone()
    if not r:
        print("no such agent run"); return
    _rule(f"@{r['id']} · {r['agent_type']} · {r['project_id']}")
    print(f"  {D}when{R} {(r['ts'] or '')[:16].replace('T',' ')} · {r['n_tools']} tool calls · "
          f"{_n(r['out_tokens'])} output tokens")
    if r["episode_id"]:
        print(f"  {D}episode{R} #{r['episode_id']}")
    print(f"\n  {B}task given{R}")
    for l in textwrap.wrap(" ".join((r["prompt"] or "").split()), W - 6)[:14]:
        print("    " + l)
    print(f"\n  {B}what it concluded{R}")
    for l in textwrap.wrap(" ".join((r["result"] or "").split()), W - 6)[:40]:
        print("    " + l)


def cmd_digest(con, a):
    paths = digestmod.write_all(con, project=a.project)
    _rule("Digests")
    for p in paths:
        print(f"  {G}✓{R} {p}")


SPARK = "▁▂▃▄▅▆▇█"


def _spark(vals):
    if not vals or max(vals) == 0:
        return ""
    hi = max(vals)
    return "".join(SPARK[min(7, int(v / hi * 7.99))] if v else "·" for v in vals)


def cmd_project(con, a):
    """Everything about one logical project, across every repo it lives in."""
    key = f"%{a.name}%"
    row = con.execute("SELECT id,name FROM projects WHERE id LIKE ? OR name LIKE ? LIMIT 1",
                      (key, key)).fetchone()
    pid = row["id"] if row else a.name
    name = row["name"] if row else a.name

    eps = con.execute("""SELECT DISTINCT e.* FROM episodes e
                         JOIN episode_projects p ON p.episode_id = e.id
                         WHERE p.project_id = ? ORDER BY e.started""", (pid,)).fetchall()
    if not eps:
        print(f"  {D}no project matching {a.name!r}. try: ./chronicle-cli projects{R}")
        return

    ids = [e["id"] for e in eps]
    qs = ",".join("?" * len(ids))
    tot_out = sum(e["out_tokens"] for e in eps)
    tot_cr = sum(e["cache_read"] for e in eps)
    tot_p = sum(e["n_prompts"] for e in eps)
    tot_d = sum(e["duration_s"] for e in eps)
    tot_k = sum(e["compactions"] for e in eps)
    days = sorted({e["started"][:10] for e in eps})
    sess = len({e["session_id"] for e in eps})

    files, tools = set(), {}
    for e in eps:
        files |= set(json.loads(e["files_touched"] or "[]"))
        for k, v in json.loads(e["tools"] or "{}").items():
            tools[k] = tools.get(k, 0) + v

    runs = con.execute(f"SELECT * FROM agent_runs WHERE episode_id IN ({qs}) "
                       f"OR project_id=?", (*ids, pid)).fetchall()
    ag_out = sum(r["out_tokens"] or 0 for r in runs)

    _rule(name)
    print(f"  {B}{len(eps)}{R} episodes in {sess} sessions · {tot_p} prompts · "
          f"{len(days)} active days · {days[0]} → {days[-1]}")
    print(f"  {B}{_n(tot_out)}{R} output tokens{D} (+{_n(ag_out)} from agents){R} · "
          f"{_n(tot_cr)} cache reads · {_dur(tot_d)} elapsed")
    repos = con.execute("""SELECT root, COUNT(DISTINCT episode_id) eps, SUM(n_files) f
                           FROM episode_projects WHERE project_id=? AND source='files'
                           GROUP BY root ORDER BY f DESC""", (pid,)).fetchall()
    print(f"  {len(runs)} agent runs · {tot_k} compactions · "
          f"{B}{len(files)}{R} files across {B}{len(repos)}{R} repos")

    if repos:
        print(f"\n  {B}repos{R}")
        for r in repos:
            print(f"    {D}{r['root']:<52}{R}{r['eps']:>4} eps{r['f']:>6} files")

    # activity by week
    import collections as _c
    wk = _c.Counter()
    for e in eps:
        d = datetime.date.fromisoformat(e["started"][:10])
        wk[d - datetime.timedelta(days=d.weekday())] += e["out_tokens"]
    weeks = sorted(wk)
    if len(weeks) > 1:
        span = [(weeks[0] + datetime.timedelta(days=7 * i)) for i in
                range(((weeks[-1] - weeks[0]).days // 7) + 1)]
        print(f"\n  {B}output tokens by week{R}  {C}{_spark([wk.get(w, 0) for w in span])}{R}"
              f"  {D}{weeks[0]} → {weeks[-1]}{R}")

    print(f"\n  {B}biggest episodes{R}")
    for e in sorted(eps, key=lambda x: -x["out_tokens"])[: a.top]:
        print(f"    {D}#{e['id']:<5}{e['started'][:10]}{R} {_n(e['out_tokens']):>6}tok "
              f"{_dur(e['duration_s']):>6} {e['n_prompts']:>3}p  {e['title'][:W-46]}")

    if tools:
        top = sorted(tools.items(), key=lambda kv: -kv[1])[:10]
        print(f"\n  {B}tools{R}  " + "  ".join(f"{k}×{v}" for k, v in top))

    if runs:
        print(f"\n  {B}agent runs{R} ({len(runs)}, newest first)")
        for r in sorted(runs, key=lambda x: x["ts"] or "", reverse=True)[: a.top]:
            head = (r["prompt"] or "").strip().splitlines()
            print(f"    {C}@{r['id']:<5}{R}{D}{_day(r['ts'])}{R} "
                  f"{(head[0][:W-30] if head else '')}")

    print(f"\n  {D}full timeline: ./chronicle-cli timeline {pid}{R}")


def cmd_map(con, a):
    """Where work launched in one folder actually landed."""
    rows = con.execute("""
      SELECT e.project_id AS launched, p.project_id AS landed, p.root,
             COUNT(DISTINCT e.id) eps, SUM(p.n_files) files, MAX(e.ended) last
      FROM episode_projects p JOIN episodes e ON e.id = p.episode_id
      WHERE p.source='files'
      GROUP BY e.project_id, p.project_id, p.root
      ORDER BY files DESC""").fetchall()
    _rule("Where the work actually landed")
    print(f"  {D}{'launched in':<15}{'edited under':<48}{'eps':>4}{'files':>7}   last{R}")
    cross = 0
    for r in rows:
        if a.cross and r["launched"] == r["landed"]:
            continue
        mark = ""
        if r["launched"] != r["landed"]:
            mark = f"  {Y}←{R}"
            cross += 1
        root = r["root"] if len(r["root"]) < 47 else "…" + r["root"][-45:]
        name = r["launched"] if r["launched"] == r["landed"] else f"{r['launched']}→{r['landed']}"
        print(f"  {B}{name[:14]:<15}{R}{D}{root:<48}{R}{r['eps']:>4}{r['files']:>7}"
              f"   {D}{_day(r['last'])}{R}{mark}")
    if cross:
        print(f"\n  {Y}{cross} path{'s' if cross != 1 else ''} where the session folder and the work "
              f"belong to different projects.{R}")
        print(f"  {D}Fix by adding the path to aliases.json, then: ./chronicle-cli index --rebuild{R}")


def cmd_why(con, a):
    from . import recall
    text = " ".join(a.text)
    hits, trace = recall.find(con, text, explain=True)
    _rule("Would recall fire?")
    for k, v in trace:
        mark = G + "✓" + R if v.startswith("MATCH") else (D + "·" + R)
        print(f"  {mark} {k:<22} {v}")
    print()
    if hits:
        print(f"  {G}fires{R} — this is what would be injected:\n")
        print(recall.render(hits))
    else:
        print(f"  {D}stays silent{R}")
    print(f"\n  {D}gates: >={recall.MIN_TERMS} distinctive words · >={recall.MIN_OVERLAP} shared · "
          f">={recall.MIN_COVERAGE:.0%} coverage · >={recall.MIN_STRONG} rare · "
          f"score >={recall.MIN_SCORE}{R}")


def cmd_report(con, a):
    from . import report as rp
    text = rp.build(con, a.project, a.since, a.until)
    if not text:
        print(f"  {D}no work in that window{R}"); return
    if a.out:
        open(a.out, "w").write(text)
        print(f"  {G}✓{R} {a.out}  ({len(text)} chars)")
    else:
        print(text)


def cmd_brief(con, a):
    from . import brief
    pid = a.project or brief.project_for_cwd(con, os.getcwd())
    if not pid:
        print(f"  {D}no project matches this directory{R}"); return
    text = brief.build(con, pid)
    _rule(f"Session brief · {pid}")
    if not text:
        print(f"  {D}nothing indexed yet{R}"); return
    print(text)
    print(f"\n  {D}{len(text)} characters — this is injected at every session start "
          f"in this project{R}")


def cmd_recall(con, a):
    from . import checkpoint
    cp = checkpoint.latest(a.session)
    if not cp:
        print(f"  {D}no checkpoint saved yet — one is written every time a session compacts{R}")
        return
    _rule(f"Checkpoint · {cp['created']}")
    print(f"  {D}session {cp['session_id']} · trigger {cp['trigger']} · "
          f"{cp['n_records']} records{R}")
    print()
    print(checkpoint.render_markdown(cp))


def cmd_checkpoints(con, a):
    from . import checkpoint
    files = checkpoint.all_checkpoints()
    _rule(f"Checkpoints ({len(files)})")
    for f in files[-a.limit:]:
        print(f"  {D}{f.parent.name[:8]}{R}  {f.stem}  {D}{f}{R}")
    if not files:
        print(f"  {D}none yet{R}")


def cmd_install(con, a):
    from . import install
    install.install(uninstall=a.uninstall, mcp=a.mcp)


def cmd_mcp(con, a):
    from . import mcp
    mcp.serve()


def cmd_doctor(con, a):
    from . import doctor
    rep = doctor.run()
    _rule("Chronicle doctor")
    mark = {doctor.OK: G + "✓" + R, doctor.WARN: Y + "!" + R, doctor.FAIL: "\033[31m✗" + R}
    section = None
    for sec, status, label, detail, fix in rep.rows:
        if sec != section:
            section = sec
            print(f"\n  {B}{sec}{R}")
        print(f"    {mark[status]} {label:<20}{D}{detail}{R}")
        if fix:
            print(f"      {Y}→ {fix}{R}")
    c = rep.counts()
    unindexed = any(l == "contents" and st != doctor.OK for _, st, l, _, _ in rep.rows)
    print()
    if unindexed and not c[doctor.FAIL]:
        print(f"  {Y}nothing indexed yet{R} — the install looks fine, but there is no history "
              f"to answer from.\n  Run: ./chronicle-cli index")
    elif c[doctor.FAIL]:
        print(f"  \033[31m{c[doctor.FAIL]} failing{R}, {c[doctor.WARN]} warning, "
              f"{c[doctor.OK]} passing — fix the ✗ items above")
    elif c[doctor.WARN]:
        print(f"  {G}healthy{R} — {c[doctor.OK]} passing, {c[doctor.WARN]} optional "
              f"item{'s' if c[doctor.WARN] != 1 else ''} not set up")
    else:
        print(f"  {G}everything passing{R} ({c[doctor.OK]} checks)")
    return 1 if c[doctor.FAIL] else 0


def cmd_serve(con, a):
    from . import web
    web.serve(a.port, not a.no_open)


def cmd_aliases(con, a):
    print(f"  {D}{ALIASES_PATH}{R}")
    print(ALIASES_PATH.read_text() if ALIASES_PATH.exists() else "  (none yet)")


# ---------------------------------------------------------------- entry

def main(argv=None):
    p = argparse.ArgumentParser(prog="chronicle", description="The record layer for agentic work.")
    sub = p.add_subparsers(dest="cmd")

    x = sub.add_parser("index", help="ingest new/changed sessions")
    x.add_argument("--rebuild", action="store_true"); x.set_defaults(fn=cmd_index)

    x = sub.add_parser("stats", help="corpus overview"); x.set_defaults(fn=cmd_stats)
    x = sub.add_parser("projects", help="every project, most recent first"); x.set_defaults(fn=cmd_projects)

    x = sub.add_parser("timeline", help="episodes, newest first")
    x.add_argument("project", nargs="?"); x.add_argument("-n", "--limit", type=int, default=25)
    x.set_defaults(fn=cmd_timeline)

    x = sub.add_parser("search", help="full-text search over episodes and agent runs")
    x.add_argument("query", nargs="+"); x.add_argument("-p", "--project")
    x.add_argument("-n", "--limit", type=int, default=12); x.set_defaults(fn=cmd_search)

    x = sub.add_parser("show", help="one episode in full")
    x.add_argument("id", type=int); x.add_argument("--prompts", type=int, default=25)
    x.set_defaults(fn=cmd_show)

    x = sub.add_parser("agents", help="subagent and workflow runs")
    x.add_argument("-p", "--project"); x.add_argument("-n", "--limit", type=int, default=30)
    x.set_defaults(fn=cmd_agents)

    x = sub.add_parser("agent", help="one agent run: task and conclusion")
    x.add_argument("id", type=int); x.set_defaults(fn=cmd_agent)

    x = sub.add_parser("digest", help="regenerate DIGEST.md per project")
    x.add_argument("-p", "--project"); x.set_defaults(fn=cmd_digest)

    x = sub.add_parser("project", help="one project in full, across every repo it lives in")
    x.add_argument("name"); x.add_argument("--top", type=int, default=8)
    x.set_defaults(fn=cmd_project)

    x = sub.add_parser("map", help="where work launched in one folder actually landed")
    x.add_argument("--cross", action="store_true", help="only rows that cross projects")
    x.set_defaults(fn=cmd_map)

    x = sub.add_parser("doctor", help="check that the whole install is working")
    x.set_defaults(fn=cmd_doctor)

    x = sub.add_parser("serve", help="open the local dashboard")
    x.add_argument("-p", "--port", type=int, default=7777)
    x.add_argument("--no-open", action="store_true"); x.set_defaults(fn=cmd_serve)

    x = sub.add_parser("why", help="explain whether recall would fire for some text")
    x.add_argument("text", nargs="+"); x.set_defaults(fn=cmd_why)

    x = sub.add_parser("report", help="a work log you can send to someone")
    x.add_argument("project", nargs="?")
    x.add_argument("--since", default="7d", help="7d, 2w, or a date")
    x.add_argument("--until"); x.add_argument("-o", "--out")
    x.set_defaults(fn=cmd_report)

    x = sub.add_parser("brief", help="what a new session in this project gets told")
    x.add_argument("project", nargs="?"); x.set_defaults(fn=cmd_brief)

    x = sub.add_parser("recall", help="print the most recent pre-compaction checkpoint")
    x.add_argument("session", nargs="?"); x.set_defaults(fn=cmd_recall)

    x = sub.add_parser("checkpoints", help="list saved checkpoints")
    x.add_argument("-n", "--limit", type=int, default=20); x.set_defaults(fn=cmd_checkpoints)

    x = sub.add_parser("install", help="install hooks and the background indexer")
    x.add_argument("--uninstall", action="store_true")
    x.add_argument("--mcp", action="store_true",
                   help="also register the read-only MCP server with Claude Code")
    x.set_defaults(fn=cmd_install)

    x = sub.add_parser("mcp", help="internal: run the MCP server on stdio")
    x.set_defaults(fn=cmd_mcp)

    x = sub.add_parser("hook", help="internal: hook entry point")
    x.add_argument("event"); x.set_defaults(fn=None)

    x = sub.add_parser("aliases", help="show the project alias map"); x.set_defaults(fn=cmd_aliases)

    a = p.parse_args(argv)
    if a.cmd == "hook":
        from . import hooks
        return hooks.run(a.event)
    if not getattr(a, "fn", None):
        p.print_help(); return 0
    con = db.connect()
    a.fn(con, a)
    return 0


if __name__ == "__main__":
    sys.exit(main())
