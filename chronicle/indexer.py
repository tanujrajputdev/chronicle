import os, json, time, datetime, collections
from .config import SOURCE, load_aliases, load_ignore_roots
from . import ingest, db
from .redact import scrub


def stable_id(con, session_id, seq):
    """Same work, same number — forever."""
    r = con.execute("SELECT id FROM episode_ids WHERE session_id=? AND seq=?",
                    (session_id, seq)).fetchone()
    if r:
        return r["id"]
    cur = con.execute("INSERT INTO episode_ids (session_id, seq) VALUES (?,?)", (session_id, seq))
    return cur.lastrowid


def stable_agent_id(con, path):
    """An agent run is identified by its transcript file, so its number never moves."""
    r = con.execute("SELECT id FROM agent_ids WHERE path=?", (path,)).fetchone()
    if r:
        return r["id"]
    return con.execute("INSERT INTO agent_ids (path) VALUES (?)", (path,)).lastrowid


def resolve_project(cwd, dirname, alias_paths, labels):
    path = cwd or ingest.decode_dirname(dirname)
    best, name = -1, None
    for prefix, target in alias_paths.items():
        if path == prefix or path.startswith(prefix.rstrip("/") + "/"):
            if len(prefix) > best:
                best, name = len(prefix), target
    if not name:
        name = os.path.basename(path.rstrip("/")) or path
    return name, labels.get(name, name), path


def _iso(d):
    return d.isoformat() if d else None


def build(con, rebuild=False, verbose=True, progress=None):
    alias_paths, labels = load_aliases()
    ignore = load_ignore_roots()
    if rebuild:
        db.reset(con)

    done = {r["path"]: (r["size"], r["mtime"]) for r in con.execute("SELECT * FROM files")}
    sess_files = ingest.scan_session_files()
    agent_files = ingest.scan_agent_files()

    proj_paths = collections.defaultdict(set)
    stats = {"sessions": 0, "episodes": 0, "messages": 0, "agents": 0,
             "redactions": 0, "skipped": 0, "bytes": 0}
    session_spans = {}

    for i, (dirname, path) in enumerate(sess_files):
        st = os.stat(path)
        if not rebuild and done.get(path) == (st.st_size, st.st_mtime):
            stats["skipped"] += 1
            continue
        if progress:
            progress(i + 1, len(sess_files), os.path.basename(path))
        stats["bytes"] += st.st_size

        s = ingest.parse_session(path)
        sid = os.path.basename(path)[:-6]
        pid, pname, ppath = resolve_project(s["cwd"], dirname, alias_paths, labels)
        proj_paths[pid].add(ppath)

        con.execute("DELETE FROM messages WHERE session_id=?", (sid,))
        con.execute("DELETE FROM episodes WHERE session_id=?", (sid,))
        con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", (
            sid, pid, ppath, s["branch"], s["title"],
            _iso(s["events"][0][0]) if s["events"] else None,
            _iso(s["events"][-1][0]) if s["events"] else None,
            s["n_user"], s["n_asst"], s["compactions"],
            s["out_tokens"], s["cache_read"]))
        stats["sessions"] += 1

        eps = ingest.segment(s["events"])
        spans = []
        for ep in eps:
            title = ingest.episode_title(ep, s["title"])
            opening = next((t for _, t in ep["prompts"]
                            if not ingest.is_machinery(t) and ingest._substantive(t)), "")
            title, r1 = scrub(title)
            opening, r2 = scrub(opening[:2000])
            body, r3 = scrub(ingest.episode_body(ep))
            stats["redactions"] += r1 + r2 + r3

            eid = stable_id(con, sid, ep["seq"])
            con.execute(
                "INSERT OR REPLACE INTO episodes (id,session_id,project_id,seq,started,ended,"
                "duration_s,n_prompts,n_tools,compactions,n_agents,out_tokens,cache_read,title,"
                "opening_prompt,branch,files_touched,tools) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, sid, pid, ep["seq"], _iso(ep["started"]), _iso(ep["ended"]),
                 int((ep["ended"] - ep["started"]).total_seconds()),
                 sum(1 for _, t in ep["prompts"] if not ingest.is_machinery(t)),
                 sum(ep["tools"].values()), ep["compactions"],
                 ep["agents"], ep["out_tokens"], ep["cache_read"], title, opening,
                 s["branch"], json.dumps(sorted(ep["files"])[:200]),
                 json.dumps(dict(ep["tools"].most_common(30)))))
            spans.append((eid, ep["started"], ep["ended"]))

            roots = collections.Counter()
            for f in ep["files"]:
                rt = ingest.file_root(f)
                if rt and not any(rt == g or rt.startswith(g.rstrip("/") + "/") for g in ignore):
                    roots[rt] += 1
            con.execute("INSERT OR REPLACE INTO episode_projects VALUES (?,?,?,?,?)",
                        (eid, pid, "cwd", 0, ppath))
            for rt, cnt in roots.items():
                rpid, _, _ = resolve_project(rt, dirname, alias_paths, labels)
                con.execute("INSERT OR REPLACE INTO episode_projects VALUES (?,?,?,?,?)",
                            (eid, rpid, "files", cnt, rt))
                proj_paths[rpid].add(rt)
            con.execute("DELETE FROM episodes_fts WHERE rowid=?", (eid,))
            con.execute("INSERT INTO episodes_fts (rowid,title,opening_prompt,body) VALUES (?,?,?,?)",
                        (eid, title, opening, body))

            rows = []
            for d, t in ep["prompts"]:
                c, r = scrub(t)
                stats["redactions"] += r
                kind = "meta" if ingest.is_machinery(t) else "prompt"
                rows.append((eid, sid, "user", _iso(d), c, kind))
            con.executemany("INSERT INTO messages (episode_id,session_id,role,ts,text,kind) "
                            "VALUES (?,?,?,?,?,?)", rows)
            stats["messages"] += sum(1 for r in rows if r[5] == "prompt")
            stats["episodes"] += 1

        session_spans[sid] = spans
        con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
                    (path, st.st_size, st.st_mtime, sid, "session",
                     datetime.datetime.now().isoformat(timespec="seconds")))
        con.commit()

    # ---- agent runs ---------------------------------------------------
    if not session_spans:
        session_spans = {}
        for r in con.execute("SELECT id,session_id,started,ended FROM episodes"):
            session_spans.setdefault(r["session_id"], []).append(
                (r["id"], ingest._dt(r["started"]), ingest._dt(r["ended"])))

    for dirname, sid, wf, path in agent_files:
        st = os.stat(path)
        if not rebuild and done.get(path) == (st.st_size, st.st_mtime):
            continue
        a = ingest.parse_agent(path)
        eid = None
        for cand, s0, s1 in session_spans.get(sid, []):
            if a["started"] and s0 and s1 and s0 <= a["started"] <= s1 + datetime.timedelta(hours=2):
                eid = cand
                break
        row = con.execute("SELECT project_id FROM sessions WHERE id=?", (sid,)).fetchone()
        pid = row["project_id"] if row else os.path.basename(ingest.decode_dirname(dirname))
        atype = "workflow" if wf else "subagent"
        prompt, r1 = scrub(a["prompt"])
        result, r2 = scrub(a["result"])
        stats["redactions"] += r1 + r2

        aid = stable_agent_id(con, path)
        con.execute(
            "INSERT OR REPLACE INTO agent_runs (id,episode_id,session_id,project_id,path,"
            "agent_type,workflow_id,ts,ended,prompt,result,n_tools,out_tokens) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, eid, sid, pid, path, atype, wf, _iso(a["started"]), _iso(a["ended"]),
             prompt, result, a["n_tools"], a["out_tokens"]))
        con.execute("DELETE FROM agents_fts WHERE rowid=?", (aid,))
        con.execute("INSERT INTO agents_fts (rowid,prompt,result) VALUES (?,?,?)",
                    (aid, prompt, result))
        con.execute("INSERT OR REPLACE INTO files VALUES (?,?,?,?,?,?)",
                    (path, st.st_size, st.st_mtime, sid, "agent",
                     datetime.datetime.now().isoformat(timespec="seconds")))
        stats["agents"] += 1
    con.commit()

    for pid, paths in proj_paths.items():
        prev = con.execute("SELECT paths FROM projects WHERE id=?", (pid,)).fetchone()
        allp = set(json.loads(prev["paths"])) if prev else set()
        allp |= paths
        con.execute("INSERT OR REPLACE INTO projects VALUES (?,?,?)",
                    (pid, labels.get(pid, pid), json.dumps(sorted(allp))))
    from . import recall
    try:
        stats["vocab"] = recall.build_vocab(con)
    except Exception:
        stats["vocab"] = 0
    fold_descendants(con)

    con.execute("INSERT OR REPLACE INTO meta VALUES ('last_index',?)",
                (datetime.datetime.now().isoformat(timespec="seconds"),))
    con.commit()
    return stats


def fold_descendants(con):
    """A session run in <project>/subdir is the same project, not a new one."""
    rows = con.execute("SELECT id,cwd FROM sessions WHERE cwd IS NOT NULL").fetchall()
    roots = {}
    for r in rows:
        pid = con.execute("SELECT project_id FROM sessions WHERE id=?", (r["id"],)).fetchone()["project_id"]
        roots.setdefault(r["cwd"], pid)
    merged = {}
    for path, pid in roots.items():
        best, target = -1, None
        for other, opid in roots.items():
            if other != path and path.startswith(other.rstrip("/") + "/") and len(other) > best:
                best, target = len(other), opid
        if target and target != pid:
            merged[pid] = target
    for src, dst in merged.items():
        old = con.execute("SELECT paths FROM projects WHERE id=?", (src,)).fetchone()
        new = con.execute("SELECT paths FROM projects WHERE id=?", (dst,)).fetchone()
        if old:
            merged_paths = sorted(set(json.loads(old["paths"] or "[]"))
                                  | set(json.loads(new["paths"] or "[]") if new else []))
            con.execute("UPDATE projects SET paths=? WHERE id=?",
                        (json.dumps(merged_paths), dst))
        for t in ("sessions", "episodes", "agent_runs"):
            con.execute(f"UPDATE {t} SET project_id=? WHERE project_id=?", (dst, src))
        con.execute("UPDATE OR REPLACE episode_projects SET project_id=? WHERE project_id=?",
                    (dst, src))
        con.execute("DELETE FROM projects WHERE id=?", (src,))
    con.commit()
    return merged
