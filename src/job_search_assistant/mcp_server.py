"""MCP server: lets an MCP client (Claude Code, Claude Desktop, ...) work on the job DB.

    uv run python -m job_search_assistant.mcp_server   # stdio; Claude Code starts it (.mcp.json)

Phase 2 without the Claude API: the *client's* model is the extractor. The
server holds no LLM; it serves the guidelines and postings and validates and
stores what the model sends back, in the same `extractions` table and under
the same prompt version as the API extractor, so `extract score` grades both.

stdout is the protocol channel (JSON-RPC): never print() here, log to stderr.
"""

import logging
import sqlite3
from collections.abc import Callable
from contextlib import closing
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from job_search_assistant import db, evalset, extract

ROOT = Path(__file__).resolve().parents[2]  # the client may start us from any directory
MODEL_PREFIX = "claude-code/"  # keeps client-made results apart from API results
MAX_LIMIT = 50

INSTRUCTIONS = """\
Job-search database (postings from Arbeitnow, Bundesagentur, Adzuna, LinkedIn/XING alerts).
For extraction: call get_extraction_guidelines once, then list_jobs_to_extract, then
get_job + save_extraction per posting. Judge each posting only from get_job's text."""


def build_server(
    connect: Callable[[], sqlite3.Connection],
    guidelines: str,
    labels_dir: Path = evalset.LABELS_DIR,
) -> MCPServer:
    """`connect` opens a fresh connection per call (tools may run in worker threads)."""
    system = extract.system_prompt(guidelines)
    version = extract.prompt_version(system)
    server = MCPServer("job-search-assistant", instructions=INSTRUCTIONS)

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
        model: str, scope: Literal["eval", "corpus"] = "eval", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Postings that have no extraction yet from `model` under the current guidelines.

        model: your own model id, e.g. "claude-opus-5-5".
        scope: "eval" = the 30 hand-labelled postings; "corpus" = every posting with a
        description.
        """
        limit = max(1, min(limit, MAX_LIMIT))
        with closing(connect()) as conn:
            if scope == "eval":
                jobs = [job for _, _, job in extract.eval_jobs(conn, labels_dir)]
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

    @server.prompt()
    def extract_postings(scope: str = "eval") -> str:
        """Extract every remaining posting in a scope (eval or corpus)."""
        return (
            f"Extract the remaining job postings in scope {scope!r} with the "
            "job-search-assistant tools. First call get_extraction_guidelines. Then, until "
            "list_jobs_to_extract returns nothing: get_job, extract exactly per the "
            "guidelines, save_extraction with your own model id. Use only the text returned "
            "by get_job; do not read files in this repository (in particular eval/labels/, "
            "which holds the answers you are graded against). Report how many you saved."
        )

    return server


def main() -> None:
    logging.basicConfig(level=logging.INFO)  # stderr; stdout belongs to the protocol
    guidelines = (ROOT / extract.GUIDELINES).read_text()
    server = build_server(
        lambda: db.connect(ROOT / "data/jobs.db"), guidelines, ROOT / evalset.LABELS_DIR
    )
    server.run()


if __name__ == "__main__":
    main()
