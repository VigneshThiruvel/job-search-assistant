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
import os
import sqlite3
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

from job_search_assistant import applications, daily, db, embed, inbox, tailor

ROOT = Path(__file__).resolve().parents[2]
HOSTS = ["127.0.0.1", "localhost"]
ORIGINS = {f"http://{h}:{p}" for h in HOSTS for p in (8000, 5173)}  # app + Vite dev server
Status = Literal["queued", "selected", "ready", "skipped", "applied"]


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


class StatusCounts(BaseModel):
    new_today: int
    pending_extraction: int = Field(description="fresh target postings the agent must extract")
    queued: int
    selected: int
    ready: int
    inbox_open: int = Field(description="alert jobs waiting for a pasted description")
    unembedded: int = Field(description="postings with text but no embedding: run daily")


def create_app(
    connect: Callable[[], sqlite3.Connection],
    resume_tex: Path = tailor.TEX,
    inbox_dir: Path = inbox.INBOX,
    drafts_dir: Path = applications.DRAFTS,
    static_dir: Path | None = None,
    hosts: list[str] = HOSTS,
) -> FastAPI:
    """`connect` opens a fresh connection per request (sync endpoints run in threads)."""
    app = FastAPI(title="job-search-assistant", version="0.7a")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=hosts)

    @app.middleware("http")
    async def same_origin_writes(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            json_body = request.headers.get("content-type", "").startswith("application/json")
            if request.headers.get("origin") not in ORIGINS or not json_body:
                return JSONResponse({"detail": "cross-site write blocked"}, status_code=403)
        return await call_next(request)

    def card(conn: sqlite3.Connection, r: dict) -> Card:
        url = inbox.alert_urls(conn, [r["job_id"]]).get(r["job_id"], r["url"])
        return Card(
            **{f: r[f] for f in Card.model_fields if f in r},
            apply_url=url,
            has_resume=r["resume_json"] is not None,
            has_letter=r["letter"] is not None,
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
            unembedded = conn.execute(
                """SELECT count(*) FROM jobs j WHERE description != '' AND NOT EXISTS
                       (SELECT 1 FROM embeddings e WHERE e.job_id = j.id AND e.model = ?)""",
                (embed.DEFAULT_MODEL,),
            ).fetchone()[0]
            open_items = len(inbox.open_items(conn, inbox_dir))
        return StatusCounts(**s, inbox_open=open_items, unembedded=unembedded)

    @app.get("/api/applications")
    def list_applications(status: Status | None = None) -> list[Card]:
        with closing(connect()) as conn:
            return [card(conn, r) for r in applications.items(conn, status)]

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
        review = None
        if r["resume_json"]:
            review = resume_review(
                tailor.parse(resume_tex.read_text()),
                tailor.TailoredResume.model_validate_json(r["resume_json"]),
            )
        claims = json.loads(r["claims_json"]) if r["claims_json"] else []
        return Detail(card=c, description=description, resume=review, letter=r["letter"],
                      claims=claims)  # fmt: skip

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
    def applied(job_id: int, body: Action) -> Card:
        return act(job_id, lambda c: applications.close(c, job_id, "applied", body.note))

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

    if static_dir and static_dir.is_dir():  # the built React app; dev uses Vite instead
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="ui")
    return app


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
