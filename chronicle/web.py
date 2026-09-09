"""Local dashboard. Reads the index, serves on 127.0.0.1 only, no dependencies."""
import os, json, html, datetime, sqlite3, collections, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from . import db

E = html.escape


def n(x):
    x = x or 0
    if x >= 1_000_000_000: return f"{x/1e9:.1f}B"
    if x >= 1_000_000: return f"{x/1e6:.1f}M"
    if x >= 1_000: return f"{x/1e3:.1f}k"
    return str(x)


def plural(n):
    return "" if n == 1 else "s"


def dur(s):
    h, m = divmod(int(s or 0) // 60, 60)
    return f"{h}h {m:02d}m" if h else f"{m}m"


CSS = """
:root{
 --paper:#F2F3F1;--surf:#fff;--surf2:#EAECE9;--ink:#15181A;--ink2:#3D4448;
 --mut:#6B7378;--faint:#949B9F;--rule:#DCE0DC;--rule2:#C6CCC7;
 --ox:#8C2F39;--oxw:#F6EAEB;--brass:#8E7028;--brassw:#F4EEDE;--teal:#1F6257;--tealw:#E6EFEC;
}
@media(prefers-color-scheme:dark){:root{
 --paper:#131517;--surf:#191C1E;--surf2:#212528;--ink:#E9E7E3;--ink2:#C2C6C7;
 --mut:#8D9599;--faint:#6A7276;--rule:#282D30;--rule2:#3A4145;
 --ox:#C9616B;--oxw:#2B1E20;--brass:#C7A25C;--brassw:#272215;--teal:#57A697;--tealw:#16241F;
}}
*{box-sizing:border-box}
body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.55 ui-sans-serif,-apple-system,"Helvetica Neue",Arial,sans-serif;-webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
a.q:hover{color:var(--ox)}
.ser{font-family:Charter,"Iowan Old Style",Palatino,Georgia,serif}
.mo{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-variant-numeric:tabular-nums}
.num{font-family:ui-monospace,"SF Mono",Menlo,monospace;font-variant-numeric:tabular-nums;text-align:right;white-space:nowrap}
td.mo,td.d.mo{white-space:nowrap}

.shell{display:grid;grid-template-columns:216px 1fr;min-height:100vh}
nav{border-right:1px solid var(--rule);padding:22px 0 40px;position:sticky;top:0;height:100vh;overflow-y:auto;background:var(--surf)}
nav h1{margin:0 20px 18px;font-size:19px;font-weight:600;letter-spacing:-.02em}
nav h1 a{display:block}
nav .grp{color:var(--faint);font-size:12px;margin:20px 20px 7px}
nav ul{list-style:none;margin:0;padding:0}
nav li a{display:flex;justify-content:space-between;gap:8px;padding:5px 20px;font-size:13.5px;color:var(--ink2)}
nav li a:hover{background:var(--surf2);color:var(--ink)}
nav li a.on{color:var(--ox);box-shadow:inset 2px 0 0 var(--ox);background:var(--oxw)}
nav li a span{color:var(--faint);font-size:12px}

main{padding:26px 34px 90px;max-width:1120px}
@media(max-width:860px){.shell{grid-template-columns:1fr}nav{position:static;height:auto;border-right:0;border-bottom:1px solid var(--rule)}main{padding:20px}}

.top{display:flex;align-items:center;gap:18px;flex-wrap:wrap;margin-bottom:26px}
.top form{flex:1;min-width:220px}
.top input{width:100%;padding:8px 12px;border:1px solid var(--rule2);border-radius:2px;background:var(--surf);color:var(--ink);font:14px ui-sans-serif,-apple-system,sans-serif}
.top input:focus{outline:2px solid var(--ox);outline-offset:-1px;border-color:transparent}

h2{font-size:26px;font-weight:500;letter-spacing:-.02em;margin:0 0 4px}
h3{font-size:14px;font-weight:600;color:var(--mut);margin:34px 0 10px}
.sub{color:var(--mut);font-size:13.5px;margin:0 0 22px}

table{width:100%;border-collapse:collapse}
.wrap{overflow-x:auto;border:1px solid var(--rule);border-radius:2px;background:var(--surf)}
th{text-align:left;font-size:12px;font-weight:600;color:var(--faint);padding:9px 14px;border-bottom:1px solid var(--rule2);white-space:nowrap}
th.num{text-align:right}
td{padding:9px 14px;border-bottom:1px solid var(--rule);vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--surf2)}
td.t{font-weight:500}
td.d{color:var(--mut);font-size:13px}

.mk{display:inline-block;font-size:11.5px;padding:1px 6px;border-radius:2px;margin-left:5px;white-space:nowrap}
.mk.ag{background:var(--tealw);color:var(--teal)}
.mk.ck{background:var(--brassw);color:var(--brass)}
.mk.fl{background:var(--surf2);color:var(--mut)}

.stats{display:flex;gap:30px;flex-wrap:wrap;padding:14px 0 20px;border-bottom:1px solid var(--rule);margin-bottom:24px}
.stats div b{display:block;font-size:22px;font-weight:600;letter-spacing:-.02em;font-family:ui-monospace,Menlo,monospace}
.stats div span{color:var(--mut);font-size:12.5px}

.prompts{border-left:2px solid var(--rule2);padding-left:16px;margin:0}
.prompts li{margin:0 0 11px;list-style:none}
.prompts time{color:var(--faint);font-size:12px;margin-right:8px;font-family:ui-monospace,Menlo,monospace}
.files{columns:2;column-gap:26px;font-size:12.5px;color:var(--mut);margin:0;padding:0;list-style:none}
.files li{break-inside:avoid;margin-bottom:3px;font-family:ui-monospace,Menlo,monospace;overflow-wrap:anywhere}
@media(max-width:700px){.files{columns:1}}
.body{background:var(--surf);border:1px solid var(--rule);border-radius:2px;padding:18px 20px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:14px;line-height:1.6}
.snip{color:var(--mut);font-size:13px;margin-top:3px}
.snip b{color:var(--ink);background:var(--oxw);font-weight:600;padding:0 2px}
.empty{color:var(--mut);padding:30px 0}
:focus-visible{outline:2px solid var(--ox);outline-offset:2px}
"""


def bars(vals, w=132, h=22, color="var(--ox)"):
    if not vals or max(vals) == 0:
        return ""
    import math
    hi, k = math.sqrt(max(vals)), len(vals)
    vals = [math.sqrt(v) for v in vals]
    bw = w / k
    out = [f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true">']
    for i, v in enumerate(vals):
        bh = max(2.0, (v / hi) * h) if v else 1.0
        out.append(f'<rect x="{i*bw:.1f}" y="{h-bh:.1f}" width="{max(1.0,bw-1):.1f}" '
                   f'height="{bh:.1f}" fill="{color}" opacity="{0.9 if v else 0.22}"/>')
    return "".join(out) + "</svg>"


def weekly(con, pid=None, weeks=18):
    q = "SELECT started,out_tokens FROM episodes"
    args = []
    if pid:
        q += " WHERE id IN (SELECT episode_id FROM episode_projects WHERE project_id=?)"
        args.append(pid)
    rows = con.execute(q, args).fetchall()
    if not rows:
        return []
    today = datetime.date.today()
    start = today - datetime.timedelta(days=today.weekday() + 7 * (weeks - 1))
    buckets = [0] * weeks
    for r in rows:
        d = datetime.date.fromisoformat(r["started"][:10])
        i = (d - start).days // 7
        if 0 <= i < weeks:
            buckets[i] += r["out_tokens"] or 0
    return buckets


def markers(e):
    s = ""
    if e["n_agents"]:
        s += f'<span class="mk ag">{e["n_agents"]} agents</span>'
    if e["compactions"]:
        s += f'<span class="mk ck">{e["compactions"]} compactions</span>'
    nf = len(json.loads(e["files_touched"] or "[]"))
    if nf:
        s += f'<span class="mk fl">{nf} files</span>'
    return s


def shell(con, title, body, active=None):
    ps = con.execute("""SELECT p.project_id id, COUNT(DISTINCT p.episode_id) c,
                        MAX(e.ended) last, (SELECT name FROM projects WHERE id=p.project_id) nm
                        FROM episode_projects p JOIN episodes e ON e.id=p.episode_id
                        GROUP BY p.project_id ORDER BY last DESC""").fetchall()
    li = "".join(
        f'<li><a class="q {"on" if r["id"]==active else ""}" href="/p/{E(r["id"])}">'
        f'{E(r["nm"] or r["id"])}<span>{r["c"]}</span></a></li>' for r in ps)
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{E(title)} — Chronicle</title><style>{CSS}</style></head><body><div class="shell">
<nav><h1><a href="/">Chronicle</a></h1>
<div class="grp">Projects</div><ul>{li}</ul>
<div class="grp">Also</div><ul>
<li><a class="q" href="/agents">Agent runs</a></li>
<li><a class="q" href="/checkpoints">Checkpoints</a></li></ul></nav>
<main><div class="top"><form action="/search"><input name="q" placeholder="Search everything you have ever asked" autocomplete="off"></form></div>
{body}</main></div></body></html>"""


# ---------------------------------------------------------------- pages

def page_home(con):
    s = con.execute("""SELECT COUNT(*) n,SUM(out_tokens) o,SUM(n_prompts) p,
                       COUNT(DISTINCT substr(started,1,10)) d FROM episodes""").fetchone()
    ag = con.execute("SELECT COUNT(*) n FROM agent_runs").fetchone()["n"]
    rows = con.execute("""SELECT p.project_id id,(SELECT name FROM projects WHERE id=p.project_id) nm,
                          COUNT(DISTINCT p.episode_id) eps, MAX(e.ended) last
                          FROM episode_projects p JOIN episodes e ON e.id=p.episode_id
                          GROUP BY p.project_id ORDER BY last DESC""").fetchall()
    body = [f'<h2 class="ser">Everything, since {con.execute("SELECT MIN(started) m FROM episodes").fetchone()["m"][:10]}</h2>',
            '<p class="sub">Each row is a project. The bars are output tokens per week for the last eighteen weeks.</p>',
            f'<div class="stats"><div><b>{s["n"]}</b><span>episodes</span></div>'
            f'<div><b>{s["p"]}</b><span>prompts</span></div>'
            f'<div><b>{s["d"]}</b><span>active days</span></div>'
            f'<div><b>{ag}</b><span>agent runs</span></div>'
            f'<div><b>{n(s["o"])}</b><span>output tokens</span></div></div>',
            '<div class="wrap"><table><thead><tr><th>Project</th><th>Activity</th>'
            '<th class="num">Episodes</th><th class="num">Prompts</th><th class="num">Agents</th>'
            '<th class="num">Tokens</th><th>Last worked</th></tr></thead><tbody>']
    for r in rows:
        d = con.execute("""SELECT COUNT(*) eps,SUM(n_prompts) p,SUM(n_agents) a,SUM(out_tokens) o
                           FROM episodes WHERE id IN
                           (SELECT episode_id FROM episode_projects WHERE project_id=?)""",
                        (r["id"],)).fetchone()
        body.append(f'<tr><td class="t"><a class="q" href="/p/{E(r["id"])}">{E(r["nm"] or r["id"])}</a></td>'
                    f'<td>{bars(weekly(con, r["id"]))}</td>'
                    f'<td class="num">{d["eps"]}</td><td class="num">{d["p"]}</td>'
                    f'<td class="num">{d["a"] or 0}</td><td class="num">{n(d["o"])}</td>'
                    f'<td class="d">{r["last"][:10]}</td></tr>')
    body.append("</tbody></table></div><h3>Most recent work</h3>")
    body.append(recent_table(con, con.execute(
        "SELECT * FROM episodes ORDER BY started DESC LIMIT 12").fetchall(), show_proj=True))
    return shell(con, "Overview", "".join(body))


def recent_table(con, eps, show_proj=False):
    out = ['<div class="wrap"><table><thead><tr><th>Date</th>']
    if show_proj:
        out.append("<th>Project</th>")
    out.append('<th>What happened</th><th class="num">Prompts</th><th class="num">Time</th>'
               '<th class="num">Tokens</th></tr></thead><tbody>')
    for e in eps:
        p = f'<td class="d">{E(e["project_id"])}</td>' if show_proj else ""
        out.append(f'<tr><td class="d mo">{e["started"][:10]}</td>{p}'
                   f'<td class="t"><a class="q" href="/e/{e["id"]}">{E(e["title"])}</a>{markers(e)}</td>'
                   f'<td class="num">{e["n_prompts"]}</td><td class="num">{dur(e["duration_s"])}</td>'
                   f'<td class="num">{n(e["out_tokens"])}</td></tr>')
    return "".join(out) + "</tbody></table></div>"


def page_project(con, pid):
    row = con.execute("SELECT name FROM projects WHERE id=?", (pid,)).fetchone()
    name = row["name"] if row else pid
    eps = con.execute("""SELECT * FROM episodes WHERE id IN
                         (SELECT episode_id FROM episode_projects WHERE project_id=?)
                         ORDER BY started DESC""", (pid,)).fetchall()
    if not eps:
        return shell(con, name, '<p class="empty">Nothing indexed for this project yet.</p>')
    tot = lambda k: sum(e[k] or 0 for e in eps)
    days = {e["started"][:10] for e in eps}
    repos = con.execute("""SELECT root,COUNT(DISTINCT episode_id) eps,SUM(n_files) f
                           FROM episode_projects WHERE project_id=? AND source='files'
                           GROUP BY root ORDER BY f DESC""", (pid,)).fetchall()
    runs = con.execute("SELECT * FROM agent_runs WHERE project_id=? ORDER BY ts DESC", (pid,)).fetchall()

    b = [f'<h2 class="ser">{E(name)}</h2>',
         f'<p class="sub">{len(eps)} episodes over {len(days)} active days, '
         f'{eps[-1]["started"][:10]} to {eps[0]["ended"][:10]}.</p>',
         f'<div class="stats"><div><b>{n(tot("out_tokens"))}</b><span>output tokens</span></div>'
         f'<div><b>{tot("n_prompts")}</b><span>prompts</span></div>'
         f'<div><b>{dur(tot("duration_s"))}</b><span>elapsed</span></div>'
         f'<div><b>{len(runs)}</b><span>agent runs</span></div>'
         f'<div><b>{tot("compactions")}</b><span>compactions</span></div></div>',
         f'<div>{bars(weekly(con, pid, 26), w=560, h=44)}</div>']
    if repos:
        b.append("<h3>Repos this project actually lives in</h3><div class=\"wrap\"><table><tbody>")
        for r in repos:
            b.append(f'<tr><td class="mo d">{E(r["root"])}</td>'
                     f'<td class="num">{r["eps"]} episode{"s" if r["eps"]!=1 else ""}</td>'
                     f'<td class="num">{r["f"]} file{plural(r["f"])}</td></tr>')
        b.append("</tbody></table></div>")
    b.append("<h3>Every episode</h3>")
    b.append(recent_table(con, eps))
    if runs:
        b.append("<h3>Agent runs</h3><div class=\"wrap\"><table><tbody>")
        for r in runs[:40]:
            head = (r["prompt"] or "").strip().splitlines()
            b.append(f'<tr><td class="d mo">{(r["ts"] or "")[:10]}</td>'
                     f'<td><a class="q" href="/a/{r["id"]}">{E(head[0][:110] if head else "(no prompt)")}</a></td>'
                     f'<td class="num d">{r["n_tools"]} tools</td></tr>')
        b.append("</tbody></table></div>")
    return shell(con, name, "".join(b), active=pid)


def page_episode(con, eid):
    e = con.execute("SELECT * FROM episodes WHERE id=?", (eid,)).fetchone()
    if not e:
        return shell(con, "Not found", '<p class="empty">No episode with that id.</p>')
    msgs = con.execute("SELECT * FROM messages WHERE episode_id=? AND kind='prompt' ORDER BY ts",
                       (eid,)).fetchall()
    runs = con.execute("SELECT * FROM agent_runs WHERE episode_id=? ORDER BY ts", (eid,)).fetchall()
    touched = con.execute("SELECT project_id,root,n_files FROM episode_projects WHERE episode_id=? "
                          "AND source='files' ORDER BY n_files DESC", (eid,)).fetchall()
    files = json.loads(e["files_touched"] or "[]")
    tools = json.loads(e["tools"] or "{}")
    pn = con.execute("SELECT name FROM projects WHERE id=?", (e["project_id"],)).fetchone()

    b = [f'<h2 class="ser">{E(e["title"])}</h2>',
         f'<p class="sub"><a class="q" href="/p/{E(e["project_id"])}">'
         f'{E(pn["name"] if pn else e["project_id"])}</a> &nbsp; {e["started"][:16].replace("T"," ")} to '
         f'{e["ended"][11:16]} &nbsp; {dur(e["duration_s"])} &nbsp; branch {E(e["branch"] or "—")}</p>',
         f'<div class="stats"><div><b>{e["n_prompts"]}</b><span>prompts</span></div>'
         f'<div><b>{e["n_tools"]}</b><span>tool calls</span></div>'
         f'<div><b>{n(e["out_tokens"])}</b><span>output tokens</span></div>'
         f'<div><b>{e["compactions"]}</b><span>compactions</span></div>'
         f'<div><b>{len(runs)}</b><span>agent runs</span></div></div>']
    if touched:
        b.append("<h3>Where the work landed</h3><div class=\"wrap\"><table><tbody>")
        for t in touched:
            odd = "" if t["project_id"] == e["project_id"] else \
                  f' <span class="mk ck">{E(t["project_id"])}</span>'
            b.append(f'<tr><td class="mo d">{E(t["root"])}{odd}</td>'
                     f'<td class="num">{t["n_files"]} file{plural(t["n_files"])}</td></tr>')
        b.append("</tbody></table></div>")
    if msgs:
        b.append(f"<h3>What you asked, verbatim ({len(msgs)})</h3><ul class=\"prompts\">")
        for m in msgs:
            b.append(f'<li><time>{m["ts"][11:16]}</time>{E(" ".join((m["text"] or "").split()))}</li>')
        b.append("</ul>")
    if runs:
        b.append("<h3>Agents it spawned</h3><div class=\"wrap\"><table><tbody>")
        for r in runs:
            head = (r["prompt"] or "").strip().splitlines()
            b.append(f'<tr><td><a class="q" href="/a/{r["id"]}">'
                     f'{E(head[0][:120] if head else "(no prompt)")}</a></td>'
                     f'<td class="num d">{n(r["out_tokens"])}</td></tr>')
        b.append("</tbody></table></div>")
    if tools:
        b.append("<h3>Tools</h3><p class=\"sub mo\">" +
                 "&nbsp;&nbsp; ".join(f"{E(k)} {v}" for k, v in list(tools.items())[:14]) + "</p>")
    if files:
        b.append(f"<h3>Files touched ({len(files)})</h3><ul class=\"files\">")
        b += [f"<li>{E(f)}</li>" for f in files]
        b.append("</ul>")
    return shell(con, e["title"][:40], "".join(b), active=e["project_id"])


def page_agent(con, aid):
    r = con.execute("SELECT * FROM agent_runs WHERE id=?", (aid,)).fetchone()
    if not r:
        return shell(con, "Not found", '<p class="empty">No agent run with that id.</p>')
    back = f'<a class="q" href="/e/{r["episode_id"]}">episode {r["episode_id"]}</a>' if r["episode_id"] else "—"
    b = [f'<h2 class="ser">Agent run {r["id"]}</h2>',
         f'<p class="sub">{E(r["project_id"])} &nbsp; {(r["ts"] or "")[:16].replace("T"," ")} &nbsp; '
         f'{r["n_tools"]} tool calls &nbsp; {n(r["out_tokens"])} output tokens &nbsp; from {back}</p>',
         "<h3>The task it was given</h3>", f'<div class="body">{E(r["prompt"] or "")}</div>',
         "<h3>What it concluded</h3>", f'<div class="body">{E(r["result"] or "")}</div>']
    return shell(con, f"Agent {r['id']}", "".join(b), active=r["project_id"])


def page_agents(con):
    rows = con.execute("SELECT * FROM agent_runs ORDER BY ts DESC LIMIT 300").fetchall()
    b = ['<h2 class="ser">Agent runs</h2>',
         f'<p class="sub">{len(rows)} of them. Every one produced a full result that has never '
         f'been visible anywhere else.</p>', '<div class="wrap"><table><thead><tr><th>Date</th>'
         '<th>Project</th><th>Task</th><th class="num">Tools</th><th class="num">Tokens</th>'
         '</tr></thead><tbody>']
    for r in rows:
        head = (r["prompt"] or "").strip().splitlines()
        b.append(f'<tr><td class="d mo">{(r["ts"] or "")[:10]}</td><td class="d">{E(r["project_id"])}</td>'
                 f'<td><a class="q" href="/a/{r["id"]}">{E(head[0][:110] if head else "(no prompt)")}</a>'
                 + (f'<span class="mk ag">workflow</span>' if r["workflow_id"] else "") +
                 f'</td><td class="num">{r["n_tools"]}</td><td class="num">{n(r["out_tokens"])}</td></tr>')
    return shell(con, "Agent runs", "".join(b) + "</tbody></table></div>")


def page_search(con, q):
    b = [f'<h2 class="ser">{E(q)}</h2>']
    try:
        eps = con.execute("""SELECT e.*, snippet(episodes_fts,2,'<b>','</b>','…',18) s
                             FROM episodes_fts JOIN episodes e ON e.id=episodes_fts.rowid
                             WHERE episodes_fts MATCH ? ORDER BY bm25(episodes_fts) LIMIT 40""",
                          (q,)).fetchall()
        ags = con.execute("""SELECT a.*, snippet(agents_fts,1,'<b>','</b>','…',14) s
                             FROM agents_fts JOIN agent_runs a ON a.id=agents_fts.rowid
                             WHERE agents_fts MATCH ? ORDER BY bm25(agents_fts) LIMIT 15""",
                          (q,)).fetchall()
    except sqlite3.OperationalError:
        return shell(con, q, "".join(b) + '<p class="empty">That query confused the search index. '
                                          'Try plain words, or "an exact phrase".</p>')
    b.append(f'<p class="sub">{len(eps)} episodes, {len(ags)} agent runs.</p>')
    if eps:
        b.append('<div class="wrap"><table><tbody>')
        for e in eps:
            b.append(f'<tr><td class="d mo">{e["started"][:10]}</td><td class="d">{E(e["project_id"])}</td>'
                     f'<td class="t"><a class="q" href="/e/{e["id"]}">{E(e["title"])}</a>'
                     f'<div class="snip">{e["s"] or ""}</div></td></tr>')
        b.append("</tbody></table></div>")
    if ags:
        b.append("<h3>Agent runs</h3><div class=\"wrap\"><table><tbody>")
        for r in ags:
            head = (r["prompt"] or "").strip().splitlines()
            b.append(f'<tr><td class="d mo">{(r["ts"] or "")[:10]}</td>'
                     f'<td><a class="q" href="/a/{r["id"]}">{E(head[0][:100] if head else "")}</a>'
                     f'<div class="snip">{r["s"] or ""}</div></td></tr>')
        b.append("</tbody></table></div>")
    if not eps and not ags:
        b.append('<p class="empty">Nothing matched. Fewer or broader words usually finds it.</p>')
    return shell(con, q, "".join(b))


def page_checkpoints(con):
    from . import checkpoint
    files = sorted(checkpoint.all_checkpoints(), reverse=True)
    b = ['<h2 class="ser">Checkpoints</h2>',
         '<p class="sub">Saved automatically the moment before Claude Code compacts a session. '
         'Each holds what a summary loses: your exact words, the open task list, files in flight.</p>']
    if not files:
        b.append('<p class="empty">None yet. The next compaction writes one.</p>')
    for f in files[:40]:
        b.append(f'<h3>{f.stem}</h3><div class="body">{E(f.read_text()[:6000])}</div>')
    return shell(con, "Checkpoints", "".join(b))


ROUTES = {"/": page_home, "/agents": page_agents, "/checkpoints": page_checkpoints}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        p = u.path.rstrip("/") or "/"
        con = db.connect()
        try:
            if p in ROUTES:
                out = ROUTES[p](con)
            elif p == "/search":
                q = urllib.parse.parse_qs(u.query).get("q", [""])[0].strip()
                out = page_search(con, q) if q else page_home(con)
            elif p.startswith("/p/"):
                out = page_project(con, urllib.parse.unquote(p[3:]))
            elif p.startswith("/e/"):
                out = page_episode(con, int(p[3:]))
            elif p.startswith("/a/"):
                out = page_agent(con, int(p[3:]))
            else:
                self.send_error(404); return
        except Exception as ex:
            out = shell(con, "Error", f'<p class="empty">{E(type(ex).__name__)}: {E(str(ex))}</p>')
        data = out.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(port=7777, open_browser=True):
    srv = HTTPServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}"
    print(f"  Chronicle dashboard on {url}   (ctrl-c to stop)")
    if open_browser:
        try:
            import webbrowser; webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
