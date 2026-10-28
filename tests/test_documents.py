import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_applications import save_resume

from job_search_assistant import applications, db, documents, tailor
from job_search_assistant.models import Job

TEX = (Path(__file__).parent / "fixtures/tailor/resume.tex").read_text()


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setenv("DOCUMENT_PREFIX", "jane_doe")
    monkeypatch.setenv("APPLICATION_DIR", str(tmp_path / "unused"))  # tests pass `folder`
    built: list[tuple[int, str]] = []

    def fake_build(conn, job_id, kind, resume_tex, drafts=None, date=None):
        built.append((job_id, kind))
        pdf = tmp_path / "build" / f"{job_id}-{kind}.pdf"
        pdf.parent.mkdir(exist_ok=True)
        pdf.write_text(f"PDF of job {job_id} ({kind})")
        return pdf

    monkeypatch.setattr(documents, "build", fake_build)
    conn = db.connect(":memory:")
    for i, title in ((1, "Data Analyst (m/w/d)"), (2, "Data Engineer"), (3, "Data Analyst")):
        job = Job(source="arbeitnow", source_job_id=f"j{i}", url=f"https://e/{i}", title=title,
                  company="ACME GmbH", location="Berlin", country=None, remote=None,
                  description="d", posted_at=datetime.now(UTC), raw={})  # fmt: skip
        db.upsert_job(conn, job, lang="en", content_hash=f"h{i}", seen_at=datetime.now(UTC))
        applications.queue(conn, i, "apply", "Good fit.")
        applications.select(conn, i)
        save_resume(conn, i)
    yield conn, built
    conn.close()


def test_working_file_then_renamed_after_applying(setup, tmp_path) -> None:
    conn, built = setup
    docs = tmp_path / "docs"
    working = docs / "resume" / "jane_doe_resume.pdf"
    assert documents.save(conn, 1, "resume", TEX, folder=docs) == working
    assert working.read_text() == "PDF of job 1 (resume)"

    applications.close(conn, 1, "applied")
    final = docs / "resume" / "acme-gmbh_data-analyst.pdf"  # gender marker dropped
    assert documents.finalize(conn, 1, TEX, folder=docs) == [final]
    assert final.read_text() == "PDF of job 1 (resume)" and not working.exists()
    assert built == [(1, "resume")]  # renamed, not rebuilt
    assert documents.finalize(conn, 1, TEX, folder=docs) == []  # nothing left to rename
    assert documents.saved(conn, 1) == [{"kind": "resume", "path": str(final), "exists": True}]


def test_another_jobs_working_file_is_never_renamed(setup, tmp_path) -> None:
    conn, built = setup
    docs = tmp_path / "docs"
    working = docs / "resume" / "jane_doe_resume.pdf"
    documents.save(conn, 1, "resume", TEX, folder=docs)
    documents.save(conn, 2, "resume", TEX, folder=docs)  # overwrites the working file
    applications.close(conn, 1, "applied")
    [final] = documents.finalize(conn, 1, TEX, folder=docs)
    assert final.read_text() == "PDF of job 1 (resume)"  # rebuilt from job 1's draft
    assert working.read_text() == "PDF of job 2 (resume)"  # job 2's file stays

    # Same company and title as job 1: the name gets the job id.
    documents.save(conn, 3, "resume", TEX, folder=docs)
    applications.close(conn, 3, "applied")
    assert documents.finalize(conn, 3, TEX, folder=docs) == [
        docs / "resume" / "acme-gmbh_data-analyst_3.pdf"
    ]
    assert not working.exists()  # job 3's save had replaced job 2's; it moved on applying
    # Saving after applying goes straight to the job's own name, never the working file.
    assert documents.save(conn, 1, "resume", TEX, folder=docs) == final
    assert not working.exists()


def test_unsaved_jobs_get_no_files_and_drafts_are_required(setup, tmp_path) -> None:
    conn, built = setup
    applications.close(conn, 2, "applied")
    assert documents.finalize(conn, 2, TEX, folder=tmp_path / "docs") == []
    assert not (tmp_path / "docs").exists()


def test_letter_is_escaped_once_and_keeps_its_lines() -> None:
    letter = (
        "Dear Ms. **Ng**,\n\nR&D at 100% — C# and \\LaTeX_{x}.\nThanks\n\n# Kind regards,\nJane"
    )
    tex = tailor.render_letter(TEX, letter, "28 October 2026")
    body = tex[tex.index(r"\hfill") :]
    assert r"Dear Ms. Ng," in body and "**" not in body
    assert r"R\&D at 100\% --- C\# and \textbackslash{}LaTeX\_\{x\}. \\" in body
    assert "Kind regards, \\\\\nJane" in body
    assert tex.startswith(TEX[: TEX.index(r"\begin{document}")])  # the résumé's preamble


@pytest.mark.skipif(shutil.which("latexmk") is None, reason="needs a LaTeX installation")
def test_letter_compiles(tmp_path) -> None:
    path = tmp_path / "cover_letter.tex"
    path.write_text(tailor.render_letter(TEX, "Dear team,\n\nI'd like to join (R&D).", "1 May"))
    pdf, pages = tailor.compile_pdf(path)
    assert pdf.exists() and pages == 1
