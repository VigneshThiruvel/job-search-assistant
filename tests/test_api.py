from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_applications import save_resume
from test_inbox import DESCRIPTION

from job_search_assistant import applications, db, inbox
from job_search_assistant.api import create_app
from job_search_assistant.models import Job

TEX = Path(__file__).parent / "fixtures/tailor/resume.tex"
ORIGIN = {"Origin": "http://localhost:5173"}


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "jobs.db"
    conn = db.connect(path)
    for source, sid, title in (("arbeitnow", "a1", "Data Analyst"),
                               ("linkedin", "l2", "Junior Data Engineer"),
                               ("arbeitnow", "a3", "Data Engineer")):  # fmt: skip
        job = Job(
            source=source, source_job_id=sid, url=f"https://example.com/{sid}", title=title,
            company="ACME", location="Berlin", country=None, remote=None,
            description=DESCRIPTION if source == "arbeitnow" else "",
            posted_at=datetime.now(UTC), raw={},
        )  # fmt: skip
        db.upsert_job(conn, job, lang="en", content_hash=sid, seen_at=datetime.now(UTC))
    # Job 3 is the same job as the LinkedIn alert 2: it belongs to the alerts list.
    conn.executemany("INSERT INTO job_groups VALUES (?, ?)", [(1, 1), (2, 2), (3, 2)])
    conn.commit()
    applications.queue(conn, 1, "maybe", "Partial fit.")
    applications.queue(conn, 3, "apply", "Strong fit.")
    conn.close()

    box = tmp_path / "inbox"
    box.mkdir()
    (box / "2-acme.txt").write_text(f"# header\nExpired: no\nNotes:\n\n{inbox.MARKER}\n\n")
    app = create_app(lambda: db.connect(path), resume_tex=TEX, inbox_dir=box,
                     drafts_dir=tmp_path / "drafts", hosts=["testserver"])  # fmt: skip
    return TestClient(app), path, box


def test_queue_lists_and_channels(setup) -> None:
    client, _, _ = setup
    cards = client.get("/api/applications", params={"status": "queued"}).json()
    by_id = {c["job_id"]: c for c in cards}
    assert by_id[1]["channel"] == "other" and by_id[1]["apply_url"] == "https://example.com/a1"
    assert by_id[3]["channel"] == "alerts" and by_id[3]["apply_url"] == "https://example.com/l2"
    assert client.get("/api/status").json()["queued"] == 2


def test_my_moves_go_through_the_queue_rules(setup) -> None:
    client, path, _ = setup
    r = client.post("/api/applications/3/select", json={"letter": False, "note": "stress SQL"},
                    headers=ORIGIN)  # fmt: skip
    assert r.status_code == 200 and r.json()["status"] == "selected"
    assert r.json()["needs_resume"] is True and r.json()["needs_letter"] is False

    r = client.post("/api/applications/99/skip", json={}, headers=ORIGIN)
    assert r.status_code == 409 and "not in the queue" in r.json()["detail"]
    client.post("/api/applications/1/applied", json={}, headers=ORIGIN)
    r = client.post("/api/applications/1/skip", json={}, headers=ORIGIN)
    assert r.status_code == 409  # applied is final

    conn = db.connect(path)
    save_resume(conn, 3)
    conn.close()
    detail = client.get("/api/applications/3").json()
    assert detail["card"]["status"] == "ready" and detail["card"]["note"] == "stress SQL"
    first = detail["resume"]["entries"][0]["bullets"][0]
    assert first["sources"][0]["id"] == "E1.1" and first["text"] == first["sources"][0]["text"]
    assert detail["resume"]["skills"] is None  # order unchanged
    assert client.get("/api/applications/42").status_code == 404


def test_pasting_in_the_ui_replaces_the_file(setup) -> None:
    client, path, box = setup
    assert [i["job_id"] for i in client.get("/api/inbox").json()] == [2]
    assert client.get("/api/status").json()["inbox_open"] == 1

    r = client.post("/api/inbox/2", json={"description": "too short"}, headers=ORIGIN)
    assert r.status_code == 409 and "too short" in r.json()["detail"]
    r = client.post("/api/inbox/2", json={"description": DESCRIPTION, "notes": "recruiter"},
                    headers=ORIGIN)  # fmt: skip
    assert r.status_code == 200
    assert client.get("/api/inbox").json() == []
    assert (box / "done" / "2-acme.txt").exists()
    conn = db.connect(path)
    assert conn.execute("SELECT description FROM jobs WHERE id = 2").fetchone()[0]
    assert inbox.my_note(conn, 2) == ("recruiter", False)


def test_cross_site_writes_and_foreign_hosts_are_blocked(setup) -> None:
    client, _, _ = setup
    # A form on another site: no allowed Origin, not JSON.
    r = client.post("/api/applications/1/skip", data={"note": "x"},
                    headers={"Origin": "https://evil.example"})  # fmt: skip
    assert r.status_code == 403
    r = client.post("/api/applications/1/skip", json={})  # no Origin at all
    assert r.status_code == 403
    r = client.post("/api/applications/1/skip", data={"note": "x"}, headers=ORIGIN)  # form body
    assert r.status_code == 403
    # DNS rebinding: a request whose Host is someone else's domain.
    r = client.get("/api/applications", headers={"Host": "evil.example"})
    assert r.status_code == 400
