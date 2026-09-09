import sqlite3
from .config import DB_PATH

SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS files (
  path TEXT PRIMARY KEY, size INTEGER, mtime REAL,
  session_id TEXT, kind TEXT, indexed_at TEXT
);

CREATE TABLE IF NOT EXISTS projects (
  id TEXT PRIMARY KEY, name TEXT, paths TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY, project_id TEXT, cwd TEXT, branch TEXT,
  title TEXT, started TEXT, ended TEXT,
  n_user INTEGER, n_asst INTEGER, compactions INTEGER,
  out_tokens INTEGER, cache_read INTEGER, in_tokens INTEGER, cache_write INTEGER
);

-- ids are handed out once per (session, sequence) and never reassigned, so a
-- reference like "#118" keeps meaning the same work across re-indexes and rebuilds.
CREATE TABLE IF NOT EXISTS episode_ids (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT, seq INTEGER, UNIQUE(session_id, seq)
);

CREATE TABLE IF NOT EXISTS episodes (
  id INTEGER PRIMARY KEY,
  session_id TEXT, project_id TEXT, seq INTEGER,
  started TEXT, ended TEXT, duration_s INTEGER,
  n_prompts INTEGER, n_tools INTEGER, compactions INTEGER, n_agents INTEGER,
  out_tokens INTEGER, cache_read INTEGER, in_tokens INTEGER, cache_write INTEGER,
  title TEXT, opening_prompt TEXT, branch TEXT,
  files_touched TEXT, tools TEXT
);
CREATE INDEX IF NOT EXISTS ix_ep_proj ON episodes(project_id, started);
CREATE INDEX IF NOT EXISTS ix_ep_sess ON episodes(session_id, seq);

CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  episode_id INTEGER, session_id TEXT, role TEXT, ts TEXT, text TEXT,
  kind TEXT DEFAULT 'prompt'
);
CREATE INDEX IF NOT EXISTS ix_msg_ep ON messages(episode_id, ts);

CREATE TABLE IF NOT EXISTS agent_ids (
  id INTEGER PRIMARY KEY AUTOINCREMENT, path TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY,
  episode_id INTEGER, session_id TEXT, project_id TEXT,
  path TEXT UNIQUE, agent_type TEXT, workflow_id TEXT,
  ts TEXT, ended TEXT, prompt TEXT, result TEXT,
  n_tools INTEGER, out_tokens INTEGER, in_tokens INTEGER,
  cache_read INTEGER, cache_write INTEGER
);
CREATE INDEX IF NOT EXISTS ix_ar_ep ON agent_runs(episode_id);

CREATE VIRTUAL TABLE IF NOT EXISTS episodes_fts USING fts5(
  title, opening_prompt, body, tokenize='porter unicode61'
);

CREATE VIRTUAL TABLE IF NOT EXISTS agents_fts USING fts5(
  prompt, result, tokenize='porter unicode61'
);

CREATE TABLE IF NOT EXISTS episode_projects (
  episode_id INTEGER, project_id TEXT, source TEXT, n_files INTEGER,
  root TEXT, PRIMARY KEY (episode_id, project_id, root)
);
CREATE INDEX IF NOT EXISTS ix_epr_proj ON episode_projects(project_id);

CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def connect_ro(timeout_ms=60):
    """Read-only, and it gives up rather than waiting behind the indexer's lock.
    Used on the prompt path, where being slow is worse than being silent."""
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=timeout_ms / 1000)
    con.row_factory = sqlite3.Row
    con.execute(f"PRAGMA busy_timeout={timeout_ms}")
    return con


def reset(con):
    # episode_ids and agent_ids deliberately survive: they make references stable
    for t in ("files", "sessions", "episodes", "messages", "agent_runs", "episode_projects",
              "episodes_fts", "agents_fts", "projects"):
        con.execute(f"DELETE FROM {t}")
    con.commit()
