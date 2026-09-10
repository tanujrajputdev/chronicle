"""Recover file paths from Bash commands that write.

Claude Code reports `file_path` for Edit/Write/MultiEdit/NotebookEdit, so those
attribute cleanly. A shell heredoc, a redirect or `sed -i` writes a file just as
really and reports nothing, so that work lands in no project at all.

This is the only place in Chronicle that guesses. It guesses conservatively,
because a wrong path is worse than no path: it files real work under the wrong
project, silently, with nothing to notice. Three rules keep it honest.

1. Heredoc bodies are stripped before anything is matched. They are data, not
   shell syntax, and they are full of `>` — markdown quotes, JSX, jinja, regexes,
   commit messages. Left in, they produce "paths" like `,` and `The`.
2. A candidate must look like a path: no shell metacharacters, and either a
   directory separator or an extension.
3. The file must exist when the index is built. This is the gate that does the
   most work. It costs one stat, it drops junk that survived rules 1 and 2, and
   a scratch file that no longer exists should not be steering attribution
   anyway.
"""

import os, re

# `<<EOF`, `<<-'EOF'`, `<< "EOF"` — the terminator is what we scan forward for.
HEREDOC_RX = re.compile(r"<<-?\s*[\"']?(\w+)[\"']?")

# Shell-metacharacter-free, and plausibly a path rather than a bare word.
PATHISH_RX = re.compile(r"^[A-Za-z0-9_./~@+-]+$")
EXT_RX = re.compile(r"\.[A-Za-z0-9]{1,6}$")

# Each rule captures the *destination*. Ordered most specific first.
RULES = (
    # cat > out.md <<'EOF' / tee -a out.md <<EOF
    ("heredoc", re.compile(r"\b(?:cat|tee)\s+(?:-a\s+)?>{0,2}\s*([A-Za-z0-9_./~@+-]+)\s*<<")),
    # sed -i '' -e 's/x/y/' file.py
    ("sed", re.compile(r"\bsed\s+-[a-zA-Z]*i[a-zA-Z]*\s+(?:[\"']{2}\s+)?(?:-e\s+)?"
                       r"(?:[\"'][^\"']*[\"']\s+)?([A-Za-z0-9_./~@+-]+)")),
    ("tee", re.compile(r"\btee\s+(?:-a\s+)?([A-Za-z0-9_./~@+-]+)")),
    ("copy", re.compile(r"\b(?:cp|mv)\s+(?:-[a-zA-Z]+\s+)*[A-Za-z0-9_./~@+-]+\s+"
                        r"([A-Za-z0-9_./~@+-]+)")),
    ("touch", re.compile(r"\btouch\s+([A-Za-z0-9_./~@+-]+)")),
    # plain `> out` / `>> out`, but not `2>`, `>&2`, or a `->` arrow
    ("redirect", re.compile(r"(?:^|[;&|]\s*|\)\s*)[^;&|<>\n]*?(?<![0-9<>=-])>{1,2}\s*"
                            r"([A-Za-z0-9_./~@+-]+)")),
)

# Never attribute work to these, whatever the command said.
SKIP_RX = re.compile(r"^(/dev/|/proc/|/sys/)")


def strip_heredocs(cmd):
    """Drop heredoc bodies, keeping the line that opens them.

    Everything between `<<EOF` and a line that is exactly `EOF` is content the
    user wrote, not shell the user ran. Scanning it for redirects is how you end
    up attributing an episode to a file called `The`.
    """
    lines = cmd.split("\n")
    out = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = HEREDOC_RX.search(line)
        if m:
            term = m.group(1)
            i += 1
            while i < len(lines) and lines[i].strip() != term:
                i += 1  # body, discarded
        i += 1
    return "\n".join(out)


def _plausible(target):
    if not target or not PATHISH_RX.match(target):
        return False
    if SKIP_RX.match(target):
        return False
    if target.startswith("-") or target in (".", ".."):
        return False
    # a bare word with no separator and no extension is a stream name, a
    # variable, or noise — not a file we should file work under
    return "/" in target or bool(EXT_RX.search(target))


def targets(command, cwd=None, exists=os.path.exists):
    """Absolute paths a Bash command appears to have written.

    `exists` is injectable so the test suite can describe a filesystem without
    building one. Relative paths are resolved against `cwd`; without a cwd they
    cannot be resolved and are dropped rather than guessed at.
    """
    if not command or ">" not in command and not any(
        k in command for k in ("sed", "tee", "cp ", "mv ", "touch")
    ):
        return []

    found = []
    seen = set()
    cleaned = strip_heredocs(command)
    for _rule, rx in RULES:
        for m in rx.finditer(cleaned):
            t = m.group(1).strip("\"'")
            if not _plausible(t):
                continue
            p = os.path.expanduser(t)
            if not os.path.isabs(p):
                if not cwd:
                    continue
                p = os.path.normpath(os.path.join(cwd, p))
            if p in seen:
                continue
            seen.add(p)
            if exists(p):
                found.append(p)
    return found
