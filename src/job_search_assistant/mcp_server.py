"""MCP server: lets an MCP client (Claude Code, Claude Desktop, ...) work on the job DB.

    uv run python -m job_search_assistant.mcp_server   # stdio; Claude Code starts it (.mcp.json)

Phase 2 without the Claude API: the *client's* model is the extractor. The
server holds no LLM; it serves the guidelines and postings and validates and
stores what the model sends back, in the same `extractions` table and under
the same prompt version as the API extractor, so `extract score` grades both.

Phase 3 RAG works the same way: the server *retrieves* (local embeddings +
extraction filters) and Claude Code *generates* (explains fit and gaps).

Phase 5 rounds it off for the Phase 6 agent: typed results (Pydantic models, so
clients get an output schema), annotations (read-only vs. writing tools),
resources for plain documents, and the agent's inputs (new jobs, job details,
per-job skill fit). No tool submits anything anywhere: the agent drafts, I apply.

stdout is the protocol channel (JSON-RPC): never print() here, log to stderr.
"""

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from datetime import date
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from job_search_assistant import db, embed, evalset, extract, graph, search, skills

ROOT = Path(__file__).resolve().parents[2]  # the client may start us from any directory
MODEL_PREFIX = "claude-code/"  # keeps client-made results apart from API results
MAX_LIMIT = 50

INSTRUCTIONS = """\
Job-search database (postings from Arbeitnow, Bundesagentur, Adzuna, LinkedIn/XING alerts).
For extraction: call get_extraction_guidelines once, then list_jobs_to_extract, then
get_job + save_extraction per posting. Judge each posting only from get_job's text.
For job matching: get_profile (my résumé), match_resume or search_jobs, then get_job for
the full text of promising results.
For skill normalization: get_skill_guidelines once, then per batch list_canonical_skills,
list_unmapped_skills, save_skill_mappings.
For career planning: skill_gaps (knowledge graph in Neo4j) — which skills I lack, which
single skill unlocks jobs, how well I cover each job, what each role asks for.
For a daily run: list_new_jobs, then get_job_details and job_fit per job.
Nothing here applies to jobs; drafts are for me to review and submit."""

# Hints for clients deciding what to run without asking. Hints, not guarantees: a client
# should only trust them from servers it trusts. No tool reaches outside this machine.
READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
# Saves add rows and never overwrite; repeating a save is rejected, so it changes nothing.
APPEND = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False
)


class JobSummary(BaseModel):
    """One posting in a result list, with its extraction's key fields if it has one."""

    job_id: int
    title: str
    company: str
    location: str | None
    url: str
    similarity: float | None = Field(
        None, description="search only: ranks results, not a fit percentage"
    )
    first_seen: str | None = Field(None, description="list_new_jobs only (ISO timestamp)")
    role_family: str | None = None
    seniority: str | None = None
    german_level: str | None = None
    required_skills: list[str] | None = None


class NewJobs(BaseModel):
    since: str
    new_postings: int = Field(description="new duplicate groups, before my filters")
    pending_extraction: int = Field(
        description="new target-title postings without an extraction; my filters drop them "
        "until extracted (prompt extract_postings, scope target)"
    )
    jobs: list[JobSummary]


class Duplicate(BaseModel):
    job_id: int
    source: str
    url: str


class JobDetails(BaseModel):
    job_id: int
    source: str
    url: str
    title: str
    company: str
    location: str | None
    posted_at: str | None
    first_seen: str
    lang: str | None
    description: str = Field(description="Adzuna: 500-char snippet; email alerts: empty")
    eligible: bool = Field(description="passes my filters (as in match_resume)")
    extraction: extract.Extraction | None
    duplicates: list[Duplicate] = Field(description="the same job from other sources")


class SkillFit(BaseModel):
    skill: str
    category: str
    required: bool = Field(description="false = nice to have")
    my_credit: float = Field(
        description="1.0 I have it or a narrower skill; 0.5 only a broader one; 0.0 missing"
    )
    via: list[str] = Field(description="my skills that give the credit")


class JobFit(BaseModel):
    job_id: int
    title: str
    company: str
    coverage: float | None = Field(
        description="share of required skills I cover (partial counts half); "
        "None if no required skill is mapped"
    )
    skills: list[SkillFit]


def build_server(
    connect: Callable[[], sqlite3.Connection],
    guidelines: str,
    labels_dir: Path = evalset.LABELS_DIR,
    resume: Path = search.RESUME,
    load_encoder: Callable[[], embed.Encoder] = embed.load_encoder,
    skill_spec: str = "",
    graph_query: Callable[..., list[dict]] | None = None,
) -> MCPServer:
    """`connect` opens a fresh connection per call (tools may run in worker threads).

    The embedding model is loaded on the first search, not at startup (~2 GB, ~10 s).
    """
    system = extract.system_prompt(guidelines)
    version = extract.prompt_version(system)
    server = MCPServer("job-search-assistant", instructions=INSTRUCTIONS)
    encoder: list[embed.Encoder] = []
    encoder_lock = threading.Lock()

    def encode(text: str):
        with encoder_lock:
            if not encoder:
                encoder.append(load_encoder())
        return encoder[0]([text])[0]

    extraction = (search.EXTRACTION_MODEL, version)

    def load_extraction(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT output_json FROM extractions WHERE job_id = ? AND model = ? "
            "AND prompt_version = ?",
            (job_id, *extraction),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def summarize(conn: sqlite3.Connection, job: dict[str, Any], **extra: Any) -> JobSummary:
        x = load_extraction(conn, job["id"]) or {}
        fields = ("role_family", "seniority", "german_level", "required_skills")
        return JobSummary(
            job_id=job["id"],
            title=job["title"],
            company=job["company"],
            location=job["location"],
            url=job["url"],
            **{f: x[f] for f in fields if f in x},
            **extra,
        )

    def retrieve(text: str, k: int, my_filters: bool) -> list[JobSummary]:
        k = max(1, min(k, MAX_LIMIT))
        filters = search.MY_FILTERS if my_filters else search.Filters()
        with closing(connect()) as conn:
            hits = search.search(conn, encode(text), k=k, filters=filters, extraction=extraction)
            return [
                summarize(
                    conn,
                    {
                        "id": h.job_id,
                        "title": h.title,
                        "company": h.company,
                        "location": h.location,
                        "url": h.url,
                    },
                    similarity=round(h.similarity, 3),
                )  # fmt: skip
                for h in hits
            ]

    def stored_model(model: str) -> str:
        return model if model.startswith(MODEL_PREFIX) else MODEL_PREFIX + model

    @server.tool(annotations=READ_ONLY)
    def get_extraction_guidelines() -> str:
        """The extraction spec. Read it before extracting; apply every rule exactly.

        Returns the prompt_version that save_extraction must be called with.
        """
        return f"prompt_version: {version}\n\n{system}"

    @server.tool(annotations=READ_ONLY)
    def list_jobs_to_extract(
        model: str, scope: Literal["eval", "target", "corpus"] = "eval", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Postings that have no extraction yet from `model` under the current guidelines.

        model: your own model id, e.g. "claude-opus-5-5".
        scope: "eval" = the 30 hand-labelled postings; "target" = one posting per duplicate
        group with a target-role title, in-scope location and full description;
        "corpus" = every posting with a description.
        """
        limit = max(1, min(limit, MAX_LIMIT))
        with closing(connect()) as conn:
            if scope == "eval":
                jobs = [job for _, _, job in extract.eval_jobs(conn, labels_dir)]
            elif scope == "target":
                jobs = [job for job in evalset.candidates(conn) if job["target"]]
            else:
                jobs = [dict(r) for r in conn.execute(
                    "SELECT id, title FROM jobs WHERE description != '' ORDER BY id"
                )]  # fmt: skip
            done = {
                r[0]
                for r in conn.execute(
                    "SELECT job_id FROM extractions WHERE model = ? AND prompt_version = ?",
                    (stored_model(model), version),
                )
            }
        todo = [{"job_id": j["id"], "title": j["title"]} for j in jobs if j["id"] not in done]
        return todo[:limit]

    @server.tool(annotations=READ_ONLY)
    def get_job(job_id: int) -> str:
        """One posting's title, company, location and full description."""
        with closing(connect()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ToolError(f"no job with id {job_id}")
        return extract.posting_message(dict(row))

    @server.tool(annotations=APPEND)
    def save_extraction(
        job_id: int, model: str, prompt_version: str, extraction: extract.Extraction
    ) -> str:
        """Store your extraction for one posting. Results are never overwritten.

        model: your own model id, e.g. "claude-opus-5-5".
        prompt_version: as returned by get_extraction_guidelines.
        """
        if prompt_version != version:
            raise ToolError(
                f"prompt_version {prompt_version!r} is stale (current: {version}); "
                "the guidelines changed, call get_extraction_guidelines again"
            )
        with closing(connect()) as conn:
            if conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job_id,)).fetchone() is None:
                raise ToolError(f"no job with id {job_id}")
            try:
                extract.store_extraction(
                    conn, job_id, extraction, model=stored_model(model), version=version
                )
            except sqlite3.IntegrityError:
                raise ToolError(
                    f"job {job_id} already has an extraction from {stored_model(model)}"
                ) from None
        return f"saved job {job_id} as {stored_model(model)} / {version}"

    @server.tool(annotations=READ_ONLY)
    def get_profile() -> str:
        """My résumé as plain text (skills, experience, projects, education, languages)."""
        if not resume.exists():
            raise ToolError(f"no résumé at {resume}")
        return resume.read_text()

    @server.tool(annotations=READ_ONLY)
    def match_resume(k: int = 10, my_filters: bool = True) -> list[JobSummary]:
        """Postings most similar to my résumé (semantic search), best first.

        my_filters: only postings in my scope (target role, junior/mid/unknown seniority, ≤ 2 years,
        German at most basic, no other language required, in-scope location).
        Similarity only ranks results; its absolute value is not a fit percentage.
        """
        if not resume.exists():
            raise ToolError(f"no résumé at {resume}")
        return retrieve(resume.read_text(), k, my_filters)

    @server.tool(annotations=READ_ONLY)
    def search_jobs(query: str, k: int = 10, my_filters: bool = True) -> list[JobSummary]:
        """Semantic search over postings for a free-text query (German or English), best first.

        my_filters: as in match_resume. Set false to search all postings, including ones
        without an extraction (e.g. jobs the title filter missed).
        """
        return retrieve(query, k, my_filters)

    def raw_skill_table(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
        text = resume.read_text() if resume.exists() else ""
        return skills.raw_skills(conn, (search.EXTRACTION_MODEL, version), text)

    @server.tool(annotations=READ_ONLY)
    def get_skill_guidelines() -> str:
        """The skill normalization rules. Read them before mapping; apply them exactly."""
        return f"Categories: {', '.join(skills.CATEGORIES)}\n\n{skill_spec}"

    @server.tool(annotations=READ_ONLY)
    def list_canonical_skills() -> dict[str, dict[str, str | None]]:
        """Every canonical skill so far: name -> {category, parent}. Reuse these names."""
        with closing(connect()) as conn:
            return skills.canonical_skills(conn)

    @server.tool(annotations=READ_ONLY)
    def list_unmapped_skills(limit: int = 200) -> dict[str, Any]:
        """Raw skills not mapped yet, alphabetical (variants sit together), with counts.

        raw: the key to pass back to save_skill_mappings. display: the most common written
        form. mentions: how many postings list it. in_resume: it is one of my skills.
        """
        limit = max(1, min(limit, 500))
        with closing(connect()) as conn:
            raw = raw_skill_table(conn)
            left = skills.unmapped(conn, raw)
        return {
            "remaining": len(left),
            "batch": [{"raw": k, **raw[k]} for k in left[:limit]],
        }

    @server.tool(annotations=APPEND)
    def save_skill_mappings(mappings: list[skills.SkillMapping]) -> str:
        """Store a batch of raw -> canonical mappings. All or nothing: any error rejects the
        batch and lists every problem, so fix them and resend the whole batch."""
        with closing(connect()) as conn:
            errors = skills.save_mappings(conn, mappings, set(raw_skill_table(conn)))
        if errors:
            raise ToolError(f"{len(errors)} problem(s), nothing saved:\n" + "\n".join(errors))
        return f"saved {len(mappings)} mappings"

    @server.tool(annotations=READ_ONLY)
    def skill_gaps(
        kind: Literal["gaps", "unlock", "coverage", "roles", "together"] = "gaps",
        scope: Literal["eligible", "target"] = "eligible",
        k: int = 15,
        skill: str | None = None,
    ) -> list[dict[str, Any]]:  # untyped: each kind returns different columns
        """Knowledge-graph queries over normalized skills (jobs → skills ← me).

        kind: "gaps" = skills I lack, weighted by how many jobs need them (required 1.0,
        nice-to-have 0.5; halved when I have a broader skill). "unlock" = skills that are
        the *only* missing required skill of a job. "coverage" = share of each job's
        required skills I have. "roles" = top required skills per role family.
        "together" = skills listed alongside `skill` (canonical name).
        scope: "eligible" = jobs I can apply to now (my filters); "target" = all target-role
        jobs in the market, including senior ones (where to grow).
        """
        if kind == "together" and not skill:
            raise ToolError("kind='together' needs a canonical skill name")
        run = graph_query or default_graph_query()
        return run(kind, scope, max(1, min(k, MAX_LIMIT)), **({"skill": skill} if skill else {}))

    @server.tool(annotations=READ_ONLY)
    def list_new_jobs(since: str, my_filters: bool = True, limit: int = MAX_LIMIT) -> NewJobs:
        """Postings first seen on or after `since` (ISO date, e.g. "2026-10-22"), newest first.

        A job counts as new only if no source had it before (duplicate groups), and each
        job is listed once. my_filters: as in match_resume; postings without an extraction
        fail them, so check pending_extraction.
        """
        try:
            since = date.fromisoformat(since).isoformat()
        except ValueError:
            raise ToolError(f"since must be an ISO date like 2026-10-22, got {since!r}") from None
        limit = max(1, min(limit, MAX_LIMIT))
        params: dict[str, Any] = {"since": since, "limit": limit}
        with closing(connect()) as conn:
            filters = search.MY_FILTERS if my_filters else search.Filters()
            join, where = search.filter_sql(conn, filters, extraction, params)
            grouped = """SELECT j.*, COALESCE(g.group_id, j.id) AS grp
                         FROM jobs j LEFT JOIN job_groups g ON g.job_id = j.id"""
            new_groups = (
                f"SELECT grp FROM ({grouped}) GROUP BY grp HAVING min(first_seen) >= :since"
            )
            rows = conn.execute(
                f"""WITH new AS ({new_groups}), picked AS (
                        SELECT j.*, ROW_NUMBER() OVER (
                            PARTITION BY j.grp ORDER BY length(j.description) DESC, j.id) AS rn
                        FROM ({grouped}) j {join}
                        WHERE j.grp IN (SELECT grp FROM new) {where}
                    )
                    SELECT * FROM picked WHERE rn = 1
                    ORDER BY first_seen DESC, id DESC LIMIT :limit""",
                params,
            ).fetchall()
            total = conn.execute(
                f"SELECT count(*) FROM ({new_groups})", {"since": since}
            ).fetchone()[0]
            done = {
                r[0]
                for r in conn.execute(
                    "SELECT job_id FROM extractions WHERE model = ? AND prompt_version = ?",
                    extraction,
                )
            }
            pending = sum(
                1
                for c in evalset.candidates(conn)
                if c["target"] and c["first_seen"] >= since and c["id"] not in done
            )
            jobs = [summarize(conn, dict(r), first_seen=r["first_seen"]) for r in rows]
        return NewJobs(since=since, new_postings=total, pending_extraction=pending, jobs=jobs)

    @server.tool(annotations=READ_ONLY)
    def get_job_details(job_id: int) -> JobDetails:
        """One posting with its full text, extraction, eligibility and duplicates elsewhere.

        Use this (not get_job) when matching or drafting; get_job is for extraction.
        """
        with closing(connect()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise ToolError(f"no job with id {job_id}")
            x = load_extraction(conn, job_id)
            eligible = job_id in search.eligible_job_ids(conn, search.MY_FILTERS, extraction)
            duplicates = conn.execute(
                """SELECT j.id, j.source, j.url FROM job_groups a
                   JOIN job_groups b ON b.group_id = a.group_id AND b.job_id != a.job_id
                   JOIN jobs j ON j.id = b.job_id
                   WHERE a.job_id = ? ORDER BY j.id""",
                (job_id,),
            ).fetchall()
        return JobDetails(
            job_id=row["id"],
            source=row["source"],
            url=row["url"],
            title=row["title"],
            company=row["company"],
            location=row["location"],
            posted_at=row["posted_at"],
            first_seen=row["first_seen"],
            lang=row["lang"],
            description=row["description"],
            eligible=eligible,
            extraction=extract.Extraction.model_validate(x) if x else None,
            duplicates=[Duplicate(job_id=d[0], source=d[1], url=d[2]) for d in duplicates],
        )

    @server.tool(annotations=READ_ONLY)
    def job_fit(job_id: int) -> JobFit:
        """One job's skills against mine, from the knowledge graph (normalized skills).

        Facts for explaining fit: each required / nice-to-have skill with my credit and
        the skill of mine that earns it. Skills the taxonomy dropped (vague, soft) are absent.
        """
        with closing(connect()) as conn:
            row = conn.execute("SELECT title, company FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                raise ToolError(f"no job with id {job_id}")
            if load_extraction(conn, job_id) is None:
                raise ToolError(f"job {job_id} has no extraction, so it is not in the graph")
        run = graph_query or default_graph_query()
        rows = [SkillFit(**r) for r in run("fit", "target", MAX_LIMIT, job_id=job_id)]
        required = [s.my_credit for s in rows if s.required]
        return JobFit(
            job_id=job_id,
            title=row["title"],
            company=row["company"],
            coverage=round(sum(required) / len(required), 2) if required else None,
            skills=rows,
        )

    # Resources: documents the client *reads* (by URI, often attached by the user, e.g.
    # @job-search:profile://resume in Claude Code), where tools are actions the *model*
    # chooses to call. The tool twins above stay because models reach for tools on their own.
    @server.resource("profile://resume", mime_type="text/markdown")
    def resume_resource() -> str:
        """My résumé (the text that is embedded and searched with)."""
        return get_profile()

    @server.resource("guidelines://extraction", mime_type="text/markdown")
    def extraction_guidelines_resource() -> str:
        """The Phase 2 extraction spec, with its prompt_version."""
        return get_extraction_guidelines()

    @server.resource("guidelines://skills", mime_type="text/markdown")
    def skill_guidelines_resource() -> str:
        """The Phase 4 skill normalization rules."""
        return get_skill_guidelines()

    @server.resource("job://{job_id}")
    def job_resource(job_id: int) -> str:
        """One posting as text (a resource *template*: any job id fills the URI)."""
        return get_job(job_id)

    @server.prompt()
    def normalize_skills(batches: str = "3", batch_size: str = "200") -> str:
        """Map raw skills to canonical ones, a few batches per session."""
        return (
            f"Normalize skills with the job-search-assistant tools, up to {batches} batches of "
            f"{batch_size}. First call get_skill_guidelines. For each batch: call "
            "list_canonical_skills (reuse existing names; never create a synonym of one), then "
            f"list_unmapped_skills with limit={batch_size}, map every raw skill in the batch "
            "per the guidelines, and save_skill_mappings. If it reports problems, fix them and "
            "resend the whole batch. Stop when list_unmapped_skills returns an empty batch. "
            "Report how many you mapped and any mappings you were unsure about."
        )

    @server.prompt()
    def match_me(k: str = "10") -> str:
        """Find my best-matching jobs and explain the fit (RAG: retrieve, then generate)."""
        return (
            f"Find my {k} best-matching jobs with the job-search-assistant tools. Call "
            f"get_profile, then match_resume with k={k}, then get_job for each result. For "
            "each job write: why it fits (cite concrete items from my résumé), gaps (required "
            "skills or experience my résumé does not show), and red flags (language, "
            "seniority, location, eligibility restrictions). Finish with a ranked shortlist "
            "of the jobs worth applying to first, with one line of reasoning each. Use only "
            "what the tools return; never claim experience my résumé does not show."
        )

    @server.prompt()
    def extract_postings(scope: str = "eval", max_jobs: str = "50") -> str:
        """Extract up to max_jobs remaining postings in a scope (eval, target or corpus)."""
        return (
            f"Extract up to {max_jobs} remaining job postings in scope {scope!r} with the "
            "job-search-assistant tools. First call get_extraction_guidelines. Then, until "
            f"you have saved {max_jobs} or list_jobs_to_extract returns nothing: get_job, "
            "extract exactly per the guidelines, save_extraction with your own model id. "
            "Use only the text returned "
            "by get_job; do not read files in this repository (in particular eval/labels/, "
            "which holds the answers you are graded against). Report how many you saved."
        )

    return server


def default_graph_query() -> Callable[..., list[dict]]:
    """Neo4j-backed queries; connects once, on first use (Neo4j must be running)."""
    if not _driver:
        try:
            _driver.append(graph.connect_neo4j())
        except Exception as exc:  # no Docker, wrong password, ...
            reason = type(exc).__name__
            raise ToolError(f"Neo4j not reachable ({reason}); run docker compose up -d") from None
    return lambda *args, **kw: graph.query(_driver[0], *args, **kw)


_driver: list = []


def main() -> None:
    logging.basicConfig(level=logging.INFO)  # stderr; stdout belongs to the protocol
    guidelines = (ROOT / extract.GUIDELINES).read_text()
    server = build_server(
        lambda: db.connect(ROOT / "data/jobs.db"),
        guidelines,
        ROOT / evalset.LABELS_DIR,
        resume=ROOT / search.RESUME,
        skill_spec=(ROOT / skills.SPEC).read_text(),
    )
    server.run()


if __name__ == "__main__":
    main()
