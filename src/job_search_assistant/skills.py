"""Phase 4: normalize extracted skills into a canonical taxonomy.

    python -m job_search_assistant.skills status   # progress
    python -m job_search_assistant.skills export   # DB -> taxonomy/skills.yaml (for review)
    python -m job_search_assistant.skills import   # reviewed YAML -> DB (YAML wins)

Raw skills are the written forms from the extractions (and my résumé), keyed
lowercase with collapsed whitespace. Each maps to one canonical skill in
`skills` (name, category, parent) or to NULL (dropped). The mapping is made by
an MCP client following eval/SKILLS.md; after my review the YAML file is the
source of truth.
"""

import argparse
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

SPEC = Path("eval/SKILLS.md")
TAXONOMY = Path("taxonomy/skills.yaml")
EXTRA = Path("profile/extra_skills.yaml")  # graph-only skills, see extra_skills()
CATEGORIES = (
    "programming", "cloud", "data_engineering", "database", "ml_ai", "analytics_bi",
    "devops", "software_engineering", "methods", "business_domain",
)  # fmt: skip
SKILL_FIELDS = ("required_skills", "nice_to_have_skills")


class SkillMapping(BaseModel):
    """One raw skill -> canonical skill (or null = drop)."""

    model_config = ConfigDict(extra="forbid")
    raw: str = Field(description="the raw skill exactly as listed by list_unmapped_skills")
    skill: str | None = Field(description="canonical name, or null to drop")
    category: Literal[CATEGORIES] | None = Field(
        default=None, description="required when `skill` is new"
    )
    parent: str | None = Field(default=None, description="broader canonical skill, if any")


def raw_key(text: str) -> str:
    return " ".join(str(text).lower().split())


def resume_skills(resume_text: str) -> list[str]:
    """Skills from the résumé's 'Technical Skills' list (human languages excluded)."""
    section = re.search(r"^## Technical Skills\n(.*?)(?=^## |\Z)", resume_text, re.M | re.S)
    found = []
    for line in (section.group(1) if section else "").splitlines():
        label, _, items = line.lstrip("- ").partition(":")
        if items and label.strip().lower() != "languages":
            found += [s.strip() for s in items.split(",") if s.strip()]
    return found


def extra_skills(yaml_text: str, bullet_ids: set[str]) -> list[str]:
    """Graph-only skills of mine: shown by résumé bullets but kept off the skills list.

    Each entry is {skill, evidence: [bullet ids]}; an unknown id means the résumé changed
    and the claim needs re-checking, so it raises instead of silently keeping the skill.
    """
    entries = yaml.safe_load(yaml_text) or []
    errors, found = [], []
    for e in entries:
        skill, evidence = e.get("skill"), e.get("evidence")
        evidence = [evidence] if isinstance(evidence, str) else evidence or []
        if not skill or not evidence:
            errors.append(f"{e!r}: needs `skill` and `evidence`")
        elif raw_key(skill) in map(raw_key, found):
            errors.append(f"{skill!r}: listed twice")
        elif unknown := [b for b in evidence if b not in bullet_ids]:
            errors.append(f"{skill!r}: no résumé bullet {', '.join(unknown)}")
        else:
            found.append(skill)
    if errors:
        raise ValueError("extra skills: " + "; ".join(errors))
    return found


def load_extra_skills(path: Path = EXTRA, tex: Path | None = None) -> list[str]:
    """extra_skills() from `path`, checked against the bullets of the LaTeX résumé."""
    from job_search_assistant import tailor  # tailor imports this module

    if not path.exists():
        return []
    resume = tailor.parse((tex or tailor.TEX).read_text())
    ids = {f"{e.id}.{i}" for e in resume.entries for i in range(1, len(e.bullets) + 1)}
    return extra_skills(path.read_text(), ids)


def raw_skills(
    conn: sqlite3.Connection,
    extraction: tuple[str, str],
    resume_text: str = "",
    extra: Sequence[str] = (),
) -> dict[str, dict[str, Any]]:
    """raw key -> {display (most common written form), mentions (in postings), in_resume}.

    in_resume: one of my skills (résumé list or `extra`).
    """
    forms: dict[str, Counter] = defaultdict(Counter)
    for field in SKILL_FIELDS:
        rows = conn.execute(
            f"""SELECT v.value FROM extractions e, json_each(e.output_json, '$.{field}') v
                WHERE e.model = ? AND e.prompt_version = ?""",
            extraction,
        )
        for (value,) in rows:
            forms[raw_key(value)][value] += 1
    resume = {raw_key(s): s for s in [*resume_skills(resume_text), *extra]}
    keys = set(forms) | set(resume)
    return {
        k: {
            "display": forms[k].most_common(1)[0][0] if forms[k] else resume[k],
            "mentions": sum(forms[k].values()),
            "in_resume": k in resume,
        }
        for k in sorted(keys)
    }


def canonical_skills(conn: sqlite3.Connection) -> dict[str, dict[str, str | None]]:
    return {
        r["name"]: {"category": r["category"], "parent": r["parent"]}
        for r in conn.execute("SELECT name, category, parent FROM skills ORDER BY name")
    }


def unmapped(conn: sqlite3.Connection, raw: dict[str, dict[str, Any]]) -> list[str]:
    mapped = {r[0] for r in conn.execute("SELECT raw FROM skill_map")}
    return [k for k in raw if k not in mapped]


def save_mappings(
    conn: sqlite3.Connection, mappings: list[SkillMapping], known_raw: set[str]
) -> list[str]:
    """Validate the whole batch; store it only if there are no errors. Returns the errors."""
    skills = canonical_skills(conn)
    mapped = {r[0] for r in conn.execute("SELECT raw FROM skill_map")}
    errors: list[str] = []
    new: dict[str, dict[str, str | None]] = {}
    rows = []
    seen: set[str] = set()

    for m in mappings:
        key = raw_key(m.raw)
        if key not in known_raw:
            errors.append(f"{m.raw!r}: not a raw skill (use list_unmapped_skills)")
            continue
        if key in mapped or key in seen:
            errors.append(f"{m.raw!r}: already mapped")
            continue
        seen.add(key)
        rows.append((key, m.skill))
        if m.skill is None:
            continue
        existing = skills.get(m.skill) or new.get(m.skill)
        if existing is None:
            if m.category is None:
                errors.append(f"{m.raw!r}: new skill {m.skill!r} needs a category")
                continue
            new[m.skill] = {"category": m.category, "parent": m.parent}
        else:
            if m.category and m.category != existing["category"]:
                was = existing["category"]
                errors.append(f"{m.raw!r}: {m.skill!r} is already {was!r}, not {m.category!r}")
            if m.parent and existing["parent"] not in (None, m.parent):
                errors.append(f"{m.raw!r}: {m.skill!r} already has parent {existing['parent']!r}")
            elif m.parent and existing["parent"] is None:
                existing = new.setdefault(m.skill, dict(existing))
                existing["parent"] = m.parent

    # Parents that don't exist yet become skills of their child's category.
    for name, info in list(new.items()):
        parent = info["parent"]
        if parent == name:
            errors.append(f"{name!r}: cannot be its own parent")
        elif parent and parent not in skills and parent not in new:
            new[parent] = {"category": info["category"], "parent": None}
    taxonomy = {**skills, **new}
    for name in new:
        chain, node = {name}, taxonomy[name]["parent"]
        while node:
            if node in chain:
                errors.append(f"{name!r}: parent chain loops through {node!r}")
                break
            chain.add(node)
            node = taxonomy[node]["parent"]
    if errors:
        return errors

    now = datetime.now(UTC).isoformat()
    with conn:
        for name, info in new.items():
            conn.execute(
                """INSERT INTO skills (name, category, parent) VALUES (?, ?, ?)
                   ON CONFLICT (name) DO UPDATE SET parent = excluded.parent""",
                (name, info["category"], info["parent"]),
            )
        conn.executemany(
            "INSERT INTO skill_map (raw, skill, mapped_at) VALUES (?, ?, ?)",
            [(key, skill, now) for key, skill in rows],
        )
    return []


def export_yaml(conn: sqlite3.Connection, raw: dict[str, dict[str, Any]], path: Path) -> int:
    """Write the taxonomy grouped by skill, most-mentioned first (that's the review order)."""
    aliases: dict[str | None, list[str]] = defaultdict(list)
    for key, skill in conn.execute("SELECT raw, skill FROM skill_map ORDER BY raw"):
        aliases[skill].append(key)
    skills = canonical_skills(conn)
    mentions = {s: sum(raw.get(a, {}).get("mentions", 0) for a in aliases[s]) for s in skills}
    doc = {
        "skills": {
            name: {
                "category": skills[name]["category"],
                "parent": skills[name]["parent"],
                "mentions": mentions[name],  # informational; ignored on import
                "aliases": aliases[name],
            }
            for name in sorted(skills, key=lambda s: (-mentions[s], s))
        },
        "dropped": aliases[None],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Canonical skill taxonomy (rules: eval/SKILLS.md). Review top to bottom, edit, then\n"
        "# run `python -m job_search_assistant.skills import`. `mentions` is ignored on import.\n"
    )
    path.write_text(header + yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100))
    return len(skills)


def import_yaml(conn: sqlite3.Connection, path: Path, force: bool = False) -> list[str]:
    """Replace skills and skill_map with the YAML content; returns errors (nothing written).

    Refuses if the DB holds mappings the YAML lacks (made since the last export, e.g. by
    the daily agent): importing would silently delete them. `force` imports anyway.
    """
    doc = yaml.safe_load(path.read_text())
    skills, errors, owner = doc.get("skills") or {}, [], {}
    for name, info in skills.items():
        if info.get("category") not in CATEGORIES:
            errors.append(f"{name!r}: unknown category {info.get('category')!r}")
        if info.get("parent") is not None and info["parent"] not in skills:
            errors.append(f"{name!r}: parent {info['parent']!r} is not a skill")
        for alias in info.get("aliases") or []:
            if raw_key(alias) in owner:
                errors.append(f"alias {alias!r} under both {owner[raw_key(alias)]!r} and {name!r}")
            owner[raw_key(alias)] = name
    for alias in doc.get("dropped") or []:
        if raw_key(alias) in owner:
            errors.append(f"alias {alias!r} is both dropped and under {owner[raw_key(alias)]!r}")
        owner[raw_key(alias)] = None
    newer = sorted({r[0] for r in conn.execute("SELECT raw FROM skill_map")} - set(owner))
    if newer and not force:
        errors.append(
            f"{len(newer)} mapping(s) in the DB are not in {path.name} (e.g. {newer[:5]}); "
            "run `skills export` first, or import with --force to drop them"
        )
    if errors:
        return errors

    now = datetime.now(UTC).isoformat()
    with conn:
        conn.execute("DELETE FROM skill_map")
        conn.execute("DELETE FROM skills")
        conn.executemany(
            "INSERT INTO skills (name, category, parent) VALUES (?, ?, ?)",
            [(n, i["category"], i.get("parent")) for n, i in skills.items()],
        )
        conn.executemany(
            "INSERT INTO skill_map (raw, skill, mapped_at) VALUES (?, ?, ?)",
            [(raw, skill, now) for raw, skill in owner.items()],
        )
    return []


def main() -> None:
    from job_search_assistant import db, search

    parser = argparse.ArgumentParser(prog="python -m job_search_assistant.skills")
    parser.add_argument("command", choices=["status", "export", "import"])
    parser.add_argument("--force", action="store_true", help="import: drop DB-only mappings")
    args = parser.parse_args()
    conn = db.connect("data/jobs.db")
    resume = search.RESUME.read_text() if search.RESUME.exists() else ""
    raw = raw_skills(conn, search.current_extraction(), resume, load_extra_skills())

    if args.command == "import":
        if errors := import_yaml(conn, TAXONOMY, force=args.force):
            raise SystemExit("\n".join(errors))
        print(f"imported {TAXONOMY}")
    elif args.command == "export":
        print(f"wrote {export_yaml(conn, raw, TAXONOMY)} skills to {TAXONOMY}")
    left = unmapped(conn, raw)
    total = sum(r["mentions"] for r in raw.values())
    covered = total - sum(raw[k]["mentions"] for k in left)
    n_skills = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
    print(
        f"{len(raw) - len(left)}/{len(raw)} raw skills mapped "
        f"({covered / total:.0%} of mentions) into {n_skills} canonical skills"
    )


if __name__ == "__main__":
    main()
