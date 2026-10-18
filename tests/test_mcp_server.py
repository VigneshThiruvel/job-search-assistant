from datetime import UTC, datetime

import pytest
import yaml
from mcp import Client
from test_extract import LABELS

from job_search_assistant import db, extract
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
    return build_server(lambda: db.connect(path), "RULES", labels), path


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
    }
    # The client sees the Pydantic schema, enums included, and is held to it.
    schema = tools["save_extraction"].input_schema
    assert "extraction" in schema["required"]


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
