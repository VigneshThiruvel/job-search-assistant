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

Freshness: a job's age is the earliest `posted_at` of any copy in its duplicate group:
the ad's own date (Arbeitnow, BA, Adzuna) or the email date (LinkedIn, XING). Not
`first_seen` (we often find ads days after they were posted) and not `last_seen` (the
emails are re-read for 30 days). Only jobs posted within FRESH_DAYS are extracted,
ranked and shown in today's queue.

Pasted text lives in `pasted_descriptions` (provenance: I pasted it) and is copied
into `jobs.description`. Re-reading the alert emails resets that column to '' (the
upsert refreshes content fields), so `apply_pasted` runs after every ingest.

Channels: a job is on the "alerts" list if any copy of it came from an alert email
(that's where I apply); everything else is "other" (Arbeitnow, BA, Adzuna).
"""

import json
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
FRESH_DAYS = 2  # postings expire fast: only ads posted in the last two days
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


POSTED = "coalesce(j.posted_at, j.first_seen)"  # every source sets posted_at today


def fresh_job_ids(conn: sqlite3.Connection, days: float = FRESH_DAYS) -> set[int]:
    """Jobs whose ad was posted within `days` (earliest copy; see module docstring)."""
    cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
    rows = conn.execute(
        f"""WITH age AS (
                SELECT j.id, min({POSTED}) OVER (PARTITION BY COALESCE(g.group_id, j.id)) AS t
                FROM jobs j LEFT JOIN job_groups g ON g.job_id = j.id)
            SELECT id FROM age WHERE t >= ?""",
        (cutoff,),
    )
    return {r[0] for r in rows}


def local_midnight() -> str:
    """Start of today in local time, as a UTC ISO timestamp (for comparing stored times)."""
    midnight = datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.astimezone(UTC).isoformat()


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


def needs_description(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Fresh alert jobs that look like my roles and have no text anywhere yet.

    Freshness alone bounds the set (no "emails since the last run" window: a job that
    missed one run, e.g. before a title-rule fix, must still get its file).
    One per duplicate group (a LinkedIn and a XING alert for the same job count once);
    skipped if any copy already has a full description, or the job is in my queue.
    """
    fresh = fresh_job_ids(conn)
    rows = conn.execute(
        f"""SELECT j.*, ROW_NUMBER() OVER (
                PARTITION BY COALESCE(g.group_id, j.id) ORDER BY j.id) AS rn
            FROM jobs j LEFT JOIN job_groups g ON g.job_id = j.id
            WHERE j.source IN ({",".join("?" * len(ALERT_SOURCES))})
              AND NOT EXISTS (SELECT 1 FROM job_groups ga
                              JOIN job_groups gb ON gb.group_id = ga.group_id
                              JOIN jobs o ON o.id = gb.job_id
                              WHERE ga.job_id = j.id AND length(o.description) >= ?
                                AND o.source != 'adzuna')
              AND NOT EXISTS (SELECT 1 FROM applications a WHERE a.job_id = j.id)
            ORDER BY j.id""",
        (*ALERT_SOURCES, FULL_TEXT),
    ).fetchall()
    return [
        r for r in rows
        if r["rn"] == 1 and r["id"] in fresh and relevant_title(r["title"], r["location"])
    ]  # fmt: skip


def relevant_title(title: str, location: str | None) -> bool:
    """Worth a description at all: one of my roles by title, not senior or student, in scope."""
    return bool(
        evalset.TARGET_TITLE.search(title)
        and not evalset.SENIOR_TITLE.search(title)
        and not STUDENT_TITLE.search(title)
        and not evalset.OUT_OF_SCOPE.search(f"{location or ''} {title}")
    )


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "job"


def write_templates(conn: sqlite3.Connection, inbox: Path = INBOX) -> list[Path]:
    """One inbox file per fresh job that needs a description; existing files are left alone.

    Only jobs within FRESH_DAYS (older ones would not be ranked); unfilled files for jobs
    that have aged out are deleted. Runs after every daily step, --skip-ingest included.
    """
    inbox.mkdir(parents=True, exist_ok=True)
    fresh = fresh_job_ids(conn)
    for path in inbox.glob("*.txt"):
        m = re.match(r"(\d+)", path.name)
        if m and int(m.group(1)) not in fresh and is_untouched(path):
            path.unlink()
    existing = {f.name.split("-")[0] for f in inbox.rglob("*.txt")}  # incl. done/
    written = []
    for r in needs_description(conn):
        if str(r["id"]) in existing:
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
        try:
            save_paste(conn, int(m.group(1)), text, note, expired)
        except ValueError as exc:
            problems.append(f"{path.name}: {exc}")
            continue
        done = inbox / "done" / path.name
        done.parent.mkdir(exist_ok=True)
        path.replace(done)
        imported.append(int(m.group(1)))
    apply_pasted(conn)
    return imported, problems


def save_paste(
    conn: sqlite3.Connection, job_id: int, text: str, note: str | None, expired: bool
) -> None:
    """Store what I pasted for one alert job (from a file or the UI) and apply it."""
    text = text.strip()
    if len(text) < MIN_CHARS and not expired:
        raise ValueError(f"description too short ({len(text)} chars); paste the full text "
                         "or mark it expired")  # fmt: skip
    job = conn.execute("SELECT title, source FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None:
        raise ValueError(f"no job with id {job_id}")
    if job["source"] not in ALERT_SOURCES:
        raise ValueError(f"job {job_id} is from {job['source']}, not an alert")
    with conn:
        conn.execute(
            """INSERT INTO pasted_descriptions (job_id, text, lang, note, expired, pasted_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT (job_id) DO UPDATE SET text = excluded.text,
                   lang = excluded.lang, note = excluded.note, expired = excluded.expired,
                   pasted_at = excluded.pasted_at""",
            (job_id, text, detect_language(f"{job['title']}\n{text}") if text else None,
             (note or "").strip() or None, expired, datetime.now(UTC).isoformat()),
        )  # fmt: skip
    apply_pasted(conn)


def open_items(conn: sqlite3.Connection, inbox: Path = INBOX) -> list[dict]:
    """Jobs with an unfilled inbox file (what daily.py asked me to paste), oldest first."""
    ids = {}
    for path in inbox.glob("*.txt"):
        m = re.match(r"(\d+)", path.name)
        if m and is_untouched(path):
            ids[int(m.group(1))] = path
    if not ids:
        return []
    rows = conn.execute(
        f"SELECT id, title, company, location, source, url, posted_at FROM jobs "
        f"WHERE id IN ({','.join('?' * len(ids))}) ORDER BY id",
        list(ids),
    )
    return [dict(r) for r in rows]


def close_file(job_id: int, inbox: Path = INBOX) -> None:
    """After a UI paste: move the job's inbox file to done/, so it isn't asked again."""
    for path in inbox.glob(f"{job_id}-*.txt"):
        done = inbox / "done" / path.name
        done.parent.mkdir(exist_ok=True)
        path.replace(done)


def alert_funnel(conn: sqlite3.Connection, extraction: tuple[str, str]) -> dict:
    """What happened to each recent LinkedIn/XING job (one per duplicate group), and why.

    jobs: one per group with a title worth a look (or any text), stage queued | eligible
    (passes my filters, waits for the agent's ranking) | filtered (with reasons) |
    not_extracted | needs_description | expired. other_titles: groups skipped by title
    (not my roles, senior, student), as daily.py skips them for inbox files.
    """
    from job_search_assistant import search

    fresh = fresh_job_ids(conn)
    expired = expired_job_ids(conn)
    rows = conn.execute(
        f"""SELECT j.id, j.title, j.company, j.location, j.source, j.url,
                   COALESCE(g.group_id, j.id) AS grp, x.output_json,
                   (SELECT a.status || ':' || a.recommendation FROM applications a
                    JOIN job_groups ga ON ga.job_id = a.job_id
                    WHERE ga.group_id = COALESCE(g.group_id, j.id)) AS queued
            FROM jobs j LEFT JOIN job_groups g ON g.job_id = j.id
            LEFT JOIN extractions x ON x.job_id = j.id AND x.model = ? AND x.prompt_version = ?
            WHERE {ALERT_GROUP.format(job="j.id")}
            ORDER BY j.id""",
        extraction,
    ).fetchall()
    best: dict[int, sqlite3.Row] = {}
    for r in rows:  # per group: prefer a copy with an extraction, then an alert copy
        if r["id"] not in fresh:
            continue
        have = best.get(r["grp"])
        rank = (r["output_json"] is not None, r["source"] in ALERT_SOURCES)
        if have is None or rank > (
            have["output_json"] is not None,
            have["source"] in ALERT_SOURCES,
        ):
            best[r["grp"]] = r
    out, other_titles = [], 0
    for r in best.values():
        group = {r["id"], r["grp"]}
        skip_title = not relevant_title(r["title"], r["location"])
        if skip_title and not r["queued"] and r["output_json"] is None:
            other_titles += 1
            continue
        x = json.loads(r["output_json"]) if r["output_json"] else None
        reasons: list[str] = []
        if r["queued"]:
            stage, reasons = "queued", [r["queued"].replace(":", " · ")]
        elif group & expired:
            stage = "expired"
        elif x is None:
            stage = "not_extracted" if _has_text(conn, r["id"]) else "needs_description"
        else:
            reasons = search.explain(x, r["title"], r["location"])
            stage = "filtered" if reasons else "eligible"
        out.append(
            {
                "job_id": r["id"],
                "title": r["title"],
                "company": r["company"],
                "source": r["source"],
                "url": r["url"],
                "stage": stage,
                "reasons": reasons,
            }
        )
    order = ["queued", "eligible", "not_extracted", "needs_description", "filtered", "expired"]
    jobs = sorted(out, key=lambda o: (order.index(o["stage"]), -o["job_id"]))
    return {"jobs": jobs, "other_titles": other_titles}


def _has_text(conn: sqlite3.Connection, job_id: int) -> bool:
    row = conn.execute("SELECT length(description) FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return bool(row and row[0] >= FULL_TEXT)


def alert_urls(conn: sqlite3.Connection, job_ids: list[int]) -> dict[int, str]:
    """For jobs in the alerts channel: the alert's link (LinkedIn preferred over XING)."""
    if not job_ids:
        return {}
    rows = conn.execute(
        f"""SELECT ga.job_id, o.url FROM job_groups ga
            JOIN job_groups gb ON gb.group_id = ga.group_id
            JOIN jobs o ON o.id = gb.job_id
            WHERE ga.job_id IN ({",".join("?" * len(job_ids))})
              AND o.source IN ({", ".join(repr(s) for s in ALERT_SOURCES)})
            ORDER BY o.source = 'linkedin', o.id DESC""",  # later rows win: LinkedIn last
        job_ids,
    )
    return dict(rows.fetchall())
