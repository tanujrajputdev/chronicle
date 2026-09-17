"""End-to-end self test. Builds a synthetic corpus, runs everything against it,
and asserts on known-correct answers.

Two rules this file exists to enforce:
  1. No command may ever raise. A traceback reaching the user is a failure.
  2. Segmentation, attribution and redaction must produce the *expected* result,
     not merely "some" result.

Nothing here touches the real index, the real transcripts, or settings.json —
every run happens inside a throwaway directory.
"""
import os, sys, json, time, shutil, sqlite3, subprocess, tempfile, datetime, traceback

from .config import INSTALL_DIR

LAUNCHER = str(INSTALL_DIR / "chronicle-cli")


# ---------------------------------------------------------------- fixtures

def _ts(base, minutes):
    return (base + datetime.timedelta(minutes=minutes)).isoformat() + "Z"


def _asst(t, text=None, tools=(), out=100, compact=False):
    content = []
    if text:
        content.append({"type": "text", "text": text})
    for name, inp in tools:
        content.append({"type": "tool_use", "id": "t", "name": name, "input": inp})
    rec = {"type": "assistant", "timestamp": t,
           "message": {"role": "assistant", "model": "claude-opus-5",
                       "usage": {"output_tokens": out, "cache_read_input_tokens": 10},
                       "content": content}}
    if compact:
        rec["isCompactSummary"] = True
    return rec


def _user(t, text, cwd="/Users/x/proj-a"):
    return {"type": "user", "timestamp": t, "cwd": cwd, "gitBranch": "main",
            "message": {"role": "user", "content": text}}


def build_corpus(root):
    """A deterministic corpus with known-correct answers."""
    b = datetime.datetime(2026, 1, 5, 9, 0)
    src = os.path.join(root, "projects")

    # session A — two episodes (6h idle gap), work landing in two different repos
    d = os.path.join(src, "-Users-x-proj-a"); os.makedirs(d)
    rows = [
        _user(_ts(b, 0), "set up the invoicing module with recurring billing support"),
        _asst(_ts(b, 1), "Working on it.", [("Write", {"file_path": "/Users/x/proj-a/invoice.py"})]),
        _user(_ts(b, 10), "now wire it into the dashboard component as well"),
        _asst(_ts(b, 11), "Done.", [("Edit", {"file_path": "/Users/x/proj-b/dashboard.tsx"})]),
        # 6 hours later -> a second episode
        _user(_ts(b, 370), "the recurring billing cron is firing twice, please investigate"),
        _asst(_ts(b, 371), "Found it.", [("Edit", {"file_path": "/Users/x/proj-a/cron.py"})]),
        # harness machinery must not create an episode or become a title
        {"type": "user", "timestamp": _ts(b, 1200),
         "message": {"role": "user", "content": "<task-notification>\n<task-id>abc</task-id>\n</task-notification>"}},
        {"type": "ai-title", "aiTitle": "Set up invoicing"},
    ]
    open(os.path.join(d, "sess-a.jsonl"), "w").write(
        "\n".join(json.dumps(r) for r in rows) + "\n")

    # session B — one episode, a compaction, a secret, and a subagent transcript
    d2 = os.path.join(src, "-Users-x-proj-c"); os.makedirs(d2)
    rows = [
        _user(_ts(b, 0), "deploy with token ghp_" + "a" * 36 + " and confirm", "/Users/x/proj-c"),
        _asst(_ts(b, 1), "Deployed.", [("Bash", {"command": "deploy"})]),
        _asst(_ts(b, 2), "Summary of earlier work.", compact=True),
        _user(_ts(b, 5), "check the health endpoint responds correctly now", "/Users/x/proj-c"),
        _asst(_ts(b, 6), "It does.", [("Task", {"subagent_type": "Explore",
                                                "description": "look at deploys"})]),
    ]
    open(os.path.join(d2, "sess-b.jsonl"), "w").write(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    sub = os.path.join(d2, "sess-b", "subagents"); os.makedirs(sub)
    arows = [
        {"type": "user", "timestamp": _ts(b, 6), "isSidechain": True,
         "message": {"role": "user", "content": "Investigate the deployment pipeline thoroughly"}},
        {"type": "assistant", "timestamp": _ts(b, 8), "isSidechain": True,
         "message": {"role": "assistant", "usage": {"output_tokens": 500},
                     "content": [{"type": "text", "text": "The pipeline uses blue-green deploys."}]}},
    ]
    open(os.path.join(sub, "agent-1.jsonl"), "w").write(
        "\n".join(json.dumps(r) for r in arows) + "\n")

    # session C — hostile input: truncated json, junk, binary, an enormous line
    d3 = os.path.join(src, "-Users-x-proj-d"); os.makedirs(d3)
    with open(os.path.join(d3, "sess-c.jsonl"), "w", errors="ignore") as fh:
        fh.write(json.dumps(_user(_ts(b, 0), "a perfectly ordinary prompt about caching",
                                  "/Users/x/proj-d")) + "\n")
        fh.write('{"type":"user","timestamp":"2026-01-0\n')
        fh.write("\n\nnot json at all\n")
        fh.write("\x00\x01 binary\n")
        fh.write(json.dumps({"type": "user", "timestamp": _ts(b, 2),
                             "message": {"role": "user", "content": "y" * 150000}}) + "\n")
        fh.write(json.dumps({"type": "user"}) + "\n")
        fh.write(json.dumps({"type": "user", "timestamp": None, "message": None}) + "\n")
        fh.write(json.dumps({"type": "future_record_type", "timestamp": _ts(b, 3)}) + "\n")

    # session E — work done entirely through the shell. Edit and Write are
    # never called, so without shellwrite this episode has no files at all and
    # lands in no project. The file must really exist: attribution is gated on
    # it, so the corpus builds one.
    work = os.path.join(root, "shellproj")
    os.makedirs(work, exist_ok=True)
    open(os.path.join(work, "shell-made.md"), "w").write("# written by a heredoc\n")
    d5 = os.path.join(src, "-shellproj"); os.makedirs(d5, exist_ok=True)
    rows = [
        _user(_ts(b, 0), "write the launch notes into a markdown file", cwd=work),
        _asst(_ts(b, 1), "Writing them.", [
            ("Bash", {"command": "cat > shell-made.md <<'EOF'\n"
                                 "# notes\n> a quoted line with a caret\n"
                                 "echo nope > decoy-never-written.md\n"
                                 "EOF",
                      "description": "Write launch notes"})]),
    ]
    open(os.path.join(d5, "sess-e.jsonl"), "w").write(
        "\n".join(json.dumps(r) for r in rows) + "\n")

    # session F — a project with real history. The prompt path refuses to answer
    # a corpus under ten episodes, so without this every recall test would pass
    # for the wrong reason. Written the way people actually write: three-letter
    # acronyms nobody spells out, SQL keywords shouted mid-sentence, and one
    # genuinely rare vendor name that a question can reach for.
    d6 = os.path.join(src, "-Users-x-proj-e"); os.makedirs(d6, exist_ok=True)
    rows, cwd_e = [], "/Users/x/proj-e"
    script = [
        "set up the CRM inventory sync so every SKU lands in one stock dashboard table",
        "the CRM dashboard needs a vendor page - start with the Brightwell account",
        "export a CSV of every SKU we hold for Brightwell, grouped by warehouse",
        "the stock query should filter WHERE active = 1 AND archived IS NOT NULL",
        "add reorder thresholds to the SKU table and surface them on the CRM dashboard",
        # `crm` in lower case here on purpose: the term counts must pick it up in
        # episodes that never shouted it, which only works if the second pass
        # re-reads the corpus instead of reusing what the first pass capitalised
        "the CSV export drops stock rows when a SKU has no vendor - fix the crm join",
        "write the weekly stock digest as markdown, one dashboard section per warehouse AND vendor",
        "the dashboard chart should show thirty days of stock movement, NOT ninety",
    ]
    for i, text in enumerate(script):
        rows.append(_user(_ts(b, i * 300), text, cwd_e))          # 5h apart: one episode each
        rows.append(_asst(_ts(b, i * 300 + 2), "Done.",
                          [("Edit", {"file_path": f"/Users/x/proj-e/stock_{i}.py"})]))
    open(os.path.join(d6, "sess-f.jsonl"), "w").write(
        "\n".join(json.dumps(r) for r in rows) + "\n")

    # session G — the same subject, filed under a different project id. This is
    # the shape that makes people give up and search by hand: you are sitting in
    # one project and the work you are asking about was recorded in another.
    d7 = os.path.join(src, "-Users-x-proj-f"); os.makedirs(d7, exist_ok=True)
    rows = [
        _user(_ts(b, 30), "reconcile the Brightwell vendor invoices against the SKU ledger",
              "/Users/x/proj-f"),
        _asst(_ts(b, 32), "Reconciled.", [("Edit", {"file_path": "/Users/x/proj-f/ledger.py"})]),
    ]
    open(os.path.join(d7, "sess-g.jsonl"), "w").write(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    return src


# ---------------------------------------------------------------- harness

class Suite:
    def __init__(self, verbose=False):
        self.results = []
        self.verbose = verbose

    def check(self, name, fn):
        try:
            detail = fn()
            self.results.append((True, name, detail or ""))
        except AssertionError as e:
            self.results.append((False, name, str(e)))
        except Exception:
            self.results.append((False, name, traceback.format_exc().strip().splitlines()[-1]))

    @property
    def failed(self):
        return [r for r in self.results if not r[0]]


def run_cli(env, *args, expect_ok=True):
    r = subprocess.run([LAUNCHER, *args], capture_output=True, text=True, env=env, timeout=180)
    out = r.stdout + r.stderr
    assert "Traceback" not in out, f"`{' '.join(args)}` raised:\n{out[-400:]}"
    if expect_ok:
        assert r.returncode == 0, f"`{' '.join(args)}` exited {r.returncode}\n{out[-300:]}"
    return out


def run_hook_openpipe(env, event, payload, limit):
    """Run a hook the way Claude Code sometimes does: write the payload and keep
    the pipe open. `stdin.read()` blocks here forever — the hook must finish on
    its own, well inside the timeout `install` writes into settings.json.

    stdin is deliberately never closed, so `communicate()` cannot be used (it
    closes it, which would release a blocked read and hide the very bug this
    test exists to catch). Drain the output pipes on threads and wait instead.
    """
    import threading
    p = subprocess.Popen([str(INSTALL_DIR / "chronicle-hook"), event],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, env=env)
    sink = {}
    pumps = [threading.Thread(target=lambda k, f: sink.__setitem__(k, f.read()),
                              args=(k, f), daemon=True)
             for k, f in (("out", p.stdout), ("err", p.stderr))]
    for t in pumps:
        t.start()
    t0 = time.monotonic()
    try:
        p.stdin.write(payload if isinstance(payload, str) else json.dumps(payload))
        p.stdin.flush()                      # written — and the pipe stays open
        try:
            p.wait(timeout=limit)
        except subprocess.TimeoutExpired:
            raise AssertionError(
                f"hook {event} still running after {limit}s with stdin held open — "
                f"the session would stall and its output be discarded")
        for t in pumps:
            t.join(2)
    finally:
        if p.poll() is None:
            p.kill()
        for f in (p.stdin, p.stdout, p.stderr):
            try:
                f.close()
            except Exception:
                pass
    assert p.returncode == 0, f"hook {event} exited {p.returncode}: {sink.get('err','')[-200:]}"
    assert "Traceback" not in sink.get("err", ""), f"hook {event} raised:\n{sink.get('err','')[-300:]}"
    return sink.get("out", ""), time.monotonic() - t0


def run_hook(env, event, payload):
    r = subprocess.run([str(INSTALL_DIR / "chronicle-hook"), event],
                       input=json.dumps(payload) if isinstance(payload, dict) else payload,
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, f"hook {event} exited {r.returncode}: {r.stderr[-200:]}"
    assert "Traceback" not in r.stderr, f"hook {event} raised:\n{r.stderr[-300:]}"
    return r.stdout


# ---------------------------------------------------------------- the tests

def run(verbose=False):
    s = Suite(verbose)
    sandbox = tempfile.mkdtemp(prefix="chronicle-selftest-")
    try:
      try:
        src = build_corpus(sandbox)
        root = os.path.join(sandbox, "root")
        env = {**os.environ, "CHRONICLE_ROOT": root, "CHRONICLE_SOURCE": src,
               "CHRONICLE_MEMORY": os.path.join(sandbox, "mem"),
               "CHRONICLE_NO_BRIEF": "", "CHRONICLE_NO_RECALL": ""}
        os.makedirs(env["CHRONICLE_MEMORY"], exist_ok=True)
        db = os.path.join(root, "chronicle.db")

        def q(sql, args=()):
            con = sqlite3.connect(db); con.row_factory = sqlite3.Row
            try:
                return con.execute(sql, args).fetchall()
            finally:
                con.close()

        # ---- ingest correctness, on answers we know
        s.check("index builds", lambda: run_cli(env, "index") and None)

        def seg():
            n = q("SELECT COUNT(*) n FROM episodes WHERE session_id='sess-a'")[0]["n"]
            assert n == 2, f"expected 2 episodes from a 6h gap, got {n}"
        s.check("episodes split on the idle gap", seg)

        def machinery():
            rows = q("SELECT title FROM episodes")
            bad = [r["title"] for r in rows if "task-notification" in (r["title"] or "")
                   or (r["title"] or "").startswith("Abc")]
            assert not bad, f"harness machinery became a title: {bad}"
            n = q("SELECT COUNT(*) n FROM episodes WHERE session_id='sess-a'")[0]["n"]
            assert n == 2, f"machinery created an extra episode: {n}"
        s.check("machinery never becomes an episode", machinery)

        def title():
            t = q("SELECT title FROM episodes WHERE session_id='sess-a' ORDER BY seq")[0]["title"]
            assert "invoicing" in t.lower(), f"title came from the wrong prompt: {t!r}"
        s.check("title comes from the first real prompt", title)

        def cross():
            roots = {r["root"] for r in q(
                "SELECT root FROM episode_projects WHERE source='files'")}
            assert any("proj-b" in x for x in roots), f"cross-repo edit lost: {roots}"
        s.check("work is attributed to the repo it edited", cross)

        def agent():
            rows = q("SELECT prompt, result FROM agent_runs")
            assert len(rows) == 1, f"expected 1 agent run, got {len(rows)}"
            assert "deployment pipeline" in rows[0]["prompt"], "agent prompt lost"
            assert "blue-green" in rows[0]["result"], "agent conclusion lost"
        s.check("subagent transcripts are captured", agent)

        def compaction():
            n = q("SELECT SUM(compactions) n FROM episodes")[0]["n"] or 0
            assert n >= 1, "compaction not counted"
        s.check("compactions are counted", compaction)

        def redact():
            rows = q("SELECT text FROM messages WHERE text LIKE '%REDACTED%'")
            assert rows, "a github token survived ingest"
            live = q("SELECT COUNT(*) n FROM messages WHERE text LIKE 'ghp\\_%' ESCAPE '\\'")[0]["n"]
            assert live == 0, "a live-looking token is in the index"
        s.check("secrets are redacted at ingest", redact)

        def hostile():
            n = q("SELECT COUNT(*) n FROM episodes WHERE session_id='sess-c'")[0]["n"]
            assert n >= 1, "a file with junk lines produced nothing at all"
        s.check("malformed transcripts survive", hostile)

        # ---- stable ids
        def stable():
            before = {(r["session_id"], r["seq"]): r["id"] for r in
                      q("SELECT id, session_id, seq FROM episodes")}
            run_cli(env, "index", "--rebuild")
            after = {(r["session_id"], r["seq"]): r["id"] for r in
                     q("SELECT id, session_id, seq FROM episodes")}
            moved = {k: (before[k], after[k]) for k in before if k in after and before[k] != after[k]}
            assert not moved, f"episode ids moved across rebuild: {moved}"
        s.check("ids survive a rebuild", stable)

        # ---- every subcommand runs
        def _first(sql, what):
            rows = q(sql)
            assert rows, f"no {what} in the index — earlier ingest checks should have caught this"
            return rows[0]["id"]
        eid = aid = None
        s.check("index has episodes to query", lambda: _first("SELECT id FROM episodes LIMIT 1", "episodes"))
        s.check("index has an agent run to query", lambda: _first("SELECT id FROM agent_runs LIMIT 1", "agent runs"))
        try:
            eid = q("SELECT id FROM episodes LIMIT 1")[0]["id"]
            aid = q("SELECT id FROM agent_runs LIMIT 1")[0]["id"]
        except IndexError:
            eid, aid = 1, 1        # keep going; the checks above already recorded the failure
        simple = ["stats", "projects", "agents", "map", "doctor", "checkpoints",
                  "recall", "aliases", "digest", "index"]
        for c in simple:
            s.check(f"command: {c}", lambda c=c: run_cli(env, c, expect_ok=(c != "doctor")) and None)
        withargs = [("search", "invoicing"), ("project", "proj-a"), ("timeline", "proj-a"),
                    ("show", str(eid)), ("agent", str(aid)), ("brief", "proj-a"),
                    ("why", "a prompt about recurring billing invoicing dashboards"),
                    ("report", "proj-a")]
        for c in withargs:
            s.check(f"command: {c[0]}", lambda c=c: run_cli(env, *c) and None)

        # ---- bad input must not crash
        s.check("unknown episode id is handled",
                lambda: run_cli(env, "show", "999999") and None)
        s.check("unknown project is handled",
                lambda: run_cli(env, "project", "nope-not-real") and None)
        s.check("invalid search syntax is handled",
                lambda: run_cli(env, "search", "(((") and None)

        # ---- hooks: never raise, never block
        for ev in ("sessionstart", "precompact", "sessionend", "userpromptsubmit"):
            for name, payload in (("valid", {"session_id": "x", "cwd": "/Users/x/proj-a",
                                             "trigger": "startup", "transcript_path":
                                             os.path.join(src, "-Users-x-proj-a", "sess-a.jsonl"),
                                             "user_input": "a" * 80}),
                                  ("empty", ""), ("garbage", "not json"), ("no fields", {})):
                s.check(f"hook {ev}: {name} input",
                        lambda ev=ev, p=payload: run_hook(env, ev, p) and None)

        def brief_fires():
            out = run_hook(env, "sessionstart",
                           {"session_id": "new", "cwd": "/Users/x/proj-a", "trigger": "startup"})
            assert out.strip(), "no brief for a project with history"
            assert "Chronicle" in json.loads(out).get("additionalContext", ""), "brief malformed"
        s.check("brief fires for a known project", brief_fires)

        def brief_silent():
            out = run_hook(env, "sessionstart",
                           {"session_id": "new2", "cwd": "/nowhere/at/all", "trigger": "startup"})
            assert not out.strip(), f"spoke about an unknown directory: {out[:120]}"
        s.check("brief stays silent elsewhere", brief_silent)

        def recall_silent():
            out = run_hook(env, "userpromptsubmit",
                           {"session_id": "r", "cwd": "/Users/x/proj-a",
                            "user_input": "please make the button slightly larger on mobile screens"})
            assert not out.strip(), f"recall fired on a novel prompt: {out[:120]}"
        s.check("recall stays silent on novel work", recall_silent)

        # ---- the vocabulary a three-letter corpus needs
        def acronyms_are_learned():
            """A short word earns a place in the vocabulary by being one the corpus
            writes in capitals. That is the whole test: `crm` is an acronym, `and`
            is a function word that happens to get shouted in SQL."""
            got = {r["term"] for r in q("SELECT term FROM acronyms")}
            for w in ("crm", "sku", "csv"):
                assert w in got, f"{w!r} is written in capitals all over the corpus " \
                                 f"but was not learned: {sorted(got)}"
            for w in ("and", "not", "the", "was"):
                assert w not in got, f"{w!r} is a function word, not an acronym: {sorted(got)}"
            return f"{len(got)} learned"
        s.check("short acronyms are learned, shouted function words are not", acronyms_are_learned)

        def acronyms_reach_the_term_counts():
            """The allowlist buys nothing if the term counts cannot see it. `crm` is
            capitalised in three episodes and lower-case in a fourth, so a count of
            three would mean the second pass reused the first pass's capitals
            instead of re-reading the corpus."""
            row = q("SELECT df FROM terms WHERE term='crm'")
            assert row, "`crm` never reached the term table at all"
            shouted = q("SELECT df FROM acronyms WHERE term='crm'")[0]["df"]
            assert row[0]["df"] > shouted, \
                f"crm counted in {row[0]['df']} episodes but shouted in {shouted} — " \
                f"the lower-case use was missed"
            return f"df {row[0]['df']}, shouted in {shouted}"
        s.check("an acronym is counted wherever it appears, not only where shouted",
                acronyms_reach_the_term_counts)

        # ---- lookup: the user asked about the past
        def intent_reads_the_question():
            """Pure text, no index — which prompts are asking about past work."""
            from .recall import intent_of
            for t in ("what did we decide about the CRM?", "have we already shipped this?",
                      "use chronicle to find the vendor export", "last time we tried this it broke",
                      "did we ever finish the SKU table", "i think we had built this previously"):
                assert intent_of(t), f"missed a question about the past: {t!r}"
            for t in ("make the button larger on mobile", "add a new column to the vendor export",
                      "deploy this and watch the logs",
                      # an instruction, not a question — the trap the word invites
                      "remember to add the utm parameters to every campaign link",
                      # and the everyday second sense of every weak trigger: with
                      # nobody in the sentence, these are adjectives, not questions
                      "add a previously-unsupported flag to the parser",
                      "write a migration that backfills the earlier records",
                      "add a forgotten-password flow to the login screen"):
                assert not intent_of(t), f"read an ordinary instruction as a question: {t!r}"
        s.check("intent tells a question about the past from an instruction",
                intent_reads_the_question)

        def lookup_answers_a_question():
            out = run_hook(env, "userpromptsubmit",
                           {"session_id": "lk1", "cwd": "/Users/x/proj-e",
                            "user_input": "what did we do about the Brightwell vendor CSV export?"})
            assert out.strip(), "stayed silent on a direct question about past work"
            ctx = json.loads(out).get("additionalContext", "")
            assert "asked about past work" in ctx, f"took the repeat-work path instead: {ctx[:150]}"
            assert "brightwell" in ctx.lower(), f"found nothing about Brightwell: {ctx[:200]}"
        s.check("lookup answers a question about past work", lookup_answers_a_question)

        def lookup_crosses_projects():
            """Sitting in one project, asking about work recorded in another. This
            is the case that sends people back to searching by hand."""
            out = run_hook(env, "userpromptsubmit",
                           {"session_id": "lk2", "cwd": "/Users/x/proj-e",
                            "user_input": "have we reconciled the Brightwell invoices "
                                          "against the ledger?"})
            ctx = json.loads(out or "{}").get("additionalContext", "")
            assert "proj-f" in ctx, f"never crossed into the project that holds it: {ctx[:200]}"
        s.check("lookup finds work filed under another project", lookup_crosses_projects)

        def lookup_needs_something_to_look_for():
            """"What did we do?" names nothing. The session brief already answers
            that, and guessing here would be noise."""
            out = run_hook(env, "userpromptsubmit",
                           {"session_id": "lk3", "cwd": "/Users/x/proj-e",
                            "user_input": "hey so what did we do?"})
            assert not out.strip(), f"spoke without being given anything to find: {out[:120]}"
        s.check("lookup stays silent when the question names nothing",
                lookup_needs_something_to_look_for)

        def lookup_needs_a_rare_word():
            """Every word here is everywhere in this corpus. ANDing them together
            still matches most of it, so the question has not really named anything."""
            out = run_hook(env, "userpromptsubmit",
                           {"session_id": "lk4", "cwd": "/Users/x/proj-e",
                            "user_input": "what have we done about the stock dashboard?"})
            assert not out.strip(), f"answered a question that named nothing rare: {out[:150]}"
        s.check("lookup stays silent when nothing named is distinctive",
                lookup_needs_a_rare_word)

        def lookup_never_repeats_itself():
            payload = {"session_id": "lk5", "cwd": "/Users/x/proj-e",
                       "user_input": "what did we do about the Brightwell vendor CSV export?"}
            assert run_hook(env, "userpromptsubmit", payload).strip(), "no answer the first time"
            again = run_hook(env, "userpromptsubmit", payload)
            assert not again.strip(), f"offered the same episode twice: {again[:120]}"
        s.check("lookup does not offer the same episode twice in a session",
                lookup_never_repeats_itself)

        # ---- a hook must never wait on the writer closing stdin.
        # This is what produced "UserPromptSubmit hook timed out after 30s —
        # output discarded": the payload had arrived, and we were still blocked
        # in read() waiting for an EOF that had not come yet.
        from .hooks import BUDGET
        transcript = os.path.join(src, "-Users-x-proj-a", "sess-a.jsonl")
        held = {"session_id": "held", "cwd": "/Users/x/proj-a", "trigger": "startup",
                "transcript_path": transcript, "user_input": "a" * 80}
        for ev in ("sessionstart", "precompact", "sessionend", "userpromptsubmit"):
            def open_pipe(ev=ev):
                # cap the wait: a hang is the failure, and precompact's real
                # budget is 7 minutes — no test should sit there to find out
                limit = min(BUDGET[ev][1], 20.0)
                _, took = run_hook_openpipe(env, ev, held, limit)
                assert took < limit, f"took {took:.1f}s of a {limit}s budget"
                return f"{took:.2f}s"
            s.check(f"hook {ev}: finishes with stdin held open", open_pipe)

        def held_garbage():
            _, took = run_hook_openpipe(env, "userpromptsubmit", "not json", 6.0)
            assert took < 6.0, f"unparseable payload held the session {took:.1f}s"
            return f"{took:.2f}s"
        s.check("hook gives up on an unparseable payload", held_garbage)

        def brief_survives_open_pipe():
            out, _ = run_hook_openpipe(env, "sessionstart",
                                       {"session_id": "op", "cwd": "/Users/x/proj-a",
                                        "trigger": "startup"}, 12.0)
            assert "Chronicle" in json.loads(out or "{}").get("additionalContext", ""), \
                "brief was lost when the writer held the pipe open"
        s.check("brief still delivered with stdin held open", brief_survives_open_pipe)

        # ---- MCP
        def mcp():
            p = subprocess.Popen([sys.executable, "-m", "chronicle.mcp"], cwd=str(INSTALL_DIR),
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True,
                                 env={**env, "PYTHONPATH": str(INSTALL_DIR)})
            def call(m):
                p.stdin.write(json.dumps(m) + "\n"); p.stdin.flush()
                return json.loads(p.stdout.readline())
            try:
                init = call({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                             "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
                assert init["result"]["serverInfo"]["name"] == "chronicle", "bad serverInfo"
                tools = call({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
                assert len(tools) >= 9, f"only {len(tools)} tools"
                assert all(t.get("inputSchema") for t in tools), "a tool has no schema"
                r = call({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                          "params": {"name": "projects", "arguments": {}}})
                assert not r["result"].get("isError"), "projects tool errored"
                bad = call({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                            "params": {"name": "nope", "arguments": {}}})
                assert "error" in bad, "unknown tool did not return a JSON-RPC error"
            finally:
                p.stdin.close(); p.wait(timeout=10)
        s.check("mcp speaks the protocol", mcp)

        # ---- installer logic, without touching real settings
        def merge():
            from . import install
            fake = {"hooks": {"UserPromptSubmit": [
                {"matcher": "", "hooks": [{"type": "command", "command": "/other/tool"}]}]}}
            install._merge(fake, "/tmp/chronicle-hook")
            flat = json.dumps(fake)
            assert "/other/tool" in flat, "merging clobbered another tool's hook"
            assert flat.count("chronicle-hook") == 4, "did not install all four hooks"
            install._strip(fake)
            assert "chronicle-hook" not in json.dumps(fake), "uninstall left entries behind"
            assert "/other/tool" in json.dumps(fake), "uninstall removed another tool's hook"
        s.check("install merges and strips without collateral", merge)

        # ---- safety invariants
        def network():
            import re, pathlib
            src_all = "".join(p.read_text() for p in (INSTALL_DIR / "chronicle").glob("*.py"))
            hits = re.findall(r"\b(urllib\.request|requests\.|httpx\.|socket\.create_connection)", src_all)
            assert not hits, f"outbound network client found: {set(hits)}"
        s.check("no module makes a network call", network)

        def redaction_patterns():
            from .redact import scrub
            probes = [("sk-ant-api03-" + "a" * 40, "ANTHROPIC_KEY"), ("ghp_" + "b" * 36, "GITHUB_PAT"),
                      ("shpat_" + "c" * 32, "SHOPIFY_TOKEN"), ("AKIA" + "D" * 16, "AWS_KEY"),
                      ("AIza" + "e" * 35, "GOOGLE_KEY"), ("xoxb-" + "1" * 24, "SLACK_TOKEN"),
                      ('"password": "hunter2secret"', "PASSWORD"),
                      ("eyJhbGciOiJIUzI1.eyJzdWIiOiIxMjM0.dBjftJeZ4CVP", "JWT")]
            missed = [l for t, l in probes if f"[REDACTED:{l}]" not in scrub(t)[0]]
            assert not missed, f"redaction missed: {missed}"
        s.check("every redaction pattern still catches", redaction_patterns)

        # ---- dashboard rendering must never execute transcript content
        def dashboard_escapes():
            """A prompt containing HTML is ordinary. Rendering it as HTML is not."""
            from . import web
            import sqlite3 as _sq
            c = _sq.connect(":memory:")
            c.row_factory = _sq.Row
            c.execute("CREATE VIRTUAL TABLE f USING fts5(a,b,body)")
            c.execute("INSERT INTO f VALUES('t','o',"
                      "'the <img src=x onerror=alert(1)> payload was here')")
            raw = c.execute(f"SELECT snippet(f,2,'{web.HI0}','{web.HI1}','…',20) s "
                            "FROM f WHERE f MATCH 'payload'").fetchone()["s"]
            out = web.snip(raw)
            # the text may still *read* as a tag; it must not *be* one
            assert "<img" not in out and "&lt;img" in out, f"markup survived: {out}"
            assert "<em>payload</em>" in out, f"highlight lost: {out}"
        s.check("the dashboard escapes search snippets", dashboard_escapes)

        def dashboard_host_guard():
            """Loopback binding alone does not stop DNS rebinding."""
            from .web import ALLOWED_HOSTS
            assert "127.0.0.1" in ALLOWED_HOSTS and "localhost" in ALLOWED_HOSTS
            assert not any(h.endswith(".com") or h == "*" for h in ALLOWED_HOSTS)
        s.check("the dashboard only answers on loopback hosts", dashboard_host_guard)

        def dashboard_is_read_only():
            src = (INSTALL_DIR / "chronicle" / "web.py").read_text()
            assert "db.connect_ro" in src, "dashboard must open the index read-only"
            assert "db.connect()" not in src, "dashboard opened a writable connection"
        s.check("the dashboard cannot write to the index", dashboard_is_read_only)

        def shell_writes_are_recovered():
            """Bash writes files too, and reports no file_path when it does."""
            from . import shellwrite
            here = {"/w/notes.md", "/w/app.py", "/w/copy.md", "/w/t.txt"}
            ex = lambda p: p in here

            got = shellwrite.targets("cat > notes.md <<'EOF'\nhello\nEOF", "/w", ex)
            assert got == ["/w/notes.md"], got
            got = shellwrite.targets("sed -i '' 's/a/b/' app.py", "/w", ex)
            assert got == ["/w/app.py"], got
            got = shellwrite.targets("cp src.md copy.md", "/w", ex)
            assert got == ["/w/copy.md"], got
            got = shellwrite.targets("echo hi > t.txt", "/w", ex)
            assert got == ["/w/t.txt"], got
        s.check("bash heredocs, redirects and sed -i attribute their files",
                shell_writes_are_recovered)

        def heredoc_bodies_are_not_scanned():
            """The body of a heredoc is prose, not shell. It is full of `>`.

            Without stripping it first, a markdown blockquote becomes a file
            path and the episode gets filed under a project called `The`."""
            from . import shellwrite
            cmd = ("cat > real.md <<'EOF'\n"
                   "> quoted line\n"
                   "if (a > b) { x = 1 }\n"
                   "echo boom > /w/notafile.txt\n"
                   "EOF")
            got = shellwrite.targets(cmd, "/w", lambda p: True)
            assert got == ["/w/real.md"], got
            assert "/w/notafile.txt" not in got, "heredoc body was scanned as shell"

            # and a path that does not exist is never attributed, however
            # convincing the command looks
            got = shellwrite.targets("cat > gone.md <<'EOF'\nx\nEOF",
                                     "/w", lambda p: False)
            assert got == [], got
        s.check("heredoc bodies never become file paths", heredoc_bodies_are_not_scanned)

        def shell_write_reaches_the_episode():
            """End to end: a Bash heredoc puts a real file on the episode."""
            rows = q("SELECT files_touched FROM episodes WHERE files_touched LIKE ?",
                     ("%shell-made.md%",))
            assert rows, "a file written by a Bash heredoc never reached an episode"
            decoy = q("SELECT 1 FROM episodes WHERE files_touched LIKE ?",
                      ("%decoy-never-written%",))
            assert not decoy, "a path from inside the heredoc body was attributed"
        s.check("a shell-written file lands on its episode", shell_write_reaches_the_episode)

        def checkpoint_scrubs_tasks():
            """Task text reaches disk and the next context; it needs the scrubber too."""
            from .checkpoint import _scrub_task
            out = _scrub_task({"subject": "push with ghp_" + "b" * 36,
                               "description": '"password": "hunter2secret"',
                               "status": "pending"})
            assert "ghp_" not in out["subject"], out["subject"]
            assert "hunter2secret" not in out["description"], out["description"]
            assert out["status"] == "pending", "scrubbing dropped a field"
        s.check("checkpoints redact task text as well as prompts", checkpoint_scrubs_tasks)

        def isolation():
            real = os.path.expanduser("~/.chronicle/chronicle.db")
            assert os.path.realpath(db) != os.path.realpath(real), "selftest used the real index"
        s.check("the real index was never touched", isolation)
      except Exception:
        # the harness itself must never die silently
        s.results.append((False, "self test harness",
                          traceback.format_exc().strip().splitlines()[-1]))
    finally:
        shutil.rmtree(sandbox, ignore_errors=True)
    return s
