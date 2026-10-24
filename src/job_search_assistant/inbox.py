"""Phase 6: descriptions for LinkedIn/XING alert jobs, pasted by me.

LinkedIn and XING are never scraped or automated (CLAUDE.md hard rule); their alert
emails carry no description. So `daily.py` writes an inbox file per relevant job in
the alert emails received since the previous daily run (however many emails that is;
a job in several emails gets one file) into data/inbox/; I open the link in my own
browser, paste the description below the marker and save; the next `daily` run
imports it:

    data/inbox/6012-zalando.txt      # inbox file -> I paste -> imported, moved to done/

Above the marker I can set `Expired: yes` (still imported for the dataset, never
ranked; with no description it just closes the job) and write notes, which the agent
sees. Unfilled files older than FRESH_DAYS are deleted: those jobs wouldn't be ranked.

Freshness: a duplicate group "arrived" when its first copy did — the email date for an
alert job (the emails are re-read for 30 days, so `last_seen` says nothing), else
`first_seen`. Only groups that arrived within FRESH_DAYS are extracted and ranked daily.

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
MIN_CHARS = 200  # shorter than this is an untouched file or a mis-paste
FRESH_DAYS = 2  # postings expire fast: rank only what arrived in the last two days
EXPIRED = re.compile(r"^\s*expired:\s*(yes|y|true|ja)\s*$", re.I | re.M)
FULL_TEXT = 400  # as evalset.candidates: a description good enough for extraction
# Out of my scope (CLAUDE.md: no internships or working-student roles). Not "intern\w*":
# that would also drop "Internal Audit Data Analyst".
STUDENT_TITLE = re.compile(
    r"\b(werkstudent\w*|working student|internship\w*|intern|praktik\w*|praktikum)\b", re.I
)

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
               WHERE jobs.id = p.job_id AND p.text != '' AND jobs.description IS NOT p.text"""
        )
    return cur.rowcount


ARRIVED = f"""CASE WHEN j.source IN ({", ".join(repr(s) for s in ALERT_SOURCES)})
                   THEN coalesce(j.posted_at, j.first_seen) ELSE j.first_seen END"""


def fresh_job_ids(conn: sqlite3.Connection, days: float = FRESH_DAYS) -> set[int]:
    """Jobs whose duplicate group arrived within `days` (see module docstring)."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        f"""WITH arrival AS (
                SELECT j.id, min({ARRIVED}) OVER (PARTITION BY COALESCE(g.group_id, j.id)) AS t
                FROM jobs j LEFT JOIN job_groups g ON g.job_id = j.id)
            SELECT id FROM arrival WHERE t >= ?""",
        (cutoff,),
    )
    return {r[0] for r in rows}


def expired_job_ids(conn: sqlite3.Connection) -> set[int]:
    """Every copy of a job I marked expired."""
    rows = conn.execute(
        """SELECT COALESCE(gb.job_id, p.job_id) FROM pasted_descriptions p
           LEFT JOIN job_groups ga ON ga.job_id = p.job_id
           LEFT JOIN job_groups gb ON gb.group_id = ga.group_id
           WHERE p.expired = 1"""
    )
    return {r[0] for r in rows}


def my_note(conn: sqlite3.Connection, job_id: int) -> tuple[str | None, bool]:
    """(note, expired) from any copy of the job I wrote an inbox file for."""
    row = conn.execute(
        """SELECT group_concat(p.note, ' | '), max(p.expired) FROM pasted_descriptions p
           WHERE p.job_id IN (SELECT gb.job_id FROM job_groups ga
                              JOIN job_groups gb ON gb.group_id = ga.group_id
                              WHERE ga.job_id = ?) OR p.job_id = ?""",
        (job_id, job_id),
    ).fetchone()
    return row[0], bool(row[1])


def mail_window_start(conn: sqlite3.Connection) -> str:
    """Emails received from here on get inbox files: since the full daily run before the
    latest one (the latest is usually the one running now), else since today's midnight."""
    starts = [r[0] for r in conn.execute(
        "SELECT started_at FROM daily_runs ORDER BY id DESC LIMIT 2"
    )]  # fmt: skip
    if len(starts) == 2:
        return starts[1]
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).isoformat()


def needs_description(conn: sqlite3.Connection, since: str) -> list[sqlite3.Row]:
    """Alert jobs from emails received since `since` that look like my roles and have no
    text anywhere yet.

    One per duplicate group (a LinkedIn and a XING alert for the same job count once);
    skipped if any copy already has a full description, or the job is in my queue.
    """
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
        (*ALERT_SOURCES, since, FULL_TEXT),
    ).fetchall()
    return [
        r
        for r in rows
        if r["rn"] == 1
        and evalset.TARGET_TITLE.search(r["title"])
        and not evalset.SENIOR_TITLE.search(r["title"])
        and not STUDENT_TITLE.search(r["title"])
        and not evalset.OUT_OF_SCOPE.search(f"{r['location'] or ''} {r['title']}")
    ]


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"


def write_templates(conn: sqlite3.Connection, since: str, inbox: Path = INBOX) -> list[Path]:
    """One inbox file per fresh job that needs a description; existing files are left alone.

    Only jobs within FRESH_DAYS (older ones would not be ranked); unfilled files for jobs
    that have aged out are deleted.
    """
    inbox.mkdir(parents=True, exist_ok=True)
    fresh = fresh_job_ids(conn)
    for path in inbox.glob("*.txt"):
        m = re.match(r"(\d+)", path.name)
        if m and int(m.group(1)) not in fresh and is_untouched(path):
            path.unlink()
    existing = {f.name.split("-")[0] for f in inbox.rglob("*.txt")}  # incl. done/
    written = []
    for r in needs_description(conn, since):
        if str(r["id"]) in existing or r["id"] not in fresh:
            continue
        path = inbox / f"{r['id']}-{slug(r['company'])}.txt"
        path.write_text(
            f"# [{r['id']}] {r['title']} — {r['company']} — {r['location'] or '?'} "
            f"({r['source']})\n"
            f"# Open in your browser: {r['url']}\n"
            "# Paste the description below the line and save. Set Expired: yes if the posting\n"
            "# is closed (no description needed then). Notes are for the agent and for me.\n"
            "Expired: no\n"
            "Notes:\n\n"
            f"{MARKER}\n\n"
        )
        written.append(path)
    return written


def parse(content: str) -> tuple[str, str | None, bool]:
    """(description, notes, expired) from an inbox file.

    Notes = every line above the marker that isn't a # comment or the Expired line
    ("Notes:" prefix optional, so "Author note: ..." lines count too).
    """
    head, text = content.split(MARKER, 1) if MARKER in content else ("", content)
    notes = []
    for line in head.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or re.match(r"(?i)expired:", line):
            continue
        line = re.sub(r"(?i)^notes?:\s*", "", line)
        if line:
            notes.append(line)
    return text.strip(), " ".join(notes) or None, bool(EXPIRED.search(head))


def is_untouched(path: Path) -> bool:
    text, notes, expired = parse(path.read_text(errors="replace"))
    return not text and not notes and not expired


def import_inbox(conn: sqlite3.Connection, inbox: Path = INBOX) -> tuple[list[int], list[str]]:
    """Import filled templates (or any <job id>-*.txt); returns (job ids, problems).

    Imported files move to done/; unfilled ones stay for later.
    """
    imported, problems = [], []
    for path in sorted(inbox.glob("*.txt")):
        m = re.match(r"(\d+)", path.name)
        if not m:
            problems.append(f"{path.name}: name must start with the job id")
            continue
        text, note, expired = parse(path.read_text(errors="replace"))
        if len(text) < MIN_CHARS and not expired:
            if text:
                problems.append(f"{path.name}: description too short ({len(text)} chars), skipped")
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
                """INSERT INTO pasted_descriptions (job_id, text, lang, note, expired, pasted_at)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT (job_id) DO UPDATE SET text = excluded.text,
                       lang = excluded.lang, note = excluded.note, expired = excluded.expired,
                       pasted_at = excluded.pasted_at""",
                (job_id, text, detect_language(f"{job['title']}\n{text}") if text else None,
                 note, expired, datetime.now(UTC).isoformat()),
            )  # fmt: skip
        done = inbox / "done" / path.name
        done.parent.mkdir(exist_ok=True)
        path.replace(done)
        imported.append(job_id)
    apply_pasted(conn)
    return imported, problems
