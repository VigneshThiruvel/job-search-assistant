"""Run every source adapter and store the results: `python -m job_search_assistant.ingest`."""

import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from job_search_assistant import db
from job_search_assistant.config import ROLES
from job_search_assistant.http import PoliteFetcher, default_client
from job_search_assistant.sources.adzuna import Adzuna
from job_search_assistant.sources.arbeitnow import Arbeitnow
from job_search_assistant.sources.base import Source
from job_search_assistant.sources.bundesagentur import Bundesagentur
from job_search_assistant.text import content_hash, detect_language

log = logging.getLogger(__name__)

DATA_DIR = Path("data")


def run_source(conn: sqlite3.Connection, source: Source) -> dict[str, int]:
    """Fetch, normalize and upsert one source; always records a row in `runs`."""
    started = datetime.now(UTC)
    run_id = db.start_run(conn, source.name, started)
    before = db.count_jobs(conn, source.name)
    fetched = errors = 0

    try:
        with conn:  # one transaction: a crash mid-source leaves no half-written run
            for raw in source.fetch():
                fetched += 1
                try:
                    job = source.normalize(raw)
                    db.upsert_job(
                        conn,
                        job,
                        lang=detect_language(f"{job.title}\n{job.description}"),
                        content_hash=content_hash(job.title, job.company, job.location),
                        seen_at=started,
                    )
                except Exception:
                    errors += 1
                    log.exception("%s: failed to store record", source.name)
    except Exception:
        errors += 1
        log.exception("%s: fetch failed", source.name)

    new = db.count_jobs(conn, source.name) - before
    db.finish_run(
        conn, run_id, finished_at=datetime.now(UTC), fetched=fetched, new=new, errors=errors
    )
    stats = {"fetched": fetched, "new": new, "errors": errors}
    log.info("%s: %s", source.name, stats)
    return stats


def build_sources(raw_dir: Path) -> list[Source]:
    client = default_client()
    sources: list[Source] = [
        Arbeitnow(PoliteFetcher(client, delay=3.0), raw_dir),
        Bundesagentur(PoliteFetcher(client, delay=1.5), raw_dir, queries=ROLES),
    ]
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if app_id and app_key:
        # 3 s keeps us under 25 hits/min; 7 countries x 7 roles = 49 hits/day.
        sources.append(
            Adzuna(PoliteFetcher(client, delay=3.0), raw_dir, app_id, app_key, queries=ROLES)
        )
    else:
        log.warning("adzuna: ADZUNA_APP_ID/ADZUNA_APP_KEY not set in .env, skipping")
    return sources


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include API keys
    load_dotenv()
    conn = db.connect(DATA_DIR / "jobs.db")
    try:
        for source in build_sources(DATA_DIR / "raw"):
            run_source(conn, source)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
