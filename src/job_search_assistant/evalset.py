"""Hand-labelled eval set for Phase 2 extraction.

    python -m job_search_assistant.evalset sample   # write eval/labels/*.yaml (once)
    python -m job_search_assistant.evalset check    # validate labels, show progress

Each file holds one posting plus the labels I fill in by hand. Field
definitions and decision rules live in eval/GUIDELINES.md.
"""

import argparse
import random
import re
import sqlite3
import sys
from pathlib import Path
from typing import Any

import yaml

from job_search_assistant import db

LABELS_DIR = Path("eval/labels")
SEED = 42  # fixed, so the sample is reproducible
# Sources with full descriptions (Adzuna has 500-char snippets, email alerts none).
SOURCES = ("arbeitnow", "bundesagentur")

ENUMS: dict[str, tuple[str, ...]] = {
    "role_family": (
        "data_analyst", "data_engineer", "data_scientist", "ai_engineer", "ml_engineer",
        "analytics_engineer", "bi_analyst", "other",
    ),
    "seniority": ("intern", "working_student", "junior", "mid", "senior", "lead", "unknown"),
    "employment_type": ("full_time", "part_time", "contract", "freelance", "unknown"),
    "german_level": ("none", "plus", "basic", "business", "fluent", "native"),
    "english_level": ("none", "plus", "basic", "business", "fluent", "native"),
    "workplace": ("onsite", "hybrid", "remote", "unknown"),
    "degree": ("none", "vocational", "bachelor", "master", "phd", "unknown"),
}  # fmt: skip
BOOLS = ("is_target_role", "german_implied")
INT_OR_NULL = ("years_experience_min",)
LISTS = ("required_skills", "nice_to_have_skills", "other_languages", "restrictions")
LANGUAGE_ITEM = re.compile(r"^[A-Z][a-z]+: (plus|basic|business|fluent|native)$")
OPTIONAL = ("years_experience_min", "salary", "notes")

LABEL_TEMPLATE = """\
reviewed: false            # true only after a human checked every field below
labelled_by: human         # human | claude-draft (who wrote the labels before review)
labels:
  is_target_role:          # true/false: one of the roles in CLAUDE.md "Search scope"
  role_family:             # {role_family}
  seniority:               # {seniority}
  employment_type:         # {employment_type}
  german_level:            # {german_level}
  german_implied:          # true/false: level inferred (e.g. German-only posting), not stated
  english_level:           # {english_level}
  other_languages: []      # non-German/English, e.g. ["Dutch: fluent"] (same scale)
  workplace:               # {workplace}
  years_experience_min:    # integer, or leave empty if not stated
  degree:                  # {degree} (minimum required)
  required_skills: []      # as written, e.g. [SQL, Python, Power BI]
  nice_to_have_skills: []
  restrictions: []         # eligibility as written, e.g. ["enrolled student", "EU citizens only"]
  salary:                  # as written, or leave empty
  notes:                   # anything ambiguous; why you chose a label
""".format(**{k: " | ".join(v) for k, v in ENUMS.items()})

# Role words, not just "data"/"AI" anywhere in the title.
TARGET_TITLE = re.compile(
    r"\b(data|daten)[\w &/-]{0,25}(analyst|engineer|scientist|science|analytics)"
    r"|\banalytics engineer|\bmachine learning|\bml[\w -]{0,10}engineer"
    r"|\b(ai|ki)[\w -]{0,10}engineer"
    r"|\bbi[\w -]{0,10}(analyst|developer|engineer)|business intelligence|datenanaly",
    re.I,
)
# Search scope is Germany, neighbours or remote; the pool also has UK/US postings.
OUT_OF_SCOPE = re.compile(
    r"london|united kingdom|\buk\b|england|westminster|manchester|edinburgh|dublin|ireland"
    r"|usa|united states|new york|san francisco|milan|madrid|barcelona|lisbon|abu dhabi|dubai",
    re.I,
)
SENIOR_TITLE = re.compile(r"\b(senior|sr\.?|lead|principal|head|staff)\b", re.I)
JUNIOR_TITLE = re.compile(
    r"\b(junior|jr\.?|entry|graduate|werkstudent\w*|working student|intern\w*|praktik\w*"
    r"|trainee)\b",
    re.I,
)
MENTIONS_GERMAN = re.compile(r"\b(german|deutsch\w*)\b", re.I)

# Strata: (name, how many, predicate). Order matters: earlier strata pick first.
STRATA = [
    ("target_en_junior", 6, lambda j: j["target"] and j["lang"] == "en" and j["junior"]),
    ("target_en_mentions_german", 5, lambda j: j["target"] and j["lang"] == "en" and j["german"]),
    ("target_en_senior", 4, lambda j: j["target"] and j["lang"] == "en" and j["senior"]),
    ("target_en_other", 4, lambda j: j["target"] and j["lang"] == "en"),
    ("target_de", 7, lambda j: j["target"] and j["lang"] == "de"),
    ("not_target", 4, lambda j: not j["target"] and j["lang"] in ("en", "de")),
]


def candidates(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """One posting per duplicate group, with a real description."""
    rows = conn.execute(
        f"""SELECT j.* FROM jobs j JOIN job_groups g ON g.job_id = j.id
            WHERE j.source IN ({",".join("?" * len(SOURCES))})
              AND length(j.description) >= 400
              AND j.id = g.group_id
            ORDER BY j.id""",
        SOURCES,
    ).fetchall()
    jobs = []
    for r in rows:
        if OUT_OF_SCOPE.search(f"{r['location']} {r['title']}"):
            continue
        job = dict(r)
        job["target"] = bool(TARGET_TITLE.search(r["title"]))
        job["senior"] = bool(SENIOR_TITLE.search(r["title"]))
        job["junior"] = bool(JUNIOR_TITLE.search(r["title"]))
        job["german"] = bool(MENTIONS_GERMAN.search(r["description"]))
        jobs.append(job)
    return jobs


def sample(jobs: list[dict[str, Any]], rng: random.Random) -> list[tuple[str, dict[str, Any]]]:
    picked: list[tuple[str, dict[str, Any]]] = []
    used: set[int] = set()
    for name, n, predicate in STRATA:
        pool = [j for j in jobs if j["id"] not in used and predicate(j)]
        for job in rng.sample(pool, min(n, len(pool))):
            used.add(job["id"])
            picked.append((name, job))
    return picked


def render(stratum: str, job: dict[str, Any]) -> str:
    header = {
        "source": job["source"],
        "source_job_id": job["source_job_id"],
        "stratum": stratum,
        "title": job["title"],
        "company": job["company"],
        "location": job["location"],
        "url": job["url"],
        "lang": job["lang"],
    }
    description = "\n".join("  " + line if line else "" for line in job["description"].splitlines())
    return (
        LABEL_TEMPLATE
        + "\njob:\n"
        + "\n".join(
            f"  {line}"
            for line in yaml.safe_dump(header, allow_unicode=True, sort_keys=False).splitlines()
        )
        + "\n\ndescription: |\n"
        + description
        + "\n"
    )


def write_sample(conn: sqlite3.Connection, out: Path = LABELS_DIR, force: bool = False) -> int:
    existing = list(out.glob("*.yaml"))
    if existing and not force:
        raise SystemExit(f"{out} already has label files; refusing to overwrite (use --force)")
    for path in existing:  # --force replaces the whole set; file names differ between samples
        path.unlink()
    picked = sample(candidates(conn), random.Random(SEED))
    out.mkdir(parents=True, exist_ok=True)
    for i, (stratum, job) in enumerate(picked, 1):
        slug = re.sub(r"[^\w]+", "-", job["source_job_id"]).strip("-")[:40].lower()
        (out / f"{i:02d}-{job['source']}-{slug}.yaml").write_text(render(stratum, job))
    return len(picked)


def validate(labels: dict[str, Any] | None) -> list[str]:
    """Problems with one file's labels; empty list means complete and valid."""
    if not labels:
        return ["no labels"]
    problems = []
    for field, allowed in ENUMS.items():
        if labels.get(field) not in allowed:
            problems.append(f"{field}={labels.get(field)!r} not in {allowed}")
    for field in BOOLS:
        if not isinstance(labels.get(field), bool):
            problems.append(f"{field} must be true/false")
    for field in INT_OR_NULL:
        value = labels.get(field)
        if value is not None and not isinstance(value, int):
            problems.append(f"{field} must be an integer or empty")
    for field in LISTS:
        if not isinstance(labels.get(field), list):
            problems.append(f"{field} must be a list")
    for item in labels.get("other_languages") or []:
        if not LANGUAGE_ITEM.match(str(item)):
            problems.append(f"other_languages item {item!r} must look like 'Dutch: fluent'")
    expected = set(ENUMS) | set(BOOLS) | set(INT_OR_NULL) | set(LISTS) | set(OPTIONAL)
    if unknown := set(labels) - expected:
        problems.append(f"unknown fields: {sorted(unknown)}")
    return problems


def check(out: Path = LABELS_DIR) -> int:
    """Validate every file; only valid AND human-reviewed files count as done.

    Drafted labels (e.g. pre-filled by an LLM) stay `reviewed: false` until a
    human has checked each field -- otherwise the eval would measure agreement
    between two models rather than correctness.
    """
    files = sorted(out.glob("*.yaml"))
    done = drafted = 0
    for path in files:
        doc = yaml.safe_load(path.read_text())
        problems = validate(doc.get("labels"))
        if problems:
            print(f"{path.name}: " + "; ".join(problems))
        elif doc.get("reviewed") is True:
            done += 1
        else:
            drafted += 1
            print(f"{path.name}: valid, awaiting human review")
    print(
        f"{done}/{len(files)} reviewed, {drafted} drafted, {len(files) - done - drafted} incomplete"
    )
    return 0 if done == len(files) else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m job_search_assistant.evalset")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("sample", help="write eval/labels/*.yaml from data/jobs.db")
    s.add_argument("--force", action="store_true", help="overwrite existing label files")
    sub.add_parser("check", help="validate label files")
    args = parser.parse_args()
    if args.command == "sample":
        n = write_sample(db.connect("data/jobs.db"), force=args.force)
        print(f"wrote {n} files to {LABELS_DIR}/")
    else:
        sys.exit(check())


if __name__ == "__main__":
    main()


# --- Scoring extractions against labels ---------------------------------------------

EXACT_FIELDS = (*ENUMS, *BOOLS, *INT_OR_NULL)
SKILL_FIELDS = ("required_skills", "nice_to_have_skills")


def _norm(item: str) -> str:
    return re.sub(r"[^a-z0-9+#]+", " ", str(item).lower()).strip()


def score(pairs: list[tuple[str, str, dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    """Score (file, labelled_by, labels, predicted) pairs, overall and per labelled_by group.

    Enums/bools/ints: exact match. Salary and restrictions: present-vs-absent match
    (wording varies too much for exact comparison; both lists are kept for reading).
    other_languages: exact set match. Skills: micro precision/recall on normalized
    names, per field and ignoring the required/nice split ("any_skills").
    """
    groups: dict[str, list] = {"all": pairs}
    for pair in pairs:
        groups.setdefault(pair[1], []).append(pair)

    report: dict[str, Any] = {"groups": {}, "mismatches": {}}
    for group, items in groups.items():
        fields: dict[str, Any] = {}
        for field in EXACT_FIELDS:
            hits = [label[field] == pred[field] for _, _, label, pred in items]
            fields[field] = sum(hits) / len(hits)
        for field in ("salary", "restrictions"):
            hits = [bool(label[field]) == bool(pred[field]) for _, _, label, pred in items]
            fields[f"{field}_present"] = sum(hits) / len(hits)
        hits = [
            {_norm(x) for x in label["other_languages"]}
            == {_norm(x) for x in pred["other_languages"]}
            for _, _, label, pred in items
        ]
        fields["other_languages"] = sum(hits) / len(hits)
        for name, keys in (*((f, (f,)) for f in SKILL_FIELDS), ("any_skills", SKILL_FIELDS)):
            tp = fp = fn = 0
            for _, _, label, pred in items:
                gold = {_norm(x) for k in keys for x in label[k]}
                got = {_norm(x) for k in keys for x in pred[k]}
                tp, fp, fn = tp + len(gold & got), fp + len(got - gold), fn + len(gold - got)
            fields[name] = {
                "precision": tp / (tp + fp) if tp + fp else 1.0,
                "recall": tp / (tp + fn) if tp + fn else 1.0,
            }
        report["groups"][group] = {"n": len(items), "fields": fields}

    for field in (*EXACT_FIELDS, "other_languages", "salary", "restrictions", *SKILL_FIELDS):
        wrong = []
        for name, _, label, pred in pairs:
            if field in SKILL_FIELDS:
                gold, got = {_norm(x) for x in label[field]}, {_norm(x) for x in pred[field]}
                if gold != got:
                    wrong.append(
                        {"file": name, "missed": sorted(gold - got), "extra": sorted(got - gold)}
                    )
            elif label[field] != pred[field]:
                wrong.append({"file": name, "label": label[field], "predicted": pred[field]})
        report["mismatches"][field] = wrong
    return report


def format_report(report: dict[str, Any]) -> str:
    names = list(report["groups"])
    header = f"{'field':<26}" + "".join(
        f"{g} (n={report['groups'][g]['n']})".rjust(22) for g in names
    )
    lines = [header, "-" * len(header)]
    for field in report["groups"]["all"]["fields"]:
        cells = []
        for g in names:
            v = report["groups"][g]["fields"][field]
            cells.append(
                f"P {v['precision']:.2f} R {v['recall']:.2f}" if isinstance(v, dict) else f"{v:.0%}"
            )
        lines.append(f"{field:<26}" + "".join(c.rjust(22) for c in cells))
    return "\n".join(lines)
