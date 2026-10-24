"""Phase 6: a résumé tailored to one job — reworded and reordered, never new facts.

The source of truth is profile/resume.tex (Jake's template). The agent sees it as
plain text with ids (S = summary, E1.2 = experience 1, bullet 2, P3.1 = project 3,
bullet 1) and sends back: a new summary, the skill lines in a new order, and per
entry the bullets it keeps, each reworded and citing the original bullets it says.

Rewording defeats exact-quote checks (that's the point of it), so facts are
compared at the level where they can be checked mechanically:
  - numbers: every number in a new bullet appears in its cited bullets;
  - skills: every skill named in a new bullet (found via the Phase 4 taxonomy, so
    "Datenmodellierung" and "data modelling" are one skill) is in its cited bullets
    or entry heading, or broader than one that is (pandas -> Python is fine, the
    other way round is a new claim);
  - structure: no new entries, every role kept, facts don't move between entries,
    skill lines are permutations of the original.
Inflated verbs ("worked on" -> "led") pass these checks; the review shows each new
bullet next to its originals for that.
"""

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from job_search_assistant import skills

GUIDE = Path("profile/resume_tailoring.md")
TEX = Path("profile/resume.tex")
SUMMARY_SLACK = 1.15  # new summary may be up to 15% longer (in words) than the original
BULLET_SLACK = 1.10  # same for all kept bullets together: the page must still fit
UNSAFE = re.compile(r"[\\{}$^~]")  # LaTeX control characters: text stays text
# Raw forms that are mostly ordinary words ("regression risks", "access to"): as skill names
# they would let a bullet borrow a skill it never had, or reject plain wording.
AMBIGUOUS = {"access", "evaluation", "experiments", "optimization", "regression", "search",
             "tests", "tracking"}  # fmt: skip

# ---------------------------------------------------------------- LaTeX <-> plain text

TO_PLAIN = [
    (r"$\lambda$", "λ"), (r"$|$", "|"), (r"\%", "%"), (r"\&", "&"), (r"\#", "#"),
    (r"\_", "_"), ("---", "—"), ("--", "–"), (r"\'e", "é"), (r'\"a', "ä"),
    (r'\"o', "ö"), (r'\"u', "ü"),
]  # fmt: skip
TO_LATEX = [
    ("%", r"\%"), ("&", r"\&"), ("#", r"\#"), ("_", r"\_"), ("—", "---"), ("–", "--"),
    ("λ", r"$\lambda$"),
]  # fmt: skip


def to_plain(tex: str) -> str:
    for a, b in TO_PLAIN:
        tex = tex.replace(a, b)
    tex = re.sub(r"\\(?:textbf|emph|textit|small)\{([^{}]*)\}", r"\1", tex)
    return " ".join(tex.replace("{", "").replace("}", "").split())


def to_latex(text: str) -> str:
    for a, b in TO_LATEX:
        text = text.replace(a, b)
    return text


# ------------------------------------------------------------------------ parsing


@dataclass
class Entry:
    id: str  # E1, P2, ...
    kind: Literal["experience", "project"]
    heading_tex: str  # \resumeSubheading{..}{..}{..}{..} or \resumeProjectHeading{..}{..}
    bullets: list[str]  # plain text

    @property
    def heading(self) -> str:
        parts = re.findall(r"\{((?:[^{}]|\{[^{}]*\})*)\}", self.heading_tex)
        return " — ".join(to_plain(p) for p in parts if p.strip())


@dataclass
class Resume:
    tex: str
    summary: str
    skills: list[tuple[str, list[str]]]  # (label, items), in order
    entries: list[Entry] = field(default_factory=list)

    def bullet(self, bullet_id: str) -> str:
        entry_id, n = bullet_id.split(".")
        return self.entry(entry_id).bullets[int(n) - 1]

    def entry(self, entry_id: str) -> Entry:
        return next(e for e in self.entries if e.id == entry_id)

    def plain(self) -> str:
        lines = [f"[S] {self.summary}"]
        for e in self.entries:
            lines.append(f"[{e.id}] {e.heading}")
            lines += [f"  [{e.id}.{i}] {b}" for i, b in enumerate(e.bullets, 1)]
        lines += [f"{label}: {', '.join(items)}" for label, items in self.skills]
        return "\n".join(lines)


def braced(text: str, start: int) -> tuple[str, int]:
    """Content of the {...} group opening at text[start]; returns (content, end index)."""
    assert text[start] == "{", text[start : start + 20]
    depth = 0
    for i in range(start, len(text)):
        depth += {"{": 1, "}": -1}.get(text[i], 0)
        if depth == 0:
            return text[start + 1 : i], i + 1
    raise ValueError("unbalanced braces")


def section(tex: str, name: str) -> tuple[int, int]:
    """(start, end) of a section's body: after \\section{name} up to the next section."""
    m = re.search(rf"\\section\{{{re.escape(name)}\}}", tex)
    if not m:
        raise ValueError(f"no \\section{{{name}}} in the résumé")
    nxt = re.compile(r"\\section\{|\\end\{document\}").search(tex, m.end())
    return m.end(), nxt.start() if nxt else len(tex)


def parse(tex: str) -> Resume:
    start, end = section(tex, "Summary")
    small = tex.index(r"\small{", start)
    summary, _ = braced(tex, small + len(r"\small"))

    start, end = section(tex, "Technical Skills")
    skill_lines = [
        (label, [s.strip() for s in items.split(",")])
        for label, items in re.findall(r"\\textbf\{([^}]*)\}\{:\s*([^}]*)\}", tex[start:end])
    ]
    resume = Resume(tex, to_plain(summary), skill_lines)
    for name, kind, prefix in (("Experience", "experience", "E"), ("Projects", "project", "P")):
        start, end = section(tex, name)
        body = tex[start:end]
        heads = list(re.finditer(r"\\resume(?:Subheading|ProjectHeading)\b", body))
        for n, h in enumerate(heads, 1):
            stop = heads[n].start() if n < len(heads) else len(body)
            chunk = body[h.start() : stop]
            heading = chunk[: chunk.index(r"\resumeItemListStart")].strip()
            bullets = []
            for m in re.finditer(r"\\resumeItem\{", chunk):
                content, _ = braced(chunk, m.end() - 1)
                bullets.append(to_plain(content))
            resume.entries.append(Entry(f"{prefix}{n}", kind, heading, bullets))
    return resume


# ------------------------------------------------------------------- tailored version


class Bullet(BaseModel):
    text: str = Field(description="plain text, no LaTeX")
    sources: list[str] = Field(description="ids of the original bullets it rewords, e.g. P2.1")


class TailoredEntry(BaseModel):
    id: str = Field(description="E1, P3, ... as in get_resume_sections")
    bullets: list[Bullet]


class SkillLine(BaseModel):
    label: str
    items: list[str]


class TailoredResume(BaseModel):
    summary: str
    skills: list[SkillLine] = Field(description="every original line and item, reordered")
    entries: list[TailoredEntry] = Field(
        description="all experience entries in original order; projects you keep, best first"
    )


NUMBER = re.compile(r"\d+(?:[.,]\d+)*")


def numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in NUMBER.findall(text)}


class SkillFinder:
    """Finds canonical skills named in text, via every raw form in the taxonomy."""

    def __init__(self, skill_map: dict[str, str | None], parents: dict[str, str | None]):
        self.skill_of = {raw: s for raw, s in skill_map.items() if s and raw not in AMBIGUOUS}
        self.parents = parents
        keys = sorted(self.skill_of, key=len, reverse=True)
        long = [re.escape(k) for k in keys if len(k) > 2]
        short = [k for k in keys if len(k) <= 2]  # "r", "go", "c": only as written in names
        self.long = (
            re.compile(rf"(?<![\w+#])(?:{'|'.join(long)})(?![\w+#])", re.I) if long else None
        )
        self.short = {k: s for k, s in self.skill_of.items() if k in short}

    def find(self, text: str) -> dict[str, str]:
        """canonical skill -> the words that named it."""
        found = {}
        if self.long:
            for m in self.long.finditer(text):
                found.setdefault(self.skill_of[skills.raw_key(m.group())], m.group())
        for word in re.findall(r"(?<![\w+#])[\w+#]{1,2}(?![\w+#])", text):
            skill = self.short.get(word.lower())
            if skill and word == skill[: len(word)] and word[0].isupper():  # "Go", "R", "C"
                found.setdefault(skill, word)
        return found

    def ancestors(self, skill: str) -> set[str]:
        seen, s = set(), self.parents.get(skill)
        while s and s not in seen:
            seen.add(s)
            s = self.parents.get(s)
        return seen

    def allowed(self, source_text: str) -> set[str]:
        have = set(self.find(source_text))
        return have | {a for s in have for a in self.ancestors(s)}


def check(resume: Resume, t: TailoredResume, finder: SkillFinder) -> list[str]:
    """Problems with a tailored version; empty = it only says what the résumé says."""
    problems = []
    for name, text in [("summary", t.summary)] + [
        (f"{e.id} bullet {i}", b.text) for e in t.entries for i, b in enumerate(e.bullets, 1)
    ]:
        if UNSAFE.search(text):
            problems.append(f"{name}: no LaTeX (backslash, braces, $, ^, ~) — plain text only")

    # Summary: may draw on the whole résumé.
    whole = resume.plain()
    if extra := numbers(t.summary) - numbers(whole):
        problems.append(f"summary: numbers {sorted(extra)} are not in the résumé")
    allowed = finder.allowed(whole)
    if extra := {s: w for s, w in finder.find(t.summary).items() if s not in allowed}:
        problems.append(f"summary: names skills not in the résumé: {sorted(extra.values())}")
    if len(t.summary.split()) > SUMMARY_SLACK * len(resume.summary.split()):
        problems.append(f"summary: longer than {SUMMARY_SLACK:.0%} of the original")

    # Skills: same lines, same items, any order.
    original = {label: sorted(items) for label, items in resume.skills}
    given = {line.label: sorted(line.items) for line in t.skills}
    if given != original or len(t.skills) != len(resume.skills):
        problems.append("skills: reorder the original lines and items only (none added, dropped "
                        "or moved between lines)")  # fmt: skip

    # Entries.
    ids = [e.id for e in t.entries]
    known = {e.id: e for e in resume.entries}
    if unknown := [i for i in ids if i not in known]:
        return problems + [f"entries: unknown ids {unknown}"]
    if len(set(ids)) != len(ids):
        problems.append("entries: an id appears twice")
    roles = [e.id for e in resume.entries if e.kind == "experience"]
    if [i for i in ids if known[i].kind == "experience"] != roles:
        problems.append(f"entries: keep every experience entry, in the original order ({roles})")
    if ids[: len(roles)] != roles:
        problems.append("entries: experience entries come first")
    for e in t.entries:
        entry = known[e.id]
        if not e.bullets:
            problems.append(f"{e.id}: keep at least one bullet (or drop the project)")
        for i, b in enumerate(e.bullets, 1):
            where = f"{e.id} bullet {i}"
            if not b.sources:
                problems.append(f"{where}: cite the original bullet(s) it rewords")
                continue
            if bad := [s for s in b.sources if not re.fullmatch(rf"{e.id}\.\d+", s)
                       or int(s.split(".")[1]) > len(entry.bullets)]:  # fmt: skip
                problems.append(f"{where}: sources {bad} are not bullets of {e.id}")
                continue
            source = " ".join(resume.bullet(s) for s in b.sources)
            if extra := numbers(b.text) - numbers(source):
                problems.append(f"{where}: numbers {sorted(extra)} are not in {b.sources}")
            allowed = finder.allowed(f"{source} {entry.heading}")
            if extra := {s: w for s, w in finder.find(b.text).items() if s not in allowed}:
                problems.append(f"{where}: names skills not in {b.sources} or the {e.id} heading: "
                                f"{sorted(extra.values())}")  # fmt: skip

    kept = sum(len(b.text.split()) for e in t.entries for b in e.bullets)
    total = sum(len(b.split()) for e in resume.entries for b in e.bullets)
    if kept > BULLET_SLACK * total:
        problems.append(
            f"bullets: {kept} words in total, keep it under {int(BULLET_SLACK * total)}"
        )
    return problems


def dropped(resume: Resume, t: TailoredResume) -> list[str]:
    """Original entries and bullets the tailored version leaves out (for my review)."""
    cited = {s for e in t.entries for b in e.bullets for s in b.sources}
    kept = {e.id for e in t.entries}
    out = []
    for e in resume.entries:
        if e.id not in kept:
            out.append(f"[{e.id}] {e.heading}")
            continue
        out += [f"[{e.id}.{i}] {b}" for i, b in enumerate(e.bullets, 1)
                if f"{e.id}.{i}" not in cited]  # fmt: skip
    return out


# ---------------------------------------------------------------------- rendering


def render(resume: Resume, t: TailoredResume) -> str:
    """The original .tex with summary, skills, experience bullets and projects replaced."""
    tex = resume.tex
    known = {e.id: e for e in resume.entries}

    def entries_tex(kind: str) -> str:
        parts = []
        for e in (e for e in t.entries if known[e.id].kind == kind):
            items = "\n".join(rf"        \resumeItem{{{to_latex(b.text)}}}" for b in e.bullets)
            parts.append(f"    {known[e.id].heading_tex}\n      \\resumeItemListStart\n{items}\n"
                         "      \\resumeItemListEnd\n")  # fmt: skip
        return "\n".join(parts)

    # Replace from the bottom up so earlier offsets stay valid.
    for name, kind in (("Projects", "project"), ("Experience", "experience")):
        start, end = section(tex, name)
        a = tex.index(r"\resumeSubHeadingListStart", start) + len(r"\resumeSubHeadingListStart")
        b = tex.rindex(r"\resumeSubHeadingListEnd", start, end)
        tex = f"{tex[:a]}\n\n{entries_tex(kind)}\n  {tex[b:]}"

    start, end = section(tex, "Technical Skills")
    lines = [rf"     \textbf{{{s.label}}}{{: {', '.join(s.items)}}}" for s in t.skills]
    block = " \\\\\n".join(lines)
    first = re.search(r"\\textbf\{[^}]*\}\{:", tex[start:end]).start() + start
    last = list(re.finditer(r"\\textbf\{[^}]*\}\{:[^}]*\}", tex[start:end]))[-1].end() + start
    tex = tex[:first] + block.lstrip() + tex[last:]

    start, _ = section(tex, "Summary")
    small = tex.index(r"\small{", start) + len(r"\small")
    _, close = braced(tex, small)
    return f"{tex[: small + 1]}\n{to_latex(t.summary)}\n{tex[close - 1 :]}"


def original_pages(tex: str, work_dir: Path) -> int | None:
    """Page count of the untailored résumé: the budget a tailored one must not exceed."""
    path = work_dir / "_original" / "resume.tex"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(tex)
    return compile_pdf(path)[1]


def compile_pdf(tex_path: Path) -> tuple[Path, int | None]:
    """latexmk -> (pdf path, page count). Shell escape stays off (latexmk's default)."""
    subprocess.run(
        ["latexmk", "-pdf", "-interaction=nonstopmode", "-halt-on-error", "-no-shell-escape",
         f"-outdir={tex_path.parent}", str(tex_path)],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip
    log = tex_path.with_suffix(".log").read_text(errors="replace")
    m = re.search(r"Output written on .*?\((\d+) pages?", log, re.S)
    return tex_path.with_suffix(".pdf"), int(m.group(1)) if m else None
