import os, re, json, glob, html, datetime, collections, pathlib
from .config import (SOURCE, GAP_SECONDS, MAX_USER_TEXT, MAX_ASST_TEXT,
                     MAX_BODY, load_aliases)
from .redact import scrub
from . import shellwrite

MUTATORS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
# work continuing after the last prompt still belongs to the episode, but only for a while
TAIL_GRACE = 2 * 3600
# a repo lives one level below these; they are containers, not projects
CONTAINERS = {"dev", "Documents", "Downloads", "Desktop", "Projects",
              "code", "work", "repos", "src", "sites", "apps"}


def file_root(path):
    """The repo/project root a touched file belongs to."""
    if not path or not path.startswith("/"):
        return None
    s = path.split("/")
    if len(s) < 4 or s[1] != "Users":
        return "/".join(s[:3]) or None
    i = 3
    while i < len(s) - 1 and s[i] in CONTAINERS:
        i += 1
    return "/".join(s[: i + 1])
AGENT_TOOLS = {"Task", "Agent"}
CMD_RX = re.compile(r"<command-(?:name|message|args)>.*?</command-(?:name|message|args)>", re.S)
TAG_RX = re.compile(r"<[^>]{1,80}>")
WS_RX = re.compile(r"[ \t]+")
ANSI_RX = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
IMG_RX = re.compile(r"\[Image\s*#\d+\]")
PROMPT_CHR_RX = re.compile(r"^[\u276f>\u00a0\s]+")
NOISE_PREFIX = ("/compact", "/model", "/init", "/clear", "/cost", "/resume",
                "continue", "go on", "ok", "yes", "y", "proceed", "next", "thanks")
# text that means "this is machinery, not a request"
NOISE_STARTS = (
    "this session is being continued",
    "caveat:",
    "the messages below were generated",
    "analysis:",
    "summary:",
    "system-reminder",
    "your task is to create a detailed summary",
    "compacted (",
    "compacted(",
)
PATH_ONLY_RX = re.compile(r"^(?:/[\w.@ +-]+)+\.\w+$")


def _dt(ts):
    if not ts:
        return None
    try:
        return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _text_of(content, want_tool_result=False):
    """Flatten a message content field to plain text."""
    if isinstance(content, str):
        return content, False
    if not isinstance(content, list):
        return "", False
    parts, only_results = [], True
    for b in content:
        if not isinstance(b, dict):
            continue
        t = b.get("type")
        if t == "text":
            parts.append(b.get("text") or "")
            only_results = False
        elif t in ("tool_use", "thinking"):
            only_results = False
    return "\n".join(parts).strip(), only_results


def _clean(s, keep_lines=False):
    s = html.unescape(s or "")
    s = ANSI_RX.sub("", s)
    s = IMG_RX.sub(" ", s)
    s = CMD_RX.sub(" ", s)
    s = TAG_RX.sub(" ", s)
    s = WS_RX.sub(" ", s)
    if keep_lines:
        return "\n".join(l.strip() for l in s.split("\n") if l.strip())
    return s.replace("\n", " ").strip()


MACHINERY_RX = re.compile(
    r"^\s*(?:<command-|<local-command-|<task-notification|<user-prompt-submit-hook|<system-reminder|"
    r"\[Request interrupted|Caveat: The messages below|This session is being continued)",
    re.I)


def is_machinery(text):
    """True for harness plumbing the user never typed."""
    if not text:
        return True
    if MACHINERY_RX.match(text):
        return True
    c = _clean(text).lower()
    return c.startswith("compacted (") or c in ("/compact", "/clear", "/model", "/cost", "/resume")


def _substantive(s):
    c = _clean(s)
    if len(c) < 14:
        return False
    low = c.lower()
    for p in NOISE_PREFIX:
        if low == p or (low.startswith(p + " ") and len(low) < len(p) + 8):
            return False
    for p in NOISE_STARTS:
        if low.startswith(p):
            return False
    return True


def _title_from(text, limit=88):
    c = _clean(text, keep_lines=True)
    c = re.sub(r"^(please|hey|hi|ok|okay|so|now|lets|let's)\s+", "", c, flags=re.I)
    lines = [l.strip() for l in c.split("\n") if l.strip()]
    first = ""
    for l in lines:
        toks = l.split()
        if toks and all(PATH_ONLY_RX.match(t) for t in toks):
            continue  # a drag-and-dropped file list, not a sentence
        first = PROMPT_CHR_RX.sub("", l).lstrip("-*•#0123456789. )").strip()
        if first:
            break
    if not first and lines:
        first = " ".join(os.path.basename(t) for t in lines[0].split()[:3])
    if len(first) > limit:
        cut = first[:limit].rsplit(" ", 1)[0].rstrip(" .,;:!-")
        first = cut + "…"
    else:
        first = first.rstrip()
    return first[:1].upper() + first[1:] if first else ""


def decode_dirname(name):
    return "/" + name.lstrip("-").replace("-", "/")


# ---------------------------------------------------------------- scanning

def scan_session_files():
    """(project_dir, path) for every top-level session transcript."""
    out = []
    if not SOURCE.exists():
        return out
    for d in sorted(os.listdir(SOURCE)):
        pd = SOURCE / d
        if not pd.is_dir():
            continue
        for f in sorted(glob.glob(str(pd / "*.jsonl"))):
            out.append((d, f))
    return out


def scan_agent_files():
    """(project_dir, session_id, workflow_id, path) for every subagent transcript."""
    out = []
    if not SOURCE.exists():
        return out
    for d in sorted(os.listdir(SOURCE)):
        pd = SOURCE / d
        if not pd.is_dir():
            continue
        for root, dirs, files in os.walk(pd):
            if os.path.basename(root) != "subagents" and "/subagents" not in root:
                continue
            rel = os.path.relpath(root, pd).split(os.sep)
            if not rel or rel[0] in (".", ""):
                continue
            sid = rel[0]
            wf = next((p for p in rel if p.startswith("wf_")), None)
            for f in files:
                if f.endswith(".jsonl"):
                    out.append((d, sid, wf, os.path.join(root, f)))
    return out


# ---------------------------------------------------------------- parsing

def parse_session(path):
    """Single streaming pass -> compact events + session-level facts."""
    events = []          # (dt, kind, payload)
    title = None
    cwd = branch = None
    n_user = n_asst = compactions = 0
    out_tok = cache_rd = in_tok = cache_wr = 0

    with open(path, "r", errors="ignore") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            typ = o.get("type")
            if typ == "ai-title":
                title = o.get("aiTitle") or title
                continue
            if o.get("cwd"):
                cwd = o["cwd"]
            if o.get("gitBranch"):
                branch = o["gitBranch"]

            d = _dt(o.get("timestamp"))
            if o.get("isCompactSummary") or o.get("compactMetadata"):
                compactions += 1
                if d:
                    events.append((d, "compact", None))

            msg = o.get("message") or {}

            if typ == "user" and not o.get("isMeta") and d:
                text, only_results = _text_of(msg.get("content"))
                if only_results or not text.strip():
                    continue
                n_user += 1
                events.append((d, "user", text[:MAX_USER_TEXT]))

            elif typ == "assistant" and d:
                n_asst += 1
                u = msg.get("usage") or {}
                ot = u.get("output_tokens", 0) or 0
                cr = u.get("cache_read_input_tokens", 0) or 0
                it = u.get("input_tokens", 0) or 0
                cw = u.get("cache_creation_input_tokens", 0) or 0
                out_tok += ot
                cache_rd += cr
                in_tok += it
                cache_wr += cw
                text, _ = _text_of(msg.get("content"))
                if text:
                    events.append((d, "asst", text[:MAX_ASST_TEXT]))
                if ot or cr or it or cw:
                    events.append((d, "usage", (ot, cr, it, cw)))
                c = msg.get("content")
                if isinstance(c, list):
                    for b in c:
                        if isinstance(b, dict) and b.get("type") == "tool_use":
                            nm = b.get("name") or "?"
                            inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                            fp = inp.get("file_path") or inp.get("notebook_path")
                            sub = inp.get("subagent_type") if nm in AGENT_TOOLS else None
                            events.append((d, "tool", (nm, fp, sub)))
                            if nm == "Bash":
                                # a heredoc or redirect writes a file just as
                                # really as Write does, and reports nothing
                                for wp in shellwrite.targets(inp.get("command"), cwd):
                                    events.append((d, "shellwrite", wp))

    events.sort(key=lambda e: e[0])
    return {
        "events": events, "title": title, "cwd": cwd, "branch": branch,
        "n_user": n_user, "n_asst": n_asst, "compactions": compactions,
        "out_tokens": out_tok, "cache_read": cache_rd,
        "in_tokens": in_tok, "cache_write": cache_wr,
    }


def parse_agent(path):
    """Subagent transcript -> prompt, final result, span, cost."""
    prompt = None
    last_text = ""
    first = last = None
    n_tools = 0
    out_tok = in_tok = cache_rd = cache_wr = 0
    with open(path, "r", errors="ignore") as fh:
        for line in fh:
            try:
                o = json.loads(line)
            except Exception:
                continue
            d = _dt(o.get("timestamp"))
            if d:
                first = first or d
                last = d
            msg = o.get("message") or {}
            if o.get("type") == "user" and prompt is None:
                t, only = _text_of(msg.get("content"))
                if t and not only:
                    prompt = t[:MAX_USER_TEXT]
            elif o.get("type") == "assistant":
                u = msg.get("usage") or {}
                out_tok += u.get("output_tokens", 0) or 0
                in_tok += u.get("input_tokens", 0) or 0
                cache_rd += u.get("cache_read_input_tokens", 0) or 0
                cache_wr += u.get("cache_creation_input_tokens", 0) or 0
                t, _ = _text_of(msg.get("content"))
                if t:
                    last_text = t
                c = msg.get("content")
                if isinstance(c, list):
                    n_tools += sum(1 for b in c if isinstance(b, dict) and b.get("type") == "tool_use")
    return {"prompt": prompt or "", "result": last_text[:MAX_ASST_TEXT],
            "started": first, "ended": last, "n_tools": n_tools, "out_tokens": out_tok,
            "in_tokens": in_tok, "cache_read": cache_rd, "cache_write": cache_wr}


# ---------------------------------------------------------------- segmenting

def segment(events, gap=GAP_SECONDS):
    """Split a session's events into episodes on idle gaps between prompts."""
    prompts = [e for e in events if e[1] == "user" and not is_machinery(e[2])]
    if not prompts:
        return []
    bounds, start, last = [], prompts[0][0], prompts[0][0]
    for d, _, _ in prompts[1:]:
        if (d - last).total_seconds() > gap:
            bounds.append((start, last))
            start = d
        last = d
    bounds.append((start, last))

    eps = []
    for i, (a, b) in enumerate(bounds):
        nxt = bounds[i + 1][0] if i + 1 < len(bounds) else None
        ep = {"seq": i + 1, "started": a, "ended": b, "prompts": [], "asst": [],
              "tools": collections.Counter(), "files": set(), "agents": 0,
              "compactions": 0, "out_tokens": 0, "cache_read": 0,
              "in_tokens": 0, "cache_write": 0}
        for d, kind, payload in events:
            if d < a or (nxt is not None and d >= nxt):
                continue
            if kind == "user":
                ep["prompts"].append((d, payload))
            elif kind == "asst":
                ep["asst"].append(payload)
            elif kind == "compact":
                ep["compactions"] += 1
            elif kind == "usage":
                ep["out_tokens"] += payload[0]
                ep["cache_read"] += payload[1]
                ep["in_tokens"] += payload[2]
                ep["cache_write"] += payload[3]
            elif kind == "shellwrite":
                ep["files"].add(payload)
            elif kind == "tool":
                nm, fp, sub = payload
                ep["tools"][nm] += 1
                if nm in MUTATORS and fp:
                    ep["files"].add(fp)
                if nm in AGENT_TOOLS:
                    ep["agents"] += 1
                    if sub:
                        ep["tools"][f"agent:{sub}"] += 1
            if d > ep["ended"] and (d - b).total_seconds() <= TAIL_GRACE:
                ep["ended"] = d
        eps.append(ep)
    return eps


def episode_title(ep, fallback):
    for _, text in ep["prompts"]:
        if not is_machinery(text) and _substantive(text):
            t = _title_from(text)
            if t:
                return t
    if ep["files"]:
        names = sorted({os.path.basename(f) for f in ep["files"]})[:3]
        return "Edits to " + ", ".join(names)
    return fallback or "Untitled work"


def episode_body(ep):
    chunks = [p for _, p in ep["prompts"] if not is_machinery(p)]
    chunks += [a[:600] for a in ep["asst"]]
    body = "\n".join(chunks)
    return body[:MAX_BODY]
