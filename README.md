<div align="center">

# Chronicle

**Claude Code writes down everything you do. Then it gives you one way to read it back: a list of stale titles.**

Chronicle indexes the session history already on your disk and hands it back at the moment it is useful — when you start work, before a compaction destroys your context, and when you are about to redo something you already solved.

[![License: MIT](https://img.shields.io/badge/license-MIT-8C2F39.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-1F6257.svg)](https://www.python.org/downloads/)
[![Dependencies: none](https://img.shields.io/badge/dependencies-none-1F6257.svg)](#)
[![Network calls: zero](https://img.shields.io/badge/network%20calls-zero-8E7028.svg)](#privacy-and-security)

</div>

---

## Start here

```bash
git clone https://github.com/tanujrajputdev/chronicle.git
cd chronicle
./chronicle-cli index
./chronicle-cli agents
```

That last command is the one that tends to land. Every time Claude Code spawns a subagent, that agent's full reasoning and final answer are written to a file on your machine. Nothing surfaces them. On the machine this was built against there were **221** — including a complete file-level design blueprint for a website — all paid for, none ever read.

```
$ chronicle stats

  125 episodes across 13 projects · 47 active days · 2026-07-07 → 2026-09-09
  1155 prompts · 394h40m elapsed · 142 compactions survived
  221 agent runs (1.6M output tokens)
  51.0M output tokens · 9.5B cache reads
```

142 compactions is 142 times the context of your own work was summarised and thrown away.

---

## Contents

[Install](#install) · [What you get](#what-you-get) · [How it works](#how-it-works) · [Commands](#commands) · [The ambient layer](#the-ambient-layer) · [MCP](#mcp) · [Configuration](#configuration) · [Privacy](#privacy-and-security) · [Performance](#performance) · [Comparison](#how-it-compares) · [Limitations](#limitations) · [Contributing](#contributing)

---

## Install

Python 3.11 or newer, no packages, and Claude Code. Nothing is written outside `~/.chronicle` until you opt in.

```bash
git clone https://github.com/tanujrajputdev/chronicle.git && cd chronicle
./chronicle-cli index          # reads ~/.claude/projects
```

Then, to make it automatic:

```bash
./chronicle-cli install        # hooks, a 30-minute background re-index, and the skill
./chronicle-cli install --mcp  # also register the read-only MCP server
```

Restart Claude Code afterwards, then confirm everything is wired up:

```bash
./chronicle-cli doctor
```

`install` also links `chronicle` into the first writable directory on your `PATH`, so every
command below works from anywhere:

```bash
cd ~/any-project
chronicle brief          # no ./ and no cd needed
```

`./chronicle-cli install --uninstall` removes every trace, and backs up your `settings.json` first.

The installer **merges** into `~/.claude/settings.json` — hooks from other tools are left exactly where they are.

---

## What you get

### Search everything you have ever asked

```
$ chronicle search "why did we drop the cache"

  #118  2026-08-14  acme     Replace the read-through cache with a materialised view
        14 prompts · 3h12m · 402.1ktok  9 files  2 compactions
        … the «cache» was «dropped» because invalidation on variant writes was …
```

### Open a single piece of work

```
$ chronicle show 118

#118 — Replace the read-through cache with a materialised view
acme · 2026-08-14 09:41 to 17:03 · 3h12m · branch feat/mv-rollout
14 prompts · 402 tool calls · 402.1k output tokens · 2 compactions

Where the work landed
  /Users/you/work/acme-api   7 files
  /Users/you/work/acme-web   2 files

What you asked, verbatim (14)
  09:41  the product page is doing 40 queries per render, find out why
  10:02  don't add caching yet — show me the query plan first
  ...
```

Those verbatim prompts are exactly what a compaction summary destroys, and they are usually the real record of what you decided.

### Read the research you never saw

```
$ chronicle agent 155

Agent run @155 · acme · 2026-08-19 16:52 · 29 tool calls · 15.8k output tokens

--- TASK IT WAS GIVEN ---
You are producing a FILE-LEVEL implementation blueprint for …

--- WHAT IT CONCLUDED ---
…
```

### A work log you can send someone

```bash
chronicle report acme --since 7d -o update.md
```

Day by day: sessions, the instructions you gave, files changed, research produced, what is still open. It assembles facts and deliberately does **not** write prose, so nothing is invented on your behalf. Hand it to a model afterwards if you want it turned into a client update.

### A local dashboard

```bash
chronicle serve      # 127.0.0.1:7777
```

A developer console over the index: KPI row, token composition, a day-by-day activity grid,
project rollups with weekly sparklines, a dedicated token-usage page (by project, month and
day), an episode browser, the agent-run viewer, full-text search with highlighting, and
checkpoints. Press `/` anywhere to jump to search.

It is deliberately locked down. The connection to the index is read-only, the server answers
only to a loopback `Host` header — binding to `127.0.0.1` alone does not stop a page you visit
from rebinding its own hostname at you — every rendered string is escaped including FTS
snippets, and a strict CSP blocks any outbound request from the page.

---

## How it works

Four ideas do most of the work. Each is a deliberate departure from how Claude Code stores things.

### 1. A session is not a unit of work

Claude Code indexes by **file** and gives each one an auto-title. That assumes a session is a sitting. It often isn't: a single file can span weeks, and the title comes from whatever you happened to do in the first ten minutes.

Chronicle segments each file into **episodes** — contiguous stretches of work split on a four-hour idle gap, each with its own title, files, tools and cost. On the reference machine, the three largest session files held **53 distinct episodes**; Claude Code showed three titles.

Harness machinery (`<task-notification>`, `/compact`, command wrappers) never starts an episode and never becomes a title.

### 2. A project is not a directory

Work launched in one folder routinely edits repos elsewhere. One project on the reference machine turned out to span five separate repos; another was building an entire application in a different directory tree.

Chronicle attributes a project from **the files an episode actually edits**, not the working directory it was launched in. That includes files written through the shell: a `cat > file <<EOF`, a redirect or a `sed -i` reports no `file_path`, so without help that work lands in no project at all. `chronicle/shellwrite.py` recovers those paths, and it is deliberately the most conservative code in the project — heredoc bodies are stripped before anything is matched, a candidate has to look like a path, and the file has to exist when the index is built. `chronicle map --cross` shows any path still landing on the wrong project, and `aliases.json` fixes it permanently.

### 3. References must be permanent

`#118` is assigned once per `(session, sequence)` and never reassigned — it survives incremental indexing and `--rebuild` alike. Agent-run ids are keyed to their transcript file. An id you write down today still resolves next year.

### 4. Silence is the correct default

The repeat-work check runs on every prompt, so a false positive is a tax on your attention. It clears five gates before it says anything:

| Gate | Requirement |
|---|---|
| Distinctive words | ≥ 4 words in your prompt that are rare across your own history |
| Shared words | ≥ 3 of them appear in the candidate episode |
| Coverage | ≥ 60% of the distinctive words matched |
| Rarity | ≥ 3 of the shared words are genuinely rare (df ≤ 6%) |
| Size-normalised score | ≥ 0.030, so sprawling episodes stop matching everything |

Replayed against all 1,136 prompts in the reference history it fired **4 times**, and every one was correct. You can audit any decision it makes:

```
$ chronicle why "fix the button colour"

  · distinctive words      colour
  · stopped                only 1 distinctive words, need 4

  stays silent
```

### Data model

```
sessions ─┬─ episodes ─┬─ messages           your prompts, verbatim
          │            ├─ episode_projects   attribution: cwd + files touched
          │            └─ agent_runs         subagent task + conclusion
          └─ files                           incremental index state

episode_ids · agent_ids     stable references, survive --rebuild
terms · episode_terms       term statistics, computed at index time
episodes_fts · agents_fts   SQLite FTS5
```

One SQLite file at `~/.chronicle/chronicle.db` — 11 MB for 500 MB of transcripts.

---

## Commands

| Command | What it does |
|---|---|
| `index [--rebuild]` | Ingest new or changed sessions. Incremental and idempotent. |
| `doctor` | Check *this* install: environment, index, hooks, MCP, redaction. 24 checks. |
| `selftest` | Run the whole product against a synthetic corpus in a sandbox. 64 checks. |
| `stats` | Corpus overview. |
| `tokens [--by project\|day\|month]` | Every token spent, split into input, cache write, cache read and output. |
| `projects` | Every project, most recently worked first. |
| `project <name>` | One project in full: totals, every repo it spans, biggest work, agent runs. |
| `timeline [project]` | Episodes, newest first. |
| `search <query>` | Full-text over episodes and agent runs. FTS5 syntax. |
| `show <id>` | One episode: verbatim prompts, files, tools, agents. |
| `agents [-p project]` | Every subagent and workflow run. |
| `agent <id>` | One agent run: the task it was given and what it concluded. |
| `map [--cross]` | Where work launched in one folder actually landed. |
| `brief [project]` | What a new session here is told automatically. |
| `recall [session]` | The last pre-compaction checkpoint, in full. |
| `checkpoints` | Every checkpoint saved so far. |
| `why "<text>"` | Would the repeat-work check fire? Which gate stopped it? |
| `report [project] --since 7d` | A work log you can send someone. |
| `digest` | Regenerate `projects/<name>/DIGEST.md` and `MEMORY.md`. |
| `serve [-p 7777]` | Local dashboard. |
| `install [--mcp] [--uninstall]` | Hooks, background indexer, skill, MCP server. |

---

## The ambient layer

A dashboard is somewhere you have to remember to go. These arrive on their own.

| Hook | When | What happens |
|---|---|---|
| `SessionStart` | Every session | Injects a project brief: last session, how it ended, open tasks, files in flight. ~1,200 characters. |
| `PreCompact` | Before context is compressed | Saves your prompts verbatim, the open task list and files in flight — then `SessionStart` feeds them back into the fresh context. |
| `SessionEnd` | On exit | Re-indexes and regenerates digests in the background. |
| `UserPromptSubmit` | Every prompt | Debounced re-index, plus the repeat-work check. Costs 3 ms. |

A launchd job also re-indexes every 30 minutes, so nothing depends on a hook firing.

What a cold start looks like:

```
## Chronicle — Acme Corp, picking up from 2 days ago
**Last session (2026-09-07, #214):** buy now not showing as per session recordings…
**It ended on:** "also ive attached screenshots of more errors. fix all these errors."
**Open tasks when it stopped:** Redefine returning customer as delivered-then-returned
**Recently edited:** `snippets/product-schema.liquid`
_19 episodes over 17 days and 7 agent runs are indexed. Ask me anything about them —
nothing is loaded into context until you ask._
```

Every hook exits 0 on any failure and logs to `~/.chronicle/hooks.log`. A broken Chronicle can never block your session. The prompt path uses a read-only connection with a 60 ms timeout that gives up rather than waiting on a lock.

Turn pieces off with `CHRONICLE_NO_BRIEF=1` and `CHRONICLE_NO_RECALL=1`.

---

## MCP

```bash
./chronicle-cli install --mcp
```

Registers a **read-only** MCP server, so a session in any project can reach this history without leaving its directory. Nine tools: `projects`, `search`, `timeline`, `project`, `episode`, `agent_run`, `brief`, `report`, `recall`. Standard-library JSON-RPC over stdio, no SDK.

Costs about 740 tokens of tool definitions per session. If it isn't earning that, `install --uninstall` removes it.

---

## Configuration

Copy `aliases.example.json` to `aliases.json` to tell Chronicle that several directories are one project. It applies to the working directory **and** to every file an episode edits.

```json
{
  "projects": {
    "/Users/you/work/acme-web": "acme",
    "/Users/you/work/acme-api": "acme",
    "/Users/you/Documents/acme-legacy": "acme"
  },
  "labels": { "acme": "Acme Corp" },
  "ignore_roots": ["/private/tmp", "/tmp", "/var/folders"],
  "recall_exclude": []
}
```

Then `./chronicle-cli index --rebuild`. Run `chronicle map --cross` to find directories still landing on the wrong project.

| Variable | Default | Purpose |
|---|---|---|
| `CHRONICLE_ROOT` | `~/.chronicle` | Where the index, checkpoints and logs live |
| `CHRONICLE_SOURCE` | `~/.claude/projects` | Where to read sessions from |
| `CHRONICLE_GAP` | `14400` | Idle seconds that end an episode |
| `CHRONICLE_MEMORY` | the install dir | Where `aliases.json` and generated digests live |
| `CHRONICLE_PYTHON` | auto | Force a specific interpreter |
| `CHRONICLE_NO_BRIEF` | unset | Disable the session-start brief |
| `CHRONICLE_NO_RECALL` | unset | Disable the repeat-work check |

---

## Privacy and security

- **Zero network calls.** No account, no telemetry, no cloud. Grep the source.
- **Secrets are redacted at ingest, before anything is stored.** Anthropic, OpenAI, GitHub, Shopify, AWS, Google, Slack and Stripe keys, JWTs, private key blocks, bearer tokens and `password:`-style assignments become `[REDACTED:TYPE]`. It caught 26 on the reference machine.
- **`.gitignore` protects you.** `aliases.json`, `projects/` and `MEMORY.md` hold your real project names and are never committed.
- **Settings backups are `chmod 600`**, because `settings.json` often contains API keys.

The index still contains project names, file paths and the text of your prompts. It stays on your disk — back it up with that in mind.

---

## Performance

Measured against 500 MB of real transcripts, 125 episodes, 221 agent runs:

| Operation | Time |
|---|---|
| Full index from scratch | 3.4 s |
| Incremental re-index | 0.1 s |
| Search | 0.14 s |
| Repeat-work check | 3 ms |
| Pre-compaction checkpoint, 80 MB transcript | 2.4 s |
| Index size | 11 MB |

About 3,000 lines of Python, standard library only.

---

## How it compares

| | Chronicle | mem0 / supermemory | ccusage / vibe-log |
|---|---|---|---|
| Direction | Reconstructs what happened | Distils facts for future prompts | Counts tokens and time |
| Existing history | Indexes all of it | Starts from zero on install | Aggregates only |
| Transcripts | Kept and searchable | Explicitly discarded | Not read |
| Subagent results | Surfaced | — | — |
| Location | Your machine | Their cloud | Your machine |

They are not really competitors — they solve a different problem. If you want facts injected into future prompts, use one of those. Chronicle is for the record you already have.

---

## Limitations

- **The 30-minute background re-index is macOS only** (launchd). The hooks work everywhere and cover the same ground; you lose the belt-and-braces refresh.
- **The repeat-work check is near-silent by design.** 4 fires in 1,136 prompts. Yield grows with your history — with a small corpus most domain vocabulary is too common to count as distinctive.
- **The JSONL format is undocumented** and can change with a Claude Code release. The parser is defensive and fails soft, but a release could still break indexing.
- **Episode titles are heuristic**, taken from your first substantive prompt. Recognisable, not elegant.
- **Tested on macOS with Python 3.11 and 3.13.** Linux should work. Windows is untested.
- **One agent.** Codex, Cursor and Gemini CLI write comparable files but are not parsed yet.

---

## Contributing

Issues and pull requests welcome. Two rules that are not negotiable:

1. **No dependencies.** Standard library only.
2. **No network calls.** Anywhere, in any code path.

Before opening a PR:

```bash
chronicle selftest                        # 64 checks, ~7s, sandboxed
python3.11 -m py_compile chronicle/*.py   # the supported floor
```

`selftest` builds a synthetic corpus — including truncated JSON, binary junk, a 150 KB line
and records with a schema it has never seen — then asserts on known-correct answers: that a
six-hour gap yields exactly two episodes, that an edit in another repo is attributed there,
that a subagent's conclusion survives, that a token is redacted before it is stored, and that
episode ids do not move across a rebuild. It never reads your real transcripts and never
touches your index or `settings.json`.

**`doctor` and `selftest` answer different questions.** `doctor` asks *is my install wired up*;
`selftest` asks *does the code still work*. Run `selftest` before you push.

If you index your own history and something looks wrong — a bad episode split, a project attributed to the wrong repo, a title that makes no sense — that is the most useful issue you can file. Include the output of `chronicle stats` and `chronicle map --cross`.

---

## License

MIT — see [LICENSE](LICENSE).
