"""Local dashboard — a developer console over the index.

Serves on 127.0.0.1 only, read-only, no dependencies. Two things here are load-bearing
and easy to break:

  * every string that reaches the page is escaped, including FTS `snippet()` output.
    Snippets are raw transcript text; a prompt containing HTML would otherwise execute.
  * the Host header is checked. Binding to 127.0.0.1 does not stop a site the user
    visits from rebinding its own hostname to 127.0.0.1 and reading this as same-origin.
"""
import os, json, html, math, datetime, sqlite3, urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from . import db

E = html.escape

# snippet() markers: control characters survive escaping and never occur in transcripts
HI0, HI1 = "\x01", "\x02"


def snip(s):
    """Escape a snippet, then restore only the highlight we asked SQLite to insert."""
    return E(s or "").replace(HI0, "<em>").replace(HI1, "</em>")


def n(x):
    x = x or 0
    if x >= 1_000_000_000: return f"{x/1e9:.2f}B"
    if x >= 1_000_000: return f"{x/1e6:.1f}M"
    if x >= 1_000: return f"{x/1e3:.1f}k"
    return str(x)


def plural(k):
    return "" if k == 1 else "s"


def dur(s):
    h, m = divmod(int(s or 0) // 60, 60)
    return f"{h}h{m:02d}" if h else f"{m}m"


def pct(part, whole):
    return f"{(part or 0) / whole * 100:.1f}%" if whole else "—"


def ago(iso):
    if not iso:
        return "—"
    d = datetime.date.fromisoformat(iso[:10])
    k = (datetime.date.today() - d).days
    if k == 0: return "today"
    if k == 1: return "yesterday"
    if k < 21: return f"{k}d ago"
    if k < 120: return f"{k//7}w ago"
    return iso[:10]


# ---------------------------------------------------------------- chrome

CSS = """
:root{
 --bg:#F7F8F8;--panel:#FFFFFF;--panel2:#F0F2F2;--line:#E2E5E5;--line2:#CFD4D4;
 --fg:#14181A;--fg2:#394247;--mut:#6C777E;--dim:#96A0A6;
 --acc:#B7791F;--accw:#FBF2E1;--grn:#1E7A55;--vio:#5B4BC4;--blu:#1F6FB2;--red:#B0384A;
 --g0:#E6E9E9;--g1:#D3E3D9;--g2:#9CCBB4;--g3:#5CAE8B;--g4:#1E7A55;
}
@media(prefers-color-scheme:dark){:root{
 --bg:#0A0C0D;--panel:#101315;--panel2:#171B1E;--line:#1E2427;--line2:#2C3438;
 --fg:#E4E9EC;--fg2:#B6C0C6;--mut:#7B868D;--dim:#5A646A;
 --acc:#E3A94A;--accw:#241C0C;--grn:#5BC896;--vio:#9A8AF0;--blu:#5AA6EC;--red:#E0697C;
 --g0:#171B1E;--g1:#1B3B2E;--g2:#215E45;--g3:#2F8A62;--g4:#5BC896;
}}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);
 font:14px/1.5 ui-sans-serif,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
 -webkit-font-smoothing:antialiased}
a{color:inherit;text-decoration:none}
.mo,.num,code{font-family:ui-monospace,"SF Mono",SFMono-Regular,Menlo,Consolas,monospace;
 font-variant-numeric:tabular-nums}
.num{text-align:right;white-space:nowrap}
em{font-style:normal;color:var(--fg);background:var(--accw);
 box-shadow:inset 0 -1px 0 var(--acc);padding:0 1px}

/* ---- frame ---- */
.bar{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:14px;
 height:48px;padding:0 16px;background:var(--panel);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:8px;font-weight:600;letter-spacing:-.01em;
 font-size:14px;white-space:nowrap}
.dot{width:7px;height:7px;border-radius:50%;background:var(--grn);
 box-shadow:0 0 0 3px color-mix(in srgb,var(--grn) 20%,transparent)}
.bar form{flex:1;max-width:520px;position:relative}
.bar input{width:100%;height:30px;padding:0 34px 0 30px;border:1px solid var(--line2);
 border-radius:6px;background:var(--bg);color:var(--fg);font:13px ui-sans-serif,-apple-system,sans-serif}
.bar input:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 3px var(--accw)}
.bar .mag{position:absolute;left:10px;top:7px;color:var(--dim);font-size:12px;pointer-events:none}
.bar .kbd{position:absolute;right:8px;top:6px;pointer-events:none}
kbd{font:11px/16px ui-monospace,Menlo,monospace;color:var(--dim);border:1px solid var(--line2);
 border-bottom-width:2px;border-radius:4px;padding:0 5px;background:var(--panel2)}
.bar .meta{margin-left:auto;color:var(--dim);font-size:11.5px;white-space:nowrap}

.shell{display:grid;grid-template-columns:228px minmax(0,1fr);align-items:start}
nav{position:sticky;top:48px;height:calc(100vh - 48px);overflow-y:auto;padding:14px 0 40px;
 border-right:1px solid var(--line);background:var(--panel)}
nav .grp{color:var(--dim);font-size:10.5px;letter-spacing:.09em;text-transform:uppercase;
 margin:16px 16px 6px;font-weight:600}
nav ul{list-style:none;margin:0;padding:0 8px}
nav li a{display:flex;justify-content:space-between;gap:8px;align-items:center;
 padding:5px 8px;border-radius:6px;font-size:13px;color:var(--fg2);
 white-space:nowrap;overflow:hidden}
nav li a b{font-weight:500;overflow:hidden;text-overflow:ellipsis}
nav li a:hover{background:var(--panel2);color:var(--fg)}
nav li a.on{background:var(--accw);color:var(--acc);font-weight:600}
nav li a span{color:var(--dim);font-size:11px;font-family:ui-monospace,Menlo,monospace}
main{padding:22px 26px 120px;max-width:1220px;min-width:0}
@media(max-width:900px){.shell{grid-template-columns:1fr}
 nav{position:static;height:auto;border-right:0;border-bottom:1px solid var(--line)}
 main{padding:16px}.bar .meta{display:none}}

/* ---- headings ---- */
.head{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:0 0 4px}
h2{font-size:20px;font-weight:600;letter-spacing:-.02em;margin:0}
.sub{color:var(--mut);font-size:12.5px;margin:0 0 20px}
.sub a{color:var(--acc)}
h3{font-size:11px;letter-spacing:.09em;text-transform:uppercase;font-weight:600;
 color:var(--dim);margin:30px 0 9px}
.crumb{color:var(--dim);font-size:12px;margin:0 0 10px}
.crumb a:hover{color:var(--acc)}

/* ---- cards ---- */
.grid{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(146px,1fr))}
.card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:11px 13px}
.card .k{color:var(--dim);font-size:10.5px;letter-spacing:.07em;text-transform:uppercase;
 font-weight:600}
.card .v{font:600 22px/1.2 ui-monospace,Menlo,monospace;letter-spacing:-.02em;margin-top:5px}
.card .f{color:var(--mut);font-size:11.5px;margin-top:2px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:14px}

/* ---- tables ---- */
.wrap{background:var(--panel);border:1px solid var(--line);border-radius:8px;overflow:auto}
table{width:100%;border-collapse:collapse;font-size:13px}
th{position:sticky;top:0;background:var(--panel);text-align:left;font-size:10.5px;
 letter-spacing:.07em;text-transform:uppercase;color:var(--dim);font-weight:600;
 padding:9px 12px;border-bottom:1px solid var(--line2);white-space:nowrap}
th.num{text-align:right}
td{padding:8px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
tbody tr:last-child td{border-bottom:0}
tbody tr:hover td{background:var(--panel2)}
td.t{font-weight:500;color:var(--fg)}
td.t a:hover{color:var(--acc)}
td.d{color:var(--mut);font-size:12px;white-space:nowrap}
td.w{max-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
/* the one long column per table absorbs the leftover width; the rest stay tight */
td.grow{width:99%;max-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}

/* ---- bits ---- */
.tag{display:inline-block;font:500 10.5px/16px ui-monospace,Menlo,monospace;padding:0 5px;
 border-radius:4px;margin-left:5px;white-space:nowrap;border:1px solid transparent}
.tag.ag{color:var(--grn);border-color:color-mix(in srgb,var(--grn) 35%,transparent)}
.tag.ck{color:var(--vio);border-color:color-mix(in srgb,var(--vio) 35%,transparent)}
.tag.fl{color:var(--mut);border-color:var(--line2)}
.tag.wf{color:var(--blu);border-color:color-mix(in srgb,var(--blu) 35%,transparent)}
.meter{height:5px;border-radius:3px;background:var(--panel2);overflow:hidden;min-width:52px}
.meter i{display:block;height:100%;background:var(--acc)}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:11.5px;color:var(--mut);margin-top:9px}
.legend b{color:var(--fg);font-family:ui-monospace,Menlo,monospace;font-weight:600}
.sw{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:6px}
.stack{display:flex;height:26px;border-radius:6px;overflow:hidden;background:var(--panel2)}
.stack i{display:block}
.scale{display:flex;align-items:center;gap:4px;font-size:10.5px;color:var(--dim);
 justify-content:flex-end;margin-top:7px}
.scale i{width:10px;height:10px;border-radius:2px;display:block}

.body{background:var(--panel);border:1px solid var(--line);border-radius:8px;
 padding:14px 16px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px;line-height:1.62;
 color:var(--fg2);max-height:640px;overflow:auto}
.prompts{list-style:none;margin:0;padding:0;border-left:2px solid var(--line2)}
.prompts li{padding:0 0 10px 14px;font-size:13px;color:var(--fg2)}
.prompts time{color:var(--dim);font:11.5px ui-monospace,Menlo,monospace;margin-right:9px}
.files{columns:2;column-gap:24px;font-size:11.5px;color:var(--mut);margin:0;padding:0;
 list-style:none}
.files li{break-inside:avoid;margin-bottom:2px;font-family:ui-monospace,Menlo,monospace;
 overflow-wrap:anywhere}
@media(max-width:760px){.files{columns:1}}
.snp{color:var(--mut);font-size:12px;margin-top:3px;line-height:1.45}
.empty{color:var(--mut);padding:34px 0;font-size:13px}
:focus-visible{outline:2px solid var(--acc);outline-offset:2px}
"""

JS = """
addEventListener('keydown',function(e){
 var i=document.getElementById('q');
 if(!i)return;
 if(e.key==='/'&&document.activeElement!==i){e.preventDefault();i.focus();i.select();}
 if(e.key==='Escape'&&document.activeElement===i){i.blur();}
});
"""


# ---------------------------------------------------------------- drawings

def spark(vals, w=118, h=20, color="var(--acc)"):
    """Square-rooted bars. Token counts span four orders of magnitude; a linear
    scale would render every week but the biggest as a flat line."""
    if not vals or max(vals) == 0:
        return f'<svg width="{w}" height="{h}" aria-hidden="true"></svg>'
    hi = math.sqrt(max(vals))
    bw = w / len(vals)
    out = [f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" aria-hidden="true">']
    for i, v in enumerate(vals):
        bh = max(2.0, math.sqrt(v) / hi * h) if v else 1.0
        out.append(f'<rect x="{i*bw:.2f}" y="{h-bh:.2f}" width="{max(1.0,bw-1.2):.2f}" '
                   f'height="{bh:.2f}" rx="0.8" fill="{color}" opacity="{0.95 if v else 0.2}"/>')
    return "".join(out) + "</svg>"


HEAT = ["var(--g0)", "var(--g1)", "var(--g2)", "var(--g3)", "var(--g4)"]


def heatmap(daily, weeks=27, cell=11, gap=3):
    """A day-by-day grid, the shape every developer already knows how to read."""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=today.weekday() + 7 * (weeks - 1))
    hi = max(daily.values()) if daily else 0
    w, h = weeks * (cell + gap), 7 * (cell + gap) + 16
    out = [f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" '
           f'aria-label="daily activity for the last {weeks} weeks">']
    seen = set()
    for i in range(weeks * 7):
        d = start + datetime.timedelta(days=i)
        if d > today:
            break
        wk, dow = i // 7, i % 7
        v = daily.get(d.isoformat(), 0)
        lvl = 0 if not v or not hi else min(4, 1 + int(v / hi * 3.999))
        out.append(f'<rect x="{wk*(cell+gap)}" y="{dow*(cell+gap)+14}" width="{cell}" '
                   f'height="{cell}" rx="2.5" fill="{HEAT[lvl]}">'
                   f'<title>{d.isoformat()} · {v} prompt{plural(v)}</title></rect>')
        if d.day <= 7 and d.strftime("%b") not in seen:
            seen.add(d.strftime("%b"))
            out.append(f'<text x="{wk*(cell+gap)}" y="9" font-size="9.5" fill="var(--dim)" '
                       f'font-family="ui-monospace,Menlo,monospace">{d.strftime("%b")}</text>')
    out.append("</svg>")
    lg = "".join(f'<i style="background:{c}"></i>' for c in HEAT)
    return ("".join(out) + f'<div class="scale">less {lg} more</div>')


TOKEN_KINDS = [("in_tokens", "fresh input", "var(--acc)"),
               ("cache_write", "cache write", "var(--vio)"),
               ("cache_read", "cache read", "var(--blu)"),
               ("out_tokens", "output", "var(--grn)")]


def token_stack(t):
    """One bar, four parts. Printing a single total is what made the old number a lie:
    97% of it is cache reads, which are not billed like fresh input.

    `t` is any mapping or sqlite3.Row carrying the four token columns."""
    def g(k):
        try:
            return t[k] or 0
        except (KeyError, IndexError):
            return 0
    total = sum(g(k) for k, _, _ in TOKEN_KINDS)
    if not total:
        return ""
    segs, leg = [], []
    for k, label, col in TOKEN_KINDS:
        v = g(k)
        if v:
            segs.append(f'<i style="width:{v/total*100:.4f}%;background:{col}" '
                        f'title="{label}: {v:,}"></i>')
        leg.append(f'<span><span class="sw" style="background:{col}"></span>{label} '
                   f'<b>{n(v)}</b> · {pct(v, total)}</span>')
    return (f'<div class="stack">{"".join(segs)}</div><div class="legend">{"".join(leg)}</div>')


def markers(e):
    s = ""
    if e["n_agents"]:
        s += f'<span class="tag ag">{e["n_agents"]}&nbsp;agents</span>'
    if e["compactions"]:
        s += f'<span class="tag ck">{e["compactions"]}&nbsp;compact</span>'
    nf = len(json.loads(e["files_touched"] or "[]"))
    if nf:
        s += f'<span class="tag fl">{nf}&nbsp;files</span>'
    return s


# ---------------------------------------------------------------- queries

# episode_projects holds one row per (episode, project, repo root), so it must be
# collapsed before it is joined or every multi-repo episode is counted twice.
EP = "(SELECT DISTINCT episode_id, project_id FROM episode_projects)"


def totals(con, pid=None):
    where, args = ("WHERE p.project_id=?", [pid]) if pid else ("", [])
    return con.execute(f"""
        SELECT COUNT(DISTINCT e.id) eps, SUM(e.n_prompts) prompts, SUM(e.n_tools) tools,
               SUM(e.duration_s) secs, SUM(e.compactions) compactions,
               COUNT(DISTINCT substr(e.started,1,10)) days,
               MIN(e.started) first, MAX(e.ended) last,
               SUM(e.in_tokens) in_tokens, SUM(e.cache_write) cache_write,
               SUM(e.cache_read) cache_read, SUM(e.out_tokens) out_tokens
        FROM {EP} p JOIN episodes e ON e.id=p.episode_id {where}""", args).fetchone()


def per_project(con):
    return con.execute(f"""
        SELECT p.project_id id,
               COALESCE((SELECT name FROM projects WHERE id=p.project_id), p.project_id) nm,
               COUNT(DISTINCT e.id) eps, SUM(e.n_prompts) prompts, SUM(e.n_agents) agents,
               COUNT(DISTINCT substr(e.started,1,10)) days, MAX(e.ended) last,
               SUM(e.in_tokens) in_tokens, SUM(e.cache_write) cache_write,
               SUM(e.cache_read) cache_read, SUM(e.out_tokens) out_tokens,
               SUM(COALESCE(e.in_tokens,0)+COALESCE(e.cache_write,0)
                  +COALESCE(e.cache_read,0)+COALESCE(e.out_tokens,0)) tot
        FROM {EP} p JOIN episodes e ON e.id=p.episode_id
        GROUP BY p.project_id ORDER BY last DESC""").fetchall()


def span_weeks(con, cap=27, floor=13):
    """How much history is worth drawing. A fixed 12-month grid on a two-month
    corpus is mostly empty squares pretending to be data."""
    r = con.execute("SELECT MIN(started) m FROM episodes").fetchone()
    if not r or not r["m"]:
        return floor
    d = datetime.date.fromisoformat(r["m"][:10])
    return max(floor, min(cap, (datetime.date.today() - d).days // 7 + 2))


def weekly_all(con, weeks=20):
    """Every project's weekly output in one pass. The old page ran a full table
    scan per project; that is fine at 130 episodes and not at 13,000."""
    today = datetime.date.today()
    start = today - datetime.timedelta(days=today.weekday() + 7 * (weeks - 1))
    out = {}
    for r in con.execute(f"SELECT p.project_id id, e.started, e.out_tokens o "
                         f"FROM {EP} p JOIN episodes e ON e.id=p.episode_id"):
        i = (datetime.date.fromisoformat(r["started"][:10]) - start).days // 7
        if 0 <= i < weeks:
            out.setdefault(r["id"], [0] * weeks)[i] += r["o"] or 0
    return out


def daily(con, pid=None):
    where, args = ("WHERE p.project_id=?", [pid]) if pid else ("", [])
    return {r["d"]: r["v"] for r in con.execute(
        f"""SELECT substr(e.started,1,10) d, SUM(e.n_prompts) v FROM {EP} p
            JOIN episodes e ON e.id=p.episode_id {where} GROUP BY d""", args)}


# ---------------------------------------------------------------- shell

def shell(con, title, body, active=None, q=""):
    ps = con.execute(f"""SELECT p.project_id id, COUNT(DISTINCT p.episode_id) c,
                         MAX(e.ended) last,
                         COALESCE((SELECT name FROM projects WHERE id=p.project_id),
                                  p.project_id) nm
                         FROM {EP} p JOIN episodes e ON e.id=p.episode_id
                         GROUP BY p.project_id ORDER BY last DESC""").fetchall()
    li = "".join(
        f'<li><a class="{"on" if r["id"]==active else ""}" href="/p/{urllib.parse.quote(r["id"])}" '
        f'title="{E(r["nm"])}"><b>{E(r["nm"])}</b><span>{r["c"]}</span></a></li>' for r in ps)
    nav_items = [("/", "Overview", "home"), ("/tokens", "Token usage", "tokens"),
                 ("/agents", "Agent runs", "agents"), ("/checkpoints", "Checkpoints", "checkpoints")]
    top = "".join(f'<li><a class="{"on" if active==k else ""}" href="{h}"><b>{t}</b></a></li>'
                  for h, t, k in nav_items)
    r = con.execute("SELECT v FROM meta WHERE k='last_index'").fetchone()
    stamp = (r["v"] if r else "").replace("T", " ")[:16] or "never"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex">
<title>{E(title)} · chronicle</title><style>{CSS}</style></head><body>
<header class="bar"><a class="brand" href="/"><span class="dot"></span>chronicle</a>
<form action="/search" role="search"><span class="mag mo">&#8981;</span>
<input id="q" name="q" value="{E(q)}" autocomplete="off" spellcheck="false"
 placeholder="Search every prompt and agent result on this machine">
<span class="kbd"><kbd>/</kbd></span></form>
<div class="meta mo">indexed {E(stamp)}</div></header>
<div class="shell"><nav>
<div class="grp">Console</div><ul>{top}</ul>
<div class="grp">Projects</div><ul>{li}</ul></nav>
<main>{body}</main></div><script>{JS}</script></body></html>"""


def card(k, v, f=""):
    return (f'<div class="card"><div class="k">{k}</div><div class="v">{v}</div>'
            + (f'<div class="f">{f}</div>' if f else "") + "</div>")


def ep_table(con, eps, show_proj=False, limit=None):
    rows = eps[:limit] if limit else eps
    out = ['<div class="wrap"><table><thead><tr><th>Date</th>']
    if show_proj:
        out.append("<th>Project</th>")
    out.append('<th>Episode</th><th class="num">Prompts</th><th class="num">Tools</th>'
               '<th class="num">Elapsed</th><th class="num">Output</th></tr></thead><tbody>')
    for e in rows:
        p = f'<td class="d">{E(e["project_id"])}</td>' if show_proj else ""
        out.append(f'<tr><td class="d mo">{e["started"][:10]}</td>{p}'
                   f'<td class="t grow"><a href="/e/{e["id"]}">{E(e["title"])}</a>{markers(e)}</td>'
                   f'<td class="num mo">{e["n_prompts"]}</td>'
                   f'<td class="num mo">{e["n_tools"]}</td>'
                   f'<td class="num mo d">{dur(e["duration_s"])}</td>'
                   f'<td class="num mo">{n(e["out_tokens"])}</td></tr>')
    return "".join(out) + "</tbody></table></div>"


# ---------------------------------------------------------------- pages

def page_home(con):
    t = totals(con)
    if not t["eps"]:
        return shell(con, "Overview", '<p class="empty">Nothing indexed yet. '
                     'Run <code>chronicle index</code>.</p>', "home")
    ag = con.execute("SELECT COUNT(*) n, SUM(out_tokens) o FROM agent_runs").fetchone()
    grand = sum(t[k] or 0 for k, _, _ in TOKEN_KINDS)
    wks = span_weeks(con)
    rows, wk = per_project(con), weekly_all(con, wks)
    b = [f'<div class="head"><h2>Overview</h2></div>',
         f'<p class="sub">{t["eps"]} episodes across {len(rows)} projects, '
         f'{t["first"][:10]} to {t["last"][:10]}. An episode is a contiguous run of work — '
         f'sessions are split wherever you stopped for four hours or more.</p>',
         '<div class="grid">',
         card("Episodes", t["eps"], f'over {t["days"]} active days'),
         card("Prompts", f'{t["prompts"]:,}', f'{t["tools"]:,} tool calls'),
         card("Agent runs", ag["n"], f'{n(ag["o"])} output tokens'),
         card("Elapsed", dur(t["secs"]), f'{t["compactions"]} compactions'),
         card("Tokens", n(grand), f'{pct(t["cache_read"], grand)} of it cache reads'),
         '</div>',
         '<h3>Token composition</h3><div class="panel">' + token_stack(t) + '</div>',
         '<h3>Daily activity</h3><div class="panel">' + heatmap(daily(con), wks) + '</div>',
         '<h3>Projects</h3><div class="wrap"><table><thead><tr><th>Project</th>'
         '<th>Output, weekly</th><th class="num">Episodes</th><th class="num">Days</th>'
         '<th class="num">Prompts</th><th class="num">Agents</th><th class="num">Tokens</th>'
         '<th>Share</th><th class="num">Last</th></tr></thead><tbody>']
    top = max((r["tot"] or 0) for r in rows) or 1
    for r in rows:
        b.append(f'<tr><td class="t"><a href="/p/{urllib.parse.quote(r["id"])}">'
                 f'{E(r["nm"])}</a></td>'
                 f'<td>{spark(wk.get(r["id"], []), w=max(90, wks * 6))}</td>'
                 f'<td class="num mo">{r["eps"]}</td><td class="num mo">{r["days"]}</td>'
                 f'<td class="num mo">{r["prompts"]}</td>'
                 f'<td class="num mo">{r["agents"] or 0}</td>'
                 f'<td class="num mo">{n(r["tot"])}</td>'
                 f'<td><div class="meter"><i style="width:{(r["tot"] or 0)/top*100:.1f}%"></i>'
                 f'</div></td>'
                 f'<td class="num d">{ago(r["last"])}</td></tr>')
    b.append("</tbody></table></div><h3>Latest episodes</h3>")
    b.append(ep_table(con, con.execute(
        "SELECT * FROM episodes ORDER BY started DESC LIMIT 14").fetchall(), show_proj=True))
    return shell(con, "Overview", "".join(b), "home")


def page_tokens(con):
    t = totals(con)
    grand = sum(t[k] or 0 for k, _, _ in TOKEN_KINDS)
    if not grand:
        return shell(con, "Tokens", '<p class="empty">No token data indexed.</p>', "tokens")
    b = ['<div class="head"><h2>Token usage</h2></div>',
         '<p class="sub">Every token Claude Code has spent on this machine, split four ways. '
         'Cache reads dominate the total and are billed far below fresh input, so the '
         'headline number alone would mislead you.</p>',
         '<div class="panel">' + token_stack(t) + '</div>']
    for label, col, expr, fmt in [("By project", "Project", None, None),
                                  ("By month", "Month", "substr(e.started,1,7)", None),
                                  ("By day, last 30", "Date", "substr(e.started,1,10)", 30)]:
        b.append(f"<h3>{label}</h3>")
        if expr is None:
            rows = [(r["nm"], r["id"], r["in_tokens"], r["cache_write"], r["cache_read"],
                     r["out_tokens"], r["tot"], r["eps"])
                    for r in sorted(per_project(con), key=lambda r: -(r["tot"] or 0))]
        else:
            q = con.execute(f"""
                SELECT {expr} k, SUM(e.in_tokens) i, SUM(e.cache_write) w,
                       SUM(e.cache_read) r, SUM(e.out_tokens) o, COUNT(DISTINCT e.id) eps
                FROM episodes e GROUP BY k ORDER BY k DESC""").fetchall()
            if fmt:
                q = q[:fmt]
            rows = [(r["k"], None, r["i"], r["w"], r["r"], r["o"],
                     (r["i"] or 0)+(r["w"] or 0)+(r["r"] or 0)+(r["o"] or 0), r["eps"])
                    for r in q]
        top = max([r[6] or 0 for r in rows] or [1]) or 1
        b.append('<div class="wrap"><table><thead><tr><th>' + col +
                 '</th><th class="num">Episodes</th><th class="num">Fresh in</th>'
                 '<th class="num">Cache write</th><th class="num">Cache read</th>'
                 '<th class="num">Output</th><th class="num">Total</th><th>Share</th>'
                 '</tr></thead><tbody>')
        for nm, pid, i, w, r, o, tot, eps in rows:
            link = (f'<a href="/p/{urllib.parse.quote(pid)}">{E(nm)}</a>') if pid else \
                   f'<span class="mo">{E(nm)}</span>'
            b.append(f'<tr><td class="t">{link}</td><td class="num mo d">{eps}</td>'
                     f'<td class="num mo">{n(i)}</td><td class="num mo">{n(w)}</td>'
                     f'<td class="num mo">{n(r)}</td><td class="num mo">{n(o)}</td>'
                     f'<td class="num mo">{n(tot)}</td>'
                     f'<td><div class="meter"><i style="width:{(tot or 0)/top*100:.1f}%"></i>'
                     f'</div></td></tr>')
        b.append("</tbody></table></div>")
    return shell(con, "Tokens", "".join(b), "tokens")


def page_project(con, pid):
    row = con.execute("SELECT id,name FROM projects WHERE id=?", (pid,)).fetchone()
    if not row:
        # ids are lowercase slugs, display names are not — a hand-typed or
        # copy-pasted /p/Nutristar should still land somewhere
        row = con.execute("SELECT id,name FROM projects WHERE id=? COLLATE NOCASE "
                          "OR name=? COLLATE NOCASE LIMIT 1", (pid, pid)).fetchone()
    if row:
        pid = row["id"]
    name = row["name"] if row else pid
    eps = con.execute(f"""SELECT e.* FROM episodes e WHERE e.id IN
                          (SELECT episode_id FROM {EP} WHERE project_id=?)
                          ORDER BY e.started DESC""", (pid,)).fetchall()
    if not eps:
        return shell(con, name, '<p class="empty">Nothing indexed for this project yet.</p>',
                     active=pid)
    t = totals(con, pid)
    grand = sum(t[k] or 0 for k, _, _ in TOKEN_KINDS)
    repos = con.execute("""SELECT root, COUNT(DISTINCT episode_id) eps, SUM(n_files) f
                           FROM episode_projects WHERE project_id=? AND source='files'
                           GROUP BY root ORDER BY f DESC""", (pid,)).fetchall()
    runs = con.execute("SELECT * FROM agent_runs WHERE project_id=? ORDER BY ts DESC",
                       (pid,)).fetchall()
    b = [f'<div class="head"><h2>{E(name)}</h2>'
         f'<span class="tag fl mo">{E(pid)}</span></div>',
         f'<p class="sub">{t["eps"]} episodes over {t["days"]} active days, '
         f'{t["first"][:10]} to {t["last"][:10]} · last worked {ago(t["last"])}.</p>',
         '<div class="grid">',
         card("Prompts", f'{t["prompts"]:,}', f'{t["tools"]:,} tool calls'),
         card("Elapsed", dur(t["secs"]), f'{t["eps"]} episodes'),
         card("Agent runs", len(runs), f'{t["compactions"]} compactions'),
         card("Output", n(t["out_tokens"]), "tokens written"),
         card("Total tokens", n(grand), f'{pct(t["cache_read"], grand)} cache reads'),
         '</div>',
         '<h3>Token composition</h3><div class="panel">' + token_stack(t) + '</div>',
         '<h3>Daily activity</h3><div class="panel">' + heatmap(daily(con, pid), span_weeks(con)) + '</div>']
    if repos:
        b.append('<h3>Repositories this project actually lives in</h3>'
                 '<div class="wrap"><table><thead><tr><th>Path</th><th class="num">Episodes</th>'
                 '<th class="num">Files</th></tr></thead><tbody>')
        for r in repos:
            b.append(f'<tr><td class="mo d grow">{E(r["root"])}</td>'
                     f'<td class="num mo">{r["eps"]}</td><td class="num mo">{r["f"]}</td></tr>')
        b.append("</tbody></table></div>")
    b.append(f"<h3>Episodes ({len(eps)})</h3>")
    b.append(ep_table(con, eps))
    if runs:
        b.append(f'<h3>Agent runs ({len(runs)})</h3><div class="wrap"><table><thead><tr>'
                 '<th>Date</th><th>Task</th><th class="num">Tools</th>'
                 '<th class="num">Output</th></tr></thead><tbody>')
        for r in runs[:60]:
            head = (r["prompt"] or "").strip().splitlines()
            b.append(f'<tr><td class="d mo">{(r["ts"] or "")[:10]}</td>'
                     f'<td class="t grow"><a href="/a/{r["id"]}">'
                     f'{E(head[0][:150] if head else "(no prompt)")}</a></td>'
                     f'<td class="num mo">{r["n_tools"]}</td>'
                     f'<td class="num mo">{n(r["out_tokens"])}</td></tr>')
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
    pname = pn["name"] if pn else e["project_id"]
    grand = sum(e[k] or 0 for k, _, _ in TOKEN_KINDS)
    b = [f'<p class="crumb"><a href="/p/{urllib.parse.quote(e["project_id"])}">{E(pname)}</a>'
         f' / <span class="mo">#{e["id"]}</span></p>',
         f'<div class="head"><h2>{E(e["title"])}</h2>{markers(e)}</div>',
         f'<p class="sub mo">{e["started"][:16].replace("T", " ")} → {e["ended"][11:16]} · '
         f'{dur(e["duration_s"])} · branch {E(e["branch"] or "—")} · '
         f'session {E((e["session_id"] or "")[:8])}</p>',
         '<div class="grid">',
         card("Prompts", e["n_prompts"], "you typed"),
         card("Tool calls", e["n_tools"], f'{len(files)} files touched'),
         card("Agents", len(runs), "spawned here"),
         card("Output", n(e["out_tokens"]), "tokens written"),
         card("Total tokens", n(grand), f'{pct(e["cache_read"], grand)} cache reads'),
         '</div>']
    if grand:
        b.append('<div class="panel" style="margin-top:10px">' + token_stack(e) + '</div>')
    if touched:
        b.append('<h3>Where the work landed</h3><div class="wrap"><table><thead><tr>'
                 '<th>Path</th><th class="num">Files</th></tr></thead><tbody>')
        for t in touched:
            odd = "" if t["project_id"] == e["project_id"] else \
                  f' <span class="tag ck">{E(t["project_id"])}</span>'
            b.append(f'<tr><td class="mo d grow">{E(t["root"])}{odd}</td>'
                     f'<td class="num mo">{t["n_files"]}</td></tr>')
        b.append("</tbody></table></div>")
    if msgs:
        b.append(f'<h3>What you asked, verbatim ({len(msgs)})</h3><ul class="prompts">')
        for m in msgs:
            b.append(f'<li><time>{m["ts"][11:16]}</time>'
                     f'{E(" ".join((m["text"] or "").split()))}</li>')
        b.append("</ul>")
    if runs:
        b.append('<h3>Agents it spawned</h3><div class="wrap"><table><thead><tr>'
                 '<th>Task</th><th class="num">Tools</th><th class="num">Output</th>'
                 '</tr></thead><tbody>')
        for r in runs:
            head = (r["prompt"] or "").strip().splitlines()
            b.append(f'<tr><td class="t grow"><a href="/a/{r["id"]}">'
                     f'{E(head[0][:150] if head else "(no prompt)")}</a></td>'
                     f'<td class="num mo">{r["n_tools"]}</td>'
                     f'<td class="num mo">{n(r["out_tokens"])}</td></tr>')
        b.append("</tbody></table></div>")
    if tools:
        b.append('<h3>Tools</h3><p class="sub mo">' + "&nbsp; ".join(
            f'{E(k)}&nbsp;<span style="color:var(--acc)">{v}</span>'
            for k, v in list(tools.items())[:16]) + "</p>")
    if files:
        b.append(f'<h3>Files touched ({len(files)})</h3><ul class="files">')
        b += [f"<li>{E(f)}</li>" for f in files]
        b.append("</ul>")
    return shell(con, e["title"][:48], "".join(b), active=e["project_id"])


def page_agent(con, aid):
    r = con.execute("SELECT * FROM agent_runs WHERE id=?", (aid,)).fetchone()
    if not r:
        return shell(con, "Not found", '<p class="empty">No agent run with that id.</p>')
    back = (f'<a href="/e/{r["episode_id"]}">episode #{r["episode_id"]}</a>'
            if r["episode_id"] else "an unmatched session")
    head = (r["prompt"] or "").strip().splitlines()
    b = [f'<p class="crumb"><a href="/p/{urllib.parse.quote(r["project_id"] or "")}">'
         f'{E(r["project_id"] or "—")}</a> / <a href="/agents">agent runs</a> / '
         f'<span class="mo">@{r["id"]}</span></p>',
         f'<div class="head"><h2>{E(head[0][:110] if head else "Agent run")}</h2>'
         + (f'<span class="tag wf">workflow</span>' if r["workflow_id"] else
            f'<span class="tag ag">{E(r["agent_type"] or "subagent")}</span>') + '</div>',
         f'<p class="sub">{(r["ts"] or "")[:16].replace("T", " ")} · '
         f'{r["n_tools"]} tool calls · {n(r["out_tokens"])} output tokens · from {back}</p>',
         '<h3>The task it was given</h3>', f'<div class="body">{E(r["prompt"] or "")}</div>',
         '<h3>What it concluded</h3>',
         f'<div class="body">{E(r["result"] or "(no result recorded)")}</div>']
    return shell(con, f"@{r['id']}", "".join(b), active=r["project_id"])


def page_agents(con):
    rows = con.execute("SELECT * FROM agent_runs ORDER BY ts DESC LIMIT 400").fetchall()
    tot = con.execute("SELECT COUNT(*) n, SUM(n_tools) t, SUM(out_tokens) o "
                      "FROM agent_runs").fetchone()
    b = ['<div class="head"><h2>Agent runs</h2></div>',
         '<p class="sub">Every subagent and workflow that has run on this machine. Each one '
         'produced a full result that exists in no transcript you can read.</p>',
         '<div class="grid">',
         card("Runs", tot["n"] or 0, "recorded"),
         card("Tool calls", f'{tot["t"] or 0:,}', "made by agents"),
         card("Output", n(tot["o"]), "tokens they wrote"),
         '</div><h3>Newest first</h3>',
         '<div class="wrap"><table><thead><tr><th>Date</th><th>Project</th><th>Task</th>'
         '<th class="num">Tools</th><th class="num">Output</th></tr></thead><tbody>']
    for r in rows:
        head = (r["prompt"] or "").strip().splitlines()
        b.append(f'<tr><td class="d mo">{(r["ts"] or "")[:10]}</td>'
                 f'<td class="d w">{E(r["project_id"] or "—")}</td>'
                 f'<td class="t grow"><a href="/a/{r["id"]}">'
                 f'{E(head[0][:160] if head else "(no prompt)")}</a>'
                 + ('<span class="tag wf">workflow</span>' if r["workflow_id"] else "") +
                 f'</td><td class="num mo">{r["n_tools"]}</td>'
                 f'<td class="num mo">{n(r["out_tokens"])}</td></tr>')
    if len(rows) == 400:
        b.append('<tr><td colspan="5" class="d">Showing the newest 400. '
                 'Use search, or <code>chronicle agents</code>.</td></tr>')
    return shell(con, "Agent runs", "".join(b) + "</tbody></table></div>", "agents")


def page_search(con, q):
    b = [f'<div class="head"><h2>{E(q)}</h2></div>']
    try:
        eps = con.execute(f"""SELECT e.*, snippet(episodes_fts,2,'{HI0}','{HI1}','…',20) s
                              FROM episodes_fts JOIN episodes e ON e.id=episodes_fts.rowid
                              WHERE episodes_fts MATCH ? ORDER BY bm25(episodes_fts) LIMIT 40""",
                          (q,)).fetchall()
        ags = con.execute(f"""SELECT a.*, snippet(agents_fts,1,'{HI0}','{HI1}','…',16) s
                              FROM agents_fts JOIN agent_runs a ON a.id=agents_fts.rowid
                              WHERE agents_fts MATCH ? ORDER BY bm25(agents_fts) LIMIT 20""",
                          (q,)).fetchall()
    except sqlite3.OperationalError:
        return shell(con, q, "".join(b) + '<p class="empty">That is not valid FTS5 syntax. '
                     'Use plain words, <code>OR</code>, <code>NOT</code>, '
                     '<code>"an exact phrase"</code>, or <code>prefix*</code>.</p>', q=q)
    b.append(f'<p class="sub">{len(eps)} episode{plural(len(eps))}, '
             f'{len(ags)} agent run{plural(len(ags))}.</p>')
    if eps:
        b.append('<h3>Episodes</h3><div class="wrap"><table><thead><tr><th>Date</th>'
                 '<th>Project</th><th>Match</th></tr></thead><tbody>')
        for e in eps:
            b.append(f'<tr><td class="d mo">{e["started"][:10]}</td>'
                     f'<td class="d w">{E(e["project_id"])}</td>'
                     f'<td class="t"><a href="/e/{e["id"]}">{E(e["title"])}</a>{markers(e)}'
                     f'<div class="snp">{snip(e["s"])}</div></td></tr>')
        b.append("</tbody></table></div>")
    if ags:
        b.append('<h3>Agent runs</h3><div class="wrap"><table><thead><tr><th>Date</th>'
                 '<th>Match</th></tr></thead><tbody>')
        for r in ags:
            head = (r["prompt"] or "").strip().splitlines()
            b.append(f'<tr><td class="d mo">{(r["ts"] or "")[:10]}</td>'
                     f'<td class="t"><a href="/a/{r["id"]}">'
                     f'{E(head[0][:140] if head else "(no prompt)")}</a>'
                     f'<div class="snp">{snip(r["s"])}</div></td></tr>')
        b.append("</tbody></table></div>")
    if not eps and not ags:
        b.append('<p class="empty">Nothing matched. Fewer or broader words usually finds it.</p>')
    return shell(con, q, "".join(b), q=q)


def page_checkpoints(con):
    from . import checkpoint
    files = sorted(checkpoint.all_checkpoints(), reverse=True)
    b = ['<div class="head"><h2>Checkpoints</h2></div>',
         '<p class="sub">Written the moment before Claude Code compacts a session — your exact '
         'words, the open task list, the files in flight. All the things a summary drops.</p>']
    if not files:
        b.append('<p class="empty">None yet. The next compaction writes one.</p>')
    for f in files[:40]:
        b.append(f'<h3>{E(f.stem)}</h3><div class="body">{E(f.read_text()[:6000])}</div>')
    return shell(con, "Checkpoints", "".join(b), "checkpoints")


# ---------------------------------------------------------------- transport

ROUTES = {"/": page_home, "/tokens": page_tokens, "/agents": page_agents,
          "/checkpoints": page_checkpoints}

ALLOWED_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


class Handler(BaseHTTPRequestHandler):
    server_version = "chronicle"
    sys_version = ""

    def log_message(self, *a):
        pass

    def _host_ok(self):
        """Reject anything but a loopback Host. Listening on 127.0.0.1 alone does not
        stop DNS rebinding: a page the user visits can point its own hostname at
        127.0.0.1 and then read this dashboard as same-origin."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip().lower()
        return host in ALLOWED_HOSTS or host == ""

    def do_GET(self):
        if not self._host_ok():
            self.send_error(403, "Chronicle only answers on localhost")
            return
        u = urllib.parse.urlparse(self.path)
        p = u.path.rstrip("/") or "/"
        con = None
        try:
            con = db.connect_ro(timeout_ms=5000)     # the dashboard never writes
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
                self.send_error(404)
                return
        except ValueError:
            self.send_error(404)
            return
        except Exception as ex:
            out = shell(con, "Error", '<p class="empty">Chronicle could not render that page: '
                        f'{E(type(ex).__name__)}.</p>') if con else "<h1>500</h1>"
        finally:
            if con:
                con.close()
        data = out.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; "
                         "script-src 'unsafe-inline'; img-src data:; form-action 'self'; "
                         "base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)


def serve(port=7777, open_browser=True):
    for p in range(port, port + 12):
        try:
            srv = HTTPServer(("127.0.0.1", p), Handler)
            break
        except OSError:
            continue
    else:
        print(f"  no free port in {port}–{port + 11}. Pass --port.")
        return
    url = f"http://127.0.0.1:{p}"
    print(f"  chronicle dashboard on {url}   (ctrl-c to stop)")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped")
