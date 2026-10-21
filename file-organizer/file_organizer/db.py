"""SQLite storage: file index, hash cache, sorting plan and the undo journal."""
from __future__ import annotations

import json
import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,              -- 'dryrun' | 'execute'
    source TEXT, dest TEXT, options TEXT,
    started REAL, finished REAL,
    status TEXT,                     -- running | complete | cancelled | failed | reverted
    summary TEXT
);
CREATE TABLE IF NOT EXISTS files(
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,
    path TEXT NOT NULL,
    ext TEXT, size INTEGER, ctime REAL, mtime REAL, mime TEXT,
    category TEXT, subcategory TEXT,
    partial_hash TEXT, hash TEXT,
    cand INTEGER DEFAULT 0,          -- duplicate-detection stage marker
    is_dup INTEGER DEFAULT 0,        -- 1 = extra copy of an identical file
    cleanup INTEGER DEFAULT 0        -- 1 = executable/system file flagged for review
);
CREATE INDEX IF NOT EXISTS idx_files_run_size ON files(run_id, size);
CREATE INDEX IF NOT EXISTS idx_files_run_cat ON files(run_id, category);
CREATE TABLE IF NOT EXISTS tokens(
    file_id INTEGER NOT NULL,
    tok TEXT NOT NULL,               -- canonical key (see lexicon.canon)
    src INTEGER NOT NULL,            -- 1 file name, 2 folder name, 3 reference code, 4 text
    w REAL NOT NULL,
    surf TEXT                        -- original spelling, for names/folders/codes
);
CREATE TABLE IF NOT EXISTS hash_cache(
    path TEXT NOT NULL, algo TEXT NOT NULL, size INTEGER, mtime REAL,
    partial TEXT, "full" TEXT,
    PRIMARY KEY(path, algo)
);
CREATE TABLE IF NOT EXISTS plan(
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL,         -- the dry run that produced this row
    file_id INTEGER,
    src TEXT NOT NULL, dst TEXT, dst_key TEXT,
    size INTEGER, mtime REAL,
    status TEXT NOT NULL DEFAULT 'planned',  -- planned|skipped|done|failed|changed
    reason TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_plan_dst ON plan(run_id, dst_key);
CREATE INDEX IF NOT EXISTS idx_plan_status ON plan(run_id, status, id);
CREATE TABLE IF NOT EXISTS journal(
    id INTEGER PRIMARY KEY,
    exec_id INTEGER NOT NULL,        -- the execute run
    plan_id INTEGER,
    op TEXT,                         -- copy | move | symlink
    src TEXT, dst TEXT, size INTEGER,
    status TEXT,                     -- pending | done | failed | skipped | reverted
    error TEXT, ts REAL
);
CREATE INDEX IF NOT EXISTS idx_journal_exec ON journal(exec_id, id);
"""


def connect(path: str) -> sqlite3.Connection:
    """Open a connection. Every thread must open its own."""
    conn = sqlite3.connect(path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA cache_size=-65536")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the first release to an existing database."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(files)")}
    for col, typ in (("taken", "REAL"), ("lat", "REAL"), ("lon", "REAL"),
                     ("country", "TEXT"), ("trip", "TEXT"),
                     ("project", "TEXT"), ("role", "TEXT"), ("status", "TEXT"), ("stem", "TEXT"),
                     ("subfolder", "TEXT"), ("subgroup", "TEXT"), ("reason", "TEXT"),
                     ("cluster", "INTEGER")):
        if col not in have:
            conn.execute(f"ALTER TABLE files ADD COLUMN {col} {typ}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_geo ON files(run_id, country, taken)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_files_project ON files(run_id, project)")
    conn.commit()


def new_run(conn: sqlite3.Connection, kind: str, source: str, dest: str, options: dict) -> int:
    cur = conn.execute(
        "INSERT INTO runs(kind, source, dest, options, started, status) VALUES (?,?,?,?,?, 'running')",
        (kind, source, dest, json.dumps(options), time.time()),
    )
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, summary: dict | None = None) -> None:
    conn.execute(
        "UPDATE runs SET status=?, finished=?, summary=? WHERE id=?",
        (status, time.time(), json.dumps(summary) if summary is not None else None, run_id),
    )
    conn.commit()


def iter_batches(conn: sqlite3.Connection, sql: str, params: tuple, batch: int):
    """Stream rows page by page without holding the table in memory.

    `sql` must end with 'AND <alias.>id > ? ORDER BY <alias.>id LIMIT ?' and select id first.
    """
    last = 0
    while True:
        rows = conn.execute(sql, (*params, last, batch)).fetchall()
        if not rows:
            return
        yield rows
        last = rows[-1][0]
