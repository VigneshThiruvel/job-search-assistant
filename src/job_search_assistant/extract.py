"""Phase 2: LLM extraction of structured requirements from job postings.

    python -m job_search_assistant.extract eval [--model M] [--effort E]

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
from typing import Any, Literal

import anthropic
from pydantic import BaseModel, ConfigDict, Field

from job_search_assistant import evalset

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


class Extractor:
    def __init__(
        self,
        client: anthropic.Anthropic,
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
        row = self.conn.execute(
            """SELECT output_json FROM extractions
               WHERE job_id = ? AND model = ? AND prompt_version = ?""",
            (job["id"], self.model, self.version),
        ).fetchone()
        if row:
            return Extraction.model_validate_json(row[0])

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
        with self.conn:
            self.conn.execute(
                """INSERT INTO extractions (job_id, model, served_model, prompt_version, effort,
                   extracted_at, output_json, input_tokens, cache_read_tokens, output_tokens)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    job["id"],
                    self.model,
                    response.model,  # the fallback model, if one served the request
                    self.version,
                    self.effort,
                    datetime.now(UTC).isoformat(),
                    extraction.model_dump_json(),
                    usage.input_tokens,
                    usage.cache_read_input_tokens or 0,
                    usage.output_tokens,
                ),
            )
        return extraction


def run_eval(extractor: Extractor, labels_dir: Path = evalset.LABELS_DIR) -> dict[str, Any]:
    """Extract every eval posting and score it against the human-reviewed labels."""
    import yaml

    pairs = []
    for path in sorted(labels_dir.glob("*.yaml")):
        doc = yaml.safe_load(path.read_text())
        job = doc["job"]
        row = extractor.conn.execute(
            "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
            (job["source"], job["source_job_id"]),
        ).fetchone()
        if row is None:
            log.warning("%s: posting no longer in the database, skipped", path.name)
            continue
        predicted = extractor.extract(dict(row)).to_labels()
        pairs.append((path.name, doc.get("labelled_by", "unknown"), doc["labels"], predicted))
        log.info("extracted %s", path.name)
    return evalset.score(pairs)


def main() -> None:
    from dotenv import load_dotenv

    from job_search_assistant import db

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_dotenv()
    parser = argparse.ArgumentParser(prog="python -m job_search_assistant.extract")
    sub = parser.add_subparsers(dest="command", required=True)
    e = sub.add_parser("eval", help="extract the eval postings and score them")
    e.add_argument("--model", default=DEFAULT_MODEL)
    e.add_argument("--effort", default=DEFAULT_EFFORT)
    args = parser.parse_args()

    extractor = Extractor(
        anthropic.Anthropic(),
        db.connect("data/jobs.db"),
        GUIDELINES.read_text(),
        model=args.model,
        effort=args.effort,
    )
    report = run_eval(extractor)
    out = (
        Path("eval/results")
        / f"{datetime.now(UTC):%Y-%m-%d}-{args.model}-{args.effort}-{extractor.version}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(evalset.format_report(report))
    print(f"\nfull report: {out}")


if __name__ == "__main__":
    main()
