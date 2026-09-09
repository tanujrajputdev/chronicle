"""Find genuinely-similar past work for a prompt, using nothing but SQLite.

The design goal is silence. This runs on every prompt the user types, so a false
positive is a tax on their attention and a true positive they ignore is worse than
nothing. Every stage is a gate that prefers to bail out. No model is called; the
whole thing is term statistics over an index that already exists.
"""
import re, json, math

WORD = re.compile(r"[a-z][a-z\-]{3,29}")   # no digits, no apostrophes: ids and contractions are noise
STOP = set("""about above after again against another because been before being below between both
cannot could does doing done down during each either else even every from further getting given
gives goes going gone have having here hers herself himself into itself just like made make many
more most much must never once only other ours ourselves over same should since some such than
that their theirs them themselves then there these they this those through under until very were
what when where which while whom will with within without would your yours yourself add also want
need make made using used use need needs like want wants file files code line lines page pages
this that with from have will your our can should would could there here what when
okay yeah yes sure right good great nice fine done next then also still just now
normal general ways thing things stuff bit lot more less same other another
please thanks thank hey hello lets look looks looking see seen check checked
work works working fix fixed fixing change changed changes update updated
""".split())

# tuned against the real corpus — see `chronicle why`
MIN_TERMS = 4          # a prompt with fewer distinctive words cannot be matched safely
MAX_DF_RATIO = 0.20    # a word in >20% of episodes carries no signal
MIN_OVERLAP = 3        # a candidate must share at least this many distinctive words
MIN_COVERAGE = 0.60    # ...and that must be most of what was asked
MIN_STRONG = 3         # ...including this many genuinely rare words
MIN_SCORE = 0.030      # coverage normalised by episode size, so sprawling
                       # episodes stop matching everything. Calibrated against the
                       # real corpus: true matches score 0.038-0.075, attractors 0.013-0.017.
STRONG_DF_RATIO = 0.06
MAX_HITS = 2


def terms_of(text):
    return [w for w in WORD.findall((text or "").lower()) if w not in STOP]


def build_vocab(con):
    """Term statistics, computed once at index time so the prompt hook stays cheap."""
    con.execute("CREATE TABLE IF NOT EXISTS terms (term TEXT PRIMARY KEY, df INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS episode_terms ("
                "episode_id INTEGER PRIMARY KEY, rare TEXT, n_all INTEGER)")
    con.execute("DELETE FROM terms")
    con.execute("DELETE FROM episode_terms")
    df, per_ep = {}, {}
    for rowid, body in con.execute("SELECT rowid, body FROM episodes_fts"):
        ts = set(terms_of(body))
        per_ep[rowid] = ts
        for w in ts:
            df[w] = df.get(w, 0) + 1
    con.executemany("INSERT INTO terms VALUES (?,?)", df.items())
    n_eps = len(per_ep) or 1
    cap = max(2, int(n_eps * MAX_DF_RATIO))
    con.executemany("INSERT INTO episode_terms VALUES (?,?,?)",
                    ((eid, " ".join(w for w in ts if df.get(w, 0) <= cap), len(ts))
                     for eid, ts in per_ep.items()))
    con.commit()
    return len(df)


def distinctive(con, text, n_eps, with_df=False):
    """The words in this prompt that actually carry identity. One query, not one per word."""
    cap = max(2, int(n_eps * MAX_DF_RATIO))
    seen, order = set(), []
    for w in terms_of(text):
        if w not in seen:
            seen.add(w)
            order.append(w)
    if not order:
        return ({}, []) if with_df else []
    qs = ",".join("?" * len(order))
    df = dict(con.execute(f"SELECT term, df FROM terms WHERE term IN ({qs})", order).fetchall())
    out = [w for w in order if df.get(w, 10 ** 9) <= cap]
    return (df, out) if with_df else out


def excluded_projects(con):
    from .config import ALIASES_PATH
    try:
        return set(json.loads(ALIASES_PATH.read_text()).get("recall_exclude", ["memory"]))
    except Exception:
        return {"memory"}


def find(con, text, exclude_session=None, exclude_ids=(), explain=False):
    """Return at most MAX_HITS past episodes that are really about the same thing.

    When explain is set, returns (hits, trace) so `chronicle why` can show which gate
    stopped a candidate. Every gate prefers to bail: silence is the correct default."""
    trace = []
    n_eps = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    if n_eps < 10:
        return ([], trace) if explain else []
    strength, words = distinctive(con, text, n_eps, with_df=True)
    trace.append(("distinctive words", ", ".join(words) or "(none)"))
    if len(words) < MIN_TERMS:
        trace.append(("stopped", f"only {len(words)} distinctive words, need {MIN_TERMS}"))
        return ([], trace) if explain else []

    q = " OR ".join(f'"{w}"' for w in words[:24])
    try:
        rows = con.execute(
            """SELECT e.id, e.title, e.project_id, e.started, e.session_id,
                      t.rare, t.n_all
               FROM episodes_fts
               JOIN episodes e ON e.id = episodes_fts.rowid
               LEFT JOIN episode_terms t ON t.episode_id = e.id
               WHERE episodes_fts MATCH ? ORDER BY bm25(episodes_fts) LIMIT 12""", (q,)).fetchall()
    except Exception:
        return ([], trace) if explain else []

    skip = excluded_projects(con)
    wanted = set(words)
    strong_cap = max(1, int(n_eps * STRONG_DF_RATIO))
    hits, shown = [], set()
    for r in rows:
        if exclude_session and r["session_id"] == exclude_session:
            continue
        if r["id"] in exclude_ids or r["project_id"] in skip:
            continue
        present = wanted & set((r["rare"] or "").split())
        if len(present) < MIN_OVERLAP:
            trace.append((f"#{r['id']}", f"shares only {len(present)} of {len(wanted)} words"))
            continue
        if len(present) / len(wanted) < MIN_COVERAGE:
            trace.append((f"#{r['id']}", f"coverage {len(present)/len(wanted):.0%} < {MIN_COVERAGE:.0%}"))
            continue
        if sum(1 for w in present if strength.get(w, 99) <= strong_cap) < MIN_STRONG:
            trace.append((f"#{r['id']}", f"fewer than {MIN_STRONG} genuinely rare words shared"))
            continue
        # size-normalised: without this, the few enormous episodes match every prompt
        score = (len(present) / len(wanted)) / math.sqrt(max(1, r["n_all"] or 1))
        if score < MIN_SCORE:
            trace.append((f"#{r['id']}", f"score {score:.3f} < {MIN_SCORE} (episode too sprawling)"))
            continue
        key = (r["title"] or "")[:40].lower()
        if key in shown:
            continue
        shown.add(key)
        hits.append({"id": r["id"], "title": r["title"], "project": r["project_id"],
                     "date": r["started"][:10], "shared": sorted(present),
                     "coverage": len(present) / len(wanted), "score": score})
        trace.append((f"#{r['id']}", f"MATCH coverage {len(present)/len(wanted):.0%} score {score:.3f}"))
        if len(hits) >= MAX_HITS:
            break
    return (hits, trace) if explain else hits


def render(hits):
    L = ["## Chronicle — you have worked on this before"]
    for h in hits:
        L.append(f"- `#{h['id']}` {h['date']} · {h['project']} — {h['title'][:90]}")
    L.append("_Say the word and I will open it (`chronicle show <id>`). Nothing is loaded yet._")
    return "\n".join(L)
