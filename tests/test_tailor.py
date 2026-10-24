import shutil
from pathlib import Path

import pytest

from job_search_assistant import tailor
from job_search_assistant.tailor import Bullet, SkillLine, TailoredEntry, TailoredResume

TEX = (Path(__file__).parent / "fixtures/tailor/resume.tex").read_text()
SKILL_MAP = {  # raw -> canonical, as in the Phase 4 taxonomy
    "kafka": "Apache Kafka", "apache kafka": "Apache Kafka", "python": "Python",
    "sql": "SQL", "postgresql": "PostgreSQL", "apache flink": "Apache Flink",
    "flink": "Apache Flink", "stream processing": "Stream Processing",
    "streaming": "Stream Processing", "pandas": "pandas", "dbt": "dbt",
    "data pipelines": "Data Pipelines", "pipeline": "Data Pipelines", "r": "R",
    "regression": "Regression Analysis", "communication": None,
}  # fmt: skip
PARENTS = {"pandas": "Python", "Apache Flink": "Stream Processing"}


@pytest.fixture
def resume() -> tailor.Resume:
    return tailor.parse(TEX)


@pytest.fixture
def finder() -> tailor.SkillFinder:
    return tailor.SkillFinder(SKILL_MAP, PARENTS)


def identity(r: tailor.Resume) -> TailoredResume:
    return TailoredResume(
        summary=r.summary,
        skills=[SkillLine(label=label, items=items) for label, items in r.skills],
        entries=[
            TailoredEntry(id=e.id, bullets=[Bullet(text=b, sources=[f"{e.id}.{i}"])
                                            for i, b in enumerate(e.bullets, 1)])
            for e in r.entries
        ],
    )  # fmt: skip


def test_parse_turns_latex_into_plain_text_with_ids(resume) -> None:
    assert [e.id for e in resume.entries] == ["E1", "P1", "P2"]
    assert (
        resume.bullet("E1.1")
        == "Implemented Kafka-based job processing, reducing queue latency by 20%."
    )
    assert resume.bullet("P2.1") == "Studying λ in résumé-style language models on 4 GPUs."
    assert (
        resume.entry("P1").heading
        == "Traffic Pipeline | Python, Apache Flink — Apr 2025 – Jul 2025"
    )
    assert resume.skills[1] == ("Data Engineering", ["Apache Kafka", "Data Pipelines"])
    assert resume.summary.startswith("Data engineer with a year")


def test_untouched_resume_passes_and_renders_back(resume, finder) -> None:
    t = identity(resume)
    assert tailor.check(resume, t, finder) == []
    assert tailor.dropped(resume, t) == []
    rendered = tailor.render(resume, t)
    assert tailor.parse(rendered).plain() == resume.plain()
    assert r"queue latency by 20\%" in rendered and r"Studying $\lambda$" in rendered


def test_rewording_reordering_and_dropping_are_fine(resume, finder) -> None:
    t = TailoredResume(
        summary="Entry-level data engineer: a year of Kafka streaming pipelines.",
        skills=[SkillLine(label="Data Engineering", items=["Data Pipelines", "Apache Kafka"]),
                SkillLine(label="Programming", items=["SQL", "Python"])],
        entries=[
            TailoredEntry(id="E1", bullets=[Bullet(
                text="Cut queue latency by 20% with Kafka-based job processing.",
                sources=["E1.1"])]),
            TailoredEntry(id="P1", bullets=[Bullet(
                text="Built a real-time stream processing pipeline for taxi data.",  # broader
                sources=["P1.1"])]),
        ],
    )  # fmt: skip
    assert tailor.check(resume, t, finder) == []
    assert tailor.dropped(resume, t) == ["[E1.2] Maintained PostgreSQL databases.",
                                         "[P2] Thesis on λ — Sep 2026 – Present"]  # fmt: skip
    rendered = tailor.parse(tailor.render(resume, t))
    assert [e.id for e in rendered.entries] == ["E1", "P1"]
    assert rendered.skills[0] == ("Data Engineering", ["Data Pipelines", "Apache Kafka"])


def test_new_facts_are_rejected(resume, finder) -> None:
    def problems(**changes) -> str:
        t = identity(resume)
        for path, value in changes.items():
            if path == "summary":
                t.summary = value
            else:
                entry, n = path.split("_")
                t.entries[[e.id for e in t.entries].index(entry)].bullets[int(n)].text = value
        return "\n".join(tailor.check(resume, t, finder))

    assert "numbers ['35']" in problems(E1_0="Reduced queue latency by 35% with Kafka.")
    assert "['pandas']" in problems(P1_0="Built a pipeline with Python and pandas.")  # narrower
    assert "['dbt']" in problems(E1_1="Maintained PostgreSQL databases and dbt models.")
    # Kafka is in E1, not in P1: facts don't move between entries.
    assert "['Kafka']" in problems(P1_0="Built a real-time Kafka pipeline with Python.")
    assert "['dbt']" in problems(summary="Data engineer with a year of Kafka and dbt.")
    assert "['R']" in problems(summary="Data engineer working in R.")  # the language
    assert problems(summary="Data engineer; r is just a letter.") == ""
    assert "no LaTeX" in problems(E1_1=r"Maintained \input{/etc/passwd} databases.")


def test_structure_rules(resume, finder) -> None:
    t = identity(resume)
    t.entries = t.entries[1:]  # drops the only role
    assert any("keep every experience entry" in p for p in tailor.check(resume, t, finder))

    t = identity(resume)
    t.entries[0].bullets[0].sources = ["P1.1"]  # cites another entry's bullet
    assert any("not bullets of E1" in p for p in tailor.check(resume, t, finder))

    t = identity(resume)
    t.skills[0].items.append("Rust")
    assert any(p.startswith("skills:") for p in tailor.check(resume, t, finder))

    t = identity(resume)
    t.entries.append(TailoredEntry(id="P9", bullets=[]))
    assert any("unknown ids" in p for p in tailor.check(resume, t, finder))


@pytest.mark.skipif(shutil.which("latexmk") is None, reason="needs a LaTeX installation")
def test_compiles_to_pdf(resume, tmp_path) -> None:
    path = tmp_path / "resume.tex"
    path.write_text(tailor.render(resume, identity(resume)))
    pdf, pages = tailor.compile_pdf(path)
    assert pdf.exists() and pages == 1
