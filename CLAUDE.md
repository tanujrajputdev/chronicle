# Chronicle — working on the code

Chronicle indexes Claude Code session history from `~/.claude/projects` into SQLite.
See `README.md` for what it does and how to install it.

**Never read `~/.claude/projects/**/*.jsonl` directly** — they are hundreds of megabytes and a
single file can exceed any context window. Use `./chronicle-cli` instead; it answers in
milliseconds.

## Layout

| Path | What it is |
|---|---|
| `chronicle/ingest.py` | Streaming JSONL parser and the episode segmenter |
| `chronicle/indexer.py` | Builds the index; owns project attribution and stable ids |
| `chronicle/recall.py` | Repeat-work matching, plus the term statistics behind it |
| `chronicle/checkpoint.py` | Pre-compaction rescue |
| `chronicle/brief.py` | The session-start catch-up |
| `chronicle/hooks.py` | Claude Code hook handlers — must be fast and must never raise |
| `chronicle/mcp.py` | Read-only MCP server, stdlib JSON-RPC over stdio |
| `chronicle/web.py` | Local dashboard |
| `skill/SKILL.md.tmpl` | Rendered into `~/.claude/skills/` by `install` |
| `~/.chronicle/` | The index, checkpoints, and logs — outside the repo |

## Rules that matter

- **No dependencies.** Standard library only, Python 3.11+. Do not add a package.
- **Hooks run inside the user's session.** Every handler exits 0 on any failure and writes to
  `~/.chronicle/hooks.log` rather than disturbing the session. The prompt path uses a read-only
  connection that gives up rather than waiting on a lock.
- **Ids are permanent.** `episode_ids` and `agent_ids` survive `--rebuild` on purpose. Never
  reassign them; references in reports and checkpoints depend on it.
- **Redact before storing**, never after. See `chronicle/redact.py`.
- **Nothing leaves the machine.** No network calls in any code path.

## Checking a change

```bash
./chronicle-cli index --rebuild && ./chronicle-cli stats
./chronicle-cli why "some prompt text"     # audit the repeat-work gates
python3.11 -m py_compile chronicle/*.py    # the supported floor
```
