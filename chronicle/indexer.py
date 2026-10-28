import os, json, time, datetime, collections
from .config import SOURCE, load_aliases, load_ignore_roots, load_containers
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


def _alias_match(path, alias_paths):
    """Longest-prefix match of `path` against aliases.json entries.

    Returns (target_project_id, matched_prefix), or (None, None) if nothing
    in `alias_paths` is an ancestor of (or equal to) `path`.
    """
    return _longest_prefix_match(path, alias_paths)


def resolve_project(cwd, dirname, alias_paths, labels):
    path = cwd or ingest.decode_dirname(dirname)
    name, _ = _alias_match(path, alias_paths)
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
        # a re-index rewrites these rows from scratch; left in place, a row that
        # fold_known_roots() already moved collides with its unfolded twin
        con.execute("DELETE FROM episode_projects WHERE episode_id IN "
                    "(SELECT id FROM episode_ids WHERE session_id=?)", (sid,))
        con.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
            sid, pid, ppath, s["branch"], s["title"],
            _iso(s["events"][0][0]) if s["events"] else None,
            _iso(s["events"][-1][0]) if s["events"] else None,
            s["n_user"], s["n_asst"], s["compactions"],
            s["out_tokens"], s["cache_read"], s["in_tokens"], s["cache_write"]))
        stats["sessions"] += 1

        eps = ingest.segment(s["events"])
        if not eps and s.get("commands") and s["events"]:
            # a command-only session (no substantive prompt ever anchors an episode)
            # would otherwise drop its command tally on the floor entirely
            eps = [{"seq": 1, "started": s["events"][0][0], "ended": s["events"][-1][0],
                    "prompts": [], "asst": [], "tools": collections.Counter(),
                    "files": set(), "agents": 0, "compactions": s["compactions"],
                    "out_tokens": s["out_tokens"], "cache_read": s["cache_read"],
                    "in_tokens": s["in_tokens"], "cache_write": s["cache_write"]}]
        if eps and s.get("commands"):
            eps[0]["tools"].update({f"cmd:/{k}": v for k, v in s["commands"].items()})
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
                "opening_prompt,branch,files_touched,tools,in_tokens,cache_write) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, sid, pid, ep["seq"], _iso(ep["started"]), _iso(ep["ended"]),
                 int((ep["ended"] - ep["started"]).total_seconds()),
                 sum(1 for _, t in ep["prompts"] if not ingest.is_machinery(t)),
                 sum(ep["tools"].values()), ep["compactions"],
                 ep["agents"], ep["out_tokens"], ep["cache_read"], title, opening,
                 s["branch"], json.dumps(sorted(ep["files"])[:200]),
                 json.dumps(dict(ep["tools"].most_common(30))),
                 ep["in_tokens"], ep["cache_write"]))
            spans.append((eid, ep["started"], ep["ended"]))

            # Per-file project resolution, in priority order: (1) a known
            # session cwd wins via fold_known_roots() below, once the full
            # corpus of cwds for this build run is known; (2) an alias
            # matched against the file's real, untruncated path; (3) the
            # file_root() truncation heuristic, as a last resort.
            file_groups = collections.Counter()
            for f in ep["files"]:
                rt = ingest.file_root(f)
                if not rt or any(f == g or f.startswith(g.rstrip("/") + "/") for g in ignore):
                    continue
                target, prefix = _alias_match(f, alias_paths)
                if target:
                    file_groups[(target, prefix)] += 1
                else:
                    # Store the untruncated file path, not just the file_root()
                    # guess: fold_known_roots() below can only correct this row
                    # to a known session cwd if root is at least as deep as
                    # that cwd, which the file_root() truncation often is not.
                    rpid, _, _ = resolve_project(rt, dirname, alias_paths, labels)
                    file_groups[(rpid, f)] += 1
            con.execute("INSERT OR REPLACE INTO episode_projects VALUES (?,?,?,?,?)",
                        (eid, pid, "cwd", 0, ppath))
            for (rpid, display_root), cnt in file_groups.items():
                con.execute("INSERT OR REPLACE INTO episode_projects VALUES (?,?,?,?,?)",
                            (eid, rpid, "files", cnt, display_root))
                proj_paths[rpid].add(display_root)
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
            "agent_type,workflow_id,ts,ended,prompt,result,n_tools,out_tokens,in_tokens,"
            "cache_read,cache_write) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (aid, eid, sid, pid, path, atype, wf, _iso(a["started"]), _iso(a["ended"]),
             prompt, result, a["n_tools"], a["out_tokens"], a["in_tokens"],
             a["cache_read"], a["cache_write"]))
        con.execute("DELETE FROM agents_fts WHERE rowid=?", (aid,))
        con.execute("INSERT INTO agents_fts (rowid,prompt,result) VALUES (?,?,?)",
                    (aid, prompt, result))
        if eid and a.get("tools"):
            cur = con.execute("SELECT tools FROM episodes WHERE id=?", (eid,)).fetchone()
            if cur:
                merged = json.loads(cur["tools"] or "{}")
                for k, v in a["tools"].items():
                    merged[f"sub:{k}"] = merged.get(f"sub:{k}", 0) + v
                con.execute("UPDATE episodes SET tools=?, n_tools=? WHERE id=?",
                            (json.dumps(merged), sum(merged.values()), eid))
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
    fold_known_roots(con)

    con.execute("INSERT OR REPLACE INTO meta VALUES ('last_index',?)",
                (datetime.datetime.now().isoformat(timespec="seconds"),))
    con.commit()
    return stats


def reattribute(con):
    """Re-run project attribution over what the index already holds, reading
    no transcript: after an aliases.json edit, or to undo a fold an older
    chronicle got wrong. Only project ids move — episode and agent ids don't."""
    alias_paths, labels = load_aliases()
    ignore = load_ignore_roots()
    before = {r["id"]: r["project_id"] for r in con.execute("SELECT id, project_id FROM sessions")}
    n_projects = con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    proj_paths = collections.defaultdict(set)
    for r in con.execute("SELECT id, cwd FROM sessions WHERE cwd IS NOT NULL").fetchall():
        pid, _, ppath = resolve_project(r["cwd"], None, alias_paths, labels)
        proj_paths[pid].add(ppath)
        con.execute("UPDATE sessions SET project_id=? WHERE id=?", (pid, r["id"]))
        for t in ("episodes", "agent_runs"):
            con.execute(f"UPDATE {t} SET project_id=? WHERE session_id=?", (pid, r["id"]))

    # episode_projects is rewritten the way build() writes it; rows whose
    # episode has no session are left alone
    owner = {r["id"]: (r["project_id"], r["cwd"]) for r in con.execute(
        "SELECT e.id, s.project_id, s.cwd FROM episodes e JOIN sessions s ON s.id=e.session_id "
        "WHERE s.cwd IS NOT NULL")}
    files = collections.Counter()
    for r in con.execute("SELECT * FROM episode_projects WHERE source='files'").fetchall():
        f, eid = r["root"], r["episode_id"]
        if eid not in owner or any(f == g or f.startswith(g.rstrip("/") + "/") for g in ignore):
            continue
        target, prefix = _alias_match(f, alias_paths)
        if target:
            files[(eid, target, prefix)] += r["n_files"]
        elif rt := ingest.file_root(f):
            files[(eid, resolve_project(rt, None, alias_paths, labels)[0], f)] += r["n_files"]
    for eid, (pid, cwd) in owner.items():
        con.execute("DELETE FROM episode_projects WHERE episode_id=?", (eid,))
        con.execute("INSERT OR REPLACE INTO episode_projects VALUES (?,?,?,?,?)",
                    (eid, pid, "cwd", 0, cwd))
    for (eid, pid, root), cnt in files.items():
        con.execute("INSERT OR REPLACE INTO episode_projects VALUES (?,?,?,?,?)",
                    (eid, pid, "files", cnt, root))
        proj_paths[pid].add(root)

    for pid, paths in proj_paths.items():
        con.execute("INSERT OR REPLACE INTO projects VALUES (?,?,?)",
                    (pid, labels.get(pid, pid), json.dumps(sorted(paths))))
    fold_descendants(con)
    fold_known_roots(con)
    con.execute(
        "DELETE FROM projects WHERE id NOT IN (SELECT project_id FROM ("
        "SELECT project_id FROM sessions UNION SELECT project_id FROM episodes "
        "UNION SELECT project_id FROM agent_runs UNION SELECT project_id FROM episode_projects"
        ") WHERE project_id IS NOT NULL)")
    con.commit()
    after = {r["id"]: r["project_id"] for r in con.execute("SELECT id, project_id FROM sessions")}
    return {"sessions": len(after), "moved": sum(before.get(k) != v for k, v in after.items()),
            "projects_before": n_projects,
            "projects": con.execute("SELECT COUNT(*) FROM projects").fetchone()[0]}


def _longest_prefix_match(path, candidates, exclude_self=False):
    """Longest directory-prefix match of `path` against `candidates` keys.

    `candidates` maps a directory path to some value (e.g. a project id).
    Returns (value, matched_key) for the longest key that is an ancestor of
    (or equal to) `path`, or (None, None) if nothing matches.
    """
    best, target, key = -1, None, None
    for other, val in candidates.items():
        if exclude_self and other == path:
            continue
        if (other == path or path.startswith(other.rstrip("/") + "/")) and len(other) > best:
            best, target, key = len(other), val, other
    return target, key


def _merge_project_paths(con, src, dst):
    """Union `projects.paths` of `src` into `dst`, leaving `dst`'s row updated."""
    old = con.execute("SELECT paths FROM projects WHERE id=?", (src,)).fetchone()
    new = con.execute("SELECT paths FROM projects WHERE id=?", (dst,)).fetchone()
    if old:
        merged_paths = sorted(set(json.loads(old["paths"] or "[]"))
                              | set(json.loads(new["paths"] or "[]") if new else []))
        con.execute("UPDATE projects SET paths=? WHERE id=?",
                    (json.dumps(merged_paths), dst))


def _is_container_dir(path, containers):
    """Is `path` a folder that holds projects rather than being one?

    A session started in `~` or `~/github` still belongs to some project, but
    the folder can't be a root other sessions fold into — or every repo below
    it ends up in one project."""
    parts = path.rstrip("/").split("/")
    if len(parts) <= (3 if parts[1:2] == ["Users"] else 2):
        return True
    return parts[-1] in containers


def fold_descendants(con):
    """A session run in <project>/subdir is the same project, not a new one."""
    containers = load_containers()
    rows = con.execute("SELECT id,cwd FROM sessions WHERE cwd IS NOT NULL").fetchall()
    roots = {}
    for r in rows:
        if _is_container_dir(r["cwd"], containers):
            continue
        pid = con.execute("SELECT project_id FROM sessions WHERE id=?", (r["id"],)).fetchone()["project_id"]
        roots.setdefault(r["cwd"], pid)
    merged = {}
    for path, pid in roots.items():
        target, _ = _longest_prefix_match(path, roots, exclude_self=True)
        if target and target != pid:
            merged[pid] = target
    for src, dst in merged.items():
        _merge_project_paths(con, src, dst)
        for t in ("sessions", "episodes", "agent_runs"):
            con.execute(f"UPDATE {t} SET project_id=? WHERE project_id=?", (dst, src))
        con.execute("UPDATE OR REPLACE episode_projects SET project_id=? WHERE project_id=?",
                    (dst, src))
        con.execute("DELETE FROM projects WHERE id=?", (src,))
    con.commit()
    return merged


def fold_known_roots(con):
    """A touched file under any cwd chronicle has ever seen as a session's
    own working directory belongs to that project — no aliases.json entry
    required. This outranks alias-based resolution (mechanism 2), so it
    runs as a post-pass after per-episode file resolution has already
    written `episode_projects` rows, and after `fold_descendants()` has
    canonicalized descendant sessions' project ids.
    """
    containers = load_containers()
    known = {}
    for r in con.execute("SELECT DISTINCT cwd, project_id FROM sessions WHERE cwd IS NOT NULL"):
        if not _is_container_dir(r["cwd"], containers):
            known.setdefault(r["cwd"], r["project_id"])
    rows = con.execute(
        "SELECT episode_id, project_id, root FROM episode_projects WHERE source='files'"
    ).fetchall()
    merged = collections.defaultdict(set)
    for row in rows:
        target, _ = _longest_prefix_match(row["root"], known)
        if target and target != row["project_id"]:
            con.execute(
                "UPDATE OR REPLACE episode_projects SET project_id=? "
                "WHERE episode_id=? AND project_id=? AND root=?",
                (target, row["episode_id"], row["project_id"], row["root"]))
            merged[row["project_id"]].add(target)
    for src, dsts in merged.items():
        for dst in dsts:
            _merge_project_paths(con, src, dst)
        still_used = con.execute(
            "SELECT 1 FROM sessions WHERE project_id=? "
            "UNION SELECT 1 FROM episodes WHERE project_id=? "
            "UNION SELECT 1 FROM agent_runs WHERE project_id=? "
            "UNION SELECT 1 FROM episode_projects WHERE project_id=? LIMIT 1",
            (src, src, src, src)).fetchone()
        if not still_used:
            con.execute("DELETE FROM projects WHERE id=?", (src,))
    con.commit()
    return merged
