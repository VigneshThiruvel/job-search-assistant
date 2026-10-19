"""SQLite schema and idempotent upsert of jobs."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

import sqlite_vec

from job_search_assistant.models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY,
    source        TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    url           TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    location      TEXT,
    country       TEXT,
    remote        INTEGER,            -- 0/1, NULL = unknown
    description   TEXT NOT NULL,
    posted_at     TEXT,               -- ISO 8601, UTC
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    lang          TEXT,
    content_hash  TEXT NOT NULL,
    raw_json      TEXT NOT NULL,
    UNIQUE (source, source_job_id)
);
CREATE INDEX IF NOT EXISTS idx_jobs_content_hash ON jobs (content_hash);

-- Derived: cross-source duplicate groups, rebuilt after every ingest (see dedup.py).
CREATE TABLE IF NOT EXISTS job_groups (
    job_id   INTEGER PRIMARY KEY REFERENCES jobs (id),
    group_id INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_job_groups_group ON job_groups (group_id);

-- Phase 2: one row per (job, model, prompt version); never overwritten.
CREATE TABLE IF NOT EXISTS extractions (
    id                INTEGER PRIMARY KEY,
    job_id            INTEGER NOT NULL REFERENCES jobs (id),
    model             TEXT NOT NULL,        -- requested model (cache key)
    served_model      TEXT,                 -- differs if a refusal fallback answered
    prompt_version    TEXT NOT NULL,
    effort            TEXT,
    extracted_at      TEXT NOT NULL,
    output_json       TEXT NOT NULL,
    input_tokens      INTEGER,
    cache_read_tokens INTEGER,
    output_tokens     INTEGER,
    UNIQUE (job_id, model, prompt_version)
);

-- Phase 3: one vector per (job, embedding model); re-embedded when the text changes.
CREATE TABLE IF NOT EXISTS embeddings (
    job_id      INTEGER NOT NULL REFERENCES jobs (id),
    model       TEXT NOT NULL,
    text_hash   TEXT NOT NULL,           -- hash of the embedded text (cache key)
    embedded_at TEXT NOT NULL,
    vector      BLOB NOT NULL,           -- float32, L2-normalized (sqlite-vec format)
    PRIMARY KEY (job_id, model)
);

CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    fetched     INTEGER NOT NULL DEFAULT 0,
    new         INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0
);
"""

# On a repeat sighting, refresh content (postings get edited) and last_seen,
# but never touch first_seen.
UPSERT = """
INSERT INTO jobs (
    source, source_job_id, url, title, company, location, country, remote,
    description, posted_at, first_seen, last_seen, lang, content_hash, raw_json
) VALUES (
    :source, :source_job_id, :url, :title, :company, :location, :country, :remote,
    :description, :posted_at, :seen_at, :seen_at, :lang, :content_hash, :raw_json
)
ON CONFLICT (source, source_job_id) DO UPDATE SET
    url          = excluded.url,
    title        = excluded.title,
    company      = excluded.company,
    location     = excluded.location,
    country      = excluded.country,
    remote       = excluded.remote,
    description  = excluded.description,
    posted_at    = excluded.posted_at,
    last_seen    = excluded.last_seen,
    lang         = excluded.lang,
    content_hash = excluded.content_hash,
    raw_json     = excluded.raw_json
"""


def connect(path: Path | str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    # sqlite-vec adds vector functions (vec_distance_cosine, ...) to this connection.
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(SCHEMA)
    return conn


def upsert_job(
    conn: sqlite3.Connection, job: Job, *, lang: str | None, content_hash: str, seen_at: datetime
) -> None:
    conn.execute(
        UPSERT,
        {
            "source": job.source,
            "source_job_id": job.source_job_id,
            "url": job.url,
            "title": job.title,
            "company": job.company,
            "location": job.location,
            "country": job.country,
            "remote": job.remote,
            "description": job.description,
            "posted_at": job.posted_at.isoformat() if job.posted_at else None,
            "seen_at": seen_at.isoformat(),
            "lang": lang,
            "content_hash": content_hash,
            "raw_json": json.dumps(job.raw, ensure_ascii=False),
        },
    )


def count_jobs(conn: sqlite3.Connection, source: str) -> int:
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE source = ?", (source,)).fetchone()[0]


def start_run(conn: sqlite3.Connection, source: str, started_at: datetime) -> int:
    cur = conn.execute(
        "INSERT INTO runs (source, started_at) VALUES (?, ?)", (source, started_at.isoformat())
    )
    conn.commit()
    return cur.lastrowid


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    finished_at: datetime,
    fetched: int,
    new: int,
    errors: int,
) -> None:
    conn.execute(
        "UPDATE runs SET finished_at = ?, fetched = ?, new = ?, errors = ? WHERE id = ?",
        (finished_at.isoformat(), fetched, new, errors, run_id),
    )
    conn.commit()
