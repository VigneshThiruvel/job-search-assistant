"""Phase 2: LLM extraction of structured requirements from job postings.

    python -m job_search_assistant.extract eval [--model M] [--effort E]   # Claude API
    python -m job_search_assistant.extract score --model M                 # stored results

Two extractors write to the same table: `Extractor` calls the Claude API, and
the MCP server (mcp_server.py) lets an MCP client such as Claude Code do the
extraction itself and submit results through a tool. `score` grades whatever
is stored, so both are evaluated the same way.

The output schema is generated from the eval set's field definitions
(`evalset.ENUMS` etc.) and the system prompt *is* eval/GUIDELINES.md, so the
labels, the spec and the extractor cannot drift apart. Results are stored in
the `extractions` table keyed by (job, model, prompt version): re-running
never pays for the same extraction twice, and changing the prompt or schema
creates a new version instead of overwriting old results.
"""

import argparse
import hashlib
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from job_search_assistant import evalset

if TYPE_CHECKING:
    import anthropic

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
GUIDELINES = Path("eval/GUIDELINES.md")

Level = Literal[evalset.ENUMS["german_level"][1:]]  # plus | basic | ... (no "none")


class LanguageRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    language: str = Field(description="English name, capitalized, e.g. Dutch")
    level: Level


class Extraction(BaseModel):
    """One posting's requirements. Field meanings: see the guidelines."""

    model_config = ConfigDict(extra="forbid")
    is_target_role: bool
    role_family: Literal[evalset.ENUMS["role_family"]]
    seniority: Literal[evalset.ENUMS["seniority"]]
    employment_type: Literal[evalset.ENUMS["employment_type"]]
    german_level: Literal[evalset.ENUMS["german_level"]]
    german_implied: bool
    english_level: Literal[evalset.ENUMS["english_level"]]
    other_languages: list[LanguageRequirement]
    workplace: Literal[evalset.ENUMS["workplace"]]
    years_experience_min: int | None
    degree: Literal[evalset.ENUMS["degree"]]
    required_skills: list[str]
    nice_to_have_skills: list[str]
    restrictions: list[str]
    salary: str | None

    def to_labels(self) -> dict[str, Any]:
        """Same shape as a label file's `labels:` block."""
        labels = self.model_dump()
        labels["other_languages"] = [
            f"{lang['language']}: {lang['level']}" for lang in labels["other_languages"]
        ]
        return labels


def system_prompt(guidelines: str) -> str:
    return (
        "You extract structured requirements from job postings. The guidelines below "
        "were written for a human labeller; apply every field definition and decision "
        "rule exactly as written (including the 'Proposed rules'). Ignore workflow "
        "instructions about files, commands, notes and the reviewer's own situation. "
        "Label only what the posting says.\n\n<guidelines>\n"
        f"{guidelines}\n</guidelines>"
    )


def prompt_version(system: str) -> str:
    """Changes whenever the prompt or the output schema changes."""
    schema = json.dumps(Extraction.model_json_schema(), sort_keys=True)
    return hashlib.sha256(f"{system}\n{schema}".encode()).hexdigest()[:12]


def posting_message(job: dict[str, Any]) -> str:
    return (
        f"<title>{job['title']}</title>\n<company>{job['company']}</company>\n"
        f"<location>{job['location'] or ''}</location>\n"
        f"<description>\n{job['description']}\n</description>"
    )


def stored_extraction(
    conn: sqlite3.Connection, job_id: int, model: str, version: str
) -> Extraction | None:
    row = conn.execute(
        """SELECT output_json FROM extractions
           WHERE job_id = ? AND model = ? AND prompt_version = ?""",
        (job_id, model, version),
    ).fetchone()
    return Extraction.model_validate_json(row[0]) if row else None


def store_extraction(
    conn: sqlite3.Connection,
    job_id: int,
    extraction: Extraction,
    *,
    model: str,
    version: str,
    served_model: str | None = None,
    effort: str | None = None,
    usage: tuple[int, int, int] | None = None,  # input, cache read, output tokens
) -> None:
    """Insert one result; raises sqlite3.IntegrityError if (job, model, version) exists."""
    input_tokens, cache_read_tokens, output_tokens = usage or (None, None, None)
    with conn:
        conn.execute(
            """INSERT INTO extractions (job_id, model, served_model, prompt_version, effort,
               extracted_at, output_json, input_tokens, cache_read_tokens, output_tokens)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                job_id,
                model,
                served_model or model,
                version,
                effort,
                datetime.now(UTC).isoformat(),
                extraction.model_dump_json(),
                input_tokens,
                cache_read_tokens,
                output_tokens,
            ),
        )


def eval_jobs(
    conn: sqlite3.Connection, labels_dir: Path = evalset.LABELS_DIR
) -> list[tuple[str, dict[str, Any], dict[str, Any]]]:
    """(file name, label doc, job row) for every eval posting still in the database."""
    import yaml

    found = []
    for path in sorted(labels_dir.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        row = conn.execute(
            "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
            (doc["job"]["source"], doc["job"]["source_job_id"]),
        ).fetchone()
        if row is None:
            log.warning("%s: posting no longer in the database, skipped", path.name)
            continue
        found.append((path.name, doc, dict(row)))
    return found


def score_stored(
    conn: sqlite3.Connection, model: str, version: str, labels_dir: Path = evalset.LABELS_DIR
) -> dict[str, Any]:
    """Score stored extractions of `model` against the labels; unextracted files are listed."""
    pairs, missing = [], []
    for name, doc, job in eval_jobs(conn, labels_dir):
        extraction = stored_extraction(conn, job["id"], model, version)
        if extraction is None:
            missing.append(name)
            continue
        pairs.append(
            (name, doc.get("labelled_by", "unknown"), doc["labels"], extraction.to_labels())
        )
    if not pairs:
        raise SystemExit(f"no stored extractions for model {model!r}, prompt version {version}")
    report = evalset.score(pairs)
    report["missing"] = missing
    return report


def save_report(report: dict[str, Any], model: str, label: str, version: str) -> Path:
    safe_model = model.replace("/", "_")
    out = Path("eval/results") / f"{datetime.now(UTC):%Y-%m-%d}-{safe_model}-{label}-{version}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    return out


class Extractor:
    def __init__(
        self,
        client: "anthropic.Anthropic",
        conn: sqlite3.Connection,
        guidelines: str,
        model: str = DEFAULT_MODEL,
        effort: str = DEFAULT_EFFORT,
    ) -> None:
        self.client = client
        self.conn = conn
        self.model = model
        self.effort = effort
        self.system = system_prompt(guidelines)
        self.version = prompt_version(self.system)

    def extract(self, job: dict[str, Any]) -> Extraction:
        """Cached per (job, model, prompt version); calls the API only on a miss."""
        cached = stored_extraction(self.conn, job["id"], self.model, self.version)
        if cached:
            return cached

        response = self.client.beta.messages.parse(
            model=self.model,
            max_tokens=16000,
            # The guidelines are identical for every posting: cache them.
            system=[{"type": "text", "text": self.system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": posting_message(job)}],
            output_format=Extraction,
            output_config={"effort": self.effort},
            # On a safety decline, the API re-runs the request on a fallback model.
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        if response.stop_reason == "refusal":
            raise RuntimeError(f"job {job['id']}: refused ({response.stop_details})")
        if response.stop_reason == "max_tokens" or response.parsed_output is None:
            raise RuntimeError(f"job {job['id']}: no complete output ({response.stop_reason})")

        extraction: Extraction = response.parsed_output
        usage = response.usage
        store_extraction(
            self.conn,
            job["id"],
            extraction,
            model=self.model,
            version=self.version,
            served_model=response.model,  # the fallback model, if one served the request
            effort=self.effort,
            usage=(usage.input_tokens, usage.cache_read_input_tokens or 0, usage.output_tokens),
        )
        return extraction


def run_eval(extractor: Extractor, labels_dir: Path = evalset.LABELS_DIR) -> dict[str, Any]:
    """Extract every eval posting via the API and score it against the labels."""
    for name, _, job in eval_jobs(extractor.conn, labels_dir):
        extractor.extract(job)
        log.info("extracted %s", name)
    return score_stored(extractor.conn, extractor.model, extractor.version, labels_dir)


def main() -> None:
    from dotenv import load_dotenv

    from job_search_assistant import db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m job_search_assistant.extract")
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("eval", help="extract the eval postings via the Claude API and score them")
    e.add_argument("--model", default=DEFAULT_MODEL)
    e.add_argument("--effort", default=DEFAULT_EFFORT)
    sc = sub.add_parser("score", help="score stored extractions (e.g. from the MCP server)")
    sc.add_argument("--model", required=True, help="as stored, e.g. claude-code/claude-opus-5-5")
    args = parser.parse_args()

    conn = db.connect("data/jobs.db")
    if args.command == "score":
        version = prompt_version(system_prompt(GUIDELINES.read_text()))
        report = score_stored(conn, args.model, version)
        out = save_report(report, args.model, "stored", version)
    else:
        import anthropic

        extractor = Extractor(
            anthropic.Anthropic(),
            conn,
            GUIDELINES.read_text(),
            model=args.model,
            effort=args.effort,
        )
        report = run_eval(extractor)
        out = save_report(report, args.model, args.effort, extractor.version)
    print(evalset.format_report(report))
    if report["missing"]:
        print(f"\nnot extracted yet ({len(report['missing'])}): {', '.join(report['missing'])}")
    print(f"\nfull report: {out}")


if __name__ == "__main__":
    main()
