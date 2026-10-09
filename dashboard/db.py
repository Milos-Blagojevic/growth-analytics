"""SQLite store for the dashboard (hunt.db).

Two kinds of tables:
- Derived (DERIVED_TABLES): copies of the pipeline files (out/, discovered.csv, watch_state.json, companies.csv),
  rebuilt by `ingest`. A schema change bumps SCHEMA_VERSION, which drops and recreates them.
- User (tracking, side_projects, jobs): the owner's statuses, notes, side projects and dashboard-started jobs.
  They cannot be rebuilt, so they are never dropped; change them only by appending to USER_MIGRATIONS.
"""

import sqlite3
from pathlib import Path

DB_FILE = "hunt.db"
SCHEMA_VERSION = 1

DERIVED_TABLES = ("runs", "results", "roles", "watch_postings", "leads", "meta")

# Applied in order, each once; the number applied is kept in user_schema. Never edit an applied entry.
USER_MIGRATIONS = [
    """
    CREATE TABLE tracking (
        kind TEXT NOT NULL,           -- posting (ref = posting URL) | company (ref = domain, no posting)
        ref TEXT NOT NULL,
        status TEXT NOT NULL,         -- see app.STATUSES; "new" rows are not stored
        notes TEXT NOT NULL DEFAULT '',
        applied_at TEXT,              -- first time the status became applied
        updated_at TEXT NOT NULL,
        PRIMARY KEY (kind, ref)
    );
    CREATE TABLE side_projects (
        id INTEGER PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        gap TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,         -- idea | active | done | dropped | dismissed (a hidden scorer suggestion)
        source TEXT NOT NULL,         -- manual | scorer
        idea_key TEXT UNIQUE,         -- scorer suggestions: queries.idea_key(domain, text); NULL for manual
        origin_domain TEXT,
        origin_name TEXT,
        repo_url TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE jobs (
        id INTEGER PRIMARY KEY,
        kind TEXT NOT NULL,           -- run | discover | watch | send
        args TEXT NOT NULL,           -- JSON list of hunt.py arguments
        label TEXT NOT NULL,
        estimate TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,         -- running | succeeded | failed | cancelled | interrupted
        exit_code INTEGER,
        run_id TEXT,                  -- out/<run_id>/ the job saved, parsed from its log
        started_at TEXT NOT NULL,
        finished_at TEXT
    );
    """,
]

SCHEMA = """
CREATE TABLE runs (
    id TEXT PRIMARY KEY,          -- out/<id>/, a %Y%m%d-%H%M%S timestamp
    mode TEXT,                    -- research_only | dry_run | deliver; NULL for runs saved before the manifest
    started_at TEXT,
    saved_at TEXT NOT NULL,
    companies INTEGER NOT NULL,
    errors INTEGER NOT NULL,
    exa_requests INTEGER,
    exa_dollars REAL,
    model TEXT,
    tokens_in INTEGER,
    tokens_out INTEGER
);
CREATE TABLE results (
    run_id TEXT NOT NULL REFERENCES runs(id),
    domain TEXT NOT NULL,
    name TEXT NOT NULL,
    verified INTEGER,
    business_summary TEXT,
    signals TEXT,
    role_source TEXT,
    dead_roles TEXT,              -- JSON [{"title", "url"}]
    grounding TEXT,               -- JSON {field: {"confidence", "urls"}}
    score INTEGER,                -- NULL when not scored (research-only run or error)
    verdict TEXT,
    best_role_title TEXT,
    best_role_url TEXT,
    location_assessment TEXT,
    next_step TEXT,
    gap_note TEXT,
    scope_note TEXT,
    reasons TEXT,                 -- JSON list
    concerns TEXT,                -- JSON list
    proof_ideas TEXT,             -- JSON list
    error TEXT,
    PRIMARY KEY (run_id, domain)
);
CREATE INDEX results_domain ON results(domain, run_id);
CREATE TABLE roles (
    run_id TEXT NOT NULL,
    domain TEXT NOT NULL,
    url TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    remote_or_relocation TEXT,
    link_status TEXT,             -- live | unverified
    PRIMARY KEY (run_id, domain, url)
);
CREATE TABLE watch_postings (
    board TEXT NOT NULL,
    job_id TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,                -- NULL for entries saved before watch stored it
    remote INTEGER,
    url TEXT,
    first_seen TEXT NOT NULL,
    PRIMARY KEY (board, job_id)
);
CREATE TABLE leads (
    domain TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    why TEXT,
    focus TEXT,
    discovered_at TEXT,
    in_companies INTEGER NOT NULL -- listed in companies.csv, so the next run researches it
);
CREATE TABLE meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(root):
    """Opens <root>/hunt.db: recreates the derived tables when SCHEMA_VERSION changed, applies new user migrations."""
    conn = sqlite3.connect(Path(root) / DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")  # readers do not block the job watcher's writes
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        drops = "".join(f"DROP TABLE IF EXISTS {table};" for table in DERIVED_TABLES)
        conn.executescript(f"BEGIN; {drops} {SCHEMA} PRAGMA user_version = {SCHEMA_VERSION}; COMMIT;")
    conn.execute("CREATE TABLE IF NOT EXISTS user_schema (version INTEGER NOT NULL)")
    row = conn.execute("SELECT version FROM user_schema").fetchone()
    applied = row[0] if row else 0
    for version, script in enumerate(USER_MIGRATIONS[applied:], start=applied + 1):
        conn.executescript(
            f"BEGIN; {script} DELETE FROM user_schema; INSERT INTO user_schema VALUES ({version}); COMMIT;"
        )
    return conn
