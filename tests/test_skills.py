from datetime import UTC, datetime

import pytest
from test_extract import LABELS

from job_search_assistant import db, extract, skills
from job_search_assistant.models import Job
from job_search_assistant.skills import SkillMapping

RESUME = """## Summary

Data person.

## Technical Skills

- Programming: Python, SQL
- Data Engineering: Data Modeling, Apache Kafka
- Languages: English (Fluent), German (Basic)

## Experience
"""


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    job = Job(
        source="arbeitnow", source_job_id="j1", url="https://example.com/1", title="Data Engineer",
        company="ACME", location="Berlin", country=None, remote=None, description="d",
        posted_at=datetime(2026, 10, 1, tzinfo=UTC), raw={},
    )  # fmt: skip
    db.upsert_job(conn, job, lang="en", content_hash="h", seen_at=datetime.now(UTC))
    extraction = extract.Extraction.model_validate(
        {
            **LABELS,
            "required_skills": ["Datenmodellierung", "PySpark", "Pyspark ", "AWS Glue"],
            "nice_to_have_skills": ["data  modeling", "communication"],
        }
    )
    extract.store_extraction(conn, 1, extraction, model="m", version="v")
    yield conn
    conn.close()


def m(raw, skill, category=None, parent=None) -> SkillMapping:
    return SkillMapping(raw=raw, skill=skill, category=category, parent=parent)


def test_resume_skills_skip_human_languages() -> None:
    assert skills.resume_skills(RESUME) == ["Python", "SQL", "Data Modeling", "Apache Kafka"]


def test_raw_skills_merge_case_and_whitespace(conn) -> None:
    raw = skills.raw_skills(conn, ("m", "v"), RESUME)
    assert raw["pyspark"]["mentions"] == 2
    assert raw["data modeling"] == {"display": "data  modeling", "mentions": 1, "in_resume": True}
    assert raw["python"] == {"display": "Python", "mentions": 0, "in_resume": True}


def test_save_creates_skills_parents_and_rejects_conflicts(conn) -> None:
    known = set(skills.raw_skills(conn, ("m", "v"), RESUME))
    batch = [
        m("Datenmodellierung", "Data Modeling", "data_engineering"),
        m("data modeling", "Data Modeling"),  # reuses the skill created in this batch
        m("pyspark", "PySpark", "data_engineering", parent="Apache Spark"),
        m("aws glue", "AWS Glue", "cloud", parent="AWS"),
        m("communication", None),  # dropped
    ]
    assert skills.save_mappings(conn, batch, known) == []
    tax = skills.canonical_skills(conn)
    assert tax["Apache Spark"] == {"category": "data_engineering", "parent": None}
    assert tax["AWS"]["category"] == "cloud"
    assert skills.unmapped(conn, skills.raw_skills(conn, ("m", "v"), RESUME)) == [
        "apache kafka", "python", "sql",
    ]  # fmt: skip

    errors = skills.save_mappings(
        conn,
        [
            m("pyspark", "PySpark"),  # already mapped
            m("sql", "SQL"),  # new without category
            m("python", "Data Modeling", "programming"),  # category conflict
            m("unknown thing", "X", "methods"),  # not a raw skill
        ],
        known,
    )
    assert len(errors) == 4
    assert conn.execute("SELECT COUNT(*) FROM skill_map").fetchone()[0] == 5  # nothing added


def test_parent_cycles_are_rejected(conn) -> None:
    known = set(skills.raw_skills(conn, ("m", "v"), RESUME))
    assert skills.save_mappings(conn, [m("pyspark", "PySpark", "data_engineering", "Spark")],
                                known) == []  # fmt: skip
    errors = skills.save_mappings(conn, [m("sql", "Spark", "data_engineering", "PySpark")], known)
    assert any("loops" in e for e in errors)


def test_yaml_round_trip_and_review_edits(conn, tmp_path) -> None:
    raw = skills.raw_skills(conn, ("m", "v"), RESUME)
    skills.save_mappings(
        conn,
        [m("datenmodellierung", "Data Modeling", "data_engineering"), m("communication", None)],
        set(raw),
    )
    path = tmp_path / "skills.yaml"
    assert skills.export_yaml(conn, raw, path) == 1

    doc = path.read_text().replace("category: data_engineering", "category: database")
    path.write_text(doc)  # my review moves the skill to another category
    assert skills.import_yaml(conn, path) == []
    assert skills.canonical_skills(conn)["Data Modeling"]["category"] == "database"
    assert dict(conn.execute("SELECT raw, skill FROM skill_map").fetchall()) == {
        "datenmodellierung": "Data Modeling", "communication": None,
    }  # fmt: skip

    path.write_text(doc.replace("category: database", "category: nonsense"))
    assert skills.import_yaml(conn, path) != []


def test_import_refuses_to_drop_mappings_made_after_export(conn, tmp_path) -> None:
    raw = skills.raw_skills(conn, ("m", "v"), RESUME)
    skills.save_mappings(conn, [m("communication", None)], set(raw))
    path = tmp_path / "skills.yaml"
    skills.export_yaml(conn, raw, path)
    skills.save_mappings(conn, [m("datenmodellierung", "Data Modeling", "data_engineering")],
                         set(raw))  # fmt: skip  # e.g. the daily agent
    errors = skills.import_yaml(conn, path)
    assert len(errors) == 1 and "datenmodellierung" in errors[0]
    assert skills.import_yaml(conn, path, force=True) == []
    assert dict(conn.execute("SELECT raw, skill FROM skill_map")) == {"communication": None}


def test_extra_skills_need_existing_bullets() -> None:
    text = """
- skill: Backend Development
  evidence: [E1.1, E2.2]
- skill: AI Evaluation
  evidence: P2.2
"""
    assert skills.extra_skills(text, {"E1.1", "E2.2", "P2.2"}) == [
        "Backend Development",
        "AI Evaluation",
    ]
    with pytest.raises(ValueError, match="no résumé bullet P2.2"):
        skills.extra_skills(text, {"E1.1", "E2.2"})  # bullet removed from the résumé
    with pytest.raises(ValueError, match="listed twice"):
        skills.extra_skills(
            text + "- skill: ai evaluation\n  evidence: P2.2\n", {"E1.1", "E2.2", "P2.2"}
        )
    with pytest.raises(ValueError, match="needs `skill` and `evidence`"):
        skills.extra_skills("- skill: APIs\n", {"E1.1"})


def test_extra_skills_count_as_mine(conn) -> None:
    raw = skills.raw_skills(conn, ("m", "v"), RESUME, extra=["Backend Development"])
    assert raw["backend development"] == {
        "display": "Backend Development", "mentions": 0, "in_resume": True
    }  # fmt: skip
