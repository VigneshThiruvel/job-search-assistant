"""Phase 6, part 1: the daily pipeline. Deterministic steps only, no LLM.

    uv run python -m job_search_assistant.daily     # ingest, import pasted texts, embed
    uv run python -m job_search_assistant.daily --skip-ingest   # after pasting into data/inbox/
    /mcp__job-search__daily_run                     # part 2, in Claude Code: the agent

Fetching, deduplicating and embedding never need judgement, so they are plain
code (a workflow); the agent gets only the steps that do: extracting, mapping
new skills, explaining fit. Safe to re-run: same-day fetches hit the raw cache,
upserts are idempotent and only new or changed postings are embedded.
"""

import argparse
import logging
import sqlite3
from collections import Counter
from datetime import UTC, datetime

from dotenv import load_dotenv

from job_search_assistant import db, embed, extract, inbox, ingest, search

log = logging.getLogger(__name__)


def status(conn: sqlite3.Connection, today: str) -> dict[str, int]:
    """What the agent will find: today's new jobs, postings waiting for it, the queue."""
    new_today = conn.execute(
        """SELECT count(*) FROM (
               SELECT COALESCE(g.group_id, j.id) AS grp FROM jobs j
               LEFT JOIN job_groups g ON g.job_id = j.id
               GROUP BY grp HAVING min(j.first_seen) >= ?)""",
        (today,),
    ).fetchone()[0]
    queue = Counter(dict(conn.execute("SELECT status, count(*) FROM applications GROUP BY status")))
    return {
        "new_today": new_today,
        "pending_extraction": len(
            set(extract.pending_target(conn, search.current_extraction()))
            & inbox.fresh_job_ids(conn)
        ),
        "queued": queue["queued"],
        "selected": queue["selected"],
        "ready": queue["ready"],
    }


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines include API keys
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m job_search_assistant.daily")
    parser.add_argument("--skip-ingest", action="store_true", help="only embed + report")
    parser.add_argument("--work-minutes", type=float, default=5, help="embedding duty cycle")
    parser.add_argument("--rest-minutes", type=float, default=10)
    args = parser.parse_args()

    conn = db.connect(ingest.DATA_DIR / "jobs.db")
    try:
        if not args.skip_ingest:
            with conn:
                conn.execute("INSERT INTO daily_runs (started_at) VALUES (?)",
                             (datetime.now(UTC).isoformat(),))  # fmt: skip
            ingest.run_all(conn)
        imported, problems = inbox.import_inbox(conn)
        for p in problems:
            log.warning("inbox: %s", p)
        if conn.execute(
            """SELECT 1 FROM jobs j WHERE description != '' AND NOT EXISTS
                   (SELECT 1 FROM embeddings e WHERE e.job_id = j.id AND e.model = ?)""",
            (embed.DEFAULT_MODEL,),
        ).fetchone():  # the encoder takes ~10 s to load; skip it when nothing is new
            duty = (args.work_minutes * 60, args.rest_minutes * 60)
            encoder = embed.load_encoder(embed.DEFAULT_MODEL)
            embed.embed_jobs(conn, encoder, embed.DEFAULT_MODEL, duty_cycle=duty)
        s = status(conn, datetime.now(UTC).date().isoformat())
        templates = inbox.write_templates(conn, inbox.mail_window_start(conn))
        waiting = sorted(p.name for p in inbox.INBOX.glob("*.txt"))
    finally:
        conn.close()

    if imported:
        print(f"\nImported {len(imported)} pasted description(s) from {inbox.INBOX}/.")
    if waiting:
        print(f"\n{len(waiting)} LinkedIn/XING job(s) need a description ({len(templates)} new). "
              f"Open each file in {inbox.INBOX}/, follow its link, paste below the line, "
              "save, then run `daily --skip-ingest`:")  # fmt: skip
        print("\n".join(f"  {name}" for name in waiting))

    print(
        f"\n{s['new_today']} new jobs today, {s['pending_extraction']} from the last "
        f"{inbox.FRESH_DAYS} days waiting for extraction.\n"
        f"Queue: {s['queued']} to review, {s['selected']} selected (drafts pending), "
        f"{s['ready']} ready.\n"
        "Next, in Claude Code: /mcp__job-search__daily_run"
        + (" (then /mcp__job-search__prepare_applications)" if s["selected"] else "")
    )


if __name__ == "__main__":
    main()
