"""Phase 7a: backend of the local web UI — a JSON API over the same core as CLI and MCP.

    uv run python -m job_search_assistant.api             # http://127.0.0.1:8000 (+ web/dist)
    uv run python -m job_search_assistant.api --openapi   # schema for the TS type generator
    cd web && npm run dev                                  # dev UI on :5173, proxies /api here

The third front end over the same modules (CLI, MCP server, UI: "ports and adapters"):
every write goes through applications.py / inbox.py, so the queue rules hold here too.
The UI acts as me (select, skip, applied, paste); nothing here submits an application.

Local-only, but still guarded, because any web page I visit can make my browser send
requests to 127.0.0.1:
  - bound to 127.0.0.1, so other machines can't connect;
  - TrustedHost: only Host 127.0.0.1/localhost. Stops DNS rebinding (evil.example
    resolving to 127.0.0.1 would otherwise read my data as "same origin");
  - writes need an allowed Origin and a JSON body. A cross-site HTML form can do
    neither, and a cross-site fetch with JSON needs a CORS preflight we never grant (CSRF).
"""

import argparse
import json
import logging
import os
import sqlite3
import subprocess
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from job_search_assistant import (
    applications,
    daily,
    db,
    documents,
    embed,
    graph,
    inbox,
    runner,
    search,
    skills,
    tailor,
)

ROOT = Path(__file__).resolve().parents[2]
PACKAGE = Path(__file__).resolve().parent


def code_stamp() -> float:
    """Newest modification time of the backend's Python files."""
    return max(f.stat().st_mtime for f in PACKAGE.rglob("*.py"))


HOSTS = ["127.0.0.1", "localhost"]
ORIGINS = {f"http://{h}:{p}" for h in HOSTS for p in (8000, 5173)}  # app + Vite dev server
Status = Literal[
    "queued",
    "selected",
    "ready",
    "skipped",
    "applied",
    "screening",
    "interviewing",
    "offer",
    "accepted",
    "declined",
    "rejected",
    "withdrawn",
    "ghosted",
]  # fmt: skip (= applications.STATUSES, checked in tests)
Outcome = Literal["screening", "interviewing", "offer", "accepted", "declined", "rejected",
                  "withdrawn", "ghosted"]  # fmt: skip
Kind = Literal["resume", "cover_letter"]


class Card(BaseModel):
    """One job in my queue, as the lists show it."""

    job_id: int
    title: str
    company: str
    location: str | None
    url: str
    apply_url: str = Field(description="the alert's link for LinkedIn/XING jobs, else the posting")
    channel: Literal["alerts", "other"]
    status: Status
    recommendation: Literal["apply", "maybe", "skip"]
    fit_summary: str
    note: str | None
    has_resume: bool
    has_letter: bool
    needs_resume: bool
    needs_letter: bool
    queued_at: str
    updated_at: str
    applied_at: str | None = None
    last_event_at: str | None = None
    ghost_hint: bool = Field(False, description=f"no news for {applications.GHOST_DAYS} days")


class Event(BaseModel):
    from_status: str | None
    to_status: str
    at: str
    note: str | None
    source: str


class SavedFile(BaseModel):
    kind: Kind
    path: str
    exists: bool


class TrackerStats(BaseModel):
    applied: int
    responded: int = Field(description="got any reply: screening, interview, offer, rejection")
    interviewed: int
    offers: int
    rejected: int
    ghosted: int
    awaiting: int = Field(description="applied / screening / interviewing right now")


class SourceBullet(BaseModel):
    id: str
    text: str


class ReviewBullet(BaseModel):
    text: str
    sources: list[SourceBullet]


class ReviewEntry(BaseModel):
    id: str
    heading: str
    bullets: list[ReviewBullet]


class ResumeReview(BaseModel):
    summary_before: str
    summary_after: str
    skills: list[tailor.SkillLine] | None = Field(description="only if the order changed")
    entries: list[ReviewEntry]
    dropped: list[str]


class Detail(BaseModel):
    card: Card
    description: str
    resume: ResumeReview | None
    letter: str | None
    claims: list[applications.Claim]
    files: list[SavedFile] = []
    history: list[Event] = []


class InboxItem(BaseModel):
    job_id: int
    title: str
    company: str
    location: str | None
    source: str
    url: str
    posted_at: str | None


class Paste(BaseModel):
    description: str = ""
    notes: str | None = None
    expired: bool = False


class Action(BaseModel):
    note: str | None = None


class Select(Action):
    letter: bool = False


class Dated(Action):
    date: str | None = Field(None, description="YYYY-MM-DD when it happened, if not today")


class OutcomeChange(Dated):
    status: Outcome


class FileRequest(BaseModel):
    kind: Kind


class RunInfo(BaseModel):
    id: int
    kind: Literal["fetch", "mail", "embed", "agent_daily", "agent_prepare", "neo4j"]
    label: str
    status: Literal["running", "done", "failed", "cancelled", "interrupted"]
    started_at: str
    finished_at: str | None
    exit_code: int | None
    report: str | None = Field(description="the agent's final message (Markdown)")
    summary: str | None = Field(None, description="what the run changed, in one line")


class AlertJob(BaseModel):
    job_id: int
    title: str
    company: str
    source: str
    url: str
    stage: Literal["queued", "eligible", "not_extracted", "needs_description", "filtered",
                   "expired"]  # fmt: skip
    reasons: list[str]


class AlertFunnel(BaseModel):
    jobs: list[AlertJob] = Field(description="recent LinkedIn/XING jobs with a relevant title")
    other_titles: int = Field(description="recent alert jobs skipped by title (not my roles)")


class RunLog(BaseModel):
    run: RunInfo
    lines: list[str]
    next_offset: int


class RunRequest(BaseModel):
    kind: Literal["fetch", "mail", "embed", "agent_daily", "agent_prepare", "neo4j"]
    k: int = Field(10, ge=1, le=30, description="daily agent: LinkedIn/XING shortlist size")
    max_extract: int = Field(30, ge=1, le=100, description="daily agent: postings to extract")


class Health(BaseModel):
    outdated: bool = Field(description="the Python code changed since this server started")
    neo4j: bool
    claude: str | None = Field(description="path of the Claude Code binary, if found")
    running: RunInfo | None


class Coverage(BaseModel):
    coverage: float | None
    missing: list[str]


class FitSkill(BaseModel):
    skill: str
    category: str
    required: bool
    my_credit: float
    via: list[str]


class Fit(BaseModel):
    available: bool = Field(description="false if Neo4j is not running")
    coverage: float | None = None
    skills: list[FitSkill] = []


class Insight(BaseModel):
    available: bool
    rows: list[dict] = []


class NewMapping(BaseModel):
    raw: str
    skill: str | None
    category: str | None
    parent: str | None


class SkillReview(BaseModel):
    new: list[NewMapping]
    unimported: list[str]


class TodayQueue(BaseModel):
    cards: list[Card] = Field(description="queued today, ad posted within FRESH_DAYS")
    older: int = Field(description="still queued but older: hidden, can be dismissed")


class StatusCounts(BaseModel):
    queued_today: int = Field(description="what the Today page shows")
    new_today: int
    pending_extraction: int = Field(description="fresh target postings the agent must extract")
    queued: int
    selected: int
    ready: int
    inbox_open: int = Field(description="alert jobs waiting for a pasted description")
    newest_alert: dict[str, str | None] = Field(description="date of the newest email per source")
    unembedded: int = Field(description="postings with text but no embedding: run daily")


def create_app(
    connect: Callable[[], sqlite3.Connection],
    resume_tex: Path = tailor.TEX,
    inbox_dir: Path = inbox.INBOX,
    drafts_dir: Path = applications.DRAFTS,
    static_dir: Path | None = None,
    hosts: list[str] = HOSTS,
    runs: runner.Runner | None = None,
    graph_query: Callable[..., list[dict]] | None = None,
    taxonomy: Path = skills.TAXONOMY,
    documents_dir: Path | None = None,  # None: documents.root() (APPLICATION_DIR)
) -> FastAPI:
    """`connect` opens a fresh connection per request (sync endpoints run in threads)."""
    app = FastAPI(title="job-search-assistant", version="0.7b")
    started_code = code_stamp()  # the UI warns when the code on disk is newer
    runs = runs or runner.Runner(connect)
    query = graph_query or (lambda *a, **kw: graph.query(graph.shared_driver(), *a, **kw))

    def ask_graph(*args, **kwargs) -> list[dict] | None:
        """Graph rows, or None if Neo4j is down (the UI then offers to start it)."""
        try:
            return query(*args, **kwargs)
        except Exception:
            return None

    def run_info(conn: sqlite3.Connection, run_id: int) -> RunInfo:
        r = conn.execute("SELECT * FROM ui_runs WHERE id = ?", (run_id,)).fetchone()
        if r is None:
            raise HTTPException(404, f"no run {run_id}")
        return RunInfo(**{k: r[k] for k in r.keys() if k != "args_json"},
                       label=runner.KINDS[r["kind"]], summary=run_summary(conn, r))  # fmt: skip

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            json_body = request.headers.get("content-type", "").startswith("application/json")
            if request.headers.get("origin") not in ORIGINS or not json_body:
                return JSONResponse({"detail": "cross-site write blocked"}, status_code=403)
        return await call_next(request)

    def card(conn: sqlite3.Connection, r: dict, events: dict | None = None) -> Card:
        url = inbox.alert_urls(conn, [r["job_id"]]).get(r["job_id"], r["url"])
        events = applications.last_events(conn) if events is None else events
        last_at, applied_at = events.get(r["job_id"], (None, None))
        return Card(
            **{f: r[f] for f in Card.model_fields if f in r},
            apply_url=url,
            has_resume=r["resume_json"] is not None,
            has_letter=r["letter"] is not None,
            applied_at=applied_at,
            last_event_at=last_at,
            ghost_hint=applications.ghost_hint(r["status"], last_at),
        )

    def act(job_id: int, change: Callable[[sqlite3.Connection], None]) -> Card:
        with closing(connect()) as conn:
            try:
                change(conn)
                return card(conn, applications.item(conn, job_id))
            except ValueError as exc:  # a rule said no (wrong status, unknown job, ...)
                raise HTTPException(409, str(exc)) from None

    @app.get("/api/status")
    def status() -> StatusCounts:
        with closing(connect()) as conn:
            s = daily.status(conn, datetime.now(UTC).date().isoformat())
            unembedded = len(embed.pending(conn, embed.DEFAULT_MODEL)[0])
            open_items = len(inbox.open_items(conn, inbox_dir))
            today = len(today_ids(conn))
            newest = dict(conn.execute(
                "SELECT source, max(posted_at) FROM jobs WHERE source IN ('linkedin', 'xing') "
                "GROUP BY source"
            ).fetchall())  # fmt: skip
        return StatusCounts(
            **s,
            queued_today=today,
            inbox_open=open_items,
            unembedded=unembedded,
            newest_alert={k: newest.get(k) for k in ("linkedin", "xing")},
        )

    def today_ids(conn: sqlite3.Connection) -> set[int]:
        """Queued today (local) and the ad is still fresh: the only jobs worth reviewing."""
        queued = {r[0] for r in conn.execute(
            "SELECT job_id FROM applications WHERE status = 'queued' AND queued_at >= ?",
            (inbox.local_midnight(),),
        )}  # fmt: skip
        return queued & inbox.fresh_job_ids(conn)

    @app.get("/api/queue")
    def today_queue() -> TodayQueue:
        with closing(connect()) as conn:
            fresh = today_ids(conn)
            rows = applications.items(conn, "queued")
            return TodayQueue(cards=[card(conn, r) for r in rows if r["job_id"] in fresh],
                              older=sum(r["job_id"] not in fresh for r in rows))  # fmt: skip

    @app.post("/api/queue/dismiss-older")
    def dismiss_older() -> dict[str, str]:
        """Skip every queued job that is no longer on today's list (kept, not deleted)."""
        with closing(connect()) as conn:
            fresh = today_ids(conn)
            old = [r["job_id"] for r in applications.items(conn, "queued")
                   if r["job_id"] not in fresh]  # fmt: skip
            for job_id in old:
                applications.close(conn, job_id, "skipped", "not reviewed while fresh")
        return {"detail": f"dismissed {len(old)} older job(s)"}

    @app.get("/api/applications")
    def list_applications(status: Status | None = None) -> list[Card]:
        with closing(connect()) as conn:
            events = applications.last_events(conn)
            return [card(conn, r, events) for r in applications.items(conn, status)]

    @app.get("/api/tracker")
    def tracker() -> TrackerStats:
        with closing(connect()) as conn:
            return TrackerStats(**applications.stats(conn))

    @app.get("/api/applications/{job_id}")
    def get_application(job_id: int) -> Detail:
        with closing(connect()) as conn:
            try:
                r = applications.item(conn, job_id)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from None
            description = conn.execute(
                "SELECT description FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()[0]
            c = card(conn, r)
            files = documents.saved(conn, job_id)
            events = applications.history(conn, job_id)
        review = None
        if r["resume_json"]:
            review = resume_review(
                tailor.parse(resume_tex.read_text()),
                tailor.TailoredResume.model_validate_json(r["resume_json"]),
            )
        claims = json.loads(r["claims_json"]) if r["claims_json"] else []
        return Detail(card=c, description=description, resume=review, letter=r["letter"],
                      claims=claims, files=files, history=events)  # fmt: skip

    @app.post("/api/applications/{job_id}/select")
    def select(job_id: int, body: Select) -> Card:
        return act(job_id, lambda c: applications.select(c, job_id, body.letter, body.note))

    @app.post("/api/applications/{job_id}/letter")
    def letter(job_id: int, body: Action) -> Card:
        return act(job_id, lambda c: applications.request_letter(c, job_id, body.note))

    @app.post("/api/applications/{job_id}/skip")
    def skip(job_id: int, body: Action) -> Card:
        return act(job_id, lambda c: applications.close(c, job_id, "skipped", body.note))

    @app.post("/api/applications/{job_id}/applied")
    def applied(job_id: int, body: Dated) -> Card:
        """Record that I applied, then rename the PDFs I saved for it after the job."""

        def change(conn: sqlite3.Connection) -> None:
            applications.close(conn, job_id, "applied", body.note, body.date)
            try:
                documents.finalize(conn, job_id, resume_tex.read_text(), drafts_dir, documents_dir)
            except Exception:  # the status is recorded; the files can be re-saved by hand
                logging.getLogger(__name__).exception("renaming the PDFs of job %d", job_id)

        return act(job_id, change)

    @app.post("/api/applications/{job_id}/status")
    def outcome(job_id: int, body: OutcomeChange) -> Card:
        return act(job_id, lambda c: applications.record_outcome(
            c, job_id, body.status, body.note, body.date))  # fmt: skip

    @app.post("/api/applications/{job_id}/save")
    def save_file(job_id: int, body: FileRequest) -> SavedFile:
        """Compile the draft (a few seconds) and write it into my documents folder."""
        with closing(connect()) as conn:
            try:
                path = documents.save(conn, job_id, body.kind, resume_tex.read_text(),
                                      drafts_dir, documents_dir)  # fmt: skip
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from None
            except subprocess.CalledProcessError:
                raise HTTPException(500, "LaTeX failed; see data/drafts/ for the log") from None
        return SavedFile(kind=body.kind, path=str(path), exists=True)

    @app.post("/api/applications/{job_id}/reveal")
    def reveal(job_id: int, body: FileRequest) -> dict[str, str]:
        """Show the saved file in Finder (macOS)."""
        with closing(connect()) as conn:
            files = {f["kind"]: f for f in documents.saved(conn, job_id)}
        f = files.get(body.kind)
        if f is None or not f["exists"]:
            raise HTTPException(404, f"no saved {body.kind} for job {job_id}")
        subprocess.run(["open", "-R", f["path"]], check=False)
        return {"detail": f["path"]}

    @app.get("/api/applications/{job_id}/resume.pdf")
    def resume_pdf(job_id: int) -> FileResponse:
        """Renders and compiles the tailored résumé (a few seconds), then sends the PDF."""
        with closing(connect()) as conn:
            try:
                paths, _ = applications.export(conn, job_id, resume_tex.read_text(), drafts_dir)
            except ValueError as exc:
                raise HTTPException(404, str(exc)) from None
        pdf = next((p for p in paths if p.suffix == ".pdf"), None)
        if pdf is None:
            raise HTTPException(404, f"job {job_id} has no tailored résumé yet")
        return FileResponse(pdf, media_type="application/pdf", filename=f"resume-{job_id}.pdf")

    @app.get("/api/inbox")
    def list_inbox() -> list[InboxItem]:
        with closing(connect()) as conn:
            return [InboxItem(job_id=r["id"], **{k: r[k] for k in r if k != "id"})
                    for r in inbox.open_items(conn, inbox_dir)]  # fmt: skip

    @app.post("/api/inbox/{job_id}")
    def paste(job_id: int, body: Paste) -> dict[str, str]:
        with closing(connect()) as conn:
            try:
                inbox.save_paste(conn, job_id, body.description, body.notes, body.expired)
            except ValueError as exc:
                raise HTTPException(409, str(exc)) from None
        inbox.close_file(job_id, inbox_dir)
        return {"detail": "saved; run `daily --skip-ingest` to embed, then the daily agent"}

    @app.get("/api/alerts")
    def alerts() -> AlertFunnel:
        """Each recent LinkedIn/XING job and why it is (not) in my queue."""
        with closing(connect()) as conn:
            return AlertFunnel(**inbox.alert_funnel(conn, search.current_extraction()))

    @app.get("/api/health")
    def health() -> Health:
        try:
            graph.shared_driver().verify_connectivity()
            neo4j = True
        except Exception:
            graph._shared.clear()  # reconnect next time (Neo4j may have been restarted)
            neo4j = False
        binary = runner.claude_bin()
        with closing(connect()) as conn:
            busy = conn.execute("SELECT id FROM ui_runs WHERE status = 'running'").fetchone()
            running = run_info(conn, busy[0]) if busy else None
        return Health(outdated=code_stamp() > started_code, neo4j=neo4j,
                      claude=str(binary) if binary else None, running=running)  # fmt: skip

    @app.post("/api/runs")
    def start_run(body: RunRequest) -> RunInfo:
        try:
            run_id = runs.start(body.kind, body.k, body.max_extract)
        except RuntimeError as exc:  # another run is still going
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:  # e.g. Claude Code not found
            raise HTTPException(400, str(exc)) from None
        with closing(connect()) as conn:
            return run_info(conn, run_id)

    @app.get("/api/runs")
    def list_runs() -> list[RunInfo]:
        with closing(connect()) as conn:
            ids = [r[0] for r in conn.execute("SELECT id FROM ui_runs ORDER BY id DESC LIMIT 25")]
            return [run_info(conn, i) for i in ids]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: int, offset: int = 0) -> RunLog:
        with closing(connect()) as conn:
            info = run_info(conn, run_id)
        lines, next_offset = runs.log(run_id, offset)
        return RunLog(run=info, lines=lines, next_offset=next_offset)

    @app.post("/api/runs/{run_id}/cancel")
    def cancel_run(run_id: int) -> RunInfo:
        runs.cancel(run_id)
        with closing(connect()) as conn:
            return run_info(conn, run_id)

    @app.get("/api/coverage")
    def coverage(ids: str) -> dict[int, Coverage]:
        """Coverage of required skills per job (comma-separated ids); {} if Neo4j is down."""
        job_ids = [int(i) for i in ids.split(",") if i.strip().isdigit()][:200]
        rows = ask_graph("coverage_of", "target", 200, job_ids=job_ids) or []
        return {r["job_id"]: Coverage(coverage=r["coverage"], missing=r["missing"]) for r in rows}

    @app.get("/api/applications/{job_id}/fit")
    def fit(job_id: int) -> Fit:
        rows = ask_graph("fit", "target", 200, job_id=job_id)
        if rows is None:
            return Fit(available=False)
        required = [r["my_credit"] for r in rows if r["required"]]
        cov = round(sum(required) / len(required), 2) if required else None
        return Fit(available=True, coverage=cov, skills=[FitSkill(**r) for r in rows])

    @app.get("/api/skills/insights")
    def insights(
        kind: Literal["gaps", "unlock", "coverage", "roles", "together"] = "gaps",
        scope: Literal["eligible", "target"] = "target",
        k: int = 20,
        skill: str | None = None,
    ) -> Insight:
        if kind == "together" and not skill:
            raise HTTPException(422, "kind=together needs a skill")
        extra = {"skill": skill} if kind == "together" else {}
        rows = ask_graph(kind, scope, max(1, min(k, 50)), **extra)
        return Insight(available=rows is not None, rows=rows or [])

    @app.get("/api/skills/review")
    def skill_review() -> SkillReview:
        with closing(connect()) as conn:
            return SkillReview(**skills.review_state(conn, taxonomy))

    @app.post("/api/skills/export")
    def skill_export() -> dict[str, str]:
        """Write the DB's taxonomy to the review YAML (refused if that would lose YAML edits)."""
        with closing(connect()) as conn:
            state = skills.review_state(conn, taxonomy)
            if state["unimported"]:
                n = len(state["unimported"])
                raise HTTPException(409, f"{taxonomy} has {n} edit(s) not imported yet; run "
                                    "`skills import` first, or they'd be lost")  # fmt: skip
            n = skills.export_yaml(conn, skills.current_raw(conn), taxonomy)
        return {"detail": f"wrote {n} skills to {taxonomy}; review the git diff"}

    if static_dir and static_dir.is_dir():  # the built React app; dev uses Vite instead
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    return app


def run_summary(conn: sqlite3.Connection, run: sqlite3.Row) -> str | None:
    """One line on what an agent run changed, counted from the queue (not the agent's words)."""
    if run["status"] == "running" or run["kind"] not in ("agent_daily", "agent_prepare"):
        return None
    window = (run["started_at"], run["finished_at"])
    if run["kind"] == "agent_daily":
        rows = conn.execute(
            f"SELECT {inbox.channel_sql('a.job_id')} AS channel, count(*) FROM applications a "
            "WHERE a.queued_at BETWEEN ? AND ? GROUP BY channel",
            window,
        )
        n = dict(rows.fetchall())
        total = sum(n.values())
        return (f"{total} job(s) queued: {n.get('alerts', 0)} from LinkedIn/XING, "
                f"{n.get('other', 0)} from other sites")  # fmt: skip
    resumes, letters = (
        conn.execute(
            f"SELECT count(*) FROM {t} WHERE written_at BETWEEN ? AND ?", window
        ).fetchone()[0]
        for t in ("tailored_resumes", "cover_letters")
    )
    return f"{resumes} résumé(s) and {letters} letter(s) drafted"


def resume_review(resume: tailor.Resume, t: tailor.TailoredResume) -> ResumeReview:
    """Each tailored bullet with the original bullets it rewords (for side-by-side review)."""
    reordered = [(s.label, s.items) for s in t.skills] != resume.skills
    return ResumeReview(
        summary_before=resume.summary,
        summary_after=t.summary,
        skills=t.skills if reordered else None,
        entries=[
            ReviewEntry(
                id=e.id,
                heading=resume.entry(e.id).heading,
                bullets=[
                    ReviewBullet(
                        text=b.text,
                        sources=[SourceBullet(id=s, text=resume.bullet(s)) for s in b.sources],
                    )  # fmt: skip
                    for b in e.bullets
                ],
            )
            for e in t.entries
        ],
        dropped=tailor.dropped(resume, t),
    )


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(prog="python -m job_search_assistant.api")
    parser.add_argument("--openapi", action="store_true", help="print the OpenAPI schema")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    os.chdir(ROOT)  # profile/, data/ and eval/ paths are relative to the repo
    app = create_app(lambda: db.connect(ROOT / "data/jobs.db"), static_dir=ROOT / "web/dist")
    if args.openapi:
        print(json.dumps(app.openapi(), indent=2))
        return
    uvicorn.run(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
