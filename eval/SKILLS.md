# Skill normalization rules

Phase 2 extracted skills *as written* ("Datenmodellierung", "PySpark", "AWS Glue").
This file defines how each written form (a **raw** skill) maps to one **canonical**
skill, so the knowledge graph has one node per real skill. It is the spec for the
mapping step (Claude Code via MCP) and for my review of `taxonomy/skills.yaml`.

## Canonical names

- **English, official spelling** of the tool or method: `Apache Spark`, `PostgreSQL`,
  `Power BI`, `scikit-learn`, `Data Modeling`, `Machine Learning`.
- **Merge** spelling, casing, language and abbreviation variants:
  `Datenmodellierung`, `data modelling`, `datamodellering` → `Data Modeling`;
  `K8s` → `Kubernetes`; `Pyspark` → `PySpark`; `ML` → `Machine Learning`.
- **Keep distinct tools distinct**, even when related: `Apache Spark` ≠ `Databricks`,
  `Docker` ≠ `Kubernetes`, `Power BI` ≠ `Tableau`, `PostgreSQL` ≠ `SQL`.
- **Reuse before creating.** Always check the existing canonical skills first; a new
  name only when no existing one means the same thing.

## Parents (hierarchy)

`parent` = the broader skill a specific one belongs to, one level up. Only for real
"is a kind of / is part of" relations:

- Sub-products and services: `AWS Glue`, `AWS S3`, `AWS SageMaker` → parent `AWS`;
  `Azure Data Factory` → `Azure`; `BigQuery` → `Google Cloud`.
- Libraries and dialects: `PySpark`, `Spark SQL`, `Spark Streaming` → `Apache Spark`;
  `pandas` → `Python`; `T-SQL` → `SQL`.
- Specialisations: `Dimensional Modeling` → `Data Modeling`;
  `LLM Fine-tuning` → `Large Language Models`; `Time Series Analysis` → `Statistics`.

**Libraries: language or field?** A parent gives partial credit in gap queries, so pick
the parent whose knowledge gets you closest to the child:

- General-purpose libraries and app/web frameworks → their **language**, because a fluent
  user of the language picks them up quickly: `pandas`, `NumPy`, `Pydantic`, `FastAPI`,
  `Streamlit` → `Python`; `Shiny` → `R`; `Spring Boot` → `Java`.
- Libraries whose hard part is a **field** → that field, because knowing the language
  doesn't give you the field: `PyTorch` → `Deep Learning`, `scikit-learn` → `Machine
  Learning`, `Matplotlib` → `Data Visualization`, `Dask` → `Distributed Computing`.

"Written in Python" is a different relation (uses-language), not a parent; the graph
can add it as its own edge later.

No parent when unsure. Never a parent from a different category unless the relation
is obvious (`PySpark` → `Apache Spark` is fine).

## Categories

Each canonical skill gets exactly one:

| category | examples |
|---|---|
| `programming` | Python, SQL, R, Java, Scala, TypeScript, Bash |
| `cloud` | AWS, Azure, Google Cloud, AWS S3, Snowflake (managed platform) |
| `data_engineering` | Apache Spark, Kafka, Airflow, dbt, ETL, Data Pipelines, Data Modeling |
| `database` | PostgreSQL, MySQL, MongoDB, Vector Databases, Data Warehousing |
| `ml_ai` | Machine Learning, PyTorch, Large Language Models, RAG, MLOps, Computer Vision |
| `analytics_bi` | Power BI, Tableau, Excel, Looker, Data Visualization, Reporting |
| `devops` | Docker, Kubernetes, CI/CD, Terraform, Git, Linux, Monitoring |
| `software_engineering` | REST APIs, Microservices, Testing, System Design, React |
| `methods` | Statistics, A/B Testing, Causal Inference, Forecasting, Optimization |
| `business_domain` | SAP, Salesforce, Finance, Supply Chain, GDPR, Agile, Scrum |

## Drop (map to nothing)

Map a raw skill to `null` when it is not a learnable technical skill or too vague to
match against anything: soft skills that slipped through ("Stakeholdermanagement",
"communication"), bare words ("data", "modelling", "models", "tools"), and
certifications listed as such ("AWS certification" → `AWS`, keep; "certifications" →
drop). When a vague phrase clearly points to one skill, map it there instead
("KI-Modelle" → `Machine Learning`, "statistische Modelle" → `Statistical Modeling`).

## Granularity

Prefer the level a job seeker would list on a résumé. "dimensionale Datenmodellierung"
→ `Dimensional Modeling` (parent `Data Modeling`) is useful; "Modellierung analytischer
Datenstrukturen" → `Data Modeling` (no new node for a paraphrase).
