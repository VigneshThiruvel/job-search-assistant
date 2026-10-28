from datetime import UTC, datetime

import yaml

from job_search_assistant import db, dedup, evalset
from job_search_assistant.models import Job

VALID = {
    "is_target_role": True,
    "role_family": "data_engineer",
    "seniority": "working_student",
    "employment_type": "part_time",
    "german_level": "plus",
    "german_implied": False,
    "english_level": "fluent",
    "other_languages": ["Dutch: fluent"],
    "workplace": "hybrid",
    "years_experience_min": None,
    "degree": "bachelor",
    "required_skills": ["Python", "SQL"],
    "nice_to_have_skills": [],
    "restrictions": ["enrolled student"],
    "salary": None,
    "notes": None,
}


def test_validate_accepts_complete_labels() -> None:
    assert evalset.validate(VALID) == []


def test_validate_reports_problems() -> None:
    bad = {
        **VALID,
        "seniority": "medior",
        "german_implied": None,
        "typo_field": 1,
        "other_languages": ["dutch fluent"],
    }
    problems = " ".join(evalset.validate(bad))
    assert "other_languages" in problems
    assert "seniority" in problems
    assert "german_implied" in problems
    assert "typo_field" in problems


def add_job(conn, i: int, title: str, description: str, lang: str = "en") -> None:
    job = Job(
        source="arbeitnow",
        source_job_id=f"job-{i}",
        url=f"https://example.com/{i}",
        title=title,
        company=f"Company {i}",
        location="Berlin",
        country=None,
        remote=None,
        description=description,
        posted_at=datetime(2026, 10, 1, tzinfo=UTC),
        raw={},
    )
    db.upsert_job(conn, job, lang=lang, content_hash=str(i), seen_at=datetime.now(UTC))


def test_sample_writes_parseable_unlabelled_files(tmp_path) -> None:
    conn = db.connect(":memory:")
    description = "Line one: tasks\n\n- SQL: yes\n- # not a comment\n" + "x" * 400
    add_job(conn, 1, "Junior Data Analyst", description)
    add_job(conn, 2, "Senior Data Engineer", description)
    add_job(conn, 3, "Head of Brand", description)
    add_job(conn, 4, "Short Data Analyst", "too short")  # excluded: no real description
    dedup.rebuild_groups(conn)

    assert evalset.write_sample(conn, tmp_path) == 3
    files = sorted(tmp_path.glob("*.yaml"))
    doc = yaml.safe_load(files[0].read_text())
    assert doc["description"] == description + "\n"  # YAML block keeps text intact
    assert doc["job"]["source"] == "arbeitnow"
    assert evalset.validate(doc["labels"])  # empty template is not yet valid
    assert evalset.check(tmp_path) == 1
    assert doc["reviewed"] is False


def test_check_counts_only_reviewed_files(tmp_path, capsys) -> None:
    labels = yaml.safe_dump({"labels": VALID})
    (tmp_path / "01.yaml").write_text("reviewed: true\n" + labels)
    (tmp_path / "02.yaml").write_text("reviewed: false\n" + labels)
    assert evalset.check(tmp_path) == 1
    assert "1/2 reviewed, 1 drafted" in capsys.readouterr().out


def test_sample_refuses_to_overwrite_labels(tmp_path) -> None:
    (tmp_path / "01-existing.yaml").write_text("labels: {}\n")
    conn = db.connect(":memory:")
    try:
        evalset.write_sample(conn, tmp_path)
    except SystemExit as e:
        assert "refusing" in str(e)
    else:
        raise AssertionError("expected SystemExit")


def test_target_titles_include_ai_developers() -> None:
    from job_search_assistant.evalset import TARGET_TITLE

    for title in ("Junior KI-Entwickler / AI Developer (m/w/d)", "AI Engineer", "KI Engineer"):
        assert TARGET_TITLE.search(title), title
    assert not TARGET_TITLE.search("Junior Controller (m/w/d)")
