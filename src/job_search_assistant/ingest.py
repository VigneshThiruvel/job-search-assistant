"""Run every source adapter and store the results: `python -m job_search_assistant.ingest`."""

import argparse
import json
import logging
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from dotenv import load_dotenv

from job_search_assistant import db, dedup
from job_search_assistant.config import ROLES
from job_search_assistant.http import PoliteFetcher, default_client
from job_search_assistant.mailbox import Mailbox
from job_search_assistant.sources.adzuna import Adzuna
from job_search_assistant.sources.arbeitnow import Arbeitnow
from job_search_assistant.sources.base import Source
from job_search_assistant.sources.bundesagentur import Bundesagentur
from job_search_assistant.sources.email_alerts import linkedin, xing
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


def reprocess(conn: sqlite3.Connection, sources: list[Source]) -> int:
    """Re-normalize stored `raw_json` with the current adapter code; no network.

    Use after fixing a `normalize()` bug. Only content fields change:
    `first_seen`/`last_seen` keep their values.
    """
    updated = 0
    with conn:
        for source in sources:
            rows = conn.execute(
                "SELECT id, raw_json FROM jobs WHERE source = ?", (source.name,)
            ).fetchall()
            for job_id, raw_json in rows:
                job = source.normalize(json.loads(raw_json))
                conn.execute(
                    """UPDATE jobs SET title = ?, company = ?, location = ?, country = ?,
                       remote = ?, description = ?, url = ?, lang = ? WHERE id = ?""",
                    (
                        job.title,
                        job.company,
                        job.location,
                        job.country,
                        job.remote,
                        job.description,
                        job.url,
                        detect_language(f"{job.title}\n{job.description}"),
                        job_id,
                    ),
                )
                updated += 1
    return updated


def build_sources(raw_dir: Path) -> list[Source]:
    client = default_client()
    sources: list[Source] = [
        Arbeitnow(PoliteFetcher(client, delay=3.0), raw_dir),
        Bundesagentur(PoliteFetcher(client, delay=1.5), raw_dir, queries=ROLES),
    ]
    app_id, app_key = os.getenv("ADZUNA_APP_ID"), os.getenv("ADZUNA_APP_KEY")
    if app_id and app_key:
        # 3 s keeps us under 25 hits/min; 7 roles x 3 countries = 21 hits/day.
        sources.append(
            Adzuna(PoliteFetcher(client, delay=3.0), raw_dir, app_id, app_key, queries=ROLES)
        )
    else:
        log.warning("adzuna: ADZUNA_APP_ID/ADZUNA_APP_KEY not set in .env, skipping")
    address, password = os.getenv("GMAIL_ADDRESS"), os.getenv("GMAIL_APP_PASSWORD")
    if address and password:
        mailbox = Mailbox(address.strip(), password)
        sources += [linkedin(mailbox, raw_dir), xing(mailbox, raw_dir)]
    else:
        log.warning("email alerts: GMAIL_ADDRESS/GMAIL_APP_PASSWORD not set in .env, skipping")
    return sources


def run_all(conn: sqlite3.Connection, raw_dir: Path = DATA_DIR / "raw") -> int:
    """Every source, then the duplicate groups; returns the number of groups."""
    for source in build_sources(raw_dir):
        run_source(conn, source)
    groups = dedup.rebuild_groups(conn)
    log.info("cross-source duplicate groups: %d", groups)
    return groups


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include API keys
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m job_search_assistant.ingest")
    parser.add_argument(
        "--reprocess", action="store_true", help="re-normalize stored raw_json, no fetching"
    )
    args = parser.parse_args()
    conn = db.connect(DATA_DIR / "jobs.db")
    try:
        if args.reprocess:
            log.info("reprocessed %d jobs", reprocess(conn, build_sources(DATA_DIR / "raw")))
            log.info("cross-source duplicate groups: %d", dedup.rebuild_groups(conn))
        else:
            run_all(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
