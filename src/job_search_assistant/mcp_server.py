"""MCP server: lets an MCP client (Claude Code, Claude Desktop, ...) work on the job DB.

    uv run python -m job_search_assistant.mcp_server   # stdio; Claude Code starts it (.mcp.json)

Phase 2 without the Claude API: the *client's* model is the extractor. The
server holds no LLM; it serves the guidelines and postings and validates and
stores what the model sends back, in the same `extractions` table and under
the same prompt version as the API extractor, so `extract score` grades both.

Phase 3 RAG works the same way: the server *retrieves* (local embeddings +
extraction filters) and Claude Code *generates* (explains fit and gaps).

stdout is the protocol channel (JSON-RPC): never print() here, log to stderr.
"""

import json
import logging
import sqlite3
import threading
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from job_search_assistant import db, embed, evalset, extract, search

ROOT = Path(__file__).resolve().parents[2]  # the client may start us from any directory
MODEL_PREFIX = "claude-code/"  # keeps client-made results apart from API results
MAX_LIMIT = 50

INSTRUCTIONS = """\
Job-search database (postings from Arbeitnow, Bundesagentur, Adzuna, LinkedIn/XING alerts).
For extraction: call get_extraction_guidelines once, then list_jobs_to_extract, then
get_job + save_extraction per posting. Judge each posting only from get_job's text.
For job matching: get_profile (my résumé), match_resume or search_jobs, then get_job for
the full text of promising results."""


def build_server(
    connect: Callable[[], sqlite3.Connection],
    guidelines: str,
    labels_dir: Path = evalset.LABELS_DIR,
    resume: Path = search.RESUME,
    load_encoder: Callable[[], embed.Encoder] = embed.load_encoder,
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

    def retrieve(text: str, k: int, my_filters: bool) -> list[dict[str, Any]]:
        k = max(1, min(k, MAX_LIMIT))
        extraction = (search.EXTRACTION_MODEL, version)
        filters = search.MY_FILTERS if my_filters else search.Filters()
        with closing(connect()) as conn:
            hits = search.search(conn, encode(text), k=k, filters=filters, extraction=extraction)
            results = []
            for h in hits:
                row = conn.execute(
                    """SELECT output_json FROM extractions
                       WHERE job_id = ? AND model = ? AND prompt_version = ?""",
                    (h.job_id, *extraction),
                ).fetchone()
                summary = {}
                if row:
                    x = json.loads(row[0])
                    summary = {f: x[f] for f in ("role_family", "seniority", "german_level")}
                    summary["required_skills"] = x["required_skills"]
                results.append(
                    {
                        "job_id": h.job_id,
                        "title": h.title,
                        "company": h.company,
                        "location": h.location,
                        "url": h.url,
                        "similarity": round(h.similarity, 3),
                        **summary,
                    }
                )
        return results

    def stored_model(model: str) -> str:
        return model if model.startswith(MODEL_PREFIX) else MODEL_PREFIX + model

    @server.tool()
    def get_extraction_guidelines() -> str:
        """The extraction spec. Read it before extracting; apply every rule exactly.

        Returns the prompt_version that save_extraction must be called with.
        """
        return f"prompt_version: {version}\n\n{system}"

    @server.tool()
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

    @server.tool()
    def get_job(job_id: int) -> str:
        """One posting's title, company, location and full description."""
        with closing(connect()) as conn:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise ToolError(f"no job with id {job_id}")
        return extract.posting_message(dict(row))

    @server.tool()
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

    @server.tool()
    def get_profile() -> str:
        """My résumé as plain text (skills, experience, projects, education, languages)."""
        if not resume.exists():
            raise ToolError(f"no résumé at {resume}")
        return resume.read_text()

    @server.tool()
    def match_resume(k: int = 10, my_filters: bool = True) -> list[dict[str, Any]]:
        """Postings most similar to my résumé (semantic search), best first.

        my_filters: only postings in my scope (target role, junior/mid/unknown seniority, ≤ 2 years,
        German at most basic, no other language required, in-scope location).
        Similarity only ranks results; its absolute value is not a fit percentage.
        """
        if not resume.exists():
            raise ToolError(f"no résumé at {resume}")
        return retrieve(resume.read_text(), k, my_filters)

    @server.tool()
    def search_jobs(query: str, k: int = 10, my_filters: bool = True) -> list[dict[str, Any]]:
        """Semantic search over postings for a free-text query (German or English), best first.

        my_filters: as in match_resume. Set false to search all postings, including ones
        without an extraction (e.g. jobs the title filter missed).
        """
        return retrieve(query, k, my_filters)

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


def main() -> None:
    logging.basicConfig(level=logging.INFO)  # stderr; stdout belongs to the protocol
    guidelines = (ROOT / extract.GUIDELINES).read_text()
    server = build_server(
        lambda: db.connect(ROOT / "data/jobs.db"),
        guidelines,
        ROOT / evalset.LABELS_DIR,
        resume=ROOT / search.RESUME,
    )
    server.run()


if __name__ == "__main__":
    main()
