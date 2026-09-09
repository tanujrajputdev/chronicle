# Chronicle

**Claude Code writes down everything you do. Then it gives you one way to read it back: a list of stale titles.**

Chronicle indexes what is already on your disk and hands it back when it is useful — at the start of a session, before a compaction destroys your context, and when you start a task you have already done.

Everything runs locally. Nothing is uploaded. There is no account.

---

## The thing that convinced me to build it

Run this after indexing:

```bash
./chronicle-cli agents
```

Every time Claude Code spawns a subagent, that agent's full reasoning and conclusion are written to a file on your disk. On my machine there were **221 of them** — including a complete file-level design blueprint for a website — that I had paid for and never seen. No tool shows them to you. Chronicle does.

While you are there:

```bash
./chronicle-cli stats
```

Mine says 140 compactions. That is 140 times the context of my own work was summarised and thrown away.

---

## Install

Requires Python 3.11+ (no dependencies) and Claude Code.

```bash
git clone https://github.com/tanujrajputdev/chronicle.git
cd chronicle
./chronicle-cli index          # reads ~/.claude/projects — 490 MB took 3.4s
./chronicle-cli stats
```

That is the whole install. Nothing is written outside `~/.chronicle` (the index) until you opt in below.

To make it automatic:

```bash
./chronicle-cli install        # hooks + a 30-minute background re-index + the skill
./chronicle-cli install --mcp  # also expose it to any MCP client, read-only
```

Restart Claude Code afterwards. `./chronicle-cli install --uninstall` removes all of it.

---

## What it actually does

### It reads

Claude Code stores every session as JSONL under `~/.claude/projects`. Those files are organised the way the computer wrote them, not the way you worked — a single file can span weeks and carry one auto-generated title from the first ten minutes.

Chronicle splits them into **episodes**: contiguous stretches of real work, separated by idle gaps. My three largest session files contained 53 distinct episodes. Claude Code showed three titles.

It also resolves **projects properly**. Work launched in one folder routinely edits repos elsewhere — one of my projects turned out to span five separate repos. Chronicle attributes a project from the files an episode actually edits, not the directory it was launched in.

### It answers

```bash
./chronicle-cli search "why did we drop the redis cache"
./chronicle-cli project acme            # every repo, every episode, tokens, agents
./chronicle-cli timeline acme -n 20
./chronicle-cli show 118                # one episode: your prompts verbatim, files, agents
./chronicle-cli agent 155               # a subagent's task and full conclusion
./chronicle-cli map --cross             # work that landed outside the folder it started in
./chronicle-cli serve                   # a local dashboard on 127.0.0.1:7777
```

Episode ids are permanent. `#118` today is `#118` next year, so it is safe to write one down.

### It shows up on its own

This is the part that changes your day. After `install`, four hooks run:

| Hook | What happens |
|---|---|
| **SessionStart** | Every new session in a project opens knowing where you left off: last session, how it ended, open tasks, files in flight. About 1,200 characters. |
| **PreCompact** | The moment before Claude Code compacts, Chronicle saves what a summary destroys — your prompts verbatim, the open task list, files being edited — and `SessionStart` feeds it back into the fresh context. |
| **SessionEnd** | Re-indexes in the background. |
| **UserPromptSubmit** | Occasionally notes that you have solved this before. Costs 3 ms. |

See exactly what gets injected, any time:

```bash
./chronicle-cli brief          # what a new session here is told
./chronicle-cli recall         # the last pre-compaction checkpoint
./chronicle-cli why "some text"  # would the repeat-work check fire? which gate stopped it?
```

### It writes things you can send

```bash
./chronicle-cli report acme --since 7d -o update.md
```

A day-by-day work log built from the record — sessions, instructions, files changed, research produced, what is still open. It assembles facts and deliberately does not write prose, so nothing gets invented on your behalf.

---

## Privacy

- **Nothing leaves your machine.** No network calls, no account, no telemetry. The index is a SQLite file at `~/.chronicle/chronicle.db`.
- **Secrets are stripped as it reads.** API keys, tokens, private keys and password assignments are replaced with `[REDACTED:TYPE]` before anything is stored. It caught 26 in my history.
- **`.gitignore` protects you.** `aliases.json`, `projects/` and `MEMORY.md` contain your real project names and are never committed.

If you use Chronicle for client work, be aware the index contains client names and file paths. It stays on your disk, but back it up accordingly.

---

## Configuration

Optional. Copy `aliases.example.json` to `aliases.json` to tell Chronicle that several directories are one project:

```json
{
  "projects": {
    "/Users/you/work/acme-web": "acme",
    "/Users/you/work/acme-api": "acme"
  },
  "labels": { "acme": "Acme Corp" }
}
```

Then `./chronicle-cli index --rebuild`. Run `./chronicle-cli map --cross` to find directories that still land on the wrong project.

Environment variables: `CHRONICLE_ROOT` (index location), `CHRONICLE_SOURCE` (where to read sessions from), `CHRONICLE_GAP` (episode gap in seconds, default 14400), `CHRONICLE_NO_BRIEF`, `CHRONICLE_NO_RECALL`.

---

## Honest limitations

- **The 30-minute background re-index is macOS only** (it uses launchd). The hooks work everywhere, and they cover the same ground — you just lose the belt-and-braces refresh.
- **The repeat-work check is deliberately near-silent.** Replayed against all 1,136 prompts in my history it fired 4 times. Every one was correct, but the yield is low until your history is large.
- **The JSONL format is undocumented** and can change with a Claude Code release. The parser is defensive and fails soft, but a release could still break indexing.
- **Episode titles are heuristic**, taken from your first substantive prompt. They are recognisable, not elegant.
- **Tested on macOS with Python 3.11 and 3.13.** Linux should work; Windows is untested.

## License

MIT. See [LICENSE](LICENSE).
