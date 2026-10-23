from datetime import UTC, datetime

import numpy as np
import pytest
import yaml
from mcp import Client
from test_extract import LABELS

from job_search_assistant import db, embed, extract
from job_search_assistant.mcp_server import build_server
from job_search_assistant.models import Job

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server(tmp_path):
    path = tmp_path / "jobs.db"
    conn = db.connect(path)
    for i in (1, 2):
        job = Job(
            source="arbeitnow",
            source_job_id=f"job-{i}",
            url=f"https://example.com/{i}",
            title=f"Junior Data Analyst {i}",
            company="ACME",
            location="Berlin",
            country=None,
            remote=None,
            description="We need SQL and Python. German is a plus.",
            posted_at=datetime(2026, 10, 1, tzinfo=UTC),
            raw={},
        )
        db.upsert_job(conn, job, lang="en", content_hash=f"h{i}", seen_at=datetime.now(UTC))
    conn.commit()
    conn.close()

    labels = tmp_path / "labels"
    labels.mkdir()
    doc = {
        "reviewed": True,
        "labelled_by": "human",
        "labels": {**extract.Extraction.model_validate(LABELS).to_labels(), "notes": None},
        "job": {"source": "arbeitnow", "source_job_id": "job-1"},
    }
    (labels / "01-arbeitnow-job-1.yaml").write_text(yaml.safe_dump(doc))
    resume = tmp_path / "resume.md"
    resume.write_text("SQL and Python, junior data analyst")
    return build_server(
        lambda: db.connect(path), "RULES", labels, resume=resume, load_encoder=lambda: fake_encode
    ), path


def fake_encode(texts):
    """'1' and '2' in a text point it at job 1 or 2; everything else sits in between."""
    out = []
    for text in texts:
        v = np.array([1.0 + 5 * text.count("1"), 1.0 + 5 * text.count("2")], dtype=np.float32)
        out.append(v / np.linalg.norm(v))
    return np.array(out)


def version() -> str:
    return extract.prompt_version(extract.system_prompt("RULES"))


async def call(client, name, **arguments):
    result = await client.call_tool(name, arguments)
    return result, result.content[0].text


async def test_lists_tools_with_extraction_schema(server) -> None:
    async with Client(server[0]) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    assert set(tools) == {
        "get_extraction_guidelines",
        "list_jobs_to_extract",
        "get_job",
        "save_extraction",
        "get_profile",
        "match_resume",
        "search_jobs",
        "get_skill_guidelines",
        "list_canonical_skills",
        "list_unmapped_skills",
        "save_skill_mappings",
        "skill_gaps",
        "list_new_jobs",
        "get_job_details",
        "job_fit",
    }
    # The client sees the Pydantic schema, enums included, and is held to it.
    schema = tools["save_extraction"].input_schema
    assert "extraction" in schema["required"]
    # ... and the shape of typed results.
    assert "JobSummary" in tools["match_resume"].output_schema["$defs"]
    assert tools["skill_gaps"].annotations.read_only_hint is True
    save = tools["save_extraction"].annotations
    assert save.read_only_hint is False and save.destructive_hint is False


async def test_documents_are_resources_too(server) -> None:
    async with Client(server[0]) as client:
        uris = {str(r.uri) for r in (await client.list_resources()).resources}
        templates = (await client.list_resource_templates()).resource_templates
        profile = await client.read_resource("profile://resume")
        job = await client.read_resource("job://2")
    assert uris == {"profile://resume", "guidelines://extraction", "guidelines://skills"}
    assert [t.uri_template for t in templates] == ["job://{job_id}"]
    assert "junior data analyst" in profile.contents[0].text
    assert "<title>Junior Data Analyst 2</title>" in job.contents[0].text


async def test_eval_workflow_round_trip(server) -> None:
    mcp_server, path = server
    async with Client(mcp_server) as client:
        _, guidelines = await call(client, "get_extraction_guidelines")
        assert f"prompt_version: {version()}" in guidelines
        assert "<guidelines>\nRULES\n</guidelines>" in guidelines

        result = await client.call_tool("list_jobs_to_extract", {"model": "claude-opus-5-5"})
        todo = result.structured_content["result"]
        assert [j["title"] for j in todo] == ["Junior Data Analyst 1"]  # eval scope only

        _, posting = await call(client, "get_job", job_id=todo[0]["job_id"])
        assert "<title>Junior Data Analyst 1</title>" in posting

        result, text = await call(
            client,
            "save_extraction",
            job_id=todo[0]["job_id"],
            model="claude-opus-5-5",
            prompt_version=version(),
            extraction=LABELS,
        )
        assert not result.is_error, text

        result = await client.call_tool("list_jobs_to_extract", {"model": "claude-opus-5-5"})
        assert result.structured_content["result"] == []

    conn = db.connect(path)
    report = extract.score_stored(
        conn, "claude-code/claude-opus-5-5", version(), path.parent / "labels"
    )
    assert report["groups"]["all"]["fields"]["seniority"] == 1.0
    assert report["missing"] == []


async def test_corpus_scope_lists_all_postings(server) -> None:
    async with Client(server[0]) as client:
        result = await client.call_tool(
            "list_jobs_to_extract", {"model": "m", "scope": "corpus", "limit": 1}
        )
    assert len(result.structured_content["result"]) == 1


async def test_target_scope_needs_dedup_groups_and_target_titles(server) -> None:
    mcp_server, path = server
    conn = db.connect(path)
    with conn:
        conn.execute("UPDATE jobs SET description = ? WHERE id = 2", ("x" * 500,))
        conn.execute("INSERT INTO job_groups VALUES (1, 1), (2, 2)")
    async with Client(mcp_server) as client:
        result = await client.call_tool("list_jobs_to_extract", {"model": "m", "scope": "target"})
    # Job 1's description is too short to count as a full posting.
    assert [j["job_id"] for j in result.structured_content["result"]] == [2]


async def test_save_rejects_bad_input(server) -> None:
    base = {"job_id": 1, "model": "m", "prompt_version": version(), "extraction": LABELS}
    async with Client(server[0]) as client:
        invalid = {**LABELS, "seniority": "rockstar"}
        result, _ = await call(client, "save_extraction", **{**base, "extraction": invalid})
        assert result.is_error

        result, text = await call(client, "save_extraction", **{**base, "prompt_version": "old"})
        assert result.is_error and "stale" in text

        result, text = await call(client, "save_extraction", **{**base, "job_id": 999})
        assert result.is_error and "no job" in text

        result, _ = await call(client, "save_extraction", **base)
        assert not result.is_error
        result, text = await call(client, "save_extraction", **base)
        assert result.is_error and "already" in text


async def test_prompt_warns_against_reading_labels(server) -> None:
    async with Client(server[0]) as client:
        prompt = await client.get_prompt("extract_postings", {"scope": "target", "max_jobs": "5"})
    text = prompt.messages[0].content.text
    assert "eval/labels/" in text and "up to 5" in text


async def test_rag_tools_retrieve_with_extraction_summary(server) -> None:
    mcp_server, path = server
    conn = db.connect(path)
    embed.embed_jobs(conn, fake_encode, embed.DEFAULT_MODEL)
    extraction = extract.Extraction.model_validate({**LABELS, "other_languages": []})
    extract.store_extraction(conn, 1, extraction, model="claude-code/claude-opus-5-5",
                             version=version())  # fmt: skip
    conn.close()

    async with Client(mcp_server) as client:
        _, profile = await call(client, "get_profile")
        assert "junior data analyst" in profile

        result = await client.call_tool("search_jobs", {"query": "2", "my_filters": False})
        hits = result.structured_content["result"]
        assert [h["job_id"] for h in hits] == [2, 1]
        assert hits[0]["seniority"] is None  # job 2 has no extraction

        # My filters keep only job 1: extracted, junior, German "plus", no other language.
        result = await client.call_tool("match_resume", {"k": 5})
        hits = result.structured_content["result"]
        assert [h["job_id"] for h in hits] == [1]
        assert hits[0]["seniority"] == "junior"
        assert hits[0]["required_skills"] == ["SQL", "Python"]

        prompt = await client.get_prompt("match_me", {"k": "3"})
        assert "never claim experience" in prompt.messages[0].content.text


async def test_skill_mapping_round_trip(server) -> None:
    mcp_server, path = server
    conn = db.connect(path)
    extraction = extract.Extraction.model_validate(
        {**LABELS, "required_skills": ["SQL", "PySpark"], "nice_to_have_skills": ["dbt"]}
    )
    extract.store_extraction(conn, 1, extraction, model="claude-code/claude-opus-5-5",
                             version=version())  # fmt: skip
    conn.close()

    async with Client(mcp_server) as client:
        result = await client.call_tool("list_unmapped_skills", {})
        batch = result.structured_content["batch"]
        # Résumé ("SQL and Python, junior data analyst") has no skills section -> postings only.
        assert [b["raw"] for b in batch] == ["dbt", "pyspark", "sql"]

        bad = [{"raw": "pyspark", "skill": "PySpark"}]  # new skill without a category
        result, text = await call(client, "save_skill_mappings", mappings=bad)
        assert result.is_error and "needs a category" in text

        good = [
            {"raw": "pyspark", "skill": "PySpark", "category": "data_engineering",
             "parent": "Apache Spark"},
            {"raw": "sql", "skill": "SQL", "category": "programming"},
        ]  # fmt: skip
        result, text = await call(client, "save_skill_mappings", mappings=good)
        assert not result.is_error, text

        result = await client.call_tool("list_canonical_skills", {})
        assert result.structured_content["Apache Spark"]["category"] == "data_engineering"
        result = await client.call_tool("list_unmapped_skills", {})
        assert result.structured_content["remaining"] == 1


async def test_skill_gaps_passes_arguments_to_the_graph(tmp_path) -> None:
    calls = []

    def fake_graph(kind, scope, k, **params):
        calls.append((kind, scope, k, params))
        return [{"skill": "Airflow", "score": 2.0}]

    path = tmp_path / "jobs.db"
    db.connect(path).close()
    server = build_server(lambda: db.connect(path), "RULES", tmp_path, graph_query=fake_graph)
    async with Client(server) as client:
        result = await client.call_tool("skill_gaps", {"kind": "unlock", "scope": "target"})
        assert result.structured_content["result"] == [{"skill": "Airflow", "score": 2.0}]
        result, text = await call(client, "skill_gaps", kind="together")
        assert result.is_error and "needs a canonical skill" in text
        await client.call_tool("skill_gaps", {"kind": "together", "skill": "SQL", "k": 999})
    assert calls == [("unlock", "target", 15, {}), ("together", "eligible", 50, {"skill": "SQL"})]


def store(path, job_id, **fields) -> None:
    conn = db.connect(path)
    x = extract.Extraction.model_validate({**LABELS, "other_languages": [], **fields})
    extract.store_extraction(conn, job_id, x, model="claude-code/claude-opus-5-5",
                             version=version())  # fmt: skip
    conn.close()


async def test_new_jobs_count_groups_and_pending_extractions(server) -> None:
    mcp_server, path = server
    store(path, 1)
    conn = db.connect(path)
    with conn:
        conn.execute("UPDATE jobs SET description = ? WHERE id = 2", ("x" * 500,))
        conn.execute("INSERT INTO job_groups VALUES (1, 1), (2, 2)")
    async with Client(mcp_server) as client:
        result = await client.call_tool("list_new_jobs", {"since": "2026-01-01"})
        new = result.structured_content
        assert new["new_postings"] == 2
        assert new["pending_extraction"] == 1  # job 2: target title, long text, no extraction
        assert [j["job_id"] for j in new["jobs"]] == [1]  # job 2 fails my filters (unextracted)
        assert new["jobs"][0]["first_seen"].startswith("20")

        result = await client.call_tool(
            "list_new_jobs", {"since": "2026-01-01", "my_filters": False}
        )
        assert [j["job_id"] for j in result.structured_content["jobs"]] == [2, 1]

        result, text = await call(client, "list_new_jobs", since="yesterday")
        assert result.is_error and "ISO date" in text

    # A job seen before under another source is not new, even if this copy is.
    with conn:
        conn.execute("UPDATE jobs SET first_seen = '2025-01-01T00:00:00+00:00' WHERE id = 1")
        conn.execute("UPDATE job_groups SET group_id = 1 WHERE job_id = 2")
    conn.close()
    async with Client(mcp_server) as client:
        result = await client.call_tool(
            "list_new_jobs", {"since": "2026-01-01", "my_filters": False}
        )
    assert result.structured_content["new_postings"] == 0


async def test_job_details_bring_extraction_eligibility_and_duplicates(server) -> None:
    mcp_server, path = server
    store(path, 1)
    conn = db.connect(path)
    with conn:
        conn.execute("INSERT INTO job_groups VALUES (1, 1), (2, 1)")
    conn.close()
    async with Client(mcp_server) as client:
        details = (await client.call_tool("get_job_details", {"job_id": 1})).structured_content
        other = (await client.call_tool("get_job_details", {"job_id": 2})).structured_content
        result, text = await call(client, "get_job_details", job_id=999)
    assert details["eligible"] is True
    assert details["extraction"]["seniority"] == "junior"
    assert details["duplicates"] == [{"job_id": 2, "source": "arbeitnow",
                                      "url": "https://example.com/2"}]  # fmt: skip
    assert other["extraction"] is None and other["eligible"] is False
    assert result.is_error and "no job" in text


async def test_job_fit_adds_coverage_to_graph_rows(server, tmp_path) -> None:
    _, path = server
    store(path, 1)
    calls = []

    def fake_graph(kind, scope, k, **params):
        calls.append((kind, params))
        return [
            {"skill": "SQL", "category": "programming", "required": True, "my_credit": 1.0,
             "via": ["SQL"]},
            {"skill": "pandas", "category": "programming", "required": True, "my_credit": 0.5,
             "via": ["Python"]},
            {"skill": "dbt", "category": "data_engineering", "required": False, "my_credit": 0.0,
             "via": []},
        ]  # fmt: skip

    mcp_server = build_server(lambda: db.connect(path), "RULES", tmp_path, graph_query=fake_graph)
    async with Client(mcp_server) as client:
        fit = (await client.call_tool("job_fit", {"job_id": 1})).structured_content
        result, text = await call(client, "job_fit", job_id=2)
    assert calls == [("fit", {"job_id": 1})]  # job 2 is refused before the graph is asked
    assert fit["coverage"] == 0.75 and fit["title"] == "Junior Data Analyst 1"
    assert [s["skill"] for s in fit["skills"]] == ["SQL", "pandas", "dbt"]
    assert result.is_error and "no extraction" in text
