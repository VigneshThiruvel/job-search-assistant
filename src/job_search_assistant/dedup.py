"""Cross-source duplicate detection.

`content_hash` is frozen (XING ids are derived from it), so matching uses its
own, looser normalization and stores the result in the derived `job_groups`
table, rebuilt from scratch after every ingest.

Rule: two postings are the same job if their normalized titles are equal,
their companies are compatible (equal, or one name extends the other:
"rehau" / "rehau industries"), and their locations are compatible -- the same
city, or one side names only a country/region. A country-only posting joins a
city only when that is unambiguous (exactly one city among the candidates).
"""

import re
import sqlite3
from collections import defaultdict

from job_search_assistant.text import normalize_for_hash

_LEGAL_FORMS = {
    "gmbh", "mbh", "ag", "se", "kg", "kgaa", "co", "ug", "haftungsbeschränkt", "ohg", "ev",
    "ltd", "limited", "inc", "llc", "plc", "sarl", "sa", "sas", "bv", "nv", "srl", "spa", "ab",
    "as", "aps", "oy", "gruppe", "group", "deutschland", "germany", "dach",
}  # fmt: skip

_CITY_ALIASES = {
    "munich": "münchen",
    "muenchen": "münchen",
    "cologne": "köln",
    "koeln": "köln",
    "nuremberg": "nürnberg",
    "nuernberg": "nürnberg",
    "frankfurt am main": "frankfurt",
    "vienna": "wien",
    "zurich": "zürich",
    "zuerich": "zürich",
    "geneva": "genf",
    "genève": "genf",
    "hanover": "hannover",
    "dusseldorf": "düsseldorf",
    "duesseldorf": "düsseldorf",
}

# Locations that name no city: treated as "unknown", compatible with any city.
_NON_CITIES = {
    "", "germany", "deutschland", "austria", "österreich", "switzerland", "schweiz", "suisse",
    "netherlands", "nederland", "niederlande", "europe", "eu", "emea", "dach", "remote",
    "worldwide", "anywhere", "home office", "homeoffice",
}  # fmt: skip


def normalize_title(title: str) -> str:
    return normalize_for_hash(title)


def normalize_company(company: str) -> str:
    words = normalize_for_hash(company).split()
    kept = [w for w in words if w not in _LEGAL_FORMS]
    return " ".join(kept or words)  # never reduce a name to nothing


def normalize_city(location: str | None) -> str:
    """'Berlin, Germany' -> 'berlin'; 'Greater Munich Metropolitan Area' -> 'münchen';
    'Germany' -> '' (no city)."""
    if not location:
        return ""
    city = location.split(",")[0].lower()
    city = re.sub(r"\(.*?\)", " ", city)
    city = re.sub(r"\b(greater|metropolitan area|area|region|rhine-main|kanton)\b", " ", city)
    city = re.sub(r"\s+", " ", city).strip(" -")
    city = _CITY_ALIASES.get(city, city)
    return "" if city in _NON_CITIES else city


def companies_compatible(a: str, b: str) -> bool:
    """Equal, or the shorter (>= 4 chars, spaces ignored) is a prefix of the longer."""
    a, b = sorted((a.replace(" ", ""), b.replace(" ", "")), key=len)
    return a == b or (len(a) >= 4 and b.startswith(a))


def find_groups(rows: list[tuple[int, str, str, str | None]]) -> dict[int, int]:
    """Map job id -> group id (smallest id in its group) for (id, title, company, location)."""
    by_title: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for job_id, title, company, location in rows:
        by_title[normalize_title(title)].append(
            (job_id, normalize_company(company), normalize_city(location))
        )

    groups: dict[int, int] = {}
    for members in by_title.values():
        for company_members in _cluster_companies(members):
            _group_by_city(company_members, groups)
    return groups


def _cluster_companies(members: list[tuple[int, str, str]]) -> list[list[tuple[int, str]]]:
    """Split one title block into clusters of compatible company names."""
    clusters: list[tuple[str, list[tuple[int, str]]]] = []
    # Shortest names first, so "rehau" becomes the cluster key for "rehau industries".
    for job_id, company, city in sorted(members, key=lambda m: len(m[1])):
        for key, cluster in clusters:
            if companies_compatible(key, company):
                cluster.append((job_id, city))
                break
        else:
            clusters.append((company, [(job_id, city)]))
    return [cluster for _, cluster in clusters]


def _group_by_city(members: list[tuple[int, str]], groups: dict[int, int]) -> None:
    by_city: dict[str, list[int]] = defaultdict(list)
    for job_id, city in members:
        by_city[city].append(job_id)
    unknown = by_city.pop("", [])
    if len(by_city) == 1:  # one city: country-only postings join it
        (ids,) = by_city.values()
        by_city = {"": ids + unknown}
    elif unknown:  # no city at all, or ambiguous: unknowns group only with each other
        by_city["\0unknown"] = unknown
    for ids in by_city.values():
        for job_id in ids:
            groups[job_id] = min(ids)


def rebuild_groups(conn: sqlite3.Connection) -> int:
    """Recompute `job_groups`; returns the number of groups spanning several sources."""
    rows = conn.execute("SELECT id, title, company, location FROM jobs").fetchall()
    groups = find_groups([tuple(r) for r in rows])
    with conn:
        conn.execute("DELETE FROM job_groups")
        conn.executemany("INSERT INTO job_groups (job_id, group_id) VALUES (?, ?)", groups.items())
    return conn.execute(
        """SELECT COUNT(*) FROM (
               SELECT g.group_id FROM job_groups g JOIN jobs j ON j.id = g.job_id
               GROUP BY g.group_id HAVING COUNT(DISTINCT j.source) > 1)"""
    ).fetchone()[0]
