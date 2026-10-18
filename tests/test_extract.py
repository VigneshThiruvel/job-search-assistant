from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from job_search_assistant import db, evalset
from job_search_assistant.extract import Extraction, Extractor, system_prompt
from job_search_assistant.models import Job

LABELS = {
    "is_target_role": True,
    "role_family": "data_analyst",
    "seniority": "junior",
    "employment_type": "full_time",
    "german_level": "plus",
    "german_implied": False,
    "english_level": "fluent",
    "other_languages": [{"language": "Dutch", "level": "fluent"}],
    "workplace": "hybrid",
    "years_experience_min": 1,
    "degree": "bachelor",
    "required_skills": ["SQL", "Python"],
    "nice_to_have_skills": ["dbt"],
    "restrictions": [],
    "salary": None,
}


class FakeMessages:
    def __init__(self, extraction: Extraction, stop_reason: str = "end_turn") -> None:
        self.extraction = extraction
        self.stop_reason = stop_reason
        self.calls: list[dict] = []

    def parse(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            parsed_output=self.extraction if self.stop_reason == "end_turn" else None,
            stop_reason=self.stop_reason,
            stop_details=None,
            model=kwargs["model"],
            usage=SimpleNamespace(
                input_tokens=1200, cache_read_input_tokens=3000, output_tokens=400
            ),
        )


def fake_client(messages: FakeMessages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


@pytest.fixture
def conn():
    conn = db.connect(":memory:")
    job = Job(
        source="arbeitnow",
        source_job_id="junior-data-analyst-1",
        url="https://example.com/1",
        title="Junior Data Analyst",
        company="ACME",
        location="Berlin",
        country=None,
        remote=None,
        description="We need SQL and Python. German is a plus.",
        posted_at=datetime(2026, 10, 1, tzinfo=UTC),
        raw={},
    )
    db.upsert_job(conn, job, lang="en", content_hash="h", seen_at=datetime.now(UTC))
    yield conn
    conn.close()


def job_row(conn) -> dict:
    return dict(conn.execute("SELECT * FROM jobs").fetchone())


def test_schema_enums_come_from_the_eval_set() -> None:
    schema = Extraction.model_json_schema()
    assert schema["properties"]["seniority"]["enum"] == list(evalset.ENUMS["seniority"])
    assert schema["additionalProperties"] is False


def test_to_labels_matches_label_file_shape() -> None:
    labels = Extraction.model_validate(LABELS).to_labels()
    assert labels["other_languages"] == ["Dutch: fluent"]
    assert evalset.validate({**labels, "notes": None}) == []


def test_extract_calls_api_once_then_uses_cache(conn) -> None:
    messages = FakeMessages(Extraction.model_validate(LABELS))
    extractor = Extractor(fake_client(messages), conn, "GUIDELINES TEXT")

    first = extractor.extract(job_row(conn))
    second = extractor.extract(job_row(conn))

    assert first == second
    assert len(messages.calls) == 1
    call = messages.calls[0]
    assert call["output_format"] is Extraction
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "GUIDELINES TEXT" in call["system"][0]["text"]
    assert "Junior Data Analyst" in call["messages"][0]["content"]
    row = conn.execute("SELECT model, served_model, output_tokens FROM extractions").fetchone()
    assert tuple(row) == ("claude-opus-5-5", "claude-opus-5-5", 400)


def test_new_prompt_version_does_not_reuse_old_results(conn) -> None:
    messages = FakeMessages(Extraction.model_validate(LABELS))
    Extractor(fake_client(messages), conn, "v1").extract(job_row(conn))
    Extractor(fake_client(messages), conn, "v2").extract(job_row(conn))
    assert len(messages.calls) == 2


def test_refusal_and_truncation_raise_and_store_nothing(conn) -> None:
    for reason in ("refusal", "max_tokens"):
        messages = FakeMessages(Extraction.model_validate(LABELS), stop_reason=reason)
        with pytest.raises(RuntimeError):
            Extractor(fake_client(messages), conn, "g").extract(job_row(conn))
    assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 0


def test_system_prompt_wraps_guidelines() -> None:
    assert "<guidelines>\nRULES\n</guidelines>" in system_prompt("RULES")


def test_score_exact_fields_and_skills() -> None:
    gold = {**Extraction.model_validate(LABELS).to_labels(), "notes": None}
    pred = {**gold, "seniority": "mid", "required_skills": ["sql", "Spark"], "salary": "50k"}
    report = evalset.score(
        [("01.yaml", "human", gold, pred), ("02.yaml", "claude-draft", gold, gold)]
    )

    fields = report["groups"]["all"]["fields"]
    assert fields["seniority"] == 0.5
    assert fields["role_family"] == 1.0
    assert fields["salary_present"] == 0.5
    # required: gold {sql, python} x2; pred {sql, spark} + {sql, python} -> tp 3, fp 1, fn 1
    assert fields["required_skills"] == {"precision": 0.75, "recall": 0.75}
    assert report["groups"]["human"]["fields"]["seniority"] == 0.0
    assert report["groups"]["claude-draft"]["fields"]["seniority"] == 1.0
    assert report["mismatches"]["seniority"] == [
        {"file": "01.yaml", "label": "junior", "predicted": "mid"}
    ]
    assert "seniority" in evalset.format_report(report)
