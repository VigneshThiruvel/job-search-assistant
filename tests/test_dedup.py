from job_search_assistant import db, dedup
from job_search_assistant.dedup import (
    companies_compatible,
    find_groups,
    normalize_city,
    normalize_company,
)


def test_normalize_city() -> None:
    assert normalize_city("Berlin, Germany") == "berlin"
    assert normalize_city("Greater Munich Metropolitan Area") == "münchen"
    assert normalize_city("München, München (Kreis)") == "münchen"
    assert normalize_city("Nürnberg, Mittelfranken") == "nürnberg"
    assert normalize_city("Frankfurt Rhine-Main Metropolitan Area") == "frankfurt"
    assert normalize_city("Frankfurt am Main, Hessen") == "frankfurt"
    assert normalize_city("Germany") == normalize_city("Deutschland") == normalize_city(None) == ""


def test_normalize_company() -> None:
    assert normalize_company("ABOUT YOU SE & Co. KG") == "about you"
    assert normalize_company("Reply Deutschland SE") == "reply"
    assert normalize_company("GmbH") == "gmbh"  # never reduced to nothing


def test_companies_compatible() -> None:
    assert companies_compatible("rehau", "rehau industries")
    assert companies_compatible("1komma5", "1komma5grad")
    assert not companies_compatible("sap", "sapient")  # too short to trust a prefix
    assert not companies_compatible("just eat", "sumup")


def groups_of(rows):
    groups = find_groups(rows)
    clusters: dict[int, set[int]] = {}
    for job_id, group_id in groups.items():
        clusters.setdefault(group_id, set()).add(job_id)
    return sorted(sorted(c) for c in clusters.values())


def test_matches_across_spelling_variants() -> None:
    rows = [
        (1, "Junior Data Analyst (m/w/d)", "Arvato SE", "Harsewinkel"),
        (2, "Junior Data Analyst (f/m/x)", "Arvato", "Harsewinkel, Germany"),
        (3, "Data Engineer (w|m|d)", "REHAU Industries SE & Co. KG", "Erlangen"),
        (4, "Data Engineer", "Rehau", "Erlangen"),
    ]
    assert groups_of(rows) == [[1, 2], [3, 4]]


def test_same_title_different_cities_stay_separate() -> None:
    rows = [
        (1, "IT Consultant (w|m|d)", "zeb", "Berlin, Deutschland"),
        (2, "IT Consultant (w|m|d)", "zeb", "Hamburg, Deutschland"),
    ]
    assert groups_of(rows) == [[1], [2]]


def test_country_only_joins_a_single_city_but_not_an_ambiguous_one() -> None:
    one_city = [
        (1, "Data Analyst", "FINN", "Munich"),
        (2, "Data Analyst", "FINN GmbH", "Germany"),
    ]
    assert groups_of(one_city) == [[1, 2]]

    two_cities = [*one_city, (3, "Data Analyst", "FINN", "Berlin")]
    assert groups_of(two_cities) == [[1], [2], [3]]


def test_rebuild_groups_counts_multi_source_groups() -> None:
    conn = db.connect(":memory:")
    for i, (source, location) in enumerate(
        [("a", "Berlin"), ("b", "Berlin, Germany"), ("c", "Köln")]
    ):
        conn.execute(
            """INSERT INTO jobs (source, source_job_id, url, title, company, location,
               description, first_seen, last_seen, content_hash, raw_json)
               VALUES (?, ?, '', 'Data Analyst', 'ACME', ?, '', '', '', '', '{}')""",
            (source, str(i), location),
        )
    assert dedup.rebuild_groups(conn) == 1
    assert dedup.rebuild_groups(conn) == 1  # idempotent
