"""SQLite store for the dashboard (hunt.db).

Every table here is derived from pipeline files (out/, discovered.csv, watch_state.json, companies.csv) and
rebuilt by `ingest`, so a schema change simply drops and recreates them.
"""

import sqlite3
from pathlib import Path

DB_FILE = "hunt.db"
SCHEMA_VERSION = 1

DERIVED_TABLES = ("runs", "results", "roles", "watch_postings", "leads", "meta")

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
    """Opens <root>/hunt.db, (re)creating the derived tables when the schema version changed."""
    conn = sqlite3.connect(Path(root) / DB_FILE, timeout=10)
    conn.row_factory = sqlite3.Row
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        with conn:
            for table in DERIVED_TABLES:
                conn.execute(f"DROP TABLE IF EXISTS {table}")
            conn.executescript(SCHEMA)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return conn
