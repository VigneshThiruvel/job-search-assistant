"""Phase 3: semantic search over postings, optionally filtered by Phase 2 extractions.

    python -m job_search_assistant.search --resume           # profile/resume.md is the query
    python -m job_search_assistant.search "Kafka streaming pipelines" [--all]

Filters decide what is eligible (from the extractions), vectors decide the
order. Similarity is brute-force cosine in SQL (sqlite-vec): at a few thousand
vectors that takes milliseconds, so no approximate index is needed.
"""

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from job_search_assistant import embed, evalset, extract

EXTRACTION_MODEL = "claude-code/claude-opus-5-5"
RESUME = Path("profile/resume.md")
GERMAN_LEVELS = evalset.ENUMS["german_level"]  # ordered: none < plus < basic < ...


@dataclass(frozen=True)
class Filters:
    """Constraints checked against a posting's extraction. Unextracted postings fail them."""

    target_only: bool = False
    seniority: tuple[str, ...] | None = None
    max_german: str | None = None  # highest acceptable german_level requirement
    other_languages_optional: bool = False  # Dutch, French, ... at most "plus"

    def active(self) -> bool:
        return (
            self.target_only
            or self.seniority is not None
            or self.max_german is not None
            or self.other_languages_optional
        )


# My search scope (CLAUDE.md): target roles, entry level (mid as a stretch), basic German at
# most, no other language required (I speak English, basic German, Tamil).
MY_FILTERS = Filters(
    target_only=True,
    seniority=("junior", "mid", "unknown"),
    max_german="basic",
    other_languages_optional=True,
)


@dataclass(frozen=True)
class Hit:
    job_id: int
    title: str
    company: str
    location: str | None
    url: str
    similarity: float


def search(
    conn: sqlite3.Connection,
    query_vector,
    *,
    model: str = embed.DEFAULT_MODEL,
    k: int = 20,
    filters: Filters | None = None,
    extraction: tuple[str, str] | None = None,  # (model, prompt_version) the filters read
) -> list[Hit]:
    """Top-k postings by cosine similarity, best posting per duplicate group."""
    filters = filters or Filters()
    params: dict = {"q": embed.to_blob(query_vector), "model": model, "k": k}
    join, where = "", []
    if filters.active():
        if extraction is None:
            raise ValueError("filters need an extraction (model, prompt_version)")
        join = """JOIN extractions x ON x.job_id = j.id
                  AND x.model = :xmodel AND x.prompt_version = :xversion"""
        params["xmodel"], params["xversion"] = extraction
        if filters.target_only:
            where.append("json_extract(x.output_json, '$.is_target_role') = 1")
        if filters.seniority is not None:
            names = [f":sen{i}" for i in range(len(filters.seniority))]
            where.append(f"json_extract(x.output_json, '$.seniority') IN ({', '.join(names)})")
            params.update(zip((n[1:] for n in names), filters.seniority, strict=True))
        if filters.max_german is not None:
            allowed = GERMAN_LEVELS[: GERMAN_LEVELS.index(filters.max_german) + 1]
            names = [f":ger{i}" for i in range(len(allowed))]
            where.append(f"json_extract(x.output_json, '$.german_level') IN ({', '.join(names)})")
            params.update(zip((n[1:] for n in names), allowed, strict=True))
        if filters.other_languages_optional:
            where.append(
                """NOT EXISTS (SELECT 1 FROM json_each(x.output_json, '$.other_languages') o
                               WHERE json_extract(o.value, '$.level') != 'plus')"""
            )

    rows = conn.execute(
        f"""WITH hits AS (
                SELECT j.id, j.title, j.company, j.location, j.url,
                       vec_distance_cosine(e.vector, :q) AS distance,
                       COALESCE(g.group_id, j.id) AS grp
                FROM embeddings e
                JOIN jobs j ON j.id = e.job_id
                LEFT JOIN job_groups g ON g.job_id = j.id
                {join}
                WHERE e.model = :model {"".join(f" AND {w}" for w in where)}
            ), best AS (
                SELECT *, ROW_NUMBER() OVER (PARTITION BY grp ORDER BY distance) AS rn FROM hits
            )
            SELECT * FROM best WHERE rn = 1 ORDER BY distance LIMIT :k""",
        params,
    ).fetchall()
    return [
        Hit(r["id"], r["title"], r["company"], r["location"], r["url"], 1 - r["distance"])
        for r in rows
    ]


def current_extraction() -> tuple[str, str]:
    system = extract.system_prompt(extract.GUIDELINES.read_text())
    return EXTRACTION_MODEL, extract.prompt_version(system)


def main() -> None:
    from job_search_assistant import db

    parser = argparse.ArgumentParser(prog="python -m job_search_assistant.search")
    parser.add_argument("query", nargs="?", help="free-text query (or use --resume)")
    parser.add_argument("--resume", action="store_true", help=f"use {RESUME} as the query")
    parser.add_argument("--all", action="store_true", help="no filters (all embedded postings)")
    parser.add_argument("-k", type=int, default=20)
    args = parser.parse_args()
    if bool(args.query) == args.resume:
        parser.error("give a query or --resume (not both)")

    text = RESUME.read_text() if args.resume else args.query
    vector = embed.load_encoder()([text])[0]
    hits = search(
        db.connect("data/jobs.db"),
        vector,
        k=args.k,
        filters=Filters() if args.all else MY_FILTERS,
        extraction=current_extraction(),
    )
    for rank, h in enumerate(hits, 1):
        print(f"{rank:>3}  {h.similarity:.3f}  [{h.job_id}] {h.title} — {h.company}, {h.location}")


if __name__ == "__main__":
    main()
