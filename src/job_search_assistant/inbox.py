"""Phase 6: descriptions for LinkedIn/XING alert jobs, pasted by me.

LinkedIn and XING are never scraped or automated (CLAUDE.md hard rule); their alert
emails carry no description. So `daily.py` writes a template per relevant alert job
into data/inbox/; I open the link in my own browser, paste the description below the
marker and save; the next `daily` run imports it:

    data/inbox/6012-zalando.txt      # template -> I paste -> imported, moved to done/

Pasted text lives in `pasted_descriptions` (provenance: I pasted it) and is copied
into `jobs.description`. Re-reading the alert emails resets that column to '' (the
upsert refreshes content fields), so `apply_pasted` runs after every ingest.

Channels: a job is on the "alerts" list if any copy of it came from an alert email
(that's where I apply); everything else is "other" (Arbeitnow, BA, Adzuna).
"""

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from job_search_assistant import evalset
from job_search_assistant.text import detect_language

INBOX = Path("data/inbox")
ALERT_SOURCES = ("linkedin", "xing")
MARKER = "----- paste the job description below this line -----"
MIN_CHARS = 200  # shorter than this is an untouched template or a mis-paste
FULL_TEXT = 400  # as evalset.candidates: a description good enough for extraction

# A group is "alerts" if one of its members came from an alert email.
ALERT_GROUP = f"""EXISTS (
    SELECT 1 FROM job_groups ga JOIN job_groups gb ON gb.group_id = ga.group_id
    JOIN jobs o ON o.id = gb.job_id
    WHERE ga.job_id = {{job}} AND o.source IN ({", ".join(repr(s) for s in ALERT_SOURCES)}))"""


def channel_sql(job_column: str) -> str:
    """SQL expression: 'alerts' or 'other' for the job id in `job_column`."""
    return f"CASE WHEN {ALERT_GROUP.format(job=job_column)} THEN 'alerts' ELSE 'other' END"


def channels(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, str]:
    if not job_ids:
        return {}
    rows = conn.execute(
        f"SELECT j.id, {channel_sql('j.id')} FROM jobs j "
        f"WHERE j.id IN ({','.join('?' * len(job_ids))})",
        job_ids,
    )
    return dict(rows.fetchall())


def apply_pasted(conn: sqlite3.Connection) -> int:
    """Put pasted descriptions back into `jobs` (after an ingest reset them); returns count."""
    with conn:
        cur = conn.execute(
            """UPDATE jobs SET description = p.text, lang = p.lang
               FROM pasted_descriptions p
               WHERE jobs.id = p.job_id AND jobs.description IS NOT p.text"""
        )
    return cur.rowcount


def needs_description(conn: sqlite3.Connection, days: int = 14) -> list[sqlite3.Row]:
    """Recent alert jobs that look like my roles and have no text anywhere yet.

    One per duplicate group (a LinkedIn and a XING alert for the same job count once);
    skipped if any copy already has a full description, or the job is in my queue.
    """
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        f"""SELECT j.*, ROW_NUMBER() OVER (
                PARTITION BY COALESCE(g.group_id, j.id) ORDER BY j.id) AS rn
            FROM jobs j LEFT JOIN job_groups g ON g.job_id = j.id
            WHERE j.source IN ({",".join("?" * len(ALERT_SOURCES))})
              AND coalesce(j.posted_at, j.first_seen) >= ?
              AND NOT EXISTS (SELECT 1 FROM job_groups ga
                              JOIN job_groups gb ON gb.group_id = ga.group_id
                              JOIN jobs o ON o.id = gb.job_id
                              WHERE ga.job_id = j.id AND length(o.description) >= ?
                                AND o.source != 'adzuna')
              AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id)
            ORDER BY j.id""",
        (*ALERT_SOURCES, cutoff, FULL_TEXT),
    ).fetchall()
    return [
        r
        for r in rows
        if r["rn"] == 1
        and evalset.TARGET_TITLE.search(r["title"])
        and not evalset.SENIOR_TITLE.search(r["title"])
        and not evalset.OUT_OF_SCOPE.search(f"{r['location'] or ''} {r['title']}")
    ]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"


def write_templates(conn: sqlite3.Connection, inbox: Path = INBOX, days: int = 14) -> list[Path]:
    """One template per job that needs a description; existing files are left alone."""
    inbox.mkdir(parents=True, exist_ok=True)
    existing = {f.name.split("-")[0] for f in inbox.rglob("*.txt")}  # incl. done/
    written = []
    for r in needs_description(conn, days):
        if str(r["id"]) in existing:
            continue
        path = inbox / f"{r['id']}-{slug(r['company'])}.txt"
        path.write_text(
            f"# [{r['id']}] {r['title']} — {r['company']} — {r['location'] or '?'} "
            f"({r['source']})\n"
            f"# Open in your browser: {r['url']}\n"
            "# Paste the full description below the line and save; empty files are ignored.\n"
            f"{MARKER}\n\n"
        )
        written.append(path)
    return written


def import_inbox(conn: sqlite3.Connection, inbox: Path = INBOX) -> tuple[list[int], list[str]]:
    """Import filled templates (or any <job id>-*.txt); returns (job ids, problems).

    Imported files move to done/; untouched templates stay for later.
    """
    imported, problems = [], []
    for path in sorted(inbox.glob("*.txt")):
        m = re.match(r"(\d+)", path.name)
        if not m:
            problems.append(f"{path.name}: name must start with the job id")
            continue
        content = path.read_text(errors="replace")
        text = content.split(MARKER, 1)[1] if MARKER in content else content
        text = text.strip()
        if len(text) < MIN_CHARS:
            continue  # not filled in yet
        job_id = int(m.group(1))
        job = conn.execute("SELECT title, source FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if job is None:
            problems.append(f"{path.name}: no job with id {job_id}")
            continue
        if job["source"] not in ALERT_SOURCES:
            problems.append(f"{path.name}: job {job_id} is from {job['source']}, not an alert")
            continue
        with conn:
            conn.execute(
                """INSERT INTO pasted_descriptions (job_id, text, lang, pasted_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (job_id) DO UPDATE SET text = excluded.text,
                       lang = excluded.lang, pasted_at = excluded.pasted_at""",
                (job_id, text, detect_language(f"{job['title']}\n{text}"),
                 datetime.now(UTC).isoformat()),
            )  # fmt: skip
        done = inbox / "done" / path.name
        done.parent.mkdir(exist_ok=True)
        path.replace(done)
        imported.append(job_id)
    apply_pasted(conn)
    return imported, problems
