"""Phase 6: the review queue — what the agent proposes and what I decide.

    python -m job_search_assistant.applications list [--status queued]
    python -m job_search_assistant.applications show 4187
    python -m job_search_assistant.applications select 4187 495 [--letter] [--note "stress Kafka"]
    python -m job_search_assistant.applications letter 4187 [--note ...]  # add a letter later
    python -m job_search_assistant.applications skip 4187 [--note "needs C1 German"]
    python -m job_search_assistant.applications applied 4187
    python -m job_search_assistant.applications export 4187   # PDF (+ letter) -> data/drafts/

    queued ──(me: select)──> selected ──(agent: tailored résumé [+ letter if asked])──> ready
                                ▲                                                       │
                                └──────(me: select again / letter: redraft or add)──────┤
    skip / applied: me, from any open status ◄──────────────────────────────────────────┘

Selecting a job requests a tailored résumé; a cover letter only when I ask for one
(`--letter` or `letter`). The two requests have their own timestamps, so adding a
letter later doesn't redo the résumé. The agent may only add `queued` rows and
answer open requests (via the MCP server); every other move is mine, here.
Nothing in this project submits an application: `applied` records that *I* did.
"""

import argparse
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from job_search_assistant import inbox, tailor

GUIDE = Path("profile/cover_letter.md")  # gitignored, like the résumé
DRAFTS = Path("data/drafts")
RECOMMENDATIONS = ("apply", "maybe", "skip")
STATUSES = ("queued", "selected", "ready", "skipped", "applied")
OPEN = ("queued", "selected", "ready")
MAX_WORDS = 500  # the guide asks for 250-400; beyond this it is not a one-page letter


class Claim(BaseModel):
    """One statement about me in a letter, with the text that supports it."""

    claim: str
    evidence: str  # copied verbatim from the résumé or the guide's facts


def now() -> str:
    return datetime.now(UTC).isoformat()


def row(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM applications WHERE job_id = ?", (job_id,)).fetchone()


def status_of(conn: sqlite3.Connection, job_id: int) -> str | None:
    r = row(conn, job_id)
    return r["status"] if r else None


def queue(
    conn: sqlite3.Connection,
    job_id: int,
    recommendation: Literal["apply", "maybe", "skip"],
    fit_summary: str,
) -> None:
    """Agent: propose a job. A job is queued at most once, so re-runs never duplicate."""
    if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
        raise ValueError(f"no job with id {job_id}")
    if (status := status_of(conn, job_id)) is not None:
        raise ValueError(f"job {job_id} is already in the queue ({status})")
    if recommendation not in RECOMMENDATIONS:
        raise ValueError(f"recommendation must be one of {RECOMMENDATIONS}")
    if not fit_summary.strip():
        raise ValueError("fit_summary is empty")
    with conn:
        conn.execute(
            """INSERT INTO applications
               (job_id, status, recommendation, fit_summary, queued_at, updated_at)
               VALUES (?, 'queued', ?, ?, ?, ?)""",
            (job_id, recommendation, fit_summary.strip(), now(), now()),
        )


def _open(conn: sqlite3.Connection, job_id: int, allowed: tuple[str, ...]) -> sqlite3.Row:
    r = row(conn, job_id)
    if r is None:
        raise ValueError(f"job {job_id} is not in the queue")
    if r["status"] not in allowed:
        raise ValueError(f"job {job_id} is {r['status']}; this only works on {allowed}")
    return r


def select(conn: sqlite3.Connection, job_id: int, letter: bool = False, note: str | None = None):
    """Me: I want to apply. Requests a (fresh) tailored résumé, and a letter if `letter`."""
    _open(conn, job_id, ("queued", "selected", "ready", "skipped"))
    t = now()
    with conn:
        conn.execute(
            """UPDATE applications SET status = 'selected', resume_requested_at = ?,
                   letter_requested_at = CASE WHEN ? THEN ? ELSE letter_requested_at END,
                   note = coalesce(?, note), updated_at = ?
               WHERE job_id = ?""",
            (t, letter, t, note, t, job_id),
        )


def request_letter(conn: sqlite3.Connection, job_id: int, note: str | None = None) -> None:
    """Me: add (or redo) a letter; requests a résumé too if the job never had one."""
    r = _open(conn, job_id, ("queued", "selected", "ready", "skipped"))
    t = now()
    with conn:
        conn.execute(
            """UPDATE applications SET status = 'selected', letter_requested_at = ?,
                   resume_requested_at = coalesce(resume_requested_at, ?),
                   note = coalesce(?, note), updated_at = ?
               WHERE job_id = ?""",
            (t, t if r["resume_requested_at"] is None else None, note, t, job_id),
        )


def close(conn: sqlite3.Connection, job_id: int, status: str, note: str | None = None) -> None:
    """Me: skip a job, or record that I applied (from any open status, or skipped -> applied)."""
    if status not in ("skipped", "applied"):
        raise ValueError(f"I can close a job as skipped or applied, not {status!r}")
    _open(conn, job_id, OPEN + (("skipped",) if status == "applied" else ()))
    with conn:
        conn.execute(
            """UPDATE applications SET status = ?, note = coalesce(?, note), updated_at = ?
               WHERE job_id = ?""",
            (status, note, now(), job_id),
        )


def _answered(conn: sqlite3.Connection, table: str, job_id: int, since: str | None) -> bool:
    return (
        since is None
        or conn.execute(
            f"SELECT 1 FROM {table} WHERE job_id = ? AND written_at >= ?", (job_id, since)
        ).fetchone()
        is not None
    )


def needs(conn: sqlite3.Connection, job_id: int) -> tuple[bool, bool]:
    """(needs a résumé, needs a letter): requested and not answered since the request."""
    r = row(conn, job_id)
    if r is None or r["status"] != "selected":
        return False, False
    return (
        not _answered(conn, "tailored_resumes", job_id, r["resume_requested_at"]),
        not _answered(conn, "cover_letters", job_id, r["letter_requested_at"]),
    )


def _mark_ready_if_done(conn: sqlite3.Connection, job_id: int) -> None:
    if needs(conn, job_id) == (False, False):
        conn.execute(
            "UPDATE applications SET status = 'ready', updated_at = ? WHERE job_id = ?",
            (now(), job_id),
        )


def save_resume(
    conn: sqlite3.Connection,
    job_id: int,
    tailored: tailor.TailoredResume,
    resume: tailor.Resume,
    finder: tailor.SkillFinder,
) -> None:
    """Agent: store a tailored résumé for a selected job; checked against the original."""
    if not needs(conn, job_id)[0]:
        raise ValueError(f"job {job_id} has no open résumé request (status: "
                         f"{status_of(conn, job_id) or 'not in the queue'})")  # fmt: skip
    if problems := tailor.check(resume, tailored, finder):
        raise ValueError("\n".join(problems))
    with conn:
        conn.execute(
            "INSERT INTO tailored_resumes (job_id, resume_json, written_at) VALUES (?, ?, ?)",
            (job_id, tailored.model_dump_json(), now()),
        )
        _mark_ready_if_done(conn, job_id)


def facts(guide: str) -> str:
    """The guide's "Facts" section: things about me that aren't in the résumé."""
    m = re.search(r"^## Facts.*?$(.*?)(?=^## |\Z)", guide, re.M | re.S)
    return m.group(1) if m else ""


def normalize(text: str) -> str:
    """For quote matching: case, Markdown emphasis, dash/quote variants, whitespace."""
    text = text.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[‐-―−]", "-", text)  # hyphen, en/em dash, minus -> "-"
    text = re.sub(r"[*_`#>]", "", text)
    return " ".join(text.split())


def check_claims(claims: list[Claim], sources: str) -> list[str]:
    """Each claim's evidence must be a quote from `sources` (résumé + facts). Returns problems.

    A mechanical guard against invented experience: it can't judge whether the claim
    *follows* from the evidence (that's my review), only that the evidence exists.
    """
    if not claims:
        return ["no claims: list every statement about me with its evidence"]
    source = normalize(sources)
    problems = []
    for c in claims:
        evidence = normalize(c.evidence)
        if len(evidence) < 2:
            problems.append(f"claim {c.claim!r}: evidence is empty")
        elif evidence not in source:
            problems.append(
                f"claim {c.claim!r}: evidence {c.evidence!r} is not in the résumé or my facts"
            )
    return problems


def save_letter(
    conn: sqlite3.Connection, job_id: int, letter: str, claims: list[Claim], sources: str
) -> None:
    """Agent: store a letter for a selected job I asked a letter for."""
    if not needs(conn, job_id)[1]:
        raise ValueError(f"job {job_id}: no letter was requested (status: "
                         f"{status_of(conn, job_id) or 'not in the queue'})")  # fmt: skip
    problems = check_claims(claims, sources)
    if (words := len(letter.split())) > MAX_WORDS:
        problems.append(f"letter has {words} words; keep it under {MAX_WORDS}")
    if problems:
        raise ValueError("\n".join(problems))
    with conn:
        conn.execute(
            """INSERT INTO cover_letters (job_id, letter, claims_json, written_at)
               VALUES (?, ?, ?, ?)""",
            (job_id, letter.strip(), json.dumps([c.model_dump() for c in claims]), now()),
        )
        _mark_ready_if_done(conn, job_id)


def items(conn: sqlite3.Connection, status: str | None = None) -> list[dict]:
    """Queue rows with job title/company/url, current drafts and open requests, oldest first."""
    latest = "SELECT {col} FROM {table} t WHERE t.job_id = a.job_id ORDER BY t.id DESC LIMIT 1"
    rows = conn.execute(
        f"""SELECT a.*, j.title, j.company, j.location, j.url,
                   {inbox.channel_sql("a.job_id")} AS channel,
                   ({latest.format(col="letter", table="cover_letters")}) AS letter,
                   ({latest.format(col="claims_json", table="cover_letters")}) AS claims_json,
                   ({latest.format(col="resume_json", table="tailored_resumes")}) AS resume_json
            FROM applications a JOIN jobs j ON j.id = a.job_id
            WHERE ? IS NULL OR a.status = ?
            ORDER BY a.queued_at, a.job_id""",
        (status, status),
    )
    out = []
    for r in rows:
        item = dict(r)
        item["needs_resume"], item["needs_letter"] = needs(conn, r["job_id"])
        out.append(item)
    return out


def item(conn: sqlite3.Connection, job_id: int) -> dict:
    found = [i for i in items(conn) if i["job_id"] == job_id]
    if not found:
        raise ValueError(f"job {job_id} is not in the queue")
    return found[0]


def resume_review(resume: tailor.Resume, t: tailor.TailoredResume) -> str:
    """Tailored résumé next to the originals it rewords, plus what it leaves out."""
    lines = [f"Summary (was):\n  {resume.summary}\nSummary (now):\n  {t.summary}\n"]
    if [(s.label, s.items) for s in t.skills] != resume.skills:
        lines.append("Skills (new order):")
        lines += [f"  {s.label}: {', '.join(s.items)}" for s in t.skills]
        lines.append("")
    for e in t.entries:
        lines.append(f"[{e.id}] {resume.entry(e.id).heading}")
        for b in e.bullets:
            lines.append(f"  • {b.text}")
            lines += [f"      was {s}: {resume.bullet(s)}" for s in b.sources]
    if gone := tailor.dropped(resume, t):
        lines.append("\nLeft out:")
        lines += [f"  {g}" for g in gone]
    return "\n".join(lines)


def export(conn: sqlite3.Connection, job_id: int, resume_tex: str, out_dir: Path = DRAFTS):
    """Write the tailored résumé (.tex + PDF) and the letter (.md); returns (paths, warnings)."""
    i = item(conn, job_id)
    if not i["resume_json"] and not i["letter"]:
        raise ValueError(f"job {job_id} has no drafts yet")
    slug = re.sub(r"[^a-z0-9]+", "-", i["company"].lower()).strip("-")[:40]
    base = out_dir / f"{job_id}-{slug}"
    base.mkdir(parents=True, exist_ok=True)
    paths, warnings = [], []
    if i["resume_json"]:
        tailored = tailor.TailoredResume.model_validate_json(i["resume_json"])
        tex = base / "resume.tex"
        tex.write_text(tailor.render(tailor.parse(resume_tex), tailored))
        pdf, pages = tailor.compile_pdf(tex)
        budget = tailor.original_pages(resume_tex, out_dir)
        if pages and budget and pages > budget:
            warnings.append(f"résumé has {pages} pages, the original {budget}")
        paths += [tex, pdf]
    if i["letter"]:
        letter = base / "cover_letter.md"
        letter.write_text(f"<!-- {i['title']} — {i['company']} — {i['url']} -->\n\n{i['letter']}\n")
        paths.append(letter)
    return paths, warnings


def main() -> None:
    from job_search_assistant import db

    parser = argparse.ArgumentParser(prog="python -m job_search_assistant.applications")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list").add_argument("--status", choices=STATUSES)
    for name in ("show", "export"):
        sub.add_parser(name).add_argument("job_id", type=int)
    p = sub.add_parser("select", help="I want to apply: request a tailored résumé")
    p.add_argument("job_ids", type=int, nargs="+")
    p.add_argument("--letter", action="store_true", help="also request a cover letter")
    p.add_argument("--note", help="what to stress (applies to every job given)")
    for name in ("letter", "skip", "applied"):
        p = sub.add_parser(name)
        p.add_argument("job_id", type=int)
        p.add_argument("--note")
    args = parser.parse_args()
    conn = db.connect("data/jobs.db")

    try:
        if args.command == "list":
            rows = items(conn, args.status)
            for channel, title in (("alerts", "LinkedIn / XING"), ("other", "Other sites")):
                print(f"\n== {title} ==")
                for i in (r for r in rows if r["channel"] == channel):
                    todo = " ".join(
                        w
                        for w, n in (("résumé", i["needs_resume"]), ("letter", i["needs_letter"]))
                        if n
                    )
                    print(f"[{i['job_id']}] {i['status']:<8} {i['recommendation']:<5} "
                          f"{i['title']} — {i['company']}, {i['location'] or '?'}"
                          + (f"  (waiting: {todo})" if todo else ""))  # fmt: skip
        elif args.command == "show":
            i = item(conn, args.job_id)
            print(f"{i['title']} — {i['company']}, {i['location'] or '?'}\n{i['url']}")
            print(f"status: {i['status']}   agent says: {i['recommendation']}")
            print(f"\n{i['fit_summary']}")
            if i["note"]:
                print(f"\nmy note: {i['note']}")
            if i["resume_json"]:
                resume = tailor.parse(tailor.TEX.read_text())
                tailored = tailor.TailoredResume.model_validate_json(i["resume_json"])
                print(f"\n--- tailored résumé ---\n{resume_review(resume, tailored)}")
            if i["letter"]:
                print(f"\n--- letter ---\n{i['letter']}\n\n--- claims ---")
                for c in json.loads(i["claims_json"]):
                    print(f"- {c['claim']}\n    source: {c['evidence']}")
        elif args.command == "export":
            paths, warnings = export(conn, args.job_id, tailor.TEX.read_text())
            print("\n".join(f"wrote {p}" for p in paths))
            print("\n".join(f"WARNING: {w}" for w in warnings))
        elif args.command == "select":
            for job_id in args.job_ids:
                select(conn, job_id, letter=args.letter, note=args.note)
            print(f"selected {len(args.job_ids)} job(s); now run "
                  "/mcp__job-search__prepare_applications in Claude Code")  # fmt: skip
        elif args.command == "letter":
            request_letter(conn, args.job_id, args.note)
            print("letter requested; now run /mcp__job-search__prepare_applications")
        else:
            close(conn, args.job_id, "skipped" if args.command == "skip" else "applied", args.note)
            print(f"job {args.job_id} -> {status_of(conn, args.job_id)}")
    except ValueError as exc:
        raise SystemExit(str(exc)) from None


if __name__ == "__main__":
    main()
