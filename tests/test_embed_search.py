from datetime import UTC, datetime

import numpy as np
import pytest
from test_extract import LABELS

from job_search_assistant import db, embed, extract
from job_search_assistant.models import Job
from job_search_assistant.search import Filters, search

VOCAB = ["kafka", "streaming", "dashboards", "power", "llm", "agents", "sales"]


def fake_encode(texts):
    """Bag of words over a tiny vocabulary: similar texts get similar vectors."""
    calls.append(list(texts))
    out = []
    for text in texts:
        v = np.array([text.lower().count(w) for w in VOCAB], dtype=np.float32) + 0.01
        out.append(v / np.linalg.norm(v))
    return np.array(out)


calls: list[list[str]] = []
POSTINGS = [  # (title, description)
    ("Data Engineer", "Kafka streaming pipelines, Kafka Connect, streaming at scale"),
    ("Data Engineer", "Kafka streaming pipelines, Kafka Connect, streaming at scale"),  # dup
    ("BI Analyst", "Power BI dashboards and dashboards for sales"),
    ("AI Engineer", "LLM agents, LLM evaluation"),
    ("Newsletter", ""),  # no description: never embedded
]


@pytest.fixture
def conn():
    calls.clear()
    conn = db.connect(":memory:")
    for i, (title, description) in enumerate(POSTINGS, 1):
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
        db.upsert_job(conn, job, lang="en", content_hash=f"h{i}", seen_at=datetime.now(UTC))
    # Jobs 1 and 2 are cross-source duplicates.
    conn.executemany("INSERT INTO job_groups VALUES (?, ?)", [(1, 1), (2, 1), (3, 3), (4, 4)])
    conn.commit()
    yield conn
    conn.close()


def test_embeds_postings_with_descriptions_once(conn) -> None:
    assert embed.embed_jobs(conn, fake_encode, "fake") == 4
    assert embed.embed_jobs(conn, fake_encode, "fake") == 0  # cache hit, encoder not called
    assert len(calls) == 1


def test_edited_posting_is_re_embedded(conn) -> None:
    embed.embed_jobs(conn, fake_encode, "fake")
    conn.execute("UPDATE jobs SET description = 'now about agents' WHERE id = 3")
    assert embed.embed_jobs(conn, fake_encode, "fake") == 1
    assert "agents" in calls[-1][0]


def test_duty_cycle_rests_between_work_windows(conn) -> None:
    now = [0.0]
    rests: list[float] = []

    def clock() -> float:
        now[0] += 100  # every batch "takes" 100 s
        return now[0]

    n = embed.embed_jobs(
        conn, fake_encode, "fake", batch_size=1, duty_cycle=(250, 600),
        clock=clock, sleep=rests.append,
    )  # fmt: skip
    assert n == 4
    assert rests == [600]  # one rest after ~300 s of work; none after the last batch


def test_search_ranks_by_similarity_one_hit_per_duplicate_group(conn) -> None:
    embed.embed_jobs(conn, fake_encode, "fake")
    hits = search(conn, fake_encode(["kafka streaming"])[0], model="fake", k=10)
    assert hits[0].job_id == 1
    assert {h.job_id for h in hits} == {1, 3, 4}  # 2 is a duplicate of 1, 5 has no text
    assert hits[0].similarity > 0.9
    assert hits == sorted(hits, key=lambda h: -h.similarity)


def test_location_filter_drops_out_of_scope_places(conn) -> None:
    embed.embed_jobs(conn, fake_encode, "fake")
    conn.execute("UPDATE jobs SET location = 'Sydney, Australia' WHERE id = 3")
    conn.execute("UPDATE jobs SET location = NULL WHERE id = 4")  # unknown is kept
    hits = search(conn, fake_encode(["sales"])[0], model="fake",
                  filters=Filters(in_scope_location=True))  # fmt: skip
    assert {h.job_id for h in hits} == {1, 4}


def test_filters_use_extractions(conn) -> None:
    embed.embed_jobs(conn, fake_encode, "fake")
    analyst = extract.Extraction.model_validate({**LABELS, "german_level": "plus"})
    engineer = extract.Extraction.model_validate(
        {**LABELS, "seniority": "senior", "german_level": "none", "other_languages": [],
         "years_experience_min": 5}
    )  # fmt: skip
    extract.store_extraction(conn, 3, analyst, model="x", version="v")
    extract.store_extraction(conn, 4, engineer, model="x", version="v")
    query = fake_encode(["llm agents"])[0]

    junior = search(conn, query, model="fake", filters=Filters(seniority=("junior",)),
                    extraction=("x", "v"))  # fmt: skip
    assert [h.job_id for h in junior] == [3]  # 4 is senior; 1 has no extraction

    no_german = search(conn, query, model="fake", filters=Filters(max_german="none"),
                       extraction=("x", "v"))  # fmt: skip
    assert [h.job_id for h in no_german] == [4]

    # LABELS requires Dutch: fluent; the senior posting needs no other language.
    optional = search(conn, query, model="fake", filters=Filters(other_languages_optional=True),
                      extraction=("x", "v"))  # fmt: skip
    assert [h.job_id for h in optional] == [4]

    # LABELS asks for 1 year; unstated years are kept.
    few_years = search(conn, query, model="fake", filters=Filters(max_years=2),
                       extraction=("x", "v"))  # fmt: skip
    assert [h.job_id for h in few_years] == [3]
    conn.execute("""UPDATE extractions SET output_json =
                    json_set(output_json, '$.years_experience_min', NULL) WHERE job_id = 4""")
    assert {h.job_id for h in search(conn, query, model="fake", filters=Filters(max_years=2),
                                     extraction=("x", "v"))} == {3, 4}  # fmt: skip

    with pytest.raises(ValueError):
        search(conn, query, model="fake", filters=Filters(target_only=True))
