"""Graph projection (pure, always runs) and Cypher queries (opt-in: NEO4J_TEST=1).

The Cypher test wipes the Neo4j database it connects to; rebuild the real graph
afterwards with `python -m job_search_assistant.graph build`.
"""

import os
from datetime import UTC, datetime

import pytest
from test_extract import LABELS

from job_search_assistant import db, extract, graph, skills
from job_search_assistant.models import Job
from job_search_assistant.skills import SkillMapping

RESUME = """## Technical Skills

- Programming: Python
- Data Engineering: PySpark
- Languages: English (Fluent)
"""
JOBS = [  # title, company, required, nice, seniority
    ("Junior Data Engineer", "ACME GmbH", ["Apache Spark", "SQL"], ["pandas"], "junior"),
    ("Data Engineer", "ACME", ["Apache Spark", "Airflow"], [], "junior"),
    ("Senior Data Engineer", "Other AG", ["SQL", "Airflow", "pandas"], ["SQL"], "senior"),
]
TAXONOMY = [  # raw, skill, category, parent
    ("python", "Python", "programming", None),
    ("pyspark", "PySpark", "data_engineering", "Apache Spark"),
    ("apache spark", "Apache Spark", "data_engineering", None),
    ("sql", "SQL", "programming", None),
    ("pandas", "pandas", "programming", "Python"),
    ("airflow", "Airflow", "data_engineering", None),
]
EXTRACTION = ("claude-code/claude-opus-5-5", "v1")


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    for i, (title, company, required, nice, seniority) in enumerate(JOBS, 1):
        job = Job(
            source="arbeitnow", source_job_id=f"j{i}", url=f"https://example.com/{i}",
            title=title, company=company, location="Berlin", country=None, remote=None,
            description="d", posted_at=datetime(2026, 10, 1, tzinfo=UTC), raw={},
        )  # fmt: skip
        db.upsert_job(conn, job, lang="en", content_hash=f"h{i}", seen_at=datetime.now(UTC))
        x = extract.Extraction.model_validate(
            {**LABELS, "required_skills": required, "nice_to_have_skills": nice,
             "seniority": seniority, "other_languages": [], "years_experience_min": None}
        )  # fmt: skip
        extract.store_extraction(conn, i, x, model=EXTRACTION[0], version=EXTRACTION[1])
    raw = skills.raw_skills(conn, EXTRACTION, RESUME)
    mappings = [SkillMapping(raw=r, skill=s, category=c, parent=p) for r, s, c, p in TAXONOMY]
    assert skills.save_mappings(conn, mappings, set(raw)) == []
    yield conn
    conn.close()


def test_rows_merge_companies_dedupe_skills_and_mark_eligible(conn) -> None:
    rows = graph.graph_rows(conn, EXTRACTION, RESUME)
    assert rows["my_skills"] == ["PySpark", "Python"]
    assert len(rows["companies"]) == 2  # "ACME GmbH" and "ACME" are one company
    eligible = {j["id"]: j["props"]["eligible"] for j in rows["jobs"]}
    assert eligible == {1: True, 2: True, 3: False}  # 3 is senior
    edges = {(e["job_id"], e["skill"]): e["required"] for e in rows["requires"]}
    assert edges[(3, "SQL")] is True  # listed as required and nice: required wins
    assert edges[(1, "pandas")] is False
    assert len(edges) == 8


@pytest.mark.skipif(os.environ.get("NEO4J_TEST") != "1", reason="needs Neo4j (NEO4J_TEST=1)")
def test_cypher_queries_on_neo4j(conn) -> None:
    with graph.connect_neo4j() as driver:
        counts = graph.build(driver, graph.graph_rows(conn, EXTRACTION, RESUME))
        assert counts["Job"] == 3 and counts["Me"] == 1

        gaps = {g["skill"]: g for g in graph.query(driver, "gaps", "target")}
        assert "Apache Spark" not in gaps  # PySpark (narrower) gives full credit
        assert gaps["pandas"]["my_credit"] == 0.5  # Python (broader) gives half
        assert gaps["Airflow"]["required_by"] == 2
        assert gaps["SQL"]["score"] == 2.0

        unlock = graph.query(driver, "unlock", "eligible")
        assert {u["skill"]: u["unlocks"] for u in unlock} == {"SQL": 1, "Airflow": 1}

        coverage = graph.query(driver, "coverage", "eligible")
        assert [c["coverage"] for c in coverage] == [0.5, 0.5]

        together = graph.query(driver, "together", "target", skill="Airflow")
        assert {t["skill"] for t in together} == {"Apache Spark", "SQL", "pandas"}

        roles = graph.query(driver, "roles", "target", k=1)
        assert roles[0]["top_skills"] in (["Airflow (2)"], ["Apache Spark (2)"], ["SQL (2)"])
