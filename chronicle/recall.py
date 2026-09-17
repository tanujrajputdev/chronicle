"""Find genuinely-similar past work for a prompt, using nothing but SQLite.

The design goal is silence. This runs on every prompt the user types, so a false
positive is a tax on their attention and a true positive they ignore is worse than
nothing. Every stage is a gate that prefers to bail out. No model is called; the
whole thing is term statistics over an index that already exists.

Two different questions live here, and they need different machinery:

  `find`    answers one the user did not ask — "you have done this before". It is
            near-duplicate detection over prompt vocabulary, so the gates are
            tight: we must be nearly certain before interrupting.
  `lookup`  answers one they did ask — "what did we do about X". The phrasing is
            the trigger; precision then comes from the search itself — an AND of
            the rarest words they used — rather than from gates. If that finds
            nothing, nothing is said.

Keeping them apart matters. Loosening `find` until it caught the second kind would
make it fire on everything; the second kind needs no loosening at all, because the
user already told us what they want.
"""
import re, json, math

WORD = re.compile(r"[a-z][a-z\-]{3,29}")   # no digits, no apostrophes: ids and contractions are noise

# WORD cannot see a three-letter word, and this corpus runs on them — crm, pdp,
# oos, csv, api. Dropping the floor for every short word would admit `run`, `new`,
# `get` and a hundred other noise terms, so learn instead: a short word earns its
# place by being one the corpus writes in capitals, which is what an acronym is.
ACRONYM = re.compile(r"\b([A-Z][A-Z0-9]{1,2})\b")      # 2-3 chars, as written
SHORT_WORD = re.compile(r"\b([a-z][a-z0-9]{1,2})\b")   # ...the same, as matched
MIN_ACRONYM_EPISODES = 2                               # shouted in at least this many

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

# Short words need their own list. Nothing under four letters could reach STOP
# before, so the function words never had to be named; now that an acronym can be
# two letters, every one of them is a candidate — `NOT` and `AND` turn up shouted
# in SQL, `US` and `NO` in ordinary prose. Anything genuinely an acronym (ai, db,
# js, pr, qa, ui, ux, ci, id, kb) is deliberately absent from this list.
STOP |= set("""a an as at be by do go he if in is it me my no of on or so to up we am us
all and any are but can did does end far few for get got had has her him his how its
let lot may new not now off old one out our own per put run say see she set top too try
two use via was way who why yes yet you ok big bad the etc pm ago due
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


def terms_of(text, short=()):
    """The content words in some text.

    `short` is the learned acronym allowlist; without it this sees only words of
    four letters or more, which is the right default for callers that have no
    database handy."""
    low = (text or "").lower()
    out = [w for w in WORD.findall(low) if w not in STOP]
    if short:
        out += [w for w in SHORT_WORD.findall(low) if w in short]
    return out


def short_terms(con):
    """The acronym allowlist learned at index time.

    An index built before acronyms existed has no table, and a prompt-path caller
    must not care: no allowlist simply means no short words."""
    try:
        return {r[0] for r in con.execute("SELECT term FROM acronyms")}
    except Exception:
        return set()


def build_vocab(con):
    """Term statistics, computed once at index time so the prompt hook stays cheap.

    Two passes over the corpus rather than one: which short words count as
    acronyms is itself a fact about the corpus, and it has to be settled before
    the term counts that depend on it can be taken."""
    con.execute("CREATE TABLE IF NOT EXISTS terms (term TEXT PRIMARY KEY, df INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS episode_terms ("
                "episode_id INTEGER PRIMARY KEY, rare TEXT, n_all INTEGER)")
    con.execute("CREATE TABLE IF NOT EXISTS acronyms (term TEXT PRIMARY KEY, df INTEGER)")
    con.execute("DELETE FROM terms")
    con.execute("DELETE FROM episode_terms")
    con.execute("DELETE FROM acronyms")

    # pass 1 — which short words does this corpus write in capitals?
    acro_df = {}
    for row in con.execute("SELECT body FROM episodes_fts"):
        for w in {m.lower() for m in ACRONYM.findall(row[0] or "")} - STOP:
            acro_df[w] = acro_df.get(w, 0) + 1
    keep = {w for w, n in acro_df.items() if n >= MIN_ACRONYM_EPISODES}
    con.executemany("INSERT INTO acronyms VALUES (?,?)", ((w, acro_df[w]) for w in sorted(keep)))

    # pass 2 — term counts, now that `terms_of` can see those short words wherever
    # they appear, including the episodes that only ever wrote them in lower case
    df, per_ep = {}, {}
    for rowid, body in con.execute("SELECT rowid, body FROM episodes_fts"):
        ts = set(terms_of(body, keep))
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
    for w in terms_of(text, short_terms(con)):
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
    """Projects the prompt path should never volunteer, from `aliases.json`.

    Empty by default, which is what `aliases.example.json` and the README have
    always claimed. It used to fall back to the author's own folder name, so a
    fresh install with a project of that name was silently muted for reasons
    nothing documented."""
    from .config import ALIASES_PATH
    try:
        return set(json.loads(ALIASES_PATH.read_text()).get("recall_exclude", []))
    except Exception:
        return set()


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


# ------------------------------------------------------- the user asked about the past

# How people actually reach for their own history. Matching here is not evidence
# that any particular past work is relevant — it only earns the prompt a search,
# which then has to find something on its own before anything is said.
# Phrasings that can only be a question about past work. These stand alone.
INTENT = re.compile(r"""
      \bus(?:e|ing)\s+chronicle\b
    | \bwhat\s+(?:did|have|has|all|was|were)\s+(?:we|i|you)\b
    | \bwhat\s+(?:we|i|you)\s+(?:did|had|built|decided|discussed|implemented)\b
    | \b(?:have|has|did|had)\s+(?:we|i|you)\b
    | \bwe\s+(?:had|have)\s+(?:already|built|done|decided|implemented)\b
    | \b(?:we|i|you|ive|i've)\s+already\b
    | \blast\s+time\b
    | \bwhat\s+(?:was|were)\s+decided\b
""", re.X | re.I)

# Words that only *suggest* a question about the past, because each has an
# everyday second sense: a `previously-unsupported flag`, `the earlier records`,
# `remember to bump the version`. On their own they are adjectives and
# instructions, so they count only when someone is also in the sentence — a
# question about what was done is asked about us, not about records.
WEAK_INTENT = re.compile(r"\b(?:previously|earlier|forgot(?:ten)?|remember(?!\s+to\b))\b", re.I)
PERSON = re.compile(r"\b(?:we|i|you|ive|i've|us|our|my)\b", re.I)

# Words that belong to the asking rather than to the subject. "what we had
# implemented in the billing client" is a question about billing and clients;
# letting `implemented` compete on rarity would aim the search at the wrong thing.
ASK = set("""remember remembered forgot forgotten previously earlier already chronicle
decide decided decides decision decisions discuss discussed implement implemented
happen happened history context recall session sessions past before again
find found chat chats conversation thread
""".split())

MIN_LOOKUP_TERMS = 2      # "what did we do?" names nothing — that is the brief's job
MAX_LOOKUP_HITS = 3
LOOKUP_CANDIDATES = 8


def intent_of(text):
    """Did the user just ask about their own past work?"""
    text = text or ""
    return bool(INTENT.search(text) or
                (WEAK_INTENT.search(text) and PERSON.search(text)))


def lookup(con, text, cwd_project=None, exclude_session=None, explain=False):
    """Find the past work a retrieval question named.

    No coverage gates. The user asked, so the only question is whether they named
    something findable: ANDing together the rarest words they used is already a
    narrow question, and if it matches nothing then nothing is the right answer.

    Hits in the current project sort first but other projects are not excluded —
    the same work often lives under two project ids, and a question about it
    should find it wherever it was actually filed."""
    trace = []
    n_eps = con.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
    if n_eps < 10:
        return ([], trace) if explain else []

    seen, order = set(), []
    for w in terms_of(text, short_terms(con)):
        if w not in seen and w not in ASK:
            seen.add(w)
            order.append(w)
    if len(order) < MIN_LOOKUP_TERMS:
        trace.append(("stopped", f"names only {len(order)} thing(s) to search for, "
                                 f"need {MIN_LOOKUP_TERMS}"))
        return ([], trace) if explain else []

    qs = ",".join("?" * len(order))
    df = dict(con.execute(f"SELECT term, df FROM terms WHERE term IN ({qs})", order).fetchall())
    # a word the corpus has never seen is the rarest thing in the prompt, not the
    # most common — an unknown word is either brand new or a typo, and a typo
    # simply finds nothing
    rare = sorted(order, key=lambda w: (df.get(w, 0), order.index(w)))

    # At least one word has to be genuinely selective. "a small message for client
    # what have we done" names three words the corpus uses everywhere, and ANDing
    # them together still matched five unrelated projects — a question that names
    # nothing rare has not really named anything, and the session brief already
    # covers "what have we been doing".
    cap = max(2, int(n_eps * MAX_DF_RATIO))
    if df.get(rare[0], 0) > cap:
        trace.append(("stopped", f"nothing named is rare enough — even '{rare[0]}', the "
                                 f"rarest of them, is in {df.get(rare[0])} of {n_eps} "
                                 f"episodes (cap {cap})"))
        return ([], trace) if explain else []

    # Ask the narrow question first and widen only if it came back empty. Three
    # rare words shared by one episode is a strong claim; two is a decent one;
    # one is a topic, not a question, so the ladder stops there.
    rows = []
    for n in (3, 2):
        if len(rare) < n:
            continue
        q = " AND ".join(f'"{w}"' for w in rare[:n])
        try:
            rows = con.execute(
                """SELECT e.id, e.title, e.project_id, e.started, e.session_id
                   FROM episodes_fts
                   JOIN episodes e ON e.id = episodes_fts.rowid
                   WHERE episodes_fts MATCH ?
                   ORDER BY bm25(episodes_fts, 10.0, 5.0, 1.0) LIMIT ?""",
                (q, LOOKUP_CANDIDATES)).fetchall()
        except Exception:
            return ([], trace) if explain else []
        # the weights say what a retrieval question actually wants: an episode
        # whose *title and opening prompt* were about this, not one whose body
        # mentioned it in passing on the way to something else
        if rows:
            trace.append(("searching for", " AND ".join(rare[:n]) +
                          f"   (from: {', '.join(order[:8])})"))
            break
        trace.append((f"{n} rarest words", " AND ".join(rare[:n]) + " — no episode has all of them"))
    if not rows:
        trace.append(("stopped", "nothing in the index matches what was named"))
        return ([], trace) if explain else []

    skip = excluded_projects(con) - {cwd_project}
    hits = []
    for r in rows:
        if exclude_session and r["session_id"] == exclude_session:
            continue
        if r["project_id"] in skip:
            trace.append((f"#{r['id']}", f"skipped: {r['project_id']} is excluded from recall"))
            continue
        hits.append({"id": r["id"], "title": r["title"] or "", "project": r["project_id"],
                     "date": r["started"][:10], "here": r["project_id"] == cwd_project})
        trace.append((f"#{r['id']}", ("MATCH here" if r["project_id"] == cwd_project
                                      else f"MATCH in {r['project_id']}")))
    # this project first, bm25 order preserved within each group
    hits.sort(key=lambda h: 0 if h["here"] else 1)
    hits = hits[:MAX_LOOKUP_HITS]
    return (hits, trace) if explain else hits


def render_lookup(hits):
    """Unlike `render`, this does not wait to be asked twice. The user's prompt was
    already the request; making them say "yes, open it" is the round trip this
    whole path exists to remove."""
    L = ["## Chronicle — you asked about past work; these look relevant"]
    for h in hits:
        where = "" if h["here"] else f" · {h['project']}"
        L.append(f"- `#{h['id']}` {h['date']}{where} — {h['title'][:90]}")
    L.append("_Open any of these before answering — `chronicle show <id>`, or the chronicle "
             "skill for more. Nothing is loaded yet, and these are candidates, not an answer: "
             "if none of them fits, say so and carry on._")
    return "\n".join(L)
