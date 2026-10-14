"""SQLite schema and idempotent upsert of jobs."""

import json
import sqlite3
from datetime import datetime
from pathlib import Path

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
