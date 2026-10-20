"""SQLite store for the dashboard (hunt.db).

Two kinds of tables:
- Derived (DERIVED_TABLES): copies of the pipeline files (out/, discovered.csv, watch_state.json, companies.csv,
  interviews/, mail/), rebuilt by `ingest`. A schema change bumps SCHEMA_VERSION, which drops and recreates them.
- User (tracking, side_projects, jobs, mail_decisions): the owner's statuses, notes, side projects,
  dashboard-started jobs and answers to email suggestions. They cannot be rebuilt, so they are never dropped;
  change them only by appending to USER_MIGRATIONS.
"""

import sqlite3
from pathlib import Path

DB_FILE = "hunt.db"
SCHEMA_VERSION = 3

DERIVED_TABLES = ("runs", "results", "roles", "watch_postings", "leads", "interviews", "mail_updates", "meta")

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
    """
    CREATE TABLE mail_decisions (
        update_id TEXT PRIMARY KEY,   -- mail_updates.id: the email's Internet message ID
        decision TEXT NOT NULL,       -- applied | dismissed
        kind TEXT,                    -- applied: the tracked item whose status was set (tracking.kind, ref)
        ref TEXT,
        status TEXT,                  -- applied: the status set
        decided_at TEXT NOT NULL
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
CREATE TABLE interviews (
    id TEXT PRIMARY KEY,          -- interviews/<id>/
    company TEXT NOT NULL,
    domain TEXT,
    role TEXT NOT NULL,
    stage TEXT NOT NULL,
    scheduled_at TEXT,            -- local date and time; NULL if not set
    sort_at TEXT NOT NULL,        -- scheduled_at, else created_at
    interview TEXT NOT NULL,      -- JSON: interview.json
    transcript TEXT,              -- JSON: transcript.json without its turns, plus measured metrics; NULL if none
    prep TEXT,                    -- JSON: prep.json; NULL if none
    review TEXT                   -- JSON: review.json; NULL if none
);
CREATE TABLE mail_updates (
    id TEXT PRIMARY KEY,          -- the email's Internet message ID; a re-sent update keeps the first file's row
    file TEXT NOT NULL,           -- mail/<file>
    received_at TEXT NOT NULL,    -- local time
    sender TEXT NOT NULL,         -- "Name <address>"
    from_address TEXT NOT NULL,
    subject TEXT NOT NULL,
    web_link TEXT,
    outcome TEXT NOT NULL,        -- mail.SUGGESTED_STATUS keys: rejected | interview
    company TEXT NOT NULL,        -- as the email names it; "" if it does not
    role TEXT NOT NULL,
    summary TEXT NOT NULL
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
