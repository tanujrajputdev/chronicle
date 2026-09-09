"""Self-check. Answers "is my install actually working?" without guesswork.

Every check states what it looked at and what to do when it fails. Nothing here
mutates anything; running doctor is always safe.
"""
import os, sys, json, time, sqlite3, socket, subprocess, datetime, pathlib, re

from .config import DB_PATH, SOURCE, ROOT, INSTALL_DIR, ALIASES_PATH

OK, WARN, FAIL = "ok", "warn", "fail"


class Report:
    def __init__(self):
        self.rows = []

    def add(self, section, status, label, detail="", fix=""):
        self.rows.append((section, status, label, detail, fix))

    def counts(self):
        c = {OK: 0, WARN: 0, FAIL: 0}
        for r in self.rows:
            c[r[1]] += 1
        return c


def _size(p):
    try:
        return os.path.getsize(p)
    except OSError:
        return 0


def _human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{u}"
        n /= 1024
    return f"{n:.1f}TB"


def _ago(ts):
    d = time.time() - ts
    if d < 60: return f"{int(d)}s ago"
    if d < 3600: return f"{int(d/60)}m ago"
    if d < 86400: return f"{int(d/3600)}h ago"
    return f"{int(d/86400)}d ago"


# ---------------------------------------------------------------- checks

def check_environment(r):
    v = sys.version_info
    if v >= (3, 11):
        r.add("Environment", OK, "python", f"{v.major}.{v.minor}.{v.micro}")
    else:
        r.add("Environment", FAIL, "python", f"{v.major}.{v.minor}", "Chronicle needs 3.11+")

    try:
        con = sqlite3.connect(":memory:")
        con.execute("CREATE VIRTUAL TABLE t USING fts5(a)")
        ver = con.execute("SELECT sqlite_version()").fetchone()[0]
        r.add("Environment", OK, "sqlite", f"{ver} with FTS5")
    except Exception as e:
        r.add("Environment", FAIL, "sqlite FTS5", str(e),
              "Your Python's sqlite3 was built without FTS5; install a build that has it")

    if not SOURCE.exists():
        r.add("Environment", FAIL, "session source", str(SOURCE),
              "Claude Code has not written any sessions here yet")
        return
    files, total = 0, 0
    for root, _, names in os.walk(SOURCE):
        for n in names:
            if n.endswith(".jsonl"):
                files += 1
                total += _size(os.path.join(root, n))
    r.add("Environment", OK if files else WARN, "session source",
          f"{files} transcripts, {_human(total)} in {SOURCE}",
          "" if files else "Nothing to index yet — use Claude Code, then run index")


def check_index(r):
    if not DB_PATH.exists():
        r.add("Index", FAIL, "database", "not created",
              "Run: ./chronicle-cli index")
        return None
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    r.add("Index", OK, "database", f"{_human(_size(DB_PATH))} at {DB_PATH}")

    try:
        e = con.execute("SELECT COUNT(*) n, SUM(n_prompts) p FROM episodes").fetchone()
        a = con.execute("SELECT COUNT(*) n FROM agent_runs").fetchone()["n"]
        pj = con.execute("SELECT COUNT(DISTINCT project_id) n FROM episode_projects").fetchone()["n"]
        if e["n"]:
            r.add("Index", OK, "contents",
                  f"{e['n']} episodes · {e['p']} prompts · {a} agent runs · {pj} projects")
        else:
            r.add("Index", WARN, "contents", "empty", "Run: ./chronicle-cli index")
    except Exception as ex:
        r.add("Index", FAIL, "contents", str(ex), "The schema may be stale — run index --rebuild")
        return con

    row = con.execute("SELECT v FROM meta WHERE k='last_index'").fetchone()
    if row:
        try:
            age = (datetime.datetime.now()
                   - datetime.datetime.fromisoformat(row["v"])).total_seconds()
            status = OK if age < 86400 else WARN
            r.add("Index", status, "freshness", f"last indexed {_ago(time.time() - age)}",
                  "" if status == OK else "Run: ./chronicle-cli index")
        except Exception:
            r.add("Index", WARN, "freshness", row["v"])
    else:
        r.add("Index", WARN, "freshness", "never", "Run: ./chronicle-cli index")

    try:
        ids = con.execute("SELECT COUNT(*) n FROM episode_ids").fetchone()["n"]
        aids = con.execute("SELECT COUNT(*) n FROM agent_ids").fetchone()["n"]
        orphan = con.execute(
            "SELECT COUNT(*) n FROM episodes e "
            "LEFT JOIN episode_ids i ON i.session_id=e.session_id AND i.seq=e.seq "
            "WHERE i.id IS NULL").fetchone()["n"]
        r.add("Index", OK if not orphan else FAIL, "stable references",
              f"{ids} episode ids, {aids} agent ids reserved"
              + (f", {orphan} episodes with no reserved id" if orphan else ""),
              "" if not orphan else "Reference stability is broken — please open an issue")
    except Exception as ex:
        r.add("Index", WARN, "stable references", str(ex))

    t0 = time.time()
    try:
        n = len(con.execute(
            "SELECT rowid FROM episodes_fts WHERE episodes_fts MATCH 'the' LIMIT 20").fetchall())
        ms = (time.time() - t0) * 1000
        r.add("Index", OK, "search", f"{n} hits in {ms:.0f}ms")
    except Exception as ex:
        r.add("Index", FAIL, "search", str(ex), "Run: ./chronicle-cli index --rebuild")

    try:
        terms = con.execute("SELECT COUNT(*) n FROM terms").fetchone()["n"]
        eterms = con.execute("SELECT COUNT(*) n FROM episode_terms").fetchone()["n"]
        status = OK if terms and eterms else WARN
        r.add("Index", status, "recall vocabulary",
              f"{terms} terms over {eterms} episodes",
              "" if status == OK else "Run: ./chronicle-cli index --rebuild")
    except Exception:
        r.add("Index", WARN, "recall vocabulary", "not built",
              "Run: ./chronicle-cli index --rebuild")
    return con


def check_capture(r):
    settings = pathlib.Path.home() / ".claude" / "settings.json"
    try:
        s = json.loads(settings.read_text())
    except Exception:
        r.add("Capture", WARN, "hooks", "no settings.json",
              "Run: ./chronicle-cli install")
        return
    ours, foreign = [], 0
    for ev, groups in (s.get("hooks") or {}).items():
        for g in groups:
            for h in g.get("hooks", []):
                if "chronicle" in json.dumps(h):
                    ours.append(ev)
                else:
                    foreign += 1
    want = {"SessionStart", "PreCompact", "SessionEnd", "UserPromptSubmit"}
    missing = want - set(ours)
    r.add("Capture", OK if not missing else WARN, "hooks",
          f"{len(ours)} installed: {', '.join(sorted(set(ours))) or 'none'}"
          + (f" · {foreign} from other tools untouched" if foreign else ""),
          "" if not missing else f"Missing {', '.join(sorted(missing))} — run: ./chronicle-cli install")

    launcher = INSTALL_DIR / "chronicle-hook"
    if launcher.exists() and os.access(launcher, os.X_OK):
        r.add("Capture", OK, "hook launcher", str(launcher))
    else:
        r.add("Capture", WARN if not launcher.exists() else FAIL, "hook launcher",
              "missing" if not launcher.exists() else "not executable",
              "Run: ./chronicle-cli install")

    if sys.platform == "darwin":
        out = subprocess.run(["launchctl", "list"], capture_output=True, text=True).stdout
        if "ai.chronicle.index" in out:
            r.add("Capture", OK, "background indexer", "loaded, every 30 min")
        else:
            r.add("Capture", WARN, "background indexer", "not loaded",
                  "Run: ./chronicle-cli install (hooks still cover you)")
    else:
        r.add("Capture", WARN, "background indexer", f"unsupported on {sys.platform}",
              "macOS only — the hooks cover the same ground")

    # the bug this catches: hooks run with the user's project as cwd, so a launcher that
    # resolves anything from the current directory works only inside the install folder
    try:
        out = subprocess.run([str(launcher), "sessionstart"], input='{"session_id":"doctor",'
                             '"cwd":"/","trigger":"startup"}', capture_output=True, text=True,
                             cwd="/", timeout=30)
        if out.returncode == 0 and "Traceback" not in out.stderr and "ModuleNotFound" not in out.stderr:
            r.add("Capture", OK, "hooks run anywhere", "launcher works with an unrelated cwd")
        else:
            r.add("Capture", FAIL, "hooks run anywhere",
                  (out.stderr.strip().splitlines() or ["exit " + str(out.returncode)])[-1][:90],
                  "Run: ./chronicle-cli install")
    except Exception as ex:
        r.add("Capture", FAIL, "hooks run anywhere", str(ex)[:90], "Run: ./chronicle-cli install")

    log = ROOT / "hooks.log"
    if log.exists():
        cutoff = datetime.date.today() - datetime.timedelta(days=7)
        lines = [l for l in log.read_text(errors="ignore").splitlines() if l[:10] >= cutoff.isoformat()]
        kinds = {}
        for l in lines:
            parts = l.split()
            if len(parts) > 1:
                kinds[parts[1]] = kinds.get(parts[1], 0) + 1
        r.add("Capture", OK if lines else WARN, "recent activity",
              f"{len(lines)} firings in 7 days: "
              + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())) if lines
              else "no hook has fired in 7 days",
              "" if lines else "Restart Claude Code, then use it once")
    else:
        r.add("Capture", WARN, "recent activity", "no log yet",
              "Restart Claude Code after install, then use it once")

    cps = list((ROOT / "checkpoints").glob("*/*.md")) if (ROOT / "checkpoints").exists() else []
    r.add("Capture", OK if cps else WARN, "compaction rescue",
          f"{len(cps)} checkpoint(s) saved" if cps else "none yet",
          "" if cps else "Written automatically at your next compaction")


def check_access(r):
    skill = pathlib.Path.home() / ".claude" / "skills" / "chronicle" / "SKILL.md"
    if skill.exists():
        body = skill.read_text()
        good = str(INSTALL_DIR) in body and "{{" not in body
        r.add("Access", OK if good else FAIL, "skill",
              str(skill) + ("" if good else " — placeholders not rendered"),
              "" if good else "Run: ./chronicle-cli install")
    else:
        r.add("Access", WARN, "skill", "not installed", "Run: ./chronicle-cli install")

    try:
        s = json.loads((pathlib.Path.home() / ".claude" / "settings.json").read_text())
        entry = (s.get("mcpServers") or {}).get("chronicle")
    except Exception:
        entry = None
    if not entry:
        r.add("Access", WARN, "mcp server", "not registered",
              "Optional — run: ./chronicle-cli install --mcp")
    else:
        try:
            p = subprocess.Popen([sys.executable, "-m", "chronicle.mcp"], cwd=str(INSTALL_DIR),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.DEVNULL, text=True,
                                 env={**os.environ, "PYTHONPATH": str(INSTALL_DIR)})
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                                      "params": {"protocolVersion": "2025-06-18",
                                                 "capabilities": {}}}) + "\n")
            p.stdin.flush()
            reply = json.loads(p.stdout.readline())
            p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n")
            p.stdin.flush()
            tools = json.loads(p.stdout.readline())["result"]["tools"]
            p.stdin.close()
            r.add("Access", OK, "mcp server",
                  f"responds, protocol {reply['result']['protocolVersion']}, {len(tools)} tools")
        except Exception as ex:
            r.add("Access", FAIL, "mcp server", f"registered but not responding: {ex}",
                  "Run: ./chronicle-cli install --mcp")

    import shutil as _sh
    which = _sh.which("chronicle")
    if which:
        try:
            resolved = pathlib.Path(which).resolve()
            good = resolved == (INSTALL_DIR / "chronicle-cli").resolve()
        except OSError:
            good = False
        if good:
            probe = subprocess.run([which, "stats"], capture_output=True, text=True, cwd="/")
            if probe.returncode == 0:
                r.add("Access", OK, "chronicle on PATH", f"{which} works from any directory")
            else:
                r.add("Access", FAIL, "chronicle on PATH",
                      (probe.stderr.strip().splitlines() or ["failed"])[-1][:90],
                      "Run: ./chronicle-cli install")
        else:
            r.add("Access", WARN, "chronicle on PATH",
                  f"{which} points somewhere else", "Another tool owns that name")
    else:
        r.add("Access", WARN, "chronicle on PATH", "not linked",
              f"Run: ./chronicle-cli install, or ln -s {INSTALL_DIR}/chronicle-cli ~/.local/bin/chronicle")

    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 7777))
        r.add("Access", OK, "dashboard port", "7777 free")
    except OSError:
        r.add("Access", WARN, "dashboard port", "7777 in use",
              "Use: ./chronicle-cli serve -p 7788")
    finally:
        s.close()


def check_safety(r, con):
    from .redact import scrub
    probes = [
        ("sk-ant-api03-" + "a" * 40, "ANTHROPIC_KEY"),
        ("ghp_" + "b" * 36, "GITHUB_PAT"),
        ("shpat_" + "c" * 32, "SHOPIFY_TOKEN"),
        ("AKIA" + "D" * 16, "AWS_KEY"),
        ("AIza" + "e" * 35, "GOOGLE_KEY"),
        ("xoxb-" + "1" * 24, "SLACK_TOKEN"),
        ('"password": "hunter2secret"', "PASSWORD"),
        ("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.dBjftJeZ4CVP", "JWT"),
    ]
    caught = sum(1 for text, label in probes if f"[REDACTED:{label}]" in scrub(text)[0])
    r.add("Safety", OK if caught == len(probes) else FAIL, "secret redaction",
          f"{caught}/{len(probes)} patterns caught",
          "" if caught == len(probes) else "Please open an issue — this must be 100%")

    src = "".join(p.read_text() for p in (INSTALL_DIR / "chronicle").glob("*.py"))
    outbound = re.findall(r"\b(urllib\.request|requests\.|httpx\.|socket\.create_connection|"
                          r"http\.client\.HTTP)", src)
    r.add("Safety", OK if not outbound else FAIL, "no network calls",
          "no outbound client in any module" if not outbound else f"found {set(outbound)}",
          "" if not outbound else "Chronicle must never make a network call")

    gi = INSTALL_DIR / ".gitignore"
    protected = {"aliases.json", "projects/", "MEMORY.md"}
    if gi.exists():
        have = set(gi.read_text().split())
        missing = protected - have
        r.add("Safety", OK if not missing else WARN, "gitignore",
              "aliases.json, projects/, MEMORY.md protected" if not missing
              else f"missing: {', '.join(sorted(missing))}",
              "" if not missing else "Add them before committing")
    else:
        r.add("Safety", WARN, "gitignore", "absent",
              "Only matters if this directory is a git repo")

    if con:
        try:
            leaked = con.execute(
                "SELECT COUNT(*) n FROM messages WHERE text LIKE 'sk-ant-%' "
                "OR text LIKE 'ghp\\_%' ESCAPE '\\'").fetchone()["n"]
            red = con.execute("SELECT COUNT(*) n FROM messages "
                              "WHERE text LIKE '%[REDACTED:%'").fetchone()["n"]
            r.add("Safety", OK if not leaked else FAIL, "index is clean",
                  f"{red} redaction(s) stored, {leaked} live-looking key(s) present",
                  "" if not leaked else "Run: ./chronicle-cli index --rebuild")
        except Exception:
            pass


# ---------------------------------------------------------------- entry

def run(verbose=False):
    r = Report()
    check_environment(r)
    con = check_index(r)
    check_capture(r)
    check_access(r)
    check_safety(r, con)
    return r
