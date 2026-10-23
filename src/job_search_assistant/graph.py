"""Phase 4: knowledge graph of jobs, skills, companies and me, in Neo4j.

    python -m job_search_assistant.graph build                 # rebuild from SQLite
    python -m job_search_assistant.graph gaps [--scope target]  # skills I'm missing
    python -m job_search_assistant.graph unlock|coverage|roles  # other gap queries
    python -m job_search_assistant.graph fit --job 4187         # one job vs my skills

    (Job)-[:REQUIRES {required}]->(Skill)-[:BROADER]->(Skill)
    (Job)-[:POSTED_BY]->(Company)   (Job)-[:IS_ROLE]->(Role)
    (Skill)-[:IN_CATEGORY]->(Category)   (Me)-[:HAS_SKILL]->(Skill)

The graph is a derived projection of SQLite (extractions + skill taxonomy +
résumé), rebuilt from scratch on every build, like `job_groups`.

Credit for a required skill S: full if I have S or anything narrower than S
(PySpark covers Apache Spark); half if I only have something broader (Python
for pandas): see "Libraries: language or field?" in eval/SKILLS.md.
"""

import argparse
import json
import os
import sqlite3
from collections import Counter, defaultdict
from typing import Any

from job_search_assistant import dedup, search, skills

SCOPES = {"eligible": "eligible", "target": "is_target"}  # CLI/tool name -> Job property

# My credit for skill s: 1.0 if I have s or a narrower skill, 0.5 if only a broader one.
CREDIT = """
    CASE
      WHEN EXISTS { (me)-[:HAS_SKILL]->(:Skill)-[:BROADER*0..4]->(s) } THEN 1.0
      WHEN EXISTS { (me)-[:HAS_SKILL]->(:Skill)<-[:BROADER*1..4]-(s) } THEN 0.5
      ELSE 0.0
    END"""

QUERIES: dict[str, str] = {
    # Skills I lack, weighted: required 1.0, nice-to-have 0.5, halved if I have a broader one.
    "gaps": f"""
        MATCH (me:Me), (j:Job)-[r:REQUIRES]->(s:Skill) WHERE j[$scope]
        WITH me, s, r, {CREDIT} AS credit
        WHERE credit < 1.0
        RETURN s.name AS skill, s.category AS category, credit AS my_credit,
               count(CASE WHEN r.required THEN 1 END) AS required_by,
               count(CASE WHEN NOT r.required THEN 1 END) AS nice_in,
               round(sum(CASE WHEN r.required THEN 1.0 ELSE 0.5 END) * (1 - credit), 1) AS score
        ORDER BY score DESC, skill LIMIT $k""",
    # Jobs where exactly one required skill is missing: learning it makes me fully qualified.
    "unlock": f"""
        MATCH (me:Me), (j:Job)-[:REQUIRES {{required: true}}]->(s:Skill) WHERE j[$scope]
        WITH j, collect(CASE WHEN {CREDIT} < 1.0 THEN s.name END) AS missing
        WHERE size(missing) = 1
        RETURN missing[0] AS skill, count(j) AS unlocks, collect(j.title)[..5] AS jobs
        ORDER BY unlocks DESC, skill LIMIT $k""",
    # Share of each job's required skills I cover (partial credit counts half).
    "coverage": f"""
        MATCH (me:Me), (j:Job)-[:REQUIRES {{required: true}}]->(s:Skill) WHERE j[$scope]
        WITH j, count(s) AS required, sum({CREDIT}) AS covered,
             collect(CASE WHEN {CREDIT} < 1.0 THEN s.name END) AS missing
        MATCH (j)-[:POSTED_BY]->(c:Company)
        RETURN j.id AS job_id, j.title AS title, c.name AS company, required,
               round(covered / required, 2) AS coverage, missing
        ORDER BY coverage DESC, required DESC LIMIT $k""",
    # Most-required skills per role family across the market.
    "roles": """
        MATCH (role:Role)<-[:IS_ROLE]-(j:Job)-[:REQUIRES {required: true}]->(s:Skill)
        WHERE j[$scope]
        WITH role, s, count(j) AS jobs ORDER BY jobs DESC
        WITH role, collect(s.name + ' (' + toString(jobs) + ')')[..$k] AS top_skills,
             sum(jobs) AS weight
        RETURN role.name AS role, top_skills ORDER BY weight DESC""",
    # Skills that appear in the same postings as $skill.
    "together": """
        MATCH (:Skill {name: $skill})<-[:REQUIRES]-(j:Job)-[:REQUIRES]->(b:Skill)
        WHERE j[$scope]
        RETURN b.name AS skill, count(j) AS jobs ORDER BY jobs DESC, skill LIMIT $k""",
    # One job ($job_id) skill by skill: my credit (as in CREDIT) and which of my skills gives it.
    "fit": """
        MATCH (me:Me), (:Job {id: $job_id})-[r:REQUIRES]->(s:Skill)
        OPTIONAL MATCH (me)-[:HAS_SKILL]->(n:Skill)-[:BROADER*0..4]->(s)
        WITH me, r, s, collect(DISTINCT n.name) AS same_or_narrower
        OPTIONAL MATCH (me)-[:HAS_SKILL]->(b:Skill)<-[:BROADER*1..4]-(s)
        WITH r, s, same_or_narrower, collect(DISTINCT b.name) AS broader
        RETURN s.name AS skill, s.category AS category, r.required AS required,
               CASE WHEN size(same_or_narrower) > 0 THEN 1.0
                    WHEN size(broader) > 0 THEN 0.5 ELSE 0.0 END AS my_credit,
               CASE WHEN size(same_or_narrower) > 0 THEN same_or_narrower
                    ELSE broader END AS via
        ORDER BY required DESC, my_credit, skill""",
}

# Subgraphs worth looking at in the Neo4j Browser (http://localhost:7474). Paste one into
# the query bar. The whole graph (~1.7k nodes) is too big to read.
VIEWS: dict[str, str] = {
    "schema": "CALL db.schema.visualization()",
    "my_skills": """MATCH p = (:Me)-[:HAS_SKILL]->(:Skill)-[:IN_CATEGORY]->(:Category)
                    RETURN p""",
    "my_skill_tree": """MATCH p = (:Me)-[:HAS_SKILL]->(:Skill)-[:BROADER*0..3]->(:Skill)
                        RETURN p""",
    # Required skills of my eligible jobs that I don't cover (red flags of the job market).
    "eligible_gaps": """MATCH (me:Me),
                              p = (:Job {eligible: true})-[:REQUIRES {required: true}]->(s:Skill)
                        WHERE NOT EXISTS { (me)-[:HAS_SKILL]->(:Skill)-[:BROADER*0..4]->(s) }
                        RETURN p""",
    # One job against my skills: what I bring and what's missing (replace the id).
    "job_vs_me": """MATCH (j:Job {id: 4187})-[r:REQUIRES]->(s:Skill)
                    OPTIONAL MATCH m = (:Me)-[:HAS_SKILL]->(:Skill)-[:BROADER*0..4]->(s)
                    RETURN j, r, s, m""",
    "cloud_hierarchy": """MATCH p = (:Skill)-[:BROADER*1..2]->(:Skill {name: 'Cloud Computing'})
                          RETURN p""",
    # Which companies want the skills I'm missing most often.
    "companies_for_aws": """MATCH p = (:Company)<-[:POSTED_BY]-(:Job {is_target: true})
                                  -[:REQUIRES {required: true}]->(:Skill {name: 'AWS'})
                            RETURN p""",
}


def graph_rows(
    conn: sqlite3.Connection, extraction: tuple[str, str], resume_text: str
) -> dict[str, Any]:
    """Everything the graph needs, read from SQLite (pure function, no Neo4j)."""
    skill_of = dict(conn.execute("SELECT raw, skill FROM skill_map"))
    taxonomy = skills.canonical_skills(conn)
    eligible = search.eligible_job_ids(conn, search.MY_FILTERS, extraction)

    jobs, requires, names = [], {}, defaultdict(Counter)
    rows = conn.execute(
        """SELECT j.*, e.output_json FROM extractions e JOIN jobs j ON j.id = e.job_id
           WHERE e.model = ? AND e.prompt_version = ?""",
        extraction,
    )
    for r in rows:
        x = json.loads(r["output_json"])
        company = dedup.normalize_company(r["company"]) or r["company"]
        names[company][r["company"]] += 1
        jobs.append(
            {
                "id": r["id"],
                "company": company,
                "role": x["role_family"],
                "props": {
                    "title": r["title"],
                    "url": r["url"],
                    "location": r["location"],
                    "source": r["source"],
                    "seniority": x["seniority"],
                    "german_level": x["german_level"],
                    "workplace": x["workplace"],
                    "years_min": x["years_experience_min"],
                    "is_target": x["is_target_role"],
                    "eligible": r["id"] in eligible,
                },
            }
        )
        # Several raw forms can map to one skill; required wins over nice-to-have.
        for field, required in (("nice_to_have_skills", False), ("required_skills", True)):
            for raw in x[field]:
                if skill := skill_of.get(skills.raw_key(raw)):
                    key = (r["id"], skill)
                    requires[key] = requires.get(key, False) or required

    my_skills = {skill_of.get(skills.raw_key(s)) for s in skills.resume_skills(resume_text)}
    return {
        "categories": sorted({info["category"] for info in taxonomy.values()}),
        "skills": [{"name": n, **info} for n, info in taxonomy.items()],
        "companies": [{"key": k, "name": c.most_common(1)[0][0]} for k, c in names.items()],
        "jobs": jobs,
        "requires": [
            {"job_id": j, "skill": s, "required": req} for (j, s), req in sorted(requires.items())
        ],
        "my_skills": sorted(s for s in my_skills if s),
    }


BUILD = [
    "MATCH (n) DETACH DELETE n",
    "UNWIND $categories AS name CREATE (:Category {name: name})",
    """UNWIND $skills AS s
       MATCH (c:Category {name: s.category})
       CREATE (:Skill {name: s.name, category: s.category})-[:IN_CATEGORY]->(c)""",
    """UNWIND $skills AS s WITH s WHERE s.parent IS NOT NULL
       MATCH (child:Skill {name: s.name}), (parent:Skill {name: s.parent})
       CREATE (child)-[:BROADER]->(parent)""",
    "UNWIND $companies AS c CREATE (:Company {key: c.key, name: c.name})",
    """UNWIND $jobs AS j
       MATCH (c:Company {key: j.company})
       MERGE (role:Role {name: j.role})
       CREATE (job:Job {id: j.id})-[:POSTED_BY]->(c)
       SET job += j.props
       CREATE (job)-[:IS_ROLE]->(role)""",
    """UNWIND $requires AS e
       MATCH (j:Job {id: e.job_id}), (s:Skill {name: e.skill})
       CREATE (j)-[:REQUIRES {required: e.required}]->(s)""",
    """CREATE (me:Me {name: 'me'}) WITH me
       UNWIND $my_skills AS name MATCH (s:Skill {name: name})
       CREATE (me)-[:HAS_SKILL]->(s)""",
]
KEYS = (("Job", "id"), ("Skill", "name"), ("Company", "key"), ("Role", "name"),
        ("Category", "name"))  # fmt: skip
CONSTRAINTS = [
    f"CREATE CONSTRAINT {label.lower()}_{key} IF NOT EXISTS "
    f"FOR (n:{label}) REQUIRE n.{key} IS UNIQUE"
    for label, key in KEYS
]


def connect_neo4j():
    from dotenv import load_dotenv
    from neo4j import GraphDatabase

    load_dotenv()
    uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
    auth = (os.environ.get("NEO4J_USER", "neo4j"), os.environ["NEO4J_PASSWORD"])
    driver = GraphDatabase.driver(uri, auth=auth)
    driver.verify_connectivity()
    return driver


def build(driver, rows: dict[str, Any]) -> dict[str, int]:
    """Replace the whole graph with `rows` in one transaction; returns node counts."""
    for statement in CONSTRAINTS:  # schema changes can't share a transaction with writes
        driver.execute_query(statement)
    with driver.session() as session:
        session.execute_write(lambda tx: [tx.run(q, **rows).consume() for q in BUILD])
    records, _, _ = driver.execute_query(
        "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS n ORDER BY label"
    )
    return {r["label"]: r["n"] for r in records}


def query(driver, name: str, scope: str = "eligible", k: int = 15, **params) -> list[dict]:
    records, _, _ = driver.execute_query(QUERIES[name], scope=SCOPES[scope], k=k, **params)
    return [r.data() for r in records]


def main() -> None:
    from job_search_assistant import db

    parser = argparse.ArgumentParser(prog="python -m job_search_assistant.graph")
    parser.add_argument("command", choices=["build", *QUERIES])
    parser.add_argument("--scope", choices=list(SCOPES), default="eligible")
    parser.add_argument("-k", type=int, default=15)
    parser.add_argument("--skill", help="for `together`")
    parser.add_argument("--job", type=int, help="job id, for `fit`")
    args = parser.parse_args()
    if (args.command == "fit") != (args.job is not None):
        parser.error("--job goes with `fit` (and only with it)")

    with connect_neo4j() as driver:
        if args.command == "build":
            conn = db.connect("data/jobs.db")
            rows = graph_rows(conn, search.current_extraction(), search.RESUME.read_text())
            print(build(driver, rows))
            print(
                f"{len(rows['requires'])} REQUIRES edges, {len(rows['my_skills'])} skills of mine"
            )
            return
        extra = {"skill": args.skill} if args.command == "together" else {}
        if args.command == "fit":
            extra = {"job_id": args.job}
        for row in query(driver, args.command, args.scope, args.k, **extra):
            print(row)


if __name__ == "__main__":
    main()
